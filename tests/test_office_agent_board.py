"""Văn phòng 3D — "Bảng quản lý agent" (docs/dev-notes/office-agent-board.md).

- `/office/state`: trường mới theo agent (paused, run_status, started_at, run_cost_usd, cost_today_usd, questions) — chỉ số/cờ/id/giờ.
- `POST /office/do/{agent_pause,agent_resume,stop}`: ĐÚNG Action 2D (`app.ACTIONS`) qua `post_guard` (Origin/CSRF/khoá phiên `k`);
  Action có `confirm` nên LUÔN trả `need: "confirm"` trước — không gọi RPC ghi nào khi chưa có `confirm=yes`; audit kênh `web`.
- Node (`tests/office_roster_check.mjs`): nhóm/thứ tự/nút của mỗi hàng; World thật không còn bảng agent trong cảnh. Không có Node → skip.
- Tĩnh: roster.js thuần (không DOM/mạng); dock.js (bảng 2D) chỉ textContent, lần gửi đầu KHÔNG có `confirm=yes`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from cfharness.web import office as O
from test_web import BASE, _start, _stop, audit_rows, held, running_run, until  # noqa: F401 — dùng lại khung test web
from test_web_office import env, office_post  # noqa: F401 — fixture đăng nhập web

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(O.__file__).parent / "static"
NODE = shutil.which("node")


# =========================================================================== /office/state
def test_build_state_agent_board_fields():
    snap = {"agents": [
        {"id": "a", "status": "working", "paused": False, "current_run_id": "r_1", "run_status": "running",
         "run_started_at": "2026-11-12T10:00:00Z" + "x" * 80, "run_cost_usd": 0.25, "cost_today_usd": "1.5"},
        {"id": "b", "status": "paused", "paused": True, "current_run_id": None, "run_status": "completed", "cost_today_usd": None},
        {"id": "c", "status": "idle", "current_run_id": "r_2", "run_status": "Queued; rm"},
    ]}
    qs = [{"agent_id": "b", "text": "hỏi 1"}, {"agent_id": "b", "text": "hỏi 2"}]
    a, b, c = O.build_state(snap, qs, [], None)["agents"]
    assert a["paused"] is False and a["run_status"] == "running" and len(a["started_at"]) == 40 and a["run_cost_usd"] == 0.25
    assert a["cost_today_usd"] == 1.5 and a["questions"] == 0
    assert b["paused"] is True and b["run_status"] is None and b["started_at"] is None and b["cost_today_usd"] == 0.0
    assert b["questions"] == 2 and b["status"] == "paused"
    assert c["run_status"] is None  # chỉ từ khoá [a-z_]
    assert "hỏi 1" not in json.dumps(O.build_state(snap, qs, [], None), ensure_ascii=False)


async def test_office_state_agent_board_fields_live(held):
    env = held
    await env.login()
    run = await running_run(env, "ag0")
    env.hub.agents.create({"id": "ag1", "name": "ag1", "model": "haiku"})
    env.hub.agents.set_paused("ag1", True)
    d = (await env.client.get("/office/state")).json()
    ag = {a["id"]: a for a in d["agents"]}
    assert ag["ag0"]["run_id"] == run["id"] and ag["ag0"]["run_status"] == "running" and ag["ag0"]["started_at"]
    assert ag["ag0"]["paused"] is False and isinstance(ag["ag0"]["cost_today_usd"], float)
    assert ag["ag1"]["paused"] is True and ag["ag1"]["status"] == "paused" and ag["ag1"]["run_id"] is None


# =========================================================================== thao tác
async def test_controls_require_login_and_guard(held):
    env = held
    run = await running_run(env)
    for name, rid in (("agent_pause", "ag0"), ("agent_resume", "ag0"), ("stop", run["id"])):
        r = await env.client.post(f"/office/do/{name}", data={"id": rid, "confirm": "yes"})
        assert r.status_code == 401, name
    await env.login()
    full = {"id": run["id"], "confirm": "yes"}
    for c in ({"data": {"k": env.key, **full}, "headers": {"Origin": BASE}},
              {"data": {"csrf": env.csrf, **full}, "headers": {"Origin": BASE}},
              {"data": {"csrf": env.csrf, "k": env.key, **full}, "headers": {"Origin": "http://evil.example"}}):
        r = await env.client.post("/office/do/stop", data=c["data"], headers=c["headers"])
        assert r.status_code == 403 and r.json()["ok"] is False, c
    assert env.hub.store.get_run(run["id"])["status"] == "running"
    assert env.hub.agents.get_row("ag0")["status"] != "paused"


async def test_pause_resume_confirm_first_then_audited(held):
    env = held
    await env.login()
    env.hub.agents.create({"id": "ag0", "name": "ag0", "model": "haiku"})
    j = (await office_post(env, "agent_pause", {"id": "ag0"})).json()
    assert j["need"] == "confirm" and "ag0" in j["message"]
    assert env.hub.agents.get_row("ag0")["status"] != "paused"  # chưa xác nhận → chưa ghi
    j = (await office_post(env, "agent_pause", {"id": "ag0", "confirm": "yes"})).json()
    assert j["ok"] is True and env.hub.agents.get_row("ag0")["status"] == "paused"
    assert audit_rows(env, "agent.pause")[-1]["channel"] == "web"
    assert (await env.client.get("/office/state")).json()["agents"][0]["paused"] is True
    j = (await office_post(env, "agent_resume", {"id": "ag0"})).json()
    assert j["need"] == "confirm" and env.hub.agents.get_row("ag0")["status"] == "paused"
    j = (await office_post(env, "agent_resume", {"id": "ag0", "confirm": "yes"})).json()
    assert j["ok"] is True and env.hub.agents.get_row("ag0")["status"] != "paused"
    assert (await office_post(env, "agent_pause", {"confirm": "yes"})).status_code == 400  # thiếu id


async def test_stop_run_confirm_first_then_stops(held):
    env = held
    await env.login()
    run = await running_run(env)
    j = (await office_post(env, "stop", {"id": run["id"]})).json()
    assert j["need"] == "confirm" and j["danger"] is True and run["id"] in j["message"]
    assert env.hub.store.get_run(run["id"])["status"] == "running"
    j = (await office_post(env, "stop", {"id": run["id"], "confirm": "yes"})).json()
    assert j["ok"] is True and "run_id" not in j
    assert await until(lambda: env.hub.store.get_run(run["id"])["status"] not in ("running", "queued"))
    assert audit_rows(env, "run.stop")[-1]["channel"] == "web"


def test_whitelist_names_map_to_2d_actions():
    from cfharness.web.app import ACTIONS

    for name in ("agent_pause", "agent_resume", "stop", "estop", "team_estop", "agent_delete"):
        assert O.OFFICE_ACTIONS[name] == name and ACTIONS[name].confirm, name  # có confirm → plan_action luôn hỏi trước
    assert set(O.OFFICE_CONFIRM2) == {"estop", "team_estop", "agent_delete"}  # ảnh hưởng rộng → bước 2
    assert "run_resume" not in O.OFFICE_ACTIONS and "agent_purge" not in O.OFFICE_ACTIONS and "gates_mode" not in O.OFFICE_ACTIONS


async def test_estop_two_steps_then_audited(held):
    env = held
    await env.login()
    j = (await office_post(env, "estop", {"on": "1", "stop_running": "0"})).json()
    assert j["need"] == "confirm" and "ESTOP" in j["message"]
    j = (await office_post(env, "estop", {"on": "1", "stop_running": "0", "confirm": "yes"})).json()
    assert j["need"] == "confirm2" and j["kind"] == "check" and "KHÔNG run mới" in j["check"]
    assert env.hub.runs.estop["on"] is False  # chưa qua bước 2 → chưa ghi
    j = (await office_post(env, "estop", {"on": "1", "stop_running": "0", "confirm": "yes", "confirm2": "yes"})).json()
    assert j["ok"] is True and env.hub.runs.estop["on"] is True
    assert audit_rows(env, "estop.set")[-1]["channel"] == "web"
    assert (await env.client.get("/office/state")).json()["hub"]["estop"] is True
    j = (await office_post(env, "estop", {"on": "0", "confirm": "yes"})).json()
    assert j["need"] == "confirm2" and env.hub.runs.estop["on"] is True  # tắt cũng 2 bước
    j = (await office_post(env, "estop", {"on": "0", "confirm": "yes", "confirm2": "yes"})).json()
    assert j["ok"] is True and env.hub.runs.estop["on"] is False


async def test_team_estop_and_agent_delete_two_steps(held):
    env = held
    await env.login()
    env.hub.agents.create({"id": "ag9", "name": "ag9", "model": "haiku"})
    t = env.hub.teams.create({"id": "docs", "name": "Docs", "members": [{"agent_id": "ag9", "role": "worker"}]})
    tid = t["id"]
    j = (await office_post(env, "team_estop", {"id": tid, "on": "1", "confirm": "yes"})).json()
    assert j["need"] == "confirm2" and tid in j["check"]
    j = (await office_post(env, "team_estop", {"id": tid, "on": "1", "confirm": "yes", "confirm2": "yes"})).json()
    assert j["ok"] is True
    st = (await env.client.get("/office/state")).json()
    assert [x["estop"] for x in st["teams"] if x["id"] == tid] == [True]
    f = {"id": "ag9", "detach_teams": "1"}  # bảng 2D gửi kèm khi agent đang trong team
    j = (await office_post(env, "agent_delete", f)).json()
    assert j["need"] == "confirm" and j["danger"] is True and "gỡ khỏi mọi team" in j["message"]
    j = (await office_post(env, "agent_delete", {**f, "confirm": "yes"})).json()
    assert j["need"] == "confirm2" and "thùng rác" in j["check"] and "gỡ khỏi team" in j["check"]
    assert env.hub.agents.get_row("ag9", allow_archived=True).get("deleted_at") is None
    j = (await office_post(env, "agent_delete", {**f, "confirm": "yes", "confirm2": "yes"})).json()
    assert j["ok"] is True, j
    assert env.hub.agents.get_row("ag9", allow_archived=True).get("deleted_at")


# =========================================================================== client
@pytest.mark.skipif(not NODE, reason="cần Node để import roster.js / dựng World")
def test_roster_board_headless():
    r = subprocess.run([NODE, str(ROOT / "tests" / "office_roster_check.mjs"), str(ROOT)], capture_output=True, text=True,
                       encoding="utf-8", timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL" not in r.stdout and r.stdout.count("ok ") >= 12, r.stdout


def test_dock_static_rules():
    """Điều khiển agent = bảng 2D DOM (office/dock.js): chữ agent chỉ qua textContent, không HTML/style inline; lần gửi đầu không có
    confirm; confirm=yes chỉ trong trình xử lý nút "Đồng ý" do người bấm."""
    roster = (STATIC / "office" / "roster.js").read_text(encoding="utf-8")
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|createElement|fetch\(|location|document", roster)
    dock = (STATIC / "office" / "dock.js").read_text(encoding="utf-8")
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|\.style\b|setAttribute\('style'|fetch\(", dock)
    first = dock[dock.index("async function control("):dock.index("function askConfirm(")]
    assert "confirm: 'yes'" not in first and "'/office/do/'" in first
    yes = dock[dock.index("function askConfirm("):dock.index("function yesLabel(")]
    assert yes.count("confirm: 'yes'") == 1 and "button(" in yes  # chỉ trong nút "Đồng ý" do người bấm
    two = dock[dock.index("function askConfirm2("):dock.index("async function send(")]
    assert two.count("confirm2: 'yes'") == 1 and "cb.checked" in two  # bước 2: phải tích ô
    assert "confirm:" not in dock[dock.index("function estopChoice("):dock.index("async function control(")]  # chọn mức ≠ xác nhận
    js = (STATIC / "office.js").read_text(encoding="utf-8")
    assert "createDock(" in js and "drawRoster" not in js and "flows.control(" not in js
    assert "dock.control('agent_pause'" in js
    flows = (STATIC / "office" / "flows.js").read_text(encoding="utf-8")
    assert "function control(" not in flows
    world = (STATIC / "office" / "world.js").read_text(encoding="utf-8")
    assert "'agents'" not in world and "ROSTER" not in world
