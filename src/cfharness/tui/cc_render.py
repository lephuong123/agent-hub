"""Kiểu vẽ "Claude Code" dùng chung cho `cfharness chat` fullscreen (`tui/chat.py`) và màn xem agent (`tui/agent_view.py`).

THUẦN (không Textual, không I/O): mỗi hàm trả MỘT `rich.text.Text` (Textual chỉ cho kéo chọn chữ trên `Text`/`Content`).
Chữ của agent/điều phối viên là DỮ LIỆU: luôn qua `termsafe.clean/one_line`, không markup, không link.

Quy ước hình (giống Claude Code CLI):
- tin người:      `> chữ` (xám), dòng sau thụt 2
- lời agent:      `● chữ` (chấm trắng; agent con/phụ: chấm xám), dòng sau thụt 2
- tool:           `● Tên(tham số)` — chấm xanh lá = xong, xám nhấp nháy = đang chạy, đỏ = lỗi; kết quả dưới gutter `  ⎿  `,
                  rút gọn N dòng + `… +k dòng (click để mở)`
- suy nghĩ:       `✻ Đang nghĩ…` nghiêng mờ
- spinner:        `✻ Đang nghĩ… (12s · ↓ 1.2k token · esc để dừng)` màu cam ACCENT, ký tự quay SPINNER_FRAMES
- ghi chú:        `  ⎿  chữ` mờ; cảnh báo vàng `⚠`, lỗi đỏ `✗`
Chữ hiển thị cho người: tiếng Việt (quy ước repo); tên tool/phím giữ nguyên.
"""

from __future__ import annotations

import json
import time
from typing import Any

from rich.text import Text

from ..termsafe import clean, one_line

ACCENT = "#D97757"  # cam Claude
PERMISSION = "#B1B9F9"  # tím nhạt (hộp duyệt)
DIM = "grey50"
SUBTLE = "grey70"
OK = "#4EBA65"
ERR = "#FF6B80"
WARN = "#FFC107"
USER = "grey70"
SPINNER_FRAMES = "·✢✳✶✻✽✻✶✳✢"
GUTTER = "  ⎿  "
INDENT = "     "  # thẳng hàng dưới gutter
PREVIEW_LINES = 3


def _lines(text: Any) -> list[str]:
    s = clean(text).replace("\t", "    ").rstrip("\n")
    return s.split("\n") if s.strip() else []


def fmt_tokens(n: float | int | None) -> str:
    n = float(n or 0)
    if n < 1000:
        return str(int(n))
    if n < 1_000_000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "m"


def fmt_elapsed(s: float | None) -> str:
    s = max(0, int(s or 0))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m {s % 60}s"
    return f"{m // 60}h {m % 60}m"


# ---------------------------------------------------------------------------- tin
def user_text(text: Any) -> Text:
    t = Text()
    for i, ln in enumerate(_lines(text) or [""]):
        t.append("\n  " if i else "> ", style=USER)
        t.append(ln, style=USER)
    return t


def assistant_text(text: Any, *, nested: bool = False, live: bool = False, bullet: bool = True) -> Text:
    """Lời agent: `● ` + chữ; `live` = đang viết (con trỏ ▌)."""
    t = Text()
    t.append("● " if bullet else "  ", style=DIM if nested else "bold white")
    lines = _lines(text) or [""]
    for i, ln in enumerate(lines):
        if i:
            t.append("\n  ")
        t.append(ln)
    if live:
        t.append("▌", style=ACCENT)
    return t


def thinking_text(text: Any = "", *, expanded: bool = False) -> Text:
    t = Text("✻ Đang nghĩ…", style=f"italic {DIM}")
    lines = _lines(text)
    shown = lines if expanded else lines[:PREVIEW_LINES]
    for ln in shown:
        t.append(f"\n  {ln}", style=f"italic {DIM}")
    if len(lines) > len(shown):
        t.append(f"\n  … +{len(lines) - len(shown)} dòng (click để mở)", style=DIM)
    return t


def note_text(text: Any, tone: str | None = None) -> Text:
    """Ghi chú hệ thống dưới gutter: thường (mờ) · warn (vàng ⚠) · err (đỏ ✗)."""
    if tone == "err":
        return Text(f"✗ {clean(text)}", style=ERR)
    if tone == "warn":
        return Text(f"⚠ {clean(text)}", style=WARN)
    t = Text(GUTTER, style=DIM)
    for i, ln in enumerate(_lines(text) or [""]):
        if i:
            t.append("\n" + INDENT)
        t.append(ln, style=DIM)
    return t


# ---------------------------------------------------------------------------- tool
def tool_text(name: str, args: Any = "", *, status: str = "done", result: Any = None, expanded: bool = False,
              duration_s: float | None = None, started: float | None = None, now: float | None = None,
              tool_input: Any = None, nested: bool = False, blink: bool = True) -> Text:
    """`● Tên(args)` + kết quả dưới `⎿`. status ∈ running | done | error | denied. `blink` = pha nhấp nháy của chấm khi đang chạy."""
    now = time.monotonic() if now is None else now
    t = Text()
    color = {"done": OK, "error": ERR, "denied": ERR}.get(status, DIM)
    t.append("● " if (status != "running" or blink) else "  ", style=color)
    t.append(one_line(name or "?", 80), style="bold")
    a = one_line(args or "", 400 if expanded else 120)
    if a:
        t.append(f"({a})")
    if status == "running" and started is not None:
        t.append(f"  {fmt_elapsed(now - started)}", style=DIM)
    elif duration_s is not None and status != "running":
        t.append(f"  {float(duration_s):.1f}s", style=DIM)
    if status == "running":
        t.append(f"\n{GUTTER}", style=DIM)
        t.append("Đang chạy…", style=DIM)
        return t
    if status == "denied":
        t.append(f"\n{GUTTER}", style=DIM)
        t.append("Người dùng từ chối", style=ERR)
        return t
    res = _lines(result)
    shown = res if expanded else res[:PREVIEW_LINES]
    if not shown:
        t.append(f"\n{GUTTER}", style=DIM)
        t.append("(không có nội dung)", style=DIM)
    for i, ln in enumerate(shown):
        t.append(f"\n{GUTTER if i == 0 else INDENT}", style=DIM)
        t.append(ln, style=ERR if status == "error" else "")
    if len(res) > len(shown):
        t.append(f"\n{INDENT}… +{len(res) - len(shown)} dòng (click để mở)", style=DIM)
    if expanded and tool_input:
        try:
            body = json.dumps(tool_input, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            body = str(tool_input)
        t.append(f"\n{INDENT}input:", style=DIM)
        for ln in _lines(body):
            t.append(f"\n{INDENT}  {ln}", style=DIM)
    if nested:
        out = Text()
        for i, ln in enumerate(t.split("\n", allow_blank=True)):
            if i:
                out.append("\n")
            out.append("    ")
            out.append_text(ln)
        return out
    return t


# ---------------------------------------------------------------------------- spinner / trạng thái
def spinner_text(activity: str = "Đang nghĩ", *, elapsed_s: float = 0, tokens: int | float | None = None, frame: int = 0,
                 hint: str | None = None, interrupt: str = "esc để dừng") -> Text:
    t = Text()
    t.append(f"{SPINNER_FRAMES[frame % len(SPINNER_FRAMES)]} {one_line(activity, 60).rstrip('…')}… ", style=ACCENT)
    parts = [fmt_elapsed(elapsed_s)]
    if tokens:
        parts.append(f"↓ {fmt_tokens(tokens)} token")
    if hint:
        parts.append(hint)
    parts.append(interrupt)
    t.append(f"({' · '.join(parts)})", style=DIM)
    return t


def stats_text(*, tool_uses: int | None = None, tokens: int | float | None = None, elapsed_s: float | None = None,
               cost_usd: float | None = None) -> str:
    """`12 tool · 3.4k token · 45s · $0.012` (bỏ phần không có)."""
    parts = []
    if tool_uses:
        parts.append(f"{tool_uses} tool")
    if tokens:
        parts.append(f"{fmt_tokens(tokens)} token")
    if elapsed_s is not None:
        parts.append(fmt_elapsed(elapsed_s))
    if cost_usd:
        parts.append(f"${cost_usd:.3f}")
    return " · ".join(parts)


__all__ = ["ACCENT", "PERMISSION", "DIM", "SUBTLE", "OK", "ERR", "WARN", "USER", "SPINNER_FRAMES", "GUTTER", "INDENT",
           "user_text", "assistant_text", "thinking_text", "note_text", "tool_text", "spinner_text", "stats_text",
           "fmt_tokens", "fmt_elapsed"]
