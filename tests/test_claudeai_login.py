"""Đăng nhập claude.ai cho connector từ web hub (`account.claudeai_*`, docs/dev-notes/manager-connectors.md).

Người dùng tự đăng nhập trên web hub của mình: daemon chạy `claude auth login --claudeai` trong PTY (như luồng thêm tài khoản),
người mở link + dán code → CLI lưu phiên của user OS → connector claude.ai nạp được cho run. Không gọi `claude` thật:
`tests/fake_claude_auth.py` (status/login/logout) + PTY thật của nền tảng.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from cfharness import auth
from cfharness.errors import HubError
from cfharness.runtime import claude_cli
from m7_fake import close, make_daemon, rpc

FAKE = str(Path(__file__).with_name("fake_claude_auth.py"))
GOOD = "goodcode#state123"
TOK = "sk-ant-oat01-" + "Zz9_" * 20

needs_pty = pytest.mark.skipif(sys.platform == "win32", reason="claude giả là script shebang (POSIX)")


@pytest.fixture
async def d(hub_home, tmp_path, monkeypatch):
    for k in [k for k in os.environ if k.startswith(("FAKE_SETUP_", "FAKE_AUTH_"))]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("FAKE_AUTH_STATE", str(tmp_path / "claudeai.login"))
    monkeypatch.setenv("FAKE_AUTH_TOKEN_SEEN", str(tmp_path / "token.seen"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOK)  # env daemon có token → không bao giờ tới `claude auth *`
    dm = await make_daemon(hub_home)
    dm.hub.config.set("runtime.claude.binary", FAKE)
    dm.hub.secrets.set("CLAUDE_CODE_OAUTH_TOKEN", TOK)
    claude_cli._login_cache.clear()
    yield dm
    await dm.hub.accounts.close_all_logins()
    await close(dm)
    claude_cli._login_cache.clear()


def test_claudeai_methods_human_only():
    for m in ("account.claudeai_status", "account.claudeai_login_start", "account.claudeai_login_finish", "account.claudeai_logout"):
        assert m in auth.HUMAN_ONLY


@needs_pty
async def test_login_flow_status_and_logout(d, tmp_path):
    st = await rpc(d, "account.claudeai_status", {})
    assert st["logged_in"] is False and st["login"] is None and st["connectors_auto"] is True
    claude_cli._login_cache["claude"] = (0.0, False)

    lg = await rpc(d, "account.claudeai_login_start", {})
    assert lg["login_id"].startswith("l_") and lg["url"].startswith("https://")
    assert (await rpc(d, "account.claudeai_status", {}))["login"]["login_id"] == lg["login_id"]  # web hiện lại bước 2 sau tải trang
    # lượt claude.ai không dùng được để tạo tài khoản token
    with pytest.raises(Exception):
        await rpc(d, "account.login_finish", {"login_id": lg["login_id"], "code": GOOD})

    lg = await rpc(d, "account.claudeai_login_start", {})
    st = await rpc(d, "account.claudeai_login_finish", {"login_id": lg["login_id"], "code": GOOD})
    assert st["logged_in"] is True and st["email"] == "user@example.com" and st["login"] is None
    assert (tmp_path / "claudeai.login").exists()
    assert not (tmp_path / "token.seen").exists()  # không bao giờ mang token hub khi đăng nhập/kiểm phiên claude.ai
    assert claude_cli._login_cache == {}  # run sau kiểm lại phiên ngay
    assert d.hub.accounts._logins == {}
    ev = d.hub.store.query_events(types=["account.claudeai_login"])
    assert [e.payload for e in ev] == [{"id": "machine", "logged_in": True}]
    rows = [r[0] for r in d.hub.store.conn.execute("SELECT data FROM audit_log WHERE kind LIKE 'account.claudeai%'")]
    assert rows and not any(GOOD in (x or "") for x in rows)  # code không vào audit

    st = await rpc(d, "account.claudeai_logout", {})
    assert st["logged_in"] is False and not (tmp_path / "claudeai.login").exists()


@needs_pty
async def test_bad_code_reports_without_echo(d, tmp_path):
    lg = await rpc(d, "account.claudeai_login_start", {})
    with pytest.raises(Exception) as ei:
        await rpc(d, "account.claudeai_login_finish", {"login_id": lg["login_id"], "code": "wrong-code-xyz"})
    assert "wrong-code-xyz" not in str(ei.value)
    assert not (tmp_path / "claudeai.login").exists() and d.hub.accounts._logins == {}


@needs_pty
async def test_cli_exits_but_no_session_saved(d, tmp_path, monkeypatch):
    """CLI báo xong nhưng máy không lưu được phiên (vd Keychain) → lỗi rõ, không báo thành công."""
    lg = await rpc(d, "account.claudeai_login_start", {})
    monkeypatch.setenv("FAKE_AUTH_STATE", str(tmp_path / "other"))  # status đọc file khác → "chưa đăng nhập"
    h = d.hub.accounts._logins[lg["login_id"]].handle
    with pytest.raises(HubError, match="chưa thấy phiên claude.ai"):
        await d.hub.accounts.claudeai_login_finish(lg["login_id"], GOOD)
    assert h._closed


async def test_finish_unknown_login(d):
    with pytest.raises(Exception):
        await rpc(d, "account.claudeai_login_finish", {"login_id": "l_nope", "code": GOOD})
