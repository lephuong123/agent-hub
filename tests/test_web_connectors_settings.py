"""Công tắc connector claude.ai trên web (settings_schema.CONNECTORS_GROUP): hiện ở Cài đặt › Tài khoản Claude, không lặp ở Hành vi
hub; BẬT chỉ nhận khi dùng web trên chính máy chạy hub — qua link public chỉ được tắt."""

from __future__ import annotations

from test_web import env  # noqa: F401  (fixture)

from cfharness.web import settings_schema as S

CLOUD = {"x-forwarded-for": "203.0.113.9"}


def test_connectors_loosens():
    assert S.connectors_loosens("connectors.auto", False, True)
    assert not S.connectors_loosens("connectors.auto", True, False)
    assert not S.connectors_loosens("connectors.auto", True, True)
    assert S.connectors_loosens("runtime.claude.connectors_auth", "token", "auto")
    assert S.connectors_loosens("runtime.claude.connectors_auth", "token", "login")
    assert not S.connectors_loosens("runtime.claude.connectors_auth", "auto", "token")
    assert not S.connectors_loosens("runtime.claude.connectors_auth", "auto", "login")
    assert not S.connectors_loosens("files.cloud_download", False, True)


async def test_group_on_accounts_tab_not_config_tab(env):  # noqa: F811
    await env.login()
    acc = (await env.client.get("/settings/accounts")).text
    assert 'id="cfg-connectors"' in acc and 'value="connectors.auto"' in acc and 'value="runtime.claude.connectors_auth"' in acc
    assert 'name="next" value="/settings/accounts#cfg-connectors"' in acc
    cfg = (await env.client.get("/settings/config")).text
    assert 'id="cfg-connectors"' not in cfg


async def test_public_can_only_turn_off(env):  # noqa: F811
    await env.login()

    async def put(key, value, headers=None):
        return await env.post("/do/config_set", {"key": key, "value": value, "confirm": "yes", "next": "/settings/accounts"},
                              headers=headers)

    # tắt qua link public: được
    await put("connectors.auto", "false", CLOUD)
    assert env.hub.config.get("connectors.auto") is False
    await put("runtime.claude.connectors_auth", "token", CLOUD)
    assert env.hub.config.get("runtime.claude.connectors_auth") == "token"
    # bật lại qua link public: bị từ chối, giá trị không đổi
    await put("connectors.auto", "true", CLOUD)
    assert env.hub.config.get("connectors.auto") is False
    await put("runtime.claude.connectors_auth", "auto", CLOUD)
    assert env.hub.config.get("runtime.claude.connectors_auth") == "token"
    # trên chính máy: bật được; giá trị ngoài danh mục bị từ chối
    await put("connectors.auto", "true")
    assert env.hub.config.get("connectors.auto") is True
    await put("runtime.claude.connectors_auth", "auto")
    assert env.hub.config.get("runtime.claude.connectors_auth") == "auto"
    await put("runtime.claude.connectors_auth", "env")
    assert env.hub.config.get("runtime.claude.connectors_auth") == "auto"
