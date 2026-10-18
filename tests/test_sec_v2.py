"""Hardening SEC — kiểm toán vòng 2 (docs/acceptance/traceability-v2.md §3): V2-N1, V2-N2, V2-N3."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_hub import auth
from agent_hub.daemon import Daemon
from agent_hub.errors import Blocked
from agent_hub.guard.classifier import GuardContext, classify, protected_path
from agent_hub.guard.policy import PolicyStore
from agent_hub.guard.runtime import sandbox_settings
from agent_hub.paths import HubPaths
from agent_hub.runtime.fake import FakeRuntime


# ============================================================================ V2-N1
def test_every_mutating_method_is_classified():
    """Mọi method GHI của mọi luồng phải ở HUMAN_ONLY hoặc AGENT_ALLOWED_MUTATING (có lý do). Method mới chưa phân loại → đỏ."""
    unclassified = sorted(auth.all_mutating() - auth.HUMAN_ONLY - set(auth.AGENT_ALLOWED_MUTATING))
    assert not unclassified, f"method ghi chưa phân loại quyền: {unclassified}"
    assert not (auth.HUMAN_ONLY & set(auth.AGENT_ALLOWED_MUTATING))
    for m in ("agent.revert", "agent.archive", "trash.restore", "trash.purge"):
        assert m in auth.HUMAN_ONLY


@pytest.fixture
def home(monkeypatch):
    h = Path(os.path.realpath("/tmp")) / f"ahv2-{os.getpid()}-{time.time_ns() % 10**6}"
    monkeypatch.setenv("AGENT_HUB_HOME", str(h))
    monkeypatch.delenv("AGENT_HUB_SOCKET", raising=False)
    p = HubPaths.resolve(h)
    p.ensure()
    p.config.write_text("guard:\n  git_probe: false\n")
    yield p
    shutil.rmtree(h, ignore_errors=True)


async def test_admin_surface_requires_human_token_over_socket(home):
    """QA V2-N1: tiến trình cục bộ KHÔNG token gọi agent.revert/archive/trash.* → từ chối (trước: revert bật lại allow_bypass)."""
    d = Daemon(home, runtime=FakeRuntime())
    d.hub.start()
    srv = await asyncio.start_unix_server(d._handle_conn, path=str(home.socket))
    try:
        d.hub.agents.create({"id": "a2", "name": "A2", "tool_policy": {"allow_bypass": True}})
        d.hub.agents.update("a2", {"tool_policy": {"allow_bypass": False}})
        code = ("import json,socket,sys;m,p=sys.argv[2],json.loads(sys.argv[3]);s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1]);"
                "s.sendall((json.dumps({'jsonrpc':'2.0','id':1,'method':m,'params':p})+'\\n').encode());print(s.makefile().readline())")
        for m, prm in (("agent.revert", {"id": "a2", "to": 1}), ("agent.archive", {"id": "a2"}), ("trash.restore", {"id": "a2"}),
                       ("trash.purge", {"id": "a2", "confirm": "a2"})):
            cp = await asyncio.to_thread(subprocess.run, [sys.executable, "-c", code, str(home.socket), m, json.dumps(prm)],
                                         capture_output=True, text=True, timeout=30)
            assert json.loads(cp.stdout).get("error", {}).get("code") == -32003, (m, cp.stdout)
        assert not (d.hub.store.get_agent("a2").get("tool_policy") or {}).get("allow_bypass")
    finally:
        srv.close()
        await d.hub.stop()


def test_purge_gate_at_service_layer(tmp_path):
    """trash.purge gọi agent.purge qua lời gọi NỘI BỘ → cổng ở tầng service chặn khi người gọi không phải người."""
    from agent_hub.hub import Hub
    from agent_hub.recovery import purge_agent

    h = Hub(HubPaths.resolve(tmp_path / "hub"), runtime=FakeRuntime())
    h.agents.create({"id": "a3", "name": "A3"})
    h.agents.remove("a3")
    tok = auth.CURRENT_CALLER.set(auth.Caller(via="socket", pid=123, token_ok=True, run_id="r_x", evidence=["descendant_of_run_pid:1"]))
    try:
        with pytest.raises(Blocked):
            purge_agent(h, "a3", by="user", channel="cli")
    finally:
        auth.CURRENT_CALLER.reset(tok)
    assert h.store.get_agent("a3") is not None
    purge_agent(h, "a3", by="retention", channel="hub")  # vòng nền retention vẫn chạy
    h.store.close()


# ============================================================================ classifier
@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    root = tmp_path_factory.mktemp("v2")
    hub, ws, uh = root / "hub", root / "ws", root / "home"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "m.py").write_text("x")
    hub.mkdir()
    uh.mkdir()
    ps = PolicyStore(hub, user_home=uh)
    ps.ensure()
    time.sleep(1.1)
    return GuardContext(ws, ps.load(), run_since=time.time(), hub_socket=str(hub / "hub.sock"), git_probe=False), uh


def _held(v):
    return v.verdict in ("destructive", "uncertain") or v.forbidden


def test_v2_n1_cli_admin_forbidden(ctx):
    c, _ = ctx
    for cmd in ("agent-hub agent revert a2 --to 1", "agent-hub agent archive a2", "agent-hub trash restore a2", "agent-hub trash purge a2"):
        assert classify("Bash", {"command": cmd}, c).forbidden, cmd


def test_v2_n2_scheduler_commands_held(ctx):
    c, _ = ctx
    for cmd in ("crontab mycron.txt", "at now + 1 minute", "launchctl load job.plist", "systemctl --user enable x.service"):
        assert _held(classify("Bash", {"command": cmd}, c)), cmd
    assert not _held(classify("Bash", {"command": "crontab -l"}, c))


def test_v2_n2_persistence_locations_protected(ctx):
    c, uh = ctx
    for p in (f"{uh}/.config/git/config", f"{uh}/Library/LaunchAgents/a.plist", "/tmp/other/.git/hooks/pre-commit"):
        assert protected_path(c, p, "write") is not None, p
        assert _held(classify("Write", {"file_path": p, "content": "x"}, c)), p
    assert protected_path(c, f"{c.cwd}/src/new.py", "write") is None
    sb = sandbox_settings(Path(c.policy.hub_home), Path(c.hub_socket), {})
    uhome = os.path.expanduser("~")
    for x in (".config/git", "Library/LaunchAgents", ".zshrc", ".ssh"):
        assert f"{uhome}/{x}" in sb["filesystem"]["denyWrite"], x


# ============================================================================ V2-N3
def test_v2_n3_command_name_normalized(ctx):
    c, _ = ctx
    plain = classify("Bash", {"command": "rm -rf src"}, c)
    assert _held(plain)
    for cmd in ("\\rm -rf src", "/bin/rm -rf src", "command rm -rf src", "env rm -rf src"):
        assert classify("Bash", {"command": cmd}, c).verdict == plain.verdict, cmd
    sock = c.hub_socket
    split = sock[:-2] + "''" + sock[-2:]
    assert classify("Bash", {"command": f"socat - UNIX-CONNECT:{split}"}, c).forbidden
