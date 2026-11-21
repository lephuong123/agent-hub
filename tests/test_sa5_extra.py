"""SA5: ESTOP hẹn gỡ, tài nguyên + lịch sử, nhập CSV, cập nhật theo đợt, báo cáo tuần/tháng, xuất hồ sơ + trang SAC."""

from __future__ import annotations

import json

import pytest

from cfharness import admin_control as AC
from cfharness.admind.collector import collect
from cfharness.admind.ops_extra import run_estop_timers
from cfharness.admind.runner import Result

from admind_fakes import hub_status, install_fakes
from admind_helpers import make_env
from test_sac_web import H, Signer, login, make_client

TOKEN = "sk-ant-oat01-" + "k" * 90


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    for d in ("control", "admin-log", "policy", "status", "audit", "report"):
        (tmp_path / "shared" / d).mkdir(parents=True)
    e.fakes = install_fakes(e, tmp_path)
    m, cf, r = e.fakes
    m.group, m.plist_set, m.ports = ["an", "binh", "chi"], {"an", "binh", "chi"}, {"an": 8721, "binh": 8723, "chi": 8725}
    m.hubs = [hub_status("an"), hub_status("binh"), hub_status("chi")]
    e.make_admin("boss")
    for u in ("an", "binh", "chi"):
        e.ok("user.import", {"os_user": u}, as_="boss")
    collect(e.state)
    yield e
    e.state.db.close()


def test_estop_timed_auto_off(env):
    env.ok("estop.on_timed", {"scope": "user", "ids": ["an"], "hours": 1}, as_="boss")
    assert (AC.control_dir() / "estop.an").exists()
    assert run_estop_timers(env.state) == 0
    env.clock.tick(3601)
    assert run_estop_timers(env.state) == 1
    assert not (AC.control_dir() / "estop.an").exists()
    assert env.state.audit.search(op_prefix="estop.auto_off")


def test_resources_history(env):
    r = env.ok("sys.resources", as_="boss")
    assert "disk" in r and len(r["users_disk"]) == 3
    h = env.ok("sys.history", {"days": 30}, as_="boss")
    assert h["series"] and h["series"][0]["cost"] == pytest.approx(4.5)


def test_csv_import(env):
    m, cf, r = env.fakes
    aid = env.ok("claude.account_add", {"name": "Max", "token": TOKEN}, as_="boss")["id"]
    bad = "os_user,full_name\ndung,Dũng\ndung,Trùng\nRoot,Sai\n"
    out = env.ok("user.import_csv", {"csv": bad, "claude_account_id": aid}, as_="boss")
    errs = {x["line"]: x["errors"] for x in out["rows"]}
    assert not errs[2] and errs[3] and errs[4]
    good = "os_user,full_name,email\ndung,Lê Dũng,dung@ex.com\nem,Trần Em,\n"
    out = env.ok("user.import_csv", {"csv": good, "claude_account_id": aid}, as_="boss")
    assert out["dry_run"] and out["valid"] == 2
    env.state.extra["fake_fs"] = True
    env.state.extra["launchd_dir"] = str(env.state.paths.root / "ld")
    (env.state.paths.root / "ld").mkdir()
    r.on(lambda a: a[:2] == ["sysadminctl", "-addUser"], lambda a: (m.existing.add(a[2]), Result(0, "", ""))[1])
    r.on("--version", Result(0, "cfharness 0.2.28", ""))
    r.on("Reply with just: OK", Result(0, "OK", ""))
    r.on("web totp enroll", Result(0, "https://x/enroll?t=abc", ""))
    out = env.ok("user.import_csv", {"csv": good, "claude_account_id": aid, "dry_run": False}, as_="boss")
    assert len(out["jobs"]) == 2
    assert {u["os_user"] for u in env.state.db.all("SELECT os_user FROM hub_users WHERE state='active'")} >= {"dung", "em"}


def test_rollout_waves(env):
    m, cf, r = env.fakes
    env.state.extra["fast_soak"] = True
    r.on("--version", Result(0, "cfharness 0.2.28", ""))
    out = env.ok("hub.rollout", {"tag": "v0.2.30", "canary": "an", "batch": 1, "soak_minutes": 1}, as_="boss")
    assert out["waves"] == [["an"], ["binh"], ["chi"]]
    j = env.state.jobs.get(out["job_id"])
    assert j["state"] == "succeeded", j
    assert sum(1 for a, _ in r.calls if "agent-hub@v0.2.30" in " ".join(a)) == 3


def test_rollout_stops_on_canary_alert(env):
    m, cf, r = env.fakes
    env.state.extra["fast_soak"] = True
    r.on("--version", Result(0, "cfharness 0.2.28", ""))
    m.hubs = [hub_status("an", state="stale"), hub_status("binh"), hub_status("chi")]
    collect(env.state)
    out = env.ok("hub.rollout", {"tag": "v0.2.30", "canary": "an", "batch": 2, "soak_minutes": 1}, as_="boss")
    j = env.state.jobs.get(out["job_id"])
    assert j["state"] == "failed" and j["resume_from"] == "soak"
    assert sum(1 for a, _ in r.calls if "agent-hub@v0.2.30" in " ".join(a)) == 1


def test_report_period_and_export(env):
    uid = env.state.db.one("SELECT id FROM hub_users WHERE os_user='an'")["id"]
    for i, day in enumerate(("2026-11-18", "2026-11-19")):
        env.state.db.exec("INSERT INTO daily_reports(user_id, day, level, data, anomalies, ingested_at) VALUES (?,?,?,?,?,?)",
                          (uid, day, "metrics", json.dumps({"runs": {"total": 3, "failed": 1}, "tasks": {"done": 1}, "cost_usd": 2.0}), "[]", 0))
    r = env.ok("report.period", {"period": "week", "end": "2026-11-20"}, as_="boss")
    assert r["rows"][0]["os_user"] == "an" and r["rows"][0]["runs"] == 6 and r["totals"]["cost"] == 4.0
    csv_text = env.ok("user.export", {"users": ["an", "binh"]}, as_="boss")["csv"]
    assert csv_text.splitlines()[0].startswith("os_user") and "binh" in csv_text


def test_sa5_pages(env):
    s = Signer()
    c = make_client(env, s)
    login(env, c, s)
    for path in ("/system", "/users/bulk", "/users/export", "/updates", "/reports/period", "/reports/period?period=month", "/estop"):
        r = c.get(path, headers=H(s))
        assert r.status_code == 200, (path, r.text[:300])
