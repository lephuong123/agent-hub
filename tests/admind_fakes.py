"""Bản giả cho test admind: máy (nhóm, plist, file trạng thái, ingress), Cloudflare API, runner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cfharness.admind.machine import Machine
from cfharness.admind.runner import FakeRunner, Result
from cfharness.admind.tunnel import Ingress


def hub_status(user: str, *, state: str = "ok", cost: float = 1.5, running: int = 1, version: str = "0.2.28", **extra: Any) -> dict[str, Any]:
    data = {"schema": 2, "user": user, "hub_id": "ab" * 8, "version": version, "daemon": {"up": True, "started_at": "2026-11-20T00:00:00Z"},
            "cost_usd": {"today": cost, "week": cost * 3, "month": cost * 10}, "runs": {"running": running, "queued": 0, "today_total": 5,
            "today_completed": 4, "today_failed": 1}, "approvals": {"open": 2, "oldest_wait_s": 10}, "agents": {"total": 3},
            "budget": {"day_pct": 40}, "web": {"public": True, "totp": True}, "claude": {"auth_ok": True}, "guard": {"sandbox": True}}
    data.update(extra)
    return {"user": user, "hub_id": "ab" * 8, "layout": "dir", "state": state, "age_s": 5, "stale": state == "stale", "data": data, "error": None}


class FakeMachine(Machine):
    def __init__(self, state: Any, ingress_path: Path | None = None) -> None:
        super().__init__(state)
        self.group: list[str] = []
        self.plist_set: set[str] = set()
        self.ports: dict[str, int] = {}
        self.hubs: list[dict[str, Any]] = []
        self.ingress_path = ingress_path
        self.disk_info = {"total_gb": 500.0, "free_gb": 200.0}
        self.existing: set[str] = set()

    def users_group(self) -> list[str]:
        return sorted(self.group)

    def plists(self) -> set[str]:
        return set(self.plist_set)

    def plist_ports(self, user: str) -> dict[str, int | None]:
        return {"web": self.ports.get(user), "mcp": None}

    def status(self) -> dict[str, Any]:
        return {"hubs": list(self.hubs), "user_dirs": [h["user"] for h in self.hubs]}

    def disk(self) -> dict[str, float]:
        return dict(self.disk_info)

    def os_user_exists(self, user: str) -> bool:
        return user in self.existing

    def ingress(self) -> Ingress:
        return Ingress.parse(self.ingress_path.read_text(encoding="utf-8"))


class FakeCF:
    def __init__(self, zones: dict[str, str] | None = None) -> None:
        self.zones_map = zones or {"example.com": "zid1", "cchc.vn": "zid2"}
        self.records: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.connections = [{"id": "c1"}]
        self.n = 0

    def zone_id(self, name: str) -> str | None:
        return self.zones_map.get(name)

    def zones(self) -> list[dict[str, Any]]:
        return [{"id": v, "name": k} for k, v in self.zones_map.items()]

    def dns_find(self, zone_id: str, fqdn: str) -> list[dict[str, Any]]:
        return [r for r in self.records.get(zone_id, []) if r["name"] == fqdn]

    def cname_upsert(self, zone_id: str, fqdn: str, target: str) -> dict[str, Any]:
        self.calls.append(("cname", (zone_id, fqdn, target)))
        for r in self.records.setdefault(zone_id, []):
            if r["name"] == fqdn:
                r["content"] = target
                return r
        self.n += 1
        r = {"id": f"rec{self.n}", "type": "CNAME", "name": fqdn, "content": target, "proxied": True}
        self.records[zone_id].append(r)
        return r

    def dns_delete(self, zone_id: str, record_id: str) -> None:
        self.calls.append(("dns_delete", (zone_id, record_id)))
        self.records[zone_id] = [r for r in self.records.get(zone_id, []) if r["id"] != record_id]

    def tunnel_connections(self, account_id: str, tunnel_id: str) -> list[dict[str, Any]]:
        return self.connections

    def access_app_delete(self, account_id: str, app_id: str) -> None:
        self.calls.append(("access_delete", app_id))


INGRESS = """tunnel: tun-123
credentials-file: /etc/cfharness/tun-123.json
ingress:
  - hostname: lamlinh.example.com
    service: http://127.0.0.1:8721
  - service: http_status:404
"""


def install_fakes(env: Any, tmp_path: Path) -> tuple[FakeMachine, FakeCF, FakeRunner]:
    ing = tmp_path / "tunnel.yml"
    ing.write_text(INGRESS, encoding="utf-8")
    m = FakeMachine(env.state, ing)
    cf = FakeCF()
    r = FakeRunner()
    env.state.extra["machine"] = m
    env.state.cloudflare = cf
    env.state.runner = r
    env.state.extra["http_probe"] = lambda host: {"http": 200, "cert_days": 80}
    env.state.db.set_setting("cf.ingress_path", str(ing))
    env.state.db.set_setting("cf.tunnel_id", "tun-123")
    env.state.db.set_setting("cf.account_id", "acc1")
    return m, cf, r


__all__ = ["FakeMachine", "FakeCF", "FakeRunner", "Result", "hub_status", "install_fakes", "INGRESS"]
