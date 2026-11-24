"""Khu Cài đặt 2 cột (base.html shell "set", app.py settings_index/settings_tab, templates/settings/): mỗi tab /settings/<tab> một trang
riêng giữ id neo cũ, /settings là trang danh mục mang mọi neo cũ, early.js chuyển link /settings#<id> cũ, redirect sau thao tác về đúng tab."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from test_web import env  # noqa: F401  (fixture)

# tab → id neo cũ (của trang /settings dài trước đây) phải còn trong tab mới
TABS = {
    "safety": ("estop", "gates"),
    "accounts": ("accounts",),
    "config": ("config", "cfg-runs", "cfg-approval", "cfg-manager", "cfg-retention"),
    "notifications": ("slack", "slack2", "cfg-notify"),
    "secrets": ("secrets",),
    "filehub": ("filehub", "filehub-lane"),
    "security": ("devices", "cfg-security"),
    "system": ("update", "health-slot"),
    "backup": ("backup", "hub-transfer-link"),
    "support": ("support",),
    "desktop": ("desktop-app",),
}
OLD_IDS = ("estop", "gates", "accounts", "claudeai", "acct-add", "config", "slack", "slack2", "cfg-notify", "secrets", "filehub",
           "filehub-lane", "devices", "cfg-security", "health", "update", "backup", "hub-transfer-link", "support", "desktop-app")


@pytest.mark.parametrize("tab", sorted(TABS))
async def test_each_tab_renders_in_settings_shell(env, tab):  # noqa: F811
    await env.login()
    r = await env.client.get(f"/settings/{tab}")
    assert r.status_code == 200, tab
    html = r.text
    assert '<div class="shell set">' in html and 'class="set-page"' in html
    for i in TABS[tab]:
        assert f'id="{i}"' in html, (tab, i)
    nav = re.search(r'<nav class="snav"[^>]*>(.*?)</nav>', html, re.S).group(1)
    assert re.search(rf'<a href="/settings/{tab}" class="on"[^>]*aria-current="page"', nav), tab
    assert 'class="set-mback" href="/settings"' in html  # điện thoại: về trang danh sách


async def test_unknown_tab_is_404(env):  # noqa: F811
    await env.login()
    assert (await env.client.get("/settings/khong-co")).status_code == 404


async def test_index_keeps_every_old_anchor(env):  # noqa: F811
    await env.login()
    html = (await env.client.get("/settings")).text
    for i in OLD_IDS:
        assert f'id="{i}"' in html, i
    # mỗi neo nằm ngay trước dòng dẫn tới tab chứa nó (không JS vẫn cuộn tới đúng mục)
    assert re.search(r'id="estop" class="set-anchor"></span>.*?<a href="/settings/safety"', html, re.S)
    assert re.search(r'id="desktop-app" class="set-anchor"></span><a href="/settings/desktop"', html)


def test_early_js_maps_old_hashes():
    js = (Path(__file__).resolve().parents[1] / "src" / "cfharness" / "web" / "static" / "early.js").read_text(encoding="utf-8")
    for h, tab in (("estop", "safety"), ("gates", "safety"), ("slack2", "notifications"), ("cfg-notify", "notifications"),
                   ("devices", "security"), ("cfg-security", "security"), ("health", "system"), ("update", "system"),
                   ("hub-transfer-link", "backup"), ("support", "support"), ("desktop-app", "desktop"), ("filehub-lane", "filehub")):
        assert re.search(rf'"?{re.escape(h)}"?: "{tab}"', js), h
    assert '/^acct-/.test(sh) ? "accounts"' in js and '/^cfg-/.test(sh) ? "config"' in js


async def test_safety_tab_danger_zone_first(env):  # noqa: F811
    await env.login()
    html = (await env.client.get("/settings/safety")).text
    body = html.split('class="set-page"', 1)[1]
    assert body.index("Vùng nguy hiểm") < body.index('id="gates"')
    assert "Chỉ chặn run mới" in body and 'name="stop_running" value="1"' in body
    assert 'href="/permissions"' in body


async def test_tabs_only_call_rpcs_they_need(env):  # noqa: F811
    """Tab Hệ thống/Bảo mật không vẽ thẻ Slack/tài khoản; tab Hành vi hub không có nhóm notify (nằm ở Thông báo & kênh)."""
    await env.login()
    cfg = (await env.client.get("/settings/config")).text
    assert 'id="cfg-notify"' not in cfg and 'id="cfg-security"' not in cfg
    notif = (await env.client.get("/settings/notifications")).text
    assert 'id="cfg-runs"' not in notif and 'name="next" value="/settings/notifications#cfg-notify"' in notif
    system = (await env.client.get("/settings/system")).text
    assert 'id="slack2"' not in system and 'id="accounts"' not in system and "HUB_HOME" in system


async def test_actions_redirect_back_to_tab(env):  # noqa: F811
    await env.login()
    r = await env.post("/do/config_set", {"key": "runs.stop_grace_s", "value": "12", "confirm": "yes",
                                          "next": "/settings/config#cfg-runs"})
    assert r.status_code == 303 and r.headers["location"] == "/settings/config#cfg-runs"
    r = await env.post("/do/secret_set", {"name": "X_TOKEN", "value": "v", "confirm": "yes", "next": "/settings/secrets"})
    assert r.status_code == 303 and r.headers["location"] == "/settings/secrets"
    html = (await env.client.get("/settings/secrets")).text
    assert "X_TOKEN" in html and 'name="next" value="/settings/secrets"' in html


async def test_old_settings_links_are_gone_from_app_pages(env):  # noqa: F811
    await env.login()
    for path in ("/manager", "/tasks", "/mcp", "/permissions", "/update", "/settings/accounts", "/settings/notifications"):
        html = (await env.client.get(path)).text
        assert "/settings#" not in html, path
    menu = (await env.client.get("/tasks")).text.split('class="menu-pop side-menu-pop"', 1)[1]
    assert 'href="/settings/desktop"' in menu
