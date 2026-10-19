"""Hook PreToolUse fail-CLOSED và MCP `hub-guard` (permission-prompt-tool) — giao thức + lỗi (FR-SG-03 bước 3)."""

from __future__ import annotations

import json
import subprocess
import sys

from cfharness.guard.runtime import build_settings, guard_mcp_entry, hook_command, pattern_matches, policy_deny_rules, strip_for_tickets

HOOK_IN = {"session_id": "s", "cwd": "/tmp", "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "t1"}


def run_hook(args, stdin):
    p = subprocess.run([sys.executable, "-I", "-m", "cfharness.guard.hook", *args], input=stdin, capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout, p.stderr


def decision(out):
    return json.loads(out.strip().splitlines()[-1])["hookSpecificOutput"]


def test_hook_daemon_unreachable_denies(tmp_path):
    rc, out, _ = run_hook(["--run", "r_x", "--socket", str(tmp_path / "nope.sock"), "--timeout", "5"], json.dumps(HOOK_IN))
    d = decision(out)
    assert rc == 0 and d["permissionDecision"] == "deny" and "fail-closed" in d["permissionDecisionReason"]


def test_hook_bad_input_denies(tmp_path):
    rc, out, _ = run_hook(["--run", "r_x", "--socket", str(tmp_path / "s.sock")], "not json{")
    assert rc == 0 and decision(out)["permissionDecision"] == "deny"


def test_hook_daemon_silent_times_out_denies(tmp_path):
    """Daemon nhận kết nối nhưng không trả lời → hook tự hết giờ (trước timeout CLI) và deny."""
    import socket
    import threading

    import tempfile

    path = tempfile.mkdtemp(prefix="ahg", dir="/tmp") + "/h.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    conns = []
    threading.Thread(target=lambda: conns.append(srv.accept()), daemon=True).start()
    rc, out, _ = run_hook(["--run", "r_x", "--socket", path, "--timeout", "1"], json.dumps(HOOK_IN))
    assert rc == 0 and decision(out)["permissionDecision"] == "deny"
    srv.close()


def test_hook_command_wraps_exit2(tmp_path):
    cmd = hook_command("r_1", tmp_path / "x.sock", 60, {"python": "/nonexistent/python"})
    assert cmd.endswith("|| exit 2") and "-I -m cfharness.guard.hook" in cmd
    p = subprocess.run(cmd, shell=True, input=json.dumps(HOOK_IN), capture_output=True, text=True)
    assert p.returncode == 2  # python hỏng → exit 2 = CLI chặn (không fail-open)


def test_settings_and_mcp_entry(tmp_path):
    s = build_settings({"autoMemoryEnabled": False}, run_id="r_1", socket=tmp_path / "s.sock", policy_dir=tmp_path / "policy", guard_cfg={"hook_timeout_s": 900}, approval_timeout_s=300)
    h = s["hooks"]["PreToolUse"][0]
    assert h["matcher"] == "*" and h["hooks"][0]["timeout"] >= 420 and "--timeout 870" in h["hooks"][0]["command"]
    assert s["autoMemoryEnabled"] is False
    assert f"Write(/{tmp_path}/policy/**)" in s["permissions"]["deny"] and any(r.startswith("Edit(//") for r in s["permissions"]["deny"])
    # approval.timeout lớn hơn → timeout hook tự nâng
    s2 = build_settings({}, run_id="r", socket=tmp_path / "s", policy_dir=tmp_path, guard_cfg={"hook_timeout_s": 100}, approval_timeout_s=600)
    assert s2["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == 720
    e = guard_mcp_entry("r_1", tmp_path / "s.sock", tmp_path, {}, 900000)
    assert e["args"][:3] == ["-I", "-m", "cfharness.guard.mcp_guard"] and "870" in e["args"]
    assert policy_deny_rules(tmp_path / "p")[0].startswith("Edit(//")


def test_pattern_matching_and_ticket_strip():
    assert pattern_matches("Bash(git push --force:*)", "Bash", {"command": "cd x && git push --force origin main"})
    assert not pattern_matches("Bash(git push --force:*)", "Bash", {"command": "git push origin main"})
    assert pattern_matches("Bash", "Bash", {"command": "ls"})
    assert pattern_matches("Edit(//data/p/**)", "Edit", {"file_path": "/data/p/x"})
    keep, removed = strip_for_tickets(
        ["Bash(git push --force:*)", "Bash(mkfs:*)"], [{"tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}}]
    )
    assert removed == ["Bash(git push --force:*)"] and keep == ["Bash(mkfs:*)"]


def _mcp(lines, sock):
    p = subprocess.run(
        [sys.executable, "-I", "-m", "cfharness.guard.mcp_guard", "--run", "r_x", "--socket", sock, "--timeout", "5"],
        input="".join(json.dumps(m) + "\n" for m in lines),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return [json.loads(x) for x in p.stdout.splitlines() if x.strip()]


def test_mcp_guard_protocol_and_fail_closed(tmp_path):
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-code", "version": "2"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "approve", "arguments": {"tool_name": "Bash", "input": {"command": "rm -rf x"}, "tool_use_id": "t"}}},
    ]
    out = {m["id"]: m for m in _mcp(msgs, str(tmp_path / "none.sock"))}
    assert out[1]["result"]["serverInfo"]["name"] == "hub-guard" and out[1]["result"]["protocolVersion"] == "2025-06-18"
    assert [t["name"] for t in out[2]["result"]["tools"]] == ["approve"]
    res = out[3]["result"]
    assert "structuredContent" not in res and len(res["content"]) == 1 and res["content"][0]["type"] == "text"
    body = json.loads(res["content"][0]["text"])
    assert body["behavior"] == "deny" and "fail-closed" in body["message"]
