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


# ============================================================================ GĐ3: island là mặc định, lối thoát ?ui=old (một bản phát hành)
from test_web_manager import designate, send  # noqa: E402
from test_web_manager_v2 import menv  # noqa: E402,F401
from cfharness.web.manager import UI_COOKIE  # noqa: E402


async def test_island_is_default_and_ui_old_escape_hatch_is_sticky(menv):
    e = menv
    await e.login()
    await designate(e)
    j = await send(e, "chào")
    cid = j["conversation_id"]
    island = 'id="coord"'
    old = 'id="mgr-grid"'
    for url in ("/manager", f"/manager?c={cid}", "/manager?new=1", "/manager?ui=next"):
        r = await e.client.get(url)
        assert r.status_code == 200 and island in r.text and old not in r.text, url
    # ?ui=old → trang cũ + nhớ lựa chọn (link nội bộ của trang cũ không mang ?ui=)
    r = await e.client.get("/manager?ui=old")
    assert r.status_code == 200 and old in r.text and island not in r.text
    assert 'href="/manager?ui=new"' in r.text  # đường quay về giao diện mới
    assert UI_COOKIE in r.headers.get("set-cookie", "")
    r = await e.client.get(f"/manager?c={cid}")
    assert old in r.text and island not in r.text
    # vỏ lưới của app.js cũ (X-Ahub-Partial: grid) luôn là trang cũ
    e.client.cookies.clear()
    await e.login()
    r = await e.client.get(f"/manager?c={cid}", headers={"X-Ahub-Partial": "grid"})
    assert old in r.text
    # ?ui=new → island và xoá lựa chọn
    await e.client.get("/manager?ui=old")
    r = await e.client.get("/manager?ui=new")
    assert island in r.text and UI_COOKIE in r.headers.get("set-cookie", "")  # Set-Cookie xoá (max-age=0)
    assert island in (await e.client.get("/manager")).text


def test_island_links_drop_ui_param():
    """Link/URL trong island không còn `?ui=next`; link `/manager?ui=old` trong trang thì tải thật (không bị island chặn)."""
    from pathlib import Path

    d = Path(__file__).resolve().parent.parent / "src" / "cfharness" / "web" / "static" / "coord"
    src = "".join(f.read_text(encoding="utf-8") for f in d.glob("*.js"))
    assert "ui=next" not in src
    assert 'u.searchParams.get("ui") === "old"' in src


def test_voice_observer_scoped_to_coordinator_region():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "src" / "cfharness" / "web" / "static" / "voice.js").read_text(encoding="utf-8")
    assert "mo.observe(document.body" not in js
    assert 'document.getElementById("coord")' in js and 'tg.closest(".md")' in js
