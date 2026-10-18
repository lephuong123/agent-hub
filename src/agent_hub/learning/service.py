"""`LearningService` (`hub.learning`) — lõi học M4-A nối vào hub. `attach(hub)` gọi trong `Hub.__init__`.

Thành phần: `policy` (công tắc/bounds/rate — chính sách an toàn), `signals` (FR-LN-01), `reviews` (FR-LN-02/03),
`pipeline` (FR-LN-04/05/18/19), `guard` (FR-LN-07), `ledger` (FR-LN-06). `hub.knowledge` = `KnowledgeStore` (FR-LN-11).
Listener trên bus (đồng bộ, không chặn): Signal → trigger; event run → bộ phát hiện + bộ đếm + kết thúc review.
Vòng nền (`learning.tick_s`) khởi động ở `hub.started`: review đến hạn, proposal hoãn đến hạn.
`learnself_call(run_id, op, args)`: RPC từ MCP `hub-self` (vai trò `knowledge` cho run thường, `learning` cho review fork).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import HubError, NotFound
from ..events import TERMINAL_RUN_EVENTS, HubEvent, make_event
from .canary import CanaryManager
from .guard import LearningGuard
from .ledger import Ledger
from .pipeline import Pipeline
from .policy import LearningPolicy
from .review import ReviewScheduler
from .signals import SignalCollector

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("agent_hub.learning")

KNOWLEDGE_OPS = ("skills_list", "skill_view")
LEARNING_OPS = ("skills_list", "skill_view", "memory_view", "propose_memory", "propose_skill_new", "propose_skill_patch", "propose_skill_merge",
                "propose_skill_archive", "propose_config_change", "propose_user_profile")


class LearningService:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.policy = LearningPolicy(hub.paths.home)
        self.ledger = Ledger(hub)
        self.signals = SignalCollector(hub)
        self.guard = LearningGuard(hub)
        self.pipeline = Pipeline(hub, self)
        self.reviews = ReviewScheduler(hub, self)
        self.canary = CanaryManager(hub, self)  # M5-A: L3 tầng team/global/USER — canary/shadow (FR-LN-16)
        self.read_marks: dict[str, set[tuple[str, str, str, str]]] = {}
        self._task: asyncio.Task | None = None
        hub.bus.add_listener(self._on_event)

    # ------------------------------------------------------------------ công tắc (FR-LN-15)
    def switches(self, agent_id: str | None) -> dict[str, bool]:
        tps: list[dict[str, Any]] = []
        if agent_id:
            try:
                for r in self.hub.store.conn.execute("SELECT t.policies FROM team_members m JOIN teams t ON t.id=m.team_id WHERE m.agent_id=? AND t.archived_at IS NULL", (agent_id,)):
                    import json

                    tps.append(json.loads(r[0] or "{}"))
            except Exception:  # pragma: no cover
                pass
        return self.policy.switches(agent_id, tps)

    # ------------------------------------------------------------------ bus
    def _on_event(self, ev: HubEvent) -> None:
        try:
            if ev.type == "hub.started":
                self.start()
                return
            if ev.type == "hub.stopping":
                self.stop()
                return
            if ev.type == "learning.signal":
                sig = self.signals.get(str(ev.payload.get("signal_id") or ""))
                if sig is not None:
                    self.reviews.on_signal(sig)
                self.canary.on_event(ev)
                return
            self.signals.on_event(ev)
            self.canary.on_event(ev)
            if ev.type in TERMINAL_RUN_EVENTS and ev.run_id and ev.source == "hub" and ev.type != "run.waiting_human":
                run = self.hub.store.get_run(ev.run_id)
                if run is not None and run.get("status") not in ("queued", "running"):
                    self._call_soon(self._after_run, run["id"])
        except Exception:  # pragma: no cover - listener không được làm hỏng bus
            log.exception("learning listener lỗi với %s", ev.type)

    def _call_soon(self, fn: Any, *a: Any) -> None:
        try:
            asyncio.get_running_loop().call_soon(fn, *a)
        except RuntimeError:
            fn(*a)

    def _after_run(self, run_id: str) -> None:
        run = self.hub.store.get_run(run_id)
        if run is None:
            return
        try:
            self.signals.on_run_terminal(run)
        except Exception:
            log.exception("bộ phát hiện Signal lỗi run %s", run_id)
        self.reviews.on_run_terminal(run)
        self._cache_watch(run)

    # ------------------------------------------------------------------ vòng nền
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self.loop())
        except RuntimeError:
            self._task = None

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(float(self.hub.config.get("learning.tick_s") or 15))
                self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("learning tick lỗi")

    def tick(self) -> dict[str, Any]:
        started = self.reviews.tick()
        canary = [c for c in self.canary.tick() if c.get("action") not in ("watching", "none")]
        applied = self.pipeline.process_deferred()
        return {"reviews_started": started, "deferred_processed": [a.get("proposal_id") for a in applied], "canary": canary}

    # ------------------------------------------------------------------ FR-OB-04
    def cache_stats(self, agent_id: str | None = None, days: int = 1) -> dict[str, Any]:
        from .cache import cache_stats

        return cache_stats(self.hub, agent_id=agent_id, days=days)

    def _cache_watch(self, run: dict[str, Any]) -> None:
        if run.get("kind") in ("review", "eval", "eval_judge") or run.get("resumed_from_session"):
            return
        from .cache import hash_changes_today

        lim = int(self.hub.config.get("learning.cache_warn_hash_changes_per_day") or 0)
        n = hash_changes_today(self.hub, run["agent_id"])
        if lim and n > lim:
            day = datetime.now().strftime("%Y-%m-%d")
            flags = dict(self.hub.store.state_get("learning.cache_warned", {}) or {})
            if flags.get(run["agent_id"]) != day:
                flags[run["agent_id"]] = day
                self.hub.store.state_set("learning.cache_warned", flags)
                self.hub.publish(make_event("cache.warning", run["agent_id"], {"prompt_hash_changes_today": n, "limit": lim,
                                                                               "hint": "system prompt đổi nhiều lần trong ngày → mất prompt cache (FR-OB-04)"}))

    # ------------------------------------------------------------------ MCP hub-self (learnself.call)
    def learnself_call(self, run_id: str, op: str, args: dict[str, Any]) -> Any:
        run = self.hub.store.get_run(run_id)
        if run is None:
            raise NotFound(f"run {run_id} không tồn tại")
        opts = run.get("run_opts") or {}
        is_review = bool(opts.get("learning_review"))
        allowed = LEARNING_OPS if is_review else KNOWLEDGE_OPS
        if op not in allowed:
            raise HubError(f"tool '{op}' không dùng được trong run này", code=-32003)
        if run.get("status") not in ("running", "queued"):
            raise HubError(f"run {run_id} không còn chạy (status={run.get('status')})", code=-32003)
        agent_id = run["agent_id"]
        ks = self.hub.knowledge
        home = None
        if opts.get("eval_overlay"):
            home = Path(str((opts["eval_overlay"] or {}).get("home") or "")) or None
        if op == "skills_list":
            return {"skills": [{"name": s["name"], "description": s.get("description"), "scope": s.get("scope"), "pinned": bool(s.get("pinned")),
                                "created_by": s.get("created_by"), "use_count": s.get("use_count"), "shadows": s.get("shadows")}
                               for s in ks.effective_skills(agent_id, home=home)]}
        if op == "skill_view":
            name = str(args.get("name") or "")
            res = ks.skill_view(agent_id, name, args.get("file_path"), touch=not is_review, home=home)
            s = ks.find_skill(agent_id, name, home=home) or {}
            self.read_marks.setdefault(run_id, set()).add((s.get("scope") or "agent", s.get("owner") or "", name, res["file_path"]))
            return res
        if op == "memory_view":
            if args.get("scope") in ("team", "global"):  # M5-A: xem kho tầng team/global theo phiên bản agent đang thấy
                sc = str(args["scope"])
                owner = self.pipeline._resolve_team(agent_id, None) if sc == "team" else None
                st = "user" if (args.get("target") == "user" and sc == "global") else "memory"
                entries = ks.versions.read_memory(agent_id, sc, owner, st)
                from ..knowledge.store import memory_chars

                return {"scope": sc, "owner": owner, "target": st, "entries": entries, "chars": memory_chars(entries), "limit": ks.memory_limit(sc, st)}
            return ks.memory_view(agent_id, "user" if args.get("target") == "user" else "memory")
        job_id = opts.get("review_job_id")
        job = self.reviews.get(job_id) if job_id else {}
        common = {"agent_id": agent_id, "created_by": "review", "review_job_id": job_id, "run_id": run_id, "rationale": str(args.get("rationale") or ""),
                  "evidence_signal_ids": list((job or {}).get("signal_ids") or [])}
        scope = str(args.get("scope") or "agent")
        if scope not in ("agent", "team", "global"):
            raise HubError("scope phải là agent|team|global")
        if op == "propose_memory":
            action = str(args.get("action") or "add")
            if scope != "agent" and args.get("target") == "user":
                raise HubError("hồ sơ người dùng dùng chung: dùng propose_user_profile")
            return self.pipeline.propose(target="memory", op={"add": "add", "patch": "patch", "remove": "delete"}.get(action, "add"), scope=scope,
                                         payload={"store": "user" if args.get("target") == "user" else "memory", "action": action,
                                                  "content": args.get("content"), "old_text": args.get("old_text")}, **common)
        if op == "propose_user_profile":
            action = str(args.get("action") or "add")
            return self.pipeline.propose(target="user_profile", op={"add": "add", "patch": "patch", "remove": "delete"}.get(action, "add"), scope="global",
                                         payload={"store": "user", "action": action, "content": args.get("content"), "old_text": args.get("old_text")}, **common)
        if op == "propose_skill_new":
            return self.pipeline.propose(target="skill", op="add", scope=scope, payload={k: args.get(k) for k in ("name", "description", "body")}, **common)
        if op == "propose_skill_patch":
            return self.pipeline.propose(target="skill", op="patch", payload={k: args.get(k) for k in ("name", "file_path", "old_string", "new_string", "content", "remove_file")}, **common)
        if op == "propose_skill_merge":
            return self.pipeline.propose(target="skill", op="merge", payload={k: args.get(k) for k in ("name", "absorbed_into")}, **common)
        if op == "propose_skill_archive":
            return self.pipeline.propose(target="skill", op="archive", payload={k: args.get(k) for k in ("name", "absorbed_into", "reason")}, **common)
        if op == "propose_config_change":
            ch = dict(args.get("changes") or {})
            return self.pipeline.propose(target="config", op="patch", payload={"changes": ch, "soul": args.get("soul")}, **common)
        raise HubError(f"op không hỗ trợ: {op}")

    # ------------------------------------------------------------------ hợp đồng B
    def materialize(self, proposal: dict[str, Any], home: Path, cfg: dict[str, Any]) -> dict[str, Any]:
        return self.pipeline.materialize(proposal, home, cfg)

    def status(self, agent_id: str | None = None) -> dict[str, Any]:
        conn = self.hub.store.conn
        since = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        q = " AND agent_id=?" if agent_id else ""
        a = (agent_id,) if agent_id else ()
        props = {r[0]: r[1] for r in conn.execute(f"SELECT status, COUNT(*) FROM proposals WHERE created_at>=?{q} GROUP BY 1", (since, *a))}
        out = {
            "policy": {k: self.policy.get(k) for k in ("enabled", "auto_apply")},
            "switches": self.switches(agent_id) if agent_id else None,
            "today": {"proposals": props, "applied": int(conn.execute(f"SELECT COUNT(*) FROM ledger_entries WHERE applied_at>=? AND op!='rollback'{q}", (since, *a)).fetchone()[0]),
                      "rollbacks": int(conn.execute(f"SELECT COUNT(*) FROM ledger_entries WHERE applied_at>=? AND op='rollback'{q}", (since, *a)).fetchone()[0]),
                      "reviews": int(conn.execute(f"SELECT COUNT(*) FROM review_jobs WHERE created_at>=?{q}", (since, *a)).fetchone()[0]),
                      "review_cost_usd": round(float(conn.execute(f"SELECT COALESCE(SUM(cost_usd),0) FROM runs WHERE kind='review' AND queued_at>=?{q}", (since, *a)).fetchone()[0]), 6)},
            "queued_reviews": [{k: j.get(k) for k in ("id", "agent_id", "trigger", "not_before", "source_run_id")} for j in self.reviews.list(agent_id, "queued")],
            "running_reviews": [{k: j.get(k) for k in ("id", "agent_id", "run_id")} for j in self.reviews.list(agent_id, "running")],
            "deferred": [{k: p.get(k) for k in ("id", "agent_id", "target", "target_ref", "status", "not_before", "block_detail")}
                         for st in ("deferred_rate_limited", "deferred_no_eval") for p in self.pipeline.list(agent_id, st, 50)],
            "pending_approval_queue": 0,  # FR-LN-04: KHÔNG có hàng chờ duyệt
        }
        if agent_id:
            out["counters"] = self.reviews.counters_for(agent_id)
        return out


def attach(hub: "Hub") -> None:
    from ..knowledge import KnowledgeStore

    hub.knowledge = KnowledgeStore(hub)  # type: ignore[attr-defined]
    hub.learning = LearningService(hub)  # type: ignore[attr-defined]
    hub.knowledge.archive_skill = _archive_skill_port(hub)  # type: ignore[attr-defined]


def _archive_skill_port(hub: "Hub"):
    """Cổng cho curator pha 1 của M4-B: archive (xoá mềm) qua PIPELINE (guard pin, ledger actor=curator)."""

    def archive_skill(owner: str, name: str, actor: str = "curator", reason: str | None = None) -> dict[str, Any]:
        res = hub.learning.pipeline.propose(target="skill", op="archive", agent_id=owner, payload={"name": name, "reason": f"stale: {reason or ''}"},
                                            created_by=actor if actor in ("curator", "user") else "curator", rationale=reason or "")
        if res["status"] != "applied":
            raise HubError(f"không archive được skill {owner}/{name}: {res.get('reason') or res['status']}")
        return res

    return archive_skill
