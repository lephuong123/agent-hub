"""ClaudeCliRuntime với tiến trình con thật nhưng `claude` GIẢ (tests/fake_claude.py) — không gọi model."""

import asyncio
import sys
from pathlib import Path

import pytest

from cfharness.config import DEFAULTS
from cfharness.hub import Hub
from cfharness.runtime import ClaudeCliRuntime
from conftest import fake_cli

FAKE = Path(__file__).parent / "fake_claude.py"


@pytest.fixture
def fake_bin(tmp_path):
    p = fake_cli(tmp_path, FAKE)
    return p


@pytest.fixture
def hub(hub_home, fake_bin):
    cfg = {**DEFAULTS["runtime"]["claude"], "binary": str(fake_bin)}
    h = Hub(hub_home, runtime=ClaudeCliRuntime(cfg))
    h.start()
    yield h
    h.store.close()


async def test_real_subprocess_stream_json_run(hub):
    hub.agents.create({"id": "a", "name": "A", "model": "haiku"})
    run = hub.runs.submit("a", "xin chào")
    final = await hub.runs.wait(run["id"], 15)
    assert final["outcome"] == "completed", final["error"]
    assert final["cost_usd"] == 0.001 and final["summary"] == "ok"
    assert final["exit_code"] == 0  # stdin được đóng sau result → CLI thoát
    argv = final["argv_redacted"]
    assert "--setting-sources" in argv and "--strict-mcp-config" in argv and "--input-format" in argv
    assert not final["isolation_violations"]
    msgs = [e for e in hub.store.query_events(run_id=run["id"]) if e.type == "message.complete"]
    assert msgs[0].payload["text"] == "echo: xin chào"  # prompt đã đi qua stdin


async def test_stop_running_subprocess_is_cancelled(hub):
    hub.agents.create({"id": "b", "name": "B"})
    run = hub.runs.submit("b", "hang please")
    for _ in range(200):
        await asyncio.sleep(0.02)
        if hub.store.query_events(run_id=run["id"], types=["run.started"]):
            break
    await hub.runs.stop(run["id"])
    final = await hub.runs.wait(run["id"], 20)
    assert final["outcome"] == "cancelled"
    evs = [e.type for e in hub.store.query_events(run_id=run["id"])]
    assert "run.cancelled" in evs
