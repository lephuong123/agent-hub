"""FR-RT-09 / FR-SG-04 end-to-end: daemon thật (tiến trình con) + `claude` giả có guard (tests/fake_claude_guard.py)
chạy hook PreToolUse THẬT của hub (python -I -m cfharness.guard.hook) qua socket. Không gọi model.

Ca: lệnh thường không hỏi; thao tác phá huỷ bị giữ → duyệt trong lúc giữ → chạy đúng 1 lần; quá thời gian giữ →
waiting_human (nhả slot, KHÔNG tự từ chối) → duyệt → resume `--resume` với vé một lần; vé không dùng lại; deny;
luật deny tĩnh thắng hook allow → hub khởi động lại với argv bỏ đúng mẫu đó; duyệt qua MCP `hub_approve` (luồng A).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
import yaml

from cfharness.cli import main
from cfharness.daemon import spawn_background
from cfharness.rpc import RpcClient

FAKE = Path(__file__).parent / "fake_claude_guard.py"


def wait_for(fn, timeout=30.0, step=0.1):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(step)
    raise AssertionError("hết giờ chờ điều kiện")


@pytest.fixture
def env(hub_home, tmp_path):
    fake_bin = tmp_path / "claude"
    fake_bin.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    fake_bin.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    exec_log = tmp_path / "exec.log"
    cfg = {
        "runtime": {"kind": "claude", "claude": {"binary": str(fake_bin), "env_extra": {"MCP_TOOL_TIMEOUT": "900000", "FAKE_STATE_DIR": str(state), "FAKE_EXEC_LOG": str(exec_log)}}},
        "approval": {"timeout_s": 20, "pause_delay_s": 0.2, "reminder_check_s": 0.5},
        "runs": {"stop_grace_s": 1},
        "guard": {"git_probe": False},
    }
    hub_home.config.write_text(yaml.safe_dump(cfg))
    work = tmp_path / "work"
    work.mkdir()
    (work / "dummy").mkdir()
    (work / "dummy" / "keep.txt").write_text("x")
    (work / "dummy-copy").mkdir()  # đổi tên giữ nguyên thời điểm tạo → vẫn "có từ trước run"
    time.sleep(1.1)  # dummy có TRƯỚC run → không phải "file run tự tạo"
    spawn_background(hub_home)
    rpc = RpcClient(hub_home.socket, timeout=60)
    rpc.call("agent.create", {"config": {"id": "coder", "name": "Coder", "model": "haiku"}})
    rpc.call("agent.create", {"config": {"id": "other", "name": "Other", "model": "haiku"}})
    yield {"rpc": rpc, "work": work, "log": exec_log, "paths": hub_home}
    main(["daemon", "stop"])


def executed(env) -> list[str]:
    return env["log"].read_text().splitlines() if env["log"].exists() else []


def pending(rpc, run_id=None):
    return rpc.call("approval.list", {"status": "open", **({"run_id": run_id} if run_id else {})})


def test_safe_commands_run_without_approval(env):
    rpc = env["rpc"]
    r = rpc.call("run.start", {"agent": "coder", "prompt": "BASH: ls\nBASH: touch made.txt\nBASH: rm made.txt", "cwd": str(env["work"])})
    final = rpc.call("run.wait", {"id": r["id"], "timeout": 30})
    assert final["outcome"] == "completed", final
    assert executed(env) == ["ls", "touch made.txt", "rm made.txt"]
    assert rpc.call("approval.list", {"status": "all"}) == []
    argv = final["argv_redacted"]
    assert "--permission-prompt-tool" in argv and "mcp__hub-guard__approve" in argv
    dec = rpc.call("guard.decisions", {"run_id": r["id"]})
    assert {d["verdict"] for d in dec} <= {"safe", "write"} and all(d["action"] == "allow" for d in dec)


def test_destructive_held_then_approved_runs_once(env):
    rpc = env["rpc"]
    r = rpc.call("run.start", {"agent": "coder", "prompt": "BASH: ls\nBASH: rm -rf dummy", "cwd": str(env["work"])})
    ap = wait_for(lambda: pending(rpc, r["id"]))[0]
    assert ap["status"] == "open" and ap["matched_rule"] == "delete_not_run_created" and "rm -rf dummy" in ap["preview"]
    assert (env["work"] / "dummy").exists()  # đang giữ: chưa chạy
    assert rpc.call("agent.get", {"id": "coder"})["status"] == "waiting"
    rpc.call("approval.approve", {"id": ap["id"], "channel": "cli"})
    final = rpc.call("run.wait", {"id": r["id"], "timeout": 30})
    assert final["outcome"] == "completed"
    assert executed(env).count("rm -rf dummy") == 1 and not (env["work"] / "dummy").exists()
    got = rpc.call("approval.get", {"id": ap["id"]})
    assert got["status"] == "allowed" and got["ticket_used_at"] and got["wait_s"] is not None
    au = rpc.call("audit.list", {"kind": "approval.resolved"})
    assert au and au[0]["data"]["action_fingerprint"] == ap["action_fingerprint"] and au[0]["channel"] == "cli"


def test_timeout_waiting_human_releases_slot_then_resume_once(env):
    rpc = env["rpc"]
    rpc.call("config.set", {"key": "approval.timeout_s", "value": 1})
    rpc.call("config.set", {"key": "hub.max_concurrent_runs", "value": 1})
    r = rpc.call("run.start", {"agent": "coder", "prompt": "BASH: rm -rf dummy\nBASH: echo should-not-run", "cwd": str(env["work"])})
    first = rpc.call("run.wait", {"id": r["id"], "timeout": 30})
    assert first["status"] == "waiting_human" and first["outcome"] is None  # KHÔNG tự từ chối, không lỗi
    ap = pending(rpc, r["id"])
    assert len(ap) == 1 and ap[0]["status"] == "waiting_human"
    assert (env["work"] / "dummy").exists() and "should-not-run" not in " ".join(executed(env))
    # slot đã nhả: run khác chạy được dù max_concurrent_runs=1
    r2 = rpc.call("run.start", {"agent": "other", "prompt": "BASH: echo other"})
    assert rpc.call("run.wait", {"id": r2["id"], "timeout": 30})["outcome"] == "completed"
    # khoá thư mục vẫn giữ: run khác cùng cwd phải chờ
    r3 = rpc.call("run.start", {"agent": "other", "prompt": "BASH: echo same-dir", "cwd": str(env["work"])})
    time.sleep(1.0)
    assert rpc.call("run.get", {"id": r3["id"]})["status"] == "queued"
    # người duyệt → resume với vé một lần
    rpc.call("approval.approve", {"id": ap[0]["id"]})
    old = wait_for(lambda: (x := rpc.call("run.get", {"id": r["id"]}))["status"] == "resumed" and x)
    new_id = old["continued_in_run_id"]
    final = rpc.call("run.wait", {"id": new_id, "timeout": 30})
    assert final["outcome"] == "completed", final
    assert "--resume" in final["argv_redacted"] and final["session_id"] == r["session_id"]
    assert executed(env).count("rm -rf dummy") == 1 and not (env["work"] / "dummy").exists()
    assert rpc.call("run.wait", {"id": r3["id"], "timeout": 30})["outcome"] == "completed"  # khoá đã nhả
    # vé không dùng lại: cùng thao tác lần nữa trong cùng session → mở approval MỚI
    (env["work"] / "dummy-copy").rename(env["work"] / "dummy")
    rpc.call("config.set", {"key": "approval.timeout_s", "value": 20})
    c = rpc.call("run.continue", {"id": r["id"], "prompt": "BASH: rm -rf dummy"})
    ap2 = wait_for(lambda: pending(rpc, c["id"]))[0]
    assert ap2["id"] != ap[0]["id"]
    rpc.call("approval.deny", {"request_id": ap2["id"], "note": "giữ lại thư mục này"})
    assert rpc.call("run.wait", {"id": c["id"], "timeout": 30})["outcome"] == "completed"
    assert (env["work"] / "dummy").exists() and executed(env).count("rm -rf dummy") == 1
    ev = rpc.call("events.query", {"run_id": r["id"]})
    types = [e["type"] for e in ev]
    assert "approval.open" in types and "approval.waiting_human" in types and "run.waiting_human" in types and "run.resumed" in types


def test_static_deny_beats_hook_allow_restart_with_rule_lifted(env):
    rpc = env["rpc"]
    r = rpc.call("run.start", {"agent": "coder", "prompt": "BASH: git push --force origin main", "cwd": str(env["work"])})
    ap = wait_for(lambda: pending(rpc, r["id"]))[0]
    first = rpc.call("run.get", {"id": r["id"]})
    assert "Bash(git push --force:*)" in first["argv_redacted"]
    rpc.call("approval.approve", {"id": ap["id"]})
    old = wait_for(lambda: (x := rpc.call("run.get", {"id": r["id"]}))["status"] == "resumed" and x)
    final = rpc.call("run.wait", {"id": old["continued_in_run_id"], "timeout": 30})
    assert "Bash(git push --force:*)" not in final["argv_redacted"]  # bỏ đúng mẫu cho lần resume này
    assert "Bash(git push -f:*)" in final["argv_redacted"]
    assert executed(env).count("git push --force origin main") == 1


def test_forbidden_self_approval_blocked(env):
    rpc = env["rpc"]
    r = rpc.call("run.start", {"agent": "coder", "prompt": "BASH: cfharness approve ap_123", "cwd": str(env["work"])})
    assert rpc.call("run.wait", {"id": r["id"], "timeout": 30})["outcome"] == "completed"
    assert executed(env) == [] and rpc.call("approval.list", {"status": "all"}) == []
    types = [e["type"] for e in rpc.call("events.query", {"run_id": r["id"]})]
    assert "guard.blocked" in types


async def test_mcp_hub_approve_resolves_approval(env):
    """Hợp đồng A↔B mục 4: hub_open_requests/hub_approve của MCP server gọi approval.list|approve của B."""
    from mcp import Client

    from cfharness.mcp_server.server import build_server

    rpc = env["rpc"]
    paths = env["paths"]
    r = rpc.call("run.start", {"agent": "coder", "prompt": "BASH: rm -rf dummy", "cwd": str(env["work"])})
    import asyncio

    for _ in range(300):
        if pending(rpc, r["id"]):
            break
        await asyncio.sleep(0.1)
    srv = build_server(lambda m, p: RpcClient(paths.socket, timeout=60).call(m, p))
    async with Client(srv) as c:
        reqs = (await c.call_tool("hub_open_requests", {})).structured_content["requests"]
        assert len(reqs) == 1 and reqs[0]["run_id"] == r["id"]
        # hardening H-14 (FR-MS-05): open_requests trong hub_events có mô tả tool + input
        assert reqs[0]["tool_name"] == "Bash" and "rm -rf dummy" in str(reqs[0].get("tool_input"))
        evs = (await c.call_tool("hub_events", {"run_id": r["id"], "wait_s": 0})).structured_content
        opened = [o for o in evs["open_requests"] if o["id"] == reqs[0]["id"]]
        assert opened and opened[0]["tool_name"] == "Bash" and "rm -rf dummy" in str(opened[0].get("tool_input"))
        res = await c.call_tool("hub_approve", {"request_id": reqs[0]["id"], "note": "ok từ Claude Code"})
        assert not res.is_error, res.content
    for _ in range(300):
        run = rpc.call("run.get", {"id": r["id"]})
        if run["status"] not in ("running", "queued"):
            break
        await asyncio.sleep(0.1)
    assert run["outcome"] == "completed"
    got = rpc.call("approval.get", {"id": reqs[0]["id"]})
    assert got["status"] == "allowed" and got["resolved_channel"].startswith("mcp:") and got["note"] == "ok từ Claude Code"
    assert executed(env).count("rm -rf dummy") == 1
