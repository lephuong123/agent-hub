"""Che theo GIÁ TRỊ secret trong cấu hình MCP do `claude` giữ (plan-mcp-claude-native PR1 — phần còn lại).

`mcp_client/claude_redact.py`: header/env/args/url (userinfo, query) trong `.claude.json` `mcpServers` (+ `projects[*]`) và
`remote-settings.json` `managedMcpServers` của cấu hình mặc định + mọi thư mục tài khoản token → `redact.register_secret_values`.
Đọc lại khi mtime/size đổi; gọi ở `Hub.start` và lúc run khởi động. Không gọi `claude`; file giả trong thư mục tạm.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from cfharness.mcp_client import claude_redact
from cfharness.mcp_client.claude_redact import ClaudeConfigRedactor, entry_secret_values
from cfharness.redact import mask_values, redact_str
from m7_fake import MgrScript, close, make_daemon


def _write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def chome(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "claude-default"
    d.mkdir()
    monkeypatch.setenv("CFHARNESS_TEST_CLAUDE_HOME", str(d))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return d


def test_entry_values_heuristics():
    entry = {
        "type": "http",
        "url": "https://user:UrlPassw0rd-XYZ123@mcp.example/sse?api_key=QueryKey-9988776655&page=1",
        "headers": {"Authorization": "Bearer HdrTok-aaaa1111bbbb", "Content-Type": "application/json", "X-Api-Key": "XKeyValue77"},
        "env": {"PATH": "/usr/local/bin:/usr/bin", "GITHUB_TOKEN": "EnvTok-5566778899", "LOG_LEVEL": "debugging"},
        "args": ["--token", "ArgTok-1234567890", "--port", "8080", "--verbose", "Zq8Lk2Vp9Xw4Rt7Ys1Nb6Mc3"],
    }
    vals = set(entry_secret_values(entry))
    for v in ("UrlPassw0rd-XYZ123", "QueryKey-9988776655", "HdrTok-aaaa1111bbbb", "XKeyValue77", "EnvTok-5566778899",
              "ArgTok-1234567890", "Zq8Lk2Vp9Xw4Rt7Ys1Nb6Mc3"):
        assert v in vals, v
    for v in ("application/json", "/usr/local/bin:/usr/bin", "debugging", "8080", "Bearer HdrTok-aaaa1111bbbb", "mcp.example"):
        assert v not in vals, v
    assert entry_secret_values({"env": {"API_KEY": "short"}}) == []  # < MIN_VALUE_LEN
    assert entry_secret_values({"env": {"API_KEY": "${MY_SECRET}"}}) == []  # tham chiếu, không phải giá trị
    assert entry_secret_values("bad") == [] and entry_secret_values({"url": "http://[bad"}) == []


def test_refresh_default_and_account_dirs_mtime(chome, tmp_path, caplog):
    home = tmp_path / "hub"
    acct = home / "claude-config" / "a_0123abcd"
    _write(chome / ".claude.json", {"mcpServers": {"jira": {"type": "http", "url": "https://j.example/mcp",
                                                            "headers": {"Authorization": "Bearer DefaultHdr-PR1-0001"}}},
                                    "projects": {"/x": {"mcpServers": {"p": {"command": "x", "env": {"DB_PASSWORD": "ProjPw-PR1-0002"}}}}}})
    _write(chome / ".claude" / "remote-settings.json",
           {"managedMcpServers": {"org": {"type": "http", "url": "https://o.example", "headers": {"X-Org-Token": "OrgTok-PR1-0003"}}}})
    _write(acct / ".claude.json", {"mcpServers": {"gh": {"command": "gh-mcp", "env": {"GITHUB_TOKEN": "AcctTok-PR1-0004"}}}})
    (home / "claude-config" / "not-an-account").mkdir()
    _write(home / "claude-config" / "not-an-account" / ".claude.json", {"mcpServers": {"z": {"env": {"API_KEY": "Ignored-PR1-0099"}}}})
    files = [str(p) for p in claude_redact.config_files(home)]
    assert str(acct / ".claude.json") in files and not any("not-an-account" in f for f in files)
    r = ClaudeConfigRedactor(home)
    with caplog.at_level(logging.DEBUG):
        assert r.refresh() == 3
    for v in ("DefaultHdr-PR1-0001", "ProjPw-PR1-0002", "OrgTok-PR1-0003", "AcctTok-PR1-0004"):
        assert mask_values(f"err: {v}!") == "err: ***!", v
        assert v not in caplog.text
    assert "Ignored-PR1-0099" in mask_values("Ignored-PR1-0099")
    assert redact_str('{"h": "DefaultHdr-PR1-0001"}') == '{"h": "***"}'
    # không đổi → chỉ stat; đổi (thêm server) → đọc lại đúng file đó
    assert r.refresh() == 0
    p = acct / ".claude.json"
    _write(p, {"mcpServers": {"gh": {"env": {"GITHUB_TOKEN": "AcctTok-PR1-0004"}}, "new": {"headers": {"Authorization": "Bearer NewHdr-PR1-0005"}}}})
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert r.refresh() == 1
    assert mask_values("NewHdr-PR1-0005") == "***"
    # thư mục bổ sung (tài khoản đang dùng ngoài claude-config) + file hỏng không làm lỗi
    extra = tmp_path / "extra"
    _write(extra / ".claude.json", {"mcpServers": {"e": {"env": {"SECRET": "ExtraSec-PR1-0006"}}}})
    (chome / ".claude.json").write_text("{bad json", encoding="utf-8")
    assert r.refresh([extra, None]) == 2
    assert mask_values("ExtraSec-PR1-0006") == "***"


async def test_hub_start_and_run_start_register(chome, hub_home):
    _write(chome / ".claude.json", {"mcpServers": {"s": {"type": "http", "url": "https://s.example",
                                                         "headers": {"Authorization": "Bearer StartHdr-PR1-0007"}}}})
    dm = await make_daemon(hub_home, MgrScript())
    try:
        assert mask_values("StartHdr-PR1-0007") == "***"  # Hub.start
        acct = hub_home.home / "claude-config" / "a_0000beef"
        _write(acct / ".claude.json", {"mcpServers": {"g": {"env": {"API_TOKEN": "RunStart-PR1-0008"}}}})
        dm.hub.runs._refresh_claude_redact(acct)  # lượt khởi động run (tài khoản đang dùng)
        assert mask_values("RunStart-PR1-0008") == "***"
        # hỏng → chỉ cảnh báo, không làm hỏng run
        dm.hub.claude_redact = object()
        dm.hub.runs._refresh_claude_redact(None)
    finally:
        await close(dm)
