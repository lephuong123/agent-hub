"""Báo cáo kết quả task cho người: khung 3 phần cố định "Đã làm / Còn mở / Cần bạn".

- `REPORT_FORMAT`: khối hướng dẫn chép vào prompt của agent làm task (tasks.py) và mô tả tool `task_complete` (hub_self.py) —
  mọi kết quả task kết thúc bằng cùng một khung để hub đọc lại được mà không cần LLM.
- `parse(text)`: tách câu kết quả (dòng đầu) và 3 phần theo tiêu đề (chấp nhận tiếng Việt và tiếng Anh). Không có tiêu đề nào →
  cả văn bản là phần "đã làm".
- `pick_summary(signal, final)`: chọn bản tóm tắt lưu vào task — bản ĐẦY ĐỦ hơn giữa `task_complete(summary)` và tin nhắn cuối.
- `compose_root(subs)`: kết quả task gốc đã tách — gộp tất định các task con thành một báo cáo cùng khung (không id nội bộ).
- `owner_report(task)`: chữ hub tự gửi về hội thoại người đã giao việc khi task gốc xong (manager/service.py).
"""

from __future__ import annotations

import re
from typing import Any

DONE, OPEN, NEED = "done", "open", "need"
LABELS = {DONE: "Đã làm", OPEN: "Còn mở", NEED: "Cần bạn"}

REPORT_FORMAT = (
    "End your final message with the task report in Markdown (people read it, the coordinator forwards it as is). First line: the "
    "outcome in one sentence. Then exactly these three bold headings, each followed by short bullets, in the owner's language "
    "(Vietnamese headings: **Đã làm**, **Còn mở**, **Cần bạn**; English: **Done**, **Still open**, **Needs you**):\n"
    "  - Done: what changed or was found (file paths, PR/branch links, key findings, assumptions you made).\n"
    "  - Still open: what is not finished or not verified, known risks, follow-ups. Write 'Không có' / 'None' if nothing.\n"
    "  - Needs you: the concrete decisions or actions only the human can do (review a PR, give access, choose an option). "
    "Write 'Không cần gì' / 'Nothing' if nothing.\n"
    "  Code or commands in fenced blocks with a language tag. No narration of your process, no tool names, no internal ids."
)

_ALIASES = {
    DONE: ("đã làm", "đã làm được", "kết quả", "done", "what was done", "what i did", "changes"),
    OPEN: ("còn mở", "còn lại", "chưa xong", "still open", "open", "open issues", "remaining", "not done"),
    NEED: ("cần bạn", "cần người dùng", "cần anh", "cần chị", "cần bạn làm", "needs you", "need from you", "needs from you",
           "action needed", "what i need"),
}
_KEY_OF = {a: k for k, al in _ALIASES.items() for a in al}
# tiêu đề: `## Đã làm`, `**Đã làm**`, `**Đã làm:**`, `Đã làm:` (cả dòng), có thể có emoji/ký hiệu đầu dòng
_HEAD = re.compile(r"^[ \t]*(?:#{1,6}[ \t]*)?(?:[^\w\s*#]{1,3}[ \t]*)?(\*\*|__)?[ \t]*([^\n*_:]{2,40}?)[ \t]*:?[ \t]*(?:\1)?[ \t]*:?[ \t]*$",
                   re.M | re.U)
_EMPTY = re.compile(r"^[\s\-*•]*(không có( gì)?|không cần gì( thêm)?|không|chưa có|none|nothing|n/?a|—|-)?[\s.]*$", re.I | re.U)


def _key(m: re.Match[str]) -> str | None:
    line = m.group(0).strip()
    if not (line.startswith("#") or m.group(1) or line.endswith(":")):  # chữ trơn "done" không phải tiêu đề
        return None
    return _KEY_OF.get(re.sub(r"\s+", " ", m.group(2).strip().lower()))


def parse(text: str | None) -> dict[str, str]:
    """→ {"outcome", "done", "open", "need", "structured"}. Dòng trước tiêu đề đầu tiên = câu kết quả (+ phần đã làm nếu không có
    tiêu đề "Đã làm"). Nội dung từng phần giữ nguyên Markdown."""
    s = (text or "").strip()
    out = {"outcome": "", DONE: "", OPEN: "", NEED: "", "structured": ""}
    heads = [(m.start(), m.end(), k) for m in _HEAD.finditer(s) if (k := _key(m))]
    pre = s[: heads[0][0]].strip() if heads else s
    first, _, rest = pre.partition("\n")
    out["outcome"] = first.strip()
    if rest.strip():
        out[DONE] = rest.strip()
    for i, (_, end, k) in enumerate(heads):
        body = s[end: heads[i + 1][0] if i + 1 < len(heads) else len(s)].strip()
        out[k] = (out[k] + "\n" + body).strip() if out[k] and body else (out[k] or body)
    out["structured"] = "1" if heads else ""
    return out


def is_empty(section: str | None) -> bool:
    return not section or bool(_EMPTY.match(section.strip()))


def pick_summary(signal: str | None, final: str | None) -> str:
    """Bản tóm tắt lưu vào task: tin nhắn cuối (viết cho người) trừ khi `task_complete(summary)` đầy đủ hơn rõ rệt."""
    a, b = (signal or "").strip(), (final or "").strip()
    if not a or not b:
        return a or b
    if parse(b)["structured"] and not parse(a)["structured"]:
        return b
    if parse(a)["structured"] and not parse(b)["structured"]:
        return a
    return b if len(b) >= len(a) else a


def _bullets(text: str) -> list[str]:
    """Phần một báo cáo → các ý (giữ khối mã nguyên một ý)."""
    items: list[str] = []
    cur: list[str] = []
    fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fence = not fence
            cur.append(line)
            continue
        m = re.match(r"^[ \t]{0,1}[-*•][ \t]+(.*)$", line) if not fence else None
        if m:
            if cur:
                items.append("\n".join(cur).strip())
            cur = [m.group(1)]
        elif line.strip() or fence:
            cur.append(line if fence or line.startswith("  ") else line.strip())
    if cur:
        items.append("\n".join(cur).strip())
    return [i for i in items if i and not is_empty(i)]


def render(outcome: str, done: list[str], open_: list[str], need: list[str]) -> str:
    def block(k: str, items: list[str], none: str) -> str:
        body = "\n".join(f"- {i}" for i in items) if items else f"- {none}"
        return f"**{LABELS[k]}**\n{body}"

    parts = [outcome.strip()] if outcome.strip() else []
    parts += [block(DONE, done, "—"), block(OPEN, open_, "Không có"), block(NEED, need, "Không cần gì")]
    return "\n\n".join(parts)


def compose_root(subs: list[dict[str, Any]], *, open_extra: list[str] | None = None, need_extra: list[str] | None = None) -> str:
    """Kết quả task gốc đã tách thành task con: mỗi task con một dòng ở "Đã làm"; "Còn mở"/"Cần bạn" gom từ các task con (ghi
    tên phần việc phía trước) + dòng của hub (`open_extra`/`need_extra`, vd nhánh tích hợp của luồng D). Tất định — không gọi LLM,
    không id nội bộ."""
    done, open_, need = [], [], []
    for s in subs:
        p = parse(s.get("result_summary"))
        title = str(s.get("title") or "").strip() or "(không tên)"
        who = f" — {s['assignee_agent_id']}" if s.get("assignee_agent_id") else ""
        line = f"**{title}**{who}: {p['outcome'] or 'xong'}"
        extra = _bullets(p[DONE]) if p["structured"] else ([p[DONE]] if p[DONE] else [])
        if extra:
            line += "\n" + "\n".join(f"  - {x.replace(chr(10), chr(10) + '    ')}" for x in extra[:8])
        done.append(line)
        open_ += [f"{title}: {x}" for x in _bullets(p[OPEN])]
        need += [f"{title}: {x}" for x in _bullets(p[NEED])]
    n = len(subs)
    outcome = f"Đã xong cả {n} phần việc." if n != 1 else "Đã xong phần việc."
    return render(outcome, done, open_ + list(open_extra or []), list(need_extra or []) + need)


def owner_report(task: dict[str, Any]) -> str:
    """Chữ hub gửi người khi task (gốc) xong: tên việc + báo cáo của agent; thiếu phần nào thì nói rõ là agent không ghi
    (không bịa "không cần gì")."""
    title = str(task.get("title") or "").strip()
    summary = str(task.get("result_summary") or "").strip()
    head = f"✅ Xong việc **{title}**" if title else "✅ Xong việc"
    if not summary:
        return head + "\n\n(Agent không để lại báo cáo kết quả — mở task để xem chi tiết.)"
    p = parse(summary)
    tail = []
    if p["structured"] and not p[OPEN] and not p[NEED]:
        tail.append("_(Agent không ghi phần còn mở / cần bạn.)_")
    elif not p["structured"]:
        tail.append("_(Agent không ghi rõ phần còn mở / cần bạn — hỏi lại nếu cần.)_")
    return "\n\n".join([head, summary, *tail])


def digest(text: str | None, limit: int = 700) -> str:
    """Một dòng cho sự kiện tick của điều phối viên: câu kết quả + "Còn mở" + "Cần bạn" trước (thứ nó cần để quyết bước tiếp),
    rồi mới đến phần đã làm nếu còn chỗ."""
    p = parse(text)
    flat = lambda s: re.sub(r"\s+", " ", s).strip()  # noqa: E731
    parts = [flat(p["outcome"])] if p["outcome"] else []
    if p["structured"]:
        parts += [f"Still open: {flat(p[OPEN]) or '-'}", f"Needs owner: {flat(p[NEED]) or '-'}"]
        if p[DONE]:
            parts.append(f"Done: {flat(p[DONE])}")
    elif p[DONE]:
        parts.append(flat(p[DONE]))
    s = " | ".join(parts)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def needs_owner(task: dict[str, Any]) -> bool:
    """Báo cáo có ghi việc người phải làm không (để chọn `notify_kind` need_human thay vì done)."""
    return not is_empty(parse(task.get("result_summary"))[NEED])
