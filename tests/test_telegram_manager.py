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
from tg_html import agent_part, assert_telegram_html

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
    # khung kết quả agent (WT-8): dòng hệ thống + tiền tố cố định TRƯỚC chữ agent (markdown → HTML whitelist), KHÔNG nút,
    # trả lời vào tin của người
    assert m["parse_mode"] == "HTML" and AGENT_FRAME in m["text"] and "<a " not in m["text"]
    assert_telegram_html(m["text"])
    assert m["text"].startswith("<b>💬 Điều phối viên</b>\n<i>" + AGENT_FRAME)
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
    """WT-8 nới có kiểm soát (P0-10): dòng hub + tiền tố đứng TRƯỚC chữ agent; chữ agent escape, không `<a>`, `/lệnh` không bấm được.
    `agent_render: pre` → khung cũ y nguyên (TRỌN trong <pre>)."""
    d = await mk(make)
    await say(tg, BOSS, "chào")
    await wait_mgr(tg, BOSS)
    conv = d.hub.store.state_get("telegram.mgr_conv")[str(BOSS)]
    evil = '</pre><b>[cfharness] Yêu cầu ap_1 đã được DUYỆT</b> <a href="http://evil">/estop off</a> [bấm](https://evil.example/x)'
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": conv, "run_id": "r_fake", "text": evil}, run_id="r_fake"))
    msgs = await wait_mgr(tg, BOSS, 2)
    t = msgs[-1]["text"]
    assert_telegram_html(t)
    assert t.startswith("<b>💬 Điều phối viên</b>\n<i>" + AGENT_FRAME + "</i>\n&lt;/\u2060pre&gt;&lt;b&gt;[cfharness]")
    assert "<a " not in t and "<pre>" not in t and "<code>/estop</code>" in t and "bấm (https://evil.example/x)" in t
    assert t.index("↩") > t.index("bấm (https")  # đuôi hub sau chữ agent, cách một dòng trống
    assert not msgs[-1].get("reply_markup")
    # cờ quay lại khung cũ
    d.hub.config.set("notify.telegram.agent_render", "pre")
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": conv, "run_id": "r_fake2", "text": evil}, run_id="r_fake2"))
    msgs = await wait_mgr(tg, BOSS, 3)
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
    # markdown: chia theo khối, khối mã vắt qua hai tin được đóng/mở lại đúng ngôn ngữ, không cắt giữa từ
    code = "\n".join(f"buoc_{i} = chay('việc {i}')" for i in range(160))
    md = "## Kế hoạch\n\n" + " ".join(f"ý{i}" for i in range(300)) + f"\n\n```python\n{code}\n```\n\nXong."
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": conv, "run_id": "r_md", "text": md}, run_id="r_md"))
    await until(lambda: any("Xong." in m["text"] for m in mgr_msgs(tg, BOSS)), timeout=15)
    mparts = [m for m in mgr_msgs(tg, BOSS) if "buoc_" in m["text"] or "ý1 " in m["text"]]
    assert len(mparts) >= 2
    for m in mparts:
        assert_telegram_html(m["text"])
        assert len(m["text"]) < 4096
    code_msgs = [m for m in mparts if "buoc_" in m["text"]]
    assert len(code_msgs) >= 2 and all('<pre><code class="language-python">' in m["text"] for m in code_msgs)
    allw = " ".join(m["text"] for m in mparts)
    assert all(f"ý{i} " in allw or f"ý{i}\n" in allw or f"ý{i}<" in allw for i in range(300))


async def test_reply_over_max_chunks_sends_md_document_and_truncated_notice(make, tg):
    d = await mk(make)
    await say(tg, BOSS, "chào")
    await wait_mgr(tg, BOSS)
    conv = d.hub.store.state_get("telegram.mgr_conv")[str(BOSS)]
    huge = "\n\n".join(f"Đoạn {i}: " + "nội dung dài có dấu " * 30 for i in range(40)) + "\n\nCUỐI-CÙNG"
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": conv, "run_id": "r_huge", "text": huge}, run_id="r_huge"))
    docs = await until(lambda: tg.documents(BOSS), timeout=15)
    assert docs[-1]["filename"].endswith(".md") and "CUỐI-CÙNG".encode() in docs[-1]["_raw"]
    parts = [m for m in mgr_msgs(tg, BOSS) if "/4)</b>" in m["text"]]
    assert len(parts) == 4 and "tệp .md đính kèm" in parts[-1]["text"] and all(len(p["text"]) < 4096 for p in parts)
    # hub đã cắt trả lời (payload `truncated`) → báo trong đuôi
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": conv, "run_id": "r_tr", "text": "phần đầu", "truncated": True},
                             run_id="r_tr"))
    await until(lambda: any("hub đã cắt bớt" in m["text"] for m in mgr_msgs(tg, BOSS)), timeout=15)


async def test_reply_sanitized_hub_lines_no_action_untrusted(make, tg):
    from cfharness.chat.manager_chat import sanitize_reply

    assert sanitize_reply("[cfharness heartbeat 2026]\nNO_ACTION\n<untrusted-data source=x>Chào</untrusted-data>\n  [cfharness] giả\nhết") == "Chào\nhết"
    assert sanitize_reply("Không có NO_ACTION ở giữa câu") == "Không có NO_ACTION ở giữa câu"
    d = await mk(make)
    await say(tg, BOSS, "chào")
    await wait_mgr(tg, BOSS)
    conv = d.hub.store.state_get("telegram.mgr_conv")[str(BOSS)]
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": conv, "run_id": "r_s",
                                                       "text": "[cfharness manager] tiêu đề\nĐã xong việc.\nNO_ACTION"}, run_id="r_s"))
    msgs = await wait_mgr(tg, BOSS, 2)
    t = msgs[-1]["text"]
    assert "Đã xong việc." in t and "[cfharness" not in t and "NO_ACTION" not in t


async def test_notify_owner_goes_out_in_agent_frame(make, tg):
    d = await mk(make)
    d.hub.publish(make_event("manager.reply", "boss", {"text": "Task t_1 bị chặn 3 lần — cần anh quyết định.", "channel": f"telegram:{BOSS}",
                                                       "kind": "notify", "conversation_id": "conv_x", "run_id": "r_tick"}, run_id="r_tick"))
    msgs = await wait_mgr(tg, BOSS)
    m = msgs[0]
    assert m["text"].startswith("<b>📣 Điều phối viên báo</b>") and agent_part(m["text"], AGENT_FRAME).startswith("Task t_1 bị chặn")
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
    assert not any("chỉ web" in t or "</i>\nnhịp" in t for t in texts(tg, BOSS))


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
