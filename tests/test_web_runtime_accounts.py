"""Thẻ "Tài khoản ChatGPT (Codex)" / "Tài khoản Grok" ở Cài đặt → Tài khoản AI (web/runtime_account_pages.py) + form "tạo điều phối viên
mới" chọn Claude/ChatGPT/Grok (web/manager.py). Daemon IN-PROCESS như tests/test_web_accounts.py; RPC `runtime_account.*` thay bằng
handler GIẢ ghi lại lời gọi.

Kiểm (không được chặn người dùng): trang hiện NGAY dữ liệu cache (không chờ kiểm); daemon cũ không có RPC / dữ liệu lạ → khối Claude vẫn
dùng được; Dùng/Đăng xuất/Xoá cần xác nhận rồi mới gọi RPC; thêm tài khoản cần tên; ô form không tên `method`; form điều phối viên THÊM
lựa chọn runtime (agent cũ vẫn chỉ định được).
"""

from __future__ import annotations

import pytest

from cfharness.runtime import FakeRuntime
from cfharness.web.app import ACTIONS  # noqa: F401 — nạp app (đăng ký thao tác) trước
from test_web import ISOLATED, _start, _stop

RT_RPC = ("runtime_account.list", "runtime_account.refresh", "runtime_account.login_start", "runtime_account.login_cancel",
          "runtime_account.logout", "runtime_account.use", "runtime_account.rm", "runtime_account.detect")


def _card(kind: str, label: str, accounts: list[dict], login=None, binary="C:/x/cli.exe") -> dict:
    return {"kind": kind, "label": label, "active": next((a["id"] for a in accounts if a.get("active")), "machine"), "accounts": accounts,
            "binary": binary, "version": "1.0", "configured": kind, "model": None, "effort": None, "login": login, "candidates": []}


class FakeRt:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.data = {
            "codex": _card("codex", "ChatGPT (Codex)", [
                {"id": "machine", "label": "Đăng nhập của máy", "machine": True, "active": True, "checking": False, "logged_in": True,
                 "email": "biz@example.com", "plan": "team", "workspace": "Acme", "checked_at": 1795590000.0,
                 "rate_limits": {"primary": {"used_percent": 7, "window_mins": 300}, "secondary": {"used_percent": 2, "window_mins": 10080}}},
                {"id": "a_12345678", "label": "Cá nhân", "machine": False, "active": False, "checking": True, "logged_in": None,
                 "checked_at": None}]),
            "grok": _card("grok", "Grok", [{"id": "machine", "label": "Đăng nhập của máy", "machine": True, "active": True, "checking": False,
                                            "logged_in": False, "checked_at": 1795590000.0}], binary=None),
        }

    def install(self, d) -> None:
        def mk(name):
            def h(p):
                p = {k: v for k, v in (p or {}).items() if k != "_audit"}
                self.calls.append((name, p))
                if name == "runtime_account.list":
                    return self.data
                if name == "runtime_account.login_start":
                    return {"state": "pending", "url": "https://auth.openai.com/codex/device", "code": "ABCD-EFGHI", "label": p.get("label")}
                return {"ok": True}
            return h

        for n in RT_RPC:
            d.methods[n] = mk(n)


@pytest.fixture
async def env(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    e.rt = FakeRt()
    e.rt.install(e.d)
    await e.login()
    yield e
    await _stop(e)


def called(env, name: str) -> list[dict]:
    return [p for n, p in env.rt.calls if n == name]


async def test_cards_render_inline_from_cache(env):
    r = await env.client.get("/settings/accounts")
    assert r.status_code == 200
    t = r.text
    assert "Tài khoản AI" in t and 'id="accounts"' in t  # khối Claude vẫn còn
    assert 'id="rt-codex"' in t and 'id="rt-grok"' in t
    assert "Đang kiểm tài khoản" not in t  # không còn khung chờ: vẽ NGAY từ cache
    assert "biz@example.com" in t and "Business" in t and "workspace: Acme" in t and "5 giờ 7%" in t and "tuần 2%" in t
    assert "Đang kiểm…" in t  # tài khoản mới chưa có cache: chip đang kiểm, thẻ tự làm mới
    assert 'hx-trigger="every 3s"' in t
    assert "Không tìm thấy CLI" in t  # grok chưa cài
    assert 'name="method"' not in t
    assert called(env, "runtime_account.list")


async def test_no_polling_when_idle(env):
    env.rt.data["codex"]["accounts"] = env.rt.data["codex"]["accounts"][:1]
    r = await env.client.get("/partials/runtime-accounts")
    assert r.status_code == 200 and 'hx-trigger="every 3s"' not in r.text


@pytest.mark.parametrize("breakage", ["missing", "garbage"])
async def test_accounts_page_survives_old_daemon_or_bad_data(env, breakage):
    if breakage == "missing":
        for n in RT_RPC:
            env.d.methods.pop(n, None)
    else:
        env.d.methods["runtime_account.list"] = lambda p: {"codex": {"accounts": "oops"}, "grok": 5}
    r = await env.client.get("/settings/accounts")
    assert r.status_code == 200 and 'id="accounts"' in r.text  # Tài khoản Claude vẫn dùng được
    assert ("Daemon chưa hỗ trợ tài khoản ChatGPT/Grok" in r.text) or ("Không hiển thị được tài khoản ChatGPT/Grok" in r.text)


async def test_use_logout_rm_need_confirm(env):
    for act, rpcn in (("rtacct_use", "runtime_account.use"), ("rtacct_logout", "runtime_account.logout"), ("rtacct_rm", "runtime_account.rm")):
        r = await env.post(f"/do/{act}", {"kind": "codex", "id": "a_12345678", "name": "Cá nhân", "next": "/settings/accounts"})
        assert r.status_code == 200 and "«Cá nhân»" in r.text and not called(env, rpcn)  # trang xác nhận phía server
        r = await env.post(f"/do/{act}", {"kind": "codex", "id": "a_12345678", "name": "Cá nhân", "confirm": "yes", "next": "/settings/accounts"})
        assert r.status_code in (302, 303) and called(env, rpcn)[-1] == {"kind": "codex", "id": "a_12345678"}


async def test_add_account_requires_label_and_passes_via(env):
    r = await env.post("/do/rtacct_login", {"kind": "codex", "new": "1", "label": " ", "via": "device", "next": "/settings/accounts"},
                       follow_redirects=True)
    assert not called(env, "runtime_account.login_start") and "đặt tên cho tài khoản" in r.text
    await env.post("/do/rtacct_login", {"kind": "codex", "new": "1", "label": "Công ty", "via": "browser", "next": "/settings/accounts"})
    assert called(env, "runtime_account.login_start")[-1] == {"kind": "codex", "method": "browser", "label": "Công ty"}
    await env.post("/do/rtacct_login", {"kind": "grok", "id": "machine", "via": "device", "next": "/settings/accounts"})
    assert called(env, "runtime_account.login_start")[-1] == {"kind": "grok", "method": "device", "id": "machine"}
    r = await env.post("/do/rtacct_login", {"kind": "gemini", "id": "machine", "next": "/settings/accounts"}, follow_redirects=True)
    assert len(called(env, "runtime_account.login_start")) == 2


async def test_manager_setup_offers_all_runtimes(env):
    env.hub.agents.create({"id": "old", "name": "Old agent"})
    env.hub.agents.create({"id": "gpt", "name": "GPT", "runtime": "codex"})
    r = await env.client.get("/manager")
    t = r.text
    if 'action="/do/manager_create"' not in t:
        pytest.skip("hub thử đã có điều phối viên")
    assert "Old agent (old) — Claude" in t and "GPT (gpt) — ChatGPT (Codex)" in t  # agent cũ vẫn chỉ định được
    for v, lab in (("claude", "Claude"), ("codex", "ChatGPT (Codex)"), ("grok", "Grok")):
        assert f'<option value="{v}">{lab}</option>' in t


def test_manager_create_params():
    from cfharness.web.manager import _mgr_create_params

    p = _mgr_create_params({"runtime": "grok", "agent_id": "boss", "name": "  Sếp   Grok "})
    assert p == {"id": "boss", "template": "manager", "overrides": {"name": "Sếp Grok", "runtime": "grok"}}
    with pytest.raises(ValueError):
        _mgr_create_params({"runtime": "gemini", "agent_id": "x"})
    with pytest.raises(ValueError):
        _mgr_create_params({"runtime": "codex", "agent_id": " "})


# ------------------------------------------------------------------ khoá BẢO VỆ chỉnh trên UI (máy chạy hub + xác nhận 2 bước)
async def test_protected_key_flow(env, monkeypatch):
    key, form = "manager.capabilities.profile", {"key": "manager.capabilities.profile", "value": "hermes", "next": "/settings/security"}
    assert env.hub.config.get(key) == "strict"
    # form thường không đổi được khoá bảo vệ
    r = await env.post("/do/config_set", {**form, "confirm": "yes"}, follow_redirects=True)
    assert env.hub.config.get(key) == "strict" and "khoá bảo vệ" in r.text
    # bước 1: xác nhận; bước 2: hệ quả "NỚI LỎNG"; chưa đổi gì
    r = await env.post("/do/config_set_protected", form)
    assert r.status_code == 200 and env.hub.config.get(key) == "strict"
    r = await env.post("/do/config_set_protected", {**form, "confirm": "yes"})
    assert r.status_code == 200 and "NỚI LỎNG" in r.text and env.hub.config.get(key) == "strict"
    # qua link public → từ chối dù đã xác nhận 2 lần
    monkeypatch.setattr("cfharness.web.file_pages.is_local_request", lambda request: False)
    r = await env.post("/do/config_set_protected", {**form, "confirm": "yes", "confirm2": "yes"}, follow_redirects=True)
    assert env.hub.config.get(key) == "strict" and "chính máy chạy hub" in r.text
    monkeypatch.undo()
    # trên máy chạy hub, đủ 2 bước → đổi (Windows: hermes cần bật "khung rộng khi không có sandbox" trước — cũng chỉnh trên UI)
    await env.post("/do/config_set_protected", {"key": "manager.allow_hermes_without_sandbox", "value": "true", "confirm": "yes",
                                                "confirm2": "yes", "next": "/settings/security"})
    await env.post("/do/config_set_protected", {**form, "confirm": "yes", "confirm2": "yes"})
    assert env.hub.config.get(key) == "hermes"
    rows = env.hub.store.conn.execute("SELECT count(*) FROM audit_log WHERE kind='config.set' AND channel='web'").fetchone()
    assert rows[0] >= 2  # mỗi lần đổi khoá bảo vệ trên web có một dòng audit kênh web
    # khoá runtime codex: giá trị nguy hiểm bị daemon từ chối
    r = await env.post("/do/config_set_protected", {"key": "runtime.codex.windows_sandbox", "value": "unelevated", "confirm": "yes",
                                                     "confirm2": "yes", "next": "/settings/accounts"}, follow_redirects=True)
    assert env.hub.config.get("runtime.codex.windows_sandbox") == "mxc"


async def test_security_page_editable_locally_readonly_publicly(env, monkeypatch):
    t = (await env.client.get("/settings/security")).text
    assert 'action="/do/config_set_protected"' in t and 'value="guard.enabled"' in t and "Bảo vệ &amp; quyền" in t
    t = (await env.client.get("/settings/accounts")).text
    assert 'value="runtime.codex.sandbox"' in t and 'id="rt-settings"' in t
    monkeypatch.setattr("cfharness.web.file_pages.is_local_request", lambda request: False)
    t = (await env.client.get("/settings/security")).text
    assert 'action="/do/config_set_protected"' not in t and "chỉ xem qua link public" in t


async def test_cli_path_change_is_local_only(env, monkeypatch):
    monkeypatch.setattr("cfharness.web.file_pages.is_local_request", lambda request: False)
    r = await env.post("/do/rtacct_config", {"kind": "codex", "binary": "C:/evil.exe", "model": "", "effort": "", "next": "/settings/accounts"},
                       follow_redirects=True)
    assert env.hub.config.get("runtime.codex.binary") == "codex" and "chính máy chạy hub" in r.text
    # model/effort (không đổi chương trình chạy) vẫn sửa được qua link public
    await env.post("/do/rtacct_config", {"kind": "codex", "binary": "codex", "model": "gpt-5.6-luna", "effort": "low", "next": "/settings/accounts"})
    assert env.hub.config.get("runtime.codex.model") == "gpt-5.6-luna" and env.hub.config.get("runtime.codex.effort") == "low"


async def test_nullable_bool_shows_profile_default_not_on(env):
    """Bool để trống (= theo hồ sơ phê duyệt) KHÔNG được hiện thành "Bật" (lưu nhầm sẽ bật thật) — lỗi thấy khi chạy thử."""
    import re

    t = (await env.client.get("/settings/security")).text
    i = t.index('name="key" value="guard.hold_uncertain"')
    sel = t[i:t.index("</select>", i)]
    assert re.search(r'<option value="" selected>\(theo hồ sơ phê duyệt\)</option>', sel)
    assert '<option value="true" selected>' not in sel
