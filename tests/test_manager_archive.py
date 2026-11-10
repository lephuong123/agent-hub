"""#7 Quản lý hội thoại điều phối viên: `manager.archive|unarchive` (xoá mềm `archived_at`), lọc `manager.conversations{archived}`,
khôi phục khi người nhắn tiếp, quyền (chỉ người — agent/điều phối viên bị chặn ở daemon), audit. Không gọi claude thật."""

from __future__ import annotations

import json

import pytest

from cfharness import auth
from cfharness.errors import BLOCKED, CONFLICT, HubError
from cfharness.guard.classifier import GuardContext, classify
from cfharness.guard.policy import PolicyStore
from m7_fake import close, make_daemon, manager_run, rpc, wait_status


@pytest.fixture
async def env(hub_home):
    d = await make_daemon(hub_home)
    yield d
    await close(d)


async def _idle_conv(d) -> str:
    """Hội thoại web có một lượt đã XONG (run manager không giữ)."""
    d.script.hold_manager = False
    if d.hub.store.get_agent("boss") is None:
        d.hub.agents.create({"id": "boss", "name": "boss", "role": "hub boss", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
        await rpc(d, "manager.set", {"agent_id": "boss"})
    res = await rpc(d, "manager.chat", {"channel": "web", "text": "tóm tắt hôm nay"})
    await wait_status(d, res["run_id"], ("completed",))
    return res["conversation_id"]


async def test_archive_hides_keeps_history_and_unarchive_restores(env):
    d = env
    cid = await _idle_conv(d)
    n_msgs = len(await rpc(d, "manager.history", {"conversation_id": cid}))
    out = await rpc(d, "manager.archive", {"conversation_id": cid})
    assert out["id"] == cid and out["archived_at"]
    assert cid not in [c["id"] for c in await rpc(d, "manager.conversations", {"channel": "web"})]
    assert cid not in [c["id"] for c in (await rpc(d, "manager.get"))["conversations"]]
    assert [c["id"] for c in await rpc(d, "manager.conversations", {"archived": True})] == [cid]
    # tin + run + audit giữ nguyên
    assert len(await rpc(d, "manager.history", {"conversation_id": cid})) == n_msgs
    assert d.hub.store.conn.execute("SELECT COUNT(*) FROM runs WHERE kind='manager'").fetchone()[0] >= 1
    au = d.hub.audit.query(kind="manager.archive")
    assert au and au[0]["actor"] == "user"
    # lưu trữ lần nữa = không đổi (idempotent)
    assert (await rpc(d, "manager.archive", {"conversation_id": cid}))["archived_at"] == out["archived_at"]
    # "dùng lại gần nhất" không mở lại hội thoại đã lưu trữ → cuộc mới
    res = await rpc(d, "manager.chat", {"channel": "web", "text": "việc mới", "reuse_latest": True})
    assert res["conversation_id"] != cid
    await wait_status(d, res["run_id"], ("completed",))
    back = await rpc(d, "manager.unarchive", {"conversation_id": cid})
    assert back["archived_at"] is None and cid in [c["id"] for c in await rpc(d, "manager.conversations", {"channel": "web"})]
    assert d.hub.audit.query(kind="manager.unarchive")
    with pytest.raises(HubError):
        await rpc(d, "manager.archive", {"conversation_id": "mc_khongco"})


async def test_archive_refuses_running_conversation_and_chat_restores(env):
    d = env
    res = await manager_run(d)  # run manager giữ ở running
    with pytest.raises(HubError) as ei:
        await rpc(d, "manager.archive", {"conversation_id": res["conversation_id"]})
    assert ei.value.code == CONFLICT and "đang có run" in ei.value.message
    d.hub.runs.runtime().release()  # type: ignore[attr-defined]
    await wait_status(d, res["run_id"], ("completed",))
    d.script.hold_manager = False
    cid = res["conversation_id"]
    await rpc(d, "manager.archive", {"conversation_id": cid})
    # người nhắn THẲNG vào hội thoại đã lưu trữ → tự khôi phục, không mất tin
    r2 = await rpc(d, "manager.chat", {"channel": "web", "text": "tiếp nhé", "conversation_id": cid})
    assert r2["conversation_id"] == cid
    assert cid in [c["id"] for c in await rpc(d, "manager.conversations", {"channel": "web"})]


async def test_archive_is_human_only_agent_and_manager_blocked(env):
    d = env
    cid = await _idle_conv(d)
    assert {"manager.archive", "manager.unarchive"} <= auth.HUMAN_ONLY
    assert "manager.archive" not in auth.MANAGER_ALLOWED and "manager.unarchive" not in auth.MANAGER_ALLOWED
    assert {"manager.archive", "manager.unarchive"} <= auth.all_mutating()
    agent = auth.Caller(via="socket", run_id="r_w", agent_id="w1", evidence=["env_run_id"])
    for m in ("manager.archive", "manager.unarchive"):
        with pytest.raises(HubError) as ei:
            await rpc(d, m, {"conversation_id": cid}, as_caller=agent)
        assert ei.value.code == BLOCKED
    # điều phối viên (lớp 2: gọi thẳng như thể lách được tool hub-self) → manager_forbidden + audit actor manager:boss
    d.script.hold_manager = True
    res = await manager_run(d)
    for m in ("manager.archive", "manager.unarchive"):
        with pytest.raises(HubError) as ei:
            await rpc(d, m, {"conversation_id": cid}, as_caller=auth.manager_caller("boss", res["run_id"]))
        assert ei.value.code == BLOCKED and (ei.value.data or {}).get("reason") == "manager_forbidden"
    rows = [r for r in d.hub.audit.query(limit=200) if r["actor"] == "manager:boss" and r["kind"] == "auth.denied"]
    assert any("manager.archive" in json.dumps(r.get("data"), ensure_ascii=False) for r in rows)
    assert cid in [c["id"] for c in await rpc(d, "manager.conversations", {"channel": "web"})]  # không đổi gì


def test_classifier_archive_cli_forbidden_for_agents(tmp_path):
    ctx = GuardContext(cwd=tmp_path, policy=PolicyStore(tmp_path / "h").load())
    for cmd in ("cfharness manager archive mc_x", "cfharness manager unarchive mc_x"):
        assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd
    assert not classify("Bash", {"command": "cfharness manager conversations --archived"}, ctx).forbidden
