"""Tiện ích test cổng cfharness (filehub chế độ lane): daemon in-process + ScriptedRuntime + server lane GIẢ (`fake_kgs_lane.py`).

Không gọi claude, không gọi Không gian số thật. `configure_lane(d, lane)` = như sau `filehub lane setup` + một thư mục chung chế độ lane.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from cfharness.daemon import Daemon
from fake_kgs_lane import KEY, FakeLane
from m3_fake import ScriptedRuntime
from m7_fake import rpc


class Script:
    def __init__(self) -> None:
        self.by_prompt: dict[str, dict[str, Any]] = {}
        self.specs: list[Any] = []
        self.on_start: Callable[[Any], None] | None = None

    def __call__(self, spec: Any) -> dict[str, Any]:
        self.specs.append(spec)
        if self.on_start is not None:
            self.on_start(spec)
        for k, v in self.by_prompt.items():
            if k in spec.prompt:
                return dict(v)
        return {"text": "ok", "cost": 0.001}


async def make(hub_home: Any) -> tuple[Daemon, Script]:
    s = Script()
    d = Daemon(hub_home, runtime=ScriptedRuntime(s))
    d.hub.config.set("tasks.tick_s", 0.05)
    d.hub.start()
    for a in ("w1", "w2"):
        d.hub.agents.create({"id": a, "name": a, "limits": {"max_concurrent_runs": 5}})
    return d, s


async def configure_lane(d: Daemon, lane: FakeLane, *, name: str = "chung", root: str = "r_chung", fast: bool = True) -> None:
    await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
    d.hub.config.set("filehub.folders", [{"name": name, "root_id": root, "ws_id": "w", "kind": "folder", "mode": "lane", "server": "qltm"}])
    if fast:
        d.hub.config.set("filehub.lane.push_quiet_s", 0.1)
        d.hub.config.set("filehub.lane.push_min_interval_s", 0)
        d.hub.config.set("filehub.lane.scan_interval_s", 0.05)
        d.hub.config.set("filehub.lane.reconnect_s", [0.05, 0.2])
        d.hub.config.set("filehub.lane.final_flush_s", 5)


async def until(cond: Callable[[], Any], timeout: float = 10.0, step: float = 0.03) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        v = cond()
        if v:
            return v
        if loop.time() > deadline:
            raise AssertionError("hết thời gian chờ điều kiện")
        await asyncio.sleep(step)


async def close(d: Daemon) -> None:
    d.hub.runs.runtime().release()  # type: ignore[attr-defined]
    await asyncio.sleep(0.05)
    d.hub.runs.shutting_down = True
    await d.hub.dispatcher.stop()
    await d.hub.filehub.stop()
    d.hub.store.close()


def steers(d: Daemon, run_id: str) -> list[str]:
    return [e.payload.get("text") or "" for e in d.hub.store.query_events(types=["user.message"]) if e.run_id == run_id and e.payload.get("by") == "hub"]


def events(d: Daemon, etype: str) -> list[Any]:
    return list(d.hub.store.query_events(types=[etype]))
