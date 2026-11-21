"""Socket của admind (C3: không bao giờ nghe mạng) + vòng nền định kỳ.

- POSIX: socket Unix `paths.socket`, `root:<nhóm của _cfh_sac> 0660` trong thư mục 0750. Mỗi kết nối kiểm uid đầu kia do KERNEL báo
  (`peer_uid`: macOS `LOCAL_PEERCRED`, Linux `SO_PEERCRED`) — chỉ root và `_cfh_sac`. Payload không tự khai danh tính được.
- Một dòng JSON một yêu cầu (≤ `MAX_REQ_BYTES`), trả một dòng JSON. Xử lý trong thread (`Dispatcher.handle` đồng bộ).
- Windows: không hỗ trợ (admind nhắm macOS — NFR-SA-12); `serve` báo lỗi rõ.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
import sys
from typing import Any, Awaitable, Callable

from .dispatch import MAX_REQ_BYTES, Dispatcher, Peer, State
from .paths import SAC_USER

log = logging.getLogger("cfharness.admind.server")


def peer_uid(sock: Any) -> int | None:
    try:
        if sys.platform == "darwin":
            data = sock.getsockopt(0, 0x001, 256)  # SOL_LOCAL, LOCAL_PEERCRED → struct xucred {u_int cr_version; uid_t cr_uid; ...}
            _ver, uid = struct.unpack_from("=II", data)
            return int(uid)
        if hasattr(socket, "SO_PEERCRED"):
            _pid, uid, _gid = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            return int(uid)
    except (OSError, AttributeError, struct.error):
        return None
    return None


def sac_uid_gid() -> tuple[int | None, int | None]:
    try:
        import pwd

        pw = pwd.getpwnam(SAC_USER)
        return int(pw.pw_uid), int(pw.pw_gid)
    except (ImportError, KeyError):
        return None, None


class Server:
    def __init__(self, state: State, *, allowed_uids: set[int] | None = None) -> None:
        sac_uid, self.sac_gid = sac_uid_gid()
        uids = allowed_uids if allowed_uids is not None else ({sac_uid} if sac_uid is not None else set())
        self.dispatcher = Dispatcher(state, allowed_uids=uids)
        state.extra["dispatcher"] = self.dispatcher
        self.state = state
        self.periodic: list[tuple[float, Callable[[State], Any]]] = []

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        uid = peer_uid(sock) if sock is not None else None
        peer = Peer(uid=uid, root=(uid == 0))
        try:
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError:
                    return
                except asyncio.LimitOverrunError:
                    writer.write(b'{"ok":false,"error":{"code":"invalid","message":"yeu cau qua lon"}}\n')
                    await writer.drain()
                    return
                try:
                    req = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    req = None
                resp = await asyncio.to_thread(self.dispatcher.handle, req, peer)
                writer.write((json.dumps(resp, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
                await writer.drain()
        except (ConnectionError, OSError):
            return
        finally:
            try:
                writer.close()
            except Exception:  # pragma: no cover
                pass

    def every(self, seconds: float, fn: Callable[[State], Any]) -> None:
        self.periodic.append((seconds, fn))

    async def _loop(self, seconds: float, fn: Callable[[State], Any]) -> None:
        while True:
            try:
                await asyncio.to_thread(fn, self.state)
            except Exception:  # noqa: BLE001
                log.exception("việc định kỳ %s lỗi", getattr(fn, "__name__", fn))
            await asyncio.sleep(seconds)

    async def serve(self, *, ready: Callable[[], Awaitable[None] | None] | None = None) -> None:
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("admind chỉ chạy trên macOS/Linux (socket Unix)")
        path = self.state.paths.socket
        self.state.paths.ensure()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        old = os.umask(0o117)
        try:
            server = await asyncio.start_unix_server(self._client, path=str(path), limit=MAX_REQ_BYTES)  # type: ignore[attr-defined]
        finally:
            os.umask(old)
        if os.geteuid() == 0 and self.sac_gid is not None:  # type: ignore[attr-defined]
            os.chown(path, 0, self.sac_gid)  # type: ignore[attr-defined]
            os.chown(self.state.paths.run_dir, 0, self.sac_gid)  # type: ignore[attr-defined]
        os.chmod(path, 0o660)
        tasks = [asyncio.create_task(self._loop(s, f)) for s, f in self.periodic]
        log.info("admind nghe %s", path)
        if ready:
            r = ready()
            if asyncio.iscoroutine(r):
                await r
        try:
            async with server:
                await server.serve_forever()
        finally:
            for t in tasks:
                t.cancel()
