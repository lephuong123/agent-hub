"""Web MCP (`web/mcp_pages.py`): danh sách THÔ, CHỈ ĐỌC các MCP server do claude quản lý theo tài khoản.

Hub không còn lớp chính sách MCP (gán theo agent, tin cậy, lọc tool): trang `/mcp` chỉ vẽ kết quả RPC `mcp.list` `{}` →
`{"accounts": [{"id", "label", "config_path", "servers": [{"name", "transport", "target"}], "error"}]}`. Daemon in-process thật
(fixture của `test_web`), nhưng `mcp.list` được giả ở lớp web để test khớp đúng hợp đồng mới, độc lập với backend.
"""

from __future__ import annotations

from typing import Any

import pytest

from test_web import ISOLATED, _start, _stop  # noqa: F401
from cfharness.runtime import FakeRuntime


@pytest.fixture
async def env(hub_home, monkeypatch):
    monkeypatch.setenv("CFHARNESS_TEST_CLAUDE_HOME", str(hub_home.home / "claude-home"))
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    yield e
    await _stop(e)


def _stub_list(env, monkeypatch, result: Any) -> list[tuple[str, Any]]:
    """Giả `mcp.list` ở client RPC của web (mọi trang đi qua đây); method khác tới daemon thật. Trả danh sách lượt gọi `mcp.*`
    để kiểm params và để chắc web không gọi method `mcp.*` nào đã gỡ."""
    calls: list[tuple[str, Any]] = []
    client = env.app.state.web.client
    real = client.call

    async def fake(method, params=None, **kw):
        if method.startswith("mcp."):
            calls.append((method, params))
            if method != "mcp.list":
                raise AssertionError(f"web gọi RPC đã gỡ: {method}")
            if isinstance(result, Exception):
                raise result
            return result
        return await real(method, params, **kw)

    monkeypatch.setattr(client, "call", fake)
    return calls


LISTING = {"accounts": [
    {"id": None, "label": "Đăng nhập của máy", "config_path": "/home/u/.claude.json",
     "servers": [{"name": "atlassian", "transport": "http", "target": "mcp.atlassian.com"},
                 {"name": "fs", "transport": "stdio", "target": "npx -y @mcp/fs --token ***"}],
     "error": None},
    {"id": "a_1234abcd", "label": "Công việc", "config_path": "/hub/claude-config/a_1234abcd/.claude.json", "servers": [],
     "error": "Không đọc được .claude.json: JSON hỏng"},
    {"id": "a_99990000", "label": "Cá nhân", "config_path": "/hub/claude-config/a_99990000/.claude.json", "servers": [], "error": None},
]}


async def test_list_grouped_by_account_with_error_and_empty(env, monkeypatch):
    calls = _stub_list(env, monkeypatch, LISTING)
    await env.login()
    r = await env.client.get("/mcp")
    assert r.status_code == 200
    assert calls == [("mcp.list", {})]
    html = r.text
    # nhóm theo tài khoản: nhãn + đường dẫn cấu hình, theo đúng thứ tự
    i_machine, i_work, i_personal = (html.index(x) for x in ("Đăng nhập của máy", "Công việc", "Cá nhân"))
    assert i_machine < i_work < i_personal
    assert "/home/u/.claude.json" in html and "/hub/claude-config/a_1234abcd/.claude.json" in html
    assert 'id="mcp-acct-default"' in html and 'id="mcp-acct-a_1234abcd"' in html
    # mỗi server một dòng: tên, transport, đích (đã che từ backend)
    assert "atlassian" in html and "mcp.atlassian.com" in html and "npx -y @mcp/fs --token ***" in html
    assert ">http<" in html and ">stdio<" in html
    # lỗi theo tài khoản + trạng thái trống của tài khoản không lỗi, không server
    assert "Không đọc được .claude.json: JSON hỏng" in html
    assert "Tài khoản này chưa có MCP server nào" in html
    assert html.count("Tài khoản này chưa có MCP server nào") == 1  # tài khoản lỗi không hiện thêm trạng thái trống
    # hướng dẫn thêm server bằng claude + mọi agent dùng mọi server
    for s in ("claude mcp add", "CLAUDE_CONFIG_DIR=", "claude mcp login", "/mcp</code>", "Mọi agent tự động dùng mọi MCP server"):
        assert s in html, s
    # chỉ đọc: không còn thao tác nào
    for gone in ("/do/mcp_", 'href="/mcp/', 'action="/mcp/', "Tin cậy", "Agent dùng"):
        assert gone not in html, gone


async def test_empty_state(env, monkeypatch):
    _stub_list(env, monkeypatch, {"accounts": []})
    await env.login()
    r = await env.client.get("/mcp")
    assert r.status_code == 200 and "Chưa có MCP server nào" in r.text and "claude mcp add" in r.text


async def test_rpc_error_shows_message_not_500(env, monkeypatch):
    from cfharness.rpc import RpcError

    _stub_list(env, monkeypatch, RpcError(-32601, "Method not found"))
    await env.login()
    r = await env.client.get("/mcp")
    assert r.status_code == 200 and "Không đọc được danh sách MCP" in r.text


async def test_malformed_listing_is_tolerated(env, monkeypatch):
    _stub_list(env, monkeypatch, {"accounts": [None, {"id": "a_1", "servers": [None, {"name": "x"}]}, "rác"]})
    await env.login()
    r = await env.client.get("/mcp")
    assert r.status_code == 200 and "<b>x</b>" in r.text


@pytest.mark.parametrize("path", ["/mcp/docs", "/mcp/agent/nc", "/mcp/docs/tool", "/mcp/browse"])
async def test_old_urls_redirect_to_list(env, monkeypatch, path):
    _stub_list(env, monkeypatch, {"accounts": []})
    await env.login()
    r = await env.client.get(path)
    assert r.status_code == 303 and r.headers["location"] == "/mcp"


async def test_old_post_urls_redirect_and_removed_actions_404(env, monkeypatch):
    calls = _stub_list(env, monkeypatch, {"accounts": []})
    await env.login()
    r = await env.post("/mcp/docs/tool", {"op": "block", "tool": "x"})
    assert r.status_code == 303 and r.headers["location"] == "/mcp"
    for act in ("mcp_edit", "mcp_remove", "mcp_restore", "mcp_claude_add", "mcp_claude_rm", "mcp_claude_login_start",
                "mcp_claude_login_finish", "mcp_claude_logout", "mcp_claude_register", "mcp_claude_check"):
        r = await env.post(f"/do/{act}", {"name": "docs", "mcp_name": "docs", "account_id": "machine", "confirm": "yes"})
        assert r.status_code == 404, act
    assert calls == []  # không gọi RPC mcp.* nào đã gỡ


async def test_agent_form_has_no_mcp_fields_and_saves(env, monkeypatch):
    calls = _stub_list(env, monkeypatch, {"accounts": []})
    await env.login()
    r = await env.client.get("/agents/new")
    assert r.status_code == 200 and 'name="mcp"' not in r.text and "Công cụ MCP" not in r.text
    r = await env.post("/agents/create", {"id": "nc", "template": "researcher", "name": "Nghiên cứu", "confirm": "yes"})
    assert r.status_code == 303
    a = env.hub.agents.show("nc")
    assert a["name"] == "Nghiên cứu" and not a.get("mcp_servers")
    r = await env.client.get("/agents/nc/edit")
    assert r.status_code == 200 and 'name="mcp"' not in r.text
    base = {"name": "Nghiên cứu 2", "role": a["role"], "description": a["description"], "model": "haiku", "soul": a["soul"], "confirm": "yes"}
    r = await env.post("/agents/nc/save", base)
    assert r.status_code == 303 and env.hub.agents.show("nc")["name"] == "Nghiên cứu 2"
    r = await env.client.get("/agents/nc")
    assert r.status_code == 200 and "/mcp/agent/" not in r.text and "xem cấu hình MCP" not in r.text
    r = await env.client.get("/agents")
    assert r.status_code == 200 and 'data-label="MCP"' not in r.text
    assert calls == []


async def test_accounts_page_has_no_mcp_section(env, monkeypatch):
    calls = _stub_list(env, monkeypatch, {"accounts": []})
    await env.login()
    r = await env.client.get("/settings/accounts")
    assert r.status_code == 200
    for gone in ("MCP của tài khoản", "Đăng ký cho hub", "mcp_claude_", "acct-mcp", "Kiểm tra MCP"):
        assert gone not in r.text, gone
    assert calls == []
