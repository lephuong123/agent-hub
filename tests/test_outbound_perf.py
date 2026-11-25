"""Gọi ra dịch vụ ngoài nhẹ hơn (docs/dev-notes/plan-api-caching.md — PR8).

- Slack/Telegram: một `AsyncClient` sống lâu cho mỗi đối tượng API (`PooledHttp`), bot giữ đối tượng tới khi token đổi; lỗi mạng
  vẫn gỡ token.
- Slack `users.info`: cache LRU + TTL dùng chung cho ngữ cảnh thread và ghép nối; người chưa có trong cache hỏi song song.
- `QueuedNotifier`: 429 → chờ theo `retry_after` / `Retry-After`.
- updater: ETag (`If-None-Match`), cache đĩa `HUB_HOME/update/latest.json`, singleflight.
- `account.claudeai_status {cached}`: trang Cài đặt không chạy `claude auth status` mỗi lần tải.

Không gọi mạng thật (httpx `MockTransport`, `urlopen` giả), không chạy `claude` thật (`_auth_info` giả).
"""

from __future__ import annotations

import asyncio
import io
import json
import threading
import time
import urllib.error
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any

import pytest

from cfharness import updater as U
from cfharness.chat.base import ChatBot
from cfharness.chat.slack import bot as slack_bot
from cfharness.chat.slack.api import SlackApi, SlackError
from cfharness.chat.slack.media import download
from cfharness.notify import base as nbase
from cfharness.notify.telegram import TelegramApi, TelegramError, _httpx
from m7_fake import close, make_daemon, rpc

BOT = "xoxb" + "-gia-token-" + "kiem-thu-0001"  # token giả (ghép chuỗi: tránh bộ quét secret)
TG = "123456789:AAH-secret-token-value_0123456789"


# ---------------------------------------------------------------- client http sống lâu
class Counting:
    """MockTransport đếm request + ghi lại client đã dùng."""

    def __init__(self, handler: Any) -> None:
        self.n = 0
        self.handler = handler
        self.transport = _httpx.MockTransport(self._h)

    def _h(self, req: Any) -> Any:
        self.n += 1
        return self.handler(req)


async def test_slack_api_reuses_one_client_and_closes():
    seen: list[str] = []

    def h(req):
        seen.append(req.headers.get("Authorization", ""))
        assert "cookie" not in {k.lower() for k in req.headers}  # không mang cookie giữa các lời gọi
        return _httpx.Response(200, json={"ok": True, "user_id": "U1"}, headers={"Set-Cookie": "b=1; Domain=slack.test; Path=/"})

    ct = Counting(h)
    api = SlackApi("https://slack.test/api", BOT, transport=ct.transport)
    await api.call("auth.test")
    c1 = api._client
    await api.call("auth.test")
    assert api._client is c1 and ct.n == 2 and seen == [f"Bearer {BOT}"] * 2
    await api.aclose()
    assert api._client is None and c1.is_closed
    await api.call("auth.test")  # đóng rồi vẫn dùng lại được (client mới)
    assert api._client is not None and api._client is not c1
    await api.aclose()


async def test_slack_api_network_error_scrubs_token():
    def h(req):
        raise _httpx.ConnectError(f"không kết nối được tới {req.url} với {BOT}")

    api = SlackApi("https://slack.test/api", BOT, transport=_httpx.MockTransport(h))
    with pytest.raises(SlackError) as ei:
        await api.call("auth.test")
    assert BOT not in str(ei.value) and "auth.test" in str(ei.value)
    await api.aclose()


async def test_slack_api_429_retry_after():
    api = SlackApi("https://slack.test/api", BOT,
                   transport=_httpx.MockTransport(lambda r: _httpx.Response(429, headers={"Retry-After": "7"})))
    with pytest.raises(SlackError) as ei:
        await api.call("chat.postMessage", {"channel": "D1", "text": "x"})
    assert ei.value.retry_after == 7.0 and nbase._retry_after(ei.value) == 7.0
    await api.aclose()


async def test_slack_media_download_uses_pooled_client():
    def h(req):
        assert req.headers["Authorization"] == f"Bearer {BOT}"
        return _httpx.Response(200, content=b"hello", headers={"Content-Type": "text/plain"})

    ct = Counting(h)
    api = SlackApi("https://slack.test/api", BOT, transport=ct.transport)
    assert await download(api, "https://files.slack.test/files-pri/T-F1/a.txt", 100) == b"hello"
    c1 = api._client
    assert await download(api, "https://files.slack.test/files-pri/T-F1/a.txt", 100) == b"hello"
    assert api._client is c1 and ct.n == 2
    with pytest.raises(SlackError, match="quá lớn"):
        await download(api, "https://files.slack.test/files-pri/T-F1/a.txt", 2)
    await api.aclose()


async def test_slack_media_download_does_not_follow_redirect():
    api = SlackApi("https://slack.test/api", BOT,
                   transport=_httpx.MockTransport(lambda r: _httpx.Response(302, headers={"Location": "https://evil.test/x"})))
    with pytest.raises(SlackError, match="HTTP 302"):
        await download(api, "https://files.slack.test/files-pri/T-F1/a.txt", 100)
    await api.aclose()


async def test_telegram_api_reuses_client_and_scrubs():
    ct = Counting(lambda r: _httpx.Response(200, json={"ok": True, "result": {"id": 1, "username": "b"}}))
    api = TelegramApi("https://tg.test", TG, transport=ct.transport)
    assert (await api.call("getMe"))["username"] == "b"
    c1 = api._client
    await api.call("sendMessage", {"chat_id": 1, "text": "x"})
    assert api._client is c1 and ct.n == 2
    await api.aclose()

    def boom(req):
        raise _httpx.ReadTimeout(f"timeout {req.url}")  # URL chứa token

    bad = TelegramApi("https://tg.test", TG, transport=_httpx.MockTransport(boom))
    with pytest.raises(TelegramError) as ei:
        await bad.call("getUpdates", timeout=1)
    assert TG not in str(ei.value)
    await bad.aclose()


async def test_telegram_download_pooled():
    def h(req):
        if req.url.path.endswith("/getFile"):
            return _httpx.Response(200, json={"ok": True, "result": {"file_path": "docs/a.txt", "file_size": 3}})
        assert req.url.path == f"/file/bot{TG}/docs/a.txt"
        return _httpx.Response(200, content=b"abc")

    ct = Counting(h)
    api = TelegramApi("https://tg.test", TG, transport=ct.transport)
    assert await api.download("F1", 100) == b"abc"
    assert ct.n == 2 and api._client is not None
    await api.aclose()


class _Holder:
    """Phần `ChatBot` giữ đối tượng API (không dựng cả bot)."""

    NAME = "t"
    _api_cache = None
    _cached_api = ChatBot._cached_api
    _close_api = ChatBot._close_api
    stop_api = ChatBot.stop


async def test_chatbot_cached_api_recreated_on_token_change():
    h = _Holder()
    made: list[SlackApi] = []

    def make(tok):
        def f():
            a = SlackApi("https://slack.test/api", tok, transport=_httpx.MockTransport(lambda r: _httpx.Response(200, json={"ok": True})))
            made.append(a)
            return a
        return f

    a1 = h._cached_api(("b", BOT, 15.0), make(BOT))
    assert h._cached_api(("b", BOT, 15.0), make(BOT)) is a1 and len(made) == 1
    await a1.call("auth.test")
    c1 = a1._client
    assert BOT not in repr(h._api_cache)  # khoá là sha256, không giữ token dạng rõ
    a2 = h._cached_api(("b", BOT + "x", 15.0), make(BOT + "x"))  # token đổi → đối tượng mới, client cũ đóng ở nền
    assert a2 is not a1 and len(made) == 2
    for _ in range(20):
        if c1.is_closed:
            break
        await asyncio.sleep(0.01)
    assert c1.is_closed


async def test_pooled_client_new_per_event_loop():
    api = SlackApi("https://slack.test/api", BOT, transport=_httpx.MockTransport(lambda r: _httpx.Response(200, json={"ok": True})))
    await api.call("auth.test")
    c1 = api._client

    def other_loop():
        asyncio.run(api.call("auth.test"))

    t = threading.Thread(target=other_loop)
    t.start()
    t.join()
    assert api._client is not c1  # client gắn loop khác → tạo mới, không dùng chéo loop
    await api.aclose()  # client của loop khác: chỉ bỏ, không lỗi
    await c1.aclose()


# ---------------------------------------------------------------- Slack users.info
class _NamesBot:
    _names: OrderedDict

    def __init__(self) -> None:
        self._names = OrderedDict()
        self.pairing = {"pending": {"D1": {"username": None, "name": None}}}

    _cached_user = slack_bot.SlackBot._cached_user
    _user_info = slack_bot.SlackBot._user_info
    _user_name = slack_bot.SlackBot._user_name
    _fill_name = slack_bot.SlackBot._fill_name


class _FakeUsers:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.active = 0
        self.peak = 0
        self.fail = fail or set()

    async def call(self, method, params=None, **kw):
        assert method == "users.info"
        uid = params["user"]
        self.calls.append(uid)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.02)
            if uid in self.fail:
                raise SlackError("users.info: user_not_found")
            return {"user": {"name": f"h_{uid}", "real_name": f"Real {uid}", "profile": {"display_name": f"D {uid}", "real_name": f"Real {uid}"}}}
        finally:
            self.active -= 1


async def test_user_names_cached_shared_with_fill_name(monkeypatch):
    b, api = _NamesBot(), _FakeUsers()
    assert await b._user_name(api, "U1") == "D U1"
    await b._fill_name(api, "D1", "U1")  # ghép nối dùng chung cache — không gọi users.info lần nữa
    assert api.calls == ["U1"]
    assert b.pairing["pending"]["D1"] == {"username": "h_U1", "name": "Real U1"}


async def test_user_names_lru_ttl_and_error(monkeypatch):
    monkeypatch.setattr(slack_bot, "NAMES_MAX", 2)
    b, api = _NamesBot(), _FakeUsers(fail={"UX"})
    for u in ("U1", "U2"):
        await b._user_name(api, u)
    await b._user_name(api, "U1")  # U1 mới dùng → U2 là cũ nhất
    await b._user_name(api, "U3")
    assert list(b._names) == ["U1", "U3"] and api.calls == ["U1", "U2", "U3"]
    assert await b._user_name(api, "UX") == "UX"  # lỗi → uid, nhớ ngắn hạn
    exp_err = b._names["UX"][0]
    assert exp_err - time.monotonic() <= slack_bot.NAMES_ERR_TTL_S + 1
    await b._user_name(api, "UX")
    assert api.calls.count("UX") == 1
    b._names["UX"] = (time.monotonic() - 1, b._names["UX"][1])  # hết hạn → hỏi lại
    await b._user_name(api, "UX")
    assert api.calls.count("UX") == 2
    await b._fill_name(api, "D1", "UX")  # lỗi → không ghi đè tên ghép nối
    assert b.pairing["pending"]["D1"] == {"username": None, "name": None}


async def test_thread_context_looks_up_users_concurrently():
    from cfharness.chat.slack.bot import SlackBot

    msgs = [{"ts": f"10{i}.0", "user": f"U{i}", "text": f"tin {i}"} for i in range(6)]

    class Api(_FakeUsers):
        async def call(self, method, params=None, **kw):
            if method == "conversations.replies":
                return {"messages": msgs}
            return await super().call(method, params, **kw)

    b = _NamesBot()
    b.info = {"bot_user_id": "UBOT"}
    api = Api()
    out = await SlackBot._thread_context(b, api, "C1", "100.0", "999.0")
    assert sorted(api.calls) == [f"U{i}" for i in range(6)] and api.peak > 1
    assert "D U3 (U3): tin 3" in out
    api.calls.clear()
    await SlackBot._thread_context(b, api, "C1", "100.0", "999.0")
    assert api.calls == []  # lần sau: cache


# ---------------------------------------------------------------- QueuedNotifier tôn trọng Retry-After
class _Rate(Exception):
    def __init__(self, ra):
        super().__init__("429")
        self.retry_after = ra


def test_retry_after_parsing():
    assert nbase._retry_after(_Rate(5)) == 5.0 and nbase._retry_after(_Rate("3")) == 3.0
    assert nbase._retry_after(_Rate(None)) is None and nbase._retry_after(_Rate("Wed, 21 Oct")) is None
    assert nbase._retry_after(RuntimeError("x")) is None
    hdr = urllib.error.HTTPError("https://hooks.slack.test/x", 429, "Too Many", {"Retry-After": "12"}, io.BytesIO(b""))
    assert nbase._retry_after(hdr) == 12.0
    resp = SimpleNamespace(status_code=429, headers={"Retry-After": "4"})
    assert nbase._retry_after(SimpleNamespace(response=resp)) == 4.0


async def test_queued_notifier_waits_retry_after(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(nbase.asyncio, "sleep", fake_sleep)
    published: list[Any] = []
    hub = SimpleNamespace(config=SimpleNamespace(get=lambda k: {"max_attempts": 3, "backoff_base_s": 1}),
                          publish=published.append, store=None)

    class N(nbase.QueuedNotifier):
        channel = "x"
        tries = 0

        def target(self):
            return {"ok": True}

        async def _send(self, target, ev, msg):
            N.tries += 1
            if N.tries == 1:
                raise _Rate(30)
            if N.tries == 2:
                raise _Rate(10_000)  # trần RETRY_AFTER_MAX_S
            return {}

    ev = SimpleNamespace(payload={}, agent_id=None, run_id=None, type="approval.open")
    await N(hub)._deliver(ev, "msg")
    assert sleeps == [30.0, nbase.RETRY_AFTER_MAX_S] and N.tries == 3
    failed = [e for e in published if e.type == "notify.failed"]
    assert [e.payload["retry_in_s"] for e in failed] == [30.0, nbase.RETRY_AFTER_MAX_S]


# ---------------------------------------------------------------- updater: ETag + cache đĩa + singleflight
class _Resp:
    def __init__(self, body: Any, etag: str | None) -> None:
        self._b = json.dumps(body).encode()
        self.headers = {"ETag": etag} if etag else {}

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_github(monkeypatch, tag="v99.0.0"):
    calls: list[dict[str, str]] = []
    lock = threading.Lock()

    def urlopen(req, timeout=None):
        with lock:
            calls.append({"url": req.full_url, "inm": req.get_header("If-none-match") or ""})
        time.sleep(0.05)
        assert req.full_url.endswith("/releases/latest")
        if req.get_header("If-none-match") == '"e1"':
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, io.BytesIO(b""))
        return _Resp({"tag_name": tag, "name": tag, "published_at": None, "html_url": "https://github.test/r"}, '"e1"')

    monkeypatch.setattr(U.urllib.request, "urlopen", urlopen)
    return calls


def test_updater_etag_disk_cache_and_singleflight(hub_home, monkeypatch):
    calls = _fake_github(monkeypatch)
    up = U.Updater(hub_home)
    res: list[dict] = []
    ts = [threading.Thread(target=lambda: res.append(up.latest())) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(calls) == 1 and all(r["tag"] == "v99.0.0" for r in res)  # singleflight
    disk = json.loads(up.latest_path.read_text(encoding="utf-8"))
    assert disk["result"]["tag"] == "v99.0.0" and next(iter(disk["http"].values()))["etag"] == '"e1"'

    up2 = U.Updater(hub_home)  # daemon khởi động lại trong TTL → đọc đĩa, không hỏi GitHub
    assert up2.latest()["tag"] == "v99.0.0" and len(calls) == 1
    r = up2.latest(refresh=True)  # bấm "Kiểm tra lại" → hỏi kèm If-None-Match, 304 → kết quả cũ
    assert r["ok"] and r["tag"] == "v99.0.0" and len(calls) == 2 and calls[-1]["inm"] == '"e1"'

    d = json.loads(up.latest_path.read_text(encoding="utf-8"))
    d["ts"] = time.time() - U.CHECK_TTL_S - 5  # cache đĩa hết hạn → hỏi lại (vẫn có ETag)
    up.latest_path.write_text(json.dumps(d), encoding="utf-8")
    up3 = U.Updater(hub_home)
    assert up3.latest()["tag"] == "v99.0.0" and len(calls) == 3 and calls[-1]["inm"] == '"e1"'


def test_updater_errors_not_persisted(hub_home, monkeypatch):
    def urlopen(req, timeout=None):
        raise urllib.error.URLError("mạng hỏng")

    monkeypatch.setattr(U.urllib.request, "urlopen", urlopen)
    up = U.Updater(hub_home)
    r = up.latest()
    assert not r["ok"] and "mạng hỏng" in r["error"] and not up.latest_path.exists()
    assert up.latest() is r  # lỗi vẫn cache RAM (không hỏi lại mỗi lần tải trang)


def test_updater_injected_fetch_skips_disk(hub_home):
    up = U.Updater(hub_home, fetch=lambda t: {"tag": "v1.0.0", "name": "v1.0.0", "published_at": None, "url": "u"})
    assert up.latest()["tag"] == "v1.0.0" and not up.latest_path.exists()


# ---------------------------------------------------------------- account.claudeai_status {cached}
@pytest.fixture
async def d(hub_home, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    dm = await make_daemon(hub_home)
    probes: list[tuple] = []
    state = {"logged_in": True}

    async def fake_auth_info(env, *args):
        probes.append(args)
        await asyncio.sleep(0)
        if args and args[0] == "logout":
            state["logged_in"] = False
            return {}, ""
        if state["logged_in"]:
            return {"loggedIn": True, "authMethod": "claude.ai", "email": "u@example.com", "orgName": "Org"}, ""
        return {"loggedIn": False}, ""

    monkeypatch.setattr(dm.hub.accounts, "_auth_info", fake_auth_info)
    dm.probes, dm.cai_state = probes, state  # type: ignore[attr-defined]
    yield dm
    await dm.hub.accounts.close_all_logins()
    await close(dm)


async def _settle(dm) -> None:
    for _ in range(50):
        if not any(not t.done() for t in dm.hub.accounts._cai_bg.values()):
            return
        await asyncio.sleep(0.01)


async def test_claudeai_status_cached_no_probe_when_fresh(d):
    st = await rpc(d, "account.claudeai_status", {"cached": True})  # chưa có cache → probe ngay
    assert st["logged_in"] is True and st["email"] == "u@example.com" and "cached" not in st and len(d.probes) == 1
    for _ in range(3):
        st = await rpc(d, "account.claudeai_status", {"cached": True})
    assert st["cached"] is True and st["logged_in"] is True and st["email"] == "u@example.com" and len(d.probes) == 1
    assert "machine" in st["accounts"]
    await rpc(d, "account.claudeai_status", {})  # không cached (CLI) → probe như cũ
    assert len(d.probes) == 2


async def test_claudeai_status_cached_stale_refreshes_in_background(d):
    await rpc(d, "account.claudeai_status", {"cached": True})
    d.cai_state["logged_in"] = False  # đăng xuất ngoài hub
    acc = d.hub.accounts
    acc.claudeai_ttl_s = 0.0
    st = await rpc(d, "account.claudeai_status", {"cached": True})
    assert st["cached"] is True and st["logged_in"] is True  # trả ngay bản cũ, không chờ claude
    st2 = await rpc(d, "account.claudeai_status", {"cached": True})  # lượt nền đang chạy → không mở thêm
    await _settle(d)
    assert len(d.probes) == 2
    acc.claudeai_ttl_s = 600.0
    st = await rpc(d, "account.claudeai_status", {"cached": True})
    assert st["logged_in"] is False and st2["cached"] is True and len(d.probes) == 2


async def test_claudeai_logout_and_remove_refresh_cache(d):
    acc = d.hub.accounts
    await rpc(d, "account.claudeai_status", {"cached": True})
    st = await rpc(d, "account.claudeai_logout", {})  # đăng xuất → probe thật, cache cập nhật
    assert st["logged_in"] is False
    assert (await rpc(d, "account.claudeai_status", {"cached": True}))["logged_in"] is False

    aid = acc.add_token("A", "sk-ant-oat01-" + "Aa1_" * 20)["id"]
    acc._cai_cache_set(aid, True, "a@example.com")
    acc.claudeai_ttl_s = 0.0
    st = await rpc(d, "account.claudeai_status", {"id": aid, "cached": True})
    assert st["cached"] is True
    acc.remove(aid)  # xoá tài khoản → bỏ cache + huỷ lượt nền; lượt nền muộn không ghi lại
    await _settle(d)
    assert aid not in acc._cai_cache() and aid not in acc._cai_bg
