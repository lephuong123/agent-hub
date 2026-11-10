"""Audit METADATA cho admin (admin_audit.py — plan-admin-users P2 §2b): danh sách trắng, không chữ tự do, giữ 30 ngày, bộ đọc lọc lại."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from cfharness import admin_audit as aa
from cfharness import admin_overview as ao

ME = ao.os_user()
SECRET = "sk-ant-TOPSECRET deploy to /Users/an/du-an-B"


def test_line_for_whitelist_and_no_free_text():
    kinds = frozenset({"config.set", "agent.create", "web.login_failed"})
    r = aa.line_for("agent.create", "user", "telegram:123456", {"params": {"name": SECRET}}, kinds=kinds)
    assert r["kind"] == "agent.create" and r["actor"] == "user" and r["channel"] == "telegram" and r["outcome"] == "ok"
    assert SECRET not in json.dumps(r) and "123456" not in json.dumps(r)
    assert aa.line_for("evil.kind", "user", "cli", {}, kinds=kinds) is None
    assert aa.line_for("agent.create", "agent:secret-agent-name", "cli", {}, kinds=kinds)["actor"] == "agent"
    assert aa.line_for("web.login_failed", "anonymous", "web", {}, kinds=kinds)["outcome"] == "failed"
    # config.set: CHỈ tên khoá thuộc DEFAULTS, không giá trị
    r = aa.line_for("config.set", "user", "cli", {"params": {"key": "admin_export.interval_s", "value": SECRET}}, kinds=kinds)
    assert r["key"] == "admin_export.interval_s" and SECRET not in json.dumps(r)
    r = aa.line_for("config.set", "user", "cli", {"params": {"key": "guard.sandbox.extra_deny_read.du_an_B"}}, kinds=kinds)
    assert r["key"] == "guard"  # đuôi khoá ngoài DEFAULTS (có thể mang chữ tự do) bị cắt về nhánh gốc
    assert "key" not in aa.line_for("config.set", "user", "cli", {"params": {"key": "x y z; rm"}}, kinds=kinds)
    assert "admin.estop_set" in aa.allowed_kinds() and "config.set" in aa.allowed_kinds()


def test_writer_tap_prune_and_reader(hub_home, tmp_path):
    from cfharness.hub import Hub

    hub = Hub(hub_home)
    try:
        status = tmp_path / "status"
        status.mkdir()
        hub.config.set("admin_export.dir", str(status))
        hub.audit.record("agent.create", None, None, {"params": {"name": SECRET}}, actor="user", channel="cli")
        assert not (tmp_path / "audit").exists()  # xuất tắt → không ghi
        hub.config.set("admin_export.enabled", True)
        hub.audit.record("agent.create", None, None, {"params": {"name": SECRET}}, actor="user", channel="cli")
        assert hub.admin_audit.written == 0  # chưa cấp thư mục audit/<user>/ → bỏ qua
        ud = tmp_path / "audit" / ME
        ud.mkdir(parents=True)
        old = ud / f"{(datetime.now() - timedelta(days=40)).strftime('%Y-%m-%d')}.jsonl"
        old.write_text("{}\n")
        hub.audit.record("agent.create", None, None, {"params": {"name": SECRET}}, actor="user", channel="telegram:999")
        hub.audit.record("config.set", None, None, {"params": {"key": "budget.hub_usd_per_day", "value": 123}}, actor="user", channel="web")
        hub.audit.record("totally.unknown", None, None, {"x": SECRET})
        assert hub.admin_audit.written == 2 and not old.exists()  # giữ 30 ngày
        today = ud / f"{datetime.now().strftime('%Y-%m-%d')}.jsonl"
        text = today.read_text()
        assert SECRET not in text and "999" not in text and "123" not in text.replace("2026", "")
        assert oct(today.stat().st_mode & 0o777) == "0o640"
        # dòng giả / chữ tự do chèn vào file bị lọc khi đọc
        with open(today, "a") as f:
            f.write(json.dumps({"ts": "2026-11-10T00:00:00Z", "kind": "agent.create", "summary": SECRET, "actor": SECRET}) + "\n")
            f.write("not json\n")
        r = aa.read_audit(status, user=ME)
        assert len(r["rows"]) == 3 and SECRET not in json.dumps(r)
        assert {x["kind"] for x in r["rows"]} == {"agent.create", "config.set"}
        assert any(x.get("key") == "budget.hub_usd_per_day" for x in r["rows"])
        os.symlink(ud, tmp_path / "audit" / "evil")
        assert aa.read_audit(status, user="evil")["rows"] == []
    finally:
        hub.store.close()


def test_secret_set_at_metadata(tmp_path):
    import time

    from cfharness.secrets import SecretStore

    s = SecretStore(tmp_path / "secrets")
    t0 = time.time()
    s.set("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-xyz")
    at = s.set_at("CLAUDE_CODE_OAUTH_TOKEN")
    assert at is not None and abs(at - t0) < 5
    assert "sk-ant-oat-xyz" not in (tmp_path / "secrets" / ".meta.json").read_text()
    assert [x["name"] for x in s.list()] == ["CLAUDE_CODE_OAUTH_TOKEN"]  # file metadata không lộ thành secret
    s.delete("CLAUDE_CODE_OAUTH_TOKEN")
    assert s.set_at("CLAUDE_CODE_OAUTH_TOKEN") is None
