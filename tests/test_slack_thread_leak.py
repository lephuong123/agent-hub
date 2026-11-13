"""Slack kênh chung: bot CHỈ nói trong thread đã tag nó — không bao giờ đăng lên gốc kênh (trừ kết quả lịch khi người bật
`channel_root_posts`, tests/test_slack_schedule_root.py). Lỗi người dùng báo: "tag bot trong thread mà bot lại trả lời ra ngoài channel".
Nguồn rò: trả lời dài nhiều phần (phần 2+ mất `reply_to`), báo lỗi/hướng dẫn không có `reply_to`, tệp `[[file:]]` + báo lỗi tệp,
`run.continue` từ thread (xác nhận + kết quả run). Slack giả (tests/slack_fake.py), daemon in-process."""

from __future__ import annotations

from cfharness.chat.base import ChatError
from cfharness.events import make_event
from cfharness.notify.telegram import CHUNK
from cfharness.runtime import FakeRuntime

from slack_fake import BOT_USER, FakeSlack
from test_slack import BOSS, DM_BOSS, deliver, make, rpc, sk, until  # noqa: F401  (fixture dùng lại)

CH = "C0TEAM0001"


async def mk(make, *, manager: bool = True, runtime=None):
    from cfharness.web import manager_stub

    d = await make(chats=(DM_BOSS, CH), sl_cfg={"allow_channels": True, "thread_context": False}, runtime=runtime)
    manager_stub.install(d)
    if manager:
        d.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
        await rpc(d, "manager.set", {"agent_id": "boss"})
    return d


def root_posts(sk: FakeSlack) -> list[dict]:
    return [m for m in sk.sent(CH) if not m.get("thread_ts")]


def seed_thread(sk: FakeSlack) -> str:
    """Thread người tạo trong kênh (bot chưa tham gia) — ts gốc."""
    return sk.thread_seed(CH, [(BOSS, "Có lỗi đăng nhập ở trang quản trị"), ("U0OTHER01", "mình cũng gặp")])


async def tag_in_thread(sk: FakeSlack, root: str, text: str = "xem giúp") -> None:
    await deliver(sk, sk.mention(CH, BOSS, f"<@{BOT_USER}> {text}", thread_ts=root))


def conv_of(d) -> str:
    return (d.hub.store.state_get("slack.mgr_conv") or {})[CH]


async def test_long_reply_all_chunks_stay_in_thread(make, sk):
    """Trả lời dài (nhiều phần) của run đang chạy → MỌI phần trong thread đã tag (trước đây phần 2+ lên gốc kênh)."""
    d = await mk(make, runtime=FakeRuntime(hold=True))
    root = seed_thread(sk)
    await tag_in_thread(sk, root)
    run = (await until(lambda: d.hub.store.list_runs(agent_id="boss")))[0]
    long = "\n".join(f"dòng {i} " + "x" * 80 for i in range(3 * CHUNK // 80))
    n = len(sk.sent(CH))
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": conv_of(d), "run_id": run["id"], "text": long,
                                                       "channel": f"slack:{CH}", "kind": "reply", "ok": True}, run_id=run["id"]))
    parts = await until(lambda: [m for m in sk.sent(CH)[n:] if "Điều phối viên (" in sk.text_of(m)]
                        if sum("Điều phối viên (" in sk.text_of(m) for m in sk.sent(CH)[n:]) >= 3 else None, timeout=15)
    assert len(parts) >= 3
    assert all(m.get("thread_ts") == root for m in sk.sent(CH)), [m.get("thread_ts") for m in sk.sent(CH)]
    assert not root_posts(sk)


async def test_long_reply_without_reply_to_stays_in_conversation_thread(make, sk):
    """Run tiếp tục (không có reply_to, đi qua `mgr_convthread`) + trả lời dài → mọi phần vào thread của cuộc."""
    d = await mk(make)
    root = seed_thread(sk)
    await tag_in_thread(sk, root)
    await until(lambda: [m for m in sk.sent(CH) if "Điều phối viên" in sk.text_of(m)], timeout=15)
    long = "y" * (2 * CHUNK + 50)
    n = len(sk.sent(CH))
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": conv_of(d), "run_id": "r_resumed0009", "text": long,
                                                       "channel": f"slack:{CH}", "kind": "reply", "ok": True}, run_id="r_resumed0009"))
    await until(lambda: len(sk.sent(CH)) >= n + 3, timeout=15)
    assert all(m.get("thread_ts") == root for m in sk.sent(CH))


async def test_no_manager_guide_goes_to_thread(make, sk):
    """Chưa chỉ định điều phối viên → hướng dẫn trả vào thread đã tag, không lên gốc kênh."""
    await mk(make, manager=False)
    root = seed_thread(sk)
    await tag_in_thread(sk, root)
    m = (await until(lambda: [m for m in sk.sent(CH) if "Chưa có điều phối viên" in sk.text_of(m)], timeout=10))[0]
    assert m.get("thread_ts") == root
    assert not root_posts(sk)


async def test_tag_at_channel_root_error_goes_to_new_thread(make, sk):
    """Tag ở gốc kênh + lỗi `manager.chat` → báo lỗi trong thread của chính tin tag (không phải tin mới ở gốc kênh)."""
    d = await mk(make)
    real = d.hub.slack.call

    async def failing(chat, method, params=None):
        if method == "manager.chat":
            raise ChatError("hết ngân sách")
        return await real(chat, method, params)

    d.hub.slack.call = failing  # type: ignore[method-assign]
    eid = sk.mention(CH, BOSS, f"<@{BOT_USER}> làm báo cáo")
    await deliver(sk, eid)
    tag_ts = next(m["ts"] for m in sk.history[CH] if m.get("user") == BOSS)
    m = (await until(lambda: [m for m in sk.sent(CH) if "Không gửi được cho điều phối viên" in sk.text_of(m)], timeout=10))[0]
    assert m.get("thread_ts") == tag_ts
    assert not root_posts(sk)


async def test_marked_files_and_file_errors_stay_in_thread(make, sk):
    """`[[file:]]` trong trả lời ở thread kênh chung → tệp tải lên cùng thread; tệp bị từ chối → tin báo lỗi cũng trong thread."""
    d = await mk(make)
    root = seed_thread(sk)
    await tag_in_thread(sk, root)
    await until(lambda: [m for m in sk.sent(CH) if "Điều phối viên" in sk.text_of(m)], timeout=15)
    conv = conv_of(d)
    croot = d.hub.slack.media.conv_root(conv)  # type: ignore[attr-defined]
    (croot / "out").mkdir(parents=True, exist_ok=True)
    (croot / "out" / "report.md").write_text("# Báo cáo\n")
    d.hub.publish(make_event("manager.reply", "boss", {"conversation_id": conv, "run_id": "r_resumed0010", "channel": f"slack:{CH}",
                                                       "text": "Xong.\n[[file: out/report.md]]\n[[file: ../../x.txt]]", "kind": "reply",
                                                       "ok": True}, run_id="r_resumed0010"))
    done = await until(lambda: sk.calls_of("files.completeUploadExternal"), timeout=15)
    assert done[0]["channel_id"] == CH and done[0]["thread_ts"] == root
    err = (await until(lambda: [m for m in sk.sent(CH) if "Không gửi được một số tệp" in sk.text_of(m)], timeout=10))[0]
    assert err.get("thread_ts") == root
    assert not root_posts(sk)


async def test_run_continue_from_thread_ack_and_result_stay_in_thread(make, sk):
    """Trả lời trong thread vào tin kết quả run (`run.continue`) → xác nhận + kết quả run con trong thread, không lên gốc kênh."""
    d = await mk(make)
    root = seed_thread(sk)
    await tag_in_thread(sk, root)
    reply = (await until(lambda: [m for m in sk.sent(CH) if "Điều phối viên" in sk.text_of(m)], timeout=15))[0]
    d.hub.agents.create({"id": "coder", "name": "coder"})
    r = d.hub.runs.submit("coder", "làm việc")
    await until(lambda: d.hub.store.get_run(r["id"])["status"] == "completed")
    d.hub.slack._map_message(CH, reply["_ts"], r["id"])  # tin bot trong thread ↔ run (như tin kết quả của giaoviec)
    d.hub.store.state_set("slack.mgr_msgmap", {})  # để thread reply đi nhánh run.continue
    await tag_in_thread(sk, root, "làm tiếp phần 2")
    await until(lambda: [m for m in sk.sent(CH) if "Đã gửi tiếp" in sk.text_of(m)], timeout=10)
    await until(lambda: [m for m in sk.sent(CH) if "coder — " in sk.text_of(m)], timeout=15)
    assert all(m.get("thread_ts") == root for m in sk.sent(CH)), [(m.get("thread_ts"), sk.text_of(m)[:40]) for m in sk.sent(CH)]


async def test_send_without_thread_never_hits_channel_root(make, sk):
    """Đơn vị: `_send` vào kênh chung không có reply_to và không có thread đang xử lý → không đăng gốc kênh (DM người thao tác hoặc bỏ)."""
    from cfharness.chat.base import _UID

    d = await mk(make)
    bot = d.hub.slack
    api = bot.api()
    res = await bot._send(api, CH, "không có thread")
    assert res == {"message_id": None} and not sk.sent(CH)
    _UID.set(BOSS)
    try:
        await bot._send(api, CH, "chuyển DM")
    finally:
        _UID.set(None)
    assert not sk.sent(CH)
    assert any("chuyển DM" in sk.text_of(m) for m in sk.sent(DM_BOSS))
