"""Chế độ 3D "Văn phòng" (web/office.py, docs/dev-notes/office-3d.md): `/office`, `/office/state`, `/sse/office`, nút chuyển 2D/3D.

Cùng khung với tests/test_web.py: daemon IN-PROCESS trên socket thật + FakeRuntime, app Starlette qua ASGITransport. Không gọi `claude`.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from cfharness.web import office as O
from test_web import BASE, ISOLATED, _start, _stop, audit_rows, held, open_approval, open_uncertain, running_run, until  # noqa: F401 — dùng lại khung test web

from cfharness.runtime import FakeRuntime

WEB = Path(O.__file__).parent
HAS_PAGE = (WEB / "templates" / "office.html").exists()
need_page = pytest.mark.skipif(not HAS_PAGE, reason="templates/office.html chưa có (luồng client đang làm song song)")
FREE_TEXT = ("text", "input", "args_preview", "result_preview", "preview", "prompt_preview", "summary", "reason", "html")


@pytest.fixture
async def env(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    yield e
    await _stop(e)


# =========================================================================== xác thực
async def test_office_requires_login(env):
    c = env.client
    r = await c.get("/office")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    for path in ("/office/state", "/sse/office"):
        r = await c.get(path)
        assert r.status_code == 401, path


async def test_office_state_polling_does_not_extend_session(env):
    await env.login()
    from cfharness.web.security import COOKIE

    sess = env.auth.sessions[env.client.cookies.get(COOKIE)]
    sess.last -= 100
    before = sess.last
    assert (await env.client.get("/office/state")).status_code == 200
    assert sess.last == before


@need_page
async def test_office_page_no_inline_script_and_csp_unchanged(env):
    await env.login()
    ref = await env.client.get("/")
    r = await env.client.get("/office")
    assert r.status_code == 200
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", r.text)
    assert "cdn" not in r.text.lower() and "unpkg" not in r.text.lower()
    assert r.headers["content-security-policy"] == ref.headers["content-security-policy"]
    assert not re.search(r"\sstyle=", r.text)  # không style inline


@need_page
async def test_office_page_is_standalone_canvas_only(env):
    """Chế độ 3D = toàn trang 3D: không sidebar/thanh trên/tab đáy/bảng bên/HUD chữ — chỉ canvas + nút "2D" + danh sách sr-only."""
    await env.login()
    html = (await env.client.get("/office")).text
    for bad in ('class="side"', 'class="shell', 'class="mtop"', 'class="tabbar"', "<nav", 'id="view-toggle"', 'class="nav', "app.js",
                "htmx", 'href="/agents', 'href="/runs', 'class="pill', "office-side", "office-help"):
        assert bad not in html, bad
    assert html.lstrip().lower().startswith("<!doctype html>") and '<meta charset="utf-8">' in html and 'name="viewport"' in html
    assert 'name="color-scheme"' in html and "favicon.svg" in html and "office.css" in html
    assert re.search(r'<script type="module" src="/static/office\.js[^"]*"></script>', html)
    assert 'id="office-canvas"' in html and 'data-state="/office/state"' in html and 'data-sse="/sse/office"' in html
    m = re.search(r'<a [^>]*id="office-to-2d"[^>]*>', html)
    assert m and 'href="/"' in m.group(0)
    # phần chữ chỉ cho trình đọc màn hình: nằm trong khối sr-only
    sr = html.index('class="sr-only"')
    for i in ('id="office-list"', 'id="office-hud"', 'id="office-panel"', 'id="office-expired"'):
        assert html.index(i) > sr, i
    assert 'aria-live="polite"' in html
    # các trang khác vẫn có giao diện 2D đầy đủ
    assert 'class="side"' in (await env.client.get("/agents")).text


# =========================================================================== /office/state
async def test_office_state_shape(env):
    await env.login()
    hub = env.hub
    hub.agents.create({"id": "lap-trinh", "name": "Lập trình viên", "model": "haiku", "role": "Software engineer"})
    hub.agents.create({"id": "doc", "name": "doc", "model": "haiku"})
    hub.teams.create({"id": "toa-soan", "name": "Toà soạn", "members": [{"agent_id": "lap-trinh", "role": "worker"}]})
    hub.teams.create({"id": "zz-team", "name": "ZZ", "members": [{"agent_id": "lap-trinh", "role": "lead"}]})
    r = await env.client.get("/office/state")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"].startswith("application/json")
    d = r.json()
    assert set(d) == {"epoch", "last_seq", "hub", "manager_id", "teams", "agents", "task_counts", "tasks", "approvals", "questions", "runs"}
    assert set(d["hub"]) == {"estop", "running", "queued", "rate_limited", "approvals_waiting_human", "cost_today_usd"}
    assert set(d["task_counts"]) == {"ready", "running", "review", "blocked"}
    assert set(d["tasks"]) == {"ready", "running", "review", "blocked"} and all(isinstance(v, list) for v in d["tasks"].values())
    assert d["approvals"] == [] and d["questions"] == [] and d["runs"] == []
    assert [t["id"] for t in d["teams"]] == ["toa-soan", "zz-team"]
    ag = {a["id"]: a for a in d["agents"]}
    keys = {"id", "name", "role", "team", "team_role", "status", "tool", "task", "run_id", "approvals", "is_manager"}
    assert all(set(a) == keys for a in d["agents"])
    lt = ag["lap-trinh"]
    assert lt["team"] == "toa-soan" and lt["team_role"] == "worker"  # nhiều team → team đầu tiên
    assert lt["name"] == "Lập trình viên" and lt["status"] in O.STATUSES and not lt["is_manager"]
    assert ag["doc"]["team"] is None and ag["doc"]["team_role"] is None
    assert d["manager_id"] is None
    assert not any(k in json.dumps(d) for k in ('"last_tool_preview"', '"args_preview"', '"result_preview"'))


async def test_office_state_after_run_has_tool_name_only(env):
    await env.login()
    env.hub.agents.create({"id": "ag0", "name": "ag0", "model": "haiku"})
    run = env.hub.runs.submit("ag0", "dòng đầu của việc\nchi tiết dài không được gửi")
    await env.hub.runs.wait(run["id"], 20)
    d = (await env.client.get("/office/state")).json()
    a = next(x for x in d["agents"] if x["id"] == "ag0")
    assert a["tool"] is None or re.fullmatch(r"[A-Za-z0-9_\-:.]{1,64}", a["tool"])
    assert "chi tiết dài" not in json.dumps(d, ensure_ascii=False)


def test_build_state_pure_rules():
    snap = {
        "epoch": "e-1", "last_seq": 7,
        "hub": {"running": 1, "queued": 2, "estop": {"on": True}, "rate_limit": {"status": "rejected"}, "approvals_waiting_human": 1},
        "agents": [
            {"id": "a", "name": "A", "status": "idle", "last_tool": "Bash; rm -rf /", "task": "\x1b[31mđỏ‮\n2", "current_run_id": None,
             "pending_approvals": ["ap1", "ap2"], "last_tool_preview": "rm -rf /"},
            {"id": "m", "name": "M", "status": "weird", "task": "x" * 200},
        ],
    }
    teams = [{"id": "t1", "name": "T1", "members": [{"agent_id": "a", "role": "lead"}], "counts": {"ready": 2, "review": 1, "done": 9}},
             {"id": "t2", "name": "T2", "members": [{"agent_id": "a", "role": "worker"}], "counts": {"ready": 1, "blocked": 3}}]
    d = O.build_state(snap, [{"agent_id": "a"}], teams, "m")
    assert d["hub"] == {"estop": True, "running": 1, "queued": 2, "rate_limited": True, "approvals_waiting_human": 1, "cost_today_usd": 0.0}
    assert d["task_counts"] == {"ready": 3, "running": 0, "review": 1, "blocked": 3}
    a, m = d["agents"]
    assert a["status"] == "asking" and a["team"] == "t1" and a["team_role"] == "lead" and a["approvals"] == 2
    assert a["tool"] == "Bashrm-rf" and a["task"] == "[31mđỏ"  # ký tự điều khiển/bidi bị bỏ, chỉ dòng đầu
    assert "last_tool_preview" not in a
    assert m["status"] == "idle" and m["is_manager"] and len(m["task"]) == 80 and d["manager_id"] == "m"
    assert O.build_state({"hub": {"rate_limit": {"status": "allowed"}}}, [], [], None)["hub"]["rate_limited"] is False


async def test_office_state_lists_running_run_and_tasks(held):
    """Daemon thật: run đang chạy xuất hiện ở `runs` (chỉ id/agent/giờ bắt đầu/chi phí); task.list → cột kanban, tiêu đề đã cắt."""
    env = held
    await env.login()
    run = await running_run(env, "ag0", "dòng đầu\nprompt chi tiết không được gửi")
    env.hub.teams.create({"id": "t1", "name": "T1", "members": [{"agent_id": "ag0", "role": "worker"}]})
    env.hub.tasks.create({"team": "t1", "title": "Việc " + "z" * 100, "body": "thân task bí mật", "assignee": "ag0"})
    d = (await env.client.get("/office/state")).json()
    rr = [r for r in d["runs"] if r["id"] == run["id"]]
    assert rr and rr[0]["agent"] == "ag0" and rr[0]["started_at"] and isinstance(rr[0]["cost_usd"], float)
    titles = [t["title"] for col in d["tasks"].values() for t in col]
    assert titles and all(len(t) <= O.TITLE_MAX for t in titles)
    dump = json.dumps(d, ensure_ascii=False)
    assert "prompt chi tiết" not in dump and "thân task bí mật" not in dump


def test_build_state_new_fields_truncated_server_side():
    """approvals/questions/tasks/runs + chi phí hôm nay: chữ tự do bị cắt Ở SERVER (một dòng, bỏ ký tự điều khiển/bidi), id an toàn."""
    long = "rm -rf /data\n" + "x" * 500 + "\x1b[31m\u202e"
    snap = {
        "hub": {"cost_today_usd": "1.234567891"},
        "agents": [{"id": "a", "status": "working", "current_run_id": "r_1", "run_started_at": "2026-10-29T10:00:00Z", "run_cost_usd": 0.5},
                   {"id": "b", "status": "idle", "current_run_id": None}],
        "approvals": [{"id": "ap1", "agent_id": "a", "tool_name": "Bash", "destructive_class": "fs_delete", "status": "waiting_human",
                       "wait_s": 12.7, "preview": long, "tool_input": {"command": "secret-input"}, "reason": "lý do tự do"}],
    }
    qs = [{"id": "q1", "agent_id": "b", "status": "with_manager", "text": "Câu hỏi\n" + "y" * 400, "options": ["bí mật"]}]
    tasks = [{"id": f"t{i}", "status": st, "title": "T" * 100 + "\nbody", "assignee_agent_id": "a", "body": "nội dung dài"}
             for st in ("ready", "running", "review", "blocked", "done") for i in range(7)]
    d = O.build_state(snap, qs, [], None, tasks)
    assert d["hub"]["cost_today_usd"] == 1.234568
    ap = d["approvals"][0]
    assert set(ap) == {"id", "agent", "tool", "klass", "status", "wait_s", "preview"}
    assert ap["tool"] == "Bash" and ap["klass"] == "fs_delete" and ap["status"] == "waiting_human" and ap["wait_s"] == 12
    assert len(ap["preview"]) == O.PREVIEW_MAX and ap["preview"].endswith("…") and "\n" not in ap["preview"] and "\x1b" not in ap["preview"]
    q = d["questions"][0]
    assert set(q) == {"id", "agent", "status", "text"} and len(q["text"]) == O.PREVIEW_MAX and "\n" not in q["text"]
    assert set(d["tasks"]) == {"ready", "running", "review", "blocked"}
    for col in d["tasks"].values():
        assert len(col) == O.TASK_TOP
        assert all(set(t) == {"id", "title", "assignee"} and len(t["title"]) == O.TITLE_MAX for t in col)
    assert d["runs"] == [{"id": "r_1", "agent": "a", "started_at": "2026-10-29T10:00:00Z", "cost_usd": 0.5}]
    dump = json.dumps(d, ensure_ascii=False)
    for leak in ("secret-input", "lý do tự do", "bí mật", "nội dung dài", "body", "\u202e"):
        assert leak not in dump, leak
    assert O.one_line(None, 10) is None and O.one_line(" \n\t ", 10) is None and O.one_line("a\u200bb", 10) == "a b"


# =========================================================================== SSE converter
def ev(type_: str, payload: dict | None = None, **kw) -> dict:
    return {"type": type_, "seq": kw.pop("seq", 5), "source": kw.pop("source", "hub"), "agent_id": kw.pop("agent_id", "ag0"),
            "payload": payload or {}, **kw}


def _no_free_text(frames: list[dict]) -> None:
    for f in frames:
        assert set(f) <= {"t", "seq", "type", "agent", "team", "tool", "status", "outcome"}, f
        assert not any(k in f for k in FREE_TEXT)


def test_converter_tool_start_has_name_only():
    out = O.office_frames(ev("tool.start", {"name": "Bash", "input": {"command": "cat secret"}, "args_preview": "cat secret"}, source="adapter"))
    assert out == [{"t": "ev", "seq": 5, "type": "tool.start", "agent": "ag0", "team": None, "tool": "Bash", "status": None, "outcome": None}]
    assert "secret" not in json.dumps(out)
    out = O.office_frames(ev("tool.complete", {"name": "mcp__x__y", "is_error": True, "result_preview": "boom"}, source="adapter"))
    assert out[0]["tool"] == "mcp__x__y" and out[0]["status"] == "error" and "boom" not in json.dumps(out)
    out = O.office_frames(ev("tool.start", {"name": "<img src=x onerror=alert(1)>" + "A" * 100}))
    assert re.fullmatch(r"[A-Za-z0-9_\-:.]{1,64}", out[0]["tool"])


def test_converter_drops_non_hub_run_end_and_free_text_events():
    assert O.office_frames(ev("run.completed", {"outcome": "completed"}, source="adapter")) == []
    assert O.office_frames(ev("message.complete", {"text": "hello"})) == []
    assert O.office_frames(ev("message.delta", {"text": "h"})) == []
    assert O.office_frames(ev("user.message", {"text": "x"})) == []
    out = O.office_frames(ev("run.completed", {"outcome": "completed", "summary": "kết quả"}))
    assert out[0]["outcome"] == "completed" and {"t": "refresh"} in out and "kết quả" not in json.dumps(out, ensure_ascii=False)
    out = O.office_frames(ev("run.failed", {"outcome": "Lỗi tự do có dấu cách"}))
    assert out[0]["outcome"] is None


def test_converter_estop_status_and_refresh():
    on = O.office_frames(ev("hub.estop", {"on": True, "reason": "bí mật"}, agent_id=None))
    off = O.office_frames(ev("hub.estop", {"on": False}, agent_id=None))
    assert on[0]["status"] == "on" and off[0]["status"] == "off" and on[0]["agent"] is None
    assert on[-1] == {"t": "refresh"} and "bí mật" not in json.dumps(on, ensure_ascii=False)
    te = O.office_frames(ev("team.estop", {"on": True, "team_id": "t1", "reason": "x"}, agent_id=None, team_id="t1"))
    assert te[0]["team"] == "t1" and te[0]["status"] == "on" and te[-1] == {"t": "refresh"}
    ts = O.office_frames(ev("task.status", {"from": "running", "to": "review", "reason": "chữ"}, team_id="t1"))
    assert ts[0]["status"] == "review" and ts[-1] == {"t": "refresh"}
    st = O.office_frames(ev("agent.status", {"status": "working"}))
    assert st[0]["status"] == "working" and st[-1] == {"t": "refresh"}
    for t in ("approval.open", "approval.delegated", "question.open", "question.answered", "run.started", "run.queued"):
        assert O.office_frames(ev(t, {"summary": "x", "text": "y"}))[-1] == {"t": "refresh"}, t
    ma = O.office_frames(ev("manager.action", {"tool": "task_create", "summary": "giao việc"}))
    assert ma[0]["tool"] == "task_create" and len(ma) == 1
    sp = O.office_frames(ev("subagent.spawn", {"description": "mô tả", "type": "general"}, source="adapter"))
    assert len(sp) == 1 and sp[0]["type"] == "subagent.spawn"
    frames = [f for t in sorted(O.ALLOWED) for f in O.office_frames(ev(t, {"text": "t", "summary": "s", "reason": "r", "on": True, "name": "Bash"}))]
    _no_free_text(frames)
    assert all("\"s\"" not in json.dumps(f) and "\"r\"" not in json.dumps(f) for f in frames)


async def test_sse_office_live(held):
    """Generator SSE toàn hub với run đang chạy: nhận khung hello + tool.start (chỉ tên tool) + refresh, không chữ tự do."""
    env = held
    await env.login()
    from starlette.requests import Request

    from cfharness.web.app import _stream

    st = env.hub.status()
    scope = {"type": "http", "app": env.app, "path": "/", "headers": [], "query_string": b""}
    params = {"level": "summary", "since_seq": int(st["last_seq"]), "epoch": st["epoch"]}
    out: list[dict] = []

    async def consume():
        async for chunk in _stream(Request(scope), params, O.office_frames):
            if chunk.startswith("data: "):
                out.append(json.loads(chunk[6:]))

    task = asyncio.create_task(consume())
    try:
        await until(lambda: any(m.get("t") == "hello" for m in out))
        await running_run(env)
        assert await until(lambda: any(m.get("type") == "tool.start" for m in out))
        env.rt.release()
        assert await until(lambda: any(m.get("type") == "run.completed" for m in out), 20)
    finally:
        task.cancel()
    assert out[0]["t"] == "hello" and any(m.get("t") == "refresh" for m in out)
    evs = [m for m in out if m.get("t") == "ev"]
    assert all(m["type"] in O.ALLOWED for m in evs)
    _no_free_text([m for m in out if m.get("t") in ("ev", "refresh")])
    assert all(m.get("tool") is None or re.fullmatch(r"[A-Za-z0-9_\-:.]{1,64}", m["tool"]) for m in evs)


async def test_sse_office_endpoint_streams(env):
    """Handler `/sse/office` (gọi trực tiếp — ASGITransport của httpx đệm cả thân response nên không đọc được luồng vô hạn)."""
    await env.login()
    from starlette.requests import Request

    st = env.hub.status()
    for qs in (b"", f"since={st['last_seq']}&epoch={st['epoch']}".encode()):
        scope = {"type": "http", "app": env.app, "path": "/sse/office", "headers": [], "query_string": qs, "method": "GET"}
        resp = await O.sse_office(Request(scope))
        assert resp.media_type == "text/event-stream" and resp.headers["cache-control"] == "no-store"
        it = resp.body_iterator
        frames = []
        async for chunk in it:
            if chunk.startswith("data: "):
                frames.append(json.loads(chunk[6:]))
                break
        await it.aclose()
        assert frames == [{"t": "hello", "truncated": False}]


# =========================================================================== nút chuyển 2D/3D
def test_mode_switch_static_logic():
    early = (WEB / "static" / "early.js").read_text(encoding="utf-8")
    app_js = (WEB / "static" / "app.js").read_text(encoding="utf-8")
    base = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
    css = (WEB / "static" / "app.css").read_text(encoding="utf-8")
    assert 'indexOf("#k=") === 0' in early and 'removeItem("cfh_view")' in early
    assert 'location.replace("/office")' in early and '"view-3d"' in early
    # chế độ 3D = toàn trang 3D: MỌI trang (trừ /office, /login) chuyển sang /office
    assert 'pth !== "/office" && pth !== "/login"' in early and 'location.pathname === "/"' not in early
    # đăng nhập lại (fragment #k=) không chuyển trang: nhánh xoá đứng TRƯỚC nhánh chuyển hướng
    assert early.index('removeItem("cfh_view")') < early.index('location.replace("/office")')
    assert 'localStorage.removeItem(KEY); localStorage.removeItem("cfh_view")' in app_js  # đăng xuất
    assert "[data-view-toggle]" in app_js and '"aria-pressed"' in app_js
    assert 'view_toggle(\'view-toggle\'' in base and "data-view-toggle" in base and 'href="{{ \'/\' if on3d else \'/office\' }}"' in base
    assert ".view-toggle[aria-pressed=\"true\"]" in css


async def test_view_toggle_rendered_as_link_on_2d_pages(env):
    await env.login()
    html = (await env.client.get("/agents")).text
    m = re.search(r'<a href="/office" id="view-toggle"[^>]*aria-pressed="false"[^>]*>', html)
    assert m and "data-view-toggle" in m.group(0)
    assert 'id="view-toggle-m"' in html


# =========================================================================== giai đoạn 2: duyệt + câu hỏi + chat trong 3D
async def test_phase2_endpoints_require_login(env):
    c = env.client
    for path in ("/office/approval/ap_x", "/office/question/q_x", "/office/chat", "/sse/office/chat"):
        assert (await c.get(path)).status_code == 401, path
    r = await c.post("/office/do/approve", data={"id": "x", "confirm": "yes", "confirm2": "yes"})
    assert r.status_code == 401


async def office_post(env, name: str, data: dict, **kw):
    return await env.client.post(f"/office/do/{name}", data={"csrf": env.csrf, "k": env.key, **data},
                                 headers={"X-Ahub-Fetch": "1", "Origin": BASE, **kw.pop("headers", {})}, **kw)


async def test_office_do_guard_rejects_missing_csrf_key_or_cross_origin(held):
    env = held
    await env.login()
    run = await running_run(env)
    ap = open_approval(env.hub, run["id"])
    full = {"id": ap["id"], "confirm": "yes", "confirm2": "yes"}
    cases = [
        {"data": {"k": env.key, **full}, "headers": {"Origin": BASE}},  # thiếu csrf
        {"data": {"csrf": env.csrf, **full}, "headers": {"Origin": BASE}},  # thiếu khoá phiên k
        {"data": {"csrf": env.csrf, "k": env.key, **full}, "headers": {"Origin": "http://evil.example"}},
        {"data": {"csrf": env.csrf, "k": env.key, **full}, "headers": {"Origin": BASE, "Sec-Fetch-Site": "cross-site"}},
    ]
    for c in cases:
        r = await env.client.post("/office/do/approve", data=c["data"], headers=c["headers"])
        assert r.status_code == 403 and r.json()["ok"] is False, c
    assert env.hub.approvals.get(ap["id"])["status"] == "open"
    r = await office_post(env, "stop", {"id": run["id"], "confirm": "yes"})  # ngoài danh sách trắng
    assert r.status_code == 404 and r.json()["ok"] is False
    r = await office_post(env, "estop", {"on": "1", "confirm": "yes"})
    assert r.status_code == 404 and not env.hub.runs.estop.get("on")


async def test_office_approve_needs_confirm_then_heavy_step2(held):
    env = held
    await env.login()
    run = await running_run(env)
    ap = env.hub.approvals.open(env.hub.store.get_run(run["id"]), tool_name="Bash", tool_input={"command": "rm -rf ~/projects"}, tool_use_id="t",
                                fingerprint="fp-h", verdict={"klass": "file", "rule": "rm_recursive_outside_run", "verdict": "destructive"}, channel="hook")
    d = (await env.client.get(f"/office/approval/{ap['id']}")).json()
    assert d["heavy"] and d["view"]["text"] == "rm -rf ~/projects" and d["can_rule"] and not d["always_offer"] and d["agent"] == "ag0"
    r = await office_post(env, "approve", {"id": ap["id"]})
    assert r.status_code == 200 and r.json()["need"] == "confirm" and env.hub.approvals.get(ap["id"])["status"] == "open"
    r = await office_post(env, "approve", {"id": ap["id"], "confirm": "yes"})
    j = r.json()
    assert j["need"] == "confirm2" and j["kind"] == "heavy" and j["view"]["text"] == "rm -rf ~/projects" and j["always"] is None
    assert env.hub.approvals.get(ap["id"])["status"] == "open" and env.key not in r.text
    r = await office_post(env, "approve", {"id": ap["id"], "confirm": "yes", "confirm2": "yes"})
    assert r.status_code == 200 and r.json()["ok"] is True
    got = env.hub.approvals.get(ap["id"])
    assert got["status"] == "allowed" and got["resolved_channel"] == "web"
    assert audit_rows(env, "approval.resolved")[-1]["channel"] == "web"
    light = open_approval(env.hub, run["id"])  # lớp nhẹ: xác nhận một bước
    r = await office_post(env, "approve", {"id": light["id"], "confirm": "yes"})
    assert r.json()["ok"] is True and env.hub.approvals.get(light["id"])["status"] == "allowed"


async def test_office_deny_with_note(held):
    env = held
    await env.login()
    run = await running_run(env)
    ap = open_approval(env.hub, run["id"])
    j = (await office_post(env, "deny", {"id": ap["id"]})).json()
    assert j["need"] == "confirm" and j["danger"] is True and j["note_label"]
    j = (await office_post(env, "deny", {"id": ap["id"], "confirm": "yes", "note": "đừng xoá thư mục này"})).json()
    assert j["ok"] is True
    got = env.hub.approvals.get(ap["id"])
    assert got["status"] == "denied" and got["resolved_channel"] == "web" and "đừng xoá" in (got.get("note") or "")


async def test_office_approve_always_and_rule(held):
    env = held
    await env.login()
    run = await running_run(env)
    bad = open_approval(env.hub, run["id"])
    r = await office_post(env, "approve_always", {"id": bad["id"], "confirm": "yes"})
    assert r.status_code == 400 and r.json()["ok"] is False and "luôn cho phép" in r.json()["error"]
    r = await office_post(env, "approve_always", {"id": bad["id"], "confirm": "yes", "confirm2": "yes"})
    assert r.status_code == 400 and env.hub.approvals.get(bad["id"])["status"] == "open" and env.hub.guard.policy()["always_allow"] == []
    ok = open_uncertain(env.hub, run["id"])
    assert (await env.client.get(f"/office/approval/{ok['id']}")).json()["always_offer"] is True
    assert (await office_post(env, "approve_always", {"id": ok["id"]})).json()["need"] == "confirm"
    j = (await office_post(env, "approve_always", {"id": ok["id"], "confirm": "yes"})).json()
    assert j["need"] == "confirm2" and j["kind"] == "always" and j["always"] and env.hub.approvals.get(ok["id"])["status"] == "open"
    assert (await office_post(env, "approve_always", {"id": ok["id"], "confirm": "yes", "confirm2": "yes"})).json()["ok"] is True
    assert env.hub.approvals.get(ok["id"])["status"] == "allowed" and len(env.hub.guard.policy()["always_allow"]) == 1
    # cho phép theo luật: 3D hỏi xác nhận (2D không có) rồi bước 2 grant (chọn thời hạn) — chưa quyết gì trước confirm2
    assert (await office_post(env, "approve_rule", {"id": bad["id"]})).json()["need"] == "confirm"
    j = (await office_post(env, "approve_rule", {"id": bad["id"], "confirm": "yes"})).json()
    assert j["need"] == "confirm2" and j["kind"] == "grant" and j["grant_for"] == ["", "8h", "24h", "7d", "30d"]
    # bước 2 nêu đúng luật + agent (như approve2.html)
    assert j["rule"] == env.hub.approvals.get(bad["id"])["matched_rule"] and j["rule"] and j["agent"] == bad["agent_id"]
    assert env.hub.approvals.get(bad["id"])["status"] == "open" and env.hub.guard.rule_grants() == {}
    j = (await office_post(env, "approve_rule", {"id": bad["id"], "confirm": "yes", "confirm2": "yes", "grant_for": "8h"})).json()
    assert j["ok"] is True and env.hub.approvals.get(bad["id"])["status"] == "allowed" and env.hub.guard.rule_grants()


async def test_office_answer_question(held):
    env = held
    await env.login()
    run = await running_run(env)
    q = env.hub.questions.open(env.hub.store.get_run(run["id"]), "Xoá kiểu nào?\nchi tiết dòng 2", ["Xoá mềm", "Xoá hẳn"])
    d = (await env.client.get(f"/office/question/{q['id']}")).json()
    assert d["text"] == "Xoá kiểu nào?\nchi tiết dòng 2" and [o["label"] for o in d["options"]] == ["Xoá mềm", "Xoá hẳn"]
    assert d["allow_text"] is True and d["status"] in ("open", "with_manager")
    assert (await office_post(env, "answer", {"id": q["id"]})).status_code == 400  # thiếu lựa chọn/chữ
    assert (await office_post(env, "answer", {"id": q["id"], "keys": "1"})).json()["need"] == "confirm"
    assert env.hub.questions.get(q["id"])["status"] != "answered"
    j = (await office_post(env, "answer", {"id": q["id"], "keys": "1", "confirm": "yes"})).json()
    assert j["ok"] is True and env.hub.questions.get(q["id"])["status"] == "answered"
    assert audit_rows(env, "question.answer")[-1]["channel"] == "web"
    assert (await env.client.get("/office/question/khong-co")).status_code == 404


async def test_office_chat_without_manager(env):
    await env.login()
    d = (await env.client.get("/office/chat")).json()
    assert d == {"available": False, "manager_id": None, "conversation_id": None, "conversations": [], "messages": []}
    assert (await env.client.get("/sse/office/chat")).status_code == 404


def test_chat_frames_json_no_html():
    f = O.chat_frames("c1")
    evil = "<script>alert(1)</script>"
    assert f({"type": "message.delta", "run_id": "r1", "payload": {"text": evil}}) == [{"t": "delta", "run": "r1", "text": evil}]
    assert f({"type": "message.delta", "run_id": "r1", "subagent_id": "s", "payload": {"text": "x"}}) == []
    assert f({"type": "manager.reply", "run_id": "r1", "payload": {"text": evil, "conversation_id": "c1", "kind": "notify"}}) == \
        [{"t": "reply", "run": "r1", "conv": "c1", "text": evil, "notify": True}]
    assert f({"type": "manager.reply", "run_id": "r1", "payload": {"text": "x", "conversation_id": "c2"}}) == []  # hội thoại khác
    a = f({"type": "manager.action", "run_id": "r1", "payload": {"tool": "task_create", "summary": evil, "ok": True}})
    assert a == [{"t": "action", "run": "r1", "tool": "task_create", "summary": evil, "ok": True}]
    q = f({"type": "question.open", "agent_id": "m", "run_id": "r1", "payload": {"question_id": "q1", "conversation_id": "c1", "text": evil}})
    assert q == [{"t": "question", "conv": "c1", "id": "q1"}]
    assert f({"type": "question.open", "payload": {"question_id": "q2", "text": "x"}}) == []  # không thuộc hội thoại
    assert f({"type": "run.completed", "source": "hub", "run_id": "r1", "payload": {"outcome": "completed"}}) == [{"t": "run_end", "run": "r1", "outcome": "completed"}]
    assert f({"type": "run.completed", "source": "adapter", "run_id": "r1", "payload": {}}) == []
    assert f({"type": "manager.tick.done", "payload": {}}) == [{"t": "tick"}]
    assert f({"type": "tool.start", "payload": {"name": "Bash"}}) == []
    for frame in a + q:
        assert "html" not in frame and "side" not in frame and "one" not in frame


def test_chat_messages_flatten_and_limit():
    tl = [{"kind": "user", "text": "chào", "ts": "1", "run_id": "r1"},
          {"kind": "actions", "run_id": "r1", "items": [{"kind": "action", "summary": "giao việc", "tool": "task_create", "ts": "2", "run_id": "r1", "ok": True}]},
          {"kind": "manager", "text": "<b>xong</b>", "ts": "3", "run_id": "r1"},
          {"kind": "question", "q": {"id": "q1", "text": "Chọn?", "status": "open"}, "ts": "4", "run_id": "r1"}]
    m = O.chat_messages(tl)
    assert [x["kind"] for x in m] == ["user", "action", "manager", "question"]
    assert m[1]["text"] == "giao việc" and m[1]["ok"] is True and m[2]["text"] == "<b>xong</b>" and m[3]["id"] == "q1"
    assert all("html" not in x for x in m)
    assert len(O.chat_messages([{"kind": "user", "text": str(i)} for i in range(80)])) == 50


def test_office_html_carries_csrf_attr():
    assert 'data-csrf="{{ csrf }}"' in (WEB / "templates" / "office.html").read_text(encoding="utf-8")


async def test_office_chat_with_manager_stub(hub_home):
    from cfharness.web import manager_stub

    e = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        manager_stub.install(e.d, force=True)
        e.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
        await e.login()
        r = await e.post("/do/manager_set", {"agent_id": "boss", "confirm": "yes", "next": "/manager"})
        assert r.status_code == 303
        d = (await e.client.get("/office/chat")).json()
        assert d["available"] and d["manager_id"] == "boss" and d["conversation_id"] is None and d["messages"] == []
        r = await e.post("/manager/send", {"text": "<b>chào</b> đội"}, headers={"X-Ahub-Fetch": "1"})
        assert r.status_code == 200
        cid = r.json()["conversation_id"]
        assert await until(lambda: any(m["role"] == "manager" for m in (e.d.methods["manager.history"]({"conversation_id": cid}) or [])), 20)
        d = (await e.client.get(f"/office/chat?c={cid}")).json()
        assert d["conversation_id"] == cid and [c["id"] for c in d["conversations"]] == [cid]
        kinds = [m["kind"] for m in d["messages"]]
        assert kinds[0] == "user" and "manager" in kinds and d["messages"][0]["text"] == "<b>chào</b> đội"
        assert all(set(m) >= {"kind", "text", "ts", "run_id"} and "html" not in m for m in d["messages"])
        assert (await e.client.get("/office/chat?c=khong-co")).status_code in (400, 404)
        # SSE chat: handler trả luồng JSON (gọi trực tiếp — ASGITransport đệm cả thân response)
        from starlette.requests import Request

        scope = {"type": "http", "app": e.app, "path": "/sse/office/chat", "headers": [], "query_string": f"c={cid}".encode(), "method": "GET"}
        resp = await O.sse_office_chat(Request(scope))
        it = resp.body_iterator
        first = None
        async for chunk in it:
            if chunk.startswith("data: "):
                first = json.loads(chunk[6:])
                break
        await it.aclose()
        assert first == {"t": "hello", "truncated": False}
    finally:
        await _stop(e)
