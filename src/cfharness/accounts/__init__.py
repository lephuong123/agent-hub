"""Danh sách tài khoản Claude (`hub.accounts`): token `claude setup-token` của nhiều tài khoản, NGƯỜI bấm đổi tài khoản đang dùng.

- service.py — `AccountService`: thêm (dán token / đăng nhập qua `claude setup-token`), đổi, đổi tên, xoá, kiểm `claude auth status`.
- rpc.py — RPC `account.*` (ghi ∈ HUMAN_ONLY, tự bọc audit — `MUTATING_ACCOUNTS`).
- cli.py — `cfharness account list|add|login|use|rename|rm|check`.
- login.py — (luồng LOGIN) chạy `claude setup-token` trong PTY.

KHÔNG tự đổi tài khoản (đã chốt 2026-11-13). Hợp đồng: docs/dev-notes/plan-claude-accounts.md.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .service import AccountService

if TYPE_CHECKING:
    from ..hub import Hub

__all__ = ["AccountService", "attach"]


def attach(hub: "Hub") -> None:
    hub.accounts = AccountService(hub)  # type: ignore[attr-defined]
    try:  # lần đầu: CLAUDE_CODE_OAUTH_TOKEN sẵn có → tài khoản "Mặc định"
        hub.accounts.migrate_legacy()  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover — migrate lỗi không được chặn hub khởi động
        import logging

        logging.getLogger("cfharness.accounts").exception("không migrate được CLAUDE_CODE_OAUTH_TOKEN sang danh sách tài khoản")
