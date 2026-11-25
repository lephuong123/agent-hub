"""Điều hướng chat-first (base.html, _nav.html, `/partials/manager/side`): `/` → /manager, trang Hoạt động ở /activity, thanh bên chính chỉ
còn luồng việc, trang cấu hình vào qua Cài đặt, nút Dừng khẩn cấp luôn hiện, menu người dùng, tabbar mobile."""

from __future__ import annotations

import re

from test_web import env  # noqa: F401  (fixture)


def _nav(html: str) -> str:
    m = re.search(r'<nav class="nav" aria-label="Điều hướng chính">(.*?)</nav>', html, re.S)
    assert m, "thiếu thanh bên chính"
    return m.group(1)


def _tabbar(html: str) -> str:
    m = re.search(r'<nav class="tabbar"(.*?)</nav>', html, re.S)
    assert m
    return m.group(1)


async def test_root_redirects_to_chat_and_activity_is_overview(env):  # noqa: F811
    await env.login()
    r = await env.client.get("/")
    assert r.status_code == 302 and r.headers["location"] == "/manager"
    a = await env.client.get("/activity")
    assert a.status_code == 200 and "<h1>Hoạt động</h1>" in a.text and "Tổng quan" not in _nav(a.text)
    # các route app desktop / đăng nhập không bị ảnh hưởng
    assert (await env.client.get("/.well-known/cfharness.json")).status_code == 200
    assert (await env.client.get("/login")).headers["location"] == "/manager"


async def test_unauthenticated_root_still_goes_to_login(env):  # noqa: F811
    r = await env.client.get("/")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


async def test_main_sidebar_is_workflow_only(env):  # noqa: F811
    await env.login()
    html = (await env.client.get("/runs")).text
    nav = _nav(html)
    labels = re.findall(r'<span class="lbl">([^<]+)</span>', nav)
    assert labels == ["Cần bạn", "Công việc", "Lượt chạy", "Đội ngũ", "Hoạt động"]
    assert 'href="/activity"' in nav and 'href="/agents"' in nav
    for gone in ("/mcp", "/skills", "/memory", "/learnings", "/teams", "/schedules", "/systems", "/costs", "/audit", "/logs", "/permissions"):
        assert f'href="{gone}"' not in nav, gone
    assert 'href="/manager"' in html.split('class="side-top"', 1)[1][:400]  # logo → chat
    # vùng HỘI THOẠI nạp lười trên mọi trang (không phải /manager)
    assert 'hx-get="/partials/manager/side"' in html


async def test_config_page_renders_in_settings_shell(env):  # noqa: F811
    await env.login()
    html = (await env.client.get("/mcp")).text
    assert '<div class="shell set">' in html and '<nav class="nav" aria-label="Điều hướng chính">' not in html  # thanh bên chính ẩn
    m = re.search(r'<nav class="snav" aria-label="Điều hướng Cài đặt">(.*?)</nav>', html, re.S)
    assert m
    assert re.search(r'<a href="/mcp" class="on"[^>]*aria-current="page"', m.group(1))
    assert 'id="set-back"' in html and 'href="/manager"' in html.split('id="set-back"', 1)[0][-80:]
    assert 'action="/do/estop"' in html.split('class="side-foot sset-foot"', 1)[1]  # Dừng khẩn cấp vẫn ở chân cột
    assert 'class="set-mback" href="/settings"' in html  # điện thoại: nút ← Cài đặt
    # trang làm việc: thanh bên chính, không cột Cài đặt
    tasks = (await env.client.get("/tasks")).text
    assert '<div class="shell">' in tasks and "snav" not in tasks
    # Đội ngũ (/agents) ở thanh bên chính, không vào khu Cài đặt
    assert '<div class="shell">' in (await env.client.get("/agents")).text


async def test_settings_index_is_catalog(env):  # noqa: F811
    await env.login()
    html = (await env.client.get("/settings")).text
    m = re.search(r'<div class="set-page" id="set-index">(.*?)</main>', html, re.S)
    assert m
    for href in ("/settings/safety", "/settings/accounts", "/settings/config", "/settings/notifications", "/settings/secrets",
                 "/settings/filehub", "/settings/security", "/settings/system", "/settings/backup", "/settings/support",
                 "/settings/desktop", "/teams", "/schedules", "/skills", "/memory", "/learnings", "/mcp", "/systems", "/costs",
                 "/audit", "/logs", "/permissions"):
        assert f'href="{href}"' in m.group(0), href
    assert 'href="/agents"' not in m.group(0)


async def test_estop_button_always_visible(env):  # noqa: F811
    await env.login()
    html = (await env.client.get("/tasks")).text
    foot = html.split('class="side-foot"', 1)[1]
    assert 'action="/do/estop"' in foot and 'name="on" value="1"' in foot and "data-confirm=" in foot
    assert "Dừng khẩn cấp" in foot
    # sheet "Thêm" trên điện thoại: ESTOP đứng đầu
    sheet = _tabbar(html).split('class="tab-sheet"', 1)[1]
    assert sheet.index('action="/do/estop"') < sheet.index('href="/agents"')
    # bật ESTOP → nút đỏ + Tắt
    r = await env.post("/do/estop", {"on": "1", "confirm": "yes", "next": "/tasks"})
    assert r.status_code == 303
    html = (await env.client.get("/tasks")).text
    foot = html.split('class="side-foot"', 1)[1]
    assert "ESTOP đang bật" in foot and 'name="on" value="0"' in foot
    await env.post("/do/estop", {"on": "0", "confirm": "yes", "next": "/tasks"})


async def test_user_menu_and_mobile_tabbar(env):  # noqa: F811
    await env.login()
    html = (await env.client.get("/inbox")).text
    menu = html.split('class="menu-pop side-menu-pop"', 1)[1].split("</details>", 1)[0]
    for item in ('href="/settings"', 'href="/costs"', 'href="/update"', 'href="/settings/desktop"', 'action="/logout"'):
        assert item in menu, item
    assert 'href="/machine"' not in menu  # không phải máy có quản trị
    tab = _tabbar(html)
    top = re.findall(r'<a href="(/[a-z]+)"', tab.split("<details", 1)[0])
    assert top == ["/manager", "/inbox", "/tasks", "/runs"]
    sheet_links = re.findall(r'<a href="([^"]+)"', tab.split('class="tab-sheet"', 1)[1])
    assert len(sheet_links) <= 9 and "/settings" in sheet_links and "/mcp" not in sheet_links


async def test_side_partial_lists_conversations(env):  # noqa: F811
    await env.login()
    r = await env.client.get("/partials/manager/side")
    assert r.status_code == 200
    assert "Hội thoại" in r.text and "Gần đây" in r.text
    assert 'id="upd-dot"' in r.text and 'hx-swap-oob="true"' in r.text


async def test_update_flag_never_waits_for_network(monkeypatch):
    """Thanh bên (nạp lười trên MỌI trang) không được chờ `update.check` (có thể gọi mạng nhiều giây khi daemon chưa cache):
    lần đầu trả False ngay, kiểm tra chạy nền; lần sau đọc giá trị đã cache."""
    import asyncio
    import time
    from types import SimpleNamespace

    from cfharness.web import manager as M

    st = SimpleNamespace()
    calls = []

    async def slow_rpc(request, method, params=None, **kw):
        calls.append(method)
        await asyncio.sleep(0.5)
        return {"update_available": True}

    monkeypatch.setattr(M, "rpc", slow_rpc)
    monkeypatch.setattr(M, "ws", lambda request: st)
    t0 = time.monotonic()
    assert await M.update_flag(None) is False
    assert await M.update_flag(None) is False  # chỉ một lượt kiểm tra chạy nền cùng lúc
    assert time.monotonic() - t0 < 0.2
    await st._upd_task
    assert calls == ["update.check"]
    assert await M.update_flag(None) is True
