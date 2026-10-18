"""SEC — ngoại lệ sandbox THEO AGENT cho localhost/loopback (team toa-soan-ia: `npm run dev`, Playwright/CDP).

Spike thật claude 2.1.302 (dev-notes hardening-SEC §8): chỉ `sandbox.network.allowLocalBinding: true` là đủ cho bind/listen cổng
local VÀ kết nối 127.0.0.1 (curl qua proxy lẫn kết nối thẳng); Unix socket + denyRead/denyWrite vẫn bị chặn.
"""

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
from agent_hub.guard.classifier import GuardContext, classify
from agent_hub.guard.policy import PolicyStore
from agent_hub.guard.runtime import build_settings
from agent_hub.hub import Hub
from agent_hub.paths import HubPaths
from agent_hub.runtime.fake import FakeRuntime


def _settings(tmp_path, overrides):
    hub = tmp_path / "hub"
    (hub / "policy").mkdir(parents=True, exist_ok=True)
    return hub, build_settings({}, run_id="r", socket=hub / "hub.sock", policy_dir=hub / "policy", guard_cfg={}, approval_timeout_s=300,
                               sandbox_overrides=overrides)


def test_default_has_no_local_binding(tmp_path):
    _, s = _settings(tmp_path, None)
    assert "allowLocalBinding" not in s["sandbox"]["network"]


def test_override_only_relaxes_loopback_not_fs_or_unix_socket(tmp_path):
    hub, base = _settings(tmp_path, None)
    _, s = _settings(tmp_path, {"allow_localhost": True})
    sb = s["sandbox"]
    assert sb["network"]["allowLocalBinding"] is True
    assert sb["filesystem"] == base["sandbox"]["filesystem"]  # filesystem KHÔNG nới
    assert str(hub / "hub.sock") not in (sb["network"].get("allowUnixSockets") or [])
    assert not sb["network"].get("allowAllUnixSockets")
    assert sb["allowUnsandboxedCommands"] is False


def test_override_set_only_by_human_and_audited(tmp_path):
    h = Hub(HubPaths.resolve(tmp_path / "hub"), runtime=FakeRuntime())
    h.agents.create({"id": "dev", "name": "Dev"})
    tok = auth.CURRENT_CALLER.set(auth.Caller(via="socket", pid=1, token_ok=True, run_id="r_x", evidence=["descendant_of_run_pid:1"]))
    try:
        with pytest.raises(Blocked):
            h.guard.sandbox_set("dev", allow_localhost=True)
    finally:
        auth.CURRENT_CALLER.reset(tok)
    assert not h.guard.sandbox_overrides("dev").get("allow_localhost")
    r = h.guard.sandbox_set("dev", allow_localhost=True, by="user")
    assert r["allow_localhost"] and h.guard.sandbox_overrides()["dev"]["allow_localhost"]
    rows = [dict(x) for x in h.store.conn.execute("SELECT * FROM audit_log")]
    assert any("sandbox_override" in str(x.get("kind") or x.get("action") or "") for x in rows)
    assert [e for e in h.store.query_events() if e.type == "guard.sandbox_override"]
    assert "guard.sandbox.set" in auth.HUMAN_ONLY
    h.store.close()


def test_override_is_outside_agent_config_so_learning_cannot_touch(tmp_path):
    """Lưu ở hub_state (không phải cấu hình agent) → ledger/proposal/revert/clone/export không đặt hay giữ được."""
    h = Hub(HubPaths.resolve(tmp_path / "hub"), runtime=FakeRuntime())
    h.agents.create({"id": "dev", "name": "Dev"})
    from agent_hub.errors import HubError

    with pytest.raises(HubError):  # khoá lạ trong cấu hình agent bị từ chối ở validate
        h.agents.update("dev", {"sandbox_overrides": {"allow_localhost": True}})
    assert not h.guard.sandbox_overrides("dev")
    h.store.close()


def test_run_spec_settings_carry_override_for_that_agent_only(tmp_path):
    p = HubPaths.resolve(tmp_path / "hub")
    p.ensure()
    p.config.write_text("guard:\n  git_probe: false\nhub:\n  max_concurrent_runs: 5\n")

    async def main():
        rt = FakeRuntime(hold=True)
        h = Hub(p, runtime=rt)
        h.start()
        for a in ("dev", "other"):
            h.agents.create({"id": a, "name": a})
        h.guard.sandbox_set("dev", allow_localhost=True)
        ids = {a: h.runs.submit(a, "x")["id"] for a in ("dev", "other")}
        got = {}
        for _ in range(300):
            await asyncio.sleep(0.02)
            for a, rid in ids.items():
                f = list(p.home.rglob(f"runtime/{rid}/settings.json"))
                if f and a not in got:
                    got[a] = json.loads(f[0].read_text())
            if len(got) == 2:
                break
        rt.release()
        await h.stop()
        return got

    got = asyncio.run(main())
    assert got["dev"]["sandbox"]["network"].get("allowLocalBinding") is True
    assert "allowLocalBinding" not in got["other"]["sandbox"]["network"]


def test_agent_cannot_set_override_via_cli_classifier(tmp_path):
    ps = PolicyStore(tmp_path / "hub", user_home=tmp_path)
    ctx = GuardContext(tmp_path, ps.load(), git_probe=False)
    assert classify("Bash", {"command": "agent-hub guard sandbox allow-localhost dev on"}, ctx).forbidden
    assert not classify("Bash", {"command": "agent-hub guard sandbox list"}, ctx).forbidden


def test_tui_snapshot_and_agent_list_expose_warning(tmp_path):
    from agent_hub.tui.rpc import snapshot

    d = Daemon(HubPaths.resolve(tmp_path / "hub"), runtime=FakeRuntime())
    d.hub.agents.create({"id": "dev", "name": "Dev"})
    d.hub.agents.create({"id": "plain", "name": "Plain"})
    d.hub.guard.sandbox_set("dev", allow_localhost=True)
    rows = {r["id"]: r for r in snapshot(d.hub)["agents"]}
    assert rows["dev"]["sandbox_localhost"] is True and rows["plain"]["sandbox_localhost"] is False
    lst = {r["id"]: r for r in d._agent_list({})}
    assert lst["dev"]["sandbox_overrides"]["allow_localhost"] is True and lst["plain"]["sandbox_overrides"] == {}
    d.hub.store.close()
