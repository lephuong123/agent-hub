"""GĐ0 trang Điều phối (sửa nhanh hiệu năng — plan option B):

- khung "đang trả lời" đọc event GẦN NHẤT của run (run dài > PENDING_SCAN event không ra tool cũ);
- mỗi lần tải trang chỉ MỘT `manager.conversations` (kể cả `?w=`), GET không ghi "đã đọc" — `POST /manager/read` (CSRF + khoá phiên);
- đổi tab bằng app.js (`X-Ahub-Partial: grid`) → vỏ tối giản, không thanh bên/menu;
- nháp markdown đang chạy chữ không vào LRU `present.md`, nhịp nháp ≥ 0,5 s;
- tĩnh có `?v=` → Cache-Control immutable; gzip cho HTML/JS (không cho SSE).
Không gọi `claude`."""

from __future__ import annotations

import re

import pytest

from cfharness.events import make_event
from cfharness.runtime import FakeRuntime
from cfharness.web import manager_stub
from cfharness.web import present as P

from test_web import ISOLATED, _start, _stop, until  # noqa: F401
from test_web_manager import designate, mgr_rpc, send


@pytest.fixture
async def menv(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    e.stub = manager_stub.install(e.d)
    e.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    yield e
    await _stop(e)


@pytest.fixture
async def mheld(hub_home):
    rt = FakeRuntime(ISOLATED, line_delay_s=0.01, hold=True)
    e = await _start(hub_home, rt)
    e.stub = manager_stub.install(e.d)
    e.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    yield e
    rt.release()
    await _stop(e)


def _spy(e, method: str) -> list[dict]:
    calls: list[dict] = []
    orig = e.d.methods[method]

    def spy(p):
        calls.append(dict(p))
        return orig(p)

    e.d.methods[method] = spy
    return calls


# =========================================================================== khung "đang trả lời": event gần nhất
async def test_pending_frame_reads_newest_events_of_long_run(mheld, monkeypatch):
    from cfharness.web import manager as M

    e = mheld
    await e.login()
    await designate(e)
    j = await send(e, "làm việc dài")
    conv, run = j["conversation_id"], j["run_id"]
    assert await until(lambda: (e.hub.store.get_run(run) or {}).get("status") == "running")
    # run "dài": tool cũ (đã xong từ lâu) rồi nhiều event, cuối cùng là tool đang dùng — chỉ đọc 4 event gần nhất
    e.hub.publish(make_event("tool.start", "boss", {"name": "Glob", "tool_id": "old", "args_preview": "cu-xa-lam"}, run_id=run))
    for i in range(10):
        e.hub.publish(make_event("learning.applied", "boss", {"i": i}, run_id=run))
    e.hub.publish(make_event("tool.start", "boss", {"name": "Grep", "tool_id": "new", "args_preview": "moi-nhat"}, run_id=run))
    calls = _spy(e, "events.query")
    monkeypatch.setattr(M, "PENDING_SCAN", 4)
    page = (await e.client.get(f"/manager?c={conv}")).text
    frame = re.search(rf'data-pending="{run}".*', page, re.S).group(0)
    assert "moi-nhat" in frame and "cu-xa-lam" not in frame, frame[:2000]
    assert any(c.get("run_id") == run and c.get("order") == "desc" and c.get("limit") == 4 for c in calls), calls


# =========================================================================== RPC trùng + "đã đọc" không ghi trong GET
async def test_one_conversations_rpc_and_get_never_marks_read(menv):
    e = menv
    await e.login()
    await designate(e)
    w = await mgr_rpc(e, "workstream.create", {"title": "Dreem", "key": "LH-1"})
    j = await send(e, "chào")
    cid = j["conversation_id"]
    await until(lambda: (e.hub.store.get_run(j["run_id"]) or {}).get("status") not in ("queued", "running"))
    convs = _spy(e, "manager.conversations")
    reads = _spy(e, "manager.conversation_read")
    for url in ("/manager", f"/manager?c={cid}", f"/manager?w={w['id']}", "/partials/manager/tabs?c=" + cid):
        convs.clear()
        r = await e.client.get(url)
        assert r.status_code == 200, (url, r.text[:300])
        assert len(convs) == 1, (url, convs)  # một lần duy nhất (limit WS_CONV_LIMIT, mọi kênh)
    assert reads == []  # GET không bao giờ ghi
    # POST /manager/read: cần CSRF + khoá phiên
    assert (await e.client.post("/manager/read", data={"csrf": e.csrf, "c": cid})).status_code == 403
    assert (await e.post("/manager/read", {"c": cid, "csrf": "sai"})).status_code == 403
    assert reads == []
    r = await e.post("/manager/read", {"c": cid}, headers={"X-Ahub-Fetch": "1"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert reads == [{"conversation_id": cid}]
    assert (await e.post("/manager/read", {}, headers={"X-Ahub-Fetch": "1"})).status_code == 400


async def test_viewing_conversation_shows_no_unread_without_writing(menv):
    e = menv
    await e.login()
    await designate(e)
    w = await mgr_rpc(e, "workstream.create", {"title": "Dreem", "key": "LH-2"})
    r = await e.post("/manager/send", {"text": "làm đi", "w": w["id"]}, headers={"X-Ahub-Fetch": "1"})
    cid = r.json()["conversation_id"]
    await until(lambda: (e.hub.store.get_run(r.json()["run_id"]) or {}).get("status") not in ("queued", "running"))
    e.hub.store.conn.execute("UPDATE manager_conversations SET read_upto=0 WHERE id=?", (cid,))

    def tab(html: str) -> str:
        return re.search(rf'<a class="ws-tab[^"]*" href="/manager\?w={w["id"]}".*?</a>', html, re.S).group(0)

    assert '<span class="ws-unread">' in tab((await e.client.get("/partials/manager/tabs")).text)
    assert '<span class="ws-unread">' not in tab((await e.client.get(f"/manager?c={cid}")).text)  # đang xem: không hiện
    assert '<span class="ws-unread">' in tab((await e.client.get("/partials/manager/tabs")).text)  # …nhưng chưa ghi
    await e.post("/manager/read", {"c": cid}, headers={"X-Ahub-Fetch": "1"})
    assert '<span class="ws-unread">' not in tab((await e.client.get("/partials/manager/tabs")).text)


# =========================================================================== đổi tab: vỏ tối giản
async def test_partial_grid_shell_for_tab_switch(menv):
    e = menv
    await e.login()
    await designate(e)
    j = await send(e, "chào")
    badge = _spy(e, "tui.snapshot")
    r = await e.client.get(f"/manager?c={j['conversation_id']}", headers={"X-Ahub-Partial": "grid"})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    t = r.text
    assert 'id="mgr-grid"' in t and 'class="side-recents"' in t and 'id="toasts"' in t and "<title>Điều phối · cfharness</title>" in t
    assert 'class="nav"' not in t and "<script" not in t and 'id="confirm-dlg"' not in t  # không thanh bên/menu, không nạp lại JS
    full = (await e.client.get(f"/manager?c={j['conversation_id']}")).text
    assert 'class="nav"' in full and "app.js" in full
    grid = lambda h: re.search(r'<div class="mgr-grid" id="mgr-grid">.*', h, re.S).group(0)  # noqa: E731
    assert grid(t).split('data-sse-manager=')[0] == grid(full).split('data-sse-manager=')[0]
    assert len(badge) <= 2  # trang đầy đủ có thể hỏi trạng thái agent; vỏ thì không thêm số trên menu


# =========================================================================== nháp chạy chữ
def test_draft_markdown_skips_lru_and_is_throttled():
    from cfharness.web import manager as M

    assert M.DRAFT_EVERY_S >= 0.5
    P._MD_CACHE.clear()
    out = P.md("**nháp** đang chạy", cache=False)
    assert "<strong>nháp</strong>" in out and len(P._MD_CACHE) == 0
    assert P.md("**nháp** đang chạy") == out and len(P._MD_CACHE) == 1
    assert P.md("<script>x</script>", cache=False).count("<script>") == 0  # vẫn an toàn


def test_converter_draft_uses_uncached_markdown(monkeypatch):
    from types import SimpleNamespace

    from cfharness.web import manager as M

    seen: list[bool] = []
    real = P.md

    def md(text, *, cache=True):
        seen.append(cache)
        return real(text, cache=cache)

    monkeypatch.setattr(M.P, "md", md)
    monkeypatch.setattr(M, "_mod", lambda request: SimpleNamespace())
    t = [0.0]
    conv = M._converter(None, clock=lambda: t[0])
    msgs = []
    for i in range(6):
        t[0] += 0.2  # 0,2 s mỗi delta → nháp tối đa mỗi 0,5 s
        msgs += conv({"type": "message.delta", "run_id": "r1", "seq": i, "payload": {"text": f"chữ {i} "}})
    drafts = [m for m in msgs if m["t"] == "draft"]
    assert 1 <= len(drafts) <= 3 and seen and not any(seen)


# =========================================================================== tĩnh + gzip
async def test_static_versioned_is_immutable_and_gzip(menv):
    e = menv
    await e.login()
    html = (await e.client.get("/")).text
    url = re.search(r'src="(/static/app\.js\?v=[0-9a-f]+)"', html).group(1)
    r = await e.client.get(url, headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"] and "max-age=31536000" in r.headers["cache-control"]
    assert r.headers.get("content-encoding") == "gzip" and "initManager" in r.text  # httpx tự giải nén
    r = await e.client.get("/static/app.js")  # không phiên bản → vẫn hỏi lại server
    assert r.headers["cache-control"] == "no-cache"
    r = await e.client.get("/manager", headers={"Accept-Encoding": "gzip"})
    assert r.headers.get("content-encoding") == "gzip" and r.headers["cache-control"] == "no-store"
    assert r.headers.get("x-frame-options") or r.headers.get("content-security-policy")  # header bảo mật vẫn có


async def test_sse_is_never_gzipped():
    from cfharness.web.app import GzipMiddleware

    sent: list[dict] = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"data: x\n\n" * 400, "more_body": False})

    async def send(m):
        sent.append(m)

    async def receive():
        return {"type": "http.request", "body": b""}

    mw = GzipMiddleware(app)
    await mw({"type": "http", "path": "/sse/manager", "method": "GET", "headers": [(b"accept-encoding", b"gzip")]}, receive, send)
    assert not any(k == b"content-encoding" for k, _ in sent[0]["headers"])
