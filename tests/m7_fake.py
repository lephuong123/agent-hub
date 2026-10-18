"""Tiện ích test M7 (điều phối viên): daemon in-process + ScriptedRuntime, run manager giữ ở `running` để gọi tool hub-self."""

from __future__ import annotations

import asyncio
from typing import Any

from agent_hub.daemon import Daemon
from agent_hub.errors import HubError
from m3_fake import ScriptedRuntime


class MgrScript:
    def __init__(self) -> None:
        self.hold_manager = True
        self.reply = "Đã giao việc."
        self.specs: list[Any] = []

    def __call__(self, spec: Any) -> dict[str, Any]:
        self.specs.append(spec)
        if spec.prompt.lstrip().startswith(("[agent-hub · OWNER", "[agent-hub heartbeat", "[agent-hub · key")):
            return {"text": self.reply, "cost": 0.001, "hold": self.hold_manager}
        return {"text": "worker done", "cost": 0.001}


async def make_daemon(hub_home: Any, script: MgrScript | None = None) -> Daemon:
    s = script or MgrScript()
    d = Daemon(hub_home, runtime=ScriptedRuntime(s))
    d.hub.config.set("tasks.tick_s", 0.05)
    d.hub.config.set("manager.check_s", 3600)  # test tự gọi maybe_tick
    d.hub.start()
    d.script = s  # type: ignore[attr-defined]
    return d


async def rpc(d: Daemon, method: str, params: dict[str, Any] | None = None, *, as_caller: Any = None) -> Any:
    resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": dict(params or {})}, as_caller=as_caller)
    if "error" in resp:
        e = resp["error"]
        raise HubError(e["message"], data=e.get("data"), code=e.get("code"))
    return resp["result"]


async def wait_status(d: Daemon, run_id: str, statuses: tuple[str, ...] = ("running",), timeout: float = 10.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        r = d.hub.store.get_run(run_id) or {}
        if r.get("status") in statuses:
            return r
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"run {run_id} status={r.get('status')} (chờ {statuses})")
        await asyncio.sleep(0.02)


async def manager_run(d: Daemon, agent_id: str = "boss", text: str = "hello") -> dict[str, Any]:
    """Chỉ định `agent_id` làm manager (tạo nếu chưa có), nhắn một tin, chờ run manager ở `running` (giữ bởi ScriptedRuntime)."""
    if d.hub.store.get_agent(agent_id) is None:
        d.hub.agents.create({"id": agent_id, "name": agent_id, "role": "hub boss", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    await rpc(d, "manager.set", {"agent_id": agent_id})
    res = await rpc(d, "manager.chat", {"channel": "web", "text": text})
    await wait_status(d, res["run_id"])
    return res


async def tool(d: Daemon, run_id: str, op: str, args: dict[str, Any] | None = None) -> Any:
    return await rpc(d, "managerself.call", {"run_id": run_id, "op": op, "args": dict(args or {})})


async def close(d: Daemon) -> None:
    d.hub.runs.runtime().release()  # type: ignore[attr-defined]
    await asyncio.sleep(0.05)
    d.hub.runs.shutting_down = True
    await d.hub.manager.stop()  # type: ignore[attr-defined]
    await d.hub.dispatcher.stop()
    d.hub.store.close()
