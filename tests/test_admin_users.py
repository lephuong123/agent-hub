"""Quản lý user + bố cục status/<u>/<hub_id>.json + setup check (admin_users.py, admin_overview.py — plan-admin-users P1/P2).

Không gọi sudo/dseditgroup/launchctl thật: runner giả, euid tiêm, lstat giả.
"""

from __future__ import annotations

import json
import os
import stat
import time
from types import SimpleNamespace

import pytest

from cfharness import admin_control as ac
from cfharness import admin_overview as ao
from cfharness import admin_users as au

ME = ao.os_user()
HID = "0123456789abcdef"


def _sample(**over) -> dict:
    d = {"schema": 2, "user": ME, "hub_id": HID, "version": "0.2.8", "generated_at": "2026-11-10T08:00:00Z", "generated_ts": time.time(),
         "interval_s": 60, "daemon": {"up": True}, "cost_usd": {"today": 1.0, "week": 3.0, "month": 9.5}, "runs": {"running": 1},
         "policy": {"rev_applied": 2, "valid": True}}
    d.update(over)
    return d


def test_reader_new_layout_and_legacy(tmp_path):
    d = tmp_path / "status"
    (d / ME).mkdir(parents=True)
    (d / ME / f"{HID}.json").write_text(json.dumps(_sample()))
    (d / ME / "fedcba9876543210.json").write_text(json.dumps(_sample()))  # hub_id trong file ≠ tên file → sai chủ
    (d / f"{ME}.json").write_text(json.dumps(_sample(schema=1)))  # bố cục phẳng cũ, schema 1 vẫn đọc được
    (d / ME / "notes.txt").write_text("x")
    os.symlink(d / ME, d / "evil")  # thư mục user là symlink → không theo
    r = ao.read_dir(d)
    by = {(h["user"], h["layout"], h.get("hub_id")): h for h in r["hubs"]}
    assert by[(ME, "dir", HID)]["state"] == "ok" and by[(ME, "dir", HID)]["data"]["cost_usd"]["month"] == 9.5
    assert by[(ME, "dir", "fedcba9876543210")]["state"] == "owner_mismatch"
    assert by[(ME, "flat", HID)]["state"] == "ok"
    assert by[("evil", "dir", None)]["error"] == "symlink"
    assert r["user_dirs"] == [ME]


def test_exporter_prefers_user_dir(hub_home, tmp_path):
    from cfharness.hub import Hub

    hub = Hub(hub_home)
    try:
        d = tmp_path / "status"
        d.mkdir()
        hub.config.set("admin_export.dir", str(d))
        hub.config.set("admin_export.enabled", True)
        assert hub.admin_export.export_once() == d / f"{ME}.json"  # chưa cấp thư mục riêng → bố cục cũ
        (d / ME).mkdir()
        p = hub.admin_export.export_once()
        assert p == d / ME / f"{ao.hub_id(hub_home.home)}.json"
        data = json.loads(p.read_text())
        assert data["schema"] == 2 and ao.sanitize(data) == data
        for sec in ("guard", "claude", "web", "chat", "disk", "policy", "admin_estop"):
            assert sec in data, sec
        assert ao.read_dir(d)["hubs"][0]["state"] in ("ok", "stopped")
    finally:
        hub.store.close()


def test_user_states():
    ok = {"state": "ok", "data": {"policy": {"rev_applied": 2, "valid": True}}}
    assert au.user_state([], member=True, plist=False) == "not_installed"
    assert au.user_state([], member=True, plist=True) == "installed_no_export"
    assert au.user_state([ok], member=True, plist=True) == "ok"
    assert au.user_state([ok], member=False, plist=True) == "revoked_running"
    assert au.user_state([{"state": "stale"}], member=True, plist=True) == "stale"
    assert au.user_state([{"state": "malformed"}], member=True, plist=True) == "bad_file"
    assert au.user_state([ok], member=True, plist=True, expected_rev=3) == "policy_mismatch"
    assert au.user_state([{"state": "stopped", "data": {}}], member=True, plist=True) == "stopped"


def test_list_users_merges_sources(tmp_path):
    plist = tmp_path / "LaunchDaemons"
    plist.mkdir()
    (plist / "com.cfharness.daemon.an.plist").write_text("x")
    (plist / "com.cfharness.daemon.binh.plist").write_text("x")
    (plist / "com.other.plist").write_text("x")
    ov = {"hubs": [{"user": "an", "hub_id": HID, "state": "ok", "data": {"cost_usd": {"today": 1.5, "month": 4}, "runs": {"running": 2}}},
                   {"user": "cuong", "hub_id": HID, "state": "ok", "data": {}}],
          "user_dirs": ["an", "dung"]}
    rows = {r["user"]: r for r in au.list_users(ov, plist_dir=plist, members=["an", "binh", "dung"])}
    assert rows["an"]["state"] == "ok" and rows["an"]["cost_today_usd"] == 1.5 and rows["an"]["running"] == 2
    assert rows["binh"]["state"] == "installed_no_export"
    assert rows["cuong"]["state"] == "revoked_running"  # không còn trong nhóm nhưng hub vẫn xuất
    assert rows["dung"]["state"] == "not_installed" and rows["dung"]["granted"]


def test_setup_check_with_fake_stat(tmp_path, monkeypatch, as_admin):
    root = tmp_path / "shared"
    monkeypatch.setenv("CFHARNESS_TEST_SHARED_ROOT", str(root))
    as_admin(users=["an"], admins=["boss", "an"])
    uid = {"root": 0, "an": 501}
    gid = {"wheel": 0, "cfharness-admin": 4242}
    spec = {str(root): ("root", "wheel", 0o755), str(root / "status"): ("boss", "cfharness-admin", 0o3773),
            str(root / "control"): ("root", "cfharness-admin", 0o2775), str(root / "status" / "an"): ("an", "cfharness-admin", 0o750)}

    def lstat(p):
        if p not in spec:
            raise FileNotFoundError(p)
        o, g, m = spec[p]
        return SimpleNamespace(st_mode=stat.S_IFDIR | m, st_uid=uid.get(o, 777), st_gid=gid[g])

    (root / "status" / "an").mkdir(parents=True)
    monkeypatch.setattr(au, "_owner_name", lambda u: {0: "root", 501: "an", 777: "boss"}.get(u))
    r = au.setup_check(me="boss", lstat=lstat, gid_name=lambda g: {0: "wheel", 4242: "cfharness-admin"}.get(g), admin=True)
    by = {c["id"]: c for c in r["checks"]}
    assert by["group:cfharness-admin"]["level"] == "ok" and by["group:cfharness-users"]["level"] == "ok"
    assert by["group:overlap"]["level"] == "warn" and "an" in by["group:overlap"]["detail"]
    assert by["dir:/"]["level"] == "ok" and by["dir:control"]["level"] == "ok"
    assert by["dir:status"]["level"] == "warn" and "bố cục Q6 cũ" in by["dir:status"]["detail"]
    assert by["dir:policy"]["level"] == "fail" and any("mkdir" in f for f in by["dir:policy"]["fix"])
    assert by["user_dir:an"]["level"] == "fail" and "2750" in by["user_dir:an"]["detail"]
    assert r["level"] == "fail" and not r["ok"]


def test_add_and_revoke_plans(tmp_path, monkeypatch):
    root = tmp_path / "shared"
    for d in ("control", "admin-log", "status"):
        (root / d).mkdir(parents=True)
    monkeypatch.setenv("CFHARNESS_TEST_SHARED_ROOT", str(root))
    with pytest.raises(ValueError):
        au.AddPlan("an; rm -rf /", root / "status")
    with pytest.raises(ValueError):
        au.AddPlan("root", root / "status")
    calls: list[list[str]] = []
    plan = au.AddPlan("an", root / "status", runner=lambda argv: (calls.append(argv) or (0, "", ""))).build()
    cmds = plan.commands()
    assert cmds[0] == "sudo dseditgroup -o edit -a an -t user cfharness-users"
    assert f"sudo chmod 2750 {root / 'status' / 'an'}" in cmds and any("install-daemon --user an" in c for c in cmds)
    with pytest.raises(PermissionError):
        plan.apply(euid=501)
    assert not calls
    done = plan.apply(euid=0)
    assert ["/usr/sbin/dseditgroup", "-o", "edit", "-a", "an", "-t", "user", "cfharness-users"] in calls
    assert ["/bin/chmod", "2750", str(root / "status" / "an")] in calls and done[-1] == "admin-log: user_add an"
    assert ac.admin_log_read("an")[0]["action"] == "user_add"
    calls.clear()
    rp = au.RevokePlan("an", root / "status", runner=lambda argv: (calls.append(argv) or (1 if "bootout" in argv else 0, "", "")))
    rp.build()
    assert any("estop.an" in c for c in rp.commands()) and not any("rm -rf" in c for c in rp.commands())
    rp.apply(euid=0)
    assert (root / "control" / "estop.an").exists() and ac.read_flags("an")["reason_code"] == "revoked"
    assert ["/usr/sbin/dseditgroup", "-o", "edit", "-d", "an", "-t", "user", "cfharness-users"] in calls
    assert [r["action"] for r in ac.admin_log_read("an")][:2] == ["user_revoke", "estop_on"]


def test_install_daemon_system_install(tmp_path):
    from cfharness.systems.autostart import SYSTEM_PROGRAM, LaunchDaemonPlan

    p = LaunchDaemonPlan("an", user_home=tmp_path / "an", system_install=True)
    assert p.argv()[0] == SYSTEM_PROGRAM and p.path_env.startswith("/opt/cfharness/bin:")
    cmds = p.commands()
    assert any("uv tool install" in c and "UV_TOOL_BIN_DIR=/opt/cfharness/bin" in c for c in cmds)
    assert any("chown -R root:wheel /opt/cfharness" in c for c in cmds)
    q = LaunchDaemonPlan("an", user_home=tmp_path / "an")
    assert q.argv()[0].endswith(".local/bin/cfharness") and not any("uv tool install" in c for c in q.commands())


async def test_users_rpc_requires_admin(hub_home, tmp_path, as_admin):
    from m7_fake import close, make_daemon, rpc

    from cfharness import auth
    from cfharness.errors import HubError

    d = await make_daemon(hub_home)
    try:
        status = tmp_path / "status"
        (status / ME).mkdir(parents=True)
        (status / ME / f"{HID}.json").write_text(json.dumps(_sample()))
        d.hub.config.set("admin_export.dir", str(status))
        as_admin(users=[ME])
        r = await rpc(d, "admin.users")
        assert [u["user"] for u in r["users"] if u["user"] == ME] == [ME]
        detail = await rpc(d, "admin.user", {"user": ME})
        assert detail["hubs"][0]["hub_id"] == HID and "history" in detail
        with pytest.raises(HubError):
            await rpc(d, "admin.user", {"user": "../etc"})
        agent = auth.Caller(via="socket", pid=99, token_ok=True, run_id="r_x", agent_id="w1", evidence=["env_run_id"])
        for m in ("admin.users", "admin.user", "admin.alerts", "admin.audit", "admin.policy", "admin.self", "admin.nav", "admin.setup_check"):
            assert m in auth.HUMAN_ONLY, m
            with pytest.raises(HubError):
                await rpc(d, m, {"user": ME}, as_caller=agent)
        ac._GETGROUPS = lambda: []
        for m in ("admin.users", "admin.alerts", "admin.audit"):
            with pytest.raises(HubError) as ei:
                await rpc(d, m, {})
            assert "cfharness-admin" in str(ei.value)
        with pytest.raises(HubError):
            await rpc(d, "admin.policy", {"user": "someoneelse"})
        assert (await rpc(d, "admin.policy", {}))["exists"] in (True, False)  # chính sách của chính mình: ai cũng xem được
    finally:
        await close(d)
