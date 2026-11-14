"""Trang "Điều phối" (M7 MGR-UI) — daemon IN-PROCESS + FakeRuntime, app Starlette qua ASGITransport (như tests/test_web.py).

Daemon chưa có `manager.*` của MGR-CORE → cài stub cùng chữ ký (`cfharness.web.manager_stub`). Test cần điều khiển nội dung
trả lời (XSS) cài stub bắt buộc (`force=True`). Không gọi `claude`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re

import pytest

from cfharness.events import make_event
from cfharness.web import manager_stub
from cfharness.web import present as P

from test_web import ISOLATED, _start, _stop, audit_rows, until  # noqa: F401  (dùng lại hạ tầng test web)
from cfharness.runtime import FakeRuntime

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
    assert "cfharness manager set" in r.text
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


# =========================================================================== đính kèm (nút 📎 / kéo–thả / dán ảnh)
PNG_1PX = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6300010000050001"
                        "0d0a2db40000000049454e44ae426082")


async def post_files(e, files: list[tuple[str, bytes, str]], text: str = "", conv: str | None = None):
    data = {"csrf": e.csrf, "k": e.key, "text": text, **({"c": conv} if conv else {})}
    return await e.client.post("/manager/send", data=data, files=[("attach", f) for f in files], headers={"X-Ahub-Fetch": "1"})


async def test_send_with_image_and_text_file_attachments(menv):
    await menv.login()
    await designate(menv)
    seen: list[dict] = []
    orig = menv.d.methods["manager.chat"]

    def spy(p):  # ghi lại tham số web gửi daemon; chỉ chuyển tiếp phần chữ (test không phụ thuộc cách daemon lưu tệp)
        seen.append(dict(p))
        return orig({**{k: v for k, v in p.items() if k not in ("images", "files")}, "text": p.get("text") or "(xem tệp đính kèm)"})

    menv.d.methods["manager.chat"] = spy
    md = "# Kế hoạch\nviệc 1".encode()
    r = await post_files(menv, [("chup man hinh.png", PNG_1PX, "image/png"), ("ghi chú.md", md, "text/markdown")], text="xem giúp tôi")
    assert r.status_code == 200, r.text
    j = r.json()
    assert "chup man hinh.png" in j["html_user"] and "ghi chú.md" in j["html_user"]
    p = seen[-1]
    assert p["channel"] == "web" and p["text"] == "xem giúp tôi"
    assert [i["name"] for i in p["images"]] == ["chup man hinh.png"] and base64.b64decode(p["images"][0]["data_b64"]) == PNG_1PX
    assert [f["name"] for f in p["files"]] == ["ghi chú.md"] and base64.b64decode(p["files"][0]["data_b64"]) == md
    # chỉ ảnh, không chữ → vẫn gửi (daemon tự điền nội dung)
    r = await post_files(menv, [("a.png", PNG_1PX, "image/png")], conv=j["conversation_id"])
    assert r.status_code == 200, r.text
    assert seen[-1]["text"] == "" and len(seen[-1]["images"]) == 1 and "files" not in seen[-1]


async def test_office_document_attachment_forwarded(menv):
    from test_doc_extract import XLSX

    await menv.login()
    await designate(menv)
    seen: list[dict] = []
    orig = menv.d.methods["manager.chat"]

    def spy(p):
        seen.append(dict(p))
        return orig({k: v for k, v in p.items() if k != "files"})

    menv.d.methods["manager.chat"] = spy
    r = await post_files(menv, [("Rà soát.xlsx", XLSX, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                                ("bao-cao.pdf", b"%PDF-1.7\n\x00\xff binary", "application/pdf")], text="File này")
    assert r.status_code == 200, r.text
    assert "Rà soát.xlsx" in r.json()["html_user"]
    assert [f["name"] for f in seen[-1]["files"]] == ["Rà soát.xlsx", "bao-cao.pdf"]
    assert base64.b64decode(seen[-1]["files"][0]["data_b64"]) == XLSX


async def test_unsupported_or_oversized_attachment_rejected_before_daemon(menv):
    await menv.login()
    await designate(menv)
    r = await post_files(menv, [("cai-dat.exe", b"MZ\x90\x00\xff\xfe binary", "application/octet-stream")], text="đọc giúp")
    assert r.status_code == 400 and "chưa hỗ trợ" in r.json()["error"]
    # đuôi .xlsx nhưng nội dung không phải zip Office → từ chối (không tin đuôi)
    r = await post_files(menv, [("gia.xlsx", b"\x00\x01 not a zip", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")])
    assert r.status_code == 400 and "chưa hỗ trợ" in r.json()["error"]
    # đuôi .png nhưng nội dung không phải ảnh, cũng không phải UTF-8 → từ chối (phân loại theo nội dung, không theo tên)
    r = await post_files(menv, [("gia.png", b"\xff\x00\x01 nope", "image/png")])
    assert r.status_code == 400
    r = await post_files(menv, [(f"{i}.png", PNG_1PX, "image/png") for i in range(5)])
    assert r.status_code == 400 and "tối đa 4 ảnh" in r.json()["error"]
    assert not menv.hub.store.list_runs(agent_id="boss")  # không gửi gì khi có tệp hỏng


# =========================================================================== XSS — chữ điều phối viên = markdown đã cứng hoá (Q1), tin người = chữ thô
MD_EVIL = XSS + "\n\n**đậm** ![ảnh](http://x/a.png) [a](http://evil.example/p) [js](javascript:alert(4))\n\n```py\nprint('<b>')\n```"
ON_ATTR = re.compile(r"<[^>]+\son[a-z]+\s*=", re.I)


def _log_html(t: str) -> str:
    """Phần dòng thời gian của trang (#mgr-log … ô nhập) — chỗ chữ của agent xuất hiện."""
    a = t.index('id="mgr-log"')
    return t[a:t.index('class="composer-wrap"', a)]


async def test_manager_content_is_hardened_markdown_user_text_plain(stubbed):
    """Q1 (2026-11-13): chữ điều phối viên = markdown qua present.md() đã cứng hoá; tin người vẫn là chữ thô. XSS vẫn bị vô hiệu."""
    e = stubbed
    await e.login()
    await designate(e)
    j = await send(e, XSS + " **không đậm**")  # tin của người chỉ là dữ liệu — không markdown
    conv, run = j["conversation_id"], j["run_id"]
    assert "<script>" not in j["html_user"] and "&lt;script&gt;" in j["html_user"] and "<strong>" not in j["html_user"]
    assert await until(lambda: run not in e.stub.run_conv)
    e.stub.convs[conv]["messages"].append({"role": "manager", "text": MD_EVIL, "ts": "2099-01-01T00:00:00+00:00", "run_id": run})
    e.hub.publish(make_event("manager.action", "boss", {"tool": "agent_create", "target": "javascript:alert(3)", "summary": XSS}, run_id=run))
    e.hub.publish(make_event("manager.action", "boss", {"tool": "agent_update", "target": "agent:../../logout", "summary": "x"}, run_id=run))
    for path in (f"/manager?c={conv}", "/"):
        t = (await e.client.get(path)).text
        # thẻ <a> THÔ của agent không lọt qua (URL trần trong chữ đã escape được linkify thành link cứng hoá class="ext" — hợp lệ)
        assert "<script>alert" not in t and "<img src=x" not in t and '<a href="http://evil">' not in t
        assert not re.search(r'href="\s*javascript:', t, re.I)
    r = await e.client.get(f"/manager?c={conv}")
    t = r.text
    log = _log_html(t)
    assert "<img" not in log and not ON_ATTR.search(log) and "<script" not in log
    assert "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;" in t or "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in t
    # markdown của điều phối viên: đậm, khối code có header + nút chép, ảnh → link chữ, link ngoài có rel + tên miền, javascript: không là link
    mgr = log[log.index('class="mm mgr'):]
    assert '<div class="md">' in mgr and "<strong>đậm</strong>" in mgr and 'class="tx-code' in mgr and "tx-copy" in mgr
    assert re.search(r'<a [^>]*href="http://evil\.example/p"[^>]*rel="nofollow noopener noreferrer"', mgr) or \
        re.search(r'<a [^>]*rel="nofollow noopener noreferrer"[^>]*href="http://evil\.example/p"', mgr)
    assert 'class="ext-host"' in mgr and "evil.example" in mgr
    assert 'href="http://x/a.png"' in mgr  # ảnh chỉ còn là link chữ
    # tin người: chữ thô (không <strong>), văn bản gốc cho Sao chép/Sửa nằm trong <template class="mm-src"> và đã escape
    user = log[log.index('class="mm user"'):log.index('class="mm mgr')]
    assert "<strong>" not in user and "**không đậm**" in user
    srcs = re.findall(r'<template class="mm-src">(.*?)</template>', log, re.S)
    assert len(srcs) >= 2 and all("<script" not in s and "<img" not in s for s in srcs)
    assert any("&lt;script&gt;" in s for s in srcs) and any("![ảnh](http://x/a.png)" in s for s in srcs)  # Copy = markdown gốc
    assert 'data-mm="copy"' in log and 'data-mm="retry"' in log and 'data-mm="edit"' in log
    assert 'href="/agents/../../logout"' not in t and "/agents/..%2F" not in t  # id không an toàn → không có link
    assert 'aria-live' not in t[t.index('id="mgr-log"'):t.index('id="mgr-log"') + 400]  # log không còn aria-live (vùng role=status riêng)
    assert 'id="mgr-status-live" role="status"' in t and 'id="mgr-jump"' in t and 'id="mgr-send"' in t
    assert "script-src 'self'" in r.headers["content-security-policy"]


async def _sse_collect(e, events: list[dict], until_pred, conv_factory=None) -> list[dict]:
    from starlette.requests import Request

    from cfharness.web.app import _stream
    from cfharness.web.manager import _converter

    st = e.hub.status()
    got: list[dict] = []
    req = Request({"type": "http", "app": e.app, "path": "/", "headers": [], "query_string": b""})
    params = {"agent_id": "boss", "since_seq": st["last_seq"], "epoch": st["epoch"], "level": "detail"}

    async def reader() -> None:
        async for chunk in _stream(req, params, (conv_factory or _converter)(req)):
            if chunk.startswith("data: "):
                got.append(json.loads(chunk[6:]))
                if until_pred(got):
                    return

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.2)
    for ev in events:
        e.hub.publish(make_event(ev["type"], "boss", ev["payload"], run_id=ev.get("run_id", "r_x"), **({"subagent_id": ev["sub"]} if ev.get("sub") else {})))
    await asyncio.wait_for(task, 10)
    return got


async def test_sse_streams_reply_action_markdown_hardened_with_seq(stubbed):
    e = stubbed
    await e.login()
    await designate(e)
    got = await _sse_collect(e, [
        {"type": "message.delta", "payload": {"text": "Đang tạo…"}},
        {"type": "manager.action", "payload": {"tool": "team_create", "target": "team:docs", "summary": XSS}},
        {"type": "manager.reply", "payload": {"conversation_id": "conv_1", "run_id": "r_x", "text": MD_EVIL}},
    ], lambda g: any(m.get("t") == "reply" for m in g) and any(m.get("t") == "action" for m in g))
    kinds = [m["t"] for m in got]
    assert "delta" in kinds and "action" in kinds and "reply" in kinds
    d = next(m for m in got if m["t"] == "delta")
    assert d["run"] == "r_x" and d["text"] == "Đang tạo…"  # delta là chữ thuần — client gán bằng textContent
    for m in got:
        if m["t"] not in ("hello", "end"):
            assert isinstance(m.get("seq"), int), m  # P0-4: mọi message mang seq của event gốc (client bỏ trùng khi nối lại)
        for k in ("html", "side", "one"):
            if k in m:
                assert "<script" not in m[k] and "<img" not in m[k] and not ON_ATTR.search(m[k]), m[k]
                assert not re.search(r'href="\s*javascript:', m[k], re.I)
    act = next(m for m in got if m["t"] == "action")
    assert 'href="/teams#team-docs"' in act["html"] and "&lt;script&gt;" in act["html"]
    rep = next(m for m in got if m["t"] == "reply")
    assert rep["conv"] == "conv_1" and 'class="mm mgr"' in rep["html"] and "&lt;img src=x" in rep["html"]
    assert "<strong>đậm</strong>" in rep["html"] and 'rel="nofollow noopener noreferrer"' in rep["html"]
    # reply giữ cả lời dẫn đã chạy chữ ("Đang tạo…") — không xoá chữ đã stream
    assert rep["html"].index("Đang tạo…") < rep["html"].index("<strong>đậm</strong>")
    seqs = [m["seq"] for m in got if isinstance(m.get("seq"), int)]
    assert seqs == sorted(seqs)


async def test_sse_seg_think_tool_have_no_html_and_subagent_ignored(stubbed):
    e = stubbed
    await e.login()
    await designate(e)
    got = await _sse_collect(e, [
        {"type": "reasoning.delta", "payload": {"text": "<b>nghĩ</b>"}},
        {"type": "tool.start", "payload": {"name": "<script>alert(1)</script>", "tool_id": "t1", "input": {"x": "<img src=x onerror=alert(1)>"}}},
        {"type": "tool.start", "payload": {"name": "Read", "tool_id": "t9", "input": {"file_path": "/a/b.py"}}, "sub": "sa_1"},
        {"type": "tool.complete", "payload": {"name": "<script>alert(1)</script>", "tool_id": "t1"}},
        {"type": "message.delta", "payload": {"text": "phần một"}},
        {"type": "message.complete", "payload": {"text": "phần một"}},
        {"type": "message.complete", "payload": {"text": "của subagent"}, "sub": "sa_1"},
        {"type": "manager.reply", "payload": {"conversation_id": "conv_1", "run_id": "r_x", "text": "xong"}},
    ], lambda g: any(m.get("t") == "reply" for m in g))
    think = [m for m in got if m["t"] == "think"]
    tools = [m for m in got if m["t"] == "tool"]
    segs = [m for m in got if m["t"] == "seg"]
    assert think and think[0]["text"] == "<b>nghĩ</b>" and "html" not in think[0]
    assert len(tools) == 2 and all("html" not in m for m in tools)  # tool của subagent bị bỏ
    assert tools[0]["name"] == "<script>alert(1)</script>" and tools[0]["done"] is False and tools[1]["done"] is True  # chuỗi JSON thô
    assert len(segs) == 1 and segs[0]["run"] == "r_x"  # message.complete của subagent không sinh seg
    drafts = [m for m in got if m["t"] == "draft"]
    assert drafts and "phần một" in drafts[-1]["html"]
    rep = next(m for m in got if m["t"] == "reply")
    assert "phần một" in rep["html"] and "xong" in rep["html"]  # lời dẫn + câu trả lời cuối, cách đoạn


def test_draft_throttled_sanitized_and_capped():
    """P1-1: nháp markdown ≤ 1 lần/150 ms (đồng hồ giả), luôn gửi khi hết đoạn; HTML = present.md() (không <script>/<img>);
    quá DRAFT_MAX thì thôi gửi nháp (chỉ delta thô)."""
    from types import SimpleNamespace

    from cfharness.web import manager as M

    class Mod:
        def item(self, it):
            return P.md(it.get("text") or "")

    clock = [100.0]
    orig = M._mod
    M._mod = lambda request: Mod()
    try:
        conv = M._converter(SimpleNamespace(), clock=lambda: clock[0])
    finally:
        M._mod = orig

    def ev(t, text, seq, **kw):
        return {"type": t, "run_id": "r1", "seq": seq, "payload": {"text": text}, **kw}

    out = conv(ev("message.delta", "<script>x</script> ![i](http://h/a.png)", 1))
    assert [m["t"] for m in out] == ["delta", "draft"] and all(m["seq"] == 1 for m in out)
    assert "<script" not in out[1]["html"] and "<img" not in out[1]["html"] and "&lt;script&gt;" in out[1]["html"]
    clock[0] += 0.05
    assert [m["t"] for m in conv(ev("message.delta", " thêm", 2))] == ["delta"]  # chưa đủ 150 ms → chỉ delta
    clock[0] += 0.2
    assert [m["t"] for m in conv(ev("message.delta", " nữa", 3))] == ["delta", "draft"]
    clock[0] += 0.01
    out = conv(ev("message.complete", "", 4))
    assert [m["t"] for m in out] == ["seg", "draft"]  # hết đoạn → luôn gửi nháp
    big = "x" * (M.DRAFT_MAX + 10)
    clock[0] += 1
    assert [m["t"] for m in conv(ev("message.delta", big, 5))] == ["delta"]  # quá ngưỡng → không nháp
    assert conv(ev("message.delta", "sub", 6, subagent_id="sa")) == []


@pytest.fixture
async def mheld(hub_home):
    """Điều phối viên THẬT (MGR-CORE) + runtime giữ run ở trạng thái running → thử nút Dừng."""
    rt = FakeRuntime(ISOLATED, line_delay_s=0.01, hold=True)
    e = await _start(hub_home, rt)
    e.stub = manager_stub.install(e.d)
    e.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    e.hub.agents.create({"id": "other", "name": "other", "model": "haiku"})
    yield e
    rt.release()
    await _stop(e)


async def test_stop_button_route_guards_scope_and_audit(mheld):
    e = mheld
    await e.login()
    await designate(e)
    j = await send(e, "làm việc dài")
    conv, run = j["conversation_id"], j["run_id"]
    assert await until(lambda: (e.hub.store.get_run(run) or {}).get("status") == "running")
    page = (await e.client.get(f"/manager?c={conv}")).text
    assert f'data-pending="{run}"' in page and 'aria-busy="true"' in page and "i-stop" in page
    calls: list[dict] = []
    orig = e.d.methods["run.stop"]

    def spy(p):
        calls.append(dict(p))
        return orig(p)

    e.d.methods["run.stop"] = spy
    # chưa đăng nhập → 401; thiếu khoá phiên / CSRF sai → 403; không gọi run.stop
    c = e.client
    assert (await c.post("/manager/stop", data={"csrf": e.csrf, "run": run, "c": conv})).status_code == 403
    assert (await e.post("/manager/stop", {"run": run, "c": conv, "csrf": "sai"})).status_code == 403
    # run của agent khác → 403, không dừng
    other = e.hub.runs.submit("other", "việc khác")
    r = await e.post("/manager/stop", {"run": other["id"], "c": conv}, headers={"X-Ahub-Fetch": "1"})
    assert r.status_code == 403 and r.json()["error"]
    assert not calls
    # đúng lượt của điều phối viên → run.stop, audit kênh web, run bị huỷ
    r = await e.post("/manager/stop", {"run": run, "c": conv}, headers={"X-Ahub-Fetch": "1"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert calls and calls[-1]["id"] == run
    final = await e.hub.runs.wait(run, 20)
    assert final["status"] == "cancelled"
    rows = audit_rows(e, "run.stop")
    assert rows and rows[-1]["channel"] == "web" and rows[-1]["actor"] == "user"
    # form thuần (không JS) → 303 về hội thoại
    r = await e.post("/manager/stop", {"run": run, "c": conv})
    assert r.status_code == 303 and r.headers["location"].startswith(f"/manager?c={conv}")
    await c.post("/logout", data={"csrf": e.csrf})
    assert (await c.post("/manager/stop", data={"run": run})).status_code in (401, 403)


def test_manager_templates_have_no_handlers_and_template_scan_covers_them():
    from pathlib import Path

    tdir = Path(P.__file__).parent / "templates"
    for name in ("_manager.html", "manager.html"):
        s = (tdir / name).read_text(encoding="utf-8")
        assert not re.search(r"\son[a-z]+\s*=", s, re.I) and not re.search(r"\sstyle\s*=", s, re.I), name
    js = (tdir.parent / "static" / "app.js").read_text(encoding="utf-8")
    mgr = js[js.index("function initManager"):]
    assert "scroll-behavior" not in (tdir.parent / "static" / "app.css").read_text(encoding="utf-8").split(".mgr-log {", 1)[1].split("}", 1)[0]
    assert ".innerHTML = m.html" in mgr and "aria-live" not in mgr  # nháp = HTML server; còn lại textContent


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
    s = P.mgr_message({"role": "schedule", "text": "⏰ Lịch 'Sáng' (hằng ngày 08:00) chạy.", "run_id": "r2"})
    assert s["kind"] == "note" and s["schedule"] and s["run_id"] == "r2"


# =========================================================================== #7: chạy nhịp ngay, tin lịch (lưu trữ: conv_archive + test_manager_archive.py)
async def _replied(e, conv: str) -> None:
    for _ in range(200):
        h = await mgr_rpc(e, "manager.history", {"conversation_id": conv, "limit": 50})
        if any(m["role"] == "manager" for m in h):
            runs = [e.hub.store.get_run(m["run_id"]) for m in h if m.get("run_id")]
            if all(r and r["status"] not in ("queued", "running") for r in runs):
                return
        await asyncio.sleep(0.05)
    raise AssertionError("điều phối viên chưa trả lời")


async def test_tick_now_from_web(menv):
    await menv.login()
    await designate(menv)
    r = await menv.client.get("/manager")
    assert "/do/manager_tick" in r.text and "Chạy nhịp ngay" in r.text
    r = await menv.post("/do/manager_tick", {"next": "/manager"})
    assert r.status_code == 200 and "Chạy một nhịp" in r.text  # xác nhận trước (tốn tiền)
    r = await menv.post("/do/manager_tick", {"next": "/manager", "confirm": "yes"})
    assert r.status_code == 303 and r.headers["location"].startswith("/manager?c=mc_")
    assert audit_rows(menv, "manager.tick")[-1]["channel"] == "web"


async def test_schedule_message_shown_in_conversation(menv):
    await menv.login()
    await designate(menv)
    conv = (await send(menv, "xin chào"))["conversation_id"]
    await _replied(menv, conv)
    menv.hub.manager._add_msg(conv, "schedule", "⏰ Lịch 'Nhắc <b>uống nước</b>' (mỗi 15 phút) chạy.", None)
    r = await menv.client.get(f"/manager?c={conv}")
    assert 'class="mm-note info"' in r.text and "Nhắc &lt;b&gt;uống nước&lt;/b&gt;" in r.text


async def test_manager_reply_mentioned_files_get_view_open_chips(stubbed):
    """Đường dẫn file tuyệt đối trong chữ điều phối viên → chip Xem/Mở/Thư mục theo agent điều phối viên (file-open.md §6).
    Link trần → link thật. Tin của người: không gắn chip."""
    e = stubbed
    await e.login()
    await designate(e)
    j = await send(e, r"file C:\Users\x\note.md đâu?")
    conv, run = j["conversation_id"], j["run_id"]
    assert await until(lambda: run not in e.stub.run_conv)
    text = (r"Ở đây: `C:\Users\x\.cfharness\agents\boss\workspaces\conv-1\notes.md`. Thư mục `C:\Users\x\ws` "
            "và https://jira.example/browse/LH-1")
    e.stub.convs[conv]["messages"].append({"role": "manager", "text": text, "ts": "2099-01-01T00:00:00+00:00", "run_id": run})
    log = _log_html((await e.client.get(f"/manager?c={conv}")).text)
    mgr = log[log.index('class="mm mgr'):]
    user = log[log.index('class="mm user"'):log.index('class="mm mgr')]
    assert 'class="mm-files"' in mgr and mgr.count('class="fchip"') == 1  # thư mục không có chip
    assert "/files/view?agent=boss&amp;path=C%3A%5CUsers%5Cx%5C.cfharness%5Cagents%5Cboss%5Cworkspaces%5Cconv-1%5Cnotes.md" in mgr
    assert 'data-fopen="open"' in mgr and 'data-fopen="reveal"' in mgr
    assert re.search(r'<a href="https://jira\.example/browse/LH-1"[^>]*rel="nofollow noopener noreferrer"', mgr)
    assert "fchip" not in user
