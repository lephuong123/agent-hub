"""Giao diện web (`agent-hub web`) — daemon IN-PROCESS trên Unix socket thật + FakeRuntime, app Starlette qua ASGITransport.

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

from agent_hub.auth import load_token
from agent_hub.daemon import Daemon
from agent_hub.runtime import FakeRuntime
from agent_hub.web import present as P
from agent_hub.web.app import agent_changes, create_app, replay_items
from agent_hub.web.security import (
    COOKIE, NotLoopback, WebAuth, check_login_token, login_url, mint_login_token, read_state, require_loopback, state_path, write_state,
)

ISOLATED = Path(__file__).parent / "fixtures" / "claude-hub-isolated-run.jsonl"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"


class Env:
    def __init__(self, d: Daemon, task: asyncio.Task, rt: FakeRuntime, paths) -> None:
        self.d, self.task, self.rt, self.paths = d, task, rt, paths
        self.hub = d.hub
        self.auth = WebAuth()
        self.app = create_app(paths, auth=self.auth, port=PORT)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE)
        self.csrf = ""

    async def login(self) -> str:
        r = await self.client.get("/login?t=" + self.auth.mint())
        assert r.status_code == 303, r.text
        sid = r.cookies.get(COOKIE) or self.client.cookies.get(COOKIE)
        self.csrf = self.auth.sessions[sid].csrf
        return self.csrf

    async def post(self, path: str, data: dict, **kw):
        return await self.client.post(path, data={"csrf": self.csrf, **data}, **kw)

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
    assert r.status_code == 200 and "agent-hub web --open" in r.text
    # cookie giả không có tác dụng
    c.cookies.set(COOKIE, "khong-hop-le")
    assert (await c.get("/")).status_code == 303


async def test_one_time_login_link_sets_strict_httponly_cookie(env):
    tok = env.auth.mint()
    r = await env.client.get("/login?t=" + tok)
    assert r.status_code == 303 and r.headers["location"] == "/"
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
    r = await c2.post("/login", data={"link": login_url("127.0.0.1", PORT, env.auth.mint())})
    assert r.status_code == 303
    await c2.aclose()
    # đăng xuất: phiên bị huỷ
    await env.login()
    sid = env.client.cookies.get(COOKIE)
    r = await env.post("/logout", {})
    assert r.status_code == 303 and sid not in env.auth.sessions


async def test_host_header_must_be_loopback(env):
    await env.login()
    r = await env.client.get("/", headers={"Host": "evil.example:8765"})
    assert r.status_code == 421
    r = await env.client.get("/", headers={"Host": f"localhost:{PORT}"})
    assert r.status_code == 200


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
    assert h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer" and h["x-content-type-options"] == "nosniff"
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
    r = await env.post("/agents/create", {"id": "nc", "template": "researcher", "name": "Nghiên cứu", "mcp": ["docs-mcp"], "max_budget_usd_per_run": "0.3"})
    assert r.status_code == 303 and r.headers["location"] == "/agents/nc", r.text[:400]
    a = env.hub.agents.show("nc")
    assert a["mcp_servers"] == ["docs-mcp"] and a["name"] == "Nghiên cứu"
    assert a["tool_policy"]["tools"] == ["Read", "Grep", "Glob", "WebSearch", "WebFetch"]  # ô trống = theo template
    assert a["limits"]["max_budget_usd_per_run"] == 0.3
    # lỗi nhập → form hiện lại kèm lỗi, không tạo
    r = await env.post("/agents/create", {"id": "Sai Id", "template": "researcher"})
    assert r.status_code == 400 and "id" in r.text
    # sửa: bỏ docs-mcp, thêm other
    r = await env.post("/agents/nc/save", {"name": "Nghiên cứu 2", "mcp": ["other"], "model": "haiku", "role": "r", "description": "", "soul": a["soul"]})
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
    assert f"agent-hub takeover {rid}" in r.text
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
    await env.post("/do/secret_set", {"name": "MY_TOKEN", "value": "sieu-bi-mat-123"})
    r = await env.client.get("/settings")
    assert "MY_TOKEN" in r.text and "sieu-bi-mat-123" not in r.text
    # xoá mềm + khôi phục + revert
    await env.post("/do/agent_delete", {"id": "ag0", "confirm": "yes"})
    assert "ag0" not in [a["id"] for a in env.hub.agents.list()]
    r = await env.client.get("/agents")
    assert "Thùng rác" in r.text
    await env.post("/do/agent_restore", {"id": "ag0"})
    assert "ag0" in [a["id"] for a in env.hub.agents.list()]
    await env.post("/do/agent_pause", {"id": "ag0", "confirm": "yes"})
    assert env.hub.agents.show("ag0")["status"] == "paused"
    await env.post("/do/agent_resume", {"id": "ag0"})
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
    await env.post("/do/team_member", {"team": "docs", "agent": "w1", "role": "worker"})
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

    from agent_hub.web.app import _run_converter, _stream

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
    from agent_hub.cli import main

    assert main(["web", "--host", "0.0.0.0"]) == 1
    assert "chỉ được bind loopback" in capsys.readouterr().err


def test_cli_web_url_mints_link_from_running_server_state(hub_home, capsys):
    from agent_hub.cli import main

    assert main(["web", "--url"]) == 1  # chưa có server
    key = os.urandom(32)
    p = write_state(hub_home, host="127.0.0.1", port=8790, key=key)
    assert (p.stat().st_mode & 0o777) == 0o600 and (p.parent.stat().st_mode & 0o777) == 0o700
    assert read_state(hub_home)["port"] == 8790
    assert main(["web", "--url"]) == 0
    out = capsys.readouterr().out
    m = re.search(r"http://127\.0\.0\.1:8790/login\?t=(\S+)", out)
    assert m and check_login_token(key, m.group(1))
    state_path(hub_home).unlink()


def test_agent_in_run_cannot_reach_web_secrets(hub_home):
    """Agent trong run: sandbox chặn đọc HUB_HOME/auth (khoá ký link web), tool Read bị deny, lệnh `agent-hub web` bị cấm."""
    from agent_hub.guard.classifier import GuardContext, classify
    from agent_hub.guard.policy import PolicyStore
    from agent_hub.guard.runtime import sandbox_settings, secret_read_deny_rules

    sb = sandbox_settings(hub_home.home, hub_home.socket, {})
    assert any(x.rstrip("/").endswith("/auth") for x in sb["filesystem"]["denyRead"])
    assert any(x.rstrip("/").endswith("/auth") for x in sb["filesystem"]["denyWrite"])
    rules = secret_read_deny_rules(hub_home.home, hub_home.socket)
    assert any("auth/**" in r and r.startswith("Read(") for r in rules)
    ps = PolicyStore(hub_home.home)
    ps.ensure()
    ctx = GuardContext(hub_home.home / "agents", ps.load(), run_since=time.time(), hub_socket=str(hub_home.socket), git_probe=False)
    for cmd in ("agent-hub web --url", "agent-hub web --open", "agent-hub web"):
        assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd


async def test_web_process_is_human_but_socket_without_token_is_not(env, tmp_path):
    """Web gọi daemon với token người (đọc file như CLI). Không có token (vd agent không đọc được file) → HUMAN_ONLY bị chặn."""
    from agent_hub.rpc import RpcClient, RpcError

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
    line = P.activity_item({"type": "tool.start", "agent_id": "ag0", "ts": "2026-10-18T01:02:03Z", "payload": {"name": "Bash", "args_preview": "ls"}})
    assert line and line["agent"] == "ag0" and "Bash" in line["text"]


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
