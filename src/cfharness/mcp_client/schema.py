"""Migration v34 (store.MIGRATIONS): mọi dòng `mcp_servers` nguồn hub (manual / claude-import) → nguồn `claude` TẠI CHỖ.

Hub thôi tự giữ cách chạy/kết nối MCP bên thứ ba từ v0.2.32 (docs/dev-notes/plan-mcp-claude-native.md). Giữ tên (agent đã gán giữ
nguyên), trust, `tools` (include/exclude/require_approval/annotations ghi đè của người), mô tả, timeout và cột `annotations_cache`;
bỏ command/args/env/headers/auth, url rút về `scheme://host[:port]`. Server phải có trong `.claude.json` của tài khoản đang dùng —
thiếu thì run của agent gán báo lỗi rõ (runs.py, fail-closed) và `hub doctor` cảnh báo. Dòng trỏ về chính hub bị xoá mềm (H-18).
Không xoá secret nào (chúng vẫn được che).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def _converted(name: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    from .config import McpServerConfig

    try:
        return McpServerConfig(name=name, **cfg).stored()
    except Exception:  # dòng hỏng (tên lạ…) → để nguyên, registry bỏ qua khi đọc lỗi
        return None


def _points_to_hub(cfg: dict[str, Any]) -> bool:
    from ..guard.service import is_hub_mcp_config

    try:
        return bool(is_hub_mcp_config(cfg))
    except Exception:  # best-effort: cấu hình lạ → coi như không phải MCP của hub
        return False


def to_claude_source(conn: sqlite3.Connection) -> None:
    from ..events import iso

    rows = conn.execute("SELECT name, config, deleted_at FROM mcp_servers").fetchall()
    for name, raw, deleted_at in rows:
        try:
            cfg = json.loads(raw or "{}")
        except ValueError:
            continue
        if not isinstance(cfg, dict):
            continue
        # dòng trỏ về chính hub (`cfharness mcp serve` / cổng MCP HTTP của hub): sau khi bỏ lệnh/url thì không còn nhận ra được
        # (guard `mcp_hub_self`, khung quyền điều phối viên) → xoá mềm (H-18: không bao giờ là MCP của agent)
        hub_self = not deleted_at and _points_to_hub(cfg)
        new = _converted(name, cfg)
        if new is None:
            continue
        conn.execute("UPDATE mcp_servers SET transport=?, config=?, source='claude'" + (", deleted_at=?" if hub_self else "") + " WHERE name=?",
                     (new.get("transport") or "stdio", json.dumps(new, ensure_ascii=False), *((iso(),) if hub_self else ()), name))
