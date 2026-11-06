"""Dịch "ngôn ngữ chung" của lõi chat (chat/base.py) sang Slack: mrkdwn + Block Kit.

An toàn (mrkdwn injection): Slack hiểu `<!channel>`, `<@U…>`, `<http://x|nhãn>` — mọi giá trị động phải escape `& < >`
(`&amp; &lt; &gt;`, đúng như HTML). Chữ `html=True` của lõi ĐÃ escape bằng `esc` → chỉ còn thẻ `<b> <i> <pre> <code>` của hub; chữ
thuần thì escape tại đây. Mọi khối chữ gửi với `verbatim: true` (Slack không tự biến tên kênh/URL/mention thành link).
Nội dung agent nằm trong ``` (khối mã): dấu ` trong đó đổi thành ˋ để agent không thoát được khỏi khối mã. Riêng trả lời của điều phối
viên (`<blockquote>`, bot.agent_frame) là trích dẫn `>` có markdown nhẹ + link http(s) bấm được + tag người dùng `<@U…>` — xem `quote_mrkdwn`.
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


# ---------------------------------------------------------------- khung trả lời agent (`<blockquote>`, chỉ Slack sinh — bot.agent_frame)
# Vào: chữ agent ĐÃ escape `& < >`. Ra: mrkdwn trong trích dẫn. Agent chỉ tạo được `<…>` là link do hàm này dựng (chỉ http/https)
# và mention MỘT người dùng đúng dạng `<@U…>`/`<@W…>` (điều phối viên cần tag người review) — KHÔNG `<!channel>`/`<!here>`/
# `<!subteam^…>`, không `<@U…|nhãn>` giả tên. Link có nhãn trông như URL/tên miền → hiện URL thật (không giả đích).
_BQ = re.compile(r"<blockquote>(.*?)</blockquote>", re.S)
_SMALL = re.compile(r"\n?<small>(.*?)</small>\s*$", re.S)
_URL_CH = r"(?:(?!&lt;|&gt;)[^\s|<>`*])"
_MDLINK = re.compile(r"\[([^\]\n]{1,200})\]\((https?://" + _URL_CH + r"+?)\)")
_BARE = re.compile(r"(?<![<|\w])https?://" + _URL_CH + r"+")
_INLINE = re.compile(r"(`[^`\n]+`)")
_DOMAINISH = re.compile(r"://|^\s*(?:www\.)?[\w-]+(?:\.[\w-]+)+(?:/\S*)?\s*$")
_TRAIL = ".,;:!?'\")]"
_USER_MENTION = re.compile(r"&lt;@([UW][A-Z0-9]{8,11})&gt;")


def _bare_url(m: re.Match) -> str:
    url, tail = m.group(0), ""
    while url and url[-1] in _TRAIL:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url, tail = url[:-1], url[-1] + tail
    return f"<{url}>{tail}" if url.split("://", 1)[1] else m.group(0)


def _md_link(m: re.Match) -> str:
    label, url = m.group(1).strip(), m.group(2)
    if not label or _DOMAINISH.search(label):
        return f"<{url}>"
    return f"<{url}|{label.replace('|', '¦')}>"


def _inline(seg: str) -> str:
    """Một đoạn dòng ngoài `code`: markdown thường gặp → mrkdwn, rồi link."""
    seg = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: f"*{m.group(1) or m.group(2)}*", seg)
    seg = re.sub(r"~~(.+?)~~", r"~\1~", seg)
    out, pos = [], 0
    for m in _MDLINK.finditer(seg):
        out.append(_BARE.sub(_bare_url, seg[pos:m.start()]))
        out.append(_md_link(m))
        pos = m.end()
    out.append(_BARE.sub(_bare_url, seg[pos:]))
    return _USER_MENTION.sub(lambda m: f"<@{m.group(1)}>", "".join(out))


def quote_mrkdwn(body: str) -> str:
    lines: list[str] = []
    fence = False
    for raw in body.split("\n"):
        line = raw.rstrip()
        if line.lstrip().startswith("```"):
            fence = not fence
            lines.append("> ```")
            continue
        if fence:
            lines.append("> " + _code_safe(line))
            continue
        h = re.match(r"\s{0,3}#{1,6}\s+(.*)", line)
        if h:
            line = f"**{h.group(1).strip().strip('*')}**"
        line = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", line)
        line = "".join(p if p.startswith("`") and p.endswith("`") and len(p) > 1 else _inline(p) for p in _INLINE.split(line))
        lines.append("> " + line if line.strip() else ">")
    if fence:
        lines.append("> ```")
    while lines and lines[-1] == ">":
        lines.pop()
    return "\n".join(lines) or "> "


def to_mrkdwn(text: str, *, html: bool) -> str:
    if not html:
        return escape(text)
    out, pos = [], 0
    for m in _BQ.finditer(text):
        out.append(html_to_mrkdwn(text[pos:m.start()]))
        out.append(quote_mrkdwn(m.group(1)))
        pos = m.end()
    out.append(html_to_mrkdwn(text[pos:]))
    return "".join(out)


def split_sections(mrkdwn: str, limit: int = SECTION_MAX) -> list[str]:
    """Chia theo dòng thành các đoạn ≤ `limit`; khối ``` bị cắt ngang được đóng/mở lại ở ranh giới."""
    parts: list[str] = []
    cur: list[str] = []
    size = 0
    in_code = False
    fence = "```"  # khối mã trong trích dẫn (quote_mrkdwn) đóng/mở lại bằng "> ```" để không rơi khỏi trích dẫn
    for line in mrkdwn.split("\n"):
        while len(line) > limit:  # dòng quá dài (hiếm) → cắt cứng
            head, line = line[:limit - 8], line[limit - 8:]
            if cur:
                parts.append("\n".join(cur) + ("\n" + fence if in_code else ""))
                cur, size = ([fence] if in_code else []), (len(fence) + 1 if in_code else 0)
            parts.append((fence + "\n" if in_code else "") + head + ("\n" + fence if in_code else ""))
        if size + len(line) + 1 > limit and cur:
            parts.append("\n".join(cur) + ("\n" + fence if in_code else ""))
            cur, size = ([fence] if in_code else []), (len(fence) + 1 if in_code else 0)
        cur.append(line)
        size += len(line) + 1
        if line.count("```") % 2 == 1:
            in_code = not in_code
            fence = "> ```" if line.startswith(">") else "```"
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
    note = ""
    if html:  # `<small>` cuối tin (chỉ bot.agent_frame sinh) → khối context chữ nhỏ, xám
        m = _SMALL.search(text)
        if m:
            text, note = text[:m.start()], html_to_mrkdwn(m.group(1)).strip()
    md = to_mrkdwn(text, html=html)
    blocks: list[dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": s or " ", "verbatim": True}}
                                    for s in split_sections(md)]
    if note:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": note[:SECTION_MAX], "verbatim": True}]})
    blocks += buttons_blocks(markup)
    if len(blocks) > MAX_BLOCKS:
        blocks = blocks[:MAX_BLOCKS - 1] + [{"type": "context", "elements": [{"type": "mrkdwn", "text": "…(đã cắt — xem đầy đủ trên web/TUI)"}]}]
    plain = re.sub(r"<(https?://[^|>]+)(?:\|([^>]*))?>", lambda m: m.group(2) or m.group(1), md)  # link → nhãn/URL
    plain = re.sub(r"(?m)^> ?", "", plain)
    fallback = _ENT.sub(lambda m: {"amp": "&", "lt": "<", "gt": ">"}[m.group(1)], re.sub(r"[*_`]", "", plain))
    return {"text": escape(fallback[:FALLBACK_MAX]) or " ", "blocks": blocks}
