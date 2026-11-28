"""Xuất / nhập toàn bộ hub sang máy khác (hub_transfer.py, hub_transfer_cli.py, pathmap.py) — PR1 của plan-hub-clone.md.

Hub nguồn và đích là hai HUB_HOME tạm với hai `$HOME` giả khác nhau (mô phỏng hai máy). Không gọi claude, không mạng.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import stat
import sys
import tarfile
from pathlib import Path

import pytest

from cfharness import auth
from cfharness import backup as B
from cfharness import hub_transfer as T
from cfharness.errors import BLOCKED, Conflict, HubError
from cfharness.pathmap import PathMap
from cfharness.paths import HubPaths

AGENT = auth.Caller(via="socket", pid=99, token_ok=True, run_id="r_x", agent_id="w1", evidence=["env_run_id"])
SECRET_VALUE = "sk-TRANSFER-SECRET-XYZ"


def _paths(home: Path) -> HubPaths:
    p = HubPaths.resolve(home)
    p.ensure()
    return p


def make_source(tmp_path: Path, monkeypatch) -> tuple[HubPaths, Path]:
    """Hub "máy A": agent + team + task đang chạy + lịch + tri thức qua ledger + fact graph + run dở + trạng thái máy + file run thô."""
    from cfharness.hub import Hub
    from cfharness.runtime.fake import FakeRuntime

    user = tmp_path / "userA"
    proj = user / "Workspace" / "proj"
    proj.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(user))
    p = _paths(user / ".cfharness")
    p.config.write_text(f"guard:\n  git_probe: false\nworkspace:\n  allowed_roots: ['{user}/Workspace', '{p.home}/agents']\n")
    (p.home / "policy").mkdir(exist_ok=True)
    (p.home / "policy" / "protected.yaml").write_text(f"paths:\n- {proj}/prod\n")
    h = Hub(p, runtime=FakeRuntime())
    try:
        h.agents.create({"id": "a1", "name": "A1", "model": "haiku", "workspace_default": str(proj)})
        h.agents.create({"id": "w1", "name": "W1", "model": "haiku"})
        h.teams.create({"id": "t1", "name": "Team"})
        h.teams.member_set("t1", "a1", "worker")
        t = h.tasks.create({"title": "việc dở", "body": "b", "assignee": "a1"})
        h.tasks.set_status(t["id"], "running")
        t2 = h.tasks.create({"title": "xong rồi", "body": "b", "assignee": "a1"})
        h.tasks.set_status(t2["id"], "done")
        h.schedules.create({"kind": "agent", "target": "a1", "expr": "daily 08:00", "prompt": "báo cáo"})
        r = h.learning.pipeline.propose(target="memory", op="add", agent_id="a1", payload={"store": "memory", "action": "add",
                                        "content": f"Repo chính ở {proj}"}, created_by="user")
        assert r.get("status") in ("applied", "promoted", "canary"), r
        g = h.learning.graph_write("a1", [{"action": "add_fact", "src": "user", "relation": "prefers", "dst": None,
                                           "fact": f"User keeps code in {proj}"}], created_by="user")
        assert g, g
        for rid, st in (("r_run1", "running"), ("r_q1", "queued"), ("r_old", "completed")):
            h.store.insert_run({"id": rid, "agent_id": "a1", "kind": "adhoc", "prompt": "x", "status": st, "queued_at": "2026-11-01T00:00:00+00:00",
                                "cwd": str(proj)})
        h.store.conn.execute("INSERT INTO events(epoch, seq, ts, type, level, source, agent_id, payload) VALUES ('e',1,'t','x','info','hub','a1',?)",
                             (json.dumps({"cwd": str(proj)}),))
        for k, v in (("repo_locks", {str(proj): "r_run1"}), ("telegram.offset", 42), ("pending_resume", ["r_run1"]), ("cli_compat", {"v": "2"}),
                     ("keep_me", {"p": str(proj)})):
            h.store.state_set(k, v)
        h.integrity.rebaseline("test")
    finally:
        h.store.close()
    (p.home / "runs" / "r_old.jsonl").write_text('{"type":"result"}\n')
    (p.home / "checkpoints" / "r_old").mkdir(parents=True)
    (p.home / "checkpoints" / "r_old" / "checkpoint.json").write_text("{}")
    (p.home / "secrets" / "API_KEY").write_text(SECRET_VALUE)
    return p, proj


def _members(pkg: Path) -> list[str]:
    with tarfile.open(pkg, "r:") as tf:
        return tf.getnames()


def _rewrite_pkg(pkg: Path, *, member_fn=None, manifest_fn=None, extra: list[tuple[tarfile.TarInfo, bytes]] = ()) -> None:
    """Viết lại gói (tráo nội dung phần tử / manifest / thêm phần tử lạ) — mô phỏng gói bị sửa."""
    out = pkg.with_name("x.tmp")
    with tarfile.open(pkg, "r:") as src, tarfile.open(out, "w:", format=tarfile.PAX_FORMAT) as dst:
        mems = list(src)
        for mem in mems:
            data = src.extractfile(mem).read()
            if mem.name == T.MANIFEST:
                for ti, b in extra:
                    dst.addfile(ti, io.BytesIO(b))
                if manifest_fn:
                    data = json.dumps(manifest_fn(json.loads(data))).encode()
            elif member_fn:
                data = member_fn(mem.name, data)
            mem.size = len(data)
            dst.addfile(mem, io.BytesIO(data))
    os.replace(out, pkg)


# ============================================================================ pathmap
def test_pathmap_single_pass_longest_first_and_exact_boundary():
    pm = PathMap([("/Users/a", "/home/b"), ("/Users/a/.cfharness", "/Users/a/hub2"), ("/x", "/x")])
    assert pm.rewrite('{"h": "/Users/a/.cfharness/agents/a1", "w": "/Users/a/Workspace", "o": "/Users/ab/z"}') == \
        '{"h": "/Users/a/hub2/agents/a1", "w": "/home/b/Workspace", "o": "/Users/ab/z"}'
    assert pm.rewrite("/Users/a") == "/home/b" and pm.rewrite("/Users/a2/x") == "/Users/a2/x"
    assert [x["from"] for x in pm.as_list()] == ["/Users/a/.cfharness", "/Users/a"]  # cặp không đổi bị bỏ
    assert not PathMap([("/a/", "/a")])  # chuẩn hoá dấu / cuối


# ============================================================================ xuất
def test_export_package_format_private_and_excludes(tmp_path, monkeypatch):
    src, _proj = make_source(tmp_path, monkeypatch)
    r = T.export_offline(src, profile="full")
    pkg = Path(r["path"])
    assert pkg.parent == src.home / "exports" and pkg.suffix == ".cfhx" and T.ID_RE.match(r["id"]), r
    assert stat.S_IMODE(os.stat(pkg).st_mode) == 0o600 and stat.S_IMODE(os.stat(pkg.parent).st_mode) == 0o700
    names = _members(pkg)
    assert names[0] == "hub.db" and names[-1] == "MANIFEST.json"
    assert "home/agents/a1/MEMORY.md" in names and "home/config.yaml" in names and "extra/runs/r_old.jsonl" in names
    assert not any("checkpoints" in n or "secrets" in n or n.startswith("home/exports") for n in names), names
    assert SECRET_VALUE.encode() not in pkg.read_bytes()
    m = T.read_manifest(pkg)
    assert m["format"] == "cfharness-export" and m["schema"] == 1 and m["profile"] == "full" and m["mode_hint"] == "clone"
    assert m["source"]["home"] == str(src.home) and m["source"]["user_home"] == str(tmp_path / "userA") and m["source"]["sep"] == os.sep
    assert m["source"]["user_version"] == B.latest_schema_version() and len(m["source"]["hub_id"]) == 16
    assert m["missing_secrets"] == ["API_KEY"] and set(m["sections"]) == {"db", "home", "runs", "repos"}
    assert any(f["path"] == "hub.db" and f["gz"] for f in m["files"]) and "checkpoints" in m["excluded"]
    assert m["path_roots"]["workspaces"] == [str(tmp_path / "userA" / "Workspace" / "proj")]
    v = T.verify_package(pkg)
    assert v["ok"] and v["integrity"] == "ok", v
    # profile core: không có runs/
    core = T.export_offline(src, profile="core")
    assert not any(n.startswith("extra/runs/") for n in _members(Path(core["path"])))
    assert {"exports", "imports", "filehub"} <= set(B.EXCLUDED)  # bản sao lưu không nhét gói xuất, profile sync Không gian số


def test_export_refused_inside_run_and_while_daemon_lock_held(tmp_path, monkeypatch):
    from cfharness.daemon import acquire_instance_lock, release_instance_lock

    src, _ = make_source(tmp_path, monkeypatch)
    lk = acquire_instance_lock(src)
    try:
        with pytest.raises(Conflict, match="daemon đang chạy"):
            T.export_offline(src)
    finally:
        release_instance_lock(lk)
    monkeypatch.setenv(auth.ENV_RUN_ID, "r_agent")
    with pytest.raises(HubError, match="chỉ người"):
        T.export_offline(src)
    from cfharness.cli import main

    assert main(["hub", "export", "--home", str(src.home)]) == 3


# ============================================================================ nhập clone (máy B)
def test_import_clone_roundtrip_rewrites_paths_and_isolates_clone(tmp_path, monkeypatch):
    from cfharness.hub import Hub
    from cfharness.runtime.fake import FakeRuntime

    src, proj = make_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src)["path"])
    src_db = sqlite3.connect(str(src.db))
    src_ledger = src_db.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0]
    src_facts = src_db.execute("SELECT COUNT(*) FROM mem_facts").fetchone()[0]
    src_db.close()

    userb = tmp_path / "userB"
    userb.mkdir()
    monkeypatch.setenv("HOME", str(userb))
    dst = HubPaths.resolve(userb / ".cfharness")
    new_proj = userb / "Workspace" / "proj"

    # dry-run: in kế hoạch, KHÔNG tạo gì ở đích
    plan = T.import_plan(dst, pkg)
    assert not plan["blockers"] and plan["home_blank"] and not dst.home.exists(), plan
    assert {"from": str(src.home), "to": str(dst.home)} in plan["path_map"]
    assert {"from": str(tmp_path / "userA"), "to": str(userb)} in plan["path_map"]
    assert plan["db_rewrite"].get("agents.home_path") == 2 and plan["db_rewrite"].get("agents.workspace_default") == 1
    assert "agents/a1/MEMORY.md" in plan["review"] and plan["knowledge_rows_with_old_paths"] >= 1
    assert "config.yaml" in plan["files_rewrite"] and "policy/protected.yaml" in plan["files_rewrite"]
    assert plan["runs_interrupted"] == 2 and plan["tasks_blocked"] == 1 and plan["schedules_paused"] == 1
    assert {"repo_locks", "telegram.offset", "pending_resume", "cli_compat"} <= set(plan["state_cleared"]) and "keep_me" not in plan["state_cleared"]
    assert set(plan["config_changed"]) == {"notify.telegram.enabled", "notify.slack.enabled"}
    assert plan["missing_secrets"] == ["API_KEY"]

    r = T.import_package(dst, pkg)
    assert r["mode"] == "clone" and r["pre_restore"] is None and r["runs_files"] == 1
    assert stat.S_IMODE(os.stat(dst.db).st_mode) == 0o600
    assert not any(x.name.startswith(".import-") for x in dst.home.iterdir())  # stage đã dọn
    assert not (dst.home / "secrets" / "API_KEY").exists() and (dst.home / "runs" / "r_old.jsonl").exists()
    assert not (dst.home / "checkpoints" / "r_old").exists()

    h = Hub(dst, runtime=FakeRuntime())
    try:
        a1 = h.store.get_agent("a1")
        assert Path(a1["home_path"]) == dst.home / "agents" / "a1" and a1["workspace_default"] == str(new_proj)
        assert h.teams.find("t1") is not None
        st = {t["title"]: t for t in h.store.conn.execute("SELECT title, status, block_reason FROM tasks").fetchall()}
        assert st["việc dở"]["status"] == "blocked" and st["việc dở"]["block_reason"].startswith("[hub]") and st["xong rồi"]["status"] == "done"
        cm = h.store.conn.execute("SELECT author, text FROM task_comments WHERE text LIKE '[hub]%'").fetchall()
        assert len(cm) == 1 and cm[0]["author"] == "hub"
        (sch,) = h.store.conn.execute("SELECT status, paused_by FROM schedules").fetchall()
        assert sch["status"] == "paused" and sch["paused_by"] == "import"
        runs = {x["id"]: x["status"] for x in h.store.conn.execute("SELECT id, status FROM runs")}
        assert runs == {"r_run1": "interrupted", "r_q1": "interrupted", "r_old": "completed"}
        assert h.store.conn.execute("SELECT cwd FROM runs WHERE id='r_old'").fetchone()[0] == str(new_proj)
        assert h.store.conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0] == src_ledger
        assert h.store.conn.execute("SELECT COUNT(*) FROM mem_facts").fetchone()[0] == src_facts
        # graph = tri thức thuộc ledger → không viết lại tay; append-only giữ lịch sử
        assert str(proj) in h.store.conn.execute("SELECT fact FROM mem_facts").fetchone()[0]
        assert json.loads(h.store.conn.execute("SELECT payload FROM events WHERE type='x'").fetchone()[0])["cwd"] == str(proj)
        for k in ("repo_locks", "telegram.offset", "pending_resume", "cli_compat"):
            assert h.store.state_get(k) is None, k
        assert h.store.state_get("keep_me") == {"p": str(new_proj)}
        imp = h.store.state_get("imported_from")
        assert imp["mode"] == "clone" and imp["export_id"] == pkg.stem and h.store.state_get("cloned_from")["hub_id"]
        fu = h.store.state_get("import_followups")
        assert "agents/a1/MEMORY.md" in fu["files"] and fu["graph_rows"] >= 1
        assert h.store.conn.execute("SELECT COUNT(*) FROM audit_log WHERE kind='hub.imported'").fetchone()[0] == 1
        assert h.config.get("notify.telegram.enabled") is False and h.config.get("notify.slack.enabled") is False
        assert str(src.home) not in dst.config.read_text() and str(new_proj) in (dst.home / "policy" / "protected.yaml").read_text()
        assert str(proj) in (dst.home / "agents" / "a1" / "MEMORY.md").read_text()  # chưa tự sửa (ledger)
        res = h.integrity.check(restore=False)
        assert not res.get("policy") and not res.get("config") and not res.get("first_baseline"), res
    finally:
        h.store.close()
    # nguồn không bị đụng
    s = sqlite3.connect(str(src.db))
    assert s.execute("SELECT status FROM runs WHERE id='r_run1'").fetchone()[0] == "running"
    s.close()


def test_import_move_keeps_channels_schedules_and_resumes(tmp_path, monkeypatch):
    src, _ = make_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="core")["path"])
    monkeypatch.setenv("HOME", str(tmp_path / "userB"))
    dst = HubPaths.resolve(tmp_path / "userB" / ".cfharness")
    r = T.import_package(dst, pkg, mode="move", maps=[(str(tmp_path / "userA" / "Workspace"), str(tmp_path / "repos"))])
    assert r["runs_interrupted"] == 1 and r["runs_pending_resume"] == 1 and r["tasks_blocked"] == 0 and not r["config_changed"]
    c = sqlite3.connect(str(dst.db))
    assert c.execute("SELECT status FROM schedules").fetchone()[0] == "active"
    assert c.execute("SELECT status FROM runs WHERE id='r_q1'").fetchone()[0] == "queued"
    assert json.loads(c.execute("SELECT value FROM hub_state WHERE key='pending_resume'").fetchone()[0]) == ["r_run1"]
    assert c.execute("SELECT workspace_default FROM agents WHERE id='a1'").fetchone()[0] == str(tmp_path / "repos" / "proj")  # --map-path thắng
    c.close()


# ============================================================================ từ chối
def test_import_refuses_tampered_or_unsafe_packages(tmp_path, monkeypatch):
    src, _ = make_source(tmp_path, monkeypatch)
    dst = HubPaths.resolve(tmp_path / "dst")

    def fresh() -> Path:
        return Path(T.export_offline(src, profile="core")["path"])

    # nội dung bị tráo (manifest cũ) → sha256 lệch
    pkg = fresh()
    _rewrite_pkg(pkg, member_fn=lambda n, d: b"\x1f\x8b" + d[2:-8] + b"\0" * 8 if n == "home/agents/a1/MEMORY.md" else d)
    v = T.verify_package(pkg)
    assert not v["ok"] and any("MEMORY.md" in e or "gói hỏng" in e for e in v["errors"]), v
    with pytest.raises(HubError, match="không qua kiểm tra"):
        T.import_package(dst, pkg)
    assert not dst.db.exists()

    # tên phần tử nguy hiểm: `..`, tuyệt đối, symlink, ngoài tập sao lưu
    for name, typ in (("home/agents/../../evil", tarfile.REGTYPE), ("/etc/evil", tarfile.REGTYPE), ("home/agents/a1/link", tarfile.SYMTYPE),
                      ("home/secrets/STOLEN", tarfile.REGTYPE), ("extra/../hub.db", tarfile.REGTYPE)):
        pkg = fresh()
        ti = tarfile.TarInfo(name)
        ti.type = typ
        ti.size = 0 if typ == tarfile.SYMTYPE else 3
        if typ == tarfile.SYMTYPE:
            ti.linkname = "/etc/passwd"
        _rewrite_pkg(pkg, extra=[(ti, b"" if typ == tarfile.SYMTYPE else b"bad")])
        v = T.verify_package(pkg)
        assert not v["ok"] and any("không hợp lệ" in e for e in v["errors"]), (name, v)
        assert not T.import_plan(dst, pkg)["verify"]["ok"]

    # gói từ cfharness mới hơn (schema DB) / định dạng mới hơn / phần bắt buộc lạ
    for fn, msg in ((lambda m: {**m, "source": {**m["source"], "user_version": B.latest_schema_version() + 5}}, "mới hơn"),
                    (lambda m: {**m, "schema": 99}, "mới hơn"),
                    (lambda m: {**m, "sections": {**m["sections"], "future2": {"prefix": "future2.json"}}}, "bắt buộc"),
                    (lambda m: {**m, "sections": {**m["sections"], "repos": {"prefix": "other.json", "optional": True}}}, "repos khai báo sai"),
                    (lambda m: {**m, "sections": {**m["sections"], "secrets": {"prefix": "secrets.enc"}}}, "thiếu khối secrets"),
                    (lambda m: {**m, "format": "cfharness-backup"}, "format")):
        pkg = fresh()
        _rewrite_pkg(pkg, manifest_fn=fn)
        plan = T.import_plan(dst, pkg)
        assert plan["blockers"] and any(msg in b for b in plan["blockers"]), plan["blockers"]
        with pytest.raises(HubError):
            T.import_package(dst, pkg)
    # phần lạ `optional` (bản sau thêm) → bỏ qua kèm cảnh báo, vẫn nhập được
    pkg = fresh()
    ti = tarfile.TarInfo("future/s1.jsonl")
    ti.size = 2
    _rewrite_pkg(pkg, extra=[(ti, b"{}")], manifest_fn=lambda m: {**m, "sections": {**m["sections"], "future": {"prefix": "future/", "optional": True}}})
    v = T.verify_package(pkg)
    assert v["ok"] and any("future" in w for w in v["warnings"]), v
    assert not dst.db.exists()


def test_import_requires_blank_home_or_replace_with_pre_restore(tmp_path, monkeypatch):
    from cfharness.daemon import acquire_instance_lock, release_instance_lock

    src, _ = make_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="core")["path"])
    monkeypatch.setenv("HOME", str(tmp_path / "userB"))
    dst = HubPaths.resolve(tmp_path / "userB" / ".cfharness")
    T.import_package(dst, pkg)
    (dst.home / "agents" / "a1" / "MEMORY.md").write_text("đổi ở máy B\n")
    assert not T.home_blank(dst)
    with pytest.raises(Conflict, match="không trống"):
        T.import_package(dst, pkg)
    lk = acquire_instance_lock(dst)
    try:
        with pytest.raises(Conflict, match="daemon"):
            T.import_package(dst, pkg, replace=True)
    finally:
        release_instance_lock(lk)
    r = T.import_package(dst, pkg, replace=True)
    assert r["pre_restore"] and B.verify(dst, r["pre_restore"])["ok"]
    with tarfile.open(B.archive_path(dst, r["pre_restore"]), "r:gz") as tf:
        assert tf.extractfile("home/agents/a1/MEMORY.md").read() == "đổi ở máy B\n".encode()
    assert "đổi ở máy B" not in (dst.home / "agents" / "a1" / "MEMORY.md").read_text()
    monkeypatch.setenv(auth.ENV_RUN_ID, "r_agent")
    with pytest.raises(HubError, match="chỉ người"):
        T.import_package(dst, pkg, replace=True)


def test_cli_export_to_file_and_import_dry_run_then_apply(tmp_path, monkeypatch, capsys):
    from cfharness.cli import main

    src, _ = make_source(tmp_path, monkeypatch)
    out = tmp_path / "moved" / "hub.cfhx"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["hub", "export", str(out), "--profile", "core", "--home", str(src.home)]) == 1  # có secret, không TTY, không passphrase
    assert not out.exists() and not list((src.home / "exports").glob("*.cfhx"))  # từ chối TRƯỚC khi đóng gói
    capsys.readouterr()
    assert main(["hub", "export", str(out), "--profile", "core", "--no-secrets", "--home", str(src.home)]) == 0
    text = capsys.readouterr().out
    assert out.is_file() and stat.S_IMODE(os.stat(out).st_mode) == 0o600 and "API_KEY" in text and "KHÔNG mã hoá" in text
    assert main(["hub", "export", str(out), "--no-secrets", "--home", str(src.home)]) == 1  # không ghi đè
    capsys.readouterr()
    monkeypatch.setenv("HOME", str(tmp_path / "userB"))
    dst_home = tmp_path / "userB" / ".cfharness"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert main(["hub", "import", str(out), "--dry-run", "--home", str(dst_home)]) == 0
    text = capsys.readouterr().out
    assert "dry-run" in text and "Bản đồ đường dẫn" in text and "agents.home_path" in text and not (dst_home / "hub.db").exists()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert main(["hub", "import", str(out), "--home", str(dst_home)]) == 1  # không TTY, thiếu --yes
    capsys.readouterr()
    assert main(["hub", "import", str(out), "--home", str(dst_home), "--map-path", "relative=/x", "--yes"]) == 1
    assert main(["hub", "import", str(out), "--home", str(dst_home), "--yes"]) == 0
    assert "đã nhập" in capsys.readouterr().out and (dst_home / "hub.db").exists()
    assert main(["hub", "import", str(out), "--home", str(dst_home), "--yes", "--replace"]) == 1  # --replace không TTY cần --confirm-id
    assert main(["hub", "import", str(out), "--home", str(dst_home), "--yes", "--replace", "--confirm-id", out.stem]) == 1  # id gói ≠ tên file
    m = T.read_manifest(out)
    assert main(["hub", "import", str(out), "--home", str(dst_home), "--yes", "--replace", "--confirm-id", m["id"]]) == 0


# ============================================================================ trong daemon: RPC + làm lặng + quyền
async def test_rpc_export_human_only_quiesce_and_stop(hub_home):
    from m7_fake import MgrScript, close, make_daemon, rpc, wait_status

    class Hold(MgrScript):
        def __call__(self, spec):
            self.specs.append(spec)
            return {"text": "x", "hold": True}

    d = await make_daemon(hub_home, Hold())
    try:
        d.hub.agents.create({"id": "w1", "name": "W1", "model": "haiku"})
        with pytest.raises(HubError) as ei:
            await rpc(d, "hub.export", {}, as_caller=AGENT)
        assert ei.value.code == BLOCKED and not list(T.exports_dir(hub_home).glob("*.cfhx"))
        run = d.hub.runs.submit("w1", "làm việc")
        await wait_status(d, run["id"])
        with pytest.raises(HubError, match="--stop"):
            await rpc(d, "hub.export", {"profile": "core"})
        assert d.hub.runs.quiesced is None  # luôn thả lặng
        r = await rpc(d, "hub.export", {"profile": "core", "stop": True, "_audit": {"channel": "cli"}})
        assert r["stopped_runs"] == [run["id"]] and Path(r["path"]).is_file() and r["verify"]["ok"]
        assert d.hub.runs.quiesced is None
        rows = d.hub.store.conn.execute("SELECT channel FROM audit_log WHERE kind='hub.export' AND outcome='ok'").fetchall()
        assert [x["channel"] for x in rows] == ["cli"]  # L3: lần từ chối trước (thiếu --stop) là dòng outcome=error riêng
        # đang lặng: run mới xếp hàng, không khởi chạy; thả → chạy
        d.hub.runs.quiesced = "export"
        r2 = d.hub.runs.submit("w1", "sau khi xuất")
        await asyncio.sleep(0.1)
        assert d.hub.store.get_run(r2["id"])["status"] == "queued"
        d.hub.runs.quiesced = None
        d.hub.runs.tick()
        await wait_status(d, r2["id"])
    finally:
        await close(d)


def test_auth_classifier_and_sandbox_protect_transfer(tmp_path):
    from cfharness.guard.classifier import HUB_HUMAN_ONLY_CLI, GuardContext, classify, sensitive_read
    from cfharness.guard.policy import PolicyStore
    from cfharness.guard.runtime import HUB_WRITE_DENY_NAMES, sandbox_settings, secret_read_deny_rules

    assert "hub.export" in auth.HUMAN_ONLY and "hub.export" in auth.all_mutating() and "hub.export" not in auth.MANAGER_ALLOWED
    assert HUB_HUMAN_ONLY_CLI["hub"] is None  # mọi lệnh con (export|import|unfreeze|doctor)
    hub = tmp_path / "h"
    ctx = GuardContext(cwd=tmp_path, policy=PolicyStore(hub).load())
    for cmd in ("cfharness hub export --stop", "cfharness hub import /tmp/x.cfhx --yes --replace --stop", "uv run cfharness hub export /tmp/a.cfhx",
                "python3 -m cfharness hub import x.cfhx --dry-run"):
        v = classify("Bash", {"command": cmd}, ctx)
        assert v.forbidden, cmd
    home = str(ctx.policy.hub_home)
    for sub in ("exports", "imports", "filehub"):  # filehub/ (main) và exports/imports/ (hub clone) cùng có sau khi gộp nhánh
        assert sensitive_read(f"{home}/{sub}/a-20261115-120000.cfhx", ctx)
        assert sub in HUB_WRITE_DENY_NAMES
    sb = sandbox_settings(Path(home), Path(home) / "hub.sock", {})
    fs = sb["filesystem"]
    for sub in ("exports", "imports", "filehub"):
        assert any(x.rstrip("/").endswith(f"/{sub}") for x in fs["denyRead"]) and any(x.rstrip("/").endswith(f"/{sub}") for x in fs["denyWrite"])
        assert any(f"{sub}/**" in r and r.startswith("Read(") for r in secret_read_deny_rules(Path(home), Path(home) / "hub.sock"))


def test_chat_peers_and_slack_app_dropped_on_clone_kept_on_move():
    """plan-chat-setup-web: tên người được phép (`telegram.peers`/`slack.peers`) + app Slack đã biết (`slack.app`) đi theo token —
    clone bỏ token Telegram/Slack nên bỏ luôn; move giữ."""
    import sqlite3

    assert {"telegram.peers", "slack.peers", "slack.app"} <= set(T.CLONE_STATE)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE hub_state(key TEXT PRIMARY KEY, value TEXT)")
    for k in ("telegram.peers", "slack.peers", "slack.app", "keep_me"):
        conn.execute("INSERT INTO hub_state VALUES (?, '{}')", (k,))
    assert set(T._state_keys(conn, "clone")) == {"telegram.peers", "slack.peers", "slack.app"}
    assert T._state_keys(conn, "move") == []
