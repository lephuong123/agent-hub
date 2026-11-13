"""Chấm offline phong cách câu trả lời THẬT (RPC chỉ đọc `eval.style`, CLI `cfharness eval style`). Chỉ SELECT, không ghi DB.

Nguồn:
- mặc định: tin `manager` / `notify` của điều phối viên (`manager_messages`) trong khoảng `since`; yêu cầu = tin `user` cùng run.
  Bỏ: tin lỗi do hub viết (`chatview.FAIL_PREFIX`), câu trả lời nhịp chỉ có `NO_ACTION` (tín hiệu nhịp, không gửi người).
- `run_id`: tin điều phối viên của run đó; run không phải điều phối viên → `runs.summary` với yêu cầu = `runs.prompt`.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..errors import NotFound
from .style import RULES, StyleOptions, check_style

FAIL_PREFIX = "(Điều phối viên không trả lời được:"  # = manager.chatview.FAIL_PREFIX (không import để module này nhẹ)


def _item(src: dict[str, Any], reply: str, request: str, opts: StyleOptions) -> dict[str, Any]:
    r = check_style(reply, request, opts)
    return {**src, "score": r.score, "passed": r.passed, "chars": len(reply),
            "violations": [{"rule": v.rule, "detail": v.detail, "samples": v.samples} for v in r.violations],
            "checked": r.checked, "preview": " ".join(reply.split())[:120]}


def _request_for(conn, run_id: str | None) -> str:
    if not run_id:
        return ""
    row = conn.execute("SELECT text FROM manager_messages WHERE run_id=? AND role='user' ORDER BY id LIMIT 1", (run_id,)).fetchone()
    return str(row[0]) if row else ""


def _is_tick(conn, run_id: str | None) -> bool:
    return bool(run_id) and conn.execute("SELECT 1 FROM manager_messages WHERE run_id=? AND role='tick' LIMIT 1", (run_id,)).fetchone() is not None


def score_replies(store, *, run_id: str | None = None, since: str | None = None, agent_id: str | None = None,
                  conversation_id: str | None = None, limit: int = 50, options: dict[str, Any] | None = None) -> dict[str, Any]:
    from ..reports import _iso, parse_since

    conn = store.conn
    opts = StyleOptions.from_spec(options)
    items: list[dict[str, Any]] = []
    skipped: Counter[str] = Counter()
    if run_id:
        run = store.find_run(run_id)
        rid = run["id"] if run else run_id
        rows = conn.execute("SELECT id, conversation_id, role, text, run_id, ts FROM manager_messages WHERE run_id=? AND role IN ('manager','notify') ORDER BY id",
                            (rid,)).fetchall()
        if not rows and run is None:
            raise NotFound(f"không tìm thấy run '{run_id}'")
        if not rows and run is not None:
            reply = str(run.get("summary") or "")
            if not reply.strip():
                skipped["empty"] += 1
            else:
                items.append(_item({"source": "run", "run_id": run["id"], "agent_id": run.get("agent_id"), "kind": run.get("kind"),
                                    "ts": run.get("ended_at")}, reply, str(run.get("prompt") or ""), opts))
    else:
        where = ["m.role IN ('manager','notify')", "m.ts>=?"]
        args: list[Any] = [_iso(parse_since(since))]
        if agent_id:
            where.append("c.agent_id=?")
            args.append(agent_id)
        if conversation_id:
            where.append("m.conversation_id LIKE ?")
            args.append(conversation_id + "%")
        args.append(max(1, int(limit)))
        rows = conn.execute("SELECT m.id, m.conversation_id, m.role, m.text, m.run_id, m.ts FROM manager_messages m "
                            "JOIN manager_conversations c ON c.id=m.conversation_id WHERE " + " AND ".join(where) +
                            " ORDER BY m.id DESC LIMIT ?", args).fetchall()
    for row in rows:
        mid, cid, role, text, rid, ts = row[0], row[1], row[2], str(row[3] or ""), row[4], row[5]
        if text.startswith(FAIL_PREFIX):
            skipped["hub_failure"] += 1
            continue
        if not text.strip() or text.strip() == "(không có nội dung)":
            skipped["empty"] += 1
            continue
        tick = role == "manager" and _is_tick(conn, rid)
        if tick and text.strip() == "NO_ACTION":
            skipped["tick_no_action"] += 1
            continue
        items.append(_item({"source": "manager", "message_id": mid, "conversation_id": cid, "role": "tick" if tick else role,
                            "run_id": rid, "ts": ts}, text, "" if tick else _request_for(conn, rid), opts))
    by_rule = Counter(v["rule"] for it in items for v in it["violations"])
    n = len(items)
    return {
        "items": items,
        "summary": {"n": n, "passed": sum(1 for it in items if it["passed"]),
                    "avg_score": round(sum(it["score"] for it in items) / n, 4) if n else None,
                    "by_rule": {r: by_rule[r] for r in RULES if by_rule[r]}, "skipped": dict(skipped)},
        "options": {k: getattr(opts, k) for k in ("short_request_chars", "short_budget", "min_vi_ratio", "min_words", "default_lang")}
                   | {"skip": list(opts.skip)},
    }
