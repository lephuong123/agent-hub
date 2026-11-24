"""admind SA1-3..SA1-5 + tên miền: nhập user có sẵn, thu thập số liệu, danh sách theo phạm vi, hồ sơ, ESTOP, cảnh báo, zone/tên miền, tunnel."""

from __future__ import annotations

import pytest

from cfharness import admin_control as AC
from cfharness.admind.collector import collect
from cfharness.admind.health import check_domains
from cfharness.admind.tunnel import Ingress, IngressError, TunnelConfig

from admind_fakes import hub_status, install_fakes
from admind_helpers import make_env


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    (tmp_path / "shared" / "control").mkdir(parents=True)
    (tmp_path / "shared" / "admin-log").mkdir(parents=True)
    e.fakes = install_fakes(e, tmp_path)
    yield e
    e.state.db.close()


def _seed(env):
    m, cf, r = env.fakes
    m.group = ["lamlinh", "an"]
    m.plist_set = {"lamlinh", "an"}
    m.ports = {"lamlinh": 8721, "an": 8723}
    m.hubs = [hub_status("lamlinh"), hub_status("an", state="stale", cost=0.5)]
    env.make_admin("boss")
    env.ok("zone.add", {"name": "example.com"}, as_="boss")
    cands = env.ok("user.import_scan", as_="boss")
    assert {c["os_user"] for c in cands} == {"lamlinh", "an"}
    lam = next(c for c in cands if c["os_user"] == "lamlinh")
    assert lam["hostnames"] == ["lamlinh.example.com"] and lam["web_port"] == 8721
    env.ok("user.import", {"os_user": "lamlinh", "full_name": "Lâm Linh", "email": "lam@ex.com"}, as_="boss")
    env.ok("user.import", {"os_user": "an"}, as_="boss")


def test_import_and_collect(env):
    _seed(env)
    assert env.ok("user.import_scan", as_="boss") == []
    collect(env.state)
    rows = {r["os_user"]: r for r in env.ok("user.list", as_="boss")}
    assert rows["lamlinh"]["hostname"] == "lamlinh.example.com" and rows["lamlinh"]["hub_status"] == "ok"
    assert rows["lamlinh"]["cost_today"] == 1.5 and rows["lamlinh"]["running"] == 1
    assert rows["an"]["hub_status"] == "stale" and rows["an"]["alerts"] >= 1
    ov = env.ok("overview", as_="boss")
    assert ov["users"]["active"] == 2 and ov["hubs"]["up"] == 1 and ov["cost_today"] == 2.0
    prof = env.ok("user.profile", {"id": "lamlinh"}, as_="boss")
    assert prof["identity"]["full_name"] == "Lâm Linh" and prof["domains"][0]["sub"] == "lamlinh"
    assert prof["activity_visible"] is False and prof["activity"] is None


def test_alert_resolves_when_hub_recovers(env):
    _seed(env)
    collect(env.state)
    an = env.state.db.one("SELECT id FROM hub_users WHERE os_user='an'")["id"]
    assert env.state.db.one("SELECT * FROM alerts WHERE subject=? AND code='stale' AND state='on'", (an,))
    env.fakes[0].hubs = [hub_status("lamlinh"), hub_status("an")]
    collect(env.state)
    assert not env.state.db.one("SELECT * FROM alerts WHERE subject=? AND code='stale' AND state!='resolved'", (an,))


def test_operator_scope_filters(env):
    _seed(env)
    g = env.ok("group.create", {"name": "Phòng A"}, as_="boss")["id"]
    lam = env.state.db.one("SELECT id FROM hub_users WHERE os_user='lamlinh'")["id"]
    env.ok("user.update", {"id": lam, "group_id": g}, as_="boss")
    env.make_admin("opr", role="operator", scope={"groups": [g]})
    rows = env.ok("user.list", as_="opr")
    assert [r["os_user"] for r in rows] == ["lamlinh"]
    assert env.err("user.profile", {"id": "an"}, as_="opr") == "forbidden"
    assert env.ok("user.profile", {"id": "lamlinh"}, as_="opr")
    # ESTOP: Operator bị giới hạn không bật được toàn máy / user ngoài phạm vi
    assert env.err("estop.on", {"scope": "all"}, as_="opr") == "forbidden"
    assert env.err("estop.on", {"scope": "user", "ids": ["an"]}, as_="opr") == "forbidden"
    env.ok("estop.on", {"scope": "user", "ids": ["lamlinh"], "reason_code": "maintenance"}, as_="opr")


def test_estop_flags(env):
    _seed(env)
    env.ok("estop.on", {"scope": "user", "ids": ["lamlinh"], "reason_code": "security"}, as_="boss")
    assert (AC.control_dir() / "estop.lamlinh").exists()
    flags = env.ok("estop.list", as_="boss")
    assert flags[0]["user"] == "lamlinh" and flags[0]["reason_code"] == "security"
    env.make_admin("opr", role="operator")
    assert env.err("estop.off", {"scope": "user", "ids": ["lamlinh"]}, as_="opr") == "forbidden"  # Operator không gỡ
    env.ok("estop.off", {"scope": "user", "ids": ["lamlinh"]}, as_="boss")
    assert not (AC.control_dir() / "estop.lamlinh").exists()
    env.ok("estop.on", {"scope": "all", "reason_code": "incident"}, as_="boss")
    assert env.ok("overview", as_="boss")["estop"] is True


def test_team_lead_estop_only_team(env):
    _seed(env)
    env.make_admin("lead", role="team_lead", scope={"teams": ["t_00000001"]})
    assert env.err("estop.on", {"scope": "all"}, as_="lead") == "forbidden"
    assert env.err("estop.on", {"scope": "user", "ids": ["an"]}, as_="lead") == "forbidden"
    assert env.err("estop.on", {"scope": "team", "ids": ["t_99999999"]}, as_="lead") == "forbidden"


def test_alert_ack_mute(env):
    _seed(env)
    collect(env.state)
    a = env.ok("alert.list", as_="boss")[0]
    env.ok("alert.ack", {"id": a["id"], "note": "đang xem"}, as_="boss")
    env.ok("alert.mute", {"id": a["id"], "hours": 2}, as_="boss")
    b = next(x for x in env.ok("alert.list", as_="boss") if x["id"] == a["id"])
    assert b["state"] == "ack" and b["muted"] and b["note"] == "đang xem"
    env.make_admin("aud", role="auditor")
    assert env.err("alert.ack", {"id": a["id"]}, as_="aud") == "forbidden"


def test_disk_low_alert(env):
    env.fakes[0].disk_info = {"total_gb": 500.0, "free_gb": 5.0}
    collect(env.state)
    assert env.state.db.one("SELECT * FROM alerts WHERE code='disk_low' AND state='on'")


# ============================================================================ tên miền
def test_zone_add_unknown(env):
    env.make_admin("boss")
    assert env.err("zone.add", {"name": "khong-co.org"}, as_="boss") == "not_found"


def test_domain_health_and_reconcile(env):
    _seed(env)
    m, cf, _r = env.fakes
    zid = env.state.db.one("SELECT cf_zone_id FROM zones WHERE name='example.com'")["cf_zone_id"]
    cf.records[zid] = [{"id": "r1", "type": "CNAME", "name": "lamlinh.example.com", "content": "tun-123.cfargotunnel.com", "proxied": True}]
    check_domains(env.state)
    d = env.ok("domain.list", as_="boss")
    assert d[0]["fqdn"] == "lamlinh.example.com" and d[0]["state"] == "healthy"
    issues = env.ok("domain.reconcile", as_="boss")
    assert any(i["kind"] == "hub_without_domain" and i["user"] == "an" for i in issues)
    # DNS trỏ tunnel khác → unhealthy + cảnh báo
    cf.records[zid][0]["content"] = "khac.cfargotunnel.com"
    check_domains(env.state)
    assert env.ok("domain.list", as_="boss")[0]["state"] == "unhealthy"
    assert env.state.db.one("SELECT * FROM alerts WHERE code='domain_dns' AND state='on'")


def test_domain_create_job(env):
    _seed(env)
    m, cf, r = env.fakes
    zid = env.state.db.one("SELECT id FROM zones WHERE name='example.com'")["id"]
    assert env.err("domain.create", {"user": "an", "zone_id": zid, "sub": "superadmin"}, as_="boss") == "invalid"  # tên cấm
    assert env.err("domain.create", {"user": "an", "zone_id": zid, "sub": "lamlinh"}, as_="boss") == "conflict"  # trùng
    out = env.ok("domain.create", {"user": "an", "zone_id": zid, "sub": "an-hub"}, as_="boss")
    j = env.state.jobs.get(out["job_id"])
    assert j["state"] == "succeeded", j
    assert ("cname", ("zid1", "an-hub.example.com", "tun-123.cfargotunnel.com")) in cf.calls  # ĐÚNG zone
    ing = Ingress.parse(m.ingress_path.read_text(encoding="utf-8"))
    assert ing.hosts()["an-hub.example.com"] == "http://127.0.0.1:8723"
    assert r.ran("cloudflared tunnel --config") and r.ran("ingress validate") and r.ran("config set web.public_origin https://an-hub.example.com")
    # thu hồi
    did = out["domain_id"]
    j2 = env.state.jobs.get(env.ok("domain.revoke", {"id": did}, as_="boss")["job_id"])
    assert j2["state"] == "succeeded", j2
    assert "an-hub.example.com" not in Ingress.parse(m.ingress_path.read_text(encoding="utf-8")).hosts()
    assert not cf.dns_find("zid1", "an-hub.example.com")


def test_tunnel_validate_failure_keeps_old(env, tmp_path):
    m, _cf, r = env.fakes
    r.on("ingress validate", __import__("admind_fakes").Result(1, "", "bad config"))
    tc = TunnelConfig(m.ingress_path, r)
    before = m.ingress_path.read_text(encoding="utf-8")
    with pytest.raises(IngressError):
        tc.apply(lambda ing: ing.set("x.example.com", "http://127.0.0.1:9000"))
    assert m.ingress_path.read_text(encoding="utf-8") == before


def test_tunnel_probe_failure_rolls_back(env):
    m, _cf, r = env.fakes
    tc = TunnelConfig(m.ingress_path, r, probe=lambda h: False, kickstart_wait_s=0)
    before = m.ingress_path.read_text(encoding="utf-8")
    with pytest.raises(IngressError):
        tc.apply(lambda ing: ing.set("x.example.com", "http://127.0.0.1:9000"), check_host="x.example.com")
    assert m.ingress_path.read_text(encoding="utf-8") == before


def test_tunnel_forbids_mcp_port(env):
    m, _cf, r = env.fakes
    tc = TunnelConfig(m.ingress_path, r, forbidden_ports=lambda: {8722})
    with pytest.raises(IngressError):
        tc.apply(lambda ing: ing.set("x.example.com", "http://127.0.0.1:8722"))


def test_ingress_roundtrip():
    ing = Ingress.parse("tunnel: t\ningress:\n  - hostname: a.x.com\n    service: http://127.0.0.1:8001\n  - service: http_status:404\n")
    assert ing.set("b.x.com", "http://127.0.0.1:8002") and not ing.set("b.x.com", "http://127.0.0.1:8002")
    txt = ing.dump()
    assert txt.strip().endswith("service: http_status:404")
    assert Ingress.parse(txt).hosts() == {"a.x.com": "http://127.0.0.1:8001", "b.x.com": "http://127.0.0.1:8002"}


def test_port_allocation(env):
    _seed(env)
    p = env.ok("port.next", as_="boss")
    assert p["web"] == 8711 and p["mcp"] == 8712
