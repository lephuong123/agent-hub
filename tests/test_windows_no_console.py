"""Windows: không bật cửa sổ console (lỗi "terminal bật lên rồi tắt liên tục", docs/dev-notes/windows.md §6).

- quét mã nguồn: mọi subprocess.run/Popen/call/check_output/check_call + asyncio.create_subprocess_* trong `src/cfharness`
  phải dùng `no_window_kwargs` / `new_group_kwargs` / `detached_kwargs`, hoặc nằm trong ALLOWLIST kèm lý do;
- `oscompat.no_window_kwargs`, `oscompat.ensure_hidden_console`;
- chọn pythonw GUI cho hook/MCP của run (`guard.runtime.child_python`) — giả `IS_WINDOWS` như test_windows_guard_runtime.
Không gọi `claude` thật."""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from cfharness import oscompat
from cfharness.guard import runtime as rt
from cfharness.observe_hook import add_observe_hooks

SRC = Path(__file__).resolve().parent.parent / "src" / "cfharness"
SPAWN_SUBPROCESS = {"run", "Popen", "call", "check_output", "check_call"}
SPAWN_ASYNCIO = {"create_subprocess_exec", "create_subprocess_shell"}
SAFE_HELPERS = {"no_window_kwargs", "new_group_kwargs", "detached_kwargs"}

# (file, hàm bao quanh) → lý do được spawn KHÔNG qua helper
ALLOWLIST = {
    ("tui/cli.py", "cmd_takeover"): "takeover TƯƠNG TÁC: `claude --resume` phải dùng console của người",
    ("tui/app.py", "takeover"): "takeover TƯƠNG TÁC từ TUI: dùng console của người",
    ("learning/skill_cli.py", "edit_text"): "`skill add|edit` mở $EDITOR TƯƠNG TÁC (vi/notepad): dùng console của người",
    ("recovery.py", "pid_start_time"): "`ps` — chỉ nhánh POSIX",
    ("tui/bench.py", "_ps"): "`ps` — bench bỏ qua trên Windows (unsupported_reason)",
    ("systems/launchd.py", "default_runner"): "launchctl/systemctl — chỉ macOS/Linux (Windows dùng taskscheduler.default_runner)",
    ("systems/launchd.py", "parse_plist"): "/usr/bin/plutil — chỉ macOS",
}


def _spawn_calls() -> list[tuple[str, str, int, bool]]:
    out = []
    for f in sorted(SRC.rglob("*.py")):
        rel = f.relative_to(SRC).as_posix()
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=rel)
        stack: list[str] = []

        def visit(node: ast.AST) -> None:
            fn = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if fn:
                stack.append(node.name)  # type: ignore[attr-defined]
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                mod, attr = node.func.value.id, node.func.attr
                if (mod == "subprocess" and attr in SPAWN_SUBPROCESS) or (mod == "asyncio" and attr in SPAWN_ASYNCIO):
                    safe = any(k.arg is None and isinstance(k.value, ast.Call) and isinstance(k.value.func, ast.Name)
                               and k.value.func.id in SAFE_HELPERS for k in node.keywords)
                    out.append((rel, stack[-1] if stack else "<module>", node.lineno, safe))
            for c in ast.iter_child_nodes(node):
                visit(c)
            if fn:
                stack.pop()

        visit(tree)
    return out


def test_every_subprocess_spawn_is_windowless_or_allowlisted():
    calls = _spawn_calls()
    assert len(calls) >= 15  # bộ quét còn thấy các lời gọi (không bị vô hiệu do đổi cách import)
    bad = [f"{rel}:{line} ({fn})" for rel, fn, line, safe in calls if not safe and (rel, fn) not in ALLOWLIST]
    assert not bad, ("spawn thiếu **no_window_kwargs() (Windows: bật cửa sổ console nếu tiến trình cha không có console) — "
                     "thêm helper hoặc thêm vào ALLOWLIST kèm lý do: " + ", ".join(bad))
    used = {(rel, fn) for rel, fn, _, _ in calls}
    assert not set(ALLOWLIST) - used, f"ALLOWLIST cũ (không còn lời gọi): {set(ALLOWLIST) - used}"


def test_run_spawn_keeps_shared_console():
    # tiến trình run chính phải CHUNG console với daemon (CTRL_BREAK — interrupt_group), không CREATE_NO_WINDOW
    src = (SRC / "runtime" / "claude_cli.py").read_text(encoding="utf-8")
    start = re.search(r"\n    async def start\(.*?(?=\n    (?:async )?def |\Z)", src, re.S).group(0)
    assert "new_group_kwargs(" in start and "no_window_kwargs(" not in start


# ---------------------------------------------------------------------------- oscompat
def test_no_window_kwargs_windows(monkeypatch):
    monkeypatch.setattr(oscompat, "IS_WINDOWS", True)
    assert oscompat.no_window_kwargs() == {"creationflags": 0x08000000}
    assert oscompat.no_window_kwargs(0x00000200) == {"creationflags": 0x08000200}  # gộp với CREATE_NEW_PROCESS_GROUP


def test_no_window_kwargs_posix(monkeypatch):
    monkeypatch.setattr(oscompat, "IS_WINDOWS", False)
    assert oscompat.no_window_kwargs() == {} and oscompat.no_window_kwargs(0x200) == {}  # Popen POSIX từ chối creationflags


def test_ensure_hidden_console_posix(monkeypatch):
    monkeypatch.setattr(oscompat, "IS_WINDOWS", False)
    assert oscompat.ensure_hidden_console() == "posix"


@pytest.mark.skipif(not oscompat.IS_WINDOWS, reason="chỉ Windows")
def test_ensure_hidden_console_real_pythonw(tmp_path):
    """pythonw (không console, như daemon do Task Scheduler chạy): sau ensure_hidden_console, tiến trình con console KHÔNG cờ
    kế thừa console không cửa sổ (hwnd 0) thay vì được cấp console mới có cửa sổ."""
    from cfharness.systems.autostart import windows_gui_python

    g = windows_gui_python(sys.executable, getattr(sys, "_base_executable", None) or sys.executable)
    if not g:
        pytest.skip("không có pythonw GUI")
    probe = ("import ctypes,sys;from ctypes import wintypes as w;k=ctypes.WinDLL('kernel32');k.GetConsoleWindow.restype=w.HWND;"
             "print(int(k.GetConsoleWindow() or 0), k.GetConsoleCP())")
    script = tmp_path / "s.py"
    script.write_text(
        "import json,subprocess,sys\nfrom cfharness import oscompat\nm=oscompat.ensure_hidden_console()\n"
        f"r=subprocess.run([{sys.executable!r},'-I','-c',{probe!r}],capture_output=True,text=True)\n"
        f"open({str(tmp_path / 'o.json')!r},'w').write(json.dumps({{'mode':m,'child':r.stdout.split()}}))\n", encoding="utf-8")
    env = {k: v for k, v in __import__("os").environ.items()}
    if g[1]:
        env["__PYVENV_LAUNCHER__"] = g[1]
    subprocess.run([g[0], str(script)], env=env, timeout=60, check=True)
    res = json.loads((tmp_path / "o.json").read_text())
    assert res["mode"] in ("no_window", "attached", "hidden")
    hwnd, cp = res["child"]
    assert int(cp) != 0  # con có console (kế thừa) …
    if res["mode"] != "hidden":
        assert hwnd == "0"  # … và console đó không có cửa sổ


# ---------------------------------------------------------------------------- pythonw cho hook/MCP
def _pe(path: Path, subsystem: int) -> Path:
    """Tệp PE tối thiểu: chữ ký MZ, e_lfanew, "PE\\0\\0", Subsystem (2 = GUI, 3 = console) — như test_autostart."""
    path.parent.mkdir(parents=True, exist_ok=True)
    off = 0x80
    b = bytearray(0x200)
    b[0:2] = b"MZ"
    b[0x3C:0x40] = off.to_bytes(4, "little")
    b[off:off + 4] = b"PE\0\0"
    b[off + 24 + 68:off + 24 + 70] = subsystem.to_bytes(2, "little")
    path.write_bytes(bytes(b))
    return path


@pytest.fixture
def uv_venv(tmp_path, monkeypatch):
    """venv kiểu uv: python.exe + pythonw.exe của venv đều CONSOLE; pythonw GUI chỉ có cạnh python gốc."""
    venv_py = _pe(tmp_path / "venv" / "Scripts" / "python.exe", 3)
    _pe(venv_py.with_name("pythonw.exe"), 3)
    base_py = _pe(tmp_path / "base" / "python.exe", 3)
    base_w = _pe(base_py.with_name("pythonw.exe"), 2)
    monkeypatch.setattr(sys, "executable", str(venv_py))
    monkeypatch.setattr(sys, "_base_executable", str(base_py), raising=False)
    monkeypatch.setattr(rt, "IS_WINDOWS", True)
    return venv_py, base_w


def test_child_python_windows_uses_gui_pythonw(uv_venv):
    venv_py, base_w = uv_venv
    assert rt.child_python({}) == (str(base_w), {"__PYVENV_LAUNCHER__": str(venv_py)})


def test_child_python_respects_explicit_python(uv_venv):
    assert rt.child_python({"python": r"C:\Py\python.exe"}) == (r"C:\Py\python.exe", {})


def test_child_python_no_gui_falls_back(uv_venv):
    venv_py, base_w = uv_venv
    _pe(base_w, 3)  # không còn tệp GUI nào
    assert rt.child_python({}) == (str(venv_py), {})


def test_child_python_posix_unchanged(monkeypatch):
    monkeypatch.setattr(rt, "IS_WINDOWS", False)
    assert rt.child_python({}) == (sys.executable, {})
    assert rt.bash_python({}) == __import__("shlex").quote(sys.executable)


def test_hook_command_windows_pythonw_fail_closed(uv_venv):
    venv_py, base_w = uv_venv
    cmd = rt.hook_command("r1", Path(r"C:\Users\A\.cfharness\hub.sock"), 60, {})
    assert "\\" not in cmd  # Git Bash: `\` là ký tự thoát
    assert cmd.startswith(f"__PYVENV_LAUNCHER__={rt.sb_path(venv_py)} {rt.sb_path(base_w)} -I -m cfharness.guard.hook")
    assert cmd.endswith("|| exit 2")  # pythonw thiếu/lỗi → bash ≠0 → chặn


def test_mcp_entries_windows_pythonw(uv_venv, tmp_path):
    from cfharness.mcp_server.hub_self import server_entry

    venv_py, base_w = uv_venv
    g = rt.guard_mcp_entry("r1", tmp_path / "hub.sock", tmp_path, {}, 900000)
    assert g["command"] == str(base_w) and g["env"]["__PYVENV_LAUNCHER__"] == str(venv_py)
    assert g["env"]["CFHARNESS_HOME"] == str(tmp_path) and g["args"][:3] == ["-I", "-m", "cfharness.guard.mcp_guard"]
    h = server_entry("r1", tmp_path / "hub.sock", tmp_path, "worker", {})
    assert h["command"] == str(base_w) and h["env"]["__PYVENV_LAUNCHER__"] == str(venv_py)
    assert h["env"]["CFHARNESS_RUN_ID"] == "r1"


def test_observe_hooks_use_python_cmd(uv_venv):
    pre = rt.bash_python({})
    s, evs = add_observe_hooks({}, run_id="r1", socket_path="/s", hub_home="/h", python="ignored", cfg=None, python_cmd=pre)
    cmds = [h["hooks"][0]["command"] for ev in evs for h in s["hooks"][ev]]
    assert cmds and all(c.startswith(pre + " -I -m cfharness.observe_hook ") and c.endswith("|| true") for c in cmds)
    s2, _ = add_observe_hooks({}, run_id="r1", socket_path="/s", hub_home="/h", python="/usr/bin/python3", cfg=None)
    assert s2["hooks"]["Stop"][0]["hooks"][0]["command"].startswith("/usr/bin/python3 -I -m cfharness.observe_hook Stop")
