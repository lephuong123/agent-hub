import asyncio

from agent_hub_demo.bus import Aggregator, AgentState, EventBus
from agent_hub_demo.events import make_event


def _pub(bus, aid, n, type_="message.delta"):
    for i in range(n):
        bus.publish(make_event(type_, aid, {"text": f"t{i}"}))


async def test_seq_va_epoch():
    bus = EventBus(epoch="e1")
    _pub(bus, "a1", 3)
    _pub(bus, "a2", 2)
    evs, trunc = bus.replay("a1")
    assert [e.seq for e in evs] == [1, 2, 3] and not trunc
    assert all(e.epoch == "e1" for e in evs)
    assert bus.last_seq("a2") == 2


async def test_replay_khi_attach_muon_khong_ho_khong_trung():
    bus = EventBus()
    _pub(bus, "a1", 10)
    sub = bus.subscribe("a1", since_seq=4)
    _pub(bus, "a1", 3)  # live sau khi attach
    got = [e.seq for e in sub.drain()]
    assert got == list(range(5, 14))


async def test_ring_cat_va_bao_truncated():
    bus = EventBus(ring_events=5)
    _pub(bus, "a1", 12)
    evs, trunc = bus.replay("a1", since_seq=0)
    assert [e.seq for e in evs] == [8, 9, 10, 11, 12] and trunc
    evs, trunc = bus.replay("a1", since_seq=7)
    assert len(evs) == 5 and not trunc


async def test_ring_gioi_han_byte():
    bus = EventBus(ring_events=10_000, ring_bytes=3_000)
    for i in range(100):
        bus.publish(make_event("message.delta", "a1", {"text": "x" * 100}))
    evs, trunc = bus.replay("a1")
    assert 0 < len(evs) < 100 and trunc


async def test_firehose_khong_co_delta():
    bus = EventBus()
    fire = bus.subscribe(None, include_deltas=False)
    _pub(bus, "a1", 5)
    bus.publish(make_event("tool.start", "a1", {"name": "Bash"}))
    bus.publish(make_event("tool.start", "a2", {"name": "Read"}))
    got = fire.drain()
    assert [e.type for e in got] == ["tool.start", "tool.start"]


async def test_viewer_cham_bi_cat_agent_khong_cho():
    bus = EventBus()
    slow = bus.subscribe("a1", maxsize=3)
    fast = bus.subscribe("a1")
    _pub(bus, "a1", 10)  # không bao giờ block
    assert slow.dropped and bus.subscriber_count == 1
    assert len(fast.drain()) == 10


async def test_aggregator():
    bus = EventBus()
    agg = Aggregator()
    bus.add_listener(agg.apply)
    agg.register(AgentState(id="a1", name="x", team="T", base_tokens=100, base_cost=0.1))
    bus.publish(make_event("turn.start", "a1", {"model": "m", "task": "việc A"}))
    bus.publish(make_event("tool.start", "a1", {"name": "Bash", "summary": "ls"}))
    bus.publish(make_event("usage.tick", "a1", {"total_tokens": 50, "cost_usd": 0.05, "context_percent": 12}))
    st = agg.agents["a1"]
    assert st.task == "việc A" and st.last_tool == "Bash" and st.tool_running
    assert st.tokens == 150 and abs(st.cost - 0.15) < 1e-9
    bus.publish(make_event("request.open", "a1", {"request_id": "r1", "tool": "Bash", "command": "rm -rf x"}))
    assert st.pending_request["command"] == "rm -rf x"
    bus.publish(make_event("request.cancel", "a1", {"request_id": "r1", "decision": "approved"}))
    assert st.pending_request is None
    bus.publish(make_event("turn.complete", "a1", {"total_tokens": 80, "cost_usd": 0.08}))
    assert st.tokens == 180 and st.run_tokens == 0
    bus.publish(make_event("learning.proposal", "a1", {"proposal_id": "L1", "title": "t", "diff": "+x"}))
    assert agg.pending_proposals == 1
    bus.publish(make_event("learning.applied", "a1", {"proposal_id": "L1", "action": "applied", "by": "u"}))
    assert agg.proposals["L1"].status == "applied"
