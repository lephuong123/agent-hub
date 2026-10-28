"""RPC `learn.*`, `knowledge.*`, `learnself.call` (MCP hub-self) trên daemon — M4-A.

`register_rpc(daemon)` gọi TRƯỚC `mcp_server.rpc.register_rpc` (để method thay đổi trạng thái được bọc audit — tên nằm
trong `MUTATING`). Bọc thêm `run.stop` để ghi Signal `cancel` khi NGƯỜI dừng run (CLI/TUI/MCP đều đi qua method này).
`learn.policy_set` / `knowledge.pin` là thao tác của NGƯỜI (chính sách an toàn): agent không gọi được socket hub (guard M1)
và lệnh CLI tương ứng bị bộ phân loại coi là `policy_change` (forbidden).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..errors import HubError

if TYPE_CHECKING:
    from ..daemon import Daemon


def _req(p: dict[str, Any], key: str) -> Any:
    if p.get(key) is None:
        raise HubError(f"thiếu tham số '{key}'", code=-32602)
    return p[key]


class LearningRpc:
    def __init__(self, daemon: "Daemon") -> None:
        self.d = daemon
        self.hub = daemon.hub

    @property
    def L(self):
        return self.hub.learning

    @property
    def K(self):
        return self.hub.knowledge

    # ------------------------------------------------------------------ learn.*
    def feed(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        since = p.get("since")
        if not since:
            since = (datetime.now(timezone.utc) - timedelta(days=int(p.get("days") or 1))).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        where = ["ts>=?", "(type LIKE 'learning.%' OR type LIKE 'review.%' OR (type='guard.blocked' AND payload LIKE '%\"source\": \"learning\"%') OR type='cache.warning')"]
        args: list[Any] = [since]
        if p.get("agent"):
            where.append("agent_id=?")
            args.append(p["agent"])
        sql = "SELECT ts, type, agent_id, run_id, payload FROM events WHERE " + " AND ".join(where) + " ORDER BY ts DESC LIMIT ?"
        import json

        return [{"ts": r[0], "type": r[1], "agent_id": r[2], "run_id": r[3], "payload": json.loads(r[4])}
                for r in self.hub.store.conn.execute(sql, (*args, int(p.get("limit") or 100)))]

    def journey(self, p: dict[str, Any]) -> dict[str, Any]:
        agent = _req(p, "agent")
        self.hub.agents.get_row(agent)
        items: list[dict[str, Any]] = []
        for s in self.L.signals.list(agent, limit=int(p.get("limit") or 50)):
            if s["kind"] == "failure_obs":
                continue
            items.append({"ts": s["created_at"], "what": "signal", "id": s["id"], "kind": s["kind"], "run_id": s.get("run_id"), "review": s.get("processed_by_review_id"),
                          "note": (s.get("evidence") or {}).get("note") or (s.get("evidence") or {}).get("text")})
        for j in self.L.reviews.list(agent, limit=50):
            items.append({"ts": j.get("started_at") or j["created_at"], "what": "review", "id": j["id"], "status": j["status"], "trigger": j["trigger"], "run_id": j.get("run_id"),
                          "cost_usd": j.get("cost_usd")})
        for pr in self.L.pipeline.list(agent, limit=100):
            items.append({"ts": pr["created_at"], "what": "proposal", "id": pr["id"], "status": pr["status"], "risk_class": pr.get("risk_class"), "target": pr["target"],
                          "op": pr["op"], "target_ref": pr.get("target_ref"), "ledger_entry_id": pr.get("ledger_entry_id"), "block": pr.get("block_detail")})
        for e in self.L.ledger.list(agent, limit=100):
            items.append({"ts": e["applied_at"], "what": "ledger", "id": e["id"], "op": e["op"], "actor": e["actor"], "target": e["target"], "target_ref": e.get("target_ref"),
                          "rolled_back_by": e.get("rolled_back_by"), "summary": e.get("summary")})
        items.sort(key=lambda x: x["ts"] or "")
        return {"agent_id": agent, "items": items}

    def propose(self, p: dict[str, Any]) -> dict[str, Any]:
        by = str(p.get("created_by") or "user")
        if by not in ("user",):
            raise HubError("qua RPC/CLI chỉ tạo được proposal của NGƯỜI (created_by=user); vòng học đề xuất qua review fork", code=-32003)
        return self.L.pipeline.propose(target=_req(p, "target"), op=_req(p, "op"), payload=p.get("payload") or {}, agent_id=p.get("agent"), scope=p.get("scope") or "agent",
                                       team_id=p.get("team"), target_ref=p.get("target_ref"), rationale=p.get("rationale") or "", created_by="user")

    def rollback(self, p: dict[str, Any]) -> dict[str, Any]:
        actor = str(((p.get("_audit") or {}).get("actor")) or "user")
        return self.L.ledger.rollback(_req(p, "id"), actor if actor in ("user",) else "user", p.get("reason"), chain=bool(p.get("chain")), force=bool(p.get("force")))

    def policy_set(self, p: dict[str, Any]) -> dict[str, Any]:
        from ..config import parse_value

        v = p.get("value")
        if isinstance(v, str) and p.get("parse", True):
            v = parse_value(v)
        data = self.L.policy.set(_req(p, "key"), v)
        from ..events import make_event

        self.hub.publish(make_event("policy.changed", None, {"file": "policy/learning.yaml", "key": p["key"], "value": v, "by": "user"}))
        return data

    def signal_add(self, p: dict[str, Any]) -> dict[str, Any]:
        agent = _req(p, "agent")
        self.hub.agents.get_row(agent)
        run = self.hub.store.find_run(p["run"]) if p.get("run") else None
        sid = self.L.signals.record(agent, _req(p, "kind"), {"note": p.get("note")}, run_id=(run or {}).get("id"), source=str(((p.get("_audit") or {}).get("channel")) or "cli"))
        return {"signal_id": sid}

    # ------------------------------------------------------------------ knowledge.*
    def k_show(self, p: dict[str, Any]) -> dict[str, Any]:
        return self.K.overview(_req(p, "agent"))

    def k_view(self, p: dict[str, Any]) -> dict[str, Any]:
        return self.K.skill_view(_req(p, "agent"), _req(p, "name"), p.get("file"), touch=False)

    def k_memory(self, p: dict[str, Any]) -> dict[str, Any]:
        return self.K.memory_view(_req(p, "agent"), "user" if p.get("target") == "user" else "memory")

    def k_pin(self, p: dict[str, Any]) -> dict[str, Any]:
        scope = p.get("scope") or "agent"
        owner = p.get("agent") if scope == "agent" else p.get("team") if scope == "team" else None
        if scope == "agent":
            self.hub.agents.get_row(owner)
        target = _req(p, "target")
        if target not in ("skill", "memory", "config", "memory_entry"):
            raise HubError("target pin: skill | memory | config | memory_entry")
        ref = str(_req(p, "ref"))
        if target == "memory_entry":  # M5-A (FR-LN-14): pin TỪNG DÒNG; ref = đoạn trích duy nhất của dòng (hoặc khoá 12 hex)
            from .canary import entry_hash

            store = "user" if (p.get("file") or ("USER.md" if scope == "global" else "MEMORY.md")) == "USER.md" else "memory"
            entries = self.K.read_memory(scope, owner, store)
            hits = [e for e in entries if ref in e or entry_hash(e) == ref]
            if len(hits) != 1:
                raise HubError(f"'{ref}' khớp {len(hits)} dòng — trích dài hơn cho duy nhất")
            ref = entry_hash(hits[0])
        return self.K.set_pin(scope, owner, target, ref, bool(p.get("on", True)), by="user")

    # ------------------------------------------------------------------ M5-A: tầng team/global, phiên bản, USER, canary
    def k_tier(self, p: dict[str, Any]) -> dict[str, Any]:
        scope = _req(p, "scope")
        if scope not in ("team", "global"):
            raise HubError("scope: team | global")
        return self.K.versions.tier_overview(scope, _req(p, "team") if scope == "team" else None)

    def k_versions(self, p: dict[str, Any]) -> dict[str, Any]:
        agent = _req(p, "agent")
        self.hub.agents.get_row(agent)
        return {"agent_id": agent, "versions": self.K.versions.version_map(agent)}

    def canary_check(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        if p.get("id"):
            return [self.L.canary.check(p["id"])]
        return self.L.canary.tick()

    def methods(self) -> dict[str, Any]:
        L = lambda: self.hub.learning  # noqa: E731
        return {
            "learnself.call": lambda p: L().learnself_call(_req(p, "run_id"), _req(p, "op"), p.get("args") or {}),
            "learn.status": lambda p: L().status(p.get("agent")),
            "learn.signals": lambda p: [s for s in L().signals.list(p.get("agent"), p.get("kind"), int(p.get("limit") or 100)) if s["kind"] != "failure_obs"],
            "learn.signal_add": self.signal_add,
            "learn.refine": lambda p: L().reviews.refine(_req(p, "agent"), p.get("focus"), p.get("run")),
            "learn.reviews": lambda p: L().reviews.list(p.get("agent"), p.get("status"), int(p.get("limit") or 50)),
            "learn.review_get": lambda p: L().reviews.get(_req(p, "id")),
            "learn.review_cancel": lambda p: L().reviews.cancel(_req(p, "id")),
            "learn.proposals": lambda p: L().pipeline.list(p.get("agent"), p.get("status"), int(p.get("limit") or 100)),
            "learn.proposal_get": lambda p: L().pipeline.get(_req(p, "id")),
            "learn.propose": self.propose,
            "learn.retry": lambda p: L().pipeline.retry(_req(p, "id"), revive=bool(p.get("revive"))),
            "learn.ledger": lambda p: L().ledger.list(p.get("agent"), target=p.get("target"), actor=p.get("actor"), since=p.get("since"), limit=int(p.get("limit") or 100)),
            "learn.show": lambda p: L().ledger.show(_req(p, "id")),
            "learn.rollback": self.rollback,
            "learn.feed": self.feed,
            "learn.journey": self.journey,
            "learn.policy_get": lambda p: L().policy.get(p.get("key")),
            "learn.policy_set": self.policy_set,
            "learn.cache": lambda p: L().cache_stats(p.get("agent"), int(p.get("days") or 1)),
            "learn.tick": lambda p: L().tick(),
            "knowledge.show": self.k_show,
            "knowledge.skills": lambda p: self.K.effective_skills(_req(p, "agent")),
            "knowledge.skill_view": self.k_view,
            "knowledge.memory": self.k_memory,
            "knowledge.pin": self.k_pin,
            "knowledge.pins": lambda p: self.K.pins(p.get("scope"), p.get("agent") if p.get("scope") != "team" else p.get("team")),
            # M5-A
            "knowledge.tier": self.k_tier,
            "knowledge.versions": self.k_versions,
            "knowledge.user": lambda p: self.K.versions.user_profile(p.get("agent")),
            "learn.canaries": lambda p: L().canary.list(p.get("status"), p.get("agent"), int(p.get("limit") or 100)),
            "learn.canary_get": lambda p: L().canary.get(_req(p, "id")),
            "learn.canary_check": self.canary_check,
        }


MUTATING_M4A = ("learn.signal_add", "learn.refine", "learn.review_cancel", "learn.propose", "learn.retry", "learn.rollback", "learn.policy_set", "knowledge.pin",
                "learn.canary_check",
                # skill manager của người (learning/skill_rpc.py)
                "knowledge.skill_save", "knowledge.skill_archive", "knowledge.skill_restore", "knowledge.skill_import", "knowledge.skill_promote")


def register_rpc(daemon: "Daemon") -> None:
    r = LearningRpc(daemon)
    daemon.methods.update(r.methods())
    from .skill_rpc import SkillManageRpc

    daemon.methods.update(SkillManageRpc(daemon).methods())  # knowledge.skill_* (skill manager của người)
    orig_stop = daemon.methods.get("run.stop")
    if orig_stop is not None:
        def run_stop(p: dict[str, Any]) -> Any:
            try:  # Signal `cancel` (FR-LN-01): người dừng run — ghi TRƯỚC khi dừng để còn biết tool đang chạy
                daemon.hub.learning.signals.human_stop(str(p.get("id") or ""), str(((p.get("_audit") or {}).get("channel")) or "cli"))
            except Exception:  # pragma: no cover
                pass
            return orig_stop(p)

        daemon.methods["run.stop"] = run_stop
