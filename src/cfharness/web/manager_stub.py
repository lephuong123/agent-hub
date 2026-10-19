"""STUB hợp đồng RPC `manager.*` (docs/plan/M7-manager.md) — CHỈ dùng cho test/demo của luồng MGR-UI khi daemon chưa có
bản thật của MGR-CORE. `install(daemon)` không làm gì nếu daemon đã có `manager.get` (trừ khi `force=True`).

Cùng chữ ký với hợp đồng:
- `manager.get()` → `{agent_id|null, conversations:[…], last_tick, next_tick}`; `manager.set({agent_id|null})`.
- `manager.chat({channel, conversation_id?, text})` → `{conversation_id, run_id}` (không có `conversation_id` → hội thoại MỚI);
  trả lời đến qua event `manager.reply{conversation_id, run_id, text}` khi run của manager kết thúc.
- `manager.conversations({channel?})`, `manager.history({conversation_id, limit})` → `[{role: user|manager, text, ts, run_id}]`.
- Event `manager.action{tool, target, summary, audit_id}` — stub KHÔNG tự phát (test/demo tự `hub.publish`).
Hội thoại giữ trong RAM (mất khi daemon tắt). Không kiểm quyền (bản thật: `manager.set` HUMAN_ONLY ở daemon).
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

from ..errors import Conflict, HubError, NotFound
from ..events import TERMINAL_RUN_EVENTS, HubEvent, iso, make_event

if TYPE_CHECKING:
    from ..daemon import Daemon


class ManagerStub:
    def __init__(self, daemon: "Daemon") -> None:
        self.d = daemon
        self.hub = daemon.hub
        self.agent_id: str | None = None
        self.convs: dict[str, dict[str, Any]] = {}
        self.run_conv: dict[str, str] = {}
        self.last_tick: str | None = None
        self.next_tick: str | None = None
        self.hub.bus.add_listener(self._on_event)

    # ------------------------------------------------------------------ RPC
    def get(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"agent_id": self.agent_id, "conversations": self.conversations({}), "last_tick": self.last_tick, "next_tick": self.next_tick}

    def set(self, p: dict[str, Any]) -> dict[str, Any]:
        aid = p.get("agent_id")
        if aid:
            self.hub.agents.show(aid)  # NotFound nếu không có
        self.agent_id = aid or None
        return self.get({})

    def chat(self, p: dict[str, Any]) -> dict[str, Any]:
        if not self.agent_id:
            raise Conflict("chưa có điều phối viên — chỉ định bằng `cfharness manager set <agent>`")
        text = str(p.get("text") or "").strip()
        channel = str(p.get("channel") or "cli")
        if not text:
            raise HubError("thiếu 'text'")
        cid = p.get("conversation_id")
        if cid and cid not in self.convs:
            raise NotFound(f"không có hội thoại '{cid}'")
        if not cid:
            cid = "conv_" + secrets.token_hex(4)
            self.convs[cid] = {"id": cid, "channel": channel, "created_at": iso(), "updated_at": iso(), "messages": []}
        conv = self.convs[cid]
        run = self.hub.runs.submit(self.agent_id, text)
        self.run_conv[run["id"]] = cid
        conv["messages"].append({"role": "user", "text": text, "ts": iso(), "run_id": run["id"]})
        conv["updated_at"] = iso()
        return {"conversation_id": cid, "run_id": run["id"]}

    def conversations(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        ch = p.get("channel")
        rows = [{k: v for k, v in c.items() if k != "messages"} | {"messages": len(c["messages"])} for c in self.convs.values()
                if not ch or c["channel"] == ch]
        return sorted(rows, key=lambda c: c["updated_at"], reverse=True)

    def history(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        cid = p.get("conversation_id")
        if cid not in self.convs:
            raise NotFound(f"không có hội thoại '{cid}'")
        return self.convs[cid]["messages"][-int(p.get("limit") or 100):]

    # ------------------------------------------------------------------ trả lời
    def _on_event(self, ev: HubEvent) -> None:
        if ev.type not in TERMINAL_RUN_EVENTS or ev.source != "hub" or ev.run_id not in self.run_conv:
            return
        cid = self.run_conv.pop(ev.run_id)
        run = self.hub.store.get_run(ev.run_id) or {}
        if (run.get("outcome") or run.get("status")) != "completed":
            return
        text = str(run.get("summary") or "")
        conv = self.convs.get(cid)
        if conv is not None:
            conv["messages"].append({"role": "manager", "text": text, "ts": iso(), "run_id": ev.run_id})
            conv["updated_at"] = iso()
        self.hub.publish(make_event("manager.reply", self.agent_id, {"conversation_id": cid, "run_id": ev.run_id, "text": text}, run_id=ev.run_id))


def install(daemon: "Daemon", *, force: bool = False) -> ManagerStub | None:
    if "manager.get" in daemon.methods and not force:
        return None
    s = ManagerStub(daemon)
    daemon.methods.update({
        "manager.get": s.get, "manager.set": s.set, "manager.chat": s.chat, "manager.conversations": s.conversations, "manager.history": s.history,
    })
    daemon._manager_stub = s  # type: ignore[attr-defined]
    return s
