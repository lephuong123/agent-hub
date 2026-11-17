"""Proxy loopback chèn token MCP OAuth (mcp_client/oauth_proxy.py, docs/dev-notes/mcp-oauth.md §8.1).

AS + MCP giả thật trên 127.0.0.1 (tests/oauth_fake.py), không mạng ngoài, không gọi `claude`. Kiểm:
- chuyển tiếp kèm token mới nhất, làm mới khi sắp hết hạn VÀ khi upstream trả 401 giữa run (run dài > hạn token);
- stream: Streamable HTTP trả text/event-stream không bị đệm; transport SSE cũ (event `endpoint` viết lại qua proxy);
- bảo mật: chỉ bind 127.0.0.1, từ chối thiếu/sai khoá, route lạ, path ngoài danh sách, Origin (trình duyệt), Host lạ, sau khi run
  kết thúc (thu hồi); mcp.json runtime KHÔNG chứa token/refresh token; settings.json không mở loopback cho sandbox;
- tắt (mặc định) → đường cũ (Bearer trong mcp.json).
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from pathlib import Path

import httpx2 as httpx
import pytest

from cfharness.hub import Hub
from cfharness.mcp_client.oauth import load_record, save_record
from cfharness.mcp_client.oauth_proxy import KEY_HEADER
from cfharness.runtime import FakeRuntime
from oauth_fake import FakeOAuth
from test_mcp_oauth import add_server, connect

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def fake():
    f = FakeOAuth().start()
    yield f
    f.release.set()
    f.stop()


@pytest.fixture
async def hub(hub_home):
    h = Hub(hub_home, runtime=FakeRuntime())
    h.start()
    h.config.set("mcp.oauth_proxy.enabled", True)
    yield h
    await h.mcp_proxy.stop()
    h.store.close()


async def _route(hub, name="notes", run_id="r_test"):
    await hub.mcp_proxy.ensure_started()
    cfg = hub.mcp.config_of(hub.mcp.require(name))
    return hub.mcp_proxy.register(run_id, cfg)


async def _post(entry, *, headers=None, url=None, body=INIT):
    h = dict(ACCEPT)
    h.update(entry["headers"] if headers is None else headers)
    async with httpx.AsyncClient(timeout=10) as c:
        return await c.post(url or entry["url"], headers=h, content=json.dumps(body))


# =========================================================================== chuyển tiếp + làm mới
async def test_forwards_with_current_token_and_refreshes_near_expiry(hub, fake):
    add_server(hub, fake)
    await connect(hub, fake)
    entry = await _route(hub)
    assert entry["url"].startswith(f"http://127.0.0.1:{hub.mcp_proxy.port}/r/") and entry["url"].endswith("/mcp")
    assert set(entry["headers"]) == {KEY_HEADER}
    r = await _post(entry)
    assert r.status_code == 200 and r.json()["result"]["serverInfo"]["name"] == "fake-oauth-mcp"
    first = fake.issued_access[-1]
    assert fake.mcp_auth_seen[-1] == f"Bearer {first}"
    # token sắp hết hạn GIỮA run (vd run > 1 giờ) → proxy làm mới trước khi chuyển tiếp, không cần spawn lại
    rec = load_record(hub.secrets, "notes")
    rec["tokens"]["expires_at"] = time.time() + 30
    save_record(hub.secrets, "notes", rec)
    r = await _post(entry, body={**INIT, "id": 2, "method": "tools/list", "params": {}})
    assert r.status_code == 200 and r.json()["result"]["tools"][0]["name"] == "doc_ghi_chu"
    second = fake.issued_access[-1]
    assert second != first and fake.mcp_auth_seen[-1] == f"Bearer {second}"


async def test_upstream_401_forces_one_refresh_and_retry(hub, fake):
    add_server(hub, fake)
    await connect(hub, fake)
    entry = await _route(hub)
    first = fake.issued_access[-1]
    fake.access[first] = 0  # server coi token đã hết hạn dù bản ghi còn hạn (đồng hồ lệch / thu hồi phía server)
    r = await _post(entry)
    assert r.status_code == 200
    assert fake.issued_access[-1] != first
    assert fake.mcp_auth_seen[-2:] == [f"Bearer {first}", f"Bearer {fake.issued_access[-1]}"]
    # refresh cũng hỏng → 401 KHÔNG kèm WWW-Authenticate (claude không tự mở luồng OAuth với proxy)
    fake.access[fake.issued_access[-1]] = 0
    fake.refresh.clear()
    r = await _post(entry)
    assert r.status_code == 401 and "www-authenticate" not in r.headers


# =========================================================================== stream
async def test_streamable_http_event_stream_is_not_buffered(hub, fake):
    add_server(hub, fake)
    await connect(hub, fake)
    entry = await _route(hub)
    fake.sse_mode = True
    h = {**ACCEPT, **entry["headers"]}
    async with httpx.AsyncClient(timeout=10) as c:
        async with c.stream("POST", entry["url"], headers=h, content=json.dumps(INIT)) as r:
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
            it = r.aiter_text()
            got = ""
            while "notifications/progress" not in got:  # khối 1 tới TRƯỚC khi server sinh khối 2
                got += await asyncio.wait_for(it.__anext__(), 5)
            assert "serverInfo" not in got
            fake.release.set()
            async for t in it:
                got += t
    assert "fake-oauth-mcp" in got


async def test_legacy_sse_transport_endpoint_rewritten_through_proxy(hub, fake):
    hub.mcp.add({"name": "legacy", "transport": "sse", "url": fake.base + "/sse", "auth": {"type": "oauth"}})
    # bản ghi token gắn đúng URL /sse (AS giả chỉ phát token cho /mcp — gieo thẳng)
    tok = "at_seed_" + "x" * 20
    fake.access[tok] = time.time() + 3600
    save_record(hub.secrets, "legacy", {"server_url": fake.base + "/sse", "resource": fake.base + "/sse",
                                         "tokens": {"access_token": tok, "expires_at": time.time() + 3600}})
    entry = await _route(hub, "legacy")
    assert entry["type"] == "sse" and entry["url"].endswith("/sse")
    async with httpx.AsyncClient(timeout=10) as c:
        async with c.stream("GET", entry["url"], headers={"Accept": "text/event-stream", **entry["headers"]}) as r:
            assert r.status_code == 200
            it = r.aiter_lines()
            ep = None
            while ep is None:
                line = await asyncio.wait_for(it.__anext__(), 5)
                if line.startswith("data:"):
                    ep = line[5:].strip()
            route_id = entry["url"].split("/r/", 1)[1].split("/", 1)[0]
            assert ep.startswith(f"/r/{route_id}/messages?session_id=")  # endpoint đi QUA proxy, không phải server thật
            post_url = f"http://127.0.0.1:{hub.mcp_proxy.port}{ep}"
            # thiếu khoá ở endpoint POST → từ chối
            p = await c.post(post_url, headers={"Content-Type": "application/json"}, content=json.dumps(INIT))
            assert p.status_code == 403
            p = await c.post(post_url, headers={"Content-Type": "application/json", **entry["headers"]}, content=json.dumps(INIT))
            assert p.status_code == 202
            data = ""
            while "serverInfo" not in data:
                data += await asyncio.wait_for(it.__anext__(), 5)
    assert all(a == f"Bearer {tok}" for a in fake.mcp_auth_seen)


# =========================================================================== bảo mật
async def test_security_rejections(hub, fake):
    add_server(hub, fake)
    await connect(hub, fake)
    entry = await _route(hub)
    sock = hub.mcp_proxy._sock
    assert sock.getsockname()[0] == "127.0.0.1"  # chỉ loopback, không bao giờ 0.0.0.0
    seen = len(fake.mcp_auth_seen)
    base = entry["url"]
    rid = base.split("/r/", 1)[1].split("/", 1)[0]
    port = hub.mcp_proxy.port
    cases = [
        ({}, base),  # thiếu khoá
        ({KEY_HEADER: "sai"}, base),  # sai khoá
        ({KEY_HEADER: entry["headers"][KEY_HEADER] + "x"}, base),
        (entry["headers"], base.replace(rid, rid[:-2] + "zz")),  # route lạ
        (entry["headers"], f"http://127.0.0.1:{port}/r/{rid}/as/token"),  # path ngoài danh sách (token endpoint cùng origin)
        (entry["headers"], f"http://127.0.0.1:{port}/mcp"),  # không qua route
        ({**entry["headers"], "Origin": "https://evil.example"}, base),  # trình duyệt / CSRF
        ({**entry["headers"], "Host": "evil.example"}, base),  # DNS rebinding
    ]
    for headers, url in cases:
        r = await _post(entry, headers=headers, url=url)
        assert r.status_code in (403, 404), (headers, url, r.status_code)
        assert "Bearer" not in r.text
    assert len(fake.mcp_auth_seen) == seen  # không request nào tới được server thật
    # route của run KHÁC không dùng được khoá của run này
    other = await _route(hub, run_id="r_other")
    r = await _post(entry, url=other["url"])
    assert r.status_code == 403
    # thu hồi khi run kết thúc
    assert hub.mcp_proxy.revoke_run("r_test") == 1
    r = await _post(entry)
    assert r.status_code == 403
    assert (await _post(other)).status_code == 200
    # URL server đổi giữa run → không gửi token sang URL mới
    hub.mcp.update("notes", {"url": fake.base + "/mcp2"})
    assert (await _post(other)).status_code == 403


async def test_run_mcp_json_points_to_proxy_and_never_contains_token(hub, fake, hub_home):
    add_server(hub, fake)
    await connect(hub, fake)
    hub.agents.create({"id": "a", "name": "A", "model": "haiku", "mcp_servers": ["notes"]})
    seen: dict = {}
    rt: FakeRuntime = hub.runs.runtime()  # type: ignore[assignment]
    orig = rt.start

    async def spy(spec):
        p = Path(spec.mcp_config_path)
        seen["raw"] = p.read_bytes()
        seen["mcp"] = json.loads(seen["raw"])
        seen["settings"] = json.loads(Path(spec.settings_path).read_text(encoding="utf-8"))
        e = seen["mcp"]["mcpServers"]["notes"]
        seen["during"] = (await _post(e)).status_code  # trong lúc run: proxy dùng được
        seen["entry"] = e
        return await orig(spec)

    rt.start = spy  # type: ignore[method-assign]
    run = hub.runs.submit("a", "đọc ghi chú")
    final = await hub.runs.wait(run["id"], 20)
    assert final["status"] == "completed", final.get("error")
    e = seen["entry"]
    assert e["url"].startswith(f"http://127.0.0.1:{hub.mcp_proxy.port}/r/") and set(e["headers"]) == {KEY_HEADER}
    assert seen["during"] == 200
    rec = load_record(hub.secrets, "notes")
    for t in fake.issued_access + fake.issued_refresh + [rec["tokens"]["access_token"]]:
        assert t.encode() not in seen["raw"]
    assert b"Bearer" not in seen["raw"] and b"Authorization" not in seen["raw"]
    # không cần (và không được) mở loopback cho Bash của agent: client MCP chạy trong tiến trình claude, ngoài sandbox
    sb = (seen["settings"].get("sandbox") or {}).get("network") or {}
    # (allowedDomains có 127.0.0.1 ở test chỉ vì server MCP GIẢ nằm trên loopback — host MCP của agent; spike hardening-SEC §8:
    # chỉ allowLocalBinding mở loopback cho Bash)
    assert not sb.get("allowLocalBinding")
    # sau run: route bị thu hồi
    assert (await _post(e)).status_code == 403
    assert hub.mcp_proxy.routes_of(run["id"]) == []


async def test_run_fails_clearly_when_not_connected_even_with_proxy(hub, fake):
    add_server(hub, fake)
    hub.agents.create({"id": "a", "name": "A", "model": "haiku", "mcp_servers": ["notes"]})
    run = hub.runs.submit("a", "đọc ghi chú")
    final = await hub.runs.wait(run["id"], 20)
    assert final["status"] == "failed" and "đăng nhập OAuth" in (final["error"] or "")
    assert hub.mcp_proxy.routes == {}


async def test_disabled_keeps_legacy_bearer_path(hub, fake):
    hub.config.set("mcp.oauth_proxy.enabled", False)
    add_server(hub, fake)
    await connect(hub, fake)
    hub.agents.create({"id": "a", "name": "A", "model": "haiku", "mcp_servers": ["notes"]})
    seen: dict = {}
    rt: FakeRuntime = hub.runs.runtime()  # type: ignore[assignment]
    orig = rt.start

    async def spy(spec):
        seen["mcp"] = json.loads(Path(spec.mcp_config_path).read_text(encoding="utf-8"))
        return await orig(spec)

    rt.start = spy  # type: ignore[method-assign]
    run = hub.runs.submit("a", "x")
    final = await hub.runs.wait(run["id"], 20)
    assert final["status"] == "completed"
    e = seen["mcp"]["mcpServers"]["notes"]
    assert e["url"] == fake.mcp_url and e["headers"]["Authorization"] == f"Bearer {fake.issued_access[-1]}"
    assert not hub.mcp_proxy.running


async def test_port_busy_falls_back_to_legacy_path(hub, fake):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        hub.config.set("mcp.oauth_proxy.port", s.getsockname()[1])
        add_server(hub, fake)
        await connect(hub, fake)
        hub.agents.create({"id": "a", "name": "A", "model": "haiku", "mcp_servers": ["notes"]})
        seen: dict = {}
        rt: FakeRuntime = hub.runs.runtime()  # type: ignore[assignment]
        orig = rt.start

        async def spy(spec):
            seen["mcp"] = json.loads(Path(spec.mcp_config_path).read_text(encoding="utf-8"))
            return await orig(spec)

        rt.start = spy  # type: ignore[method-assign]
        run = hub.runs.submit("a", "x")
        final = await hub.runs.wait(run["id"], 20)
    assert final["status"] == "completed"
    assert seen["mcp"]["mcpServers"]["notes"]["url"] == fake.mcp_url


def test_config_branch_is_security_key():
    from cfharness.guard.integrity import SECURITY_KEYS

    assert "mcp.oauth_proxy" in SECURITY_KEYS
