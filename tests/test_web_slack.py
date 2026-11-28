"""Trang "Kết nối Slack" (`/settings/notifications/slack`, web/slack_pages.py — plan-chat-setup-web PR2/PR3) — daemon IN-PROCESS + Slack GIẢ
cục bộ (tests/slack_fake.py), app Starlette qua ASGITransport như tests/test_web.py. Không gọi Slack/claude thật.

Kiểm: manifest đúng (Socket Mode, interactivity, tab Messages, message.im, lệnh slash theo cấu hình — đổi ngay ở bước 1, scope); bước suy
ra từ `slack.status`; POST thiếu CSRF/khoá phiên → 403; token sai dạng → không gọi RPC; bước 2 dán bot token → `slack.verify_bot` → bước 3
có link THẲNG tới App-Level Tokens của app → dán app token → `slack.setup{app_token}` → bước 4 "Mở DM với bot" + `ghepnoi <mã>` → người
nhắn mã hiện ở danh sách chờ (htmx poll) → "Đây là tôi — cho phép" gọi thẳng `slack.allow` (DM đang chờ; kênh / id thủ công vẫn xác nhận)
→ người được phép có tên (escape); gỡ/gửi thử/tắt gọi đúng RPC; webhook một chiều ở Nâng cao; neo cũ; audit kênh `web`; token không bao
giờ có trong response/audit/log.
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

from slack_fake import APP_ID, APP_TOKEN, BOT_TOKEN, TEAM, FakeSlack

BOSS, DM_BOSS = "U0BOSS001", "D0BOSS001"
SLACK_RPC = ("slack.verify_bot", "slack.setup", "slack.pending", "slack.allow", "slack.revoke", "slack.disable", "slack.test")
PAGE = "/settings/notifications/slack"


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
                          | {"has_tokens": bool((p or {}).get("bot_token") or (p or {}).get("app_token"))}))
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
    m = re.search(r'href="(https://api\.slack\.com/apps\?new_app=1&amp;manifest_json=[^"]+)"[^>]*>(?:(?!</a>).)*Tạo app Slack 2 chiều', html, re.S)
    assert m, "thiếu link tạo app 2 chiều"
    q = parse_qs(urlsplit(unescape(m.group(1))).query)
    return json.loads(q["manifest_json"][0])


def stg(html: str) -> list[str]:
    """Các bước đang "now" của stepper."""
    return re.findall(r'<li class="now"><span>(\d)</span>', html)


# =========================================================================== hàm thuần
def test_manifest_2way_and_slash_validation():
    m = slack_2way_manifest()
    assert m["settings"]["socket_mode_enabled"] is True and m["settings"]["interactivity"]["is_enabled"] is True
    assert m["settings"]["event_subscriptions"]["bot_events"] == ["message.im", "app_mention", "message.channels", "message.groups"]
    assert m["features"]["app_home"]["messages_tab_enabled"] is True
    assert m["features"]["slash_commands"][0]["command"] == "/cfh"
    assert set(m["oauth_config"]["scopes"]["bot"]) == {"chat:write", "im:history", "im:read", "im:write", "commands", "users:read", "files:read", "files:write",
                                                        "app_mentions:read", "channels:history", "groups:history", "reactions:write"}
    assert slack_2way_manifest("/cfh-phuong")["features"]["slash_commands"][0]["command"] == "/cfh-phuong"
    assert slack_2way_manifest()["features"]["slash_commands"][0]["command"] == "/cfh"  # hằng số không bị sửa
    for bad in ("cfh", "/cfh x", "/" + "a" * 40, "/<script>", None, ""):
        assert slack_slash(bad) == "/cfh", bad


def test_stage_links_and_person_are_strict():
    from cfharness.web import slack_pages as S

    assert S.stage(None) == "down" and S.stage({}) == "app" and S.stage({"configured": True}) == "app_token"
    assert S.stage({"configured": True, "app_token": True}) == "off"
    assert S.stage({"configured": True, "app_token": True, "interactive": True}) == "pair"
    assert S.stage({"configured": True, "app_token": True, "interactive": True, "allowed_chat_ids": ["D1"], "pairing_active": True}) == "pair"
    assert S.stage({"configured": True, "app_token": True, "interactive": True, "allowed_chat_ids": ["D1"]}) == "done"
    good = {"app_general": f"https://api.slack.com/apps/{APP_ID}/general", "dm": f"https://slack.com/app_redirect?app={APP_ID}&team={TEAM}"}
    assert S.links({"links": good}) == good
    assert S.links({"links": {"app_general": "javascript:alert(1)", "dm": "https://evil.example/app_redirect?app=A&team=T"}}) == {}
    assert S.links(None) == {} and S.links({"links": "x"}) == {}
    p = S.person({"chat_id": "D0X000001", "user_id": "U1; rm", "name": "<b>A</b>\n" + "x" * 99, "username": "@a‮b"})
    assert p["dm"] and p["user_id"] == "" and "\n" not in p["name"] and len(p["name"]) <= 60 and "‮" not in p["username"]
    assert S.person({"chat_id": "C0X000001"})["dm"] is False


# =========================================================================== trang
async def test_page_steps_manifest_slash_and_webhook_in_advanced(env):
    r = await env.client.get(PAGE)
    assert r.status_code in (302, 303) and "/login" in r.headers.get("location", "")
    await env.login()
    html = (await get(env, PAGE)).text
    assert "Kết nối Slack" in html and stg(html) == ["1", "2"] and 'id="slack-step1"' in html and 'action="/do/slack2_verify"' in html
    m = create_link(html)
    assert m["settings"]["socket_mode_enabled"] and m["features"]["slash_commands"][0]["command"] == "/cfh"
    # ô token: password, không tự điền, có mẫu kiểm phía trình duyệt; chưa có token → không ô ghi đè; ô app token chưa dùng được
    for name, pref in (("bot_token", "xoxb-"), ("app_token", "xapp-")):
        tag = re.search(rf'<input name="{name}"[^>]*>', html).group(0)
        assert 'type="password"' in tag and 'autocomplete="off"' in tag and f'pattern="{pref}' in tag and "required" in tag
    assert 'name="overwrite"' not in html and "disabled" in re.search(r'<input name="app_token"[^>]*>', html).group(0)
    # webhook một chiều nằm trong "Nâng cao" của trang Slack (neo cũ #slack ở đó)
    assert 'id="slack-advanced"' in html and 'action="/do/slack_connect"' in html and SLACK_CREATE_URL.replace("&", "&amp;") in html
    assert 'id="webhook"' in html and 'id="slack"' in html and 'id="slack2"' in html
    # bước 1: đổi lệnh slash ngay trên trang → manifest dựng lại; sai dạng → không ghi
    r = await post(env, "/do/slack2_slash", {"slash": "/cfh-phuong", "next": PAGE})
    assert r.status_code == 303 and env.hub.config.get("notify.slack.slash_command") == "/cfh-phuong"
    assert audit_rows(env, "config.set")[-1]["channel"] == "web"
    assert create_link((await get(env, PAGE)).text)["features"]["slash_commands"][0]["command"] == "/cfh-phuong"
    await post(env, "/do/slack2_slash", {"slash": "/<script>", "next": PAGE})
    assert env.hub.config.get("notify.slack.slash_command") == "/cfh-phuong"
    # partial yêu cầu đăng nhập
    env.client.cookies.clear()
    assert (await env.client.get("/partials/slack/pending")).status_code == 401


async def test_old_anchors_still_lead_to_slack(env):
    """Link cũ /settings/notifications#slack|#slack2 và /settings#slack|#slack2: không JS → neo vẫn nằm trên thẻ Slack (có nút sang trang
    Slack); có JS → early.js chuyển thẳng sang /settings/notifications/slack."""
    await env.login()
    note = (await get(env, "/settings/notifications")).text
    card = re.search(r'<article class="chan-card" id="chan-slack">.*?</article>', note, re.S).group(0)
    assert 'id="slack"' in card and 'id="slack2"' in card and f'href="{PAGE}"' in card
    early = (await env.client.get("/static/early.js")).text
    assert '"/settings/notifications/slack"' in early and "slack2" in early and '"#webhook"' in early
    idx = (await get(env, "/settings")).text
    assert 'id="slack2"' in idx and 'href="/settings/notifications"' in idx


async def test_post_rejected_without_csrf_or_session_key(env):
    await env.login()
    tok = {"bot_token": BOT_TOKEN, "app_token": APP_TOKEN, "confirm": "yes", "next": PAGE}
    for path in ("/do/slack2_verify", "/do/slack2_app", "/do/slack2_setup"):
        assert (await env.client.post(path, data={**tok, "k": env.key})).status_code == 403  # thiếu CSRF
        assert (await env.client.post(path, data={**tok, "csrf": "sai", "k": env.key})).status_code == 403
        assert (await env.client.post(path, data={**tok, "csrf": env.csrf})).status_code == 403  # thiếu khoá phiên (WT-1)
    for path in ("/do/slack2_allow", "/do/slack2_allow_confirm", "/do/slack2_revoke", "/do/slack2_test", "/do/slack2_disable", "/do/slack2_slash"):
        r = await env.client.post(path, data={"chat_id": DM_BOSS, "user_id": BOSS, "slash": "/x", "confirm": "yes", "k": env.key})
        assert r.status_code == 403, path
    assert env.calls == []
    assert env.hub.secrets.get("SLACK_BOT_TOKEN") is None and env.hub.secrets.get("SLACK_APP_TOKEN") is None


async def test_stepper_verify_app_token_pair_allow_manage(env, sk, caplog):
    caplog.set_level(logging.DEBUG)
    await env.login()
    # bước 2: dạng token sai → không gọi RPC, thông điệp không chứa giá trị đã dán
    r = await post(env, "/do/slack2_verify", {"bot_token": "xoxp-userTokenNotBot0123456789", "confirm": "yes", "next": PAGE})
    assert r.status_code == 303 and env.calls == []
    page = (await get(env, PAGE)).text
    assert "bot token phải có dạng xoxb-" in page and "userTokenNotBot" not in page
    await post(env, "/do/slack2_verify", {"bot_token": APP_TOKEN, "confirm": "yes", "next": PAGE})  # dán nhầm xapp vào ô bot
    assert env.calls == []
    # bot token đúng → slack.verify_bot (không trang xác nhận) → chỉ lưu bot token, nhớ app
    r = await post(env, "/do/slack2_verify", {"bot_token": BOT_TOKEN, "confirm": "yes", "next": PAGE})
    assert r.status_code == 303 and r.headers["location"] == PAGE, r.text
    assert names(env) == ["slack.verify_bot"] and env.calls[0][1]["has_tokens"] and env.calls[0][1]["_audit"]["channel"] == "web"
    assert env.hub.secrets.get("SLACK_BOT_TOKEN") == BOT_TOKEN and env.hub.secrets.get("SLACK_APP_TOKEN") is None
    assert [(a["channel"], a["actor"]) for a in audit_rows(env, "slack.verify_bot")] == [("web", "user")]
    # bước 3: link THẲNG tới trang App-Level Tokens của app vừa tạo; ô bot token giờ đòi tích "ghi đè"
    html = (await get(env, PAGE)).text
    assert stg(html) == ["3"] and f'href="https://api.slack.com/apps/{APP_ID}/general"' in html
    assert "disabled" not in re.search(r'<input name="app_token"[^>]*>', html).group(0) and 'name="overwrite"' in html
    n = len(env.calls)
    await post(env, "/do/slack2_verify", {"bot_token": BOT_TOKEN, "confirm": "yes", "next": PAGE})  # không tích ghi đè → chặn ở web
    assert len(env.calls) == n and "đã có bot token" in (await get(env, PAGE)).text
    await post(env, "/do/slack2_app", {"app_token": BOT_TOKEN, "confirm": "yes", "next": PAGE})  # app token sai dạng
    assert len(env.calls) == n
    # app token đúng → slack.setup{app_token} (bot token lấy từ secret store) → Socket Mode + mã ghép nối
    r = await post(env, "/do/slack2_app", {"app_token": APP_TOKEN, "confirm": "yes", "next": PAGE})
    assert r.status_code == 303 and env.calls[-1][0] == "slack.setup" and env.calls[-1][1]["_audit"]["channel"] == "web"
    assert env.hub.secrets.get("SLACK_APP_TOKEN") == APP_TOKEN and env.hub.config.get("notify.slack.team_id") == TEAM
    assert env.hub.config.get("notify.slack.interactive") is True
    code = env.hub.slack.pairing["code"]
    html = (await get(env, PAGE)).text
    assert stg(html) == ["4"] and 'id="slack-step4"' in html
    assert f"ghepnoi {code}" in html and 'hx-get="/partials/slack/pending"' in html and 'hx-trigger="every 3s"' in html
    assert f'href="https://slack.com/app_redirect?app={APP_ID}&amp;team={TEAM}"' in html and "Mở DM với bot" in html
    # bot nối Socket Mode → người nhắn mã trong DM → hiện ở danh sách chờ (poll), có tên từ users.info
    assert await until(lambda: env.hub.slack.info.get("polling") and sk.connected())
    sk.user_message(DM_BOSS, BOSS, f"ghepnoi {code}")
    assert await until(lambda: env.hub.slack.pending(None) and env.hub.slack.pending(None)[0].get("name"))
    part = (await get(env, "/partials/slack/pending")).text
    assert "Sếp Thử" in part and DM_BOSS in part and 'action="/do/slack2_allow"' in part and "Đây là tôi — cho phép" in part
    assert "data-confirm" not in re.search(r'<form method="post" action="/do/slack2_allow".*?</form>', part, re.S).group(0)
    assert "slack.pending" in names(env) and audit_rows(env, "slack.pending") == []  # poll là đọc — không ghi audit
    # DM không ở danh sách chờ → từ chối (không gọi slack.allow)
    r = await post(env, "/do/slack2_allow", {"chat_id": "D0OTHER01", "user_id": BOSS, "next": PAGE})
    assert r.status_code == 303 and "slack.allow" not in names(env)
    # Q2: "Đây là tôi — cho phép" → slack.allow NGAY (không trang xác nhận); user_id lấy từ daemon, không tin form
    r = await post(env, "/do/slack2_allow", {"chat_id": DM_BOSS, "user_id": "U0EVIL0001", "name": "Boss", "next": PAGE})
    assert r.status_code == 303, r.text
    assert ("slack.allow", {"chat_id": DM_BOSS, "user_id": BOSS, "has_tokens": False, "_audit": {"channel": "web", "actor": "user"}}) in env.calls
    assert DM_BOSS in env.hub.config.get("notify.slack.allowed_chat_ids") and env.hub.config.get("notify.slack.allowed_user_ids") == [BOSS]
    assert audit_rows(env, "slack.allow")[-1]["channel"] == "web"
    # quản lý: người được phép có TÊN + id nhỏ + Gỡ
    html = (await get(env, PAGE)).text
    assert 'id="slack-manage"' in html and "Sếp Thử" in html and "@sep" in html and 'action="/do/slack2_revoke"' in html and DM_BOSS in html
    assert "ghepnoi " + code not in html and stg(html) == []  # mã đã dùng; xong → không bước nào "đang làm"
    # id sai dạng → không gọi RPC; thêm bằng id thủ công → luôn có trang xác nhận
    n = len(env.calls)
    await post(env, "/do/slack2_allow_confirm", {"chat_id": "x; rm", "user_id": BOSS, "confirm": "yes", "next": PAGE})
    assert len(env.calls) == n
    r = await post(env, "/do/slack2_allow_confirm", {"chat_id": "D0SECOND1", "user_id": "U0SECOND1", "next": PAGE})
    assert r.status_code == 200 and "điều khiển cfharness" in r.text and len(env.calls) == n
    # gửi thử → slack.test → tin tới DM
    before = len(sk.sent(DM_BOSS))
    r = await post(env, "/do/slack2_test", {"next": PAGE})
    assert r.status_code == 303 and names(env)[-1] == "slack.test"
    assert await until(lambda: len(sk.sent(DM_BOSS)) > before)
    # mã ghép nối mới (không dán lại token)
    r = await post(env, "/do/slack2_pair", {"next": PAGE})
    assert r.status_code == 303 and env.calls[-1][0] == "slack.setup" and not env.calls[-1][1]["has_tokens"]
    assert f"ghepnoi {env.hub.slack.pairing['code']}" in (await get(env, PAGE)).text
    # gỡ → slack.revoke (tên biến mất)
    r = await post(env, "/do/slack2_revoke", {"chat_id": DM_BOSS, "confirm": "yes", "next": PAGE})
    assert r.status_code == 303 and names(env)[-1] == "slack.revoke"
    assert DM_BOSS not in env.hub.config.get("notify.slack.allowed_chat_ids") and "Sếp Thử" not in (await get(env, PAGE)).text
    # tắt (giữ token) → "Bật lại"; tắt + xoá token → về bước 1
    r = await post(env, "/do/slack2_disable", {"confirm": "yes", "next": PAGE})
    assert r.status_code == 303 and env.calls[-1] == ("slack.disable", {"forget_token": False, "has_tokens": False, "_audit": {"channel": "web", "actor": "user"}})
    assert "Bật lại" in (await get(env, PAGE)).text
    await post(env, "/do/slack2_disable", {"forget": "1", "confirm": "yes", "next": PAGE})
    assert env.hub.secrets.get("SLACK_BOT_TOKEN") is None and env.hub.secrets.get("SLACK_APP_TOKEN") is None
    assert stg((await get(env, PAGE)).text) == ["1", "2"]
    # token không bao giờ có trong response (thân + header), audit, log
    await asyncio.sleep(0.05)
    blob = "\n".join(env.bodies)
    audit = json.dumps([dict(x) for x in env.hub.store.conn.execute("SELECT * FROM audit_log").fetchall()], default=str)
    for t in (BOT_TOKEN, APP_TOKEN):
        assert t not in blob and t not in audit and t not in caplog.text


async def test_two_tokens_at_once_still_works_and_overwrite_guard(env, sk):
    """Dán cả hai token một lần (`slack2_setup`) vẫn chạy (có trang xác nhận nêu GHI ĐÈ); app token đã có → bước 3 đòi tích ghi đè."""
    await env.login()
    r = await post(env, "/do/slack2_setup", {"bot_token": BOT_TOKEN, "app_token": APP_TOKEN, "next": PAGE})
    assert r.status_code == 200 and "GHI ĐÈ" in r.text and env.calls == [] and BOT_TOKEN not in r.text
    r = await post(env, "/do/slack2_setup", {"bot_token": BOT_TOKEN, "app_token": APP_TOKEN, "confirm": "yes", "next": PAGE})
    assert r.status_code == 303 and names(env) == ["slack.setup"] and env.hub.secrets.get("SLACK_APP_TOKEN") == APP_TOKEN
    n = len(env.calls)
    await post(env, "/do/slack2_app", {"app_token": APP_TOKEN, "confirm": "yes", "next": PAGE})
    assert len(env.calls) == n and "đã có app token" in (await get(env, PAGE)).text
    r = await post(env, "/do/slack2_app", {"app_token": APP_TOKEN, "overwrite": "yes", "confirm": "yes", "next": PAGE})
    assert r.status_code == 303 and names(env)[-1] == "slack.setup"


async def test_peer_names_escaped(env, sk):
    await env.login()
    env.hub.secrets.set("SLACK_BOT_TOKEN", BOT_TOKEN)
    env.hub.secrets.set("SLACK_APP_TOKEN", APP_TOKEN)
    env.hub.config.set("notify.slack.interactive", True)
    env.hub.config.set("notify.slack.allowed_chat_ids", [DM_BOSS])
    env.hub.slack.record_peer(DM_BOSS, BOSS, "<script>alert(1)</script>\nX", "<img src=x>")
    html = (await get(env, PAGE)).text
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<img src=x>" not in html
    assert "<script>alert(1)" not in (await get(env, "/settings/notifications")).text
