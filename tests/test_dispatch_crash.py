"""FR-TK-05/06: giết tiến trình `claude` (giả, subprocess thật) giữa chừng → run `crashed` → retry tạo Run mới
`--resume <session cũ>` (`parent_run_id` trỏ run cũ), lịch sử 2 run còn đủ; task done."""

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from cfharness.config import DEFAULTS
from cfharness.hub import Hub
from cfharness.runtime import ClaudeCliRuntime
from conftest import fake_cli

FAKE = Path(__file__).parent / "fake_claude_team.py"


async def settle(cond, timeout=15.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        await asyncio.sleep(0.05)
        if cond():
            return
    raise AssertionError("hết giờ")


@pytest.fixture
def hub(hub_home, tmp_path, monkeypatch):
    p = fake_cli(tmp_path, FAKE)
    monkeypatch.setenv("FAKE_PID_FILE", str(tmp_path / "pid"))
    cfg = {**DEFAULTS["runtime"]["claude"], "binary": str(p)}
    h = Hub(hub_home, runtime=ClaudeCliRuntime(cfg))
    h.config.set("tasks.tick_s", 0.1)
    h.config.set("runs.stop_grace_s", 1)
    h.start()
    yield h, tmp_path
    h.runs.shutting_down = True
    for ar in list(h.runs.active.values()):
        proc = (ar.handle.internal or {}).get("proc") if ar.handle else None
        if proc and proc.returncode is None:
            proc.kill()
    h.store.close()


async def test_kill_worker_mid_run_retries_with_resume(hub):
    h, tmp = hub
    h.agents.create({"id": "w", "name": "W"})
    t = h.tasks.create({"assignee": "w", "title": "long job", "body": "HANG until killed"})
    await settle(lambda: (tmp / "pid").exists() and (tmp / "pid").read_text().strip())
    first = h.tasks.row(t["id"])["current_run_id"]
    os.kill(int((tmp / "pid").read_text()), getattr(signal, "SIGKILL", signal.SIGTERM))  # giết worker từ ngoài (Windows: SIGTERM = TerminateProcess)
    await settle(lambda: h.tasks.row(t["id"])["status"] == "done")
    runs = sorted((r for r in h.store.list_runs() if r["task_id"] == t["id"]), key=lambda r: r["queued_at"])
    assert [r["outcome"] for r in runs] == ["crashed", "completed"]
    assert runs[0]["id"] == first and runs[1]["parent_run_id"] == first
    argv = runs[1]["argv_redacted"]
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == runs[0]["session_id"]
    assert h.tasks.row(t["id"])["result_summary"] == "resumed and finished"
    assert runs[1]["cost_usd"] == pytest.approx(0.002)  # run crash không có result → cả phần luỹ kế tính cho run resume


async def test_lead_plan_via_json_schema_subprocess(hub, monkeypatch):
    h, tmp = hub
    monkeypatch.setenv("FAKE_PLAN", '{"summary":"s","subtasks":[{"key":"a","title":"A","body":"do A","assignee":"w1"},{"key":"b","title":"B","body":"do B","assignee":"w2","parents":["a"]}]}')
    for a in ("lead", "w1", "w2"):
        h.agents.create({"id": a, "name": a})
    h.teams.create({"id": "tm", "members": [{"agent_id": "lead", "role": "lead"}, {"agent_id": "w1", "role": "worker"}, {"agent_id": "w2", "role": "worker"}]})
    root = h.tasks.create({"team": "tm", "title": "big job", "body": "split it"})
    await settle(lambda: h.tasks.row(root["id"])["status"] == "done")
    lead_run = next(r for r in h.store.list_runs() if r["kind"] == "lead")
    assert "--json-schema" in lead_run["argv_redacted"] and lead_run["schema_valid"] == 1
    subs = h.tasks.subtasks(root["id"])
    assert [s["assignee_agent_id"] for s in subs] == ["w1", "w2"]
