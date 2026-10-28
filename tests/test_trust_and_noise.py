"""Bớt hỏi duyệt + trust full theo agent + lệnh cài đặt quyền trong chat (docs/dev-notes/trust-and-chat-settings.md).

- host dev tin cậy (`guard.sandbox.dev_host`): `ssrf_private_resolved` không giữ cho ĐÚNG host + agent người đặt;
- script đã commit của repo (`guard.trusted_repo_scripts`): API xoá trong mã không làm lệnh `node <script>` thành destructive;
- xoá thư mục do task tạo / thư mục tạm hệ thống (`guard.task_created_deletes`);
- `notify.reminder_max_count`;
- trust full (`guard.trust.set`): guard cho phép mọi thứ (vẫn ghi quyết định), ESTOP vẫn chặn, run mới bypassPermissions không sandbox;
- lệnh chat `/trust`, `/perm`, `/reminders` qua daemon thật.

Không gọi claude: Hub/Daemon in-process + FakeRuntime; repo git tạm.
"""

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import time
from pathlib import Path

import pytest

from cfharness import auth
from cfharness.errors import Blocked, HubError
from cfharness.guard.classifier import GuardContext, classify
from cfharness.guard.policy import PolicyStore
from cfharness.hub import Hub
from cfharness.runtime import FakeRuntime

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_DATE": "2020-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00Z"}
RM_JS = "const fs = require('fs');\nfs.rmSync('dist', { recursive: true, force: true });\n"


def git(cwd, *a) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, env=GIT_ENV)
    assert r.returncode == 0, (a, r.stderr)
    return r.stdout.strip()


@pytest.fixture
def hub(hub_home):
    rt = FakeRuntime(hold=True)
    h = Hub(hub_home, runtime=rt)
    h.config.set("approval.timeout_s", 0.3)
    h.guard.resolver = lambda host: {"dev.example": ["127.0.0.1"], "lan.example": ["10.0.0.7"]}.get(host, ["93.184.216.34"])
    yield h
    rt.release()
    h.store.close()


async def _run(hub, agent="w", cwd=None):
    if hub.store.get_agent(agent) is None:
        hub.agents.create({"id": agent, "name": agent.upper()})
    r = hub.runs.submit(agent, "x", cwd=str(cwd) if cwd else None)
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


async def check(hub, run, tool, ti):
    return await asyncio.wait_for(hub.guard.check({"run_id": run["id"], "tool_name": tool, "tool_input": ti, "channel": "hook"}), 20)


def as_agent():
    return auth.CURRENT_CALLER.set(auth.Caller(via="socket", pid=1, token_ok=True, run_id="r_x", evidence=["descendant_of_run_pid:1"]))


# ============================================================================ host dev tin cậy
async def test_dev_host_trusted_only_for_that_agent_and_host(hub):
    run = await _run(hub, "w")
    other = await _run(hub, "v")
    nav = {"url": "https://dev.example:3000/settings"}
    v = await gtest(hub, run, tool="WebFetch", ti=nav)
    assert v["rule"] == "ssrf_private_resolved" and v["hold"]
    r = hub.guard.sandbox_dev_host("w", "https://dev.example:3000/x")  # dán cả URL → lấy host
    assert r["dev_hosts"] == ["dev.example"] and "dev.example" in r["allow_domains"]
    v = await gtest(hub, run, tool="WebFetch", ti=nav)
    assert not v["hold"] and v["rule"] != "ssrf_private_resolved"
    assert (await gtest(hub, other, tool="WebFetch", ti=nav))["hold"]  # agent khác vẫn hỏi
    assert (await gtest(hub, run, tool="WebFetch", ti={"url": "https://lan.example/"}))["hold"]  # host khác vẫn hỏi
    hub.guard.sandbox_dev_host("w", "dev.example", remove=True)
    assert (await gtest(hub, run, tool="WebFetch", ti=nav))["hold"]
    for bad in ("*.example", "dev example", ""):
        with pytest.raises(HubError):
            hub.guard.sandbox_dev_host("w", bad)
    tok = as_agent()
    try:
        with pytest.raises(Blocked):
            hub.guard.sandbox_dev_host("w", "dev.example")
    finally:
        auth.CURRENT_CALLER.reset(tok)
    assert "guard.sandbox.dev_host" in auth.HUMAN_ONLY
    assert [e for e in hub.store.query_events() if e.type == "guard.sandbox_override" and (e.payload or {}).get("dev_host")]


# ============================================================================ script đã commit của repo
def _repo_with_script(base: Path) -> Path:
    repo = base / "repo"
    (repo / "scripts").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    (repo / "scripts" / "clean.mjs").write_text(RM_JS)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")  # ngày commit 2020 → trước mốc task
    return repo


async def test_committed_repo_script_not_destructive(hub, tmp_path):
    repo = _repo_with_script(tmp_path)
    run = await _run(hub, "w", cwd=repo)
    v = await gtest(hub, run, "node scripts/clean.mjs --staged")
    assert v["verdict"] == "write" and v["rule"] == "repo_script_run", v
    (repo / "scripts" / "new.mjs").write_text(RM_JS)  # chưa track (agent vừa viết)
    assert (await gtest(hub, run, "node scripts/new.mjs"))["rule"] == "code_delete_api"
    (repo / "scripts" / "clean.mjs").write_text(RM_JS + "// changed\n")  # đã sửa so với HEAD
    assert (await gtest(hub, run, "node scripts/clean.mjs"))["rule"] == "code_delete_api"
    git(repo, "checkout", "-q", "--", "scripts/clean.mjs")
    hub.config.set("approval.profile", "strict")
    assert (await gtest(hub, run, "node scripts/clean.mjs"))["rule"] == "code_delete_api"


async def test_script_committed_during_task_still_held(hub, tmp_path):
    repo = _repo_with_script(tmp_path)
    run = await _run(hub, "w", cwd=repo)
    (repo / "scripts" / "late.mjs").write_text(RM_JS)
    env = {**GIT_ENV}
    env.pop("GIT_AUTHOR_DATE"), env.pop("GIT_COMMITTER_DATE")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], env=env, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "late"], env=env, check=True)  # commit SAU khi task bắt đầu
    assert (await gtest(hub, run, "node scripts/late.mjs"))["rule"] == "code_delete_api"


# ============================================================================ xoá thư mục do task tạo / thư mục tạm
def _ctx(tmp_path, **kw) -> GuardContext:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return GuardContext(cwd=ws, policy=PolicyStore(tmp_path / "hubhome").load(), git_probe=False, **kw)


def test_task_created_temp_dir_delete(tmp_path, monkeypatch):
    import tempfile

    systemp = tmp_path / "systemp"
    systemp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(systemp))
    for k in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(k, str(systemp))
    now = time.time()
    big = systemp / "lh-test"
    (big / "node_modules").mkdir(parents=True)
    for i in range(5):
        (big / "node_modules" / f"f{i}.js").write_text("x")
    cmd = {"command": f"rm -rf {str(big).replace(os.sep, '/')}"}
    # session bắt đầu SAU khi thư mục được tạo (run trước của cùng task tạo nó)
    v = classify("Bash", cmd, _ctx(tmp_path, run_since=now + 100, worktree_since=now - 10, max_walk=2))
    assert v.verdict == "destructive" and v.rule == "delete_not_run_created"
    v = classify("Bash", cmd, _ctx(tmp_path, run_since=now + 100, worktree_since=now - 10, max_walk=2, task_created_deletes=True))
    assert v.verdict == "write" and v.rule == "delete_run_created", v
    # thư mục ngoài thư mục tạm: vẫn duyệt từng file (max_walk) — quá giới hạn → hỏi
    outside = tmp_path / "ws" / "_tmp_mui"
    (outside / "a").mkdir(parents=True)
    for i in range(5):
        (outside / "a" / f"f{i}").write_text("x")
    v = classify("Bash", {"command": f"rm -rf {str(outside).replace(os.sep, '/')}"},
                 _ctx(tmp_path, run_since=now + 100, worktree_since=now - 10, max_walk=2, task_created_deletes=True))
    assert v.rule != "delete_run_created"
    # workspace nằm TRONG thư mục tạm (Linux: repo clone vào /tmp, CI) → thư mục con của workspace vẫn duyệt từng file
    ws_in_tmp = systemp / "repo"
    (ws_in_tmp / "build" / "a").mkdir(parents=True)
    for i in range(5):
        (ws_in_tmp / "build" / "a" / f"f{i}").write_text("x")
    ctx = GuardContext(cwd=ws_in_tmp, policy=PolicyStore(tmp_path / "hubhome").load(), git_probe=False, run_since=now + 100,
                       worktree_since=now - 10, max_walk=2, task_created_deletes=True)
    v = classify("Bash", {"command": f"rm -rf {str(ws_in_tmp / 'build').replace(os.sep, '/')}"}, ctx)
    assert v.rule != "delete_run_created"
    # thư mục tạm có TRƯỚC task → vẫn hỏi
    v = classify("Bash", cmd, _ctx(tmp_path, run_since=now + 100, worktree_since=now + 50, max_walk=2, task_created_deletes=True))
    assert v.verdict == "destructive"


# ============================================================================ nhắc lại
async def test_reminder_max_count(hub):
    run = await _run(hub, "w")
    hub.config.set("notify.reminder_interval_s", 0.01)
    hub.config.set("notify.reminder_max_count", 1)
    with pytest.raises(ValueError):
        hub.config.set("notify.reminder_max_count", -1)
    ap = hub.approvals.open(run, tool_name="Bash", tool_input={"command": "rm -rf x"}, tool_use_id=None, fingerprint="fp",
                            verdict={"verdict": "destructive", "rule": "r", "klass": "file", "reason": "x", "targets": []}, channel="hook")
    await asyncio.sleep(0.05)
    assert hub.approvals.remind_due() == [ap["id"]]
    await asyncio.sleep(0.05)
    assert hub.approvals.remind_due() == []


# ============================================================================ trust full
async def test_trust_full_allows_everything_but_estop(hub, tmp_path):
    run = await _run(hub, "w")
    push = {"command": "git push --force origin main"}
    hub.guard.trust_set("w", "full")
    r = await check(hub, run, "Bash", push)
    assert r["decision"] == "allow" and r["decided_by"] == "trust"
    r = await check(hub, run, "Bash", {"command": "cfharness config set approval.profile strict"})  # forbidden thường ngày
    assert r["decision"] == "allow"
    r = await check(hub, run, "WebFetch", {"url": "http://lan.example/"})
    assert r["decision"] == "allow"
    rows = [dict(x) for x in hub.store.conn.execute("SELECT classifier, action, verdict FROM guard_decisions WHERE run_id=?", (run["id"],))]
    assert rows and all(x["classifier"] == "trust" and x["action"] == "allow" for x in rows)
    assert any(x["verdict"] == "destructive" for x in rows)  # vẫn phân loại để audit đúng mức
    assert not hub.approvals.list("pending")
    hub.store.state_set("estop", {"on": True})
    try:
        assert (await check(hub, run, "Bash", push))["decision"] == "deny"
    finally:
        hub.store.state_set("estop", {"on": False})
    hub.guard.trust_set("w", "off")
    r = await check(hub, run, "Bash", {"command": "cfharness config set approval.profile strict"})
    assert r["decision"] == "deny"  # forbidden lại như thường


async def test_trust_expiry_review_runs_and_human_only(hub):
    hub.agents.create({"id": "w", "name": "W"})
    r = hub.guard.trust_set("w", "full", for_s=3600)
    assert r["until"] and hub.guard.trust_of("w")
    allv = hub.store.state_get("agent_trust")
    allv["w"]["until"] = "2020-01-01T00:00:00Z"
    hub.store.state_set("agent_trust", allv)
    assert hub.guard.trust_of("w") is None and hub.guard.trust_all() == {}
    hub.guard.trust_set("w", "full")
    assert hub.guard.run_trusted({"agent_id": "w", "run_opts": {}})
    assert hub.guard.run_trusted({"agent_id": "w", "kind": "review", "run_opts": {}}) is None
    assert hub.guard.run_trusted({"agent_id": "w", "run_opts": {"learning_review": True}}) is None
    assert hub.guard.run_trusted({"agent_id": "w", "run_opts": {"eval_overlay": {"x": 1}}}) is None
    for bad in (("w", "maybe", None), ("w", "full", -5)):
        with pytest.raises(HubError):
            hub.guard.trust_set(bad[0], bad[1], for_s=bad[2])
    tok = as_agent()
    try:
        with pytest.raises(Blocked):
            hub.guard.trust_set("w", "full")
    finally:
        auth.CURRENT_CALLER.reset(tok)
    assert "guard.trust.set" in auth.HUMAN_ONLY
    assert [e for e in hub.store.query_events() if e.type == "guard.trust"]
    audits = [dict(x) for x in hub.store.conn.execute("SELECT * FROM audit_log")]
    assert any("guard.trust" in str(x.get("kind") or x.get("action") or "") for x in audits)


async def test_trust_set_all(hub):
    for aid in ("a", "b"):
        hub.agents.create({"id": aid, "name": aid.upper()})
    r = hub.guard.trust_set_all("full", for_s=3600)
    assert r["count"] == 2 and sorted(r["agents"]) == ["a", "b"] and r["until"]
    assert set(hub.guard.trust_all()) == {"a", "b"}
    assert len([e for e in hub.store.query_events() if e.type == "guard.trust"]) == 2  # mỗi agent một event
    with pytest.raises(HubError):
        hub.guard.trust_set_all("maybe")
    tok = as_agent()
    try:
        with pytest.raises(Blocked):
            hub.guard.trust_set_all("full")
    finally:
        auth.CURRENT_CALLER.reset(tok)
    assert "guard.trust.set_all" in auth.HUMAN_ONLY
    assert hub.guard.trust_set_all("off")["count"] == 2 and hub.guard.trust_all() == {}


async def test_trust_new_agents_default(hub):
    hub.agents.create({"id": "old", "name": "OLD"})
    assert hub.guard.trust_of("old") is None  # mặc định "off": như cũ
    with pytest.raises(ValueError):
        hub.config.set("guard.trust_new_agents", "maybe")
    hub.config.set("guard.trust_new_agents", "full")
    assert hub.guard.trust_of("old") is None  # chỉ áp lúc tạo — agent có sẵn giữ nguyên
    tok = as_agent()  # điều phối viên/agent tạo agent mới cũng nhận trust (người đã chọn mặc định này)
    try:
        hub.agents.create({"id": "new", "name": "NEW"})
    finally:
        auth.CURRENT_CALLER.reset(tok)
    t = hub.guard.trust_of("new")
    assert t and t["mode"] == "full" and t["until"] is None and t["by"] == "default"
    assert any(e.type == "guard.trust" and e.agent_id == "new" for e in hub.store.query_events())
    hub.guard.trust_set("new", "off")
    assert hub.guard.trust_of("new") is None
    hub.config.set("guard.trust_new_agents", "off")
    hub.agents.create({"id": "later", "name": "LATER"})
    assert hub.guard.trust_of("later") is None


async def test_trusted_spec_bypass_no_sandbox_on_real_repo(hub, tmp_path):
    from cfharness.runtime.claude_cli import build_argv

    repo = tmp_path / "repo"
    repo.mkdir()
    hub.config.set("workspace.allowed_roots", [str(tmp_path)])
    hub.agents.create({"id": "w", "name": "W"})
    run = hub.store.get_run(hub.runs.submit("w", "hi", cwd=str(repo))["id"])
    spec = hub.runs._prepare_spec(run, hub.store.get_agent("w"))
    assert spec.tool_policy.get("permission_mode") != "bypassPermissions" and not spec.trust_full
    import json

    assert json.loads(spec.settings_path.read_text(encoding="utf-8"))["sandbox"]["enabled"] is True
    hub.guard.trust_set("w", "full")
    run = hub.store.get_run(hub.runs.submit("w", "hi", cwd=str(repo))["id"])
    spec = hub.runs._prepare_spec(run, hub.store.get_agent("w"))
    assert spec.workspace_kind == "dir" and spec.trust_full and spec.tool_policy["permission_mode"] == "bypassPermissions"
    st = json.loads(spec.settings_path.read_text(encoding="utf-8"))
    assert st["sandbox"] == {"enabled": False} and not (st.get("permissions") or {}).get("deny")
    assert "PreToolUse" in (st.get("hooks") or {})  # hook vẫn chạy: ghi quyết định + ESTOP
    assert not any("push --force" in x for x in spec.tool_policy.get("disallowed_tools") or [])
    argv, _ = build_argv(spec, hub.config.get("runtime.claude"))
    i = argv.index("--permission-mode")
    assert argv[i + 1] == "bypassPermissions"


# ============================================================================ lệnh chat
async def test_chat_settings_commands(hub_home):
    from cfharness.daemon import Daemon
    from cfharness.manager import repl
    from cfharness.tui.client import AsyncHubClient

    d = Daemon(hub_home, runtime=FakeRuntime())
    task = asyncio.create_task(d.serve())
    for _ in range(500):
        if hub_home.socket.exists():
            break
        await asyncio.sleep(0.02)
    d.hub.agents.create({"id": "w", "name": "W"})
    d.hub.agents.create({"id": "boss", "name": "Boss", "role": "hub boss", "tool_policy": {"tools": ["Read"]}})
    resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "manager.set", "params": {"agent_id": "boss"}})
    assert "error" not in resp, resp
    buf = io.StringIO()
    s = repl.ChatSession(AsyncHubClient(hub_home.socket, channel="cli"), repl.Out(color=False, file=buf, width=160))
    answers: list[str] = []

    async def ask(prompt: str) -> str:
        return answers.pop(0) if answers else "n"

    s.ask = ask
    assert await s.open()
    try:
        assert await s.handle("/help") and "/trust" in buf.getvalue()
        answers[:] = ["y", "n"]  # bước 2 từ chối → không đổi gì
        await s.handle("/trust w full 2h")
        assert d.hub.guard.trust_of("w") is None
        answers[:] = ["y", "y"]
        await s.handle("/trust w full 2h")
        t = d.hub.guard.trust_of("w")
        assert t and t["until"] and t["channel"] == "cli"
        answers[:] = ["y"]
        await s.handle("/trust w off")
        assert d.hub.guard.trust_of("w") is None
        answers[:] = ["y"]
        await s.handle("/reminders once")
        assert d.hub.config.get("notify.reminder_max_count") == 1
        answers[:] = ["y"]
        await s.handle("/trust-host w dev.example")
        assert d.hub.guard.sandbox_overrides("w")["dev_hosts"] == ["dev.example"]
        answers[:] = ["y"]
        await s.handle("/profile strict")
        assert d.hub.config.get("approval.profile") == "strict"
        answers[:] = ["y"]
        await s.handle("/delegate off file,git")
        assert d.hub.config.get("approval.delegate_to_manager")["enabled"] is False
        await s.handle("/delegate on bogus")
        assert "không giao được" in buf.getvalue()
        await s.handle("/perm")
        out = buf.getvalue()
        assert "Quyền & phê duyệt" in out and "host dev dev.example" in out and "strict" in out
    finally:
        await s.close()
        d.stopping.set()
        try:
            await asyncio.wait_for(task, 20)
        except Exception:
            pass
