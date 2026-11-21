"""Cảnh báo của SAC (FR-SA-MON-02): mở / cập nhật / xác nhận / tắt tiếng / hồi phục, chống trùng theo (loại đối tượng, đối tượng, mã).

Mã: bộ `admin_watch.ALERT_CODES` hiện có (theo hub) + mã mới của SAC. Gửi tin (Telegram/Slack/email) ở `notify.py` — đọc các cảnh báo
`notified=0`; payload chỉ id/enum (không chữ tự do).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .db import new_id

if TYPE_CHECKING:
    from .dispatch import State

SAC_CODES = ("domain_dns", "domain_tunnel", "domain_tls", "domain_http", "domain_orphan", "port_conflict", "job_failed", "disk_low",
             "team_overdue", "superadmin_login_new_ip", "tamper", "approval_solo", "missing_report", "policy_mismatch")
LEVELS = ("info", "warn", "crit")
ALERT_VI = {
    "stale": "hub không báo trạng thái", "stopped": "hub đã dừng", "owner_mismatch": "file trạng thái sai chủ", "malformed": "file trạng thái hỏng",
    "missing": "thiếu file trạng thái", "budget_80": "chi phí ≥ 80% ngân sách", "budget_100": "chi phí vượt ngân sách", "tamper": "phát hiện sửa nhật ký",
    "auth_block": "Claude lỗi đăng nhập", "approval_waiting_long": "duyệt chờ lâu", "version_behind": "phiên bản cũ",
    "policy_mismatch": "chính sách chưa áp", "admin_estop_on": "ESTOP quản trị đang bật", "revoked_running": "user đã thu hồi vẫn chạy",
    "domain_dns": "DNS sai", "domain_tunnel": "ingress tunnel lệch", "domain_tls": "chứng chỉ lỗi/sắp hết hạn", "domain_http": "HTTPS không ra trang đăng nhập",
    "domain_orphan": "tên miền mồ côi", "port_conflict": "trùng cổng", "job_failed": "job lỗi", "disk_low": "đĩa sắp đầy",
    "team_overdue": "việc team quá hạn", "superadmin_login_new_ip": "đăng nhập SAC từ IP mới", "approval_solo": "thao tác tới hạn chạy không người duyệt thứ hai",
    "missing_report": "thiếu báo cáo ngày",
}


def raise_alert(state: "State", subject_kind: str, subject: str, code: str, level: str = "warn") -> str:
    now = state.clock()
    level = level if level in LEVELS else "warn"
    cur = state.db.one("SELECT id FROM alerts WHERE subject_kind=? AND subject=? AND code=? AND state!='resolved'", (subject_kind, subject, code))
    if cur:
        state.db.exec("UPDATE alerts SET updated_at=?, level=? WHERE id=?", (now, level, cur["id"]))
        return str(cur["id"])
    aid = new_id("al")
    state.db.exec("INSERT INTO alerts(id, host_id, subject_kind, subject, code, level, state, opened_at, updated_at) VALUES (?,?,?,?,?,?,'on',?,?)",
                  (aid, state.db.host_id, subject_kind, subject, code, level, now, now))
    return aid


def resolve_alert(state: "State", subject_kind: str, subject: str, code: str) -> bool:
    now = state.clock()
    return state.db.exec("UPDATE alerts SET state='resolved', resolved_at=?, updated_at=?, notified=0 WHERE subject_kind=? AND subject=? AND code=?"
                         " AND state!='resolved'", (now, now, subject_kind, subject, code)) > 0


def sync_alerts(state: "State", subject_kind: str, subject: str, codes: set[str], *, managed: set[str], level_of: dict[str, str] | None = None) -> None:
    """Đặt tập cảnh báo đang mở của một đối tượng = `codes` (chỉ trong phạm vi `managed`): mở mã mới, hồi phục mã đã hết."""
    open_codes = {r["code"] for r in state.db.all("SELECT code FROM alerts WHERE subject_kind=? AND subject=? AND state!='resolved'",
                                                  (subject_kind, subject))}
    for c in codes - open_codes:
        raise_alert(state, subject_kind, subject, c, (level_of or {}).get(c, "warn"))
    for c in (open_codes & managed) - codes:
        resolve_alert(state, subject_kind, subject, c)


def list_alerts(state: "State", *, include_resolved: bool = False, limit: int = 200) -> list[dict[str, Any]]:
    q = "SELECT * FROM alerts" + ("" if include_resolved else " WHERE state!='resolved'") + " ORDER BY updated_at DESC LIMIT ?"
    rows = state.db.all(q, (limit,))
    now = state.clock()
    for r in rows:
        r["label"] = ALERT_VI.get(r["code"], r["code"])
        r["muted"] = bool(r["muted_until"] and r["muted_until"] > now)
    return rows
