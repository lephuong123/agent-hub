"""Thiết bị tin cậy cho đăng nhập web TOTP + báo Telegram vào/ra web (docs/dev-notes/plan-trusted-device.md, web-public.md).

Daemon IN-PROCESS + FakeRuntime, app Starlette qua ASGITransport (peer 127.0.0.1 như tunnel cục bộ). Telegram: server Bot API GIẢ
(`tests/telegram_fake.py`). Không gọi `claude`, không mạng ngoài.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import time
from pathlib import Path

import httpx2 as httpx
import pytest
import yaml

from cfharness import auth
from cfharness.daemon import Daemon
from cfharness.runtime import FakeRuntime
from cfharness.web.app import create_app
from cfharness.web.devices import DeviceStore, settings, ua_label
from cfharness.web.public import LoginLimiter
from cfharness.web.security import WebAuth, cookie_name
from cfharness.web.totp import TotpStore, totp

from telegram_fake import TOKEN, FakeTelegram

ISOLATED = Path(__file__).parent / "fixtures" / "claude-hub-isolated-run.jsonl"
PORT = 8765
ORIGIN = "https://an.example.com"
LOCAL = f"http://cfharness.localhost:{PORT}"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
UA_PHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
HDR = {"origin": ORIGIN, "user-agent": UA}


class Env:
    def __init__(self, d: Daemon, paths) -> None:
        self.d, self.paths, self.hub = d, paths, d.hub
        self.clients: list[httpx.AsyncClient] = []
        self.events: list = []
        d.hub.bus.add_listener(lambda ev: self.events.append(ev) if ev.type.startswith("web.") else None)

    def app(self, *, public: str | None = ORIGIN, limiter: LoginLimiter | None = None, devices: dict | None = None):
        return create_app(self.paths, auth=WebAuth(), port=PORT, public_origin=public, limiter=limiter, trusted_devices=devices)

    def client(self, app, base: str = ORIGIN) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base)
        self.clients.append(c)
        return c

    def audit(self, kind: str) -> list[dict]:
        rows = self.hub.store.conn.execute("SELECT kind, actor, channel, data FROM audit_log WHERE kind=? ORDER BY id", (kind,)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows]

    def kinds(self) -> list[str]:
        return [e.type for e in self.events]


@pytest.fixture
async def env(hub_home):
    d = Daemon(hub_home, runtime=FakeRuntime(ISOLATED))
    task = asyncio.create_task(d.serve())
    for _ in range(200):
        if hub_home.socket.exists():
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    e = Env(d, hub_home)
    yield e
    for c in e.clients:
        await c.aclose()
    d.stopping.set()
    try:
        await asyncio.wait_for(task, 20)
    except Exception:
        pass


async def until(pred, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        v = pred()
        if v:
            return v
        await asyncio.sleep(0.05)
    raise AssertionError("hết giờ chờ điều kiện")


def code_of(paths) -> str:
    return totp(TotpStore(paths)._read("totp.json")["secret"])


def dev_cookie(paths) -> str:
    return f"__Host-{cookie_name(paths.home)}_dev"


def set_cookie_of(r, name: str) -> str:
    for v in r.headers.get_list("set-cookie"):
        if v.startswith(name + "="):
            return v
    return ""


async def trusted_login(e: Env, app, c, ua: str = UA):
    """Đăng nhập TOTP có tick "Tin cậy thiết bị này" → (response, token thiết bị)."""
    r = await c.post("/login", data={"code": code_of(e.paths), "trust": "1"}, headers={**HDR, "user-agent": ua})
    assert r.status_code == 303, r.text[:300]
    sc = set_cookie_of(r, dev_cookie(e.paths))
    assert sc, r.headers.get_list("set-cookie")
    return r, sc.split(";", 1)[0].split("=", 1)[1]


def browser(e: Env, app, token: str):
    """Trình duyệt mới chỉ có cookie thiết bị (vd mở lại sau khi phiên hết hạn)."""
    c = e.client(app)
    c.cookies.set(dev_cookie(e.paths), token, domain="an.example.com", path="/")
    return c


def session_of(app, c, paths):
    sid = c.cookies.get(cookie_name(paths.home))
    return app.state.web.auth.sessions.get(sid) if sid else None


async def do(app, c, paths, name: str, data: dict, *, csrf=True, key=True):
    s = session_of(app, c, paths)
    body = {**data}
    if csrf:
        body["csrf"] = s.csrf
    if key:
        body["k"] = s.key
    return await c.post(f"/do/{name}", data=body, headers=HDR)


# ============================================================================ kho (thuần)
def test_store_hash_only_private_file(hub_home):
    TotpStore(hub_home).enroll(activate=True)
    ds = DeviceStore(hub_home)
    tok, pub, ev = ds.trust(ip="203.0.113.5", ua=UA)
    did, sec = tok.split(".", 1)
    raw = ds.path.read_text()
    assert sec not in raw and tok not in raw and did in raw and not ev
    if sys.platform != "win32":
        assert (ds.path.stat().st_mode & 0o777) == 0o600 and (ds.path.parent.stat().st_mode & 0o777) == 0o700
    assert ds.valid(did) and pub["ua_label"] == "Chrome · macOS"
    rows = ds.list()
    assert rows[0]["did"] == did and not any("sha" in k for k in rows[0])
    assert ds.redeem_and_rotate(did + "." + secrets.token_urlsafe(32), ip="1.1.1.1", ua=UA)[0] == "bad_secret"
    assert ds.redeem_and_rotate("x.y", ip="1.1.1.1", ua=UA)[0] == "malformed"
    assert ds.redeem_and_rotate("AAAAAAAAAAAA." + sec, ip="1.1.1.1", ua=UA)[0] == "unknown"


def test_rotation_grace_and_reuse_detection(hub_home):
    TotpStore(hub_home).enroll(activate=True)
    ds = DeviceStore(hub_home, {"rotate_grace_s": 60})
    t0 = 1_800_000_000.0
    tok0, pub, _ = ds.trust(ip="1.1.1.1", ua=UA, now=t0)
    st, tok1, _ = ds.redeem_and_rotate(tok0, ip="1.1.1.1", ua=UA, now=t0 + 10)
    assert st == "ok" and tok1 != tok0
    st, tok2, _ = ds.redeem_and_rotate(tok0, ip="1.1.1.1", ua=UA, now=t0 + 30)  # tab thứ hai trong cửa sổ grace
    assert st == "ok" and tok2
    st, _, p = ds.redeem_and_rotate(tok0, ip="6.6.6.6", ua=UA, now=t0 + 200)  # token cũ sau grace → nghi bị đánh cắp
    assert st == "reuse" and p["did"] == pub["did"]
    assert not ds.valid(pub["did"], now=t0 + 201)
    assert ds.redeem_and_rotate(tok2, ip="1.1.1.1", ua=UA, now=t0 + 202)[0] == "unknown"  # thiết bị đã bị thu hồi


def test_absolute_expiry_no_sliding(hub_home):
    TotpStore(hub_home).enroll(activate=True)
    ds = DeviceStore(hub_home, {"ttl_days": 30})
    t0 = 1_800_000_000.0
    tok, pub, _ = ds.trust(ip="1.1.1.1", ua=UA, now=t0)
    for day in (5, 15, 29):  # dùng thường xuyên KHÔNG kéo dài hạn
        st, tok, p = ds.redeem_and_rotate(tok, ip="1.1.1.1", ua=UA, now=t0 + day * 86400)
        assert st == "ok" and p["expires_at"] == pub["expires_at"]
    assert ds.redeem_and_rotate(tok, ip="1.1.1.1", ua=UA, now=t0 + 30 * 86400 + 1)[0] == "expired"
    assert settings({"ttl_days": 365})["ttl_days"] == 90 and settings({"max_devices": 99})["max_devices"] == 20


def test_totp_change_revokes_all_devices(hub_home):
    ts = TotpStore(hub_home)
    ts.enroll(activate=True)
    ds = DeviceStore(hub_home)
    tok, pub, _ = ds.trust(ip="1.1.1.1", ua=UA)
    ts.enroll(activate=True)  # đăng ký lại → xoá file thiết bị
    assert ts.devices_forgotten == 1 and not ds.valid(pub["did"]) and ds.list() == []
    tok, pub, _ = ds.trust(ip="1.1.1.1", ua=UA)
    # lớp dự phòng: bí mật đổi KHÔNG qua TotpStore (file thiết bị còn) → totp_fp lệch → vô hiệu
    a = ts._read("totp.json")
    from cfharness.web.totp import new_secret

    ts._write("totp.json", {**a, "secret": new_secret()})
    assert not ds.valid(pub["did"]) and ds.redeem_and_rotate(tok, ip="1.1.1.1", ua=UA)[0] == "totp_changed"
    ds.trust(ip="1.1.1.1", ua=UA)
    assert ts.disable() and ts.devices_forgotten == 1 and ds.list() == []
    with pytest.raises(ValueError):
        ds.trust(ip="1.1.1.1", ua=UA)  # TOTP tắt → không tin cậy được


def test_max_devices_evicts_least_recent(hub_home):
    TotpStore(hub_home).enroll(activate=True)
    ds = DeviceStore(hub_home, {"max_devices": 2})
    t0 = 1_800_000_000.0
    a, pa, _ = ds.trust(ip="1.1.1.1", ua=UA, now=t0)
    b, pb, _ = ds.trust(ip="1.1.1.1", ua=UA, now=t0 + 1)
    ds.redeem_and_rotate(a, ip="1.1.1.1", ua=UA, now=t0 + 2)  # a mới dùng → b lâu không dùng nhất
    _, pc, ev = ds.trust(ip="1.1.1.1", ua=UA, now=t0 + 3)
    assert ev == [pb["did"]] and {r["did"] for r in ds.list(now=t0 + 4)} == {pa["did"], pc["did"]}


def test_symlink_or_foreign_file_ignored(hub_home, tmp_path):
    TotpStore(hub_home).enroll(activate=True)
    ds = DeviceStore(hub_home)
    tok, pub, _ = ds.trust(ip="1.1.1.1", ua=UA)
    evil = tmp_path / "evil.json"
    evil.write_text(ds.path.read_text())
    ds.path.unlink()
    os.symlink(evil, ds.path)
    assert ds.list() == [] and not ds.valid(pub["did"]) and ds.redeem_and_rotate(tok, ip="1", ua=UA)[0] == "unknown"


def test_brute_force_random_tokens(hub_home):
    TotpStore(hub_home).enroll(activate=True)
    ds = DeviceStore(hub_home)
    tok, pub, _ = ds.trust(ip="1.1.1.1", ua=UA)
    did = pub["did"]
    for i in range(1000):
        guess = (did if i % 2 else secrets.token_urlsafe(9)) + "." + secrets.token_urlsafe(32)
        assert ds.redeem_and_rotate(guess, ip="6.6.6.6", ua=UA)[0] != "ok"
    assert ds.valid(did) and ds.redeem_and_rotate(tok, ip="1.1.1.1", ua=UA)[0] == "ok"  # thiết bị thật không bị ảnh hưởng


def test_ua_label_fixed_vocabulary():
    assert ua_label(UA) == "Chrome · macOS" and ua_label(UA_PHONE) == "Safari · iPhone"
    assert ua_label("<script>alert(1)</script>\n🚨 Hub bị hack") == "Trình duyệt khác"
    assert ua_label(None) == "Trình duyệt khác"


def test_config_validator_and_integrity(hub_home):
    from cfharness.config import HubConfig
    from cfharness.guard.integrity import SECURITY_KEYS

    cfg = HubConfig(hub_home.config)
    for k, v in (("web.trusted_devices.ttl_days", 91), ("web.trusted_devices.ttl_days", 0), ("web.trusted_devices.max_devices", 21),
                 ("web.trusted_devices.rotate_grace_s", 301), ("web.trusted_devices.enabled", "yes"), ("web.trusted_devices.lol", 1)):
        with pytest.raises(ValueError):
            cfg.set(k, v)
    assert cfg.set("web.trusted_devices.ttl_days", 30) == 30
    assert cfg.get("web.trusted_devices")["max_devices"] == 5 and cfg.get("web.trusted_devices")["enabled"] is True
    assert "web" in SECURITY_KEYS and "config.set" in auth.HUMAN_ONLY


def test_classifier_blocks_agent_access(hub_home):
    from cfharness.guard.classifier import GuardContext, classify
    from cfharness.guard.policy import PolicyStore

    ps = PolicyStore(hub_home.home)
    ps.ensure()
    ctx = GuardContext(hub_home.home / "agents", ps.load(), run_since=time.time(), hub_socket=str(hub_home.socket), git_probe=False)
    for cmd in ("cfharness web devices list", "cfharness web devices revoke-all", "cfharness web devices revoke abcdefghijkl"):
        assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd
    p = str(hub_home.home / "auth" / "trusted_devices.json")
    v = classify("Bash", {"command": f"cat {p}"}, ctx)
    assert v.forbidden or v.verdict != "safe"
    from cfharness.guard.classifier import SENSITIVE_HINT

    assert SENSITIVE_HINT.search("/x/trusted_devices.json")


# ============================================================================ HTTP
async def test_trust_cookie_attributes_and_file(env):
    TotpStore(env.paths).enroll(activate=True)
    app = env.app()
    c = env.client(app)
    r = await c.get("/login")
    assert 'name="trust"' in r.text and 'id="device-login"' in r.text and 'data-auto="1"' in r.text
    assert 'name="trust" value="1" checked' not in r.text  # mặc định KHÔNG tick (máy dùng chung)
    r, tok = await trusted_login(env, app, c)
    sc = set_cookie_of(r, dev_cookie(env.paths)).lower()
    assert sc.startswith("__host-") and "secure" in sc and "httponly" in sc and "samesite=strict" in sc and "path=/" in sc and "domain" not in sc
    assert 25 * 86400 < int(sc.split("max-age=")[1].split(";")[0]) <= 30 * 86400
    assert r.headers["location"].endswith("&d=1")
    assert tok.split(".", 1)[1] not in DeviceStore(env.paths).path.read_text()
    s = session_of(app, c, env.paths)
    assert s and s.device_id == tok.split(".")[0]
    await until(lambda: "web.device_trusted" in env.kinds() and "web.login" in env.kinds())
    ev = next(e for e in env.events if e.type == "web.login")
    assert ev.payload["method"] == "totp" and ev.payload["trusted"] is True and ev.payload["ua_label"] == "Chrome · macOS"
    assert env.audit("web.device_trusted")[0]["channel"] == "web"


async def test_local_host_never_trusts(env):
    TotpStore(env.paths).enroll(activate=True)
    app = env.app()
    c = env.client(app, base=LOCAL)
    r = await c.get("/login")
    assert 'name="trust"' not in r.text and 'id="device-login"' not in r.text
    r = await c.post("/login", data={"code": code_of(env.paths), "trust": "1"}, headers={"origin": LOCAL})
    assert r.status_code == 303 and not set_cookie_of(r, dev_cookie(env.paths)) and DeviceStore(env.paths).list() == []
    # cookie thiết bị THẬT gửi tới host cục bộ (dev server agent cùng host khác cổng) → không đổi được thành phiên
    tok, _, _ = DeviceStore(env.paths).trust(ip="1.1.1.1", ua=UA)
    c2 = env.client(app, base=LOCAL)
    c2.cookies.set(dev_cookie(env.paths), tok, domain="cfharness.localhost", path="/")
    r = await c2.post("/login", data={"device": "1"}, headers={"origin": LOCAL})
    assert r.status_code == 200 and not c2.cookies.get(cookie_name(env.paths.home))
    # public_origin cổng ≠ 443 (Funnel nhiều hub cùng host) → tắt mặc định
    app2 = env.app(public="https://an.example.com:8443")
    c3 = env.client(app2, base="https://an.example.com:8443")
    r = await c3.get("/login")
    assert r.status_code == 200 and 'name="trust"' not in r.text
    app3 = env.app(public="https://an.example.com:8443", devices={"allow_port": True})
    r = await env.client(app3, base="https://an.example.com:8443").get("/login")
    assert 'name="trust"' in r.text


async def test_device_login_rotates_and_reuse_revokes(env):
    TotpStore(env.paths).enroll(activate=True)
    app = env.app(devices={"rotate_grace_s": 0})
    _, tok0 = await trusted_login(env, app, env.client(app))
    b = browser(env, app, tok0)
    r = await b.post("/login", data={"device": "1"}, headers={**HDR, "user-agent": UA_PHONE})
    assert r.status_code == 303 and r.headers["location"].startswith("/#k=") and r.headers["location"].endswith("&d=1")
    tok1 = set_cookie_of(r, dev_cookie(env.paths)).split(";", 1)[0].split("=", 1)[1]
    assert tok1 != tok0 and session_of(app, b, env.paths).device_id == tok0.split(".")[0]
    assert (await b.get("/settings")).status_code == 200
    await until(lambda: any(e.type == "web.login" and e.payload.get("method") == "device" for e in env.events))
    assert env.audit("web.device_ua_changed")  # Chrome·macOS → Safari·iPhone: chỉ audit, không chặn
    await asyncio.sleep(0.01)
    # kẻ giữ token CŨ (đã xoay) → thu hồi thiết bị, phiên thật đang mở chết ngay
    thief = browser(env, app, tok0)
    r = await thief.post("/login", data={"device": "1"}, headers=HDR)
    assert r.status_code == 401 and not thief.cookies.get(cookie_name(env.paths.home))
    assert "max-age=0" in set_cookie_of(r, dev_cookie(env.paths)).lower() or 'expires=' in set_cookie_of(r, dev_cookie(env.paths)).lower()
    assert DeviceStore(env.paths).list() == []
    r = await b.get("/settings")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    await until(lambda: "web.device_reuse_detected" in env.kinds())
    assert env.audit("web.device_reuse_detected")[0]["actor"] == "anonymous"


async def test_bad_device_cookie_does_not_touch_limiter_and_works_when_locked(env):
    TotpStore(env.paths).enroll(activate=True)
    lim = LoginLimiter(ip_max_failures=2, global_max_failures=100)
    app = env.app(limiter=lim)
    _, tok = await trusted_login(env, app, env.client(app))
    did = tok.split(".")[0]
    for bad in ("garbage", "AAAAAAAAAAAA." + secrets.token_urlsafe(32), did + "." + secrets.token_urlsafe(32)):
        r = await browser(env, app, bad).post("/login", data={"device": "1"}, headers=HDR)
        assert r.status_code in (200, 401) and r.status_code != 303
    assert not lim._all and not lim._ip_until  # token sai không cộng vào bộ giới hạn mã
    assert len(env.audit("web.device_rejected")) <= 1  # giới hạn ghi theo IP
    # khoá do dò mã → người thật vẫn vào bằng thiết bị tin cậy
    for _ in range(2):
        await env.client(app).post("/login", data={"code": "000000" if code_of(env.paths) != "000000" else "111111"}, headers=HDR)
    assert lim.locked("127.0.0.1")
    r = await browser(env, app, tok).post("/login", data={"device": "1"}, headers=HDR)
    assert r.status_code == 303
    await until(lambda: "web.login_locked" in env.kinds())


async def test_revoke_from_settings_kills_session_and_needs_csrf_key(env):
    TotpStore(env.paths).enroll(activate=True)
    app = env.app()
    c = env.client(app)
    _, tok = await trusted_login(env, app, c)
    did = tok.split(".")[0]
    page = (await c.get("/settings")).text
    assert "Thiết bị tin cậy" in page and did in page and "thiết bị này" in page and "Đăng xuất và quên thiết bị này" in page
    assert tok.split(".", 1)[1] not in page
    assert (await do(app, c, env.paths, "device_revoke", {"did": did, "confirm": "yes"}, csrf=False)).status_code == 403
    assert (await do(app, c, env.paths, "device_revoke", {"did": did, "confirm": "yes"}, key=False)).status_code == 403
    r = await c.post("/do/device_revoke", data={"did": did, "confirm": "yes", "csrf": session_of(app, c, env.paths).csrf,
                                                 "k": session_of(app, c, env.paths).key}, headers={**HDR, "origin": "https://evil.example.com"})
    assert r.status_code == 403 and DeviceStore(env.paths).list()
    r = await do(app, c, env.paths, "device_revoke", {"did": did})  # chưa xác nhận → trang xác nhận, chưa thu hồi
    assert r.status_code == 200 and DeviceStore(env.paths).list()
    r = await do(app, c, env.paths, "device_revoke", {"did": did, "confirm": "yes"})
    assert r.status_code == 303 and DeviceStore(env.paths).list() == []
    assert (await c.get("/settings")).status_code == 303  # phiên tạo trên thiết bị đó chết ngay
    await until(lambda: "web.device_revoked" in env.kinds())
    assert (await browser(env, app, tok).post("/login", data={"device": "1"}, headers=HDR)).status_code == 401


async def test_revoke_outside_web_process_and_totp_disable_kill_session(env):
    TotpStore(env.paths).enroll(activate=True)
    app = env.app()
    c = env.client(app)
    _, tok = await trusted_login(env, app, c)
    assert (await c.get("/")).status_code == 200
    DeviceStore(env.paths).revoke(tok.split(".")[0])  # như `cfharness web devices revoke` (tiến trình khác)
    assert (await c.get("/")).status_code == 303
    # tắt TOTP → mọi thiết bị vô hiệu
    TotpStore(env.paths).enroll(activate=True)
    c2 = env.client(app)
    _, tok2 = await trusted_login(env, app, c2)
    assert (await c2.get("/")).status_code == 200
    TotpStore(env.paths).disable()
    assert (await c2.get("/")).status_code == 303
    assert (await browser(env, app, tok2).post("/login", data={"device": "1"}, headers=HDR)).status_code == 200  # tính năng tắt (TOTP tắt)


async def test_logout_keeps_device_and_forget_revokes(env):
    TotpStore(env.paths).enroll(activate=True)
    app = env.app()
    c = env.client(app)
    _, tok = await trusted_login(env, app, c)
    s = session_of(app, c, env.paths)
    r = await c.post("/logout", data={"csrf": s.csrf}, headers=HDR)
    assert r.status_code == 303 and not set_cookie_of(r, dev_cookie(env.paths)) and DeviceStore(env.paths).list()
    await until(lambda: any(e.type == "web.logout" and e.payload.get("forget") is False for e in env.events))
    b = browser(env, app, tok)
    r = await b.post("/login", data={"device": "1"}, headers=HDR)
    assert r.status_code == 303
    s = session_of(app, b, env.paths)
    r = await b.post("/logout", data={"csrf": s.csrf, "forget": "1"}, headers=HDR)
    assert r.status_code == 303 and DeviceStore(env.paths).list() == []
    sc = set_cookie_of(r, dev_cookie(env.paths)).lower()
    assert sc and ("max-age=0" in sc or "expires=" in sc)
    await until(lambda: any(e.type == "web.device_revoked" and e.payload.get("reason") == "logout_forget" for e in env.events))


async def test_host_and_origin_checks_on_device_login(env):
    TotpStore(env.paths).enroll(activate=True)
    app = env.app()
    _, tok = await trusted_login(env, app, env.client(app))
    b = browser(env, app, tok)
    r = await b.post("/login", data={"device": "1"}, headers={**HDR, "host": "evil.example.com"})
    assert r.status_code == 421
    r = await b.post("/login", data={"device": "1"}, headers={**HDR, "origin": "https://evil.example.com"})
    assert r.status_code == 403
    r = await b.post("/login", data={"device": "1"}, headers={**HDR, "sec-fetch-site": "cross-site"})
    assert r.status_code == 403
    assert DeviceStore(env.paths).list()  # các lần bị chặn không xoay/thu hồi


async def test_enroll_can_trust_device(env):
    store = TotpStore(env.paths)
    _, token = store.enroll()
    app = env.app()
    c = env.client(app)
    r = await c.post("/enroll", data={"t": token, "action": "show"}, headers=HDR)
    assert 'name="trust"' in r.text
    secret = store._read("totp_enroll.json")["secret"]
    r = await c.post("/enroll", data={"t": token, "action": "confirm", "code": totp(secret), "trust": "1"}, headers=HDR)
    assert r.status_code == 303 and set_cookie_of(r, dev_cookie(env.paths)) and len(DeviceStore(env.paths).list()) == 1


async def test_link_login_and_lock_notify(env):
    app = env.app()
    c = env.client(app, base=LOCAL)
    r = await c.get("/login?t=" + app.state.web.auth.mint(), headers={"user-agent": UA})
    assert r.status_code == 303 and "&d=1" not in r.headers["location"] and not set_cookie_of(r, dev_cookie(env.paths))
    await until(lambda: any(e.type == "web.login" and e.payload.get("method") == "link" for e in env.events))


# ============================================================================ RPC web.notify: chỉ người, lọc metadata
async def test_web_notify_human_only_and_sanitized(env):
    d = env.d
    assert "web.notify" in auth.HUMAN_ONLY and "web.notify" in auth.all_mutating()
    req = {"jsonrpc": "2.0", "id": 1, "method": "web.notify", "params": {"kind": "web.login", "data": {"method": "totp"}}}
    resp = await d._dispatch(req, via_socket=True, peer=None)  # tiến trình cục bộ KHÔNG có token người (agent trong run)
    assert "error" in resp and resp["error"]["data"]["reason"] == "human_only"
    assert not env.audit("web.login")
    resp = await d._dispatch({"jsonrpc": "2.0", "id": 2, "method": "web.notify", "params": {"kind": "run.completed", "data": {}}})
    assert "error" in resp
    secret_cookie = "s3cr3tCookieValue_" + secrets.token_urlsafe(8)
    resp = await d._dispatch({"jsonrpc": "2.0", "id": 3, "method": "web.notify", "params": {
        "kind": "web.login", "channel": "web",
        "data": {"method": "totp", "ip": "203.0.113.9", "ua_label": "Chrome · macOS\n\x1b[31m", "cookie": secret_cookie, "token": secret_cookie,
                 "k": secret_cookie, "did": "../../etc"}}})
    assert "result" in resp
    ev = await until(lambda: next((e for e in env.events if e.type == "web.login"), None))
    flat = json.dumps(ev.payload) + json.dumps(env.audit("web.login"))
    assert secret_cookie not in flat and "did" not in ev.payload and "\x1b" not in flat
    assert ev.payload["ip"] == "203.0.113.9" and ev.payload["method"] == "totp"


def test_format_texts_metadata_only():
    from cfharness.events import EVENT_LEVELS, make_event
    from cfharness.notify.slack import format_message
    from cfharness.notify.telegram import format_event
    from cfharness.web_events import KINDS

    for k in KINDS:
        assert k in EVENT_LEVELS
        ev = make_event(k, None, {"method": "device", "ip": "203.0.113.9", "ua_label": "Safari · iPhone", "did": "abcdefghijkl", "scope": "ip",
                                  "count": 3, "reason": "user", "token": "TOPSECRET"})
        t = format_event(ev)
        assert t and "TOPSECRET" not in t and "203.0.113.9" in t, k
        assert format_message(ev)
    sch = json.loads((Path(__file__).parents[1] / "docs" / "schema" / "event.v1.json").read_text())
    assert all(k in json.dumps(sch) for k in KINDS)


# ============================================================================ Telegram (server Bot API giả)
GROUP, BOSS = -1001234, 111


@pytest.fixture
def tg():
    f = FakeTelegram()
    yield f
    f.close()


async def _tg_daemon(hub_home, tg, allowed, **tgc):
    hub_home.config.write_text(yaml.safe_dump({"guard": {"git_probe": False}, "notify": {"telegram": {
        "api_base": tg.url, "poll_timeout_s": 1, "idle_check_s": 0.05, "backoff_base_s": 0.05,
        "allowed_chat_ids": [str(x) for x in allowed], **tgc}}}))
    d = Daemon(hub_home, runtime=FakeRuntime())
    d.hub.secrets.set("TELEGRAM_BOT_TOKEN", TOKEN)
    d.hub.start()
    tg.hubs.append(d.hub)
    return d


async def test_telegram_web_events_go_to_private_group(hub_home, tg):
    d = await _tg_daemon(hub_home, tg, (BOSS, GROUP), allow_groups=True, allowed_user_ids=[str(BOSS)])
    try:
        assert d.hub.telegram.security_chats() == [str(GROUP)]
        for kind, data in (("web.login", {"method": "device", "ip": "203.0.113.9", "ua_label": "Safari · iPhone", "did": "abcdefghijkl"}),
                           ("web.device_reuse_detected", {"did": "abcdefghijkl", "ip": "198.51.100.7", "ua_label": "Chrome · Windows"}),
                           ("web.login_locked", {"ip": "198.51.100.7", "scope": "all"})):
            resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "web.notify",
                                      "params": {"kind": kind, "data": {**data, "cookie": "COOKIEVAL"}}})
            assert "result" in resp
        msgs = await until(lambda: (lambda m: m if len(m) >= 3 else None)([x["text"] for x in tg.sent(GROUP)]))
        assert any("thiết bị tin cậy" in m and "Safari · iPhone" in m and "203.0.113.9" in m for m in msgs)
        assert any("DÙNG LẠI" in m for m in msgs) and any("TẠM KHOÁ" in m for m in msgs)
        assert not any("COOKIEVAL" in m for m in msgs)
        assert all("reply_markup" not in x for x in tg.sent(GROUP))  # không kèm nút
        assert not tg.sent(BOSS)  # vào nhóm riêng của hub, không rải vào chat riêng
    finally:
        await asyncio.wait_for(d.hub.stop(), 15)


async def test_telegram_web_events_fallback_private_chat(hub_home, tg):
    d = await _tg_daemon(hub_home, tg, (BOSS,))
    try:
        await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "web.notify", "params": {"kind": "web.logout", "data": {"ip": "203.0.113.9"}}})
        msgs = await until(lambda: [x["text"] for x in tg.sent(BOSS)])
        assert "Đăng xuất web" in msgs[0]
    finally:
        await asyncio.wait_for(d.hub.stop(), 15)
