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

from cfharness.auth import token_path
from cfharness.guard.classifier import GuardContext, classify
from cfharness.guard.policy import PolicyStore, package_dir
from cfharness.guard.runtime import build_settings, check_extra_args, rule_path, sandbox_settings, sandbox_supported, sb_path
from cfharness.guard.service import is_hub_mcp_config
from cfharness.runtime.base import RunSpec
from cfharness.runtime.claude_cli import ArgvError, build_argv


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
    # Windows: sandbox chưa có → không fail-closed (CLI sẽ từ chối mọi run) — xem docs/dev-notes/windows.md
    assert sb["enabled"] and sb["failIfUnavailable"] is sandbox_supported() and sb["allowUnsandboxedCommands"] is False and sb["autoAllowBashIfSandboxed"] is False
    dw, dr = sb["filesystem"]["denyWrite"], sb["filesystem"]["denyRead"]
    for p in ("policy", "hub.db", "hub.db-wal", "config.yaml", "secrets", "mcp", "auth"):
        assert sb_path(hub / p) in dw, p
    assert sb_path(hub / "agents" / "a" / "MEMORY.md") in dw and sb_path(hub / "agents" / "a" / "runtime") in dw
    assert sb_path(os.path.realpath(package_dir())) in dw  # mã nguồn hook/guard
    # workspace của chính run KHÔNG bị chặn (không có tổ tiên nào của cwd trong denyWrite)
    cwd = sb_path(hub / "agents" / "a" / "workspaces" / "r1")
    assert not any(cwd == d or cwd.startswith(d.rstrip("/") + "/") for d in dw)
    assert sb_path(token_path(sock)) in dr and sb_path(hub / "secrets") in dr and sb_path(f"{hub}/agents/*/runtime") in dr
    # Unix socket: hub.sock không bao giờ được phép; ssh-agent được (git push)
    socks = sb["network"].get("allowUnixSockets") or []
    # R2-2: ssh-agent mặc định TẮT (agent không push được); người bật bằng guard.sandbox.allow_ssh_agent
    assert sb_path(sock) not in socks and "/tmp/ssh-agent.sock" not in socks
    _, _, s_on = _settings(tmp_path / "on", sandbox={"allow_ssh_agent": True})
    assert "/tmp/ssh-agent.sock" in (s_on["sandbox"]["network"].get("allowUnixSockets") or [])
    from cfharness.guard.runtime import DEFAULT_ALLOWED_DOMAINS  # R3-1: mặc định hẹp, không "*"
    assert sb["network"]["allowedDomains"] == DEFAULT_ALLOWED_DOMAINS
    assert not any("github" in d for d in sb["network"]["allowedDomains"])
    # tool Read/Grep/Glob của agent cũng không đọc được token/secret/mcp.json
    deny = s["permissions"]["deny"]
    assert f"Read(/{rule_path(token_path(sock))})" in deny and f"Read(/{rule_path(hub)}/secrets/**)" in deny and f"Read(/{rule_path(hub)}/agents/*/runtime/**)" in deny
    json.dumps(s)  # settings.json hợp lệ


def test_sandbox_config_cannot_reopen_hub_socket_or_exclude_commands(tmp_path):
    hub, sock, s = _settings(tmp_path, sandbox={"allow_unix_sockets": [str(tmp_path / "hub" / "hub.sock")]})
    assert sb_path(sock) not in (s["sandbox"]["network"].get("allowUnixSockets") or [])
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
    assert sb_path(hub / "evals") not in sb["filesystem"]["denyWrite"]


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
    assert is_hub_mcp_config({"command": "cfharness", "args": ["mcp", "serve"]})
    assert is_hub_mcp_config({"command": "/usr/local/bin/cfharness", "args": ["--home", "/x", "mcp", "serve", "--http"]})
    assert is_hub_mcp_config({"command": "python3", "args": ["-m", "cfharness.mcp_server.server"]})
    assert is_hub_mcp_config({"url": "http://127.0.0.1:8765/mcp"})
    assert is_hub_mcp_config({"url": "http://localhost:9999/mcp"}, port=9999)
    assert not is_hub_mcp_config({"command": "python3", "args": ["-m", "cfharness.mcp_server.hub_self"]})
    assert not is_hub_mcp_config({"url": "https://api.example.com/mcp"})


def test_hub_mcp_human_only_tools_forbidden_under_any_name(tmp_path):
    ps = PolicyStore(tmp_path / "hub", user_home=tmp_path)
    ctx = GuardContext(tmp_path, ps.load(), git_probe=False, hub_mcp_servers=frozenset({"ops"}))
    for tool in ("mcp__ops__hub_agent_update", "mcp__ops__hub_estop", "mcp__ops__hub_approve", "mcp__ops__hub_learn_rollback",
                 "mcp__whatever__hub_agent_update", "mcp__whatever__hub_estop", "mcp__ops__agent_update", "mcp__ops__hub_team_update"):
        v = classify(tool, {}, ctx)
        assert v.forbidden, (tool, v)
    # server trỏ về CHÍNH hub (H-18): MỌI tool bị cấm, kể cả tool đọc — agent đọc trạng thái hub qua hub-self của run
    assert classify("mcp__ops__hub_status", {}, ctx).forbidden
    assert not classify("mcp__other__agent_update", {}, ctx).forbidden  # server khác, không phải hub


def test_guard_off_drops_hook_but_keeps_sandbox_and_static_deny(tmp_path):
    """guard.enabled=false: không hook PreToolUse (không hỏi duyệt) nhưng sandbox OS + permissions.deny tĩnh vẫn áp."""
    hub = tmp_path / "hub"
    (hub / "policy").mkdir(parents=True)
    sock = hub / "hub.sock"
    s = build_settings({}, run_id="r", socket=sock, policy_dir=hub / "policy", guard_cfg={"hook_timeout_s": 900},
                       approval_timeout_s=300, cwd=hub / "ws", hook=False)
    assert "PreToolUse" not in (s.get("hooks") or {})
    assert s["sandbox"]["enabled"] and sb_path(hub / "policy") in s["sandbox"]["filesystem"]["denyWrite"]
    assert sb_path(token_path(sock)) in s["sandbox"]["filesystem"]["denyRead"]
    assert f"Read(/{rule_path(hub)}/secrets/**)" in s["permissions"]["deny"]


# H-18 (mở rộng ở plan-mcp-claude-native PR7, commit 0bc3cd5): server trỏ về CHÍNH hub = điều khiển hub bằng DANH TÍNH NGƯỜI (token của
# `cfharness mcp serve`) → MỌI tool bị cấm, kể cả tool ĐỌC (đọc run/prompt/chi phí của mọi agent, không truy được agent nào gọi) và tool
# hub thêm sau này. Agent làm việc với hub qua `hub-self` (ràng run, theo vai trò).
HUB_TOOLS_ANY = ("hub_status", "hub_runs", "hub_run_show", "hub_agents", "hub_events", "hub_run", "hub_cancel", "hub_steer",
                 "hub_task_create", "hub_approve", "hub_estop", "a_tool_added_later")


@pytest.mark.parametrize("tool", HUB_TOOLS_ANY)
def test_every_tool_of_detected_hub_server_is_forbidden(tmp_path, tool):
    ps = PolicyStore(tmp_path / "hub", user_home=tmp_path)
    ctx = GuardContext(tmp_path, ps.load(), git_probe=False, hub_mcp_servers=frozenset({"ops"}))
    v = classify(f"mcp__ops__{tool}", {}, ctx)
    assert v.forbidden and v.rule == "hub_mcp_forbidden", (tool, v)


@pytest.mark.parametrize("name", ["cfharness", "agent-hub", "agent_hub", "hub"])
def test_reserved_hub_names_forbidden_without_detection(tmp_path, name):
    """Tên dành riêng của hub (`names.HUB_MCP_SERVER_NAMES`) bị chặn kể cả khi không nhận ra theo lệnh/URL (vd bọc qua wrapper)."""
    ps = PolicyStore(tmp_path / "hub", user_home=tmp_path)
    ctx = GuardContext(tmp_path, ps.load(), git_probe=False)
    for tool in ("hub_status", "hub_run", "anything"):
        v = classify(f"mcp__{name}__{tool}", {}, ctx)
        assert v.forbidden and v.rule == "hub_mcp_forbidden", (name, tool, v)


def test_hub_server_registered_under_unusual_name(tmp_path):
    """Tên server có ký tự claude thay bằng `_` trong tên tool (`My Hub (local)` → `My_Hub__local_`) vẫn bị nhận ra."""
    from cfharness.mcp_client.claude_source import tool_server_name

    ps = PolicyStore(tmp_path / "hub", user_home=tmp_path)
    ctx = GuardContext(tmp_path, ps.load(), git_probe=False, hub_mcp_servers=frozenset({"My Hub (local)"}))
    tsn = tool_server_name("My Hub (local)")
    assert tsn == "My_Hub__local_"  # tên đã chuẩn hoá CHỨA "__" — trước đây guard tách sai server/tool ở đây
    for tool in ("hub_status", "hub_approve", "anything"):
        v = classify(f"mcp__{tsn}__{tool}", {}, ctx)
        assert v.forbidden and v.rule == "hub_mcp_forbidden", (tool, v)
    assert not classify("mcp__My_Hub__search", {}, ctx).forbidden  # server khác chỉ trùng phần đầu tên


@pytest.mark.parametrize("tool,safe", [
    ("task_complete", False), ("task_show", True), ("memory_search", True), ("memory_recall", True), ("team_roster", True),
])
def test_hub_self_tools_not_affected_by_hub_server_block(tmp_path, tool, safe):
    """`hub-self` (MCP của RUN, ràng run_id) KHÔNG bị luật chặn server hub — đó là kênh hợp lệ của agent với hub."""
    ps = PolicyStore(tmp_path / "hub", user_home=tmp_path)
    ctx = GuardContext(tmp_path, ps.load(), git_probe=False, hub_mcp_servers=frozenset({"ops", "hub"}))
    v = classify(f"mcp__hub-self__{tool}", {}, ctx)
    assert not v.forbidden and v.rule == "hub_self_tool" and (v.verdict == "safe") == safe, (tool, v)


@pytest.mark.parametrize("tool", [
    "mcp__atlassian__getJiraIssue", "mcp__claude_ai_Google_Drive__search_files", "mcp__github__list_repos",
    "mcp__hubspot__search_contacts", "mcp__my-hub-tools__lookup", "mcp__docs__search", "mcp__playwright__browser_navigate",
])
def test_third_party_mcp_not_blocked_by_hub_rules(tmp_path, tool):
    """MCP bên thứ ba (kể cả tên chứa "hub": github, hubspot…) KHÔNG bị chặn nhầm — agent dùng chung MCP của tài khoản (PR7)."""
    ps = PolicyStore(tmp_path / "hub", user_home=tmp_path)
    ctx = GuardContext(tmp_path, ps.load(), git_probe=False, hub_mcp_servers=frozenset({"ops"}))
    v = classify(tool, {}, ctx)
    assert not v.forbidden and v.rule == "mcp_third_party", (tool, v)


def test_hub_human_only_tool_names_forbidden_on_unrecognised_server(tmp_path):
    """Server hub đăng ký dưới tên LẠ mà không nhận ra (wrapper, URL qua proxy): tool chỉ-người vẫn bị chặn theo TÊN tool;
    tool đọc thì không (không phân biệt được với MCP bên thứ ba) — vì vậy lớp nhận diện theo lệnh/URL + gỡ khỏi run là chính."""
    ps = PolicyStore(tmp_path / "hub", user_home=tmp_path)
    ctx = GuardContext(tmp_path, ps.load(), git_probe=False)
    for tool in ("hub_approve", "hub_agent_update", "hub_estop", "hub_team_update", "hub_learn_rollback"):
        v = classify(f"mcp__weird__{tool}", {}, ctx)
        assert v.forbidden, (tool, v)
    assert not classify("mcp__weird__hub_status", {}, ctx).forbidden
