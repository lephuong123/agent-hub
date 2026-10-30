"""Tự resume có giới hạn run ad-hoc kết thúc vì lỗi TẠM THỜI + thống kê run hay treo (docs/dev-notes/run-auto-resume.md).

Chính sách (`hub.auto_resume`, listener bus):
- Nguyên nhân tạm thời (`transient_cause`) — CHỈ theo danh sách cho phép, phần còn lại không bao giờ tự resume:
  `crashed` (tiến trình bị giết/chết) → `process_killed`, `timed_out` → `timeout`, `rate_limited` → `rate_limit`,
  `failed` có lỗi mạng/API 5xx (`NETWORK_PATTERNS`) → `network`. `interrupted` (daemon khởi động lại) do `recovery.py` lo
  (resume ngay khi khởi động, Q-09) nhưng dùng CHUNG trần số lần ở đây. Không bao giờ: `budget_exceeded`, `blocked`,
  `cancelled` (người dừng/ESTOP), `protocol_violation`, `failed` khác (guard chặn, chưa đăng nhập…).
- Phạm vi: run không gắn task, `kind ∈ recovery.auto_resume_kinds` (mặc định `adhoc`). Run của task đã có retry `--resume`
  của dispatcher (FR-TK-05, circuit breaker) — không chạy hai cơ chế chồng nhau.
- Trần: `recovery.auto_resume_max_attempts` lần liên tiếp theo chuỗi `parent_run_id` (đếm các run có `resume_reason`
  `auto_resume:*` hoặc `daemon_restart`). Hết lượt → event `run.auto_resume{status: exhausted}` + audit, dừng.
- Backoff: `auto_resume_backoff_s * 2^attempt` (trần `auto_resume_backoff_max_s`); rate limit thì chờ tới thời điểm reset
  đã biết (`dispatcher._run_reset`). Lịch hẹn lưu ở `hub_state.auto_resume_pending` → daemon khởi động lại vẫn chạy tiếp.
- Lúc tới hạn kiểm lại: ESTOP, agent tạm dừng/đã xoá, session đang bận, run đã được tiếp tục (`continued_in_run_id`) →
  bỏ (idempotent: daemon là luồng đơn, kiểm-tạo-ghi không có `await` xen giữa). Ngân sách/đồng thời: `RunManager.submit`
  (chặn ngân sách → `skipped`), run mới vào hàng đợi chung.
- Mọi bước phát event `run.auto_resume{status: scheduled|started|skipped|exhausted}` trên run CŨ + audit `run.auto_resume`.

`hang_stats(conn, since)`: run `timed_out|interrupted|crashed` theo agent, kind, tool đang chạy dở (tool.start cuối không có
tool.complete), khoảng thời lượng — cho thẻ Sức khoẻ (RPC `report.hangs`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .errors import HubError
from .events import HubEvent, make_event

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger("cfharness.auto_resume")

OUTCOME_CAUSE = {"crashed": "process_killed", "timed_out": "timeout", "rate_limited": "rate_limit"}
NETWORK_PATTERNS = re.compile(
    r"ECONNRESET|ETIMEDOUT|ECONNREFUSED|EAI_AGAIN|ENOTFOUND|socket hang up|fetch failed|network error|connection (?:error|reset|refused)"
    r"|overloaded|API Error: 5\d\d|\b50[234]\b.*(?:gateway|unavailable)",
    re.I,
)
AUTO_REASON = "auto_resume:"
HANG_OUTCOMES = ("timed_out", "interrupted", "crashed")

AUTO_RESUME_PROMPT = (
    "[cfharness] Your previous turn ended early because of a transient problem ({cause}); the hub resumed this session automatically. "
    "Continue the task from where you left off. Re-check the current state before repeating any action that may already have run."
)


def transient_cause(run: dict[str, Any]) -> str | None:
    """Nguyên nhân tạm thời của run đã kết thúc, None nếu KHÔNG được tự resume (danh sách cho phép, fail-closed)."""
    oc = run.get("outcome") or run.get("status")
    if oc == "interrupted":
        return "daemon_restart"
    if oc in OUTCOME_CAUSE:
        return OUTCOME_CAUSE[oc]
    if oc == "failed" and NETWORK_PATTERNS.search(f"{run.get('error') or ''} {run.get('summary') or ''}"):
        return "network"
    return None


def chain_attempts(store: Any, run: dict[str, Any], limit: int = 50) -> int:
    """Số lần tự resume liên tiếp đã làm để đi tới `run` (đi ngược `parent_run_id`, dừng ở run không do tự resume)."""
    n, cur = 0, run
    for _ in range(limit):
        reason = cur.get("resume_reason") or ""
        if not (reason.startswith(AUTO_REASON) or reason == "daemon_restart") or not cur.get("parent_run_id"):
            break
        n += 1
        cur = store.get_run(cur["parent_run_id"]) or {}
    return n


class AutoResumer:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._handles: dict[str, asyncio.TimerHandle] = {}
        hub.bus.add_listener(self._on_bus)

    def cfg(self, key: str) -> Any:
        return self.hub.config.get(f"recovery.{key}")

    def _event(self, run: dict[str, Any], status: str, **data: Any) -> None:
        from .approvals import audit_record

        payload = {"status": status, **data}
        self.hub.publish(make_event("run.auto_resume", run["agent_id"], payload, run_id=run["id"], session_id=run.get("session_id")))
        audit_record("run.auto_resume", run["agent_id"], run["id"], {**payload, "actor": "hub", "channel": "hub"})

    def exhausted(self, run: dict[str, Any], cause: str) -> bool:
        """Đã hết lượt tự resume cho chuỗi của `run` → phát event + audit `exhausted`, trả True. Dùng chung với recovery.py."""
        mx = int(self.cfg("auto_resume_max_attempts") or 0)
        att = chain_attempts(self.hub.store, run)
        if att < mx:
            return False
        self._event(run, "exhausted", cause=cause, attempt=att, max_attempts=mx)
        return True

    # -- listener
    def _on_bus(self, ev: HubEvent) -> None:
        if ev.type != "run.failed" or not ev.run_id or ev.source != "hub":
            return
        try:
            asyncio.get_running_loop().call_soon(self.consider, ev.run_id)
        except RuntimeError:
            pass

    def consider(self, run_id: str) -> str | None:
        """Quyết định cho run vừa kết thúc; trả trạng thái (`scheduled|exhausted|None`)."""
        run = self.hub.store.get_run(run_id)
        if run is None or not self.cfg("auto_resume_enabled") or run.get("task_id") or run.get("continued_in_run_id"):
            return None
        if (run.get("kind") or "adhoc") not in (self.cfg("auto_resume_kinds") or []):
            return None
        cause = transient_cause(run)
        if cause is None or cause == "daemon_restart":
            return None
        if self.exhausted(run, cause):
            return "exhausted"
        att = chain_attempts(self.hub.store, run)
        delay = min(float(self.cfg("auto_resume_backoff_s") or 0) * (2 ** att), float(self.cfg("auto_resume_backoff_max_s") or 0))
        if cause == "rate_limit":
            reset, _ = self.hub.dispatcher._run_reset(run)
            if reset is not None:
                delay = max(delay, (reset - datetime.now(timezone.utc)).total_seconds())
        self._persist(run_id, time.time() + delay)
        self._arm(run_id, delay)
        self._event(run, "scheduled", cause=cause, attempt=att + 1, max_attempts=int(self.cfg("auto_resume_max_attempts") or 0), delay_s=round(delay, 1))
        return "scheduled"

    # -- lịch hẹn (bền qua restart)
    def _pending(self) -> dict[str, float]:
        return dict(self.hub.store.state_get("auto_resume_pending", {}) or {})

    def _persist(self, run_id: str, due: float | None) -> None:
        p = self._pending()
        if due is None:
            p.pop(run_id, None)
        else:
            p[run_id] = due
        self.hub.store.state_set("auto_resume_pending", p)

    def _arm(self, run_id: str, delay: float) -> None:
        old = self._handles.pop(run_id, None)
        if old:
            old.cancel()
        try:
            self._handles[run_id] = asyncio.get_running_loop().call_later(max(0.0, delay), self.fire, run_id)
        except RuntimeError:
            pass

    def start(self) -> None:
        """Hub.start: hẹn lại các lượt resume đã lên lịch trước khi daemon dừng."""
        now = time.time()
        for rid, due in self._pending().items():
            self._arm(rid, float(due) - now)

    def stop(self) -> None:
        for h in self._handles.values():
            h.cancel()
        self._handles.clear()

    def fire(self, run_id: str) -> dict[str, Any] | None:
        self._handles.pop(run_id, None)
        self._persist(run_id, None)
        run = self.hub.store.get_run(run_id)
        if run is None:
            return None
        cause = transient_cause(run) or "unknown"
        reason = self._block_reason(run)
        if reason:
            self._event(run, "skipped", cause=cause, reason=reason)
            return None
        att = chain_attempts(self.hub.store, run) + 1
        rr = f"{AUTO_REASON}{cause}"
        try:
            if run.get("started_at") and run.get("session_id"):
                nr = self.hub.runs.submit(run["agent_id"], AUTO_RESUME_PROMPT.format(cause=cause), cwd=run.get("cwd"), budget_usd=run.get("max_budget_usd"),
                                          model=run.get("model"), kind=run.get("kind") or "adhoc", resume_session_id=run["session_id"],
                                          parent_run_id=run["id"], resume_reason=rr)
            else:
                nr = self.hub.runs.submit(run["agent_id"], run.get("prompt") or "", cwd=run.get("cwd"), budget_usd=run.get("max_budget_usd"),
                                          model=run.get("model"), kind=run.get("kind") or "adhoc", parent_run_id=run["id"], resume_reason=rr)
        except HubError as e:  # ngân sách, ESTOP, chưa đăng nhập… → không resume
            self._event(run, "skipped", cause=cause, reason=e.message)
            return None
        self.hub.store.update_run(run["id"], {"continued_in_run_id": nr["id"]})
        self._event(run, "started", cause=cause, attempt=att, new_run_id=nr["id"])
        self.hub.publish(make_event("run.resumed", run["agent_id"], {"from_run_id": run["id"], "run_id": nr["id"], "reason": rr, "attempt": att},
                                    run_id=run["id"], session_id=run.get("session_id")))
        return nr

    def _block_reason(self, run: dict[str, Any]) -> str | None:
        if not self.cfg("auto_resume_enabled"):
            return "auto_resume tắt"
        if run.get("continued_in_run_id"):
            return f"đã tiếp tục ở run {run['continued_in_run_id']}"
        if self.hub.runs.estop.get("on"):
            return "ESTOP đang bật"
        ag = self.hub.store.get_agent(run["agent_id"])
        if ag is None or ag.get("archived_at") or ag.get("deleted_at") or ag["status"] == "paused":
            return "agent tạm dừng/đã xoá"
        if run.get("session_id"):
            busy = self.hub.store.conn.execute("SELECT id FROM runs WHERE session_id=? AND status IN ('queued','running','waiting_human') LIMIT 1",
                                               (run["session_id"],)).fetchone()
            if busy:
                return f"session đang bận (run {busy[0]})"
        from .workspace import resume_conflicts

        if resume_conflicts(run):
            return "file của run đã bị sửa sau khi dừng"
        return None


# ============================================================================ thống kê run treo / quá giờ
DURATION_BUCKETS = ((60, "<1 phút"), (600, "1–10 phút"), (1800, "10–30 phút"), (3600, "30–60 phút"), (float("inf"), "≥1 giờ"))


def _secs(a: str | None, b: str | None) -> float | None:
    try:
        return (datetime.fromisoformat(b.replace("Z", "+00:00")) - datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds()  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        return None


def _open_tool(conn: Any, run_id: str) -> str | None:
    """Tool đang chạy dở lúc run dừng: tool.start cuối chưa có tool.complete cùng id."""
    done: set[str] = set()
    for typ, p in conn.execute("SELECT type, payload FROM events WHERE run_id=? AND type IN ('tool.start','tool.complete') ORDER BY epoch DESC, seq DESC",
                               (run_id,)):
        try:
            d = json.loads(p) or {}
        except (TypeError, ValueError):
            continue
        tid = d.get("tool_id")
        if typ == "tool.complete":
            done.add(tid)
        elif tid not in done:
            return d.get("name") or "?"
    return None


def hang_stats(conn: Any, since: str, *, top: int = 8) -> dict[str, Any]:
    """Run `timed_out|interrupted|crashed` từ `since`: tổng, theo outcome/agent/kind/tool đang dở/thời lượng + kết quả tự resume."""
    rows = conn.execute(f"SELECT id, agent_id, kind, outcome, started_at, ended_at FROM runs WHERE outcome IN ({','.join('?' * len(HANG_OUTCOMES))}) "
                        "AND queued_at >= ?", (*HANG_OUTCOMES, since)).fetchall()
    by: dict[str, Counter[str]] = {k: Counter() for k in ("outcome", "agent", "kind", "tool", "duration")}
    for r in rows:
        by["outcome"][r["outcome"]] += 1
        by["agent"][r["agent_id"]] += 1
        by["kind"][r["kind"] or "adhoc"] += 1
        by["tool"][_open_tool(conn, r["id"]) or "(không có tool dở)"] += 1
        s = _secs(r["started_at"], r["ended_at"])
        by["duration"][next(lbl for lim, lbl in DURATION_BUCKETS if s is not None and s < lim) if s is not None else "(chưa start)"] += 1
    total = conn.execute("SELECT COUNT(*) FROM runs WHERE queued_at >= ?", (since,)).fetchone()[0]
    auto = Counter(r[0] for r in conn.execute("SELECT resume_reason FROM runs WHERE queued_at >= ? AND (resume_reason LIKE 'auto_resume:%' "
                                              "OR resume_reason='daemon_restart')", (since,)))
    return {"since": since, "hung": len(rows), "runs_total": total,
            **{f"by_{k}": [{"key": kk, "count": v} for kk, v in c.most_common(top)] for k, c in by.items()},
            "auto_resumed": dict(auto)}
