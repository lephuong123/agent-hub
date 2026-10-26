"""OAuth 2.1 cho MCP server từ xa (docs/dev-notes/mcp-oauth.md) — AS giả + MCP được bảo vệ thật trên 127.0.0.1 (tests/oauth_fake.py).

Kiểm: discovery (401 → PRM → AS metadata), DCR, PKCE S256, `resource`, state một lần / sai / hết hạn, iss (RFC 9207), refresh,
chèn Bearer vào mcp.json runtime (0600, xoá sau run), run fail rõ khi chưa kết nối, probe dùng token, ngắt kết nối + thu hồi,
không lộ token/code vào hub.db/log/audit/event, luồng web (connect → callback công khai) và CLI login (listener loopback).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx
import pytest

from cfharness.errors import HubError
from cfharness.hub import Hub
from cfharness.mcp_client import build_agent_mcp_servers
from cfharness.mcp_client.oauth import bearer_for, load_record, pkce_pair, require_secure, secret_name
from cfharness.runtime import FakeRuntime
from oauth_fake import FakeOAuth

WIN = sys.platform == "win32"


@pytest.fixture
def fake():
    f = FakeOAuth().start()
    yield f
    f.stop()


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home, runtime=FakeRuntime())
    h.start()
    yield h
    h.store.close()


def add_server(hub, fake, name="notes", **auth):
    return hub.mcp.add({"name": name, "transport": "http", "url": fake.mcp_url, "auth": {"type": "oauth", **auth}})


def authorize(url: str) -> dict[str, str]:
    """Trình duyệt giả: mở authorize_url (AS giả tự "cho phép") → đọc Location về redirect_uri."""
    r = httpx.get(url, follow_redirects=False, timeout=10)
    assert r.status_code == 302, r.text
    loc = r.headers["location"]
    return {k: v[0] for k, v in parse_qs(urlsplit(loc).query).items()} | {"_redirect": loc.split("?", 1)[0]}


async def connect(hub, fake, name="notes", redirect="http://cfharness.localhost:8765/mcp/oauth/callback") -> dict:
    st = await hub.mcp_oauth.start(name, redirect)
    cb = await asyncio.to_thread(authorize, st["authorize_url"])
    assert cb["_redirect"] == redirect
    return await hub.mcp_oauth.callback(cb["state"], code=cb["code"], iss=cb.get("iss"))


def leak_blob(paths) -> bytes:
    blobs = b""
    for p in [paths.db, paths.db.with_name("hub.db-wal"), paths.mcp_registry, *paths.logs_dir.rglob("*")]:
        if p.exists() and p.is_file():
            blobs += p.read_bytes()
    return blobs


# =========================================================================== thuần
def test_pkce_and_secure_url_rules():
    v, c = pkce_pair()
    import base64
    import hashlib

    assert 43 <= len(v) <= 128 and c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    assert require_secure("https://x.example/a", "x")
    assert require_secure("http://127.0.0.1:9/a", "x") and require_secure("http://cfharness.localhost:1/cb", "x")
    with pytest.raises(HubError, match="https"):
        require_secure("http://evil.example/token", "token endpoint")
    assert secret_name("a-b") != secret_name("a_b")


def test_config_validation(hub, fake):
    with pytest.raises(HubError, match="http/sse"):
        hub.mcp.add({"name": "s", "command": "x", "auth": {"type": "oauth"}})
    with pytest.raises(HubError, match="Authorization"):
        hub.mcp.add({"name": "h", "transport": "http", "url": fake.mcp_url, "headers": {"Authorization": "Bearer x"}, "auth": {"type": "oauth"}})
    # client_secret nhập rõ → chuyển vào secret store, cấu hình chỉ giữ ${…}
    res = hub.mcp.add({"name": "m", "transport": "http", "url": fake.mcp_url,
                       "auth": {"type": "oauth", "client_id": "cid", "client_secret_ref": "very-secret-client-pw"}})
    ref = res["config"]["auth"]["client_secret_ref"]
    assert ref.startswith("${MCP_M_OAUTH_CLIENT_SECRET") and "very-secret" not in json.dumps(res["config"])
    assert res["oauth_status"] == "not_connected" and res["oauth"]["client"] == "manual"


# =========================================================================== luồng đầy đủ
async def test_discovery_dcr_pkce_connect_and_status(hub, fake, hub_home):
    add_server(hub, fake)
    st = await hub.mcp_oauth.start("notes", "http://cfharness.localhost:8765/mcp/oauth/callback")
    q = parse_qs(urlsplit(st["authorize_url"]).query)
    assert st["authorize_url"].startswith(fake.issuer + "/authorize?") and st["client"] == "dcr"
    assert q["code_challenge_method"] == ["S256"] and len(q["state"][0]) >= 40
    assert q["resource"] == [fake.mcp_url] and q["scope"] == ["mcp.read"]  # scope từ WWW-Authenticate
    assert q["client_id"][0] in fake.clients  # DCR thật
    cb = await asyncio.to_thread(authorize, st["authorize_url"])
    res = await hub.mcp_oauth.callback(cb["state"], code=cb["code"])
    assert res["ok"] and res["oauth_status"] == "connected" and res["oauth"]["refreshable"]
    at = fake.issued_access[-1]
    # token chỉ ở secret store (bản ghi JSON), không bao giờ trong kết quả RPC/show/list
    assert at not in json.dumps(res) and at not in json.dumps(hub.mcp.show("notes")) and at not in json.dumps(hub.mcp.with_oauth(hub.mcp.list()))
    assert load_record(hub.secrets, "notes")["tokens"]["access_token"] == at
    show = hub.mcp.show("notes")
    assert show["oauth_status"] == "connected" and show["oauth"]["expires_at"] and show["oauth"]["issuer"].rstrip("/") == fake.issuer
    # state một lần: dùng lại → từ chối
    with pytest.raises(HubError, match="state không hợp lệ"):
        await hub.mcp_oauth.callback(cb["state"], code=cb["code"])
    # đăng nhập lại với cùng redirect → dùng lại bản DCR (không đăng ký thêm)
    n = len(fake.clients)
    await connect(hub, fake)
    assert len(fake.clients) == n


async def test_state_mismatch_expired_and_provider_error(hub, fake):
    add_server(hub, fake)
    with pytest.raises(HubError, match="state không hợp lệ"):
        await hub.mcp_oauth.callback("khong-ton-tai", code="x")
    st = await hub.mcp_oauth.start("notes", "http://127.0.0.1:1/cb")
    cb = await asyncio.to_thread(authorize, st["authorize_url"])
    real = hub.mcp_oauth.clock
    hub.mcp_oauth.clock = lambda: real() + 601  # quá 10 phút
    try:
        res = await hub.mcp_oauth.callback(cb["state"], code=cb["code"])
    finally:
        hub.mcp_oauth.clock = real
    assert not res["ok"] and res["reason"] == "expired_state" and hub.mcp.show("notes")["oauth_status"] == "not_connected"
    st = await hub.mcp_oauth.start("notes", "http://127.0.0.1:1/cb")
    state = parse_qs(urlsplit(st["authorize_url"]).query)["state"][0]
    res = await hub.mcp_oauth.callback(state, error="access_denied", error_description="người dùng từ chối")
    assert not res["ok"] and "access_denied" in res["error"]
    # code giả/không khớp PKCE → token endpoint từ chối, không lưu gì
    st = await hub.mcp_oauth.start("notes", "http://127.0.0.1:1/cb")
    state = parse_qs(urlsplit(st["authorize_url"]).query)["state"][0]
    res = await hub.mcp_oauth.callback(state, code="code_gia")
    assert not res["ok"] and res["reason"] == "token_failed" and "invalid_grant" in res["error"]
    assert (load_record(hub.secrets, "notes") or {}).get("tokens") is None


async def test_pkce_verifier_is_checked(hub, fake):
    add_server(hub, fake)
    st = await hub.mcp_oauth.start("notes", "http://127.0.0.1:1/cb")
    cb = await asyncio.to_thread(authorize, st["authorize_url"])
    hub.mcp_oauth.pending[cb["state"]].verifier = "x" * 64  # kẻ chặn code không có verifier đúng
    res = await hub.mcp_oauth.callback(cb["state"], code=cb["code"])
    assert not res["ok"] and "PKCE" in res["error"]


async def test_iss_param_checked_when_advertised(hub_home):
    f = FakeOAuth(iss_param=True).start()
    h = Hub(hub_home, runtime=FakeRuntime())
    try:
        add_server(h, f)
        st = await h.mcp_oauth.start("notes", "http://127.0.0.1:1/cb")
        cb = await asyncio.to_thread(authorize, st["authorize_url"])
        res = await h.mcp_oauth.callback(cb["state"], code=cb["code"], iss="https://attacker.example/as")
        assert not res["ok"] and res["reason"] == "iss_mismatch"
        st = await h.mcp_oauth.start("notes", "http://127.0.0.1:1/cb")
        cb = await asyncio.to_thread(authorize, st["authorize_url"])
        res = await h.mcp_oauth.callback(cb["state"], code=cb["code"])  # thiếu iss mà AS đã khai → từ chối
        assert not res["ok"] and res["reason"] == "iss_missing"
        res = await connect(h, f)
        assert res["ok"]
    finally:
        f.stop()
        h.store.close()


async def test_no_dcr_needs_manual_client(hub_home):
    f = FakeOAuth(dcr=False, manual_client=("my-app", "my-app-secret-123")).start()
    h = Hub(hub_home, runtime=FakeRuntime())
    try:
        add_server(h, f)
        with pytest.raises(HubError, match="client_id") as ei:
            await h.mcp_oauth.start("notes", "http://127.0.0.1:1/cb")
        assert ei.value.data["oauth"] == "no_dcr"
        h.mcp.update("notes", {"auth": {"type": "oauth", "client_id": "my-app", "client_secret_ref": "my-app-secret-123"}})
        res = await connect(h, f)
        assert res["ok"] and res["oauth"]["client"] == "manual"
        assert f.token_calls[-1]["_basic"] is True and "client_secret" not in f.token_calls[-1]  # client_secret_basic
    finally:
        f.stop()
        h.store.close()


async def test_legacy_without_prm_uses_server_origin(hub_home):
    """Server không có PRM → dự phòng spec 2025-03-26 (AS metadata ở origin server). AS giả của test không phục vụ đường gốc →
    start báo lỗi rõ thay vì gửi token tới chỗ lạ."""
    f = FakeOAuth(prm=False).start()
    h = Hub(hub_home, runtime=FakeRuntime())
    try:
        add_server(h, f)
        with pytest.raises(HubError, match="DCR") as ei:  # endpoint mặc định <origin>/register → 404
            await h.mcp_oauth.start("notes", "http://127.0.0.1:1/cb")
        assert ei.value.data["oauth"] == "dcr_failed"
    finally:
        f.stop()
        h.store.close()


# =========================================================================== refresh / run / probe
async def test_refresh_before_run_and_injection_into_runtime_mcp_json(hub, fake, hub_home):
    add_server(hub, fake)
    hub.agents.create({"id": "a", "name": "A", "model": "haiku", "mcp_servers": ["notes"]})
    # chưa kết nối → run FAIL rõ ràng (fail-closed), không chạy thiếu tool lặng lẽ
    run = hub.runs.submit("a", "đọc ghi chú")
    final = await hub.runs.wait(run["id"], 20)
    assert final["status"] == "failed" and "đăng nhập OAuth" in (final["error"] or "")
    await connect(hub, fake)
    first = fake.issued_access[-1]
    # token còn < 5 phút → run làm mới trước khi ghi mcp.json
    rec = load_record(hub.secrets, "notes")
    rec["tokens"]["expires_at"] = time.time() + 60
    from cfharness.mcp_client.oauth import save_record

    save_record(hub.secrets, "notes", rec)
    seen: dict = {}
    rt: FakeRuntime = hub.runs.runtime()  # type: ignore[assignment]
    orig = rt.start

    async def spy(spec):
        p = Path(spec.mcp_config_path)
        seen["mcp"] = json.loads(p.read_text(encoding="utf-8"))
        seen["mode"] = p.stat().st_mode & 0o777
        seen["path"] = p
        return await orig(spec)

    rt.start = spy  # type: ignore[method-assign]
    run = hub.runs.submit("a", "đọc ghi chú")
    final = await hub.runs.wait(run["id"], 20)
    assert final["status"] == "completed", final.get("error")
    second = fake.issued_access[-1]
    assert second != first  # đã refresh
    assert seen["mcp"]["mcpServers"]["notes"]["headers"]["Authorization"] == f"Bearer {second}"
    assert WIN or seen["mode"] == 0o600
    assert not seen["path"].exists()  # file runtime bị xoá sau run
    assert load_record(hub.secrets, "notes")["tokens"]["refresh_token"] == fake.issued_refresh[-1]  # refresh_token xoay vòng
    # hợp đồng cho luồng B (build_agent_mcp_servers) cũng chèn token
    assert build_agent_mcp_servers(hub.agents.get_row("a"), home=hub_home.home)["notes"]["headers"]["Authorization"] == f"Bearer {second}"
    # không token nào lọt vào argv/event/DB
    run_row = hub.store.get_run(run["id"])
    assert second not in json.dumps(run_row, default=str)
    hub.store.conn.execute("PRAGMA wal_checkpoint(FULL)")
    blob = leak_blob(hub_home)
    for t in fake.issued_access + fake.issued_refresh:
        assert t.encode() not in blob


async def test_refresh_failure_marks_error_and_expired_token_blocks(hub, fake):
    add_server(hub, fake)
    await connect(hub, fake)
    rec = load_record(hub.secrets, "notes")
    fake.revoked.append(rec["tokens"]["refresh_token"])  # AS thu hồi refresh_token
    with pytest.raises(HubError):
        await hub.mcp_oauth.refresh("notes")
    st = hub.mcp.show("notes")
    assert st["oauth_status"] == "error" and "đăng nhập lại" in st["oauth"]["error"]
    rec = load_record(hub.secrets, "notes")
    rec["tokens"]["expires_at"] = time.time() - 1
    from cfharness.mcp_client.oauth import save_record

    save_record(hub.secrets, "notes", rec)
    with pytest.raises(HubError, match="hết hạn"):
        bearer_for(hub.secrets, hub.mcp.config_of(hub.mcp.require("notes")))


async def test_probe_uses_token_and_url_change_drops_token(hub, fake):
    add_server(hub, fake)
    pr = await hub.mcp.probe("notes")
    assert not pr["ok"] and "đăng nhập" in pr["error"] and "login" in pr["hint"]
    await connect(hub, fake)
    pr = await hub.mcp.probe("notes")
    assert pr["ok"], pr
    assert "doc_ghi_chu" in pr["tools"] and fake.mcp_auth_seen[-1] == f"Bearer {fake.issued_access[-1]}"
    # đổi URL → token cũ không bao giờ gửi sang URL mới
    res = hub.mcp.update("notes", {"url": fake.base + "/mcp2"})
    assert any("OAuth" in w for w in res["warnings"]) and res["oauth_status"] != "connected"
    with pytest.raises(HubError, match="đăng nhập"):
        hub.mcp.resolved_entry("notes")


async def test_disconnect_revokes_and_deletes(hub, fake):
    add_server(hub, fake)
    await connect(hub, fake)
    rt = load_record(hub.secrets, "notes")["tokens"]["refresh_token"]
    res = await hub.mcp_oauth.disconnect("notes")
    assert res["revoked"] and res["disconnected"] and rt in fake.revoked
    assert not hub.secrets.exists(secret_name("notes")) and hub.mcp.show("notes")["oauth_status"] == "not_connected"


# =========================================================================== RPC + audit (daemon in-process) + web + CLI
WEB_PORT = 8765
WEB_BASE = f"http://cfharness.localhost:{WEB_PORT}"


async def _daemon(paths):
    from cfharness.daemon import Daemon

    d = Daemon(paths, runtime=FakeRuntime())
    task = asyncio.create_task(d.serve())
    for _ in range(200):
        if paths.socket.exists():
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    return d, task


async def _stop(d, task):
    d.stopping.set()
    try:
        await asyncio.wait_for(task, 20)
    except Exception:
        pass


async def test_web_connect_and_public_callback_flow(hub_home, fake):
    from cfharness.web.app import create_app
    from cfharness.web.security import COOKIE, WebAuth

    d, task = await _daemon(hub_home)
    auth = WebAuth()
    app = create_app(hub_home, auth=auth, port=WEB_PORT)
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=WEB_BASE)
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=WEB_BASE)
    try:
        r = await c.get("/login?t=" + auth.mint())
        sid = r.cookies.get(COOKIE) or c.cookies.get(COOKIE)
        csrf, key = auth.sessions[sid].csrf, r.headers["location"].split("#k=", 1)[1]
        # thêm server http + ô "Đăng nhập OAuth" (không probe vì chưa có token) → về trang server
        r = await c.post("/do/mcp_add", data={"csrf": csrf, "k": key, "confirm": "yes", "name": "notes", "transport": "http",
                                              "url": fake.mcp_url, "oauth": "1", "next": "/mcp"})
        assert r.status_code == 303 and r.headers["location"] == "/mcp/notes"
        page = (await c.get("/mcp/notes")).text
        assert "Đăng nhập OAuth" in page and "chưa kết nối" in page and 'action="/do/mcp_connect"' in page
        assert "chưa kết nối" in (await c.get("/mcp")).text
        # connect cần CSRF + khoá phiên WT-1
        assert (await c.post("/do/mcp_connect", data={"csrf": csrf, "name": "notes"})).status_code == 403
        r = await c.post("/do/mcp_connect", data={"csrf": csrf, "k": key, "name": "notes"})
        assert r.status_code == 200
        url = re.search(r'http-equiv="refresh" content="0;url=([^"]+)"', r.text).group(1).replace("&amp;", "&")
        assert url.startswith(fake.issuer + "/authorize?")
        assert parse_qs(urlsplit(url).query)["redirect_uri"] == [WEB_BASE + "/mcp/oauth/callback"]
        cb = await asyncio.to_thread(authorize, url)
        # callback = điều hướng cấp cao từ trang nhà cung cấp: cookie SameSite=Strict KHÔNG được gửi → route công khai
        bad = await anon.get("/mcp/oauth/callback", params={"state": "sai", "code": cb["code"]})
        assert bad.status_code == 400 and "không hợp lệ" in bad.text
        # Host lạ vẫn bị chặn (chống DNS rebinding) kể cả route công khai
        assert (await anon.get("/mcp/oauth/callback", params={"state": cb["state"]}, headers={"host": "evil.example:8765"})).status_code == 421
        ok = await anon.get("/mcp/oauth/callback", params={"state": cb["state"], "code": cb["code"]})
        assert ok.status_code == 200 and "Đã kết nối" in ok.text and "/mcp/notes?oauth=connected" in ok.text
        assert cb["code"] not in ok.text and fake.issued_access[-1] not in ok.text
        page = (await c.get("/mcp/notes?oauth=connected")).text
        assert "đã kết nối" in page and fake.issued_access[-1] not in page
        assert "đã kết nối" in (await c.get("/mcp")).text
        # có phiên (trình duyệt gửi cookie) → flash + 303
        r = await c.post("/do/mcp_connect", data={"csrf": csrf, "k": key, "name": "notes"})
        url = re.search(r'content="0;url=([^"]+)"', r.text).group(1).replace("&amp;", "&")
        cb2 = await asyncio.to_thread(authorize, url)
        r = await c.get("/mcp/oauth/callback", params={"state": cb2["state"], "code": cb2["code"]})
        assert r.status_code == 303 and r.headers["location"] == "/mcp/notes"
        # ngắt kết nối: cần xác nhận
        r = await c.post("/do/mcp_disconnect", data={"csrf": csrf, "k": key, "name": "notes", "next": "/mcp/notes"})
        assert r.status_code == 200 and "Ngắt kết nối OAuth" in r.text
        r = await c.post("/do/mcp_disconnect", data={"csrf": csrf, "k": key, "name": "notes", "confirm": "yes", "next": "/mcp/notes"})
        assert r.status_code == 303 and d.hub.mcp.show("notes")["oauth_status"] == "not_connected"
        # audit: có thao tác, kênh web, KHÔNG có code/state/token
        rows = [dict(x) for x in d.hub.store.conn.execute("SELECT kind, channel, data FROM audit_log WHERE kind LIKE 'mcp.oauth_%'")]
        kinds = {x["kind"] for x in rows}
        assert {"mcp.oauth_start", "mcp.oauth_callback", "mcp.oauth_disconnect"} <= kinds and all(x["channel"] == "web" for x in rows)
        dump = json.dumps(rows, ensure_ascii=False)
        for s in (cb["code"], cb["state"], cb2["code"], cb2["state"], *fake.issued_access, *fake.issued_refresh):
            assert s not in dump
        # client_secret nhập rõ qua RPC (đường CLI `--oauth-client-secret`) → secret store; audit chỉ thấy ${…}/***
        await d.methods["mcp.update"]({"name": "notes", "patch": {"auth": {"type": "oauth", "client_id": "x", "client_secret_ref": "plain-client-secret-xyz"}}})
        au = json.dumps([dict(x) for x in d.hub.store.conn.execute("SELECT data FROM audit_log WHERE kind='mcp.update'")], ensure_ascii=False)
        assert "plain-client-secret-xyz" not in au and au != "[]"
        d.hub.store.conn.execute("PRAGMA wal_checkpoint(FULL)")
        blob = leak_blob(hub_home)
        assert b"plain-client-secret-xyz" not in blob
        for s in (cb["code"], cb2["code"], *fake.issued_access, *fake.issued_refresh):
            assert s.encode() not in blob
    finally:
        await c.aclose()
        await anon.aclose()
        await _stop(d, task)


async def test_oauth_rpc_is_human_only(hub_home):
    from cfharness import auth

    for m in ("mcp.oauth_start", "mcp.oauth_callback", "mcp.oauth_disconnect", "mcp.oauth_refresh"):
        assert m in auth.HUMAN_ONLY and m in auth.all_mutating()
    from cfharness.guard.classifier import HUB_HUMAN_ONLY_CLI

    assert {"login", "logout"} <= HUB_HUMAN_ONLY_CLI["mcp"]


def test_cli_login_via_loopback_listener(hub_home, fake, capsys, monkeypatch):
    """`cfharness mcp login` qua daemon thật: listener 127.0.0.1 nhận callback từ "trình duyệt" (AS giả tự cho phép)."""
    import yaml

    from cfharness.cli import main
    from cfharness.daemon import spawn_background

    hub_home.config.write_text(yaml.safe_dump({"runtime": {"kind": "fake"}}))
    spawn_background(hub_home)
    try:
        assert main(["mcp", "add", "--url", fake.mcp_url, "--oauth", "--no-probe", "notes"]) == 0
        capsys.readouterr()
        opened: list[str] = []

        def fake_browser(url, new=0):
            opened.append(url)
            def browse():
                cb = authorize(url)
                httpx.get(cb["_redirect"], params={"state": cb["state"], "code": cb["code"]}, timeout=10)

            threading.Thread(target=browse, daemon=True).start()
            return True

        monkeypatch.setattr("webbrowser.open", fake_browser)
        assert main(["mcp", "login", "notes", "--port", "0", "--timeout", "60"]) == 0
        out = capsys.readouterr().out
        assert "đã kết nối notes" in out and opened
        assert fake.issued_access[-1] not in out
        assert main(["mcp", "show", "notes"]) == 0
        assert "oauth: connected" in capsys.readouterr().out
        assert main(["mcp", "logout", "notes"]) == 0
        assert "đã ngắt kết nối" in capsys.readouterr().out
    finally:
        main(["daemon", "stop"])
