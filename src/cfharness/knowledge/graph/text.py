"""Chuẩn hoá văn bản của graph memory (thuần, không DB).

- `nfc(s)`: Unicode NFC + gộp khoảng trắng + bỏ ký tự điều khiển — dạng LƯU (tên thực thể, fact, alias). Tiếng Việt gõ từ
  nhiều bộ gõ (tổ hợp/dựng sẵn) về cùng một chuỗi byte.
- `canon(s)`: khoá so khớp thực thể/alias = NFC → gấp dấu (`session_search.fold`: bỏ dấu, `đ`→`d`, chữ thường) → gộp khoảng
  trắng. "Nguyễn Văn A" ≡ "nguyen van a" ≡ "NGUYỄN  VĂN A".
- `folded(s)`: cột tìm kiếm FTS (gấp dấu, GIỮ độ dài như `session_search.fold`) — câu hỏi có dấu hay không dấu đều khớp.
"""

from __future__ import annotations

import re
import unicodedata

from ...session_search import fold

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩﻿]")


def nfc(s: object) -> str:
    t = unicodedata.normalize("NFC", str(s or ""))
    return " ".join(_CTRL.sub("", t).split())


def folded(s: object) -> str:
    return fold(nfc(s))


def canon(s: object) -> str:
    return " ".join(folded(s).split())


def norm_relation(s: object) -> str:
    """Quan hệ: snake_case ASCII (`works_on`, `prefers`) — so khớp mâu thuẫn theo (thực thể, quan hệ) cần khoá ổn định."""
    t = re.sub(r"[^a-z0-9]+", "_", canon(s)).strip("_")
    return t[:64]
