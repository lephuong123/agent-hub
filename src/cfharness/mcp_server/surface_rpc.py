"""RPC bề mặt CLI/MCP bổ sung khi hardening (luồng CLI: H-06, H-07, H-08, H-11, H-14).

Đăng ký trong `mcp_server.rpc.register_rpc` TRƯỚC bước bọc audit (method ghi nằm trong `MUTATING`), nên không cần
sửa `daemon.py`. Method:
- `agent.delete` (thay bản của daemon: thêm `detach_teams`), `agent.archive` (= delete), `agent.revert {id, to}`;
- `trash.list|restore|purge` — thùng rác hợp nhất (agent, team, MCP server); purge chỉ agent (gọi `agent.purge` của guard);
- `memory.search {query, agent_id?, scope?, limit?}` (memory/skill theo từ khoá + transcript qua FTS5 v17, KHÔNG gọi LLM) +
  `memory.search_self {run_id, args}` cho hub-self (ràng với run của tiến trình gọi);
- `session.search {mode: search|around|read|browse, …}` + `session.search_self {run_id, args}` — tìm kiếm hội thoại đầy đủ
  (session_search.py). Phạm vi nhìn tính từ NGƯỜI GỌI (auth.SCOPED_READ): agent thường chỉ thấy của mình + team,
  điều phối viên/người thấy hết, socket không token và không thuộc run → từ chối;
- `hook.observe` / `hook.timeout` — hook quan sát fail-OPEN (FR-RT-17), xem `runtime/observe_hook.py`;
- `mcp.agent_run {agent_id}` (run hiện tại/gần nhất của agent cho `hub_events(agent_id=…)`), `mcp.cancel_task {task_id}`;
- `hub.compat` — kết quả kiểm phiên bản CLI lúc daemon khởi động + cảnh báo `raw.unknown` (header TUI, `doctor`).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .. import auth as _auth
from ..errors import Blocked, HubError, NotFound
from ..events import make_event

if TYPE_CHECKING:
    from ..daemon import Daemon

MUTATING_SURFACE = ("agent.archive", "agent.revert", "trash.restore", "trash.purge", "mcp.cancel_task")
TECHNICAL_KINDS = ("review", "eval", "eval_judge", "curator", "learning")
_TOKEN = re.compile(r"[\wÀ-ỹ]+", re.U)
HOOK_EVENTS = ("PostToolUse", "UserPromptSubmit", "Stop")


def _req(p: dict[str, Any], key: str) -> Any:
    if p.get(key) is None:
        raise HubError(f"thiếu tham số '{key}'", code=-32602)
    return p[key]


# ============================================================================ memory search (FR-MS-02 hub_memory_search)
def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text or "") if len(t) > 1]


def _score(q: list[str], text: str) -> float:
    if not q:
        return 0.0
    low = text.lower()
    hits = sum(1 for t in q if t in low)
    if not hits:
        return 0.0
    phrase = 1.0 if " ".join(q) in low else 0.0
    return hits / len(q) + phrase


def _snippet(text: str, q: list[str], width: int = 240) -> str:
    low = text.lower()
    pos = min((low.find(t) for t in q if t in low), default=0)
    start = max(0, pos - width // 3)
    s = text[start : start + width].strip().replace("\n", " ")
    return ("…" if start else "") + s + ("…" if start + width < len(text) else "")


def memory_search(hub: Any, query: str, *, agent_id: str | None = None, scope: str = "all", limit: int = 10, vis: Any = None) -> dict[str, Any]:
    """Tìm trong memory (MEMORY/USER các tầng agent→team→global), skill hiệu lực và transcript (FTS5 trên toàn bộ hội thoại — v17,
    ẩn phiên kỹ thuật review/eval/curator). Thuần đọc, tất định, không gọi LLM. `vis` (session_search.Visibility) giới hạn phạm vi
    theo người gọi: agent thường chỉ thấy memory/skill của chính nó và transcript của nó + team."""
    q = _tokens(query)
    if not q:
        raise HubError("query rỗng")
    scope = scope or "all"
    if scope not in ("all", "memory", "skills", "transcripts"):
        raise HubError("scope phải là all|memory|skills|transcripts")
    if vis is not None and not vis.all:
        if agent_id and agent_id not in vis.agents:
            raise Blocked(f"memory_search: không được xem dữ liệu của agent '{agent_id}' (chỉ của chính mình)")
        agent_id = agent_id or (vis.agents[0] if vis.agents else None)
        if agent_id is None:
            raise Blocked("memory_search: không xác định được agent của người gọi")
    agents = [agent_id] if agent_id else [a["id"] for a in hub.store.list_agents()]
    results: list[dict[str, Any]] = []
    ks = getattr(hub, "knowledge", None)
    seen_tier: set[tuple[str, str | None, str]] = set()
    for aid in agents:
        if hub.store.get_agent(aid) is None:
            raise NotFound(f"không tìm thấy agent '{aid}'")
        if scope in ("all", "memory"):
            tiers = ks.agent_tiers(aid) if ks is not None else [("agent", aid)]
            for tscope, owner in tiers:
                for store_name in ("memory", "user"):
                    key = (tscope, owner, store_name)
                    if tscope != "agent" and key in seen_tier:
                        continue
                    seen_tier.add(key)
                    try:
                        entries = ks.read_memory(tscope, owner, store_name) if ks is not None else []
                    except Exception:
                        entries = []
                    if ks is None and tscope == "agent":
                        row = hub.store.get_agent(aid)
                        f = Path(row["home_path"]) / ("MEMORY.md" if store_name == "memory" else "USER.md")
                        entries = [e.strip() for e in (f.read_text(encoding="utf-8").split("\n§\n") if f.exists() else []) if e.strip()]
                    for e in entries:
                        sc = _score(q, e)
                        if sc:
                            results.append({"kind": "memory", "agent_id": aid if tscope == "agent" else None, "scope": tscope,
                                            "owner": owner, "store": store_name, "score": round(sc, 3), "text": _snippet(e, q)})
        if scope in ("all", "skills") and ks is not None:
            try:
                skills = ks.effective_skills(aid)
            except Exception:
                skills = []
            for s in skills:
                if s.get("scope") != "agent" and ("skill", s.get("owner"), s["name"]) in seen_tier:
                    continue
                seen_tier.add(("skill", s.get("owner"), s["name"]))
                body = ""
                try:
                    body = (Path(s["path"]) / "SKILL.md").read_text(encoding="utf-8", errors="replace")
                except (KeyError, OSError):
                    pass
                sc = _score(q, f"{s['name']} {s.get('description') or ''} {body}")
                if sc:
                    results.append({"kind": "skill", "agent_id": aid if s.get("scope") == "agent" else None, "scope": s.get("scope"),
                                    "owner": s.get("owner"), "name": s["name"], "score": round(sc, 3),
                                    "text": _snippet(body or s.get("description") or s["name"], q)})
    if scope in ("all", "transcripts"):
        # v17: FTS5 trên toàn bộ hội thoại (session_search.py) thay cho LIKE trên prompt/summary run
        from .. import session_search as ss

        vis = vis or ss.Visibility()
        sp: dict[str, Any] = {"mode": "search", "query": query, "limit": 50}
        if agent_id:
            sp["agent_id"] = agent_id
        try:
            hits = ss.search(hub.store.conn, sp, vis)["hits"]
        except HubError:
            hits = []
        for i, h in enumerate(hits):
            results.append({"kind": "transcript", "agent_id": h.get("agent_id"), "run_id": h.get("run_id"), "message_id": h["id"], "role": h.get("role"),
                            "session_id": h.get("session_id"), "conversation_id": h.get("conversation_id"), "at": h.get("ts"),
                            "score": round(0.9 * (1.0 + 0.5 / (1 + i)), 3), "text": h["snippet"]})
    results.sort(key=lambda r: (-r["score"], r["kind"]))
    return {"query": query, "results": results[: max(1, min(int(limit or 10), 50))], "total": len(results)}


# ============================================================================ trash
def trash_list(hub: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for a in hub.store.list_agents(include_archived=True):
        if a.get("deleted_at") or a.get("archived_at"):
            out.append({"kind": "agent", "id": a["id"], "deleted_at": a.get("deleted_at") or a.get("archived_at"),
                        "purge_after": a.get("purge_after"), "path": a.get("trash_path")})
    try:
        for r in hub.store.conn.execute("SELECT id, archived_at, purge_after FROM teams WHERE archived_at IS NOT NULL ORDER BY archived_at"):
            out.append({"kind": "team", "id": r[0], "deleted_at": r[1], "purge_after": r[2], "path": None})
    except Exception:  # pragma: no cover - cột purge_after chưa có
        for r in hub.store.conn.execute("SELECT id, archived_at FROM teams WHERE archived_at IS NOT NULL"):
            out.append({"kind": "team", "id": r[0], "deleted_at": r[1], "purge_after": None, "path": None})
    mcp = getattr(hub, "mcp", None)
    if mcp is not None:
        for r in mcp.list(include_deleted=True):
            if r.get("deleted_at"):
                out.append({"kind": "mcp", "id": r["name"], "deleted_at": r["deleted_at"], "purge_after": r.get("purge_after"), "path": r.get("trash_path")})
    out.sort(key=lambda r: str(r.get("deleted_at") or ""))
    return out


def _trash_kind(hub: Any, ident: str, kind: str | None) -> str:
    items = [t for t in trash_list(hub) if t["id"] == ident and (kind is None or t["kind"] == kind)]
    if not items:
        raise NotFound(f"không có '{ident}' trong thùng rác" + (f" (loại {kind})" if kind else ""))
    kinds = sorted({t["kind"] for t in items})
    if len(kinds) > 1:
        raise HubError(f"'{ident}' trùng tên ở nhiều loại ({', '.join(kinds)}) — chỉ rõ --kind")
    return kinds[0]


# ============================================================================ đăng ký
class SurfaceRpc:
    def __init__(self, daemon: "Daemon") -> None:
        self.daemon = daemon
        self.hub = daemon.hub

    async def _call(self, method: str, p: dict[str, Any]) -> Any:
        fn = self.daemon.methods.get(method)
        if fn is None:
            raise HubError(f"method {method} chưa có trên daemon này")
        res = fn(p)
        if asyncio.iscoroutine(res):
            res = await res
        return res

    def agent_delete(self, p: dict[str, Any]) -> dict[str, Any]:
        return self.hub.agents.remove(_req(p, "id"), detach_teams=bool(p.get("detach_teams")))

    def agent_revert(self, p: dict[str, Any]) -> dict[str, Any]:
        to = p.get("to") if p.get("to") is not None else p.get("revision")
        if to is None:
            raise HubError("thiếu tham số 'to' (số revision)", code=-32602)
        try:
            to = int(to)
        except (TypeError, ValueError):
            raise HubError("'to' phải là số revision") from None
        return self.hub.agents.revert(_req(p, "id"), to, actor=str(p.get("actor") or "user"))

    async def trash_restore(self, p: dict[str, Any]) -> dict[str, Any]:
        ident = str(_req(p, "id"))
        kind = _trash_kind(self.hub, ident, p.get("kind"))
        if kind == "agent":
            res = self.hub.agents.restore(ident)
        elif kind == "team":
            res = self.hub.teams.restore(ident)
        else:
            res = self.hub.mcp.restore(ident)
        return {"kind": kind, "id": ident, "restored": True, "item": res}

    async def trash_purge(self, p: dict[str, Any]) -> dict[str, Any]:
        ident = str(_req(p, "id"))
        kind = _trash_kind(self.hub, ident, p.get("kind"))
        if kind != "agent":
            raise HubError(f"purge loại '{kind}' chưa hỗ trợ — chỉ purge được agent (team/MCP tự xoá khi hết hạn retention)")
        res = await self._call("agent.purge", {"id": ident, "confirm": p.get("confirm"), "channel": p.get("channel") or "cli", "by": p.get("by") or "user"})
        return {"kind": kind, "id": ident, "purged": True, "item": res}

    # ---------------------------------------------------------------- tìm kiếm (phạm vi theo NGƯỜI GỌI — daemon xác định, auth.SCOPED_READ)
    def _caller_vis(self, what: str) -> Any:
        """Người/nội bộ/điều phối viên → thấy tất cả; agent → chính nó + team; socket không token, không rõ agent → từ chối."""
        from .. import session_search as ss

        c = _auth.current_caller()
        if c is None or c.human:
            return ss.Visibility()
        if c.manager or (c.agent_id and self.hub.manager.is_manager(c.agent_id)):
            return ss.Visibility(who=f"manager:{c.manager or c.agent_id}")
        if c.is_agent and c.agent_id:
            return ss.agent_visibility(self.hub.store.conn, c.agent_id)
        raise Blocked(f"{what}: người gọi không xác định (thiếu token người, không thuộc run nào)", data={"caller": c.to_dict()})

    def _self_run_vis(self, p: dict[str, Any], what: str) -> tuple[dict[str, Any], Any]:
        """hub-self: run phải đang chạy VÀ (qua socket) người gọi phải là tiến trình của CHÍNH run đó."""
        from .. import session_search as ss

        run = self.hub.store.get_run(str(_req(p, "run_id")))
        if run is None or run["status"] not in ("queued", "running"):
            raise HubError(f"run không còn chạy — {what} chỉ dùng được trong run đang chạy")
        c = _auth.current_caller()
        if c is not None and c.via == "socket" and not c.human and c.run_id != run["id"]:
            raise Blocked(f"{what}: run_id không khớp tiến trình gọi (chỉ hub-self của chính run đó)", data={"caller": c.to_dict()})
        if self.hub.manager.is_manager(run["agent_id"]):
            return run, ss.Visibility(who=f"manager:{run['agent_id']}")
        return run, ss.agent_visibility(self.hub.store.conn, run["agent_id"])

    def memory_search(self, p: dict[str, Any]) -> dict[str, Any]:
        vis = self._caller_vis("memory.search")
        return memory_search(self.hub, str(_req(p, "query")), agent_id=p.get("agent_id") or p.get("agent"), scope=p.get("scope") or "all",
                             limit=int(p.get("limit") or 10), vis=vis)

    def memory_search_self(self, p: dict[str, Any]) -> dict[str, Any]:
        """hub-self `memory_search`: memory/skill của agent của run; transcript của nó + team (điều phối viên: mọi hội thoại).
        Có `mode` (search|around|read|browse) hoặc scope=sessions → tìm kiếm hội thoại đầy đủ (session.search)."""
        run, vis = self._self_run_vis(p, "memory_search")
        args = dict(p.get("args") or {})
        if args.get("mode") or args.get("scope") == "sessions":
            args.setdefault("group", True)  # agent: mặc định gộp theo luồng (như Hermes trả top session)
            return self._session(args, vis)
        own = None if vis.all else run["agent_id"]
        return memory_search(self.hub, str(args.get("query") or ""), agent_id=own, scope=args.get("scope") or "all",
                             limit=int(args.get("limit") or 8), vis=vis)

    def _session(self, args: dict[str, Any], vis: Any) -> dict[str, Any]:
        from .. import session_search as ss

        args = {k: v for k, v in args.items() if not str(k).startswith("_") and k != "scope"}
        args.setdefault("mode", "search")
        if args["mode"] == "search" and not str(args.get("query") or "").strip():
            raise HubError("thiếu tham số 'query'", code=-32602)
        return ss.search(self.hub.store.conn, args, vis)

    def session_search(self, p: dict[str, Any]) -> dict[str, Any]:
        """`session.search {mode, query?, message_id?, window?, run_id?, session_id?, conversation_id?, agent_id?, team_id?, kind?, role?,
        since?, until?, include_technical?, group?, limit?, offset?}` — xem session_search.py."""
        return self._session(dict(p), self._caller_vis("session.search"))

    def session_search_self(self, p: dict[str, Any]) -> dict[str, Any]:
        _run, vis = self._self_run_vis(p, "session_search")
        args = dict(p.get("args") or {})
        args.setdefault("group", True)
        return self._session(args, vis)

    def _run_for_hook(self, run_id: str | None) -> dict[str, Any] | None:
        return self.hub.store.get_run(run_id) if run_id else None

    def hook_observe(self, p: dict[str, Any]) -> dict[str, Any]:
        """Hook quan sát (FR-RT-17): CHỈ metadata (tên event, tool, độ dài) — không ghi input/output tool (có thể chứa secret)."""
        run = self._run_for_hook(p.get("run_id"))
        if run is None:
            return {"ok": False, "reason": "unknown_run"}
        event = str(p.get("event") or "")
        if event not in HOOK_EVENTS:
            raise HubError(f"event hook không hỗ trợ: {event}")
        data = p.get("data") if isinstance(p.get("data"), dict) else {}
        payload = {"hook_event": event, "tool_name": data.get("tool_name"), "session_id": data.get("session_id"),
                   "prompt_chars": data.get("prompt_chars") if event == "UserPromptSubmit" else None,
                   "stop_hook_active": data.get("stop_hook_active") if event == "Stop" else None}
        ev = make_event("hook.observed", run["agent_id"], {k: v for k, v in payload.items() if v is not None}, run_id=run["id"])
        ev.source = "hook"
        if run.get("task_id"):
            ev.task_id, ev.team_id = run.get("task_id"), run.get("team_id")
        self.hub.publish(ev)
        return {"ok": True}

    def hook_timeout(self, p: dict[str, Any]) -> dict[str, Any]:
        run = self._run_for_hook(p.get("run_id"))
        payload = {"hook_event": p.get("event"), "timeout_s": p.get("timeout_s"), "elapsed_ms": p.get("elapsed_ms"), "reason": p.get("reason") or "timeout",
                   "fail_open": True}
        ev = make_event("hook.timeout", run["agent_id"] if run else None, payload, run_id=run["id"] if run else None)
        ev.source = "hook"
        self.hub.publish(ev)
        return {"ok": True}

    def agent_run(self, p: dict[str, Any]) -> dict[str, Any]:
        aid = str(_req(p, "agent_id"))
        ag = self.hub.agents.get_row(aid, allow_archived=True)
        rid = ag.get("current_run_id")
        if not rid:
            rows = self.hub.store.list_runs(agent_id=aid, limit=1)
            rid = rows[0]["id"] if rows else None
        if not rid:
            raise NotFound(f"agent '{aid}' chưa có run nào")
        return {"agent_id": aid, "run_id": rid}

    async def cancel_task(self, p: dict[str, Any]) -> dict[str, Any]:
        """`hub_cancel(task_id)`: dừng mọi run đang chạy/chờ của task và các task con (origin_task_id)."""
        tid = str(_req(p, "task_id"))
        if self.hub.store.conn.execute("SELECT 1 FROM tasks WHERE id=?", (tid,)).fetchone() is None:
            raise NotFound(f"không tìm thấy task '{tid}'")
        rows = self.hub.store.conn.execute(
            "SELECT id FROM runs WHERE status IN ('queued','running') AND task_id IN (SELECT id FROM tasks WHERE id=? OR origin_task_id=?)",
            (tid, tid),
        ).fetchall()
        stopped = []
        for (rid,) in rows:
            try:
                await self._call("run.stop", {"id": rid})
                stopped.append(rid)
            except HubError:
                pass
        return {"task_id": tid, "stopped_runs": stopped, "status": "stop_requested" if stopped else "no_active_run"}

    def compat(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"cli": self.hub.store.state_get("cli_compat"), "unknown_rate": self.hub.store.state_get("cli_unknown_rate")}

    def methods(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {
            "agent.delete": self.agent_delete,
            "agent.archive": self.agent_delete,
            "agent.revert": self.agent_revert,
            "trash.list": lambda p: trash_list(self.hub),
            "trash.restore": self.trash_restore,
            "trash.purge": self.trash_purge,
            "memory.search": self.memory_search,
            "memory.search_self": self.memory_search_self,
            "session.search": self.session_search,
            "session.search_self": self.session_search_self,
            "hook.observe": self.hook_observe,
            "hook.timeout": self.hook_timeout,
            "mcp.agent_run": self.agent_run,
            "mcp.cancel_task": self.cancel_task,
            "hub.compat": self.compat,
        }


def register(daemon: "Daemon") -> SurfaceRpc:
    s = SurfaceRpc(daemon)
    daemon.methods.update(s.methods())
    return s
