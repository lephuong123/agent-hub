"""Daemon tự mở lại socket nghe khi asyncio đóng nó sau lỗi accept (Windows ProactorEventLoop, WinError 64).

Trước bản sửa: tiến trình daemon vẫn sống nhưng không còn LISTENING → `cfharness chat`/web/CLI đều báo "daemon không chạy".
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from cfharness import oscompat, transport
from cfharness.daemon import Daemon
from cfharness.oscompat import IS_WINDOWS
from cfharness.paths import HubPaths
from cfharness.rpc import RpcClient
from cfharness.runtime.fake import FakeRuntime


@pytest.fixture
def home(monkeypatch):
    base = Path(tempfile.gettempdir()) if IS_WINDOWS else Path(os.path.realpath("/tmp"))
    h = base / f"ahlis-{os.getpid()}-{time.time_ns() % 10**6}"
    monkeypatch.setenv("CFHARNESS_HOME", str(h))
    monkeypatch.delenv("CFHARNESS_SOCKET", raising=False)
    p = HubPaths.resolve(h)
    p.ensure()
    p.config.write_text("guard:\n  git_probe: false\n")
    yield p
    shutil.rmtree(h, ignore_errors=True)


def _ping(p: HubPaths) -> dict:
    return RpcClient(p.socket, timeout=5).call("hub.ping")


def test_server_listening_detects_closed_socket():
    async def go():
        srv = await asyncio.start_server(lambda r, w: None, host="127.0.0.1", port=0)
        try:
            assert transport.server_listening(srv)
            srv.sockets[0]._sock.close()  # như asyncio làm trong `_start_serving` khi accept lỗi
            assert not transport.server_listening(srv)
        finally:
            srv.close()

    asyncio.run(go())


async def test_daemon_reopens_listener_after_accept_failure(home):
    d = Daemon(home, runtime=FakeRuntime(hold=True))
    d.hub.start()
    d.server = await transport.start_server(d._handle_conn, home.socket, limit=16 * 1024 * 1024)
    watch = asyncio.create_task(d._watch_listener(interval=0.05))
    try:
        assert (await asyncio.to_thread(_ping, home))["pong"]
        old = d.server
        old.sockets[0]._sock.close()
        assert not transport.server_listening(old)
        for _ in range(100):
            await asyncio.sleep(0.05)
            if d.server is not old and transport.server_listening(d.server):
                break
        assert d.server is not old and transport.server_listening(d.server)
        assert (await asyncio.to_thread(_ping, home))["pong"]  # client đọc lại endpoint mới
    finally:
        d.stopping.set()
        await watch
        d.server.close()
        oscompat.JOBS.close_all()
        d.hub.runs._runtime.release()
        await d.hub.stop()
