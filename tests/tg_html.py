"""Kiểm HTML gửi Telegram (parse_mode=HTML) — dùng chung cho test_mdconv / test_telegram* (P0-10, WT-8 nới có kiểm soát).

`assert_telegram_html(s)`: chỉ thẻ trong whitelist Telegram, cân (đóng đúng thứ tự), không `pre`/`code`/`a` trong `blockquote`,
`blockquote` không lồng và chỉ ở cấp ngoài cùng (thuộc tính duy nhất được phép: `expandable`, không giá trị), không lồng gì trong
`pre`/`code` ngoài `<code class="language-…">` ngay trong `<pre>`.
`tappable_commands(s)`: các `/lệnh` Telegram CÓ THỂ biến thành lệnh bấm được — tìm trên chữ thấy được (sau khi bỏ thẻ), bỏ những cái
bắt đầu trong `pre`/`code` hoặc trong URL. Luật = hợp của tdlib (`match_bot_commands`) và tdesktop (`RegExpBotCommand`): "/" +
[A-Za-z0-9_], ký tự trước không phải chữ/số/`_` (tdesktop nhận cả sau `<`, `>`, `/`).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

ALLOWED = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "blockquote", "tg-spoiler", "span"}
_CMD = re.compile(r"(?<!\w)/[A-Za-z0-9_]{1,64}")
_URL = re.compile(r"(?i)\b(?:https?|ftp)://\S*|\bwww\.\S*")


class _Check(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.tags: list[str] = []
        self.text: list[str] = []  # chữ thấy được
        self.mask: list[bool] = []  # True = ký tự nằm trong pre/code

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        if tag not in ALLOWED:
            self.errors.append(f"thẻ lạ <{tag}>")
        if "blockquote" in self.stack and tag in ("pre", "code", "a", "blockquote"):
            self.errors.append(f"<{tag}> trong blockquote")
        if tag == "blockquote" and self.stack:
            self.errors.append(f"<blockquote> nằm trong {self.stack}")
        if self.stack and self.stack[-1] == "code" or ("pre" in self.stack and not (tag == "code" and self.stack[-1] == "pre")):
            self.errors.append(f"<{tag}> lồng trong pre/code")
        for k, v in attrs:
            if tag == "code" and k == "class" and str(v or "").startswith("language-"):
                continue
            if tag == "a" and k == "href":
                continue
            if tag == "blockquote" and k == "expandable" and v is None:
                continue
            self.errors.append(f"thuộc tính {k}= trên <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> không khớp {self.stack}")
            return
        self.stack.pop()

    def handle_data(self, data: str) -> None:
        inside = "pre" in self.stack or "code" in self.stack
        self.text.append(data)
        self.mask.extend([inside] * len(data))


def _parse(s: str) -> _Check:
    p = _Check()
    p.feed(s)
    p.close()
    return p


def assert_telegram_html(s: str) -> list[str]:
    p = _parse(s)
    assert not p.errors, p.errors
    assert not p.stack, f"thẻ chưa đóng {p.stack}"
    return p.tags


def tappable_commands(s: str) -> list[str]:
    p = _parse(s)
    text = "".join(p.text)
    mask = list(p.mask)
    for u in _URL.finditer(text):
        mask[u.start():u.end()] = [True] * (u.end() - u.start())
    return [m.group(0) for m in _CMD.finditer(text) if not mask[m.start()]]


def agent_part(text: str, frame: str) -> str:
    """Phần SAU dòng tiền tố cố định (chữ agent + đuôi hub)."""
    i = text.index(frame)
    return text[text.index("\n", i) + 1:]
