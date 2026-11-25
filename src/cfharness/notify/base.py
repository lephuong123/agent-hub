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


RETRY_AFTER_MAX_S = 300.0  # trần chờ theo Retry-After / retry_after khi gửi thông báo bị 429


def _retry_after(e: BaseException) -> float | None:
    """Số giây dịch vụ yêu cầu chờ khi bị giới hạn tốc độ: thuộc tính `retry_after` (SlackError/TelegramError) hoặc header
    `Retry-After` (giây) của response 429 gắn trên lỗi. Không có/không hợp lệ → None."""
    v = getattr(e, "retry_after", None)
    if v is None:  # httpx: lỗi có `.response`; urllib HTTPError: chính lỗi có `.code` + `.headers` (webhook Slack)
        resp = getattr(e, "response", None) or e
        headers = getattr(resp, "headers", None)
        if headers is not None and (getattr(resp, "status_code", None) or getattr(resp, "code", None)) == 429:
            try:
                v = headers.get("Retry-After")
            except Exception:  # pragma: no cover
                v = None
    try:
        f = float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
    return f if f is not None and f > 0 else None


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
        # gọi ở MỌI event của bus → nhớ theo revision của config (plan-api-caching PR1); trả bản sao nông như trước
        rev = getattr(self.hub.config, "revision", None)  # config giả (test) không có revision → không nhớ
        if rev is None:
            return dict(self.hub.config.get(f"notify.{self.channel}") or {})
        memo = getattr(self, "_cfg_memo", None)
        if memo is None or memo[0] != rev:
            memo = (rev, dict(self.hub.config.get(f"notify.{self.channel}") or {}))
            self._cfg_memo = memo
        return dict(memo[1])

    def _secret(self, name: str | None) -> str | None:
        if not name:
            return None
        sec = getattr(self.hub, "secrets", None)
        if sec is not None and hasattr(sec, "lookup"):
            try:
                return sec.lookup(name)
            except Exception:  # fail-closed: không đọc được secret → không có (kênh tắt)
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
        from ..hub_freeze import frozen

        events = self.cfg().get("events") or []
        # approval.escalated = "người được hỏi lần đầu" → nhận ở mọi cấu hình đã nhận approval.open (danh sách cũ người tự đặt)
        # đề xuất golden có nút = bản một chạm của `golden.suggested` → nhận ở mọi cấu hình đã nhận golden.suggested
        # Lọc theo loại TRƯỚC (rẻ, loại ngay message.delta/progress) rồi mới hỏi DB `frozen` — plan-api-caching PR1
        if ev.type not in events and not (ev.type == "approval.escalated" and "approval.open" in events) \
                and not (ev.type == "golden.suggestion_opened" and "golden.suggested" in events):
            return
        if frozen(self.hub):  # hub đã chuyển nhà sang máy khác (hub_freeze): máy này không gửi gì ra kênh ngoài
            return
        if self._delegated(ev):
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

    def _delegated(self, ev: HubEvent) -> bool:
        """Approval đang giao điều phối viên (`routed_to=manager`) → chưa làm phiền người; quá hạn/điều phối viên chuyển lên →
        `approval.escalated` mới thông báo. Đọc trạng thái HIỆN TẠI trong DB (event cũ có thể chậm hơn)."""
        if ev.type not in ("approval.open", "approval.waiting_human", "approval.reminder"):
            return False
        rid = (ev.payload or {}).get("request_id")
        ap = getattr(self.hub, "approvals", None)
        if not rid or ap is None:
            return (ev.payload or {}).get("routed_to") == "manager"
        try:
            row = ap.get(str(rid))
        except Exception:
            log.debug("notify: không đọc được approval — dùng routed_to của event", exc_info=True)
            return (ev.payload or {}).get("routed_to") == "manager"
        return row.get("routed_to") == "manager" and row.get("status") in ("open", "waiting_human")

    def ensure_worker(self) -> None:
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(self._worker(), name="notify-worker")
            except RuntimeError:
                pass

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # best-effort: chờ worker đã huỷ kết thúc
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
        from ..hub_freeze import frozen

        for i in range(attempts):
            t = self.target()
            if t is None or frozen(self.hub):  # đóng băng giữa lúc tin còn trong hàng đợi → bỏ (máy mới gửi)
                return
            t0 = time.monotonic()
            try:
                extra = await self._send(t, ev, msg) or {}
                self.sent += 1
                self.hub.publish(make_event("notify.sent", ev.agent_id, {"channel": self.channel, **extra, "for": ev.type, **ids}, run_id=ev.run_id))
                return
            except Exception as e:  # best-effort: đã log (lỗi đã gỡ token) + event notify.failed; không exc_info vì traceback có thể mang URL webhook
                self.failed += 1
                err = clip(e, 300)
                log.warning("gửi %s lỗi (lần %d/%d, %s): %s", self.channel, i + 1, attempts, ev.type, err)
                retry_in = base * (2**i) if i + 1 < attempts else None
                ra = _retry_after(e)
                if retry_in is not None and ra is not None:  # 429: dịch vụ bảo chờ bao lâu → chờ đủ (trần RETRY_AFTER_MAX_S)
                    retry_in = max(retry_in, min(ra, RETRY_AFTER_MAX_S))
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
