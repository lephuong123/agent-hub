"""Luồng việc (workstream) của điều phối viên — docs/dev-notes/workstreams.md. Daemon in-process + ScriptedRuntime, không gọi claude."""

from __future__ import annotations

import pytest

from cfharness import auth
from cfharness.errors import HubError
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


# ------------------------------------------------------------------ CRUD + RPC
async def test_workstream_crud_key_lookup_and_conflicts(d):
    await _boss(d)
    w = await rpc(d, "workstream.create", {"title": "Monitor", "key": "LH-55166", "sprint": "S216", "state": "Goal: monitor page"})
    assert w["id"].startswith("ws_") and w["label"] == "LH-55166 · Monitor" and w["state"] == "Goal: monitor page"
    assert (await rpc(d, "workstream.get", {"id": "lh-55166"}))["id"] == w["id"]  # mã không phân biệt hoa thường
    with pytest.raises(HubError, match="đã có luồng"):
        await rpc(d, "workstream.create", {"title": "khác", "key": "lh-55166"})
    with pytest.raises(HubError, match="không hợp lệ"):
        await rpc(d, "workstream.create", {"title": "x", "key": "có dấu cách"})
    up = await rpc(d, "workstream.update", {"id": w["id"], "status": "closed"})
    assert up["status"] == "closed" and up["closed_at"]
    assert [x["id"] for x in await rpc(d, "workstream.list")] == []
    assert [x["id"] for x in await rpc(d, "workstream.list", {"status": "closed"})] == [w["id"]]
    w2 = await rpc(d, "workstream.create", {"title": "Monitor v2", "key": "LH-55166"})  # mã của luồng đã đóng dùng lại được
    with pytest.raises(HubError, match="đã có luồng"):
        await rpc(d, "workstream.update", {"id": w["id"], "status": "active"})
    assert (await rpc(d, "workstream.get", {"id": "LH-55166"}))["id"] == w2["id"]  # mã → luồng đang mở
    st = await rpc(d, "workstream.update", {"id": w2["id"], "state": "Decisions: base = release"})
    assert st["state"] == "Decisions: base = release" and st["state_by"] == "user"
    evs = [e.payload["action"] for e in d.hub.store.query_events(types=["workstream.updated"])]
    assert evs[0] == "created" and "closed" in evs and "state" in evs


async def test_create_name_only_random_and_key_from_title(d):
    await _boss(d)
    w = await rpc(d, "workstream.create", {"title": "LH-55166: Monitor page"})
    assert (w["key"], w["title"]) == ("LH-55166", "Monitor page")
    r = await rpc(d, "workstream.create", {})
    assert r["title"].startswith("Luồng ") and r["key"] is None
    r2 = await rpc(d, "workstream.create", {"title": ""})
    assert r2["title"] != r["title"]
    up = await rpc(d, "workstream.update", {"id": w["id"], "title": "LH-55166 · Monitor v2"})
    assert (up["key"], up["title"]) == ("LH-55166", "Monitor v2")
    up = await rpc(d, "workstream.update", {"id": r["id"], "title": "Release 5.13"})
    assert (up["key"], up["title"]) == (None, "Release 5.13")


def test_workstream_rpcs_are_human_only():
    for m in ("workstream.create", "workstream.update", "manager.conversation_update"):
        assert m in auth.HUMAN_ONLY
    assert {"workstream.create", "workstream.update", "manager.conversation_update"} <= auth.all_mutating()


# ------------------------------------------------------------------ hội thoại trong luồng + ngữ cảnh session mới
async def test_chat_in_workstream_loads_state_and_lobby_lists_workstreams(d):
    await _boss(d)
    w = await rpc(d, "workstream.create", {"title": "Dreem shot", "key": "LH-55815", "state": "Branch feat/shot; PR #712 waiting QA"})
    r = await rpc(d, "manager.chat", {"channel": "web", "text": "sao rồi?", "workstream": "LH-55815"})
    assert r["workstream_id"] == w["id"]
    run = await wait_status(d, r["run_id"])
    p = run["prompt"]
    assert "WORKSTREAM" in p and "PR #712 waiting QA" in p and "workstream LH-55815 · Dreem shot" in p
    assert run["run_opts"]["manager_workstream"] == w["id"]
    await _finish(d, r["run_id"])
    # lượt sau resume cùng session: không nạp lại khối STATE
    r2 = await rpc(d, "manager.chat", {"channel": "web", "text": "tiếp", "conversation_id": r["conversation_id"]})
    run2 = await wait_status(d, r2["run_id"])
    assert "WORKSTREAM" not in run2["prompt"] and "workstream LH-55815" in run2["prompt"]
    await _finish(d, r2["run_id"])
    # sảnh: danh sách luồng đang mở
    lob = await rpc(d, "manager.chat", {"channel": "web", "text": "việc mới"})
    run3 = await wait_status(d, lob["run_id"])
    assert "LOBBY" in run3["prompt"] and "LH-55815: Dreem shot" in run3["prompt"]
    assert lob["workstream_id"] is None
    convs = await rpc(d, "manager.conversations", {"workstream": "LH-55815"})
    assert [c["id"] for c in convs] == [r["conversation_id"]]
    assert [c["id"] for c in await rpc(d, "manager.conversations", {"lobby": True})] == [lob["conversation_id"]]


async def test_rotated_session_carries_workstream_state(d):
    d.hub.config.set("manager.rotate_after_turns", 1)
    await _boss(d)
    await rpc(d, "workstream.create", {"title": "Release 5.13", "key": "S215-REL", "state": "Release branch release/5.13 cut"})
    r = await rpc(d, "manager.chat", {"channel": "cli", "text": "a", "workstream": "S215-REL"})
    await wait_status(d, r["run_id"])
    await _finish(d, r["run_id"])
    r2 = await rpc(d, "manager.chat", {"channel": "cli", "text": "b", "conversation_id": r["conversation_id"]})
    run2 = await wait_status(d, r2["run_id"])
    assert run2["run_opts"].get("manager_rotated") == "turns"
    assert "release/5.13 cut" in run2["prompt"] and "fresh session" in run2["prompt"]


# ------------------------------------------------------------------ task gắn luồng
async def test_task_create_files_under_workstream(d):
    res = await manager_run(d, text="lobby")
    d.hub.agents.create({"id": "w1", "name": "w1"})
    w = await rpc(d, "workstream.create", {"title": "Vulns", "key": "LH-54698"})
    t1 = await tool(d, res["run_id"], "task_create", {"title": "fix", "body": "x", "assignee": "w1", "workstream": "LH-54698"})
    assert t1["workstream"] == "LH-54698 · Vulns"
    assert d.hub.tasks.get(t1["task_id"])["workstream_id"] == w["id"]
    t0 = await tool(d, res["run_id"], "task_create", {"title": "khác", "body": "x", "assignee": "w1", "if_busy": "queue"})
    assert t0["workstream"] is None  # sảnh, không truyền → ngoài luồng
    with pytest.raises(HubError, match="không có luồng"):
        await tool(d, res["run_id"], "task_create", {"title": "y", "body": "x", "assignee": "w1", "workstream": "LH-0"})
    # task con (origin_task_id) kế thừa luồng
    sub = d.hub.tasks.create({"assignee": "w1", "title": "con", "body": "x", "origin_task_id": t1["task_id"]}, created_by="human")
    assert sub["workstream_id"] == w["id"]
    lst = await tool(d, res["run_id"], "task_list", {"workstream": "LH-54698"})
    assert {t["id"] for t in lst} == {t1["task_id"], sub["id"]}
    ov = await tool(d, res["run_id"], "hub_overview", {"workstream": "LH-54698"})
    assert {t["id"] for t in ov["open_tasks"]} == {t1["task_id"], sub["id"]}
    assert ov["workstreams"][0]["key"] == "LH-54698" and ov["current_workstream"] is None
    # hội thoại gốc của task lưu trên cột task (không còn phụ thuộc bản đồ hub_state 200 mục)
    assert d.hub.store.conn.execute("SELECT manager_conversation_id FROM tasks WHERE id=?", (t1["task_id"],)).fetchone()[0] == res["conversation_id"]


async def test_task_created_in_workstream_conversation_defaults_to_it(d):
    await _boss(d)
    d.hub.agents.create({"id": "w1", "name": "w1"})
    w = await rpc(d, "workstream.create", {"title": "Monitor", "key": "LH-55166"})
    r = await rpc(d, "manager.chat", {"channel": "web", "text": "giao việc", "workstream": w["id"]})
    await wait_status(d, r["run_id"])
    t = await tool(d, r["run_id"], "task_create", {"title": "build", "body": "x", "assignee": "w1"})
    assert d.hub.tasks.get(t["task_id"])["workstream_id"] == w["id"]
    t2 = await tool(d, r["run_id"], "task_create", {"title": "ngoài", "body": "x", "assignee": "w1", "workstream": "none", "if_busy": "queue"})
    assert d.hub.tasks.get(t2["task_id"])["workstream_id"] is None
    g = await tool(d, r["run_id"], "workstream_get", {})
    assert g["id"] == w["id"] and [x["id"] for x in g["open_tasks"]] == [t["task_id"]]
    s = await tool(d, r["run_id"], "workstream_state_set", {"text": "Goal: monitor\nPR: #1"})
    assert s["state_chars"] == len("Goal: monitor\nPR: #1")
    assert (await rpc(d, "workstream.get", {"id": w["id"]}))["state_by"] == "manager:boss"
    acts = [e.payload for e in d.hub.store.query_events(types=["manager.action"]) if e.payload["tool"] == "workstream_state_set"]
    assert acts and acts[-1]["target"] == f"workstream:{w['id']}"


async def test_manager_creates_workstream_and_attaches_lobby_conversation(d):
    res = await manager_run(d, text="mở luồng cho LH-55900")
    out = await tool(d, res["run_id"], "workstream_create", {"title": "Bulk export", "key": "LH-55900", "state": "Goal: export"})
    att = await tool(d, res["run_id"], "conversation_set_workstream", {"workstream": "LH-55900"})
    assert att["conversation_id"] == res["conversation_id"] and att["workstream_id"] == out["id"]
    other = await rpc(d, "workstream.create", {"title": "Khác", "key": "LH-1"})
    with pytest.raises(HubError, match="chỉ người chuyển"):
        await tool(d, res["run_id"], "conversation_set_workstream", {"workstream": other["id"]})
    # người chuyển được
    c = await rpc(d, "manager.conversation_update", {"conversation_id": res["conversation_id"], "workstream": other["id"]})
    assert c["workstream_id"] == other["id"]
    c = await rpc(d, "manager.conversation_update", {"conversation_id": res["conversation_id"], "workstream": ""})
    assert c["workstream_id"] is None


# ------------------------------------------------------------------ nhịp theo luồng + định tuyến thông báo
async def test_tick_runs_one_workstream_at_a_time_and_notifies_its_conversation(d):
    d.hub.config.set("manager.event_debounce_s", 0)
    res = await manager_run(d, text="sảnh")
    d.hub.agents.create({"id": "w1", "name": "w1"})
    d.hub.agents.create({"id": "w2", "name": "w2"})
    wa = await rpc(d, "workstream.create", {"title": "A", "key": "EP-A", "state": "STATE OF A"})
    wb = await rpc(d, "workstream.create", {"title": "B", "key": "EP-B"})
    ta = await tool(d, res["run_id"], "task_create", {"title": "a", "body": "x", "assignee": "w1", "workstream": "EP-A"})
    tb = await tool(d, res["run_id"], "task_create", {"title": "b", "body": "x", "assignee": "w2", "workstream": "EP-B"})
    await _finish(d, res["run_id"])
    m = d.hub.manager
    m.pending.clear()
    m._pending_keys.clear()
    d.hub.tasks.set_status(ta["task_id"], "blocked", reason="cần A")
    d.hub.tasks.set_status(tb["task_id"], "blocked", reason="cần B")
    assert [e["workstream_id"] for e in m.pending] == [wa["id"], wb["id"]]
    tk = await m.maybe_tick()
    assert tk["workstream_id"] == wa["id"] and tk["events"] == 1 and tk["pending_events"] == 1
    run = await wait_status(d, tk["run_id"])
    assert "cần A" in run["prompt"] and "cần B" not in run["prompt"] and "STATE OF A" in run["prompt"]
    assert run["run_opts"]["manager_workstream"] == wa["id"]
    hb = d.hub.store.conn.execute("SELECT channel, workstream_id FROM manager_conversations WHERE id=?", (tk["conversation_id"],)).fetchone()
    assert tuple(hb) == ("heartbeat", wa["id"])
    # task giao từ SẢNH nhưng thuộc luồng A → thông báo về hội thoại của luồng A (chưa có → hội thoại web của luồng), không về sảnh
    n = await tool(d, tk["run_id"], "notify_owner", {"text": "A vướng", "task_id": ta["task_id"]})
    assert n["conversation_id"] != res["conversation_id"]
    conv_a = d.hub.store.conn.execute("SELECT channel, workstream_id, title FROM manager_conversations WHERE id=?", (n["conversation_id"],)).fetchone()
    assert tuple(conv_a) == ("web", wa["id"], "EP-A · A")
    n2 = await tool(d, tk["run_id"], "notify_owner", {"text": "báo luồng A"})  # không task_id → luồng của nhịp
    assert n2["conversation_id"] == n["conversation_id"]
    reps = [e.payload for e in d.hub.store.query_events(types=["manager.reply"]) if e.payload.get("kind") == "notify"]
    assert [r["conversation_guessed"] for r in reps[-2:]] == [False, False]
    assert (await rpc(d, "manager.conversations", {"workstream": "EP-A"}))[0]["unread"] == 2
    await _finish(d, tk["run_id"])
    # lượt sau: luồng B, hội thoại nhịp riêng
    tk2 = await m.maybe_tick()
    assert tk2["workstream_id"] == wb["id"] and tk2["conversation_id"] != tk["conversation_id"] and tk2["pending_events"] == 0
    run2 = await wait_status(d, tk2["run_id"])
    assert "cần B" in run2["prompt"] and "cần A" not in run2["prompt"]


async def test_conversation_update_rename_archive_and_read(d):
    res = await manager_run(d, text="xin chào")
    await _finish(d, res["run_id"])
    cid = res["conversation_id"]
    convs = await rpc(d, "manager.conversations", {"channel": "web"})
    assert convs[0]["unread"] == 1  # câu trả lời của điều phối viên chưa xem
    c = await rpc(d, "manager.conversation_update", {"conversation_id": cid, "title": "  Đổi   tên ", "read": True})
    assert c["title"] == "Đổi tên" and c["unread"] == 0
    c = await rpc(d, "manager.conversation_update", {"conversation_id": cid, "archived": True})
    assert c["archived_at"]
    assert await rpc(d, "manager.conversations", {"channel": "web"}) == []
    assert [x["id"] for x in await rpc(d, "manager.conversations", {"archived": True})] == [cid]
    await rpc(d, "manager.conversation_update", {"conversation_id": cid, "archived": False})
    assert [x["id"] for x in await rpc(d, "manager.conversations", {"channel": "web"})] == [cid]
