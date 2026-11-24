"""`sac.db` — CSDL riêng của Super Admin Console (plan-superadmin §2.6). Chỉ admind mở (root 0600).

Migration theo `PRAGMA user_version` như `store.py`: chỉ THÊM vào cuối `MIGRATIONS`. Mọi bảng nghiệp vụ có `host_id` (Q10/B1).
Máy thật là nguồn sự thật; bảng ở đây là bản ghi mong muốn + lịch sử.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

MIGRATIONS: list[str] = [
    # v1 — máy, tài khoản quản trị, nhật ký quản trị (chuỗi băm), cấu hình
    """
    CREATE TABLE hosts (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, is_local INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
    );
    CREATE TABLE admin_accounts (
        id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL, role TEXT NOT NULL,
        scope TEXT NOT NULL DEFAULT '{}', pw_hash TEXT, totp_on INTEGER NOT NULL DEFAULT 0, totp_step INTEGER NOT NULL DEFAULT -1,
        state TEXT NOT NULL DEFAULT 'pending', enroll_hash TEXT, enroll_until REAL, user_id TEXT,
        created_at REAL NOT NULL, created_by TEXT, last_login_at REAL, last_login_ip TEXT
    );
    CREATE TABLE audit (
        seq INTEGER PRIMARY KEY, ts REAL NOT NULL, host_id TEXT, actor_id TEXT, actor_email TEXT, role TEXT, ip TEXT,
        op TEXT NOT NULL, target TEXT, params TEXT, outcome TEXT NOT NULL, error_code TEXT, job_id TEXT, approver_id TEXT,
        prev_hash TEXT NOT NULL, hash TEXT NOT NULL
    );
    CREATE INDEX audit_ts ON audit(ts);
    CREATE INDEX audit_target ON audit(target);
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL, updated_by TEXT);
    """,
    # v2 — user / hub / zone / tên miền / nhóm / mẫu số liệu / cảnh báo (SA1)
    """
    CREATE TABLE groups_ (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL);
    CREATE TABLE hub_users (
        id TEXT PRIMARY KEY, host_id TEXT NOT NULL, os_user TEXT NOT NULL, full_name TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
        group_id TEXT, state TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, created_by TEXT,
        revoked_at REAL, imported INTEGER NOT NULL DEFAULT 0, UNIQUE(host_id, os_user)
    );
    CREATE TABLE hubs (
        id TEXT PRIMARY KEY, host_id TEXT NOT NULL, user_id TEXT NOT NULL, hub_id TEXT, web_port INTEGER, mcp_port INTEGER,
        version TEXT, install_kind TEXT, pinned_tag TEXT, status TEXT NOT NULL DEFAULT 'unknown', snapshot TEXT NOT NULL DEFAULT '{}',
        profile TEXT NOT NULL DEFAULT '{}', last_seen REAL, UNIQUE(host_id, user_id)
    );
    CREATE UNIQUE INDEX hubs_web_port ON hubs(host_id, web_port) WHERE web_port IS NOT NULL;
    CREATE UNIQUE INDEX hubs_mcp_port ON hubs(host_id, mcp_port) WHERE mcp_port IS NOT NULL;
    CREATE TABLE zones (
        id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, cf_zone_id TEXT, token_state TEXT NOT NULL DEFAULT 'unknown', checked_at REAL,
        created_at REAL NOT NULL
    );
    CREATE TABLE domains (
        id TEXT PRIMARY KEY, host_id TEXT NOT NULL, hub_user_id TEXT, zone_id TEXT NOT NULL, sub TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'hub',
        dns_record_id TEXT, ingress_service TEXT, access_app_id TEXT, state TEXT NOT NULL DEFAULT 'unknown', health TEXT NOT NULL DEFAULT '{}',
        checked_at REAL, created_at REAL NOT NULL, redirect_to TEXT, redirect_until REAL, UNIQUE(zone_id, sub)
    );
    CREATE TABLE samples (
        ts REAL NOT NULL, host_id TEXT NOT NULL, user_id TEXT NOT NULL, cost_today REAL, cost_month REAL, running INTEGER, queued INTEGER,
        failed_today INTEGER, approvals INTEGER, budget_pct REAL
    );
    CREATE INDEX samples_user_ts ON samples(user_id, ts);
    CREATE TABLE alerts (
        id TEXT PRIMARY KEY, host_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject TEXT NOT NULL, code TEXT NOT NULL, level TEXT NOT NULL,
        state TEXT NOT NULL, opened_at REAL NOT NULL, updated_at REAL NOT NULL, resolved_at REAL, acked_by TEXT, muted_until REAL,
        note TEXT NOT NULL DEFAULT '', notified INTEGER NOT NULL DEFAULT 0
    );
    CREATE UNIQUE INDEX alerts_open ON alerts(subject_kind, subject, code) WHERE state != 'resolved';
    """,
    # v3 — job, bước, yêu cầu duyệt (SA2)
    """
    CREATE TABLE jobs (
        id TEXT PRIMARY KEY, host_id TEXT NOT NULL, kind TEXT NOT NULL, target TEXT, params TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL,
        created_by TEXT, approved_by TEXT, created_at REAL NOT NULL, started_at REAL, finished_at REAL, error TEXT, resume_from TEXT
    );
    CREATE TABLE job_steps (
        job_id TEXT NOT NULL, idx INTEGER NOT NULL, name TEXT NOT NULL, state TEXT NOT NULL, started_at REAL, finished_at REAL,
        log TEXT NOT NULL DEFAULT '', PRIMARY KEY(job_id, idx)
    );
    CREATE TABLE approvals (
        id TEXT PRIMARY KEY, op TEXT NOT NULL, params TEXT NOT NULL, target TEXT, requested_by TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, decided_by TEXT, decided_at REAL, run_after REAL, result TEXT
    );
    """,
    # v4 — team, việc team, báo cáo, hồ sơ, chính sách, tài khoản Claude (SA3, SA4)
    """
    CREATE TABLE teams (
        id TEXT PRIMARY KEY, host_id TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
        goal TEXT NOT NULL DEFAULT '', lead_user_id TEXT, starts_on TEXT, ends_on TEXT, channel TEXT NOT NULL DEFAULT '{}',
        budget TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL
    );
    CREATE TABLE team_members (
        team_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL, skills TEXT NOT NULL DEFAULT '[]', joined_at REAL NOT NULL,
        PRIMARY KEY(team_id, user_id)
    );
    CREATE TABLE team_tasks (
        id TEXT PRIMARY KEY, team_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL,
        assignee_id TEXT, due TEXT, priority TEXT NOT NULL DEFAULT 'normal', state TEXT NOT NULL, depends_on TEXT NOT NULL DEFAULT '[]',
        result_path TEXT, review_note TEXT NOT NULL DEFAULT '', reject_reason TEXT NOT NULL DEFAULT '', hub_task_id TEXT,
        progress INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL
    );
    CREATE TABLE daily_reports (
        user_id TEXT NOT NULL, day TEXT NOT NULL, level TEXT NOT NULL, data TEXT NOT NULL, anomalies TEXT NOT NULL DEFAULT '[]',
        source_hash TEXT, ingested_at REAL NOT NULL, PRIMARY KEY(user_id, day)
    );
    CREATE TABLE profile_snapshots (user_id TEXT NOT NULL, ts REAL NOT NULL, data TEXT NOT NULL, PRIMARY KEY(user_id, ts));
    CREATE TABLE policy_versions (
        scope TEXT NOT NULL, rev INTEGER NOT NULL, content TEXT NOT NULL, author TEXT, created_at REAL NOT NULL, diff TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(scope, rev)
    );
    CREATE TABLE claude_accounts (
        id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, state TEXT NOT NULL DEFAULT 'unknown', set_at REAL NOT NULL, checked_at REAL,
        quota_note TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE user_claude (user_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, assigned_at REAL NOT NULL);
    """,
]


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def loads(s: Any, default: Any = None) -> Any:
    if not isinstance(s, str) or not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def _split(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


class SacDB:
    """Một kết nối, khoá toàn cục (admind chỉ một tiến trình; handler chạy trong thread)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        if fresh:
            try:
                os.chmod(self.path, 0o600)
            except OSError:  # pragma: no cover
                pass
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.migrate()
        self.host_id = self._ensure_local_host()

    # ------------------------------------------------------------------ migration
    def version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> None:
        with self.lock:
            cur = self.version()
            for i, sql in enumerate(MIGRATIONS, start=1):
                if i <= cur:
                    continue
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in _split(sql):
                        self.conn.execute(stmt)
                    self.conn.execute(f"PRAGMA user_version={i}")
                    self.conn.execute("COMMIT")
                except BaseException:
                    self.conn.execute("ROLLBACK")
                    raise

    def _ensure_local_host(self) -> str:
        row = self.one("SELECT id FROM hosts WHERE is_local=1")
        if row:
            return str(row["id"])
        hid = new_id("h")
        self.exec("INSERT INTO hosts(id, name, is_local, created_at) VALUES (?,?,1,?)",
                  (hid, (socket.gethostname() or "local")[:120], time.time()))
        return hid

    # ------------------------------------------------------------------ truy vấn
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def exec(self, sql: str, params: tuple | dict = ()) -> int:
        with self.lock:
            return int(self.conn.execute(sql, params).rowcount)

    def all(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self.lock:
            r = self.conn.execute(sql, params).fetchone()
            return dict(r) if r else None

    def get_setting(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return loads(r["value"], default) if r else default

    def set_setting(self, key: str, value: Any, by: str | None = None) -> None:
        self.exec("INSERT INTO settings(key, value, updated_at, updated_by) VALUES (?,?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                  (key, dumps(value), time.time(), by))

    def close(self) -> None:
        with self.lock:
            self.conn.close()
