"""CLI câu hỏi của agent: `agent-hub questions [list|show|escalate|cancel]`, `agent-hub answer <id> [--pick 1,3] [--text …]`."""

from __future__ import annotations

import argparse
from typing import Any

STATUS_VI = {"with_manager": "chờ điều phối viên", "open": "chờ bạn", "answered": "đã trả lời", "cancelled": "đã huỷ"}


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    q = sub.add_parser("questions", help="câu hỏi agent đang chờ trả lời (agent trong task hỏi điều phối viên trước, rồi tới bạn)", parents=[common])
    q.add_argument("--all", action="store_true", help="gồm cả đã trả lời/đã huỷ")
    q.add_argument("--agent")
    qs = q.add_subparsers(dest="q_action")
    ls = qs.add_parser("list", help="liệt kê (mặc định: đang chờ)", parents=[common])
    ls.add_argument("--all", action="store_true", default=argparse.SUPPRESS, help="gồm cả đã trả lời/đã huỷ")
    ls.add_argument("--agent", default=argparse.SUPPRESS)
    sh = qs.add_parser("show", help="chi tiết một câu hỏi", parents=[common])
    sh.add_argument("id")
    es = qs.add_parser("escalate", help="lấy câu hỏi khỏi điều phối viên để tự trả lời", parents=[common])
    es.add_argument("id")
    cc = qs.add_parser("cancel", help="huỷ câu hỏi (agent không được trả lời)", parents=[common])
    cc.add_argument("id")
    cc.add_argument("--reason", default="")
    q.set_defaults(fn=cmd_questions)
    a = sub.add_parser("answer", help="trả lời câu hỏi của agent: agent-hub answer <id> --pick 2 [--text …]", parents=[common])
    a.add_argument("id")
    a.add_argument("--pick", "-p", help="số phương án (nhiều: 1,3)")
    a.add_argument("--text", "-t", help="câu trả lời bằng chữ (thêm hoặc thay lựa chọn)")
    a.set_defaults(fn=cmd_answer)


def print_question(q: dict[str, Any], *, full: bool = False) -> None:
    head = f"{q['id']}  [{STATUS_VI.get(q['status'], q['status'])}]  agent {q['agent_id']}" + (f" · task {q['task_id']}" if q.get("task_id") else "") \
        + (f" · run {q['run_id']}" if q.get("run_id") else "") + f" · {str(q.get('created_at') or '')[:16]}"
    print(head)
    text = q["text"] if full else q["text"].splitlines()[0][:160]
    print("  ❓ " + text.replace("\n", "\n     "))
    for o in q.get("options") or []:
        print(f"     {o['key']}. {o['label']}" + (f" — {o['description']}" if full and o.get("description") else ""))
    if q.get("escalation_note") and full:
        print(f"  Ghi chú khi chuyển lên bạn: {q['escalation_note']}")
    a = q.get("answer")
    if a:
        pick = ", ".join(f"{k}. {lbl}" for k, lbl in zip(a.get("keys") or [], a.get("labels") or []))
        print(f"  ✅ {q.get('answered_by')} ({q.get('answered_channel')}): {pick}" + (f" — {a['text']}" if a.get("text") else ""))
    elif q["status"] in ("open", "with_manager"):
        hint = "--pick <số>" if q.get("options") else '--text "…"'
        print(f"  → agent-hub answer {q['id']} {hint}" + (" (chọn nhiều: 1,3)" if q.get("multi") else ""))


def cmd_questions(ctx: Any) -> int:
    a = ctx.args
    act = getattr(a, "q_action", None) or "list"
    if act == "list":
        rows = ctx.call("question.list", {"status": "all" if a.all else "pending", "agent_id": a.agent, "limit": 50})
        if ctx.json:
            ctx.out(rows)
            return 0
        if not rows:
            print("Không có câu hỏi nào đang chờ.")
            return 0
        for q in rows:
            print_question(q)
        return 0
    if act == "show":
        q = ctx.call("question.get", {"id": a.id})
    elif act == "escalate":
        q = ctx.call("question.escalate", {"id": a.id, "note": "người lấy về tự trả lời"})
    else:
        q = ctx.call("question.cancel", {"id": a.id, "reason": a.reason})
    if ctx.json:
        ctx.out(q)
    else:
        print_question(q, full=True)
    return 0


def cmd_answer(ctx: Any) -> int:
    from ..cli import eprint

    a = ctx.args
    if not a.pick and not a.text:
        q = ctx.call("question.get", {"id": a.id})
        print_question(q, full=True)
        eprint("lỗi: cần --pick <số> hoặc --text \"…\"")
        return 1
    q = ctx.call("question.answer", {"id": a.id, "keys": a.pick or [], "text": a.text, "channel": "cli"})
    if ctx.json:
        ctx.out(q)
    else:
        print_question(q, full=True)
        print("Đã gửi câu trả lời — agent sẽ làm tiếp.")
    return 0
