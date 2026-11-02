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

from ..errors import Blocked, HubError, NotFound
from ..events import TERMINAL_RUN_EVENTS, HubEvent, make_event
from .canary import CanaryManager
from .graph_extract import GraphExtractor
from .guard import LearningGuard
from .ledger import Ledger
from .pipeline import Pipeline
from .policy import LearningPolicy
from .review import ReviewScheduler
from .signals import SignalCollector

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.learning")

KNOWLEDGE_OPS = ("skills_list", "skill_view")
AGENT_GRAPH_ACTIONS = ("add_entity", "add_alias", "add_fact", "invalidate_fact")  # merge/supersede/invalidate_entity: consolidation/người
MEMORY_OP = "memory"  # tool `memory` của agent đang chạy (kiểu Hermes): ghi MEMORY.md/USER.md tầng agent ngay trong session
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
        self.graph_extract = GraphExtractor(hub, self)  # graph memory PR write: trích xuất theo lô cuối session / PreCompact / xoay session
        from .graph_quality import GraphQuality

        self.graph_quality = GraphQuality(hub)  # graph memory PR quality: độ tin cậy theo kết quả run + consolidation (vòng nền learning_ops)
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
        try:
            self.graph_extract.on_run_terminal(run)
        except Exception:
            log.exception("xếp trích xuất graph lỗi run %s", run_id)
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
        for h in list(self.graph_extract._pending.values()):
            h.cancel()
        self.graph_extract._pending.clear()
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
        allowed = LEARNING_OPS if is_review else KNOWLEDGE_OPS + ((MEMORY_OP,) if self.hub.config.get("learning.agent_memory_tool") else ())
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
        if op == MEMORY_OP:
            return self._agent_memory(run, args)
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
            store = "user" if args.get("target") == "user" else "memory"
            if args.get("operations") is not None:
                return self.pipeline.propose(target="memory", op="patch", scope=scope, payload={"store": store, "operations": _memory_ops(args)}, **common)
            return self.pipeline.propose(target="memory", op={"add": "add", "patch": "patch", "remove": "delete"}.get(action, "add"), scope=scope,
                                         payload={"store": store, "action": action, "content": args.get("content"), "old_text": args.get("old_text")}, **common)
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

    def memory_scopes(self, agent_id: str) -> list[str]:
        """Tầng tool `memory` của agent được ghi: luôn `agent`; `team` khi là lead của một team (chỉ team mình lead), điều phối viên
        hoặc policy `agents.<id>.shared_memory_write`; `global` (kể cả USER dùng chung) chỉ điều phối viên hoặc policy. Daemon là ranh
        giới — schema tool chỉ ẩn/hiện `scope`."""
        mgr = getattr(self.hub, "manager", None)
        if (mgr is not None and mgr.is_manager(agent_id)) or self.policy.shared_memory_write(agent_id):
            return ["agent", "team", "global"]
        lead = self.hub.store.conn.execute("SELECT 1 FROM team_members m JOIN teams t ON t.id=m.team_id WHERE m.agent_id=? AND m.role='lead' "
                                           "AND t.archived_at IS NULL LIMIT 1", (agent_id,)).fetchone()
        return ["agent", "team"] if lead else ["agent"]

    def _shared_team(self, agent_id: str, team_id: str | None, wide: bool) -> str:
        """scope=team của tool `memory`: lead chỉ ghi team mình LEAD; điều phối viên/policy ghi mọi team agent thuộc (như review)."""
        if wide:
            return self.pipeline._resolve_team(agent_id, team_id)
        led = [r[0] for r in self.hub.store.conn.execute("SELECT m.team_id FROM team_members m JOIN teams t ON t.id=m.team_id WHERE m.agent_id=? "
                                                        "AND m.role='lead' AND t.archived_at IS NULL ORDER BY m.team_id", (agent_id,))]
        if team_id:
            if team_id not in led:
                raise Blocked(f"tool memory: '{agent_id}' không phải lead của team '{team_id}' — không ghi được tầng team đó")
            return team_id
        if len(led) != 1:
            raise HubError(f"'{agent_id}' lead {len(led)} team — chỉ rõ team_id")
        return led[0]

    def _agent_memory(self, run: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        """Tool `memory` (kiểu Hermes): agent ghi memory ngay trong session. Vẫn qua pipeline (guard, threat scan, hạn mức ký tự,
        ledger, rollback) với `created_by=agent`; KHÔNG giới hạn số lần (`rate.agent_tool_max_per_day`, mặc định 0 = không giới hạn).
        `scope` team/global (`memory_scopes`): áp dụng ngay cho mọi agent của tầng, canary `watch` tự rollback theo mục khi hồi quy.
        Hạn mức ký tự xét trên kết quả cuối của lô. Ghi xuống đĩa ngay nhưng system prompt của session hiện tại KHÔNG đổi
        (snapshot đóng băng, giữ prompt cache) — kết quả trả trạng thái sống."""
        opts = run.get("run_opts") or {}
        if run.get("kind") in ("eval", "eval_judge", "curator") or opts.get("eval_overlay"):
            raise HubError("tool memory không dùng được trong run eval/curator", code=-32003)
        if args.get("target") == "graph":
            return self._agent_graph(run, args)
        agent_id = run["agent_id"]
        store = "user" if args.get("target") == "user" else "memory"
        scope = str(args.get("scope") or "agent")
        if scope not in ("agent", "team", "global"):
            raise HubError("scope phải là agent|team|global")
        allowed = self.memory_scopes(agent_id)
        if scope not in allowed:
            raise Blocked(f"tool memory: '{agent_id}' không được ghi tầng {scope} (được: {', '.join(allowed)}) — người bật bằng "
                          f"`cfharness learn policy set agents.{agent_id}.shared_memory_write true`")
        common = {"agent_id": agent_id, "run_id": run["id"], "created_by": "agent", "rationale": str(args.get("rationale") or "")}
        payload = {"store": store, "operations": _memory_ops(args)}
        owner = None
        if scope == "agent":
            res = self.pipeline.propose(target="memory", op="patch", scope="agent", payload=payload, **common)
        elif store == "user":  # USER dùng chung = một hồ sơ cho mọi agent
            if scope != "global":
                raise HubError("hồ sơ người dùng dùng chung chỉ có ở tầng global (scope='global', target='user')")
            res = self.pipeline.propose(target="user_profile", op="patch", scope="global", payload=payload, **common)
        else:
            owner = self._shared_team(agent_id, args.get("team_id"), "global" in allowed) if scope == "team" else None
            res = self.pipeline.propose(target="memory", op="patch", scope=scope, team_id=owner, payload=payload, **common)
        ks = self.hub.knowledge
        if scope == "agent":
            view = ks.memory_view(agent_id, store)
        else:
            from ..knowledge.store import memory_chars

            entries = ks.read_memory(scope, owner, store)
            view = {"entries": entries, "chars": memory_chars(entries), "limit": ks.memory_limit(scope, store)}
        out: dict[str, Any] = {k: res.get(k) for k in ("status", "reason", "rule", "not_before") if res.get(k) is not None}
        if res.get("status") == "canary":  # watch: đã áp dụng cho mọi agent của tầng
            out["status"] = "applied"
            out["watch"] = res.get("canary_id")
        out.update(target=store, scope=scope, usage=f"{view['chars']}/{view['limit']} chars", entries=view["entries"])
        if owner:
            out["team_id"] = owner
        if out.get("status") == "applied":
            out["note"] = ("Saved to disk. Your system prompt for this session is unchanged; the change is visible from the next session."
                           + ("" if scope == "agent" else f" Shared with every agent of this {scope} tier; it is watched and rolled back automatically if it causes regressions."))
        return out

    def _agent_graph(self, run: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        """Tool `memory` với `target='graph'` (graph memory PR write): agent ghi fact có cấu trúc (subject, relation, object, câu fact)
        vào graph tầng agent/team/global — quyền tầng như MEMORY.md (`graph_write` → `memory_scopes`). `forget` đóng hiệu lực fact
        (không xoá). Provenance: episode = các lượt NGƯỜI gần nhất của session (không phải output tool), `source_kind=agent_tool`."""
        ops: list[dict[str, Any]] = []
        for f in args.get("facts") or []:
            if not isinstance(f, dict):
                continue
            op: dict[str, Any] = {"action": "add_fact", "src": f.get("subject"), "relation": f.get("relation"), "fact": f.get("fact")}
            for a, b in (("object", "dst"), ("subject_type", "src_type"), ("object_type", "dst_type"), ("valid_from", "valid_from"), ("multi", "multi")):
                if f.get(a) not in (None, ""):
                    op[b] = f[a]
            ops.append(op)
        for v in args.get("forget") or []:
            if not isinstance(v, dict):
                continue
            if v.get("fact_id"):
                ops.append({"action": "invalidate_fact", "fact_id": v["fact_id"]})
            else:
                op = {"action": "invalidate_fact", "src": v.get("subject"), "relation": v.get("relation")}
                if v.get("object"):
                    op["dst"] = v["object"]
                ops.append(op)
        if not ops:
            raise HubError("target='graph' cần facts=[{subject, relation, object?, fact}] hoặc forget=[{subject, relation}]")
        scope = str(args.get("scope") or "agent")
        ge = self.graph_extract
        key = ge.source_key(run)
        users = [m for m in (ge.collect(key) if key else []) if m["role"] == "user"][-3:]
        text = "\n".join(f"[#{m['id']} user] {m['text'][:2000]}" for m in users) or f"(no user message indexed for run {run['id']})"
        episode = {"text": text, "run_id": run["id"], "session_id": run.get("session_id"), "source_kind": "agent_tool",
                   "source_ref": f"run:{run['id']}"}
        res = self.graph_write(run["agent_id"], ops, scope=scope, team_id=args.get("team_id"), episode=episode, created_by="agent",
                               run_id=run["id"], rationale=str(args.get("rationale") or ""))
        out: dict[str, Any] = {k: res.get(k) for k in ("status", "reason", "rule", "not_before", "target_ref") if res.get(k) is not None}
        if res.get("status") == "canary":
            out["status"] = "applied"
            out["watch"] = res.get("canary_id")
        out.update(target="graph", scope=scope)
        prop = self.pipeline.get(res["proposal_id"]) if res.get("proposal_id") else {}
        if prop.get("diff"):
            out["changes"] = [ln for ln in str(prop["diff"]).splitlines() if not ln.startswith("+ episode")][:40]
        if out.get("status") == "applied":
            out["note"] = ("Saved to your memory graph. Facts are recalled in future sessions; a new value for the same subject and relation "
                           "closes the old fact (kept as history).")
        return out

    def agent_propose(self, *, target: str, op: str, payload: dict[str, Any], agent_id: str | None, scope: str = "agent",
                      team_id: str | None = None, target_ref: str | None = None, rationale: str = "", run_id: str | None = None) -> dict[str, Any]:
        """H-05: `learn.propose` từ tiến trình agent (daemon hạ xuống `created_by=agent`). Quyền tầng GIỐNG tool `memory`
        (`memory_scopes`) cho memory/USER/graph — trước đây đường này gọi thẳng pipeline nên worker thường ghi được tầng team/global.
        Graph đi qua `graph_write` (lọc thao tác consolidation + `confidence` agent tự đặt)."""
        if not agent_id:
            raise HubError("learn.propose từ agent cần agent")
        if target == "graph":
            pl = payload if isinstance(payload, dict) else {}
            return self.graph_write(agent_id, list(pl.get("operations") or []), scope=scope, team_id=team_id,
                                    episode=pl.get("episode") if isinstance(pl.get("episode"), dict) else None, created_by="agent",
                                    run_id=run_id, rationale=rationale)
        if target == "user_profile":
            scope = "global"
        if target in ("memory", "user_profile") and scope != "agent":
            allowed = self.memory_scopes(agent_id)
            if scope not in allowed:
                raise Blocked(f"learn propose: '{agent_id}' không được ghi tầng {scope} (được: {', '.join(allowed)}) — người bật bằng "
                              f"`cfharness learn policy set agents.{agent_id}.shared_memory_write true`")
            if scope == "team":
                team_id = self._shared_team(agent_id, team_id, "global" in allowed)
        return self.pipeline.propose(target=target, op=op, payload=payload, agent_id=agent_id, scope=scope, team_id=team_id,
                                     target_ref=target_ref, rationale=rationale, created_by="agent", run_id=run_id)

    def graph_write(self, agent_id: str, operations: list[dict[str, Any]], *, scope: str = "agent", team_id: str | None = None,
                    episode: dict[str, Any] | None = None, created_by: str = "agent", run_id: str | None = None, rationale: str = "") -> dict[str, Any]:
        """Ghi graph memory (docs/dev-notes/plan-graph-memory.md) — cửa vào chung cho trích xuất cuối session (PR write) và tool hub-self.
        Quyền tầng GIỐNG tool `memory`: `created_by` agent/review chỉ ghi tầng trong `memory_scopes` (agent; lead → team mình lead;
        điều phối viên / policy `shared_memory_write` → mọi tầng). Người (`created_by=user`) ghi mọi tầng. Vẫn qua pipeline: guard (threat
        scan, trần từng fact, trùng lặp), suất `rate.graph_writes_per_agent_per_day`, ledger (rollback theo hàng), canary watch/canary."""
        if scope not in ("agent", "team", "global"):
            raise HubError("scope phải là agent|team|global")
        if run_id:
            run = self.hub.store.get_run(run_id) or {}
            if run.get("kind") in ("eval", "eval_judge", "curator") or (run.get("run_opts") or {}).get("eval_overlay"):
                raise HubError("graph memory không ghi được từ run eval/curator", code=-32003)
        owner = None
        if created_by != "user":
            allowed = self.memory_scopes(agent_id)
            if scope not in allowed:
                raise Blocked(f"graph: '{agent_id}' không được ghi tầng {scope} (được: {', '.join(allowed)}) — người bật bằng "
                              f"`cfharness learn policy set agents.{agent_id}.shared_memory_write true`")
            if scope == "team":
                owner = self._shared_team(agent_id, team_id, "global" in allowed)
        elif scope == "team":
            owner = self.pipeline._resolve_team(agent_id, team_id)
        ops = list(operations or [])
        if created_by == "agent":  # agent (tool `memory`, `learn propose`): không tự đặt độ tin cậy, không thao tác của consolidation
            clean: list[Any] = []
            for o in ops:
                if not isinstance(o, dict):
                    clean.append(o)
                    continue
                if str(o.get("action") or "") not in AGENT_GRAPH_ACTIONS:
                    raise Blocked(f"graph: agent chỉ được {', '.join(AGENT_GRAPH_ACTIONS)} — '{o.get('action')}' là thao tác của consolidation/người")
                clean.append({k: v for k, v in o.items() if k != "confidence" and not str(k).startswith("_")})
            ops = clean
        payload: dict[str, Any] = {"operations": ops}
        if episode:
            payload["episode"] = dict(episode)
        return self.pipeline.propose(target="graph", op="patch", agent_id=agent_id, scope=scope, team_id=owner, payload=payload,
                                     created_by=created_by, run_id=run_id, rationale=rationale)

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


def _memory_ops(args: dict[str, Any]) -> list[dict[str, Any]]:
    """`operations` (lô) hoặc một thao tác `action/content/old_text` → danh sách thao tác chuẩn hoá."""
    raw = args.get("operations")
    if raw is None:
        raw = [{"action": args.get("action"), "content": args.get("content"), "old_text": args.get("old_text")}]
    if not isinstance(raw, list):
        raise HubError("operations phải là mảng")
    return [{"action": str((o or {}).get("action") or ""), "content": (o or {}).get("content"), "old_text": (o or {}).get("old_text")}
            for o in raw if isinstance(o, dict)] or [{"action": ""}]


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
