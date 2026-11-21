"""SA3: hồ sơ SPEC v3 + báo cáo ngày (hub xuất → admind đọc), luồng giao việc Team user đầu-cuối (admind ↔ hub), gom + gửi báo cáo."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import pytest

from cfharness import admin_control as AC
from cfharness.admin_profile import ProfileExporter, read_profile, read_report
from cfharness.admind.ops_teams import sync as team_sync
from cfharness.admind.reports import tick as report_tick

from admind_fakes import hub_status, install_fakes
from admind_helpers import make_env

ME = AC.os_user()


@pytest.fixture
def shared(tmp_path, monkeypatch):
    root = tmp_path / "shared"
    for d in ("control", "admin-log", "policy", "status", "audit", "report", "teams"):
        (root / d).mkdir(parents=True)
    (root / "status" / ME).mkdir()
    (root / "report" / ME).mkdir()
    monkeypatch.setenv("CFHARNESS_TEST_SHARED_ROOT", str(root))
    return root


async def test_profile_and_daily_report_export(hub_home, shared):
    from m7_fake import close, make_daemon

    d = await make_daemon(hub_home)
    try:
        d.hub.agents.create({"id": "w1", "name": "Người viết <b>", "model": "haiku"})
        ex = ProfileExporter(d.hub)
        ex.tick(shared / "status", None)
        prof = json.loads((shared / "status" / ME / "profile.json").read_text(encoding="utf-8"))
        assert prof["schema"] == 3 and "activity" not in prof  # mức Hoạt động tắt mặc định
        assert any(a["name"] == "Người viết <b>" for a in prof["agents"])
        yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        rep = json.loads((shared / "report" / ME / f"{yday}.json").read_text(encoding="utf-8"))
        assert rep["level"] == "metrics" and "items" not in rep and rep["runs"]["total"] == 0
        # bật mức Hoạt động (admind ghi visibility.json) → hồ sơ có activity
        (shared / "control" / "visibility.json").write_text(json.dumps({"activity": True, "transparency": False}))
        ex.last_profile = 0
        ex.tick(shared / "status", None)
        assert "activity" in json.loads((shared / "status" / ME / "profile.json").read_text(encoding="utf-8"))
        # phía admind đọc + lọc lại
        p = read_profile(ME)
        assert p and p["schema"] == 3 and isinstance(p["agents"], list)
        assert read_report(ME, yday)["runs"]["total"] == 0
        assert read_report(ME, "../../etc") is None
    finally:
        await close(d)


def test_clean_profile_drops_junk():
    from cfharness.admin_profile import clean_profile

    p = clean_profile({"schema": 3, "agents": [{"name": "x" * 500, "trust": "god", "mcp": ["a", 5]}], "tasks": {"done": 3, "bad": "x"},
                       "evil": "<script>", "activity": {"recent": [{"title": "t‮x", "duration_s": -5}]}})
    assert "evil" not in p and p["agents"][0]["trust"] == "normal" and len(p["agents"][0]["name"]) <= 60
    assert p["tasks"] == {"done": 3} and p["activity"]["recent"][0]["duration_s"] is None


async def test_team_task_flow_end_to_end(tmp_path, monkeypatch, hub_home, shared):
    from m7_fake import close, make_daemon

    env = make_env(tmp_path, monkeypatch)
    m, cf, r = install_fakes(env, tmp_path)
    monkeypatch.setenv("CFHARNESS_TEST_SHARED_ROOT", str(shared))
    m.group, m.plist_set, m.ports, m.hubs = [ME], {ME}, {ME: 8721}, [hub_status(ME)]
    env.make_admin("boss")
    env.ok("user.import", {"os_user": ME}, as_="boss")
    t = env.ok("team.upsert", {"slug": "du-an-a", "name": "Dự án A"}, as_="boss")["id"]
    env.ok("team.member_set", {"team_id": t, "user": ME, "role": "member", "skills": ["nội dung"]}, as_="boss")
    env.make_admin("lead", role="team_lead", scope={"teams": [t]})
    first = env.ok("team.task_create", {"team_id": t, "title": "Viết báo cáo", "description": "Tóm tắt Q4", "assignee": ME, "due": "2026-12-01"},
                   as_="lead")["id"]
    dep = env.ok("team.task_create", {"team_id": t, "title": "Gửi khách", "assignee": ME, "depends_on": [first]}, as_="lead")["id"]
    inbox = shared / "teams" / "du-an-a" / "inbox" / ME
    assert (inbox / f"{first}.json").exists() and not (inbox / f"{dep}.json").exists()  # việc phụ thuộc chưa giao
    # Team Lead không thao tác được team khác / hạ tầng
    t2 = env.ok("team.upsert", {"slug": "khac", "name": "Khác"}, as_="boss")["id"]
    assert env.err("team.get", {"id": t2}, as_="lead") == "forbidden"
    assert env.err("team.task_create", {"team_id": t2, "title": "x"}, as_="lead") == "forbidden"
    assert env.err("hub.restart", {"users": [ME]}, as_="lead") == "forbidden"

    d = await make_daemon(hub_home)
    try:
        d.hub.agents.create({"id": "w1", "name": "w1", "model": "haiku"})
        ib = d.hub.team_inbox
        assert ib.sync() == 1
        items = ib.items()
        assert items[0]["title"] == "Viết báo cáo" and items[0]["state"] == "new"
        out = ib.accept(first, "w1")
        task = d.hub.tasks.get(out["task_id"])
        assert task["title"] == "Viết báo cáo" and "Tóm tắt Q4" in task["body"]
        team_sync(env.state)
        assert env.state.db.one("SELECT state FROM team_tasks WHERE id=?", (first,))["state"] == "doing"
        ib.submit(first, "teams/du-an-a/shared/bao-cao.md", "xong")
        team_sync(env.state)
        row = env.state.db.one("SELECT * FROM team_tasks WHERE id=?", (first,))
        assert row["state"] == "review" and row["result_path"].endswith("bao-cao.md")
        env.ok("team.task_review", {"id": first, "approve": True}, as_="lead")
        assert (inbox / f"{dep}.json").exists()  # phụ thuộc xong → việc sau được giao
        ib.sync()
        assert {i["id"]: i["state"] for i in ib.items()}[first] == "done"
        # từ chối việc thứ hai
        ib.reject(dep, "không đủ thông tin")
        team_sync(env.state)
        row = env.state.db.one("SELECT * FROM team_tasks WHERE id=?", (dep,))
        assert row["state"] == "unassigned" and "không đủ" in row["reject_reason"]
        board = env.ok("team.get", {"id": t}, as_="lead")
        assert board["members"][0]["os_user"] == ME and len(board["tasks"]) == 2
    finally:
        await close(d)
        env.state.db.close()


def test_reports_ingest_anomaly_and_send(tmp_path, monkeypatch, shared):
    env = make_env(tmp_path, monkeypatch)
    m, cf, r = install_fakes(env, tmp_path)
    monkeypatch.setenv("CFHARNESS_TEST_SHARED_ROOT", str(shared))
    m.group, m.plist_set, m.hubs = [ME], {ME}, [hub_status(ME)]
    env.make_admin("boss")
    env.ok("user.import", {"os_user": ME}, as_="boss")
    sink: list[str] = []
    env.state.extra["notify_sink"] = sink
    env.clock.t = time.mktime(datetime(2026, 11, 20, 9, 0).timetuple())
    days = [(datetime(2026, 11, 20) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, 5)]
    for i, day in enumerate(days):
        cost = 9.0 if i == 0 else 1.0
        rep = {"schema": 1, "user": ME, "day": day, "level": "activity", "runs": {"total": 10, "completed": 6, "failed": 4},
               "tasks": {"done": 2, "stuck": 0}, "cost_usd": cost, "active_hours": [9, 10],
               "items": [{"title": "Soạn hợp đồng", "agent": "w1", "status": "completed", "summary": "đã soạn"}]}
        (shared / "report" / ME / f"{day}.json").write_text(json.dumps(rep), encoding="utf-8")
    from cfharness.admind.reports import ingest

    u = env.state.db.one("SELECT id, os_user FROM hub_users")
    for day in reversed(days[1:]):
        ingest(env.state, u, day)
    report_tick(env.state)
    rows = env.ok("report.day", {"day": days[0]}, as_="boss")["rows"]
    assert rows[0]["cost"] == 9.0 and "cost_spike" in rows[0]["anomalies"] and "failure_rate" in rows[0]["anomalies"]
    assert sink and "Báo cáo ngày toàn hệ" in sink[0] and ME in sink[0]
    # mức Hoạt động tắt → không có items; bật (kèm số văn bản) → có
    assert "items" not in env.ok("report.get", {"user": ME, "day": days[0]}, as_="boss")["data"]
    env.ok("sys.config_set", {"key": "visibility.activity", "value": True, "ref": "QĐ 12/2026"}, as_="boss")
    assert env.ok("report.get", {"user": ME, "day": days[0]}, as_="boss")["data"]["items"][0]["title"] == "Soạn hợp đồng"
    assert env.ok("report.search", {"q": "hợp đồng"}, as_="boss")
    assert json.loads((shared / "control" / "visibility.json").read_text())["activity"] is True
    env.make_admin("opr", role="operator")
    assert env.err("report.search", {"q": "hợp đồng"}, as_="opr") == "forbidden"  # Operator không xem mức Hoạt động
    cost = env.ok("cost.report", {"by": "user"}, as_="boss")
    assert "rows" in cost
    env.state.db.close()


def test_activity_needs_reference(tmp_path, monkeypatch, shared):
    env = make_env(tmp_path, monkeypatch)
    monkeypatch.setenv("CFHARNESS_TEST_SHARED_ROOT", str(shared))
    env.make_admin("boss")
    assert env.err("sys.config_set", {"key": "visibility.activity", "value": True}, as_="boss") == "invalid"
    env.state.db.close()


async def test_hub_team_inbox_page(hub_home, shared):
    from test_web import ISOLATED, _start, _stop

    from cfharness.runtime import FakeRuntime

    env = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        await env.login()
        html = (await env.client.get("/costs")).text
        assert 'href="/team-inbox"' in html  # thư mục teams có → menu hiện
        assert (await env.client.get("/team-inbox")).status_code == 200
        assert "Cần bạn" in (await env.client.get("/inbox")).text  # trang "Cần bạn" có sẵn không bị đè
    finally:
        await _stop(env)
