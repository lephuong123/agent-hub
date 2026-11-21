"""Vai trò và ma trận phân quyền (BRD mục 8). DÙNG CHUNG cho admind (kiểm thật) và SAC (dựng menu — VIS-08).

`MATRIX[group][role]` = "write" | "read"; không có khoá = ẩn hoàn toàn. Mã thao tác khai `group` (ops.Op). Thao tác đọc cần "read"
hoặc "write"; thao tác ghi cần "write". Operator/Team Lead còn bị giới hạn theo phạm vi (`scope`: nhóm/zone/team) — kiểm ở `in_scope`.
"""

from __future__ import annotations

from typing import Any

SUPER = "super_admin"
OPERATOR = "operator"
AUDITOR = "auditor"
TEAM_LEAD = "team_lead"
ROLES = (SUPER, OPERATOR, AUDITOR, TEAM_LEAD)
ROLE_VI = {SUPER: "Super Admin", OPERATOR: "Operator", AUDITOR: "Auditor", TEAM_LEAD: "Team Lead"}

W, R = "write", "read"

MATRIX: dict[str, dict[str, str]] = {
    "self": {SUPER: W, OPERATOR: W, AUDITOR: W, TEAM_LEAD: W},          # phiên, đổi mật khẩu của chính mình
    "admin_accounts": {SUPER: W},                                        # AUTH-08
    "domain": {SUPER: W, OPERATOR: R, AUDITOR: R},                       # DOM
    "user_lifecycle": {SUPER: W},                                        # tạo / thu hồi / khôi phục / nhập / sửa
    "user_suspend": {SUPER: W, OPERATOR: W},                             # tạm khoá / mở khoá
    "user_delete": {SUPER: W},                                           # xoá hẳn (four-eyes)
    "user_view": {SUPER: R, OPERATOR: R, AUDITOR: R, TEAM_LEAD: R},      # danh sách + hồ sơ mức Số liệu/Hồ sơ (Team Lead: thành viên)
    "user_activity": {SUPER: R, AUDITOR: R, TEAM_LEAD: R},               # mức Hoạt động (Q5: Team Lead thấy thành viên)
    "report": {SUPER: R, OPERATOR: R, AUDITOR: R, TEAM_LEAD: R},         # RPT
    "team_admin": {SUPER: W},                                            # tạo team, thành viên, ngân sách
    "team_board": {SUPER: W, AUDITOR: R, TEAM_LEAD: W},                  # bảng việc, giao, duyệt
    "hub": {SUPER: W, OPERATOR: W},                                      # HUB
    "access": {SUPER: W, OPERATOR: W},                                   # ACC
    "policy": {SUPER: W, OPERATOR: R, AUDITOR: R},                       # POL
    "estop_on": {SUPER: W, OPERATOR: W, TEAM_LEAD: W},
    "estop_off": {SUPER: W},
    "estop_view": {SUPER: R, OPERATOR: R, AUDITOR: R, TEAM_LEAD: R},
    "claude": {SUPER: W, OPERATOR: R},                                   # CLA-01..03
    "cost": {SUPER: R, OPERATOR: R, AUDITOR: R, TEAM_LEAD: R},           # CLA-04
    "monitor": {SUPER: W, OPERATOR: W, AUDITOR: R},                      # MON
    "audit": {SUPER: R, OPERATOR: R, AUDITOR: R},                        # AUD (Operator: chỉ dòng của mình — lọc ở handler)
    "content": {SUPER: W},                                               # CNT (chưa làm — Q1)
    "system": {SUPER: W},                                                # SYS
    "job": {SUPER: W, OPERATOR: W, AUDITOR: R},                          # xem/thử lại/huỷ job
    "approval": {SUPER: W},                                              # duyệt four-eyes
}


def allowed(role: str, group: str, need: str) -> bool:
    got = MATRIX.get(group, {}).get(role)
    if got is None:
        return False
    return need == R or got == W


def visible_groups(role: str) -> set[str]:
    return {g for g, m in MATRIX.items() if role in m}


def scope_of(account: dict[str, Any]) -> dict[str, list[str]]:
    sc = account.get("scope") or {}
    if not isinstance(sc, dict):
        return {}
    return {k: [str(x) for x in v] for k, v in sc.items() if k in ("groups", "zones", "teams") and isinstance(v, list)}


def unrestricted(account: dict[str, Any]) -> bool:
    """Super Admin và Auditor không bị giới hạn phạm vi; Operator không khai phạm vi = toàn máy; Team Lead LUÔN bị giới hạn theo team."""
    role = account.get("role")
    if role in (SUPER, AUDITOR):
        return True
    if role == TEAM_LEAD:
        return False
    return not any(scope_of(account).values())


def in_scope(account: dict[str, Any], subject: dict[str, Any] | None) -> bool:
    """`subject` = thông tin phạm vi của user bị tác động: {"group_id", "zones": [...], "teams": [...]}. None = đối tượng toàn máy."""
    if unrestricted(account):
        return True
    if subject is None:  # thao tác toàn máy: tài khoản bị giới hạn phạm vi không được làm
        return False
    sc = scope_of(account)
    if subject.get("group_id") and subject["group_id"] in sc.get("groups", []):
        return True
    if set(subject.get("zones") or []) & set(sc.get("zones", [])):
        return True
    return bool(set(subject.get("teams") or []) & set(sc.get("teams", [])))
