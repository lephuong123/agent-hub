"""Nối secret store, audit, registry MCP và bộ theo dõi tool ghi vào `Hub` (gọi từ `Hub.__init__`).

`ToolWriteWatcher` (FR-MC-03): listener của bus. Với mỗi `tool.start` của tool MCP (`mcp__<server>__<tool>`)
KHÔNG có `readOnlyHint: true` theo annotations đã probe (tool chưa probe = coi là ghi) → ghi audit
`tool.write` (agent, run, server, tool, tham số đã che) và phát event `tool.write` (level summary).
Khi `tool.complete` tương ứng tới → audit `tool.write.result` (is_error, result_preview).
Event mới được phát qua `loop.call_soon` để không lồng publish trong listener (giữ thứ tự seq cho subscriber).
"""

from __future__ import annotations

import json

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from .. import audit as audit_mod
from ..audit import AuditLog
from ..events import HubEvent, make_event
from ..secrets import SecretStore
from . import register_registry
from .registry import McpRegistry

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("agent_hub.mcp_client")
INPUT_MAX = 4000


def parse_mcp_tool(name: str, known_servers: list[str]) -> tuple[str, str] | None:
    if not name.startswith("mcp__"):
        return None
    rest = name[5:]
    for s in sorted(known_servers, key=len, reverse=True):
        if rest.startswith(s + "__"):
            return s, rest[len(s) + 2 :]
    if "__" in rest:
        s, t = rest.split("__", 1)
        return s, t
    return None


class ToolWriteWatcher:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.pending: dict[str, dict[str, Any]] = {}

    def _ignored(self) -> set[str]:
        try:
            return set(self.hub.config.get("mcp.audit_ignore_servers") or [])
        except KeyError:  # pragma: no cover
            return set()

    def __call__(self, ev: HubEvent) -> None:
        try:
            if ev.type == "tool.start":
                self._on_start(ev)
            elif ev.type == "tool.complete" and ev.payload.get("tool_id") in self.pending:
                self._on_complete(ev)
        except Exception:  # pragma: no cover — không để audit làm hỏng luồng event
            log.exception("ToolWriteWatcher lỗi với %s", ev.type)

    def _on_start(self, ev: HubEvent) -> None:
        name = str(ev.payload.get("name") or "")
        if not name.startswith("mcp__"):
            return
        reg: McpRegistry = self.hub.mcp
        known = [r["name"] for r in reg.list()]
        parsed = parse_mcp_tool(name, known)
        if parsed is None:
            return
        server, tool = parsed
        if server in self._ignored():
            return
        ann = reg.tool_annotations(server, tool)
        if ann is not None and ann.get("readOnlyHint") is True:
            return
        inp = ev.payload.get("input")
        data = {
            "server": server,
            "tool": tool,
            "tool_id": ev.payload.get("tool_id"),
            "input": inp,
            "annotations_known": ann is not None,
            "trust": (ann or {}).get("trust") or (reg.get_row(server) or {}).get("trust"),
            "destructiveHint": (ann or {}).get("destructiveHint"),
        }
        audit_id = self.hub.audit.record(
            "tool.write",
            ev.agent_id,
            ev.run_id,
            data,
            actor=f"agent:{ev.agent_id}" if ev.agent_id else "agent",
            channel="runtime",
            target=f"{server}/{tool}",
            summary=str(ev.payload.get("args_preview") or "")[:200],
        )
        if ev.payload.get("tool_id"):
            self.pending[str(ev.payload["tool_id"])] = {"server": server, "tool": tool}
        out = make_event(
            "tool.write",
            ev.agent_id,
            {
                "server": server,
                "tool": tool,
                "tool_id": ev.payload.get("tool_id"),
                "run_id": ev.run_id,
                "args_preview": ev.payload.get("args_preview"),
                "annotations_known": ann is not None,
                "trust": data["trust"],
                "audit_id": audit_id,
            },
            run_id=ev.run_id,
            session_id=ev.session_id,
            subagent_id=ev.subagent_id,
        )
        self._publish_later(out)

    def _on_complete(self, ev: HubEvent) -> None:
        info = self.pending.pop(str(ev.payload.get("tool_id")))
        self.hub.audit.record(
            "tool.write.result",
            ev.agent_id,
            ev.run_id,
            {
                **info,
                "tool_id": ev.payload.get("tool_id"),
                "is_error": ev.payload.get("is_error"),
                "duration_s": ev.payload.get("duration_s"),
                "result_preview": ev.payload.get("result_preview"),
            },
            actor=f"agent:{ev.agent_id}" if ev.agent_id else "agent",
            channel="runtime",
            target=f"{info['server']}/{info['tool']}",
        )

    def _publish_later(self, ev: HubEvent) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.hub.publish(ev)
            return
        loop.call_soon(self.hub.publish, ev)


def attach(hub: "Hub") -> None:
    """Gắn các dịch vụ của luồng A vào hub: `hub.secrets`, `hub.audit`, `hub.mcp`, watcher tool ghi."""
    try:
        env_fallback = bool(hub.config.get("secrets.env_fallback"))
    except KeyError:  # pragma: no cover
        env_fallback = True
    secrets = SecretStore(hub.paths.home / "secrets", env_fallback=env_fallback)
    hub.secrets = secrets  # type: ignore[attr-defined]
    hub.audit = AuditLog(hub.store, mask=secrets.mask_values)  # type: ignore[attr-defined]
    audit_mod.install(hub.audit)  # type: ignore[attr-defined]

    def agents_using(name: str) -> list[str]:
        return [a["id"] for a in hub.store.list_agents() if name in (a.get("mcp_servers") or [])]

    hub.mcp = McpRegistry(lambda: hub.store.conn, hub.paths, secrets, publish=hub.publish, agents_using=agents_using)  # type: ignore[attr-defined]
    register_registry(hub.paths.home, hub.mcp)  # type: ignore[attr-defined]
    hub.bus.add_listener(ToolWriteWatcher(hub))
    # H-12: bộ che theo GIÁ TRỊ — nạp mọi secret đã lưu + giá trị env-fallback mà registry đang tham chiếu
    try:
        secrets.preload()
        from ..secrets import refs_in

        for row in hub.store.conn.execute("SELECT * FROM mcp_servers"):
            for name in refs_in(json.dumps(dict(row), default=str)):
                secrets.lookup(name)
    except Exception:  # pragma: no cover — không để lỗi nạp che làm hỏng khởi động
        pass
