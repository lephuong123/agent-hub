"""GĐ1 trang Điều phối — hợp đồng v2 (docs/dev-notes/dieu-phoi-island.md):

- `GET /api/manager/snapshot` (hình dạng JSON, cursor, HTML đã cứng hoá chỉ ở mục `manager`);
- `/sse/manager/v2`: id `<boot>.<n>` tăng đơn điệu, nối lại sau `Last-Event-ID` không mất không trùng, `resync` khi hở quá ring /
  web khởi động lại; `msg.delta` chữ thô, `msg.done` HTML đã cứng hoá; phạm vi (`ws.status` cho mọi luồng, `msg.*` chỉ hội thoại đã mở);
- subscription daemon `detail_agent_id` (chữ chạy chỉ của điều phối viên); `POST /api/manager/watch`, `/api/manager/act/<tên>` (idempotency);
- kịch bản `[demo]` của hub demo. Không gọi `claude`."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from cfharness.bus import EventBus
from cfharness.events import make_event
from cfharness.runtime import FakeRuntime
from cfharness.web import demo_stream
from cfharness.web import manager_feed as F
from cfharness.web import manager_stub

from test_web import ISOLATED, _start, _stop, until  # noqa: F401
from test_web_manager import designate, send


# =========================================================================== nguồn event giả cho Feed (không daemon)
class FakeSub:
    def __init__(self, src: "FakeSource", params: dict[str, Any]) -> None:
        self.src, self.params, self.closed = src, params, False

    async def __aiter__(self):
        yield {"subscribed": {"epoch": self.src.epoch, "truncated": False}}
        while not self.closed:
            ev = await self.src.q.get()
            if ev is None:
                yield {"end": {"reason": "closed"}}
                return
            yield ev

    def close(self) -> None:
        self.closed = True


class FakeSource:
    """Daemon giả: `call` (manager.get, run.get, workstream.list, manager.conversations) + `open_sub` (hàng đợi event)."""

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.epoch = "ep1"
        self.seq = 0
        self.runs: dict[str, dict[str, Any]] = {}
        self.active: list[dict[str, Any]] = []
        self.convs: list[dict[str, Any]] = []
        self.subs: list[FakeSub] = []
        self.calls: list[str] = []

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append(method)
        if method == "manager.get":
            return {"agent_id": "boss"}
        if method == "run.get":
            return self.runs.get(params["id"]) or {"id": params["id"], "run_opts": {}}
        if method == "workstream.list":
            return [dict(w) for w in self.active] if params.get("status") == "active" else []
        if method == "manager.conversations":
            return [dict(c) for c in self.convs]
        return None

    def open_sub(self, params: dict[str, Any]) -> FakeSub:
        s = FakeSub(self, params)
        self.subs.append(s)
        return s

    def ev(self, type_: str, payload: dict[str, Any] | None = None, *, run: str | None = None, agent: str = "boss", **kw: Any) -> dict[str, Any]:
        self.seq += 1
        e = {"type": type_, "agent_id": agent, "run_id": run, "payload": payload or {}, "seq": self.seq, "ts": "2026-11-23T10:00:00+00:00",
             "source": kw.pop("source", "adapter"), **kw}
        self.q.put_nowait(e)
        return e


def parse(blocks: list[str]) -> list[tuple[str | None, dict[str, Any]]]:
    out = []
    for b in blocks:
        eid, data = None, None
        for ln in b.splitlines():
            if ln.startswith("id: "):
                eid = ln[4:]
            elif ln.startswith("data: "):
                data = json.loads(ln[6:])
        if data is not None:
            out.append((eid, data))
    return out


async def take(gen, n: int, *, pred=None, timeout: float = 5.0) -> list[tuple[str | None, dict[str, Any]]]:
    """Đọc luồng tới khi có `n` message (thoả `pred`) — trả mọi message đã đọc."""
    got: list[tuple[str | None, dict[str, Any]]] = []

    async def run() -> None:
        hit = 0
        async for block in gen:
            for m in parse([block]):
                got.append(m)
                if pred is None or pred(m[1]):
                    hit += 1
            if hit >= n:
                return

    await asyncio.wait_for(run(), timeout)
    return got


@pytest.fixture
async def feed():
    src = FakeSource()
    src.runs["r1"] = {"id": "r1", "run_opts": {"manager_conversation": "cA", "manager_workstream": "ws1"}}
    src.runs["r2"] = {"id": "r2", "run_opts": {"manager_conversation": "cB"}}
    f = F.Feed(src.call, src.open_sub)
    f.src = src  # type: ignore[attr-defined]
    await f.ensure()
    yield f
    await f.close()


async def settle(f: F.Feed, n: int, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if f.n >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"feed chỉ có {f.n} message, cần {n}")


# =========================================================================== id + nối lại
async def test_ids_monotonic_and_replay_after_cut_has_no_loss_no_dup(feed):
    src = feed.src
    w = F.Watch(convs={"cA"})
    start = F.parse_id(feed.cursor)
    gen = F.stream(feed, w, start)
    for i in range(30):
        src.ev("message.delta", {"text": f"t{i} "}, run="r1")
    first = await take(gen, 12, pred=lambda d: d["t"] == "msg.delta")
    await gen.aclose()  # cắt luồng giữa chừng
    last_id = [eid for eid, d in first if eid][-1]
    for i in range(30, 50):
        src.ev("message.delta", {"text": f"t{i} "}, run="r1")
    await settle(feed, 50)
    gen2 = F.stream(feed, w, F.parse_id(last_id))  # như EventSource gửi Last-Event-ID
    second = await take(gen2, 50 - 12, pred=lambda d: d["t"] == "msg.delta")
    await gen2.aclose()
    texts = [d["text"] for _, d in first + second if d["t"] == "msg.delta"]
    assert texts == [f"t{i} " for i in range(50)]  # 0 mất, 0 trùng, đúng thứ tự
    ns = [F.parse_id(eid)[1] for eid, d in first + second if eid]
    assert ns == sorted(ns) and len(ns) == len(set(ns))  # id tăng đơn điệu
    assert all(eid and eid.startswith(feed.boot + ".") for eid, d in first + second if d["t"] != "hello")


async def test_resync_when_gap_exceeds_ring_or_boot_changed(feed):
    src = feed.src
    feed.ring_max = 10
    old = F.parse_id(feed.cursor)
    for i in range(25):
        src.ev("message.delta", {"text": str(i)}, run="r1")
    await settle(feed, 25)
    got = await take(F.stream(feed, F.Watch(convs={"cA"}), old), 1, pred=lambda d: d["t"] == "resync")
    kinds = [d["t"] for _, d in got]
    assert kinds[:2] == ["hello", "resync"] and got[1][1]["reason"] == "gap"
    assert got[1][0] == feed.cursor  # resync mang id hiện tại → nối lại sau đó không resync nữa
    got = await take(F.stream(feed, F.Watch(convs={"cA"}), ("khac", 3)), 1, pred=lambda d: d["t"] == "resync")
    assert got[1][1] == {"t": "resync", "reason": "boot"}
    # vẫn trong ring → không resync
    gen = F.stream(feed, F.Watch(convs={"cA"}), (feed.boot, feed.n - 3))
    got = await take(gen, 3, pred=lambda d: d["t"] == "msg.delta")
    await gen.aclose()
    assert [d["t"] for _, d in got] == ["hello", "msg.delta", "msg.delta", "msg.delta"]


async def test_epoch_change_emits_resync(feed):
    src = feed.src
    src.epoch = "ep2"
    src.q.put_nowait(None)  # daemon đóng luồng (khởi động lại) → feed đăng ký lại, thấy epoch mới
    for _ in range(300):
        if any(json.loads(e.raw)["t"] == "resync" for e in feed.ring):
            break
        await asyncio.sleep(0.01)
    msgs = [json.loads(e.raw) for e in feed.ring]
    assert {"t": "resync", "reason": "epoch"} in msgs
    assert src.subs[-1].params.get("since_seq") is not None or src.seq == 0


# =========================================================================== nội dung + phạm vi
async def test_delta_is_raw_text_and_done_is_sanitized_html(feed):
    src = feed.src
    gen = F.stream(feed, F.Watch(convs={"cA"}), F.parse_id(feed.cursor))
    src.ev("run.started", {}, run="r1", source="hub")
    src.ev("message.delta", {"text": "<img src=x onerror=alert(1)> **đậm**"}, run="r1")
    src.ev("message.complete", {"text": "<img src=x onerror=alert(1)> **đậm**"}, run="r1")
    src.ev("manager.reply", {"conversation_id": "cA", "run_id": "r1", "text": "**đậm** <script>alert(2)</script>"}, run="r1")
    got = await take(gen, 1, pred=lambda d: d["t"] == "msg.done")
    await gen.aclose()
    by = {d["t"]: d for _, d in got}
    assert by["run.start"]["conv"] == "cA" and by["run.start"]["ws"] == "ws1"
    d = by["msg.delta"]
    assert d["text"] == "<img src=x onerror=alert(1)> **đậm**" and "html" not in d  # chữ thô, không markdown phía server
    item = by["msg.done"]["item"]
    assert item["kind"] == "manager" and item["id"].startswith("e")
    assert "<strong>đậm</strong>" in item["html"]
    low = item["html"].lower()
    assert "<script" not in low and "<img" not in low and "onerror" not in low.replace("onerror=alert(1)&gt;", "")
    # chữ gốc (= chữ đã lưu của câu trả lời, KHÔNG gộp đoạn đã chạy — tải lại trang thấy y hệt) cho nút Sao chép — island gán textContent
    assert item["text"] == "**đậm** <script>alert(2)</script>"
    assert F.md_cached(item["id"], item["text"]) == item["html"]  # cache theo id


async def test_scope_ws_status_for_all_msg_only_for_watched(feed):
    src = feed.src
    src.active = [{"id": "ws1", "key": "LH-1", "title": "Một", "status": "active", "tasks": {}, "needs_human": 0}]
    await feed.refresh_tabs()  # mốc
    wa = F.Watch(convs={"cA"})
    wb = F.Watch(ws={F.LOBBY})  # chỉ Sảnh
    ga, gb = F.stream(feed, wa, F.parse_id(feed.cursor)), F.stream(feed, wb, F.parse_id(feed.cursor))
    src.ev("message.delta", {"text": "cho A"}, run="r1")
    src.ev("message.delta", {"text": "cho B"}, run="r2")  # r2: hội thoại cB ở Sảnh
    src.active[0]["tasks"] = {"running": 1, "total": 1, "open": 1}
    src.ev("run.started", {}, run="rx", agent="lap-trinh", source="hub")  # agent khác → chỉ đổi trạng thái luồng
    ra = await take(ga, 1, pred=lambda d: d["t"] == "ws.status" and d["ws"] == "ws1")
    rb = await take(gb, 1, pred=lambda d: d["t"] == "ws.status" and d["ws"] == "ws1")
    await ga.aclose()
    await gb.aclose()
    ta = [d.get("text") for _, d in ra if d["t"] == "msg.delta"]
    tb = [d.get("text") for _, d in rb if d["t"] == "msg.delta"]
    assert ta == ["cho A"] and tb == ["cho B"]
    st = next(d for _, d in ra if d["t"] == "ws.status")
    assert st["dot"] == "run" and st["label"] == "LH-1"
    assert not any(d["t"] == "run.start" and d.get("run") == "rx" for _, d in ra + rb)  # run agent khác không lộ ra


async def test_skipped_messages_still_advance_last_event_id(feed):
    src = feed.src
    gen = F.stream(feed, F.Watch(convs={"cZ"}), F.parse_id(feed.cursor), heartbeat_s=0.05)
    for i in range(5):
        src.ev("message.delta", {"text": str(i)}, run="r1")  # ngoài phạm vi
    await settle(feed, 5)
    blocks = []
    async for b in gen:
        blocks.append(b)
        if b.startswith("id: "):
            break
    await gen.aclose()
    # nhịp ping là message thật (client canh im lặng) — vẫn mang id cuối: nối lại không quét lại phần đã bỏ
    assert blocks[-1] == f"id: {feed.boot}.{feed.n}\ndata: {F.PING}\n\n"  # chỉ id (không data) — nối lại không quét lại phần đã bỏ


def test_bus_detail_agent_id_filters_detail_events():
    bus = EventBus()
    sub = bus.subscribe(None, level="summary", detail_agent_id="boss")
    for agent in ("boss", "other"):
        bus.publish(make_event("message.delta", agent, {"text": "x"}, run_id="r"))
        bus.publish(make_event("run.started", agent, {}, run_id="r"))
    got = [(e.agent_id, e.type) for e in sub.drain()]
    assert got == [("boss", "message.delta"), ("boss", "run.started"), ("other", "run.started")]


# =========================================================================== qua daemon thật + HTTP
@pytest.fixture
async def menv(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED, line_delay_s=0.005))
    e.stub = manager_stub.install(e.d)
    e.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    yield e
    f = getattr(e.app.state, "mgr_feed", None)
    if f is not None:
        await f.close()
    await _stop(e)


async def test_snapshot_shape_and_live_stream_through_daemon(menv):
    e = menv
    await e.login()
    r = await e.client.get("/api/manager/snapshot")
    assert r.status_code == 200 and r.json()["manager"]["agent_id"] is None and "setup" in r.json()
    await designate(e)
    j = await send(e, "Chào <b>bạn</b>")
    cid, run = j["conversation_id"], j["run_id"]
    assert await until(lambda: (e.hub.store.get_run(run) or {}).get("status") == "completed")
    await asyncio.sleep(0.1)
    r = await e.client.get(f"/api/manager/snapshot?c={cid}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    s = r.json()
    assert s["v"] == 1 and s["conv"] == cid and s["scope"] == F.LOBBY
    assert F.parse_id(s["cursor"])[0] == s["boot"]
    assert {"conversations", "workstreams", "items", "pending", "actions", "side_actions", "conv_runs"} <= set(s)
    assert set(s["workstreams"]) == {"enabled", "lobby", "active", "closed"} and s["workstreams"]["lobby"]["ws"] == F.LOBBY
    kinds = [it["kind"] for it in s["items"]]
    assert "user" in kinds and "manager" in kinds and kinds[0] == "day"
    user = next(it for it in s["items"] if it["kind"] == "user")
    assert user["text"] == "Chào <b>bạn</b>" and "html" not in user  # tin người: chữ thô
    mgr = next(it for it in s["items"] if it["kind"] == "manager")
    assert mgr["id"].startswith("m") and mgr["html"].startswith("<") and mgr["run_id"] == run
    assert any(c["id"] == cid for c in s["conversations"])

    # luồng v2 qua daemon thật: chữ chạy của điều phối viên tới nhờ `detail_agent_id`
    feed = e.app.state.mgr_feed
    gen = F.stream(feed, F.Watch(convs={cid}), F.parse_id(feed.cursor))
    j2 = await send(e, "lần nữa", cid)
    got = await take(gen, 1, pred=lambda d: d["t"] == "msg.done" and d["run"] == j2["run_id"], timeout=15)
    await gen.aclose()
    types = [d["t"] for _, d in got]
    assert "run.start" in types and "msg.delta" in types and "tool.start" in types, types
    done = next(d for _, d in got if d["t"] == "msg.done")
    assert done["conv"] == cid and done["item"]["html"]
    # trang cũ + luồng cũ vẫn chạy
    assert (await e.client.get(f"/manager?c={cid}")).status_code == 200


async def test_watch_endpoint_and_idempotent_answer(menv):
    e = menv
    await e.login()
    await designate(e)
    j = await send(e, "hỏi tôi đi")
    cid, run = j["conversation_id"], j["run_id"]
    assert await until(lambda: (e.hub.store.get_run(run) or {}).get("status") == "completed")
    assert (await e.client.post("/api/manager/watch", data={"csrf": e.csrf, "tab": "t1", "c": cid})).status_code == 403  # thiếu khoá phiên
    r = await e.post("/api/manager/watch", {"tab": "t1", "c": cid, "w": "ws9"}, headers={"X-Ahub-Fetch": "1"})
    assert r.status_code == 200 and r.json() == {"ok": True, "c": [cid], "w": ["ws9"]}
    r = await e.post("/api/manager/watch", {"tab": "t1", "c": "khac", "op": "set"}, headers={"X-Ahub-Fetch": "1"})
    assert r.json()["c"] == ["khac"] and r.json()["w"] == []

    q = e.hub.questions.open(e.hub.store.get_run(run), "Chọn hướng?", ["A", "B"], source="manager", conversation_id=cid)
    s = (await e.client.get(f"/api/manager/snapshot?c={cid}")).json()
    assert any(a["kind"] == "question" and a["id"] == q["id"] and a["options"][0]["label"] == "A" for a in s["actions"])
    calls: list[dict] = []
    orig = e.d.methods["question.answer"]

    def spy(p):
        calls.append(dict(p))
        return orig(p)

    e.d.methods["question.answer"] = spy
    form = {"id": q["id"], "keys": "1", "idem": "k-123", "confirm": "yes"}
    r1 = await e.post("/api/manager/act/answer", form, headers={"X-Ahub-Fetch": "1"})
    assert r1.status_code == 200 and r1.json().get("ok") is True, r1.text
    r2 = await e.post("/api/manager/act/answer", form, headers={"X-Ahub-Fetch": "1"})
    assert r2.status_code == 200 and r2.json().get("replayed") is True
    assert len(calls) == 1  # gửi lại cùng khoá → không gọi RPC lần hai
    assert (await e.post("/api/manager/act/estop", {"idem": "x"})).status_code == 404


async def test_sse_v2_route_headers(menv):
    e = menv
    await e.login()
    await designate(e)
    feed = F.feed_of(type("R", (), {"app": e.app, "scope": {}})())  # cùng Feed của app
    await feed.ensure()
    assert feed.ready.is_set() and feed.conv.mgr == "boss"
    assert any(r.path == "/sse/manager/v2" for r in e.app.routes) and not any(r.path == "/sse/manager" for r in e.app.routes)  # luồng cũ đã gỡ


# =========================================================================== kịch bản [demo]
def test_demo_tokens_roundtrip_and_lines_parse():
    assert "".join(demo_stream.tokens(demo_stream.ANSWER)) == demo_stream.ANSWER
    toks = demo_stream.tokens(demo_stream.ANSWER)
    assert len(toks) > 150 and max(len(t) for t in toks) <= 7

    class Spec:
        session_id = "s-demo"
        cwd = Path(".")

    from cfharness.claude_adapter import ClaudeStreamAdapter

    ls = demo_stream.lines(Spec(), ISOLATED)
    deltas = [w for ln, w in ls if '"text_delta"' in ln]
    assert len(deltas) > 150 and all(w == demo_stream.TOKEN_S for w in deltas)  # ~100 token/s
    ad = ClaudeStreamAdapter("boss", "r_demo")
    evs = [ev for ln, _ in ls for ev in ad.feed(ln)]
    types = [ev.type for ev in evs]
    assert types.count("message.delta") > 150
    tools = [ev.payload.get("name") for ev in evs if ev.type == "tool.start"]
    assert tools == ["TodoWrite", "Read", "Bash"]


async def test_demo_runtime_streams_and_opens_approval_and_question(hub_home, monkeypatch):
    from cfharness.web.demo import DemoRuntime

    monkeypatch.setattr(demo_stream, "TOKEN_S", 0.0005)
    rt = DemoRuntime(ISOLATED, 0.0)
    e = await _start(hub_home, rt)
    rt.hub = e.hub
    try:
        e.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read"]}})
        await e.login()
        await designate(e)
        sub = e.hub.bus.subscribe("boss", level="detail")
        j = await send(e, "[demo] tình hình thế nào?")
        assert await until(lambda: (e.hub.store.get_run(j["run_id"]) or {}).get("status") == "completed", timeout=20)
        evs = sub.drain()
        assert sum(1 for x in evs if x.type == "message.delta") > 150
        assert sum(1 for x in evs if x.type == "manager.action") == 2
        aps = e.hub.approvals.list("waiting_human", run_id=j["run_id"])
        assert len(aps) == 1 and "tmp-demo" in aps[0]["preview"]
        qs = [q for q in e.hub.questions.list("all") if q.get("conversation_id") == j["conversation_id"]]
        assert qs and qs[0]["status"] in ("open", "with_manager")
    finally:
        await _stop(e)
