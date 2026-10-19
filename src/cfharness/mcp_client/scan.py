"""Quét mô tả tool MCP tìm mẫu prompt injection (FR-MC-03, R-04).

Mẫu tiếng Anh mượn Hermes `tools/mcp_tool_schema.py` (`_MCP_INJECTION_PATTERNS`), thêm mẫu tiếng Việt
và ký tự ẩn (Unicode TAG, zero-width, điều khiển hướng chữ). Tool đáng ngờ: cảnh báo + mặc định
bị chặn (`agent_mcp_tool_rules` đưa vào disallowed) cho tới khi người thêm vào include rõ ràng.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.IGNORECASE), reason)
    for p, reason in (
        # Hermes
        (r"ignore\s+(all\s+)?previous\s+instructions", "ghi đè chỉ dẫn ('ignore previous instructions')"),
        (r"you\s+are\s+now\s+a", "đổi danh tính ('you are now a…')"),
        (r"your\s+new\s+(task|role|instructions?)\s+(is|are)", "đổi nhiệm vụ"),
        (r"(^|\n|\s)system\s*:\s*", "chèn system prompt"),
        (r"<\s*/?\s*(system|human|assistant)\s*>", "chèn thẻ vai trò"),
        (r"do\s+not\s+(tell|inform|mention|reveal)", "chỉ thị che giấu"),
        (r"(curl|wget|fetch)\s+https?://", "lệnh mạng trong mô tả"),
        (r"base64\.(b64decode|decodebytes)", "giải mã base64"),
        (r"\bexec\s*\(|\beval\s*\(", "thực thi mã"),
        (r"import\s+(subprocess|os|shutil|socket)", "import nguy hiểm"),
        # tiếng Việt
        (r"bỏ\s+qua\s+(mọi\s+|tất\s+cả\s+|các\s+)?(chỉ\s+dẫn|hướng\s+dẫn|lệnh)\s+(trước|ở\s+trên)", "ghi đè chỉ dẫn (tiếng Việt)"),
        (r"(đừng|không\s+được)\s+(nói|báo|tiết\s+lộ)\s+(cho\s+)?(người\s+dùng|user)", "chỉ thị che giấu (tiếng Việt)"),
        (r"nhiệm\s+vụ\s+mới\s+của\s+bạn\s+là", "đổi nhiệm vụ (tiếng Việt)"),
    )
]
# U+E0000..E007F (TAG), zero-width, bidi override/isolate
_HIDDEN = re.compile("[\U000e0000-\U000e007f​-‏‪-‮⁠-⁤⁦-⁩﻿]")


def scan_description(text: str | None) -> list[str]:
    """Danh sách lý do nghi ngờ (rỗng = sạch)."""
    if not text:
        return []
    out = [reason for pat, reason in _PATTERNS if pat.search(text)]
    if _HIDDEN.search(text):
        out.append("ký tự Unicode ẩn")
    return out
