"""Wizard "Kết nối Telegram" trên web (web/telegram_pages.py, docs/dev-notes/plan-chat-setup-web.md PR1 + PR6) — daemon IN-PROCESS + Bot API
GIẢ cục bộ (tests/telegram_fake.py), app Starlette qua ASGITransport như tests/test_web.py. Không gọi Telegram/claude thật.

Kiểm: trang hiện đúng từng bước (chưa token / ghép nối / xong / đã tắt); chưa đăng nhập bị chặn; POST thiếu CSRF/khoá phiên → 403; token sai
dạng → không gọi RPC, lỗi không lặp lại giá trị; token bị Telegram từ chối → lỗi, không lưu; dán token lần đầu KHÔNG có trang xác nhận, đã
có token thì phải tích ô ghi đè; deep link + QR + `/start <mã>`; người nhắn mã hiện ở danh sách chờ (htmx poll, tên `<script>` bị escape) →
"Đây là tôi — cho phép" gọi thẳng `telegram.allow` (Q2) → allowlist + tin chào; chat không ở danh sách chờ bị từ chối; thêm bằng id vẫn có
xác nhận; gửi thử/gỡ/tắt; audit kênh `web`; token không bao giờ có trong response/audit/log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import pytest
import yaml

from cfharness.runtime import FakeRuntime
from cfharness.web import app as _app  # noqa: F401 — nạp app trước (telegram_pages đăng ký vào app lúc app nạp xong)
from cfharness.web import telegram_pages as T
from test_web import ISOLATED, _start, _stop, audit_rows, until  # noqa: F401 — dùng lại khung test web

from telegram_fake import TOKEN, FakeTelegram

BOSS = 555001
PAGE = "/settings/notifications/telegram"
TG_RPC = ("telegram.setup", "telegram.pending", "telegram.allow", "telegram.revoke", "telegram.disable", "telegram.test")
BAD_TOKEN = "987654321:ZZZ-notTheRightToken_abcdefghijklmn"


@pytest.fixture
def tg():
    f = FakeTelegram()
    yield f
    f.close()


@pytest.fixture
async def env(hub_home, tg):
    hub_home.config.write_text(yaml.safe_dump({"guard": {"git_probe": False},
                                               "notify": {"telegram": {"api_base": tg.url, "poll_timeout_s": 1, "idle_check_s": 0.05,
                                                                       "backoff_base_s": 0.05}}}))
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    tg.hubs.append(e.hub)
    e.calls = spy(e)
    e.bodies = []
    yield e
    await _stop(e)


def spy(env) -> list[tuple[str, dict]]:
    """Bọc handler `telegram.*` của daemon: ghi tên method + params (KHÔNG giữ token) rồi chạy thật."""
    calls: list[tuple[str, dict]] = []
    for name in TG_RPC:
        fn = env.d.methods[name]

        def wrapped(p, _fn=fn, _n=name):
            calls.append((_n, {k: v for k, v in (p or {}).items() if k != "token" and (not k.startswith("_") or k == "_audit")}
                          | {"has_token": bool((p or {}).get("token"))}))
            return _fn(p)

        env.d.methods[name] = wrapped
    return calls


async def get(env, path: str):
    r = await env.client.get(path)
    env.bodies.append(r.text)
    return r


async def post(env, path: str, data: dict, **kw):
    r = await env.post(path, data, **kw)
    env.bodies.append(r.text + json.dumps(dict(r.headers)))
    return r


def names(env) -> list[str]:
    return [n for n, _ in env.calls]


def start_msg(tg: FakeTelegram, chat_id: int, text: str, *, first: str, username: str, chat_type: str = "private", from_id: int | None = None) -> int:
    m = {"message_id": 10_000 + tg.next_update, "date": int(time.time()), "text": text,
         "chat": {"id": chat_id, "type": chat_type, "username": username, "first_name": first},
         "from": {"id": from_id or chat_id, "is_bot": False, "username": username, "first_name": first}}
    return tg._push({"message": m})


# =========================================================================== hàm thuần
def test_deep_link_qr_and_person_are_strict():
    assert T.deep_link("hub_test_bot", "0A1B2C3D") == "https://t.me/hub_test_bot?start=0A1B2C3D"
    for u, c in (("hub bot", "0A1B2C3D"), ("x\"><script>", "0A1B2C3D"), ("hub_test_bot", "0a1b2c3d"), ("hub_test_bot", "ZZ"), (None, None)):
        assert T.deep_link(u, c) is None, (u, c)
    svg = str(T.qr_svg("https://t.me/hub_test_bot?start=0A1B2C3D"))
    assert svg.startswith("<svg ") and "<path" in svg and "script" not in svg.lower() and "style" not in svg.lower() and 'role="img"' in svg
    assert T.qr_svg("https://evil.example/?start=0A1B2C3D") is None and T.qr_svg("javascript:alert(1)") is None and T.qr_svg(None) is None
    p = T.person({"chat_id": "7", "user_id": "7; rm", "type": "private", "name": "<script>alert(1)</script>\n" + "x" * 100, "username": "a‮b"})
    assert "\n" not in p["name"] and len(p["name"]) <= T.NAME_MAX and p["user_id"] == "" and "‮" not in p["username"] and p["group"] is False
    assert T.person({"chat_id": "-1007", "type": "group"})["group"] is True
    h = T.bot_name_hint("My-Mac.local")
    assert h == "cfh_my_mac_bot" and re.fullmatch(r"[a-z0-9_]{5,32}", h)
    assert T.bot_name_hint("").startswith("cfh_") and T.bot_name_hint("").endswith("_bot")


# =========================================================================== trang
async def test_page_requires_login_and_fresh_state(env):
    r = await env.client.get(PAGE)
    assert r.status_code in (302, 303) and "/login" in r.headers.get("location", "")
    assert (await env.client.get("/partials/telegram/pending")).status_code == 401
    await env.login()
    html = (await get(env, PAGE)).text
    assert "Kết nối Telegram" in html and 'href="https://t.me/BotFather"' in html and "<pre>/newbot</pre>" in html
    assert 'action="/do/tg_setup"' in html and 'name="overwrite"' not in html  # chưa có token → không ô ghi đè, không trang xác nhận
    tag = re.search(r'<input name="token"[^>]*>', html).group(0)
    assert 'type="password"' in tag and 'autocomplete="off"' in tag and "pattern=" in tag and "required" in tag
    # trang Thông báo & kênh: trạng thái từ telegram.status + link wizard; không còn bắt chạy CLI
    note = (await get(env, "/settings/notifications")).text
    assert 'href="/settings/notifications/telegram"' in note and "notify telegram setup" not in note
    # token đã lưu nhưng bot không chạy được → KHÔNG hiện "đang hoạt động" chỉ vì secret tồn tại
    env.hub.secrets.set("TELEGRAM_BOT_TOKEN", BAD_TOKEN)
    assert await until(lambda: env.hub.telegram.info.get("last_error"))
    note = (await get(env, "/settings/notifications")).text
    row = re.search(r"<dt>Telegram</dt>.*?</dd>", note, re.S).group(0)
    assert "lỗi kết nối" in row and "đang hoạt động" not in row
    assert BAD_TOKEN not in note


async def test_post_rejected_without_csrf_or_session_key(env):
    await env.login()
    tok = {"token": TOKEN, "confirm": "yes", "next": PAGE}
    assert (await env.client.post("/do/tg_setup", data={**tok, "k": env.key})).status_code == 403  # thiếu CSRF
    assert (await env.client.post("/do/tg_setup", data={**tok, "csrf": "sai", "k": env.key})).status_code == 403
    assert (await env.client.post("/do/tg_setup", data={**tok, "csrf": env.csrf})).status_code == 403  # thiếu khoá phiên (WT-1)
    for path in ("/do/tg_allow", "/do/tg_allow_confirm", "/do/tg_revoke", "/do/tg_test", "/do/tg_disable", "/do/tg_pair"):
        r = await env.client.post(path, data={"chat_id": str(BOSS), "confirm": "yes", "k": env.key})
        assert r.status_code == 403, path
    env.client.cookies.clear()  # chưa đăng nhập
    r = await env.client.post("/do/tg_setup", data={**tok, "csrf": env.csrf, "k": env.key})
    assert r.status_code in (302, 303, 401, 403)
    assert env.calls == [] and env.hub.secrets.get("TELEGRAM_BOT_TOKEN") is None


async def test_bad_token_not_echoed_and_not_saved(env, caplog):
    caplog.set_level(logging.DEBUG)
    await env.login()
    r = await post(env, "/do/tg_setup", {"token": "not-a-token-SECRETPART", "confirm": "yes", "next": PAGE})
    assert r.status_code == 303 and env.calls == []
    page = (await get(env, PAGE)).text
    assert "token phải có dạng" in page and "SECRETPART" not in page
    # đúng dạng nhưng Telegram từ chối (401) → lỗi, không lưu, không lặp lại token
    r = await post(env, "/do/tg_setup", {"token": BAD_TOKEN, "confirm": "yes", "next": PAGE})
    assert r.status_code == 303 and names(env) == ["telegram.setup"]
    page = (await get(env, PAGE)).text
    assert "Telegram từ chối token" in page
    assert env.hub.secrets.get("TELEGRAM_BOT_TOKEN") is None
    blob = "\n".join(env.bodies)
    audit = json.dumps([dict(x) for x in env.hub.store.conn.execute("SELECT * FROM audit_log").fetchall()], default=str)
    for t in (BAD_TOKEN, BAD_TOKEN.split(":", 1)[1]):
        assert t not in blob and t not in audit and t not in caplog.text


async def test_setup_pair_allow_test_revoke_disable(env, tg, caplog):
    caplog.set_level(logging.DEBUG)
    await env.login()
    # lần đầu: KHÔNG có trang xác nhận trung gian → thẳng telegram.setup
    r = await post(env, "/do/tg_setup", {"token": TOKEN, "confirm": "yes", "next": PAGE})
    assert r.status_code == 303 and r.headers["location"] == PAGE, r.text
    assert names(env) == ["telegram.setup"] and env.calls[0][1]["has_token"] and env.calls[0][1]["_audit"]["channel"] == "web"
    assert env.hub.secrets.get("TELEGRAM_BOT_TOKEN") == TOKEN
    assert [(a["channel"], a["actor"]) for a in audit_rows(env, "telegram.setup")] == [("web", "user")]
    code = env.hub.telegram.pairing["code"]
    html = (await get(env, PAGE)).text
    assert "Đã kết nối bot @hub_test_bot" in html
    link = f"https://t.me/hub_test_bot?start={code}"
    assert f'href="{link}"' in html and f"/start {code}" in html and "Mở Telegram và bấm Start" in html
    assert 'hx-get="/partials/telegram/pending"' in html and 'hx-trigger="every 3s"' in html
    assert '<div class="totp-qr tg-qr"><svg ' in html and "chia sẻ màn hình" in html  # PR6: QR SVG dựng ở server
    assert 'name="overwrite"' in html  # đã có token → đổi token phải tích ô ghi đè
    # đã có token: không tích ô ghi đè → daemon KHÔNG nhận token mới
    n = len(env.calls)
    r = await post(env, "/do/tg_setup", {"token": TOKEN, "confirm": "yes", "next": PAGE})
    assert r.status_code == 303 and len(env.calls) == n
    assert "Ghi đè token cũ" in (await get(env, PAGE)).text
    # người nhắn mã (tên có <script>) → hiện ở danh sách chờ, đã escape
    assert await until(lambda: env.hub.telegram.info.get("polling"))
    uid = start_msg(tg, BOSS, f"/start {code}", first="<script>alert(1)</script>", username="boss")
    assert await until(lambda: tg.processed(uid) and env.hub.telegram.pending(None))
    part = (await get(env, "/partials/telegram/pending")).text
    assert "<script>alert(1)</script>" not in part and "&lt;script&gt;alert(1)&lt;/script&gt;" in part
    assert str(BOSS) in part and 'action="/do/tg_allow"' in part and "Đây là tôi — cho phép" in part and "every 3s" in part
    assert "telegram.pending" in names(env) and audit_rows(env, "telegram.pending") == []  # poll là đọc — không ghi audit
    # chat KHÔNG ở danh sách chờ → không cho phép (không gọi telegram.allow)
    r = await post(env, "/do/tg_allow", {"chat_id": "777", "next": PAGE})
    assert r.status_code == 303 and "telegram.allow" not in names(env)
    assert "777" not in (env.hub.config.get("notify.telegram.allowed_chat_ids") or [])
    # nhóm qua đường không xác nhận → chặn trước RPC
    await post(env, "/do/tg_allow", {"chat_id": "-100123", "next": PAGE})
    assert "telegram.allow" not in names(env)
    # Q2: "Đây là tôi — cho phép" → telegram.allow ngay (không trang trung gian) → allowlist + tin chào
    before = len(tg.sent(BOSS))
    r = await post(env, "/do/tg_allow", {"chat_id": str(BOSS), "name": "<script>", "next": PAGE})
    assert r.status_code == 303 and r.headers["location"] == PAGE
    assert ("telegram.allow", {"chat_id": str(BOSS), "has_token": False, "_audit": {"channel": "web", "actor": "user"}}) in env.calls
    assert str(BOSS) in env.hub.config.get("notify.telegram.allowed_chat_ids") and str(BOSS) in env.hub.config.get("notify.telegram.allowed_user_ids")
    assert env.hub.telegram.pairing is None
    assert await until(lambda: len(tg.sent(BOSS)) > before) and "Đã kết nối cfharness" in tg.sent(BOSS)[-1]["text"]
    assert audit_rows(env, "telegram.allow")[-1]["channel"] == "web"
    html = (await get(env, PAGE)).text
    assert "Đã kết nối" in html and 'action="/do/tg_test"' in html and 'action="/do/tg_revoke"' in html and f"/start {code}" not in html
    assert "Đã cho phép &lt;script&gt; (555001)" in html  # flash mang tên người nhắn — autoescape
    # gửi thử → telegram.test → tin tới chat
    before = len(tg.sent(BOSS))
    r = await post(env, "/do/tg_test", {"next": PAGE})
    assert r.status_code == 303 and names(env)[-1] == "telegram.test"
    assert await until(lambda: len(tg.sent(BOSS)) > before)
    assert "Đã gửi tin thử tới 1 chat" in (await get(env, PAGE)).text
    # thêm bằng id thủ công → luôn có trang xác nhận
    n = len(env.calls)
    r = await post(env, "/do/tg_allow_confirm", {"chat_id": "424242", "next": PAGE})
    assert r.status_code == 200 and "điều khiển cfharness" in r.text and len(env.calls) == n
    # tạo mã mới (không dán lại token)
    r = await post(env, "/do/tg_pair", {"next": PAGE})
    assert r.status_code == 303 and env.calls[-1][0] == "telegram.setup" and not env.calls[-1][1]["has_token"]
    assert f"/start {env.hub.telegram.pairing['code']}" in (await get(env, PAGE)).text
    # gỡ chat → telegram.revoke (có xác nhận)
    r = await post(env, "/do/tg_revoke", {"chat_id": str(BOSS), "next": PAGE})
    assert r.status_code == 200 and names(env)[-1] != "telegram.revoke"
    r = await post(env, "/do/tg_revoke", {"chat_id": str(BOSS), "confirm": "yes", "next": PAGE})
    assert r.status_code == 303 and names(env)[-1] == "telegram.revoke"
    assert str(BOSS) not in env.hub.config.get("notify.telegram.allowed_chat_ids")
    # tắt (giữ token) → trang "đã tắt" + bật lại không cần token
    r = await post(env, "/do/tg_disable", {"confirm": "yes", "next": PAGE})
    assert r.status_code == 303 and env.calls[-1] == ("telegram.disable", {"forget_token": False, "has_token": False, "_audit": {"channel": "web", "actor": "user"}})
    assert env.hub.config.get("notify.telegram.enabled") is False and env.hub.secrets.get("TELEGRAM_BOT_TOKEN") == TOKEN
    html = (await get(env, PAGE)).text
    assert "đã tắt" in html and "Bật lại (dùng token đã lưu)" in html
    # tắt + xoá token → về bước đầu
    r = await post(env, "/do/tg_disable", {"forget": "1", "confirm": "yes", "next": PAGE})
    assert r.status_code == 303 and env.calls[-1][1]["forget_token"] is True
    assert env.hub.secrets.get("TELEGRAM_BOT_TOKEN") is None
    assert 'name="overwrite"' not in (await get(env, PAGE)).text
    assert audit_rows(env, "telegram.disable")[-1]["channel"] == "web"
    # token không bao giờ có trong response (thân + header), audit, log, phiên web
    await asyncio.sleep(0.05)
    blob = "\n".join(env.bodies)
    audit = json.dumps([dict(x) for x in env.hub.store.conn.execute("SELECT * FROM audit_log").fetchall()], default=str)
    secret_part = TOKEN.split(":", 1)[1]
    assert secret_part not in blob and secret_part not in audit and secret_part not in caplog.text
    for s in env.auth.sessions.values():
        assert secret_part not in json.dumps(s.extra, default=str)
