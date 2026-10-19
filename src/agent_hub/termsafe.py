"""Làm sạch chữ do agent sinh trước khi in ra terminal (REPL chat, TUI) — chữ của agent là DỮ LIỆU.

- Bỏ chuỗi điều khiển ANSI/VT (CSI `ESC[…`, OSC `ESC]…BEL|ST` — gồm link OSC 8 và đổi tiêu đề cửa sổ, DCS/SOS/PM/APC, ESC đơn),
  dạng 8-bit (C1 `\\x80-\\x9f`, vd `\\x9b` = CSI).
- Bỏ ký tự điều khiển C0 trừ `\\n` `\\t`; `\\r\\n` → `\\n`, `\\r` lẻ bị bỏ (không ghi đè được dòng đã in).
- Bỏ ký tự bidi (U+202A–202E, U+2066–2069, U+200E/200F, U+061C) và zero-width (U+200B–200D, U+2060, U+FEFF) — không đảo/giấu được chữ.
Kết quả luôn in qua `rich.text.Text` (không markup) — `[link=…]`/`[bold]` hiện nguyên văn.
"""

from __future__ import annotations

import re
from typing import Any

_ESC_SEQ = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC (kể cả không có ký tự kết thúc)
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"  # DCS/SOS/PM/APC
    r"|\x1b[ -/]*[0-~]?"  # ESC đơn / ESC + trung gian
    r"|\x9b[0-?]*[ -/]*[@-~]"  # CSI 8-bit
    r"|\x9d[^\x07\x9c]*(?:\x07|\x9c)?"  # OSC 8-bit
)
_INVISIBLE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f؜​-‏‪-‮⁠-⁩﻿]")


def clean(text: Any, limit: int | None = None) -> str:
    """Chữ an toàn để in ra terminal (xem docstring module). `limit` cắt theo số ký tự (thêm `…`)."""
    s = str(text if text is not None else "")
    s = s.replace("\r\n", "\n")
    s = _ESC_SEQ.sub("", s)
    s = _INVISIBLE.sub("", s)
    if limit is not None and len(s) > limit:
        s = s[: max(0, limit - 1)] + "…"
    return s


def one_line(text: Any, limit: int = 120) -> str:
    """Một dòng đã làm sạch (xuống dòng/tab → khoảng trắng)."""
    s = " ".join(clean(text).split())
    return s if len(s) <= limit else s[: max(0, limit - 1)] + "…"


__all__ = ["clean", "one_line"]
