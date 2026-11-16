"""Nhập hub KHÁC hệ điều hành / khác hệ thống file (pathmap.py hai phía, transfer_os.py) — PR4 của docs/dev-notes/plan-hub-clone.md §5b.

Chạy được trên macOS/Linux: "máy Windows" được mô phỏng —
- gói "từ Windows" = gói xuất thật rồi đổi mọi đường dẫn sang `C:\\Users\\a\\…` (DB, file, transcript, manifest `source.os=windows`);
- đích "Windows" = tham số `target_os="windows"` + `--map-path` sang `C:\\Users\\b\\…` (file vẫn ghi vào thư mục tạm của máy này).
Test cần Windows thật đánh dấu `skipif`. Không gọi claude, không mạng.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest
import yaml

from cfharness import hub_transfer as T
from cfharness import transfer_os as TO
from cfharness import transfer_sessions as TS
from cfharness.errors import HubError
from cfharness.pathmap import PathMap, db_rewrite, is_abs, path_style, syntax_for
from cfharness.paths import HubPaths

from test_hub_transfer import _rewrite_pkg, make_source
from test_hub_transfer_sessions import S_MGR, S_TASK, dest, source

BS = "\\"
WA = r"C:\Users\a"  # $HOME của "máy Windows" nguồn
WB = r"C:\Users\b"  # $HOME của "máy Windows" đích


# ============================================================================ pathmap hai phía
def test_pathmap_posix_to_windows_tail_separators_only_on_path_tokens():
    pm = PathMap([("/Users/a", WB), ("/Users/a/.cfharness", WB + r"\.cfharness")])
    r = pm.rewrite
    assert r("/Users/a/Workspace/proj") == WB + r"\Workspace\proj"
    assert r("/Users/a/.cfharness/agents/a1") == WB + r"\.cfharness\agents\a1"  # cặp dài trước
    assert r("/Users/a") == WB and r("/Users/ab/x /Users/a2") == "/Users/ab/x /Users/a2"  # ranh giới đúng
    # lệnh shell: hai đường dẫn, `&&`, đường dẫn tương đối không đụng, URL không đụng
    assert r("cd /Users/a/x && ls src/y; curl https://e.com/p/q") == rf"cd {WB}\x && ls src/y; curl https://e.com/p/q"
    assert r("cp /Users/a/x /Users/a/y") == rf"cp {WB}\x {WB}\y"
    # thư mục có khoảng trắng (đoạn sau là đường dẫn tương đối) — vẫn cùng token
    assert r("/Users/a/Library/Application Support/x y") == WB + r"\Library\Application Support\x y"
    # chữ tự do: ngoặc markdown, `:dòng`, dấu câu, phép chia
    assert r("[f](/Users/a/x/f.md) và (/Users/a/z), 10/2") == rf"[f]({WB}\x\f.md) và ({WB}\z), 10/2"
    assert r("lỗi ở /Users/a/x/app.py:12") == rf"lỗi ở {WB}\x\app.py:12"
    assert r("/Users/a/x/(1)/y") == WB + r"\x\(1)\y"


def test_pathmap_posix_to_windows_json_and_yaml_stay_valid():
    pm = PathMap([("/Users/a", WB)])
    v = {"cwd": "/Users/a/Workspace/proj", "u": "https://x.com/a/b", "cmd": 'cd "/Users/a/x" && ls\n/Users/a/y', "/Users/a/k": 1}
    out = pm.rewrite(json.dumps(v, ensure_ascii=False))  # auto: chuỗi là JSON hợp lệ → dạng escape
    got = json.loads(out)
    assert got == {"cwd": WB + r"\Workspace\proj", "u": "https://x.com/a/b", "cmd": f'cd "{WB}\\x" && ls\n{WB}\\y', WB + r"\k": 1}
    # file .json ép cú pháp json; YAML: nháy kép → escape, nháy đơn / không nháy → thô
    y = "a: '/Users/a/x'\nb: \"/Users/a/y/z\"\nc: /Users/a/w\nd: [/Users/a/p, '/Users/a/q']\n"
    assert yaml.safe_load(pm.rewrite(y, syntax="yaml")) == {"a": WB + r"\x", "b": WB + r"\y\z", "c": WB + r"\w", "d": [WB + r"\p", WB + r"\q"]}
    assert syntax_for("policy/protected.yaml") == "yaml" and syntax_for("x/checkpoint.json") == "json" and syntax_for("a.md") == "auto"
    # chữ thường (không phải JSON) có dấu nháy → thô
    assert pm.rewrite('say "/Users/a/x"') == f'say "{WB}\\x"'


def test_pathmap_windows_to_posix_all_forms_and_drive_case():
    pm = PathMap([(WA, "/home/b"), (WA + r"\.cfharness", "/home/b/.cfharness")])
    r = pm.rewrite
    assert r(WA + r"\Workspace\p q\r") == "/home/b/Workspace/p q/r"
    assert r(json.dumps({"cwd": WA + r"\x\y", "o": r"c:\users\a"})) == json.dumps({"cwd": "/home/b/x/y", "o": r"c:\users\a"})
    assert r("c:/Users/a/x/y") == "/home/b/x/y" and r(r"c:\Users\a\x") == "/home/b/x"  # chữ ổ đĩa không phân biệt hoa/thường
    assert r(WA + r"\.cfharness\agents\a1 và C:\Users\ab") == r"/home/b/.cfharness/agents/a1 và C:\Users\ab"
    assert json.loads(r(json.dumps(WA + "\\x\nline"))) == "/home/b/x\nline"  # escape JSON `\n` kết thúc token
    assert r(r"D:\repos\x") == r"D:\repos\x"
    needles = pm.needles()
    assert {WA, "c" + WA[1:], WA.replace(BS, BS * 2), "C:/Users/a"} <= set(needles)


def test_pathmap_windows_to_windows_keeps_form_and_kinds():
    pm = PathMap([(WA, r"D:\hub"), ("C:/Users/a/.cfharness/", r"D:\hub\h")])  # dạng `/` + dấu cuối được chuẩn hoá
    assert pm.as_list() == [{"from": WA + r"\.cfharness", "to": r"D:\hub\h"}, {"from": WA, "to": r"D:\hub"}]
    assert pm.rewrite(WA + r"\x") == r"D:\hub\x" and pm.rewrite("C:/Users/a/x") == "D:/hub/x"
    assert pm.rewrite(json.dumps(WA + r"\.cfharness\y")) == json.dumps(r"D:\hub\h\y")
    assert path_style(r"C:\x") == path_style("c:/x") == path_style(r"\\srv\share\x") == "windows" and path_style("/x") == "posix"
    assert path_style("x/y") is None and path_style("//x") is None and is_abs(r"C:\x") and not is_abs("C:x")
    assert T.parse_map_paths([r"C:\Users\a\repos=/Users/b/repos"]) == [(r"C:\Users\a\repos", "/Users/b/repos")]
    with pytest.raises(ValueError):
        T.parse_map_paths(["repos=/x"])


def test_db_rewrite_posix_to_windows_json_columns_stay_valid(tmp_path):
    db = tmp_path / "x.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE hub_state(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("CREATE TABLE runs(id TEXT, cwd TEXT, n INTEGER)")
    c.execute("INSERT INTO hub_state VALUES ('k', ?)", (json.dumps({"p": "/Users/a/x/y", "l": ["/Users/a"]}),))
    c.execute("INSERT INTO runs VALUES ('r', '/Users/a/proj/sub', 1)")
    c.commit()
    c.close()
    assert db_rewrite(db, PathMap([("/Users/a", WB)]), apply=True) == {"hub_state.value": 1, "runs.cwd": 1}
    c = sqlite3.connect(db)
    assert json.loads(c.execute("SELECT value FROM hub_state").fetchone()[0]) == {"p": WB + r"\x\y", "l": [WB]}
    assert c.execute("SELECT cwd FROM runs").fetchone()[0] == WB + r"\proj\sub"
    c.close()


def test_rewrite_line_jsonl_windows_to_posix():
    pm = PathMap([(WA, "/home/b")])
    line = json.dumps({"cwd": WA + r"\p", "message": {"content": [{"input": {"command": rf"dir {WA}\p\src && type {WA}\p\a.txt"}}]}}).encode() + b"\n"
    out = json.loads(TS.rewrite_line(line, pm, [n.encode() for n in pm.needles()]))
    assert out["cwd"] == "/home/b/p" and out["message"]["content"][0]["input"]["command"] == "dir /home/b/p/src && type /home/b/p/a.txt"


def test_encode_cwd_windows_paths():
    assert TS.encode_cwd(r"C:\Users\x") == "C--Users-x"
    assert TS.encode_cwd(r"C:\Users\x\.cfharness\agents\a1\workspaces\conv-mc_1") == "C--Users-x--cfharness-agents-a1-workspaces-conv-mc-1"
    assert TS.encode_cwd(r"D:\Dự án\p q") == "D--D---n-p-q"


# ============================================================================ tên file ở đích
def test_invalid_reason_and_safe_segment_deterministic():
    for seg in ("a:b", "a?b", "x*", "CON", "con.txt", "Nul.tar.gz", "COM1", "lpt9.md", "trail.", "trail ", "a\x01b", 'q"t', "COM\u00b9"):
        assert TO.invalid_reason(seg), seg
    for seg in ("cong", "con-x", "console.md", "COM10", "a_b", "Báo cáo (1).md", ".archive"):
        assert TO.invalid_reason(seg) is None, seg
    assert TO.safe_segment("a:b?.md") == "a_b_.md" and TO.safe_segment("con.txt") == "con_.txt" and TO.safe_segment("trail.") == "trail_"
    assert TO.safe_segment("con", skill=True) == "con-skill" and TO.safe_segment("aux", skill=True) == "aux-skill"
    assert TO.safe_segment("x.", skill=True) == "x-"
    for s in ("con", "aux", "lpt1"):
        assert TO.SKILL_NAME_RE.match(TO.safe_segment(s, skill=True)) and not TO.invalid_reason(TO.safe_segment(s, skill=True))


def test_plan_names_windows_target_renames_knowledge_refuses_rest_skips_claude():
    rels = ["agents/a1/MEMORY.md", "agents/a1/skills/con/SKILL.md", "agents/a1/skills/con/references/a:b.md",
            "agents/a1/skills/con/references/a_b.md", "agents/a1/skills/x/references/nul.txt", "global/skills/aux/SKILL.md",
            "teams/t1/skills/com1/SKILL.md", "agents/a1/.archive/skills/old__1/trail.", "trash/agents/z__1/f?.md",
            "agents/con/MEMORY.md", "policy/a?b.yaml", "claude/sid1/tool-results/a:b.txt", "claude/sid1.jsonl", "runs/r1.jsonl"]
    p = TO.plan_names(rels, target_os="windows", case_insensitive=False)
    assert p["renames"] == {
        "agents/a1/skills/con/SKILL.md": "agents/a1/skills/con-skill/SKILL.md",
        "agents/a1/skills/con/references/a:b.md": "agents/a1/skills/con-skill/references/a_b_2.md",  # trùng a_b.md sau khi đổi
        "agents/a1/skills/con/references/a_b.md": "agents/a1/skills/con-skill/references/a_b.md",
        "agents/a1/skills/x/references/nul.txt": "agents/a1/skills/x/references/nul_.txt",
        "global/skills/aux/SKILL.md": "global/skills/aux-skill/SKILL.md",
        "teams/t1/skills/com1/SKILL.md": "teams/t1/skills/com1-skill/SKILL.md",
        "agents/a1/.archive/skills/old__1/trail.": "agents/a1/.archive/skills/old__1/trail_",
        "trash/agents/z__1/f?.md": "trash/agents/z__1/f_.md",
    }
    assert {(s["scope"], s["owner"], s["old"], s["new"]) for s in p["skills"]} == {
        ("agent", "a1", "con", "con-skill"), ("global", "global", "aux", "aux-skill"), ("team", "t1", "com1", "com1-skill")}
    assert [x["path"] for x in p["refused"]] == ["agents/con/MEMORY.md", "policy/a?b.yaml"]
    assert "tên dành riêng" in p["refused"][0]["reason"] and [x["path"] for x in p["skipped"]] == ["claude/sid1/tool-results/a:b.txt"]
    assert TO.plan_names(reversed(rels), target_os="windows", case_insensitive=False) == p  # tất định
    # đích POSIX phân biệt hoa/thường: không đổi gì
    q = TO.plan_names(rels, target_os="posix", case_insensitive=False)
    assert not q["renames"] and not q["refused"] and not q["skipped"]


def test_plan_names_case_insensitive_target_collisions():
    rels = ["agents/a1/skills/s1/references/NOTE.md", "agents/a1/skills/s1/references/note.md", "agents/a1/skills/s1/SKILL.md",
            "agents/a1/MEMORY.md", "agents/a1/memory.md", "policy/A.yaml", "policy/a.yaml", "claude/x/A.txt", "claude/x/a.txt"]
    p = TO.plan_names(rels, target_os="posix", case_insensitive=True)  # vd macOS (APFS mặc định)
    assert p["renames"] == {"agents/a1/skills/s1/references/note.md": "agents/a1/skills/s1/references/note_2.md"}
    assert [x["path"] for x in p["refused"]] == ["agents/a1/memory.md", "policy/a.yaml"]
    assert "hoa/thường" in p["refused"][0]["reason"] and [x["path"] for x in p["skipped"]] == ["claude/x/a.txt"]
    assert not TO.plan_names(rels, target_os="posix", case_insensitive=False)["refused"]


def test_fs_case_insensitive_probe_matches_real_fs(tmp_path):
    (tmp_path / "Probe").mkdir()
    real = (tmp_path / "pRoBE").exists()
    assert TO.fs_case_insensitive(tmp_path / "Probe") is real
    assert TO.fs_case_insensitive(tmp_path / "not" / "yet" / "there") is TO.fs_case_insensitive(tmp_path)  # dò tổ tiên gần nhất, không tạo
    assert not (tmp_path / "not").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="cần Windows thật: kiểm luật tên với hệ thống file NTFS")
def test_invalid_reason_agrees_with_real_windows_fs(tmp_path):  # pragma: no cover — chỉ Windows
    for seg in ("a:b", "CON", "nul.txt", "x?", "trail."):
        assert TO.invalid_reason(seg)
        p = tmp_path / seg
        try:
            p.write_text("x")
            ok = (tmp_path / seg).name in os.listdir(tmp_path)
        except OSError:
            ok = False
        assert not ok, seg


def test_mcp_command_problem_kinds(tmp_path):
    exe = tmp_path / "bin" / "tool"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    assert TO.command_problem(str(exe), target_os="posix") is None
    assert "không có file" in TO.command_problem(str(tmp_path / "nope"), target_os="posix")
    assert "kiểu windows" in TO.command_problem(r"C:\Program Files\nodejs\node.exe", target_os="posix")
    assert "kiểu posix" in TO.command_problem("/opt/homebrew/bin/node", target_os="windows")
    assert TO.command_problem("sh", target_os="posix") is None
    assert "PATH" in TO.command_problem("cfh-no-such-cmd-xyz", target_os="posix")


# ============================================================================ gói giả "từ Windows" → nhập trên máy này
def _walk(o, fn):
    if isinstance(o, str):
        return fn(o)
    if isinstance(o, dict):
        return {(fn(k) if isinstance(k, str) else k): _walk(v, fn) for k, v in o.items()}
    if isinstance(o, list):
        return [_walk(x, fn) for x in o]
    return o


def _windowsify(pkg: Path, pairs: list[tuple[str, str]], extra_state: dict | None = None) -> Path:
    """Gói xuất thật (POSIX) → gói như xuất từ máy Windows: mọi đường dẫn theo `pairs` đổi sang kiểu Windows (DB qua pathmap — cột JSON
    thành dạng escape —, file cấu hình, transcript từng dòng, index session, manifest `source.os=windows`), sha256/cỡ tính lại."""
    pm = PathMap(pairs)
    out = pkg.with_name("from-windows.cfhx")
    m = T.read_manifest(pkg)
    work = Path(tempfile.mkdtemp(dir=pkg.parent))
    files = []
    with tarfile.open(pkg, "r:") as src, tarfile.open(out, "w:", format=tarfile.PAX_FORMAT) as dst:
        metas = {f["path"]: f for f in m["files"]}
        for mem in src:
            if mem.name == T.MANIFEST:
                continue
            raw = src.extractfile(mem).read()
            data = gzip.decompress(raw) if metas[mem.name]["gz"] else raw
            if mem.name == T.DB_MEMBER:
                f = work / "hub.db"
                f.write_bytes(data)
                db_rewrite(f, pm, apply=True)
                if extra_state:
                    c = sqlite3.connect(f)
                    with c:
                        for k, v in extra_state.items():
                            c.execute("INSERT OR REPLACE INTO hub_state(key, value, updated_at) VALUES (?,?,?)", (k, json.dumps(v), "t"))
                    c.close()
                data = f.read_bytes()
            elif mem.name == "claude/index.json":
                idx = _walk(json.loads(data), lambda s: pm.rewrite(s, syntax="text"))
                for e in idx["sessions"].values():
                    e["project_dir"] = TS.encode_cwd(e["cwd"])
                data = json.dumps(idx, ensure_ascii=False).encode()
            elif mem.name.endswith(".jsonl") and mem.name.startswith("claude/"):
                nd = [n.encode() for n in pm.needles()]
                data = b"".join(TS.rewrite_line(ln, pm, nd) for ln in io.BytesIO(data))
            elif mem.name.startswith("home/") and mem.name.endswith((".yaml", ".json", ".md")):
                data = pm.rewrite(data.decode(), syntax=syntax_for(mem.name)).encode()
            files.append(T._add_member(dst, mem.name, io.BytesIO(data), work, level=1))
        m["files"] = files
        m["source"] = {**m["source"], "os": "windows", "sep": BS, "home": pm.rewrite(m["source"]["home"]), "home_real": pm.rewrite(m["source"]["home_real"]),
                       "user_home": pm.rewrite(m["source"]["user_home"])}
        m["path_roots"] = _walk(m["path_roots"], lambda s: pm.rewrite(s, syntax="text"))
        mb = json.dumps(m, ensure_ascii=False).encode()
        dst.addfile(T._tarinfo(T.MANIFEST, len(mb)), io.BytesIO(mb))
    return out


def _add_mcp(home: Path, servers: dict[str, str]) -> None:
    c = sqlite3.connect(home / "hub.db")
    with c:
        for name, cmd in servers.items():
            cfg = {"name": name, "transport": "stdio", "command": cmd, "args": []}
            c.execute("INSERT INTO mcp_servers (name, transport, config, trust, tools_include, tools_exclude, source, created_at, updated_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (name, "stdio", json.dumps(cfg), "untrusted", "[]", "[]", "manual", "t", "t"))
    c.close()


def test_import_package_from_windows_on_this_machine(tmp_path, monkeypatch):
    from cfharness.hub import Hub
    from cfharness.runtime.fake import FakeRuntime

    src, proj, chome_a, f = source(tmp_path, monkeypatch)
    _add_mcp(src.home, {"winnode": "/never/nodejs/node.exe", "plain": "sh"})
    pkg = Path(T.export_offline(src, profile="full")["path"])
    ua = tmp_path / "userA"
    wpkg = _windowsify(pkg, [(str(ua), WA), (str(chome_a), WA + r"\.claude"), ("/never/nodejs", r"C:\Program Files\nodejs")],
                       extra_state={"win_forward": {"p": "c:/Users/a/Workspace/proj/x"}})
    m = T.read_manifest(wpkg)
    assert m["source"]["os"] == "windows" and m["path_roots"]["hub_home"] == WA + r"\.cfharness"
    assert T.verify_package(wpkg)["ok"]

    dst, chome_b = dest(tmp_path, monkeypatch)
    ub = tmp_path / "userB"
    plan = T.import_plan(dst, wpkg, claude_home=chome_b)
    assert not plan["blockers"], plan["blockers"]
    assert {"from": WA + r"\.cfharness", "to": str(dst.home)} in plan["path_map"] and {"from": WA, "to": str(ub)} in plan["path_map"]
    assert any("gói từ máy windows" in w for w in plan["warnings"]) and any("install-service" in w for w in plan["warnings"])
    assert plan["mcp_missing"] == [{"server": "winnode", "command": r"C:\Program Files\nodejs\node.exe", "reason": "đường dẫn kiểu windows trên máy posix"}]
    assert plan["claude_sessions"]["sessions"] == 3 and not dst.home.exists()

    r = T.import_package(dst, wpkg, claude_home=chome_b)
    assert r["claude_sessions"]["written"] and r["mcp_missing"][0]["server"] == "winnode"
    new_proj = str(ub / "Workspace" / "proj")
    new_conv = str(dst.home / "agents" / "a1" / "workspaces" / "conv-mc_0001")
    c = sqlite3.connect(dst.db)
    c.row_factory = sqlite3.Row
    a1 = c.execute("SELECT home_path, workspace_default FROM agents WHERE id='a1'").fetchone()
    assert a1["home_path"] == str(dst.home / "agents" / "a1") and a1["workspace_default"] == new_proj
    st = {r_["key"]: json.loads(r_["value"]) for r_ in c.execute("SELECT key, value FROM hub_state")}
    assert st["keep_me"] == {"p": new_proj} and st["win_forward"] == {"p": new_proj + "/x"}
    assert c.execute("SELECT cwd FROM runs WHERE id='r_m'").fetchone()[0] == new_conv
    mcp = {x["name"]: x["probe_error"] for x in c.execute("SELECT name, probe_error FROM mcp_servers")}
    assert mcp["winnode"].startswith("[hub]") and "cfharness mcp edit winnode" in mcp["winnode"] and mcp["plain"] is None
    assert st["import_followups"]["mcp_missing"][0]["server"] == "winnode"
    blob = "\n".join(str(v) for v in st.values())
    assert WA not in blob and "C:/Users/a" not in blob and "c:/Users/a" not in blob
    c.close()
    assert WA not in dst.config.read_text() and str(ub / "Workspace") in dst.config.read_text()
    assert (dst.home / "policy" / "protected.yaml").read_text().strip().endswith(f"{new_proj}/prod")
    # transcript: thư mục theo cwd POSIX mới, cwd + đường dẫn trong tool_use về dạng POSIX
    got = TS.find_session(chome_b, new_conv, S_MGR)
    assert got is not None and got.parent.name == TS.encode_cwd(new_conv)
    rows = got.read_text(encoding="utf-8").splitlines()
    assert json.loads(rows[0])["cwd"] == new_conv
    assert json.loads(rows[1])["message"]["content"][0]["input"]["file_path"] == f"{new_proj}/app.py"
    assert TS.find_session(chome_b, new_proj, S_TASK)
    h = Hub(dst, runtime=FakeRuntime())
    try:
        res = h.integrity.check(restore=False)
        assert not res.get("policy") and not res.get("config"), res
        assert h.store.get_agent("a1")["workspace_default"] == new_proj
    finally:
        h.store.close()


# ============================================================================ gói POSIX → đích "Windows" (mô phỏng)
def _win_source(tmp_path, monkeypatch):
    src, proj, chome_a, f = source(tmp_path, monkeypatch)
    from cfharness.hub import Hub
    from cfharness.runtime.fake import FakeRuntime

    sk = src.home / "agents" / "a1" / "skills" / "con"
    (sk / "references").mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: con\ndescription: đọc console\n---\n\nXem references/a:b.md\n")
    (sk / "references" / "a:b.md").write_text("ghi chú")  # `:` hợp lệ trên POSIX
    h = Hub(src, runtime=FakeRuntime())
    try:
        h.knowledge.sync_index("agent", "a1")
        h.integrity.rebaseline("test")
    finally:
        h.store.close()
    _add_mcp(src.home, {"brew": "/opt/homebrew-never/bin/node"})
    return src, proj, chome_a, f


def test_import_posix_package_into_simulated_windows_target(tmp_path, monkeypatch):
    src, proj, _ca, f = _win_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="full")["path"])
    dst, chome_b = dest(tmp_path, monkeypatch)
    ua = str(tmp_path / "userA")
    maps = [(ua, WB), (str(src.home), WB + r"\.cfharness")]
    kw = {"maps": maps, "target_os": "windows", "case_insensitive": True, "claude_home": chome_b}
    plan = T.import_plan(dst, pkg, **kw)
    assert not plan["blockers"], plan["blockers"]
    assert plan["target_os"] == "windows" and {"from": "agents/a1/skills/con/SKILL.md", "to": "agents/a1/skills/con-skill/SKILL.md"} in plan["renamed"]
    assert any("sandbox" in w for w in plan["warnings"]) and any("đổi tên" in w for w in plan["warnings"])
    assert plan["mcp_missing"][0]["reason"] == "đường dẫn kiểu posix trên máy windows"

    r = T.import_package(dst, pkg, **kw)
    sk = dst.home / "agents" / "a1" / "skills" / "con-skill"
    assert (sk / "references" / "a_b.md").read_text() == "ghi chú" and not (dst.home / "agents" / "a1" / "skills" / "con").exists()
    assert "name: con-skill\n" in (sk / "SKILL.md").read_text()
    c = sqlite3.connect(dst.db)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT name, path FROM skills WHERE owner_id='a1'").fetchone()
    assert row["name"] == "con-skill" and row["path"] == WB + r"\.cfharness\agents\a1\skills\con-skill"
    a1 = c.execute("SELECT home_path, workspace_default FROM agents WHERE id='a1'").fetchone()
    assert a1["home_path"] == WB + r"\.cfharness\agents\a1" and a1["workspace_default"] == WB + r"\Workspace\proj"
    st = {x["key"]: json.loads(x["value"]) for x in c.execute("SELECT key, value FROM hub_state")}
    assert st["keep_me"] == {"p": WB + r"\Workspace\proj"}  # cột JSON: escape đúng, đọc lại được
    fu = st["import_followups"]
    assert {"from": "agents/a1/skills/con/references/a:b.md", "to": "agents/a1/skills/con-skill/references/a_b.md"} in fu["renamed"]
    assert fu["skills_renamed"] == [{"scope": "agent", "owner": "a1", "old": "con", "new": "con-skill"}] and fu["mcp_missing"][0]["server"] == "brew"
    c.close()
    cfg = yaml.safe_load(dst.config.read_text())
    assert WB + r"\Workspace" in cfg["workspace"]["allowed_roots"]
    assert yaml.safe_load((dst.home / "policy" / "protected.yaml").read_text())["paths"] == [WB + r"\Workspace\proj\prod"]
    # session: thư mục `C--Users-b-…`, cwd kiểu Windows
    win_conv = WB + r"\.cfharness\agents\a1\workspaces\conv-mc_0001"
    got = TS.find_session(chome_b, win_conv, S_MGR)
    assert got is not None and got.parent.name == "C--Users-b--cfharness-agents-a1-workspaces-conv-mc-0001"
    rows = got.read_text(encoding="utf-8").splitlines()
    assert json.loads(rows[0])["cwd"] == win_conv
    tool = json.loads(rows[1])["message"]["content"]
    assert tool[0]["input"]["file_path"] == WB + r"\Workspace\proj\app.py"
    assert tool[1]["input"]["command"] == rf"ls {win_conv} && cat {WB}\Workspace\proj\README.md"
    assert r["renamed"] and r["target_os"] == "windows"


def test_windows_target_refuses_unrenamable_names_before_writing(tmp_path, monkeypatch):
    src, _proj = make_source(tmp_path, monkeypatch)
    from cfharness.hub import Hub
    from cfharness.runtime.fake import FakeRuntime

    h = Hub(src, runtime=FakeRuntime())
    try:
        h.agents.create({"id": "nul", "name": "Nul", "model": "haiku"})  # hợp lệ trên POSIX, tên dành riêng trên Windows
    finally:
        h.store.close()
    pkg = Path(T.export_offline(src, profile="core")["path"])
    dst = HubPaths.resolve(tmp_path / "dst")
    plan = T.import_plan(dst, pkg, target_os="windows")
    assert plan["blockers"] and any("agents/nul" in b and "đổi tên ở máy nguồn" in b for b in plan["blockers"]), plan["blockers"]
    with pytest.raises(HubError, match="không nhập được vào máy này"):
        T.import_package(dst, pkg, target_os="windows")
    assert not dst.db.exists() and not any(dst.home.glob(".import-*"))
    # cùng gói vào đích POSIX: không chặn
    assert not T.import_plan(dst, pkg, target_os="posix", case_insensitive=False)["blockers"]


def test_case_insensitive_target_renames_knowledge_and_refuses_other(tmp_path, monkeypatch):
    import hashlib

    src, _proj = make_source(tmp_path, monkeypatch)
    pkg = Path(T.export_offline(src, profile="core")["path"])

    def add(names: list[str]):
        extra, entries = [], []
        for n in names:
            data = f"nội dung {n}".encode()
            ti = tarfile.TarInfo(n)
            ti.size = len(data)
            extra.append((ti, data))
            entries.append({"path": n, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "gz": False, "packed": len(data)})
        _rewrite_pkg(pkg, extra=extra, manifest_fn=lambda m: {**m, "files": [*m["files"], *entries]})

    add(["home/agents/a1/skills/s1/SKILL.md", "home/agents/a1/skills/s1/references/NOTE.md", "home/agents/a1/skills/s1/references/note.md"])
    dst = HubPaths.resolve(tmp_path / "userB" / ".cfharness")
    monkeypatch.setenv("HOME", str(tmp_path / "userB"))
    plan = T.import_plan(dst, pkg, case_insensitive=True)
    assert not plan["blockers"] and plan["renamed"] == [{"from": "agents/a1/skills/s1/references/note.md",
                                                         "to": "agents/a1/skills/s1/references/note_2.md"}], plan
    r = T.import_package(dst, pkg, case_insensitive=True)
    ref = dst.home / "agents" / "a1" / "skills" / "s1" / "references"
    assert sorted(os.listdir(ref)) == ["NOTE.md", "note_2.md"] and (ref / "note_2.md").read_text() == "nội dung home/agents/a1/skills/s1/references/note.md"
    assert r["renamed"]
    # tệp không thuộc tri thức trùng hoa/thường → từ chối
    pkg2 = Path(T.export_offline(src, profile="core")["path"])
    pkg = pkg2
    add(["home/agents/a1/memory.md"])
    plan = T.import_plan(HubPaths.resolve(tmp_path / "d2"), pkg2, case_insensitive=True)
    assert any("hoa/thường" in b for b in plan["blockers"]), plan["blockers"]
