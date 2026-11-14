"""Task gốc đã tách không còn kẹt `running` khi task con bị huỷ + `task.reassign` (giữ task trong task gốc).

Sự cố 2026-11-14: điều phối viên "chuyển việc sang agent rảnh" bằng cách huỷ task con rồi tạo task mới đứng riêng.
- t_b477adfb82: 4 task con done, task con cuối bị huỷ → task gốc `running` mãi (chỉ xét lại task gốc khi task con XONG).
- t_c9acb8faf7: cả 5 task con bị huỷ → không nhánh nào xử lý "không còn task con".
Daemon không start → dispatcher không claim gì; trạng thái chỉ đổi do lời gọi trong test."""

import pytest

from cfharness import auth
from cfharness.daemon import Daemon
from cfharness.errors import HubError
from m3_fake import ScriptedRuntime


async def rpc(d, method, params):
    resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in resp:
        raise HubError(resp["error"]["message"])
    return resp["result"]


def _setup(hub_home):
    d = Daemon(hub_home, runtime=ScriptedRuntime(lambda spec: {"text": "x", "cost": 0.0}))
    h = d.hub
    for aid in ("lead", "coder", "coder2", "reviewer", "outsider"):
        h.agents.create({"id": aid, "name": aid, "role": aid})
    h.teams.create({"id": "web"})
    h.teams.member_set("web", "lead", "lead")
    for aid in ("coder", "coder2", "reviewer"):
        h.teams.member_set("web", aid, "worker")
    return d, h


def _decomposed_root(h, n: int):
    root = h.tasks.create({"team": "web", "title": "việc lớn", "decompose": True}, created_by="manager:dieu-phoi")
    subs = [h.tasks.create({"team": "web", "assignee": "coder", "title": f"phần {i}", "origin_task_id": root["id"]}, created_by="agent:lead")
            for i in range(n)]
    h.tasks.set_status(root["id"], "running", reason="lead tách", decomposed=1)
    return root, subs


async def test_cancel_last_open_subtask_completes_root(hub_home):
    """t_b477adfb82: mọi task con khác đã done, huỷ task con cuối → task gốc done (bỏ qua task con đã huỷ)."""
    d, h = _setup(hub_home)
    root, subs = _decomposed_root(h, 3)
    for s in subs[:2]:
        h.tasks.set_status(s["id"], "done", reason="test", result_summary=f"xong {s['title']}")
    assert h.tasks.row(root["id"])["status"] == "running"
    await rpc(d, "mcp.cancel_task", {"task_id": subs[2]["id"], "reason": "agent bận"})
    r = h.tasks.row(root["id"])
    assert r["status"] == "done" and "xong phần 0" in (r["result_summary"] or "")
    h.store.close()


async def test_cancel_every_subtask_blocks_root_for_owner(hub_home):
    """t_c9acb8faf7: huỷ hết task con → task gốc `blocked` có lý do (hiện ở "Cần bạn"), không `running` im lặng."""
    d, h = _setup(hub_home)
    root, subs = _decomposed_root(h, 2)
    await rpc(d, "mcp.cancel_task", {"task_id": subs[0]["id"]})
    assert h.tasks.row(root["id"])["status"] == "running"  # còn task con mở
    await rpc(d, "mcp.cancel_task", {"task_id": subs[1]["id"]})
    r = h.tasks.row(root["id"])
    assert r["status"] == "blocked" and "đã bị huỷ" in (r["block_reason"] or "")
    h.store.close()


async def test_cancel_root_still_archives_everything(hub_home):
    """Huỷ CHÍNH task gốc: task con archive theo, task gốc giữ `archived` (không bị xét lại thành blocked)."""
    d, h = _setup(hub_home)
    root, subs = _decomposed_root(h, 2)
    await rpc(d, "mcp.cancel_task", {"task_id": root["id"]})
    assert h.tasks.row(root["id"])["status"] == "archived"
    assert all(h.tasks.row(s["id"])["status"] == "archived" for s in subs)
    h.store.close()


async def test_reassign_keeps_task_in_root(hub_home):
    d, h = _setup(hub_home)
    root, subs = _decomposed_root(h, 1)
    t = subs[0]
    out = await rpc(d, "task.reassign", {"id": t["id"], "assignee": "coder2", "reason": "coder bận"})
    assert out["assignee_agent_id"] == "coder2" and out["origin_task_id"] == root["id"] and out["status"] == t["status"]
    ev = h.store.query_events(types=["task.reassigned"])
    assert ev and ev[-1].payload["from"] == "coder" and ev[-1].payload["to"] == "coder2"
    au = h.audit.query(kind="task.reassign")
    assert au and au[0]["actor"] == "user"
    # ngoài team / task gốc / task đã chạy → từ chối
    with pytest.raises(HubError, match="không thuộc team"):
        await rpc(d, "task.reassign", {"id": t["id"], "assignee": "outsider"})
    with pytest.raises(HubError, match="task gốc"):
        await rpc(d, "task.reassign", {"id": root["id"], "assignee": "coder"})
    h.tasks.set_status(t["id"], "done", reason="test")
    with pytest.raises(HubError, match="chỉ đổi người làm được task chưa chạy"):
        await rpc(d, "task.reassign", {"id": t["id"], "assignee": "coder"})
    h.store.close()


def test_reassign_is_human_only_but_manager_allowed():
    assert "task.reassign" in auth.HUMAN_ONLY and "task.reassign" in auth.MANAGER_ALLOWED
    assert "task.reassign" not in auth.AGENT_ALLOWED_MUTATING


def test_manager_tool_and_cancel_hint():
    from cfharness.mcp_server.hub_self import MANAGER_TOOLS

    assert "task_reassign" in MANAGER_TOOLS
    assert "task_reassign" in MANAGER_TOOLS["task_cancel"]["description"]
