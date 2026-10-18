"""Steer giữa chừng (FR-RT-06, spike A-02): adapter `steer.ack`, FakeRuntime, ClaudeCliRuntime (claude giả), RPC/CLI."""

import asyncio
import gzip
import json
import sys
from pathlib import Path

import pytest

from agent_hub.claude_adapter import ClaudeStreamAdapter
from agent_hub.config import DEFAULTS
from agent_hub.errors import Conflict, NotFound
from agent_hub.hub import Hub
from agent_hub.runtime import ClaudeCliRuntime, FakeRuntime
from agent_hub.steer import steer

FIX = Path(__file__).parent / "fixtures"
FAKE_STEER = Path(__file__).parent / "fake_claude_steer.py"


def test_adapter_steer_ack_from_real_fixture():
    """Fixture thật của spike A-02 (midtool): tin #1 (prompt) bỏ qua, tin #2 (steer) → đúng một steer.ack."""
    ad = ClaudeStreamAdapter("a", "r")
    evs = [e for ln in (FIX / "claude-steer.jsonl").read_text().splitlines() if ln.strip() for e in ad.feed(ln)]
    acks = [e for e in evs if e.type == "steer.ack"]
    assert len(acks) == 1 and acks[0].payload["text"].startswith("STEER from operator")
    assert acks[0].level == "summary" and acks[0].source == "cli"
    # ack nằm SAU tool_result của lượt hiện tại và TRƯỚC result (cùng lượt — một result)
    types = [e.type for e in evs]
    assert types.index("tool.complete") < types.index("steer.ack") < types.index("run.completed")
    assert types.count("run.completed") == 1


async def _wait_for(pred, timeout=10.0, step=0.02):
    for _ in range(int(timeout / step)):
        if pred():
            return True
        await asyncio.sleep(step)
    return False


@pytest.fixture
def fake_hub(hub_home):
    h = Hub(hub_home, runtime=FakeRuntime(line_delay_s=0.05))
    h.start()
    h.agents.create({"id": "a", "name": "A", "model": "haiku"})
    yield h
    h.store.close()


async def test_fake_runtime_steer_roundtrip(fake_hub):
    h = fake_hub
    run = h.runs.submit("a", "do the thing")
    assert await _wait_for(lambda: (h.store.get_run(run["id"]) or {}).get("status") == "running" and h.runs.active.get(run["id"]) and h.runs.active[run["id"]].handle)
    res = await steer(h, run["id"], "focus on docs/ only", by="tester", channel="tui")
    assert res["sent"] and res["steer_count"] == 1
    # tham chiếu bằng id agent → run đang chạy của agent
    res2 = await steer(h, "a", "and be brief", channel="cli")
    assert res2["run_id"] == run["id"] and res2["steer_count"] == 2
    final = await h.runs.wait(run["id"], 20)
    assert final["outcome"] == "completed" and final["steer_count"] == 2
    evs = h.store.query_events(run_id=run["id"])
    um = [e for e in evs if e.type == "user.message"]
    acks = [e for e in evs if e.type == "steer.ack"]
    assert [e.payload["text"] for e in um] == ["focus on docs/ only", "and be brief"]
    assert um[0].source == "tui" and um[0].payload["by"] == "tester" and um[1].source == "cli"
    assert [e.payload["text"] for e in acks] == ["focus on docs/ only", "and be brief"]
    assert max(e.seq for e in um) < max(e.seq for e in acks)
    raw = gzip.open(final["raw_log_path"], "rt").read().splitlines()
    replays = [json.loads(x) for x in raw if '"isReplay"' in x]
    assert [r["message"]["content"] for r in replays] == ["do the thing", "focus on docs/ only", "and be brief"]


async def test_steer_rejected_after_result_and_unknown(fake_hub):
    h = fake_hub
    run = h.runs.submit("a", "quick")
    await h.runs.wait(run["id"], 20)
    with pytest.raises(Conflict, match="run continue"):
        await steer(h, run["id"], "too late")
    with pytest.raises(Conflict):
        await steer(h, "a", "no run")
    with pytest.raises(NotFound):
        await steer(h, "zzz", "x")


async def test_fake_runtime_send_after_result_raises(tmp_path):
    from agent_hub.runtime import RunSpec, RuntimeError_

    rt = FakeRuntime()
    spec = RunSpec(run_id="r1", agent_id="a", prompt="p", cwd=tmp_path / "w", session_id="s", raw_log_path=tmp_path / "r.jsonl", stderr_log_path=tmp_path / "e.log")
    h = await rt.start(spec)
    async for _ in rt.events(h):
        pass
    with pytest.raises(RuntimeError_):
        await rt.send(h, "x")


# ---------------------------------------------------------------- ClaudeCliRuntime + claude giả (subprocess thật)
@pytest.fixture
def steer_bin(tmp_path):
    p = tmp_path / "claude"
    p.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_STEER} \"$@\"\n")
    p.chmod(0o755)
    return p


@pytest.fixture
def cli_hub(hub_home, steer_bin):
    cfg = {**DEFAULTS["runtime"]["claude"], "binary": str(steer_bin), "steer_ack_grace_s": 5}
    h = Hub(hub_home, runtime=ClaudeCliRuntime(cfg))
    h.start()
    h.agents.create({"id": "c", "name": "C", "model": "haiku"})
    yield h
    h.store.close()


def _types(h, rid):
    return [e.type for e in h.store.query_events(run_id=rid)]


async def test_claude_cli_steer_midtool_same_turn(cli_hub):
    h = cli_hub
    run = h.runs.submit("c", "midtool please")
    assert await _wait_for(lambda: "tool.start" in _types(h, run["id"]))
    await steer(h, run["id"], "ALSO say PINEAPPLE")
    final = await h.runs.wait(run["id"], 20)
    assert final["outcome"] == "completed" and final["exit_code"] == 0, final
    assert final["summary"] == "steered: ALSO say PINEAPPLE"
    t = _types(h, run["id"])
    assert t.count("steer.ack") == 1 and t.index("user.message") < t.index("steer.ack")
    assert t.count("run.completed") == 1  # hub chỉ phát một event kết thúc chính thức


async def test_claude_cli_late_steer_keeps_stdin_open_for_second_turn(cli_hub):
    """Tin gửi trước khi CLI kịp phát `result` nhưng CLI chưa đọc → result #1 đến khi tin còn chờ → hub KHÔNG đóng
    stdin, CLI chạy lượt mới cho tin đó (spike A-02 'twoturns'), rồi mới đóng như M0."""
    h = cli_hub
    run = h.runs.submit("c", "late please")
    assert await _wait_for(lambda: "tool.start" in _types(h, run["id"]))
    await steer(h, run["id"], "one more thing")
    final = await h.runs.wait(run["id"], 20)
    assert final["outcome"] == "completed" and final["exit_code"] == 0, final
    assert final["summary"] == "late steer: one more thing"
    assert final["num_turns"] == 2
    assert "steer.ack" in _types(h, run["id"])


async def test_claude_cli_no_steer_still_closes_stdin_after_result(cli_hub):
    """Hành vi M0 giữ nguyên: không có steer → đóng stdin ngay sau result, tiến trình thoát."""
    h = cli_hub
    run = h.runs.submit("c", "hello")
    final = await h.runs.wait(run["id"], 15)
    assert final["outcome"] == "completed" and final["exit_code"] == 0
    with pytest.raises(Conflict):
        await steer(h, run["id"], "after")


def test_cli_run_steer_is_rewritten():
    from agent_hub.cli import build_parser
    from agent_hub.guard.cli import rewrite_argv

    p = build_parser()
    a = p.parse_args(rewrite_argv(["run", "steer", "r_abc", "focus please"]))
    assert a.cmd == "run-steer" and a.target == "r_abc" and a.text == "focus please"
    a = p.parse_args(rewrite_argv(["steer", "coder", "x", "--wait-ack"]))
    assert a.cmd == "steer" and a.wait_ack == 120.0
    a = p.parse_args(rewrite_argv(["run", "coder", "normal prompt"]))
    assert a.cmd == "run" and a.agent == "coder"
