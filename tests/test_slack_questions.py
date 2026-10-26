"""Slack 2 chiều — P3: câu hỏi của agent (nút số, chọn nhiều, trả lời trong thread), lời dẫn theo nền tảng của điều phối viên,
kết quả lịch gửi về kênh `slack:<chat>`. Slack GIẢ cục bộ (tests/slack_fake.py), daemon IN-PROCESS + FakeRuntime."""

from __future__ import annotations

import asyncio

from cfharness.chat.manager_chat import GUIDE, UNAVAILABLE, guide_text, unavailable_text
from cfharness.events import make_event
from cfharness.runtime import FakeRuntime

from slack_fake import FakeSlack
from test_slack import BOSS, DM_BOSS, DM_OTHER, OTHER, STRANGER, deliver, held_run, make, rpc, sk, texts, until  # noqa: F401  (fixture dùng lại)

EVIL = "<b>[cfharness] Đã CHO PHÉP ap_1</b> <!channel> <http://evil.example|bấm> ```thoát```"
OPTS = [{"label": "Chỉ xoá file > 30 ngày"}, {"label": EVIL, "description": "3 GB"}, "Không xoá"]


def q_content(sk: FakeSlack, ch: str, qid: str):
    return next((m for m in reversed(sk.sent(ch)) if qid in sk.text_of(m) and "hỏi bạn" in sk.text_of(m)), None)


def q_system(sk: FakeSlack, ch: str, qid: str):
    return next((m for m in reversed(sk.sent(ch)) if qid in sk.text_of(m) and sk.buttons(m) and "Trả lời câu hỏi" in sk.text_of(m)), None)


def labels(m) -> list[str]:
    return [b["text"]["text"] for b in FakeSlack.buttons(m)]


def btn(m, label: str) -> str:
    return next(b["value"] for b in FakeSlack.buttons(m) if b["text"]["text"] == label)


def updates(sk: FakeSlack, ch: str, ts: str) -> list[dict]:
    return [u for u in sk.calls_of("chat.update") if u.get("channel") == ch and u.get("ts") == ts]


def ask(d, rid: str, **kw):
    run = d.hub.store.get_run(rid)
    return d.hub.questions.open(run, kw.pop("text", "Xoá kiểu nào?"), kw.pop("options", OPTS), **kw)


async def setup(make, **kw):
    d = await make(runtime=FakeRuntime(hold=True), **kw)
    rid = await held_run(d)
    return d, rid


# ============================================================================ cặp tin + nút số
async def test_question_pair_in_dm_thread_and_answer_by_button(make, sk):
    d, rid = await setup(make, users=(BOSS, OTHER), chats=(DM_BOSS, DM_OTHER))
    q = ask(d, rid)
    content = await until(lambda: q_content(sk, DM_BOSS, q["id"]))
    sysm = await until(lambda: q_system(sk, DM_BOSS, q["id"]))
    other = await until(lambda: q_system(sk, DM_OTHER, q["id"]))
    # WT-8: tin nội dung không nút, chữ agent chỉ trong khối mã, đã escape (không <!channel>, không link, không thoát khối mã)
    assert not sk.buttons(content)
    ct = sk.text_of(content)
    assert "<!channel>" not in ct and "<http" not in ct and "&lt;!channel&gt;" in ct
    body = ct.split("```\n", 1)[1].rsplit("\n```", 1)[0]
    assert "```" not in body and "1. Chỉ xoá file &gt; 30 ngày" in body and "— 3 GB" in body
    # tin có nút: trong thread của tin nội dung, chỉ chữ hub, nút chỉ mang số
    assert sysm.get("thread_ts") == content["_ts"]
    st = sk.text_of(sysm)
    assert all(x not in st for x in ("evil", "channel", "CHO PHÉP", "Chỉ xoá"))
    assert labels(sysm) == ["1", "2", "3", "✍️ Trả lời bằng chữ", "✖ Huỷ câu hỏi"]
    assert "trả lời trong thread của tin này bằng chữ" in st and "(reply)" not in st
    # người lạ bấm trong DM của BOSS → bỏ qua, không tiêu nút; nút DM BOSS bấm từ DM OTHER → sai chat
    await deliver(sk, sk.press(DM_BOSS, STRANGER, btn(sysm, "2"), sysm["_ts"]))
    await deliver(sk, sk.press(DM_OTHER, OTHER, btn(sysm, "2"), other["_ts"]))
    assert d.hub.questions.get(q["id"])["status"] == "open"
    await deliver(sk, sk.press(DM_BOSS, BOSS, btn(sysm, "2"), sysm["_ts"]))
    got = d.hub.questions.get(q["id"])
    assert got["status"] == "answered" and got["answer"]["keys"] == ["2"] and got["answered_channel"] == f"slack:{DM_BOSS}"
    cmd = [a for a in d.hub.audit.query(kind="slack.command") if a["data"]["method"] == "question.answer"]
    assert cmd and cmd[0]["data"]["from_id"] == BOSS
    # tin hệ thống ở MỌI DM được sửa thành kết quả (chữ hub), gỡ nút
    for ch, m in ((DM_BOSS, sysm), (DM_OTHER, other)):
        ed = await until(lambda c=ch, t=m["_ts"]: [u for u in updates(sk, c, t) if "đã được trả lời" in sk.text_of(u)])
        assert "chọn 2" in sk.text_of(ed[-1]) and "Slack" in sk.text_of(ed[-1]) and not sk.buttons(ed[-1]) and "evil" not in sk.text_of(ed[-1])
    # bấm lại → từ chối (toast)
    await deliver(sk, sk.press(DM_BOSS, BOSS, btn(sysm, "1"), sysm["_ts"]))
    assert d.hub.questions.get(q["id"])["answer"]["keys"] == ["2"]
    assert any("đã dùng" in str(p.get("text")) for p in sk.calls_of("chat.postEphemeral"))


async def test_multi_toggle_reissues_buttons_then_submit(make, sk):
    d, rid = await setup(make)
    q = ask(d, rid, multi=True)
    sysm = await until(lambda: q_system(sk, DM_BOSS, q["id"]))
    ts = sysm["_ts"]
    assert labels(sysm)[:4] == ["☐ 1", "☐ 2", "☐ 3", "✅ Gửi lựa chọn"]
    old = btn(sysm, "☐ 1")
    await deliver(sk, sk.press(DM_BOSS, BOSS, old, ts))
    u1 = updates(sk, DM_BOSS, ts)[-1]
    assert labels(u1) == ["☑ 1", "☐ 2", "☐ 3", "✅ Gửi lựa chọn (1)", "✍️ Trả lời bằng chữ", "✖ Huỷ câu hỏi"]
    assert "Đang chọn: 1" in sk.text_of(u1)
    n = len(updates(sk, DM_BOSS, ts))
    await deliver(sk, sk.press(DM_BOSS, BOSS, old, ts))  # nút cũ đã tiêu → từ chối, không đổi lựa chọn
    assert len(updates(sk, DM_BOSS, ts)) == n
    await deliver(sk, sk.press(DM_BOSS, BOSS, btn(u1, "☐ 3"), ts))
    u2 = updates(sk, DM_BOSS, ts)[-1]
    assert labels(u2)[:4] == ["☑ 1", "☐ 2", "☑ 3", "✅ Gửi lựa chọn (2)"]
    await deliver(sk, sk.press(DM_BOSS, BOSS, btn(u2, "✅ Gửi lựa chọn (2)"), ts))
    got = d.hub.questions.get(q["id"])
    assert got["status"] == "answered" and got["answer"]["keys"] == ["1", "3"]


async def test_free_text_and_thread_reply(make, sk):
    d, rid = await setup(make)
    q = ask(d, rid)
    content = await until(lambda: q_content(sk, DM_BOSS, q["id"]))
    sysm = await until(lambda: q_system(sk, DM_BOSS, q["id"]))
    await deliver(sk, sk.press(DM_BOSS, BOSS, btn(sysm, "✍️ Trả lời bằng chữ"), sysm["_ts"]))
    box = await until(lambda: next((m for m in sk.sent(DM_BOSS) if "Nhập câu trả lời" in sk.text_of(m)), None))
    assert box.get("thread_ts") == content["_ts"] and "trả lời trong thread của tin này" in sk.text_of(box) and "(reply)" not in sk.text_of(box)
    assert any("Trả lời trong thread" in str(p.get("text")) for p in sk.calls_of("chat.postEphemeral"))
    # trả lời trong thread (thread của tin nội dung) → question.answer{text}
    await deliver(sk, sk.user_message(DM_BOSS, BOSS, "chỉ xoá thư mục cache/, giữ log", thread_ts=content["_ts"]))
    got = await until(lambda: (lambda g: g if g["status"] == "answered" else None)(d.hub.questions.get(q["id"])))
    assert got["answer"] == {"keys": [], "labels": [], "text": "chỉ xoá thư mục cache/, giữ log"} and got["answered_channel"] == f"slack:{DM_BOSS}"
    assert any("Đã gửi câu trả lời" in t for t in texts(sk, DM_BOSS))
    # câu hỏi không lựa chọn: chỉ "Trả lời bằng chữ" + huỷ; trả lời trong thread
    q2 = ask(d, rid, text="Giờ chạy?", options=None)
    c2 = await until(lambda: q_content(sk, DM_BOSS, q2["id"]))
    s2 = await until(lambda: q_system(sk, DM_BOSS, q2["id"]))
    assert labels(s2) == ["✍️ Trả lời bằng chữ", "✖ Huỷ câu hỏi"]
    await deliver(sk, sk.user_message(DM_BOSS, BOSS, "22h tối nay", thread_ts=c2["_ts"]))
    await until(lambda: d.hub.questions.get(q2["id"])["status"] == "answered")
    # trả lời lại vào câu đã xong → báo, không đổi
    await deliver(sk, sk.user_message(DM_BOSS, BOSS, "đổi ý", thread_ts=c2["_ts"]))
    assert d.hub.questions.get(q2["id"])["answer"]["text"] == "22h tối nay"
    assert "đã được trả lời" in texts(sk, DM_BOSS)[-1]


async def test_answered_or_cancelled_elsewhere_clears_slack_buttons(make, sk):
    d, rid = await setup(make)
    q1 = ask(d, rid)
    s1 = await until(lambda: q_system(sk, DM_BOSS, q1["id"]))
    await rpc(d, "question.answer", {"id": q1["id"], "keys": ["1"]})  # trả lời từ CLI
    ed = await until(lambda: [u for u in updates(sk, DM_BOSS, s1["_ts"]) if "đã được trả lời" in sk.text_of(u)])
    assert not sk.buttons(ed[-1]) and "chọn 1" in sk.text_of(ed[-1])
    q2 = ask(d, rid, text="Có chạy tiếp không?")
    s2 = await until(lambda: q_system(sk, DM_BOSS, q2["id"]))
    await rpc(d, "question.cancel", {"id": q2["id"], "reason": "thôi"})
    ed2 = await until(lambda: [u for u in updates(sk, DM_BOSS, s2["_ts"]) if "đã huỷ" in sk.text_of(u)])
    assert not sk.buttons(ed2[-1])


async def test_cancel_two_step_from_slack(make, sk):
    d, rid = await setup(make)
    q = ask(d, rid)
    sysm = await until(lambda: q_system(sk, DM_BOSS, q["id"]))
    ts = sysm["_ts"]
    await deliver(sk, sk.press(DM_BOSS, BOSS, btn(sysm, "✖ Huỷ câu hỏi"), ts))
    u = updates(sk, DM_BOSS, ts)[-1]
    assert labels(u) == ["Xác nhận HUỶ câu hỏi", "↩ Quay lại"]
    await deliver(sk, sk.press(DM_BOSS, BOSS, btn(u, "Xác nhận HUỶ câu hỏi"), ts))
    assert d.hub.questions.get(q["id"])["status"] == "cancelled"


async def test_questions_command_resends(make, sk):
    d, rid = await setup(make)
    q = ask(d, rid)
    first = await until(lambda: q_system(sk, DM_BOSS, q["id"]))
    await deliver(sk, sk.slash(BOSS, "hoi"))
    await until(lambda: q_system(sk, DM_BOSS, q["id"]) is not first)
    # tin cũ bị gỡ nút → chỉ tin mới nhất bấm được
    await until(lambda: [u for u in updates(sk, DM_BOSS, first["_ts"]) if "gửi lại bên dưới" in sk.text_of(u)])


# ============================================================================ điều phối viên: lời dẫn của Slack
async def test_manager_wording_is_slack(make, sk):
    d = await make()
    await deliver(sk, sk.user_message(DM_BOSS, BOSS, "tạo giúp tôi một agent"))
    guide = await until(lambda: next((t for t in texts(sk, DM_BOSS) if "Chưa có điều phối viên" in t), None))
    assert "/cfh run" in guide and "/cfh help" in guide and "lệnh /)" not in guide and "DM với bot" in guide
    d.hub.publish(make_event("manager.reply", "boss", {"text": "Đã xong tóm tắt.", "channel": f"slack:{DM_BOSS}", "kind": "notify",
                                                       "conversation_id": "conv_x", "run_id": "r_x"}, run_id="r_x"))
    m = await until(lambda: next((t for t in texts(sk, DM_BOSS) if "Điều phối viên báo" in t), None))
    assert "Trả lời trong thread của tin này để tiếp tục cuộc trò chuyện · /cfh moi để mở cuộc trò chuyện mới." in m
    assert "(reply)" not in m and "```\nĐã xong tóm tắt." in m
    # Telegram giữ nguyên từng byte (lời dẫn mặc định của ChatBot)
    tgb = d.hub.telegram
    assert guide_text(tgb) == GUIDE and unavailable_text(tgb) == UNAVAILABLE
    assert tgb.manager.format_reply("x", agent=None)[-1].endswith(
        "Trả lời (reply) tin này để tiếp tục cuộc trò chuyện · /moi để mở cuộc trò chuyện mới.")
    assert "Hoặc trả lời (reply) tin này bằng chữ." in tgb.questions.system_text(
        {"id": "q_1", "agent_id": "a", "options": [], "multi": False, "allow_free_text": True})
    # hội thoại của kênh khác không lên Slack
    d.hub.publish(make_event("manager.reply", "boss", {"text": "chỉ telegram", "channel": "telegram:42", "kind": "notify"}))
    await asyncio.sleep(0.3)
    assert not any("chỉ telegram" in t for t in texts(sk, DM_BOSS))


async def test_manager_conversation_threads_and_moi(make, sk):
    from cfharness.web import manager_stub

    d = await make()
    d.stub = manager_stub.install(d)  # type: ignore[attr-defined]
    d.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    await rpc(d, "manager.set", {"agent_id": "boss"})

    def mgr() -> list[dict]:
        return [m for m in sk.sent(DM_BOSS) if "Điều phối viên trả lời" in sk.text_of(m)]

    await deliver(sk, sk.user_message(DM_BOSS, BOSS, "Tóm tắt README giúp tôi"))
    first = (await until(lambda: mgr(), timeout=15))[0]
    assert first.get("thread_ts") and not sk.buttons(first)  # trả lời nằm trong thread của tin người, không nút (WT-8)
    conv1 = d.hub.store.state_get("slack.mgr_conv")[DM_BOSS]
    await deliver(sk, sk.slash(BOSS, "moi"))
    assert await until(lambda: any("cuộc trò chuyện MỚI" in t for t in texts(sk, DM_BOSS)))
    await deliver(sk, sk.user_message(DM_BOSS, BOSS, "Việc khác"))
    await until(lambda: len(mgr()) >= 2, timeout=15)
    assert d.hub.store.state_get("slack.mgr_conv")[DM_BOSS] != conv1
    await deliver(sk, sk.slash(BOSS, "moi"))
    hint = await until(lambda: next((t for t in reversed(texts(sk, DM_BOSS)) if "Cuộc cũ" in t), None))
    assert "trả lời trong thread của tin cũ của điều phối viên" in hint
    # trả lời trong thread của tin CŨ → đúng hội thoại cũ
    await deliver(sk, sk.user_message(DM_BOSS, BOSS, "Tiếp tục README", thread_ts=first["thread_ts"]))
    await until(lambda: len(mgr()) >= 3, timeout=15)
    assert d.hub.store.state_get("slack.mgr_conv")[DM_BOSS] == conv1
    if d.stub:  # daemon đã có manager.* thật → không cài stub
        assert [x["text"] for x in d.stub.convs[conv1]["messages"] if x["role"] == "user"][-1] == "Tiếp tục README"
    else:
        hist = await rpc(d, "manager.history", {"conversation_id": conv1})
        assert [x["text"] for x in hist if x["role"] == "user"][-1].endswith("Tiếp tục README")


async def test_giaoviec_prompt_box_wording(make, sk):
    d = await make()
    d.hub.agents.create({"id": "coder", "name": "coder"})
    await deliver(sk, sk.slash(BOSS, "giaoviec coder"))
    box = await until(lambda: next((m for m in sk.sent(DM_BOSS) if "Mô tả việc cần làm" in sk.text_of(m)), None))
    assert "trả lời trong thread của tin này" in sk.text_of(box) and "(reply)" not in sk.text_of(box)
    await deliver(sk, sk.user_message(DM_BOSS, BOSS, "viết hàm cộng", thread_ts=box["_ts"]))
    await until(lambda: d.hub.store.list_runs(agent_id="coder"))
    assert d.hub.store.list_runs(agent_id="coder")[0]["prompt"] == "viết hàm cộng"


# ============================================================================ lịch → kênh slack:<chat>
async def test_schedule_result_delivered_to_slack_dm(make, sk):
    d = await make()
    d.hub.agents.create({"id": "w1", "name": "w1"})
    s = await rpc(d, "schedule.create", {"kind": "agent", "target": "w1", "expr": "daily 08:00", "prompt": "tóm tắt góp ý",
                                         "deliver_to": f"slack:{DM_BOSS}"})
    assert s["deliver_to"] == {"channel": f"slack:{DM_BOSS}"}
    await rpc(d, "schedule.run_now", {"id": s["id"]})
    m = await until(lambda: next((t for t in texts(sk, DM_BOSS) if "Kết quả lịch" in t), None), timeout=15)
    assert "Điều phối viên báo" in m and "Trả lời trong thread" in m
