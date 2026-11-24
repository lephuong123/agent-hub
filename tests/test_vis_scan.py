"""FR-SA-VIS-10: user thường không thấy dấu vết quản trị nào — quét mọi trang web, lệnh CLI, method RPC `admin.*`, tin Telegram/Slack.

Chạy trong CI mỗi PR (NFR-SA-04): lộ một route/lệnh/method quản trị cho user thường = lỗi chặn phát hành.
"""

from __future__ import annotations

import re

import pytest

from cfharness import admin_control as ac
from cfharness.runtime import FakeRuntime

TRACES = re.compile(r"Quản trị máy|Người dùng máy|Tổng quan máy|superadmin|Super Admin|/admin/|cfharness-admin|ESTOP quản trị|admind",
                    re.IGNORECASE)


@pytest.fixture
def shared(tmp_path, monkeypatch):
    root = tmp_path / "shared"
    for d in ("control", "admin-log", "policy", "status"):
        (root / d).mkdir(parents=True)
    monkeypatch.setenv("CFHARNESS_TEST_SHARED_ROOT", str(root))
    return root


async def test_web_pages_have_no_admin_traces(hub_home, shared):
    from test_web import ISOLATED, _start, _stop

    env = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        ac.set_flag(True, ac.os_user(), reason_code="security", by="boss")  # ESTOP quản trị đang bật: banner phải trung tính
        env.hub.admin_control.check()
        await env.login()
        paths = sorted({r.path for r in env.app.routes if getattr(r, "path", None) and "{" not in r.path
                        and not r.path.startswith(("/static", "/sse", "/partials", "/do/", "/api", "/login", "/logout", "/enroll", "/mcp-oauth"))
                        and "GET" in (getattr(r, "methods", None) or {"GET"})})
        assert len(paths) > 15
        leaks = []
        for p in paths:
            r = await env.client.get(p)
            if r.status_code == 200 and TRACES.search(r.text):
                leaks.append((p, TRACES.search(r.text).group(0)))
        assert not leaks, leaks
        for p in ("/admin/overview", "/admin/users", "/admin/alerts", "/admin/audit", "/machine"):
            assert (await env.client.get(p)).status_code == 404, p
    finally:
        await _stop(env)


async def test_rpc_admin_methods_hidden(hub_home, shared):
    from m7_fake import close, make_daemon, rpc

    from cfharness.errors import HubError

    d = await make_daemon(hub_home)
    try:
        names = sorted(m for m in d.methods if m.startswith(("admin.", "superadmin.")))
        assert names and not any(m.startswith("superadmin.") for m in names)  # hub không có method SAC (API-02)
        for m in names:
            if m == "admin.nav":
                assert (await rpc(d, m, {})) == {"is_admin": False, "managed": False}
                continue
            with pytest.raises(HubError) as ei:
                await rpc(d, m, {"user": ac.os_user(), "token": "x" * 40})
            assert str(ei.value).startswith("không có"), (m, str(ei.value))  # method không tồn tại / không có (directory_serve)
    finally:
        await close(d)


def test_cli_admin_commands_hidden(monkeypatch):
    monkeypatch.setenv("CFHARNESS_TEST_SHOW_ADMIN_CLI", "0")
    monkeypatch.setattr(ac, "_GETGROUPS", lambda: [])
    import os

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("chạy bằng root: lệnh quản trị hiện có chủ đích")
    from cfharness.cli import _top_commands, build_parser

    top = {name for name, _ in _top_commands(build_parser())}
    assert not top & {"admin", "admind", "superadmin"}, top & {"admin", "admind", "superadmin"}
    help_text = build_parser().format_help()
    assert not TRACES.search(help_text)


def test_notify_neutral():
    from types import SimpleNamespace

    from cfharness.notify.slack import format_message

    for on in (True, False):
        ev = SimpleNamespace(type="hub.admin_estop", payload={"on": on, "reason_code": "security", "scope": "user"}, run_id=None, agent_id=None)
        try:
            msg = format_message(ev)
        except TypeError:
            msg = format_message(ev, None)
        assert msg and not TRACES.search(msg) and "security" not in msg
