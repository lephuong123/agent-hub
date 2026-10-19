import asyncio

import pytest

from cfharness.bus import Aggregator, EventBus
from cfharness.claude_adapter import adapt_lines
from cfharness.events import make_event


def ev(agent, type_="tool.start", **p):
    return make_event(type_, agent, p)


async def test_global_seq_and_epoch():
    bus = EventBus()
    a = bus.publish(ev("a"))
    b = bus.publish(ev("b"))
    c = bus.publish(ev("a"))
    assert [a.seq, b.seq, c.seq] == [1, 2, 3]
    assert a.epoch == b.epoch == bus.epoch and bus.epoch.startswith("e-")
    assert EventBus().epoch != bus.epoch


async def test_replay_since_and_no_gap_subscribe():
    bus = EventBus()
    for i in range(5):
        bus.publish(ev("a", i=i))
    bus.publish(ev("b"))
    evs, truncated = bus.replay("a", 2)
    assert [e.seq for e in evs] == [3, 4, 5] and not truncated
    sub = bus.subscribe("a", since_seq=3)
    bus.publish(ev("a", i=99))
    got = sub.drain()
    assert [e.seq for e in got] == [4, 5, 7]  # replay + live, không hở không trùng


async def test_ring_truncation_and_backfill():
    bus = EventBus(ring_events=3)
    for i in range(10):
        bus.publish(ev("a", i=i))
    evs, truncated = bus.replay("a", 0)
    assert truncated and [e.seq for e in evs] == [8, 9, 10]
    _, ok = bus.replay("a", 7)
    assert not ok
    sub = bus.subscribe("a", since_seq=0, backfill=lambda s: [ev("a", from_db=True)])
    assert not sub.truncated and sub.drain()[0].payload == {"from_db": True}


async def test_slow_consumer_dropped_agent_not_blocked():
    bus = EventBus()
    slow = bus.subscribe("a", maxsize=10)
    fast = bus.subscribe("a", maxsize=100_000)
    for i in range(10_000):
        bus.publish(ev("a", i=i))
    assert slow.closed_reason == "slow_consumer"
    assert bus.subscriber_count == 1
    assert len(fast.drain()) == 10_000
    assert await asyncio.wait_for(slow.get(), 1) is not None  # còn dữ liệu đã nhận trước khi bị ngắt


async def test_filters_level_and_run():
    bus = EventBus()
    sub = bus.subscribe(None, run_id="r1", level="summary")
    bus.publish(make_event("message.delta", "a", {"text": "x"}, run_id="r1"))
    bus.publish(make_event("tool.start", "a", {}, run_id="r1"))
    bus.publish(make_event("tool.start", "a", {}, run_id="r2"))
    got = sub.drain()
    assert [e.type for e in got] == ["tool.start"] and got[0].run_id == "r1"


async def test_aggregator_usage(sample_lines):
    bus = EventBus()
    agg = Aggregator()
    bus.add_listener(agg.apply)
    bus.publish_many(adapt_lines("a", sample_lines, run_id="r1"))
    bus.publish_many(adapt_lines("a", sample_lines, run_id="r2"))
    snap = agg.snapshot()
    assert snap["runs_completed"] == 2
    assert snap["cost_usd"] == pytest.approx(2 * 0.00474188, abs=1e-6)
    assert agg.agents["a"].last_tool == "Bash"
    assert snap["rate_limit"]["status"] == "allowed"
