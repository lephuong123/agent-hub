"""Xoay session của điều phối viên khi session đắt/lớn: mỗi `--resume` nạp lại cả lịch sử → tick NO_ACTION $0.25–0.65, lịch chết
`budget_exceeded` trong 1 lượt (sự cố thật). Luật: `ManagerService.rotate_reason`. ScriptedRuntime — không gọi claude."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from m7_fake import close, make_daemon, rpc, wait_status


@pytest.fixture
async def d(hub_home):
    dm = await make_daemon(hub_home)
    dm.script.hold_manager = False
    dm.hub.agents.create({"id": "boss", "name": "boss", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    await rpc(dm, "manager.set", {"agent_id": "boss"})
    yield dm
    await close(dm)


async def _first_turn(d, channel="web"):
    r1 = await rpc(d, "manager.chat", {"channel": channel, "text": "nhớ số 7"})
    await wait_status(d, r1["run_id"], ("completed",))
    await asyncio.sleep(0.05)
    return r1, d.hub.store.get_run(r1["run_id"])["session_id"]


def _make_large(d, sid, *, ctx=120_000, cost=None, idle_s=None):
    """Giả lập session lớn như hub thật: usage của run gần nhất, chi phí luỹ kế, thời điểm kết thúc."""
    usage = {"input_tokens": 10, "cache_read_input_tokens": ctx - 10, "cache_creation_input_tokens": 0, "output_tokens": 50}
    d.hub.store.conn.execute("UPDATE runs SET usage=?, num_turns=1 WHERE session_id=?", (json.dumps(usage), sid))
    if cost is not None:
        d.hub.store.conn.execute("UPDATE sessions SET cumulative_cost_usd=? WHERE id=?", (cost, sid))
    if idle_s is not None:
        ended = (datetime.now(timezone.utc) - timedelta(seconds=idle_s)).isoformat().replace("+00:00", "Z")
        d.hub.store.conn.execute("UPDATE runs SET ended_at=? WHERE session_id=?", (ended, sid))


async def test_small_recent_session_still_resumes(d):
    r1, sid = await _first_turn(d)
    r2 = await rpc(d, "manager.chat", {"channel": "web", "text": "số mấy?", "conversation_id": r1["conversation_id"]})
    run2 = d.hub.store.get_run(r2["run_id"])
    assert run2["resumed_from_session"] == sid and "manager_rotated" not in run2["run_opts"]


async def test_rotate_when_session_cost_exceeds_cap(d):
    r1, sid = await _first_turn(d)
    _make_large(d, sid, ctx=1000, cost=15.0)  # session $15 như hội thoại thật
    r2 = await rpc(d, "manager.chat", {"channel": "web", "text": "số mấy?", "conversation_id": r1["conversation_id"]})
    run2 = d.hub.store.get_run(r2["run_id"])
    assert run2["resumed_from_session"] is None and run2["run_opts"]["manager_rotated"] == "cost"
    assert "nhớ số 7" in run2["prompt"] and "fresh session" in run2["prompt"]  # mang theo tin gần nhất
    assert run2["session_id"] != sid
    await wait_status(d, r2["run_id"], ("completed",))
    # chi phí run mới tính từ 0 của session mới (không trừ luỹ kế $15 của session cũ → không âm/không sai)
    assert d.hub.store.get_run(r2["run_id"])["cost_usd"] == pytest.approx(0.001)


async def test_chat_rotates_large_session_only_when_idle(d):
    r1, sid = await _first_turn(d)
    _make_large(d, sid, idle_s=60)  # lớn nhưng cache còn nóng → resume
    assert d.hub.manager.rotate_reason(d.hub.manager._conv_row(r1["conversation_id"]), "chat") is None
    _make_large(d, sid, idle_s=7200)
    r2 = await rpc(d, "manager.chat", {"channel": "web", "text": "tiếp", "conversation_id": r1["conversation_id"]})
    run2 = d.hub.store.get_run(r2["run_id"])
    assert run2["resumed_from_session"] is None and run2["run_opts"]["manager_rotated"] == "idle_large"


async def test_heartbeat_tick_does_not_reload_large_session(d):
    t1 = await d.hub.manager.maybe_tick(force=True)
    await wait_status(d, t1["run_id"], ("completed",))
    await asyncio.sleep(0.05)
    sid = d.hub.store.get_run(t1["run_id"])["session_id"]
    t2 = await d.hub.manager.maybe_tick(force=True)  # nhỏ → resume session heartbeat
    assert d.hub.store.get_run(t2["run_id"])["resumed_from_session"] == sid
    await wait_status(d, t2["run_id"], ("completed",))
    await asyncio.sleep(0.05)
    _make_large(d, sid, idle_s=30)  # lớn, dù vừa chạy
    t3 = await d.hub.manager.maybe_tick(force=True)
    run3 = d.hub.store.get_run(t3["run_id"])
    assert run3["resumed_from_session"] is None and run3["run_opts"]["manager_rotated"] == "background_large"
    # tick chạy trong hội thoại heartbeat riêng, không đụng session hội thoại của người
    assert run3["run_opts"]["manager_conversation"] == t1["conversation_id"]


async def test_schedule_fire_into_large_conversation_starts_fresh_session(d):
    r1, sid = await _first_turn(d)
    _make_large(d, sid, idle_s=10)
    conv = d.hub.manager._conv_row(r1["conversation_id"])
    run = d.hub.manager._submit(conv, "[cfharness · key x] scheduled job", kind="schedule", budget=2.0)
    assert run["resumed_from_session"] is None and run["run_opts"]["manager_rotated"] == "background_large"
    assert "nhớ số 7" in run["prompt"]


async def test_rotation_rules_can_be_disabled(d):
    r1, sid = await _first_turn(d)
    for k in ("rotate_session_cost_usd", "rotate_context_tokens", "rotate_idle_s"):
        d.hub.config.set(f"manager.{k}", 0)
    _make_large(d, sid, cost=50.0, idle_s=99999)
    conv = d.hub.manager._conv_row(r1["conversation_id"])
    assert d.hub.manager.rotate_reason(conv, "schedule") is None
