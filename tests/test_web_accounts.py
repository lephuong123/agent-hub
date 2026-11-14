"""Khối "Tài khoản Claude" ở Cài đặt (web/account_pages.py, docs/dev-notes/plan-claude-accounts.md — luồng web).

Daemon IN-PROCESS + app Starlette qua ASGITransport như tests/test_web.py. RPC `account.*` do luồng lõi làm song song → ở đây thay bằng
handler GIẢ trong bộ nhớ (gắn vào `Daemon.methods`), ghi tên method + params. Không gọi claude thật.

Kiểm: danh sách hiện đủ (nhãn, Đang dùng, chip trạng thái, chi phí) và KHÔNG có token; Dùng/Đổi tên/Xoá/Kiểm tra gọi đúng RPC với
`_audit.channel=web`; Dùng/Xoá cần xác nhận; luồng đăng nhập 2 bước (login_start → link https + ô code → login_finish), huỷ; dán token
(add_token); token/code không bao giờ nằm trong response (kể cả trang xác nhận, flash); banner "Đổi sang tài khoản…" khi `auth_block` /
cổng hạn mức đóng; daemon cũ chưa có `account.*` → khối hiện hướng dẫn; thẻ Sức khoẻ nhắc đổi tài khoản.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from cfharness.errors import Conflict, HubError, NotFound
from cfharness.runtime import FakeRuntime
from cfharness.web.app import ACTIONS  # nạp app TRƯỚC (app đăng ký thao tác của account_pages khi import)
from cfharness.web.account_pages import gate_closed, status_chip
from test_web import ISOLATED, _start, _stop  # noqa: F401 — dùng lại khung test web

TOKEN = "sk-ant-oat01-SECRETxTOKENxDOxNOTxLEAK-0123456789abcdef"
CODE = "CODExSECRETxABCxDOxNOTxLEAK"
LOGIN_URL = "https://claude.ai/oauth/authorize?code=true&client_id=abc&state=xyz"
ACCOUNT_RPC = ("account.list", "account.login_start", "account.login_finish", "account.login_cancel", "account.add_token",
               "account.use", "account.rename", "account.rm", "account.check")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class FakeAccounts:
    """Thay cho `accounts/rpc.py` của luồng lõi — giữ hợp đồng kết quả (Account không bao giờ có token)."""

    def __init__(self) -> None:
        now = datetime.now(timezone.utc)
        self.active = "a_11111111"
        self.rows: dict[str, dict] = {
            "a_11111111": self._acct("a_11111111", "Công ty", status="rate_limited", rate_limit={"until": _iso(now + timedelta(hours=2)), "utilization": 1.0},
                                     cost=1.25),
            "a_22222222": self._acct("a_22222222", "Cá nhân", status="ok", cost=0.5),
            "a_33333333": self._acct("a_33333333", "Hỏng", status="auth_error", last_error="token bị thu hồi"),
            "machine": self._acct("machine", "Đăng nhập của máy", kind="machine", status="unknown"),
        }
        self.tokens: dict[str, str] = {}
        self.logins: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def _acct(id_: str, label: str, *, kind: str = "token", status: str = "ok", rate_limit=None, last_error=None, cost: float = 0.0) -> dict:
        return {"id": id_, "label": label, "kind": kind, "active": False, "created_at": "2026-11-01T00:00:00+00:00",
                "expires_at": "2027-11-01T00:00:00+00:00", "last_used_at": "2026-11-13T01:00:00+00:00", "status": status,
                "last_error": last_error, "rate_limit": rate_limit, "cost_today_usd": cost}

    def view(self, id_: str) -> dict:
        return {**self.rows[id_], "active": id_ == self.active}

    def install(self, d) -> None:
        handlers = {"account.list": self.list, "account.login_start": self.login_start, "account.login_finish": self.login_finish,
                    "account.login_cancel": self.login_cancel, "account.add_token": self.add_token, "account.use": self.use,
                    "account.rename": self.rename, "account.rm": self.rm, "account.check": self.check}
        for name, fn in handlers.items():
            def wrapped(p, _fn=fn, _n=name):
                p = p or {}
                self.calls.append((_n, {k: v for k, v in p.items() if k not in ("token", "code") and (not k.startswith("_") or k == "_audit")}
                                   | {"has_token": bool(p.get("token")), "has_code": bool(p.get("code"))}))
                return _fn(p)
            d.methods[name] = wrapped

    def _get(self, id_: str) -> dict:
        if id_ not in self.rows:
            raise NotFound(f"không có tài khoản {id_}")
        return self.rows[id_]

    def list(self, p):
        return {"active": self.active, "accounts": [self.view(i) for i in self.rows]}

    def login_start(self, p):
        lid = f"L{len(self.logins) + 1}"
        self.logins[lid] = p["label"]
        return {"login_id": lid, "url": LOGIN_URL, "expires_at": _iso(datetime.now(timezone.utc) + timedelta(minutes=10))}

    def login_finish(self, p):
        label = self.logins.pop(p["login_id"], None)
        if label is None:
            raise NotFound("lượt đăng nhập đã hết hạn")
        if p.get("code") != CODE:
            raise HubError("code không đúng — đăng nhập lại")
        return self._new(label, TOKEN)

    def login_cancel(self, p):
        self.logins.pop(p.get("login_id"), None)
        return {"ok": True}

    def _new(self, label: str, token: str) -> dict:
        id_ = f"a_{len(self.rows):08x}"
        self.rows[id_] = self._acct(id_, label)
        self.tokens[id_] = token
        return {"account": self.view(id_)}

    def add_token(self, p):
        return self._new(p["label"], p["token"])

    def use(self, p):
        self._get(p["id"])
        prev, self.active = self.active, p["id"]
        return {"active": self.active, "previous": prev}

    def rename(self, p):
        self._get(p["id"])["label"] = p["label"]
        return {"account": self.view(p["id"])}

    def rm(self, p):
        self._get(p["id"])
        if p["id"] == self.active:
            raise Conflict("không xoá được tài khoản đang dùng — đổi sang tài khoản khác trước")
        del self.rows[p["id"]]
        return {"ok": True}

    def check(self, p):
        a = self._get(p["id"])
        a["status"] = "ok"
        return {"account": self.view(p["id"])}


@pytest.fixture
async def env(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    e.acc = FakeAccounts()
    e.acc.install(e.d)
    e.bodies = []
    e.status_extra = {}
    orig = e.d.methods["hub.status"]

    def status(p, _o=orig):
        return {**_o(p), **e.status_extra}

    e.d.methods["hub.status"] = status
    await e.login()
    yield e
    await _stop(e)


async def get(env, path: str):
    r = await env.client.get(path)
    env.bodies.append(r.text + json.dumps(dict(r.headers)))
    return r


async def post(env, path: str, data: dict, **kw):
    r = await env.post(path, data, **kw)
    env.bodies.append(r.text + json.dumps(dict(r.headers)))
    return r


def names(env) -> list[str]:
    return [n for n, _ in env.acc.calls if n != "account.list"]


def last(env, name: str) -> dict:
    return [p for n, p in env.acc.calls if n == name][-1]


def no_leak(env) -> None:
    for b in env.bodies:
        assert TOKEN not in b and CODE not in b


def section(html: str) -> str:
    m = re.search(r'<section class="card[^"]*" id="accounts">.*?</section>', html, re.S)
    assert m, "thiếu khối Tài khoản Claude"
    return m.group(0)


# =========================================================================== hàm thuần
def test_status_chip_and_gate():
    assert status_chip({"status": "ok"}) == ("ok", "ok")
    assert status_chip({"status": "auth_error"}) == ("lỗi đăng nhập", "danger")
    lbl, tone = status_chip({"status": "rate_limited", "rate_limit": {"until": _iso(datetime.now(timezone.utc) + timedelta(hours=1))}})
    assert tone == "warn" and re.fullmatch(r"hết hạn mức tới (\d\d/\d\d )?\d\d:\d\d", lbl)
    assert status_chip({"status": "rate_limited", "rate_limit": None}) == ("hết hạn mức", "warn")
    assert status_chip({"status": "expiring", "expires_at": "2026-11-20T00:00:00+00:00"})[0].startswith("token sắp hết hạn (")
    assert status_chip({"status": "unknown"})[1] == "muted"
    now = datetime.now(timezone.utc)
    assert gate_closed({"rate_limit_gate": {"until": _iso(now + timedelta(minutes=5))}})
    assert gate_closed({"rate_limit_gate": {"until": _iso(now - timedelta(minutes=5))}}) is None
    assert gate_closed({"rate_limit_gate": None}) is None and gate_closed(None) is None


# =========================================================================== danh sách
async def test_list_renders_without_token(env):
    env.acc.tokens["a_11111111"] = TOKEN
    r = await get(env, "/settings")
    assert r.status_code == 200
    s = section(r.text)
    assert "Tài khoản Claude" in s and "Công ty" in s and "Cá nhân" in s and "Đăng nhập của máy" in s
    assert "đăng nhập sẵn của claude trên máy chạy hub" in s  # dòng phụ của machine không lặp lại tên
    row = re.search(r'<tr id="acct-a_11111111">.*?</tr>', s, re.S).group(0)
    assert "Đang dùng" in row and "hết hạn mức tới" in row and "1.25" in row
    assert 'action="/do/account_use"' not in row and 'action="/do/account_rm"' not in row  # đang dùng: không Dùng/Xoá
    other = re.search(r'<tr id="acct-a_22222222">.*?</tr>', s, re.S).group(0)
    assert "Đang dùng" not in other and 'pill ok">ok' in other
    for a in ("account_use", "account_rm", "account_check", "account_rename"):
        assert f'action="/do/{a}"' in other
    bad = re.search(r'<tr id="acct-a_33333333">.*?</tr>', s, re.S).group(0)
    assert "lỗi đăng nhập" in bad and "token bị thu hồi" in bad
    mach = re.search(r'<tr id="acct-machine">.*?</tr>', s, re.S).group(0)
    assert 'action="/do/account_rm"' not in mach and 'action="/do/account_use"' in mach
    assert 'action="/do/account_rename"' not in mach  # core không đổi tên được machine
    # form thêm: 2 tab, ô token password không value
    assert 'action="/do/account_login_start"' in s and 'action="/do/account_add_token"' in s and "Dán token" in s
    tag = re.search(r'<input name="token"[^>]*>', s).group(0)
    assert 'type="password"' in tag and "value=" not in tag and 'autocomplete="off"' in tag
    # không banner khi không bị chặn
    assert 'id="acct-switch"' not in s
    no_leak(env)


async def test_old_daemon_without_account_rpc(env):
    for n in ACCOUNT_RPC:
        env.d.methods.pop(n, None)
    r = await get(env, "/settings")
    assert r.status_code == 200
    assert "Daemon chưa có quản lý tài khoản" in section(r.text)


# =========================================================================== thao tác từng dòng
async def test_use_needs_confirm_then_calls_rpc(env):
    r = await post(env, "/do/account_use", {"id": "a_22222222", "name": "Cá nhân", "next": "/settings#accounts"})
    assert r.status_code == 200 and "Chuyển sang tài khoản Claude «Cá nhân»" in r.text  # trang xác nhận phía server
    assert "account.use" not in names(env)
    r = await post(env, "/do/account_use", {"id": "a_22222222", "name": "Cá nhân", "confirm": "yes", "next": "/settings#accounts"})
    assert r.status_code == 303 and r.headers["location"].startswith("/settings")
    p = last(env, "account.use")
    assert p["id"] == "a_22222222" and p["_audit"]["channel"] == "web"
    assert env.acc.active == "a_22222222"
    page = (await get(env, "/settings")).text
    assert "Đã chuyển sang tài khoản Cá nhân" in page
    assert "Đang dùng" in re.search(r'<tr id="acct-a_22222222">.*?</tr>', page, re.S).group(0)


async def test_rename_rm_check(env):
    r = await post(env, "/do/account_rename", {"id": "a_22222222", "label": "  Nhà   riêng ", "next": "/settings#accounts"})
    assert r.status_code == 303
    p = last(env, "account.rename")
    assert p == {**p, "id": "a_22222222", "label": "Nhà riêng"} and p["_audit"]["channel"] == "web"
    # tên rỗng → không gọi RPC
    n = len(names(env))
    await post(env, "/do/account_rename", {"id": "a_22222222", "label": "  ", "next": "/settings#accounts"})
    assert len(names(env)) == n
    # kiểm tra
    r = await post(env, "/do/account_check", {"id": "a_33333333", "name": "Hỏng", "next": "/settings#accounts"})
    assert r.status_code == 303 and last(env, "account.check")["id"] == "a_33333333"
    assert "Kiểm tra Hỏng: ok" in (await get(env, "/settings")).text
    # xoá: cần xác nhận, rồi gọi RPC
    r = await post(env, "/do/account_rm", {"id": "a_33333333", "name": "Hỏng", "next": "/settings#accounts"})
    assert r.status_code == 200 and "Xoá tài khoản Claude «Hỏng»" in r.text and "account.rm" not in names(env)
    r = await post(env, "/do/account_rm", {"id": "a_33333333", "name": "Hỏng", "confirm": "yes", "next": "/settings#accounts"})
    assert r.status_code == 303 and last(env, "account.rm")["id"] == "a_33333333" and "a_33333333" not in env.acc.rows
    # xoá tài khoản đang dùng → Conflict hiện thành flash lỗi
    await post(env, "/do/account_rm", {"id": "a_11111111", "name": "Công ty", "confirm": "yes", "next": "/settings#accounts"})
    page = (await get(env, "/settings")).text
    assert "flash error" in page and "không xoá được tài khoản đang dùng" in page


async def test_post_without_csrf_rejected(env):
    r = await env.client.post("/do/account_use", data={"id": "a_22222222", "confirm": "yes"})
    assert r.status_code == 403 and "account.use" not in names(env)


# =========================================================================== thêm tài khoản
async def test_login_start_finish_flow(env):
    r = await post(env, "/do/account_login_start", {"label": "Dự phòng", "next": "/settings#accounts"})
    assert r.status_code == 303
    assert last(env, "account.login_start")["label"] == "Dự phòng" and last(env, "account.login_start")["_audit"]["channel"] == "web"
    s = section((await get(env, "/settings")).text)
    link = re.search(r'<a class="btn primary" href="([^"]+)" target="_blank" rel="noopener noreferrer">', s)
    assert link and link.group(1).replace("&amp;", "&") == LOGIN_URL
    assert "Bước 2/2 — Dự phòng" in s and 'action="/do/account_login_finish"' in s
    assert 'name="login_id" value="L1"' in s
    tag = re.search(r'<input name="code"[^>]*>', s).group(0)
    assert 'type="password"' in tag and "value=" not in tag
    # code sai → flash lỗi, code không lộ
    await post(env, "/do/account_login_finish", {"login_id": "L1", "code": "WRONG", "next": "/settings#accounts"})
    env.acc.logins["L1"] = "Dự phòng"  # fake tiêu lượt khi sai; mở lại để thử tiếp
    # code đúng
    r = await post(env, "/do/account_login_finish", {"login_id": "L1", "code": CODE, "next": "/settings#accounts"})
    assert r.status_code == 303
    p = last(env, "account.login_finish")
    assert p["login_id"] == "L1" and p["has_code"] and "code" not in p and p["_audit"]["channel"] == "web"
    page = (await get(env, "/settings")).text
    assert "Đã thêm tài khoản Dự phòng" in page
    s = section(page)
    assert "Bước 2/2" not in s and 'action="/do/account_login_start"' in s  # lượt đăng nhập đã xong → về bước 1
    assert "Dự phòng" in s
    no_leak(env)


async def test_login_cancel_and_bad_url(env):
    await post(env, "/do/account_login_start", {"label": "X", "next": "/settings#accounts"})
    assert "Bước 2/2" in section((await get(env, "/settings")).text)
    r = await post(env, "/do/account_login_cancel", {"login_id": "L1", "next": "/settings#accounts"})
    assert r.status_code == 303 and last(env, "account.login_cancel")["login_id"] == "L1"
    assert "Bước 2/2" not in section((await get(env, "/settings")).text)
    # daemon trả link không phải https → không đưa vào trang, tự huỷ lượt
    orig = env.acc.login_start
    env.acc.login_start = lambda p: {**orig(p), "url": "javascript:alert(1)"}
    env.acc.install(env.d)
    await post(env, "/do/account_login_start", {"label": "Y", "next": "/settings#accounts"})
    page = (await get(env, "/settings")).text
    assert "javascript:alert" not in page and "Bước 2/2" not in section(page) and "flash error" in page
    assert names(env)[-1] == "account.login_cancel"


async def test_add_token(env):
    r = await post(env, "/do/account_add_token", {"label": "Token tay", "token": TOKEN, "next": "/settings#accounts"})
    assert r.status_code == 303
    p = last(env, "account.add_token")
    assert p["label"] == "Token tay" and p["has_token"] and p["_audit"]["channel"] == "web"
    assert TOKEN in env.acc.tokens.values()
    page = (await get(env, "/settings")).text
    assert "Đã thêm tài khoản Token tay" in page
    # token có khoảng trắng → không gọi RPC, lỗi không chứa token
    n = len(names(env))
    await post(env, "/do/account_add_token", {"label": "Z", "token": TOKEN + " x", "next": "/settings#accounts"})
    assert len(names(env)) == n
    assert "token không được chứa khoảng trắng" in (await get(env, "/settings")).text
    no_leak(env)


async def test_secret_never_on_confirm_page(env):
    """Nếu ai đó gắn `confirm` cho add_token/login_finish, trang xác nhận vẫn bỏ token/code (secret_fields)."""


    for name, field, val in (("account_add_token", "token", TOKEN), ("account_login_finish", "code", CODE)):
        act = ACTIONS[name]
        assert field in act.secret_fields
        old, act.confirm = act.confirm, "Xác nhận?"
        try:
            r = await post(env, f"/do/{name}", {"label": "A", "login_id": "L9", field: val, "next": "/settings"})
            assert r.status_code == 200 and val not in r.text
        finally:
            act.confirm = old
    no_leak(env)


# =========================================================================== banner đổi tài khoản
async def test_banner_on_auth_block(env):
    env.status_extra = {"auth_block": {"reason": "chưa đăng nhập"}}
    s = section((await get(env, "/settings")).text)
    assert 'id="acct-switch"' in s and "lỗi đăng nhập" in s and "Đổi sang tài khoản…" in s
    form = re.search(r'<form method="post" action="/do/account_use"[^>]*>.*?</form>', s.split('id="acct-switch"', 1)[1], re.S).group(0)
    opts = re.findall(r'<option value="([^"]+)"', form)
    assert opts == ["a_22222222", "machine"]  # không gồm tài khoản đang dùng / lỗi đăng nhập
    r = await post(env, "/do/account_use", {"id": "machine", "confirm": "yes", "next": "/settings#accounts"})
    assert r.status_code == 303 and env.acc.active == "machine"


async def test_banner_on_closed_gate(env):
    now = datetime.now(timezone.utc)
    env.status_extra = {"rate_limit_gate": {"until": _iso(now + timedelta(hours=1)), "source": "rejected"}}
    s = section((await get(env, "/settings")).text)
    assert 'id="acct-switch"' in s and "hết hạn mức tới" in s and 'action="/do/account_use"' in s
    # cổng đã mở (until đã qua) → không banner
    env.status_extra = {"rate_limit_gate": {"until": _iso(now - timedelta(minutes=1))}}
    assert 'id="acct-switch"' not in section((await get(env, "/settings")).text)
    # không còn tài khoản khác dùng được → gợi ý thêm tài khoản
    env.status_extra = {"rate_limit_gate": {"until": _iso(now + timedelta(hours=1))}}
    for i in ("a_22222222", "machine"):
        env.acc.rows.pop(i)
    s = section((await get(env, "/settings")).text)
    assert 'id="acct-switch"' in s and "Chưa có tài khoản khác" in s


async def test_health_mentions_switch(env):
    env.status_extra = {"auth_block": {"reason": "x"}, "rate_limit": {"until": "2026-11-13T10:00:00+00:00"}, "account": {"id": "a_11111111", "label": "Công ty"}}
    h = (await get(env, "/partials/health")).text
    assert h.count("đổi sang tài khoản khác ở mục Tài khoản Claude") == 2
    assert "đang dùng Công ty" in h
