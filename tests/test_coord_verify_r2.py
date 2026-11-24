"""Island Điều phối — các lỗi verify vòng 2 (docs/dev-notes/dieu-phoi-island.md):

- ngắt SSE lâu hơn WATCH_TTL_S (ngủ máy) → server dọn phạm vi của tab; client nối lại bằng URL mang ĐỦ phạm vi hiện tại (tests/
  coord_store_check.mjs kiểm phía client) → server phát lại từ ring phần sau `since` của hội thoại mở SAU khi tải trang (0 mất);
- nhịp ping là message thật (client canh luồng chết lặng);
- `act.item` ("Hành động gần đây") tới cả tab chỉ mở hội thoại KHÁC của cùng luồng (Scope.side);
- jsmin: `/` sau `++`/`--` hậu tố là phép chia."""

from __future__ import annotations

import shutil
import subprocess
import time

from cfharness.web import jsmin
from cfharness.web import manager_feed as F

from test_web_manager_v2 import FakeSource, parse, take  # noqa: F401


async def test_reconnect_after_ttl_with_full_scope_replays_later_conversation(monkeypatch):
    src = FakeSource()
    src.runs["r1"] = {"id": "r1", "run_opts": {"manager_conversation": "cBoot"}}
    src.runs["r2"] = {"id": "r2", "run_opts": {"manager_conversation": "cLater"}}
    f = F.Feed(src.call, src.open_sub)
    await f.ensure()
    try:
        w = f.watch("tab1", ["cBoot"], [])
        gen = F.stream(f, w, F.parse_id(f.cursor), heartbeat_s=30)
        src.ev("run.started", {}, run="r1")
        got = await take(gen, 1, pred=lambda d: d["t"] == "run.start")
        last = F.parse_id(got[-1][0])
        f.watch("tab1", ["cLater"], [])  # khung mở sau khi tải trang (ảnh chụp ?tab=)
        await gen.aclose()  # mạng rớt (ngủ máy)
        assert w.live == 0
        src.ev("run.started", {}, run="r2")  # trong lúc ngắt
        src.ev("message.delta", {"text": "đang trả lời"}, run="r2")
        # ngắt > TTL: lần gọi bất kỳ dọn phạm vi của tab (không còn luồng nào giữ)
        monkeypatch.setattr(F, "WATCH_TTL_S", 0.0)
        w.seen = time.monotonic() - 10
        f.watch("tab-khac", [], [])
        assert "tab1" not in f.watches
        # client tự mở lại với URL mang ĐỦ phạm vi hiện tại (không phải URL ban đầu chỉ có cBoot) + since = id cuối
        w2 = f.watch("tab1", ["cBoot", "cLater"], [])
        gen2 = F.stream(f, w2, last, heartbeat_s=30)
        got2 = await take(gen2, 2, pred=lambda d: d.get("conv") == "cLater")
        await gen2.aclose()
        ts = [d["t"] for _, d in got2 if d.get("conv") == "cLater"]
        assert ts[:2] == ["run.start", "msg.delta"], ts
    finally:
        await f.close()


async def test_heartbeat_is_a_data_message():
    src = FakeSource()
    f = F.Feed(src.call, src.open_sub)
    await f.ensure()
    try:
        gen = F.stream(f, F.Watch(), F.parse_id(f.cursor), heartbeat_s=0.05)
        blocks = []
        async for b in gen:
            blocks.append(b)
            if F.PING in b:
                break
        await gen.aclose()
        assert ("", {"t": "ping"}) in [(e or "", d) for e, d in parse(blocks)]  # EventSource chỉ phát message khi có `data:`
    finally:
        await f.close()


async def test_act_item_reaches_tab_watching_other_conversation_of_same_workstream():
    src = FakeSource()
    src.runs["r1"] = {"id": "r1", "run_opts": {"manager_conversation": "cB", "manager_workstream": "ws1"}}
    src.runs["r2"] = {"id": "r2", "run_opts": {"manager_conversation": "cX", "manager_workstream": "ws2"}}
    f = F.Feed(src.call, src.open_sub)
    await f.ensure()
    try:
        f.conv_ws.update({"cA": "ws1", "cB": "ws1", "cX": "ws2"})
        w = f.watch("tab1", ["cA"], [])  # tab chỉ mở hội thoại cA của luồng ws1
        assert "ws1" in w.side and "ws1" not in w.ws
        gen = F.stream(f, w, F.parse_id(f.cursor), heartbeat_s=30)
        src.ev("message.delta", {"text": "của cB"}, run="r1")  # msg.* của hội thoại khác: KHÔNG tới
        src.ev("manager.action", {"tool": "agent.create", "summary": "Tạo agent", "audit_id": 7, "conversation_id": "cB"}, run="r1")
        src.ev("manager.action", {"tool": "agent.create", "summary": "Luồng khác", "audit_id": 8, "conversation_id": "cX"}, run="r2")
        src.ev("manager.action", {"tool": "agent.create", "summary": "Mốc", "audit_id": 9, "conversation_id": "cB"}, run="r1")
        got = await take(gen, 2, pred=lambda d: d["t"] == "act.item")
        await gen.aclose()
        items = [d for _, d in got]
        assert [d["item"]["audit_id"] for d in items if d["t"] == "act.item"] == [7, 9]  # 8 (luồng ws2) không tới
        assert not any(d["t"] == "msg.delta" for d in items)
        # đặt lại phạm vi (op=set) dọn cả phần side
        f.watch("tab1", [], [], replace=True)
        assert not w.side
    finally:
        await f.close()


def test_jsmin_postfix_increment_then_division(tmp_path):
    out = jsmin.minify("let a = i++ / n;\nlet b = j-- / 2;\nreturn /x\\/y/.test(s);\n")
    assert out == "let a=i++/n;\nlet b=j--/2;\nreturn/x\\/y/.test(s);\n", out
    node = shutil.which("node")
    if node:
        js = tmp_path / "x.mjs"
        js.write_text("function f(i,n,j,s){" + out + "}\n", encoding="utf-8")
        r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
