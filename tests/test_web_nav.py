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


async def test_config_page_shows_temporary_settings_item(env):  # noqa: F811
    await env.login()
    html = (await env.client.get("/mcp")).text
    nav = _nav(html)
    assert 'nav-cfg-head"><a href="/settings">Cài đặt</a>' in nav
    assert re.search(r'<a href="/mcp" class="on nav-cfg"[^>]*aria-current="page"', nav)
    assert re.search(r'<a class="item on" href="/settings">', html)  # menu người dùng đánh dấu Cài đặt
    # trang làm việc: không có mục tạm
    assert "nav-cfg" not in _nav((await env.client.get("/tasks")).text)


async def test_settings_has_manage_grid(env):  # noqa: F811
    await env.login()
    html = (await env.client.get("/settings")).text
    m = re.search(r'<section class="card" id="manage".*?</section>', html, re.S)
    assert m
    for href in ("/teams", "/schedules", "/skills", "/memory", "/learnings", "/mcp", "/systems", "/costs", "/audit", "/logs", "/permissions"):
        assert f'href="{href}"' in m.group(0), href
    assert 'id="estop"' in html and 'id="desktop-app"' in html  # các thẻ cũ + neo cũ vẫn còn (chưa tách tab)


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
    for item in ('href="/settings"', 'href="/costs"', 'href="/update"', 'href="/settings#desktop-app"', 'action="/logout"'):
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
