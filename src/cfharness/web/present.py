"""Định dạng thuần cho giao diện web (không phụ thuộc Starlette) — dùng lại logic hiển thị của TUI (`tui/model.py`).

- Nhãn trạng thái agent/run/task bằng tiếng Việt dễ hiểu cho người không quen terminal.
- `transcript_item(ev)`: event (dict wire) → mục transcript (dict) để template `_items.html` vẽ; dùng chung cho phần
  phát lại từ DB lúc mở trang và cho luồng SSE live.
- `activity_item(ev)`: dòng "hoạt động gần đây" (firehose mức summary, FR-UI-09) — câu tiếng Việt do hub sinh, chỉ sự kiện có nghĩa (UX v2).
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from ..tui.model import fmt_cost, fmt_dur, fmt_tokens, parse_ts, short_tool

__all__ = [
    "AGENT_STATUS", "RUN_STATUS", "TASK_STATUS", "fmt_cost", "fmt_dur", "fmt_tokens", "short_tool", "fmt_ts", "ago",
    "md", "transcript_item", "parse_ts", "wait_reason", "activity_item", "agent_status", "run_status", "is_live_run", "first_line", "tokens_of",
    "run_title", "question_view", "QUESTION_STATUS", "avatar_tone", "iso_ts",
]

# (icon trong /static/icons.svg, nhãn, tông màu CSS)
AGENT_STATUS: dict[str, tuple[str, str, str]] = {
    "working": ("dot", "Đang làm", "ok"),
    "waiting": ("shield", "Chờ duyệt", "warn"),
    "waiting_human": ("pause", "Chờ bạn duyệt", "danger"),
    "asking": ("question", "Chờ bạn trả lời", "ask"),
    "idle": ("circle", "Rảnh", "muted"),
    "error": ("x", "Lỗi", "danger"),
    "paused": ("pause", "Tạm dừng", "muted"),
}
RUN_STATUS: dict[str, tuple[str, str]] = {
    "queued": ("Đang xếp hàng", "info"),
    "running": ("Đang chạy", "ok"),
    "waiting_human": ("Chờ bạn duyệt", "danger"),
    "completed": ("Xong", "ok"),
    "failed": ("Lỗi", "danger"),
    "cancelled": ("Đã dừng", "muted"),
    "interrupted": ("Bị gián đoạn", "warn"),
    "budget_exceeded": ("Vượt ngân sách", "warn"),
    "timed_out": ("Quá giờ", "warn"),
    "crashed": ("Tiến trình lỗi", "danger"),
    "rate_limited": ("Bị giới hạn tốc độ", "warn"),
    "blocked": ("Bị chặn", "danger"),
    "protocol_violation": ("Sai giao thức", "danger"),
    "resumed": ("Đã tiếp tục ở run khác", "muted"),
}
TASK_STATUS: dict[str, tuple[str, str]] = {
    "triage": ("Chờ phân loại", "warn"),
    "todo": ("Cần làm", "info"),
    "ready": ("Sẵn sàng", "info"),
    "running": ("Đang làm", "ok"),
    "review": ("Đang review", "info"),
    "blocked": ("Bị chặn", "danger"),
    "done": ("Xong", "muted"),
    "archived": ("Đã lưu trữ", "muted"),
}
TASK_COLUMNS = ("triage", "todo", "ready", "running", "review", "blocked", "done")
LIVE_RUN = ("queued", "running", "waiting_human")
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


WAIT_REASON = {
    "workspace_locked": "thư mục đang bị run khác giữ", "budget": "hết ngân sách", "estop": "ESTOP đang bật", "team_estop": "ESTOP team",
    "concurrency": "hết slot chạy", "agent_busy": "agent đang bận", "agent_paused": "agent tạm dừng", "rate_limit": "giới hạn tốc độ",
    "backoff": "chờ thử lại", "no_assignee": "chưa có người nhận", "member_limit": "thành viên đủ việc",
}


def wait_reason(r: str | None) -> str:
    return WAIT_REASON.get(r or "", r or "")


def agent_status(s: str | None) -> tuple[str, str, str]:
    return AGENT_STATUS.get(s or "", ("circle", s or "—", "muted"))


def run_status(s: str | None) -> tuple[str, str]:
    return RUN_STATUS.get(s or "", (s or "—", "muted"))


def is_live_run(run: dict[str, Any] | None) -> bool:
    return bool(run) and run.get("status") in LIVE_RUN  # type: ignore[union-attr]


def tokens_of(usage: Any) -> int:
    if isinstance(usage, str):
        try:
            usage = json.loads(usage)
        except json.JSONDecodeError:
            return 0
    if not isinstance(usage, dict):
        return 0
    return sum(int(usage.get(k) or 0) for k in USAGE_KEYS)


def fmt_ts(s: str | None, *, date: bool | None = None) -> str:
    t = parse_ts(s)
    if t is None:
        return "—"
    d = datetime.fromtimestamp(t)
    today = d.date() == datetime.now().date()
    if date is False or (date is None and today):
        return d.strftime("%H:%M:%S")
    return d.strftime("%d/%m %H:%M")


def ago(s: str | None, now: float | None = None) -> str:
    t = parse_ts(s)
    if t is None:
        return "—"
    return fmt_dur((now or time.time()) - t) + " trước"


def first_line(s: str | None, n: int = 120) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    line = s.splitlines()[0]
    return line[:n] + ("…" if len(line) > n or len(s.splitlines()) > 1 else "")


_MD: Any = None


def md(text: str | None) -> str:
    """Markdown → HTML AN TOÀN (HTML thô trong văn bản bị escape, link chỉ http/https/mailto). Không có thư viện → <pre>."""
    global _MD
    from markupsafe import escape

    text = text or ""
    try:
        if _MD is None:
            from markdown_it import MarkdownIt

            m = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
            m.validateLink = lambda url: url.strip().lower().startswith(("http://", "https://", "mailto:"))  # type: ignore[method-assign]
            _MD = m
        return str(_MD.render(text))
    except Exception:  # pragma: no cover — markdown-it-py đi kèm rich/textual
        return f"<pre>{escape(text)}</pre>"


# ============================================================================ transcript
def _diff_lines(name: str, inp: Any) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if not isinstance(inp, dict):
        return out
    if name in ("Edit", "MultiEdit"):
        edits = inp.get("edits") or [{"old_string": inp.get("old_string"), "new_string": inp.get("new_string")}]
        for e in edits[:3]:
            out += [("del", ln) for ln in str(e.get("old_string") or "").splitlines()[:8]]
            out += [("add", ln) for ln in str(e.get("new_string") or "").splitlines()[:8]]
    elif name == "Write" and inp.get("content"):
        lines = str(inp["content"]).splitlines()
        out += [("add", ln) for ln in lines[:8]]
        if len(lines) > 8:
            out.append(("more", f"… (+{len(lines) - 8} dòng)"))
    return out


def transcript_item(ev: dict[str, Any]) -> dict[str, Any] | None:
    """Một event → một mục hiển thị (None = bỏ qua). `dom` (nếu có) = id phần tử để thay tại chỗ (tool đang chạy → xong)."""
    t = ev.get("type", "")
    p = ev.get("payload") or {}
    base = {"seq": ev.get("seq"), "ts": fmt_ts(ev.get("ts"), date=False), "sub": bool(ev.get("subagent_id"))}
    if ev.get("subagent_id") and t in ("message.delta", "reasoning.delta", "message.complete", "reasoning.progress"):
        return None
    if t == "message.complete":
        text = p.get("text") or ""
        return {**base, "kind": "msg", "html": md(text)} if text.strip() else None
    if t == "reasoning.complete":
        return {**base, "kind": "reasoning", "text": p.get("text") or ""}
    if t == "run.queued":
        budget = p.get("max_budget_usd")
        return {**base, "kind": "prompt", "text": p.get("prompt_preview") or "", "meta": f"{p.get('kind') or 'adhoc'} · ngân sách {'$' + str(budget) if budget else '—'}" + (" · tiếp tục phiên cũ" if p.get("resume") else "")}
    if t == "run.started":
        mcp = [str(m.get("name")) for m in p.get("mcp_servers") or [] if isinstance(m, dict)]
        return {**base, "kind": "start", "text": f"Bắt đầu · model {p.get('model') or '?'} · quyền {p.get('permission_mode') or '?'}", "mcp": mcp}
    if t == "tool.start":
        name = p.get("name") or "?"
        return {**base, "kind": "tool", "dom": f"tool-{p.get('tool_id') or ev.get('seq')}", "name": short_tool(name), "summary": p.get("args_preview") or "",
                "running": True, "diff": _diff_lines(name, p.get("input") or {})}
    if t == "tool.complete":
        name = p.get("name") or "?"
        prev = (p.get("result_preview") or "").strip()
        return {**base, "kind": "tool", "dom": f"tool-{p.get('tool_id') or ev.get('seq')}", "name": short_tool(name), "summary": p.get("args_preview") or "",
                "running": False, "dur": p.get("duration_s"), "error": bool(p.get("is_error")), "result": prev[:600], "diff": []}
    if t == "subagent.spawn":
        return {**base, "kind": "note", "tone": "info", "text": f"Subagent {p.get('type')}: {p.get('description') or ''}"}
    if t in ("approval.open", "approval.waiting_human"):
        # một yêu cầu = MỘT mục (dom theo id): waiting_human thay tại chỗ mục approval.open (không lặp 2–3 lần)
        aid = p.get("request_id") or p.get("approval_id")
        return {**base, "kind": "approval", "dom": f"ev-ap-{aid}" if aid else None, "human": t == "approval.waiting_human", "tool": p.get("tool") or "",
                "preview": p.get("preview") or "", "rule": p.get("matched_rule") or "", "reason": p.get("reason") or "", "id": aid}
    if t == "approval.resolved":
        dec = p.get("decision")
        label = {"allowed": "Đã cho phép", "denied": "Đã từ chối", "cancelled": "Đã huỷ yêu cầu duyệt"}.get(dec, str(dec))
        aid = p.get("request_id")
        return {**base, "kind": "approval", "resolved": True, "decision": dec, "dom": f"ev-ap-{aid}" if aid else None, "tool": p.get("tool") or "",
                "preview": p.get("preview") or "", "id": aid,
                "label": f"{label} bởi {p.get('by') or '?'} qua {p.get('channel') or '?'}" + (f" — {p['note']}" if p.get("note") else "")}
    if t in ("question.open", "question.escalated", "question.reminder"):
        qid = p.get("question_id")
        who = "điều phối viên" if p.get("routed_to") == "manager" else "bạn"
        return {**base, "kind": "question", "dom": f"ev-q-{qid}" if qid else None, "id": qid, "text": p.get("text") or "", "answered": False,
                "label": f"Agent hỏi {who}" + (" (điều phối viên chuyển lên bạn)" if t == "question.escalated" else "")}
    if t == "question.answered":
        qid = p.get("question_id")
        pick = "; ".join(f"{k}. {lbl}" for k, lbl in zip(p.get("keys") or [], p.get("labels") or []))
        who = "điều phối viên" if str(p.get("by") or "").startswith("manager:") else "bạn"
        return {**base, "kind": "question", "dom": f"ev-q-{qid}" if qid else None, "id": qid, "answered": True,
                "label": f"Đã được {who} trả lời qua {p.get('channel') or '?'}", "text": " — ".join(x for x in (pick, p.get("text") or "") if x)}
    if t == "question.cancelled":
        qid = p.get("question_id")
        return {**base, "kind": "note", "tone": "muted", "dom": f"ev-q-{qid}" if qid else None, "text": f"Câu hỏi đã huỷ ({p.get('reason') or ''})"}
    if t == "user.message":
        return {**base, "kind": "steer", "text": p.get("text") or "", "meta": f"{p.get('by') or 'user'} qua {p.get('channel') or '?'}"}
    if t == "steer.ack":
        return {**base, "kind": "note", "tone": "info", "text": "Agent đã nhận chỉ đạo"}
    if t in ("run.completed", "run.failed", "run.cancelled", "run.interrupted", "run.waiting_human") and ev.get("source", "hub") == "hub":
        oc = p.get("outcome") or t.split(".")[1]
        label, tone = run_status(oc)
        dur = (p.get("duration_ms") or 0) / 1000
        return {**base, "kind": "end", "tone": tone, "text": f"{label} · {fmt_dur(dur) if dur else '—'} · {p.get('num_turns') or '?'} lượt · ${float(p.get('cost_usd') or 0):.4f}",
                "error": str(p.get("error") or "")[:400] if oc != "completed" else ""}
    if t in ("error", "isolation.violation", "permission.denied", "guard.blocked", "auth.blocked", "budget.exceeded", "auth.denied"):
        detail = p.get("message") or p.get("detail") or p.get("name") or p.get("tool_name") or p
        return {**base, "kind": "note", "tone": "danger", "text": f"{EVENT_VI.get(t, t)}: {str(detail)[:300]}"}
    if t == "run.takeover":
        return {**base, "kind": "note", "tone": "info", "text": "Người tiếp quản bằng Claude Code"}
    return None


def stats_of(ev: dict[str, Any]) -> dict[str, Any] | None:
    p = ev.get("payload") or {}
    if ev.get("type") == "usage.tick":
        return {"tokens": fmt_tokens(p.get("total_tokens")), "cost": fmt_cost(p.get("cost_usd"))}
    return None


# ============================================================================ firehose ("Hoạt động gần đây")
# Chỉ sự kiện CÓ NGHĨA với người (bắt đầu/xong/lỗi/duyệt/hỏi/task/học/điều phối viên) — câu chữ tiếng Việt do HUB sinh; chữ của
# agent (prompt, câu hỏi) chỉ là phần trích ngắn, template autoescape. Tool/lời agent từng bước không vào đây (xem trang run).
EVENT_VI = {
    "error": "Lỗi", "isolation.violation": "Vi phạm cô lập", "permission.denied": "Bị từ chối quyền", "auth.blocked": "claude chưa đăng nhập",
    "budget.exceeded": "Vượt ngân sách", "auth.denied": "Bị chặn (chỉ người)", "guard.blocked": "Guard chặn", "notify.failed": "Gửi thông báo lỗi",
}
_LEARN_VI = {"learning.applied": "tự học: áp dụng thay đổi", "learning.rolled_back": "tự học: đã rollback", "learning.eval_failed": "tự học: eval không đạt",
             "learning.canary": "tự học: đang thử (canary)", "learning.promoted": "tự học: đã chốt thay đổi"}


def _clip(s: Any, n: int = 90) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def activity_text(ev: dict[str, Any]) -> tuple[str, str] | None:
    """(câu tiếng Việt, tông) cho một event, hoặc None = không hiện (nhiễu)."""
    t = ev.get("type", "")
    p = ev.get("payload") or {}
    if ev.get("subagent_id"):
        return None
    if t in ("run.completed", "run.failed", "run.cancelled", "run.interrupted") and ev.get("source", "hub") == "hub":
        oc = p.get("outcome") or t.split(".")[1]
        label, tone = run_status(oc)
        dur = (p.get("duration_ms") or 0) / 1000
        extra = f" · {fmt_dur(dur)}" if dur else ""
        err = f" — {_clip(p.get('error'), 70)}" if p.get("error") and oc != "completed" else ""
        return f"{label.lower()} · ${float(p.get('cost_usd') or 0):.4f}{extra}{err}", {"ok": "ok", "muted": ""}.get(tone, tone)
    if t in ("approval.open", "approval.waiting_human"):
        what = _clip(p.get("preview") or p.get("tool"), 70)
        return (f"chờ bạn duyệt: {what}", "danger") if t == "approval.waiting_human" else (f"xin duyệt: {what}", "warn")
    if t == "approval.resolved":
        dec = {"allowed": "được cho phép", "denied": "bị từ chối", "cancelled": "đã huỷ"}.get(p.get("decision"), str(p.get("decision")))
        return f"thao tác {dec} (bởi {p.get('by') or '?'} qua {p.get('channel') or '?'})", "ok" if p.get("decision") == "allowed" else ""
    if t == "question.open":
        who = "điều phối viên" if p.get("routed_to") == "manager" else "bạn"
        return f"hỏi {who}: “{_clip(p.get('text'), 80)}”", "ask"
    if t == "question.escalated":
        return f"câu hỏi được chuyển lên bạn: “{_clip(p.get('text'), 70)}”", "ask"
    if t == "question.answered":
        who = "điều phối viên" if str(p.get("by") or "").startswith("manager:") else "bạn"
        return f"câu hỏi đã được {who} trả lời", "ok"
    if t == "task.status":
        to = p.get("to")
        if to not in ("running", "done", "blocked", "review"):
            return None
        label, tone = TASK_STATUS.get(to or "", (to, ""))
        return f"task {p.get('task_id') or ''} → {label.lower()}" + (f": {_clip(p.get('reason'), 60)}" if to == "blocked" and p.get("reason") else ""), \
            {"muted": "ok"}.get(tone, tone)
    if t == "manager.action":
        return _clip(p.get("summary") or p.get("tool"), 100), "danger" if p.get("ok") is False else "info"
    if t in _LEARN_VI:
        return _LEARN_VI[t], "warn" if t in ("learning.rolled_back", "learning.eval_failed") else ""
    if t == "user.message":
        return f"bạn chỉ đạo qua {p.get('channel') or '?'}: “{_clip(p.get('text'), 70)}”", "info"
    if t == "hub.estop":
        return (f"ESTOP BẬT {p.get('reason') or ''}".strip(), "danger") if p.get("on") else ("ESTOP đã tắt", "ok")
    if t in EVENT_VI and t != "notify.failed":
        return f"{EVENT_VI[t]}: {_clip(p.get('message') or p.get('detail') or p.get('rule') or p.get('name') or '', 70)}", "danger"
    return None


def activity_item(ev: dict[str, Any]) -> dict[str, Any] | None:
    r = activity_text(ev)
    if r is None:
        return None
    text, tone = r
    return {"ts": fmt_ts(ev.get("ts"), date=False), "iso": ev.get("ts"), "agent": ev.get("agent_id") or "hub", "text": text, "tone": tone,
            "run_id": ev.get("run_id"), "task_id": ev.get("task_id") or (ev.get("payload") or {}).get("task_id")}


# ============================================================================ run, câu hỏi, avatar
_HUB_HEAD = __import__("re").compile(r"^\s*\[cfharness[^\]]*\]\s*")


def run_title(run: dict[str, Any], titles: dict[str, str] | None = None, n: int = 110) -> str:
    """Tiêu đề người đọc được: tiêu đề task nếu run thuộc task; không thì dòng đầu prompt (bỏ tiêu đề `[cfharness …]`)."""
    tid = run.get("task_id")
    if tid and titles and titles.get(tid):
        return first_line(titles[tid], n)
    if run.get("kind") == "manager":
        pr = str(run.get("prompt") or "")
        return "Điều phối viên · nhịp kiểm tra" if pr.lstrip().startswith("[cfharness heartbeat") else "Điều phối viên · trả lời tin nhắn"
    s = _HUB_HEAD.sub("", str(run.get("prompt") or ""))
    return first_line(s, n) or "—"


QUESTION_STATUS = {"with_manager": ("Đang chờ điều phối viên", "info"), "open": ("Chờ bạn trả lời", "ask"), "answered": ("Đã trả lời", "ok"),
                   "cancelled": ("Đã huỷ", "muted")}


def question_view(q: dict[str, Any]) -> dict[str, Any]:
    """Câu hỏi → dữ liệu cho thẻ (chữ agent giữ nguyên — template autoescape + pre-wrap)."""
    a = q.get("answer") or {}
    pick = [f"{k}. {lbl}" for k, lbl in zip(a.get("keys") or [], a.get("labels") or [])]
    by = str(q.get("answered_by") or "")
    who = "Điều phối viên" if by.startswith("manager:") else ("Hub" if by == "hub" else "Bạn")
    label, tone = QUESTION_STATUS.get(q.get("status") or "", (q.get("status") or "", "muted"))
    return {"status_label": label, "status_tone": tone, "pending": q.get("status") in ("with_manager", "open"), "pick": pick,
            "answer_text": a.get("text") or "", "answered_by": who, "options": q.get("options") or [], "single_quick": bool(q.get("options")) and not q.get("multi")}


def avatar_tone(name: str | None) -> str:
    return f"c{sum(ord(c) for c in (name or '?')) % 5}"


def iso_ts(s: Any) -> str:
    """ISO cho thuộc tính `datetime` của <time> (JS hiện thời gian tương đối)."""
    t = parse_ts(s)
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds") if t else ""


# ============================================================================ xem đầy đủ thao tác chờ duyệt (WT-2)
import re as _re

_DANGER = _re.compile(
    r"(?<![\w-])(rm|rmdir|unlink|shred|truncate|dd|mkfs[\w.]*|mv|cp|rsync|chmod|chown|kill|pkill|killall|sudo|crontab|launchctl|systemctl|"
    r"curl|wget|tee|find|xargs|sqlite3|psql|mysql)(?![\w-])"
    r"|git\s+(push|reset|clean|checkout|restore|branch\s+-D|filter-branch|rebase)|--force(?:-with-lease)?\b|--delete\b|--hard\b|-(?:rf|fr|r|f)\b"
    r"|\b(DROP|DELETE|TRUNCATE|ALTER|UPDATE|INSERT)\b|>>?|\|\s*(?:sh|bash|zsh)\b",
    _re.IGNORECASE,
)
_INVISIBLE = _re.compile("[\u0000-\u0008\u000b\u000c\u000d\u000e-\u001f\u007f​-‏‪-‮⁠-⁤⁦-⁩﻿]")
LONG_ACTION = 400


def _visible(ch: str) -> str:
    return {"\r": "␍"}.get(ch, f"⟦U+{ord(ch):04X}⟧")


def highlight(text: str) -> list[tuple[str, str]]:
    """Văn bản → các đoạn (loại, chữ): `''` thường, `'hit'` phần nguy hiểm (mark), `'ctl'` ký tự ẩn/điều khiển/bidi hiện thành ký hiệu."""
    out: list[tuple[str, str]] = []
    pos = 0
    marks = sorted([(m.start(), m.end(), "hit") for m in _DANGER.finditer(text)] + [(m.start(), m.end(), "ctl") for m in _INVISIBLE.finditer(text)])
    for s, e, k in marks:
        if s < pos:
            continue
        if s > pos:
            out.append(("", text[pos:s]))
        seg = text[s:e]
        out.append((k, "".join(_visible(c) for c in seg) if k == "ctl" else seg))
        pos = e
    if pos < len(text):
        out.append(("", text[pos:]))
    return out


def action_view(ap: dict[str, Any]) -> dict[str, Any]:
    """Toàn bộ thao tác (KHÔNG cắt): Bash → lệnh đầy đủ; tool khác (kể cả `mcp__*`) → mọi tham số dạng JSON."""
    inp = ap.get("tool_input")
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except json.JSONDecodeError:
            inp = {"input": inp}
    tool = ap.get("tool_name") or ""
    if tool == "Bash" and isinstance(inp, dict) and isinstance(inp.get("command"), str):
        text = inp["command"]
        extra = {k: v for k, v in inp.items() if k not in ("command", "description")}
        if extra:
            text += "\n\n# tham số khác: " + json.dumps(extra, ensure_ascii=False)
    elif isinstance(inp, dict) and inp:
        text = json.dumps(inp, ensure_ascii=False, indent=2, default=str)
    else:
        text = ap.get("preview") or ""
    lines = text.count("\n") + 1
    return {"tool": short_tool(tool) if tool.startswith("mcp__") else tool, "tool_full": tool, "text": text, "segments": highlight(text),
            "length": len(text), "lines": lines, "long": len(text) > LONG_ACTION or lines > 8,
            "hidden_chars": bool(_INVISIBLE.search(text))}


# ============================================================================ Điều phối viên (M7 — MGR-UI)
# Mọi chữ do điều phối viên viết (trả lời, tóm tắt hành động, đích) là DỮ LIỆU: template vẽ bằng autoescape, CSS pre-wrap —
# không markdown, không HTML thô, không link do agent tự đặt (link duy nhất là link nội bộ dựng từ loại + id đích).
MGR_TARGET_KINDS: dict[str, tuple[str, str]] = {
    # loại đích → (nhãn, tiền tố URL nội bộ)
    "agent": ("agent", "/agents/"),
    "task": ("task", "/tasks/"),
    "run": ("run", "/runs/"),
    "team": ("team", "/teams#team-"),
    "approval": ("yêu cầu duyệt", "/approvals#"),
    "learning": ("thay đổi học", "/learnings/"),
    "mcp": ("MCP", "/mcp/"),
}
_TOOL_KIND = (("agent_", "agent"), ("team_", "team"), ("task_", "task"), ("run_", "run"), ("learn_", "learning"))
_ID_SAFE = __import__("re").compile(r"^[A-Za-z0-9_.:\-]{1,128}$")


def mgr_target(target: Any, tool: str | None = None) -> dict[str, Any]:
    """Đích của `manager.action` → {kind, id, label, href|None}. Nhận "agent:x", {"kind": "agent", "id": "x"} hoặc id trần
    (đoán loại theo tên tool, vd `agent_create`). Chỉ dựng link NỘI BỘ cho loại đã biết + id an toàn — không bao giờ dùng URL do agent đưa."""
    from urllib.parse import quote

    kind: str | None = None
    ident: str | None = None
    if isinstance(target, dict):
        kind = str(target.get("kind") or target.get("type") or "") or None
        ident = str(target.get("id") or target.get("name") or "") or None
    elif target is not None:
        s = str(target)
        k, sep, rest = s.partition(":")
        if sep and k in MGR_TARGET_KINDS:
            kind, ident = k, rest
        else:
            ident = s
    if kind is None and tool:
        t = str(tool).rsplit("__", 1)[-1].removeprefix("hub_").removeprefix("manager_")
        kind = next((v for p, v in _TOOL_KIND if t.startswith(p)), None)
    label_kind, prefix = MGR_TARGET_KINDS.get(kind or "", (kind or "", ""))
    href = (prefix + quote(ident, safe="")) if prefix and ident and _ID_SAFE.match(ident) else None
    label = (f"{label_kind} {ident}" if label_kind else str(ident or "")).strip()
    return {"kind": kind, "id": ident, "label": label, "href": href}


def mgr_action_item(ev: dict[str, Any]) -> dict[str, Any]:
    """Event `manager.action` (dict wire) → mục thẻ hành động."""
    p = ev.get("payload") or {}
    tool = str(p.get("tool") or "")
    tgt = mgr_target(p.get("target"), tool)
    ok = p.get("ok", True) is not False and not p.get("error")
    return {"kind": "action", "tool": short_tool(tool) if tool else "", "summary": str(p.get("summary") or tool or "hành động"),
            "target": tgt, "run_id": ev.get("run_id"), "ts": ev.get("ts"), "audit_id": p.get("audit_id"), "ok": ok,
            "error": str(p.get("error") or "") or None}


def mgr_message(m: dict[str, Any]) -> dict[str, Any] | None:
    """Mục `manager.history` → {kind: user|manager, text, ts, run_id}. Chấp nhận vài tên khoá (role/author, text/content)."""
    role = str(m.get("role") or m.get("author") or m.get("kind") or "").lower()
    text = m.get("text") if m.get("text") is not None else m.get("content")
    if role in ("user", "human", "owner"):
        kind = "user"
    elif role in ("manager", "assistant", "agent", "notify", "reply", "tick"):
        kind = "manager"
    else:
        return None
    return {"kind": kind, "text": str(text or ""), "ts": m.get("ts") or m.get("created_at"), "run_id": m.get("run_id"),
            "notify": role == "notify" or bool(m.get("notify")), "tick": role == "tick"}


def mgr_conv(c: dict[str, Any]) -> dict[str, Any]:
    cid = c.get("id") or c.get("conversation_id")
    n = c.get("messages")
    return {"id": cid, "channel": c.get("channel"), "updated_at": c.get("updated_at") or c.get("last_at") or c.get("created_at"),
            "created_at": c.get("created_at"), "count": n if isinstance(n, int) else (len(n) if isinstance(n, list) else c.get("message_count")),
            "title": first_line(str(c.get("title") or c.get("preview") or ""), 60)}
