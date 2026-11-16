"""Session Claude Code trong gói xuất/nhập hub (transfer_sessions.py) — PR3 của docs/dev-notes/plan-hub-clone.md.

Hai "máy" = hai `$HOME` giả + hai claude home giả (`CFHARNESS_TEST_CLAUDE_HOME`). Transcript là jsonl TỰ TẠO có cấu trúc giống Claude Code
(không chép transcript thật). Tên thư mục project trong `test_encode_cwd_matches_observed_names` lấy từ quan sát `~/.claude/projects`
trên máy dev (chỉ tên thư mục + cwd tương ứng). Không gọi claude, không mạng.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tarfile
from pathlib import Path

import pytest

from cfharness import hub_transfer as T
from cfharness import transfer_secrets as S
from cfharness import transfer_sessions as TS
from cfharness.errors import HubError
from cfharness.pathmap import PathMap
from cfharness.paths import HubPaths

from test_hub_transfer import SECRET_VALUE, make_source

S_MGR = "11111111-aaaa-4bbb-8ccc-000000000001"  # hội thoại điều phối viên
S_TASK = "22222222-aaaa-4bbb-8ccc-000000000002"  # run của task chưa xong
S_DONE = "33333333-aaaa-4bbb-8ccc-000000000003"  # run đã xong của task đã xong → chỉ profile full
S_FOREIGN = "44444444-aaaa-4bbb-8ccc-000000000004"  # session KHÔNG thuộc hub (Claude Code người dùng tự chạy)
S_LINK = "55555555-aaaa-4bbb-8ccc-000000000005"  # thuộc hub nhưng nằm sau symlink → không lấy
PW = "đúng-passphrase-dài-đủ-12"


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    monkeypatch.setattr(S, "SCRYPT_N", 2**14)


def _lines(cwd: str, sid: str, proj: Path) -> list[str]:
    """Transcript giả, cấu trúc giống Claude Code: dòng user/assistant có `cwd`, tool_use với đường dẫn, tool_result chứa secret,
    file-history-snapshot có KHOÁ là đường dẫn, một dòng hỏng."""
    base = {"sessionId": sid, "cwd": cwd, "version": "2.1.325", "gitBranch": "main", "userType": "external"}
    return [
        json.dumps({**base, "type": "user", "uuid": "u1", "message": {"role": "user", "content": "sửa file giúp tôi — Tiếng Việt"}},
                   ensure_ascii=False),
        json.dumps({**base, "type": "assistant", "uuid": "a1", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": f"{proj}/app.py", "old_string": "x", "new_string": "y"}},
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": f"ls {cwd} && cat {proj}/README.md"}}]}}),
        json.dumps({**base, "type": "user", "uuid": "u2", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": f"token={SECRET_VALUE}\n{proj}/README.md"}]}}),
        json.dumps({"type": "file-history-snapshot", "snapshot": {"trackedFileBackups": {f"{proj}/app.py": {"version": 1}}}}),
        "{hỏng không phải json " + str(proj),
    ]


def _write_session(chome: Path, cwd: str, sid: str, proj: Path, *, sub: bool = False) -> Path:
    d = chome / "projects" / TS.encode_cwd(cwd)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_text("\n".join(_lines(cwd, sid, proj)) + "\n", encoding="utf-8")
    if sub:
        (d / sid / "subagents").mkdir(parents=True)
        (d / sid / "subagents" / "agent-x.jsonl").write_text(json.dumps({"cwd": cwd, "isSidechain": True, "path": f"{proj}/x"}) + "\n")
        (d / sid / "tool-results").mkdir()
        (d / sid / "tool-results" / "r1.txt").write_text("đầu ra dài")
        os.symlink("/etc/hosts", d / sid / "tool-results" / "leak.txt")  # symlink trong cây session → bỏ
    return f


def source(tmp_path: Path, monkeypatch) -> tuple[HubPaths, Path, Path, dict[str, str]]:
    """Hub máy A + claude home A: S_MGR (điều phối viên, có cây subagent), S_TASK (task đang chạy), S_DONE (task xong), S_FOREIGN, S_LINK."""
    src, proj = make_source(tmp_path, monkeypatch)
    chome = tmp_path / "userA" / ".claude"
    monkeypatch.setenv(TS.TEST_ENV, str(chome))
    conv_cwd = str(src.home / "agents" / "a1" / "workspaces" / "conv-mc_0001")
    c = sqlite3.connect(str(src.db))
    with c:
        open_task = c.execute("SELECT id FROM tasks WHERE status='running'").fetchone()[0]
        done_task = c.execute("SELECT id FROM tasks WHERE status='done'").fetchone()[0]
        c.execute("INSERT INTO manager_conversations(id, channel, agent_id, session_id, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                  ("mc_0001", "web", "a1", S_MGR, "t", "t"))
        for rid, sid, tid, st, cwd in (("r_m", S_MGR, None, "completed", conv_cwd), ("r_t", S_TASK, open_task, "failed", str(proj)),
                                       ("r_d", S_DONE, done_task, "completed", str(proj)), ("r_l", S_LINK, None, "completed", str(proj))):
            c.execute("INSERT INTO runs(id, task_id, agent_id, kind, prompt, session_id, cwd, status, queued_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (rid, tid, "a1", "adhoc", "x", sid, cwd, st, "2026-11-02T00:00:00+00:00"))
            c.execute("INSERT INTO sessions(id, agent_id, first_run_id, last_run_id, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                      (sid, "a1", rid, rid, "t", "t"))
    c.close()
    files = {
        S_MGR: _write_session(chome, conv_cwd, S_MGR, proj, sub=True),
        S_TASK: _write_session(chome, str(proj), S_TASK, proj),
        S_DONE: _write_session(chome, str(proj), S_DONE, proj),
        S_FOREIGN: _write_session(chome, str(proj), S_FOREIGN, proj),
    }
    outside = tmp_path / "outside-proj"
    outside.mkdir()
    (outside / f"{S_LINK}.jsonl").write_text(json.dumps({"cwd": str(proj)}) + "\n")
    os.symlink(outside, chome / "projects" / "-linked-dir")
    return src, proj, chome, {"conv_cwd": conv_cwd, **{k: str(v) for k, v in files.items()}}


def dest(tmp_path: Path, monkeypatch) -> tuple[HubPaths, Path]:
    user = tmp_path / "userB"
    user.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(user))
    chome = user / ".claude"
    monkeypatch.setenv(TS.TEST_ENV, str(chome))
    return HubPaths.resolve(user / ".cfharness"), chome


def _members(pkg: Path) -> list[str]:
    with tarfile.open(pkg, "r:") as tf:
        return tf.getnames()


def _mapped(src: HubPaths, dst: HubPaths, tmp_path: Path, p: str) -> str:
    return PathMap([(str(tmp_path / "userA"), str(tmp_path / "userB")), (str(src.home), str(dst.home))]).rewrite(p)


# ============================================================================ quy tắc mã hoá cwd
# (cwd, tên thư mục) QUAN SÁT trên máy dev (~/.claude/projects, Claude Code 2.1.3xx) — 3.655/3.655 thư mục khớp quy tắc
OBSERVED = [
    ("/Users/phuonglq/.cfharness/agents/dieu-phoi/workspaces/conv-mc_02219b054e",
     "-Users-phuonglq--cfharness-agents-dieu-phoi-workspaces-conv-mc-02219b054e"),
    ("/Users/phuonglq/.agent-hub/agents/dieu-phoi/workspaces/conv-mc_318f46bafc",
     "-Users-phuonglq--agent-hub-agents-dieu-phoi-workspaces-conv-mc-318f46bafc"),
    ("/private/tmp/claude-501/-Users-phuonglq-Workspace-agent-hub/e77b5236-4485-4813-8b88-ed8c360a2291/scratchpad/probe",
     "-private-tmp-claude-501--Users-phuonglq-Workspace-agent-hub-e77b5236-4485-4813-8b88-ed8c360a2291-scratchpad-probe"),
    ("/private/tmp/claude-501/-Users-phuonglq-Workspace-agent-hub/7a2b31e7-f727-4750-a243-887611456f93/scratchpad/KhongGianSo/cfharness-test/"
     "Báo cáo (từ web)",
     "-private-tmp-claude-501--Users-phuonglq-Workspace-agent-hub-7a2b31e7-f727-4750-a243-887611456f93-scratchpad-KhongGianSo-cfharness-test-"
     "B-o-c-o--t--web-"),
]


@pytest.mark.parametrize("cwd,name", OBSERVED)
def test_encode_cwd_matches_observed_names(cwd, name):
    assert TS.encode_cwd(cwd) == name


def test_encode_cwd_long_and_astral_match_claude_code():
    # Giá trị chuẩn tính bằng chính hàm JS của Claude Code (WH/AX trong binary 2.1.325) chạy trên node
    long_cwd = "/Users/u/" + "rất-dài/" * 30 + "😀x"
    want = "-Users-u-" + "r-t-d-i-" * 23 + "r-t-d-i-l1456n"
    assert TS.encode_cwd(long_cwd) == want and len(want) == 200 + 1 + len("l1456n")
    assert TS.encode_cwd("/Users/u/a😀b") == "-Users-u-a--b"  # ký tự ngoài BMP = 2 đơn vị UTF-16 → 2 dấu
    assert TS.encode_cwd("C:\\Users\\x\\proj") == "C--Users-x-proj"  # dạng Windows (PR4 kiểm bằng spike thật)


def test_find_session_contract_long_names_prefix_scan(tmp_path):
    """Hợp đồng `claude --resume`: tên thư mục dài lệch hash (bản build khác) vẫn tìm thấy theo tiền tố + cwd."""
    home = tmp_path / "ch"
    cwd = "/Users/u/" + "x" * 250
    other = home / "projects" / (TS.sanitize(cwd)[:200] + "-otherhash")
    other.mkdir(parents=True)
    (other / "s1.jsonl").write_text(json.dumps({"cwd": cwd}) + "\n")
    assert TS.find_session(home, cwd, "s1") == other / "s1.jsonl"
    assert TS.project_dir_for(home / "projects", cwd) == other  # nhập dùng lại thư mục cùng tiền tố có cwd khớp
    assert TS.find_session(home, cwd + "y", "s1") is None
    assert TS.project_dir_for(home / "projects", "/a/b") == home / "projects" / "-a-b"


def test_rewrite_line_parses_rewrites_keys_and_keeps_bad_lines():
    pm = PathMap([("/Users/a", "/home/b")])
    needles = [n.encode() for n in pm.needles()]
    line = json.dumps({"cwd": "/Users/a/p", "m": {"/Users/a/p/f.py": 1, "t": "xem /Users/a/p/x và /Users/ab"}, "v": "Tiếng Việt", "n": 1.5},
                      ensure_ascii=False).encode() + b"\n"
    out = json.loads(TS.rewrite_line(line, pm, needles))
    assert out == {"cwd": "/home/b/p", "m": {"/home/b/p/f.py": 1, "t": "xem /home/b/p/x và /Users/ab"}, "v": "Tiếng Việt", "n": 1.5}
    assert TS.rewrite_line(b"{bad /Users/a/p\n", pm, needles) == b"{bad /Users/a/p\n"
    untouched = b'{"cwd": "/elsewhere"}\n'
    assert TS.rewrite_line(untouched, pm, needles) is untouched


# ============================================================================ xuất
def test_export_full_takes_only_hub_sessions_masks_secrets_no_symlinks(tmp_path, monkeypatch):
    src, _proj, _chome, _f = source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="full")["path"])
    names = _members(pkg)
    assert f"claude/{S_MGR}.jsonl" in names and f"claude/{S_TASK}.jsonl" in names and f"claude/{S_DONE}.jsonl" in names
    assert f"claude/{S_MGR}/subagents/agent-x.jsonl" in names and f"claude/{S_MGR}/tool-results/r1.txt" in names
    assert "claude/index.json" in names
    assert not any(S_FOREIGN in n or S_LINK in n or n.endswith("leak.txt") for n in names), names
    raw = pkg.read_bytes()
    assert SECRET_VALUE.encode() not in raw  # nội dung gzip — kiểm cả bản giải nén bên dưới
    m = T.read_manifest(pkg)
    sec = m["sections"]["claude"]
    assert sec["prefix"] == "claude/" and sec["optional"] is True and sec["sessions"] == 3 and sec["redacted"] is True
    with tarfile.open(pkg, "r:") as tf:
        import gzip

        body = gzip.decompress(tf.extractfile(f"claude/{S_TASK}.jsonl").read()).decode()
        idx = json.loads(gzip.decompress(tf.extractfile("claude/index.json").read()))
    assert SECRET_VALUE not in body and "token=***" in body
    assert set(idx["sessions"]) == {S_MGR, S_TASK, S_DONE} and idx["sessions"][S_MGR]["cwd"].endswith("conv-mc_0001")
    assert idx["sessions"][S_TASK]["masked"] == 1 and idx["claude_home"] == str(tmp_path / "userA" / ".claude")
    assert T.verify_package(pkg)["ok"]


def test_export_core_only_open_work(tmp_path, monkeypatch):
    src, *_ = source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="core")["path"])
    names = [n for n in _members(pkg) if n.startswith("claude/") and n.endswith(".jsonl") and n.count("/") == 1]
    assert sorted(names) == sorted([f"claude/{S_MGR}.jsonl", f"claude/{S_TASK}.jsonl"]), names


def test_export_without_sessions_has_no_claude_section(tmp_path, monkeypatch):
    src, _ = make_source(tmp_path, monkeypatch)
    monkeypatch.setenv(TS.TEST_ENV, str(tmp_path / "empty-claude"))
    m = T.read_manifest(Path(T.export_offline(src, profile="full")["path"]))
    assert "claude" not in m["sections"]


# ============================================================================ nhập
def test_roundtrip_sessions_land_in_reencoded_dirs_with_rewritten_paths(tmp_path, monkeypatch):
    src, proj, _ca, f = source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="full")["path"])
    dst, chome = dest(tmp_path, monkeypatch)
    plan = T.import_plan(dst, pkg)
    cs = plan["claude_sessions"]
    assert cs["present"] and cs["sessions"] == 3 and cs["bytes"] > 0 and not cs["conflicts"] and not (chome / "projects").exists()
    r = T.import_package(dst, pkg)  # mặc định clone — vẫn nhập session
    assert r["mode"] == "clone" and len(r["claude_sessions"]["written"]) == 3
    new_conv = _mapped(src, dst, tmp_path, f["conv_cwd"])
    new_proj = _mapped(src, dst, tmp_path, str(proj))
    assert new_conv.startswith(str(dst.home)) and new_proj.startswith(str(tmp_path / "userB"))
    got = TS.find_session(chome, new_conv, S_MGR)  # đúng file `claude --resume S_MGR` đọc khi chạy trong cwd mới
    assert got == chome / "projects" / TS.encode_cwd(new_conv) / f"{S_MGR}.jsonl"
    assert TS.find_session(chome, new_proj, S_TASK) and TS.find_session(chome, new_proj, S_DONE)
    assert not list(chome.glob(f"projects/*/{S_FOREIGN}.jsonl"))
    text = got.read_text(encoding="utf-8")
    rows = text.splitlines()
    first = json.loads(rows[0])
    assert first["cwd"] == new_conv and first["message"]["content"] == "sửa file giúp tôi — Tiếng Việt"
    tool = json.loads(rows[1])["message"]["content"]
    assert tool[0]["input"]["file_path"] == f"{new_proj}/app.py" and tool[1]["input"]["command"] == f"ls {new_conv} && cat {new_proj}/README.md"
    assert json.loads(rows[3])["snapshot"]["trackedFileBackups"] == {f"{new_proj}/app.py": {"version": 1}}
    assert rows[4] == "{hỏng không phải json " + str(proj)  # dòng không parse được giữ nguyên
    assert "***" in rows[2] and SECRET_VALUE not in text
    assert str(tmp_path / "userA") not in "\n".join(rows[:4])
    sub = got.parent / S_MGR / "subagents" / "agent-x.jsonl"
    assert json.loads(sub.read_text())["path"] == f"{new_proj}/x" and (got.parent / S_MGR / "tool-results" / "r1.txt").read_text() == "đầu ra dài"
    assert stat.S_IMODE(os.stat(got).st_mode) == 0o600 and stat.S_IMODE(os.stat(got.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(chome / "projects").st_mode) == 0o700
    # DB đích: cwd của run khớp thư mục session mới (resume dùng runs.cwd)
    c = sqlite3.connect(str(dst.db))
    assert c.execute("SELECT cwd FROM runs WHERE id='r_m'").fetchone()[0] == new_conv
    c.close()


def test_same_sid_on_target_is_never_overwritten(tmp_path, monkeypatch):
    src, *_ = source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="full")["path"])
    dst, chome = dest(tmp_path, monkeypatch)
    mine = chome / "projects" / "-somewhere-else" / f"{S_TASK}.jsonl"
    mine.parent.mkdir(parents=True)
    mine.write_text("của máy B\n")
    plan = T.import_plan(dst, pkg)
    assert plan["claude_sessions"]["conflicts"] == [S_TASK] and any("trùng id" in w for w in plan["warnings"])
    r = T.import_package(dst, pkg)
    assert r["claude_sessions"]["conflicts"] == [S_TASK] and {w["sid"] for w in r["claude_sessions"]["written"]} == {S_MGR, S_DONE}
    assert mine.read_text() == "của máy B\n" and len(list(chome.glob(f"projects/*/{S_TASK}.jsonl"))) == 1


def test_failure_after_sessions_written_removes_them(tmp_path, monkeypatch):
    src, *_ = source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="full")["path"])
    assert T.seal_package(pkg, src.home, PW)["sealed"]
    dst, chome = dest(tmp_path, monkeypatch)
    (chome / "projects").mkdir(parents=True)
    keep = chome / "projects" / "-mine" / "other.jsonl"
    keep.parent.mkdir()
    keep.write_text("x")

    def boom(*a, **k):  # bước ghi secret (sau khi session đã ghi) lỗi
        raise OSError("ghi secret lỗi (giả)")

    monkeypatch.setattr(S, "apply", boom)
    with pytest.raises(HubError, match="trở lại trống"):
        T.import_package(dst, pkg, passphrase=PW)
    left = sorted(p.relative_to(chome).as_posix() for p in chome.rglob("*"))
    assert left == ["projects", "projects/-mine", "projects/-mine/other.jsonl"], left
    assert T.home_blank(dst)


def test_failure_inside_session_install_cleans_partial(tmp_path, monkeypatch):
    src, *_ = source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="full")["path"])
    dst, chome = dest(tmp_path, monkeypatch)
    real = TS._write_new
    n = {"i": 0}

    def flaky(*a, **k):
        n["i"] += 1
        if n["i"] == 3:
            raise OSError("đĩa đầy (giả)")
        return real(*a, **k)

    monkeypatch.setattr(TS, "_write_new", flaky)
    with pytest.raises(HubError, match="trở lại trống"):
        T.import_package(dst, pkg)
    assert not chome.exists() or not list(chome.rglob("*.jsonl"))
    assert T.home_blank(dst)


def test_old_importer_skips_claude_section_with_warning(tmp_path, monkeypatch):
    """Bản cfharness chưa biết phần `claude` (PR2): `optional` → bỏ qua kèm cảnh báo, vẫn nhập được phần còn lại."""
    src, *_ = source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="full")["path"])
    dst, chome = dest(tmp_path, monkeypatch)
    monkeypatch.setattr(T, "KNOWN_SECTIONS", ("db", "home", "runs", "secrets"))
    real_dest = T._member_dest
    monkeypatch.setattr(T, "_member_dest", lambda n: None if n.startswith("claude/") else real_dest(n))
    v = T.verify_package(pkg)
    assert v["ok"] and any("'claude'" in w for w in v["warnings"]), v
    T.import_package(dst, pkg)
    assert dst.db.exists()
    assert not chome.exists() or not list(chome.rglob("*.jsonl"))


def test_unsafe_claude_member_names_rejected():
    for bad in (("claude", "../x.jsonl"), ("claude", "x.txt"), ("claude", "bad id!", "a"), ("claude", "s1", ".."), ("claude",)):
        assert TS.member_dest(bad) is None, bad
    assert str(TS.member_dest(("claude", "s1.jsonl"))) == "claude/s1.jsonl"
    assert str(TS.member_dest(("claude", "s1", "subagents", "a.jsonl"))) == "claude/s1/subagents/a.jsonl"


def test_cli_plan_text_mentions_sessions(tmp_path, monkeypatch):
    from cfharness.hub_transfer_cli import _plan_text

    src, *_ = source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="core")["path"])
    dst, _chome = dest(tmp_path, monkeypatch)
    txt = _plan_text(T.import_plan(dst, pkg))
    assert "Session Claude Code: 2 session" in txt and "che secret" in txt and "clone" in txt
