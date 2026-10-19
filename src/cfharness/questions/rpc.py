"""RPC câu hỏi (UX). Chữ ký cho web/Telegram/TUI:

- `question.list {status?: pending|with_manager|open|answered|cancelled|all, agent_id?, run_id?, task_id?, routed_to?, limit?}` → `[Question]`
- `question.get {id}` → Question = `{id, agent_id, run_id, task_id, conversation_id, source, text, options:[{key,label,description}], multi,
  allow_free_text, status, routed_to, escalation_note, answer:{keys,labels,text}|null, answered_by, answered_channel, created_at, …}`
- `question.answer {id, keys?: [str]|"1,3", text?, channel?}` (HUMAN_ONLY; điều phối viên được gọi qua MANAGER_ALLOWED khi câu hỏi
  `with_manager`) → Question. `by` lấy từ NGƯỜI GỌI, không tin tham số.
- `question.escalate {id, note?}` (HUMAN_ONLY; điều phối viên qua MANAGER_ALLOWED) → Question
- `question.cancel {id, reason?}` (HUMAN_ONLY) → Question
Event: `question.open|escalated|answered|cancelled|reminder`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..errors import HubError

if TYPE_CHECKING:
    from ..daemon import Daemon

MUTATING_QUESTIONS = ("question.answer", "question.escalate", "question.cancel")


def _id(p: dict[str, Any]) -> str:
    v = p.get("id") or p.get("question_id")
    if not v:
        raise HubError("thiếu tham số 'id'", code=-32602)
    return str(v)


def _who(p: dict[str, Any]) -> tuple[str, str]:
    """(by, channel) theo NGƯỜI GỌI thật: điều phối viên → `manager:<id>`/manager; người → user + kênh (web/telegram:<id>/cli)."""
    from ..auth import current_caller

    c = current_caller()
    if c is not None and c.manager:
        return f"manager:{c.manager}", "manager"
    ch = str(p.get("channel") or "")
    if c is not None and c.via not in ("socket", "internal"):
        ch = c.via  # kênh trong daemon (telegram:<chat>) — không tin tham số
    return "user", ch or "cli"


def register_rpc(daemon: "Daemon") -> None:
    from ..mcp_server.rpc import wrap_with_audit

    qs = daemon.hub.questions  # type: ignore[attr-defined]

    def _answer(p: dict[str, Any]) -> dict[str, Any]:
        by, ch = _who(p)
        return qs.answer(_id(p), keys=p.get("keys"), text=p.get("text"), by=by, channel=ch)

    def _escalate(p: dict[str, Any]) -> dict[str, Any]:
        by, ch = _who(p)
        return qs.escalate(_id(p), by=by, note=p.get("note"), channel=ch)

    def _cancel(p: dict[str, Any]) -> dict[str, Any]:
        by, ch = _who(p)
        return qs.cancel(_id(p), reason=str(p.get("reason") or ""), by=by, channel=ch)

    methods: dict[str, Callable[[dict[str, Any]], Any]] = {
        "question.list": lambda p: qs.list(p.get("status") or "pending", agent_id=p.get("agent_id"), run_id=p.get("run_id"), task_id=p.get("task_id"),
                                           routed_to=p.get("routed_to"), limit=int(p.get("limit") or 100)),
        "question.get": lambda p: qs.get(_id(p)),
        "question.answer": _answer,
        "question.escalate": _escalate,
        "question.cancel": _cancel,
    }
    for name in MUTATING_QUESTIONS:
        methods[name] = wrap_with_audit(daemon.hub, name, methods[name])
    daemon.methods.update(methods)
