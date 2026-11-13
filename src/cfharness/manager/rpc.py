"""RPC của điều phối viên (M7 MGR-CORE). Chữ ký ỔN ĐỊNH cho MGR-UI (docs/dev-notes/M7-core.md):

- `manager.get {}` → `{agent_id|null, conversations:[Conv], last_tick, next_tick, tick:{heartbeat, interval_s, pending_events,
  last_tick_run_id, last_reason, last_skip, running}, designated_at}`
- `manager.set {agent_id|null}` (HUMAN_ONLY) → như `manager.get`
- `manager.chat {channel, text, conversation_id?, reuse_latest?, images?:[{name, data_b64}]}` (HUMAN_ONLY; ảnh PNG/JPEG/GIF/WebP ≤ 5 MB, ≤ 4 → lưu
  `<cwd hội thoại>/attachments/`, prompt bảo điều phối viên xem bằng Read; `text` được rỗng khi có ảnh;
  `files?:[{name, data_b64}]` = tệp VĂN BẢN UTF-8 ≤ 10 MB, ≤ 4, tên do hub làm sạch → cùng thư mục `attachments/`) → `{conversation_id, run_id, session_id?, resumed?, steered?}`.
  Không có `conversation_id` → hội thoại MỚI (trừ `reuse_latest=true`: hội thoại gần nhất của đúng kênh). Hội thoại đang có run
  chạy → tin được steer vào run đó (`steered=true`). `conversation_id` phải cùng `channel` (khác → Conflict); hội thoại của manager CŨ →
  tự chuyển sang hội thoại gần nhất/mới của manager hiện tại trên kênh đó (`switched_from`).
- `manager.conversations {channel?, limit?, all?}` → `[Conv]` (mặc định chỉ hội thoại của manager HIỆN TẠI), Conv = `{id, channel, agent_id, session_id, turns, title, messages (số),
  last_run_id, created_at, updated_at}`
- `manager.conversations` nhận thêm `workstream?` (id/mã luồng), `lobby?` (chỉ hội thoại sảnh), `archived?`; Conv thêm `workstream_id, unread, archived_at`.
- `manager.conversation_update {conversation_id, title?, workstream? ("" = về sảnh), archived?, read?}` (HUMAN_ONLY) → Conv.
- `manager.conversation_read {conversation_id}` (HUMAN_ONLY, không audit) — đánh dấu đã xem tới tin cuối (`unread` = 0).
- `manager.chat` nhận thêm `workstream?` (hội thoại MỚI tạo trong luồng đó; với `reuse_latest` = hội thoại gần nhất của luồng trên kênh).
- Luồng việc (manager/workstreams.py): `workstream.list {status?: active|closed|all}` → `[WS]`, `workstream.get {id}` → WS đầy đủ (state,
  open_tasks, recent_done, conversations), `workstream.create {title, key?, sprint?, state?}` (HUMAN_ONLY), `workstream.update {id, title?, key?,
  sprint?, status?: active|closed, state?}` (HUMAN_ONLY), `workstream.delete {id}` (HUMAN_ONLY, audit; xoá hẳn luồng — hội thoại về Sảnh,
  task bỏ gắn luồng, bản chụp + STATE.md vào audit `workstream.snapshot`) → `{id, label, deleted, detached:{conversations, tasks}, state}`. WS = `{id, key, title, label, sprint, status, tasks:{open, running, blocked, done, total},
  needs_human, conversations, last_activity, …}`. Event `workstream.updated{workstream_id, key, title, status, action, by}`.
- `manager.history {conversation_id, limit?}` → `[{id, role: user|manager|notify|tick, text, run_id, ts}]` (cũ → mới)
- `manager.tick {force?}` (HUMAN_ONLY) → `{run_id|null, skipped?, reason?, events?, conversation_id?}`
- `managerself.call {run_id, op, args}` — tool hub-self của run manager (daemon ràng run ↔ người gọi).
- `manager.capabilities {}` (đọc) → `{config, effective:{profile, tools, mcp, web, warnings}, profiles, manager, problem}` — khung quyền §9;
  ghi bằng `config.set {key: "manager.capabilities", value}` (HUMAN_ONLY + audit).
Event: `manager.reply{conversation_id, run_id, text, channel, kind: reply|tick|notify, ok, outcome?}`,
`manager.action{tool, target ("agent:x"|"task:t_…"|…), summary, audit_id, ok, error?}`, `manager.tick{status, reason, events}`,
`manager.designated{agent_id, previous}`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..errors import HubError

if TYPE_CHECKING:
    from ..daemon import Daemon

MUTATING_MANAGER = ("manager.set", "manager.chat", "manager.tick", "managerself.call", "manager.conversation_update", "manager.conversation_read",
                    "workstream.create", "workstream.update", "workstream.delete")
_AUDITED = ("manager.set", "manager.chat", "manager.tick", "manager.conversation_update", "workstream.create", "workstream.update",
            "workstream.delete")  # managerself.call: từng thao tác bên trong tự audit


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
        return await m.chat(str(_req(p, "channel")), str(p.get("text") or ""), conversation_id=p.get("conversation_id"),
                            reuse_latest=bool(p.get("reuse_latest")), images=p.get("images"), files=p.get("files"),
                            workstream=p.get("workstream") or None)

    def _conv_update(p: dict[str, Any]) -> dict[str, Any]:
        arch = p.get("archived")
        return m.update_conversation(str(_req(p, "conversation_id")), title=p.get("title"), workstream=p.get("workstream"),
                                     archived=None if arch is None else bool(arch), read=bool(p.get("read")))

    def _ws_update(p: dict[str, Any]) -> dict[str, Any]:
        ch = dict(p.get("changes") or {})
        for k in ("title", "key", "sprint", "status", "state"):
            if k in p and k not in ch:
                ch[k] = p[k]
        return m.ws.update(_req(p, "id"), ch, by="user")

    methods: dict[str, Callable[[dict[str, Any]], Any]] = {
        "manager.get": lambda p: m.get(),
        "manager.set": _set,
        "manager.chat": _chat,
        "manager.conversations": lambda p: m.conversations(p.get("channel"), limit=int(p.get("limit") or 100), all_managers=bool(p.get("all")),
                                                           workstream=p.get("workstream") or None, lobby=bool(p.get("lobby")),
                                                           archived=bool(p.get("archived"))),
        "manager.conversation_update": _conv_update,
        # đánh dấu đã xem (web mở hội thoại) — ghi nhẹ, KHÔNG audit (mỗi lần xem trang)
        "manager.conversation_read": lambda p: m.mark_read(str(_req(p, "conversation_id"))) or {"ok": True},
        "workstream.list": lambda p: m.ws.list(str(p.get("status") or "active"), limit=int(p.get("limit") or 100)),
        "workstream.get": lambda p: m.ws.get(_req(p, "id")),
        "workstream.create": lambda p: m.ws.create(p.get("title"), key=p.get("key"), sprint=p.get("sprint"), state=p.get("state"), by="user"),
        "workstream.update": _ws_update,
        "workstream.delete": lambda p: m.ws.delete(_req(p, "id"), by="user"),
        "manager.history": lambda p: m.history(str(_req(p, "conversation_id")), int(p.get("limit") or 100)),
        "manager.tick": lambda p: m.maybe_tick(force=bool(p.get("force", True))),
        "managerself.call": lambda p: m.self_call(str(_req(p, "run_id")), str(_req(p, "op")), p.get("args") or {}),
        "manager.capabilities": lambda p: m.capabilities_info(),  # đọc; ghi = config.set manager.capabilities (HUMAN_ONLY, audit)
    }
    for name in _AUDITED:
        methods[name] = wrap_with_audit(daemon.hub, name, methods[name])
    daemon.methods.update(methods)
