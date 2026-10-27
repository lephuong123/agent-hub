"""Slack 2 chiều P2 — modal thay `force_reply`: giao việc (nút `fg`, `/cfh giaoviec <agent>`) và "Trả lời bằng chữ" cho câu hỏi.
Slack GIẢ cục bộ (tests/slack_fake.py), daemon IN-PROCESS + FakeRuntime."""

from __future__ import annotations

from cfharness.runtime import FakeRuntime

from slack_fake import FakeSlack
from test_slack import BOSS, DM_BOSS, DM_OTHER, OTHER, STRANGER, deliver, held_run, make, sk, texts, until  # noqa: F401  (fixture)

EVIL = "<!channel> <@U0BOSS001> <http://evil.example|bấm> *đậm*"


def btn(sk: FakeSlack, msg: dict, label: str) -> str:
    return next(b["value"] for b in sk.buttons(msg) if b["text"]["text"] == label)


def rejected(d, reason: str) -> bool:
    return any(a["data"].get("reason") == f"modal:{reason}" for a in d.hub.audit.query(kind="slack.callback_rejected", limit=50))


async def open_run_modal(d, sk: FakeSlack) -> dict:
    """`/cfh giaoviec` → danh sách agent có nút → bấm "1" → modal giao việc cho coder."""
    d.hub.agents.create({"id": "coder", "name": "Coder"})
    await deliver(sk, sk.slash(BOSS, "giaoviec"))
    pick = next(m for m in reversed(sk.sent(DM_BOSS)) if "Giao việc cho agent nào" in sk.text_of(m))
    await deliver(sk, sk.press(DM_BOSS, BOSS, btn(sk, pick, "1"), pick["_ts"]))
    return await until(lambda: sk.views()[-1] if sk.views() else None)


# ============================================================================ giao việc
async def test_agent_button_opens_modal_and_submission_starts_run(make, sk):
    d = await make()
    view = await open_run_modal(d, sk)
    assert view["title"]["text"] == "Giao việc" and view["callback_id"] == "cfh_input"
    assert next(b for b in view["blocks"] if b["type"] == "input")["label"]["text"] == "Yêu cầu"
    opened = sk.calls_of("views.open")[-1]
    assert opened["trigger_id"].startswith("trig-")
    assert any("hộp nhập vừa mở" in sk.text_of(u) for u in sk.calls_of("chat.update"))
    assert not any("Mô tả việc cần làm" in t for t in texts(sk, DM_BOSS))  # không có tin mời trả lời trong thread
    await deliver(sk, sk.submit_view(BOSS, view, "viết hàm cộng"))
    await until(lambda: any("Đã giao việc cho" in t for t in texts(sk, DM_BOSS)))
    runs = d.hub.store.list_runs(agent_id="coder")
    assert len(runs) == 1 and runs[0]["prompt"] == "viết hàm cộng"
    assert d.hub.audit.query(kind="run.start")[0]["channel"] == f"slack:{DM_BOSS}"
    cmd = [a for a in d.hub.audit.query(kind="slack.command") if a["data"]["method"] == "run.start"]
    assert cmd and cmd[0]["data"]["from_id"] == BOSS
    res = await until(lambda: [m for m in sk.sent(DM_BOSS) if "coder — xong" in sk.text_of(m)])  # theo dõi run → tin kết quả
    assert "[cfharness]" not in sk.text_of(res[-1])
    # gửi lại cùng modal (nonce đã tiêu) → từ chối, không chạy lần hai
    await deliver(sk, sk.submit_view(BOSS, view, "chạy lại lần nữa"))
    assert len(d.hub.store.list_runs(agent_id="coder")) == 1 and rejected(d, "unknown_or_used")


async def test_slash_giaoviec_agent_without_prompt_opens_modal(make, sk):
    d = await make()
    d.hub.agents.create({"id": "coder", "name": "Coder"})
    await deliver(sk, sk.slash(BOSS, "giaoviec coder"))
    view = await until(lambda: sk.views()[-1] if sk.views() else None)
    assert "coder" in view["blocks"][0]["text"]["text"]
    await deliver(sk, sk.submit_view(BOSS, view, "sửa lỗi X"))
    await until(lambda: d.hub.store.list_runs(agent_id="coder"))
    assert d.hub.store.list_runs(agent_id="coder")[0]["prompt"] == "sửa lỗi X"


async def test_expired_trigger_falls_back_to_thread_reply(make, sk):
    d = await make()
    d.hub.agents.create({"id": "coder", "name": "Coder"})
    sk.fail_views = True  # trigger_id hết hạn (hàng đợi chậm) → views.open lỗi
    await deliver(sk, sk.slash(BOSS, "giaoviec coder"))
    ask = await until(lambda: next((m for m in sk.sent(DM_BOSS) if "Mô tả việc cần làm" in sk.text_of(m)), None))
    assert "trả lời trong thread" in sk.text_of(ask) and d.hub.slack.modals.count() == 0  # nonce đã gỡ khi mở lỗi
    await deliver(sk, sk.user_message(DM_BOSS, BOSS, "viết test", thread_ts=ask["_ts"]))
    await until(lambda: d.hub.store.list_runs(agent_id="coder"))
    assert d.hub.store.list_runs(agent_id="coder")[0]["prompt"] == "viết test"


async def test_forged_stranger_and_other_user_submissions_rejected(make, sk):
    d = await make(users=(BOSS, OTHER), chats=(DM_BOSS, DM_OTHER))
    view = await open_run_modal(d, sk)
    # metadata bịa
    await deliver(sk, sk.submit_view(BOSS, view, "bịa", metadata="khong-co-nonce-nay"))
    # người lạ / người khác trong allowlist dùng nonce của BOSS → từ chối, KHÔNG tiêu nonce
    await deliver(sk, sk.submit_view(STRANGER, view, "người lạ"))
    await deliver(sk, sk.submit_view(OTHER, view, "người khác"))
    # workspace khác
    await deliver(sk, sk.submit_view(BOSS, view, "workspace lạ", team="T0EVIL001"))
    # modal không phải của bot (callback_id khác) → bỏ qua
    await deliver(sk, sk.submit_view(BOSS, view, "modal lạ", callback_id="khac"))
    assert d.hub.store.list_runs(agent_id="coder") == []
    assert all(rejected(d, r) for r in ("unknown_or_used", "wrong_user", "wrong_team"))
    assert sk.sent(DM_OTHER) == []
    # chủ thật vẫn gửi được (nonce còn nguyên)
    await deliver(sk, sk.submit_view(BOSS, view, "việc thật"))
    await until(lambda: d.hub.store.list_runs(agent_id="coder"))
    assert [r["prompt"] for r in d.hub.store.list_runs(agent_id="coder")] == ["việc thật"]


async def test_revoked_user_cannot_submit_open_modal(make, sk):
    d = await make()
    view = await open_run_modal(d, sk)
    cfg = d.hub.config.get("notify.slack")
    d.hub.config.set("notify.slack.allowed_user_ids", [])  # thu hồi sau khi mở hộp nhập
    try:
        await deliver(sk, sk.submit_view(BOSS, view, "sau khi thu hồi"))
    finally:
        d.hub.config.set("notify.slack.allowed_user_ids", cfg["allowed_user_ids"])
    assert d.hub.store.list_runs(agent_id="coder") == [] and rejected(d, "not_allowed")


# ============================================================================ câu hỏi: trả lời bằng chữ
async def test_question_free_text_via_modal(make, sk):
    d = await make(runtime=FakeRuntime(hold=True))
    rid = await held_run(d)
    run = d.hub.store.get_run(rid)
    q = d.hub.questions.open(run, EVIL, [{"label": EVIL}, "Không"])
    sysm = await until(lambda: sk.msg_for(DM_BOSS, q["id"]))
    await deliver(sk, sk.press(DM_BOSS, BOSS, btn(sk, sysm, "✍️ Trả lời bằng chữ"), sysm["_ts"]))
    view = await until(lambda: sk.views()[-1] if sk.views() else None)
    assert view["title"]["text"] == "Trả lời câu hỏi" and "Nhập câu trả lời" in view["blocks"][0]["text"]["text"]
    blob = str(view)
    assert "evil" not in blob and "<!channel>" not in blob and "đậm" not in blob  # chữ agent không vào modal
    assert any("hộp vừa mở" in str(p.get("text")) for p in sk.calls_of("chat.postEphemeral"))
    # bàn phím được cấp lại (nonce cũ đã tiêu) — vẫn bấm được số
    assert any(sk.buttons(u) for u in sk.calls_of("chat.update") if u.get("ts") == sysm["_ts"])
    await deliver(sk, sk.submit_view(STRANGER, view, "người lạ trả lời"))
    assert d.hub.questions.get(q["id"])["status"] == "open" and rejected(d, "wrong_user")
    await deliver(sk, sk.submit_view(BOSS, view, "chỉ xoá cache/"))
    got = await until(lambda: (lambda g: g if g["status"] == "answered" else None)(d.hub.questions.get(q["id"])))
    assert got["answer"]["text"] == "chỉ xoá cache/" and got["answered_channel"] == f"slack:{DM_BOSS}"
    assert any("Đã gửi câu trả lời" in t for t in texts(sk, DM_BOSS))
    await deliver(sk, sk.submit_view(BOSS, view, "đổi ý"))  # dùng lại → từ chối
    assert d.hub.questions.get(q["id"])["answer"]["text"] == "chỉ xoá cache/"
