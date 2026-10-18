"""RPC của điều phối viên (M7 MGR-CORE). Chữ ký ỔN ĐỊNH cho MGR-UI (docs/dev-notes/M7-core.md):

- `manager.get {}` → `{agent_id|null, conversations:[Conv], last_tick, next_tick, tick:{heartbeat, interval_s, pending_events,
  last_tick_run_id, last_reason, last_skip, running}, designated_at}`
- `manager.set {agent_id|null}` (HUMAN_ONLY) → như `manager.get`
- `manager.chat {channel, text, conversation_id?, reuse_latest?}` (HUMAN_ONLY) → `{conversation_id, run_id, session_id?, resumed?, steered?}`.
  Không có `conversation_id` → hội thoại MỚI (trừ `reuse_latest=true`: hội thoại gần nhất của đúng kênh). Hội thoại đang có run
  chạy → tin được steer vào run đó (`steered=true`). `conversation_id` phải cùng `channel` (khác → Conflict); hội thoại của manager CŨ →
  tự chuyển sang hội thoại gần nhất/mới của manager hiện tại trên kênh đó (`switched_from`).
- `manager.conversations {channel?, limit?, all?}` → `[Conv]` (mặc định chỉ hội thoại của manager HIỆN TẠI), Conv = `{id, channel, agent_id, session_id, turns, title, messages (số),
  last_run_id, created_at, updated_at}`
- `manager.history {conversation_id, limit?}` → `[{id, role: user|manager|notify|tick, text, run_id, ts}]` (cũ → mới)
- `manager.tick {force?}` (HUMAN_ONLY) → `{run_id|null, skipped?, reason?, events?, conversation_id?}`
- `managerself.call {run_id, op, args}` — tool hub-self của run manager (daemon ràng run ↔ người gọi).
Event: `manager.reply{conversation_id, run_id, text, channel, kind: reply|tick|notify, ok, outcome?}`,
`manager.action{tool, target ("agent:x"|"task:t_…"|…), summary, audit_id, ok, error?}`, `manager.tick{status, reason, events}`,
`manager.designated{agent_id, previous}`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..errors import HubError

if TYPE_CHECKING:
    from ..daemon import Daemon

MUTATING_MANAGER = ("manager.set", "manager.chat", "manager.tick", "managerself.call")
_AUDITED = ("manager.set", "manager.chat", "manager.tick")  # managerself.call: từng thao tác bên trong tự audit


def _req(p: dict[str, Any], key: str) -> Any:
    if p.get(key) is None:
        raise HubError(f"thiếu tham số '{key}'", code=-32602)
    return p[key]


def register_rpc(daemon: "Daemon") -> None:
    from ..mcp_server.rpc import wrap_with_audit

    m = daemon.hub.manager  # type: ignore[attr-defined]
    m.daemon = daemon

    def _set(p: dict[str, Any]) -> dict[str, Any]:
        from ..auth import current_caller

        c = current_caller()
        ch = c.via if c is not None and c.via not in ("socket", "internal") else "cli"
        return m.set(p.get("agent_id") or None, by="user", channel=ch)

    async def _chat(p: dict[str, Any]) -> dict[str, Any]:
        return await m.chat(str(_req(p, "channel")), str(_req(p, "text")), conversation_id=p.get("conversation_id"),
                            reuse_latest=bool(p.get("reuse_latest")))

    methods: dict[str, Callable[[dict[str, Any]], Any]] = {
        "manager.get": lambda p: m.get(),
        "manager.set": _set,
        "manager.chat": _chat,
        "manager.conversations": lambda p: m.conversations(p.get("channel"), limit=int(p.get("limit") or 100), all_managers=bool(p.get("all"))),
        "manager.history": lambda p: m.history(str(_req(p, "conversation_id")), int(p.get("limit") or 100)),
        "manager.tick": lambda p: m.maybe_tick(force=bool(p.get("force", True))),
        "managerself.call": lambda p: m.self_call(str(_req(p, "run_id")), str(_req(p, "op")), p.get("args") or {}),
    }
    for name in _AUDITED:
        methods[name] = wrap_with_audit(daemon.hub, name, methods[name])
    daemon.methods.update(methods)
