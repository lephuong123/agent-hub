import asyncio

from agent_hub_demo.simulator import FakeHub


def test_trang_thai_ban_dau():
    hub = FakeHub(live=False)
    st = {a.name: a.status for a in hub.agg.agents.values()}
    assert st == {"researcher": "working", "writer": "working", "coder": "waiting", "reviewer": "idle", "ops": "error"}
    assert {a.team for a in hub.agg.agents.values()} == {"Nghiên cứu", "Vận hành"}
    assert hub.agg.agents["a3"].pending_request["command"].startswith("rm -rf")
    assert hub.agg.pending_proposals >= 2
    # event đi qua adapter thật: có turn.start với session_id từ dòng system/init
    evs, _ = hub.bus.replay("a1")
    assert evs[0].type == "turn.start" and evs[0].session_id


def test_duyet_va_tu_choi():
    hub = FakeHub(live=False)
    assert hub.deny("a3")
    a3 = hub.agg.agents["a3"]
    assert a3.status == "idle" and a3.pending_request is None
    evs, _ = hub.bus.replay("a3")
    assert any(e.type == "request.cancel" and e.payload["decision"] == "denied" for e in evs)
    assert any(e.type == "tool.complete" and e.payload["is_error"] for e in evs)
    assert not hub.approve("a3")  # không còn gì để duyệt


def test_steer_stop_crud_learning():
    hub = FakeHub(live=False)
    hub.steer("a4", "nhớ kiểm tra nhóm rỗng")
    types = [e.type for e in hub.bus.replay("a4")[0]]
    assert "user.message" in types and types[-1] == "agent.status"
    hub.stop("a1")
    assert hub.agg.agents["a1"].status == "stopped"
    aid = hub.create_agent("thu-ky", "Tổng hợp công văn", "Vận hành", "haiku", ["tools-cchc"])
    assert hub.agg.agents[aid].status == "idle"
    hub.update_agent(aid, name="thu-ky-2", team="Nghiên cứu")
    assert hub.agg.agents[aid].team == "Nghiên cứu"
    assert hub.delete_agent(aid) and aid not in hub.agg.agents
    pid = next(p.proposal_id for p in hub.agg.proposals.values() if p.status == "pending")
    assert hub.approve_learning(pid) and hub.agg.proposals[pid].status == "applied"
    assert hub.rollback_learning(pid) and hub.agg.proposals[pid].status == "rolled_back"


async def test_chay_live_nhanh():
    hub = FakeHub(live=True, speed=0.01, learning_every=5)
    await hub.start()
    before = hub.bus.last_seq("a2")
    await asyncio.sleep(0.5)
    assert hub.bus.last_seq("a2") > before
    hub.approve("a3")
    await asyncio.sleep(0.5)
    assert hub.agg.agents["a3"].status != "waiting" or hub.agg.agents["a3"].pending_request
    await hub.shutdown()
