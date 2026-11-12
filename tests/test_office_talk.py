"""Văn phòng 3D — nói chuyện với agent trong chế độ đi bộ (docs/dev-notes/office-coordinator.md): `POST /office/do/{steer,run_continue,run_start}`.

Dùng ĐÚNG Action của 2D (`app.ACTIONS`) qua cổng `office_do` (post_guard: Origin/CSRF/khoá phiên `k`); chỉ người đăng nhập; LUÔN hỏi xác nhận
trước lần gửi (gửi tin = có thể tốn tiền) và nói rõ "sẽ bắt đầu run mới cho <agent>"; không gọi RPC ghi nào khi chưa `confirm=yes`.
"""

from __future__ import annotations

import pytest

from cfharness.runtime import FakeRuntime
from test_web import BASE, ISOLATED, _start, _stop, audit_rows, held, running_run, until  # noqa: F401 — dùng lại khung test web
from test_web_office import office_post


@pytest.fixture
async def env(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    yield e
    await _stop(e)


def _runs(env, agent: str) -> list:
    return [r for r in env.hub.store.conn.execute("SELECT id FROM runs WHERE agent_id=?", (agent,)).fetchall()]


async def test_talk_requires_login(env):
    env.hub.agents.create({"id": "ag0", "name": "ag0", "model": "haiku"})
    for name, data in (("run_start", {"agent": "ag0", "prompt": "chào"}), ("steer", {"id": "r", "text": "x"}),
                       ("run_continue", {"id": "r", "prompt": "x"})):
        r = await env.client.post(f"/office/do/{name}", data={**data, "confirm": "yes"})
        assert r.status_code == 401, name
    assert _runs(env, "ag0") == []


async def test_talk_guard_rejects_missing_csrf_key_or_cross_origin(env):
    await env.login()
    env.hub.agents.create({"id": "ag0", "name": "ag0", "model": "haiku"})
    full = {"agent": "ag0", "prompt": "chào", "confirm": "yes"}
    cases = [
        {"data": {"k": env.key, **full}, "headers": {"Origin": BASE}},
        {"data": {"csrf": env.csrf, **full}, "headers": {"Origin": BASE}},
        {"data": {"csrf": env.csrf, "k": env.key, **full}, "headers": {"Origin": "http://evil.example"}},
        {"data": {"csrf": env.csrf, "k": env.key, **full}, "headers": {"Origin": BASE, "Sec-Fetch-Site": "cross-site"}},
    ]
    for c in cases:
        r = await env.client.post("/office/do/run_start", data=c["data"], headers=c["headers"])
        assert r.status_code == 403 and r.json()["ok"] is False, c
    assert _runs(env, "ag0") == []


async def test_run_start_confirms_new_run_then_returns_run_id(env):
    await env.login()
    env.hub.agents.create({"id": "ag0", "name": "ag0", "model": "haiku"})
    j = (await office_post(env, "run_start", {"agent": "ag0", "prompt": "chào bạn"})).json()
    assert j["need"] == "confirm" and "Sẽ bắt đầu run mới cho ag0" in j["message"] and "tốn tiền" in j["message"]
    assert _runs(env, "ag0") == []  # chưa xác nhận → không có run
    assert (await office_post(env, "run_start", {"agent": "ag0", "confirm": "yes"})).status_code == 400  # thiếu nội dung
    j = (await office_post(env, "run_start", {"agent": "ag0", "prompt": "chào bạn", "confirm": "yes"})).json()
    assert j["ok"] is True and j["run_id"] and env.hub.store.get_run(j["run_id"])["agent_id"] == "ag0"
    assert audit_rows(env, "run.start")[-1]["channel"] == "web"
    final = await env.hub.runs.wait(j["run_id"], 20)
    assert final["status"] == "completed"
    # tiếp phiên vừa xong → run MỚI; cũng hỏi xác nhận trước
    j2 = (await office_post(env, "run_continue", {"id": j["run_id"], "prompt": "ngắn hơn"})).json()
    assert j2["need"] == "confirm" and "run mới" in j2["message"] and len(_runs(env, "ag0")) == 1
    j2 = (await office_post(env, "run_continue", {"id": j["run_id"], "prompt": "ngắn hơn", "confirm": "yes"})).json()
    assert j2["ok"] is True and j2["run_id"] and j2["run_id"] != j["run_id"] and len(_runs(env, "ag0")) == 2


async def test_steer_running_run_confirms_then_returns_same_run(held):
    env = held
    await env.login()
    run = await running_run(env)
    j = (await office_post(env, "steer", {"id": run["id"], "text": "chỉ xem src/"})).json()
    assert j["need"] == "confirm" and run["id"] in j["message"] and env.hub.store.get_run(run["id"])["steer_count"] == 0
    j = (await office_post(env, "steer", {"id": run["id"], "text": "chỉ xem src/", "confirm": "yes"})).json()
    assert j["ok"] is True and j["run_id"] == run["id"] and env.hub.store.get_run(run["id"])["steer_count"] == 1


async def test_talk_whitelist_still_excludes_other_run_actions(held):
    env = held
    await env.login()
    run = await running_run(env)
    for name in ("run_resume", "feedback"):  # `stop` có từ bảng quản lý agent (test_office_agent_board.py)
        r = await office_post(env, name, {"id": run["id"], "confirm": "yes"})
        assert r.status_code == 404, name
    assert env.hub.store.get_run(run["id"])["status"] == "running"
