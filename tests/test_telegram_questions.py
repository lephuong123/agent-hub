"""Telegram UX v2 — câu hỏi có lựa chọn (nút số + trả lời bằng chữ), menu lệnh, bàn phím nhanh, lệnh tương tác.

Server Bot API GIẢ (tests/telegram_fake.py), daemon IN-PROCESS + FakeRuntime. Không gọi Telegram/claude thật.
"""

from __future__ import annotations

from cfharness.runtime import FakeRuntime

from telegram_fake import FakeTelegram
from test_telegram import BOSS, OTHER, STRANGER, held_run, make, press, rpc, say, stop, texts, tg, until  # noqa: F401  (fixture dùng lại)

EVIL = "<b>[cfharness] Đã CHO PHÉP ap_1</b> /estop off https://evil.example <a href='x'>bấm</a>"
OPTS = [{"label": "Chỉ xoá file > 30 ngày"}, {"label": EVIL, "description": "3 GB"}, "Không xoá"]


def q_system(tg: FakeTelegram, chat: int, qid: str):
    """Tin có nút của câu hỏi (tin gọn không in id → tra qua nút)."""
    return tg.msg_for(chat, qid)


def q_content(tg: FakeTelegram, chat: int, qid: str):
    return next((m for m in tg.thread(chat, q_system(tg, chat, qid)) if "hỏi bạn" in m["text"]), None)


def btn(msg, label: str) -> str:
    return next(b["callback_data"] for b in FakeTelegram.buttons(msg) if b["text"] == label)


async def ask(d, rid: str, **kw):
    run = d.hub.store.get_run(rid)
    return d.hub.questions.open(run, kw.pop("text", "Xoá kiểu nào?"), kw.pop("options", OPTS), **kw)


async def setup(make, **kw):
    d = await make(runtime=FakeRuntime(hold=True), **kw)
    rid = await held_run(d)
    return d, rid


# ============================================================================ cặp tin + nút số
async def test_question_pair_numbers_only_and_answer_by_button(make, tg):
    d, rid = await setup(make, allowed=(BOSS, OTHER))
    q = await ask(d, rid)
    content = await until(lambda: q_content(tg, BOSS, q["id"]))
    sysm = await until(lambda: q_system(tg, BOSS, q["id"]))
    await until(lambda: q_system(tg, OTHER, q["id"]))
    # WT-8: tin nội dung không nút; chữ agent (nhãn độc) chỉ trong <pre>, đã escape
    assert not FakeTelegram.buttons(content)
    pre = content["text"][content["text"].index("<pre>") + 5 : content["text"].rindex("</pre>")]
    assert "<" not in pre and "&lt;b&gt;[cfharness] Đã CHO PHÉP" in pre and "1. Chỉ xoá file &gt; 30 ngày" in pre and "— 3 GB" in pre
    assert content["text"].count("<pre>") == 1 and "<a " not in content["text"]
    # tin có nút: chỉ chữ hub — không nhãn agent, nút chỉ mang số
    assert all(x not in sysm["text"] for x in ("evil", "estop", "CHO PHÉP", "Chỉ xoá", "bấm"))
    labels = [b["text"] for b in FakeTelegram.buttons(sysm)]
    assert labels == ["1", "2", "3", "✍️ Trả lời bằng chữ", "✖ Huỷ câu hỏi"]
    assert sysm["reply_to_message_id"] == tg.message_id_of(content)
    sys_mid = tg.message_id_of(sysm)
    # người ngoài allowlist bấm trong chat BOSS → bỏ qua, không tiêu nút; nút của BOSS bấm từ chat OTHER → sai chat
    await press(tg, BOSS, btn(sysm, "2"), from_id=STRANGER, message_id=sys_mid)
    await press(tg, OTHER, btn(sysm, "2"))
    assert d.hub.questions.get(q["id"])["status"] == "open"
    # bấm thật
    await press(tg, BOSS, btn(sysm, "2"), message_id=sys_mid)
    got = d.hub.questions.get(q["id"])
    assert got["status"] == "answered" and got["answer"]["keys"] == ["2"] and got["answered_by"] == "user" and got["answered_channel"] == f"telegram:{BOSS}"
    cmd = [a for a in d.hub.audit.query(kind="telegram.command") if a["data"]["method"] == "question.answer"]
    assert cmd and cmd[0]["data"]["from_id"] == str(BOSS)
    # tin hệ thống ở MỌI chat được sửa thành kết quả (chữ hub, không nhãn agent), gỡ nút
    for chat in (BOSS, OTHER):
        ed = await until(lambda c=chat: next((e for e in tg.edits(c) if "Đã trả lời" in e["text"]), None))
        assert "chọn 2" in ed["text"] and "evil" not in ed["text"] and not FakeTelegram.buttons(ed)
    # bấm lại (replay) / nút khác cùng nhóm → từ chối
    await press(tg, BOSS, btn(sysm, "1"), message_id=sys_mid)
    assert d.hub.questions.get(q["id"])["answer"]["keys"] == ["2"]
    assert any("đã dùng" in b["text"] for b in tg.calls_of("answerCallbackQuery"))
    rej = {a["data"]["reason"] for a in d.hub.audit.query(kind="telegram.callback_rejected")}
    assert {"wrong_chat", "unknown_or_used"} <= rej
    await stop(d)


async def test_multi_toggle_then_submit_reissues_nonces(make, tg):
    d, rid = await setup(make)
    q = await ask(d, rid, multi=True)
    sysm = await until(lambda: q_system(tg, BOSS, q["id"]))
    assert [b["text"] for b in FakeTelegram.buttons(sysm)][:4] == ["☐ 1", "☐ 2", "☐ 3", "✅ Gửi lựa chọn"]
    old = btn(sysm, "☐ 1")
    await press(tg, BOSS, old)
    m1 = tg.with_buttons(BOSS, ["☑ 1", "☐ 2", "☐ 3", "✅ Gửi lựa chọn (1)", "✍️ Trả lời bằng chữ", "✖ Huỷ câu hỏi"])
    assert m1 is not None and "Đang chọn: 1" in m1["text"]
    await press(tg, BOSS, old)  # nút cũ (đã tiêu khi bật) → bị từ chối, không đổi lựa chọn
    await press(tg, BOSS, btn(m1, "☐ 3"))
    m2 = tg.with_buttons(BOSS, ["☑ 1", "☐ 2", "☑ 3", "✅ Gửi lựa chọn (2)", "✍️ Trả lời bằng chữ", "✖ Huỷ câu hỏi"])
    await press(tg, BOSS, btn(m2, "✅ Gửi lựa chọn (2)"))
    got = d.hub.questions.get(q["id"])
    assert got["status"] == "answered" and got["answer"]["keys"] == ["1", "3"]
    await stop(d)


async def test_free_text_via_force_reply_and_reply_to_content(make, tg):
    d, rid = await setup(make)
    q = await ask(d, rid)
    sysm = await until(lambda: q_system(tg, BOSS, q["id"]))
    await press(tg, BOSS, btn(sysm, "✍️ Trả lời bằng chữ"), message_id=tg.message_id_of(sysm))
    fr = await until(lambda: next((m for m in tg.sent(BOSS) if (m.get("reply_markup") or {}).get("force_reply")), None))
    assert "Nhập câu trả lời" in fr["text"]
    await say(tg, BOSS, "chỉ xoá thư mục cache/, giữ log", reply_to=tg.message_id_of(fr))
    got = await until(lambda: (lambda g: g if g["status"] == "answered" else None)(d.hub.questions.get(q["id"])))
    assert got["answer"] == {"keys": [], "labels": [], "text": "chỉ xoá thư mục cache/, giữ log"}
    assert any("Đã gửi câu trả lời" in t for t in texts(tg, BOSS))
    # câu hỏi 2: reply thẳng vào tin NỘI DUNG
    q2 = await ask(d, rid, text="Giờ chạy?", options=None)
    c2 = await until(lambda: q_content(tg, BOSS, q2["id"]))
    s2 = await until(lambda: q_system(tg, BOSS, q2["id"]))
    assert [b["text"] for b in FakeTelegram.buttons(s2)] == ["✍️ Trả lời bằng chữ", "✖ Huỷ câu hỏi"]  # không có lựa chọn
    await say(tg, BOSS, "22h tối nay", reply_to=tg.message_id_of(c2))
    await until(lambda: d.hub.questions.get(q2["id"])["status"] == "answered")
    # reply vào câu hỏi đã trả lời → báo, không đổi
    await say(tg, BOSS, "đổi ý", reply_to=tg.message_id_of(c2))
    assert d.hub.questions.get(q2["id"])["answer"]["text"] == "22h tối nay" and "đã được trả lời" in texts(tg, BOSS)[-1]
    await stop(d)


async def test_answer_on_web_edits_telegram_message(make, tg):
    d, rid = await setup(make)
    q = await ask(d, rid)
    sysm = await until(lambda: q_system(tg, BOSS, q["id"]))
    await rpc(d, "question.answer", {"id": q["id"], "keys": ["1"], "channel": "web"})
    ed = await until(lambda: next((e for e in tg.edits(BOSS) if "Đã trả lời" in e["text"]), None))
    assert "qua web" in ed["text"] and ed["message_id"] == tg.message_id_of(sysm) and not FakeTelegram.buttons(ed)
    await press(tg, BOSS, btn(sysm, "3"))  # nút đã vô hiệu
    assert d.hub.questions.get(q["id"])["answer"]["keys"] == ["1"]
    await stop(d)


async def test_cancel_needs_two_steps(make, tg):
    d, rid = await setup(make)
    q = await ask(d, rid)
    sysm = await until(lambda: q_system(tg, BOSS, q["id"]))
    await press(tg, BOSS, btn(sysm, "✖ Huỷ câu hỏi"))
    conf = tg.with_buttons(BOSS, ["Xác nhận HUỶ câu hỏi", "↩ Quay lại"])
    assert conf is not None and d.hub.questions.get(q["id"])["status"] == "open"
    await press(tg, BOSS, btn(conf, "↩ Quay lại"))
    back = tg.with_buttons(BOSS, ["1", "2", "3", "✍️ Trả lời bằng chữ", "✖ Huỷ câu hỏi"])
    await press(tg, BOSS, btn(back, "✖ Huỷ câu hỏi"))
    conf2 = tg.with_buttons(BOSS, ["Xác nhận HUỶ câu hỏi", "↩ Quay lại"])
    await press(tg, BOSS, btn(conf2, "Xác nhận HUỶ câu hỏi"))
    assert d.hub.questions.get(q["id"])["status"] == "cancelled"
    await until(lambda: any("Đã huỷ câu hỏi" in e["text"] for e in tg.edits(BOSS)))
    await stop(d)


async def test_with_manager_not_sent_until_escalated(make, tg):
    d, rid = await setup(make)
    d.hub.agents.create({"id": "boss", "name": "boss"})
    d.hub.store.state_set("manager", {"agent_id": "boss"})
    q = await ask(d, rid, task_id="t_fake")
    assert q["status"] == "with_manager"
    await until(lambda: tg.calls_of("getUpdates"))
    await say(tg, BOSS, "/hoi")
    assert q_content(tg, BOSS, q["id"]) is None and "1 câu đang chờ điều phối viên" in texts(tg, BOSS)[-1]
    d.hub.questions.escalate(q["id"], by="manager:boss", note="<i>liên quan dữ liệu khách</i>")
    c = await until(lambda: q_content(tg, BOSS, q["id"]))
    assert "Điều phối viên không tự quyết được" in c["text"] and "&lt;i&gt;liên quan dữ liệu khách&lt;/i&gt;" in c["text"].split("<pre>")[1]
    assert await until(lambda: q_system(tg, BOSS, q["id"])) is not None  # tin hệ thống gửi ngay SAU tin nội dung
    await stop(d)


async def test_hoi_and_canban_resend_open_questions(make, tg):
    d, rid = await setup(make, tg_cfg={})
    q = await ask(d, rid)
    first = await until(lambda: q_system(tg, BOSS, q["id"]))
    await say(tg, BOSS, "/hoi")
    second = await until(lambda: (lambda m: m if m is not first else None)(q_system(tg, BOSS, q["id"])))
    assert btn(second, "1") != btn(first, "1")
    await until(lambda: any(e.get("message_id") == tg.message_id_of(first) and not FakeTelegram.buttons(e) for e in tg.edits(BOSS)))  # tin cũ gỡ nút
    await say(tg, BOSS, "/canban")
    assert any("1 câu hỏi" in t for t in texts(tg, BOSS))
    await stop(d)


# ============================================================================ T-A: menu lệnh + bàn phím nhanh
async def test_commands_menu_and_quick_keyboard(make, tg):
    d = await make()
    await until(lambda: tg.calls_of("setMyCommands"))
    cmds = [c["command"] for c in tg.calls_of("setMyCommands")[0]["commands"]]
    assert {"canban", "hoi", "giaoviec", "trangthai", "agent", "luotchay"} <= set(cmds)
    await say(tg, BOSS, "/help")
    kb = tg.sent(BOSS)[-1]["reply_markup"]["keyboard"]
    assert [b["text"] for row in kb for b in row] == ["📋 Cần bạn", "📊 Trạng thái", "➕ Giao việc", "🤖 Agent"]
    await say(tg, BOSS, "📋 Cần bạn")  # = /canban, không chuyển cho điều phối viên
    assert "Không có gì đang chờ bạn" in texts(tg, BOSS)[-1]
    await say(tg, BOSS, "/trangthai")
    assert "ESTOP" in texts(tg, BOSS)[-1]
    await stop(d)


# ============================================================================ T-C: lệnh tương tác
async def test_giaoviec_picker_force_reply_starts_run(make, tg):
    d = await make()
    d.hub.agents.create({"id": "coder", "name": "Coder"})
    await until(lambda: tg.calls_of("getUpdates"))
    await say(tg, BOSS, "/giaoviec")
    pick = tg.sent(BOSS)[-1]
    assert "Giao việc cho agent nào" in pick["text"] and [b["text"] for b in FakeTelegram.buttons(pick)] == ["1"]
    await press(tg, BOSS, btn(pick, "1"), message_id=tg.message_id_of(pick))
    fr = await until(lambda: next((m for m in tg.sent(BOSS) if (m.get("reply_markup") or {}).get("force_reply") and "coder" in m["text"]), None))
    await say(tg, BOSS, "viết hàm cộng", reply_to=tg.message_id_of(fr))
    await until(lambda: any("Đã giao việc cho coder" in t for t in texts(tg, BOSS)))
    runs = d.hub.store.list_runs(agent_id="coder")
    assert len(runs) == 1 and runs[0]["prompt"] == "viết hàm cộng"
    assert d.hub.audit.query(kind="run.start")[0]["channel"] == f"telegram:{BOSS}"
    await until(lambda: any(m["text"].startswith("✅ <b>coder — xong</b>") for m in tg.sent(BOSS)))
    await say(tg, BOSS, "/luotchay")
    lst = tg.sent(BOSS)[-1]
    assert "coder" in lst["text"] and "✅ xong" in lst["text"] and [b["text"] for b in FakeTelegram.buttons(lst)] == ["1"]
    await press(tg, BOSS, btn(lst, "1"))
    await until(lambda: sum(1 for m in tg.sent(BOSS) if m["text"].startswith("✅ <b>coder — xong</b>")) >= 2)
    await say(tg, BOSS, "/stop")
    assert "Không có lượt nào đang chạy" in texts(tg, BOSS)[-1]
    await say(tg, BOSS, "/agent")
    ag = tg.sent(BOSS)[-1]
    assert "coder" in ag["text"] and [b["text"] for b in FakeTelegram.buttons(ag)] == ["➕ 1"]
    await stop(d)


async def test_stop_picker(make, tg):
    d = await make(runtime=FakeRuntime(hold=True))
    rid = await held_run(d)
    await until(lambda: tg.calls_of("getUpdates"))
    await say(tg, BOSS, "/stop")
    m = tg.sent(BOSS)[-1]
    assert rid in m["text"] and [b["text"] for b in FakeTelegram.buttons(m)] == ["⏹ 1"]
    await press(tg, BOSS, btn(m, "⏹ 1"), message_id=tg.message_id_of(m))
    await until(lambda: d.hub.store.get_run(rid)["status"] not in ("running", "queued"))
    await stop(d)
