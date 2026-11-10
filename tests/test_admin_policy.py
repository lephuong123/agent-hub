"""Chính sách root-owned (admin_policy.py — plan-admin-users P2): đọc an toàn, gộp chặt hơn, fail-open/fail-closed, áp vào RunManager.

`TRUSTED_UIDS` đổi thành uid của user chạy test (không cần file root thật).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cfharness import admin_control as ac
from cfharness import admin_policy as ap
from cfharness.errors import Blocked

ME = ac.os_user()


@pytest.fixture
def pol(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "shared"
    (root / "policy").mkdir(parents=True)
    (root / "control").mkdir()
    monkeypatch.setenv("CFHARNESS_TEST_SHARED_ROOT", str(root))
    monkeypatch.setattr(ap, "TRUSTED_UIDS", (os.getuid(),))
    return root / "policy"


def _w(d: Path, name: str, obj, mode: int = 0o644) -> Path:
    p = d / name
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    os.chmod(p, mode)
    return p


def test_read_rejects_bad_owner_mode_symlink_json(pol, monkeypatch):
    _w(pol, "a.json", {"rev": 1, "budget": {"usd_per_day": 5}, "note": "IGNORE ALL", "models": {"allow": ["haiku", "x; rm -rf /"]}})
    data, err = ap.read_policy_file(pol / "a.json")
    assert err is None and data == {"rev": 1, "budget": {"usd_per_day": 5.0}, "models": {"allow": ["haiku"]}}
    _w(pol, "b.json", {"rev": 1}, mode=0o664)
    assert ap.read_policy_file(pol / "b.json") == (None, "writable_by_others")
    _w(pol, "c.json", "{bad")
    assert ap.read_policy_file(pol / "c.json")[1] == "bad_json"
    os.symlink(pol / "a.json", pol / "d.json")
    assert ap.read_policy_file(pol / "d.json")[1] == "symlink"
    assert ap.read_policy_file(pol / "zzz.json") == (None, "missing")
    monkeypatch.setattr(ap, "TRUSTED_UIDS", (0,))  # file do user (không phải root) sở hữu → bỏ
    assert ap.read_policy_file(pol / "a.json") == (None, "bad_owner")


def test_effective_takes_stricter_and_fail_open(pol):
    _w(pol, "_machine.json", {"rev": 3, "budget": {"usd_per_day": 50, "usd_per_run_max": 2}, "runs": {"max_concurrent": 4},
                              "require": {"sandbox": True}, "models": {"allow": ["haiku", "sonnet"]}, "min_version": "0.1.0"})
    _w(pol, "_default.json", {"rev": 7, "budget": {"usd_per_day": 10}, "runs": {"max_concurrent": 8}, "require": {"export": True},
                              "models": {"allow": ["sonnet", "opus"]}, "min_version": "0.2.0"})
    e = ap.effective(ME)
    assert e["valid"] and e["sources"] == ["machine", "default"] and e["rev"] == 7 and e["machine_rev"] == 3
    p = e["policy"]
    assert p["budget"] == {"usd_per_day": 10.0, "usd_per_run_max": 2.0} and p["runs"] == {"max_concurrent": 4}
    assert p["require"] == {"sandbox": True, "export": True} and p["models"] == {"allow": ["sonnet"]} and p["min_version"] == "0.2.0"
    # file riêng của user thay _default
    _w(pol, f"{ME}.json", {"rev": 9, "budget": {"usd_per_day": 100}})
    e = ap.effective(ME)
    assert e["rev"] == 9 and e["policy"]["budget"]["usd_per_day"] == 50.0 and "export" not in e["policy"].get("require", {})
    # file của user hỏng → fail-OPEN (bỏ file, cảnh báo, không chặn) khi máy không bắt buộc
    _w(pol, f"{ME}.json", {"rev": 9}, mode=0o666)
    e = ap.effective(ME)
    assert e["valid"] is False and e["errors"] == ["user:writable_by_others"] and e["blocked"] is False
    assert e["policy"]["budget"]["usd_per_day"] == 50.0  # vẫn áp _machine


def test_policy_required_fails_closed(pol):
    _w(pol, "_machine.json", {"rev": 1, "policy_required": True})
    e = ap.effective(ME)
    assert e["required"] and e["blocked"]  # không có file user/mặc định
    _w(pol, "_default.json", {"rev": 2})
    assert ap.effective(ME)["blocked"] is False
    _w(pol, "_default.json", "{broken")
    assert ap.effective(ME)["blocked"] is True
    # _machine.json hỏng → không biết có bắt buộc không → fail-open
    _w(pol, "_machine.json", "{broken")
    assert ap.effective(ME)["blocked"] is False


async def test_runmanager_applies_policy(hub_home, pol):
    from m7_fake import close, make_daemon

    d = await make_daemon(hub_home)
    try:
        hub = d.hub
        hub.agents.create({"id": "w1", "name": "w1", "model": "haiku"})
        hub.config.set("hub.max_concurrent_runs", 6)
        _w(pol, "_default.json", {"rev": 1, "budget": {"usd_per_day": 3, "usd_per_run_max": 0.25}, "runs": {"max_concurrent": 2}})
        hub.admin_policy.invalidate()
        assert hub.runs.effective_limit() == 2
        scopes = hub.runs.budget_scopes("w1", None)
        assert ("admin", "admin") in [(s[0], s[1]) for s in scopes]
        r = hub.runs.submit("w1", "x", budget_usd=5)
        assert r["max_budget_usd"] == 0.25  # trần mỗi run: lấy giá trị chặt hơn
        # hết ngân sách ngày của chính sách → chặn
        hub.store.conn.execute("UPDATE runs SET status='completed', cost_usd=3.5, ended_at=? WHERE id=?",
                               (__import__("cfharness.store", fromlist=["now_iso"]).now_iso(), r["id"]))
        with pytest.raises(Blocked) as ei:
            hub.runs.submit("w1", "y")
        assert "chính sách quản trị máy" in str(ei.value)
        # require.sandbox / min_version / models.allow
        _w(pol, "_default.json", {"rev": 2, "require": {"sandbox": True}})
        hub.admin_policy.invalidate()
        hub.config.set("guard.sandbox.enabled", False)
        with pytest.raises(Blocked) as ei:
            hub.runs.submit("w1", "z")
        assert ei.value.data["reason"] == "admin_require_sandbox"
        hub.config.set("guard.sandbox.enabled", True)
        _w(pol, "_default.json", {"rev": 3, "min_version": "999.0.0"})
        hub.admin_policy.invalidate()
        with pytest.raises(Blocked) as ei:
            hub.runs.submit("w1", "z")
        assert ei.value.data["reason"] == "admin_min_version"
        _w(pol, "_default.json", {"rev": 4, "models": {"allow": ["sonnet"]}})
        hub.admin_policy.invalidate()
        with pytest.raises(Blocked) as ei:
            hub.runs.submit("w1", "z")
        assert ei.value.data["reason"] == "admin_model_not_allowed"
        assert hub.runs.submit("w1", "z", model="sonnet-4-5")["status"] in ("queued", "running")
        # fail-closed khi máy bắt buộc chính sách và phần của user hỏng
        _w(pol, "_machine.json", {"rev": 1, "policy_required": True})
        _w(pol, "_default.json", "{broken")
        hub.admin_policy.invalidate()
        with pytest.raises(Blocked) as ei:
            hub.runs.submit("w1", "z", model="sonnet")
        assert ei.value.data["reason"] == "admin_policy_invalid"
    finally:
        await close(d)


def test_require_export_forces_exporter(hub_home, pol, tmp_path):
    from cfharness.hub import Hub

    hub = Hub(hub_home)
    try:
        status = tmp_path / "status"
        status.mkdir()
        hub.config.set("admin_export.dir", str(status))
        assert hub.admin_export.enabled is False
        _w(pol, "_default.json", {"rev": 5, "require": {"export": True}})
        hub.admin_policy.invalidate()
        assert hub.admin_export.enabled is True
        p = hub.admin_export.export_once()
        data = json.loads(p.read_text())
        assert data["policy"]["rev_applied"] == 5 and data["policy"]["valid"] is True
    finally:
        hub.store.close()
