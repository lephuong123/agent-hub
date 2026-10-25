"""Khung quyền rộng của điều phối viên (hermes: Bash/Write/Web/MCP) khi KHÔNG có sandbox OS (Windows) — FAIL-CLOSED.

Khung hermes được thiết kế để chạy dưới đủ ba lớp: hook guard + approval, sandbox OS (Seatbelt/bubblewrap) và token người
(docs/plan/M7-manager.md §9). Windows chưa có sandbox OS của Claude Code (`guard.runtime.sandbox_supported()` = False) → Bash của
điều phối viên ghi được mọi nơi user ghi được. Mặc định:

- ĐẶT `manager.capabilities` thành khung rộng bị từ chối (`HubConfig.set` → ValueError, không ghi);
- khung rộng có sẵn trong config.yaml (sửa tay, mang từ máy macOS sang…) được GIẢI về strict + cảnh báo (`ManagerPermissions.capabilities`);
- `cfharness doctor` cảnh báo.

Chỉ khi NGƯỜI đặt `manager.allow_hermes_without_sandbox: true` (nhánh `manager` ∈ `integrity.SECURITY_KEYS` → agent sửa file bị
khôi phục; `config.set` ∈ HUMAN_ONLY) thì khung rộng mới có hiệu lực trên máy không sandbox.
"""

from __future__ import annotations

from typing import Any

OPT_IN_KEY = "manager.allow_hermes_without_sandbox"
STRICT_TOOLS = frozenset({"Read", "Grep", "Glob"})


def os_sandbox_available() -> bool:
    from ..guard.runtime import sandbox_supported

    return sandbox_supported()


def is_wide(caps: dict[str, Any]) -> bool:
    """Khung (đã resolve) rộng hơn strict: tool ngoài Read/Grep/Glob hoặc MCP ngoài hub-self."""
    return bool(set(caps.get("tools") or ()) - STRICT_TOOLS) or (caps.get("mcp") or "none") != "none"


def opted_in(manager_cfg: Any) -> bool:
    return isinstance(manager_cfg, dict) and manager_cfg.get("allow_hermes_without_sandbox") is True


def blocked(caps: dict[str, Any], manager_cfg: Any) -> bool:
    return is_wide(caps) and not os_sandbox_available() and not opted_in(manager_cfg)


BLOCK_MSG = ("khung điều phối viên rộng (hermes: Bash/Write/Web/MCP) cần sandbox OS — máy này (Windows) không có, nên mặc định bị từ "
             "chối. Nếu chấp nhận rủi ro, NGƯỜI đặt `cfharness config set manager.allow_hermes_without_sandbox true` trước "
             "(docs/dev-notes/windows.md)")


def gate(caps: dict[str, Any], manager_cfg: Any) -> dict[str, Any]:
    """Khung hiệu lực sau cổng sandbox: rộng + không sandbox + chưa opt-in → strict (giữ cảnh báo cũ + thêm lý do)."""
    if not blocked(caps, manager_cfg):
        return caps
    from .permissions import resolve_capabilities

    strict = resolve_capabilities({"profile": "strict"})
    strict["warnings"] = list(caps.get("warnings") or []) + [f"khung '{caps.get('profile')}' bị hạ về strict: {BLOCK_MSG}"]
    strict["sandbox_blocked"] = True
    return strict


def check_set(key: str, merged: dict[str, Any]) -> None:
    """Gọi từ `HubConfig.set` với config SAU khi ghi (chưa ghi đĩa): đặt khung rộng khi không sandbox + chưa opt-in → ValueError.
    Chỉ áp cho khoá đổi khung (`manager.capabilities*`, `manager`) — tắt opt-in vẫn được (khung rộng khi đó tự hạ về strict)."""
    if not (key == "manager" or key.startswith("manager.capabilities")):
        return
    from .permissions import resolve_capabilities

    mgr = merged.get("manager") or {}
    if blocked(resolve_capabilities(mgr.get("capabilities")), mgr):
        raise ValueError(BLOCK_MSG)


def doctor_note(config_data: dict[str, Any]) -> tuple[bool, str] | None:
    """(ok, chi tiết) cho `cfharness doctor`; None khi máy có sandbox OS hoặc khung là strict."""
    if os_sandbox_available():
        return None
    from .permissions import resolve_capabilities

    mgr = config_data.get("manager") or {}
    caps = resolve_capabilities(mgr.get("capabilities"))
    if not is_wide(caps):
        return None
    if opted_in(mgr):
        return False, (f"manager.capabilities '{caps['profile']}' chạy KHÔNG sandbox OS (đã opt-in manager.allow_hermes_without_sandbox) — "
                       "Bash/Write của điều phối viên chỉ còn hook guard + bộ phân loại chặn")
    return False, (f"manager.capabilities '{caps['profile']}' bị hạ về strict vì không có sandbox OS — "
                   "đặt manager.allow_hermes_without_sandbox=true nếu chấp nhận rủi ro, hoặc về strict cho khớp")
