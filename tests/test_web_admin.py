"""Web quản trị máy (web/admin_pages.py — plan-admin-users): /admin/users[/<u>], /admin/alerts, /admin/audit, /machine, ESTOP admin.

Client thuần qua daemon in-process; thư mục chung = thư mục tạm; không script/style inline (CSP 'self').
"""

from __future__ import annotations

import json
import time

import pytest

from cfharness import admin_control as ac
from cfharness import admin_overview as ao
from cfharness.runtime import FakeRuntime

ME = ao.os_user()
HID = "0123456789abcdef"


@pytest.fixture
def shared(tmp_path, monkeypatch):
    root = tmp_path / "shared"
    for d in ("control", "admin-log", "policy", "status"):
        (root / d).mkdir(parents=True)
    monkeypatch.setenv("CFHARNESS_TEST_SHARED_ROOT", str(root))
    return root


def _status(root, **over):
    d = {"schema": 2, "user": ME, "hub_id": HID, "version": "0.2.8", "generated_at": "2026-11-10T08:00:00Z", "generated_ts": time.time(),
         "interval_s": 60, "daemon": {"up": True}, "cost_usd": {"today": 1.25, "week": 3.0, "month": 9.5}, "runs": {"running": 1},
         "guard": {"sandbox": True, "tamper_today": 0}, "claude": {"cli_version": "2.1.330", "auth_ok": True}, "note": "<script>alert(1)</script>"}
    d.update(over)
    (root / "status" / ME).mkdir(exist_ok=True)
    (root / "status" / ME / f"{HID}.json").write_text(json.dumps(d))


async def test_admin_users_pages_and_estop_action(hub_home, shared, as_admin):
    from test_web import ISOLATED, _start, _stop

    as_admin(users=[ME])
    env = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        env.hub.config.set("admin_export.dir", str(shared / "status"))
        _status(shared)
        w = env.hub.admin_watch
        w._sample(ao.overview(env.hub), force=True)
        await env.login()
        html = (await env.client.get("/costs")).text
        assert 'href="/admin/users"' in html and 'href="/machine"' in html
        # khu Cài đặt: nhóm "Quản trị máy" (cột nav + trang danh mục) chỉ hiện với quản trị
        idx = (await env.client.get("/settings")).text
        assert ">Quản trị máy</div>" in idx and 'href="/admin/overview"' in idx and 'href="/admin/users"' in idx
        page = (await env.client.get("/admin/users")).text
        assert f"/admin/users/{ME}" in page and "<script>alert" not in page and "Người dùng máy" in page
        up = (await env.client.get(f"/admin/users/{ME}")).text
        assert "<svg" in up and 'class="chart-bar"' in up and "Chi phí theo ngày" in up and "<script>alert" not in up
        assert " style=" not in up and "<script>" not in up.split("</main>")[0]  # không style/script inline trong nội dung trang
        assert (await env.client.get("/admin/users/..%2Fetc")).status_code == 404
        assert (await env.client.get("/admin/alerts")).status_code == 200
        assert (await env.client.get("/admin/audit")).status_code == 200
        # ESTOP admin: luôn qua trang xác nhận trước khi gọi RPC ghi
        r = await env.post("/do/admin_estop_on", {"user": ME, "reason_code": "maintenance", "next": "/admin/users"})
        assert r.status_code == 200 and "BẬT ESTOP quản trị máy" in r.text and not (shared / "control" / f"estop.{ME}").exists()
        r = await env.post("/do/admin_estop_on", {"user": ME, "reason_code": "maintenance", "next": "/admin/users", "confirm": "yes"})
        assert r.status_code == 303 and (shared / "control" / f"estop.{ME}").exists()
        assert env.hub.audit.query(kind="admin.estop_set")
        r = await env.post("/do/admin_estop_on", {"user": "*", "reason_code": "<b>x</b>", "confirm": "yes"})
        assert not (shared / "control" / "estop").exists()  # mã lý do lạ bị từ chối
        env.hub.admin_control.check()
        html = (await env.client.get("/costs")).text
        assert "Hub đang tạm dừng" in html  # banner trung tính phía user (VIS-04; admin cũng là một hub user)
        r = await env.post("/do/admin_estop_off", {"user": ME, "confirm": "yes", "next": "/admin/users"})
        assert r.status_code == 303 and not (shared / "control" / f"estop.{ME}").exists()
    finally:
        await _stop(env)


async def test_machine_page_user_side(hub_home, shared):
    from test_web import ISOLATED, _start, _stop

    env = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        ac.set_flag(True, ME, reason_code="security", by="boss")
        env.hub.admin_control.check()
        await env.login()
        html = (await env.client.get("/costs")).text
        # VIS-02/04: user thường — không menu quản trị, không trang /machine (minh bạch tắt mặc định), banner trung tính
        assert 'href="/machine"' not in html and 'href="/admin/users"' not in html
        shown = html.replace('href="/settings/security"', "")  # mục "Bảo mật & thiết bị" của cột Cài đặt — không phải mã lý do
        assert "Hub đang tạm dừng" in html and "security" not in shown and "boss" not in html
        nf = await env.client.get("/khong-co-trang-nay")
        for path in ("/admin/users", "/admin/overview", "/admin/alerts", "/admin/audit", f"/admin/users/{ME}", "/machine"):
            r = await env.client.get(path)
            assert r.status_code == 404 and r.text == nf.text, path  # giống hệt route không tồn tại
        settings = (await env.client.get("/settings")).text
        assert "Hub đang tạm dừng" in settings and "Tắt ESTOP" not in settings and "/machine" not in settings
        assert ">Quản trị máy</div>" not in settings and 'href="/admin/overview"' not in settings  # user thường: không nhóm Quản trị máy
        safety = (await env.client.get("/settings/safety")).text
        assert "Hub đang tạm dừng" in safety and "Tắt ESTOP" not in safety and "/machine" not in safety
        # VIS-07: bật trang minh bạch → user thấy "Quản trị máy" và dữ liệu về mình
        (shared / "control" / "visibility.json").write_text(json.dumps({"schema": 1, "activity": False, "transparency": True}))
        env.app.state.web._admin_nav = None
        page = (await env.client.get("/machine")).text
        assert "ĐANG BẬT" in page and "security" in page and "estop_on" in page
        assert "&#34;schema&#34;: 2" in page or '"schema": 2' in page  # đúng JSON sẽ xuất
    finally:
        await _stop(env)


def test_bars_pure():
    from cfharness.web.admin_pages import bars

    c = bars([{"label": "2026-11-01", "value": 0}, {"label": "2026-11-02", "value": 4}, {"label": "2026-11-03", "value": 2}])
    assert c["top"] == 4 and c["bars"][1]["h"] == c["height"] - c["pad"] and c["bars"][0]["h"] == 0
    assert bars([])["bars"] == []
