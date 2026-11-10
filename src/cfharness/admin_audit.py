"""Audit METADATA cho người quản trị máy (plan-admin-users P2 §2b; chi tiết `docs/dev-notes/admin-users.md`).

Hub user (khi đang xuất trạng thái — `AdminExporter.enabled`) chép MỖI dòng audit thành một dòng JSONL tối giản vào
`<thư mục cha của admin_export.dir>/audit/<user>/<YYYY-MM-DD>.jsonl` (thư mục `<user>/` do root tạo khi cấp quyền; thiếu → bỏ qua).
Mỗi dòng CHỈ: `ts`, `kind` (danh sách trắng: method RPC đã phân loại + một số loại audit hệ thống), `actor` (enum, KHÔNG id agent),
`channel` (enum, bỏ `:<chat_id>`), `outcome` (enum), và với `config.set` thêm `key` (tên khoá thuộc DEFAULTS, KHÔNG giá trị).
Không summary/data/target/prompt. Trần dòng/byte mỗi ngày; file cũ hơn `KEEP_DAYS` ngày bị xoá (bởi chính hub user).
Bộ đọc (hub admin, RPC `admin.audit`) coi file là DỮ LIỆU KHÔNG TIN CẬY: không theo symlink, chủ file = user, lọc lại `AUDIT_SPEC`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger("cfharness.admin_audit")

KEEP_DAYS = 30
MAX_LINES_PER_DAY = 5000
MAX_BYTES_PER_DAY = 1024 * 1024
ACTORS = ("user", "agent", "manager", "hub", "anonymous", "curator", "learning", "other")
CHANNELS = ("cli", "tui", "web", "mcp", "telegram", "slack", "runtime", "hook", "internal", "schedule", "other")
OUTCOMES = ("ok", "denied", "failed")
KIND_RE = r"^[a-z][a-z0-9_]{0,40}(\.[a-z0-9_]{1,40}){0,3}$"
KEY_RE = r"^[a-z][a-z0-9_]{0,40}(\.[A-Za-z0-9_]{1,40}){0,5}$"
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")
AUDIT_SPEC: dict[str, str] = {"ts": "iso", "kind": "re:" + KIND_RE, "actor": "enum:" + ",".join(ACTORS),
                              "channel": "enum:" + ",".join(CHANNELS), "outcome": "enum:" + ",".join(OUTCOMES), "key": "re:" + KEY_RE}
# loại audit KHÔNG phải method RPC nhưng có ý nghĩa với admin (đăng nhập web, chặn agent, toàn vẹn…)
EXTRA_KINDS = frozenset({"web.login", "web.login_failed", "web.login_locked", "web.totp_confirmed", "web.totp_enroll", "auth.denied",
                         "telegram.ignored", "slack.ignored", "guard.integrity_accept", "guard.tamper_detected", "approval.open",
                         "approval.resolved", "approval.manager_decided", "manager.action", "run.auto_resume", "hub.admin_estop"})
FAIL_KINDS = {"auth.denied": "denied", "web.login_failed": "failed", "web.login_locked": "denied", "telegram.ignored": "denied",
              "slack.ignored": "denied"}


def allowed_kinds() -> frozenset[str]:
    from . import auth

    return frozenset(auth.all_mutating() | auth.HUMAN_ONLY | EXTRA_KINDS)


def _actor(a: str) -> str:
    a = (a or "").split(":", 1)[0]
    return a if a in ACTORS else "other"


def _channel(c: str) -> str:
    c = (c or "").split(":", 1)[0]
    return c if c in CHANNELS else "other"


def _config_key(data: dict[str, Any]) -> str | None:
    from .config import DEFAULTS

    params = data.get("params") if isinstance(data, dict) else None
    key = params.get("key") if isinstance(params, dict) else None
    if not isinstance(key, str) or not re.match(KEY_RE, key):
        return None
    cur: Any = DEFAULTS
    for part in key.split("."):  # chỉ tên khoá THUỘC DEFAULTS (khoá tuỳ ý người gõ có thể mang chữ tự do)
        if not isinstance(cur, dict) or part not in cur:
            return key.split(".", 1)[0] if key.split(".", 1)[0] in DEFAULTS else None
        cur = cur[part]
    return key


def line_for(kind: str, actor: str, channel: str, data: dict[str, Any], *, kinds: frozenset[str] | None = None) -> dict[str, Any] | None:
    """Dòng metadata (đã lọc AUDIT_SPEC) hoặc None nếu loại không thuộc danh sách trắng."""
    from .admin_control import filter_flat, iso_now

    if kind not in (kinds if kinds is not None else allowed_kinds()):
        return None
    rec: dict[str, Any] = {"ts": iso_now(), "kind": kind, "actor": _actor(actor), "channel": _channel(channel), "outcome": FAIL_KINDS.get(kind, "ok")}
    if kind == "config.set":
        k = _config_key(data)
        if k:
            rec["key"] = k
    return filter_flat(rec, AUDIT_SPEC)


def audit_root(status_dir: Path) -> Path:
    return Path(status_dir).expanduser().parent / "audit"


class AdminAuditWriter:
    """Tap của `hub.audit` (AuditLog.taps). Ghi khi bộ xuất trạng thái bật và thư mục `<audit>/<user>/` đã được cấp."""

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._kinds: frozenset[str] | None = None
        self._pruned: str | None = None
        self.written = 0
        self.dropped = 0
        self._day: str | None = None
        self._lines = 0
        a = getattr(hub, "audit", None)
        if a is not None and hasattr(a, "taps"):
            a.taps.append(self.tap)

    def _dir(self) -> Path | None:
        from .admin_overview import USER_RE, os_user

        exp = getattr(self.hub, "admin_export", None)
        if exp is None or not exp.enabled:
            return None
        user = os_user()
        if not USER_RE.match(user):
            return None
        d = audit_root(exp.directory) / user
        return d if d.is_dir() and not d.is_symlink() else None

    def tap(self, kind: str, actor: str, channel: str, data: dict[str, Any]) -> None:
        d = self._dir()
        if d is None:
            return
        if self._kinds is None:
            self._kinds = allowed_kinds()
        rec = line_for(kind, actor, channel, data, kinds=self._kinds)
        if rec is None:
            return
        day = datetime.now().strftime("%Y-%m-%d")
        p = d / f"{day}.jsonl"
        line = (json.dumps(rec, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        try:
            try:
                size = os.lstat(p).st_size
            except FileNotFoundError:
                size = 0
            if self._day != day:  # đếm lại số dòng đã có khi sang ngày / daemon khởi động lại
                self._day, self._lines = day, _count_lines(p)
            if size + len(line) > MAX_BYTES_PER_DAY or self._lines >= MAX_LINES_PER_DAY:
                self.dropped += 1
                return
            fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o640)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
            self.written += 1
            self._lines += 1
        except OSError as e:
            log.warning("admin_audit: không ghi được %s: %s", p, e)
            return
        if self._pruned != day:
            self._pruned = day
            prune(d, keep_days=KEEP_DAYS)


def _count_lines(p: Path) -> int:
    try:
        with open(p, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def prune(d: Path, *, keep_days: int = KEEP_DAYS, today: datetime | None = None) -> list[str]:
    cutoff = ((today or datetime.now()) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    gone: list[str] = []
    try:
        names = os.listdir(d)
    except OSError:
        return gone
    for n in names:
        if DAY_RE.match(n) and n[:10] < cutoff:
            try:
                os.unlink(d / n)
                gone.append(n)
            except OSError:
                pass
    return gone


def attach(hub: "Hub") -> None:
    hub.admin_audit = AdminAuditWriter(hub)  # type: ignore[attr-defined]


def read_audit(status_dir: Path, *, user: str | None = None, days: int = 7, limit: int = 1000) -> dict[str, Any]:
    """Hub admin: đọc metadata audit (mới nhất trước). Không ném vì nội dung file."""
    from .admin_control import USER_RE, filter_flat, read_small
    from .admin_overview import _owner_name

    root = audit_root(status_dir)
    out: dict[str, Any] = {"dir": str(root), "exists": root.is_dir(), "rows": [], "errors": []}
    if not out["exists"]:
        return out
    users = [user] if user else sorted(n for n in os.listdir(root) if USER_RE.match(n))
    since = (datetime.now() - timedelta(days=max(1, min(int(days), KEEP_DAYS)))).strftime("%Y-%m-%d")
    rows: list[dict[str, Any]] = []
    for u in users:
        if not USER_RE.match(u or ""):
            continue
        ud = root / u
        if not ud.is_dir() or ud.is_symlink():
            continue
        try:
            names = sorted((n for n in os.listdir(ud) if DAY_RE.match(n) and n[:10] >= since), reverse=True)
        except OSError:
            out["errors"].append(f"{u}:list_failed")
            continue
        for n in names:
            raw, st, err = read_small(ud / n, MAX_BYTES_PER_DAY + 4096)
            if raw is None or st is None:
                out["errors"].append(f"{u}/{n}:{err}")
                continue
            owner = _owner_name(st.st_uid)
            if owner is not None and owner != u:
                out["errors"].append(f"{u}/{n}:owner_mismatch")
                continue
            for ln in raw.decode("utf-8", "replace").splitlines():
                try:
                    r = filter_flat(json.loads(ln), AUDIT_SPEC)
                except ValueError:
                    continue
                if r.get("ts") and r.get("kind"):
                    rows.append({"user": u, **r})
    rows.sort(key=lambda r: r["ts"], reverse=True)
    out["rows"] = rows[:max(1, min(int(limit), 5000))]
    out["total"] = len(rows)
    return out


__all__ = ["AUDIT_SPEC", "AdminAuditWriter", "attach", "audit_root", "line_for", "prune", "read_audit"]

