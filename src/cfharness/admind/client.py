"""Client của admind — dùng bởi web SAC (`superadmin/`) và CLI `cfharness superadmin|admind`.

`AdmindClient(transport=...)` nhận một hàm `dict -> dict` để test/ dev gọi thẳng `Dispatcher.handle` (không socket, mọi nền tảng).
"""

from __future__ import annotations

import json
import secrets
import socket
from pathlib import Path
from typing import Any, Callable

from .paths import AdmindPaths


class AdmindError(Exception):
    def __init__(self, code: str, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class AdmindUnavailable(AdmindError):
    def __init__(self, message: str = "admind không chạy") -> None:
        super().__init__("unavailable", message)


class AdmindClient:
    def __init__(self, socket_path: Path | str | None = None, *, timeout: float = 30.0,
                 transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self.socket_path = Path(socket_path) if socket_path else AdmindPaths.default().socket
        self.timeout = timeout
        self.transport = transport

    def _send(self, req: dict[str, Any]) -> dict[str, Any]:
        if self.transport is not None:
            return self.transport(json.loads(json.dumps(req, default=str)))
        if not hasattr(socket, "AF_UNIX"):
            raise AdmindUnavailable("admind chỉ có trên macOS/Linux")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]
        s.settimeout(self.timeout)
        try:
            s.connect(str(self.socket_path))
            s.sendall((json.dumps(req, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise AdmindUnavailable(f"admind không chạy ({e.__class__.__name__})") from None
        except OSError as e:
            raise AdmindUnavailable(f"không nối được admind: {e}") from None
        finally:
            s.close()
        try:
            return json.loads(buf.decode("utf-8"))
        except ValueError:
            raise AdmindUnavailable("admind trả dữ liệu hỏng") from None

    def call(self, op: str, params: dict[str, Any] | None = None, *, ticket: str | None = None, ip: str = "", ua: str = "") -> Any:
        req = {"op": op, "params": params or {}, "req_id": secrets.token_hex(6), "meta": {"ip": ip, "ua": ua}}
        if ticket:
            req["ticket"] = ticket
        resp = self._send(req)
        if not isinstance(resp, dict):
            raise AdmindUnavailable("admind trả dữ liệu hỏng")
        if resp.get("ok"):
            return resp.get("result")
        err = resp.get("error") or {}
        raise AdmindError(str(err.get("code") or "failed"), str(err.get("message") or "lỗi"), err.get("data"))
