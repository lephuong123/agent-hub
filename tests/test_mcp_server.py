"""MCP server của hub (FR-MS-01..07): test bằng client SDK in-process, stdio thật và HTTP cục bộ.

Daemon thật (tiến trình con) + FakeRuntime — không gọi `claude`.
"""

import json
import os
import socket
import sys
import threading
import time

import pytest
import yaml
from mcp import Client

from agent_hub.cli import main
from agent_hub.daemon import spawn_background
from agent_hub.mcp_server.server import READ_ONLY_TOOLS, build_server
from agent_hub.rpc import RpcClient


@pytest.fixture
def daemon(hub_home):
    hub_home.config.write_text(yaml.safe_dump({"runtime": {"kind": "fake", "fake": {"line_delay_s": 0.02}}, "runs": {"stop_grace_s": 1}}))
    spawn_background(hub_home)
    yield hub_home
    main(["daemon", "stop"])


def caller_for(paths):
    return lambda method, params: RpcClient(paths.socket, timeout=60).call(method, params)


def sc(result):
    assert not result.is_error, result.content
    return result.structured_content


async def test_tools_annotations_and_resources(daemon):
    srv = build_server(caller_for(daemon))
    async with Client(srv) as c:
        assert "hub_run" in (c.instructions or "") and "hub_events" in c.instructions
        tools = {t.name: t for t in (await c.list_tools()).tools}
        expected = {"hub_status", "hub_list_agents", "hub_agent_get", "hub_agent_create", "hub_agent_update", "hub_agent_archive",
                    "hub_run", "hub_run_status", "hub_events", "hub_cancel", "hub_open_requests", "hub_approve", "hub_deny",
                    "hub_feedback", "hub_estop"}
        assert expected <= set(tools)
        for name, t in tools.items():
            assert t.annotations is not None and t.output_schema, name
            assert t.annotations.read_only_hint is (name in READ_ONLY_TOOLS), name
            assert t.description and any(ch in t.description for ch in "ạảãáàâăêôơưđ"), name  # mô tả tiếng Việt
        for d in ("hub_cancel", "hub_agent_archive", "hub_approve", "hub_estop"):
            assert tools[d].annotations.destructive_hint is True
        assert tools["hub_run"].annotations.destructive_hint is False
        assert "agent" in tools["hub_run"].input_schema["properties"]
        r = await c.read_resource("hub://huong-dan")
        assert "Quy trình" in r.contents[0].text


async def test_run_events_flow_idempotency_feedback(daemon):
    srv = build_server(caller_for(daemon))
    async with Client(srv) as c:
        st = sc(await c.call_tool("hub_status", {}))
        assert st["access"] == "control" and st["open_requests"] == []
        created = sc(await c.call_tool("hub_agent_create", {"agent_id": "haiku-helper", "model": "haiku", "role": "trợ lý", "idempotency_key": "k1"}))
        assert created["agent_id"] == "haiku-helper" and created["revision"] == 1 and not created["idempotent_replay"]
        again = sc(await c.call_tool("hub_agent_create", {"agent_id": "haiku-helper", "idempotency_key": "k1"}))
        assert again["idempotent_replay"] is True
        dup = await c.call_tool("hub_agent_create", {"agent_id": "haiku-helper"})
        assert dup.is_error and "đã tồn tại" in dup.content[0].text
        agents = sc(await c.call_tool("hub_list_agents", {}))["agents"]
        assert [a["id"] for a in agents] == ["haiku-helper"]

        t0 = time.monotonic()
        run = sc(await c.call_tool("hub_run", {"agent": "haiku-helper", "prompt": "đếm từ", "idempotency_key": "run-1"}))
        assert time.monotonic() - t0 < 1.0  # FR-MS-02: trả ngay
        assert run["run_id"].startswith("r_") and run["status"] in ("queued", "running")
        rep = sc(await c.call_tool("hub_run", {"agent": "haiku-helper", "prompt": "đếm từ", "idempotency_key": "run-1"}))
        assert rep["run_id"] == run["run_id"] and rep["idempotent_replay"]  # FR-MS-07

        after, epoch, seen, calls = run["after_seq"], run["epoch"], [], 0
        while True:
            calls += 1
            ev = sc(await c.call_tool("hub_events", {"run_id": run["run_id"], "after_seq": after, "epoch": epoch, "wait_s": 5}))
            seen += ev["events"]
            after, epoch = ev["latest_seq"], ev["epoch"]
            if ev["done"]:
                break
            assert calls < 50
        types = [e["type"] for e in seen]
        assert types[0] == "run.queued" and "tool.start" in types and types[-1] == "run.completed"
        assert [e["seq"] for e in seen] == sorted(e["seq"] for e in seen) and len({e["seq"] for e in seen}) == len(seen)
        assert all("input" not in e["payload"] for e in seen if e["type"] == "tool.start")
        assert ev["result"]["status"] == "completed" and ev["result"]["cost_usd"] == pytest.approx(0.00474188)
        # gọi lại sau khi xong: trả ngay, không event mới
        t0 = time.monotonic()
        ev2 = sc(await c.call_tool("hub_events", {"run_id": run["run_id"], "after_seq": after, "epoch": epoch, "wait_s": 20}))
        assert ev2["events"] == [] and ev2["done"] and time.monotonic() - t0 < 2
        rs = sc(await c.call_tool("hub_run_status", {"run_id": run["run_id"]}))
        assert rs["done"] and rs["status"] == "completed"

        fb = sc(await c.call_tool("hub_feedback", {"run_id": run["run_id"], "kind": "wrong", "note": "phải dùng dd/mm/yyyy"}))
        assert fb["kind"] == "correction" and fb["signal_id"].startswith("sg_")

        assert sc(await c.call_tool("hub_open_requests", {}))["requests"] == []
        ap = await c.call_tool("hub_approve", {"request_id": "ap_x"})
        assert ap.is_error  # approval.* chưa cài hoặc request không tồn tại → lỗi rõ, không tự duyệt

    rpc = RpcClient(daemon.socket)
    sig = rpc.call("feedback.list", {"run_id": run["run_id"]})
    assert sig[0]["kind"] == "correction" and sig[0]["source"] == "mcp" and sig[0]["evidence"]["note"] == "phải dùng dd/mm/yyyy"
    au = rpc.call("audit.list", {"kind": "mcp.run"})
    assert au and au[0]["channel"].startswith("mcp:") and au[0]["run_id"] == run["run_id"]
    au = rpc.call("audit.list", {"kind": "mcp.agent_create"})
    assert au and au[-1]["agent_id"] == "haiku-helper"


async def test_events_long_poll_waits(daemon):
    """Không có event mới → chờ đủ wait_s rồi trả events=[] (không lỗi timeout)."""
    rpc = RpcClient(daemon.socket)
    rpc.call("agent.create", {"config": {"id": "a1", "name": "a1"}})
    rid = rpc.call("run.start", {"agent": "a1", "prompt": "x"})["id"]
    rpc.call("run.wait", {"id": rid, "timeout": 20})
    srv = build_server(caller_for(daemon))
    async with Client(srv) as c:
        ev = sc(await c.call_tool("hub_events", {"run_id": rid}))
        assert ev["done"] and ev["events"]
        bad = await c.call_tool("hub_events", {"run_id": "r_khongco"})
        assert bad.is_error and "không tìm thấy run" in bad.content[0].text
    # run đang chạy chậm: long-poll trả về ngay khi có event
    rpc.call("config.set", {"key": "runtime.fake.line_delay_s", "value": 0.3})
    rid2 = rpc.call("run.start", {"agent": "a1", "prompt": "y"})["id"]
    st = rpc.call("hub.status")
    t0 = time.monotonic()
    first = rpc.call("mcp.events", {"run_id": rid2, "after_seq": st["last_seq"], "epoch": st["epoch"], "wait_s": 10})
    assert time.monotonic() - t0 < 5 and first["events"] and all(e["seq"] > st["last_seq"] for e in first["events"])
    # run đã xong, không còn event mới → trả ngay (done), không chờ
    rpc.call("run.wait", {"id": rid2, "timeout": 30})
    last = rpc.call("mcp.events", {"run_id": rid2, "after_seq": 0})
    t0 = time.monotonic()
    tail = rpc.call("mcp.events", {"run_id": rid2, "after_seq": last["latest_seq"], "epoch": last["epoch"], "wait_s": 10})
    assert tail["events"] == [] and tail["done"] and time.monotonic() - t0 < 2
    # run đang xếp hàng (agent chỉ 1 run đồng thời) → không có event mới: chờ đủ wait_s, trả events=[] (không lỗi)
    r3 = rpc.call("run.start", {"agent": "a1", "prompt": "chạy lâu"})["id"]
    r4 = rpc.call("run.start", {"agent": "a1", "prompt": "xếp hàng"})["id"]
    head = rpc.call("mcp.events", {"run_id": r4, "after_seq": 0, "wait_s": 0})
    t0 = time.monotonic()
    idle = rpc.call("mcp.events", {"run_id": r4, "after_seq": head["latest_seq"], "epoch": head["epoch"], "wait_s": 1})
    assert idle["events"] == [] and not idle["done"] and idle["status"] == "queued" and time.monotonic() - t0 >= 0.9
    rpc.call("run.stop", {"id": r4})
    rpc.call("run.stop", {"id": r3})


async def test_read_level_denied_and_cancel(daemon):
    rpc = RpcClient(daemon.socket)
    rpc.call("agent.create", {"config": {"id": "a1", "name": "a1"}})
    ro = build_server(caller_for(daemon), default_level="read")
    async with Client(ro) as c:
        assert sc(await c.call_tool("hub_list_agents", {}))["agents"]
        r = await c.call_tool("hub_cancel", {"run_id": "r_x"})
        assert r.is_error and "control" in r.content[0].text
        r = await c.call_tool("hub_run", {"agent": "a1", "prompt": "x"})
        assert r.is_error
    assert rpc.call("run.list", {}) == []  # không có thay đổi
    rpc.call("config.set", {"key": "runtime.fake.line_delay_s", "value": 0.5})
    srv = build_server(caller_for(daemon))
    async with Client(srv) as c:
        run = sc(await c.call_tool("hub_run", {"agent": "a1", "prompt": "chậm"}))
        time.sleep(0.5)
        cancel = sc(await c.call_tool("hub_cancel", {"run_id": run["run_id"]}))
        assert cancel["run_id"] == run["run_id"]
    final = rpc.call("run.wait", {"id": run["run_id"], "timeout": 30})
    assert final["status"] == "cancelled"
    au = rpc.call("audit.list", {"kind": "run.stop"})
    assert au and au[0]["channel"].startswith("mcp:")


async def test_stdio_transport(daemon):
    from mcp.client.stdio import StdioServerParameters

    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
    env["AGENT_HUB_HOME"] = str(daemon.home)
    params = StdioServerParameters(command=sys.executable, args=["-m", "agent_hub", "mcp", "serve"], env=env)
    async with Client(params, read_timeout_seconds=30) as c:
        t0 = time.monotonic()
        st = sc(await c.call_tool("hub_list_agents", {}))
        assert st == {"agents": []} and time.monotonic() - t0 < 1.0  # FR-MS-01: ≤ 1 s
        assert sc(await c.call_tool("hub_status", {}))["access"] == "control"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_http_transport_tokens(daemon):
    import httpx2
    import uvicorn

    from agent_hub.mcp_server.http import build_http_app, ensure_tokens
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    with pytest.raises(ValueError, match="loopback"):
        build_http_app(caller_for(daemon), {"read": "a", "control": "b"}, host="0.0.0.0")
    toks = ensure_tokens(daemon)
    assert oct(os.stat(daemon.home / "mcp_server" / "tokens.json").st_mode & 0o777) == "0o600"
    assert ensure_tokens(daemon) == toks
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_http_app(caller_for(daemon), toks), host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        url = f"http://127.0.0.1:{port}/mcp"
        async with httpx2.AsyncClient() as h:
            r = await h.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert r.status_code == 401
            r = await h.post(url, json={}, headers={"Authorization": "Bearer sai"})
            assert r.status_code == 401
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {toks['read']}"}) as http:
            async with Client(streamable_http_client(url, http_client=http)) as c:
                st = sc(await c.call_tool("hub_status", {}))
                assert st["access"] == "read"
                r = await c.call_tool("hub_estop", {"on": True, "reason": "x"})
                assert r.is_error and "control" in r.content[0].text
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {toks['control']}"}) as http:
            async with Client(streamable_http_client(url, http_client=http)) as c:
                assert sc(await c.call_tool("hub_status", {}))["access"] == "control"
                r = sc(await c.call_tool("hub_estop", {"on": False}))
                assert r["on"] is False
    finally:
        server.should_exit = True
        th.join(5)
    assert RpcClient(daemon.socket).call("estop.get")["on"] is False


def test_snippet_does_not_touch_claude_config(hub_home, tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "h"
    fake_home.mkdir()
    cj = fake_home / ".claude.json"
    cj.write_text('{"mcpServers": {}}')
    monkeypatch.setenv("HOME", str(fake_home))
    assert main(["mcp", "snippet", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["command"].startswith("claude mcp add") and out["json"]["mcpServers"]["agent-hub"]["args"][-2:] == ["mcp", "serve"]
    assert out["json"]["mcpServers"]["agent-hub"]["env"]["AGENT_HUB_HOME"] == str(hub_home.home)
    assert cj.read_text() == '{"mcpServers": {}}'
