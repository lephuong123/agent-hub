"""Client JSON-RPC bất đồng bộ cho TUI (asyncio, cùng event loop với Textual).

- `call(method, params)`: mỗi lời gọi một kết nối Unix socket ngắn (giống `rpc.RpcClient`), không chặn UI.
- `subscribe(params)`: kết nối dài `events.subscribe` → async iterator các message: `{"subscribed": {...}}`,
  rồi từng event (dict wire), cuối cùng `{"end": {"reason": …}}`. `close()` đóng kết nối — daemon thấy
  `client_closed` và bỏ subscription; run KHÔNG bị ảnh hưởng (NFR-05).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

from .. import transport
from ..errors import SYSTEM_ERROR
from ..rpc import DaemonUnavailable, RpcError, encode, raise_for, response_req_id, with_auth

LIMIT = 64 * 1024 * 1024


class AsyncHubClient:
    def __init__(self, sock_path: Path | str, *, channel: str = "tui", timeout: float = 30.0) -> None:
        self.sock_path = str(sock_path)
        self.channel = channel
        self.timeout = timeout
        self._id = 0
        self.last_req_id: str | None = None

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            return await transport.open_connection(self.sock_path, limit=LIMIT)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
            raise DaemonUnavailable(f"daemon không chạy (socket {self.sock_path}): {e}") from None

    async def call(self, method: str, params: dict[str, Any] | None = None, *, audit: bool = False, timeout: float | None = None) -> Any:
        params = dict(params or {})
        if audit:
            params["_audit"] = {"channel": self.channel, "actor": "user"}
        reader, writer = await self._open()  # mở socket TRƯỚC: daemon dừng → DaemonUnavailable (không phải lỗi kiểm chủ socket)
        sent: str | None = None
        try:
            p = with_auth(self.sock_path, params)  # + `_trace.req_id` (L2): bind trong task hiện tại hoặc sinh mới
            sent = p["_trace"]["req_id"]
            self._id += 1
            writer.write(encode({"jsonrpc": "2.0", "id": self._id, "method": method, "params": p}))
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout or self.timeout)
        finally:
            writer.close()
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
            except Exception:  # pragma: no cover
                pass
            self._writer = None


def subscribe(client: AsyncHubClient, params: dict[str, Any]) -> Subscription:
    return Subscription(client, params)
