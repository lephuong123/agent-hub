"""Trang "Hệ thống ngoài" (web/systems_pages.py, đề xuất #9): xem hệ thống + job, quét/đăng ký, thêm, gỡ, khoá, tạm dừng/bật lại/khởi động
lại job — qua `ACTIONS` (CSRF + khoá phiên, xác nhận, audit kênh web). launchctl GIẢ (tests/test_systems.py), không gọi `claude`.
"""

from __future__ import annotations

import json

import pytest

from cfharness.runtime import FakeRuntime
from cfharness.web.systems_pages import job_confirm, summary
from test_systems import attach_world, make_world
from test_web import ISOLATED, _start, _stop, audit_rows  # noqa: F401


@pytest.fixture
async def env(hub_home, tmp_path):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    e.world = make_world(tmp_path)  # type: ignore[attr-defined]
    attach_world(e.hub, e.world)  # type: ignore[attr-defined]
    yield e
    await _stop(e)


def _job(hub, label: str) -> dict:
    return next(j for s in hub.systems.list() for j in s["jobs"] if j["label"] == label)


# =========================================================================== thuần
def test_pure_helpers():
    rows = [{"jobs": [{"status": "running"}, {"status": "paused", "locked": True}]}, {"jobs": [{"status": "error"}]}, {"jobs": []}]
    assert summary(rows) == {"systems": 3, "jobs": 3, "running": 1, "paused": 1, "error": 1, "locked": 1}
    assert "không tự bật lại" in job_confirm("pause", "x") and "chạy lại ngay" in job_confirm("restart", "x")


# =========================================================================== xác thực / menu
async def test_requires_login_csrf_key_and_menu(env):
    c = env.client
    assert (await c.get("/systems")).status_code == 303
    assert (await c.post("/do/system_scan_apply", data={"confirm": "yes"})).status_code == 401
    await env.login()
    r = await c.post("/do/system_scan_apply", data={"confirm": "yes", "csrf": env.csrf})  # thiếu khoá phiên
    assert r.status_code == 403 and env.hub.systems.registry() == {}
    r = await c.get("/systems")
    assert r.status_code == 200 and "Chưa đăng ký hệ thống nào" in r.text
    assert 'href="/systems"' in r.text and 'aria-current="page"' in r.text and "Hệ thống ngoài" in r.text


# =========================================================================== quét + đăng ký
async def test_scan_preview_then_register_with_confirm(env):
    await env.login()
    hub = env.hub
    r = await env.client.get("/systems?scan=1")
    assert r.status_code == 200 and "viec-nhom-2" in r.text and "bot-khac" in r.text and "/do/system_scan_apply" in r.text
    assert hub.systems.registry() == {}  # quét thử không đăng ký
    r = await env.post("/do/system_scan_apply", {"next": "/systems"})
    assert r.status_code == 200 and "Đăng ký MỌI hệ thống mới" in r.text and hub.systems.registry() == {}
    r = await env.post("/do/system_scan_apply", {"next": "/systems", "confirm": "yes"})
    assert r.status_code == 303 and r.headers["location"] == "/systems"
    assert sorted(hub.systems.registry()) == ["bot-khac", "viec-nhom-2"]
    au = [a for a in audit_rows(env, "system.scan") if json.loads(a["data"]).get("params", {}).get("apply") or "apply" in a["data"]]
    assert au and au[-1]["channel"] == "web" and au[-1]["actor"] == "user"
    r = await env.client.get("/systems")
    t = r.text
    assert "Việc nhóm 2" in t and "com.vn2.bantin" in t and "com.vn2.web" in t and "đang chạy" in t
    assert "com.hub.self" not in t  # job của chính hub không bao giờ hiện
    assert "/do/system_job" in t and "/do/system_lock" in t


async def test_add_by_path_and_remove(env):
    await env.login()
    hub = env.hub
    path = str(env.world["a"])  # type: ignore[attr-defined]
    r = await env.post("/do/system_add", {"path": path, "next": "/systems"})
    assert r.status_code == 200 and "Đăng ký thư mục" in r.text and hub.systems.registry() == {}
    r = await env.post("/do/system_add", {"path": path, "next": "/systems", "confirm": "yes"})
    assert r.status_code == 303 and r.headers["location"] == "/systems/viec-nhom-2"
    assert list(hub.systems.registry()) == ["viec-nhom-2"]
    assert audit_rows(env, "system.add")[-1]["channel"] == "web"
    # đường dẫn của chính hub → từ chối, báo lỗi (flash), không đăng ký
    r = await env.post("/do/system_add", {"path": str(hub.paths.home), "next": "/systems", "confirm": "yes"})
    assert r.status_code == 303 and list(hub.systems.registry()) == ["viec-nhom-2"]
    # gỡ: xác nhận (nguy hiểm) → gỡ khỏi registry, về /systems
    r = await env.post("/do/system_rm", {"id": "viec-nhom-2", "next": "/systems/viec-nhom-2"})
    assert r.status_code == 200 and "Gỡ hệ thống viec-nhom-2" in r.text and hub.systems.registry()
    r = await env.post("/do/system_rm", {"id": "viec-nhom-2", "next": "/systems/viec-nhom-2", "confirm": "yes"})
    assert r.status_code == 303 and r.headers["location"] == "/systems" and hub.systems.registry() == {}
    assert audit_rows(env, "system.rm")[-1]["channel"] == "web"


# =========================================================================== thao tác job
async def test_job_pause_resume_restart_need_confirm_and_audit(env):
    await env.login()
    hub, fake = env.hub, env.world["fake"]  # type: ignore[attr-defined]
    hub.systems.scan(apply=True)
    form = {"label": "com.vn2.bantin", "action": "pause", "next": "/systems"}
    r = await env.post("/do/system_job", form)
    assert r.status_code == 200 and "Tạm dừng job com.vn2.bantin" in r.text
    assert not any(c[0] == "bootout" for c in fake.calls)  # chưa xác nhận → không đụng launchctl
    r = await env.post("/do/system_job", {**form, "confirm": "yes"})
    assert r.status_code == 303 and _job(hub, "com.vn2.bantin")["status"] == "paused"
    au = audit_rows(env, "system.job")[-1]
    assert au["channel"] == "web" and au["actor"] == "user"
    assert _job(hub, "com.vn2.bantin")["paused_by"]["reason"] == "người tạm dừng từ web"
    r = await env.client.get("/systems")
    assert "TẠM DỪNG" in r.text and "Bật lại" in r.text
    r = await env.post("/do/system_job", {**form, "action": "resume", "confirm": "yes"})
    assert r.status_code == 303 and _job(hub, "com.vn2.bantin")["status"] == "running"
    r = await env.post("/do/system_job", {"label": "com.vn2.web", "action": "restart", "next": "/systems", "confirm": "yes"})
    assert r.status_code == 303 and fake.loaded["com.vn2.web"] == 5151
    # thao tác lạ / job không đăng ký → không gọi gì
    n = len(fake.calls)
    r = await env.post("/do/system_job", {**form, "action": "unload", "confirm": "yes"})
    assert r.status_code == 303
    r = await env.post("/do/system_job", {"label": "com.ngoai.root", "action": "pause", "next": "/systems", "confirm": "yes"})
    assert r.status_code == 303 and "com.ngoai.root" not in {c[-1].rsplit("/", 1)[-1] for c in fake.calls[n:] if c[0] == "bootout"}


async def test_lock_unlock_and_detail_page_with_log(env):
    await env.login()
    hub = env.hub
    hub.systems.scan(apply=True)
    r = await env.post("/do/system_lock", {"label": "com.vn2.bantin", "on": "1", "next": "/systems", "confirm": "yes"})
    assert r.status_code == 303 and _job(hub, "com.vn2.bantin")["locked"]
    assert audit_rows(env, "system.lock")[-1]["channel"] == "web"
    r = await env.client.get("/systems/viec-nhom-2")
    t = r.text
    assert r.status_code == 200 and "khoá" in t and "Mở khoá" in t and "Gửi bản tin sáng OK" in t and "/do/system_rm" in t
    r = await env.post("/do/system_lock", {"label": "com.vn2.bantin", "on": "0", "next": "/systems", "confirm": "yes"})
    assert r.status_code == 303 and not _job(hub, "com.vn2.bantin")["locked"]
    assert (await env.client.get("/systems/khong-co")).status_code == 404


async def test_free_text_is_escaped(env):
    """Tiêu đề (README) và log của app là dữ liệu ngoài — autoescape, không bao giờ thành HTML."""
    await env.login()
    a = env.world["a"]  # type: ignore[attr-defined]
    (a / "README.md").write_text("# <script>alert(1)</script>\n", encoding="utf-8")
    log = a.parent.parent / "bantin.log"
    log.write_text("<img src=x onerror=alert(2)>\n", encoding="utf-8")
    env.hub.systems.scan(apply=True)
    for url in ("/systems", "/systems/viec-nhom-2"):
        t = (await env.client.get(url)).text
        assert "<script>alert(1)" not in t and "&lt;script&gt;alert(1)" in t
    t = (await env.client.get("/systems/viec-nhom-2")).text
    assert "<img src=x" not in t and "&lt;img src=x" in t
