"""Hồ sơ phê duyệt (docs/dev-notes/approval-profiles.md): profile strict|balanced|autonomous, ghi đè annotations MCP, luật
"luôn cho phép tool MCP", worktree do task tạo + extra_write_roots, `git reset --hard` an toàn, tự cho phép theo checkpoint,
giao điều phối viên duyệt (routed_to / approval.delegated / approval.escalated), on_timeout=deny_and_continue, `approvals stats`,
và danh sách "KHÔNG BAO GIỜ tự động" (mỗi mục một test `test_never_*`).

Không gọi claude: Hub/Daemon in-process + FakeRuntime/ScriptedRuntime; repo git tạm.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from cfharness import auth
from cfharness.errors import HubError
from cfharness.guard import profiles
from cfharness.guard.classifier import GuardContext, Verdict, classify
from cfharness.guard.policy import PolicyStore
from cfharness.hub import Hub
from cfharness.runtime import FakeRuntime
from m7_fake import MgrScript, close, make_daemon, manager_run, rpc, tool, wait_status

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
           "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *a) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, env=GIT_ENV)
    assert r.returncode == 0, (a, r.stderr)
    return r.stdout.strip()


def make_repo(base: Path) -> Path:
    """repo (nhánh main, 1 commit có src/ + sub/) + remote `origin` (bare) đã fetch → refs/remotes/origin/main chứa HEAD."""
    repo = base / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src" / "a.txt").write_text("a\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "b.txt").write_text("b\n")
    (repo / ".gitignore").write_text("*.log\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(base, "clone", "-q", "--bare", str(repo), str(base / "origin.git"))
    git(repo, "remote", "add", "origin", str(base / "origin.git"))
    git(repo, "fetch", "-q", "origin")
    return repo


def posix(p) -> str:
    return str(p).replace("\\", "/")


@pytest.fixture(autouse=True)
def _tempdir_elsewhere(tmp_path, monkeypatch):
    """Windows: thư mục tạm (TEMP) luôn là gốc ghi của tool ghi file, mà repo test lại nằm trong TEMP → trỏ tempdir sang chỗ khác."""
    import tempfile

    other = tmp_path / "_systemp"
    other.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(other))


@pytest.fixture
def hub(hub_home):
    rt = FakeRuntime(hold=True)
    h = Hub(hub_home, runtime=rt)
    h.config.set("approval.timeout_s", 0.3)
    yield h
    rt.release()


async def _run(hub, agent="w", cwd=None, **kw):
    if hub.store.get_agent(agent) is None:
        hub.agents.create({"id": agent, "name": agent.upper()})
    r = hub.runs.submit(agent, "x", cwd=str(cwd) if cwd else None, **kw)
    for _ in range(500):
        cur = hub.store.get_run(r["id"])
        if cur and cur["status"] == "running":
            return cur
        await asyncio.sleep(0.01)
    raise AssertionError("run không chạy")


async def gtest(hub, run, cmd=None, *, tool="Bash", ti=None, cwd=None):
    p = {"run_id": run["id"], "cwd": str(cwd or run["cwd"])}
    if tool == "Bash":
        p["command"] = cmd
    else:
        p["tool"], p["input"] = tool, ti
    return await hub.guard.test(p)


def ctx_for(tmp_path, **kw) -> GuardContext:
    pol = PolicyStore(tmp_path / "hubhome").load()
    return GuardContext(cwd=tmp_path, policy=pol, git_probe=False, **kw)


# ============================================================================ profile
def test_default_profile_is_balanced_and_knobs(hub):
    assert profiles.profile(hub.config) == "balanced"
    assert profiles.knob(hub.config, "guard.hold_uncertain") is False
    assert profiles.knob(hub.config, "approval.checkpoint_auto_allow") is True
    assert profiles.knob(hub.config, "approval.delegate_to_manager.enabled") is True
    assert profiles.knob(hub.config, "approval.scratch_auto_allow") is False
    hub.config.set("approval.profile", "strict")
    assert all(profiles.knob(hub.config, k) is v for k, v in
               {"guard.hold_uncertain": True, "approval.checkpoint_auto_allow": False, "approval.delegate_to_manager.enabled": False,
                "guard.safe_reset_probe": False, "guard.own_worktree_writes": False}.items())
    hub.config.set("approval.profile", "autonomous")
    assert profiles.knob(hub.config, "approval.scratch_auto_allow") is True


def test_existing_explicit_hold_uncertain_wins_over_profile(hub_home):
    hub_home.config.write_text(yaml.safe_dump({"guard": {"hold_uncertain": True}}))  # cài đặt cũ
    h = Hub(hub_home, runtime=FakeRuntime())
    assert profiles.profile(h.config) == "balanced"
    assert profiles.knob(h.config, "guard.hold_uncertain") is True
    assert h.guard.needs_human(Verdict("uncertain", "code_write_dynamic", "file", "x"))
    assert profiles.resolved(h.config)["knobs"]["guard.hold_uncertain"]["source"] == "explicit"


def test_invalid_values_rejected_and_fail_closed(hub):
    for k, v in (("approval.profile", "yolo"), ("approval.on_timeout", "deny"), ("approval.delegate_to_manager.classes", ["hub"]),
                 ("approval.delegate_to_manager.classes", ["network"]), ("approval.delegate_to_manager.timeout_s", 1), ("guard.hold_uncertain", "no"),
                 ("approval.on_timeout_by_agent", {"w": "skip"})):
        with pytest.raises(ValueError):
            hub.config.set(k, v)
    # sửa tay giá trị lạ → strict (an toàn nhất)
    data = yaml.safe_load(hub.paths.config.read_text()) or {}
    data.setdefault("approval", {})["profile"] = "yolo"
    hub.paths.config.write_text(yaml.safe_dump(data))
    hub.config.reload()
    assert profiles.profile(hub.config) == "strict"


async def test_profile_setting_is_human_only(hub_home):
    d = await make_daemon(hub_home)
    try:
        agent = auth.Caller(via="socket", pid=12345, token_ok=False, run_id="r_x", agent_id="w", evidence=["ppid:r_x"])
        with pytest.raises(HubError):
            await rpc(d, "config.set", {"key": "approval.profile", "value": "autonomous"}, as_caller=agent)
        with pytest.raises(HubError):
            await rpc(d, "config.set", {"key": "guard.hold_uncertain", "value": False}, as_caller=agent)
        assert profiles.profile(d.hub.config) == "balanced"
        assert "config.set" in auth.HUMAN_ONLY and "approval" in __import__("cfharness.guard.integrity", fromlist=["x"]).SECURITY_KEYS
        # agent gõ CLI → cấm thẳng (không mở approval)
        v = classify("Bash", {"command": "cfharness config set approval.profile autonomous"}, d.hub.guard.context_for(None))
        assert v.forbidden and v.rule == "hub_human_only"
        v = classify("Bash", {"command": "cfharness mcp annotate playwright browser_navigate --not-destructive"}, d.hub.guard.context_for(None))
        assert v.forbidden
    finally:
        await close(d)


async def test_profile_tamper_in_config_yaml_restored(tmp_path):
    from cfharness.paths import HubPaths

    p = HubPaths.resolve(tmp_path / "hub")
    p.ensure()
    p.config.write_text(yaml.safe_dump({"guard": {"git_probe": False, "integrity": {"tick_s": 0.05}}, "approval": {"profile": "strict"}}))
    h = Hub(p, runtime=FakeRuntime())
    h.start()
    try:
        await asyncio.sleep(0.3)
        data = yaml.safe_load(p.config.read_text())
        data["approval"]["profile"] = "autonomous"  # agent ghi thẳng config.yaml
        p.config.write_text(yaml.safe_dump(data))
        for _ in range(40):
            await asyncio.sleep(0.05)
            if (yaml.safe_load(p.config.read_text()).get("approval") or {}).get("profile") == "strict":
                break
        assert yaml.safe_load(p.config.read_text())["approval"]["profile"] == "strict"
    finally:
        await h.stop()


# ============================================================================ MCP: ghi đè annotations của người + luật mcp_tool
def _probe_cache(hub, name, tools):
    hub.mcp.add({"name": name, "transport": "stdio", "command": "npx", "args": ["x"], "trust": "trusted"})
    hub.store.conn.execute("UPDATE mcp_servers SET annotations_cache=?, last_probe_at='2026-01-01' WHERE name=?", (json.dumps(tools), name))


def test_mcp_annotation_override_set_by_human(hub):
    _probe_cache(hub, "playwright", {"browser_navigate": {"destructiveHint": True, "description": "Navigate to a URL"},
                                     "browser_close": {"destructiveHint": True, "description": "Close the page"},
                                     "browser_delete_cookies": {"destructiveHint": True}, "browser_run": {"destructiveHint": True}})
    hub.mcp.update("playwright", {"tools": {"require_approval": ["browser_run"]}})
    ctx = hub.guard.context_for(None)
    assert classify("mcp__playwright__browser_navigate", {"url": "https://x"}, ctx).rule == "mcp_destructive_hint"
    hub.mcp.update("playwright", {"tools": {"annotations": {"browser_*": {"destructiveHint": False}}}})
    ann = hub.mcp.tool_annotations("playwright", "browser_navigate")
    assert ann["destructiveHint"] is False and ann["human_override"] == {"destructiveHint": False}
    for t in ("browser_navigate", "browser_close"):
        v = classify(f"mcp__playwright__{t}", {}, ctx)
        assert v.verdict == "write" and not v.hold, (t, v)
    # ghi đè KHÔNG gỡ luật tên-xoá và require_approval của người
    assert classify("mcp__playwright__browser_delete_cookies", {}, ctx).rule == "mcp_delete_name"
    assert classify("mcp__playwright__browser_run", {}, ctx).rule == "mcp_require_approval"
    rules = hub.mcp.tool_rules_for_servers(["playwright"])
    assert "mcp__playwright__browser_navigate" in rules["allowed"] and "mcp__playwright__browser_run" not in rules["allowed"]
    with pytest.raises(HubError):
        hub.mcp.update("playwright", {"tools": {"annotations": {"x": {"trust": "trusted"}}}})


async def test_always_allow_mcp_tool_for_one_agent(hub):
    hub.config.set("approval.profile", "strict")
    _probe_cache(hub, "pw", {"browser_navigate": {"destructiveHint": True}, "browser_delete_all": {"destructiveHint": True}})
    run = await _run(hub, "w")
    task = asyncio.create_task(hub.guard.check({"run_id": run["id"], "tool_name": "mcp__pw__browser_navigate", "tool_input": {"url": "https://a"}, "channel": "hook"}))
    for _ in range(100):
        aps = hub.approvals.list("pending", run_id=run["id"])
        if aps:
            break
        await asyncio.sleep(0.01)
    offer = hub.guard.always_offer(aps[0])
    assert offer and offer["kind"] == "mcp_tool" and offer["confirm"] == "mcp__pw__browser_navigate" and offer["agent_id"] == "w"
    spec = hub.guard.always_prepare(aps[0]["id"])
    hub.approvals.resolve(aps[0]["id"], "allowed")
    hub.guard.always_commit(spec)
    assert (await task)["decision"] == "allow"
    res = await hub.guard.check({"run_id": run["id"], "tool_name": "mcp__pw__browser_navigate", "tool_input": {"url": "https://other"}, "channel": "hook"})
    assert res["decision"] == "allow"  # tham số khác vẫn không hỏi lại
    # agent khác / tool tên xoá → vẫn hỏi
    other = await _run(hub, "w2")
    v = classify("mcp__pw__browser_navigate", {}, hub.guard.context_for(other))
    assert v.rule == "mcp_destructive_hint"
    assert classify("mcp__pw__browser_delete_all", {}, hub.guard.context_for(run)).verdict == "destructive"  # luật chỉ cho ĐÚNG tool đó
    _probe_cache(hub, "pw2", {"remove_page": {}})  # tên xoá → không bao giờ có lựa chọn mcp_tool
    from cfharness.guard.classifier import mcp_tool_always_eligible

    assert not mcp_tool_always_eligible("mcp__pw2__remove_page", classify("mcp__pw2__remove_page", {}, hub.guard.context_for(run)))


# ============================================================================ worktree do task tạo + extra_write_roots
async def test_edit_in_own_worktree_allowed_balanced_not_strict(hub, tmp_path):
    repo = make_repo(tmp_path)
    old_wt = tmp_path / "wt_old"
    git(repo, "worktree", "add", "-q", "-b", "old", str(old_wt), "origin/main")  # worktree có TRƯỚC run (của người/ticket khác)
    time.sleep(1.2)
    run = await _run(hub, "w", cwd=repo)
    wt = tmp_path / "_wt" / "lh-1"
    git(repo, "worktree", "add", "-q", "-b", "feat", str(wt), "origin/main")
    ti = {"file_path": str(wt / "src" / "a.txt"), "old_string": "a", "new_string": "b"}
    v = await gtest(hub, run, tool="Edit", ti=ti)
    assert v["rule"] == "own_worktree_write" and v["verdict"] == "write" and not v["hold"]
    v = await gtest(hub, run, tool="Edit", ti={"file_path": str(old_wt / "src" / "a.txt"), "old_string": "a", "new_string": "b"})
    assert v["rule"] == "write_outside_workspace"
    other = make_repo(tmp_path / "other")
    wt2 = tmp_path / "_wt" / "other"
    git(other, "worktree", "add", "-q", "-b", "x", str(wt2), "origin/main")
    v = await gtest(hub, run, tool="Edit", ti={"file_path": str(wt2 / "src" / "a.txt"), "old_string": "a", "new_string": "b"})
    assert v["rule"] == "write_outside_workspace"  # worktree của repo KHÁC
    hub.config.set("approval.profile", "strict")
    v = await gtest(hub, run, tool="Edit", ti=ti)
    assert v["rule"] == "write_outside_workspace"


async def test_task_extra_write_roots_validated_and_applied(hub, tmp_path):
    repo = make_repo(tmp_path)
    hub.agents.create({"id": "w", "name": "W"})
    wt = tmp_path / "_wt" / "lh-2"  # chưa tồn tại lúc tạo task (agent tạo worktree trong run)
    t = hub.tasks.create({"title": "t", "body": "b", "assignee": "w", "workspace": {"kind": "dir", "path": str(repo), "extra_write_roots": [str(wt)]}})
    assert t["workspace"]["extra_write_roots"] == [str(wt.resolve())]
    with pytest.raises(HubError):  # ngoài workspace.allowed_roots
        hub.tasks.create({"title": "t", "body": "b", "assignee": "w", "workspace": {"kind": "dir", "path": str(repo), "extra_write_roots": [str(Path.home())]}})
    with pytest.raises(HubError):  # chứa HUB_HOME (vùng bảo vệ)
        hub.tasks.create({"title": "t", "body": "b", "assignee": "w", "workspace": {"kind": "dir", "path": str(repo), "extra_write_roots": [str(hub.paths.home)]}})
    with pytest.raises(HubError):  # tương đối
        hub.tasks.create({"title": "t", "body": "b", "assignee": "w", "workspace": {"kind": "dir", "path": str(repo), "extra_write_roots": ["rel/x"]}})
    run = await _run(hub, "w", cwd=repo, run_opts={"extra_write_roots": [str(wt.resolve())]})
    ti = {"file_path": str(wt / "f.ts"), "content": "x"}
    v = await gtest(hub, run, tool="Write", ti=ti)
    assert v["verdict"] == "write" and v["rule"] == "file_write"
    # run_opts lạ (ngoài allowed_roots) bị guard bỏ qua
    run2 = await _run(hub, "w2", run_opts={"extra_write_roots": [str(Path.home() / "Desktop")]})
    v = await gtest(hub, run2, tool="Write", ti={"file_path": str(Path.home() / "Desktop" / "x.txt"), "content": "x"})
    assert v["rule"] == "write_outside_workspace"


def test_dispatcher_worktree_kind_keeps_repo_path(hub, tmp_path):
    repo = make_repo(tmp_path)
    path, kind = hub.dispatcher._workspace({"workspace": {"kind": "worktree", "path": str(repo), "name": "x"}})
    assert (path, kind) == (str(repo), "worktree")
    assert hub.dispatcher._workspace({"workspace": {"kind": "scratch"}}) == (None, "scratch")


# ============================================================================ git reset --hard trong worktree
async def test_reset_hard_safe_in_clean_worktree_only(hub, tmp_path):
    repo = make_repo(tmp_path)
    run = await _run(hub, "w", cwd=repo)
    wt = tmp_path / "_wt" / "r"
    git(repo, "worktree", "add", "-q", "-b", "feat", str(wt), "origin/main")
    (repo / "src" / "a.txt").write_text("changed\n")
    git(repo, "commit", "-qam", "c2")
    git(repo, "push", "-q", "origin", "main")
    git(repo, "fetch", "-q", "origin")
    cmd = f"git -C {posix(wt)} reset -q --hard origin/main"
    v = await gtest(hub, run, cmd)
    assert v["rule"] == "git_reset_hard_safe" and v["verdict"] == "write", v
    # dirty (untracked) → giữ
    (wt / "new.txt").write_text("x")
    assert (await gtest(hub, run, cmd))["rule"] == "git_reset_hard"
    (wt / "new.txt").unlink()
    # commit chỉ có trên nhánh hiện tại → mất commit → giữ
    (wt / "src" / "a.txt").write_text("mine\n")
    git(wt, "commit", "-qam", "local only")
    assert (await gtest(hub, run, cmd))["rule"] == "git_reset_hard"
    # repo chính (không phải worktree liên kết / scratch) → giữ
    assert (await gtest(hub, run, "git reset --hard HEAD~1"))["rule"] == "git_reset_hard"
    hub.config.set("approval.profile", "strict")
    git(wt, "reset", "-q", "--hard", "origin/main")
    assert (await gtest(hub, run, cmd))["rule"] == "git_reset_hard"


# ============================================================================ tự cho phép theo checkpoint
async def test_checkpoint_revertible_delete_auto_allowed(hub, tmp_path):
    repo = make_repo(tmp_path)
    (repo / "logs").mkdir()
    (repo / "logs" / "keep.log").write_text("ignored, không có trong checkpoint")
    (repo / "sub" / "dirty.txt").write_text("untracked lúc checkpoint")  # được lưu trong checkpoint (status)
    time.sleep(1.2)
    run = await _run(hub, "w", cwd=repo)
    assert (hub.store.get_run(run["id"])["checkpoint"] or {}).get("kind") == "git"
    v = await gtest(hub, run, "rm -rf sub")
    assert v["verdict"] == "destructive" and not v["hold"] and v["auto_allow"]["by"] == "checkpoint", v
    res = await hub.guard.check({"run_id": run["id"], "tool_name": "Bash", "tool_input": {"command": "rm -rf sub"}, "channel": "hook"})
    assert res["decision"] == "allow" and res["decided_by"] == "checkpoint"
    row = hub.store.conn.execute("SELECT classifier, action FROM guard_decisions WHERE run_id=? ORDER BY id DESC LIMIT 1", (run["id"],)).fetchone()
    assert tuple(row) == ("checkpoint", "allow")
    v = await gtest(hub, run, "rm -rf logs")  # file bị ignore: không revert được → hỏi người
    assert v["hold"] and v["auto_allow"] is None
    hub.config.set("approval.profile", "strict")
    assert (await gtest(hub, run, "rm -rf sub"))["hold"]


# ============================================================================ KHÔNG BAO GIỜ tự động (mọi profile) — mỗi mục một test
ROOTS = ["c:/allowed" if os.name == "nt" else "/allowed"]
INSIDE = "C:/allowed/repo/x" if os.name == "nt" else "/allowed/repo/x"


def _never(v, **kw):
    return profiles.never_automatic(v, roots=ROOTS, cwd=INSIDE, **kw)


def _assert_never(hub, v, **kw):
    assert _never(v, **kw) is not None
    hub.config.set("approval.profile", "autonomous")  # profile rộng nhất
    hub.config.set("approval.delegate_to_manager.classes", list(profiles.DELEGABLE_CLASSES))
    assert profiles.delegable(v, hub.config, roots=ROOTS, cwd=INSIDE, **kw) is not None


def test_never_protected_resource(hub):
    _assert_never(hub, Verdict("destructive", "delete_not_run_created", "file", "x", [INSIDE], protected_id="env-files"))
    _assert_never(hub, Verdict("destructive", "protected_delete", "file", "x", [INSIDE]))


def test_never_hub_policy_secrets_zone(hub):
    for v in (Verdict("destructive", "policy_write", "policy", "x", [INSIDE]), Verdict("destructive", "hub_admin", "hub", "x"),
              Verdict("destructive", "hub_secret_read", "hub", "x", forbidden=True), Verdict("uncertain", "sensitive_dynamic", "hub", "x")):
        _assert_never(hub, v)
    assert hub.guard.needs_human(Verdict("uncertain", "sensitive_dynamic", "hub", "x"))  # uncertain vùng hub vẫn giữ dù không giữ uncertain


def test_never_forbidden_verdict(hub):
    _assert_never(hub, Verdict("destructive", "delete_not_run_created", "file", "x", [INSIDE], forbidden=True))


def test_never_git_push_force_push_remote_change(hub):
    for rule in ("git_push_force_or_delete", "git_push_refspec", "protected_branch_push", "git_remote_change", "git_push"):
        _assert_never(hub, Verdict("destructive", rule, "git", "x", [INSIDE]))


def test_never_paths_outside_allowed_roots(hub):
    out = "D:/elsewhere/f" if os.name == "nt" else "/elsewhere/f"
    _assert_never(hub, Verdict("destructive", "write_outside_workspace", "file", "x", [out]))
    _assert_never(hub, Verdict("destructive", "delete_not_run_created", "file", "x", []))  # đích không xác định
    assert profiles.never_automatic(Verdict("destructive", "git_discard_changes", "git", "x"), roots=ROOTS, cwd=out) is not None


def test_never_credential_access(hub):
    for rule in ("git_credential", "keychain_access", "keychain_delete", "forge_token_access"):
        _assert_never(hub, Verdict("destructive", rule, "system", "x", [INSIDE]))


def test_never_sandbox_exception(hub):
    _assert_never(hub, Verdict("destructive", "delete_not_run_created", "file", "x", [INSIDE]), tool_name="Bash",
                  tool_input={"command": "rm -rf x", "dangerouslyDisableSandbox": True})


def test_never_mcp_require_approval_or_network(hub):
    _assert_never(hub, Verdict("uncertain", "mcp_require_approval", "mcp", "x"), tool_name="mcp__s__t")
    _assert_never(hub, Verdict("uncertain", "web_domain_not_allowed", "network", "x"))


async def test_never_while_estop(hub, tmp_path):
    repo = make_repo(tmp_path)
    run = await _run(hub, "w", cwd=repo)
    v = Verdict("destructive", "delete_not_run_created", "file", "x", [str(repo / "sub")])
    ctx = hub.guard.context_for(run)
    assert hub.guard.auto_allow(run, "Bash", {}, v, ctx) is not None
    hub.store.state_set("estop", {"on": True})
    try:
        assert hub.guard.auto_allow(run, "Bash", {}, v, ctx) is None
        assert hub.approvals.manager_ready("w") is None  # không giao điều phối viên khi ESTOP
    finally:
        hub.store.state_set("estop", {"on": False})


async def test_never_auto_end_to_end_push_in_checkpointed_repo(hub, tmp_path):
    """Classifier thật: git push --force trong repo có checkpoint của run vẫn giữ chờ người ở profile autonomous."""
    repo = make_repo(tmp_path)
    run = await _run(hub, "w", cwd=repo)
    hub.config.set("approval.profile", "autonomous")
    v = await gtest(hub, run, "git push --force origin main")
    assert v["hold"] and v["never_automatic"]


# ============================================================================ giao điều phối viên
class Script(MgrScript):
    def __call__(self, spec):
        if spec.prompt.startswith("HOLD"):
            self.specs.append(spec)
            return {"text": "x", "cost": 0.001, "hold": True}
        return super().__call__(spec)


async def _worker_run(d, agent="w1", cwd=None):
    if d.hub.store.get_agent(agent) is None:
        d.hub.agents.create({"id": agent, "name": agent, "role": "dev", "model": "haiku"})
    r = d.hub.runs.submit(agent, "HOLD work", cwd=str(cwd) if cwd else None)
    return await wait_status(d, r["id"])


async def _open(d, run, path):
    task = asyncio.create_task(d.hub.guard.check({"run_id": run["id"], "tool_name": "Write", "tool_input": {"file_path": str(path), "content": "x"},
                                                  "channel": "hook"}))
    for _ in range(300):
        aps = d.hub.approvals.list("pending", run_id=run["id"])
        if aps:
            return task, aps[0]
        await asyncio.sleep(0.01)
    raise AssertionError("approval không mở")


@pytest.fixture
async def mgr_env(hub_home, tmp_path):
    d = await make_daemon(hub_home, Script())
    d.hub.config.set("approval.timeout_s", 20)
    d.hub.config.set("guard.git_probe", False)
    (tmp_path / "work").mkdir()
    (tmp_path / "other").mkdir()
    yield d, tmp_path
    await close(d)


async def test_delegated_approval_decided_by_manager(mgr_env):
    d, tmp = mgr_env
    sub = d.hub.bus.subscribe(level="summary")
    m = await manager_run(d)
    run = await _worker_run(d, cwd=tmp / "work")
    task, ap = await _open(d, run, tmp / "other" / "f.txt")  # ngoài workspace, trong allowed_roots, lớp file
    assert ap["routed_to"] == "manager" and ap["delegated_at"]
    assert any(e["kind"] == "approval_delegated" and e.get("approval_id") == ap["id"] for e in d.hub.manager.pending)
    # chưa làm phiền người: notifier bỏ qua approval.open của approval đang giao
    from cfharness.events import make_event

    assert d.hub.notifier._delegated(make_event("approval.open", "w1", {"request_id": ap["id"]}))
    lst = await tool(d, m["run_id"], "approval_list")
    assert [x["id"] for x in lst] == [ap["id"]] and lst[0]["targets"]
    res = await tool(d, m["run_id"], "approval_decide", {"approval_id": ap["id"], "decision": "allow", "reason": "writes the task's own output file"})
    assert res["status"] == "allowed"
    assert (await asyncio.wait_for(task, 10))["decision"] == "allow"
    got = d.hub.approvals.get(ap["id"])
    assert got["resolved_by"] == "manager:boss" and got["resolved_channel"] == "manager" and "writes the task" in got["note"]
    au = d.hub.store.conn.execute("SELECT actor FROM audit_log WHERE kind='approval.resolved' ORDER BY id DESC LIMIT 1").fetchone()
    assert au[0] == "manager:boss"
    # escalation → người; điều phối viên không quyết được nữa
    task2, ap2 = await _open(d, run, tmp / "other" / "g.txt")
    await tool(d, m["run_id"], "approval_escalate", {"approval_id": ap2["id"], "note": "không chắc"})
    assert d.hub.approvals.get(ap2["id"])["routed_to"] == "human"
    assert not d.hub.notifier._delegated(make_event("approval.open", "w1", {"request_id": ap2["id"]}))
    with pytest.raises(HubError):
        await tool(d, m["run_id"], "approval_decide", {"approval_id": ap2["id"], "decision": "allow", "reason": "x"})
    d.hub.approvals.resolve(ap2["id"], "denied", note="người từ chối")  # người quyết bất kỳ lúc nào
    assert (await asyncio.wait_for(task2, 10))["decision"] == "deny"
    # hợp đồng với chat UI: cả hai event đến subscriber mức summary, có routed_to
    seen = {}
    while not sub.queue.empty():
        ev = sub.queue.get_nowait()
        if ev is not None and ev.type in ("approval.delegated", "approval.escalated"):
            seen.setdefault(ev.type, ev.payload)
    assert seen["approval.delegated"]["routed_to"] == "manager" and seen["approval.escalated"]["routed_to"] == "human"


async def test_delegation_timeout_escalates_to_human(mgr_env):
    d, tmp = mgr_env
    await manager_run(d)
    run = await _worker_run(d, cwd=tmp / "work")
    task, ap = await _open(d, run, tmp / "other" / "f.txt")
    assert ap["routed_to"] == "manager"
    assert d.hub.approvals.check_delegations(now=time.time() + 5) == []
    assert d.hub.approvals.check_delegations(now=time.time() + d.hub.approvals.delegation_timeout_s() + 1) == [ap["id"]]
    row = d.hub.approvals.get(ap["id"])
    assert row["routed_to"] == "human" and row["escalated_at"] and "phút" in row["escalation_note"]
    evs = [e for e in d.hub.store.query_events(run_id=run["id"], limit=200) if e.type == "approval.escalated"]
    assert evs and evs[-1].payload["by"] == "hub"
    d.hub.approvals.resolve(ap["id"], "allowed")
    await asyncio.wait_for(task, 10)


async def test_manager_never_decides_own_or_human_or_undelegable(mgr_env):
    d, tmp = mgr_env
    m = await manager_run(d)
    # approval của chính run điều phối viên → người
    mrun = d.hub.store.get_run(m["run_id"])
    v = Verdict("destructive", "write_outside_workspace", "file", "x", [str(tmp / "other" / "f")])
    assert d.hub.guard.route_for(mrun, "Write", {}, v, d.hub.guard.context_for(mrun))[0] == "human"
    # lớp ngoài danh sách giao (db) → người
    run = await _worker_run(d, cwd=tmp / "work")
    ctx = d.hub.guard.context_for(run)
    assert d.hub.guard.route_for(run, "Bash", {}, Verdict("destructive", "sql_drop", "db", "x", [str(tmp / "work")]), ctx)[0] == "human"
    assert d.hub.guard.route_for(run, "Bash", {}, Verdict("destructive", "git_push_force_or_delete", "git", "x", [str(tmp / "work")]), ctx)[0] == "human"
    # strict → không giao
    d.hub.config.set("approval.profile", "strict")
    task, ap = await _open(d, run, tmp / "other" / "h.txt")
    assert ap["routed_to"] == "human"
    for args in ({"approval_id": ap["id"], "decision": "allow", "reason": "x"}, {"approval_id": ap["id"], "decision": "deny", "reason": "x"}):
        with pytest.raises(HubError):
            await tool(d, m["run_id"], "approval_decide", args)
    # lớp 2 (daemon) với người gọi manager: approval người / `always`
    caller = auth.manager_caller("boss", m["run_id"])
    for p in ({"id": ap["id"], "decision": "allow", "reason": "x"}, {"id": ap["id"], "decision": "allow", "reason": "x", "always": True}):
        with pytest.raises(HubError):
            await rpc(d, "approval.manager_decide", p, as_caller=caller)
    # agent thường không gọi được (HUMAN_ONLY); người cũng không dùng đường này
    agent = auth.Caller(via="socket", pid=1, token_ok=False, run_id=run["id"], agent_id="w1", evidence=["ppid"])
    with pytest.raises(HubError):
        await rpc(d, "approval.manager_decide", {"id": ap["id"], "decision": "allow", "reason": "x"}, as_caller=agent)
    with pytest.raises(HubError):
        await rpc(d, "approval.manager_decide", {"id": ap["id"], "decision": "allow", "reason": "x"})
    d.hub.approvals.resolve(ap["id"], "denied")
    await asyncio.wait_for(task, 10)


# ============================================================================ on_timeout = deny_and_continue
async def test_deny_and_continue_then_late_approval_resumes(hub):
    hub.config.set("approval.profile", "strict")
    hub.config.set("approval.on_timeout_by_agent", {"w": "deny_and_continue"})
    run = await _run(hub, "w")
    res = await hub.guard.check({"run_id": run["id"], "tool_name": "Bash", "tool_input": {"command": "eval \"$CMD\""}, "channel": "hook"})
    assert res["decision"] == "deny" and res.get("continued") and "run continues" in res["reason"] and not res.get("waiting_human")
    ap = hub.approvals.get(res["approval_id"])
    assert ap["status"] == "waiting_human" and ap["timeout_action"] == "deny_and_continue"
    assert hub.store.get_run(run["id"])["status"] == "running"  # KHÔNG tạm dừng
    # thao tác y hệt lần nữa → từ chối ngay (không giữ lại 0.3 s)
    res2 = await hub.guard.check({"run_id": run["id"], "tool_name": "Bash", "tool_input": {"command": "eval \"$CMD\""}, "channel": "hook"})
    assert res2["decision"] == "deny" and res2["approval_id"] == ap["id"]
    hub.approvals.resolve(ap["id"], "allowed")
    hub.runs.runtime().release(run["id"])
    for _ in range(500):
        if hub.approvals.get(ap["id"]).get("resume_run_id"):
            break
        await asyncio.sleep(0.02)
    new = hub.store.get_run(hub.approvals.get(ap["id"])["resume_run_id"])
    assert new["resume_reason"] == "approval" and new["session_id"] == run["session_id"]
    # agent khác: mặc định wait (Q-04)
    assert profiles.on_timeout(hub.config, "other") == "wait"


# ============================================================================ thống kê
async def test_approval_stats_groups(hub):
    from cfharness.approvals import approval_stats

    hub.config.set("approval.profile", "strict")
    run = await _run(hub, "w")
    for cmd in ("eval \"$A\"", "eval \"$B\""):
        await hub.guard.check({"run_id": run["id"], "tool_name": "Bash", "tool_input": {"command": cmd}, "channel": "hook"})
    aps = hub.approvals.list("pending", run_id=run["id"])
    hub.approvals.resolve(aps[0]["id"], "denied")
    st = approval_stats(hub, 30)
    assert st["total"] == 2 and st["pending"] == 1
    assert st["by_rule"][0]["key"] == "eval_dynamic" and st["by_rule"][0]["count"] == 2
    assert {r["key"] for r in st["by_agent"]} == {"w"} and {r["key"] for r in st["by_tool"]} == {"Bash"}
    assert {r["key"]: r["count"] for r in st["by_outcome"]} == {"denied": 1, "waiting_human": 1}
    assert st["wait"]["median_s"] is not None and sum(st["wait"]["buckets"].values()) == 2


def test_cli_rewrites_approvals_stats_and_profile():
    from cfharness.cli import build_parser
    from cfharness.guard.cli import rewrite_argv

    assert rewrite_argv(["approvals", "stats", "--days", "7"]) == ["approvals-stats", "--days", "7"]
    assert rewrite_argv(["--json", "approvals", "profile"]) == ["--json", "approvals-profile"]
    a = build_parser().parse_args(rewrite_argv(["approvals", "stats", "--days", "7"]))
    assert a.days == 7
    a = build_parser().parse_args(["mcp", "annotate", "playwright", "browser_*", "--not-destructive"])
    assert a.not_destructive and a.tool == "browser_*"
