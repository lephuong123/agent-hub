"""Audit append-only (FR-OB-08, FR-SG-08) + hàm hợp đồng audit.record cho luồng B."""

import sqlite3

import pytest

from cfharness import audit
from cfharness.hub import Hub


def test_record_query_and_append_only(hub_home):
    hub = Hub(hub_home)
    try:
        hub.secrets.set("MY_TOKEN", "tok-value-123456")
        aid = audit.record("guard.decision", "a1", "r_1", {"tool": "Bash", "token": "x", "note": "dùng tok-value-123456", "channel": "mcp:claude-code", "actor": "user"})
        assert aid
        rows = hub.audit.query(kind="guard.*")
        assert len(rows) == 1
        r = rows[0]
        assert r["channel"] == "mcp:claude-code" and r["actor"] == "user" and r["agent_id"] == "a1" and r["run_id"] == "r_1"
        assert r["data"]["token"] == "***"  # khoá nhạy cảm bị che
        assert "tok-value-123456" not in str(r)  # giá trị trong secret store bị che
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            hub.store.conn.execute("UPDATE audit_log SET kind='x'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            hub.store.conn.execute("DELETE FROM audit_log")
    finally:
        hub.store.close()


def test_record_without_hub_is_noop(monkeypatch):
    monkeypatch.setattr(audit, "_installed", None)
    assert audit.record("x", None, None, {}) is None
