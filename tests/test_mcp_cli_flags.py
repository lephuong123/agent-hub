"""`cfharness mcp add|edit`: cờ đặt SAU tên server không bị nuốt thành lệnh stdio; đổi stdio → http bỏ `command` cũ."""

from __future__ import annotations

import pytest

from cfharness.cli import build_parser
from cfharness.errors import HubError
from cfharness.hub import Hub
from cfharness.runtime import FakeRuntime
from cfharness.mcp_server.cli import _hoist_trailing_flags, _server_payload


def _payload(argv: list[str], act: str) -> tuple[object, dict]:
    a = build_parser().parse_args(["mcp", act, *argv])
    _hoist_trailing_flags(a, act)
    return a, _server_payload(a, creating=act == "add")


def test_flags_after_name_are_flags_not_stdio_command():
    a, d = _payload(["svc", "--transport", "http", "--url", "https://x.example/api/mcp", "--trust", "trusted",
                     "--description", "Mô tả", "--no-probe"], "add")
    assert "command" not in d and d["transport"] == "http" and d["url"] == "https://x.example/api/mcp"
    assert "auth" not in d and d["trust"] == "trusted" and d["description"] == "Mô tả" and a.no_probe
    # OAuth do hub giữ đã gỡ (v0.2.28): cờ --oauth* → lỗi hướng dẫn `mcp claude add` (không thành tham số lệnh stdio)
    with pytest.raises(SystemExit, match="mcp claude add"):
        _payload(["svc", "--url", "https://x.example/api/mcp", "--oauth"], "add")


def test_flags_before_name_and_stdio_after_double_dash_unchanged():
    _, d = _payload(["--trust", "trusted", "svc", "--", "npx", "-y", "pkg"], "add")
    assert d["command"] == "npx" and d["args"] == ["-y", "pkg"] and d["transport"] == "stdio" and d["trust"] == "trusted"
    _, d = _payload(["svc", "--trust", "trusted", "--", "npx", "-y", "pkg"], "add")
    assert d["command"] == "npx" and d["args"] == ["-y", "pkg"] and d["trust"] == "trusted"
    _, d = _payload(["svc", "npx", "-y", "pkg"], "add")
    assert d["command"] == "npx" and d["args"] == ["-y", "pkg"]


def test_edit_flags_after_name_and_unknown_flag_errors():
    a, d = _payload(["svc", "--url", "https://x.example/mcp", "--no-oauth", "--probe"], "edit")
    assert d == {"url": "https://x.example/mcp", "transport": "http", "auth": None} and a.probe
    with pytest.raises(SystemExit):
        _payload(["svc", "--khong-co"], "edit")


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home, runtime=FakeRuntime())
    h.start()
    yield h
    h.store.close()


def test_update_stdio_to_http_drops_command(hub):
    hub.mcp.add({"name": "svc", "transport": "stdio", "command": "--transport", "args": ["http"], "env": {"A": "1"}})
    with pytest.raises(HubError, match="mcp claude add"):  # OAuth nguồn hub mới bị từ chối (gỡ từ v0.2.28)
        hub.mcp.update("svc", {"transport": "http", "url": "https://x.example/api/mcp", "auth": {"type": "oauth"}, "trust": "trusted"})
    r = hub.mcp.update("svc", {"transport": "http", "url": "https://x.example/api/mcp", "headers": {"X-K": "v"}, "trust": "trusted"})
    assert r["transport"] == "http" and r["trust"] == "trusted"
    assert not ({"command", "args", "env"} & set(r["config"])) and r["config"]["headers"]
    r = hub.mcp.update("svc", {"transport": "stdio", "command": "npx", "args": ["pkg"]})
    assert r["transport"] == "stdio" and not ({"url", "auth", "headers"} & set(r["config"]))
