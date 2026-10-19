"""Khung chung cho các kênh thông báo ra ngoài hub (FR-SG-09): Slack, Telegram.

Mỗi kênh là một listener của bus: lọc theo `notify.<kênh>.events`, dựng tin (đã che secret), đưa vào hàng đợi;
worker gửi với backoff (`backoff_base_s` × 2^n, tối đa `max_attempts`), mỗi lần thành công phát `notify.sent`,
mỗi lần lỗi phát `notify.failed` (payload có `channel`). Chưa cấu hình → tắt êm (không lỗi, không event).
Lớp con định nghĩa: `channel`, `target()`, `render(ev)`, `async _send(target, ev, msg) -> dict` (thông tin thêm cho `notify.sent`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ..events import HubEvent, make_event
from ..redact import redact_str

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.notify")


def clip(s: Any, n: int = 300) -> str:
    t = redact_str(str(s or ""))
    return t if len(t) <= n else t[:n] + "…"


class QueuedNotifier:
    channel = "base"

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.queue: asyncio.Queue[tuple[HubEvent, Any]] | None = None
        self._task: asyncio.Task | None = None
        self.sent: int = 0
        self.failed: int = 0

    # ------------------------------------------------------------------ cấu hình (lớp con ghi đè)
    def cfg(self) -> dict[str, Any]:
        return dict(self.hub.config.get(f"notify.{self.channel}") or {})

    def _secret(self, name: str | None) -> str | None:
        if not name:
            return None
        sec = getattr(self.hub, "secrets", None)
        if sec is not None and hasattr(sec, "lookup"):
            try:
                return sec.lookup(name)
            except Exception:
                return None
        return None

    def target(self) -> dict[str, Any] | None:  # pragma: no cover - lớp con
        raise NotImplementedError

    def render(self, ev: HubEvent) -> Any | None:  # pragma: no cover - lớp con
        raise NotImplementedError

    async def _send(self, target: dict[str, Any], ev: HubEvent, msg: Any) -> dict[str, Any]:  # pragma: no cover - lớp con
        raise NotImplementedError

    # ------------------------------------------------------------------ bus
    def __call__(self, ev: HubEvent) -> None:
        if ev.type not in (self.cfg().get("events") or []):
            return
        if self.target() is None:
            return
        msg = self.render(ev)
        if not msg:
            return
        if self.queue is None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                log.warning("không có event loop — bỏ thông báo %s %s", self.channel, ev.type)
                return
            self.queue = asyncio.Queue()
        self.queue.put_nowait((ev, msg))
        self.ensure_worker()

    def ensure_worker(self) -> None:
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._worker())
            except RuntimeError:
                pass

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def drain(self, timeout: float = 10) -> None:
        if self.queue is not None:
            await asyncio.wait_for(self.queue.join(), timeout)

    async def _worker(self) -> None:
        assert self.queue is not None
        while True:
            ev, msg = await self.queue.get()
            try:
                await self._deliver(ev, msg)
            except Exception:  # pragma: no cover - không để worker chết
                log.exception("lỗi gửi thông báo %s", self.channel)
            finally:
                self.queue.task_done()

    async def _deliver(self, ev: HubEvent, msg: Any) -> None:
        c = self.cfg()
        attempts = int(c.get("max_attempts") or 5)
        base = float(c.get("backoff_base_s") or 2)
        ids = {"request_id": ev.payload.get("request_id"), "digest_id": ev.payload.get("digest_id")}
        for i in range(attempts):
            t = self.target()
            if t is None:
                return
            t0 = time.monotonic()
            try:
                extra = await self._send(t, ev, msg) or {}
                self.sent += 1
                self.hub.publish(make_event("notify.sent", ev.agent_id, {"channel": self.channel, **extra, "for": ev.type, **ids}, run_id=ev.run_id))
                return
            except Exception as e:
                self.failed += 1
                err = clip(e, 300)
                log.warning("gửi %s lỗi (lần %d/%d, %s): %s", self.channel, i + 1, attempts, ev.type, err)
                retry_in = base * (2**i) if i + 1 < attempts else None
                self.hub.publish(
                    make_event(
                        "notify.failed",
                        ev.agent_id,
                        {"channel": self.channel, "for": ev.type, **ids, "attempt": i + 1, "error": err, "retry_in_s": retry_in,
                         "elapsed_s": round(time.monotonic() - t0, 3)},
                        run_id=ev.run_id,
                    )
                )
                if retry_in is None:
                    return
                await asyncio.sleep(retry_in)
