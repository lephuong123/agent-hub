"""Import MCP từ ~/.claude.json GIẢ (FR-MC-04): 10 server, secret không lọt registry/DB, file nguồn không đổi."""

import hashlib
import json

import pytest

from cfharness.cli import main
from cfharness.daemon import spawn_background
from cfharness.hub import Hub
from cfharness.mcp_client.importer import convert_entry, looks_secret_value

SECRETS = [
    "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "s3cr3t-Pa55word!",
    "xoxb-1234567890-abcdefghijKLMNOP",
    "Zq8vN2mL5kR9tW1yX4bC7dF0",
    "tokenInArgs_9f8e7d6c5b4a3210ZZ",
    "QueryKey_7Hh2Jj4Kk6Ll8Mm0Nn",
]


def fake_claude_json(home):
    data = {
        "numStartups": 42,
        "mcpServers": {
            "viec-nhom-2": {"type": "http", "url": "https://viec.example/mcp", "headers": {"Authorization": f"Bearer {SECRETS[0]}"}},
            "vb": {"type": "stdio", "command": "npx", "args": ["-y", "vb-mcp"], "env": {"DB_PASSWORD": SECRETS[1], "LANG": "vi_VN"}},
            "slack": {"command": "slack-mcp", "args": [], "env": {"SLACK_BOT_TOKEN": SECRETS[2]}},
            "noi-bo": {"type": "http", "url": "https://noibo.example/mcp", "headers": {"X-Client": "agent", "X-Api": SECRETS[3]}},
            "cli-arg": {"command": "srv", "args": ["--token", SECRETS[4], "--verbose"]},
            "q": {"type": "http", "url": f"https://q.example/mcp?key={SECRETS[5]}&lang=vi"},
            "serena": {"command": "uvx", "args": ["--from", "git+https://github.com/oraios/serena", "serena", "start-mcp-server"]},
            "env-ref": {"command": "x-mcp", "env": {"API_KEY": "${MY_EXISTING_VAR}"}},
            "sse-one": {"type": "sse", "url": "https://sse.example/sse"},
            "plain": {"command": "/usr/local/bin/plain-mcp", "args": ["/tmp/data"]},
        },
        "projects": {str(home / "proj"): {"mcpServers": {"proj-only": {"command": "proj-mcp"}}}},
    }
    p = home / ".claude.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_import_all_in_process(hub_home, tmp_path):
    src = fake_claude_json(tmp_path)
    before = sha(src)
    hub = Hub(hub_home)
    try:
        plan = hub.mcp.import_claude(src, dry_run=True)
        assert len(plan["items"]) == 10 and all(i["status"] == "would_import" for i in plan["items"])
        assert hub.mcp.list() == []
        res = hub.mcp.import_claude(src)
        assert sum(1 for i in res["items"] if i["status"] == "imported") == 10
        rows = {r["name"]: r for r in hub.mcp.list()}
        assert len(rows) == 10
        assert all(r["trust"] == "trusted" and r["source"] == "claude-import" for r in rows.values())  # Q-05
        assert rows["viec-nhom-2"]["config"]["headers"]["Authorization"].startswith("Bearer ${MCP_VIEC_NHOM_2_")
        assert rows["vb"]["config"]["env"]["LANG"] == "vi_VN"
        assert rows["cli-arg"]["config"]["args"][0] == "--token" and rows["cli-arg"]["config"]["args"][1].startswith("${")
        assert rows["env-ref"]["config"]["env"]["API_KEY"] == "${MY_EXISTING_VAR}"
        assert rows["serena"]["config"]["args"][1] == "git+https://github.com/oraios/serena"
        assert rows["sse-one"]["transport"] == "sse"
        # secret dạng rõ không có trong registry; có trong secret store và resolve lại được
        dump = json.dumps([r["config"] for r in rows.values()])
        for s in SECRETS:
            assert s not in dump
        assert hub.mcp.resolved_entry("vb")["env"]["DB_PASSWORD"] == SECRETS[1]
        assert hub.mcp.resolved_entry("q")["url"].endswith(f"key={SECRETS[5]}&lang=vi")
        # chạy lại → bỏ qua server đã có
        again = hub.mcp.import_claude(src)
        assert all(i["status"] == "skipped_exists" for i in again["items"])
        # project-level
        res = hub.mcp.import_claude(src, project=str(tmp_path / "proj"), select=["proj-only"])
        assert [i["status"] for i in res["items"]] == ["imported"]
        with pytest.raises(Exception, match="không có"):
            hub.mcp.import_claude(src, select=["khong-ton-tai"])
    finally:
        hub.store.close()
    assert sha(src) == before  # ~/.claude.json không đổi
    # không có giá trị secret trong hub.db (+ WAL), registry.yaml, logs
    blobs = b""
    for p in [hub_home.db, hub_home.db.with_name("hub.db-wal"), hub_home.mcp_registry, *hub_home.logs_dir.glob("*")]:
        if p.exists() and p.is_file():
            blobs += p.read_bytes()
    for s in SECRETS:
        assert s.encode() not in blobs


def test_import_cli_via_daemon_and_home_isolated(hub_home, tmp_path, monkeypatch, capsys):
    """Đường CLI thật: `cfharness mcp import --from claude --file <giả> --all` qua daemon."""
    import yaml

    hub_home.config.write_text(yaml.safe_dump({"runtime": {"kind": "fake"}}))
    fake_home = tmp_path / "fakehome"
    fake_home.mkdir()
    src = fake_claude_json(fake_home)
    before = sha(src)
    monkeypatch.setenv("HOME", str(fake_home))  # mặc định đọc ~/.claude.json GIẢ
    spawn_background(hub_home)
    try:
        assert main(["mcp", "import", "--from", "claude", "--all", "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["source"] == str(src) and len(out["items"]) == 10
        assert main(["mcp", "list", "--json"]) == 0
        assert len(json.loads(capsys.readouterr().out)) == 10
        assert main(["secret", "list", "--json"]) == 0
        names = [r["name"] for r in json.loads(capsys.readouterr().out)]
        assert "MY_EXISTING_VAR" in names  # tham chiếu sẵn có nhưng chưa có giá trị → báo thiếu
        assert main(["audit", "list", "--kind", "mcp.import", "--json"]) == 0
        au = json.loads(capsys.readouterr().out)
        assert au and au[0]["channel"] == "cli"
    finally:
        main(["daemon", "stop"])
    assert sha(src) == before


def test_convert_entry_heuristics():
    cfg, secrets, _ = convert_entry("x", {"command": "c", "args": ["--api-key=Abc123Def456Ghi789Jkl0", "-e", "GITHUB_TOKEN=ghp_xx11yy22zz33aa44bb55"]})
    assert cfg["args"][0] == "--api-key=${MCP_X_API_KEY}"
    assert cfg["args"][2] == "GITHUB_TOKEN=${MCP_X_GITHUB_TOKEN}"
    assert len(secrets) == 2
    assert not looks_secret_value("/Users/me/some/long/path/file.txt")
    assert not looks_secret_value("https://example.com/a/b/c/d")
    assert looks_secret_value("aZ3kP9qL2mN8xR5tW7yB")
