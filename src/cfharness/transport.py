"""Kênh điều khiển daemon ↔ client — chỉ stdlib (hook import được, chạy ở MỖI tool call).

- POSIX: Unix socket tại `HubPaths.socket` (0600) — giữ nguyên như trước.
- Windows: CPython không có `AF_UNIX`, nên daemon nghe TCP **chỉ trên 127.0.0.1**, cổng ngẫu nhiên; file `HubPaths.socket`
  (vẫn tên `hub.sock` để guard/sandbox/bộ phân loại bảo vệ y như cũ) là **file endpoint** JSON `{"host","port","pid"}`.
  Xác định người gọi bằng bảng TCP của kernel (`oscompat.tcp_peer_pid`) + kiểm cùng user OS (`oscompat.same_user`) —
  cổng loopback mọi user trên máy đều nối được, nên daemon từ chối kết nối của user khác / không xác định được pid.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path
from typing import Any, Awaitable, Callable

from .oscompat import IS_WINDOWS

LOOPBACK = "127.0.0.1"


def read_endpoint(path: str | os.PathLike[str]) -> tuple[str, int]:
    """(host, port) từ file endpoint (Windows). OSError/ValueError nếu không có/hỏng."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    host, port = str(data["host"]), int(data["port"])
    if host != LOOPBACK:
        raise ValueError(f"endpoint không phải loopback: {host}")
    return host, port


def write_endpoint(path: str | os.PathLike[str], port: int) -> None:
    p = Path(path)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"host": LOOPBACK, "port": int(port), "pid": os.getpid()}), encoding="utf-8")
    os.replace(tmp, p)


def connect(path: str | os.PathLike[str], timeout: float | None) -> socket.socket:
    """Mở kết nối tới daemon. Lỗi → OSError (FileNotFoundError/ConnectionRefusedError/…)."""
    if IS_WINDOWS:
        try:
            host, port = read_endpoint(path)
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            raise ConnectionRefusedError(f"file endpoint hỏng {path}: {e}") from None
        return socket.create_connection((host, port), timeout=timeout)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # type: ignore[attr-defined]
    s.settimeout(timeout)
    try:
        s.connect(str(path))
    except OSError:
        s.close()
        raise
    return s


async def open_connection(path: str | os.PathLike[str], limit: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if IS_WINDOWS:
        try:
            host, port = read_endpoint(path)
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            raise ConnectionRefusedError(f"file endpoint hỏng {path}: {e}") from None
        return await asyncio.open_connection(host, port, limit=limit)
    return await asyncio.open_unix_connection(str(path), limit=limit)  # type: ignore[attr-defined]


async def start_server(cb: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
                       path: str | os.PathLike[str], limit: int) -> Any:
    """Server của daemon. POSIX: Unix socket 0600 (umask 0177). Windows: TCP 127.0.0.1:<ngẫu nhiên> + file endpoint."""
    if IS_WINDOWS:
        server = await asyncio.start_server(cb, host=LOOPBACK, port=0, limit=limit)
        write_endpoint(path, server.sockets[0].getsockname()[1])
        return server
    old_umask = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(cb, path=str(path), limit=limit)  # type: ignore[attr-defined]
    finally:
        os.umask(old_umask)
    os.chmod(path, 0o600)
    return server


def peer_pid(sock: Any) -> int | None:
    """pid đầu kia kết nối, do kernel báo (macOS LOCAL_PEERPID, Linux SO_PEERCRED, Windows bảng TCP)."""
    if IS_WINDOWS:
        from .oscompat import tcp_peer_pid

        return tcp_peer_pid(sock)
    from .auth import peer_pid as _unix_peer_pid

    return _unix_peer_pid(sock)
