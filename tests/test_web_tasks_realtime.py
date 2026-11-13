"""Bảng Công việc realtime — partial /partials/tasks, /partials/task/<id>, SSE /sse/tasks (docs/dev-notes/tasks-realtime.md).

Daemon IN-PROCESS + FakeRuntime (hạ tầng của tests/test_web.py). Không gọi `claude`.
"""

from __future__ import annotations

import asyncio
import json

from starlette.requests import Request

from cfharness.web.app import sse_tasks, tasks_refresh

from test_web import _start, _stop, env, until  # noqa: F401


def _task(e, title: str, team: str = "docs", **kw) -> dict:
    """Task của team chưa có thành viên → nằm yên ở cột chờ (dispatcher không chạy gì), trạng thái do test đổi."""
    for tid in ("docs", "ops"):
        if not any(t["id"] == tid for t in e.hub.teams.list()):
            e.hub.teams.create({"id": tid, "name": tid.upper()})
    return e.hub.tasks.create({"title": title, "team": team, **kw})


# =========================================================================== bộ lọc event (hàm thuần)
def test_tasks_refresh_converter():
    ev = lambda t, tid=None, **p: {"type": t, "task_id": tid, "payload": p}  # noqa: E731
    for t in ("task.created", "task.status", "task.comment", "task.linked", "task.signal", "task.decomposed", "task.circuit_open"):
        assert tasks_refresh(ev(t, "T1")), t
    assert not tasks_refresh(ev("task.background", "T1"))  # tool Task của claude, không phải task hub
    # câu hỏi / run: chỉ khi gắn task (đổi "chờ bạn", chi phí trên thẻ)
    for t in ("question.open", "question.answered", "question.cancelled", "run.started", "run.completed", "run.failed", "run.cancelled",
              "run.interrupted", "run.waiting_human"):
        assert tasks_refresh(ev(t, "T1")), t
        assert not tasks_refresh(ev(t)), t
    for t in ("message.delta", "tool.call", "agent.status", "run.queued", "approval.open", "usage.update"):
        assert not tasks_refresh(ev(t, "T1")), t
    # trang một task: chỉ task của trang (và cha/con)
    ids = frozenset({"T1", "T2"})
    assert tasks_refresh(ev("task.status", "T2"), ids)
    assert not tasks_refresh(ev("task.status", "T9"), ids)
    assert tasks_refresh({"type": "task.status", "payload": {"task_id": "T1"}}, ids)  # task_id trong payload cũng được
    assert not tasks_refresh(ev("run.completed", "T9"), ids)


# =========================================================================== xác thực
async def test_tasks_realtime_routes_need_login(env):
    c = env.client
    for path in ("/partials/tasks", "/partials/task/x", "/sse/tasks", "/sse/tasks?task=x"):
        r = await c.get(path)
        assert r.status_code == 401, path  # 401, không chuyển hướng /login (polling/SSE)


# =========================================================================== partial
async def test_tasks_page_and_partial_render_same_board(env):
    await env.login()
    a = _task(env, "Viết tài liệu")
    b = _task(env, "Sửa lỗi <b>đậm</b>")
    env.hub.tasks.block(b["id"], "chờ người")
    page = (await env.client.get("/tasks")).text
    assert 'id="tasks-board"' in page and 'data-sse-tasks="/sse/tasks"' in page and "Tạo công việc" in page
    r = await env.client.get("/partials/tasks")
    assert r.status_code == 200
    part = r.text
    assert part.lstrip().startswith('<div class="kanban" id="tasks-board"') and "Tạo công việc" not in part and "<html" not in part
    assert f'data-id="{a["id"]}"' in part and f'data-id="{b["id"]}" data-status="blocked"' in part
    assert "&lt;b&gt;đậm" in part  # autoescape
    # đổi trạng thái → partial thấy ngay
    env.hub.tasks.unblock(b["id"])
    part2 = (await env.client.get("/partials/tasks")).text
    assert f'data-id="{b["id"]}" data-status="blocked"' not in part2
    # lọc team giữ nguyên trên partial
    c = _task(env, "Việc của team ops", team="ops")
    only = (await env.client.get("/partials/tasks?team=ops")).text
    assert f'data-id="{c["id"]}"' in only and f'data-id="{a["id"]}"' not in only


async def test_task_partial_has_main_and_oob_comments(env):
    await env.login()
    p = _task(env, "Cha")
    t = _task(env, "Con", parents=[p["id"]])
    env.hub.tasks.comment(t["id"], "ghi chú một")
    page = (await env.client.get(f"/tasks/{t['id']}")).text
    assert 'id="task-main"' in page and 'id="task-comments"' in page and "hx-swap-oob" not in page and "ghi chú một" in page
    assert f"/sse/tasks?task={t['id']}%2C{p['id']}" in page  # task của trang + cha
    part = (await env.client.get(f"/partials/task/{t['id']}")).text
    assert part.lstrip().startswith('<div id="task-main"') and '<div id="task-comments" hx-swap-oob="true">' in part and "ghi chú một" in part
    assert "Viết bình luận" not in part  # form bình luận nằm ngoài khung được thay (không mất chữ đang gõ)


# =========================================================================== SSE
async def test_sse_tasks_emits_refresh_on_status_change(env):
    await env.login()
    t = _task(env, "Theo dõi")
    other = _task(env, "Việc khác")

    async def collect(qs: bytes, out: list[dict]) -> None:
        scope = {"type": "http", "app": env.app, "path": "/sse/tasks", "headers": [], "query_string": qs, "method": "GET"}
        resp = await sse_tasks(Request(scope))
        assert resp.media_type == "text/event-stream"
        async for chunk in resp.body_iterator:
            chunk = chunk if isinstance(chunk, str) else chunk.decode()
            if chunk.startswith("data: "):
                out.append(json.loads(chunk[6:]))

    board: list[dict] = []
    one: list[dict] = []
    tasks = [asyncio.create_task(collect(b"", board)), asyncio.create_task(collect(f"task={t['id']}".encode(), one))]
    try:
        assert await until(lambda: any(m["t"] == "hello" for m in board) and any(m["t"] == "hello" for m in one))
        assert not any(m["t"] == "refresh" for m in board + one)  # không phát lại lịch sử
        env.hub.tasks.block(other["id"], "lý do")
        assert await until(lambda: any(m["t"] == "refresh" for m in board))
        await asyncio.sleep(0.2)
        assert not any(m["t"] == "refresh" for m in one)  # trang task khác không bị làm phiền
        env.hub.tasks.block(t["id"], "lý do")
        assert await until(lambda: any(m["t"] == "refresh" for m in one))
    finally:
        for x in tasks:
            x.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
