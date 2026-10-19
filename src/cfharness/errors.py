"""Lỗi nghiệp vụ của hub → mã JSON-RPC → mã thoát CLI (FR-UI-08).

Mã thoát: 0 ok · 1 lỗi người dùng · 2 lỗi hệ thống · 3 bị chặn (ESTOP/ngân sách/auth).
"""

from __future__ import annotations

from typing import Any

# Mã lỗi JSON-RPC dành cho ứng dụng (khoảng -32000..-32099)
USER_ERROR = -32001
NOT_FOUND = -32004
CONFLICT = -32009
BLOCKED = -32003
SYSTEM_ERROR = -32000

EXIT_BY_CODE = {USER_ERROR: 1, NOT_FOUND: 1, CONFLICT: 1, BLOCKED: 3, SYSTEM_ERROR: 2}


class HubError(Exception):
    code = USER_ERROR

    def __init__(self, message: str, *, data: Any = None, code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data
        if code is not None:
            self.code = code


class NotFound(HubError):
    code = NOT_FOUND


class Conflict(HubError):
    code = CONFLICT


class Blocked(HubError):
    code = BLOCKED


class SystemFailure(HubError):
    code = SYSTEM_ERROR


def exit_code_for(code: int) -> int:
    return EXIT_BY_CODE.get(code, 2)
