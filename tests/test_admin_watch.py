"""Cảnh báo + lịch sử của hub admin (admin_watch.py — plan-admin-users P1/P2) và định dạng thông báo."""

from __future__ import annotations

import time

import pytest

from cfharness import __version__
from cfharness import admin_control as ac
from cfharness.admin_watch import ALERT_CODES, hub_codes
from cfharness.events import EVENT_LEVELS, make_event


def _h(state="ok", **data):
    return {"user": "an", "hub_id": "0123456789abcdef", "state": state, "data": data or {"version": __version__}}


def test_hub_codes_pure():
    kw = {"my_version": "0.3.0", "approval_wait_s": 1800, "budget_warn_pct": 80}
    assert hub_codes(_h(version="0.3.0"), **kw) == set()
    assert hub_codes(_h("stale", version="0.3.0"), **kw) == {"stale"}
    assert hub_codes({"user": "an", "state": "owner_mismatch", "data": None}, **kw) == {"owner_mismatch"}
    assert hub_codes(_h(version="0.2.9"), **kw) == {"version_behind"}
    assert hub_codes(_h(version="0.3.0", budget={"day_pct": 85}), **kw) == {"budget_80"}
    assert hub_codes(_h(version="0.3.0", budget={"day_pct": 100}), **kw) == {"budget_100"}
    assert hub_codes(_h(version="0.3.0", guard={"tamper_today": 1}), **kw) == {"tamper"}
    assert hub_codes(_h(version="0.3.0", flags={"auth_block": True}), **kw) == {"auth_block"}
    assert hub_codes(_h(version="0.3.0", approvals={"oldest_wait_s": 4000}), **kw) == {"approval_waiting_long"}
    assert hub_codes(_h(version="0.3.0", policy={"rev_applied": 1, "valid": True}), expected_rev=2, **kw) == {"policy_mismatch"}
    assert hub_codes(_h(version="0.3.0", policy={"valid": False}), **kw) == {"policy_mismatch"}
    assert set(ALERT_CODES) >= {"stale", "budget_80", "tamper", "admin_estop_on", "missing"}
    assert "admin.alert" in EVENT_LEVELS and "hub.admin_estop" in EVENT_LEVELS


async def test_evaluate_dedupes_and_resolves(hub_home, tmp_path, monkeypatch, as_admin):
    from m7_fake import close, make_daemon

    root = tmp_path / "shared"
    (root / "control").mkdir(parents=True)
    (root / "admin-log").mkdir()
    monkeypatch.setenv("CFHARNESS_TEST_SHARED_ROOT", str(root))
    d = await make_daemon(hub_home)
    try:
        w = d.hub.admin_watch
        seen = []
        d.hub.bus.add_listener(lambda ev: seen.append(dict(ev.payload)) if ev.type == "admin.alert" else None)
        ov = {"exists": True, "hubs": [_h("stale", version=__version__)]}
        users = [{"user": "an", "granted": True, "member": True, "hubs": 1, "state": "stale"},
                 {"user": "binh", "granted": True, "member": True, "hubs": 0, "state": "not_installed"}]
        r = w.evaluate(ov, users)
        assert {(a["user"], a["code"]) for a in r["new"]} == {("an", "stale"), ("binh", "missing")}
        assert all(p["state"] == "on" for p in seen) and len(seen) == 2
        w.evaluate(ov, users)  # lặp lại: không gửi trùng
        assert len(seen) == 2
        ac.set_flag(True, None, reason_code="maintenance", by="boss")  # ESTOP mọi user → cảnh báo admin_estop_on cho từng user
        ov2 = {"exists": True, "hubs": [_h("ok", version=__version__)]}
        r = w.evaluate(ov2, users)
        assert ("an", "stale") in {(a["user"], a["code"]) for a in r["resolved"]}
        assert {"user": "an", "hub_id": "0123456789abcdef", "code": "stale", "state": "resolved"} in seen
        assert {(a["user"], a["code"]) for a in r["new"]} == {("an", "admin_estop_on"), ("binh", "admin_estop_on")}
        ac.set_flag(False, None, by="boss")
        r = w.evaluate(ov2, users)
        assert {(a["user"], a["code"]) for a in r["resolved"]} == {("an", "admin_estop_on"), ("binh", "admin_estop_on")}
        # payload chỉ id/enum
        for p in seen:
            assert set(p) <= {"user", "hub_id", "code", "state"} and p["code"] in ALERT_CODES
        # sống qua restart: trạng thái ở hub_state
        assert set(d.hub.store.state_get("admin_watch.active")) == {"binh|-|missing"}
    finally:
        await close(d)


async def test_samples_history_and_prune(hub_home, as_admin):
    from m7_fake import close, make_daemon

    d = await make_daemon(hub_home)
    try:
        w = d.hub.admin_watch
        ov = {"exists": True, "hubs": [_h("ok", version=__version__, cost_usd={"today": 2.5, "month": 10}, runs={"running": 3})]}
        assert w._sample(ov, force=True) == 1
        assert w._sample(ov) == 0  # chưa tới chu kỳ
        h = w.history("an", days=7)
        assert h["samples"] == 1 and h["cost_by_day"][-1]["value"] == 2.5 and len(h["cost_by_day"]) == 7
        assert h["running_by_hour"][-1]["value"] == 3
        d.hub.store.conn.execute("UPDATE admin_samples SET ts_epoch = ?", (time.time() - 100 * 86400,))
        assert w.prune() == 1
        assert w.history("an")["samples"] == 0
    finally:
        await close(d)


def test_notify_format_admin_events():
    from cfharness.notify.slack import format_message
    from cfharness.notify.telegram import format_event

    ev = make_event("admin.alert", None, {"user": "an", "hub_id": "0123456789abcdef", "code": "budget_100", "state": "on"})
    t = format_message(ev)
    assert "an" in t and "hết ngân sách" in t
    assert "hồi phục" not in t
    t2 = format_message(make_event("admin.alert", None, {"user": "an", "code": "budget_100", "state": "resolved"}))
    assert "đã hết cảnh báo" in t2
    # mã lạ / user giả → không gửi chữ tự do
    assert format_message(make_event("admin.alert", None, {"user": "an", "code": "<script>"})) is None
    t3 = format_message(make_event("admin.alert", None, {"user": "x y <b>", "code": "stale"}))
    assert "<b>" not in t3
    assert "BẬT ESTOP" in format_message(make_event("hub.admin_estop", None, {"on": True, "reason_code": "IGNORE PREVIOUS"}))
    assert "IGNORE" not in format_message(make_event("hub.admin_estop", None, {"on": True, "reason_code": "IGNORE PREVIOUS"}))
    assert format_event(ev) is not None
    from cfharness.config import DEFAULTS

    for ch in ("slack", "telegram"):
        assert "admin.alert" in DEFAULTS["notify"][ch]["events"] and "hub.admin_estop" in DEFAULTS["notify"][ch]["events"]


def test_admin_watch_config_is_security_key_and_validated():
    from cfharness.config import validate_setting
    from cfharness.guard.integrity import SECURITY_KEYS

    assert "admin_watch" in SECURITY_KEYS
    for k, v in (("admin_watch.interval_s", 1), ("admin_watch.history_days", 1000), ("admin_watch.enabled", "yes"), ("admin_watch.foo", 1)):
        with pytest.raises(ValueError):
            validate_setting(k, v)
    validate_setting("admin_watch.interval_s", 120)
