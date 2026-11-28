"""Phần backend của "Kết nối Telegram & Slack qua web" (docs/dev-notes/plan-chat-setup-web.md PR2/PR3/PR5):
`slack.verify_bot`, `telegram.repair`, `notify.test{channels}`, người được phép có tên (`*.peers`), chẩn đoán (`chat/diagnose.py`).
Server Telegram/Slack GIẢ cục bộ, daemon IN-PROCESS — không gọi mạng thật."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
import yaml

from cfharness import auth
from cfharness.chat.diagnose import ACTIONS, STATES, diagnose
from cfharness.daemon import Daemon
from cfharness.runtime import FakeRuntime

from slack_fake import APP_ID, APP_TOKEN, BOT_TOKEN, TEAM, FakeSlack
from telegram_fake import TOKEN, FakeTelegram

TG_BOSS = 111
SL_BOSS, DM_BOSS = "U0BOSS001", "D0BOSS001"


async def until(fn, timeout: float = 10.0, step: float = 0.02):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = fn()
        if v:
            return v
        await asyncio.sleep(step)
    raise AssertionError("hết giờ chờ điều kiện")


@pytest.fixture
def tg():
    f = FakeTelegram()
    yield f
    f.close()


@pytest.fixture
def sk():
    f = FakeSlack()
    yield f
    f.close()


@pytest.fixture
async def make(hub_home, tg, sk):
    daemons: list[Daemon] = []

    async def _m(*, tg_token=False, sl_tokens=False, tg_cfg=None, sl_cfg=None):
        c = {"guard": {"git_probe": False},
             "notify": {"telegram": {"api_base": tg.url, "poll_timeout_s": 1, "idle_check_s": 0.05, "allowed_chat_ids": [], **(tg_cfg or {})},
                        "slack": {"api_base": sk.url, "idle_check_s": 0.05, "allowed_chat_ids": [], "allowed_user_ids": [], **(sl_cfg or {})}}}
        hub_home.config.write_text(yaml.safe_dump(c))
        d = Daemon(hub_home, runtime=FakeRuntime())
        if tg_token:
            d.hub.secrets.set("TELEGRAM_BOT_TOKEN", TOKEN)
        if sl_tokens:
            d.hub.secrets.set("SLACK_BOT_TOKEN", BOT_TOKEN)
            d.hub.secrets.set("SLACK_APP_TOKEN", APP_TOKEN)
        d.hub.start()
        daemons.append(d)
        tg.hubs.append(d.hub)
        sk.hubs.append(d.hub)
        return d

    yield _m
    for d in daemons:
        try:
            await asyncio.wait_for(d.hub.stop(), 15)
        except Exception:
            pass


async def call(d: Daemon, method: str, params: dict | None = None) -> dict:
    return await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


async def rpc(d: Daemon, method: str, params: dict | None = None):
    resp = await call(d, method, params)
    if "error" in resp:
        raise AssertionError(resp["error"])
    return resp["result"]


def audit_blob(d: Daemon) -> str:
    return str(d.hub.audit.query(limit=500))


# ============================================================================ slack.verify_bot
async def test_verify_bot_saves_token_app_and_links_then_setup_with_app_token_only(make, sk, caplog):
    caplog.set_level(logging.DEBUG)
    d = await make()
    r = await rpc(d, "slack.verify_bot", {"bot_token": BOT_TOKEN})
    assert r["app_id"] == APP_ID and r["team_id"] == TEAM and r["bot_user_id"] and r["team"] == "Thử nghiệm"
    assert r["links"] == {"app_general": f"https://api.slack.com/apps/{APP_ID}/general", "app_oauth": f"https://api.slack.com/apps/{APP_ID}/oauth",
                          "dm": f"https://slack.com/app_redirect?app={APP_ID}&team={TEAM}"}
    assert d.hub.secrets.get("SLACK_BOT_TOKEN") == BOT_TOKEN and d.hub.secrets.get("SLACK_APP_TOKEN") is None
    assert d.hub.store.state_get("slack.app")["app_id"] == APP_ID
    st = await rpc(d, "slack.status")
    assert st["app_id"] == APP_ID and st["links"]["app_general"].endswith("/general")
    assert st["diagnosis"]["state"] == "off"  # interactive chưa bật (chưa có app token / setup)
    # bước 3: chỉ app token — bot token lấy từ secret store
    s = await rpc(d, "slack.setup", {"app_token": APP_TOKEN})
    assert s["team_id"] == TEAM and s["pair_code"]
    await until(lambda: d.hub.slack.info.get("polling") and sk.connected())
    st = await rpc(d, "slack.status")
    assert st["diagnosis"]["state"] == "pairing" and st["diagnosis"]["action"] == "pair"
    blob = str(r) + str(s) + str(st) + audit_blob(d) + caplog.text
    assert BOT_TOKEN not in blob and APP_TOKEN not in blob
    assert [a["kind"] for a in d.hub.audit.query(kind="slack.verify_bot")]


async def test_verify_bot_rejects_bad_shape_wrong_workspace_and_strange_app_id(make, sk):
    d = await make(sl_cfg={"team_id": "T0OTHER99"})
    resp = await call(d, "slack.verify_bot", {"bot_token": APP_TOKEN})  # dán nhầm xapp
    assert "xoxb" in resp["error"]["message"]
    resp = await call(d, "slack.verify_bot", {"bot_token": BOT_TOKEN})
    assert "workspace khác" in resp["error"]["message"] and d.hub.secrets.get("SLACK_BOT_TOKEN") is None
    # dòng audit lỗi không mang token
    assert BOT_TOKEN not in audit_blob(d) and APP_TOKEN not in audit_blob(d)
    d.hub.config.set("notify.slack.team_id", None)
    sk.app_id = "A1/../evil?x=1"  # id lạ → không dựng URL
    r = await rpc(d, "slack.verify_bot", {"bot_token": BOT_TOKEN})
    assert r["app_id"] is None and r["links"] is None
    sk.fail_bots_info = True  # manifest cũ thiếu users:read → vẫn lưu token, không link
    r = await rpc(d, "slack.verify_bot", {"bot_token": BOT_TOKEN})
    assert r["app_id"] is None and d.hub.secrets.get("SLACK_BOT_TOKEN") == BOT_TOKEN


# ============================================================================ telegram.repair
async def test_telegram_repair_deletes_webhook_and_diagnoses_conflict(make, tg):
    d = await make(tg_token=True, tg_cfg={"allowed_chat_ids": [str(TG_BOSS)]})
    await until(lambda: d.hub.telegram.info.get("polling"))
    tg.webhook_url = "https://example.invalid/hook"
    await until(lambda: d.hub.telegram.info.get("last_error"))
    st = await rpc(d, "telegram.status")
    assert st["diagnosis"]["state"] == "error" and st["diagnosis"]["action"] == "repair_webhook"
    assert st["diagnosis"]["code"] == "webhook_active" and "Conflict" not in st["diagnosis"]["message_vi"]
    r = await rpc(d, "telegram.repair")
    assert r == {"ok": True, "had_webhook": True}
    assert tg.webhook_url == "" and tg.calls_of("deleteWebhook")[-1] == {"drop_pending_updates": False}
    await until(lambda: d.hub.telegram.info.get("polling") and not d.hub.telegram.info.get("last_error"))
    assert (await rpc(d, "telegram.repair"))["had_webhook"] is False
    assert TOKEN not in audit_blob(d) and [a for a in d.hub.audit.query(kind="telegram.repair")]


async def test_telegram_repair_without_token(make):
    d = await make()
    resp = await call(d, "telegram.repair")
    assert "chưa có token" in resp["error"]["message"]


# ============================================================================ peers
async def test_telegram_peers_recorded_on_allow_cleaned_and_forgotten(make, tg):
    d = await make(tg_token=True)
    r = await rpc(d, "telegram.setup", {})
    await until(lambda: tg.calls_of("getUpdates"))
    m = tg.user_message(TG_BOSS, f"/start {r['pair_code']}", username="sep_a")
    # tên người ngoài có ký tự điều khiển / xuống dòng / rất dài
    tg.updates[-1]["message"]["chat"]["first_name"] = "Nguyễn\x1b[31m A\n<script>" + "x" * 200
    await until(lambda: tg.processed(m))
    await until(lambda: d.hub.telegram.pending(r["pair_code"]))
    await rpc(d, "telegram.allow", {"chat_id": str(TG_BOSS)})
    st = await rpc(d, "telegram.status")
    [p] = st["peers"]
    assert p["chat_id"] == str(TG_BOSS) and p["user_id"] == str(TG_BOSS) and p["username"] == "sep_a" and p["at"]
    assert "\x1b" not in p["name"] and "\n" not in p["name"] and len(p["name"]) <= 60 and p["name"].startswith("Nguyễn")
    assert st["diagnosis"]["state"] in ("ok", "setup")
    await until(lambda: d.hub.telegram.info.get("polling"))
    assert (await rpc(d, "telegram.status"))["diagnosis"]["state"] == "ok"
    # thêm tay (không qua ghép nối) → mục không tên
    await rpc(d, "telegram.allow", {"chat_id": "222"})
    names = {p["chat_id"]: p["name"] for p in (await rpc(d, "telegram.status"))["peers"]}
    assert names["222"] is None and names[str(TG_BOSS)]
    await rpc(d, "telegram.revoke", {"chat_id": "222"})
    assert "222" not in d.hub.store.state_get("telegram.peers")
    await rpc(d, "telegram.disable", {"forget_token": True})
    assert d.hub.store.state_get("telegram.peers") == {}


async def test_slack_peers_named_from_users_info_and_cleared(make, sk):
    d = await make(sl_tokens=True, sl_cfg={"interactive": True, "team_id": TEAM})
    await until(lambda: d.hub.slack.info.get("polling") and sk.connected())
    d.hub.slack.pairing = None
    await rpc(d, "slack.allow", {"chat_id": DM_BOSS, "user_id": SL_BOSS})
    [p] = (await rpc(d, "slack.status"))["peers"]
    assert p == {"chat_id": DM_BOSS, "user_id": SL_BOSS, "name": "Sếp Thử", "username": "sep", "at": p["at"]}
    d.hub.store.state_set("slack.app", {"app_id": APP_ID, "team_id": TEAM})
    await rpc(d, "slack.revoke", {"chat_id": DM_BOSS})
    assert (await rpc(d, "slack.status"))["peers"] == [] and DM_BOSS not in d.hub.store.state_get("slack.peers")
    await rpc(d, "slack.allow", {"chat_id": DM_BOSS, "user_id": SL_BOSS})
    await rpc(d, "slack.disable", {"forget_token": True})
    assert d.hub.store.state_get("slack.peers") == {} and d.hub.store.state_get("slack.app") == {}
    st = await rpc(d, "slack.status")
    assert st["app_id"] is None and st["links"] is None and st["diagnosis"]["state"] == "off"


# ============================================================================ notify.test{channels}
async def test_notify_test_all_and_listed_channels(make, tg, sk):
    d = await make(tg_token=True, sl_tokens=True, tg_cfg={"allowed_chat_ids": [str(TG_BOSS)]},
                   sl_cfg={"interactive": True, "team_id": TEAM, "allowed_chat_ids": [DM_BOSS], "allowed_user_ids": [SL_BOSS]})
    await until(lambda: d.hub.slack.info.get("polling"))
    r = await rpc(d, "notify.test", {"channels": "all"})
    ch = r["channels"]
    assert r["sent"] is True and ch["telegram"]["sent"] is True and ch["slack"]["sent"] is True
    assert ch["slack_webhook"]["skipped"] and "nhường" in ch["slack_webhook"]["reason"]
    assert tg.sent(TG_BOSS) and sk.sent(DM_BOSS)
    r = await rpc(d, "notify.test", {"channels": ["telegram"], "text": "xin chào"})
    assert set(r["channels"]) == {"telegram"} and tg.sent(TG_BOSS)[-1]["text"] == "xin chào"
    resp = await call(d, "notify.test", {"channels": ["email"]})
    assert resp["error"]["code"] == -32602
    rows = d.hub.audit.query(kind="notify.test")
    assert rows and all(TOKEN not in str(x) and BOT_TOKEN not in str(x) for x in rows)


async def test_notify_test_legacy_shape_and_unconfigured(make):
    d = await make()
    r = await rpc(d, "notify.test", {})
    assert r["sent"] is False and "reason" in r and "channels" not in r  # CLI cũ: chỉ Slack một chiều
    r = await rpc(d, "notify.test", {"channels": "all"})
    assert r["sent"] is False and all(x.get("skipped") for x in r["channels"].values())
    r = await rpc(d, "notify.test", {"channels": ["telegram"]})  # nêu tên mà chưa cấu hình → lý do, không skipped
    assert r["channels"]["telegram"]["sent"] is False and not r["channels"]["telegram"].get("skipped")


async def test_notify_test_frozen_hub_sends_nothing(make, tg):
    d = await make(tg_token=True, tg_cfg={"allowed_chat_ids": [str(TG_BOSS)]})
    d.hub.store.state_set("migrated_to", {"at": time.time(), "export_id": "x"})
    r = await rpc(d, "notify.test", {"channels": "all"})
    assert r["sent"] is False and tg.sent(TG_BOSS) == []
    assert (await rpc(d, "notify.test", {}))["sent"] is False
    st = await rpc(d, "telegram.status")
    assert st["frozen"] is True and st["diagnosis"]["state"] == "off"


# ============================================================================ quyền
async def test_new_methods_human_only_and_audited(make):
    from cfharness.chat.rpc import MUTATING_TELEGRAM
    from cfharness.chat.slack.rpc import MUTATING_SLACK
    from cfharness.guard.classifier import HUB_HUMAN_ONLY_CLI

    for m in ("slack.verify_bot", "telegram.repair", "notify.test"):
        assert m in auth.HUMAN_ONLY and m not in auth.MANAGER_ALLOWED and m in auth.all_mutating(), m
    assert "slack.verify_bot" in MUTATING_SLACK and "telegram.repair" in MUTATING_TELEGRAM
    assert "test" in HUB_HUMAN_ONLY_CLI["notify"]
    d = await make(tg_token=True)
    agent = auth.Caller(via="socket", pid=1, token_ok=False, run_id="r_x", agent_id="a1", evidence=["env:CFHARNESS_RUN_ID"])
    for m, prm in (("slack.verify_bot", {"bot_token": BOT_TOKEN}), ("telegram.repair", {}), ("notify.test", {"channels": "all"})):
        resp = await d.call_as(agent, m, prm)
        assert resp.get("error", {}).get("code") == -32003, (m, resp)
    assert d.hub.secrets.get("SLACK_BOT_TOKEN") is None and BOT_TOKEN not in audit_blob(d)


# ============================================================================ chẩn đoán (thuần)
def _tg(**kw):
    base = {"configured": True, "enabled": True, "polling": True, "last_error": None, "allowed_chat_ids": ["1"], "pairing_active": False,
            "last_poll_at": 100.0}
    base.update(kw)
    return base


def _sl(**kw):
    base = {"configured": True, "enabled": True, "interactive": True, "app_token": True, "polling": True, "last_error": None,
            "allowed_chat_ids": ["D1"], "team_id": "T1", "token_team_id": "T1", "connected_at": 50.0, "last_poll_at": 70.0}
    base.update(kw)
    return base


@pytest.mark.parametrize("st,state,action,code", [
    ({"configured": False}, "off", "setup", None),
    (_tg(enabled=False), "off", "enable", "disabled"),
    (_tg(frozen=True), "off", None, "frozen"),
    (_tg(polling=False), "setup", None, "connecting"),
    (_tg(allowed_chat_ids=[], pairing_active=True), "pairing", "pair", None),
    (_tg(allowed_chat_ids=[]), "pairing", "pair", None),
    (_tg(), "ok", None, None),
    (_tg(polling=False, last_error="getUpdates: Unauthorized"), "error", "retoken", "unauthorized"),
    (_tg(polling=False, last_error="getMe: Not Found"), "error", "retoken", "unauthorized"),
    (_tg(polling=False, last_error="getUpdates: Conflict: can't use getUpdates method while webhook is active; use deleteWebhook to delete the webhook first"),
     "error", "repair_webhook", "webhook_active"),
    (_tg(polling=False, last_error="getUpdates: Conflict: terminated by other getUpdates request; make sure that only one bot instance is running"),
     "error", "other_instance", "other_instance"),
    (_tg(polling=False, last_error="getUpdates: Too Many Requests: retry after 5"), "error", "wait", "rate_limited"),
    (_tg(polling=False, last_error="getUpdates: ConnectError: [Errno 8] nodename nor servname provided"), "error", "check_network", "network"),
    (_tg(polling=False, last_error="getUpdates: HTTP 502 (không phải JSON)"), "error", "check_network", "network"),
    (_tg(polling=False, last_error="getUpdates: lạ"), "error", "reconnect", "unknown"),
])
def test_diagnose_telegram(st, state, action, code):
    out = diagnose("telegram", st)
    assert (out["state"], out["action"], out["code"]) == (state, action, code)
    assert out["state"] in STATES and out["action"] in ACTIONS and out["message_vi"]
    assert (st.get("last_error") or "§") not in out["message_vi"]  # chuỗi lỗi ngoài không chép vào câu cho người


@pytest.mark.parametrize("st,state,action,code", [
    ({"configured": False}, "off", "setup", None),
    (_sl(interactive=False), "off", "enable", "disabled"),
    (_sl(app_token=False, polling=False), "setup", "app_token", "missing_app_token"),
    (_sl(token_team_id="T2"), "error", "reset_team", "team_mismatch"),
    (_sl(polling=False, last_error="auth.test: invalid_auth"), "error", "retoken", "invalid_auth"),
    (_sl(polling=False, last_error="auth.test: token_revoked"), "error", "retoken", "invalid_auth"),
    (_sl(polling=False, last_error="apps.connections.open: invalid_auth"), "error", "app_token", "app_token_invalid"),
    (_sl(polling=False, last_error="apps.connections.open: not_allowed_token_type"), "error", "app_token", "wrong_token_type"),
    (_sl(polling=False, last_error="auth.test: not_allowed_token_type"), "error", "retoken", "wrong_token_type"),
    (_sl(polling=False, last_error="apps.connections.open: missing_scope"), "error", "app_token", "missing_scope"),
    (_sl(polling=False, last_error="apps.connections.open: app_not_socket_mode"), "error", "app_token", "socket_mode"),
    (_sl(polling=False, last_error="auth.test: bị giới hạn tốc độ (429)"), "error", "wait", "rate_limited"),
    (_sl(polling=False, last_error="apps.connections.open: ConnectTimeout: timed out"), "error", "check_network", "network"),
    (_sl(allowed_chat_ids=[]), "pairing", "pair", None),
    (_sl(), "ok", None, None),
])
def test_diagnose_slack(st, state, action, code):
    out = diagnose("slack", st)
    assert (out["state"], out["action"], out["code"]) == (state, action, code)


def test_diagnose_last_contact_and_bad_kind():
    assert diagnose("slack", _sl())["last_contact"] == 70.0
    assert diagnose("telegram", _tg(last_poll_at=None))["last_contact"] is None
    with pytest.raises(ValueError):
        diagnose("email", {})
