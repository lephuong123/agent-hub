"""UX — câu hỏi có lựa chọn của agent (questions/): task → điều phối viên → người; trả lời → nối tiếp công việc.

Không gọi claude thật: daemon in-process + ScriptedRuntime (m7_fake). Worker giữ ở `running` để gọi tool hub-self `ask_human`.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agent_hub import auth
from agent_hub.errors import BLOCKED, HubError
from agent_hub.guard.classifier import GuardContext, classify
from agent_hub.guard.policy import PolicyStore
from m7_fake import MgrScript, close, make_daemon, manager_run, rpc, tool, wait_status

OPTS = [{"label": "Chỉ xoá file > 30 ngày"}, {"label": "Xoá cả thư mục cache", "description": "3 GB"}, "Không xoá, chỉ báo cáo"]


class Script(MgrScript):
    """Run manager giữ ở `running`; lần chạy ĐẦU của mỗi task giữ (để gọi ask_human), lần sau trả lời ngay."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: set[str] = set()
        self.worker_prompts: list[str] = []

    def __call__(self, spec):
        out = super().__call__(spec)
        if spec.prompt.startswith("[agent-hub task"):
            self.worker_prompts.append(spec.prompt)
            tid = spec.prompt.split()[2].rstrip("]")
            if tid not in self.seen:
                self.seen.add(tid)
                return {"text": "asked", "cost": 0.001, "hold": True}
        return out


async def settle(cond, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.03)
    raise AssertionError("điều kiện không đạt")


@pytest.fixture
async def env(hub_home):
    s = Script()
    d = await make_daemon(hub_home, s)
    d.hub.agents.create({"id": "w1", "name": "w1", "role": "ops", "model": "haiku", "tool_policy": {"tools": ["Read"]}})
    yield d, s
    await close(d)


async def _ask(d, *, options=OPTS, multi=False, allow_free_text=True):
    t = d.hub.tasks.create({"assignee": "w1", "title": "dọn dữ liệu", "body": "dọn /srv/data/tmp"})
    await settle(lambda: d.hub.tasks.row(t["id"])["current_run_id"] not in (None, "__claiming__"))
    rid = d.hub.tasks.row(t["id"])["current_run_id"]
    await wait_status(d, rid)
    res = d.hub.dispatcher.self_call(rid, "ask_human", {"question": "Xoá kiểu nào?", "options": options, "multi": multi, "allow_free_text": allow_free_text})
    return t, rid, res


async def _end_run(d, rid, task_id):
    d.hub.runs.runtime().release(rid)
    await settle(lambda: d.hub.tasks.row(task_id)["status"] in ("blocked", "ready", "running", "done"))


# ============================================================================ không có điều phối viên → người
async def test_no_manager_goes_to_human_and_answer_resumes_task(env):
    d, s = env
    t, rid, res = await _ask(d)
    q = d.hub.questions.get(res["question_id"])
    assert q["status"] == "open" and q["routed_to"] == "human" and res["asked"] == "the human owner"
    assert [o["key"] for o in q["options"]] == ["1", "2", "3"] and q["options"][1]["description"] == "3 GB"
    await _end_run(d, rid, t["id"])
    await settle(lambda: d.hub.tasks.row(t["id"])["status"] == "blocked")
    assert d.hub.store.get_run(rid)["outcome"] == "blocked"
    out = await rpc(d, "question.answer", {"id": q["id"], "keys": "2", "text": "giữ lại log", "channel": "web"})
    assert out["status"] == "answered" and out["answered_by"] == "user" and out["answered_channel"] == "web"
    assert out["answer"] == {"keys": ["2"], "labels": ["Xoá cả thư mục cache"], "text": "giữ lại log"}
    await settle(lambda: d.hub.tasks.row(t["id"])["status"] == "done")
    cs = d.hub.tasks.comments(t["id"])
    assert cs[-1]["kind"] == "answer" and "Xoá cả thư mục cache" in cs[-1]["text"] and "the human owner" in cs[-1]["text"]
    assert "Xoá cả thư mục cache" in s.worker_prompts[-1]  # worker chạy lại thấy câu trả lời trong bình luận
    assert d.hub.questions.get(q["id"])["resumed_at"]
    kinds = [e.type for e in d.hub.store.query_events(types=["question.open", "question.answered"])]
    assert kinds == ["question.open", "question.answered"]
    audit = [a["kind"] for a in d.hub.audit.query(limit=50)]
    assert "question.answered" in audit


async def test_answer_before_run_ends_resumes_when_task_blocks(env):
    d, s = env
    t, rid, res = await _ask(d)
    await rpc(d, "question.answer", {"id": res["question_id"], "keys": ["1"]})
    assert d.hub.tasks.row(t["id"])["status"] == "running"  # run hỏi còn chạy → chưa nối tiếp
    await _end_run(d, rid, t["id"])
    await settle(lambda: d.hub.tasks.row(t["id"])["status"] == "done")  # blocked → tự gỡ chặn → chạy lại → xong


# ============================================================================ có điều phối viên → hỏi điều phối viên trước
async def test_task_question_goes_to_manager_who_answers(env):
    d, s = env
    m = await manager_run(d)
    t, rid, res = await _ask(d)
    q = d.hub.questions.get(res["question_id"])
    assert q["status"] == "with_manager" and q["routed_to"] == "manager"
    assert any(e["kind"] == "question" and e["question_id"] == q["id"] for e in d.hub.manager.pending)
    listed = await tool(d, m["run_id"], "question_list", {})
    assert [x["id"] for x in listed] == [q["id"]] and listed[0]["options"][0]["label"] == "Chỉ xoá file > 30 ngày"
    await _end_run(d, rid, t["id"])
    await settle(lambda: d.hub.tasks.row(t["id"])["status"] == "blocked")
    assert not any(e["kind"] == "task_blocked" for e in d.hub.manager.pending)  # không báo trùng
    out = await tool(d, m["run_id"], "question_answer", {"question_id": q["id"], "keys": ["3"]})
    assert out["status"] == "answered"
    row = d.hub.questions.get(q["id"])
    assert row["answered_by"] == "manager:boss" and row["answered_channel"] == "manager"
    await settle(lambda: d.hub.tasks.row(t["id"])["status"] == "done")
    assert "the coordinator" in d.hub.tasks.comments(t["id"])[-1]["text"]
    acts = [e.payload for e in d.hub.store.query_events(types=["manager.action"])]
    assert acts[-1]["tool"] == "question_answer" and acts[-1]["ok"] and acts[-1]["target"] == f"question:{q['id']}"


async def test_manager_escalates_then_cannot_answer(env):
    d, s = env
    m = await manager_run(d)
    _, _, res = await _ask(d)
    qid = res["question_id"]
    out = await tool(d, m["run_id"], "question_escalate", {"question_id": qid, "note": "cần anh chọn — liên quan dữ liệu khách"})
    assert out["status"] == "open"
    q = d.hub.questions.get(qid)
    assert q["routed_to"] == "human" and q["escalation_note"].startswith("cần anh")
    assert [e.payload["by"] for e in d.hub.store.query_events(types=["question.escalated"])] == ["manager:boss"]
    with pytest.raises(HubError) as ei:  # lớp 1
        await tool(d, m["run_id"], "question_answer", {"question_id": qid, "keys": ["1"]})
    assert ei.value.code == BLOCKED and "chỉ người" in ei.value.message
    with pytest.raises(HubError) as ei:  # lớp 2: gọi thẳng daemon như điều phối viên
        await rpc(d, "question.answer", {"id": qid, "keys": ["1"]}, as_caller=auth.manager_caller("boss", m["run_id"]))
    assert ei.value.code == BLOCKED and (ei.value.data or {}).get("reason") == "manager_forbidden"
    assert d.hub.questions.get(qid)["status"] == "open"
    await rpc(d, "question.answer", {"id": qid, "keys": ["1"]})  # người vẫn trả lời được


async def test_manager_timeout_escalates_to_human(env):
    d, s = env
    await manager_run(d)
    _, _, res = await _ask(d)
    qid = res["question_id"]
    assert d.hub.questions.check_due()["escalated"] == []
    out = d.hub.questions.check_due(now=time.time() + 901)
    assert out["escalated"] == [qid] and d.hub.questions.get(qid)["status"] == "open"


async def test_manager_unset_reroutes_to_human(env):
    d, s = env
    await manager_run(d)
    _, _, res = await _ask(d)
    await rpc(d, "manager.set", {"agent_id": None})
    await settle(lambda: d.hub.questions.get(res["question_id"])["status"] == "open")


async def test_manager_paused_or_estop_routes_to_human(env):
    d, s = env
    await manager_run(d)
    await d.hub.runs.set_estop(True, reason="test")
    assert d.hub.questions.manager_available("w1") is None
    await d.hub.runs.set_estop(False)
    assert d.hub.questions.manager_available("w1") == "boss"
    assert d.hub.questions.manager_available("boss") is None  # manager không tự hỏi chính mình


# ============================================================================ điều phối viên hỏi người (ask_owner)
async def test_manager_ask_owner_answer_returns_to_conversation(env):
    d, s = env
    m = await manager_run(d)
    out = await tool(d, m["run_id"], "ask_owner", {"question": "Tạo agent mới hay dùng w1?", "options": [{"label": "Tạo mới"}, {"label": "Dùng w1"}]})
    q = d.hub.questions.get(out["question_id"])
    assert q["status"] == "open" and q["source"] == "manager" and q["conversation_id"] == m["conversation_id"]
    with pytest.raises(HubError) as ei:  # manager không tự trả lời câu hỏi của chính mình
        await rpc(d, "question.answer", {"id": q["id"], "keys": ["2"]}, as_caller=auth.manager_caller("boss", m["run_id"]))
    assert ei.value.code == BLOCKED
    d.hub.runs.runtime().release(m["run_id"])  # lượt manager kết thúc
    await wait_status(d, m["run_id"], ("completed",))
    await rpc(d, "question.answer", {"id": q["id"], "keys": ["2"], "channel": "web"})
    await settle(lambda: d.hub.questions.get(q["id"])["resumed_run_id"])
    r2 = d.hub.questions.get(q["id"])["resumed_run_id"]
    run2 = d.hub.store.get_run(r2)
    assert run2["agent_id"] == "boss" and (run2.get("run_opts") or {}).get("manager_conversation") == m["conversation_id"]
    hist = d.hub.manager.history(m["conversation_id"])
    assert hist[-1]["role"] == "user" and "Dùng w1" in hist[-1]["text"]


# ============================================================================ bảo mật: agent không tự trả lời
async def test_agent_caller_cannot_answer_escalate_or_cancel(env):
    d, s = env
    _, rid, res = await _ask(d)
    agent = auth.Caller(via="socket", run_id=rid, agent_id="w1", evidence=["env:AGENT_HUB_RUN_ID"])
    for method in ("question.answer", "question.escalate", "question.cancel"):
        with pytest.raises(HubError) as ei:
            await rpc(d, method, {"id": res["question_id"], "keys": ["1"]}, as_caller=agent)
        assert ei.value.code == BLOCKED, method
    assert d.hub.questions.get(res["question_id"])["status"] == "open"
    for m in ("question.answer", "question.escalate", "question.cancel"):
        assert m in auth.HUMAN_ONLY
    assert set(auth.MANAGER_ALLOWED) & {"question.answer", "question.escalate"} == {"question.answer", "question.escalate"}
    assert "question.cancel" not in auth.MANAGER_ALLOWED


def test_classifier_forbids_agent_answer_cli(tmp_path):
    ctx = GuardContext(cwd=tmp_path, policy=PolicyStore(tmp_path / "h").load())
    for cmd in ("agent-hub answer q_1 --pick 1", "agent-hub questions answer q_1", "agent-hub questions cancel q_1", "agent-hub questions escalate q_1"):
        assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd
    assert not classify("Bash", {"command": "agent-hub questions list"}, ctx).forbidden


# ============================================================================ hợp lệ hoá
async def test_validation(env):
    d, s = env
    _, rid, res = await _ask(d, multi=False, allow_free_text=False)
    qid = res["question_id"]
    for params, msg in (({"keys": ["9"]}, "không có"), ({"keys": ["1", "2"]}, "một phương án"), ({"text": "tuỳ"}, "chỉ nhận"), ({}, "cần chọn")):
        with pytest.raises(HubError) as ei:
            await rpc(d, "question.answer", {"id": qid, **params})
        assert msg in ei.value.message
    with pytest.raises(HubError):
        d.hub.dispatcher.self_call(rid, "ask_human", {"question": "x", "options": [f"o{i}" for i in range(7)]})
    with pytest.raises(HubError):
        d.hub.dispatcher.self_call(rid, "ask_human", {"question": "  "})
    await rpc(d, "question.answer", {"id": qid, "keys": ["1"]})
    with pytest.raises(HubError) as ei:
        await rpc(d, "question.answer", {"id": qid, "keys": ["2"]})
    assert "đã answered" in ei.value.message


async def test_multi_and_sanitize(env):
    d, s = env
    evil = "A‮B​\x07 C " + "x" * 200
    _, _, res = await _ask(d, options=[evil, "Hai", "hai", ""], multi=True)
    q = d.hub.questions.get(res["question_id"])
    assert [o["label"] for o in q["options"]][1] == "Hai" and len(q["options"]) == 2  # bỏ trùng (không phân biệt hoa thường) + rỗng
    lbl = q["options"][0]["label"]
    assert "‮" not in lbl and "​" not in lbl and "\x07" not in lbl and len(lbl) <= 80
    out = await rpc(d, "question.answer", {"id": q["id"], "keys": "1, 2"})
    assert out["answer"]["keys"] == ["1", "2"]


async def test_task_done_cancels_pending_question(env):
    d, s = env
    t, rid, res = await _ask(d)
    d.hub.dispatcher.self_call(rid, "task_complete", {"summary": "tự quyết rồi"})  # agent đổi ý: hoàn tất thay vì chờ
    d.hub.runs.runtime().release(rid)
    await settle(lambda: d.hub.questions.get(res["question_id"])["status"] == "cancelled")


async def test_reminder_backoff(env):
    d, s = env
    _, _, res = await _ask(d)
    now = time.time()
    assert d.hub.questions.check_due(now=now + 10)["reminded"] == []
    assert d.hub.questions.check_due(now=now + 901)["reminded"] == [res["question_id"]]
    assert d.hub.questions.check_due(now=now + 1000)["reminded"] == []  # lần 2 giãn gấp đôi
    ev = d.hub.store.query_events(types=["question.reminder"])
    assert ev[-1].payload["reminder_count"] == 1


async def test_cli_answer_roundtrip(env, capsys):
    from agent_hub.questions.cli import print_question

    d, s = env
    _, _, res = await _ask(d)
    print_question(d.hub.questions.get(res["question_id"]), full=True)
    out = capsys.readouterr().out
    assert "chờ bạn" in out and "1. Chỉ xoá file > 30 ngày" in out and f"agent-hub answer {res['question_id']} --pick" in out
