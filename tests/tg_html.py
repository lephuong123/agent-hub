"""Kiểm HTML gửi Telegram (parse_mode=HTML) — dùng chung cho test_mdconv / test_telegram* (P0-10, WT-8 nới có kiểm soát).

`assert_telegram_html(s)`: chỉ thẻ trong whitelist Telegram, cân (đóng đúng thứ tự), không `pre`/`code`/`a` trong `blockquote`,
không lồng gì trong `pre`/`code` ngoài `<code class="language-…">` ngay trong `<pre>`.
"""

from __future__ import annotations

from html.parser import HTMLParser

ALLOWED = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "blockquote", "tg-spoiler", "span"}


class _Check(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append(tag)
        if tag not in ALLOWED:
            self.errors.append(f"thẻ lạ <{tag}>")
        if "blockquote" in self.stack and tag in ("pre", "code", "a", "blockquote"):
            self.errors.append(f"<{tag}> trong blockquote")
        if self.stack and self.stack[-1] == "code" or ("pre" in self.stack and not (tag == "code" and self.stack[-1] == "pre")):
            self.errors.append(f"<{tag}> lồng trong pre/code")
        for k, v in attrs:
            if tag == "code" and k == "class" and str(v or "").startswith("language-"):
                continue
            if tag == "a" and k == "href":
                continue
            self.errors.append(f"thuộc tính {k}= trên <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"</{tag}> không khớp {self.stack}")
            return
        self.stack.pop()


def assert_telegram_html(s: str) -> list[str]:
    p = _Check()
    p.feed(s)
    p.close()
    assert not p.errors, p.errors
    assert not p.stack, f"thẻ chưa đóng {p.stack}"
    return p.tags


def agent_part(text: str, frame: str) -> str:
    """Phần SAU dòng tiền tố cố định (chữ agent + đuôi hub)."""
    i = text.index(frame)
    return text[text.index("\n", i) + 1:]
