"""Thu thập số liệu hub (SA1-3): mỗi 60 s đọc `status/` (bộ đọc gia cố của `admin_overview`), cập nhật `hubs`, mẫu lịch sử (`samples`,
5 phút), cảnh báo theo hub (`admin_watch.hub_codes` — dùng lại logic có sẵn) và hồ sơ SPEC v3 (`profile.json`) khi hub đã xuất.

Dữ liệu từ hub là KHÔNG TIN CẬY: chỉ giữ trường qua SPEC; hiển thị luôn escape; không bao giờ đưa vào LLM.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from .. import __version__
from .. import admin_watch as AW
from .alerts import sync_alerts
from .db import dumps

if TYPE_CHECKING:
    from .dispatch import State

log = logging.getLogger("cfharness.admind.collector")

SAMPLE_EVERY_S = 300
HUB_CODES = set(AW.ALERT_CODES) - {"admin_estop_on"}
LEVEL = {"tamper": "crit", "owner_mismatch": "crit", "budget_100": "crit", "revoked_running": "crit", "stale": "warn", "stopped": "warn"}


def _best(hubs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Một user có thể có nhiều file (nhiều HUB_HOME); chọn file còn sống/mới nhất."""
    order = {"ok": 0, "stopped": 1, "stale": 2, "malformed": 3, "owner_mismatch": 4}
    return sorted(hubs, key=lambda h: (order.get(h.get("state"), 9), h.get("age_s") or 1e12))[0] if hubs else None


def collect(state: "State") -> dict[str, Any]:
    from .machine import machine
    from .ops_system import setting

    m = machine(state)
    ov = m.status()
    by: dict[str, list[dict[str, Any]]] = {}
    for h in ov.get("hubs") or []:
        by.setdefault(str(h.get("user")), []).append(h)
    now = state.clock()
    do_sample = now - float(state.db.get_setting("collector.last_sample", 0) or 0) >= SAMPLE_EVERY_S
    n = 0
    for u in state.db.all("SELECT id, os_user, state FROM hub_users WHERE host_id=? AND state NOT IN ('deleted','creating','create_failed')",
                          (state.db.host_id,)):
        h = _best(by.get(u["os_user"], []))
        d = (h or {}).get("data") or {}
        status = h["state"] if h else "missing"
        state.db.exec("INSERT INTO hubs(id, host_id, user_id) VALUES (?,?,?) ON CONFLICT(host_id, user_id) DO NOTHING",
                      (f"hub_{u['id'][-16:]}", state.db.host_id, u["id"]))
        state.db.exec("UPDATE hubs SET status=?, hub_id=COALESCE(?, hub_id), version=COALESCE(?, version), snapshot=?, last_seen=CASE WHEN ? "
                      "THEN ? ELSE last_seen END WHERE host_id=? AND user_id=?",
                      (status, (h or {}).get("hub_id"), d.get("version"), dumps(d) if d else "{}", 1 if d else 0, now, state.db.host_id, u["id"]))
        if u["state"] in ("active", "suspended"):
            exp = state.db.get_setting(f"policy.user_rev.{u['os_user']}")
            codes = AW.hub_codes(h, my_version=__version__, approval_wait_s=1800, budget_warn_pct=80, expected_rev=exp) if h else {"missing"}
            if u["state"] == "suspended":
                codes -= {"stopped", "stale"}
        elif u["state"] == "revoked":
            codes = {"revoked_running"} if h and h.get("state") == "ok" else set()
        else:
            codes = set()
        sync_alerts(state, "user", u["id"], codes, managed=HUB_CODES, level_of=LEVEL)
        if do_sample and d:
            c, r = d.get("cost_usd") or {}, d.get("runs") or {}
            state.db.exec("INSERT INTO samples(ts, host_id, user_id, cost_today, cost_month, running, queued, failed_today, approvals, budget_pct)"
                          " VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (now, state.db.host_id, u["id"], c.get("today"), c.get("month"), r.get("running"), r.get("queued"),
                           r.get("today_failed"), (d.get("approvals") or {}).get("open"), (d.get("budget") or {}).get("day_pct")))
        _profile(state, u, now)
        n += 1
    if do_sample:
        state.db.set_setting("collector.last_sample", now)
        keep = int(setting(state.db, "history.keep_days")) * 86400
        state.db.exec("DELETE FROM samples WHERE ts<?", (now - keep,))
        state.db.exec("DELETE FROM profile_snapshots WHERE ts<?", (now - 30 * 86400,))
    disk = m.disk()
    if disk.get("free_gb") is not None and disk.get("total_gb"):
        low = disk["free_gb"] < max(10.0, disk["total_gb"] * 0.05)
        sync_alerts(state, "system", "disk", {"disk_low"} if low else set(), managed={"disk_low"})
    return {"users": n, "files": len(ov.get("hubs") or [])}


def _profile(state: "State", u: dict[str, Any], now: float) -> None:
    """`status/<u>/profile.json` (SPEC v3, hub ghi — SA3-1). Đọc bằng bộ đọc gia cố, lọc `PROFILE_SPEC` lần nữa."""
    try:
        from ..admin_profile import read_profile
    except ImportError:
        return
    p = read_profile(u["os_user"])
    if not p:
        return
    blob = dumps(p)
    state.db.exec("UPDATE hubs SET profile=? WHERE host_id=? AND user_id=?", (blob, state.db.host_id, u["id"]))
    last = state.db.one("SELECT ts, data FROM profile_snapshots WHERE user_id=? ORDER BY ts DESC LIMIT 1", (u["id"],))
    if not last or (now - float(last["ts"]) >= 3600 and last["data"] != blob):
        state.db.exec("INSERT OR REPLACE INTO profile_snapshots(user_id, ts, data) VALUES (?,?,?)", (u["id"], now, blob))


def snapshot(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    try:
        return json.loads(row.get("snapshot") or "{}")
    except ValueError:
        return {}
