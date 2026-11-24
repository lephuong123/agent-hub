"""Registry MCP cho agent (FR-MC-01..03, 06): chỉ còn nguồn claude — validate, trust fail-closed, xoá mềm, hàm hợp đồng, migration v34
đổi dòng nguồn hub kiểu cũ tại chỗ, RPC/CLI từ chối cấu hình kiểu hub (v0.2.32)."""

import json
import sqlite3

import pytest
import yaml

from cfharness.cli import build_parser
from cfharness.errors import Conflict, HubError, NotFound
from cfharness.hub import Hub
from cfharness.mcp_client import agent_claude_mcp_servers, agent_mcp_tool_rules, tool_annotations
from cfharness.mcp_client.schema import to_claude_source
from cfharness.mcp_client.wiring import parse_mcp_tool


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home)
    yield h
    h.store.close()


def test_add_validate_trust_reserved_and_legacy_fields_dropped(hub, hub_home):
    res = hub.mcp.add({"name": "vb", "transport": "http", "url": "https://vb.example/api/mcp?k=1", "trust": "abc"})
    assert res["trust"] == "untrusted" and any("không hợp lệ" in w for w in res["warnings"])
    assert res["config"] == {"transport": "http", "url": "https://vb.example", "tools": res["config"]["tools"], "trust": "untrusted",
                             "timeout_s": 60.0, "description": "", "source": "claude"}
    mirror = yaml.safe_load(hub_home.mcp_registry.read_text())
    assert mirror["servers"]["vb"]["source"] == "claude"
    with pytest.raises(Conflict):
        hub.mcp.add({"name": "vb", "transport": "stdio"})
    with pytest.raises(HubError, match="dành riêng"):
        hub.mcp.add({"name": "hub-guard"})
    with pytest.raises(HubError, match="url"):
        hub.mcp.add({"name": "bad2", "transport": "http", "url": "ftp://x"})
    # trường kiểu hub (≤ v0.2.30) bị bỏ khi đọc — registry không bao giờ giữ lệnh/secret
    res = hub.mcp.add({"name": "pw", "command": "npx", "args": ["-y", "pkg"], "env": {"TOKEN": "x" * 30}, "trust": "trusted"})
    assert set(res["config"]) == {"transport", "tools", "trust", "timeout_s", "description", "source"}
    assert "npx" not in hub_home.mcp_registry.read_text()


def test_update_only_policy(hub):
    hub.mcp.add({"name": "vb", "transport": "stdio", "trust": "trusted"})
    out = hub.mcp.update("vb", {"trust": "untrusted", "tools": {"exclude": ["xoa_*"]}, "description": "văn bản"})
    assert out["trust"] == "untrusted" and out["config"]["tools"]["exclude"] == ["xoa_*"] and out["config"]["description"] == "văn bản"
    for patch in ({"command": "evil"}, {"url": "https://x"}, {"headers": {"A": "b"}}, {"transport": "http"}, {"auth": None}):
        with pytest.raises(HubError, match="claude giữ"):
            hub.mcp.update("vb", patch)


def test_assign_rules_soft_delete_and_annotations(hub):
    hub.mcp.add({"name": "vb", "transport": "stdio", "trust": "trusted"})
    hub.agents.create({"id": "a1", "name": "A1", "mcp_servers": ["vb"]})
    agent = hub.agents.get_row("a1")
    assert agent_claude_mcp_servers(agent) == ["vb"]
    assert agent_claude_mcp_servers("a1") == ["vb"]
    assert agent_claude_mcp_servers({"mcp_servers": []}) == []
    assert agent_mcp_tool_rules(agent)["allowed"] == ["mcp__vb"]  # trusted, không lọc → cả server
    ann = tool_annotations("vb", "tim_viec")
    assert ann["source"] == "claude" and ann["probed"] is False and ann["trust"] == "trusted" and ann["selected"] is True
    assert tool_annotations("khong-co", "x") is None
    with pytest.raises(Conflict, match="a1"):
        hub.mcp.remove("vb")
    removed = hub.mcp.remove("vb", force=True)
    assert removed["deleted_at"] and removed["trash_path"]
    assert hub.mcp.list() == [] and len(hub.mcp.list(include_deleted=True)) == 1
    with pytest.raises(HubError, match="xoá mềm"):  # fail-closed: run không chạy thiếu server
        agent_claude_mcp_servers(agent)
    hub.mcp.restore("vb")
    assert agent_claude_mcp_servers(agent) == ["vb"]
    with pytest.raises(NotFound):
        hub.mcp.require("khong-co")


def test_migration_v34_converts_hub_rows_in_place(hub):
    """Dòng nguồn hub kiểu cũ (manual / claude-import, có lệnh/secret/OAuth) → nguồn claude, GIỮ trust/tools/annotations_cache."""
    c = hub.store.conn
    old = {
        "playwright": {"transport": "stdio", "command": "cmd", "args": ["/c", "npx", "${MCP_PLAYWRIGHT_ARG7}"], "env": {}, "trust": "trusted",
                       "lazy": False, "supports_parallel_tool_calls": False, "timeout_s": 60, "description": "",
                       "tools": {"include": [], "exclude": ["browser_close"], "require_approval": [], "annotations": {"browser_snapshot": {"readOnlyHint": True}}},
                       "source": "claude-import"},
        "vb": {"transport": "http", "url": "https://vb.example/api/mcp", "headers": {}, "trust": "untrusted", "source": "manual",
               "auth": {"type": "oauth", "scopes": [], "client_id": None, "client_secret_ref": None}},
        "tok": {"transport": "sse", "url": "https://${MCP_TOK_HOST}/sse", "headers": {"Authorization": "Bearer ${MCP_TOK_AUTH}"}, "source": "manual"},
        "hubctl": {"transport": "stdio", "command": "cfharness", "args": ["mcp", "serve"], "source": "manual"},  # trỏ về chính hub
    }
    for name, cfg in old.items():
        c.execute("INSERT INTO mcp_servers (name, transport, config, trust, annotations_cache, source, created_at, updated_at) "
                  "VALUES (?,?,?,?,?,?,?,?)", (name, cfg["transport"], json.dumps(cfg), cfg.get("trust", "untrusted"),
                                               json.dumps({"browser_snapshot": {"readOnlyHint": True}}) if name == "playwright" else None,
                                               cfg["source"], "t", "t"))
    to_claude_source(c)
    to_claude_source(c)  # chạy lại vô hại
    pw = hub.mcp.show("playwright")
    assert pw["source"] == "claude" and pw["trust"] == "trusted"
    assert pw["config"]["tools"]["exclude"] == ["browser_close"] and pw["config"]["tools"]["annotations"] == {"browser_snapshot": {"readOnlyHint": True}}
    assert pw["annotations_cache"] == {"browser_snapshot": {"readOnlyHint": True}}
    assert not set(pw["config"]) & {"command", "args", "env", "headers", "auth"}
    vb = hub.mcp.show("vb")
    assert vb["config"]["url"] == "https://vb.example" and "auth" not in vb["config"] and vb["transport"] == "http"
    tok = hub.mcp.show("tok")
    assert "url" not in tok["config"] and tok["transport"] == "sse"  # host còn là ${…} → bỏ, không đoán
    assert tool_annotations("playwright", "browser_snapshot")["readOnlyHint"] is True
    assert hub.mcp.get_row("hubctl") is None and hub.mcp.get_row("hubctl", include_deleted=True)["deleted_at"]  # H-18: xoá mềm


def test_migration_v34_runs_on_store_open(hub_home):
    from cfharness.store import MIGRATIONS

    assert MIGRATIONS[-1] == "mcp_client.schema.to_claude_source"
    h = Hub(hub_home)
    h.store.conn.execute("PRAGMA user_version=33")
    h.store.conn.execute("INSERT INTO mcp_servers (name, transport, config, trust, source, created_at, updated_at) VALUES "
                         "('fb','stdio',?, 'trusted','claude-import','t','t')", (json.dumps({"transport": "stdio", "command": "node", "args": ["x.js"]}),))
    h.store.migrate()
    row = h.store.conn.execute("SELECT source, config FROM mcp_servers WHERE name='fb'").fetchone()
    assert row[0] == "claude" and "command" not in json.loads(row[1])
    h.store.close()


def test_rpc_add_refuses_hub_config(hub):
    from cfharness.mcp_server.rpc import IntegrationRpc

    rpc = IntegrationRpc.__new__(IntegrationRpc)
    rpc.hub = hub
    for cfg in ({"name": "x", "command": "npx"}, {"name": "x", "headers": {"A": "b"}}, {"name": "x", "source": "hub"}):
        with pytest.raises(HubError, match="mcp claude add"):
            rpc.mcp_add({"config": cfg})
    import inspect

    table = inspect.getsource(IntegrationRpc.methods)
    for gone in ("mcp.probe", "mcp.import", "mcp.catalog", "mcp.migrate_to_claude", "oauth"):
        assert gone not in table


@pytest.mark.parametrize("argv", [["x", "--url", "https://a"], ["x", "--", "npx", "-y", "pkg"], ["x", "--source", "hub"], ["x", "--env", "A=b"]])
def test_cli_add_legacy_forms_point_to_claude_add(argv, capsys):
    from types import SimpleNamespace

    from cfharness.mcp_server.cli import EXIT_USER, cmd_mcp

    a = build_parser().parse_args(["mcp", "add", *argv])
    calls = []
    assert cmd_mcp(SimpleNamespace(args=a, call=lambda *x, **k: calls.append(x), json=False)) == EXIT_USER
    assert calls == [] and "mcp claude add" in capsys.readouterr().err


def test_cli_add_and_edit_policy_flags():
    from types import SimpleNamespace

    from cfharness.mcp_server.cli import EXIT_OK, cmd_mcp

    calls = []

    def call(method, params=None, **k):
        calls.append((method, params))
        return {"name": "x", "transport": "stdio", "trust": "trusted", "config": {"tools": {}}, "account": None}

    out = SimpleNamespace(args=build_parser().parse_args(["mcp", "add", "x", "--trust", "trusted", "--exclude", "a,b", "--account", "machine"]),
                          call=call, json=False, out=lambda *a: None)
    assert cmd_mcp(out) == EXIT_OK
    assert calls[-1] == ("mcp.add", {"config": {"name": "x", "tools": {"exclude": ["a", "b"]}, "trust": "trusted"}, "account_id": "machine"})
    out.args = build_parser().parse_args(["mcp", "edit", "x", "--require-approval", "send_*"])
    assert cmd_mcp(out) == EXIT_OK
    assert calls[-1] == ("mcp.update", {"name": "x", "patch": {"tools": {"require_approval": ["send_*"]}}})
    for gone in (["mcp", "probe", "x"], ["mcp", "import"], ["mcp", "login", "x"], ["mcp", "migrate-to-claude", "x"]):
        with pytest.raises(SystemExit):
            build_parser().parse_args(gone)


def test_parse_mcp_tool():
    assert parse_mcp_tool("mcp__viec-nhom-2__tim_viec", ["viec-nhom-2"]) == ("viec-nhom-2", "tim_viec")
    assert parse_mcp_tool("mcp__a__b__c", ["a__b"]) == ("a__b", "c")
    assert parse_mcp_tool("Bash", []) is None


def test_readonly_registry_on_missing_db(tmp_path):
    from cfharness.mcp_client import registry_for

    reg = registry_for(tmp_path / "khong-co-hub")
    assert reg.list() == [] and isinstance(reg.conn, sqlite3.Connection)
