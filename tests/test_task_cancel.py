"""`mcp.cancel_task` (manager `task_cancel`, MCP `hub_cancel(task_id)`): huỷ = dừng run + ARCHIVE task và task con chưa xong.

Hồi quy 04/11: điều phối viên huỷ task gốc có task con đang `ready` chờ khoá workspace (chưa có run) → cancel cũ chỉ dừng run,
trả `no_active_run`; task con vẫn được claim khi repo nhả khoá, chạy xong và báo kết quả vào thread Slack lần thứ hai.
Daemon không start → dispatcher không claim gì, trạng thái task chỉ đổi do cancel."""

from cfharness.daemon import Daemon
from cfharness.errors import HubError
from cfharness.manager import prompts
from m3_fake import ScriptedRuntime


async def rpc(d, method, params):
    resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in resp:
        raise HubError(resp["error"]["message"])
    return resp["result"]


def _setup(hub_home):
    d = Daemon(hub_home, runtime=ScriptedRuntime(lambda spec: {"text": "x", "cost": 0.0}))
    h = d.hub
    for aid in ("lead", "coder", "reviewer"):
        h.agents.create({"id": aid, "name": aid, "role": aid})
    h.teams.create({"id": "web"})
    h.teams.member_set("web", "lead", "lead")
    return d, h


async def test_cancel_archives_waiting_subtasks(hub_home):
    d, h = _setup(hub_home)
    root = h.tasks.create({"team": "web", "title": "trả lời anh Hiếu", "decompose": True}, created_by="manager:dieu-phoi")
    sub = h.tasks.create({"assignee": "coder", "title": "đọc code", "origin_task_id": root["id"]}, created_by="agent:lead")
    rev = h.tasks.create({"assignee": "reviewer", "title": "kiểm lại", "origin_task_id": root["id"], "parents": [sub["id"]]},
                         created_by="agent:lead")
    h.tasks.update(sub["id"], {"wait_reason": "workspace_locked"})
    assert h.tasks.row(sub["id"])["status"] == "ready" and h.tasks.row(rev["id"])["status"] == "todo"

    out = await rpc(d, "mcp.cancel_task", {"task_id": root["id"], "reason": "giao lại task khác"})
    assert out["status"] == "cancelled" and out["stopped_runs"] == []
    assert set(out["archived_tasks"]) == {root["id"], sub["id"], rev["id"]}
    for tid in (root["id"], sub["id"], rev["id"]):
        assert h.tasks.row(tid)["status"] == "archived", tid

    again = await rpc(d, "mcp.cancel_task", {"task_id": root["id"]})
    assert again["status"] == "already_archived" and again["archived_tasks"] == []
    h.store.close()


async def test_cancel_done_task_reports_already_done(hub_home):
    d, h = _setup(hub_home)
    t = h.tasks.create({"assignee": "coder", "title": "xong rồi"})
    h.tasks.set_status(t["id"], "done", reason="test")
    out = await rpc(d, "mcp.cancel_task", {"task_id": t["id"]})
    assert out["status"] == "already_done" and out["archived_tasks"] == []
    assert h.tasks.row(t["id"])["status"] == "done"
    h.store.close()


def test_tick_prompt_says_notify_lands_in_shared_thread():
    ev = [{"kind": "task_done", "summary": "t_1 done", "conversation_id": "mc_thread"},
          {"kind": "task_done", "summary": "t_2 done", "conversation_id": "mc_dm"}]
    p = prompts.tick_prompt(ev, dropped=0, reason="events", open_work=0,
                            conv_channels={"mc_thread": "slack:C0AL709NJ3S", "mc_dm": "slack:D0CD7FDBUN4"})
    thread_line = next(x for x in p.splitlines() if "t_1 done" in x)
    dm_line = next(x for x in p.splitlines() if "t_2 done" in x)
    assert "shared Slack channel thread" in thread_line and "never say it has not been posted" in thread_line
    assert "shared" not in dm_line
    assert prompts.shared_audience("telegram:-100123") and not prompts.shared_audience("cli")
