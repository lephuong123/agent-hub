"""Island Điều phối — các lỗi verify vòng 3:

- hub bận với event NGOÀI phạm vi tab (agent khác đang stream) đánh thức `wait()` liên tục → trước đây không bao giờ ping, watchdog client
  (40 s im lặng) nối lại vô ích mỗi ~40 s. Nay nhịp ping tính theo HẠN từ message cuối đã gửi, và mang `id` đã tiến."""

from __future__ import annotations

import asyncio

from cfharness.web import manager_feed as F

from test_web_manager_v2 import FakeSource


async def test_ping_keeps_coming_while_hub_busy_out_of_scope():
    src = FakeSource()
    src.runs["r2"] = {"id": "r2", "run_opts": {"manager_conversation": "cOther"}}
    f = F.Feed(src.call, src.open_sub)
    await f.ensure()
    try:
        w = f.watch("tab1", ["cMine"], [])
        gen = F.stream(f, w, F.parse_id(f.cursor), heartbeat_s=0.2)
        blocks: list[str] = []

        async def pump():
            async for b in gen:
                blocks.append(b)

        t = asyncio.create_task(pump())
        for _ in range(30):  # 1.5 s event ngoài phạm vi, mỗi 50 ms (< heartbeat)
            src.ev("message.delta", {"text": "x"}, run="r2")
            await asyncio.sleep(0.05)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        await gen.aclose()
        pings = [b for b in blocks if F.PING in b]
        assert len(pings) >= 4, f"chỉ {len(pings)} ping trong 1.5 s (heartbeat 0.2 s)"
        assert not any("message.delta" in b or '"msg.delta"' in b for b in blocks), "event ngoài phạm vi lọt ra"
        ids = [F.parse_id(b.split("id: ", 1)[1].split("\n", 1)[0])[1] for b in pings if b.startswith("id: ")]
        assert len(ids) >= 2 and ids == sorted(ids) and ids[-1] > ids[0], f"id ping phải tiến theo phần đã bỏ qua: {ids}"
    finally:
        await f.close()


# ============================================================================ GĐ3 + gỡ trang cũ: island là trang Điều phối duy nhất
from test_web_manager import designate, send  # noqa: E402
from test_web_manager_v2 import menv  # noqa: E402,F401


async def test_island_is_only_coordinator_page_old_escape_removed(menv):
    e = menv
    await e.login()
    await designate(e)
    j = await send(e, "chào")
    cid = j["conversation_id"]
    island = 'id="coord"'
    old = 'id="mgr-grid"'
    # `?ui=old` (lối thoát cũ), cookie cũ, vỏ lưới của app.js cũ: tất cả là island, không đặt/xoá cookie nào
    e.client.cookies.set("ahub_mgr_ui", "old", path="/manager")
    for url in ("/manager", f"/manager?c={cid}", "/manager?new=1", "/manager?ui=next", "/manager?ui=old", "/manager?ui=new"):
        r = await e.client.get(url)
        assert r.status_code == 200 and island in r.text and old not in r.text, url
        assert "ahub_mgr_ui" not in r.headers.get("set-cookie", ""), url
        assert "ui=new" not in r.text and "giao diện cũ" not in r.text, url
    r = await e.client.get(f"/manager?c={cid}", headers={"X-Ahub-Partial": "grid"})
    assert island in r.text and old not in r.text and "<aside" in r.text  # trang đầy đủ, không còn vỏ lưới
    # route chỉ của trang cũ đã gỡ; thẻ trạng thái (Tổng quan) + POST island dùng vẫn còn
    assert (await e.client.get("/sse/manager")).status_code == 404
    assert (await e.client.get("/partials/manager/tabs")).status_code == 404
    assert (await e.client.get("/partials/manager?compact=1")).status_code == 200


def test_island_links_drop_ui_param():
    """Link/URL trong island không còn `?ui=next`, không còn nhánh `?ui=old` (trang cũ đã gỡ)."""
    from pathlib import Path

    d = Path(__file__).resolve().parent.parent / "src" / "cfharness" / "web" / "static" / "coord"
    src = "".join(f.read_text(encoding="utf-8") for f in d.glob("*.js"))
    assert "ui=next" not in src
    assert '=== "old"' not in src


def test_voice_observer_scoped_to_coordinator_region():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "src" / "cfharness" / "web" / "static" / "voice.js").read_text(encoding="utf-8")
    assert "mo.observe(document.body" not in js
    assert 'document.getElementById("coord")' in js and 'tg.closest(".md")' in js
