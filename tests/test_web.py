"""Giao diện web (`cfharness web`) — daemon IN-PROCESS trên Unix socket thật + FakeRuntime, app Starlette qua ASGITransport.

Không gọi `claude`. Kiểm: đăng nhập link một lần → cookie, chặn khi chưa đăng nhập, Host/Origin/CSRF, header bảo mật,
token người không lọt vào HTML/log, thao tác HUMAN_ONLY ghi audit kênh `web`, luồng hằng ngày, SSE, chỉ bind loopback,
agent trong run không chạm được bí mật của web.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import httpx2 as httpx
import pytest

from cfharness.auth import load_token
from cfharness.daemon import Daemon
from cfharness.runtime import FakeRuntime
from cfharness.web import present as P
from cfharness.web.app import agent_changes, create_app, replay_items
from cfharness.web.security import (
    COOKIE, NotLoopback, WebAuth, check_login_token, login_url, mint_login_token, read_state, require_loopback, state_path, write_state,
)

ISOLATED = Path(__file__).parent / "fixtures" / "claude-hub-isolated-run.jsonl"
PORT = 8765
BASE = f"http://cfharness.localhost:{PORT}"


class Env:
    def __init__(self, d: Daemon, task: asyncio.Task, rt: FakeRuntime, paths) -> None:
        self.d, self.task, self.rt, self.paths = d, task, rt, paths
        self.hub = d.hub
        self.auth = WebAuth()
        self.app = create_app(paths, auth=self.auth, port=PORT)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE)
        self.csrf = ""
        self.key = ""

    async def login(self) -> str:
        r = await self.client.get("/login?t=" + self.auth.mint())
        assert r.status_code == 303, r.text
        sid = r.cookies.get(COOKIE) or self.client.cookies.get(COOKIE)
        self.csrf = self.auth.sessions[sid].csrf
        loc = r.headers["location"]
        assert loc.startswith("/#k=")  # khoá phiên chỉ đi trong fragment (trình duyệt không gửi lên server)
        self.key = loc.split("#k=", 1)[1]
        assert self.key == self.auth.sessions[sid].key
        return self.csrf

    async def post(self, path: str, data: dict, **kw):
        return await self.client.post(path, data={"csrf": self.csrf, "k": self.key, **data}, **kw)

    def audit(self, kind: str | None = None) -> list[dict]:
        rows = self.hub.audit.list(limit=500) if hasattr(self.hub.audit, "list") else []
        return [r for r in rows if kind is None or r["kind"] == kind]


async def _start(paths, rt) -> Env:
    d = Daemon(paths, runtime=rt)
    task = asyncio.create_task(d.serve())
    for _ in range(200):
        if paths.socket.exists():
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    return Env(d, task, rt, paths)


async def _stop(env: Env) -> None:
    await env.client.aclose()
    env.d.stopping.set()
    try:
        await asyncio.wait_for(env.task, 20)
    except Exception:
        pass


@pytest.fixture
async def env(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    yield e
    await _stop(e)


@pytest.fixture
async def held(hub_home):
    rt = FakeRuntime(ISOLATED, line_delay_s=0.01, hold=True)
    e = await _start(hub_home, rt)
    yield e
    rt.release()
    await _stop(e)


async def until(pred, timeout: float = 10.0) -> bool:
    for _ in range(int(timeout / 0.05)):
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


def audit_rows(env: Env, kind: str) -> list[dict]:
    rows = env.hub.store.conn.execute("SELECT kind, actor, channel, data FROM audit_log WHERE kind=? ORDER BY id", (kind,)).fetchall()
    return [dict(r) for r in rows]


def open_approval(hub, run_id: str) -> dict:
    return hub.approvals.open(
        hub.store.get_run(run_id), tool_name="Bash", tool_input={"command": "rm -rf /srv/data"}, tool_use_id="toolu_x", fingerprint="fp-" + run_id,
        verdict={"klass": "fs_delete", "rule": "rm_recursive_outside_run", "verdict": "destructive", "reason": "xoá đệ quy"}, channel="hook",
    )


async def running_run(env: Env, agent: str = "ag0", prompt: str = "cleanup job") -> dict:
    if not env.hub.agents.list() or agent not in [a["id"] for a in env.hub.agents.list()]:
        env.hub.agents.create({"id": agent, "name": agent, "model": "haiku"})
    run = env.hub.runs.submit(agent, prompt)
    assert await until(lambda: (env.hub.store.get_run(run["id"]) or {}).get("status") == "running")
    return run


# =========================================================================== xác thực
async def test_unauthenticated_requests_are_blocked(env):
    c = env.client
    r = await c.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    for path in ("/partials/overview", "/sse/firehose", "/sse/run/x"):
        assert (await c.get(path)).status_code == 401, path
    assert (await c.get("/agents", headers={"HX-Request": "true"})).status_code == 401
    r = await c.post("/do/estop", data={"on": "1", "confirm": "yes"})
    assert r.status_code == 401
    assert not env.hub.runs.estop.get("on")
    assert (await c.get("/static/app.css")).status_code == 200
    r = await c.get("/login")
    assert r.status_code == 200 and "cfharness web --open" in r.text
    # cookie giả không có tác dụng
    c.cookies.set(COOKIE, "khong-hop-le")
    assert (await c.get("/")).status_code == 303


async def test_one_time_login_link_sets_strict_httponly_cookie(env):
    tok = env.auth.mint()
    r = await env.client.get("/login?t=" + tok)
    assert r.status_code == 303 and r.headers["location"].startswith("/#k=")
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=strict" in sc and "path=/" in sc
    assert tok not in r.headers.get("location", "")
    assert (await env.client.get("/")).status_code == 200
    # dùng lại link → từ chối (một lần)
    c2 = httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url=BASE)
    r = await c2.get("/login?t=" + tok)
    assert r.status_code == 401 and COOKIE not in r.cookies
    # link giả / sai chữ ký / hết hạn
    assert (await c2.get("/login?t=abc.1.def")).status_code == 401
    forged = mint_login_token(os.urandom(32))
    assert (await c2.get("/login?t=" + forged)).status_code == 401
    old = mint_login_token(env.auth.key, ttl_s=-5)
    assert check_login_token(env.auth.key, old) is None
    # dán link vào form đăng nhập
    r = await c2.post("/login", data={"link": login_url("cfharness.localhost", PORT, env.auth.mint())})
    assert r.status_code == 303
    await c2.aclose()
    # đăng xuất: phiên bị huỷ
    await env.login()
    sid = env.client.cookies.get(COOKIE)
    r = await env.post("/logout", {})
    assert r.status_code == 303 and sid not in env.auth.sessions


async def test_host_header_must_be_web_host(env):
    """WT-1(a): chỉ phục vụ Host `cfharness.localhost:port` (cookie host-only); 127.0.0.1/localhost → chuyển hướng, không đặt cookie."""
    await env.login()
    r = await env.client.get("/", headers={"Host": "evil.example:8765"})
    assert r.status_code == 421
    for h in (f"localhost:{PORT}", f"127.0.0.1:{PORT}", f"[::1]:{PORT}"):
        r = await env.client.get("/agents?x=1", headers={"Host": h})
        assert r.status_code == 303 and r.headers["location"] == f"{BASE}/agents?x=1", h
        assert "set-cookie" not in r.headers
    raw = httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url=f"http://127.0.0.1:{PORT}")
    tok = env.auth.mint()
    r = await raw.get("/login?t=" + tok)
    assert r.status_code == 303 and r.headers["location"] == f"{BASE}/login?t={tok}" and "set-cookie" not in r.headers
    r = await raw.post("/do/estop", data={"on": "1", "confirm": "yes"})
    assert r.status_code == 421 and not env.hub.runs.estop.get("on")
    await raw.aclose()


async def test_csrf_and_origin_required_for_writes(env):
    await env.login()
    env.hub.agents.create({"id": "ag0", "name": "ag0", "model": "haiku"})
    n0 = len(env.hub.store.list_runs(limit=100))
    r = await env.client.post("/do/run_start", data={"agent": "ag0", "prompt": "x"})
    assert r.status_code == 403
    r = await env.client.post("/do/run_start", data={"csrf": "sai", "agent": "ag0", "prompt": "x"})
    assert r.status_code == 403
    r = await env.post("/do/run_start", {"agent": "ag0", "prompt": "x"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = await env.post("/do/run_start", {"agent": "ag0", "prompt": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = await env.post("/agents/create", {"id": "zz", "template": "researcher"}, headers={"Referer": "http://evil.example/x"})
    assert r.status_code == 403
    assert len(env.hub.store.list_runs(limit=100)) == n0
    r = await env.post("/do/run_start", {"agent": "ag0", "prompt": "x"}, headers={"Origin": BASE})
    assert r.status_code == 303 and r.headers["location"].startswith("/runs/r_")


async def test_security_headers_and_no_cors(env):
    await env.login()
    r = await env.client.get("/", headers={"Origin": "http://evil.example"})
    h = r.headers
    assert "default-src 'self'" in h["content-security-policy"] and "script-src 'self'" in h["content-security-policy"]
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY" and h["referrer-policy"] == "same-origin" and h["x-content-type-options"] == "nosniff"
    assert h["cache-control"] == "no-store"
    assert not any(k.lower().startswith("access-control-") for k in h)
    # không script inline / CDN
    html = r.text
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert "cdn" not in html.lower() and "unpkg" not in html.lower()
    assert re.search(r'src="/static/htmx-2\.0\.4\.min\.js"', html)


async def test_human_token_never_in_html_or_logs(held, caplog):
    env = held
    caplog.set_level(logging.DEBUG)
    human = load_token(env.paths.socket)
    assert human and len(human) > 20
    await env.login()
    run = await running_run(env)
    ap = open_approval(env.hub, run["id"])
    pages = ["/", "/partials/overview", "/agents", "/agents/ag0", "/agents/ag0/edit", "/agents/new", "/runs", f"/runs/{run['id']}",
             f"/partials/run/{run['id']}", "/approvals", "/tasks", "/teams", "/learnings", "/mcp", "/costs", "/audit", "/settings"]
    for p in pages:
        r = await env.client.get(p)
        assert r.status_code == 200, (p, r.status_code, r.text[:500])
        assert human not in r.text, p
        assert env.auth.key.hex() not in r.text, p
        assert human not in str(r.headers), p
    await env.post("/do/approve", {"id": ap["id"], "confirm": "yes"})
    assert human not in caplog.text
    assert human not in json.dumps([dict(x) for x in env.hub.store.conn.execute("SELECT * FROM audit_log").fetchall()], default=str)
    assert human not in "".join(env.client.cookies.values())


# =========================================================================== luồng hằng ngày
async def test_daily_flow_create_agent_with_mcp_assign_task_see_result_and_cost(env):
    await env.login()
    env.hub.mcp.add({"name": "docs-mcp", "transport": "stdio", "command": "true", "trust": "trusted"})
    env.hub.mcp.add({"name": "other", "transport": "stdio", "command": "true"})
    r = await env.client.get("/agents/new")
    assert "docs-mcp" in r.text and 'name="mcp"' in r.text and "researcher" in r.text
    r = await env.post("/agents/create", {"id": "nc", "template": "researcher", "name": "Nghiên cứu", "mcp": ["docs-mcp"], "max_budget_usd_per_run": "0.3", "confirm": "yes"})
    assert r.status_code == 303 and r.headers["location"] == "/agents/nc", r.text[:400]
    a = env.hub.agents.show("nc")
    assert a["mcp_servers"] == ["docs-mcp"] and a["name"] == "Nghiên cứu"
    assert a["tool_policy"]["tools"] == ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]  # ô trống = theo template
    assert a["limits"]["max_budget_usd_per_run"] == 0.3
    # lỗi nhập → form hiện lại kèm lỗi, không tạo
    r = await env.post("/agents/create", {"id": "Sai Id", "template": "researcher", "confirm": "yes"})
    assert r.status_code == 400 and "id" in r.text
    # sửa: bỏ docs-mcp, thêm other
    r = await env.post("/agents/nc/save", {"name": "Nghiên cứu 2", "mcp": ["other"], "model": "haiku", "role": "r", "description": "", "soul": a["soul"], "confirm": "yes"})
    assert r.status_code == 303
    assert env.hub.agents.show("nc")["mcp_servers"] == ["other"]
    # giao việc
    r = await env.post("/do/run_start", {"agent": "nc", "prompt": "Tóm tắt README <b>đậm</b>", "next": "/agents/nc"})
    assert r.status_code == 303
    rid = r.headers["location"].rsplit("/", 1)[1]
    final = await env.hub.runs.wait(rid, 20)
    assert final["status"] == "completed"
    r = await env.client.get(f"/runs/{rid}")
    assert r.status_code == 200
    assert "Kết quả" in r.text and "Xong" in r.text and "Nói tiếp với agent" in r.text
    assert "&lt;b&gt;đậm&lt;/b&gt;" in r.text and "<b>đậm</b>" not in r.text  # escape
    assert f"cfharness takeover {rid}" in r.text
    r = await env.client.get("/costs")
    assert "nc" in r.text and "$0.00" in r.text
    r = await env.client.get("/")
    assert "Nghiên cứu 2" in r.text
    # tiếp hội thoại
    r = await env.post("/do/run_continue", {"id": rid, "prompt": "ngắn hơn"})
    assert r.status_code == 303 and r.headers["location"] != f"/runs/{rid}"
    # audit kênh web
    for kind in ("agent.create_from_template", "agent.update", "run.start", "run.continue"):
        rows = audit_rows(env, kind)
        assert rows and rows[-1]["channel"] == "web" and rows[-1]["actor"] == "user", kind


async def test_approve_deny_need_confirmation_and_are_audited_web(held):
    env = held
    await env.login()
    run = await running_run(env)
    ap = open_approval(env.hub, run["id"])
    env.hub.approvals.to_waiting_human(ap["id"])
    r = await env.client.get("/")
    assert "Chờ bạn duyệt" in r.text and "rm -rf /srv/data" in r.text and "data-confirm" in r.text
    # chưa xác nhận → trang xác nhận, chưa quyết
    r = await env.post("/do/approve", {"id": ap["id"], "preview": "rm -rf /srv/data", "next": "/"})
    assert r.status_code == 200 and "Xác nhận" in r.text and 'name="confirm" value="yes"' in r.text
    assert env.hub.approvals.get(ap["id"])["status"] == "waiting_human"
    r = await env.post("/do/approve", {"id": ap["id"], "confirm": "yes", "next": "/"})
    assert r.status_code == 303
    assert env.hub.approvals.get(ap["id"])["status"] == "allowed"
    rows = audit_rows(env, "approval.resolved")
    assert rows[-1]["channel"] == "web" and rows[-1]["actor"] == "user"
    # duyệt lần hai (viewer khác) → lỗi rõ ràng, không đổi
    r = await env.post("/do/deny", {"id": ap["id"], "confirm": "yes", "next": "/approvals"})
    assert r.status_code == 303
    r = await env.client.get("/approvals")
    assert "flash error" in r.text
    # từ chối kèm ghi chú
    ap2 = open_approval(env.hub, run["id"])
    r = await env.post("/do/deny", {"id": ap2["id"], "confirm": "yes", "note": "không xoá dữ liệu thật"})
    a2 = env.hub.approvals.get(ap2["id"])
    assert a2["status"] == "denied" and a2["note"] == "không xoá dữ liệu thật" and a2["resolved_channel"] == "web"


async def test_steer_and_stop_from_run_page(held):
    env = held
    await env.login()
    run = await running_run(env)
    r = await env.client.get(f"/runs/{run['id']}")
    assert "Chỉ đạo agent" in r.text and f"/sse/run/{run['id']}" in r.text and "Dừng run" in r.text
    r = await env.post("/do/steer", {"id": run["id"], "text": "chỉ xem src/"})
    assert r.status_code == 303
    assert env.hub.store.get_run(run["id"])["steer_count"] == 1
    assert any(e.type == "user.message" and e.payload.get("channel") == "web" for e in env.hub.store.query_events(run_id=run["id"]))
    r = await env.post("/do/stop", {"id": run["id"]})
    assert "Xác nhận" in r.text and env.hub.store.get_run(run["id"])["status"] == "running"
    await env.post("/do/stop", {"id": run["id"], "confirm": "yes"})
    final = await env.hub.runs.wait(run["id"], 20)
    assert final["status"] == "cancelled"
    assert audit_rows(env, "run.stop")[-1]["channel"] == "web"
    assert audit_rows(env, "run.steer")[-1]["channel"] == "web"


async def test_human_only_admin_actions_audited_as_web(env):
    await env.login()
    env.hub.agents.create({"id": "ag0", "name": "ag0", "model": "haiku"})
    # ESTOP
    await env.post("/do/estop", {"on": "1", "confirm": "yes", "note": "thử"})
    assert env.hub.runs.estop["on"]
    await env.post("/do/estop", {"on": "0", "confirm": "yes"})
    assert not env.hub.runs.estop["on"]
    # cấu hình an toàn
    await env.post("/do/config_set", {"key": "hub.max_concurrent_runs", "value": "3", "confirm": "yes"})
    assert env.hub.config.get("hub.max_concurrent_runs") == 3
    # khoá không nằm trong danh sách an toàn → từ chối
    r = await env.post("/do/config_set", {"key": "guard.enabled", "value": "false", "confirm": "yes", "next": "/settings"})
    assert r.status_code == 303 and env.hub.config.get("guard.enabled") is True
    # secret: giá trị không bao giờ hiện lại
    await env.post("/do/secret_set", {"name": "MY_TOKEN", "value": "sieu-bi-mat-123", "confirm": "yes"})
    r = await env.client.get("/settings")
    assert "MY_TOKEN" in r.text and "sieu-bi-mat-123" not in r.text
    # xoá mềm + khôi phục + revert
    await env.post("/do/agent_delete", {"id": "ag0", "confirm": "yes"})
    assert "ag0" not in [a["id"] for a in env.hub.agents.list()]
    r = await env.client.get("/agents")
    assert "Thùng rác" in r.text
    await env.post("/do/agent_restore", {"id": "ag0", "confirm": "yes"})
    assert "ag0" in [a["id"] for a in env.hub.agents.list()]
    await env.post("/do/agent_pause", {"id": "ag0", "confirm": "yes"})
    assert env.hub.agents.show("ag0")["status"] == "paused"
    await env.post("/do/agent_resume", {"id": "ag0", "confirm": "yes"})
    await env.post("/do/learn_toggle", {"agent": "ag0", "on": "0", "confirm": "yes"})
    for kind in ("estop.set", "config.set", "secret.set", "agent.delete", "trash.restore", "agent.pause", "agent.resume", "learn.policy_set"):
        rows = audit_rows(env, kind)
        assert rows and rows[-1]["channel"] == "web" and rows[-1]["actor"] == "user", kind
    assert "sieu-bi-mat-123" not in json.dumps([dict(x) for x in env.hub.store.conn.execute("SELECT * FROM audit_log").fetchall()], default=str)


async def test_tasks_and_teams_from_web(env):
    await env.login()
    for a in ("w1", "rv"):
        env.hub.agents.create({"id": a, "name": a, "model": "haiku"})
    r = await env.post("/do/team_create", {"id": "docs", "name": "Docs", "charter": "Viết tài liệu"})
    assert r.status_code == 303 and env.hub.teams.show("docs")["name"] == "Docs"
    await env.post("/do/team_member", {"team": "docs", "agent": "w1", "role": "worker", "confirm": "yes"})
    r = await env.post("/do/task_create", {"title": "Cha", "assignee": "w1", "body": "x"})
    t1 = r.headers["location"].rsplit("/", 1)[1]
    r = await env.post("/do/task_create", {"title": "Con", "assignee": "rv", "parents": [t1]})
    t2 = r.headers["location"].rsplit("/", 1)[1]
    r = await env.client.get(f"/tasks/{t2}")
    assert r.status_code == 200 and "Cha" in r.text  # DAG: thấy task cha
    await env.post("/do/task_comment", {"id": t2, "text": "nhớ ghi nguồn"})
    assert any(c["text"] == "nhớ ghi nguồn" for c in env.hub.tasks.get(t2)["comments"])
    r = await env.client.get("/tasks")
    assert "Con" in r.text
    assert audit_rows(env, "team.create")[-1]["channel"] == "web"
    assert audit_rows(env, "task.create")[-1]["channel"] == "web"


async def test_sse_run_replays_and_ends(env):
    await env.login()
    env.hub.agents.create({"id": "ag0", "name": "ag0", "model": "haiku"})
    run = env.hub.runs.submit("ag0", "hello")
    await env.hub.runs.wait(run["id"], 20)
    r = await env.client.get(f"/sse/run/{run['id']}?since=0")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    msgs = [json.loads(ln[6:]) for ln in r.text.splitlines() if ln.startswith("data: ")]
    assert msgs[-1]["t"] == "end"
    r = await env.client.get("/sse/run/khong-co")
    msgs = [json.loads(ln[6:]) for ln in r.text.splitlines() if ln.startswith("data: ")]
    assert msgs and msgs[-1]["t"] == "end"


async def test_sse_run_live_items_and_deltas(held):
    """Generator SSE với run đang chạy: nhận item tool/message + delta, kết thúc khi run xong."""
    env = held
    await env.login()
    run = await running_run(env)
    from starlette.requests import Request

    from cfharness.web.app import _run_converter, _stream

    scope = {"type": "http", "app": env.app, "path": "/", "headers": [], "query_string": b""}
    req = Request(scope)
    params = {"run_id": run["id"], "since_seq": 0, "level": "detail", "until_run_end": True}
    out: list[dict] = []

    async def consume():
        async for chunk in _stream(req, params, _run_converter(env.app.state.web.templates)):
            if chunk.startswith("data: "):
                out.append(json.loads(chunk[6:]))

    task = asyncio.create_task(consume())
    assert await until(lambda: any(m.get("t") == "item" and m.get("kind") == "tool" for m in out))
    env.rt.release()
    await asyncio.wait_for(task, 20)
    kinds = {m.get("kind") for m in out if m["t"] == "item"}
    assert {"tool", "end"} <= kinds and out[-1]["t"] == "end"
    assert all("<script" not in m.get("html", "") for m in out)


# =========================================================================== bind / CLI / agent
def test_bind_loopback_only(hub_home, capsys):
    for ok in ("127.0.0.1", "::1", "localhost", "[::1]"):
        assert require_loopback(ok) == ok
    for bad in ("0.0.0.0", "192.168.1.5", "::", "example.com", ""):
        with pytest.raises(NotLoopback):
            require_loopback(bad)
    from cfharness.cli import main

    assert main(["web", "--host", "0.0.0.0"]) == 1
    assert "chỉ được bind loopback" in capsys.readouterr().err


def test_cli_web_url_mints_link_from_running_server_state(hub_home, capsys):
    from cfharness.cli import main

    assert main(["web", "--url"]) == 1  # chưa có server
    key = os.urandom(32)
    p = write_state(hub_home, host="127.0.0.1", port=8790, key=key)
    assert (p.stat().st_mode & 0o777) == 0o600 and (p.parent.stat().st_mode & 0o777) == 0o700
    assert read_state(hub_home)["port"] == 8790
    assert main(["web", "--url"]) == 0
    out = capsys.readouterr().out
    m = re.search(r"http://cfharness\.localhost:8790/login\?t=(\S+)", out)
    assert m and check_login_token(key, m.group(1))
    state_path(hub_home).unlink()


def test_agent_in_run_cannot_reach_web_secrets(hub_home):
    """Agent trong run: sandbox chặn đọc HUB_HOME/auth (khoá ký link web), tool Read bị deny, lệnh `cfharness web` bị cấm."""
    from cfharness.guard.classifier import GuardContext, classify
    from cfharness.guard.policy import PolicyStore
    from cfharness.guard.runtime import sandbox_settings, secret_read_deny_rules

    sb = sandbox_settings(hub_home.home, hub_home.socket, {})
    assert any(x.rstrip("/").endswith("/auth") for x in sb["filesystem"]["denyRead"])
    assert any(x.rstrip("/").endswith("/auth") for x in sb["filesystem"]["denyWrite"])
    rules = secret_read_deny_rules(hub_home.home, hub_home.socket)
    assert any("auth/**" in r and r.startswith("Read(") for r in rules)
    ps = PolicyStore(hub_home.home)
    ps.ensure()
    ctx = GuardContext(hub_home.home / "agents", ps.load(), run_since=time.time(), hub_socket=str(hub_home.socket), git_probe=False)
    for cmd in ("cfharness web --url", "cfharness web --open", "cfharness web"):
        assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd


async def test_web_process_is_human_but_socket_without_token_is_not(env, tmp_path):
    """Web gọi daemon với token người (đọc file như CLI). Không có token (vd agent không đọc được file) → HUMAN_ONLY bị chặn."""
    from cfharness.rpc import RpcClient, RpcError

    await env.login()
    r = await env.post("/do/estop", {"on": "1", "confirm": "yes"})
    assert env.hub.runs.estop["on"]
    # client không token (giả lập tiến trình không đọc được token) — chạy ở thread để không chặn loop daemon
    tok = env.paths.socket.with_name(env.paths.socket.name + ".auth")
    saved = tok.read_text()
    tok.unlink()
    try:
        def call():
            try:
                RpcClient(env.paths.socket, timeout=5).call("estop.set", {"on": False})
                return "ok"
            except RpcError as e:
                return e.message
        res = await asyncio.to_thread(call)
    finally:
        tok.write_text(saved)
        os.chmod(tok, 0o600)
    assert res != "ok" and "chỉ dành cho NGƯỜI" in res
    assert env.hub.runs.estop["on"]


# =========================================================================== định dạng thuần
def test_present_transcript_and_markdown_safety():
    html = P.md("**đậm** <script>alert(1)</script> [x](javascript:alert(1)) [ok](https://a.b)")
    assert "<strong>đậm</strong>" in html and "<script>" not in html and 'href="javascript' not in html and 'href="https://a.b"' in html
    evs = [
        {"type": "run.queued", "seq": 1, "payload": {"prompt_preview": "làm X", "kind": "adhoc"}},
        {"type": "tool.start", "seq": 2, "payload": {"name": "Write", "tool_id": "t1", "args_preview": "a.txt", "input": {"content": "dòng 1\ndòng 2"}}},
        {"type": "tool.complete", "seq": 3, "payload": {"name": "Write", "tool_id": "t1", "duration_s": 0.4, "result_preview": "ok"}},
        {"type": "message.complete", "seq": 4, "payload": {"text": "Xong rồi"}},
        {"type": "usage.tick", "seq": 5, "payload": {"total_tokens": 10}},
        {"type": "run.completed", "seq": 6, "source": "hub", "payload": {"outcome": "completed", "cost_usd": 0.01, "duration_ms": 2000, "num_turns": 2}},
    ]
    items = replay_items(evs)
    assert [i["kind"] for i in items] == ["prompt", "tool", "msg", "end"]
    tool = items[1]
    assert tool["running"] is False and tool["dur"] == 0.4 and tool["diff"] == [("add", "dòng 1"), ("add", "dòng 2")]
    assert "Xong" in items[3]["text"]
    assert P.agent_status("waiting_human")[1] == "Chờ bạn duyệt"
    assert P.wait_reason("agent_busy") == "agent đang bận"
    # UX v2: "Hoạt động" chỉ còn sự kiện có nghĩa, câu tiếng Việt do hub sinh — từng lời gọi tool/lời agent xem ở trang run
    assert P.activity_item({"type": "tool.start", "agent_id": "ag0", "ts": "2026-10-18T01:02:03Z", "payload": {"name": "Bash", "args_preview": "ls"}}) is None
    assert P.activity_item({"type": "message.complete", "agent_id": "ag0", "payload": {"text": "The directory is empty"}}) is None
    line = P.activity_item({"type": "run.completed", "source": "hub", "agent_id": "ag0", "ts": "2026-10-18T01:02:03Z", "run_id": "r1",
                            "payload": {"outcome": "completed", "cost_usd": 0.0123, "duration_ms": 4000}})
    assert line and line["agent"] == "ag0" and line["text"].startswith("xong · $0.0123") and line["tone"] == "ok" and line["run_id"] == "r1"
    q = P.activity_item({"type": "question.open", "agent_id": "ag0", "payload": {"text": "Xoá kiểu nào?", "routed_to": "manager"}})
    assert q and q["tone"] == "ask" and "hỏi điều phối viên" in q["text"]


def test_agent_form_parsing_sparse_for_template_full_for_edit():
    from starlette.datastructures import FormData

    f = FormData([("name", "A"), ("mcp", "x"), ("mcp", "y"), ("tools", ""), ("max_budget_usd_per_run", "0,5"), ("model", "")])
    ch = agent_changes(f, creating=True, from_template=True)
    assert ch == {"name": "A", "mcp_servers": ["x", "y"], "limits": {"max_budget_usd_per_run": 0.5}}
    ch = agent_changes(f, creating=False, from_template=False)
    assert ch["tool_policy"] == {"permission_mode": None, "tools": None} and ch["effort"] is None and ch["workspace_default"] is None
    assert ch["limits"]["budget_usd_per_day"] is None and "model" not in ch
    with pytest.raises(ValueError):
        agent_changes(FormData([("max_runtime_s", "abc")]), creating=True, from_template=True)


# =========================================================================== kiểm toán WT-1/2/6/7 + BUG waiting_human
async def test_wt1_stolen_cookie_and_csrf_cannot_write(held):
    """Tái hiện QA (evil_devserver): có cookie + CSRF đọc từ HTML + Origin giả đúng, KHÔNG có khoá phiên → 403, approval vẫn chờ."""
    env = held
    await env.login()
    run = await running_run(env)
    ap = open_approval(env.hub, run["id"])
    env.hub.approvals.to_waiting_human(ap["id"])
    html = (await env.client.get("/approvals")).text
    csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    ids = sorted(set(re.findall(r'name="id" value="(ap_[0-9a-f]+)"', html)))
    assert ids == [ap["id"]]
    # khoá phiên không có trong bất kỳ HTML nào (kể cả trang xác nhận mang trường form sang)
    assert env.key not in html
    r = await env.post("/do/stop", {"id": run["id"]})
    assert "Xác nhận" in r.text and env.key not in r.text
    evil = httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url=BASE, cookies={COOKIE: env.client.cookies.get(COOKIE)})
    for path, data in (("/do/approve", {"csrf": csrf, "id": ap["id"], "confirm": "yes", "confirm2": "yes"}),
                       ("/do/estop", {"csrf": csrf, "on": "1", "confirm": "yes"}),
                       ("/do/mcp_add", {"csrf": csrf, "name": "x", "command": "touch /tmp/pwn", "confirm": "yes"}),
                       ("/agents/ag0/save", {"csrf": csrf, "permission_mode": "acceptEdits", "confirm": "yes"}),
                       ("/do/approve", {"csrf": csrf, "id": ap["id"], "confirm": "yes", "k": "doan-bừa"})):
        r = await evil.post(path, data=data, headers={"Origin": BASE})
        assert r.status_code == 403, (path, r.status_code)
    await evil.aclose()
    assert env.hub.approvals.get(ap["id"])["status"] == "waiting_human"
    assert not env.hub.runs.estop.get("on") and not env.hub.mcp.list()
    # người thật (có khoá trong localStorage, gửi kèm form) vẫn duyệt được
    r = await env.post("/do/approve", {"id": ap["id"], "confirm": "yes"})
    assert env.hub.approvals.get(ap["id"])["status"] == "allowed"


async def test_wt1_short_session_polling_does_not_extend(env):
    import cfharness.web.security as S

    await env.login()
    sid = env.client.cookies.get(COOKIE)
    sess = env.auth.sessions[sid]
    assert S.SESSION_IDLE_S <= 3600 and S.SESSION_MAX_S <= 12 * 3600
    sess.last -= 100
    before = sess.last
    await env.client.get("/partials/overview")
    assert sess.last == before  # polling không gia hạn
    await env.client.get("/agents")
    assert sess.last > before  # thao tác của người gia hạn
    sess.last -= S.SESSION_IDLE_S + 1
    assert (await env.client.get("/")).status_code == 303  # hết hạn → đăng nhập lại
    assert sid not in env.auth.sessions


def test_wt7_safe_next_rejects_open_redirects():
    from cfharness.web.app import safe_next

    for bad in ("/\t/evil.com", "//evil.com", "/\\evil.com", "http://evil.com", "/\n/x", "/\x7f/x", "/ /x", "javascript:alert(1)", "/%0a"[:0] + "\x00"):
        assert safe_next(bad) == "/", repr(bad)
    for ok in ("/", "/runs/r_1", "/settings#secrets", "/tasks?team=docs&all=1"):
        assert safe_next(ok) == ok


async def test_wt2_full_action_shown_and_dangerous_part_marked(held):
    env = held
    await env.login()
    run = await running_run(env)
    pad = "echo '" + "an toan " * 60 + "' >/dev/null; "
    long_cmd = pad + "rm -rf ~/Documents‮"
    ap = env.hub.approvals.open(env.hub.store.get_run(run["id"]), tool_name="Bash", tool_input={"command": long_cmd}, tool_use_id="t1", fingerprint="fp-long",
                                verdict={"klass": "file", "rule": "rm_recursive", "verdict": "destructive", "reason": "xoá thư mục"}, channel="hook")
    mcp = env.hub.approvals.open(env.hub.store.get_run(run["id"]), tool_name="mcp__crm__xoa_khach_hang", tool_input={"bang": "khach_hang", "where": "id > 0"},
                                 tool_use_id="t2", fingerprint="fp-mcp", verdict={"klass": "mcp", "rule": "mcp_delete", "verdict": "destructive"}, channel="mcp")
    html = (await env.client.get("/approvals")).text
    assert "<mark>rm</mark> <mark>-rf</mark> ~/Documents" in html  # phần cuối nguy hiểm KHÔNG bị cắt, được tô
    i = html.find("~/Documents"); assert "⟦U+202E⟧" in html and "có ký tự ẩn" in html and "lệnh dài" in html, html[i - 200:i + 200]
    assert "&#34;where&#34;: &#34;id &gt; 0&#34;" in html or "&quot;where&quot;: &quot;id &gt; 0&quot;" in html  # MCP: đủ tham số
    assert "khach_hang" in html
    # hộp xác nhận mang toàn bộ lệnh
    assert html.count("rm -rf ~/Documents") >= 1
    assert ap["id"] in html and mcp["id"] in html


async def test_heavy_approval_needs_second_step(held):
    """Lớp file/db/… (như Telegram, + file) → sau hộp xác nhận còn bước 2 có toàn bộ lệnh + ô tích; lớp nhẹ → 1 bước."""
    env = held
    await env.login()
    run = await running_run(env)
    ap = env.hub.approvals.open(env.hub.store.get_run(run["id"]), tool_name="Bash", tool_input={"command": "rm -rf ~/projects"}, tool_use_id="t",
                                fingerprint="fp-h", verdict={"klass": "file", "rule": "rm_recursive_outside_run", "verdict": "destructive"}, channel="hook")
    r = await env.post("/do/approve", {"id": ap["id"], "confirm": "yes"})
    assert r.status_code == 200 and "Xác nhận lần 2" in r.text and 'name="confirm2" value="yes"' in r.text and "~/projects" in r.text
    assert env.key not in r.text
    assert env.hub.approvals.get(ap["id"])["status"] == "open"
    await env.post("/do/approve", {"id": ap["id"], "confirm": "yes", "confirm2": "yes"})
    assert env.hub.approvals.get(ap["id"])["status"] == "allowed"
    light = open_approval(env.hub, run["id"])  # fs_delete: không thuộc lớp nặng
    r = await env.post("/do/approve", {"id": light["id"], "confirm": "yes"})
    assert r.status_code == 303 and env.hub.approvals.get(light["id"])["status"] == "allowed"


async def test_wt6_sensitive_forms_need_confirmation(env):
    await env.login()
    env.hub.mcp.add({"name": "docs-mcp", "transport": "stdio", "command": "true"})
    r = await env.post("/do/mcp_add", {"name": "x", "transport": "stdio", "command": "touch /tmp/should-not-run", "next": "/mcp"})
    assert r.status_code == 200 and "CHẠY NGAY" in r.text and "touch /tmp/should-not-run" in r.text
    assert [m["name"] for m in env.hub.mcp.list()] == ["docs-mcp"]
    r = await env.post("/do/secret_set", {"name": "TELEGRAM_BOT_TOKEN", "value": "ghi-de"})
    assert r.status_code == 200 and "GHI ĐÈ" in r.text and "ghi-de" not in r.text  # giá trị không vào trang xác nhận
    assert "TELEGRAM_BOT_TOKEN" not in [x["name"] for x in env.hub.secrets.list()]
    # tạo agent → xác nhận (trang server) rồi mới tạo
    r = await env.post("/agents/create", {"id": "nc", "template": "researcher", "mcp": ["docs-mcp"]})
    assert r.status_code == 200 and "Tạo agent nc" in r.text and "docs-mcp" in r.text
    assert "nc" not in [a["id"] for a in env.hub.agents.list()]
    r = await env.post("/agents/create", {"id": "nc", "template": "researcher", "mcp": ["docs-mcp"], "confirm": "yes"})
    assert r.status_code == 303
    a = env.hub.agents.show("nc")
    base = {"name": "nc", "role": a["role"], "description": a["description"], "tools": ", ".join(a["tool_policy"]["tools"]), "mcp": ["docs-mcp"],
            "model": a["model"], "max_budget_usd_per_run": "0.5", "max_runtime_s": "1200", "max_concurrent_runs": "1", "soul": a["soul"]}
    # sửa không đụng quyền → lưu thẳng; đổi permission_mode → xác nhận; bypassPermissions → từ chối
    r = await env.post("/agents/nc/save", {**base, "permission_mode": "default"})
    assert r.status_code == 303
    r = await env.post("/agents/nc/save", {**base, "permission_mode": "acceptEdits"})
    assert r.status_code == 200 and "Đổi quyền của agent nc" in r.text and env.key not in r.text
    assert env.hub.agents.show("nc")["tool_policy"]["permission_mode"] == "default"
    r = await env.post("/agents/nc/save", {**base, "permission_mode": "acceptEdits", "confirm": "yes"})
    assert r.status_code == 303 and env.hub.agents.show("nc")["tool_policy"]["permission_mode"] == "acceptEdits"
    r = await env.post("/agents/nc/save", {**base, "permission_mode": "bypassPermissions", "confirm": "yes"})
    assert r.status_code == 400 and env.hub.agents.show("nc")["tool_policy"]["permission_mode"] == "acceptEdits"


async def test_waiting_human_event_carries_rule_and_page_has_no_dangling_colon(held):
    env = held
    await env.login()
    run = await running_run(env)
    ap = open_approval(env.hub, run["id"])
    env.hub.approvals.to_waiting_human(ap["id"])
    ev = [e for e in env.hub.store.query_events(run_id=run["id"]) if e.type == "approval.waiting_human"][-1]
    assert ev.payload["tool"] == "Bash" and ev.payload["matched_rule"] == "rm_recursive_outside_run" and ev.payload["reason"]
    html = (await env.client.get(f"/runs/{run['id']}")).text
    assert "luật ?" not in html and "</b> : <code>" not in html and "luật rm_recursive_outside_run" in html


async def test_sandbox_localhost_badge_and_toggle(env):
    """SEC §8: nhãn cảnh báo localhost (`lh-badge`) ở Tổng quan / Agent / chi tiết; bật/tắt qua `guard.sandbox.set` (HUMAN_ONLY, xác nhận, audit kênh web)."""
    await env.login()
    for a in ("ag0", "ag1"):
        env.hub.agents.create({"id": a, "name": a, "model": "haiku"})
    assert "lh-badge" not in (await env.client.get("/agents")).text
    r = await env.post("/do/sandbox_localhost", {"id": "ag0", "on": "1", "next": "/agents/ag0"})
    assert r.status_code == 200 and "MỞ CỔNG" in r.text and env.key not in r.text  # chưa xác nhận → chưa bật
    assert not env.hub.guard.sandbox_overrides("ag0").get("allow_localhost")
    r = await env.post("/do/sandbox_localhost", {"id": "ag0", "on": "1", "confirm": "yes", "next": "/agents/ag0"})
    assert r.status_code == 303
    ov = env.hub.guard.sandbox_overrides("ag0")
    assert ov["allow_localhost"] and ov["channel"] == "web" and ov["by"] == "user"
    for path in ("/", "/agents", "/agents/ag0"):
        html = (await env.client.get(path)).text
        assert html.count("lh-badge") >= 1 and "localhost</span>" in html, path
        assert "chỉ người tắt được" in html.lower(), path
    assert "lh-badge" not in (await env.client.get("/agents/ag1")).text
    rows = env.hub.store.conn.execute("SELECT channel, actor FROM audit_log WHERE kind='guard.sandbox_override'").fetchall()
    assert rows and rows[-1]["channel"] == "web"
    # không có khoá phiên → 403 (WT-1)
    r = await env.client.post("/do/sandbox_localhost", data={"csrf": env.csrf, "id": "ag0", "on": "0", "confirm": "yes"})
    assert r.status_code == 403 and env.hub.guard.sandbox_overrides("ag0")["allow_localhost"]
    await env.post("/do/sandbox_localhost", {"id": "ag0", "on": "0", "confirm": "yes"})
    assert not env.hub.guard.sandbox_overrides("ag0")["allow_localhost"]
    assert "lh-badge" not in (await env.client.get("/agents")).text
