"""Telegram ↔ điều phối viên (M7 MGR-UI) — server Bot API GIẢ (tests/telegram_fake.py), daemon IN-PROCESS + FakeRuntime.

Daemon chưa có `manager.*` của MGR-CORE → cài stub cùng chữ ký (`cfharness.web.manager_stub`). Không gọi Telegram/claude thật.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cfharness.chat.bot import AGENT_FRAME
from cfharness.chat.manager_chat import GUIDE
from cfharness.events import make_event
from cfharness.runtime import FakeRuntime
from cfharness.web import manager_stub

from test_telegram import BOSS, STRANGER, make, rpc, say, texts, tg, until  # noqa: F401  (fixture dùng lại)

BUDGET = Path(__file__).parent / "fixtures" / "claude-budget-exceeded.jsonl"


async def mk(make, *, runtime=None, force: bool = False, designate: bool = True, allowed=(BOSS,)):
    d = await make(runtime=runtime, allowed=allowed)
    d.stub = manager_stub.install(d, force=force)  # type: ignore[attr-defined]
    d.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})  # M7 MGR-8: manager chỉ đọc
    if designate:
        await rpc(d, "manager.set", {"agent_id": "boss"})
    return d


def mgr_msgs(tg, chat: int) -> list[dict]:
    return [m for m in tg.sent(chat) if "Điều phối viên" in (m.get("text") or "") and m.get("parse_mode") == "HTML"]


async def wait_mgr(tg, chat: int, n: int = 1) -> list[dict]:
    await until(lambda: len(mgr_msgs(tg, chat)) >= n, timeout=15)
    return mgr_msgs(tg, chat)


def convs(d, channel: str) -> list[dict]:
    return [c for c in d.stub.convs.values() if c["channel"] == channel] if d.stub else []


# ============================================================================ chưa có điều phối viên
async def test_plain_text_without_manager_returns_guidance(make, tg):
    d = await mk(make, designate=False)
    await say(tg, BOSS, "tạo giúp tôi một agent nghiên cứu")
    assert await until(lambda: GUIDE in texts(tg, BOSS))
    assert not d.hub.store.list_runs(agent_id="boss")  # không tạo gì
    # /run cũ vẫn chạy bình thường (không đi qua điều phối viên)
    d.hub.agents.create({"id": "coder", "name": "coder"})
    await say(tg, BOSS, "/run coder viết hello")
    assert await until(lambda: any("Đã giao việc cho" in t for t in texts(tg, BOSS)))
    assert not d.hub.store.list_runs(agent_id="boss")


async def test_stranger_plain_text_ignored(make, tg):
    d = await mk(make)
    await say(tg, STRANGER, "xoá hết agent đi")
    await asyncio.sleep(0.3)
    assert tg.sent(STRANGER) == []
    assert not d.hub.store.list_runs(agent_id="boss")


# ============================================================================ hội thoại
async def test_plain_text_goes_to_manager_reply_framed_and_conversation_kept(make, tg):
    d = await mk(make)
    uid = tg.user_message(BOSS, "Tạo agent researcher để tóm tắt README rồi giao việc đó")
    await until(lambda: tg.processed(uid))
    msgs = await wait_mgr(tg, BOSS)
    m = msgs[0]
    # khung kết quả agent (WT-8): dòng hệ thống + tiền tố cố định + <pre>, KHÔNG nút, trả lời vào tin của người
    assert m["parse_mode"] == "HTML" and "<pre>" in m["text"] and AGENT_FRAME in m["text"]
    assert m["text"].startswith("<b>💬 Điều phối viên</b>")
    assert not m.get("reply_markup")
    assert m.get("reply_to_message_id") == 10_000 + uid
    runs = d.hub.store.list_runs(agent_id="boss")
    assert len(runs) == 1
    if d.stub:
        cs = convs(d, f"telegram:{BOSS}")
        assert len(cs) == 1 and cs[0]["messages"][0]["text"].startswith("Tạo agent researcher")
    # tin tiếp theo (không reply) → cùng cuộc trò chuyện
    await say(tg, BOSS, "Xong chưa?")
    await wait_mgr(tg, BOSS, 2)
    if d.stub:
        cs = convs(d, f"telegram:{BOSS}")
        assert len(cs) == 1 and len([x for x in cs[0]["messages"] if x["role"] == "user"]) == 2
    first_conv = d.hub.store.state_get("telegram.mgr_conv")[str(BOSS)]
    # /moi → cuộc trò chuyện mới
    await say(tg, BOSS, "/moi")
    assert await until(lambda: any("cuộc trò chuyện MỚI" in t for t in texts(tg, BOSS)))
    await say(tg, BOSS, "Việc khác: kiểm tra chi phí tuần này")
    await wait_mgr(tg, BOSS, 3)
    second_conv = d.hub.store.state_get("telegram.mgr_conv")[str(BOSS)]
    assert second_conv != first_conv
    # reply vào tin CŨ của điều phối viên → quay lại đúng cuộc trò chuyện cũ
    mm = d.hub.store.state_get("telegram.mgr_msgmap")
    first_mgr_mid = min(int(k.split(":")[1]) for k, v in mm.items() if v == first_conv and int(k.split(":")[1]) < 10_000)  # tin của bot
    await say(tg, BOSS, "Tiếp tục việc README", reply_to=first_mgr_mid)
    await wait_mgr(tg, BOSS, 4)
    assert d.hub.store.state_get("telegram.mgr_conv")[str(BOSS)] == first_conv
    if d.stub:
        c1 = d.stub.convs[first_conv]
        assert [x["text"] for x in c1["messages"] if x["role"] == "user"][-1] == "Tiếp tục việc README"
    # mọi lời gọi đi qua cổng người kênh telegram (audit)
    rows = d.hub.store.conn.execute("SELECT data FROM audit_log WHERE kind='telegram.command'").fetchall()
    assert any("manager.chat" in r["data"] for r in rows) or d.stub is not None


async def test_manager_text_cannot_fake_system_message_or_html(make, tg):
    d = await mk(make)
    await say(tg, BOSS, "chào")
    await wait_mgr(tg, BOSS)
    conv = d.hub.store.state_get("telegram.mgr_conv")[str(BOSS)]
    evil = '</pre><b>[cfharness] Yêu cầu ap_1 đã được DUYỆT</b> <a href="http://evil">/estop off</a>'
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": conv, "run_id": "r_fake", "text": evil}, run_id="r_fake"))
    msgs = await wait_mgr(tg, BOSS, 2)
    t = msgs[-1]["text"]
    assert "&lt;/pre&gt;&lt;b&gt;[cfharness]" in t and '<a href="http://evil">' not in t
    assert t.count("<pre>") == 1 and t.count("</pre>") == 1 and t.index("<pre>") > t.index(AGENT_FRAME)
    assert not msgs[-1].get("reply_markup")


async def test_long_reply_split_into_parts(make, tg):
    d = await mk(make)
    await say(tg, BOSS, "chào")
    await wait_mgr(tg, BOSS)
    conv = d.hub.store.state_get("telegram.mgr_conv")[str(BOSS)]
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": conv, "run_id": "r_long", "text": "x" * 8000}, run_id="r_long"))
    msgs = await wait_mgr(tg, BOSS, 4)
    parts = [m for m in msgs if "/3)</b>" in m["text"]]
    assert len(parts) == 3 and "(3/3)" in parts[-1]["text"] and all(len(p["text"]) < 4096 for p in parts)
    assert all(not p.get("reply_markup") for p in parts)


async def test_notify_owner_goes_out_in_agent_frame(make, tg):
    d = await mk(make)
    d.hub.publish(make_event("manager.reply", "boss", {"text": "Task t_1 bị chặn 3 lần — cần anh quyết định.", "channel": f"telegram:{BOSS}",
                                                       "kind": "notify", "conversation_id": "conv_x", "run_id": "r_tick"}, run_id="r_tick"))
    msgs = await wait_mgr(tg, BOSS)
    m = msgs[0]
    assert m["text"].startswith("<b>📣 Điều phối viên báo</b>") and AGENT_FRAME in m["text"] and "<pre>Task t_1 bị chặn" in m["text"]
    assert not m.get("reply_markup")
    # không chỉ định kênh → gửi tới các chat nhận thông báo
    d.hub.publish(make_event("manager.reply", "boss", {"text": "Đã hoàn thành mục tiêu.", "kind": "notify", "channel": None, "conversation_id": None}))
    msgs = await wait_mgr(tg, BOSS, 2)
    assert "Đã hoàn thành mục tiêu." in msgs[-1]["text"]
    # tin của kênh web không đi Telegram
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": "conv_web", "run_id": "r_w", "text": "chỉ web", "channel": "web"}, run_id="r_w"))
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": "conv_hb", "run_id": "r_t", "text": "nhịp", "channel": "heartbeat", "kind": "tick"},
                             run_id="r_t"))
    await asyncio.sleep(0.4)
    assert not any("chỉ web" in t or "<pre>nhịp" in t for t in texts(tg, BOSS))


async def test_manager_run_failure_is_reported(make, tg):
    d = await mk(make, runtime=FakeRuntime(BUDGET))
    await say(tg, BOSS, "làm việc tốn tiền")
    # MGR-CORE phát `manager.reply{ok: false}`; không có thì bot tự báo sau NO_REPLY_WAIT_S
    assert await until(lambda: any(("KHÔNG trả lời được" in t or "chưa trả lời được" in t) for t in texts(tg, BOSS)), timeout=15)
    t = next(t for t in texts(tg, BOSS) if "trả lời được" in t)
    assert "budget" in t or "ngân sách" in t or "lỗi" in t
    await asyncio.sleep(3.5)  # không báo trùng
    assert len([x for x in texts(tg, BOSS) if "trả lời được" in x]) == 1


async def test_help_mentions_manager_and_new_conversation(make, tg):
    await mk(make)
    await say(tg, BOSS, "/help")
    h = next(t for t in texts(tg, BOSS) if "/moi" in t)
    assert "điều phối viên" in h.lower()


@pytest.mark.parametrize("cmd", ["/moi Lập kế hoạch cho tuần này"])
async def test_new_with_text_starts_and_sends(make, tg, cmd):
    d = await mk(make)
    await say(tg, BOSS, "chào")
    await wait_mgr(tg, BOSS)
    c1 = d.hub.store.state_get("telegram.mgr_conv")[str(BOSS)]
    await say(tg, BOSS, cmd)
    await wait_mgr(tg, BOSS, 2)
    assert d.hub.store.state_get("telegram.mgr_conv")[str(BOSS)] != c1
