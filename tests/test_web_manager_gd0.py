"""GĐ0 trang Điều phối (sửa nhanh hiệu năng — plan option B):

- khung "đang trả lời" đọc event GẦN NHẤT của run (run dài > PENDING_SCAN event không ra tool cũ);
- mỗi lần tải trang chỉ MỘT `manager.conversations` (kể cả `?w=`), GET không ghi "đã đọc" — `POST /manager/read` (CSRF + khoá phiên);
- nháp markdown đang chạy chữ không vào LRU `present.md`;
- tĩnh có `?v=` → Cache-Control immutable; gzip cho HTML/JS (không cho SSE).
Không gọi `claude`."""

from __future__ import annotations

import asyncio
import re

import pytest

from cfharness.events import make_event
from cfharness.runtime import FakeRuntime
from cfharness.web import manager_stub
from cfharness.web import present as P

from test_web import ISOLATED, _start, _stop, until  # noqa: F401
from test_web_manager import designate, mgr_rpc, send, snap



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
    # chờ runtime giả phát xong tool của chính nó (nếu không, tool đó có thể tới SAU tool "mới nhất" của test → khung hiện tool giả)
    for _ in range(100):
        if ((await snap(e, f"?c={conv}"))["pending"] or {}).get("tool"):
            break
        await asyncio.sleep(0.05)
    # run "dài": tool cũ (đã xong từ lâu) rồi nhiều event, cuối cùng là tool đang dùng — chỉ đọc 4 event gần nhất
    e.hub.publish(make_event("tool.start", "boss", {"name": "Glob", "tool_id": "old", "args_preview": "cu-xa-lam"}, run_id=run))
    for i in range(10):
        e.hub.publish(make_event("learning.applied", "boss", {"i": i}, run_id=run))
    e.hub.publish(make_event("tool.start", "boss", {"name": "Grep", "tool_id": "new", "args_preview": "moi-nhat"}, run_id=run))
    calls = _spy(e, "events.query")
    monkeypatch.setattr(M, "PENDING_SCAN", 4)
    pend: dict = {}
    for _ in range(100):  # event ghi DB bất đồng bộ — chờ tool mới nhất đã lưu
        pend = (await snap(e, f"?c={conv}"))["pending"] or {}
        if "moi-nhat" in (pend.get("tool") or ""):
            break
        await asyncio.sleep(0.05)
    assert pend["run"] == run and "moi-nhat" in pend["tool"] and "cu-xa-lam" not in pend["tool"], pend
    assert any(c.get("run_id") == run and c.get("order") == "desc" and c.get("limit") == 4 for c in calls), calls


# =========================================================================== RPC trùng + "đã đọc" không ghi trong GET
async def test_one_conversations_rpc_and_get_never_marks_read(menv, monkeypatch):
    e = menv
    await e.login()
    await designate(e)
    w = await mgr_rpc(e, "workstream.create", {"title": "Dreem", "key": "LH-1"})
    j = await send(e, "chào")
    cid = j["conversation_id"]
    await until(lambda: (e.hub.store.get_run(j["run_id"]) or {}).get("status") not in ("queued", "running"))
    assert (await e.client.get("/manager")).status_code == 200  # tạo feed của island
    feed = e.app.state.mgr_feed  # trạng thái tab làm mới NỀN theo event (debounce) — không thuộc lượt tải trang, tắt khi đếm
    monkeypatch.setattr(feed, "kick_tabs", lambda: None)
    if feed._ws_task is not None:
        await feed._ws_task
    convs = _spy(e, "manager.conversations")
    reads = _spy(e, "manager.conversation_read")
    for url in ("/manager", f"/manager?c={cid}", f"/manager?w={w['id']}", f"/api/manager/snapshot?c={cid}"):
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

    async def unread(q: str = "") -> int:  # số chưa đọc trên tab luồng (ảnh chụp island)
        tabs = (await snap(e, q))["workstreams"]["active"]
        return next(t["unread"] for t in tabs if t["ws"] == w["id"])

    reads = _spy(e, "manager.conversation_read")
    assert await unread() >= 1
    assert await unread(f"?c={cid}") == 0  # đang xem: không hiện
    assert await unread() >= 1 and reads == []  # …nhưng chưa ghi
    await e.post("/manager/read", {"c": cid}, headers={"X-Ahub-Fetch": "1"})
    assert await unread() == 0

# =========================================================================== nháp chạy chữ
def test_draft_markdown_skips_lru_and_is_throttled():
    P._MD_CACHE.clear()
    out = P.md("**nháp** đang chạy", cache=False)
    assert "<strong>nháp</strong>" in out and len(P._MD_CACHE) == 0
    assert P.md("**nháp** đang chạy") == out and len(P._MD_CACHE) == 1
    assert P.md("<script>x</script>", cache=False).count("<script>") == 0  # vẫn an toàn


# =========================================================================== tĩnh + gzip
async def test_static_versioned_is_immutable_and_gzip(menv):
    e = menv
    await e.login()
    html = (await e.client.get("/activity")).text
    url = re.search(r'src="(/static/app\.js\?v=[0-9a-f]+)"', html).group(1)
    r = await e.client.get(url, headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"] and "max-age=31536000" in r.headers["cache-control"]
    assert r.headers.get("content-encoding") == "gzip" and "function txAppend" in r.text  # httpx tự giải nén
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
    await mw({"type": "http", "path": "/sse/manager/v2", "method": "GET", "headers": [(b"accept-encoding", b"gzip")]}, receive, send)
    assert not any(k == b"content-encoding" for k, _ in sent[0]["headers"])
