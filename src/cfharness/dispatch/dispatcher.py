"""`TaskDispatcher` — chạy trong daemon (FR-TK-06): tick định kỳ (`tasks.tick_s`, mặc định 5 s) VÀ khi có sự kiện (`kick`).

Mỗi tick:
1. Đối soát task `running`/`review` đang giữ run (lease/heartbeat): run đã kết thúc mà chưa xử lý → xử lý; tiến trình
   `claude` chết theo `(pid, start_time)` quá `tasks.lease_ttl_s` không heartbeat → dừng → `crashed`.
2. `todo → ready` khi mọi cha `done` (FR-TK-04).
3. `triage` → run của lead (`kind=lead`, `--json-schema` kế hoạch) — FR-TK-08a.
4. `review` chưa có run → run của reviewer (`kind=review`, `--json-schema` phán quyết) — FR-TK-12.
5. Claim NGUYÊN TỬ task `ready` (UPDATE … WHERE status='ready' AND current_run_id IS NULL trong transaction) theo
   `priority DESC, created_at`, sau khi kiểm: ESTOP toàn hub/team, ngân sách ngày hub/team/agent (FR-OB-03), agent
   paused/bận, `max_in_progress` của thành viên, sức chứa đồng thời (kể cả điều tiết rate limit FR-RT-16), khoá repo
   (FR-RT-12). Không claim được → task ở `ready` với `wait_reason`.

Kết quả run (listener bus → `call_soon`, idempotent vì chỉ xử lý khi `task.current_run_id == run.id`):
- `completed` (hoặc agent gọi `task_complete`) → `done`, hoặc `review` nếu `review_required`; agent gọi `task_block`
  → `blocked` (run `outcome=blocked`).
- `crashed|timed_out|failed|protocol_violation` → `consecutive_failures+1`; ≥ `failure_limit` → `blocked` +
  `task.circuit_open` (FR-TK-09); còn lượt → `ready`, retry tạo Run mới (`parent_run_id`), `--resume <session cũ>`
  khi outcome là `crashed|timed_out|rate_limited` (FR-TK-05).
- `rate_limited` → requeue với backoff 30 s nhân đôi tới 10 phút, KHÔNG tính lỗi.
- `budget_exceeded` / lỗi auth → `blocked` ngay.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..errors import Blocked, HubError
from ..events import HubEvent, make_event
from ..oscompat import pid_alive
from ..store import now_iso
from . import routing
from .plan import PLAN_SCHEMA, REVIEW_SCHEMA, PlanError, validate_plan, validate_review

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.dispatch")

CLAIMING = "__claiming__"
RESUMABLE = ("crashed", "timed_out", "rate_limited")
FAILURES = ("crashed", "timed_out", "failed", "protocol_violation")
LIVE_RUN = ("queued", "running", "waiting_human")
WRITE_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


class TaskDispatcher:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._kick_scheduled = False
        self._ticking = False
        self._loop_task: asyncio.Task | None = None
        hub.bus.add_listener(self._on_bus)

    # ------------------------------------------------------------------ infra
    @property
    def store(self):
        return self.hub.store

    @property
    def conn(self):
        return self.hub.store.conn

    @property
    def tasks(self):
        return self.hub.tasks

    @property
    def teams(self):
        return self.hub.teams

    def cfg(self, key: str) -> Any:
        return self.hub.config.get(f"tasks.{key}")

    def start(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._loop_task = loop.create_task(self._loop())
        self.kick()

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(float(self.cfg("tick_s") or 5))
                self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("dispatcher tick lỗi")

    def kick(self) -> None:
        """Yêu cầu tick sớm (gộp nhiều lần gọi trong cùng vòng lặp)."""
        if self._kick_scheduled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._kick_scheduled = True

        def _go() -> None:
            self._kick_scheduled = False
            try:
                self.tick()
            except Exception:  # pragma: no cover
                log.exception("dispatcher tick lỗi")

        loop.call_soon(_go)

    def _on_bus(self, ev: HubEvent) -> None:
        if ev.source != "hub" or not ev.run_id:
            if ev.type == "ratelimit.update":
                self._on_rate_limit(ev)
            return
        if ev.type in ("run.completed", "run.failed", "run.cancelled", "run.interrupted"):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            loop.call_soon(self._safe_terminal, ev.run_id)
        elif ev.type == "run.waiting_human":
            self.kick()

    def _safe_terminal(self, run_id: str) -> None:
        try:
            self.on_run_terminal(run_id)
        except Exception:  # pragma: no cover
            log.exception("xử lý kết quả run %s lỗi", run_id)
        self.kick()

    # ------------------------------------------------------------------ rate limit (FR-RT-16)
    def _on_rate_limit(self, ev: HubEvent) -> None:
        info = (ev.payload or {}).get("rate_limit_info") or {}
        util = 0.0
        for w in (info.get("unifiedWindows") or {}).values():
            if isinstance(w, dict) and isinstance(w.get("utilization"), (int, float)):
                util = max(util, float(w["utilization"]))
        if isinstance(info.get("utilization"), (int, float)):
            util = max(util, float(info["utilization"]))
        thr = float(self.hub.config.get("ratelimit.throttle_utilization") or 0.9)
        throttled = info.get("status") not in (None, "allowed", "allowed_warning") or util >= thr
        prev = self.store.state_get("rate_limit", {}) or {}
        state = {"status": info.get("status"), "utilization": round(util, 4), "resets_at": info.get("resetsAt"), "type": info.get("rateLimitType"),
                 "throttled": throttled, "at": now_iso()}
        self.store.state_set("rate_limit", state)
        if bool(prev.get("throttled")) != throttled:
            self.hub.publish(make_event("ratelimit.throttle", None, state))

    # ------------------------------------------------------------------ helpers
    def _failure_limit(self, t: dict[str, Any]) -> int:
        if t.get("max_retries") is not None:
            return max(1, int(t["max_retries"]))
        if t.get("team_id"):
            return self.teams.policies(t["team_id"]).failure_limit
        return int(self.cfg("failure_limit") or 2)

    def _load(self, agent_id: str) -> int:
        n = self.hub.runs.active_runs(agent_id)
        r = self.conn.execute("SELECT COUNT(*) FROM tasks WHERE assignee_agent_id=? AND status IN ('ready','running')", (agent_id,)).fetchone()
        return n + int(r[0])

    def _set_wait(self, t: dict[str, Any], reason: str | None) -> None:
        if t.get("wait_reason") != reason:
            self.tasks.update(t["id"], {"wait_reason": reason})
            t["wait_reason"] = reason

    def _flag_once(self, key: str) -> bool:
        day = datetime.now().strftime("%Y-%m-%d")
        flags = dict(self.store.state_get("budget_flags", {}) or {})
        if flags.get(key) == day:
            return False
        flags[key] = day
        self.store.state_set("budget_flags", flags)
        return True

    def budget_block(self, agent_id: str, team_id: str | None) -> str | None:
        """Ngân sách ngày (FR-OB-03): trả lý do nếu hub/team/agent đã tiêu ≥ 100% hôm nay."""
        rm = self.hub.runs
        for scope, owner, used, limit in rm.budget_scopes(agent_id, team_id):
            if limit is not None and used >= float(limit):
                if self._flag_once(f"exceeded:{scope}:{owner}"):
                    ev = make_event("budget.exceeded", agent_id if scope == "agent" else None, {"scope": scope, "owner": owner, "used": round(used, 6), "limit": limit})
                    ev.team_id = team_id if scope == "team" else None
                    self.hub.publish(ev)
                return f"budget:{scope}"
        return None

    def _estop_block(self, t: dict[str, Any]) -> str | None:
        if self.hub.runs.estop.get("on"):
            return "estop"
        if t.get("team_id"):
            team = self.teams.find(t["team_id"])
            if team is None or team.get("archived_at"):
                return "team_archived"
            if team.get("estop"):
                return "team_estop"
        return None

    def _agent_block(self, agent_id: str) -> str | None:
        ag = self.store.get_agent(agent_id)
        if ag is None or ag.get("archived_at"):
            return "agent_missing"
        if ag["status"] == "paused":
            return "agent_paused"
        limit = int((ag.get("limits") or {}).get("max_concurrent_runs") or 1)
        if self.hub.runs.active_runs(agent_id) >= limit:
            return "agent_busy"
        return None

    def _workspace(self, t: dict[str, Any]) -> tuple[str | None, str]:
        """(cwd, kind). `worktree` + path (repo) → cwd = repo, run `--worktree <tên>` (CLI tạo worktree trong repo, ghi không cần duyệt).
        Trước đây path của `worktree` bị bỏ → run chạy ở scratch, worktree không bao giờ được tạo."""
        ws = t.get("workspace") or {}
        kind = ws.get("kind") or "scratch"
        return (ws.get("path") if kind in ("dir", "worktree") else None), kind

    def _lock_block(self, t: dict[str, Any], agent_id: str, session_id: str | None, read_only: bool) -> str | None:
        if read_only or not self.hub.config.get("guard.repo_lock"):
            return None
        from ..runs import _agent_read_only

        ag = self.store.get_agent(agent_id) or {}
        if ag and _agent_read_only(ag):
            return None  # FR-RT-12: agent chỉ đọc không lấy khoá (giống RunManager._lock_key)
        path, kind = self._workspace(t)
        if path is None:
            path, kind = ag.get("workspace_default"), "dir"
        key = self.hub.runs.locks.key_for(path, kind) if path else None
        if key and (self.hub.runs.locks.blocked_for(key, session_id) is not None or self.hub.runs.lock_pending(key, session_id)):
            return "workspace_locked"
        return None

    # ------------------------------------------------------------------ tick
    def tick(self) -> None:
        if self._ticking or self.hub.runs.shutting_down:
            return
        self._ticking = True
        try:
            self._reconcile_running()
            self._promote()
            self._dispatch_triage()
            self._dispatch_review()
            self._dispatch_ready()
        finally:
            self._ticking = False

    def _rows(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        return [self.store._row("tasks", r) for r in self.conn.execute(sql, args)]  # type: ignore[misc]

    def _reconcile_running(self) -> None:
        ttl = float(self.cfg("lease_ttl_s") or 120)
        now = _now()
        for t in self._rows("SELECT * FROM tasks WHERE status IN ('running','review') AND current_run_id IS NOT NULL AND current_run_id != ?", (CLAIMING,)):
            run = self.store.get_run(t["current_run_id"])
            if run is None:
                self.tasks.update(t["id"], {"current_run_id": None})
                if t["status"] == "running" and not t["decomposed"]:
                    self.tasks.set_status(t["id"], "ready", reason="run không còn tồn tại")
                continue
            if run["status"] not in LIVE_RUN:
                self.on_run_terminal(run["id"])
                continue
            if run["status"] != "running":
                continue
            ar = self.hub.runs.active.get(run["id"])
            if ar is None or ar.handle is None:
                continue
            hb = _parse(run.get("last_heartbeat_at") or run.get("started_at"))
            stale = hb is not None and (now - hb).total_seconds() > ttl
            if stale and not self._alive(ar):
                log.warning("run %s: tiến trình chết, không heartbeat > %ss → dừng (crashed)", run["id"], ttl)
                ar.crashed = True  # type: ignore[attr-defined]
                if ar.runtime is not None:
                    asyncio.ensure_future(ar.runtime.stop(ar.handle, grace_s=1))

    @staticmethod
    def _alive(ar: Any) -> bool:
        """Tiến trình `claude` của run còn sống? (tiến trình con trực tiếp của daemon nên pid chưa thể bị tái sử dụng
        trước khi được reap; runtime giả không có tiến trình → coi là sống)."""
        proc = (ar.handle.internal or {}).get("proc") if ar.handle else None
        if proc is None:
            return True
        if proc.returncode is not None:
            return False
        try:
            return pid_alive(int(ar.handle.pid))
        except (TypeError, ValueError):
            return True

    def _promote(self) -> None:
        for t in self._rows("SELECT * FROM tasks WHERE status='todo' AND deleted_at IS NULL"):
            ps = self.tasks.parents(t["id"])
            if all((self.tasks.maybe(p) or {}).get("status") == "done" for p in ps):
                self.tasks.set_status(t["id"], "ready", reason="mọi task cha đã done")

    def _due(self, t: dict[str, Any]) -> bool:
        nb = _parse(t.get("not_before"))
        if nb is not None and nb > _now():
            self._set_wait(t, "backoff")
            return False
        return True

    def _capacity_left(self) -> int:
        return self.hub.runs.capacity_left()

    def _dispatch_triage(self) -> None:
        for t in self._rows("SELECT * FROM tasks WHERE status='triage' AND current_run_id IS NULL AND deleted_at IS NULL ORDER BY priority DESC, created_at"):
            if not self._due(t):
                continue
            lead = self.teams.lead(t["team_id"]) if t.get("team_id") else None
            if not lead:
                self._set_wait(t, "no_lead")
                continue
            reason = self._estop_block(t) or self._agent_block(lead) or self.budget_block(lead, t.get("team_id"))
            if reason is None and self._capacity_left() <= 0:
                reason = "concurrency"
            if reason:
                self._set_wait(t, reason)
                continue
            retry = self.store.get_run(t["retry_from_run_id"]) if t.get("retry_from_run_id") else None
            self._start(t, lead, kind="lead", from_status="triage", to_status="running", json_schema=PLAN_SCHEMA, read_only=True, retry=retry,
                        prompt=self.tasks.lead_prompt(t, lead), hub_self="lead")

    def _dispatch_review(self) -> None:
        for t in self._rows("SELECT * FROM tasks WHERE status='review' AND current_run_id IS NULL AND deleted_at IS NULL ORDER BY priority DESC, created_at"):
            if not self._due(t):
                continue
            members = self.teams.members(t["team_id"]) if t.get("team_id") else []
            reviewer = routing.choose_reviewer(members, self._load, exclude=t.get("assignee_agent_id"))
            if reviewer is None:
                self.tasks.set_status(t["id"], "done", reason="không có reviewer — bỏ qua bước review")
                self._after_done(t)
                continue
            reason = self._estop_block(t) or self._agent_block(reviewer) or self.budget_block(reviewer, t.get("team_id"))
            if reason is None and self._capacity_left() <= 0:
                reason = "concurrency"
            if reason:
                self._set_wait(t, reason)
                continue
            self._start(t, reviewer, kind="review", from_status="review", to_status="review", json_schema=REVIEW_SCHEMA, read_only=True,
                        prompt=self.tasks.review_prompt(t, reviewer), hub_self="reviewer")

    def _dispatch_ready(self) -> None:
        rows = self._rows("SELECT * FROM tasks WHERE status='ready' AND current_run_id IS NULL AND deleted_at IS NULL ORDER BY priority DESC, created_at")
        for t in rows:
            if not self._due(t):
                continue
            reason = self._estop_block(t)
            if reason:
                self._set_wait(t, reason)
                continue
            agent_id = t.get("assignee_agent_id")
            if not agent_id and t.get("team_id"):
                team = self.teams.row(t["team_id"], allow_archived=True)
                agent_id = routing.choose_worker(t, self.teams.members(team["id"]), self._load, default=self.teams.policies(team).default_assignee)
                if agent_id:
                    self.tasks.update(t["id"], {"assignee_agent_id": agent_id})
                    t["assignee_agent_id"] = agent_id
            if not agent_id:
                self._set_wait(t, "no_assignee")
                continue
            reason = self._agent_block(agent_id)
            if reason is None and t.get("team_id"):
                m = self.teams.member(t["team_id"], agent_id)
                if m and self.teams.in_progress(t["team_id"], agent_id) >= int(m["max_in_progress"]):
                    reason = "member_limit"
            if reason is None:
                reason = self.budget_block(agent_id, t.get("team_id"))
            retry = self.store.get_run(t["retry_from_run_id"]) if t.get("retry_from_run_id") else None
            resume = retry is not None and retry.get("outcome") in RESUMABLE and retry.get("started_at") and retry.get("agent_id") == agent_id
            if reason is None:
                reason = self._lock_block(t, agent_id, retry["session_id"] if resume else None, read_only=False)
            if reason is None and self._capacity_left() <= 0:
                reason = "rate_limit" if (self.store.state_get("rate_limit", {}) or {}).get("throttled") else "concurrency"
            if reason:
                self._set_wait(t, reason)
                continue
            if resume:
                prompt = self.tasks.resume_prompt(t, retry["outcome"])
            else:
                prompt = self.tasks.worker_prompt(t, agent_id, hub_self=bool(self.cfg("hub_self_mcp")))
            self._start(t, agent_id, kind="task", from_status="ready", to_status="running", json_schema=t.get("result_schema"), read_only=False,
                        prompt=prompt, retry=retry if resume else None, parent_run=retry, hub_self="lead" if self._is_lead(t, agent_id) else "worker")

    def _is_lead(self, t: dict[str, Any], agent_id: str) -> bool:
        return bool(t.get("team_id")) and self.teams.lead(t["team_id"]) == agent_id

    def _start(
        self, t: dict[str, Any], agent_id: str, *, kind: str, from_status: str, to_status: str, prompt: str, json_schema: dict | None,
        read_only: bool, retry: dict[str, Any] | None = None, parent_run: dict[str, Any] | None = None, hub_self: str = "worker",
    ) -> dict[str, Any] | None:
        """Claim nguyên tử rồi submit run. Trả run hoặc None (đã bị tick khác claim / submit bị chặn)."""
        now = now_iso()
        with self.store.tx():
            cur = self.conn.execute(
                "UPDATE tasks SET status=?, current_run_id=?, claimed_at=?, updated_at=? WHERE id=? AND status=? AND current_run_id IS NULL",
                (to_status, CLAIMING, now, now, t["id"], from_status),
            )
            if cur.rowcount != 1:
                return None
        path, wkind = self._workspace(t)
        ov = t.get("overrides") or {}
        opts = {"json_schema": json_schema, "read_only": read_only, "hub_self": hub_self if self.cfg("hub_self_mcp") else None, "workspace_kind": wkind}
        if wkind == "worktree":
            opts["worktree"] = (t.get("workspace") or {}).get("name") or t["id"]
        extra = (t.get("workspace") or {}).get("extra_write_roots") or []
        if extra and wkind in ("dir", "worktree"):
            opts["extra_write_roots"] = [str(x) for x in extra][:5]  # đã kiểm lúc tạo task; guard kiểm lại theo workspace.allowed_roots
        if read_only:
            opts["disallowed_tools"] = WRITE_TOOLS
        budget = ov.get("budget_usd")
        if kind == "review" and self.cfg("review_budget_usd"):
            budget = float(self.cfg("review_budget_usd"))
        try:
            run = self.hub.runs.submit(
                agent_id, prompt, cwd=path, budget_usd=budget, model=ov.get("model"), kind=kind,
                resume_session_id=retry["session_id"] if retry else None, parent_run_id=(parent_run or {}).get("id"),
                resume_reason=f"task_retry:{retry['outcome']}" if retry else ("task_retry" if parent_run else None),
                task_id=t["id"], team_id=t.get("team_id"), run_opts=opts, max_runtime_s=ov.get("max_runtime_s"),
            )
        except Blocked as e:
            self.tasks.update(t["id"], {"status": from_status, "current_run_id": None, "wait_reason": "blocked:" + e.message[:80]})
            return None
        except HubError as e:
            self.tasks.update(t["id"], {"status": from_status, "current_run_id": None})
            self.tasks.set_status(t["id"], "blocked", reason=f"không khởi chạy được run: {e.message}")
            return None
        data: dict[str, Any] = {"current_run_id": run["id"], "attempts": int(t.get("attempts") or 0) + (1 if kind == "task" else 0), "wait_reason": None,
                                "retry_from_run_id": None, "lease_expires_at": _iso(_now() + timedelta(seconds=float(self.cfg("lease_ttl_s") or 120)))}
        if kind == "task":
            data["assignee_agent_id"] = agent_id
        if to_status == "running" and not t.get("started_at"):
            data["started_at"] = now
        self.tasks.update(t["id"], data)
        if from_status != to_status:
            t2 = self.tasks.row(t["id"])
            self.tasks.publish("task.status", t2, {"from": from_status, "to": to_status, "reason": f"claimed by {agent_id} (run {run['id']}, {kind})", "run_id": run["id"]}, agent_id=agent_id)
        return run

    def on_run_submitted(self, task_id: str, run_id: str, parent_run_id: str | None) -> None:
        """RunManager gọi khi một run của task được tạo NGOÀI dispatcher (resume sau approval/restart, continue)."""
        t = self.tasks.maybe(task_id)
        if t and parent_run_id and t.get("current_run_id") == parent_run_id:
            self.tasks.update(task_id, {"current_run_id": run_id})

    # ------------------------------------------------------------------ kết quả run
    def on_run_terminal(self, run_id: str) -> None:
        if self.hub.runs.shutting_down:
            return  # daemon đang dừng: run → interrupted, lần khởi động sau recovery tự resume (Q-09)
        run = self.store.get_run(run_id)
        if run is None or not run.get("task_id") or run["status"] in LIVE_RUN:
            return
        t = self.tasks.maybe(run["task_id"])
        if t is None or t.get("current_run_id") != run_id:
            return
        if t["status"] == "archived":
            self.tasks.update(t["id"], {"current_run_id": None})
            return
        if run.get("continued_in_run_id"):
            # run được tiếp tục ở run mới (resume sau approval / sau restart) → task theo run mới
            self.tasks.update(t["id"], {"current_run_id": run["continued_in_run_id"]})
            return
        kind = run.get("kind")
        if kind == "lead":
            self._on_lead(t, run)
        elif kind == "review":
            self._on_review(t, run)
        else:
            self._on_work(t, run)

    def _outcome(self, run: dict[str, Any]) -> str:
        return run.get("outcome") or run.get("status") or "failed"

    def _on_work(self, t: dict[str, Any], run: dict[str, Any]) -> None:
        oc = self._outcome(run)
        sig = run.get("task_signal") or {}
        op = sig.get("op")
        if op == "block" or oc == "blocked":
            self.tasks.set_status(t["id"], "blocked", reason=sig.get("reason") or "agent báo blocked", current_run_id=None)
            self._refresh_root(t)
            return
        if op in ("complete", "request_review") or oc == "completed":
            if oc == "completed" and not op and self.cfg("require_completion_call"):
                self.store.update_run(run["id"], {"status": "protocol_violation", "outcome": "protocol_violation", "error": "kết thúc mà không gọi task_complete/task_block"})
                return self._on_failure(t, run, "protocol_violation")
            summary = sig.get("summary") or run.get("summary") or ""
            result = sig.get("result") if sig.get("result") is not None else run.get("result_json")
            data = {"result_summary": summary, "result_json": result, "consecutive_failures": 0, "current_run_id": None, "rate_limit_hits": 0}
            needs_review = op == "request_review" or (t.get("team_id") and self.teams.policies(t["team_id"]).review_required) or (t.get("overrides") or {}).get("review")
            if needs_review and t.get("kind") != "root":
                self.tasks.set_status(t["id"], "review", reason="chờ reviewer", **data)
            else:
                self.tasks.set_status(t["id"], "done", reason=f"run {run['id']} hoàn tất", **data)
                self._after_done(t)
            return
        if oc == "rate_limited":
            hits = int(t.get("rate_limit_hits") or 0) + 1
            base = float(self.cfg("rate_limit_backoff_s") or 30)
            delay = min(base * (2 ** (hits - 1)), float(self.cfg("rate_limit_backoff_max_s") or 600))
            self.tasks.set_status(t["id"], "ready", reason=f"rate_limited — thử lại sau {int(delay)}s", current_run_id=None, rate_limit_hits=hits,
                                  not_before=_iso(_now() + timedelta(seconds=delay)), retry_from_run_id=run["id"], wait_reason="backoff")
            return
        if oc == "budget_exceeded":
            self.tasks.set_status(t["id"], "blocked", reason=f"run {run['id']} chạm trần chi phí (--max-budget-usd)", current_run_id=None)
            self._refresh_root(t)
            return
        if oc in ("cancelled", "interrupted"):
            from ..workspace import resume_conflicts

            changed = resume_conflicts(run) if oc == "interrupted" else []
            if changed:
                self.tasks.set_status(t["id"], "blocked", reason=f"run {run['id']} gián đoạn và {len(changed)} file của nó đã bị sửa tiếp ({', '.join(changed[:5])}) — không tự chạy lại", current_run_id=None)
                self._refresh_root(t)
                return
            if oc == "interrupted" or self._estop_block(t):
                # dừng do ESTOP/daemon: chờ chạy lại (resume), không tính lỗi
                self.tasks.set_status(t["id"], "ready", reason=f"run {oc} — sẽ chạy lại", current_run_id=None, retry_from_run_id=run["id"])
            else:
                self.tasks.set_status(t["id"], "blocked", reason=f"run {run['id']} bị huỷ", current_run_id=None)
                self._refresh_root(t)
            return
        if self.hub.runs.auth_block:
            self.tasks.set_status(t["id"], "blocked", reason="lỗi không thể retry: claude chưa đăng nhập", current_run_id=None)
            self._refresh_root(t)
            return
        self._on_failure(t, run, oc)

    def _on_failure(self, t: dict[str, Any], run: dict[str, Any], oc: str, *, back_to: str | None = None) -> None:
        n = int(t.get("consecutive_failures") or 0) + 1
        limit = self._failure_limit(t)
        err = (run.get("error") or "")[:300]
        if n >= limit:
            self.tasks.set_status(t["id"], "blocked", reason=f"circuit open: {n} lỗi liên tiếp (lần cuối {oc}: {err})", current_run_id=None, consecutive_failures=n)
            self.tasks.publish("task.circuit_open", self.tasks.row(t["id"]), {"failures": n, "limit": limit, "last_outcome": oc, "run_id": run["id"], "error": err})
            self._refresh_root(t)
            return
        to = back_to or ("triage" if run.get("kind") == "lead" else ("review" if run.get("kind") == "review" else "ready"))
        self.tasks.set_status(t["id"], to, reason=f"{oc} — thử lại ({n}/{limit})", current_run_id=None, consecutive_failures=n,
                              retry_from_run_id=run["id"] if oc in RESUMABLE else None)

    def _after_done(self, t: dict[str, Any]) -> None:
        self._promote()
        self._refresh_root(t)

    def _refresh_root(self, t: dict[str, Any]) -> None:
        rid = t.get("origin_task_id")
        if not rid:
            return
        root = self.tasks.maybe(rid)
        if root is None or not root.get("decomposed") or root["status"] in ("done", "archived"):
            return
        subs = [s for s in self.tasks.subtasks(rid) if s["status"] != "archived"]
        if subs and all(s["status"] == "done" for s in subs):
            parts, res = [], []
            for s in subs:
                parts.append(f"## {s['title']} ({s['id']}, {s.get('assignee_agent_id') or '-'})\n{s.get('result_summary') or ''}".rstrip())
                res.append({"task_id": s["id"], "key": s.get("plan_key"), "title": s["title"], "assignee": s.get("assignee_agent_id"),
                            "summary": s.get("result_summary"), "result": s.get("result_json")})
            leaves = [s for s in subs if not self.tasks.children(s["id"])]
            final = "\n\n".join((s.get("result_summary") or "") for s in leaves).strip()
            self.tasks.set_status(rid, "done", reason="mọi task con đã done", result_summary="\n\n".join(parts),
                                  result_json={"subtasks": res, "final": final})
            return
        blocked = [s for s in subs if s["status"] == "blocked"]
        if blocked and root["status"] != "blocked":
            self.tasks.set_status(rid, "blocked", reason=f"task con {blocked[0]['id']} blocked: {blocked[0].get('block_reason')}")
        elif not blocked and root["status"] == "blocked":
            self.tasks.set_status(rid, "running", reason="task con hết blocked")

    def _on_lead(self, t: dict[str, Any], run: dict[str, Any]) -> None:
        oc = self._outcome(run)
        sig = run.get("task_signal") or {}
        if sig.get("op") == "block":
            self.tasks.set_status(t["id"], "blocked", reason=sig.get("reason") or "lead báo blocked", current_run_id=None)
            return
        if oc != "completed":
            if oc == "rate_limited":
                hits = int(t.get("rate_limit_hits") or 0) + 1
                delay = min(float(self.cfg("rate_limit_backoff_s") or 30) * (2 ** (hits - 1)), float(self.cfg("rate_limit_backoff_max_s") or 600))
                self.tasks.set_status(t["id"], "triage", reason="rate_limited", current_run_id=None, rate_limit_hits=hits, not_before=_iso(_now() + timedelta(seconds=delay)))
                return
            if oc in ("budget_exceeded", "cancelled"):
                self.tasks.set_status(t["id"], "blocked", reason=f"run lead {oc}", current_run_id=None)
                return
            return self._on_failure(t, run, oc, back_to="triage")
        members = self.teams.members(t["team_id"])
        lead = self.teams.lead(t["team_id"])
        # lead có thể đã tạo task con qua hub-self (task_create) thay vì trả kế hoạch
        existing = self.tasks.subtasks(t["id"])
        try:
            if existing and run.get("result_json") is None:
                subs = []
            else:
                subs = validate_plan(run.get("result_json"), members, lead=lead, max_subtasks=int(self.cfg("max_subtasks") or 20))
        except PlanError as e:
            self.store.update_run(run["id"], {"status": "protocol_violation", "outcome": "protocol_violation", "error": f"kế hoạch không hợp lệ: {e}", "schema_valid": 0})
            self.hub.publish(make_event("run.protocol_violation", run["agent_id"], {"error": str(e)}, run_id=run["id"]))
            self.tasks.set_status(t["id"], "blocked", reason=f"lead trả kế hoạch không hợp lệ: {e}", current_run_id=None)
            return
        ids: dict[str, str] = {}
        for s in subs:
            child = self.tasks.create(
                {"team": t["team_id"], "title": s["title"], "body": s["body"], "assignee": s["assignee"], "role": s["role"],
                 "parents": [ids[p] for p in s["parents"]], "workspace": t.get("workspace"), "overrides": t.get("overrides"), "priority": t.get("priority"),
                 "origin_task_id": t["id"], "plan_key": s["key"], "decompose": False},
                created_by=f"agent:{run['agent_id']}",
            )
            ids[s["key"]] = child["id"]
        plan = run.get("result_json") or {}
        self.tasks.set_status(t["id"], "running", reason=f"lead tách thành {len(ids) or len(existing)} task con", current_run_id=None, decomposed=1,
                              consecutive_failures=0, result_summary=(plan.get("summary") if isinstance(plan, dict) else None))
        self.tasks.publish("task.decomposed", self.tasks.row(t["id"]), {"subtasks": list(ids.values()) or [s["id"] for s in existing], "plan_summary": plan.get("summary") if isinstance(plan, dict) else None, "run_id": run["id"]},
                           agent_id=run["agent_id"])
        if not ids and not existing:
            self.tasks.set_status(t["id"], "blocked", reason="lead không tạo task con nào")
            return
        self._refresh_root({"origin_task_id": t["id"]})  # task con tạo qua hub-self có thể đã xong trước khi lead kết thúc

    def _on_review(self, t: dict[str, Any], run: dict[str, Any]) -> None:
        oc = self._outcome(run)
        if oc != "completed":
            if oc in ("budget_exceeded", "cancelled"):
                self.tasks.set_status(t["id"], "blocked", reason=f"run review {oc}", current_run_id=None)
                self._refresh_root(t)
                return
            return self._on_failure(t, run, oc, back_to="review")
        try:
            verdict, comment = validate_review(run.get("result_json"))
        except PlanError as e:
            self.store.update_run(run["id"], {"status": "protocol_violation", "outcome": "protocol_violation", "error": str(e), "schema_valid": 0})
            return self._on_failure(t, run, "protocol_violation", back_to="review")
        self.tasks.comment(t["id"], comment or verdict, author=f"agent:{run['agent_id']}", kind=f"review:{verdict}", run_id=run["id"])
        if verdict == "approve":
            self.tasks.set_status(t["id"], "done", reason=f"reviewer {run['agent_id']} approve", current_run_id=None, consecutive_failures=0)
            self._after_done(t)
            return
        rounds = int(t.get("review_rounds") or 0) + 1
        worker_run = self.tasks.last_work_run(t["id"])
        if worker_run:
            self._signal_reject(worker_run, t, comment, run)
        maxr = self.teams.policies(t["team_id"]).max_review_rounds if t.get("team_id") else 3
        if rounds >= maxr:
            self.tasks.set_status(t["id"], "blocked", reason=f"reviewer yêu cầu sửa {rounds} lần (tối đa {maxr})", current_run_id=None, review_rounds=rounds)
            self._refresh_root(t)
            return
        self.tasks.set_status(t["id"], "ready", reason="reviewer yêu cầu sửa", current_run_id=None, review_rounds=rounds, consecutive_failures=0)

    def _signal_reject(self, worker_run: dict[str, Any], t: dict[str, Any], comment: str, review_run: dict[str, Any]) -> None:
        """FR-TK-12: Signal `reject` cho worker (nguồn học)."""
        import json
        import uuid

        sid = "sg_" + uuid.uuid4().hex[:12]
        self.conn.execute(
            "INSERT INTO signals (id, agent_id, team_id, run_id, task_id, kind, source, evidence, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (sid, worker_run["agent_id"], t.get("team_id"), worker_run["id"], t["id"], "reject", "review",
             json.dumps({"comment": comment, "review_run_id": review_run["id"], "reviewer": review_run["agent_id"]}, ensure_ascii=False), now_iso()),
        )
        self.hub.publish(make_event("learning.signal", worker_run["agent_id"], {"signal_id": sid, "kind": "reject", "source": "review", "task_id": t["id"]}, run_id=worker_run["id"]))

    # ------------------------------------------------------------------ hub-self (FR-MS-08)
    def self_call(self, run_id: str, op: str, args: dict[str, Any]) -> dict[str, Any]:
        """Lời gọi tool hub-self của agent — ràng với `run_id` (biến môi trường do hub cấp). Chỉ thao tác task của chính run."""
        run = self.store.get_run(run_id)
        if run is None or run["status"] not in ("running", "queued"):
            raise HubError("run không đang chạy — tool hub-self chỉ dùng trong run đang chạy")
        tid = run.get("task_id")
        if not tid:
            raise HubError("run này không gắn task")
        t = self.tasks.row(tid)
        target = args.get("task_id")
        if target and target != tid and op not in ("task_show",):
            raise HubError(f"run {run_id} chỉ được thao tác task của mình ({tid}), không phải {target}")
        is_lead = bool(t.get("team_id")) and self.teams.lead(t["team_id"]) == run["agent_id"]
        author = f"agent:{run['agent_id']}"
        if op == "task_show":
            show_id = target or tid
            if show_id != tid:
                other = self.tasks.row(show_id)
                if other["id"] not in self.tasks.parents(tid) and other.get("origin_task_id") != tid and not is_lead:
                    raise HubError("chỉ xem được task của mình, task cha của nó, hoặc (lead) task con")
            g = self.tasks.get(show_id)
            keep = ("id", "title", "body", "status", "team_id", "assignee_agent_id", "parents", "children", "subtasks", "comments", "result_summary", "result_json")
            return {k: g.get(k) for k in keep}
        if op == "task_comment":
            return self.tasks.comment(tid, str(args.get("text") or ""), author=author, run_id=run_id)
        if op == "ask_human":
            # UX: câu hỏi có lựa chọn → điều phối viên (nếu có) → người. Task bị chặn khi run kết thúc; trả lời → bình luận + gỡ chặn
            if run.get("kind") not in ("task", "lead"):
                raise HubError(f"ask_human không dùng được trong run {run.get('kind')}")
            q = self.hub.questions.open(run, args.get("question"), args.get("options"), multi=bool(args.get("multi")),  # type: ignore[attr-defined]
                                        allow_free_text=args.get("allow_free_text") is not False, source="ask_tool", task_id=tid)
            self.store.update_run(run_id, {"task_signal": {"op": "block", "reason": f"waiting for an answer to question {q['id']}: {q['text'][:200]}",
                                                           "question_id": q["id"]}})
            who = "the coordinator (it may pass it to the human owner)" if q["routed_to"] == "manager" else "the human owner"
            return {"ok": True, "question_id": q["id"], "asked": who,
                    "note": "Question sent. End your turn now with a short summary of what you did so far; you will be resumed with the answer."}
        if op in ("task_complete", "task_request_review", "task_block"):
            if run.get("kind") not in ("task", "lead"):
                raise HubError(f"{op} không dùng được trong run {run.get('kind')}")
            if op == "task_block":
                reason = str(args.get("reason") or "").strip()
                if not reason:
                    raise HubError("task_block cần reason")
                sig = {"op": "block", "reason": reason}
            else:
                sig = {"op": "complete" if op == "task_complete" else "request_review", "summary": str(args.get("summary") or ""), "result": args.get("result")}
            self.store.update_run(run_id, {"task_signal": sig})
            self.tasks.publish("task.signal", t, {"op": sig["op"], "run_id": run_id, "summary": (sig.get("summary") or sig.get("reason") or "")[:300]}, agent_id=run["agent_id"])
            return {"ok": True, "task_id": tid, "recorded": sig["op"], "note": "Recorded. Finish your turn now with a short summary."}
        if op in ("task_create", "task_link", "team_roster"):
            if not is_lead:
                raise HubError(f"{op} chỉ dành cho lead của team")
            if op == "team_roster":
                return {"team": t["team_id"], "members": [{"agent_id": m["agent_id"], "role": m["role"], "agent_role": m.get("agent_role"), "description": m.get("agent_description")} for m in self.teams.members(t["team_id"])]}
            if op == "task_link":
                return self.tasks.link(str(args.get("parent_id")), str(args.get("child_id")))
            parents = list(args.get("parents") or [])
            for p in parents:
                pt = self.tasks.row(p)
                if pt.get("origin_task_id") != tid:
                    raise HubError(f"parent {p} không phải task con của {tid}")
            child = self.tasks.create(
                {"team": t["team_id"], "title": args.get("title"), "body": args.get("body") or "", "assignee": args.get("assignee"), "role": args.get("role"),
                 "parents": parents, "workspace": t.get("workspace"), "overrides": t.get("overrides"), "origin_task_id": tid, "decompose": False},
                created_by=author,
            )
            return {"task_id": child["id"], "status": child["status"]}
        raise HubError(f"tool hub-self không hỗ trợ: {op}")
