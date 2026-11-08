"""P2 đa instance — giam agent trong workspace W (`workspace.confine`, docs/dev-notes/confine.md).

Không gọi claude thật: Hub in-process + FakeRuntime; `HOME` trỏ vào thư mục tạm (mô phỏng $HOME của người); registry hub tạm.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

from cfharness import auth, confine
from cfharness.errors import BLOCKED, HubError
from cfharness.guard.classifier import GuardContext, classify
from cfharness.guard.policy import PolicyStore
from cfharness.guard.runtime import build_settings
from cfharness.hub import Hub
from cfharness.runtime import FakeRuntime
from cfharness.workspace import validate_workspace


@pytest.fixture
def fakehome(tmp_path, monkeypatch):
    """$HOME giả: W, dự án khác, tài liệu riêng, toolchain."""
    home = tmp_path / "home"
    for d in ("Workspace/du-an-A/src", "Workspace/du-an-B", "Documents", ".npm", ".ssh"):
        (home / d).mkdir(parents=True)
    (home / "Workspace/du-an-A/src/app.py").write_text("print(1)")
    (home / "Workspace/du-an-B/.env").write_text("SECRET=fake")
    (home / "Documents/private.txt").write_text("x")
    monkeypatch.setenv("HOME", str(home))
    # thư mục tạm của pytest chứa $HOME giả → đặt thư mục tạm hệ thống riêng (như máy thật: $TMPDIR không nằm trong $HOME)
    import tempfile

    systmp = tmp_path / "systmp"
    systmp.mkdir()
    for k in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.setenv(k, str(systmp))
    monkeypatch.setattr(tempfile, "tempdir", str(systmp))
    return home


@pytest.fixture
def hub(hub_home, fakehome):
    rt = FakeRuntime(hold=True)
    h = Hub(hub_home, runtime=rt)
    h.config.set("approval.timeout_s", 0.3)
    yield h
    rt.release()
    h.store.close()


def W(home: Path) -> Path:
    return home / "Workspace" / "du-an-A"


def _confine(hub, home):
    hub.config.set("workspace.confine.root", str(W(home)))


# ============================================================================ cấu hình
def test_config_default_off_and_validation(hub, fakehome):
    assert hub.config.get("workspace.confine.root") is None and confine.of_hub(hub) is None
    assert "~/.npm" in hub.config.get("workspace.confine.read_allow")
    for bad in ("/", str(fakehome), "relative/dir", str(fakehome.parent), str(hub.paths.home), str(hub.paths.home / "agents")):
        with pytest.raises(ValueError):
            hub.config.set("workspace.confine.root", bad)
    with pytest.raises(ValueError):
        hub.config.set("workspace.confine.read_allow", [str(fakehome)])  # quá rộng
    with pytest.raises(ValueError):
        hub.config.set("workspace.confine", {"root": str(W(fakehome)), "oops": 1})
    _confine(hub, fakehome)
    c = confine.of_hub(hub)
    assert c.root == os.path.realpath(W(fakehome)) and os.path.realpath(fakehome / ".npm") in c.read_allow


def test_human_only_and_integrity_protected(hub, fakehome):
    from cfharness.guard import integrity as integ
    from cfharness.web import settings_schema

    assert "config.set" in auth.HUMAN_ONLY and "config.set" not in auth.MANAGER_ALLOWED
    assert "workspace.confine" in integ.SECURITY_KEYS
    assert "workspace.confine.root" not in settings_schema.BY_KEY  # không sửa được từ web (chỉ hiển thị)
    assert any(k == "workspace.confine.root" for k, _ in settings_schema.READONLY)
    _confine(hub, fakehome)
    hub.integrity.check()
    # agent (giả sử lách sandbox) sửa tay config.yaml để gỡ giam → bị khôi phục; khoá thường sửa tay vẫn được nhận
    import yaml

    p = hub.paths.home / "config.yaml"
    d = yaml.safe_load(p.read_text())
    d["workspace"]["confine"]["root"] = None
    d["workspace"]["max_files"] = 123
    p.write_text(yaml.safe_dump(d))
    res = hub.integrity.check()
    assert "workspace.confine" in (res.get("config") or {}).get("keys", []), res
    d2 = yaml.safe_load(p.read_text())
    assert d2["workspace"]["confine"]["root"] == str(W(fakehome)) and d2["workspace"]["max_files"] == 123


# ============================================================================ validate_workspace
def test_validate_workspace_red_green(hub, fakehome):
    other = fakehome / "Workspace" / "du-an-B"
    hub.config.set("workspace.allowed_roots", [str(fakehome / "Workspace")])
    validate_workspace(hub, {"kind": "dir", "path": str(other)}, "ws")  # chưa giam: được
    _confine(hub, fakehome)
    with pytest.raises(HubError, match="giam"):
        validate_workspace(hub, {"kind": "dir", "path": str(other)}, "ws")
    with pytest.raises(HubError, match="giam"):
        validate_workspace(hub, {"kind": "dir", "path": str(fakehome / "Documents")}, "ws")
    validate_workspace(hub, {"kind": "dir", "path": str(W(fakehome) / "src")}, "ws")
    validate_workspace(hub, {"kind": "dir", "path": str(hub.paths.home / "agents" / "a" / "workspaces" / "r1")}, "ws")
    # agent: workspace_default ngoài W bị từ chối ở tầng service
    with pytest.raises(HubError):
        hub.agents.create({"id": "x", "name": "X", "workspace_default": str(other)})
    hub.agents.create({"id": "y", "name": "Y", "workspace_default": str(W(fakehome))})
    # run ad-hoc cwd ngoài W
    with pytest.raises(HubError):
        hub.runs.submit("y", "hi", cwd=str(other))


def test_task_extra_write_roots_and_worktree_inside_root(hub, fakehome):
    _confine(hub, fakehome)
    hub.agents.create({"id": "w", "name": "W"})
    with pytest.raises(HubError):
        hub.tasks.create({"title": "t", "assignee": "w", "workspace": {"kind": "dir", "path": str(W(fakehome)),
                                                                      "extra_write_roots": [str(fakehome / "Workspace" / "du-an-B")]}})


def test_systems_roots_default_to_confine_root(hub, fakehome):
    _confine(hub, fakehome)
    assert hub.systems.roots() == [os.path.normpath(str(W(fakehome)))]
    hub.config.set("systems.roots", [str(fakehome / "Workspace")])
    assert hub.systems.roots() == [os.path.normpath(str(fakehome / "Workspace"))]


# ============================================================================ sandbox settings
def _settings(hub, fakehome, *, trust=False, sandbox=True, cwd=None):
    g = dict(hub.config.get("guard") or {})
    g["sandbox"] = {**(g.get("sandbox") or {}), "enabled": sandbox}
    return build_settings({}, run_id="r", socket=hub.paths.socket, policy_dir=hub.paths.home / "policy", guard_cfg=g, approval_timeout_s=300,
                          cwd=cwd or W(fakehome), trust_full=trust, confine=confine.of_hub(hub))


def test_sandbox_settings_option_a(hub, fakehome):
    s0 = _settings(hub, fakehome)
    assert "allowRead" not in s0["sandbox"]["filesystem"]  # chưa giam: như cũ
    _confine(hub, fakehome)
    hub.agents.create({"id": "w", "name": "W"})
    (hub.paths.home / "agents" / "w" / "workspaces").mkdir(parents=True, exist_ok=True)
    s = _settings(hub, fakehome)
    fs = s["sandbox"]["filesystem"]
    uh = os.path.realpath(fakehome)
    assert uh in fs["denyRead"]
    ar = fs["allowRead"]
    assert os.path.realpath(W(fakehome)) in ar and os.path.realpath(fakehome / ".npm") in ar
    assert str(hub.paths.home / "agents" / "w" / "workspaces") in ar  # glob không được sandbox mở lại → liệt kê
    assert not any("*" in x for x in ar)
    # secret của hub vẫn ở denyRead (mục cụ thể hơn thắng allowRead — spike)
    assert any(x.endswith("/secrets") for x in fs["denyRead"])
    # lớp tĩnh permissions.deny: anh em của W + mục ở $HOME, không chạm W/toolchain
    deny = s["permissions"]["deny"]
    b = os.path.realpath(fakehome / "Workspace" / "du-an-B")
    assert f"Read(/{b}/**)" in deny and f"Edit(/{b}/**)" in deny
    assert f"Read(/{os.path.realpath(fakehome / 'Documents')}/**)" in deny
    assert not any(os.path.realpath(W(fakehome)) + ")" in x or os.path.realpath(W(fakehome)) + "/**)" in x for x in deny)
    assert not any(f"/{os.path.realpath(fakehome / '.npm')}/**)" in x for x in deny)


def test_trust_full_still_confined(hub, fakehome):
    s = _settings(hub, fakehome, trust=True)
    assert s["sandbox"] == {"enabled": False}  # chưa giam: ngữ nghĩa trust cũ
    _confine(hub, fakehome)
    s = _settings(hub, fakehome, trust=True)
    sb = s["sandbox"]
    assert sb["enabled"] is True and sb["allowUnsandboxedCommands"] is False
    assert os.path.realpath(fakehome) in sb["filesystem"]["denyRead"]
    assert os.path.realpath(W(fakehome)) in sb["filesystem"]["allowRead"] and os.path.realpath(W(fakehome)) in sb["filesystem"]["allowWrite"]
    assert sb["network"]["allowedDomains"] == ["*"]  # trust: mạng mở
    assert str(hub.paths.socket) in sb["filesystem"]["denyWrite"]
    assert any("du-an-B" in x for x in s["permissions"]["deny"])
    # guard.sandbox.enabled=false + giam → vẫn có sandbox chỉ-giam
    s = _settings(hub, fakehome, sandbox=False)
    assert s["sandbox"]["enabled"] is True and os.path.realpath(fakehome) in s["sandbox"]["filesystem"]["denyRead"]


async def test_trust_full_spec_and_guard_check_confined(hub, fakehome):
    _confine(hub, fakehome)
    hub.agents.create({"id": "w", "name": "W"})
    hub.guard.trust_set("w", "full")
    run = hub.store.get_run(hub.runs.submit("w", "hi", cwd=str(W(fakehome)))["id"])
    spec = hub.runs._prepare_spec(run, hub.store.get_agent("w"))
    assert spec.trust_full
    st = json.loads(spec.settings_path.read_text(encoding="utf-8"))
    assert st["sandbox"]["enabled"] is True and os.path.realpath(fakehome) in st["sandbox"]["filesystem"]["denyRead"]
    # hook guard: trust cho phép trong W, cấm ngoài W
    for _ in range(500):
        cur = hub.store.get_run(run["id"])
        if cur and cur["status"] == "running":
            break
        await asyncio.sleep(0.01)

    async def chk(tool, ti):
        return await asyncio.wait_for(hub.guard.check({"run_id": run["id"], "tool_name": tool, "tool_input": ti, "channel": "hook"}), 20)

    assert (await chk("Read", {"file_path": str(W(fakehome) / "src" / "app.py")}))["decision"] == "allow"
    assert (await chk("Bash", {"command": "git push --force origin main"}))["decision"] == "allow"  # trust trong W như cũ
    r = await chk("Read", {"file_path": str(fakehome / "Workspace" / "du-an-B" / ".env")})
    assert r["decision"] == "deny" and "giam" in r["reason"]
    r = await chk("Write", {"file_path": str(fakehome / "Documents" / "x.txt"), "content": "x"})
    assert r["decision"] == "deny"


# ============================================================================ classifier
@pytest.fixture
def cctx(tmp_path, fakehome):
    hubdir = tmp_path / "hubc"
    hubdir.mkdir()
    ps = PolicyStore(hubdir, user_home=fakehome)
    ps.ensure()
    w = os.path.realpath(W(fakehome))
    conf = confine.Confine(root=w, read_allow=(os.path.realpath(fakehome / ".npm"),))
    return GuardContext(Path(w), ps.load(), git_probe=False, confine_root=w,
                        confine_read=tuple(confine.read_roots(conf, hubdir, cwd=w)), confine_deny=tuple(confine.deny_read_roots(conf)))


def test_classifier_read_outside_forbidden(cctx, fakehome):
    bad = [
        ("Read", {"file_path": str(fakehome / "Workspace/du-an-B/.env")}),
        ("Read", {"file_path": "../du-an-B/.env"}),
        ("Read", {"file_path": "~/Documents/private.txt"}),
        ("Grep", {"pattern": "SECRET", "path": str(fakehome / "Workspace")}),
        ("Glob", {"pattern": str(fakehome) + "/**/*.env"}),
        ("Glob", {"pattern": "../**/.env"}),
        ("LS", {"path": str(fakehome)}),
    ]
    for tool, ti in bad:
        v = classify(tool, ti, cctx)
        assert v.forbidden and v.rule == "read_outside_workspace", (tool, ti, v)
    ok = [
        ("Read", {"file_path": "src/app.py"}),
        ("Read", {"file_path": str(W(fakehome) / "src/app.py")}),
        ("Grep", {"pattern": "x"}),
        ("Glob", {"pattern": "**/*.py"}),
        ("Read", {"file_path": str(fakehome / ".npm" / "_cacache" / "x")}),
        ("Read", {"file_path": "/usr/include/stdio.h"}),
        ("Read", {"file_path": "/tmp/x.log"}),
    ]
    for tool, ti in ok:
        v = classify(tool, ti, cctx)
        assert not v.forbidden and v.verdict == "safe", (tool, ti, v)


def test_classifier_symlink_out_of_root_forbidden(cctx, fakehome):
    link = W(fakehome) / "docs-link"
    link.symlink_to(fakehome / "Documents")
    v = classify("Read", {"file_path": str(link / "private.txt")}, cctx)
    assert v.forbidden and v.rule == "read_outside_workspace"


def test_classifier_write_outside_root_forbidden(cctx, fakehome, tmp_path):
    v = classify("Write", {"file_path": str(fakehome / "Workspace/du-an-B/x.py"), "content": "x"}, cctx)
    assert v.forbidden and v.rule == "write_outside_workspace"
    # trong W nhưng ngoài cwd → vẫn hỏi người (không cấm)
    sub = W(fakehome) / "src"
    ctx2 = GuardContext(sub, cctx.policy, git_probe=False, confine_root=cctx.confine_root, confine_read=cctx.confine_read, confine_deny=cctx.confine_deny)
    v = classify("Write", {"file_path": str(W(fakehome) / "README.md"), "content": "x"}, ctx2)
    assert v.verdict == "destructive" and not v.forbidden and v.rule == "write_outside_workspace"
    # không giam → hành vi cũ (destructive, duyệt được)
    plain = GuardContext(Path(cctx.confine_root), cctx.policy, git_probe=False)
    v = classify("Write", {"file_path": str(fakehome / "Workspace/du-an-B/x.py"), "content": "x"}, plain)
    in_tmp = any(str(fakehome.resolve()).startswith(t.rstrip("/") + "/") for t in ("/tmp", "/private/tmp", os.path.realpath(tempfile.gettempdir())))
    # không giam, ghi vào thư mục tạm = `write` như cũ (Linux: tmp_path nằm trong /tmp)
    assert (v.verdict == "write") if in_tmp else (v.verdict == "destructive" and not v.forbidden)
    assert classify("Write", {"file_path": "src/new.py", "content": "x"}, cctx).verdict == "write"
    assert classify("Write", {"file_path": "/tmp/scratch.txt", "content": "x"}, cctx).verdict == "write"


async def test_guard_context_from_hub(hub, fakehome):
    _confine(hub, fakehome)
    ctx = hub.guard.context_for(None, cwd=str(W(fakehome)))
    assert ctx.confine_root == os.path.realpath(W(fakehome))
    assert classify("Read", {"file_path": str(fakehome / "Documents/private.txt")}, ctx).rule == "read_outside_workspace"


# ============================================================================ manager
async def test_manager_check_path_and_config(hub_home, fakehome):
    from m7_fake import close, make_daemon, manager_run, rpc

    d = await make_daemon(hub_home)
    try:
        assert os.path.realpath(hub_home.home) in confine.registered_homes()  # Hub.start tự đăng ký (P2.4)
        res = await manager_run(d)
        d.hub.config.set("workspace.allowed_roots", [str(fakehome / "Workspace")])
        perms = d.hub.manager.perms
        other = str(fakehome / "Workspace" / "du-an-B")
        assert perms.check_path(other, "workspace", agent_id="w1") is None
        d.hub.config.set("workspace.confine.root", str(W(fakehome)))
        assert "giam" in (perms.check_path(other, "workspace", agent_id="w1") or "")
        assert perms.check_path(str(W(fakehome)), "workspace", agent_id="w1") is None
        with pytest.raises(HubError) as ei:
            await rpc(d, "config.set", {"key": "workspace.confine.root", "value": None}, as_caller=auth.manager_caller("boss", res["run_id"]))
        assert ei.value.code == BLOCKED
        assert d.hub.config.get("workspace.confine.root") == str(W(fakehome))
    finally:
        await close(d)


# ============================================================================ registry hub (P2.4)
def test_hub_registry_and_other_hubs_denied(tmp_path, monkeypatch, fakehome):
    reg = tmp_path / "reg" / "hubs.json"
    monkeypatch.setenv("CFHARNESS_HUBS_REGISTRY", str(reg))
    a, b = tmp_path / "hubA", tmp_path / "hubB"
    a.mkdir()
    b.mkdir()
    confine.register_home(a)
    confine.register_home(b)
    confine.register_home(a)  # lặp lại không nhân bản
    data = json.loads(reg.read_text())
    assert sorted(h["home"] for h in data["hubs"]) == sorted([os.path.realpath(a), os.path.realpath(b)])
    assert stat.S_IMODE(reg.stat().st_mode) == 0o600
    assert confine.other_hub_homes(a) == [os.path.realpath(b)]
    # sandbox của hub A chặn đọc + ghi home hub B
    (a / "policy").mkdir()
    s = build_settings({}, run_id="r", socket=a / "hub.sock", policy_dir=a / "policy", guard_cfg={}, approval_timeout_s=300, cwd=tmp_path)
    assert os.path.realpath(b) in s["sandbox"]["filesystem"]["denyRead"] and os.path.realpath(b) in s["sandbox"]["filesystem"]["denyWrite"]
    # classifier: đọc/ghi hub B → cấm
    ps = PolicyStore(a, user_home=fakehome)
    ps.ensure()
    ctx = GuardContext(tmp_path, ps.load(), git_probe=False, other_hub_homes=tuple(confine.other_hub_homes(a)))
    v = classify("Read", {"file_path": str(b / "hub.db")}, ctx)
    assert v.forbidden and v.rule == "hub_secret_read"
    v = classify("Write", {"file_path": str(b / "notes.txt"), "content": "x"}, ctx)
    assert v.forbidden and v.rule == "other_hub_write"
    # hub B bị xoá → lần đăng ký sau bỏ mục chết
    import shutil

    shutil.rmtree(b)
    confine.register_home(a)
    assert [h["home"] for h in json.loads(reg.read_text())["hubs"]] == [os.path.realpath(a)]


# ============================================================================ e2e: daemon thật + claude giả chạy hook guard thật
def test_e2e_trusted_agent_confined_by_real_hook(hub_home, tmp_path, fakehome):
    import yaml

    from cfharness.cli import main
    from cfharness.daemon import spawn_background
    from cfharness.rpc import RpcClient, RpcError
    from conftest import fake_cli

    fake_bin = fake_cli(tmp_path, Path(__file__).parent / "fake_claude_guard.py")
    state = tmp_path / "state"
    state.mkdir()
    log = tmp_path / "exec.log"
    cfg = {
        "runtime": {"kind": "claude", "claude": {"binary": str(fake_bin), "env_extra": {"FAKE_STATE_DIR": str(state), "FAKE_EXEC_LOG": str(log)}}},
        "approval": {"timeout_s": 20},
        "guard": {"git_probe": False},
        "workspace": {"confine": {"root": str(W(fakehome))}},
    }
    hub_home.config.write_text(yaml.safe_dump(cfg))
    spawn_background(hub_home)
    try:
        rpc = RpcClient(hub_home.socket, timeout=60)
        rpc.call("agent.create", {"config": {"id": "coder", "name": "Coder", "model": "haiku"}})
        rpc.call("guard.trust.set", {"id": "coder", "mode": "full"})
        inside = str(W(fakehome) / "src" / "app.py")
        outside = str(fakehome / "Workspace" / "du-an-B" / ".env")
        prompt = (f'TOOL: Read {json.dumps({"file_path": outside})}\n'
                  f'TOOL: Read {json.dumps({"file_path": inside})}\n'
                  f'TOOL: Write {json.dumps({"file_path": str(fakehome / "Documents" / "x.txt"), "content": "x"})}\n'
                  "BASH: echo hello\n")
        r = rpc.call("run.start", {"agent": "coder", "prompt": prompt, "cwd": str(W(fakehome))})
        final = rpc.call("run.wait", {"id": r["id"], "timeout": 30})
        assert final["status"] == "completed", final
        lines = log.read_text().splitlines() if log.exists() else []
        assert any(inside in x for x in lines), lines  # trust: trong W chạy thẳng
        assert "echo hello" in lines
        assert not any(outside in x or "Documents" in x for x in lines), lines  # ngoài W: hook thật từ chối dù trust full
        with pytest.raises(RpcError, match="giam"):  # cwd ngoài W bị từ chối ngay khi gửi
            rpc.call("run.start", {"agent": "coder", "prompt": "x", "cwd": str(fakehome / "Workspace" / "du-an-B")})
    finally:
        main(["daemon", "stop"])

