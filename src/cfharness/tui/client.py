"""Client JSON-RPC bất đồng bộ cho TUI (asyncio, cùng event loop với Textual).

- `call(method, params)`: dùng lại kết nối rảnh (hồ ≤ POOL_MAX, rảnh ≤ POOL_IDLE_S, cùng event loop) — mỗi kết nối mới tốn bắt tay
  + daemon tra pid ngang hàng (Windows: quét bảng TCP) + kiểm user. Daemon xử lý tuần tự nhiều yêu cầu trên một kết nối; mỗi kết nối
  chỉ một yêu cầu đang chờ tại một thời điểm. `audit=True` luôn mở kết nối mới rồi đóng (như trước). Kết nối dùng lại bị daemon đóng
  (dừng/khởi động lại) trước khi có trả lời → lời gọi ĐỌC thử lại MỘT lần trên kết nối mới; lời gọi GHI (`auth.all_mutating()`) không
  bao giờ gửi lại. Timeout/lỗi giữa chừng → kết nối bị bỏ (không trả về hồ: trả lời muộn sẽ lệch thứ tự). Kết nối `events.subscribe`
  không bao giờ vào hồ.
- `subscribe(params)`: kết nối dài `events.subscribe` → async iterator các message: `{"subscribed": {...}}`,
  rồi từng event (dict wire), cuối cùng `{"end": {"reason": …}}`. `close()` đóng kết nối — daemon thấy
  `client_closed` và bỏ subscription; run KHÔNG bị ảnh hưởng (NFR-05).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, AsyncIterator

from .. import transport
from ..perf import note_rpc
from ..errors import SYSTEM_ERROR
from ..rpc import DaemonUnavailable, RpcError, encode, raise_for, response_req_id, with_auth

LIMIT = 64 * 1024 * 1024
POOL_MAX = 8  # kết nối rảnh giữ lại tối đa (web gọi đồng thời bằng asyncio.gather)
POOL_IDLE_S = 20.0

_MUTATING: frozenset[str] | None = None


def _is_mutating(method: str) -> bool:
    global _MUTATING
    if _MUTATING is None:
        from ..auth import all_mutating

        _MUTATING = all_mutating()
    return method in _MUTATING


class _StaleConnection(Exception):
    """Kết nối lấy từ hồ đã bị daemon đóng trước khi có trả lời."""


def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except Exception:  # best-effort: loop đã đóng / kết nối hỏng  # pragma: no cover
        pass


class AsyncHubClient:
    def __init__(self, sock_path: Path | str, *, channel: str = "tui", timeout: float = 30.0, cred: str = "human",
                 client_meta: Any = None) -> None:
        self.sock_path = str(sock_path)
        self.channel = channel
        # L3: loại token (`web` → `<socket>.web.auth`) + hàm trả metadata client theo yêu cầu đang xử lý (web: session_ref/ip) → `params._client`
        self.cred = cred
        self.client_meta = client_meta
        self.timeout = timeout
        self._id = 0
        self.last_req_id: str | None = None
        # (reader, writer, loop, lúc rảnh) — chỉ kết nối hỏi–đáp đã trả lời trọn vẹn
        self._idle: list[tuple[asyncio.StreamReader, asyncio.StreamWriter, asyncio.AbstractEventLoop, float]] = []

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            return await transport.open_connection(self.sock_path, limit=LIMIT)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
            raise DaemonUnavailable(f"daemon không chạy (socket {self.sock_path}): {e}") from None

    async def call(self, method: str, params: dict[str, Any] | None = None, *, audit: bool = False, timeout: float | None = None) -> Any:
        t0 = time.perf_counter()
        try:
            return await self._call(method, params, audit=audit, timeout=timeout)
        finally:
            note_rpc(method, (time.perf_counter() - t0) * 1000.0)  # phân rã request web (perf.py) — no-op ngoài request đang đo

    def _take(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        loop = asyncio.get_running_loop()
        now = time.monotonic()
        while self._idle:
            reader, writer, lp, since = self._idle.pop()
            if lp is loop and now - since < POOL_IDLE_S and not reader.at_eof() and not writer.is_closing() and reader.exception() is None:
                return reader, writer
            if lp is loop:
                _close(writer)
        return None

    def _give_back(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._idle) < POOL_MAX and not reader.at_eof() and not writer.is_closing():
            self._idle.append((reader, writer, asyncio.get_running_loop(), time.monotonic()))
        else:
            _close(writer)

    def close_idle(self) -> None:
        """Đóng mọi kết nối rảnh."""
        while self._idle:
            _close(self._idle.pop()[1])

    async def _call(self, method: str, params: dict[str, Any] | None, *, audit: bool, timeout: float | None) -> Any:
        params = dict(params or {})
        if audit:
            params["_audit"] = {"channel": self.channel, "actor": "user"}
        poolable = not audit
        conn = self._take() if poolable else None
        if conn is not None:
            try:
                return await self._roundtrip(method, params, conn, reused=True, keep=True, timeout=timeout)
            except _StaleConnection:
                # daemon đóng kết nối cũ (dừng/khởi động lại) mà không trả lời: lời gọi ĐỌC thử lại một lần trên kết nối mới; lời gọi
                # GHI không gửi lại (không biết daemon đã xử lý chưa) — báo lỗi như kết nối bị đóng
                if _is_mutating(method):
                    raise RpcError(SYSTEM_ERROR, "daemon đóng kết nối không trả lời") from None
        # mở socket TRƯỚC: daemon dừng → DaemonUnavailable (không phải lỗi kiểm chủ socket)
        return await self._roundtrip(method, params, await self._open(), reused=False, keep=poolable, timeout=timeout)

    async def _roundtrip(self, method: str, params: dict[str, Any], conn: tuple[asyncio.StreamReader, asyncio.StreamWriter], *,
                         reused: bool, keep: bool, timeout: float | None) -> Any:
        reader, writer = conn
        params = dict(params)
        sent: str | None = None
        line = b""
        try:
            meta = self.client_meta() if self.client_meta is not None else None
            if meta:
                params["_client"] = dict(meta)
            p = with_auth(self.sock_path, params, kind=self.cred)  # + `_trace.req_id` (L2): bind trong task hiện tại hoặc sinh mới
            sent = p["_trace"]["req_id"]
            self._id += 1
            try:
                writer.write(encode({"jsonrpc": "2.0", "id": self._id, "method": method, "params": p}))
                await writer.drain()
                line = await asyncio.wait_for(reader.readline(), timeout or self.timeout)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                if reused:
                    raise _StaleConnection() from None
                raise
            if not line and reused:
                raise _StaleConnection()
        finally:
            if line and keep:
                self._give_back(reader, writer)  # trả lời trọn một dòng → kết nối sạch, dùng tiếp được
            else:
                _close(writer)
        if not line:
            raise RpcError(SYSTEM_ERROR, "daemon đóng kết nối không trả lời", req_id=sent)
        msg = json.loads(line)
        self.last_req_id = response_req_id(msg, sent)
        raise_for(msg, sent)
        return msg.get("result")


class Subscription:
    """`async for msg in sub` — xem docstring module."""

    def __init__(self, client: AsyncHubClient, params: dict[str, Any]) -> None:
        self.client = client
        self.params = params
        self._writer: asyncio.StreamWriter | None = None
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        reader, writer = await self.client._open()
        self._writer = writer
        try:
            self.client._id += 1
            writer.write(encode({"jsonrpc": "2.0", "id": self.client._id, "method": "events.subscribe", "params": self.params}))
            await writer.drain()
            first = json.loads(await reader.readline() or b"{}")
            raise_for(first)
            yield {"subscribed": first.get("result") or {}}
            while not self.closed:
                line = await reader.readline()
                if not line:
                    yield {"end": {"reason": "connection_lost"}}
                    return
                msg = json.loads(line)
                if msg.get("method") == "event":
                    yield msg["params"]
                elif msg.get("method") == "events.end":
                    yield {"end": msg.get("params") or {}}
                    return
        finally:
            self.close()

    def close(self) -> None:
        self.closed = True
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # best-effort: đóng kết nối đã hỏng  # pragma: no cover
                pass
            self._writer = None


def subscribe(client: AsyncHubClient, params: dict[str, Any]) -> Subscription:
    return Subscription(client, params)
