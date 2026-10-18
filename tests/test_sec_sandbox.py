"""Hardening SEC — sandbox OS cho Bash trong settings.json của run + H-18 (extra_args, MCP hub dưới tên khác).

Spike thật (claude 2.1.302, haiku) ghi ở `docs/dev-notes/hardening-SEC.md`: với khoá `sandbox` này, Bash của agent KHÔNG
ghi được policy/ (kể cả python/symlink/rm), không sửa hub.db, không đọc token/secret, không kết nối Unix socket (kể cả
từ cháu double-fork + setsid), `launchctl submit` thất bại; ghi workspace + mạng vẫn chạy. Test ở đây kiểm cấu hình sinh ra.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent_hub.auth import token_path
from agent_hub.guard.classifier import GuardContext, classify
from agent_hub.guard.policy import PolicyStore, package_dir
from agent_hub.guard.runtime import build_settings, check_extra_args, sandbox_settings
from agent_hub.guard.service import is_hub_mcp_config
from agent_hub.runtime.base import RunSpec
from agent_hub.runtime.claude_cli import ArgvError, build_argv


def _settings(tmp_path, **kw):
    hub = tmp_path / "hub"
    (hub / "policy").mkdir(parents=True, exist_ok=True)
    (hub / "agents" / "a" / "workspaces" / "r1").mkdir(parents=True)
    (hub / "agents" / "a" / "runtime").mkdir(parents=True)
    (hub / "agents" / "a" / "MEMORY.md").write_text("m")
    (hub / "hub.db").write_text("")
    sock = hub / "hub.sock"
    s = build_settings({"autoMemoryEnabled": False}, run_id="r1", socket=sock, policy_dir=hub / "policy",
                       guard_cfg={"hook_timeout_s": 900, **kw}, approval_timeout_s=300, cwd=hub / "agents" / "a" / "workspaces" / "r1")
    return hub, sock, s


def test_sandbox_blocks_hub_writes_reads_and_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
    hub, sock, s = _settings(tmp_path)
    sb = s["sandbox"]
    assert sb["enabled"] and sb["failIfUnavailable"] and sb["allowUnsandboxedCommands"] is False and sb["autoAllowBashIfSandboxed"] is False
    dw, dr = sb["filesystem"]["denyWrite"], sb["filesystem"]["denyRead"]
    for p in ("policy", "hub.db", "hub.db-wal", "config.yaml", "secrets", "mcp", "auth"):
        assert str(hub / p) in dw, p
    assert str(hub / "agents" / "a" / "MEMORY.md") in dw and str(hub / "agents" / "a" / "runtime") in dw
    assert os.path.realpath(package_dir()) in dw  # mã nguồn hook/guard
    # workspace của chính run KHÔNG bị chặn (không có tổ tiên nào của cwd trong denyWrite)
    cwd = str(hub / "agents" / "a" / "workspaces" / "r1")
    assert not any(cwd == d or cwd.startswith(d.rstrip("/") + "/") for d in dw)
    assert str(token_path(sock)) in dr and str(hub / "secrets") in dr and f"{hub}/agents/*/runtime" in dr
    # Unix socket: hub.sock không bao giờ được phép; ssh-agent được (git push)
    socks = sb["network"].get("allowUnixSockets") or []
    # R2-2: ssh-agent mặc định TẮT (agent không push được); người bật bằng guard.sandbox.allow_ssh_agent
    assert str(sock) not in socks and "/tmp/ssh-agent.sock" not in socks
    _, _, s_on = _settings(tmp_path / "on", sandbox={"allow_ssh_agent": True})
    assert "/tmp/ssh-agent.sock" in (s_on["sandbox"]["network"].get("allowUnixSockets") or [])
    from agent_hub.guard.runtime import DEFAULT_ALLOWED_DOMAINS  # R3-1: mặc định hẹp, không "*"
    assert sb["network"]["allowedDomains"] == DEFAULT_ALLOWED_DOMAINS
    assert not any("github" in d for d in sb["network"]["allowedDomains"])
    # tool Read/Grep/Glob của agent cũng không đọc được token/secret/mcp.json
    deny = s["permissions"]["deny"]
    assert f"Read(/{token_path(sock)})" in deny and f"Read(/{hub}/secrets/**)" in deny and f"Read(/{hub}/agents/*/runtime/**)" in deny
    json.dumps(s)  # settings.json hợp lệ


def test_sandbox_config_cannot_reopen_hub_socket_or_exclude_commands(tmp_path):
    hub, sock, s = _settings(tmp_path, sandbox={"allow_unix_sockets": [str(tmp_path / "hub" / "hub.sock")]})
    assert str(sock) not in (s["sandbox"]["network"].get("allowUnixSockets") or [])
    hub2 = tmp_path / "h2"
    (hub2 / "policy").mkdir(parents=True)
    s2 = build_settings({"sandbox": {"excludedCommands": ["python3:*"], "filesystem": {"denyWrite": ["/x"]}}}, run_id="r", socket=hub2 / "hub.sock",
                        policy_dir=hub2 / "policy", guard_cfg={}, approval_timeout_s=300)
    assert "excludedCommands" not in s2["sandbox"] and "/x" in s2["sandbox"]["filesystem"]["denyWrite"]
    # tắt được bằng cấu hình (vd nền tảng không có sandbox) — mặc định BẬT
    s3 = build_settings({}, run_id="r", socket=hub2 / "hub.sock", policy_dir=hub2 / "policy", guard_cfg={"sandbox": {"enabled": False}}, approval_timeout_s=300)
    assert "sandbox" not in s3


def test_eval_overlay_workspace_not_blocked(tmp_path):
    hub = tmp_path / "hub"
    (hub / "evals" / "ov1" / "workspaces" / "r").mkdir(parents=True)
    sb = sandbox_settings(hub, hub / "hub.sock", {}, cwd=hub / "evals" / "ov1" / "workspaces" / "r")
    assert str(hub / "evals") not in sb["filesystem"]["denyWrite"]


# ------------------------------------------------------------------------------------------- H-18 extra_args
@pytest.mark.parametrize("extra,kind", [
    (["--dangerously-skip-permissions"], "dir"),
    (["--allow-dangerously-skip-permissions"], "dir"),
    (["--dangerously-skip-permissions=true"], "scratch"),
    (["--permission-mode", "bypassPermissions"], "dir"),
    (["--permission-mode=bypassPermissions"], "dir"),
    (["--permission-mode=BypassPermissions"], "dir"),
    (["--settings", "/tmp/evil.json"], "scratch"),
    (["--settings=/tmp/evil.json"], "dir"),
    (["--setting-sources", "user"], "dir"),
    (["--mcp-config", "/tmp/m.json"], "dir"),
    (["--permission-prompt-tool", "mcp__x__y"], "dir"),
    (["--allowedTools", "Bash"], "dir"),
    (["--add-dir", "/"], "dir"),
])
def test_extra_args_bypass_variants_rejected(extra, kind):
    assert check_extra_args(extra, kind) is not None


def test_extra_args_legit_allowed_and_build_argv_enforces(tmp_path):
    assert check_extra_args(["--safe-mode"], "dir") is None
    assert check_extra_args(["--permission-mode", "bypassPermissions"], "scratch") is None  # NFR-15: scratch/worktree được
    spec = RunSpec(run_id="r", agent_id="a", prompt="p", cwd=tmp_path, session_id="s", raw_log_path=tmp_path / "r.jsonl",
                   stderr_log_path=tmp_path / "e.log", workspace_kind="dir", allowed_hooks=["PreToolUse"])
    with pytest.raises(ArgvError):
        build_argv(spec, {"binary": "claude", "isolation": {"extra_args": ["--permission-mode=bypassPermissions"]}})
    with pytest.raises(ArgvError):
        build_argv(spec, {"binary": "claude", "isolation": {"extra_args": ["--allow-dangerously-skip-permissions"]}})


# ------------------------------------------------------------------------------------------- H-18 MCP hub dưới tên khác
def test_hub_mcp_detected_by_command_or_url():
    assert is_hub_mcp_config({"command": "agent-hub", "args": ["mcp", "serve"]})
    assert is_hub_mcp_config({"command": "/usr/local/bin/agent-hub", "args": ["--home", "/x", "mcp", "serve", "--http"]})
    assert is_hub_mcp_config({"command": "python3", "args": ["-m", "agent_hub.mcp_server.server"]})
    assert is_hub_mcp_config({"url": "http://127.0.0.1:8765/mcp"})
    assert is_hub_mcp_config({"url": "http://localhost:9999/mcp"}, port=9999)
    assert not is_hub_mcp_config({"command": "python3", "args": ["-m", "agent_hub.mcp_server.hub_self"]})
    assert not is_hub_mcp_config({"url": "https://api.example.com/mcp"})


def test_hub_mcp_human_only_tools_forbidden_under_any_name(tmp_path):
    ps = PolicyStore(tmp_path / "hub", user_home=tmp_path)
    ctx = GuardContext(tmp_path, ps.load(), git_probe=False, hub_mcp_servers=frozenset({"ops"}))
    for tool in ("mcp__ops__hub_agent_update", "mcp__ops__hub_estop", "mcp__ops__hub_approve", "mcp__ops__hub_learn_rollback",
                 "mcp__whatever__hub_agent_update", "mcp__whatever__hub_estop", "mcp__ops__agent_update", "mcp__ops__hub_team_update"):
        v = classify(tool, {}, ctx)
        assert v.forbidden, (tool, v)
    assert not classify("mcp__ops__hub_status", {}, ctx).forbidden
    assert not classify("mcp__other__agent_update", {}, ctx).forbidden  # server khác, không phải hub
