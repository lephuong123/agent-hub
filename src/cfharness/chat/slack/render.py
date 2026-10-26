"""Dịch "ngôn ngữ chung" của lõi chat (chat/base.py) sang Slack: mrkdwn + Block Kit.

An toàn (mrkdwn injection): Slack hiểu `<!channel>`, `<@U…>`, `<http://x|nhãn>` — mọi giá trị động phải escape `& < >`
(`&amp; &lt; &gt;`, đúng như HTML). Chữ `html=True` của lõi ĐÃ escape bằng `esc` → chỉ còn thẻ `<b> <i> <pre> <code>` của hub; chữ
thuần thì escape tại đây. Mọi khối chữ gửi với `verbatim: true` (Slack không tự biến tên kênh/URL/mention thành link).
Nội dung agent nằm trong ``` (khối mã): dấu ` trong đó đổi thành ˋ để agent không thoát được khỏi khối mã.
"""

from __future__ import annotations

import re
from typing import Any

SECTION_MAX = 2900  # Slack: text của section ≤ 3000 ký tự
MAX_BLOCKS = 45  # Slack: ≤ 50 khối/tin
LABEL_MAX = 70  # Slack: nhãn nút ≤ 75 ký tự
FALLBACK_MAX = 300  # `text` dự phòng (thông báo đẩy, trình đọc màn hình)

_TAG = re.compile(r"</?(?:b|i|pre|code)>")
_MARK = {"<b>": "*", "</b>": "*", "<i>": "_", "</i>": "_", "<code>": "`", "</code>": "`", "<pre>": "```\n", "</pre>": "\n```"}
_ENT = re.compile(r"&(amp|lt|gt);")


def escape(text: Any) -> str:
    return str(text if text is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _code_safe(s: str) -> str:
    return s.replace("`", "ˋ")


def html_to_mrkdwn(text: str) -> str:
    """HTML tối giản của hub → mrkdwn. Thẻ lạ không có (lõi chỉ sinh 4 thẻ); `<`/`>` khác đã là `&lt;`/`&gt;`."""
    out: list[str] = []
    pos = 0
    code = 0
    for m in _TAG.finditer(text):
        chunk = text[pos:m.start()]
        out.append(_code_safe(chunk) if code else chunk)
        tag = m.group(0)
        if tag in ("<pre>", "<code>"):
            code += 1
        elif tag in ("</pre>", "</code>"):
            code = max(0, code - 1)
        out.append(_MARK[tag])
        pos = m.end()
    tail = text[pos:]
    out.append(_code_safe(tail) if code else tail)
    return "".join(out)


def to_mrkdwn(text: str, *, html: bool) -> str:
    return html_to_mrkdwn(text) if html else escape(text)


def split_sections(mrkdwn: str, limit: int = SECTION_MAX) -> list[str]:
    """Chia theo dòng thành các đoạn ≤ `limit`; khối ``` bị cắt ngang được đóng/mở lại ở ranh giới."""
    parts: list[str] = []
    cur: list[str] = []
    size = 0
    in_code = False
    for line in mrkdwn.split("\n"):
        while len(line) > limit:  # dòng quá dài (hiếm) → cắt cứng
            head, line = line[:limit - 8], line[limit - 8:]
            if cur:
                parts.append("\n".join(cur) + ("\n```" if in_code else ""))
                cur, size = (["```"] if in_code else []), (4 if in_code else 0)
            parts.append(("```\n" if in_code else "") + head + ("\n```" if in_code else ""))
        if size + len(line) + 1 > limit and cur:
            parts.append("\n".join(cur) + ("\n```" if in_code else ""))
            cur, size = (["```"] if in_code else []), (4 if in_code else 0)
        cur.append(line)
        size += len(line) + 1
        if line.count("```") % 2 == 1:
            in_code = not in_code
    if cur and any(x.strip() for x in cur):
        parts.append("\n".join(cur))
    return parts or [""]


def _style(label: str) -> str | None:
    low = label.lower()
    if low.startswith("xác nhận") or low == "cho phép":
        return "primary"
    if low.startswith("từ chối") or "tắt estop" in low:
        return "danger"
    return None


def buttons_blocks(markup: dict[str, Any] | None) -> list[dict[str, Any]]:
    """`{"inline_keyboard": [[{text, callback_data}]]}` → khối `actions` (giá trị nút = dữ liệu ký của CallbackStore)."""
    if not markup:
        return []
    out: list[dict[str, Any]] = []
    for r, row in enumerate(markup.get("inline_keyboard") or []):
        els = []
        for j, b in enumerate(row):
            if not isinstance(b, dict) or not b.get("callback_data"):
                continue
            el: dict[str, Any] = {"type": "button", "action_id": f"cfh_{r}_{j}", "value": str(b["callback_data"]),
                                  "text": {"type": "plain_text", "text": str(b.get("text") or "?")[:LABEL_MAX], "emoji": True}}
            st = _style(str(b.get("text") or ""))
            if st:
                el["style"] = st
            els.append(el)
        if els:
            out.append({"type": "actions", "elements": els[:25]})
    if markup.get("force_reply"):
        out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "↩️ Trả lời trong thread của tin này.", "verbatim": True}]})
    return out


def message(text: str, markup: dict[str, Any] | None = None, *, html: bool = False) -> dict[str, Any]:
    """→ `{"text", "blocks"}` cho chat.postMessage / chat.update."""
    md = to_mrkdwn(text, html=html)
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": s or " ", "verbatim": True}}
                                    for s in split_sections(md)]
    blocks += buttons_blocks(markup)
    if len(blocks) > MAX_BLOCKS:
        blocks = blocks[:MAX_BLOCKS - 1] + [{"type": "context", "elements": [{"type": "mrkdwn", "text": "…(đã cắt — xem đầy đủ trên web/TUI)"}]}]
    fallback = _ENT.sub(lambda m: {"amp": "&", "lt": "<", "gt": ">"}[m.group(1)], re.sub(r"[*_`]", "", md))
    return {"text": escape(fallback[:FALLBACK_MAX]) or " ", "blocks": blocks}
