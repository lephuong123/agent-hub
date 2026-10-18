"""Hardening SEC — kiểm toán manager vòng 2 (docs/acceptance/security-manager.md "Vòng 2"): R2-1, R2-2, R2-4, R2-5.

Tái hiện kịch bản QA (`scratchpad/audit-mgr-r2/result-r2-*.txt`): đỏ trước khi sửa, xanh sau.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_hub.errors import HubError
from agent_hub.guard.classifier import GuardContext, classify, exec_zone
from agent_hub.guard.policy import PolicyStore, repo_deny_for
from agent_hub.guard.runtime import build_settings, executable_zones, hub_repo_root
from agent_hub.hub import Hub
from agent_hub.paths import HubPaths
from agent_hub.runtime.fake import FakeRuntime


def held(v):
    return v.verdict in ("destructive", "uncertain") or v.forbidden


# ============================================================================ R2-1: tool ghi file ngoài workspace
@pytest.fixture
def wctx(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    ps = PolicyStore(tmp_path / "hub", user_home=Path(os.path.expanduser("~")))
    ps.ensure()
    return GuardContext(ws, ps.load(), run_since=time.time(), hub_socket=str(tmp_path / "hub" / "hub.sock"), git_probe=False)


def test_write_tool_into_exec_zones_forbidden(wctx):
    """QA result-r2-paths: .pth trong site-packages của hub, /opt/homebrew/bin, ~/.npmrc, tests/conftest.py của hub → trước: write."""
    import sysconfig

    uh = os.path.expanduser("~")
    targets = [os.path.join(sysconfig.get_paths()["purelib"], "zz_audit.pth"), "/opt/homebrew/bin/git-zz", "/usr/local/bin/git-zz",
               f"{uh}/.npmrc", f"{uh}/.config/uv/uv.toml", f"{uh}/.pythonrc.py", f"{uh}/.tmux.conf"]
    root = hub_repo_root()
    if root:
        targets += [f"{root}/tests/conftest.py", f"{root}/pyproject.toml"]
    for t in targets:
        for tool in ("Write", "Edit", "MultiEdit"):
            key = "file_path"
            v = classify(tool, {key: t, "content": "x", "old_string": "a", "new_string": "b"}, wctx)
            assert v.forbidden, (tool, t, v.verdict, v.rule)
    v = classify("NotebookEdit", {"notebook_path": f"{uh}/.npmrc", "new_source": "x"}, wctx)
    assert v.forbidden


def test_write_tool_outside_workspace_needs_human(wctx, tmp_path):
    """Ghi ngoài workspace của run (không phải vùng thực thi) → chờ người; trong workspace/tmp → ghi thường."""
    outside = Path(os.path.expanduser("~")) / ".agent-hub-audit-r2" / "x.txt"
    v = classify("Write", {"file_path": str(outside), "content": "x"}, wctx)
    assert v.verdict == "destructive" and v.rule in ("write_outside_workspace", "protected_path", "exec_zone_write"), v
    assert classify("Write", {"file_path": str(wctx.cwd / "src" / "a.py"), "content": "x"}, wctx).verdict == "write"
    assert classify("Write", {"file_path": "/tmp/agent-hub-r2-x.txt", "content": "x"}, wctx).verdict == "write"


def test_write_tool_symlink_into_exec_zone_resolved(wctx):
    """Giải realpath trước khi so: symlink TRONG workspace trỏ vào site-packages → vẫn cấm."""
    import sysconfig

    link = wctx.cwd / "sp"
    os.symlink(sysconfig.get_paths()["purelib"], link)
    assert classify("Write", {"file_path": str(link / "evil.pth"), "content": "import os"}, wctx).forbidden


def test_static_deny_and_sandbox_cover_exec_zones(tmp_path):
    hub = tmp_path / "hub"
    (hub / "policy").mkdir(parents=True)
    s = build_settings({}, run_id="r", socket=hub / "hub.sock", policy_dir=hub / "policy", guard_cfg={}, approval_timeout_s=300)
    deny = set(s["permissions"]["deny"])
    dw = set(s["sandbox"]["filesystem"]["denyWrite"])
    for z in executable_zones():
        assert f"Write(/{z.rstrip('/')}/**)" in deny and f"Edit(/{z.rstrip('/')}/**)" in deny, z
        assert z in dw, z
    assert os.path.realpath(sys.prefix) in dw  # venv của chính hub (Bash)


def test_workspace_cannot_be_inside_hub_repo(tmp_path, monkeypatch):
    root = hub_repo_root()
    if not root:
        pytest.skip("không chạy từ repo hub")
    h = Hub(HubPaths.resolve(tmp_path / "hub"), runtime=FakeRuntime())
    from agent_hub.workspace import validate_workspace

    for sub in ("tests", "docs", ".git"):
        with pytest.raises(HubError):
            validate_workspace(h, {"kind": "dir", "path": os.path.join(root, sub)}, "ws")
    h.store.close()


def test_python_env_tamper_detected(tmp_path, monkeypatch):
    from agent_hub.guard import integrity

    fake_sp = tmp_path / "sp"
    fake_sp.mkdir()
    (fake_sp / "good.pth").write_text("/some/path\n")
    monkeypatch.setattr(integrity, "python_env_files", lambda: {str(p): integrity._sha(p.read_bytes()) for p in fake_sp.iterdir()})
    h = Hub(HubPaths.resolve(tmp_path / "hub"), runtime=FakeRuntime())
    assert integrity.check_python_env(h) is None  # lần đầu: nhận bản chuẩn
    (fake_sp / "zz_evil.pth").write_text("import os\n")
    info = integrity.check_python_env(h)
    assert info and any("zz_evil.pth" in x for x in info["added"])
    assert [e for e in h.store.query_events() if e.type == "guard.env_tamper"]
    h.store.close()


# ============================================================================ R2-2: lách repo_deny
@pytest.fixture
def repo_env(tmp_path):
    repo = tmp_path / "Workspace" / "toa"
    (repo / "scripts").mkdir(parents=True)
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    g("init", "-q")
    g("remote", "add", "origin", "git@github.com:org/toa.git")
    (repo / "package.json").write_text(json.dumps({"scripts": {
        "deploy": "bash scripts/deploy.sh", "release": "bash scripts/deploy.sh", "setup-db": "tsx --env-file=.env.local scripts/setup-nocodb.ts",
        "dev": "next dev -p 8131"}}))
    dep = repo / "scripts" / "deploy.sh"
    dep.write_text("#!/bin/bash\nnpm run build\nmv .next-build .next\n")
    dep.chmod(0o755)
    hub = tmp_path / "hub"
    ps = PolicyStore(hub, user_home=tmp_path)
    ps.ensure()
    for pat in ("Bash(npm run deploy:*)", "Bash(npm run setup-db:*)", "Bash(git push:*)", "Bash(tsx --env-file=.env.local scripts/:*)"):
        ps.repo_deny_add(str(repo), pat)
    # bảo vệ theo TÀI NGUYÊN: thư mục build prod `.next` (deploy hoán đổi vào đây) — mọi đường chạy deploy chạm nó
    (repo / ".next").mkdir()
    ps.add("path", str(repo / ".next"), ["overwrite", "write"], "build prod")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    subprocess.run(["git", "-C", str(clone), "remote", "add", "origin", "https://github.com/org/toa"], check=True)

    def ctx(cwd):
        return GuardContext(cwd, ps.load(), run_since=time.time(), git_probe=True, repo_deny=tuple(repo_deny_for(ps, str(cwd))),
                            repo_deny_lookup=lambda d: repo_deny_for(ps, d))

    return {"repo": repo, "scratch": scratch, "clone": clone, "ctx": ctx}


IN_REPO = [
    "npm run deploy", "npm run-script deploy", "npm run --silent deploy", "npm --silent run deploy", "npm --prefix . run deploy",
    "npm -s run deploy", "npm rum deploy", "npm urn deploy", "pnpm run deploy", "pnpm deploy", "yarn deploy", "yarn run deploy",
    "env npm run deploy", "command npm run deploy", "nohup npm run deploy", "time npm run deploy", "npm run release",
    "./scripts/deploy.sh", "npm run setup-db", "tsx --env-file=.env.local scripts/setup-nocodb.ts",
    "npm exec -- tsx --env-file=.env.local scripts/setup-nocodb.ts", "echo deploy | xargs npm run", "npm run \"$(echo deploy)\"",
    "node -e \"require('child_process').execSync('npm run deploy')\"",
    "git push", "git -C . push", "git --no-pager push", "git -c core.pager=cat push", "/usr/bin/git push", "command git push",
    "git send-pack origin main",
]


def test_repo_deny_variants_in_repo_held(repo_env):
    c = repo_env["ctx"](repo_env["repo"])
    missed = [(cmd, v.verdict, v.rule) for cmd in IN_REPO if not held(v := classify("Bash", {"command": cmd}, c))]
    assert not missed, missed


def test_repo_deny_from_outside_cwd_and_clones_held(repo_env):
    r = repo_env["repo"]
    c = repo_env["ctx"](repo_env["scratch"])
    for cmd in (f"git -C {r} push", f"npm --prefix {r} run deploy", f"cd {r} && git push", f"cd {r} && npm run deploy"):
        v = classify("Bash", {"command": cmd}, c)
        assert held(v), (cmd, v.verdict, v.rule)
    # clone NGOÀI repo cùng remote origin → cùng chính sách
    cc = repo_env["ctx"](repo_env["clone"])
    assert held(classify("Bash", {"command": "git push"}, cc))


def test_repo_deny_benign_dev_still_allowed(repo_env):
    c = repo_env["ctx"](repo_env["repo"])
    for cmd in ("npm run dev", "git status", "git commit -m x"):
        assert not held(classify("Bash", {"command": cmd}, c)), cmd


def test_repo_deny_forbid_prefix_is_forbidden(repo_env):
    PolicyStore(repo_env["ctx"](repo_env["repo"]).policy.hub_home).repo_deny_add(str(repo_env["repo"]), "forbid:Bash(npm run nuke:*)")
    c = repo_env["ctx"](repo_env["repo"])
    assert classify("Bash", {"command": "npm run nuke"}, c).forbidden


def test_git_credentials_not_readable_in_sandbox(tmp_path):
    hub = tmp_path / "hub"
    (hub / "policy").mkdir(parents=True)
    s = build_settings({}, run_id="r", socket=hub / "hub.sock", policy_dir=hub / "policy", guard_cfg={}, approval_timeout_s=300)
    dr = s["sandbox"]["filesystem"]["denyRead"]
    uh = os.path.expanduser("~")
    for x in (".ssh", ".config/gh", ".git-credentials", ".netrc"):
        assert f"{uh}/{x}" in dr, x
    assert os.environ.get("SSH_AUTH_SOCK", "<none>") not in (s["sandbox"]["network"].get("allowUnixSockets") or [])


def test_human_protected_resource_enters_sandbox_and_static_deny(tmp_path):
    hub = tmp_path / "hub"
    (hub / "policy").mkdir(parents=True)
    nxt = tmp_path / "Workspace" / "toa" / ".next"
    nxt.mkdir(parents=True)
    PolicyStore(hub).add("path", str(nxt), ["overwrite", "write"], "build prod")
    s = build_settings({}, run_id="r", socket=hub / "hub.sock", policy_dir=hub / "policy", guard_cfg={}, approval_timeout_s=300,
                       cwd=tmp_path / "Workspace" / "toa")
    real = os.path.realpath(nxt)
    assert real in s["sandbox"]["filesystem"]["denyWrite"]  # không bị gỡ dù cwd là repo chứa nó
    assert f"Write(/{real}/**)" in s["permissions"]["deny"]


# ============================================================================ R2-4: git config chạy lệnh
def test_git_config_exec_keys_destructive(wctx):
    for cmd in ("git config core.fsmonitor 'sh x'", "git config core.hooksPath /tmp/h", "git config core.sshCommand 'ssh -i k'",
                "git config alias.p '!sh x'", "git config filter.x.clean 'sh x'", "git config --add core.fsmonitor x"):
        assert classify("Bash", {"command": cmd}, wctx).verdict == "destructive", cmd
    assert classify("Bash", {"command": "git config user.name"}, wctx).verdict == "safe"


def test_daemon_git_disables_exec_config(tmp_path):
    """Daemon chạy git NGOÀI sandbox (checkpoint/diff): fsmonitor/hooksPath trong .git/config của agent KHÔNG được chạy."""
    from agent_hub.workspace import _git

    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    marker = tmp_path / "FSMONITOR_FIRED"
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", f"touch {marker}; false"], check=True)
    _git(repo, "status", "--porcelain")
    assert not marker.exists()


# ============================================================================ R2-5: TOCTOU workspace
def test_workspace_rechecked_at_spawn(tmp_path, monkeypatch):
    import asyncio

    import agent_hub.workspace as _ws

    p = HubPaths.resolve(tmp_path / "hub")
    p.ensure()
    root = tmp_path / "Workspace"
    root.mkdir()
    p.config.write_text(f"guard:\n  git_probe: false\nworkspace:\n  allowed_roots: ['{root}']\n")
    monkeypatch.setattr(_ws, "_EXTRA_ALLOWED_ROOTS", [])
    monkeypatch.delenv("AGENT_HUB_EXTRA_WORKSPACE_ROOTS", raising=False)
    later = root / "later"

    async def main():
        rt = FakeRuntime(hold=True)
        h = Hub(p, runtime=rt)
        h.start()
        h.agents.create({"id": "a", "name": "A", "workspace_default": str(later)})  # chưa tồn tại → hợp lệ lúc đặt
        evil = tmp_path / "evil-bin"
        evil.mkdir()
        os.symlink(evil, later)  # sau đó thành symlink ra ngoài gốc
        r = h.runs.submit("a", "x")
        for _ in range(200):
            await asyncio.sleep(0.02)
            row = h.store.get_run(r["id"])
            if row["status"] not in ("queued", "running"):
                break
        rt.release()
        await h.stop()
        return row

    row = asyncio.run(main())
    assert row["status"] in ("failed", "cancelled") and "gốc workspace" in (row.get("error") or ""), row
