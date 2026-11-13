"""Plugin Claude Code cho run của agent (`plugins` → `--plugin-dir`) — docs/dev-notes/agent-plugins.md."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cfharness.agents import AgentConfig
from cfharness.claude_adapter import ClaudeStreamAdapter
from cfharness.config import DEFAULTS
from cfharness.errors import HubError
from cfharness.hub import Hub
from cfharness.plugins import PluginError, resolve
from cfharness.runtime import FakeRuntime, RunSpec, build_argv
from cfharness.runtime.base import RuntimeError_
from cfharness.tui.takeover import build_takeover_argv


def _plugin(root: Path, name: str, hooks: list[str] | None = None) -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    if hooks:
        (root / "hooks").mkdir()
        (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {h: [] for h in hooks}}), encoding="utf-8")
    return root


@pytest.fixture
def claude_home(tmp_path, monkeypatch) -> Path:
    cd = tmp_path / "dotclaude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cd))
    vercel = _plugin(cd / "plugins" / "cache" / "official" / "vercel" / "0.50.0", "vercel", ["SessionStart", "PostToolUse"])
    other = _plugin(cd / "plugins" / "cache" / "mk2" / "dup" / "1.0", "dup")
    other2 = _plugin(cd / "plugins" / "cache" / "mk3" / "dup" / "1.0", "dup")
    inst = {"version": 2, "plugins": {
        "vercel@official": [{"scope": "user", "installPath": str(vercel), "version": "0.50.0"}],
        "dup@mk2": [{"scope": "user", "installPath": str(other)}],
        "dup@mk3": [{"scope": "user", "installPath": str(other2)}],
        "gone@official": [{"scope": "user", "installPath": str(cd / "nope")}],
    }}
    (cd / "plugins" / "installed_plugins.json").write_text(json.dumps(inst), encoding="utf-8")
    return cd


def test_resolve(claude_home, tmp_path):
    [p] = resolve(["vercel@official"])
    assert p.name == "vercel" and p.path.name == "0.50.0" and p.hook_events == ["PostToolUse", "SessionStart"]
    assert resolve(["vercel"])[0].path == p.path  # tên trần, duy nhất
    assert resolve(["dup@mk3"])[0].path.parent.parent.name == "mk3"
    with pytest.raises(PluginError, match="nhiều marketplace"):
        resolve(["dup"])
    with pytest.raises(PluginError, match="chưa được cài"):
        resolve(["missing@official"])
    with pytest.raises(PluginError, match="không còn"):
        resolve(["gone@official"])
    local = _plugin(tmp_path / "mine", "mine")
    assert resolve([str(local)])[0].name == "mine"
    with pytest.raises(PluginError, match="plugin.json"):
        resolve([str(tmp_path)])


def test_agent_config_plugins_validated():
    assert AgentConfig(id="a", name="a", plugins="vercel@official").plugins == ["vercel@official"]
    assert AgentConfig(id="a", name="a", plugins=["x", "x", " "]).plugins == ["x"]
    with pytest.raises(ValueError):
        AgentConfig(id="a", name="a", plugins=["../evil"])
    with pytest.raises(ValueError):
        AgentConfig(id="a", name="a", plugins=["a b"])


def test_build_argv_plugin_dir_keeps_isolation():
    claude = json.loads(json.dumps(DEFAULTS["runtime"]["claude"]))
    spec = RunSpec(run_id="r_1", agent_id="a", prompt="p", cwd=Path("/tmp/w"), session_id="11111111-2222-3333-4444-555555555555",
                   raw_log_path=Path("/tmp/r.jsonl"), stderr_log_path=Path("/tmp/r.err"), plugin_dirs=[Path("/p/one"), Path("/p/two")],
                   tool_policy={"tools": ["Read"]})
    argv, _ = build_argv(spec, claude, "2.1.312")  # --plugin-dir có trong flag_matrix
    i = argv.index("--plugin-dir")
    assert argv[i + 1] == str(Path("/p/one")) and argv[i + 3] == str(Path("/p/two"))
    assert argv[argv.index("--setting-sources") + 1] == "" and "--strict-mcp-config" in argv
    assert argv.index("--plugin-dir") < argv.index("--tools")  # cờ variadic vẫn ở cuối


def test_takeover_argv_plugin_dir():
    argv = build_takeover_argv(binary="claude", session_id="s", settings_path=Path("s.json"), mcp_path=Path("m.json"), system_prompt=None,
                               model=None, tool_policy={}, isolation={}, plugin_dirs=[Path("/p/one")])
    assert argv[argv.index("--plugin-dir") + 1] == str(Path("/p/one"))


def test_adapter_accepts_declared_plugin():
    init = {"type": "system", "subtype": "init", "session_id": "s", "plugins": [{"name": "vercel"}, {"name": "cc-plugin-telemetry"}, {"name": "evil"}]}
    ad = ClaudeStreamAdapter("a", "r", allowed_plugin_prefixes=["cc-plugin-", "vercel"])
    ad.feed(json.dumps(init))
    assert [v["name"] for v in ad.violations if v["kind"] == "plugin"] == ["evil"]


@pytest.fixture
def hub(hub_home):
    rt = FakeRuntime(hold=True)
    h = Hub(hub_home, runtime=rt)
    yield h
    rt.release()


async def test_prepare_spec_plugins(hub, claude_home):
    hub.agents.create({"id": "pa", "name": "pa", "plugins": ["vercel@official"]})
    assert hub.store.get_agent("pa")["plugins"] == ["vercel@official"]
    run = hub.store.get_run(hub.runs.submit("pa", "hi")["id"])
    spec = hub.runs._prepare_spec(run, hub.store.get_agent("pa"))
    assert [p.name for p in spec.plugin_dirs] == ["0.50.0"]
    assert "vercel" in spec.allowed_plugin_prefixes and "cc-plugin-" in spec.allowed_plugin_prefixes
    assert {"SessionStart", "PostToolUse"} <= set(spec.allowed_hooks or [])
    # agent không khai báo plugin → không --plugin-dir, không nới kiểm cô lập
    hub.agents.create({"id": "pb", "name": "pb"})
    run = hub.store.get_run(hub.runs.submit("pb", "hi")["id"])
    spec = hub.runs._prepare_spec(run, hub.store.get_agent("pb"))
    assert spec.plugin_dirs == [] and "vercel" not in (spec.allowed_plugin_prefixes or [])


async def test_missing_plugin_fails_early(hub, claude_home):
    hub.agents.create({"id": "pc", "name": "pc", "plugins": ["missing@official"]})
    with pytest.raises(HubError, match="chưa được cài"):
        hub.runs.submit("pc", "hi")
    hub.agents.update("pc", {"plugins": ["vercel"]})
    hub.runs.submit("pc", "hi")
    hub.agents.update("pc", {"plugins": ["dup"]})
    run_row = hub.store.get_agent("pc")
    with pytest.raises(RuntimeError_):  # lúc spawn cũng chặn (cài/gỡ giữa chừng)
        hub.runs._prepare_spec({"id": "r_x", "run_opts": {}, "session_id": "s", "prompt": "p"}, run_row)


def test_manager_cannot_set_plugins():
    from cfharness.manager.permissions import AGENT_FIELDS

    assert "plugins" not in AGENT_FIELDS
