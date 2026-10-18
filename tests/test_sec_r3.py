"""Hardening SEC — kiem toan manager vong 3 (R3-1): token forge trong keychain doc duoc trong sandbox.

Sandbox Claude Code KHONG chan Mach IPC toi SecurityServer. Hai lop: (1) classifier + deny tinh chan lenh/ma doc
keychain/credential/token forge; (2) mang Bash mac dinh HEP (khong github/gitlab/bitbucket) nen token doc duoc cung khong ra
toi forge. Nguoi mo them domain theo agent (HUMAN_ONLY). Da kiem that bang claude haiku (ghi o hardening-SEC §11).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agent_hub import auth
from agent_hub.errors import Blocked, HubError
from agent_hub.guard.classifier import GuardContext, classify
from agent_hub.guard.policy import PolicyStore
from agent_hub.guard.runtime import DEFAULT_ALLOWED_DOMAINS, build_settings, network_domains
from agent_hub.hub import Hub
from agent_hub.paths import HubPaths
from agent_hub.runtime.fake import FakeRuntime


@pytest.fixture
def ctx(tmp_path):
    ps = PolicyStore(tmp_path / "hub", user_home=Path(os.path.expanduser("~")))
    ps.ensure()
    (tmp_path / "ws").mkdir()
    return GuardContext(tmp_path / "ws", ps.load(), run_since=time.time(), git_probe=False)


KEYCHAIN_CMDS = [
 "security find-generic-password -s gh:github.com -w",
 "security find-internet-password -s github.com -w",
 "security dump-keychain -d",
 "security export -k login.keychain -t certs",
 "printf 'x' | git credential fill",
 "git credential approve",
 "git credential-osxkeychain get",
 "git config --global credential.helper store",
 "gh auth token",
 "gh auth status --show-token",
 "osascript -e 'do shell script \"security find-generic-password -w -s x\"'",
 "python3 -c \"import ctypes; ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security'); SecItemCopyMatching\"",
 "node -e \"require('keytar').getPassword('gh','x')\""
]


def test_keychain_and_credential_commands_forbidden(ctx):
    missed = [(c, v.verdict, v.forbidden) for c in KEYCHAIN_CMDS if not classify("Bash", {"command": c}, ctx).forbidden]
    assert not missed, missed


def test_benign_security_and_git_config_allowed(ctx):
    assert not classify("Bash", {"command": "security -h"}, ctx).forbidden
    assert not classify("Bash", {"command": "git config user.name"}, ctx).forbidden
    assert not classify("Bash", {"command": "gh pr list"}, ctx).forbidden


def test_read_credential_files_forbidden(ctx):
    uh = os.path.expanduser("~")
    for f in (f"{uh}/.config/gh/hosts.yml", f"{uh}/.git-credentials", f"{uh}/Library/Keychains/login.keychain-db", f"{uh}/.netrc"):
        assert classify("Read", {"file_path": f}, ctx).forbidden, f
        assert classify("Bash", {"command": "cat " + f}, ctx).forbidden, f


def test_default_network_excludes_forges(tmp_path):
    hub = tmp_path / "hub"
    (hub / "policy").mkdir(parents=True)
    s = build_settings({}, run_id="r", socket=hub / "hub.sock", policy_dir=hub / "policy", guard_cfg={}, approval_timeout_s=300)
    doms = s["sandbox"]["network"]["allowedDomains"]
    assert "*" not in doms
    assert not any("github" in d or "gitlab" in d or "bitbucket" in d for d in doms)
    assert any("npmjs" in d for d in doms) and any("anthropic" in d for d in doms)


def test_network_domains_includes_agent_mcp_and_overrides():
    d = network_domains({}, {"mcp_domains": ["vb.cchchn.com"], "allow_domains": ["github.com"], "allow_localhost": True})
    assert "vb.cchchn.com" in d and "github.com" in d and "127.0.0.1" in d
    for base in DEFAULT_ALLOWED_DOMAINS:
        assert base in d


def test_static_deny_covers_keychain(tmp_path):
    hub = tmp_path / "hub"
    (hub / "policy").mkdir(parents=True)
    deny = set(build_settings({}, run_id="r", socket=hub / "hub.sock", policy_dir=hub / "policy", guard_cfg={}, approval_timeout_s=300)["permissions"]["deny"])
    assert "Bash(security find-generic-password:*)" in deny and "Bash(gh auth token:*)" in deny
    uh = os.path.expanduser("~")
    assert f"Read(/{uh}/.config/gh/**)" in deny


def test_allow_domain_human_only_and_audited(tmp_path):
    h = Hub(HubPaths.resolve(tmp_path / "hub"), runtime=FakeRuntime())
    h.agents.create({"id": "a", "name": "A"})
    tok = auth.CURRENT_CALLER.set(auth.Caller(via="socket", pid=1, token_ok=True, run_id="r", evidence=["descendant_of_run_pid:1"]))
    try:
        with pytest.raises(Blocked):
            h.guard.sandbox_allow_domain("a", "github.com")
    finally:
        auth.CURRENT_CALLER.reset(tok)
    r = h.guard.sandbox_allow_domain("a", "github.com", by="user")
    assert "github.com" in r["allow_domains"]
    assert "guard.sandbox.allow_domain" in auth.HUMAN_ONLY
    assert any("sandbox_override" in str(dict(x).get("kind") or dict(x).get("action") or "") for x in h.store.conn.execute("SELECT * FROM audit_log"))
    with pytest.raises(HubError):
        h.guard.sandbox_allow_domain("a", "not a domain!!")
    h.store.close()


def test_allow_domain_cli_forbidden_from_agent(ctx):
    assert classify("Bash", {"command": "agent-hub guard sandbox allow-domain a github.com"}, ctx).forbidden
    assert not classify("Bash", {"command": "agent-hub guard sandbox list"}, ctx).forbidden


def test_agent_mcp_domains_feed_sandbox(tmp_path):
    h = Hub(HubPaths.resolve(tmp_path / "hub"), runtime=FakeRuntime())
    h.mcp.add({"name": "vb", "transport": "http", "url": "https://vb.cchchn.com/mcp"})
    h.agents.create({"id": "a", "name": "A", "mcp_servers": ["vb"]})
    ov = h.guard.run_sandbox_overrides("a")
    assert "vb.cchchn.com" in ov["mcp_domains"]
    doms = network_domains({}, ov)
    assert "vb.cchchn.com" in doms
    h.store.close()
