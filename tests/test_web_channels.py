"""Thẻ kênh ở Cài đặt › Thông báo & kênh, "Gửi thử tất cả", thẻ gợi ý ở trang Hoạt động, mô tả động ở danh mục Cài đặt
(docs/dev-notes/plan-chat-setup-web.md §3.1, §3.4 — PR2/PR5).

`present.channel_state` (thuần, dựa trên `status.diagnosis` của daemon) = MỘT nguồn trạng thái cho thẻ, trang Hoạt động và `/settings`.
Test trang dùng daemon IN-PROCESS + Bot API Telegram GIẢ (tests/telegram_fake.py); trạng thái lạ (lỗi Slack, workspace lệch…) thì thay
handler `telegram.status`/`slack.status` bằng dict dựng từ `chat.diagnose` thật. Không gọi Telegram/Slack/claude thật.
"""

from __future__ import annotations

import re

import pytest
import yaml

from cfharness.chat.diagnose import diagnose
from cfharness.runtime import FakeRuntime
from cfharness.web import present as P
from test_web import ISOLATED, _start, _stop, audit_rows, until  # noqa: F401 — dùng lại khung test web

from telegram_fake import TOKEN, FakeTelegram

BOSS = 555001


def _tg(**kw):
    st = {"configured": True, "enabled": True, "polling": True, "last_error": None, "last_poll_at": 1_700_000_000.0, "bot_username": "hub_test_bot",
          "allowed_chat_ids": ["1"], "pairing_active": False, "frozen": False}
    st.update(kw)
    st["diagnosis"] = diagnose("telegram", st)
    return st


def _sl(**kw):
    st = {"configured": True, "enabled": True, "interactive": True, "app_token": True, "polling": True, "last_error": None, "team": "Nhóm <b>A</b>",
          "team_id": "T0TEAM001", "token_team_id": "T0TEAM001", "allowed_chat_ids": ["D0BOSS001"], "pairing_active": False, "frozen": False}
    st.update(kw)
    st["diagnosis"] = diagnose("slack", st)
    return st


# =========================================================================== hàm thuần
@pytest.mark.parametrize("kind,st,webhook,state,pill,fix", [
    ("telegram", None, False, "unknown", "không rõ", None),
    ("telegram", {"configured": False}, False, "off", "chưa kết nối", ("href", "/settings/notifications/telegram")),
    ("telegram", _tg(), False, "ok", "hoạt động", None),
    ("telegram", _tg(enabled=False), False, "off", "đã tắt", ("act", "tg_pair")),
    ("telegram", _tg(frozen=True), False, "off", "đã chuyển máy", None),
    ("telegram", _tg(polling=False), False, "setup", "đang kết nối…", None),
    ("telegram", _tg(allowed_chat_ids=[]), False, "pairing", "chờ ghép nối", ("href", "/settings/notifications/telegram#tg-step3")),
    ("telegram", _tg(last_error="getUpdates: Unauthorized"), False, "error", "lỗi", ("href", "/settings/notifications/telegram#tg-setup")),
    ("telegram", _tg(last_error="getUpdates: Conflict: can't use getUpdates method while webhook is active; use deleteWebhook"), False,
     "error", "lỗi", ("act", "tg_repair")),
    ("telegram", _tg(last_error="getUpdates: Conflict: terminated by other getUpdates request; make sure"), False, "error", "lỗi",
     ("href", "/settings/notifications/telegram#tg-setup")),
    ("telegram", _tg(last_error="getUpdates: ConnectError: boom"), False, "error", "lỗi", None),
    ("slack", {"configured": False}, False, "off", "chưa kết nối", ("href", "/settings/notifications/slack")),
    ("slack", {"configured": False}, True, "ok", "chỉ thông báo", None),
    ("slack", _sl(), False, "ok", "hoạt động", None),
    ("slack", _sl(app_token=False), False, "setup", "thiếu bước", ("href", "/settings/notifications/slack#slack-step3")),
    ("slack", _sl(app_token=False), True, "ok", "chỉ thông báo", None),
    ("slack", _sl(interactive=False), False, "off", "đã tắt", ("act", "slack2_pair")),
    ("slack", _sl(token_team_id="T0OTHER01"), False, "error", "lỗi", ("href", "/settings/notifications/slack#slack-manage")),
    ("slack", _sl(last_error="auth.test: invalid_auth"), False, "error", "lỗi", ("href", "/settings/notifications/slack#slack-step2")),
    ("slack", _sl(last_error="apps.connections.open: not_allowed_token_type"), False, "error", "lỗi",
     ("href", "/settings/notifications/slack#slack-step3")),
    ("slack", _sl(allowed_chat_ids=[], pairing_active=True), False, "pairing", "chờ ghép nối", ("href", "/settings/notifications/slack#slack-step4")),
])
def test_channel_state_table(kind, st, webhook, state, pill, fix):
    c = P.channel_state(kind, st, webhook=webhook)
    assert c["state"] == state and c["pill"] == pill and c["state"] in P.CHANNEL_STATES
    assert c["tone"] == {"ok": "ok", "pairing": "warn", "setup": "warn", "error": "danger", "off": "muted", "unknown": "muted"}[state]
    assert c["message"] and "Conflict" not in c["message"] and "invalid_auth" not in c["message"]  # lỗi thô không vào câu
    if fix is None:
        assert c["fix"] is None
    elif fix[0] == "href":
        assert c["fix"]["href"] == fix[1]
    else:
        assert c["fix"]["act"] == fix[1]
    # Gửi thử chỉ khi kênh đang nhận được; webhook-only → kênh slack_webhook
    assert c["test_channel"] == ((("slack_webhook" if pill == "chỉ thông báo" else kind)) if state == "ok" else None)


def test_channel_state_fields_clean():
    c = P.channel_state("telegram", _tg(bot_username='x"><script>', allowed_chat_ids=["1", "2"]))
    assert c["bot"] is None and c["people"] == 2 and c["last_contact"] and c["page"] == "/settings/notifications/telegram"
    assert P.channel_state("telegram", _tg())["bot"] == "@hub_test_bot"
    s = P.channel_state("slack", _sl(team="A\nB‮" + "x" * 100))
    assert "\n" not in s["bot"] and "‮" not in s["bot"] and len(s["bot"]) <= 60
    with pytest.raises(ValueError):
        P.channel_state("email", {})
    assert P.channels_summary([P.channel_state("telegram", _tg()), P.channel_state("slack", None)]) == "Telegram: hoạt động · Slack: không rõ"


def test_chat_peers_clean():
    rows = P.chat_peers([{"chat_id": "123", "user_id": "123", "name": "<b>A</b>\nB", "username": "@an", "at": 1_700_000_000.0},
                         {"chat_id": "-1009", "name": None}, {"chat_id": "x; rm"}, "rác", {"chat_id": "D0X000001", "user_id": "U1 rm"}])
    assert [r["chat_id"] for r in rows] == ["123", "-1009", "D0X000001"]
    assert "\n" not in rows[0]["name"] and rows[0]["username"] == "an" and rows[0]["at"] and rows[1]["group"] and rows[2]["user_id"] == ""
    assert P.chat_peers(None) == []


# =========================================================================== trang
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
    e.tests = []
    fn = e.d.methods["notify.test"]

    def spy(p, _fn=fn):
        e.tests.append({k: v for k, v in (p or {}).items() if not k.startswith("_")})
        return _fn(p)

    e.d.methods["notify.test"] = spy
    yield e
    await _stop(e)


def fake_status(env, tg_st=None, sl_st=None):
    if tg_st is not None:
        env.d.methods["telegram.status"] = lambda p: tg_st
    if sl_st is not None:
        env.d.methods["slack.status"] = lambda p: sl_st


def card(html: str, kind: str) -> str:
    return re.search(rf'<article class="chan-card" id="chan-{kind}">.*?</article>', html, re.S).group(0)


async def test_cards_fresh_hub_connect_buttons_and_advanced(env):
    await env.login()
    html = (await env.client.get("/settings/notifications")).text
    t, s = card(html, "telegram"), card(html, "slack")
    assert "chưa kết nối" in t and 'href="/settings/notifications/telegram"' in t and "Kết nối" in t and "Gửi thử" not in t
    assert "chưa kết nối" in s and 'href="/settings/notifications/slack"' in s
    # Nâng cao theo kênh: khoá notify.telegram.* trong thẻ Telegram, notify.slack.* trong thẻ Slack; khoá chung ở "Cấu hình thông báo"
    assert 'id="cfg-notify-telegram"' in t and "notify.telegram.allow_groups" in t and "notify.slack." not in t
    assert 'id="cfg-notify-slack"' in s and "notify.slack.allow_channels" in s and "notify.telegram." not in s
    gen = html[html.index('id="notify-cfg"'):]
    assert "notify.reminder_interval_s" in gen and "notify.slack.enabled" not in gen and 'id="cfg-notify"' in gen
    assert 'id="notify-events"' in html  # "Nhận thông báo gì" giữ nguyên
    assert 'action="/do/notify_test"' in html and 'name="channel" value="all"' in html and "Gửi thử tất cả" in html


@pytest.mark.parametrize("tg_st,sl_st,needles,tg_fix", [
    (_tg(), _sl(), ["pill ok", "@hub_test_bot", "1 người được phép"], None),
    (_tg(last_error="getUpdates: Unauthorized"), _sl(), ["pill danger", "dán token mới"], "/settings/notifications/telegram#tg-setup"),
    (_tg(enabled=False), _sl(), ["đã tắt", 'action="/do/tg_pair"', "Bật lại"], None),
    (_tg(allowed_chat_ids=[]), _sl(), ["chờ ghép nối", "Ghép nối"], "/settings/notifications/telegram#tg-step3"),
])
async def test_cards_render_each_state(env, tg_st, sl_st, needles, tg_fix):
    fake_status(env, tg_st, sl_st)
    await env.login()
    html = (await env.client.get("/settings/notifications")).text
    t = card(html, "telegram")
    for n in needles:
        assert n in t, n
    if tg_fix:
        assert f'href="{tg_fix}"' in t
    s = card(html, "slack")
    assert "pill ok" in s and "Nhóm &lt;b&gt;A&lt;/b&gt;" in s and "<b>A</b>" not in s and 'value="slack"' in s  # tên workspace escape


async def test_slack_error_card_and_settings_index_desc(env):
    fake_status(env, _tg(), _sl(last_error="apps.connections.open: not_allowed_token_type"))
    await env.login()
    s = card((await env.client.get("/settings/notifications")).text, "slack")
    assert "pill danger" in s and 'href="/settings/notifications/slack#slack-step3"' in s and "Dán app token" in s
    assert "not_allowed_token_type" not in s
    idx = (await env.client.get("/settings")).text
    assert "Telegram: hoạt động · Slack: lỗi" in idx


async def test_send_test_all_and_one_channel(env, tg):
    await env.login()
    # chưa kênh nào → báo lỗi rõ, không im lặng
    r = await env.post("/do/notify_test", {"channel": "all", "next": "/settings/notifications"})
    assert r.status_code == 303 and env.tests == [{"channels": "all"}]
    assert "Chưa có kênh nào đang bật" in (await env.client.get("/settings/notifications")).text
    # kênh lạ → không gọi RPC
    await env.post("/do/notify_test", {"channel": "email", "next": "/settings/notifications"})
    assert len(env.tests) == 1
    # Telegram chạy + một người → gửi thử tất cả: tin tới chat, thông báo theo từng kênh
    env.hub.secrets.set("TELEGRAM_BOT_TOKEN", TOKEN)
    env.hub.config.set("notify.telegram.allowed_chat_ids", [str(BOSS)])
    assert await until(lambda: env.hub.telegram.info.get("polling"))
    before = len(tg.sent(BOSS))
    r = await env.post("/do/notify_test", {"channel": "all", "next": "/settings/notifications"})
    assert r.status_code == 303 and env.tests[-1] == {"channels": "all"}
    assert await until(lambda: len(tg.sent(BOSS)) > before)
    flash = (await env.client.get("/settings/notifications")).text
    assert "Telegram: đã gửi (1 chat)" in flash and "Slack 2 chiều" not in flash.split("Gửi thử —", 1)[-1][:200]  # kênh chưa cấu hình: bỏ qua, không kể
    assert [a["channel"] for a in audit_rows(env, "notify.test")][-1] == "web"
    # Gửi thử riêng một kênh
    r = await env.post("/do/notify_test", {"channel": "telegram", "next": "/settings/notifications"})
    assert r.status_code == 303 and env.tests[-1] == {"channels": ["telegram"]}
    # kênh được nêu tên mà chưa cấu hình → lỗi kèm lý do
    await env.post("/do/notify_test", {"channel": "slack", "next": "/settings/notifications"})
    assert "Gửi thử chưa được: Slack 2 chiều: lỗi" in (await env.client.get("/settings/notifications")).text
    blob = "\n".join(str(dict(x)) for x in env.hub.store.conn.execute("SELECT * FROM audit_log").fetchall())
    assert TOKEN.split(":", 1)[1] not in blob


async def test_csrf_and_session_key_required(env):
    await env.login()
    for path in ("/do/notify_test", "/do/tg_repair", "/do/slack2_slash"):
        for data in ({"k": env.key}, {"csrf": "sai", "k": env.key}, {"csrf": env.csrf}):
            r = await env.client.post(path, data={**data, "channel": "all", "confirm": "yes", "slash": "/x"})
            assert r.status_code == 403, (path, data)
    assert env.tests == []


async def test_home_card_shown_until_a_channel_works(env):
    await env.login()
    html = (await env.client.get("/activity")).text
    assert 'id="chat-cta"' in html and 'data-dismiss="chat-cta"' in html and "data-dismiss-btn" in html
    assert 'href="/settings/notifications/telegram"' in html and 'href="/settings/notifications/slack"' in html
    fake_status(env, _tg())
    assert 'id="chat-cta"' not in (await env.client.get("/activity")).text
    fake_status(env, {"configured": False, "diagnosis": diagnose("telegram", {})}, _sl(app_token=False, configured=False, webhook=True))
    assert 'id="chat-cta"' not in (await env.client.get("/activity")).text  # webhook Slack một chiều = vẫn nhận thông báo
    app_js = (await env.client.get("/static/app.js")).text
    assert "cfh_dismiss_" in app_js and "localStorage" in app_js
