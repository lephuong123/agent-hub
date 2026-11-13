"""Markdown do agent viết → định dạng của kênh chat (P0-10 Telegram, P1-9 Slack) + chia tin theo khối markdown.

Luồng: markdown-it-py (`commonmark` + `table`, `strikethrough`; `html=False` — HTML agent viết chỉ là CHỮ) → danh sách khối trung
gian (`parse`) → `to_telegram_html` / `to_slack_mrkdwn`. Mọi chữ agent đều được escape; CHỈ bộ chuyển đổi sinh thẻ/cú pháp của kênh.

Hợp đồng an toàn (WT-8 nới có kiểm soát — docs/dev-notes/telegram.md, slack.md):
- Telegram: chỉ thẻ `b i s code pre` (+ `code class="language-x"` với tên ngôn ngữ đã lọc). KHÔNG `<a>`: link http(s) hiện dạng chữ
  `nhãn (URL)` (nhãn trùng URL / trông như tên miền → chỉ URL thật); link khác (javascript:, mailto:, đường dẫn…) → chỉ nhãn.
  Chữ `/lệnh` của agent nằm trong `<code>` (Telegram không biến thành lệnh bấm được). Không dùng `<blockquote>` (trích dẫn → `▎ `).
- Slack: escape `& < >`; chỉ link `<http(s)…|nhãn>` do hàm này dựng + mention MỘT người `<@U…>`; không `<!channel>`; dấu ` trong
  code đổi thành ˋ (agent không thoát khỏi khối mã); cả khối nằm trong trích dẫn `> `.
- `split_md` chia TRƯỚC khi render (mỗi phần render hợp lệ), theo khối; khối mã bị chia được mở lại đúng ngôn ngữ ở phần sau.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode

NBSP = " "
_LANG = re.compile(r"^[A-Za-z0-9_+.#-]{1,24}$")
_DOMAINISH = re.compile(r"://|^\s*(?:www\.)?[\w-]+(?:\.[\w-]+)+(?:/\S*)?\s*$")
_TG_CMD = re.compile(r"(?<![\w/:.~<-])/[A-Za-z][^\s<>]*")  # "/lệnh" không dính chữ trước (cả đường dẫn /usr/bin) → <code>
_CMD_TRAIL = ".,;:!?'\")]”’"
TABLE_PRE_COLS = 3
TABLE_PRE_WIDTH = 34  # bảng hẹp (≤ 3 cột, ≤ ~34 ký tự/dòng) → <pre> căn cột; rộng hơn → mỗi hàng một gạch đầu dòng


@lru_cache(maxsize=1)
def _md() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    md.validateLink = lambda url: True  # mọi link vào cây; bộ render chỉ giữ http(s), còn lại → nhãn (javascript: bị bỏ)
    return md


# ============================================================================ khối trung gian
# Inline: ("text", s) | ("b"|"i"|"s", [inline]) | ("code", s) | ("link", href, [inline]) | ("br",)
# Khối:   {"t": "heading", "level", "inl"} | {"t": "para", "inl"} | {"t": "list", "ordered", "start", "items": [[khối]]}
#         | {"t": "code", "lang", "text"} | {"t": "table", "head": [inl], "rows": [[inl]]} | {"t": "quote", "blocks"} | {"t": "hr"}
def parse(md: str) -> list[dict[str, Any]]:
    return _blocks(SyntaxTreeNode(_md().parse(md or "")).children)


def _blocks(nodes: list[SyntaxTreeNode]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in nodes:
        t = n.type
        if t == "heading":
            out.append({"t": "heading", "level": int(n.tag[1:] or 1), "inl": _inl_of(n)})
        elif t == "paragraph":
            out.append({"t": "para", "inl": _inl_of(n)})
        elif t in ("bullet_list", "ordered_list"):
            start = 1
            if t == "ordered_list":
                try:
                    start = int(n.attrs.get("start", 1))
                except (TypeError, ValueError):
                    start = 1
            out.append({"t": "list", "ordered": t == "ordered_list", "start": start, "items": [_blocks(li.children) for li in n.children]})
        elif t in ("fence", "code_block"):
            info = (n.info or "").strip().split() if t == "fence" else []
            lang = info[0] if info and _LANG.match(info[0]) else ""
            out.append({"t": "code", "lang": lang, "text": n.content.rstrip("\n")})
        elif t == "table":
            head: list[list[tuple]] = []
            rows: list[list[list[tuple]]] = []
            for sec in n.children:
                for tr in sec.children:
                    cells = [_inl_of(c) for c in tr.children]
                    if sec.type == "thead" and not head:
                        head = cells
                    else:
                        rows.append(cells)
            out.append({"t": "table", "head": head, "rows": rows})
        elif t == "blockquote":
            out.append({"t": "quote", "blocks": _blocks(n.children)})
        elif t == "hr":
            out.append({"t": "hr"})
        elif n.content:  # html_block (html=False → không có) / loại lạ → chữ thường
            out.append({"t": "para", "inl": [("text", n.content.rstrip("\n"))]})
    return out


def _inl_of(n: SyntaxTreeNode) -> list[tuple]:
    out: list[tuple] = []
    for c in n.children:
        if c.type == "inline":
            out += _inline(c.children)
    return out


def _inline(nodes: list[SyntaxTreeNode]) -> list[tuple]:
    out: list[tuple] = []
    for c in nodes:
        t = c.type
        if t == "text" or t == "html_inline" or t == "text_special":
            out.append(("text", c.content))
        elif t in ("softbreak", "hardbreak"):
            out.append(("br",))
        elif t == "strong":
            out.append(("b", _inline(c.children)))
        elif t == "em":
            out.append(("i", _inline(c.children)))
        elif t == "s":
            out.append(("s", _inline(c.children)))
        elif t == "code_inline":
            out.append(("code", c.content))
        elif t == "link":
            out.append(("link", str(c.attrs.get("href") or ""), _inline(c.children)))
        elif t == "image":  # ảnh → link chữ (không nhúng ảnh)
            alt = _inline(c.children) or [("text", c.content or "ảnh")]
            out.append(("link", str(c.attrs.get("src") or ""), alt))
        elif c.content:
            out.append(("text", c.content))
    # gộp các đoạn chữ liền nhau (tự nhận URL trần/mention cần cả cụm)
    merged: list[tuple] = []
    for x in out:
        if x[0] == "text" and merged and merged[-1][0] == "text":
            merged[-1] = ("text", merged[-1][1] + x[1])
        else:
            merged.append(x)
    return merged


def plain(inl: list[tuple]) -> str:
    """Chữ thuần của inline (nhãn link, ô bảng, tiêu đề)."""
    out = []
    for x in inl:
        if x[0] in ("text", "code"):
            out.append(x[1])
        elif x[0] == "br":
            out.append(" ")
        elif x[0] == "link":
            out.append(plain(x[2]))
        else:
            out.append(plain(x[1]))
    return "".join(out)


def _http(href: str) -> bool:
    return href.lower().startswith(("http://", "https://"))


def _show_url(href: str) -> str:
    """URL để HIỆN cho người (giải mã %xx cho dễ đọc; ký tự vô hình → ký hiệu thấy được)."""
    from ..notify.telegram import visible

    try:
        s = _md().normalizeLinkText(href)
    except Exception:  # pragma: no cover - URL lạ
        s = href
    return visible(s).replace("\n", " ")


def _label_hides_url(label: str, href: str) -> bool:
    lab = label.strip()
    return not lab or lab in (href, _show_url(href)) or bool(_DOMAINISH.search(lab))


# ============================================================================ bộ render chung
class _Renderer:
    INDENT = NBSP * 3
    QUOTE = "▎ "

    def esc(self, s: str) -> str:  # pragma: no cover - lớp con
        raise NotImplementedError

    def text(self, s: str) -> str:
        return self.esc(s)

    def wrap(self, kind: str, inner: str) -> str:  # pragma: no cover - lớp con
        raise NotImplementedError

    def code_inline(self, s: str) -> str:  # pragma: no cover - lớp con
        raise NotImplementedError

    def link(self, href: str, label: list[tuple]) -> str:  # pragma: no cover - lớp con
        raise NotImplementedError

    def heading(self, inl: list[tuple]) -> str:  # pragma: no cover - lớp con
        raise NotImplementedError

    def code_block(self, lang: str, text: str) -> str:  # pragma: no cover - lớp con
        raise NotImplementedError

    def table_pre(self, lines: list[str]) -> str:  # pragma: no cover - lớp con
        raise NotImplementedError

    def bold_plain(self, s: str) -> str:  # pragma: no cover - lớp con
        raise NotImplementedError

    # ---------------------------------------------------------------- inline
    def inline(self, inl: list[tuple]) -> str:
        out = []
        for x in inl:
            k = x[0]
            if k == "text":
                out.append(self.text(x[1]))
            elif k == "br":
                out.append("\n")
            elif k in ("b", "i", "s"):
                inner = self.inline(x[1])
                out.append(self.wrap(k, inner) if inner.strip() else inner)
            elif k == "code":
                out.append(self.code_inline(x[1]))
            elif k == "link":
                out.append(self.link(x[1], x[2]))
        return "".join(out)

    # ---------------------------------------------------------------- khối
    def blocks(self, blocks: list[dict[str, Any]], depth: int = 0) -> str:
        out: list[str] = []
        prev: str | None = None
        for b in blocks:
            s = self.block(b, depth)
            if s is None:
                continue
            if out:
                out.append("\n" if prev == "heading" else "\n\n")
            out.append(s)
            prev = b["t"]
        return "".join(out)

    def block(self, b: dict[str, Any], depth: int) -> str | None:
        t = b["t"]
        if t == "heading":
            return self.heading(b["inl"])
        if t == "para":
            return self.inline(b["inl"])
        if t == "list":
            return self.list_(b, depth)
        if t == "code":
            return self.code_block(b["lang"], b["text"])
        if t == "table":
            return self.table(b)
        if t == "quote":
            inner = self.blocks(b["blocks"], depth)
            return "\n".join(self.QUOTE + ln if ln.strip() else self.QUOTE.rstrip() for ln in inner.split("\n"))
        return None  # hr: bỏ

    def list_(self, b: dict[str, Any], depth: int) -> str:
        pad = self.INDENT * depth
        lines: list[str] = []
        for i, item in enumerate(b["items"]):
            marker = f"{b['start'] + i}. " if b["ordered"] else "• "
            first = True
            for sub in item:
                if sub["t"] == "list":
                    lines.append(self.list_(sub, depth + 1))
                    continue
                s = self.block(sub, depth + 1)
                if s is None:
                    continue
                if sub["t"] == "code":  # khối mã không thụt lề được — đứng riêng
                    if first:
                        lines.append(pad + marker.rstrip())
                    lines.append(s)
                elif first:
                    lines.append(pad + marker + s.replace("\n", "\n" + pad + self.INDENT))
                else:
                    lines.append(pad + self.INDENT + s.replace("\n", "\n" + pad + self.INDENT))
                first = False
            if first:
                lines.append(pad + marker.rstrip())
        return "\n".join(lines)

    def table(self, b: dict[str, Any]) -> str:
        head = [plain(c).strip() for c in b["head"]]
        rows = [[plain(c).strip() for c in r] for r in b["rows"]]
        ncol = max([len(head)] + [len(r) for r in rows] or [0])
        if not ncol:
            return ""
        grid = [head + [""] * (ncol - len(head))] + [r + [""] * (ncol - len(r)) for r in rows]
        widths = [max(len(r[j]) for r in grid) for j in range(ncol)]
        if ncol <= TABLE_PRE_COLS and sum(widths) + 2 * (ncol - 1) <= TABLE_PRE_WIDTH:
            fmt = lambda r: "  ".join(c.ljust(widths[j]) for j, c in enumerate(r)).rstrip()  # noqa: E731
            lines = [fmt(grid[0]), "  ".join("─" * w for w in widths)] + [fmt(r) for r in grid[1:]]
            return self.table_pre(lines)
        out = []
        for r, cells in zip(b["rows"], rows):
            first = self.bold_plain(cells[0]) if cells and cells[0] else ""
            rest = []
            for j, c in enumerate(r[1:], start=1):
                val = self.inline(c).strip()
                if not val:
                    continue
                name = head[j] if j < len(head) else ""
                rest.append(f"{self.esc(name)}: {val}" if name else val)
            line = "• " + first
            if rest:
                line += (": " if first else "") + " · ".join(rest)
            out.append(line)
        return "\n".join(out)


# ============================================================================ Telegram (HTML)
class _Telegram(_Renderer):
    def esc(self, s: str) -> str:
        return _html.escape(s, quote=False)

    def text(self, s: str) -> str:
        out, pos = [], 0
        for m in _TG_CMD.finditer(s):
            tok = m.group(0)
            cut = len(tok)
            while cut > 1 and tok[cut - 1] in _CMD_TRAIL:
                cut -= 1
            out.append(self.esc(s[pos:m.start()]))
            out.append(f"<code>{self.esc(tok[:cut])}</code>{self.esc(tok[cut:])}")
            pos = m.end()
        out.append(self.esc(s[pos:]))
        return "".join(out)

    def wrap(self, kind: str, inner: str) -> str:
        return f"<{kind}>{inner}</{kind}>"

    def code_inline(self, s: str) -> str:
        return f"<code>{self.esc(s)}</code>"

    def link(self, href: str, label: list[tuple]) -> str:
        lab = plain(label).strip()
        if not _http(href):
            return self.text(lab)
        url = _show_url(href)
        if _label_hides_url(lab, href):
            return self.esc(url)
        return f"{self.text(lab)} ({self.esc(url)})"

    def heading(self, inl: list[tuple]) -> str:
        return f"<b>{self.esc(plain(inl).strip())}</b>"

    def code_block(self, lang: str, text: str) -> str:
        if lang:
            return f'<pre><code class="language-{lang}">{self.esc(text)}</code></pre>'
        return f"<pre>{self.esc(text)}</pre>"

    def table_pre(self, lines: list[str]) -> str:
        return f"<pre>{self.esc(chr(10).join(lines))}</pre>"

    def bold_plain(self, s: str) -> str:
        return f"<b>{self.esc(s)}</b>"


# ============================================================================ Slack (mrkdwn)
_URL_CH = r"(?:(?!&lt;|&gt;)[^\s|<>`*])"
_BARE = re.compile(r"(?<![<|\w])https?://" + _URL_CH + r"+")
_TRAIL = ".,;:!?'\")]"
_USER_MENTION = re.compile(r"&lt;@([UW][A-Z0-9]{8,11})&gt;")


def _bare_url(m: re.Match) -> str:
    url, tail = m.group(0), ""
    while url and url[-1] in _TRAIL:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url, tail = url[:-1], url[-1] + tail
    return f"<{url}>{tail}" if url.split("://", 1)[1] else m.group(0)


def slack_escape(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _code_safe(s: str) -> str:
    return s.replace("`", "ˋ")


class _Slack(_Renderer):
    INDENT = "    "

    def esc(self, s: str) -> str:
        return slack_escape(s)

    def text(self, s: str) -> str:
        e = _BARE.sub(_bare_url, self.esc(s))
        return _USER_MENTION.sub(lambda m: f"<@{m.group(1)}>", e)

    def wrap(self, kind: str, inner: str) -> str:
        mark = {"b": "*", "i": "_", "s": "~"}[kind]
        return f"{mark}{inner}{mark}"

    def code_inline(self, s: str) -> str:
        return f"`{_code_safe(self.esc(s))}`"

    def link(self, href: str, label: list[tuple]) -> str:
        lab = plain(label).strip()
        if not _http(href):
            return self.esc(lab)
        target = self.esc(href)
        if _label_hides_url(lab, href):
            return f"<{target}>"
        return f"<{target}|{self.esc(lab).replace('|', '¦')}>"

    def heading(self, inl: list[tuple]) -> str:
        return f"*{self.esc(plain(inl).strip().strip('*'))}*"

    def code_block(self, lang: str, text: str) -> str:
        return "```\n" + _code_safe(self.esc(text)) + "\n```"

    def table_pre(self, lines: list[str]) -> str:
        return self.code_block("", "\n".join(lines))

    def bold_plain(self, s: str) -> str:
        return f"*{self.esc(s)}*"


_TG = _Telegram()
_SL = _Slack()


def to_telegram_html(md: str) -> str:
    """Markdown → HTML Telegram (parse_mode=HTML). Mọi chữ agent đã escape; chỉ thẻ trong whitelist."""
    return _TG.blocks(parse(md)).strip("\n")


def to_slack_mrkdwn(md: str, *, quote: bool = True) -> str:
    """Markdown → mrkdwn Slack; `quote=True`: cả khối trong trích dẫn `> ` (ranh giới với chữ hub — như `quote_mrkdwn` cũ)."""
    body = _SL.blocks(parse(md)).strip("\n")
    if not quote:
        return body
    lines = ["> " + ln if ln.strip() else ">" for ln in body.split("\n")]
    while lines and lines[-1] == ">":
        lines.pop()
    return "\n".join(lines) or "> "


_TAG = re.compile(r"<[^>]*>")


def telegram_len(md: str) -> int:
    """Độ dài Telegram tính (chữ nhìn thấy sau khi bỏ thẻ, đơn vị UTF-16) của `md` sau khi render."""
    vis = _html.unescape(_TAG.sub("", to_telegram_html(md)))
    return len(vis.encode("utf-16-le")) // 2


def slack_len(md: str) -> int:
    return len(to_slack_mrkdwn(md))


# ============================================================================ chia tin theo khối
_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
_BLOCK_START = re.compile(r"^\s{0,3}(?:#{1,6}\s|[-*+]\s|\d{1,9}[.)]\s|>)")
_TABLE_DELIM = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")


@dataclass
class _Fence:
    char: str
    size: int
    opener: str  # dòng mở (bỏ thụt lề) — dùng lại khi mở lại ở phần sau

    @property
    def closer(self) -> str:
        return self.char * self.size


@dataclass
class _Group:
    lines: list[str]
    before: _Fence | None = None  # nhóm bắt đầu BÊN TRONG khối mã này
    after: _Fence | None = None  # sau nhóm vẫn còn trong khối mã này
    header: list[str] | None = None  # nhóm là phần tiếp của một bảng → lặp lại dòng tiêu đề
    table: bool = False  # nhóm là cả một bảng (2 dòng đầu = tiêu đề + dòng kẻ)
    fresh: bool = False  # nhóm chỉ có dòng mở fence (chờ dòng mã đầu tiên)


def _groups(md: str) -> list[_Group]:
    groups: list[_Group] = []
    cur: list[str] = []
    fence: _Fence | None = None

    def flush() -> None:
        nonlocal cur
        if cur:
            lines = cur
            table = len(lines) >= 3 and "|" in lines[0] and bool(_TABLE_DELIM.match(lines[1]))
            groups.append(_Group(lines, table=table))
            cur = []

    for line in md.split("\n"):
        if fence is not None:
            m = _FENCE.match(line)
            closing = bool(m and m.group(2)[0] == fence.char and len(m.group(2)) >= fence.size and not m.group(3).strip())
            last = groups[-1]
            if closing:
                last.lines.append(line)
                last.after = None
                last.fresh = False
                fence = None
            elif last.fresh:
                last.lines.append(line)
                last.fresh = False
            else:
                groups.append(_Group([line], before=fence, after=fence))
            continue
        m = _FENCE.match(line)
        if m and not (m.group(2)[0] == "`" and "`" in m.group(3)):
            flush()
            fence = _Fence(m.group(2)[0], len(m.group(2)), line.strip())
            groups.append(_Group([line], after=fence, fresh=True))
            continue
        if not line.strip():
            if cur:
                cur.append(line)
                flush()
            elif groups:
                groups[-1].lines.append(line)
            continue
        if cur and _BLOCK_START.match(line) and not (cur[0].lstrip().startswith(">") and line.lstrip().startswith(">")):
            flush()
        cur.append(line)
    flush()
    return groups


def _compose(gs: list[_Group]) -> str:
    lines: list[str] = []
    if gs[0].before is not None:
        lines.append(gs[0].before.opener)
    elif gs[0].header:
        lines += gs[0].header
    for g in gs:
        lines += g.lines
    while lines and not lines[-1].strip():
        lines.pop()
    if gs[-1].after is not None:
        lines.append(gs[-1].after.closer)
    return "\n".join(lines).strip("\n")


def _safe_space(s: str, lo: int, hi: int) -> int:
    """Dấu cách cuối cùng trong (lo, hi] không nằm giữa `code` hay [nhãn](link) — -1 nếu không có."""
    best = -1
    tick = 0
    depth = 0
    for i, ch in enumerate(s[:hi + 1]):
        if ch == "`":
            tick ^= 1
        elif ch in "[(" and not tick:
            depth += 1
        elif ch in "])" and not tick and depth:
            depth -= 1
        elif ch == " " and i > lo and not tick and not depth:
            best = i
    if best < 0:  # cú pháp lạ (ngoặc không đóng) → dấu cách bất kỳ
        best = s.rfind(" ", lo + 1, hi + 1)
    return best


def _split_group(g: _Group, budget: int, measure: Callable[[str], int]) -> list[_Group]:
    """Nhóm quá ngân sách → nhỏ hơn: theo dòng; một dòng → theo dấu cách (không cắt giữa từ), cùng lắm cắt cứng."""
    lines = g.lines
    while len(lines) > 1 and not lines[-1].strip():
        lines = lines[:-1]
    if len(lines) > 1:
        out: list[_Group] = []
        in_fence = g.before
        header = lines[:2] if g.table else None
        for i, ln in enumerate(lines):
            m = _FENCE.match(ln)
            before = in_fence
            if in_fence is None and m and i == 0 and g.after is not None:
                in_fence = g.after  # dòng mở fence
            elif in_fence is not None and m and m.group(2)[0] == in_fence.char and not m.group(3).strip() and i > 0:
                in_fence = None  # dòng đóng
            after = in_fence if i < len(lines) - 1 else g.after
            hdr = header if (header and i >= 2) else None
            out.append(_Group([ln], before=before, after=after, header=hdr))
        # dòng mở fence đứng một mình → ghép với dòng kế (không có phần chỉ gồm khối mã rỗng)
        if len(out) > 2 and out[0].before is None and out[0].after is not None and len(out[0].lines) == 1 and _FENCE.match(out[0].lines[0]):
            out[1] = _Group(out[0].lines + out[1].lines, before=None, after=out[1].after)
            out = out[1:]
        return out
    line = lines[0] if lines else ""

    def fits(k: int) -> bool:
        return measure(_compose([_Group([line[:k]], before=g.before, after=g.after or g.before, header=g.header)])) <= budget

    lo, hi = 1, len(line)
    while lo < hi:  # tiền tố dài nhất vừa ngân sách
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    cut = lo
    sp = _safe_space(line, cut // 2, cut)
    if sp > 0:
        cut = sp
    head, rest = line[:cut], line[cut:]
    if sp > 0:
        rest = rest.lstrip(" ")
    keep = g.before or g.after
    return [_Group([head], before=g.before, after=keep if g.before else None, header=g.header),
            _Group([rest], before=keep if g.before else None, after=g.after, header=None)]


def split_md(md: str, budget: int, measure: Callable[[str], int]) -> list[str]:
    """Chia `md` thành các phần markdown, mỗi phần `measure(phần) <= budget` (đo SAU khi render). Ưu tiên ranh giới khối
    (đoạn, mục danh sách, dòng mã), rồi xuống dòng, rồi dấu cách; không cắt giữa từ trừ khi một từ dài hơn cả ngân sách.
    Khối mã bị chia → phần sau mở lại bằng đúng dòng mở (cùng ngôn ngữ); bảng bị chia → phần sau lặp lại dòng tiêu đề."""
    md = (md or "").strip("\n")
    if not md or measure(md) <= budget:
        return [md]
    queue = _groups(md)
    parts: list[str] = []
    cur: list[_Group] = []
    guard = 0
    while queue:
        guard += 1
        if guard > 20000:  # pragma: no cover - chặn vòng lặp vô hạn nếu đo không đơn điệu
            parts.append(_compose(cur + queue))
            break
        g = queue.pop(0)
        if not any(x.strip() for x in g.lines) and not cur:
            continue
        trial = cur + [g]
        if measure(_compose(trial)) <= budget:
            cur = trial
            continue
        if cur:
            parts.append(_compose(cur))
            cur = []
            queue.insert(0, g)
            continue
        queue[0:0] = _split_group(g, budget, measure)
    if cur:
        parts.append(_compose(cur))
    return [p for p in parts if p.strip()] or [""]
