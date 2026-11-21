"""Vòng đời user (SA2-3..SA2-6): job tạo user 11 bước bằng runner giả, khoá/mở/thu hồi/khôi phục/xoá, hub, truy cập."""

from __future__ import annotations

import json

import pytest

from cfharness import admin_control as AC
from cfharness.admind.runner import Result

from admind_fakes import hub_status, install_fakes
from admind_helpers import make_env

TOKEN = "sk-ant-oat01-" + "x" * 90


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    for d in ("control", "admin-log", "status", "audit", "report"):
        (tmp_path / "shared" / d).mkdir(parents=True)
    e.fakes = install_fakes(e, tmp_path)
    m, cf, r = e.fakes
    e.state.extra["launchd_dir"] = str(tmp_path / "LaunchDaemons")
    (tmp_path / "LaunchDaemons").mkdir()
    e.state.extra["fake_fs"] = True
    e.state.extra["users_root"] = str(tmp_path / "Users")

    def addu(argv):
        m.existing.add(argv[2])
        return Result(0, "", "")

    r.on(lambda a: a[:2] == ["sysadminctl", "-addUser"], addu)
    r.on("--version", Result(0, "cfharness 0.2.28\n", ""))
    r.on("Reply with just: OK", Result(0, "OK\n", ""))
    r.on("manager get --json", Result(0, '{"agent_id": "dieu-phoi"}', ""))
    r.on("web totp enroll", Result(0, "Link đăng ký MỘT LẦN:\n  https://an.example.com/enroll?t=abcDEF123_-\n", ""))
    e.make_admin("boss")
    e.ok("zone.add", {"name": "example.com"}, as_="boss")
    yield e
    e.state.db.close()


def _create(env, **kw):
    zid = env.state.db.one("SELECT id FROM zones WHERE name='example.com'")["id"]
    params = {"os_user": "an", "full_name": "Nguyễn An", "email": "an@ex.com", "zone_id": zid, "sub": "an", "claude_token": TOKEN, **kw}
    return env.ok("user.create", params, as_="boss")


def test_create_user_full_job(env):
    m, cf, r = env.fakes
    out = _create(env)
    j = env.state.jobs.get(out["job_id"])
    assert j["state"] == "succeeded", json.dumps(j, ensure_ascii=False)[:2000]
    assert [s["name"] for s in j["steps"]][:3] == ["os_user", "install", "claude_token"]
    u = env.state.db.one("SELECT * FROM hub_users WHERE os_user='an'")
    assert u["state"] == "active"
    # token: chỉ qua stdin, không trong argv, không trong DB/log
    assert not any(TOKEN in " ".join(a) for a, _ in r.calls)
    assert any(stdin == TOKEN for a, stdin in r.calls if "secret" in a)
    dump = json.dumps(env.state.db.all("SELECT * FROM jobs")) + json.dumps(env.state.db.all("SELECT * FROM job_steps")) + \
        json.dumps(env.state.db.all("SELECT * FROM audit"))
    assert TOKEN not in dump and "abcDEF123" not in dump
    # DNS đúng zone + ingress + LaunchDaemon + cấu hình
    assert ("cname", ("zid1", "an.example.com", "tun-123.cfargotunnel.com")) in cf.calls
    assert r.ran("config set web.public_origin https://an.example.com") and r.ran("config set mcp_server.http_port 8712")
    from pathlib import Path

    ld = Path(env.state.extra["launchd_dir"])
    assert (ld / "com.cfharness.daemon.an.plist").exists() and (ld / "com.cfharness.web.an.plist").exists()
    assert r.ran("dseditgroup -o edit -a an -t user cfharness-users")
    # link TOTP: xem một lần
    link = env.ok("user.totp_link", {"job_id": out["job_id"]}, as_="boss")["totp_url"]
    assert link.endswith("/enroll?t=abcDEF123_-")
    assert env.err("user.totp_link", {"job_id": out["job_id"]}, as_="boss") == "not_found"


def test_create_validation(env):
    env.fakes[0].existing.add("lamlinh")
    zid = env.state.db.one("SELECT id FROM zones WHERE name='example.com'")["id"]
    base = {"full_name": "X", "claude_token": TOKEN}
    assert env.err("user.create", {**base, "os_user": "root"}, as_="boss") == "invalid"
    assert env.err("user.create", {**base, "os_user": "lamlinh"}, as_="boss") == "conflict"  # có trên máy → dùng Nhập
    assert env.err("user.create", {**base, "os_user": "Hoa"}, as_="boss") == "invalid"  # chữ hoa
    assert env.err("user.create", {"os_user": "x1", "full_name": "X", "claude_token": "abc" * 10}, as_="boss") == "invalid"
    assert env.err("user.create", {**base, "os_user": "x2", "zone_id": zid}, as_="boss") == "invalid"  # thiếu sub
    assert env.err("user.create", {**base, "os_user": "x3", "zone_id": zid, "sub": "www"}, as_="boss") == "invalid"


def test_create_failure_retry_with_token(env):
    m, cf, r = env.fakes
    r.on("Reply with just: OK", Result(1, "", "401 invalid token"))
    out = _create(env)
    j = env.state.jobs.get(out["job_id"])
    assert j["state"] == "failed" and j["resume_from"] == "claude_token"
    assert env.state.db.one("SELECT state FROM hub_users WHERE os_user='an'")["state"] == "create_failed"
    r.rules.pop(0)  # bỏ quy tắc lỗi → claude trả OK
    env.state.jobs._secrets.clear()  # giả lập admind khởi động lại: mất token trong RAM
    assert env.ok("job.get", {"id": out["job_id"]}, as_="boss")["needs_secrets"] is True
    env.ok("job.retry", {"id": out["job_id"], "secrets": {"claude_token": TOKEN}}, as_="boss")
    j = env.state.jobs.get(out["job_id"])
    assert j["state"] == "succeeded"
    assert j["steps"][0]["state"] == "done" and j["steps"][1]["state"] in ("done", "skipped")


def test_create_cancel_cleanup(env):
    m, cf, r = env.fakes
    r.on("Reply with just: OK", Result(1, "", "bad"))
    out = _create(env)
    env.ok("job.cancel", {"id": out["job_id"], "cleanup": True}, as_="boss")
    assert r.ran("sysadminctl -deleteUser an")
    assert env.state.db.one("SELECT state FROM hub_users WHERE os_user='an'")["state"] == "deleted"


def _active_user(env):
    _create(env)
    m = env.fakes[0]
    m.hubs = [hub_status("an")]


def test_suspend_resume(env):
    _active_user(env)
    r = env.fakes[2]
    j = env.state.jobs.get(env.ok("user.suspend", {"user": "an", "reason_code": "security"}, as_="boss")["job_id"])
    assert j["state"] == "succeeded"
    assert (AC.control_dir() / "estop.an").exists()
    assert r.ran("launchctl bootout system/com.cfharness.web.an") and r.ran("web devices revoke-all")
    assert env.state.db.one("SELECT state FROM hub_users WHERE os_user='an'")["state"] == "suspended"
    assert env.err("user.suspend", {"user": "an"}, as_="boss") == "conflict"
    env.make_admin("opr", role="operator")
    env.ok("user.resume", {"user": "an"}, as_="opr")  # Operator được mở khoá
    assert not (AC.control_dir() / "estop.an").exists()
    assert env.state.db.one("SELECT state FROM hub_users WHERE os_user='an'")["state"] == "active"
    assert env.err("user.revoke", {"user": "an"}, as_="opr") == "forbidden"


def test_revoke_restore_delete(env):
    _active_user(env)
    m, cf, r = env.fakes
    j = env.state.jobs.get(env.ok("user.revoke", {"user": "an"}, as_="boss")["job_id"])
    assert j["state"] == "succeeded", j
    assert r.ran("dseditgroup -o edit -d an -t user cfharness-users") and r.ran("web totp disable")
    assert env.state.db.one("SELECT state FROM domains WHERE sub='an'")["state"] == "revoked"
    assert not cf.dns_find("zid1", "an.example.com")
    # xoá hẳn: gõ sai tên → invalid; đúng → four-eyes (hệ 1 Super Admin: tự chạy sau 1 giờ + 24 giờ)
    assert env.err("user.delete", {"user": "an", "confirm": "sai", "reason": "nghỉ việc"}, as_="boss") == "invalid"
    r1 = env.call("user.delete", {"user": "an", "confirm": "an", "reason": "nghỉ việc"}, as_="boss")
    assert r1["error"]["code"] == "approval_required"
    env.clock.tick(3601)
    assert env.state.approvals.run_due() == 0  # còn chờ 24 giờ sau duyệt
    env.clock.tick(86400)
    env.state.approvals.run_due()
    assert r.ran("tar -czf") and r.ran("openssl enc") and r.ran("sysadminctl -deleteUser an")
    assert env.state.db.one("SELECT state FROM hub_users WHERE os_user='an'")["state"] == "deleted"


def test_restore(env):
    _active_user(env)
    env.ok("user.revoke", {"user": "an"}, as_="boss")
    j = env.state.jobs.get(env.ok("user.restore", {"user": "an"}, as_="boss")["job_id"])
    assert j["state"] == "succeeded", j
    assert env.state.db.one("SELECT state FROM hub_users WHERE os_user='an'")["state"] == "active"


def test_hub_ops(env, tmp_path):
    _active_user(env)
    r = env.fakes[2]
    env.ok("hub.restart", {"users": ["an"], "part": "both"}, as_="boss")
    assert r.ran("kickstart -k system/com.cfharness.daemon.an") and r.ran("kickstart -k system/com.cfharness.web.an")
    assert "output" in env.ok("hub.doctor", {"user": "an"}, as_="boss")
    logs = tmp_path / "Users" / "an" / ".cfharness" / "logs"
    logs.mkdir(parents=True)
    (logs / "daemon.out").write_text("dòng 1\ntoken sk-ant-oat01-AAAAAAAAAAAAAAAAAAAAAAAAAAAA\n", encoding="utf-8")
    out = env.ok("hub.logs", {"user": "an", "which": "daemon"}, as_="boss")
    assert out["lines"][0] == "dòng 1" and "AAAAAAAAAAAAAAAA" not in out["lines"][1]
    env.ok("hub.pin", {"user": "an", "tag": "v0.2.28"}, as_="boss")
    up = env.ok("hub.update", {"users": ["an"], "tag": "v0.2.30"}, as_="boss")
    assert up["skipped_pinned"] == ["an"]
    env.ok("hub.pin", {"user": "an"}, as_="boss")
    r.on("--version", Result(0, "cfharness 0.2.28", ""))
    up = env.ok("hub.update", {"users": ["an"], "tag": "v0.2.30"}, as_="boss")
    assert env.state.jobs.get(up["job_id"])["state"] == "succeeded"
    assert env.err("hub.config_set", {"user": "an", "key": "guard.enabled", "value": "false"}, as_="boss") == "invalid"
    env.ok("hub.config_set", {"user": "an", "key": "hub.max_concurrent_runs", "value": "3"}, as_="boss")


def test_access_ops(env):
    _active_user(env)
    r = env.fakes[2]
    out = env.ok("access.totp_reset", {"user": "an"}, as_="boss")
    assert out["totp_url"].startswith("https://an.example.com/enroll?t=")
    assert TOKEN not in json.dumps(env.state.audit.search(limit=500))
    rows = env.state.audit.search(op_prefix="access.totp_reset")
    assert rows and "enroll?t=" not in json.dumps(rows)  # link không vào audit
    r.on("web unlock-login", Result(2, "", "unknown"))
    assert env.ok("access.login_unlock", {"user": "an"}, as_="boss")["restarted"] is True
    env.ok("access.sessions_revoke", {"user": "an"}, as_="boss")
    env.make_admin("aud", role="auditor")
    assert env.err("access.totp_reset", {"user": "an"}, as_="aud") == "forbidden"
