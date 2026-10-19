"""JSON-RPC 2.0 qua Unix socket (Windows: TCP loopback — `transport.py`), mỗi thông điệp một dòng JSON (newline-delimited).

Vì sao Unix socket (không phải WebSocket localhost):
- Không mở cổng mạng nào (NFR-14); quyền truy cập = quyền file socket (0600, chỉ user hiện tại) → không cần token.
- Không thêm thư viện (stdlib asyncio/socket), độ trễ thấp, hook `cfharness hook …` (M1) gọi về dễ.
- TUI (Textual) và MCP server của hub đều chạy cùng máy, cùng user → không cần HTTP ở M0.
  Khi cần client từ xa/web (M1+: MCP HTTP) sẽ thêm transport HTTP có token, dùng chung bảng method.

Xác thực người gọi (H-01): client của NGƯỜI tự đọc token ở `<socket>.auth` (0600, daemon sinh mỗi lần khởi động)
và gửi trong `params._auth` — không thêm bước tay. Daemon còn kiểm pid ngang hàng (xem `auth.py`).

Subscription: `events.subscribe` biến kết nối thành luồng notification `{"method":"event","params":{…}}`;
kết thúc bằng notification `{"method":"events.end","params":{"reason":…}}`.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any, Iterator

from . import transport
from .errors import SYSTEM_ERROR

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class DaemonUnavailable(Exception):
    pass


def encode(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode("utf-8")


def with_auth(sock_path: Path | str, params: dict[str, Any] | None) -> dict[str, Any]:
    """Gắn token người (nếu đọc được) vào params — dùng chung cho mọi client của người."""
    from .auth import AUTH_PARAM, load_token

    p = dict(params or {})
    tok = load_token(sock_path)
    if tok and AUTH_PARAM not in p:
        p[AUTH_PARAM] = tok
    return p


class RpcClient:
    def __init__(self, sock_path: Path, timeout: float | None = 60.0) -> None:
        self.sock_path = sock_path
        self.timeout = timeout
        self._id = 0

    def _connect(self) -> socket.socket:
        try:
            s = transport.connect(self.sock_path, self.timeout)
        except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
            raise DaemonUnavailable(f"daemon không chạy (socket {self.sock_path}): {e}") from None
        return s

    def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        s = self._connect()
        if timeout is not None:
            s.settimeout(timeout)
        try:
            self._id += 1
            s.sendall(encode({"jsonrpc": "2.0", "id": self._id, "method": method, "params": with_auth(self.sock_path, params)}))
            f = s.makefile("rb")
            line = f.readline()
            if not line:
                raise RpcError(SYSTEM_ERROR, "daemon đóng kết nối không trả lời")
            msg = json.loads(line)
        finally:
            s.close()
        if "error" in msg:
            e = msg["error"]
            raise RpcError(e.get("code", SYSTEM_ERROR), e.get("message", "lỗi"), e.get("data"))
        return msg.get("result")

    def subscribe(self, params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Yield message: kết quả subscribe đầu tiên (`{"result":…}`), rồi các event dict; dừng khi `events.end`."""
        s = self._connect()
        s.settimeout(None)
        try:
            self._id += 1
            s.sendall(encode({"jsonrpc": "2.0", "id": self._id, "method": "events.subscribe", "params": params}))
            f = s.makefile("rb")
            first = json.loads(f.readline() or b"{}")
            if "error" in first:
                e = first["error"]
                raise RpcError(e.get("code", SYSTEM_ERROR), e.get("message", "lỗi"), e.get("data"))
            yield {"subscribed": first.get("result")}
            for line in f:
                msg = json.loads(line)
                if msg.get("method") == "event":
                    yield msg["params"]
                elif msg.get("method") == "events.end":
                    yield {"end": msg.get("params", {})}
                    return
        finally:
            s.close()
