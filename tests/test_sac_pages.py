"""Trang SAC dựng được với dữ liệu thật của admind (người dùng, hồ sơ mọi tab, tên miền, cảnh báo, ESTOP, CSV) + escape dữ liệu hub."""

from __future__ import annotations

import pytest

from cfharness.admind.collector import collect

from admind_fakes import hub_status, install_fakes
from admind_helpers import make_env
from test_sac_web import H, Signer, csrf_of, login, make_client


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    (tmp_path / "shared" / "control").mkdir(parents=True)
    (tmp_path / "shared" / "admin-log").mkdir(parents=True)
    e.fakes = install_fakes(e, tmp_path)
    m = e.fakes[0]
    m.group, m.plist_set, m.ports = ["lamlinh"], {"lamlinh"}, {"lamlinh": 8721}
    m.hubs = [hub_status("lamlinh", state="stale")]
    e.make_admin("boss")
    e.ok("zone.add", {"name": "example.com"}, as_="boss")
    e.ok("user.import", {"os_user": "lamlinh", "full_name": "<script>alert(1)</script>"}, as_="boss")
    collect(e.state)
    yield e
    e.state.db.close()


@pytest.fixture
def client(env):
    s = Signer()
    c = make_client(env, s)
    login(env, c, s)
    c.signer = s
    return c


def test_pages_render(env, client):
    s = client.signer
    uid = env.state.db.one("SELECT id FROM hub_users WHERE os_user='lamlinh'")["id"]
    for path in ("/", "/users", "/users/new", f"/users/{uid}/logs", "/users/import", "/groups", f"/users/{uid}", "/domains", "/alerts", "/alerts?all=1", "/estop"):
        r = client.get(path, headers=H(s))
        assert r.status_code == 200, (path, r.text[:300])
    for tab in ("overview", "access", "agents", "activity", "policy", "reports", "history"):
        r = client.get(f"/users/{uid}?tab={tab}", headers=H(s))
        assert r.status_code == 200, (tab, r.text[:400])
    html = client.get("/users", headers=H(s)).text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html  # escape
    assert "lamlinh.example.com" in html


def test_csv_exports(env, client):
    s = client.signer
    r = client.get("/users?format=csv", headers=H(s))
    assert r.headers["content-type"].startswith("text/csv") and "lamlinh" in r.text
    assert "lamlinh.example.com" in client.get("/domains?format=csv", headers=H(s)).text
    assert "auth.login" in client.get("/audit?format=csv", headers=H(s)).text


def test_estop_from_web(env, client):
    s = client.signer
    tok = csrf_of(client.get("/estop", headers=H(s)).text)
    r = client.post("/op/estop.on", data={"csrf": tok, "p.scope": "user", "p.ids:multi": "lamlinh", "p.reason_code": "security", "next": "/estop"},
                    headers=H(s), follow_redirects=False)
    assert r.status_code == 303
    assert "lamlinh" in client.get("/estop", headers=H(s)).text
    from cfharness import admin_control as AC

    assert (AC.control_dir() / "estop.lamlinh").exists()


def test_api_overview(env, client):
    r = client.get("/api/overview", headers=H(client.signer))
    assert r.status_code == 200 and r.json()["users"]["total"] == 1


def test_team_report_cost_pages(env, client):
    s = client.signer
    t = env.ok("team.upsert", {"slug": "doi-a", "name": "Đội A"}, as_="boss")["id"]
    env.ok("team.member_set", {"team_id": t, "user": "lamlinh"}, as_="boss")
    env.ok("team.task_create", {"team_id": t, "title": "<i>Việc 1</i>", "assignee": "lamlinh"}, as_="boss")
    for path in ("/teams", f"/teams/{t}", "/reports", "/reports?format=csv", "/costs", "/costs?by=day", "/costs?format=csv"):
        r = client.get(path, headers=H(s))
        assert r.status_code == 200, (path, r.text[:300])
    html = client.get(f"/teams/{t}", headers=H(s)).text
    assert "&lt;i&gt;Việc 1&lt;/i&gt;" in html and "Chờ nhận" in html
    assert ' style="' not in html  # CSP: không style inline


def test_policy_claude_pages(env, client):
    s = client.signer
    env.ok("policy.save", {"scope": "default", "content": {"budget": {"usd_per_day": 20}}}, as_="boss")
    env.ok("claude.account_add", {"name": "Max 1", "token": "sk-ant-oat01-" + "q" * 90}, as_="boss")
    for path in ("/policy", "/policy?scope=machine", "/policy?scope=default&sim=1&c.budget.usd_per_day=5&c.require.sandbox=true",
                 "/policy?user=lamlinh", "/claude"):
        r = client.get(path, headers=H(s))
        assert r.status_code == 200, (path, r.text[:300])
    html = client.get("/policy?scope=default&sim=1&c.budget.usd_per_day=5", headers=H(s)).text
    assert "Mô phỏng" in html and 'name="p.content:json"' in html
    assert "q" * 90 not in client.get("/claude", headers=H(s)).text
