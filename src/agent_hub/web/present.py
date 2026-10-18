"""Định dạng thuần cho giao diện web (không phụ thuộc Starlette) — dùng lại logic hiển thị của TUI (`tui/model.py`).

- Nhãn trạng thái agent/run/task bằng tiếng Việt dễ hiểu cho người không quen terminal.
- `transcript_item(ev)`: event (dict wire) → mục transcript (dict) để template `_items.html` vẽ; dùng chung cho phần
  phát lại từ DB lúc mở trang và cho luồng SSE live.
- `activity_item(ev)`: dòng "hoạt động gần đây" (firehose mức summary, FR-UI-09) — tái dùng `tui.model.activity_line`.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from ..tui.model import activity_line, fmt_cost, fmt_dur, fmt_tokens, parse_ts, short_tool

__all__ = [
    "AGENT_STATUS", "RUN_STATUS", "TASK_STATUS", "fmt_cost", "fmt_dur", "fmt_tokens", "short_tool", "fmt_ts", "ago",
    "md", "transcript_item", "parse_ts", "wait_reason", "activity_item", "agent_status", "run_status", "is_live_run", "first_line", "tokens_of",
]

# (biểu tượng, nhãn, tông màu CSS)
AGENT_STATUS: dict[str, tuple[str, str, str]] = {
    "working": ("●", "Đang làm", "ok"),
    "waiting": ("◐", "Chờ duyệt", "warn"),
    "waiting_human": ("⏸", "Chờ bạn duyệt", "danger"),
    "idle": ("○", "Rảnh", "muted"),
    "error": ("✖", "Lỗi", "danger"),
    "paused": ("‖", "Tạm dừng", "muted"),
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
    return AGENT_STATUS.get(s or "", ("?", s or "—", "muted"))


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
        return {**base, "kind": "note", "tone": "info", "text": f"⤷ subagent {p.get('type')}: {p.get('description') or ''}"}
    if t == "approval.waiting_human" and not (p.get("preview") or p.get("tool")):
        return {**base, "kind": "note", "tone": "danger", "text": "⏸ Quá thời gian giữ — agent tạm dừng và CHỜ BẠN duyệt (không tự từ chối)"}
    if t in ("approval.open", "approval.waiting_human"):
        return {**base, "kind": "approval", "human": t == "approval.waiting_human", "tool": p.get("tool") or "", "preview": p.get("preview") or "",
                "rule": p.get("matched_rule") or "", "reason": p.get("reason") or "", "id": p.get("request_id") or p.get("approval_id")}
    if t == "approval.resolved":
        dec = p.get("decision")
        label = {"allowed": "✓ Đã cho phép", "denied": "✖ Đã từ chối"}.get(dec, f"■ {dec}")
        return {**base, "kind": "note", "tone": "ok" if dec == "allowed" else "warn",
                "text": f"{label} bởi {p.get('by') or '?'} qua {p.get('channel') or '?'}" + (f" — {p['note']}" if p.get("note") else "")}
    if t == "user.message":
        return {**base, "kind": "steer", "text": p.get("text") or "", "meta": f"{p.get('by') or 'user'} qua {p.get('channel') or '?'}"}
    if t == "steer.ack":
        return {**base, "kind": "note", "tone": "info", "text": "✓ Agent đã nhận chỉ đạo"}
    if t in ("run.completed", "run.failed", "run.cancelled", "run.interrupted", "run.waiting_human") and ev.get("source", "hub") == "hub":
        oc = p.get("outcome") or t.split(".")[1]
        label, tone = run_status(oc)
        dur = (p.get("duration_ms") or 0) / 1000
        return {**base, "kind": "end", "tone": tone, "text": f"■ {label} · {fmt_dur(dur) if dur else '—'} · {p.get('num_turns') or '?'} lượt · ${float(p.get('cost_usd') or 0):.4f}",
                "error": str(p.get("error") or "")[:400] if oc != "completed" else ""}
    if t in ("error", "isolation.violation", "permission.denied", "guard.blocked", "auth.blocked", "budget.exceeded", "auth.denied"):
        detail = p.get("message") or p.get("detail") or p.get("name") or p.get("tool_name") or p
        return {**base, "kind": "note", "tone": "danger", "text": f"✖ {t}: {str(detail)[:300]}"}
    if t == "run.takeover":
        return {**base, "kind": "note", "tone": "info", "text": "⇄ Người tiếp quản bằng Claude Code"}
    return None


def stats_of(ev: dict[str, Any]) -> dict[str, Any] | None:
    p = ev.get("payload") or {}
    if ev.get("type") == "usage.tick":
        return {"tokens": fmt_tokens(p.get("total_tokens")), "cost": fmt_cost(p.get("cost_usd"))}
    return None


# ============================================================================ firehose
_TONE = {
    "run.completed": "ok", "approval.resolved": "ok", "run.failed": "danger", "guard.blocked": "danger", "approval.waiting_human": "danger",
    "run.waiting_human": "danger", "error": "danger", "approval.open": "warn", "run.interrupted": "warn", "hub.estop": "danger",
    "user.message": "info", "steer.ack": "info", "run.started": "info",
}


def activity_item(ev: dict[str, Any]) -> dict[str, Any] | None:
    line = activity_line(ev, {})
    if line is None:
        return None
    plain = line.plain
    t = ev.get("type", "")
    tone = _TONE.get(t, "danger" if t in ("isolation.violation", "permission.denied", "auth.blocked", "budget.exceeded", "notify.failed") else "")
    return {"ts": plain[:8].strip(), "agent": ev.get("agent_id") or "hub", "text": plain[24:].strip(), "tone": tone, "run_id": ev.get("run_id")}


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
