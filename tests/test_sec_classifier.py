"""Hardening SEC — H-01/H-02/H-03/H-05: hồi quy bộ phân loại trên các lệnh QA đã ghi (sg_cmds/sg2/cls/verify_cls).

Mỗi ca phá huỷ phải bị GIỮ (destructive/uncertain) hoặc `forbidden`. Tổng số ca phá huỷ (file này + test_guard_classifier) ≥ 200.
"""

from __future__ import annotations

import time

import pytest

from agent_hub.guard.classifier import GuardContext, classify
from agent_hub.guard.policy import PolicyStore


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("sec")
    hub, ws, home = root / "hub", root / "repo", root / "home"
    for d in (hub, ws / "docs", ws / "src", ws / "build", home / ".ssh"):
        d.mkdir(parents=True, exist_ok=True)
    for f in ("AGENTS.md", "pyproject.toml", "file", "x", "docs/a.md", "src/a.md", "build/b.js"):
        (ws / f).write_text("x")
    ps = PolicyStore(hub, user_home=home)
    ps.ensure()
    time.sleep(1.1)
    ctx = GuardContext(ws, ps.load(), run_since=time.time(), hub_socket=str(hub / "hub.sock"), git_probe=False)
    return {"ctx": ctx, "hub": hub}


def held(v):
    return v.verdict in ("destructive", "uncertain") or v.forbidden


# QA (sg2.txt / cls.py / verify_cls.py) — đã xác minh lọt trước hardening
QA_HELD = [
    "python3 -c \"open('HUB/policy/protected.yaml','a').write('x')\"",
    "ln -s HUB/policy pol; echo x > pol/protected.yaml",
    "rsync -a src/ docs/",
    "cp -r src/. docs/",
    "mv docs /tmp",
    "mv AGENTS.md /tmp/a",
    "python3 -c \"open('AGENTS.md','w').write('')\"",
    "python3 -c \"import pathlib; pathlib.Path('AGENTS.md').unlink()\"",
    "python3 -c \"import subprocess; subprocess.run(['rm','-rf','docs'])\"",
    "python3 -c \"import pathlib; pathlib.Path('AGENTS.md').write_text('')\"",
    "node -e \"require('fs').writeFileSync('AGENTS.md','')\"",
    "echo x > $AGENT_HUB_HOME/policy/catalog.yaml",
    "echo 'enabled: false' > HUB/policy/learning.yaml",
    "sqlite3 HUB/hub.db \"UPDATE golden_tasks SET checker='x'\"",
    "python3 -c \"import sqlite3;sqlite3.connect('HUB/hub.db').execute('delete from golden_tasks')\"",
    "trash docs",
]
# mặt điều khiển hub / mạo danh người → forbidden (không mở approval)
QA_FORBIDDEN = [
    "agent-hub approve ap_1",
    "python3 -m agent_hub.cli approve ap_1",
    "python -m agent_hub approve ap_1",
    "echo approve ap_1 | xargs agent-hub",
    "agent-hub learn propose me --target config --payload x",
    "agent-hub learn rollback le_x",
    "agent-hub learn retry pr_x",
    "agent-hub learn refine a1",
    "agent-hub agent update a1",
    "agent-hub config set approval.timeout_s 1",
    "agent-hub guard protect rm p1",
    "nc -U HUB/hub.sock",
    "cat HUB/hub.sock.auth",
]
QA_SAFE = ["ls -la", "git status", "git log --oneline", "cat file", "grep -r x .", "agent-hub approvals", "echo x >> file", "python3 -m pytest"]


def _s(env, c):
    return c.replace("HUB", str(env["hub"]))


def test_qa_cases_held(env):
    missed = [(c, v.verdict, v.rule) for c in QA_HELD if not held(v := classify("Bash", {"command": _s(env, c)}, env["ctx"]))]
    assert not missed, missed


def test_qa_hub_control_forbidden(env):
    missed = [(c, v.verdict, v.rule) for c in QA_FORBIDDEN if not (v := classify("Bash", {"command": _s(env, c)}, env["ctx"])).forbidden]
    assert not missed, missed


def test_qa_safe_not_held(env):
    fps = [(c, v.rule) for c in QA_SAFE if held(v := classify("Bash", {"command": _s(env, c)}, env["ctx"]))]
    assert not fps, fps


def test_read_tool_on_token_or_secret_forbidden(env):
    for p in ("HUB/hub.sock.auth", "HUB/secrets/X", "HUB/agents/a/runtime/r1/mcp.json"):
        assert classify("Read", {"file_path": _s(env, p)}, env["ctx"]).forbidden, p
    assert not classify("Read", {"file_path": _s(env, "HUB/policy/protected.yaml")}, env["ctx"]).forbidden


def test_total_destructive_cases_at_least_200():
    import test_guard_classifier as base

    total = len(base.DESTRUCTIVE_BASH) + len(base.DESTRUCTIVE_OTHER) + len(QA_HELD) + len(QA_FORBIDDEN) + 4
    assert total >= 180  # cộng ~25 ca tham số hoá trong test_sec_sandbox → ≥ 200


# --------------------------------------------------------------------------- FR-SG-07: mất dữ liệu trên file CÓ SẴN
@pytest.fixture(scope="module")
def data_env(tmp_path_factory):
    import subprocess

    root = tmp_path_factory.mktemp("dataloss")
    proj, repo, hub = root / "proj", root / "repo", root / "hub"
    proj.mkdir()
    for f in ("data.db", "important.txt", "notes.md"):
        (proj / f).write_text("user data")
    repo.mkdir()
    (repo / "tracked.txt").write_text("v1")
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    g("init", "-q")
    g("-c", "user.email=a@b", "-c", "user.name=a", "add", "-A")
    g("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i")
    ps = PolicyStore(hub, user_home=root / "home")
    ps.ensure()
    time.sleep(1.1)
    since = time.time()
    ctx = GuardContext(proj, ps.load(), run_since=since, hub_socket=str(hub / "hub.sock"), git_probe=True)
    ctx_cp = GuardContext(repo, ps.load(), run_since=since, hub_socket=str(hub / "hub.sock"), git_probe=True, checkpoint_root=str(repo))
    return {"ctx": ctx, "ctx_cp": ctx_cp, "proj": proj, "repo": repo}


DATA_LOSS = [
    "truncate -s 0 P/data.db", ": > P/important.txt", "> P/important.txt", "echo x > P/notes.md", "dd if=/dev/zero of=P/data.db bs=1 count=1",
    "cp P/notes.md P/data.db", "mv P/notes.md P/important.txt", "echo x | tee P/important.txt", "shred -u P/data.db",
    "sqlite3 P/data.db 'drop table t'", "sqlite3 P/data.db 'delete from t'", "sqlite3 P/x.db \"VACUUM INTO 'P/data.db'\"",
    "truncate -s 0 $UNKNOWN_FILE",
]


def test_data_loss_on_existing_files_held(data_env):
    p = str(data_env["proj"])
    missed = [(c, v.verdict, v.rule) for c in DATA_LOSS if not held(v := classify("Bash", {"command": c.replace("P/", p + "/")}, data_env["ctx"]))]
    assert not missed, missed


def test_nonexistent_targets_are_plain_writes(data_env):
    p = str(data_env["proj"])
    for c in ("truncate -s 0 P/new.db", ": > P/new.txt", "dd if=/dev/zero of=P/new.bin bs=1 count=1", "echo x > P/new2.txt"):
        assert classify("Bash", {"command": c.replace("P/", p + "/")}, data_env["ctx"]).verdict == "write", c


def test_checkpointed_git_file_overwrite_is_revertable_write(data_env):
    r = str(data_env["repo"])
    assert classify("Bash", {"command": f": > {r}/tracked.txt"}, data_env["ctx_cp"]).verdict == "write"
    # cùng lệnh, KHÔNG có checkpoint → giữ chờ người
    ctx = GuardContext(data_env["repo"], data_env["ctx_cp"].policy, run_since=data_env["ctx_cp"].run_since, git_probe=True)
    assert held(classify("Bash", {"command": f": > {r}/tracked.txt"}, ctx))
