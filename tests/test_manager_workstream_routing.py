"""Định tuyến tin chủ động/câu hỏi/lịch của điều phối viên KHÔNG đoán chéo luồng (lỗi: tin của luồng A hiện ở luồng B).
Daemon in-process + ScriptedRuntime, không gọi claude."""

from __future__ import annotations

import time

import pytest

from cfharness.manager.ops import ManagerOps
from cfharness.web import app as _web_app  # noqa: F401 — nạp app trước (vòng import web.manager ↔ web.app)
from cfharness.web.manager import _timeline
from m7_fake import close, make_daemon, manager_run, rpc, tool, wait_status


@pytest.fixture
async def d(hub_home):
    dm = await make_daemon(hub_home)
    yield dm
    await close(dm)


async def _finish(d, run_id):
    d.hub.runs.runtime().release()
    await wait_status(d, run_id, ("completed",))


async def _boss(d):
    if d.hub.store.get_agent("boss") is None:
        d.hub.agents.create({"id": "boss", "name": "boss", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    await rpc(d, "manager.set", {"agent_id": "boss"})


def _bump(d, cid, text="mới nhất"):
    """Hội thoại `cid` thành cuộc người cập nhật gần nhất (đẩy `updated_at` qua `_add_msg`)."""
    time.sleep(0.01)
    d.hub.manager._add_msg(cid, "user", text, None)


def _notifies(d):
    return [e.payload for e in d.hub.store.query_events(types=["manager.reply"]) if e.payload.get("kind") == "notify"]


async def _general_tick(d):
    """Lượt nhịp CHUNG (sự kiện task không thuộc luồng nào, không hội thoại gốc) → (tick, task)."""
    d.hub.config.set("manager.event_debounce_s", 0)
    if d.hub.store.get_agent("w1") is None:
        d.hub.agents.create({"id": "w1", "name": "w1"})
    m = d.hub.manager
    m.pending.clear()
    m._pending_keys.clear()
    t = d.hub.tasks.create({"assignee": "w1", "title": "x", "body": "y"}, created_by="human")
    d.hub.tasks.set_status(t["id"], "blocked", reason="lỗi")
    tk = await m.maybe_tick()
    run = await wait_status(d, tk["run_id"])
    assert "tick_convs" not in run["run_opts"] and not run["run_opts"].get("manager_workstream")
    return tk, t


async def test_general_tick_notify_falls_back_to_lobby_not_newest_workstream(d):
    res = await manager_run(d, text="sảnh")
    await _finish(d, res["run_id"])
    wb = await rpc(d, "workstream.create", {"title": "B", "key": "EP-B"})
    conv_b = d.hub.manager._new_conv("web", "boss", "chat B", workstream_id=wb["id"])
    _bump(d, conv_b["id"])  # cuộc người mới nhất thuộc luồng B
    tk, t = await _general_tick(d)
    n = await tool(d, tk["run_id"], "notify_owner", {"text": "báo chung", "task_id": t["id"]})
    assert n["conversation_id"] == res["conversation_id"]  # Sảnh, KHÔNG phải luồng B
    rep = _notifies(d)[-1]
    assert rep["conversation_guessed"] is True and rep["conversation_id"] == res["conversation_id"]
    hist_b = await rpc(d, "manager.history", {"conversation_id": conv_b["id"]})
    assert not [x for x in hist_b if x["role"] == "notify"]
    # thẻ hành động notify mang hội thoại đích (web chỉ hiện ở đúng cuộc đó)
    act = [e.payload for e in d.hub.store.query_events(types=["manager.action"]) if e.payload.get("tool") == "notify_owner"][-1]
    assert act["conversation_id"] == res["conversation_id"]
    # câu hỏi của lượt nhịp: cũng Sảnh
    q = await tool(d, tk["run_id"], "ask_owner", {"question": "làm gì?", "options": [{"key": "1", "label": "A"}]})
    assert d.hub.questions.get(q["question_id"])["conversation_id"] == res["conversation_id"]


async def test_general_tick_without_lobby_conversation_uses_heartbeat_not_workstream(d):
    await _boss(d)
    wb = await rpc(d, "workstream.create", {"title": "B", "key": "EP-B"})
    conv_b = d.hub.manager._new_conv("web", "boss", "chat B", workstream_id=wb["id"])
    _bump(d, conv_b["id"])
    tk, t = await _general_tick(d)
    n = await tool(d, tk["run_id"], "notify_owner", {"text": "báo chung", "task_id": t["id"]})
    assert n["conversation_id"] == tk["conversation_id"] and n["delivered_to"] is None  # hội thoại nhịp chung của lượt
    rep = _notifies(d)[-1]
    assert rep["channel"] is None and rep["conversation_guessed"] is True  # kênh chat: như chưa có hội thoại (gửi mọi chat)
    hist_b = await rpc(d, "manager.history", {"conversation_id": conv_b["id"]})
    assert not [x for x in hist_b if x["role"] == "notify"]
    q = await tool(d, tk["run_id"], "ask_owner", {"question": "làm gì?", "options": [{"key": "1", "label": "A"}]})
    assert d.hub.questions.get(q["question_id"])["conversation_id"] is None  # câu hỏi không gắn vào hội thoại nhịp/luồng khác


async def test_fallback_conversation_stays_within_workstream(d):
    res = await manager_run(d, text="sảnh")
    await _finish(d, res["run_id"])
    m = d.hub.manager
    wa = await rpc(d, "workstream.create", {"title": "A", "key": "EP-A"})
    wb = await rpc(d, "workstream.create", {"title": "B", "key": "EP-B"})
    conv_a = m._new_conv("web", "boss", "chat A", workstream_id=wa["id"])
    conv_a_tg = m._new_conv("telegram:1", "boss", "chat A tg", workstream_id=wa["id"])
    conv_b = m._new_conv("web", "boss", "chat B", workstream_id=wb["id"])
    _bump(d, conv_a_tg["id"])
    _bump(d, conv_b["id"])  # B mới nhất toàn hub
    assert m.fallback_conversation(wa["id"])["id"] == conv_a_tg["id"]
    assert m.fallback_conversation(wa["id"], channel="web")["id"] == conv_a["id"]
    assert m.fallback_conversation(None)["id"] == res["conversation_id"]
    assert m.fallback_conversation("ws_khong_co")["id"] == res["conversation_id"]  # luồng đã mất → Sảnh
    # run gắn luồng A (hội thoại của chính run đã mất) → đích trong luồng A, không phải B mới nhất
    run = {"id": "r_fake", "agent_id": "boss", "run_opts": {"manager_conversation": "mc_khong_co", "manager_workstream": wa["id"]}}
    conv, _ = ManagerOps(m, run)._target_conv()
    assert conv["workstream_id"] == wa["id"]


async def test_question_answer_continue_never_guesses_other_workstream(d):
    res = await manager_run(d, text="sảnh")
    await _finish(d, res["run_id"])
    m = d.hub.manager
    wa = await rpc(d, "workstream.create", {"title": "A", "key": "EP-A"})
    wb = await rpc(d, "workstream.create", {"title": "B", "key": "EP-B"})
    conv_a = m._new_conv("web", "boss", "chat A", workstream_id=wa["id"])
    conv_b = m._new_conv("web", "boss", "chat B", workstream_id=wb["id"])
    _bump(d, conv_a["id"])
    _bump(d, conv_b["id"])
    tk, _t = await _general_tick(d)
    run = d.hub.store.get_run(tk["run_id"])
    seen = []

    async def fake_chat(channel, text, conversation_id=None, **kw):
        seen.append(conversation_id)
        return {"run_id": None}

    m.chat = fake_chat
    # câu hỏi lưu hội thoại nhịp (không dùng được để nối tiếp) → Sảnh, không phải B (mới nhất)
    q = d.hub.questions.open(run, "chọn?", [{"key": "1", "label": "A"}], source="manager", conversation_id=tk["conversation_id"])
    await d.hub.questions._continue_manager(d.hub.questions.get(q["id"]))
    assert seen[-1] == res["conversation_id"]
    # câu hỏi của task thuộc luồng A, không còn hội thoại → trong luồng A
    t = d.hub.tasks.create({"assignee": "w1", "title": "a", "body": "x"}, created_by="human")
    d.hub.store.conn.execute("UPDATE tasks SET workstream_id=? WHERE id=?", (wa["id"], t["id"]))
    q2 = d.hub.questions.open(run, "chọn A?", [{"key": "1", "label": "A"}], source="manager", task_id=t["id"])
    await d.hub.questions._continue_manager(d.hub.questions.get(q2["id"]))
    assert seen[-1] == conv_a["id"]


async def test_schedule_fallback_never_crosses_workstream(d):
    res = await manager_run(d, text="sảnh")
    await _finish(d, res["run_id"])
    m = d.hub.manager
    wa = await rpc(d, "workstream.create", {"title": "A", "key": "EP-A"})
    wb = await rpc(d, "workstream.create", {"title": "B", "key": "EP-B"})
    conv_a_old = m._new_conv("web", "boss", "chat A cũ", workstream_id=wa["id"])
    conv_a = m._new_conv("web", "boss", "chat A", workstream_id=wa["id"])
    conv_b = m._new_conv("web", "boss", "chat B", workstream_id=wb["id"])
    _bump(d, conv_a["id"])
    _bump(d, conv_b["id"])
    sch = d.hub.schedules
    assert sch._deliver_conv({"name": "x", "deliver_to": {}}, create=False)["id"] == res["conversation_id"]
    assert sch._deliver_conv({"name": "x", "deliver_to": {"channel": "web"}}, create=False)["id"] == res["conversation_id"]
    # hội thoại chỉ định (trong luồng A) đã lưu trữ → cuộc khác trong luồng A, không phải B
    arch = "UPDATE manager_conversations SET archived_at='2026-01-01T00:00:00Z' WHERE id=?"
    d.hub.store.conn.execute(arch, (conv_a_old["id"],))
    assert sch._deliver_conv({"name": "x", "deliver_to": {"conversation_id": conv_a_old["id"]}}, create=False)["id"] == conv_a["id"]
    # hội thoại chỉ định ở Sảnh đã lưu trữ, Sảnh hết cuộc web → cuộc mới ở Sảnh (không mượn B)
    d.hub.store.conn.execute(arch, (res["conversation_id"],))
    new = sch._deliver_conv({"name": "x", "deliver_to": {"conversation_id": res["conversation_id"]}}, create=True)
    assert new["id"] not in (conv_b["id"], conv_a["id"]) and new["workstream_id"] is None


# ------------------------------------------------------------------ web: dòng thời gian không kéo hành động của hội thoại khác
def test_timeline_tick_run_notice_does_not_pull_actions_of_other_conversation():
    """Lượt nhịp `rt` báo vào Y (notify) và làm việc cho X → dòng thời gian của Y chỉ có tin báo + thẻ notify gắn Y."""
    msgs_y = [{"kind": "user", "text": "u", "ts": "2026-11-20T01:00:00Z", "run_id": "r1"},
              {"kind": "manager", "text": "m", "ts": "2026-11-20T01:00:30Z", "run_id": "r1"},
              {"kind": "manager", "text": "báo Y", "ts": "2026-11-20T02:00:05Z", "run_id": "rt", "notify": True}]
    acts = [{"kind": "action", "summary": "retry task của X", "ts": "2026-11-20T02:00:01Z", "run_id": "rt", "ok": True, "target": {}},
            {"kind": "action", "summary": "báo người X", "ts": "2026-11-20T02:00:02Z", "run_id": "rt", "ok": True, "target": {}, "conversation_id": "mc_x"},
            {"kind": "action", "summary": "báo người Y", "ts": "2026-11-20T02:00:04Z", "run_id": "rt", "ok": True, "target": {}, "conversation_id": "mc_y"},
            {"kind": "action", "summary": "việc của Y", "ts": "2026-11-20T01:00:10Z", "run_id": "r1", "ok": True, "target": {}}]
    tl = _timeline(msgs_y, acts, [], "mc_y")
    sums = [a["summary"] for it in tl if it["kind"] == "actions" for a in it["items"]]
    assert sums == ["việc của Y", "báo người Y"]
    # thẻ gắn hội thoại KHÁC không bao giờ hiện, kể cả khi run là của Y
    acts2 = [{"kind": "action", "summary": "lạc", "ts": "2026-11-20T01:00:11Z", "run_id": "r1", "ok": True, "target": {}, "conversation_id": "mc_x"}]
    assert not [it for it in _timeline(msgs_y, acts2, [], "mc_y") if it["kind"] == "actions"]
