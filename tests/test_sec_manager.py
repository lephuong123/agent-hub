"""Hardening SEC — kiểm toán vai trò manager (docs/acceptance/security-manager.md §3) phần SEC: MGR-1, MGR-2, MGR-8.

MGR-CORE (manager/, auth.MANAGER_ALLOWED, MGR-3/4) sửa song song — đây chỉ phủ phần của luồng SEC.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agent_hub.errors import HubError
from agent_hub.guard.classifier import GuardContext, classify, protected_path
from agent_hub.guard.policy import PolicyStore, repo_deny_for
from agent_hub.guard.runtime import build_settings, fixed_protected_dirs, path_bin_dirs, sandbox_settings
from agent_hub.hub import Hub
from agent_hub.paths import HubPaths
from agent_hub.runtime.fake import FakeRuntime
from agent_hub.workspace import validate_workspace


# ============================================================================ MGR-1
def test_fixed_protected_dirs_never_dropped_by_cwd(tmp_path):
    """MGR-1b: dù cwd = chính vùng bảo vệ, denyWrite vẫn giữ nó (không gỡ như trước)."""
    hub = tmp_path / "hub"
    (hub / "policy").mkdir(parents=True)
    uh = os.path.expanduser("~")
    for evil in (f"{uh}/.local/bin", str(hub / "policy"), str(hub / "auth")):
        s = build_settings({}, run_id="r", socket=hub / "hub.sock", policy_dir=hub / "policy",
                           guard_cfg={}, approval_timeout_s=300, cwd=Path(evil))
        dw = s["sandbox"]["filesystem"]["denyWrite"]
        assert evil.rstrip("/") in [p.rstrip("/") for p in dw], (evil, "bị gỡ khỏi denyWrite")


def test_path_bin_dirs_in_denywrite_and_protected(tmp_path):
    """MGR-1: ~/.local/bin và mọi bin trên $PATH thuộc $HOME bị chặn ghi + protected_path (chặn cả Write tool)."""
    hub = tmp_path / "hub"
    (hub / "policy").mkdir(parents=True)
    s = build_settings({}, run_id="r", socket=hub / "hub.sock", policy_dir=hub / "policy", guard_cfg={}, approval_timeout_s=300)
    dw = s["sandbox"]["filesystem"]["denyWrite"]
    uh = os.path.realpath(os.path.expanduser("~"))
    assert f"{uh}/.local/bin" in dw
    for d in path_bin_dirs():
        assert d in dw, d
    ps = PolicyStore(hub, user_home=Path(uh))
    ctx = GuardContext(Path(uh) / ".local" / "bin", ps.load(), git_probe=False)
    assert protected_path(ctx, f"{uh}/.local/bin/git", "write") is not None
    # Write tool giả lệnh vào ~/.local/bin → destructive
    assert classify("Write", {"file_path": f"{uh}/.local/bin/git", "content": "x"}, ctx).verdict == "destructive"
    # Bash tạo tên mới trong ~/.local/bin → destructive (trước: write)
    ctx2 = GuardContext(Path(f"{uh}/.local/bin"), ps.load(), run_since=time.time(), git_probe=False)
    assert classify("Bash", {"command": "ln -s /tmp/evil python3"}, ctx2).verdict == "destructive"


def test_workspace_in_sensitive_dir_rejected_for_human_and_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_HOME", str(tmp_path / "hub"))
    h = Hub(HubPaths.resolve(tmp_path / "hub"), runtime=FakeRuntime())
    ws_ok = tmp_path / "Workspace" / "proj"
    ws_ok.mkdir(parents=True)
    h.config.set("workspace.allowed_roots", [str(tmp_path / "Workspace")])
    uh = os.path.expanduser("~")
    for bad in (f"{uh}/.local/bin", str(h.paths.home / "policy"), str(h.paths.home), "/", uh, f"{uh}/.ssh"):
        with pytest.raises(HubError):
            validate_workspace(h, {"kind": "dir", "path": bad}, "workspace_default")
    import agent_hub.workspace as _ws

    (tmp_path / "elsewhere").mkdir()
    saved_extra = list(_ws._EXTRA_ALLOWED_ROOTS)
    saved_env = os.environ.pop("AGENT_HUB_EXTRA_WORKSPACE_ROOTS", None)
    _ws._EXTRA_ALLOWED_ROOTS.clear()
    try:
        with pytest.raises(HubError):  # ngoài gốc cho phép (tắt nới test)
            validate_workspace(h, {"kind": "dir", "path": str(tmp_path / "elsewhere")}, "ws")
    finally:
        _ws._EXTRA_ALLOWED_ROOTS.extend(saved_extra)
        if saved_env is not None:
            os.environ["AGENT_HUB_EXTRA_WORKSPACE_ROOTS"] = saved_env
    validate_workspace(h, {"kind": "dir", "path": str(ws_ok)}, "ws")  # trong gốc → ok
    validate_workspace(h, "scratch", "ws")  # scratch luôn ok
    h.store.close()


def test_agent_create_rejects_sensitive_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_HOME", str(tmp_path / "hub"))
    h = Hub(HubPaths.resolve(tmp_path / "hub"), runtime=FakeRuntime())
    uh = os.path.expanduser("~")
    with pytest.raises(HubError):
        h.agents.create({"id": "ops", "name": "Ops", "workspace_default": f"{uh}/.local/bin"})
    assert h.store.get_agent("ops") is None
    h.store.close()


def test_run_adhoc_cwd_in_sensitive_dir_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HUB_HOME", str(tmp_path / "hub"))
    h = Hub(HubPaths.resolve(tmp_path / "hub"), runtime=FakeRuntime())
    h.agents.create({"id": "a", "name": "A"})
    with pytest.raises(HubError):
        h.runs.submit("a", "hi", cwd=f"{os.path.expanduser('~')}/.local/bin")
    h.store.close()


# ============================================================================ MGR-2
def test_repo_deny_applies_to_any_agent_in_repo(tmp_path):
    import subprocess

    repo = tmp_path / "Workspace" / "toa"
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    hub = tmp_path / "hub"
    ps = PolicyStore(hub, user_home=tmp_path)
    ps.ensure()
    ps.repo_deny_add(str(repo), "Bash(npm run deploy:*)")
    ps.repo_deny_add(str(repo), "Bash(git push:*)")
    assert set(repo_deny_for(ps, str(repo))) == {"Bash(npm run deploy:*)", "Bash(git push:*)"}
    assert repo_deny_for(ps, str(repo / "sub")) == repo_deny_for(ps, str(repo))  # file con trong repo
    assert repo_deny_for(ps, str(tmp_path / "other")) == []
    ctx = GuardContext(repo, ps.load(), run_since=time.time(), git_probe=False, repo_deny=tuple(repo_deny_for(ps, str(repo))))
    for cmd in ("npm run deploy", "npm run deploy -- --prod", "git push origin main"):
        assert classify("Bash", {"command": cmd}, ctx).verdict == "destructive", cmd
    # cùng repo, agent KHÁC (không mẫu trên agent) vẫn bị chặn vì repo_deny gắn repo
    assert classify("Bash", {"command": "echo ok"}, ctx).verdict == "safe"


def test_repo_deny_set_is_human_only():
    from agent_hub import auth

    assert "guard.repo_deny.add" in auth.HUMAN_ONLY and "guard.repo_deny.rm" in auth.HUMAN_ONLY


def test_repo_deny_cli_forbidden_from_agent(tmp_path):
    ps = PolicyStore(tmp_path / "hub", user_home=tmp_path)
    ctx = GuardContext(tmp_path, ps.load(), git_probe=False)
    assert classify("Bash", {"command": "agent-hub guard repo-deny add /x 'Bash(git push:*)'"}, ctx).forbidden
    assert not classify("Bash", {"command": "agent-hub guard repo-deny list"}, ctx).forbidden


# ============================================================================ MGR-8
def test_manager_run_policy_edit_is_forbidden_not_approval(tmp_path):
    hub = tmp_path / "hub"
    ps = PolicyStore(hub, user_home=tmp_path)
    ps.ensure()
    normal = GuardContext(tmp_path, ps.load(), run_since=time.time(), hub_socket=str(hub / "hub.sock"), git_probe=False)
    mgr = GuardContext(tmp_path, ps.load(), run_since=time.time(), hub_socket=str(hub / "hub.sock"), git_probe=False, manager_run=True)
    # ghi policy/ bằng Write → agent thường: destructive (approval); manager: forbidden
    w = {"file_path": f"{hub}/policy/protected.yaml", "content": "x"}
    assert classify("Write", w, normal).verdict == "destructive" and not classify("Write", w, normal).forbidden
    assert classify("Write", w, mgr).forbidden
    # agent-hub guard/config/approve từ manager → forbidden (vốn forbidden cho mọi agent; đảm bảo manager không "approval")
    assert classify("Bash", {"command": f"echo x > {hub}/policy/catalog.yaml"}, mgr).forbidden
    assert classify("Bash", {"command": "agent-hub config set approval.timeout_s 1"}, mgr).forbidden
