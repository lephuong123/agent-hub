"""Giọng nói trên web (docs/dev-notes/voice.md): tin gửi bằng giọng (`voice=dictation|conversation`) KHÔNG BAO GIỜ duyệt approval hay
chốt câu hỏi xin phép qua phím tắt chat; điều phối viên nhận ghi chú "tin giọng nói"; header cho phép micro cùng origin; trang Điều phối
có nút micro + hội thoại giọng nói. Không gọi claude, không chạy trình duyệt (Web Speech API ở client)."""

from __future__ import annotations

from cfharness.manager import prompts
from m7_fake import manager_run, rpc, tool, wait_status

from test_owner_permission import _approval, _end_manager_turn, env  # noqa: F401  (fixture daemon + ScriptedRuntime)
from test_web_manager import designate, menv  # noqa: F401  (fixture web + daemon in-process)


def _prompt(d, run_id: str) -> str:
    return d.hub.store.get_run(run_id)["prompt"]


# ============================================================================ daemon: không duyệt bằng giọng nói
async def test_voice_bare_duyet_never_approves(env):
    d, _ = env
    m = await manager_run(d)
    ap = await _approval(d)
    await _end_manager_turn(d, m["run_id"])
    res = await rpc(d, "manager.chat", {"channel": "web", "text": "duyệt", "conversation_id": m["conversation_id"], "voice": "conversation"})
    assert d.hub.approvals.get(ap["id"])["status"] == "open"
    p = _prompt(d, res["run_id"])
    assert "BY VOICE" in p and "never approves" in p
    assert prompts.VOICE_CONVERSATION_NOTE in p


async def test_voice_explicit_approve_id_never_approves(env):
    d, _ = env
    m = await manager_run(d)
    ap = await _approval(d)
    await _end_manager_turn(d, m["run_id"])
    res = await rpc(d, "manager.chat", {"channel": "web", "text": f"approve {ap['id']}", "conversation_id": m["conversation_id"], "voice": "dictation"})
    assert d.hub.approvals.get(ap["id"])["status"] == "open"
    p = _prompt(d, res["run_id"])
    assert "BY VOICE" in p and prompts.VOICE_DICTATION_NOTE in p and prompts.VOICE_CONVERSATION_NOTE not in p


async def test_voice_ok_does_not_answer_permission_question(env):
    d, _ = env
    m = await manager_run(d)
    out = await tool(d, m["run_id"], "ask_owner_permission", {"kind": "sandbox_localhost", "agent": "w1"})
    await _end_manager_turn(d, m["run_id"])
    res = await rpc(d, "manager.chat", {"channel": "web", "text": "ok", "conversation_id": m["conversation_id"], "voice": "conversation"})
    assert d.hub.questions.get(out["question_id"])["status"] == "open"
    assert "BY VOICE" in _prompt(d, res["run_id"])


async def test_typed_ok_still_answers_permission_question(env):
    """Đối chứng: cùng tin gõ tay (không voice) vẫn chạy phím tắt như cũ."""
    d, _ = env
    m = await manager_run(d)
    out = await tool(d, m["run_id"], "ask_owner_permission", {"kind": "sandbox_localhost", "agent": "w1"})
    await _end_manager_turn(d, m["run_id"])
    await rpc(d, "manager.chat", {"channel": "web", "text": "ok", "conversation_id": m["conversation_id"]})
    assert d.hub.questions.get(out["question_id"])["status"] != "open"


async def test_voice_answers_ordinary_question(env):
    d, _ = env
    m = await manager_run(d)
    q = d.hub.questions.open(d.hub.store.get_run(m["run_id"]), "Giữ hay xoá thư mục cache?", ["Giữ", "Xoá"], conversation_id=m["conversation_id"])
    await _end_manager_turn(d, m["run_id"])
    res = await rpc(d, "manager.chat", {"channel": "web", "text": "Giữ.", "conversation_id": m["conversation_id"], "voice": "conversation"})
    assert d.hub.questions.get(q["id"])["status"] != "open"
    assert "BY VOICE" not in _prompt(d, res["run_id"])  # câu hỏi thường: trả lời được, không có cảnh báo duyệt


async def test_unknown_voice_value_ignored(env):
    d, _ = env
    m = await manager_run(d)
    await _end_manager_turn(d, m["run_id"])
    res = await rpc(d, "manager.chat", {"channel": "web", "text": "chào", "conversation_id": m["conversation_id"], "voice": "<x>"})
    assert "[hub] The owner dictated" not in _prompt(d, res["run_id"])


# ============================================================================ web
async def test_web_voice_header_and_controls(menv):
    await menv.login()
    await designate(menv)
    r = await menv.client.get("/manager")
    assert r.status_code == 200
    pp = r.headers["permissions-policy"]
    assert "microphone=(self)" in pp and "camera=()" in pp
    assert 'id="mgr-mic"' in r.text and 'data-voice-for="mgr-text"' in r.text and 'id="mgr-voice"' in r.text
    assert '<input type="hidden" name="voice" value="">' in r.text
    assert "/static/voice.js" in r.text
    js = await menv.client.get("/static/voice.js")
    assert js.status_code == 200
    assert "innerHTML" not in js.text and "eval(" not in js.text  # chữ agent chỉ qua textContent / speechSynthesis


async def test_web_send_voice_reaches_manager(menv):
    await menv.login()
    await designate(menv)
    r = await menv.post("/manager/send", {"text": "tình hình hôm nay thế nào", "voice": "conversation"}, headers={"X-Ahub-Fetch": "1"})
    assert r.status_code == 200, r.text
    j = r.json()
    if menv.stub is None:  # manager thật: ghi chú giọng nói nằm trong prompt của run
        await wait_status(menv.d, j["run_id"], ("running", "completed"))
        assert prompts.VOICE_CONVERSATION_NOTE in menv.hub.store.get_run(j["run_id"])["prompt"]
    r2 = await menv.post("/manager/send", {"text": "x", "voice": "javascript:"}, headers={"X-Ahub-Fetch": "1"})
    assert r2.status_code in (200, 409), r2.text  # giá trị lạ bị bỏ qua, không lỗi


async def test_dictation_textareas_marked(menv):
    await menv.login()
    r = await menv.client.get("/")
    assert r.status_code == 200
    assert "data-voice" in r.text
