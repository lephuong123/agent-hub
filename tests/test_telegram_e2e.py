"""Telegram e2e: daemon THẬT (tiến trình con) + `claude` giả có guard (hook PreToolUse thật) + server Bot API GIẢ cục bộ.

Ca FR-SG-04 qua Telegram: thao tác phá huỷ bị giữ → tin duyệt có nút tới chat trong allowlist → bấm "Cho phép" →
lệnh chạy ĐÚNG MỘT LẦN; bấm lại (replay) bị từ chối; audit kênh `telegram:<chat_id>`. Rồi `/run` từ chat → kết quả.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest
import yaml

from agent_hub.cli import main
from agent_hub.daemon import spawn_background
from agent_hub.rpc import RpcClient

from telegram_fake import TOKEN, FakeTelegram

FAKE = Path(__file__).parent / "fake_claude_guard.py"
BOSS = 111


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
    tg = FakeTelegram()
    fake_bin = tmp_path / "claude"
    fake_bin.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    fake_bin.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    exec_log = tmp_path / "exec.log"
    cfg = {
        "runtime": {"kind": "claude", "claude": {"binary": str(fake_bin), "env_extra": {"MCP_TOOL_TIMEOUT": "900000", "FAKE_STATE_DIR": str(state), "FAKE_EXEC_LOG": str(exec_log)}}},
        "approval": {"timeout_s": 30, "pause_delay_s": 0.2, "reminder_check_s": 0.5},
        "runs": {"stop_grace_s": 1},
        "guard": {"git_probe": False},
        "notify": {"telegram": {"api_base": tg.url, "poll_timeout_s": 1, "idle_check_s": 0.1, "allowed_chat_ids": [str(BOSS)]}},
    }
    hub_home.config.write_text(yaml.safe_dump(cfg))
    sec = hub_home.home / "secrets"
    sec.mkdir(parents=True, exist_ok=True)
    fd = os.open(sec / "TELEGRAM_BOT_TOKEN", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, TOKEN.encode())
    os.close(fd)
    work = tmp_path / "work"
    (work / "dummy").mkdir(parents=True)
    (work / "dummy" / "keep.txt").write_text("x")
    time.sleep(1.1)  # dummy có TRƯỚC run → không phải "file run tự tạo"
    spawn_background(hub_home)
    rpc = RpcClient(hub_home.socket, timeout=60)
    rpc.call("agent.create", {"config": {"id": "coder", "name": "Coder", "model": "haiku"}})
    yield {"rpc": rpc, "work": work, "log": exec_log, "tg": tg, "paths": hub_home}
    main(["daemon", "stop"])
    tg.close()


def executed(env) -> list[str]:
    return env["log"].read_text().splitlines() if env["log"].exists() else []


def test_approve_via_telegram_button_runs_exactly_once(env):
    rpc, tg = env["rpc"], env["tg"]
    wait_for(lambda: tg.calls_of("getUpdates"))
    r = rpc.call("run.start", {"agent": "coder", "prompt": "BASH: ls\nBASH: rm -rf dummy", "cwd": str(env["work"])})
    ap = wait_for(lambda: rpc.call("approval.list", {"status": "open", "run_id": r["id"]}))[0]
    msg = wait_for(lambda: next((m for m in tg.sent(BOSS) if ap["id"] in m["text"] and FakeTelegram.buttons(m)), None))
    assert any("rm -rf dummy" in m["text"] for m in tg.sent(BOSS)) and (env["work"] / "dummy").exists()
    approve = FakeTelegram.buttons(msg)[0]["callback_data"]
    u1 = tg.press(BOSS, approve)  # bước 1 → tin xác nhận
    wait_for(lambda: tg.processed(u1))
    step2 = wait_for(lambda: tg.with_buttons(BOSS, ["Xác nhận CHO PHÉP", "Huỷ"]))  # T-B: bước 2 = tin hệ thống được sửa
    assert (env["work"] / "dummy").exists()
    uid = tg.press(BOSS, FakeTelegram.buttons(step2)[0]["callback_data"])
    final = rpc.call("run.wait", {"id": r["id"], "timeout": 30})
    assert final["outcome"] == "completed"
    assert executed(env).count("rm -rf dummy") == 1 and not (env["work"] / "dummy").exists()
    # bấm lại sau khi đã chạy → nút đã tiêu, không có lần chạy thứ hai
    wait_for(lambda: tg.processed(uid))
    uid2 = tg.press(BOSS, approve)
    wait_for(lambda: tg.processed(uid2))
    time.sleep(0.3)
    assert executed(env).count("rm -rf dummy") == 1
    got = rpc.call("approval.get", {"id": ap["id"]})
    assert got["status"] == "allowed" and got["resolved_channel"] == f"telegram:{BOSS}"
    au = rpc.call("audit.list", {"kind": "approval.resolved"})
    assert len(au) == 1 and au[0]["channel"] == f"telegram:{BOSS}"
    assert any("đã dùng" in b.get("text", "") for b in tg.calls_of("answerCallbackQuery"))


def test_run_via_telegram_and_stranger_ignored(env):
    rpc, tg = env["rpc"], env["tg"]
    wait_for(lambda: tg.calls_of("getUpdates"))
    tg.user_message(999, "/run coder BASH: touch hacked.txt")
    tg.user_message(BOSS, "/run coder BASH: ls")
    res = wait_for(lambda: next((m for m in tg.sent(BOSS) if m["text"].startswith("<b>[agent-hub] Run ")), None))
    assert "hoàn thành" in res["text"] and "Chi phí: $" in res["text"]
    runs = rpc.call("run.list", {"agent": "coder"})
    assert len(runs) == 1 and "touch hacked.txt" not in executed(env)
    assert tg.sent(999) == []
    au = rpc.call("audit.list", {"kind": "telegram.ignored"})
    assert au and au[0]["channel"] == "telegram:999"
    # token bot không lọt vào log daemon (thư viện HTTP log URL có token ở mức INFO — đã hạ mức + che theo giá trị)
    for p in env["paths"].logs_dir.rglob("*"):
        if p.is_file():
            assert TOKEN.encode() not in p.read_bytes(), p
