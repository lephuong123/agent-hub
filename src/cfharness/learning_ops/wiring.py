"""Gắn M4-B vào hub: `attach(hub)` (gọi ở CUỐI `Hub.__init__`).

- `hub.evals`        : golden task + eval gate (`evals/`).
- `hub.learning_ops` : `LearningOps` — `regression`, `curator`, `digest` + vòng lịch nền.
Vòng nền tự khởi động khi bus phát `hub.started` (trong event loop của daemon) và dừng ở `hub.stopping` — không cần sửa
`Hub.start/stop`. Mỗi tick (`eval.ops_tick_s`):
  1. digest đến giờ (`digest.schedule`, giờ máy) → dựng + gửi Slack;
  2. eval L1 theo lô (`eval.batch_time`, mỗi ngày một lần) → `gate.run_batch_l1()`;
  3. curator pha 1 đến chu kỳ và daemon rảnh → `curator.run()`;
  4. agent có run/signal mới → `regression.check_agent()`; toàn bộ mỗi `eval.regression.full_check_min` phút.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..events import HubEvent

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.learning_ops")


class LearningOps:
    def __init__(self, hub: "Hub") -> None:
        from .curator import Curator
        from .digest import DigestService
        from .regression import RegressionMonitor

        self.hub = hub
        self.regression = RegressionMonitor(hub)
        self.curator = Curator(hub)
        self.digest = DigestService(hub)
        self._task: asyncio.Task | None = None
        self._last_full = 0.0
        self.ticks = 0
        hub.bus.add_listener(self._on_event)

    def _on_event(self, ev: HubEvent) -> None:
        if ev.type == "hub.started":
            self.start()
        elif ev.type == "hub.stopping":
            self.stop()
        self.regression.on_event(ev)
        self.digest.on_notify(ev)

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
                await asyncio.sleep(float((self.hub.config.get("eval") or {}).get("ops_tick_s") or 30))
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - vòng nền không được chết
                log.exception("learning_ops tick lỗi")

    async def tick(self, *, now_local: datetime | None = None) -> dict[str, Any]:
        """Một lượt lịch (test gọi trực tiếp)."""
        self.ticks += 1
        out: dict[str, Any] = {}
        if self.digest.due(now_local):
            out["digest"] = self.digest.run(scheduled=True)
        if self._batch_due(now_local):
            self.hub.store.state_set("eval.batch_local_date", (now_local or datetime.now().astimezone()).date().isoformat())
            out["eval_batch"] = await self.hub.evals.gate.run_batch_l1()
        if self.curator.should_run_now():
            out["curator"] = await self.curator.run()
        pending = self.regression.take_pending()
        acts: list[dict[str, Any]] = []
        for a in sorted(pending):
            acts += await self.regression.check_agent(a)
        loop_t = asyncio.get_running_loop().time()
        full_s = 60 * float((self.hub.config.get("eval") or {}).get("regression", {}).get("full_check_min", 30))
        if loop_t - self._last_full >= full_s:
            self._last_full = loop_t
            acts += await self.regression.check_all()
        if acts:
            out["regression"] = acts
        return out

    def _batch_due(self, now_local: datetime | None) -> bool:
        c = self.hub.config.get("eval") or {}
        if not c.get("enabled", True) or not c.get("batch_enabled", True):
            return False
        now_local = now_local or datetime.now().astimezone()
        from .digest import parse_hhmm

        hh, mm = parse_hhmm(c.get("batch_time"), (3, 0))
        if (now_local.hour, now_local.minute) < (hh, mm):
            return False
        return self.hub.store.state_get("eval.batch_local_date") != now_local.date().isoformat()


def attach(hub: "Hub") -> None:
    from ..evals import EvalService

    hub.evals = EvalService(hub)  # type: ignore[attr-defined]
    hub.learning_ops = LearningOps(hub)  # type: ignore[attr-defined]
