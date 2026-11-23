"""Island Điều phối — các lỗi verify vòng 4:

1. Câu trả lời tới qua SSE khác bản đọc lại sau khi tải trang: `msg.done` gộp lời dẫn đã chạy chữ ("Để mình xem các run…") còn ảnh chụp
   (tin đã lưu) thì không. Nay `msg.done` dựng từ ĐÚNG chữ đã lưu qua cùng `present.mgr_message` + id `m<message_id>` → cùng id, chữ, HTML.
2. Tin người gửi từ client khác không hiện ở khung đang ẩn tới khi mở lại: hub phát `manager.message` → `msg.user` tới mọi khung đang xem.
3. CLS khi vuốt mạnh lên dòng thời gian dài ngay sau khi mở (vẽ dần): mục đầu đang vẽ co 24 px mỗi lượt chèn (luật anh em
   `.mm.mgr + .mm.mgr`) → `cont` tính theo cả dòng thời gian; neo mục đang thấy; `contain-intrinsic-size` = hộp nội dung.
4. Mở/đóng panel xem tệp làm dòng thời gian hẹp/rộng → giữ mục đang đọc tại chỗ (`keepRead`).
Phần trình duyệt (CLS, đổi tab, chữ chạy) đo bằng Chromium headless — ghi ở docs/dev-notes/dieu-phoi-island.md."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cfharness.web import manager_feed as F
from cfharness.web import present as P
from m7_fake import close, make_daemon, manager_run, rpc, wait_status

from test_web_manager_v2 import FakeSource, take

STATIC = Path(__file__).resolve().parents[1] / "src" / "cfharness" / "web" / "static"


def _wire(e) -> dict:
    return {"type": e.type, "agent_id": e.agent_id, "run_id": e.run_id, "payload": dict(e.payload), "seq": e.seq,
            "ts": e.ts.isoformat() if hasattr(e.ts, "isoformat") else e.ts, "source": e.source}


@pytest.fixture
async def d(hub_home):
    dm = await make_daemon(hub_home)
    yield dm
    await close(dm)


# ============================================================================ 1 + 2: hub phát message_id; SSE = ảnh chụp
async def test_live_reply_and_user_message_equal_snapshot_items(d):
    d.script.hold_manager = False
    d.hub.agents.create({"id": "boss", "name": "boss", "tool_policy": {"tools": ["Read"]}})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    r = await rpc(d, "manager.chat", {"channel": "web", "text": "xem các run giúp"})
    await wait_status(d, r["run_id"], ("completed",))
    await asyncio.sleep(0.05)
    cid = r["conversation_id"]
    hist = await rpc(d, "manager.history", {"conversation_id": cid})
    snap = {m["role"]: F.item_json(P.mgr_message(m), "boss") for m in hist}  # đúng đường ảnh chụp (manager._timeline → item_json)
    evs = {t: [e for e in d.hub.store.query_events(types=[t]) if e.run_id == r["run_id"]] for t in ("manager.message", "manager.reply")}
    assert len(evs["manager.message"]) == 1 and len(evs["manager.reply"]) == 1
    conv = F.Converter()
    conv.mgr = "boss"
    sc = F.Scope(conv=cid, ws=F.LOBBY)
    # lời dẫn đã chạy chữ giữa các lần dùng tool — KHÔNG thuộc câu trả lời đã lưu
    for t, p in (("message.delta", {"text": "Để mình xem các run… "}), ("message.complete", {"text": "Để mình xem các run…"})):
        conv.convert({"type": t, "agent_id": "boss", "run_id": r["run_id"], "payload": p, "seq": 1, "ts": "x"}, sc, {})
    [(done, dsc)] = conv.convert(_wire(evs["manager.reply"][0]), sc, {})
    [(user, usc)] = conv.convert(_wire(evs["manager.message"][0]), sc, {})
    assert done["t"] == "msg.done" and user["t"] == "msg.user" and dsc.conv == usc.conv == cid
    for live, saved in ((done["item"], snap["manager"]), (user["item"], snap["user"])):
        assert live["id"] == saved["id"] and live["id"].startswith("m")
        assert live["text"] == saved["text"] and live.get("html") == saved.get("html") and live.get("cut") == saved.get("cut")
    assert "Để mình xem" not in done["item"]["text"]


async def test_steered_message_also_published(d):
    res = await manager_run(d)
    r2 = await rpc(d, "manager.chat", {"channel": "web", "text": "thêm ý", "conversation_id": res["conversation_id"]})
    assert r2["steered"]
    msgs = [e.payload for e in d.hub.store.query_events(types=["manager.message"]) if e.payload.get("text") == "thêm ý"]
    hist = await rpc(d, "manager.history", {"conversation_id": res["conversation_id"]})
    assert msgs and msgs[0]["message_id"] == [m["id"] for m in hist if m["text"] == "thêm ý"][0] and msgs[0]["run_id"] == res["run_id"]


def test_reply_without_message_id_keeps_event_id_and_text_is_payload():
    c = F.Converter()
    c.mgr = "boss"
    sc = F.Scope(conv="cA", ws="_")
    c.convert({"type": "message.delta", "agent_id": "boss", "run_id": "r1", "payload": {"text": "lời dẫn"}, "seq": 1, "ts": "t"}, sc, {})
    [(m, _)] = c.convert({"type": "manager.reply", "agent_id": "boss", "run_id": "r1", "seq": 7, "ts": "t",
                          "payload": {"conversation_id": "cA", "run_id": "r1", "text": "kết quả", "kind": "reply"}}, sc, {})
    assert m["item"]["id"] == "e7" and m["item"]["text"] == "kết quả"
    [(m, _)] = c.convert({"type": "manager.reply", "agent_id": "boss", "run_id": "r1", "seq": 8, "ts": "t",
                          "payload": {"conversation_id": "cA", "run_id": "r1", "text": "x" * 20_000, "truncated": True, "message_id": 5}}, sc, {})
    assert m["item"]["id"] == "m5" and m["item"]["cut"]  # chạm trần → cùng ghi chú "đã cắt" như ảnh chụp


async def test_msg_user_only_to_tabs_watching_that_conversation():
    src = FakeSource()
    f = F.Feed(src.call, src.open_sub)
    await f.ensure()
    try:
        ga = F.stream(f, F.Watch(convs={"cA"}), F.parse_id(f.cursor))
        gb = F.stream(f, F.Watch(convs={"cB"}), F.parse_id(f.cursor))
        src.ev("manager.message", {"conversation_id": "cA", "run_id": "r1", "message_id": 9, "role": "user", "text": "từ Telegram"}, run="r1")
        src.ev("manager.message", {"conversation_id": "cB", "run_id": "r2", "message_id": 10, "role": "user", "text": "B"}, run="r2")
        ra = await take(ga, 1, pred=lambda m: m["t"] == "msg.user")
        rb = await take(gb, 1, pred=lambda m: m["t"] == "msg.user")
        await ga.aclose()
        await gb.aclose()
        ua = [m for _, m in ra if m["t"] == "msg.user"]
        ub = [m for _, m in rb if m["t"] == "msg.user"]
        assert [m["item"]["id"] for m in ua] == ["m9"] and ua[0]["item"]["text"] == "từ Telegram" and ua[0]["item"]["kind"] == "user"
        assert [m["item"]["id"] for m in ub] == ["m10"]
    finally:
        await f.close()


# ============================================================================ 3 + 4: dòng thời gian (phần kiểm được không cần trình duyệt)
def test_timeline_layout_rules_present():
    v = (STATIC / "coord" / "views.js").read_text(encoding="utf-8")
    css = (STATIC / "coord.css").read_text(encoding="utf-8")
    # `cont` tính theo cả dòng thời gian (không theo anh em DOM) + Row vẽ lại khi đổi
    assert 'list[i - 1].kind === "manager"' in v and "n.cont !== o.cont" in v
    assert ".coord .coord-flow > .mm.mgr.cont > .mm-who { display: none; }" in css
    assert ".coord .coord-flow > .mm.mgr:not(.cont) > .mm-who { display: flex; }" in css
    # neo = mục đầu tiên bắt đầu trong khung (tìm nhị phân), không phải mục đầu đang vẽ
    assert "getBoundingClientRect().top < top) lo = m + 1" in v
    # cỡ giữ chỗ content-visibility = hộp nội dung (trừ padding + viền)
    assert "x.offsetHeight - boxExtra(x)" in v
    # đổi bề ngang → giữ mục đang đọc; xuống dòng lại không phải "tin mới"
    assert "if (wide && !p.scroll.stick) keepRead(p);" in v and "onGrow(p.scroll, !wide && hh > lastH + 1)" in v
    # panel xem tệp đóng: trả focus về chip KHÔNG cuộn dòng thời gian (khung tự giữ mục đang đọc — đo trước đây lệch −54 px)
    assert "back.focus({ preventScroll: true })" in (STATIC / "files.js").read_text(encoding="utf-8")
