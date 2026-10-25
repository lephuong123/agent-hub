"""Windows (không sandbox OS): khung điều phối viên rộng (hermes) FAIL-CLOSED trừ khi người opt-in
`manager.allow_hermes_without_sandbox` (manager/sandbox_gate.py). Cũng kiểm: claude 2.1.303 trong ma trận đã kiểm, bench bỏ qua êm.

`os_sandbox_available` bị thay bằng bản giả → chạy giống nhau trên mọi hệ điều hành. Không gọi claude thật.
"""

from __future__ import annotations

import json

import pytest

from cfharness.config import DEFAULTS, HubConfig
from cfharness.errors import HubError
from cfharness.guard.integrity import SECURITY_KEYS
from cfharness.manager import sandbox_gate
from m7_fake import close, make_daemon, rpc

HERMES = {"profile": "hermes", "tools": None, "mcp": None, "web": None}
HERMES_TOOLS = ["Bash", "Read", "Write", "Edit", "Grep", "Glob", "TodoWrite", "WebFetch", "WebSearch"]


@pytest.fixture
def no_sandbox(monkeypatch):
    monkeypatch.setattr(sandbox_gate, "os_sandbox_available", lambda: False)


@pytest.fixture
async def d(hub_home, no_sandbox):
    dm = await make_daemon(hub_home)
    yield dm
    await close(dm)


def test_opt_in_default_off_and_integrity_protected():
    assert DEFAULTS["manager"]["allow_hermes_without_sandbox"] is False
    assert "manager" in SECURITY_KEYS  # nhánh manager (gồm opt-in) bị khôi phục nếu agent sửa config.yaml


def test_set_hermes_refused_without_opt_in(tmp_path, no_sandbox):
    cfg = HubConfig(tmp_path / "config.yaml")
    for key, val in (("manager.capabilities", HERMES), ("manager.capabilities.profile", "hermes"),
                     ("manager.capabilities.tools", ["Read", "Bash"]), ("manager", {"capabilities": {"profile": "hermes"}})):
        with pytest.raises(ValueError, match="allow_hermes_without_sandbox"):
            cfg.set(key, val)
    assert cfg.get("manager.capabilities.profile") == "strict"
    assert not (tmp_path / "config.yaml").exists()  # bị từ chối = không ghi gì
    cfg.set("manager.capabilities.profile", "strict")  # strict luôn đặt được
    cfg.set("manager.allow_hermes_without_sandbox", True)
    cfg.set("manager.capabilities", HERMES)
    assert cfg.get("manager.capabilities.profile") == "hermes"
    cfg.set("manager.allow_hermes_without_sandbox", False)  # tắt opt-in vẫn được — khung tự hạ khi resolve


def test_set_hermes_allowed_when_sandbox_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_gate, "os_sandbox_available", lambda: True)
    cfg = HubConfig(tmp_path / "config.yaml")
    cfg.set("manager.capabilities", HERMES)
    assert cfg.get("manager.capabilities.profile") == "hermes"


def test_gate_pure(no_sandbox):
    from cfharness.manager.permissions import resolve_capabilities

    h = resolve_capabilities({"profile": "hermes"})
    g = sandbox_gate.gate(h, {})
    assert g["profile"] == "strict" and g["tools"] == {"Read", "Grep", "Glob"} and g["mcp"] == "none" and g["sandbox_blocked"]
    assert any("allow_hermes_without_sandbox" in w for w in g["warnings"])
    assert sandbox_gate.gate(h, {"allow_hermes_without_sandbox": True}) is h
    assert sandbox_gate.gate(h, {"allow_hermes_without_sandbox": "yes"})["profile"] == "strict"  # chỉ đúng true
    s = resolve_capabilities(None)
    assert sandbox_gate.gate(s, {}) is s
    # tool rộng tuỳ chỉnh trên profile strict cũng bị chặn
    assert sandbox_gate.gate(resolve_capabilities({"profile": "strict", "tools": ["Read", "Bash"]}), {})["tools"] == {"Read", "Grep", "Glob"}


def test_doctor_note(no_sandbox):
    assert sandbox_gate.doctor_note({"manager": {"capabilities": {"profile": "strict"}}}) is None
    ok, msg = sandbox_gate.doctor_note({"manager": {"capabilities": {"profile": "hermes"}}})
    assert not ok and "hạ về strict" in msg
    ok, msg = sandbox_gate.doctor_note({"manager": {"capabilities": {"profile": "hermes"}, "allow_hermes_without_sandbox": True}})
    assert not ok and "KHÔNG sandbox" in msg


def test_doctor_note_none_with_sandbox(monkeypatch):
    monkeypatch.setattr(sandbox_gate, "os_sandbox_available", lambda: True)
    assert sandbox_gate.doctor_note({"manager": {"capabilities": {"profile": "hermes"}}}) is None


async def test_hand_edited_hermes_resolves_strict(d):
    """config.yaml sửa tay sang hermes (không qua config.set) → khung hiệu lực vẫn strict; manager có Bash bị từ chối."""
    import yaml

    cfgp = d.paths.config
    data = yaml.safe_load(cfgp.read_text(encoding="utf-8")) or {}
    data.setdefault("manager", {})["capabilities"] = HERMES
    cfgp.write_text(yaml.safe_dump(data), encoding="utf-8")
    d.hub.config.reload()
    assert d.hub.config.get("manager.capabilities.profile") == "hermes"
    caps = d.hub.manager.perms.capabilities()
    assert caps["profile"] == "strict" and "Bash" not in caps["tools"]
    d.hub.agents.create({"id": "boss", "name": "boss", "role": "hub boss", "model": "haiku", "tool_policy": {"tools": HERMES_TOOLS}})
    with pytest.raises(HubError, match="Read/Grep/Glob"):
        await rpc(d, "manager.set", {"agent_id": "boss"})
    info = await rpc(d, "manager.capabilities")
    assert info["effective"]["profile"] == "strict"


async def test_rpc_set_refused_then_opt_in(d):
    with pytest.raises(HubError, match="allow_hermes_without_sandbox"):
        await rpc(d, "config.set", {"key": "manager.capabilities", "value": HERMES})
    await rpc(d, "config.set", {"key": "manager.allow_hermes_without_sandbox", "value": True})
    await rpc(d, "config.set", {"key": "manager.capabilities", "value": HERMES})
    d.hub.agents.create({"id": "boss", "name": "boss", "role": "hub boss", "model": "haiku", "tool_policy": {"tools": HERMES_TOOLS}})
    await rpc(d, "manager.set", {"agent_id": "boss"})
    assert d.hub.manager.perms.capabilities()["profile"] == "hermes"


# ------------------------------------------------------------------ claude 2.1.303 + bench
def test_tested_versions_include_windows_spike():
    c = DEFAULTS["runtime"]["claude"]
    assert "2.1.303" in c["tested_versions"] and "2.1.302" in c["tested_versions"]
    assert set(c["tested_versions"]) <= set(c["flag_matrix"])  # mọi phiên bản đã kiểm có danh sách cờ
    m = c["flag_matrix"]
    assert m["2.1.303"] == m["2.1.302"] and m["2.1.303"] is not m["2.1.302"]
    assert "--dangerously-skip-permissions" not in m["2.1.303"]


def test_bench_skips_cleanly_on_windows(monkeypatch, capsys):
    from cfharness.tui import bench

    monkeypatch.setattr(bench.sys, "platform", "win32")
    why = bench.unsupported_reason()
    assert why and "Windows" in why
    assert bench.main(["--home", "x"]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {"skipped": True, "reason": why, "platform": "win32"}
    assert "bỏ qua" in err
