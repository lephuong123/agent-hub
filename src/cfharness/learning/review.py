"""Kích hoạt review (FR-LN-02) + review fork bằng `claude` CLI (FR-LN-03).

Trigger (khi công tắc học bật — FR-LN-15):
- Signal mức cao (`learning.high_signals`: correction, reject, edit_output) → xếp review job ngay (≤ 5 s);
- bộ đếm theo agent: turn người (prompt + steer) ≥ `learning.trigger_turns` HOẶC vòng tool ≥ `learning.trigger_tool_rounds`
  kể từ lần review gần nhất (Hermes `nudge_interval`), chỉ tính run kết thúc bình thường;
- thủ công `cfharness learn refine <agent> [--focus …]`.
Hàng đợi theo agent, GỘP newest-wins (một job `queued`/agent: hợp Signal, nguồn = run mới nhất), debounce
`learning.review_debounce_s` sau Signal cuối; chỉ chạy khi agent RẢNH (không run thường đang chạy/chờ) — run mới của agent
KHÔNG bị review chặn (review không tính vào `max_concurrent_runs` của agent).

Review = Run `kind=review`: `claude -p <prompt Hermes + Signal> --resume <session nguồn> --fork-session --session-id <mới>`
(cùng model, cùng system prompt của session → cache; persistence ở session MỚI, session gốc không đổi), `--tools
Read,Grep,Glob`, `--permission-prompts none`, `--strict-mcp-config` + chỉ MCP `hub-self` vai trò `learning` (tool
propose_* — không ghi file), `--max-budget-usd` = `learning.review_budget_usd` (phần khám phá) + chi phí ƯỚC TÍNH nạp lại ngữ
cảnh session gốc (lượt API đầu của fork ghi lại cả ngữ cảnh vào cache: token ngữ cảnh lần cuối của session ×
`learning.review_reload_usd_per_mtok`), chặn trên bởi `learning.review_budget_max_usd`. Proposal được pipeline tự áp dụng ngay
khi tool được gọi. Hết ngân sách → run `budget_exceeded`, proposal đã gửi vẫn giữ.

Kết thúc không trọn (không tiêu Signal của job khi chưa review xong):
- `rate_limited` → job xếp hàng lại sau `learning.review_retry_backoff_s` (nhân đôi mỗi lần, ≤ 1 giờ), tối đa
  `learning.review_rate_limit_retries` lần; quá trần → `failed` nhưng Signal VẪN chưa xử lý (review sau nhặt lại).
- `budget_exceeded` CHƯA có proposal nào → thử lại tối đa `learning.review_budget_retries` lần với trần = `review_budget_max_usd`;
  quá trần → `failed` + Signal đánh dấu đã xử lý (chống vòng lặp tốn tiền). Có proposal rồi → `done` như cũ.
Lịch sử từng lần thử ở `review_jobs.result.attempts`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..errors import HubError
from ..events import make_event
from ..store import now_iso
from .prompts import render_review_prompt

if TYPE_CHECKING:
    from ..hub import Hub
    from .service import LearningService

log = logging.getLogger("cfharness.learning")
NON_WORK_KINDS = ("review", "eval", "eval_judge", "curator")
ACTIVE_STATUSES = ("queued", "running", "waiting_human")


def _utc_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ReviewScheduler:
    def __init__(self, hub: "Hub", svc: "LearningService") -> None:
        self.hub = hub
        self.svc = svc

    @property
    def conn(self):
        return self.hub.store.conn

    def cfg(self, key: str) -> Any:
        return self.hub.config.get(f"learning.{key}")

    # ------------------------------------------------------------------ job
    def get(self, job_id: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM review_jobs WHERE id=? OR id LIKE ? ORDER BY id LIMIT 1", (job_id, job_id + "%")).fetchone()
        if r is None:
            raise HubError(f"không tìm thấy review job '{job_id}'", code=-32004)
        return self.hub.store._row("review_jobs", r)  # type: ignore[return-value]

    def list(self, agent_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        where, args = [], []
        if agent_id:
            where.append("agent_id=?")
            args.append(agent_id)
        if status:
            where.append("status=?")
            args.append(status)
        sql = "SELECT * FROM review_jobs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?"
        return [self.hub.store._row("review_jobs", r) for r in self.conn.execute(sql, (*args, int(limit)))]  # type: ignore[misc]

    def _run_order(self, run_id: str | None) -> str:
        r = self.hub.store.get_run(run_id) if run_id else None
        return (r or {}).get("queued_at") or ""

    def enqueue(self, agent_id: str, trigger: str, *, signal_ids: list[str] | None = None, source_run_id: str | None = None,
                focus: str | None = None, delay_s: float | None = None) -> dict[str, Any] | None:
        if not self.svc.switches(agent_id)["enabled"]:
            return None
        delay = float(self.cfg("review_debounce_s") or 0) if delay_s is None else float(delay_s)
        nb = _utc_in(delay)
        q = self.conn.execute("SELECT * FROM review_jobs WHERE agent_id=? AND status='queued' ORDER BY created_at LIMIT 1", (agent_id,)).fetchone()
        if q is not None:  # gộp newest-wins
            job = self.hub.store._row("review_jobs", q)
            assert job is not None
            sids = list(dict.fromkeys(list(job["signal_ids"] or []) + list(signal_ids or [])))
            src = job["source_run_id"]
            if source_run_id and self._run_order(source_run_id) >= self._run_order(src):
                src = source_run_id
            trig = job["trigger"] if job["trigger"] in ("manual", "signal") and trigger == "counter" else trigger
            self.hub.store._update("review_jobs", "id", job["id"], {"signal_ids": sids, "source_run_id": src, "trigger": trig, "not_before": nb,
                                                                   "focus": focus or job.get("focus"), "updated_at": now_iso()})
            self._kick(delay)
            return self.get(job["id"])
        jid = "rv_" + uuid.uuid4().hex[:12]
        self.hub.store._insert("review_jobs", {"id": jid, "agent_id": agent_id, "trigger": trigger, "signal_ids": list(signal_ids or []), "focus": focus,
                                               "status": "queued", "source_run_id": source_run_id, "not_before": nb, "created_at": now_iso(), "updated_at": now_iso()})
        self._kick(delay)
        return self.get(jid)

    def _kick(self, delay: float) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_later(max(0.05, delay + 0.05), self.tick)

    # ------------------------------------------------------------------ trigger
    def on_signal(self, sig: dict[str, Any]) -> None:
        if sig["kind"] not in (self.cfg("high_signals") or []):
            return
        run = self.hub.store.get_run(sig["run_id"]) if sig.get("run_id") else None
        if run is not None and run.get("kind") in NON_WORK_KINDS:
            return
        self.enqueue(sig["agent_id"], "signal", signal_ids=[sig["id"]], source_run_id=(run or {}).get("id"))

    def _counters(self) -> dict[str, Any]:
        return dict(self.hub.store.state_get("learning.counters", {}) or {})

    def counters_for(self, agent_id: str) -> dict[str, Any]:
        return self._counters().get(agent_id) or {"turns": 0, "tools": 0}

    def reset_counters(self, agent_id: str) -> None:
        c = self._counters()
        c[agent_id] = {"turns": 0, "tools": 0, "reset_at": now_iso()}
        self.hub.store.state_set("learning.counters", c)

    def on_run_terminal(self, run: dict[str, Any]) -> None:
        if run.get("kind") == "review":
            self.finalize(run)
            return
        if run.get("kind") in NON_WORK_KINDS:
            return
        agent_id = run["agent_id"]
        if run.get("status") == "completed":
            tools = int(self.conn.execute("SELECT COUNT(*) FROM events WHERE run_id=? AND type='tool.start'", (run["id"],)).fetchone()[0])
            c = self._counters()
            a = c.get(agent_id) or {"turns": 0, "tools": 0}
            a["turns"] = int(a.get("turns", 0)) + 1 + int(run.get("steer_count") or 0)
            a["tools"] = int(a.get("tools", 0)) + tools
            a["last_run_id"] = run["id"]
            c[agent_id] = a
            self.hub.store.state_set("learning.counters", c)
            if a["turns"] >= int(self.cfg("trigger_turns") or 10) or a["tools"] >= int(self.cfg("trigger_tool_rounds") or 10):
                pending = [s["id"] for s in self.svc.signals.unprocessed(agent_id)]
                self.enqueue(agent_id, "counter", signal_ids=pending, source_run_id=run["id"])
        self.tick()

    def refine(self, agent_id: str, focus: str | None = None, run_ref: str | None = None) -> dict[str, Any]:
        self.hub.agents.get_row(agent_id)
        if not self.svc.switches(agent_id)["enabled"]:
            raise HubError(f"học đang tắt cho agent '{agent_id}' (cfharness learn policy …)", code=-32003)
        if run_ref:
            src = self.hub.store.find_run(run_ref)
            if src is None or src["agent_id"] != agent_id:
                raise HubError(f"run '{run_ref}' không thuộc agent '{agent_id}'")
        else:
            src = self._latest_source(agent_id)
            if src is None:
                raise HubError(f"agent '{agent_id}' chưa có run nào để review")
        pending = [s["id"] for s in self.svc.signals.unprocessed(agent_id)]
        job = self.enqueue(agent_id, "manual", signal_ids=pending, source_run_id=src["id"], focus=focus, delay_s=0)
        assert job is not None
        return job

    def _latest_source(self, agent_id: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT id FROM runs WHERE agent_id=? AND kind NOT IN ('review','eval','eval_judge','curator') AND session_id IS NOT NULL "
                              "AND status NOT IN ('queued','running','waiting_human') AND started_at IS NOT NULL ORDER BY queued_at DESC LIMIT 1", (agent_id,)).fetchone()
        return self.hub.store.get_run(r[0]) if r else None

    # ------------------------------------------------------------------ chạy
    def agent_idle(self, agent_id: str) -> bool:
        for ar in self.hub.runs.active.values():
            if ar.agent_id == agent_id:
                return False
        n = self.conn.execute(f"SELECT COUNT(*) FROM runs WHERE agent_id=? AND status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
                              (agent_id, *ACTIVE_STATUSES)).fetchone()[0]
        return int(n) == 0

    def tick(self) -> list[str]:
        started: list[str] = []
        if self.hub.runs.estop.get("on") or self.hub.runs.shutting_down:
            return started
        now = now_iso()
        for r in self.conn.execute("SELECT id FROM review_jobs WHERE status='queued' AND (not_before IS NULL OR not_before<=?) ORDER BY created_at", (now,)).fetchall():
            job = self.get(r[0])
            try:
                rid = self._try_start(job)
            except Exception as e:  # pragma: no cover - lỗi không lường trước: đánh dấu job hỏng, không chết vòng
                log.exception("review job %s lỗi", job["id"])
                self._end(job, "failed", reason=f"lỗi nội bộ: {e!r}"[:300])
                rid = None
            if rid:
                started.append(rid)
        return started

    def _try_start(self, job: dict[str, Any]) -> str | None:
        agent_id = job["agent_id"]
        sw = self.svc.switches(agent_id)
        if not sw["enabled"]:
            self._end(job, "cancelled", reason="learning.enabled=false")
            return None
        if not self.agent_idle(agent_id):
            return None
        src = self.hub.store.get_run(job["source_run_id"]) if job.get("source_run_id") else self._latest_source(agent_id)
        if src is None or not src.get("session_id") or not src.get("started_at"):
            self._end(job, "skipped", reason="không có run nguồn có session để fork")
            return None
        if src["status"] in ACTIVE_STATUSES:
            return None  # đợi run nguồn kết thúc
        if job["trigger"] == "counter" and src.get("status") != "completed" and not job.get("signal_ids"):
            self._end(job, "skipped", reason=f"run nguồn kết thúc {src.get('status')} (chỉ review run kết thúc bình thường)")
            return None
        cap = int(self.cfg("review_max_per_agent_per_day") or 0)
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if cap and int(self.conn.execute("SELECT COUNT(*) FROM review_jobs WHERE agent_id=? AND started_at>=?", (agent_id, start)).fetchone()[0]) >= cap:
            self.hub.store._update("review_jobs", "id", job["id"], {"not_before": _utc_in(3600), "reason": f"đã đủ {cap} review hôm nay", "updated_at": now_iso()})
            return None
        if job["trigger"] == "counter":
            # review định kỳ khi agent đã hết suất thay đổi hôm nay chỉ đẻ proposal bị hoãn (tốn tiền, dồn ứ sang hôm sau) → giữ job
            # chờ tới khi có suất (nửa đêm, hoặc sớm hơn khi rollback trả suất). Signal mạnh / refine thủ công vẫn chạy.
            full = self.svc.pipeline.agent_quota_full(agent_id)
            if full is not None:
                nb = min(full[0], _utc_in(3600))  # kiểm lại mỗi giờ: rollback có thể trả suất sớm
                self.hub.store._update("review_jobs", "id", job["id"], {"not_before": nb, "reason": f"chờ suất học: {full[1]}", "updated_at": now_iso()})
                return None
        signals = self.svc.signals.many(list(job.get("signal_ids") or []))
        prompt = render_review_prompt(scope=str(self.cfg("review_scope") or "combined"), signals=signals, focus=job.get("focus"))
        model = self.cfg("review_model") or src.get("model")
        opts = {"fork": True, "learning_review": True, "read_only": True, "review_job_id": job["id"], "source_run_id": src["id"]}
        budget = self.review_budget(src, retry_after_budget=any(a.get("outcome") == "budget_exceeded" for a in self._attempts(job)))
        try:
            run = self.hub.runs.submit(agent_id, prompt, cwd=src.get("cwd"), budget_usd=budget, model=model,
                                       kind="review", resume_session_id=src["session_id"], run_opts=opts,
                                       max_runtime_s=float(self.cfg("review_timeout_s") or 600))
        except HubError as e:
            self._end(job, "failed", reason=e.message[:300])
            return None
        self.hub.store._update("review_jobs", "id", job["id"], {"status": "running", "run_id": run["id"], "source_session_id": src["session_id"],
                                                               "source_run_id": src["id"], "started_at": now_iso(), "updated_at": now_iso()})
        self.reset_counters(agent_id)
        self.hub.publish(make_event("review.started", agent_id, {"review_id": job["id"], "run_id": run["id"], "trigger": job["trigger"], "source_run_id": src["id"],
                                                                 "source_session_id": src["session_id"], "signals": [s["id"] for s in signals]}, run_id=run["id"]))
        return run["id"]

    # ------------------------------------------------------------------ ngân sách
    @staticmethod
    def _run_context_tokens(run: dict[str, Any]) -> int:
        """Ước lượng cỡ ngữ cảnh (token) của session lúc run kết thúc. `runs.usage` có thể bằng 0 → ưu tiên `model_usage`
        (số thật theo model). Số là LUỸ KẾ cả run: lấy max(input + cache_creation, tổng/num_turns)."""
        mu = run.get("model_usage") or {}
        inp = cr = cc = 0
        for m in (mu.values() if isinstance(mu, dict) else []):
            if not isinstance(m, dict):
                continue
            inp += int(m.get("inputTokens") or m.get("input_tokens") or 0)
            cr += int(m.get("cacheReadInputTokens") or m.get("cache_read_input_tokens") or 0)
            cc += int(m.get("cacheCreationInputTokens") or m.get("cache_creation_input_tokens") or 0)
        if inp + cr + cc == 0:
            u = run.get("usage") or {}
            if isinstance(u, dict):
                inp = int(u.get("input_tokens") or 0)
                cr = int(u.get("cache_read_input_tokens") or 0)
                cc = int(u.get("cache_creation_input_tokens") or 0)
        total = inp + cr + cc
        if total == 0:
            return 0
        return max(inp + cc, total // max(1, int(run.get("num_turns") or 1)))

    def context_tokens(self, src: dict[str, Any]) -> int:
        """Token ngữ cảnh lần cuối biết được của session nguồn: run nguồn, không có số thì run trước đó cùng session."""
        t = self._run_context_tokens(src)
        if t or not src.get("session_id"):
            return t
        for r in self.conn.execute("SELECT id FROM runs WHERE session_id=? AND id!=? ORDER BY queued_at DESC LIMIT 5", (src["session_id"], src["id"])).fetchall():
            t = self._run_context_tokens(self.hub.store.get_run(r[0]) or {})
            if t:
                return t
        return 0

    def review_budget(self, src: dict[str, Any], *, retry_after_budget: bool = False) -> float | None:
        """--max-budget-usd của review fork. `review_budget_usd` null → None (không trần riêng, như run thường: limits của agent /
        runs.default_max_budget_usd / phần còn lại ngân sách ngày). Ngược lại = base + nạp lại ngữ cảnh, chặn trên ở
        `review_budget_max_usd` (không bao giờ dưới base). Lần thử lại sau `budget_exceeded` → dùng thẳng trần."""
        base = self.cfg("review_budget_usd")
        if not base:
            return None
        base = float(base)
        mx = self.cfg("review_budget_max_usd")
        rate = float(self.cfg("review_reload_usd_per_mtok") or 0)
        want = base + self.context_tokens(src) / 1_000_000 * rate
        if retry_after_budget:
            want = float(mx) if mx else want * 2
        if mx:
            want = min(want, float(mx))
        return round(max(base, want), 4)

    def _attempts(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        res = job.get("result") if isinstance(job.get("result"), dict) else {}
        return list((res or {}).get("attempts") or [])

    def _requeue(self, job: dict[str, Any], attempts: list[dict[str, Any]], delay_s: float, reason: str) -> str:
        """Xếp job lại hàng (Signal giữ nguyên, chưa xử lý). Agent đã có job queued khác → gộp Signal + lịch sử thử vào đó."""
        nb = _utc_in(delay_s)
        q = self.conn.execute("SELECT id FROM review_jobs WHERE agent_id=? AND status='queued' AND id!=? ORDER BY created_at LIMIT 1",
                              (job["agent_id"], job["id"])).fetchone()
        if q is not None:
            other = self.get(q[0])
            sids = list(dict.fromkeys(list(other.get("signal_ids") or []) + list(job.get("signal_ids") or [])))
            self.hub.store._update("review_jobs", "id", other["id"], {"signal_ids": sids, "not_before": max(other.get("not_before") or "", nb),
                                                                     "result": {"attempts": self._attempts(other) + attempts}, "updated_at": now_iso()})
            self._end(job, "cancelled", reason=f"{reason} — gộp vào {other['id']}", result={"attempts": attempts})
            target = other["id"]
        else:
            self.hub.store._update("review_jobs", "id", job["id"], {"status": "queued", "run_id": None, "not_before": nb, "reason": reason,
                                                                   "result": {"attempts": attempts}, "updated_at": now_iso()})
            target = job["id"]
        self._kick(delay_s)
        return target

    def _end(self, job: dict[str, Any], status: str, *, reason: str | None = None, result: dict[str, Any] | None = None, cost: float | None = None) -> None:
        self.hub.store._update("review_jobs", "id", job["id"], {"status": status, "reason": reason, "result": result, "cost_usd": cost, "ended_at": now_iso(), "updated_at": now_iso()})

    def finalize(self, run: dict[str, Any]) -> None:
        r = self.conn.execute("SELECT id FROM review_jobs WHERE run_id=?", (run["id"],)).fetchone()
        if r is None:
            return
        job = self.get(r[0])
        if job["status"] not in ("running",):
            return
        props = [{k: p.get(k) for k in ("id", "status", "target", "op", "target_ref", "risk_class", "block_reason")}
                 for p in (self.hub.store._row("proposals", x) for x in self.conn.execute("SELECT * FROM proposals WHERE review_job_id=? ORDER BY created_at", (job["id"],)))]
        outcome = run.get("outcome") or run.get("status")
        self.svc.read_marks.pop(run["id"], None)  # proposal đã lưu bằng chứng đọc của chính nó (pipeline._was_read)
        attempts = self._attempts(job) + [{"run_id": run["id"], "outcome": outcome, "cost_usd": run.get("cost_usd"), "proposals": len(props)}]
        total_cost = round(sum(float(a.get("cost_usd") or 0) for a in attempts), 6)
        retry_of = outcome if outcome == "rate_limited" or (outcome == "budget_exceeded" and not props) else None
        exhausted = None
        if retry_of:
            n = sum(1 for a in attempts if a.get("outcome") == retry_of)
            cap = int(self.cfg("review_rate_limit_retries" if retry_of == "rate_limited" else "review_budget_retries") or 0)
            if n <= cap:
                base = float(self.cfg("review_retry_backoff_s") or 0)
                delay = min(3600.0, base * (2 ** (n - 1))) if retry_of == "rate_limited" else float(self.cfg("review_debounce_s") or 0)
                why = f"{retry_of} — thử lại lần {n}/{cap}"
                target = self._requeue(job, attempts, delay, why)
                self.hub.publish(make_event("review.completed", job["agent_id"], {"review_id": job["id"], "run_id": run["id"], "outcome": outcome, "proposals": props,
                                                                                  "cost_usd": run.get("cost_usd"), "requeued": target, "retry": n}, run_id=run["id"]))
                return
            exhausted = f"{retry_of} sau {n} lần thử"
        status = "done" if outcome == "completed" or (outcome == "budget_exceeded" and props) else "failed"
        if outcome != "rate_limited":
            # rate limit là lỗi tạm thời của nhà cung cấp: Signal giữ nguyên cho review sau (không đánh rơi). Các kết thúc khác
            # (kể cả budget_exceeded hết lượt thử) tiêu Signal — tránh vòng review lặp lại tốn tiền trên cùng tín hiệu.
            self.svc.signals.mark_processed(list(job.get("signal_ids") or []), job["id"])
        self._end(job, status, reason=None if status == "done" else (exhausted or run.get("error") or outcome),
                  result={"outcome": outcome, "proposals": props, "summary": (run.get("summary") or "")[:1000], "attempts": attempts}, cost=total_cost)
        self.hub.publish(make_event("review.completed", job["agent_id"], {"review_id": job["id"], "run_id": run["id"], "outcome": outcome, "proposals": props,
                                                                          "cost_usd": run.get("cost_usd")}, run_id=run["id"]))

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] == "queued":
            self._end(job, "cancelled", reason="người huỷ")
        elif job["status"] == "running" and job.get("run_id"):
            asyncio.ensure_future(self.hub.runs.stop(job["run_id"]))
        return self.get(job["id"])


def jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))
