"""Slack: kết quả LỊCH đăng thẳng vào kênh chung (không vào thread) khi người bật `notify.slack.channel_root_posts`.
Mặc định tắt → kênh chung chỉ nhận trả lời trong thread (lịch không biết thread → DM). Trả lời chat thường vẫn vào thread.
Slack giả (tests/slack_fake.py), daemon in-process + FakeRuntime."""

from __future__ import annotations

import asyncio

from cfharness.events import make_event

from slack_fake import BOT_USER, FakeSlack
from test_slack import BOSS, DM_BOSS, deliver, make, rpc, sk, texts, until  # noqa: F401  (fixture dùng lại)

CH = "C0TEAM0001"
CH2 = "C0TEAM0002"


def roots(sk: FakeSlack, ch: str, needle: str) -> list[dict]:
    return [m for m in sk.sent(ch) if needle in sk.text_of(m) and not m.get("thread_ts")]


async def agent_schedule(d, ch: str) -> dict:
    d.hub.agents.create({"id": "w1", "name": "w1"})
    s = await rpc(d, "schedule.create", {"kind": "agent", "target": "w1", "expr": "daily 08:00", "prompt": "báo cáo hệ thống",
                                         "deliver_to": f"slack:{ch}"})
    await rpc(d, "schedule.run_now", {"id": s["id"]})
    return s


async def test_default_off_schedule_never_posts_channel_root(make, sk):
    d = await make(chats=(DM_BOSS, CH), sl_cfg={"allow_channels": True})
    await agent_schedule(d, CH)
    await until(lambda: any("Kết quả lịch" in t for t in texts(sk, DM_BOSS)), timeout=15)  # không biết thread → DM
    assert not roots(sk, CH, "Kết quả lịch")


async def test_on_schedule_result_posts_channel_root(make, sk):
    d = await make(chats=(DM_BOSS, CH), sl_cfg={"allow_channels": True, "channel_root_posts": True})
    await agent_schedule(d, CH)
    m = (await until(lambda: roots(sk, CH, "Kết quả lịch"), timeout=15))[0]
    assert "Điều phối viên báo" in sk.text_of(m)
    assert not any("Kết quả lịch" in t for t in texts(sk, DM_BOSS))


async def test_on_requires_allow_channels(make, sk):
    d = await make(chats=(DM_BOSS, CH), sl_cfg={"allow_channels": False, "channel_root_posts": True})
    await agent_schedule(d, CH)
    await until(lambda: any("Kết quả lịch" in t for t in texts(sk, DM_BOSS)), timeout=15)
    assert not roots(sk, CH, "Kết quả lịch")


async def test_on_schedule_id_must_match_channel_in_db(make, sk):
    """Payload không được tin: schedule_id lạ / lịch giao kênh KHÁC → không đăng lên gốc kênh."""
    d = await make(chats=(DM_BOSS, CH, CH2), sl_cfg={"allow_channels": True, "channel_root_posts": True})
    d.hub.agents.create({"id": "w1", "name": "w1"})
    other = await rpc(d, "schedule.create", {"kind": "agent", "target": "w1", "expr": "daily 08:00", "prompt": "x",
                                             "deliver_to": f"slack:{CH2}"})
    bot = d.hub.slack
    assert not bot.schedule_root_ok(CH, {"schedule_id": "sc_khongco"})
    assert not bot.schedule_root_ok(CH, {"schedule_id": other["id"]})
    assert not bot.schedule_root_ok(CH, {})
    assert bot.schedule_root_ok(CH2, {"schedule_id": other["id"]})
    assert not bot.schedule_root_ok(DM_BOSS, {"schedule_id": other["id"]})
    d.hub.publish(make_event("manager.reply", None, {"text": "giả mạo lịch", "channel": f"slack:{CH}", "kind": "notify",
                                                     "schedule_id": other["id"]}))
    await asyncio.sleep(0.4)
    assert not roots(sk, CH, "giả mạo lịch")


async def test_on_manager_schedule_reply_posts_root_but_chat_stays_in_thread(make, sk):
    d = await make(chats=(DM_BOSS, CH), sl_cfg={"allow_channels": True, "channel_root_posts": True})
    d.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    # chat thường (tag bot trong kênh) → vẫn trả lời trong thread
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> chào"))
    reply = (await until(lambda: [m for m in sk.sent(CH) if "💬 Điều phối viên" in sk.text_of(m)], timeout=15))[0]
    assert reply.get("thread_ts")
    # lịch của điều phối viên giao về kênh → tin mới ở gốc kênh
    s = await rpc(d, "schedule.create", {"kind": "manager", "expr": "daily 08:30", "prompt": "báo cáo sáng",
                                         "deliver_to": f"slack:{CH}"})
    n0 = len(sk.sent(CH))
    await rpc(d, "schedule.run_now", {"id": s["id"]})
    new = await until(lambda: [m for m in sk.sent(CH)[n0:] if "Điều phối viên" in sk.text_of(m)], timeout=15)
    assert not new[0].get("thread_ts")
