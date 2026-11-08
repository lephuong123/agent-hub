"""Sửa theo đợt rà soát bot Slack 2 chiều (docs/dev-notes/slack.md § "Rà soát 2026-11"): định tuyến cuộc trò chuyện trong thread sau
khởi động lại / thread chưa gắn cuộc, cắt section trích dẫn, ngữ cảnh thread dài, 429 khi gửi trả lời, thread của tag gốc kênh,
`thread_broadcast`, nhãn tin chuyển tiếp, cổng slash command, tin thử `notify slack test`. Slack giả (tests/slack_fake.py), daemon
in-process — không gọi Slack/claude thật."""

from __future__ import annotations

import asyncio

from cfharness.chat.base import _ROOT_POST
from cfharness.chat.slack import render
from cfharness.events import make_event
from cfharness.runtime import FakeRuntime

from slack_fake import BOT_USER, FakeSlack
from test_slack import BOSS, DM_BOSS, OTHER, STRANGER, deliver, make, rpc, sk, until  # noqa: F401  (fixture dùng lại)
from test_slack_threads import CH, boss_runs, conv_of, ids, mk, owner_text, replies


def mgr_conv(d) -> str | None:
    return (d.hub.store.state_get("slack.mgr_conv") or {}).get(CH)


def restart_ram(bot) -> None:
    """Như daemon vừa khởi động lại: RAM thread mất, `slack.threads` (hub_state) còn."""
    bot._thread.clear()
    bot._roots.clear()
    bot._chan_threads = None


# ---------------------------------------------------------------- 1. khởi động lại + tag trong thread → đúng cuộc của thread
async def test_restart_then_tag_in_thread_continues_thread_conversation(make, sk):
    d = await mk(make, sk)
    T = sk.thread_seed(CH, [(OTHER, "ai review PR này giúp?")])
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> xem PR", thread_ts=T))
    await until(lambda: replies(sk, T), timeout=15)
    conv1 = conv_of(d)
    before = ids(d)
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> việc khác hẳn"))  # tag gốc kênh → cuộc khác thành "hiện tại"
    await until(lambda: ids(d) - before)
    await until(lambda: conv_of(d) and conv_of(d) != conv1)
    restart_ram(d.hub.slack)
    before = ids(d)
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> tiếp PR đó", thread_ts=T))
    await until(lambda: ids(d) - before)
    assert mgr_conv(d) == conv1  # trước đây: RAM chỉ còn tin tag mới → bỏ qua tin bot đã lưu → cuộc hiện tại của kênh (sai)


def test_candidates_merge_ram_and_saved(make):  # noqa: ARG001
    from cfharness.chat.slack.bot import SlackBot

    b = SlackBot.__new__(SlackBot)
    b._thread = {"100.1": ["300.1"]}
    b._chan_threads = {f"{CH}:100.1": ["100.1", "150.1", "200.1"]}
    assert b._candidates("100.1", CH) == ["300.1", "200.1", "150.1", "100.1"]  # gộp, bỏ trùng, mới nhất trước, gốc cuối


# ---------------------------------------------------------------- 2. thread bot đã vào nhưng chưa gắn cuộc → cuộc MỚI
async def test_tag_in_bot_thread_without_conversation_opens_new(make, sk):
    d = await mk(make, sk)
    bot = d.hub.slack
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> làm báo cáo tuần"))
    await until(lambda: boss_runs(d))
    await until(lambda: conv_of(d))
    convA = conv_of(d)
    tok = _ROOT_POST.set(True)  # như kết quả lịch đăng ở gốc kênh (`channel_root_posts`) — không gắn cuộc nào
    try:
        R = (await bot._send(bot.api(), CH, "Kết quả lịch: ..."))["message_id"]
    finally:
        _ROOT_POST.reset(tok)
    assert bot._in_thread(CH, R)
    before = ids(d)
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> giải thích kết quả này", thread_ts=R))
    await until(lambda: ids(d) - before)
    assert conv_of(d) != convA  # trước đây: lẫn vào cuộc hiện tại của kênh (của thread khác)


async def test_untagged_followup_in_bot_thread_without_conversation_opens_new(make, sk):
    d = await mk(make, sk)
    bot = d.hub.slack
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> làm báo cáo tuần"))
    await until(lambda: conv_of(d))
    convA = conv_of(d)
    tok = _ROOT_POST.set(True)
    try:
        R = (await bot._send(bot.api(), CH, "Kết quả lịch: ..."))["message_id"]
    finally:
        _ROOT_POST.reset(tok)
    bot._follow(CH, str(R), BOSS)
    before = ids(d)
    await deliver(sk, sk.channel_message(CH, BOSS, "sao số này thấp vậy", thread_ts=R))
    await until(lambda: ids(d) - before)
    assert conv_of(d) != convA


# ---------------------------------------------------------------- 3. cắt section: trích dẫn giữ `> `, không cắt giữa thực thể / <…>
def test_split_quoted_long_line_keeps_quote_prefix():
    body = "> " + "chữ agent " * 700  # một dòng trích dẫn rất dài (trả lời điều phối viên không xuống dòng)
    parts = render.split_sections(body, limit=500)
    assert len(parts) > 3
    for p in parts:
        for line in p.split("\n"):
            assert line.startswith(">"), line[:40]  # không phần nào thoát khỏi trích dẫn (WT-8)
    assert all(len(p) <= 500 for p in parts)


def test_split_never_cuts_inside_entity_or_link():
    unit = "&amp;lt;x&gt; <https://example.com/a/b/c|nhãn dài có dấu cách> "
    body = "> " + unit * 200
    parts = render.split_sections(body, limit=300)
    assert len(parts) > 5
    for p in parts:
        for line in p.split("\n"):
            assert line.startswith("> ")
            s = line[2:]
            assert not render._OPEN_ENT.search(s), s[-10:]  # không có thực thể dang dở ở cuối phần
            assert s.count("<") == s.count(">"), s  # token `<…>` không bị cắt đôi


def test_split_plain_long_line_prefers_spaces():
    body = " ".join(["từ"] * 2000)
    parts = render.split_sections(body, limit=400)
    assert len(parts) > 3
    assert " ".join(parts).split() == body.split()  # không mất chữ


# ---------------------------------------------------------------- 4. thread dài: tin MỚI nhất (ngay trước tin tag) phải có
async def test_long_thread_context_includes_newest(make, sk):
    d = await mk(make, sk, ctx=True)
    root = sk.thread_seed(CH, [(OTHER, f"tin số {i:02d}") for i in range(260)] + [(OTHER, "ĐỀ XUẤT MỚI NHẤT: chuyển sang thứ Hai")])
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> làm theo đề xuất trên", thread_ts=root))
    p = owner_text((await until(lambda: boss_runs(d)))[0]["prompt"])
    assert "ĐỀ XUẤT MỚI NHẤT" in p
    assert "tin số 259" in p
    assert sk.replies_calls >= 2  # đã theo `next_cursor`


async def test_known_thread_context_uses_oldest_floor(make, sk):
    d = await mk(make, sk, ctx=True)
    root = sk.thread_seed(CH, [(OTHER, "gốc")])
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> xem", thread_ts=root))
    await until(lambda: replies(sk, root), timeout=15)
    for i in range(5):
        sk._log(CH, {"type": "message", "user": OTHER, "text": f"góp ý {i}", "ts": sk._ts(), "thread_ts": root})
    before = ids(d)
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> tổng hợp góp ý", thread_ts=root))
    new = await until(lambda: [r for r in boss_runs(d) if r["id"] not in before])
    p = owner_text(new[0]["prompt"])
    assert "góp ý 0" in p and "góp ý 4" in p
    last = [c for m, c in sk.calls if m == "conversations.replies"][-1]
    assert last.get("oldest")  # chỉ đọc phần sau tin bot đã thấy


# ---------------------------------------------------------------- 5. 429 khi gửi trả lời → thử lại; ✅ chỉ sau khi gửi được
async def _held_run(make, sk):
    from cfharness.web import manager_stub

    d = await make(chats=(DM_BOSS, CH), sl_cfg={"allow_channels": True, "thread_context": False}, runtime=FakeRuntime(hold=True))
    manager_stub.install(d)
    d.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    slept: list[float] = []

    async def fast(s: float) -> None:
        slept.append(s)

    d.hub.slack._sleep = fast
    eid = sk.mention(CH, BOSS, f"<@{BOT_USER}> làm báo cáo tuần")
    await deliver(sk, eid)
    run = (await until(lambda: boss_runs(d)))[0]
    tag_ts = [m for m in sk.history[CH] if "báo cáo tuần" in str(m.get("text"))][0]["ts"]
    await until(lambda: [c for c in sk.calls_of("reactions.add") if c.get("name") == "hourglass_flowing_sand"], timeout=10)
    return d, run, tag_ts, slept


def _reply(d, run) -> None:
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": conv_of(d), "run_id": run["id"], "text": "Báo cáo xong.",
                                                       "channel": f"slack:{CH}", "kind": "reply", "ok": True}, run_id=run["id"]))


def _idx(sk: FakeSlack, pred) -> int | None:
    with sk.cond:
        for i, (m, p) in enumerate(sk.calls):
            if pred(m, p):
                return i
    return None


async def test_reply_retried_after_429_and_done_icon_after_send(make, sk):
    d, run, tag_ts, slept = await _held_run(make, sk)
    sk.ratelimit["chat.postMessage"] = 1
    _reply(d, run)
    got = await until(lambda: [m for m in sk.sent(CH) if "Báo cáo xong" in sk.text_of(m)], timeout=15)
    assert got[0]["thread_ts"] == tag_ts and slept == [1.0]
    await until(lambda: [c for c in sk.calls_of("reactions.add") if c.get("name") == "white_check_mark"], timeout=10)
    sent_i = _idx(sk, lambda m, p: m == "chat.postMessage" and "Báo cáo xong" in sk.text_of(p))
    done_i = _idx(sk, lambda m, p: m == "reactions.add" and p.get("name") == "white_check_mark")
    assert sent_i is not None and done_i is not None and sent_i < done_i


async def test_reply_send_fails_marks_failed_not_done(make, sk):
    d, run, _tag, slept = await _held_run(make, sk)
    sk.ratelimit["chat.postMessage"] = 10
    _reply(d, run)
    await until(lambda: [c for c in sk.calls_of("reactions.add") if c.get("name") == "x"], timeout=15)
    assert not [m for m in sk.sent(CH) if "Báo cáo xong" in sk.text_of(m)]
    assert not [c for c in sk.calls_of("reactions.add") if c.get("name") == "white_check_mark"]
    assert len(slept) == 2  # thử lại 2 lần rồi thôi
    sk.ratelimit.clear()


async def test_send_does_not_wait_for_long_retry_after(make, sk):
    from cfharness.chat.slack.api import SlackError

    d = await mk(make, sk)
    bot = d.hub.slack
    calls = []

    class Api:
        async def call(self, method, params=None, **kw):
            calls.append(method)
            raise SlackError("429", code="ratelimited", retry_after=600)

    try:
        await bot._post(Api(), {"channel": DM_BOSS, "text": "x"})
        raise AssertionError("phải ném lỗi")
    except SlackError:
        pass
    assert calls == ["chat.postMessage"]


# ---------------------------------------------------------------- 6. tag gốc kênh → thread được nhớ ngay (trước tin đầu của bot)
async def test_root_tag_registers_thread_before_bot_posts(make, sk):
    d = await mk(make, sk)
    bot = d.hub.slack
    calls = []

    async def fake_handle(cid, text, api, **kw):
        calls.append(kw)

    bot.manager.handle_text = fake_handle  # run đã nhận nhưng bot chưa đăng gì (manager_progress=false)
    ev = {"type": "app_mention", "channel": CH, "user": BOSS, "text": f"<@{BOT_USER}> làm báo cáo", "ts": "1700.000100"}
    await bot._on_mention(ev, bot.cfg().get("team_id"), bot.api())
    assert calls and bot._follows(CH, "1700.000100", BOSS)
    assert bot._in_thread(CH, "1700.000100")
    assert f"{CH}:1700.000100" in (d.hub.store.state_get("slack.threads") or {})  # sống qua khởi động lại


async def test_quiet_mode_followup_before_first_reply_same_conversation(make, sk):
    from cfharness.web import manager_stub

    d = await make(chats=(DM_BOSS,), users=(BOSS,), runtime=FakeRuntime(hold=True),
                   sl_cfg={"allow_channels": True, "thread_context": False, "thread_followup": True, "manager_progress": False})
    manager_stub.install(d)
    d.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> làm báo cáo tuần"))
    await until(lambda: conv_of(d))
    conv1 = conv_of(d)
    root = [m for m in sk.history[CH] if "báo cáo tuần" in str(m.get("text"))][0]["ts"]
    assert not [m for m in sk.sent(CH) if m.get("thread_ts") == root and "Điều phối viên" in sk.text_of(m)]  # bot chưa nói gì
    n = len(d.hub.store.conn.execute("SELECT 1 FROM audit_log WHERE kind='manager.chat'").fetchall())
    await deliver(sk, sk.channel_message(CH, BOSS, "thêm phần chi phí", thread_ts=root))
    await until(lambda: len(d.hub.store.conn.execute("SELECT 1 FROM audit_log WHERE kind='manager.chat'").fetchall()) > n)
    assert conv_of(d) == conv1


# ---------------------------------------------------------------- 7. thread_broadcast ("gửi cả vào kênh") được nhận
async def test_thread_broadcast_followup_accepted(make, sk):
    d = await mk(make, sk)
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> làm báo cáo tuần"))
    root = (await until(lambda: [m for m in sk.sent(CH) if m.get("thread_ts")], timeout=15))[0]["thread_ts"]
    await until(lambda: replies(sk, root), timeout=15)
    conv1 = conv_of(d)
    before = ids(d)
    ts = sk._ts()
    ev = {"type": "message", "subtype": "thread_broadcast", "channel": CH, "user": BOSS, "text": "thêm phần chi phí", "ts": ts,
          "thread_ts": root, "channel_type": "channel"}
    await deliver(sk, sk._push("events_api", {"team_id": sk.team, "event": ev, "event_id": f"Ev{ts.replace('.', '')}"}))
    await until(lambda: ids(d) - before)
    assert conv_of(d) == conv1


async def test_thread_broadcast_mention_accepted(make, sk):
    d = await mk(make, sk)
    ts = sk._ts()
    root = sk.thread_seed(CH, [(OTHER, "PR mới")])
    ev = {"type": "app_mention", "subtype": "thread_broadcast", "channel": CH, "user": BOSS, "text": f"<@{BOT_USER}> xem giúp",
          "ts": ts, "thread_ts": root}
    await deliver(sk, sk._push("events_api", {"team_id": sk.team, "event": ev, "event_id": f"Ev{ts.replace('.', '')}"}))
    await until(lambda: boss_runs(d))


# ---------------------------------------------------------------- 8. tin chuyển tiếp + tag lần đầu trong thread lạ → nhãn đúng
async def test_forwarded_first_tag_in_unknown_thread_label_from_raw_text(make, sk):
    d = await mk(make, sk, ctx=True)
    root = sk.thread_seed(CH, [(OTHER, "ai xem lỗi này với")])
    ts = sk._ts()
    att = [{"is_share": True, "text": "Lỗi 500 ở trang thanh toán", "author_id": OTHER, "author_name": "An"}]
    ev = {"type": "app_mention", "channel": CH, "user": BOSS, "text": f"<@{BOT_USER}> xử lý cái này", "ts": ts, "thread_ts": root,
          "attachments": att}
    sk._log(CH, {"type": "message", "user": BOSS, "text": ev["text"], "ts": ts, "thread_ts": root})
    await deliver(sk, sk._push("events_api", {"team_id": sk.team, "event": ev, "event_id": f"Ev{ts.replace('.', '')}"}))
    p = owner_text((await until(lambda: boss_runs(d)))[0]["prompt"])
    assert "Lỗi 500" in p and "ai xem lỗi này với" in p
    assert "Yêu cầu: Ngữ cảnh thread" not in p and "Yêu cầu: " not in p.replace("Yêu cầu (tin tag bot): xử lý cái này", "")
    assert "Yêu cầu (tin tag bot): xử lý cái này" in p


# ---------------------------------------------------------------- 9. slash từ người lạ: không conversations.open / ghi state
async def test_slash_stranger_rejected_before_dm_open(make, sk):
    d = await mk(make, sk)
    await deliver(sk, sk.slash(STRANGER, "trangthai"))
    assert not sk.calls_of("conversations.open")
    assert STRANGER not in (d.hub.store.state_get("slack.dms") or {})
    rows = d.hub.store.conn.execute("SELECT data FROM audit_log WHERE kind='slack.ignored'").fetchall()
    assert rows


async def test_slash_allowed_user_still_works(make, sk):
    d = await mk(make, sk)  # noqa: F841
    await deliver(sk, sk.slash(BOSS, "trangthai"))
    await until(lambda: sk.sent(DM_BOSS), timeout=10)


# ---------------------------------------------------------------- 10. `notify slack test` vẫn tới kênh chung (ở gốc kênh)
async def test_send_test_posts_to_channel_root(make, sk):
    d = await make(chats=(DM_BOSS, CH), sl_cfg={"allow_channels": True})
    r = await rpc(d, "slack.test", {"text": "thử kênh"})
    assert r["sent"] and r["chats"] == 2
    roots = [m for m in sk.sent(CH) if not m.get("thread_ts") and "thử kênh" in sk.text_of(m)]
    assert len(roots) == 1
    assert sk.sent(DM_BOSS)
    assert not _ROOT_POST.get()  # không rò quyền đăng gốc kênh sang việc khác
