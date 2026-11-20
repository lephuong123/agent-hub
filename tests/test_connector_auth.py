"""Run có connector claude.ai dùng phiên `claude /login` thay token `setup-token` — docs/dev-notes/manager-connectors.md.

Sự thật đã kiểm (claude 2.1.325): với CLAUDE_CODE_OAUTH_TOKEN (setup-token), system/init KHÔNG có server `claude.ai *` nào;
với phiên /login thì có. Kiểm: env của run theo `auth_mode`, `login_available` không gửi token, chọn chế độ theo
`runtime.claude.connectors_auth`, adapter báo `connector.unavailable`, doctor. Không gọi claude thật.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from cfharness import hub_doctor
from cfharness.claude_adapter import ClaudeStreamAdapter
from cfharness.config import DEFAULTS, validate_setting
from cfharness.runtime import claude_cli
from cfharness.runtime.base import RunSpec
from cfharness.runtime.claude_cli import OAUTH_TOKEN_SECRET, ClaudeCliRuntime
from m7_fake import MgrScript, close, make_daemon, rpc, wait_status

TOOLS = ["Bash", "Read", "Write", "Edit", "Grep", "Glob"]


class _Secrets:
    def get(self, name):
        return "sk-ant-oat01-HUBTOKEN" if name == OAUTH_TOKEN_SECRET else None


def _spec(**kw):
    base = dict(run_id="r_1", agent_id="a", prompt="p", cwd=Path("/tmp/w"), session_id="11111111-2222-3333-4444-555555555555",
                raw_log_path=Path("/tmp/r.jsonl"), stderr_log_path=Path("/tmp/r.err"))
    base.update(kw)
    return RunSpec(**base)


def test_run_env_login_mode_drops_every_token(monkeypatch):
    monkeypatch.setenv(OAUTH_TOKEN_SECRET, "from-daemon-env")
    rt = ClaudeCliRuntime(dict(DEFAULTS["runtime"]["claude"]), secrets=_Secrets())
    assert rt._run_env(_spec())[OAUTH_TOKEN_SECRET] == "sk-ant-oat01-HUBTOKEN"
    env = rt._run_env(_spec(auth_mode="login", env_extra={OAUTH_TOKEN_SECRET: "from-run"}))
    assert OAUTH_TOKEN_SECRET not in env


def _fake_claude(tmp_path: Path, payload: dict) -> str:
    """claude giả: `auth status` in JSON; ghi lại có token trong env hay không."""
    p = tmp_path / "claude"
    p.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"open({str(tmp_path / 'seen')!r}, 'w').write('tok' if os.environ.get({OAUTH_TOKEN_SECRET!r}) else 'none')\n"
        f"print(json.dumps({payload!r}))\n", encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


@pytest.mark.skipif(os.name == "nt", reason="script shebang")
@pytest.mark.parametrize("payload,expected", [
    ({"loggedIn": True, "authMethod": "claude.ai"}, True),
    ({"loggedIn": True, "authMethod": "oauth_token"}, False),
    ({"loggedIn": False}, False),
])
async def test_login_available_without_token(tmp_path, payload, expected):
    claude_cli._login_cache.clear()
    cfg = {**DEFAULTS["runtime"]["claude"], "binary": _fake_claude(tmp_path, payload)}
    rt = ClaudeCliRuntime(cfg, secrets=_Secrets())
    assert await rt.login_available() is expected
    assert (tmp_path / "seen").read_text() == "none"  # không bao giờ gửi token hub khi kiểm phiên /login
    claude_cli._login_cache.clear()


def test_validate_connectors_auth():
    for v in ("auto", "login", "token"):
        validate_setting("runtime.claude.connectors_auth", v)
    with pytest.raises(ValueError):
        validate_setting("runtime.claude.connectors_auth", "env")


def test_adapter_connector_unavailable():
    init = {"type": "system", "subtype": "init", "session_id": "s", "tools": [], "plugins": [], "apiKeySource": "none",
            "mcp_servers": [{"name": "claude.ai Google Drive", "status": "needs-auth"}, {"name": "claude.ai Claude Docs", "status": "connected"}]}
    a = ClaudeStreamAdapter("w1", "r_1", allowed_mcp_prefixes=["claude.ai "], expected_connectors=["Google_Drive", "Claude_Docs", "Slack"])
    evs = [e for e in a.feed(init) if e.type == "connector.unavailable"]
    assert [(e.payload["connector"], e.payload["status"]) for e in evs] == [("Google_Drive", "needs-auth"), ("Slack", "missing")]
    # init lần 2 trong cùng tiến trình: không báo lại
    assert not [e for e in a.feed(init) if e.type == "connector.unavailable"]
    # run không có connector: không bao giờ báo
    assert not [e for e in ClaudeStreamAdapter("w1", "r_2").feed(init) if e.type == "connector.unavailable"]


# ---------------------------------------------------------------- tích hợp RunManager (ScriptedRuntime)
class HoldScript(MgrScript):
    def __call__(self, spec):
        self.specs.append(spec)
        return {"text": "ok", "cost": 0.001, "hold": True}


@pytest.fixture(autouse=True)
def _os_sandbox(monkeypatch):
    monkeypatch.setattr("cfharness.manager.sandbox_gate.os_sandbox_available", lambda: True)


@pytest.fixture
def user_home(tmp_path, monkeypatch):
    h = tmp_path / "userhome"
    (h / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


@pytest.fixture
async def d(hub_home):
    dm = await make_daemon(hub_home, HoldScript())
    yield dm
    await close(dm)


async def _run(d, connectors, monkeypatch, login):
    async def login_available(self):
        return login
    monkeypatch.setattr(type(d.hub.runs.runtime()), "login_available", login_available, raising=False)
    d.hub.agents.create({"id": "w1", "name": "w1", "model": "haiku", "tool_policy": {"tools": TOOLS, "connectors": connectors}})
    res = await rpc(d, "run.start", {"agent": "w1", "prompt": "hi"})
    run_id = res.get("run_id") or res.get("id")
    await wait_status(d, run_id, ("running",))
    spec = [s for s in d.script.specs if s.run_id == run_id][-1]
    fallback = d.hub.store.query_events(run_id=run_id, types=["connector.auth_fallback"])
    return spec, fallback


async def test_connector_run_uses_login_when_available(d, user_home, monkeypatch):
    spec, fb = await _run(d, ["Google Drive"], monkeypatch, True)
    assert spec.connectors == ["Google_Drive"] and spec.auth_mode == "login" and not fb


async def test_connector_run_falls_back_to_token(d, user_home, monkeypatch):
    spec, fb = await _run(d, ["Google Drive"], monkeypatch, False)
    assert spec.auth_mode == "token"
    assert [e.payload["reason"] for e in fb] == ["no_claude_ai_login"]


async def test_connectors_auth_token_forced(d, user_home, monkeypatch):
    d.hub.config.set("runtime.claude.connectors_auth", "token")
    spec, fb = await _run(d, ["Google Drive"], monkeypatch, True)
    assert spec.auth_mode == "token" and not fb


async def test_run_without_connectors_keeps_token(d, user_home, monkeypatch):
    spec, fb = await _run(d, None, monkeypatch, True)
    assert spec.auth_mode == "token" and spec.connectors is None and not fb


def test_doctor_connector_users():
    import sqlite3

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE agents (id TEXT, tool_policy TEXT, deleted_at TEXT)")
    c.execute("INSERT INTO agents VALUES ('w1', ?, NULL)", (json.dumps({"connectors": ["Google_Drive"]}),))
    c.execute("INSERT INTO agents VALUES ('w2', '{}', NULL)")
    c.execute("INSERT INTO agents VALUES ('old', ?, '2026-01-01')", (json.dumps({"connectors": ["Slack"]}),))
    assert hub_doctor._connector_users({}, c) == ["w1"]
    assert hub_doctor._connector_users({"manager": {"capabilities": {"connectors": ["Claude_Docs"]}}}, c) == ["(điều phối viên)", "w1"]
