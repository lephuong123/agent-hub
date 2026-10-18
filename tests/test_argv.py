"""Bộ dựng argv của ClaudeCliRuntime (FR-RT-02/03/08/11, NFR-15). Không gọi `claude`."""

import json
from pathlib import Path

import pytest

from agent_hub.config import DEFAULTS
from agent_hub.runtime import ArgvError, RunSpec, build_argv
from agent_hub.runtime.claude_cli import _redact_for_storage, child_env


def spec(**kw):
    base = dict(
        run_id="r_1",
        agent_id="coder",
        prompt="tóm tắt README",
        cwd=Path("/tmp/w"),
        session_id="11111111-2222-3333-4444-555555555555",
        raw_log_path=Path("/tmp/r.jsonl"),
        stderr_log_path=Path("/tmp/r.err"),
    )
    base.update(kw)
    return RunSpec(**base)


def cfg(**iso):
    c = {**DEFAULTS["runtime"]["claude"]}
    c["isolation"] = {**c["isolation"], **iso}
    return c


def flag_value(argv, flag):
    return argv[argv.index(flag) + 1]


def test_basic_argv():
    argv, stdin = build_argv(
        spec(
            model="sonnet",
            effort="high",
            fallback_model="haiku",
            system_prompt="SOUL",
            max_budget_usd=0.3,
            mcp_config_path=Path("/h/agents/coder/runtime/r_1/mcp.json"),
            settings_path=Path("/h/agents/coder/runtime/r_1/settings.json"),
            tool_policy={"allowed_tools": ["Read", "Grep"], "permission_mode": "acceptEdits"},
            display_name="coder:r_1",
        ),
        cfg(),
    )
    # mặc định theo spike: prompt qua stdin dạng user message stream-json
    assert json.loads(stdin) == {"type": "user", "message": {"role": "user", "content": "tóm tắt README"}}
    assert argv[:4] == ["claude", "-p", "--input-format", "stream-json"]
    assert argv[4:7] == ["--output-format", "stream-json", "--verbose"]
    assert "--replay-user-messages" in argv and "--include-hook-events" in argv
    assert "tóm tắt README" not in argv
    # cô lập theo spike A-04/A-05
    assert flag_value(argv, "--setting-sources") == ""
    assert "--include-partial-messages" in argv
    assert flag_value(argv, "--session-id") == "11111111-2222-3333-4444-555555555555"
    assert flag_value(argv, "--append-system-prompt") == "SOUL"
    assert flag_value(argv, "--model") == "sonnet"
    assert flag_value(argv, "--effort") == "high"
    assert flag_value(argv, "--fallback-model") == "haiku"
    assert flag_value(argv, "--max-budget-usd") == "0.3"
    assert flag_value(argv, "--mcp-config").endswith("mcp.json")
    assert "--strict-mcp-config" in argv
    assert flag_value(argv, "--settings").endswith("settings.json")
    assert flag_value(argv, "--permission-mode") == "acceptEdits"
    assert flag_value(argv, "--name") == "coder:r_1"
    # variadic ở cuối
    i = argv.index("--allowedTools")
    assert argv[i + 1 : i + 3] == ["Read", "Grep"] and len(argv) == i + 3
    assert "--bare" not in argv and "--dangerously-skip-permissions" not in argv


def test_isolation_flags_configurable():
    argv, _ = build_argv(spec(settings_path=Path("/s.json")), cfg(setting_sources="", extra_args=["--safe-mode"]))
    assert flag_value(argv, "--setting-sources") == ""
    assert "--safe-mode" in argv
    argv, _ = build_argv(spec(settings_path=Path("/s.json")), cfg(setting_sources=["project", "local"], pass_settings_file=False, strict_mcp_config=False))
    assert flag_value(argv, "--setting-sources") == "project,local"
    assert "--settings" not in argv and "--strict-mcp-config" not in argv


def test_snapshot_and_replace_mode():
    c = cfg()
    c["system_prompt_snapshot"] = "on"
    c["system_prompt_mode"] = "replace"
    argv, _ = build_argv(spec(system_prompt="X"), c)
    assert flag_value(argv, "--system-prompt") == "X" and "--append-system-prompt" not in argv
    assert flag_value(argv, "--system-prompt-snapshot") == "on"


def test_resume_and_fork():
    argv, _ = build_argv(spec(resume_session_id="abc"), cfg())
    assert flag_value(argv, "--resume") == "abc" and "--session-id" not in argv and "--fork-session" not in argv
    argv, _ = build_argv(spec(resume_session_id="abc", fork=True), cfg())
    assert "--fork-session" in argv


def test_text_input_mode():
    c = cfg()
    c["input_format"] = "text"
    argv, stdin = build_argv(spec(), c)
    assert stdin is None and argv[:3] == ["claude", "-p", "tóm tắt README"]
    assert "--input-format" not in argv and "--replay-user-messages" not in argv
    c["input_format"] = "bogus"
    with pytest.raises(ArgvError):
        build_argv(spec(), c)


def test_setting_sources_can_be_disabled():
    argv, _ = build_argv(spec(), cfg(setting_sources=None))
    assert "--setting-sources" not in argv


def test_prompt_starting_with_dash_goes_to_stdin():
    c = cfg()
    c["input_format"] = "text"
    argv, stdin = build_argv(spec(prompt="--help me"), c)
    assert stdin == b"--help me" and "--help me" not in argv
    assert argv[1] == "-p" and argv[2] == "--output-format"


def test_bypass_guard_nfr15():
    tp = {"permission_mode": "bypassPermissions"}
    with pytest.raises(ArgvError, match="allow_bypass"):
        build_argv(spec(tool_policy=tp), cfg())
    with pytest.raises(ArgvError, match="repo thật"):
        build_argv(spec(tool_policy={**tp, "allow_bypass": True}, workspace_kind="dir"), cfg())
    argv, _ = build_argv(spec(tool_policy={**tp, "allow_bypass": True}, workspace_kind="scratch"), cfg())
    assert flag_value(argv, "--permission-mode") == "bypassPermissions"
    with pytest.raises(ArgvError):
        build_argv(spec(), cfg(extra_args=["--dangerously-skip-permissions"]))


def test_default_permission_mode_and_tools():
    c = cfg()
    c["default_permission_mode"] = "dontAsk"
    argv, _ = build_argv(spec(tool_policy={"tools": [], "disallowed_tools": ["Bash(rm:*)"]}), c)
    assert flag_value(argv, "--permission-mode") == "dontAsk"
    assert flag_value(argv, "--tools") == ""
    assert argv[-2:] == ["--disallowedTools", "Bash(rm:*)"]


def test_budget_formatting_and_validation():
    argv, _ = build_argv(spec(max_budget_usd=0.25), cfg())
    assert flag_value(argv, "--max-budget-usd") == "0.25"
    with pytest.raises(ArgvError):
        build_argv(spec(max_budget_usd=0), cfg())


def test_redaction_for_storage():
    argv, _ = build_argv(spec(system_prompt="S" * 500, prompt="key sk-ant-abcdefghijklmnopqrstu"), cfg())
    red = _redact_for_storage(argv, spec(prompt="key sk-ant-abcdefghijklmnopqrstu"))
    assert "sk-ant-abcdefghijklmnopqrstu" not in " ".join(red)
    assert "<system-prompt 500 chars>" in red


def test_child_env_strips_parent_claude_session():
    env = child_env(cfg(), {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": "/bin", "HOME": "/h"})
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env and env["PATH"] == "/bin"
    assert env["MCP_TOOL_TIMEOUT"] == "900000"  # spike A-13
    assert "CLAUDE_CONFIG_DIR" not in env  # spike: đặt riêng làm mất đăng nhập OAuth


def test_default_settings_disable_auto_memory():
    assert DEFAULTS["runtime"]["claude"]["settings"]["autoMemoryEnabled"] is False
