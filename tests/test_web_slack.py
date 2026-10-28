"""Wizard "Slack 2 chiều" ở trang Cài đặt (P5, web/slack_pages.py) — daemon IN-PROCESS + Slack GIẢ cục bộ (tests/slack_fake.py),
app Starlette qua ASGITransport như tests/test_web.py. Không gọi Slack/claude thật.

Kiểm: khối 2 chiều hiện + manifest đúng (Socket Mode, interactivity, tab Messages, message.im, lệnh slash theo cấu hình, scope);
POST thiếu CSRF/khoá phiên → 403; token sai dạng → không gọi RPC; dán token → `slack.setup` → mã ghép nối → người nhắn mã hiện ở
danh sách chờ (htmx poll) → xác nhận = `slack.allow`; gỡ/gửi thử/tắt gọi đúng RPC; audit kênh `web`; token không bao giờ có trong
response/audit/log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from cfharness.runtime import FakeRuntime
from cfharness.web.app import SLACK_CREATE_URL, slack_2way_manifest, slack_slash
from test_web import ISOLATED, _start, _stop, audit_rows, until  # noqa: F401 — dùng lại khung test web

from slack_fake import APP_TOKEN, BOT_TOKEN, TEAM, FakeSlack

BOSS, DM_BOSS = "U0BOSS001", "D0BOSS001"
SLACK_RPC = ("slack.setup", "slack.pending", "slack.allow", "slack.revoke", "slack.disable", "slack.test")


@pytest.fixture
def sk():
    f = FakeSlack()
    yield f
    f.close()


@pytest.fixture
async def env(hub_home, sk):
    hub_home.config.write_text(yaml.safe_dump({"notify": {"slack": {"api_base": sk.url, "idle_check_s": 0.05, "backoff_base_s": 0.05}}}))
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    e.calls = spy(e)
    e.bodies = []
    yield e
    await _stop(e)


def spy(env) -> list[tuple[str, dict]]:
    """Bọc handler `slack.*` của daemon: ghi tên method + params (KHÔNG giữ token) rồi chạy thật."""
    calls: list[tuple[str, dict]] = []
    for name in SLACK_RPC:
        fn = env.d.methods[name]

        def wrapped(p, _fn=fn, _n=name):
            calls.append((_n, {k: v for k, v in (p or {}).items() if k not in ("bot_token", "app_token") and (not k.startswith("_") or k == "_audit")}
                          | {"has_tokens": bool((p or {}).get("bot_token"))}))
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


def create_link(html: str) -> dict:
    m = re.search(r'href="(https://api\.slack\.com/apps\?new_app=1&amp;manifest_json=[^"]+)"[^>]*>Tạo app Slack 2 chiều', html)
    assert m, "thiếu link tạo app 2 chiều"
    q = parse_qs(urlsplit(unescape(m.group(1))).query)
    return json.loads(q["manifest_json"][0])


# =========================================================================== hàm thuần
def test_manifest_2way_and_slash_validation():
    m = slack_2way_manifest()
    assert m["settings"]["socket_mode_enabled"] is True and m["settings"]["interactivity"]["is_enabled"] is True
    assert m["settings"]["event_subscriptions"]["bot_events"] == ["message.im", "app_mention", "message.channels", "message.groups"]
    assert m["features"]["app_home"]["messages_tab_enabled"] is True
    assert m["features"]["slash_commands"][0]["command"] == "/cfh"
    assert set(m["oauth_config"]["scopes"]["bot"]) == {"chat:write", "im:history", "im:read", "im:write", "commands", "users:read", "files:read", "files:write",
                                                        "app_mentions:read", "channels:history", "groups:history"}
    assert slack_2way_manifest("/cfh-phuong")["features"]["slash_commands"][0]["command"] == "/cfh-phuong"
    assert slack_2way_manifest()["features"]["slash_commands"][0]["command"] == "/cfh"  # hằng số không bị sửa
    for bad in ("cfh", "/cfh x", "/" + "a" * 40, "/<script>", None, ""):
        assert slack_slash(bad) == "/cfh", bad


# =========================================================================== trang
async def test_settings_shows_2way_block_and_keeps_webhook_wizard(env):
    await env.login()
    r = await get(env, "/settings")
    assert r.status_code == 200
    html = r.text
    assert 'id="slack2"' in html and "Slack 2 chiều" in html and "chưa bật" in html
    # wizard một chiều vẫn còn nguyên
    assert "Kết nối Slack" in html and 'action="/do/slack_connect"' in html and SLACK_CREATE_URL.replace("&", "&amp;") in html
    m = create_link(html)
    assert m["settings"]["socket_mode_enabled"] and m["features"]["slash_commands"][0]["command"] == "/cfh"
    # ô token: password, không tự điền, có mẫu kiểm phía trình duyệt
    for name, pref in (("bot_token", "xoxb-"), ("app_token", "xapp-")):
        tag = re.search(rf'<input name="{name}"[^>]*>', html).group(0)
        assert 'type="password"' in tag and 'autocomplete="off"' in tag and f'pattern="{pref}' in tag and "required" in tag
    # lệnh slash lấy từ cấu hình
    env.hub.config.set("notify.slack.slash_command", "/cfh-phuong")
    m = create_link((await get(env, "/settings")).text)
    assert m["features"]["slash_commands"][0]["command"] == "/cfh-phuong"
    # partial yêu cầu đăng nhập
    env.client.cookies.clear()
    assert (await env.client.get("/partials/slack/pending")).status_code == 401


async def test_setup_rejected_without_csrf_or_session_key(env):
    await env.login()
    tok = {"bot_token": BOT_TOKEN, "app_token": APP_TOKEN, "confirm": "yes", "next": "/settings#slack2"}
    r = await env.client.post("/do/slack2_setup", data={**tok, "k": env.key})  # thiếu CSRF
    assert r.status_code == 403
    r = await env.client.post("/do/slack2_setup", data={**tok, "csrf": "sai", "k": env.key})
    assert r.status_code == 403
    r = await env.client.post("/do/slack2_setup", data={**tok, "csrf": env.csrf})  # thiếu khoá phiên (WT-1)
    assert r.status_code == 403
    for path in ("/do/slack2_allow", "/do/slack2_revoke", "/do/slack2_test", "/do/slack2_disable"):
        r = await env.client.post(path, data={"chat_id": DM_BOSS, "user_id": BOSS, "confirm": "yes", "k": env.key})
        assert r.status_code == 403, path
    assert env.calls == []
    assert env.hub.secrets.get("SLACK_BOT_TOKEN") is None and env.hub.secrets.get("SLACK_APP_TOKEN") is None


async def test_setup_pair_allow_test_revoke_disable(env, sk, caplog):
    caplog.set_level(logging.DEBUG)
    await env.login()
    # dạng token sai → không gọi RPC, thông điệp không chứa giá trị đã dán
    r = await post(env, "/do/slack2_setup", {"bot_token": "xoxp-userTokenNotBot0123456789", "app_token": APP_TOKEN, "confirm": "yes", "next": "/settings#slack2"})
    assert r.status_code == 303 and env.calls == []
    page = (await get(env, "/settings")).text
    assert "bot token phải có dạng xoxb-" in page and "userTokenNotBot" not in page
    # thiếu xác nhận → trang xác nhận KHÔNG mang token
    r = await post(env, "/do/slack2_setup", {"bot_token": BOT_TOKEN, "app_token": APP_TOKEN, "next": "/settings#slack2"})
    assert r.status_code == 200 and "GHI ĐÈ" in r.text and env.calls == []
    # dán token + xác nhận → slack.setup
    r = await post(env, "/do/slack2_setup", {"bot_token": BOT_TOKEN, "app_token": APP_TOKEN, "confirm": "yes", "next": "/settings#slack2"})
    assert r.status_code == 303 and r.headers["location"] == "/settings#slack2", r.text
    assert names(env) == ["slack.setup"] and env.calls[0][1]["has_tokens"] and env.calls[0][1]["_audit"]["channel"] == "web"
    assert env.hub.secrets.get("SLACK_BOT_TOKEN") == BOT_TOKEN and env.hub.config.get("notify.slack.team_id") == TEAM
    assert env.hub.config.get("notify.slack.interactive") is True
    assert [(a["channel"], a["actor"]) for a in audit_rows(env, "slack.setup")] == [("web", "user")]
    code = env.hub.slack.pairing["code"]
    html = (await get(env, "/settings")).text
    assert f"ghepnoi {code}" in html and 'hx-get="/partials/slack/pending"' in html and 'hx-trigger="every 3s"' in html
    # bot nối Socket Mode → người nhắn mã trong DM → hiện ở danh sách chờ (poll)
    assert await until(lambda: env.hub.slack.info.get("polling") and sk.connected())
    assert "Socket Mode đã nối" in (await get(env, "/settings")).text
    sk.user_message(DM_BOSS, BOSS, f"ghepnoi {code}")
    assert await until(lambda: env.hub.slack.pending(None))
    part = (await get(env, "/partials/slack/pending")).text
    assert BOSS in part and DM_BOSS in part and 'action="/do/slack2_allow"' in part and "every 3s" in part
    assert "slack.pending" in names(env) and audit_rows(env, "slack.pending") == []  # poll là đọc — không ghi audit
    # xác nhận người → slack.allow
    r = await post(env, "/do/slack2_allow", {"chat_id": DM_BOSS, "user_id": BOSS, "name": "Boss", "next": "/settings#slack2"})
    assert r.status_code == 200 and "điều khiển cfharness" in r.text  # hộp xác nhận phía server khi không có JS
    r = await post(env, "/do/slack2_allow", {"chat_id": DM_BOSS, "user_id": BOSS, "name": "Boss", "confirm": "yes", "next": "/settings#slack2"})
    assert r.status_code == 303
    assert ("slack.allow", {"chat_id": DM_BOSS, "user_id": BOSS, "has_tokens": False, "_audit": {"channel": "web", "actor": "user"}}) in env.calls
    assert DM_BOSS in env.hub.config.get("notify.slack.allowed_chat_ids") and BOSS in env.hub.config.get("notify.slack.allowed_user_ids")
    assert audit_rows(env, "slack.allow")[-1]["channel"] == "web"
    html = (await get(env, "/settings")).text
    assert 'action="/do/slack2_revoke"' in html and DM_BOSS in html and "ghepnoi " + code not in html  # mã đã dùng
    # id sai dạng → không gọi RPC
    n = len(env.calls)
    await post(env, "/do/slack2_allow", {"chat_id": "x; rm", "user_id": BOSS, "confirm": "yes", "next": "/settings#slack2"})
    assert len(env.calls) == n
    # gửi thử → slack.test → tin tới DM
    before = len(sk.sent(DM_BOSS))
    r = await post(env, "/do/slack2_test", {"next": "/settings#slack2"})
    assert r.status_code == 303 and names(env)[-1] == "slack.test"
    assert await until(lambda: len(sk.sent(DM_BOSS)) > before)
    assert audit_rows(env, "slack.test")[-1]["channel"] == "web"
    # mã ghép nối mới (không dán lại token)
    r = await post(env, "/do/slack2_pair", {"next": "/settings#slack2"})
    assert r.status_code == 303 and env.calls[-1][0] == "slack.setup" and not env.calls[-1][1]["has_tokens"]
    assert f"ghepnoi {env.hub.slack.pairing['code']}" in (await get(env, "/settings")).text
    # gỡ DM → slack.revoke
    r = await post(env, "/do/slack2_revoke", {"chat_id": DM_BOSS, "confirm": "yes", "next": "/settings#slack2"})
    assert r.status_code == 303 and names(env)[-1] == "slack.revoke"
    assert DM_BOSS not in env.hub.config.get("notify.slack.allowed_chat_ids")
    assert audit_rows(env, "slack.revoke")[-1]["channel"] == "web"
    # tắt (giữ token) → slack.disable; bật lại không cần token
    r = await post(env, "/do/slack2_disable", {"confirm": "yes", "next": "/settings#slack2"})
    assert r.status_code == 303 and env.calls[-1] == ("slack.disable", {"forget_token": False, "has_tokens": False, "_audit": {"channel": "web", "actor": "user"}})
    assert env.hub.config.get("notify.slack.interactive") is False and env.hub.secrets.get("SLACK_BOT_TOKEN") == BOT_TOKEN
    assert "Bật lại" in (await get(env, "/settings")).text
    # tắt + xoá token
    r = await post(env, "/do/slack2_disable", {"forget": "1", "confirm": "yes", "next": "/settings#slack2"})
    assert r.status_code == 303 and env.calls[-1][1]["forget_token"] is True
    assert env.hub.secrets.get("SLACK_BOT_TOKEN") is None and env.hub.secrets.get("SLACK_APP_TOKEN") is None
    assert audit_rows(env, "slack.disable")[-1]["channel"] == "web"
    # token không bao giờ có trong response (thân + header), audit, log
    await asyncio.sleep(0.05)
    blob = "\n".join(env.bodies)
    audit = json.dumps([dict(x) for x in env.hub.store.conn.execute("SELECT * FROM audit_log").fetchall()], default=str)
    for t in (BOT_TOKEN, APP_TOKEN):
        assert t not in blob and t not in audit and t not in caplog.text
