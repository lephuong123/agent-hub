"""Trang "Điều phối" (M7 MGR-UI) — daemon IN-PROCESS + FakeRuntime, app Starlette qua ASGITransport (như tests/test_web.py).

Daemon chưa có `manager.*` của MGR-CORE → cài stub cùng chữ ký (`agent_hub.web.manager_stub`). Test cần điều khiển nội dung
trả lời (XSS) cài stub bắt buộc (`force=True`). Không gọi `claude`.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from agent_hub.events import make_event
from agent_hub.web import manager_stub
from agent_hub.web import present as P

from test_web import ISOLATED, _start, _stop, audit_rows, until  # noqa: F401  (dùng lại hạ tầng test web)
from agent_hub.runtime import FakeRuntime

XSS = '<script>alert("x")</script><img src=x onerror=alert(1)>[bấm](javascript:alert(2)) <a href="http://evil">duyệt</a>'


@pytest.fixture
async def menv(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    e.stub = manager_stub.install(e.d)  # None nếu daemon đã có manager.* thật
    e.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})  # M7 MGR-8: manager chỉ đọc
    e.hub.agents.create({"id": "researcher", "name": "researcher", "model": "haiku"})
    yield e
    await _stop(e)


@pytest.fixture
async def stubbed(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    e.stub = manager_stub.install(e.d, force=True)
    e.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})  # M7 MGR-8: manager chỉ đọc
    yield e
    await _stop(e)


async def designate(e, agent: str = "boss") -> None:
    r = await e.post("/do/manager_set", {"agent_id": agent, "confirm": "yes", "next": "/manager"})
    assert r.status_code == 303, r.text
    resp = await e.d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "manager.get", "params": {}})
    assert resp["result"]["agent_id"] == agent


async def mgr_rpc(e, method: str, params: dict | None = None):
    resp = await e.d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    assert "error" not in resp, resp
    return resp["result"]


async def send(e, text: str, conv: str | None = None) -> dict:
    data = {"text": text, **({"c": conv} if conv else {})}
    r = await e.post("/manager/send", data, headers={"X-Ahub-Fetch": "1"})
    assert r.status_code == 200, r.text
    return r.json()


# =========================================================================== menu + chưa có điều phối viên
async def test_menu_first_and_guidance_when_no_manager(menv):
    await menv.login()
    r = await menv.client.get("/manager")
    assert r.status_code == 200
    nav = re.search(r'<nav class="nav".*?</nav>', r.text, re.S).group(0)
    assert nav.index('href="/manager"') < nav.index('href="/"')  # "Điều phối" đứng đầu menu
    assert "Chưa có điều phối viên" in r.text and 'action="/do/manager_set"' in r.text
    assert "agent-hub manager set" in r.text
    assert 'data-confirm=' in r.text.split('action="/do/manager_set"', 1)[1][:400]  # có hộp xác nhận
    # Tổng quan: khung nhỏ hướng dẫn chỉ định
    r = await menv.client.get("/")
    assert "Điều phối viên" in r.text and "Chỉ định ở trang Điều phối" in r.text


async def test_designate_needs_confirmation_session_key_and_is_human_only(menv):
    await menv.login()
    # không xác nhận → trang xác nhận phía server (nêu rõ quyền), chưa đổi gì
    r = await menv.post("/do/manager_set", {"agent_id": "boss", "next": "/manager"})
    assert r.status_code == 200 and "ĐIỀU PHỐI VIÊN" in r.text and "duyệt thao tác phá huỷ" in r.text
    assert (await mgr_rpc(menv, "manager.get"))["agent_id"] is None
    # cookie + CSRF nhưng thiếu khoá phiên ngoài cookie (WT-1) → 403
    r = await menv.client.post("/do/manager_set", data={"csrf": menv.csrf, "agent_id": "boss", "confirm": "yes"})
    assert r.status_code == 403
    assert (await mgr_rpc(menv, "manager.get"))["agent_id"] is None
    await designate(menv)
    r = await menv.client.get("/manager")
    assert "Trò chuyện với điều phối viên" in r.text and 'action="/do/manager_unset"' in r.text
    if menv.stub is None:  # MGR-CORE thật: manager.set HUMAN_ONLY + audit kênh web
        rows = audit_rows(menv, "manager.set")
        assert rows and rows[-1]["channel"] == "web" and rows[-1]["actor"] == "user"
    # huỷ chỉ định cũng cần xác nhận
    r = await menv.post("/do/manager_unset", {"current": "boss", "next": "/manager"})
    assert r.status_code == 200 and "Huỷ chỉ định" in r.text
    r = await menv.post("/do/manager_unset", {"current": "boss", "confirm": "yes", "next": "/manager"})
    assert r.status_code == 303 and (await mgr_rpc(menv, "manager.get"))["agent_id"] is None


# =========================================================================== chat
async def test_chat_send_reply_history_actions_and_links(menv):
    await menv.login()
    await designate(menv)
    j = await send(menv, "Tạo agent researcher để tóm tắt README rồi giao việc đó")
    conv, run = j["conversation_id"], j["run_id"]
    assert conv and run
    assert "Tạo agent researcher" in j["html_user"] and f'data-pending="{run}"' in j["html_pending"]
    # thẻ hành động của run này (MGR-CORE phát `manager.action` cho mọi thao tác ghi)
    menv.hub.publish(make_event("manager.action", "boss", {"tool": "agent_create", "target": "agent:researcher", "summary": "Đã tạo agent researcher",
                                                         "audit_id": 7}, run_id=run))
    menv.hub.publish(make_event("manager.action", "boss", {"tool": "task_create", "target": {"kind": "task", "id": "t_abc"},
                                                         "summary": "Đã giao task t_abc cho team docs"}, run_id=run))

    async def replied() -> bool:
        h = await mgr_rpc(menv, "manager.history", {"conversation_id": conv, "limit": 50})
        return any(P.mgr_message(m) and P.mgr_message(m)["kind"] == "manager" for m in h)

    for _ in range(200):
        if await replied():
            break
        await asyncio.sleep(0.05)
    assert await replied()
    r = await menv.client.get(f"/manager?c={conv}")
    assert r.status_code == 200
    t = r.text
    assert "Tạo agent researcher để tóm tắt README" in t
    assert 'class="mm mgr"' in t  # trả lời của điều phối viên
    assert "Đã tạo agent researcher" in t and 'href="/agents/researcher"' in t
    assert "Đã giao task t_abc cho team docs" in t and 'href="/tasks/t_abc"' in t
    assert t.index("Tạo agent researcher để") < t.index("Đã tạo agent researcher")  # theo thứ tự thời gian
    assert f'href="/manager?c={conv}"' in t  # danh sách cuộc trò chuyện web
    assert "Nhịp gần nhất" in t and "Nhịp kế tiếp" in t
    # tin tiếp theo giữ conversation; "Cuộc trò chuyện mới" mở conversation khác
    j2 = await send(menv, "Còn gì đang kẹt không?", conv)
    assert j2["conversation_id"] == conv
    r = await menv.client.get("/manager?new=1")
    assert 'name="c" value=""' in r.text and "Cuộc trò chuyện mới" in r.text
    j3 = await send(menv, "Việc khác")
    assert j3["conversation_id"] != conv
    # form thuần (không JS fetch) → 303 về trang cuộc trò chuyện
    r = await menv.post("/manager/send", {"text": "form thuần", "c": conv})
    assert r.status_code == 303 and r.headers["location"].startswith(f"/manager?c={conv}&run=")


async def test_send_guards_and_errors(menv):
    c = menv.client
    assert (await c.post("/manager/send", data={"text": "x"})).status_code == 401
    await menv.login()
    r = await c.post("/manager/send", data={"csrf": menv.csrf, "text": "x"})  # thiếu khoá phiên
    assert r.status_code == 403
    r = await menv.post("/manager/send", {"text": "x", "csrf": "sai"})
    assert r.status_code == 403
    # chưa có điều phối viên → lỗi rõ ràng (JSON), không tạo run
    r = await menv.post("/manager/send", {"text": "xin chào"}, headers={"X-Ahub-Fetch": "1"})
    assert r.status_code == 400 and r.json()["error"]
    assert not menv.hub.store.list_runs(agent_id="boss")
    r = await menv.post("/manager/send", {"text": "   "}, headers={"X-Ahub-Fetch": "1"})
    assert r.status_code == 400
    assert (await c.get("/sse/manager")).status_code == 404  # chưa có điều phối viên


# =========================================================================== XSS — nội dung điều phối viên là dữ liệu
async def test_manager_content_is_escaped_never_raw_html(stubbed):
    e = stubbed
    await e.login()
    await designate(e)
    j = await send(e, XSS)  # cả tin của người cũng chỉ là dữ liệu
    conv, run = j["conversation_id"], j["run_id"]
    assert "<script>" not in j["html_user"] and "&lt;script&gt;" in j["html_user"]
    assert await until(lambda: run not in e.stub.run_conv)
    e.stub.convs[conv]["messages"].append({"role": "manager", "text": XSS, "ts": "2099-01-01T00:00:00+00:00", "run_id": run})
    e.hub.publish(make_event("manager.action", "boss", {"tool": "agent_create", "target": "javascript:alert(3)", "summary": XSS}, run_id=run))
    e.hub.publish(make_event("manager.action", "boss", {"tool": "agent_update", "target": "agent:../../logout", "summary": "x"}, run_id=run))
    for path in (f"/manager?c={conv}", "/"):
        t = (await e.client.get(path)).text
        assert "<script>alert" not in t and "<img src=x" not in t and 'href="http://evil"' not in t
        assert not re.search(r'href="\s*javascript:', t, re.I)
    t = (await e.client.get(f"/manager?c={conv}")).text
    assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in t or "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in t
    assert 'href="/agents/../../logout"' not in t and "/agents/..%2F" not in t  # id không an toàn → không có link
    # CSP vẫn chặn script inline trên trang Điều phối
    r = await e.client.get(f"/manager?c={conv}")
    assert "script-src 'self'" in r.headers["content-security-policy"]


async def test_sse_streams_reply_action_and_escapes(stubbed):
    e = stubbed
    await e.login()
    await designate(e)
    st = e.hub.status()
    got: list[dict] = []
    from starlette.requests import Request

    from agent_hub.web.app import _stream
    from agent_hub.web.manager import _converter

    req = Request({"type": "http", "app": e.app, "path": "/", "headers": [], "query_string": b""})
    params = {"agent_id": "boss", "since_seq": st["last_seq"], "epoch": st["epoch"], "level": "detail"}

    async def reader() -> None:
        async for chunk in _stream(req, params, _converter(req)):
            if chunk.startswith("data: "):
                got.append(json.loads(chunk[6:]))
                if any(m.get("t") == "reply" for m in got) and any(m.get("t") == "action" for m in got):
                    return

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.2)
    e.hub.publish(make_event("message.delta", "boss", {"text": "Đang tạo…"}, run_id="r_x"))
    e.hub.publish(make_event("manager.action", "boss", {"tool": "team_create", "target": "team:docs", "summary": XSS}, run_id="r_x"))
    e.hub.publish(make_event("manager.reply", "boss", {"conversation_id": "conv_1", "run_id": "r_x", "text": XSS}, run_id="r_x"))
    await asyncio.wait_for(task, 10)
    kinds = [m["t"] for m in got]
    assert "delta" in kinds and "action" in kinds and "reply" in kinds
    d = next(m for m in got if m["t"] == "delta")
    assert d["run"] == "r_x" and d["text"] == "Đang tạo…"  # delta là chữ thuần — client gán bằng textContent
    for m in got:
        for k in ("html", "side"):
            if k in m:
                assert "<script>" not in m[k] and "<img src=x" not in m[k], m[k]
    act = next(m for m in got if m["t"] == "action")
    assert 'href="/teams#team-docs"' in act["html"] and "&lt;script&gt;" in act["html"]
    rep = next(m for m in got if m["t"] == "reply")
    assert rep["conv"] == "conv_1" and 'class="mm mgr"' in rep["html"] and "&lt;img src=x" in rep["html"]


# =========================================================================== Tổng quan
async def test_overview_card_status_and_quick_send(menv):
    await menv.login()
    await designate(menv)
    j = await send(menv, "chào")
    r = await menv.client.get("/")
    t = r.text
    assert "Điều phối viên" in t and 'action="/manager/send"' in t and 'id="mgr-quick"' in t
    assert f'name="c" value="{j["conversation_id"]}"' in t  # gửi nhanh tiếp tục cuộc trò chuyện web gần nhất
    assert "Nhịp gần nhất" in t
    r = await menv.client.get("/partials/manager?compact=1")
    assert r.status_code == 200 and "Nhịp kế tiếp" in r.text
    r = await menv.post("/manager/send", {"text": "gửi nhanh từ tổng quan", "c": j["conversation_id"]})
    assert r.status_code == 303 and r.headers["location"].startswith("/manager?c=")


async def test_page_explains_when_daemon_has_no_manager_rpc(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        if "manager.get" in e.d.methods:
            pytest.skip("daemon đã có manager.* thật (MGR-CORE)")
        await e.login()
        r = await e.client.get("/manager")
        assert r.status_code == 200 and "chưa có chức năng điều phối viên" in r.text
        assert (await e.client.get("/")).status_code == 200
    finally:
        await _stop(e)


# =========================================================================== thuần
def test_mgr_target_links_only_internal_and_safe():
    assert P.mgr_target("agent:researcher")["href"] == "/agents/researcher"
    assert P.mgr_target({"kind": "task", "id": "t_1"})["href"] == "/tasks/t_1"
    assert P.mgr_target("r_9", "mcp__hub-self__run_start")["href"] == "/runs/r_9"
    assert P.mgr_target("docs", "team_create")["href"] == "/teams#team-docs"
    assert P.mgr_target("researcher", "agent_create")["label"] == "agent researcher"
    for bad in ("javascript:alert(1)", "agent:../x", "agent:a b", "agent:" + "x" * 200, "http://evil/x", {"kind": "agent", "id": "//evil"}):
        assert P.mgr_target(bad)["href"] is None, bad
    assert P.mgr_target(None)["href"] is None
    m = P.mgr_message({"role": "assistant", "content": "hi", "run_id": "r1"})
    assert m["kind"] == "manager" and m["text"] == "hi"
    assert P.mgr_message({"role": "system", "text": "x"}) is None
