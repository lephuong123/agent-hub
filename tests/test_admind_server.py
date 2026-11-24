"""admind: socket Unix + kiểm uid đầu kia (POSIX), client, plist cài đặt (plan-superadmin SA0-2)."""

from __future__ import annotations

import asyncio
import os
import plistlib
import socket
import sys

import pytest

from cfharness.admind import install
from cfharness.admind.client import AdmindClient, AdmindError, AdmindUnavailable
from cfharness.admind.server import Server, peer_uid

from admind_helpers import make_env

posix = pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or sys.platform == "win32", reason="socket Unix")


@posix
def test_peer_uid_reports_self():
    a, b = socket.socketpair()
    try:
        assert peer_uid(a) == os.getuid()
    finally:
        a.close()
        b.close()


@posix
async def test_socket_roundtrip_and_uid_gate(tmp_path, monkeypatch):
    # AF_UNIX giới hạn ~104 ký tự đường dẫn trên macOS → thư mục ngắn
    import tempfile

    short = tempfile.mkdtemp(prefix="adm")
    monkeypatch.setenv("CFHARNESS_TEST_ADMIND_ROOT", short)
    env = make_env(tmp_path, monkeypatch)
    monkeypatch.setenv("CFHARNESS_TEST_ADMIND_ROOT", short)
    from cfharness.admind.paths import AdmindPaths

    object.__setattr__(env.state, "paths", AdmindPaths.default())
    srv = Server(env.state, allowed_uids={os.getuid()})
    ready = asyncio.Event()
    task = asyncio.create_task(srv.serve(ready=ready.set))
    await asyncio.wait_for(ready.wait(), 5)
    try:
        cl = AdmindClient(env.state.paths.socket, timeout=5)
        r = await asyncio.to_thread(cl.call, "sys.ping")
        assert r["pong"] is True
        assert oct(os.stat(env.state.paths.socket).st_mode & 0o777) == "0o660"
        with pytest.raises(AdmindError) as ei:
            await asyncio.to_thread(cl.call, "admin.list")
        assert ei.value.code == "unauthenticated"
        # uid không thuộc danh sách → forbidden
        srv.dispatcher.allowed_uids = {12345}
        with pytest.raises(AdmindError) as ei:
            await asyncio.to_thread(cl.call, "sys.ping")
        assert ei.value.code == "forbidden"
    finally:
        task.cancel()
        env.state.db.close()


def test_client_unavailable(tmp_path):
    with pytest.raises(AdmindUnavailable):
        AdmindClient(tmp_path / "khong-co.sock", timeout=1).call("sys.ping")


def test_client_transport(tmp_path, monkeypatch):
    env = make_env(tmp_path, monkeypatch)
    from cfharness.admind.dispatch import Peer

    cl = AdmindClient(transport=lambda req: env.disp.handle(req, Peer(uid=501)))
    assert cl.call("sys.ping")["pong"]
    with pytest.raises(AdmindError):
        cl.call("không-có")
    env.state.db.close()


def test_install_plists():
    a = plistlib.loads(install.admind_plist())
    assert a["Label"] == "com.cfharness.admind" and a["ProgramArguments"][:3] == ["/opt/cfharness/bin/cfharness", "admind", "serve"]
    assert "UserName" not in a  # root
    s = plistlib.loads(install.sac_plist(port=8700))
    assert s["UserName"] == "_cfh_sac" and "--port" in s["ProgramArguments"]
    cmds = [" ".join(c) for c in install.sac_user_cmds()]
    assert any("UserShell /usr/bin/false" in c for c in cmds) and any("IsHidden 1" in c for c in cmds)
