"""Điều phối viên xin phép người bằng nút (`ask_owner_permission`, manager/owner_actions.py) + phím tắt trả lời trong chat +
guard ít chạm người (`guard.hold_uncertain=false`) + guard tắt vẫn dùng được tool của agent.

Không gọi claude thật: daemon in-process + ScriptedRuntime (m7_fake).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from cfharness import auth
from cfharness.errors import BLOCKED, HubError
from cfharness.guard.classifier import Verdict
from m7_fake import MgrScript, close, make_daemon, manager_run, rpc, tool, wait_status


async def settle(cond, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.03)
    raise AssertionError("điều kiện không đạt")


class Script(MgrScript):
    """Run worker giữ ở `running` (để mở approval trên run đang chạy)."""

    def __call__(self, spec):
        out = super().__call__(spec)
        if spec.prompt.startswith("[cfharness task"):
            return {"text": "x", "cost": 0.001, "hold": True}
        return out


@pytest.fixture
async def env(hub_home):
    s = Script()
    d = await make_daemon(hub_home, s)
    d.hub.agents.create({"id": "w1", "name": "w1", "role": "ops", "model": "haiku", "tool_policy": {"tools": ["Bash", "Read"]}})
    yield d, s
    await close(d)


async def _end_manager_turn(d, run_id):
    d.hub.runs.runtime().release(run_id)
    await wait_status(d, run_id, ("completed",))


def _manager_prompts(s):
    return [sp.prompt for sp in s.specs if sp.prompt.lstrip().startswith("[cfharness · OWNER")]


# ============================================================================ nút "Cho phép" → hub thực hiện với quyền người
async def test_sandbox_localhost_granted_by_owner_button(env):
    d, s = env
    m = await manager_run(d)
    out = await tool(d, m["run_id"], "ask_owner_permission", {"kind": "sandbox_localhost", "agent": "w1", "reason": "cần đọc NocoDB 127.0.0.1:8080"})
    q = d.hub.questions.get(out["question_id"])
    assert q["status"] == "open" and q["routed_to"] == "human" and q["conversation_id"] == m["conversation_id"]
    assert q["action"]["method"] == "guard.sandbox.set" and q["action"]["params"] == {"id": "w1", "allow_localhost": True}
    assert "`w1`" in q["text"] and "localhost" in q["text"] and "Lý do (điều phối viên ghi): cần đọc NocoDB" in q["text"]
    assert [o["label"] for o in q["options"]] == ["Cho phép", "Từ chối"]
    # hỏi lại cùng thao tác → không tạo câu hỏi thứ hai
    again = await tool(d, m["run_id"], "ask_owner_permission", {"kind": "sandbox_localhost", "agent": "w1"})
    assert again["question_id"] == q["id"]
    # điều phối viên KHÔNG tự trả lời được (câu hỏi của người; xin phép chỉ người)
    with pytest.raises(HubError) as ei:
        await rpc(d, "question.answer", {"id": q["id"], "keys": ["1"]}, as_caller=auth.manager_caller("boss", m["run_id"]))
    assert ei.value.code == BLOCKED
    with pytest.raises(HubError):
        d.hub.questions.answer(q["id"], keys=["1"], by="manager:boss", channel="manager")
    assert not d.hub.guard.sandbox_overrides("w1").get("allow_localhost")
    await _end_manager_turn(d, m["run_id"])
    await rpc(d, "question.answer", {"id": q["id"], "keys": ["1"], "channel": "web"})
    await settle(lambda: d.hub.questions.get(q["id"])["resumed_run_id"])
    assert d.hub.guard.sandbox_overrides("w1")["allow_localhost"] is True
    assert d.hub.questions.get(q["id"])["answer"]["action_result"] == {"ok": True}
    assert "[hub] Đã thực hiện sandbox_localhost cho agent:w1" in _manager_prompts(s)[-1]
    acts = [e.payload for e in d.hub.store.query_events(types=["question.action"])]
    assert acts and acts[-1]["ok"] is True and acts[-1]["target"] == "agent:w1"
    audit = d.hub.audit.query(limit=50)
    assert any(a["kind"] == "guard.sandbox_override" and a["channel"] == "web" for a in audit)  # kênh trả lời của người


async def test_owner_denies_nothing_happens(env):
    d, s = env
    m = await manager_run(d)
    out = await tool(d, m["run_id"], "ask_owner_permission", {"kind": "sandbox_domain", "agent": "w1", "domain": "api.github.com"})
    await _end_manager_turn(d, m["run_id"])
    await rpc(d, "question.answer", {"id": out["question_id"], "keys": ["2"]})
    await settle(lambda: d.hub.questions.get(out["question_id"])["resumed_run_id"])
    assert "api.github.com" not in (d.hub.guard.sandbox_overrides("w1").get("allow_domains") or [])
    assert "KHÔNG cho phép" in _manager_prompts(s)[-1]


async def test_build_refuses_unsafe_requests(env):
    d, s = env
    m = await manager_run(d)
    bad = [
        {"kind": "sandbox_localhost", "agent": "boss"},  # agent điều phối viên
        {"kind": "agent_update", "agent": "boss", "changes": {"model": "opus"}},
        {"kind": "agent_update", "agent": "w1", "changes": {"tool_policy": {"permission_mode": "bypassPermissions"}}},
        {"kind": "agent_update", "agent": "w1", "changes": {"tool_policy": {"allow_bypass": True}}},
        {"kind": "agent_update", "agent": "w1", "changes": {"extra_args": ["--x"]}},
        {"kind": "agent_update", "agent": "w1", "changes": {"subagents": {}}},
        {"kind": "sandbox_domain", "agent": "w1", "domain": "evil.com/../x"},
        {"kind": "purge", "agent": "w1"},
        {"kind": "approve_action", "approval_id": "ap_nope"},
    ]
    for a in bad:
        with pytest.raises(HubError):
            await tool(d, m["run_id"], "ask_owner_permission", a)
    assert d.hub.questions.list("pending") == []


async def test_agent_update_shows_diff_and_applies_as_human(env):
    d, s = env
    m = await manager_run(d)
    out = await tool(d, m["run_id"], "ask_owner_permission",
                     {"kind": "agent_update", "agent": "w1", "changes": {"limits": {"max_budget_usd_per_run": 9}, "tool_policy": {"tools": ["Bash", "Read", "Edit"]}}})
    q = d.hub.questions.get(out["question_id"])
    assert "- limits:" in q["text"] and "→" in q["text"] and "Edit" in q["text"]
    assert q["action"]["params"]["changes"]["tool_policy"]["tools"] == ["Bash", "Read", "Edit"]
    await _end_manager_turn(d, m["run_id"])
    await rpc(d, "question.answer", {"id": q["id"], "keys": ["1"]})
    await settle(lambda: d.hub.questions.get(q["id"])["resumed_run_id"])
    row = d.hub.store.get_agent("w1")
    assert row["limits"]["max_budget_usd_per_run"] == 9 and "Edit" in row["tool_policy"]["tools"]


# ============================================================================ phím tắt trong chat (tin người)
async def _approval(d):
    t = d.hub.tasks.create({"assignee": "w1", "title": "dọn", "body": "dọn /tmp/x"})
    await settle(lambda: d.hub.tasks.row(t["id"])["current_run_id"] not in (None, "__claiming__"))
    rid = d.hub.tasks.row(t["id"])["current_run_id"]
    run = await wait_status(d, rid)
    return d.hub.approvals.open(run, tool_name="Bash", tool_input={"command": "rm -rf /tmp/x"}, tool_use_id=None, fingerprint="fp1",
                                verdict={"verdict": "destructive", "rule": "rm_rf", "klass": "file", "reason": "xoá đệ quy"}, channel="hook")


async def test_chat_reply_ok_answers_permission_question(env):
    d, s = env
    m = await manager_run(d)
    ap = await _approval(d)
    out = await tool(d, m["run_id"], "ask_owner_permission", {"kind": "approve_action", "approval_id": ap["id"]})
    q = d.hub.questions.get(out["question_id"])
    assert "rm -rf /tmp/x" in q["text"] and ap["id"] in q["text"]  # hub dựng từ approval thật, đủ lệnh
    await _end_manager_turn(d, m["run_id"])
    res = await rpc(d, "manager.chat", {"channel": "web", "text": "ok", "conversation_id": m["conversation_id"]})
    assert d.hub.approvals.get(ap["id"])["status"] == "allowed"
    assert d.hub.approvals.get(ap["id"])["resolved_by"] == "user"
    qa = d.hub.questions.get(q["id"])
    assert qa["status"] == "answered" and qa["answered_channel"] == "web" and qa["answer"]["action_result"]["ok"] and qa["resumed_at"]
    run = d.hub.store.get_run(res["run_id"])
    assert "[hub] Đã thực hiện approve_action" in run["prompt"]  # ghi chú của hub đi cùng lượt chat
    await wait_status(d, res["run_id"])
    await asyncio.sleep(0.1)
    assert len(_manager_prompts(s)) == 2  # không đẻ thêm lượt


async def test_chat_explicit_approve_command(env):
    d, s = env
    m = await manager_run(d)
    ap = await _approval(d)
    await _end_manager_turn(d, m["run_id"])
    res = await rpc(d, "manager.chat", {"channel": "cli", "text": f"!cfharness approve {ap['id']}", "reuse_latest": True})
    assert d.hub.approvals.get(ap["id"])["status"] == "allowed"
    assert "[hub] Người đã CHO PHÉP approval" in d.hub.store.get_run(res["run_id"])["prompt"]


async def test_chat_bare_duyet_with_single_pending_approval_and_deny(env):
    d, s = env
    m = await manager_run(d)
    ap = await _approval(d)
    await _end_manager_turn(d, m["run_id"])
    await rpc(d, "manager.chat", {"channel": "web", "text": f"từ chối {ap['id']} nguy hiểm", "conversation_id": m["conversation_id"]})
    a = d.hub.approvals.get(ap["id"])
    assert a["status"] == "denied" and a["note"] == "nguy hiểm"


async def test_chat_bare_approve_only_when_whole_message(env):
    d, s = env
    m = await manager_run(d)
    ap = await _approval(d)
    await _end_manager_turn(d, m["run_id"])
    r1 = await rpc(d, "manager.chat", {"channel": "web", "text": "duyệt giúp t cái kế hoạch", "conversation_id": m["conversation_id"]})
    assert d.hub.approvals.get(ap["id"])["status"] == "open"
    await wait_status(d, r1["run_id"])
    await _end_manager_turn(d, r1["run_id"])
    await rpc(d, "manager.chat", {"channel": "web", "text": "approve tiếp đi", "conversation_id": m["conversation_id"]})
    assert d.hub.approvals.get(ap["id"])["status"] == "allowed"


async def test_chat_ordinary_text_untouched(env):
    d, s = env
    m = await manager_run(d)
    out = await tool(d, m["run_id"], "ask_owner_permission", {"kind": "sandbox_localhost", "agent": "w1"})
    await _end_manager_turn(d, m["run_id"])
    res = await rpc(d, "manager.chat", {"channel": "web", "text": "ok nhưng trước hết hãy kiểm tra lại xem NocoDB có đang chạy không đã", "conversation_id": m["conversation_id"]})
    assert d.hub.questions.get(out["question_id"])["status"] == "open"  # câu dài → không phải câu trả lời
    assert "[hub]" not in d.hub.store.get_run(res["run_id"])["prompt"]


# ============================================================================ guard ít chạm người
def test_hold_uncertain_off_only_destructive_asks(env):
    d, _ = env
    g = d.hub.guard
    unc = Verdict("uncertain", "code_write_dynamic", "file", "x")
    hubv = Verdict("uncertain", "sensitive_dynamic", "hub", "x")
    mcp = Verdict("uncertain", "mcp_require_approval", "mcp", "x")
    dest = Verdict("destructive", "rm_rf", "file", "x")
    assert all(g.needs_human(v) for v in (unc, hubv, mcp, dest))  # mặc định: như cũ
    d.hub.config.set("guard.hold_uncertain", False)
    assert not g.needs_human(unc)
    assert g.needs_human(hubv) and g.needs_human(mcp) and g.needs_human(dest)
    assert not g.needs_human(Verdict("write", "generic_cmd", None, ""))


def test_guard_off_preallows_agent_tools_and_keeps_static_deny(env):
    d, _ = env
    from cfharness.runtime.claude_cli import build_argv

    d.hub.config.set("guard.enabled", False)
    d.hub.agents.create({"id": "w2", "name": "w2", "role": "dev", "model": "haiku"})
    d.hub.agents.create({"id": "rv", "name": "rv", "role": "review", "model": "haiku", "tool_policy": {"permission_mode": "plan", "tools": ["Read"]}})
    for aid, want in (("w1", ["Bash", "Read"]), ("w2", ["Bash", "Edit", "Write"]), ("rv", [])):
        run = d.hub.store.get_run(d.hub.runs.submit(aid, "hi")["id"])
        spec = d.hub.runs._prepare_spec(run, d.hub.store.get_agent(aid))
        allowed = spec.tool_policy.get("allowed_tools") or []
        for t in want:
            assert t in allowed, (aid, t, allowed)
        if aid == "rv":
            assert "Bash" not in allowed
        assert "Bash(git push --force:*)" in spec.tool_policy["disallowed_tools"]
        assert spec.permission_prompt_tool is None
        argv, _ = build_argv(spec, d.hub.config.get("runtime.claude"))
        if want:
            assert "--allowedTools" in argv or "--allowed-tools" in argv
