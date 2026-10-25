"""`cfharness daemon install-service|uninstall-service` (systems/autostart.py): Windows Task Scheduler (+ dự phòng Startup),
macOS LaunchAgent, Linux systemd --user. Lệnh hệ điều hành là bản GIẢ — test không gọi schtasks/launchctl/systemctl thật."""

from __future__ import annotations

import json
import plistlib

import pytest

from cfharness import cli as cli_mod
from cfharness.paths import HubPaths
from cfharness.systems import autostart as au_mod
from cfharness.systems.autostart import Autostart, status_text


class FakeOS:
    """schtasks /Query|/Create|/Delete, launchctl print|bootstrap|bootout, systemctl --user is-enabled|enable|disable|daemon-reload."""

    def __init__(self, fail_create: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.tasks: set[str] = set()
        self.loaded: set[str] = set()
        self.enabled: set[str] = set()
        self.fail_create = fail_create

    def __call__(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
        self.calls.append(argv)
        if argv[0] == "schtasks":
            tn = argv[argv.index("/TN") + 1]
            if argv[1] == "/Query":
                return (0, "ok", "") if tn in self.tasks else (1, "", "ERROR: not found")
            if argv[1] == "/Create":
                if self.fail_create:
                    return 1, "", "ERROR: Access is denied."
                assert argv[argv.index("/XML") + 1].endswith(".xml") and "/F" in argv
                self.tasks.add(tn)
                return 0, "SUCCESS", ""
            if argv[1] == "/Delete":
                self.tasks.discard(tn)
                return 0, "SUCCESS", ""
        if argv[0] == "/bin/launchctl":
            if argv[1] == "print":
                return (0, "", "") if argv[2].split("/")[-1] in self.loaded else (113, "", "not found")
            if argv[1] == "bootstrap":
                self.loaded.add(plistlib.loads(open(argv[3], "rb").read())["Label"])
                return 0, "", ""
            if argv[1] == "bootout":
                self.loaded.discard(argv[2].split("/")[-1])
                return 0, "", ""
        if argv[0] == "systemctl":
            op = argv[2]
            if op == "is-enabled":
                return (0, "enabled\n", "") if argv[3] in self.enabled else (1, "disabled\n", "")
            if op == "enable":
                self.enabled.add(argv[3])
            if op == "disable":
                self.enabled.discard(argv[3])
            return 0, "", ""
        return 64, "", "unknown"

    def ops(self) -> list[str]:
        return [" ".join(c[:2]) for c in self.calls]


@pytest.fixture
def paths(tmp_path):
    p = HubPaths(tmp_path / "home")
    p.home.mkdir()
    return p


def _au(paths, tmp_path, plat: str, fake: FakeOS) -> Autostart:
    env = {"USERNAME": "phuong", "USERDOMAIN": "PC", "APPDATA": str(tmp_path / "appdata")}
    return Autostart(paths, platform=plat, runner=fake, user_home=tmp_path / "user", env=env, python=str(tmp_path / "py" / "python.exe"))


# ------------------------------------------------------------------ Windows
def _pe(path, subsystem: int) -> None:
    """Tệp PE tối thiểu: header MZ → `PE\\0\\0` + COFF (20) + optional header với Subsystem ở +68 (2 = GUI, 3 = console)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    head = bytearray(0x40)
    head[:2] = b"MZ"
    head[0x3C:0x40] = (0x40).to_bytes(4, "little")
    path.write_bytes(bytes(head) + b"PE\0\0" + bytes(20 + 68) + subsystem.to_bytes(2, "little") + bytes(64))


def test_windows_install_idempotent_status_uninstall(paths, tmp_path):
    _pe(tmp_path / "py" / "pythonw.exe", 2)
    f = FakeOS()
    a = _au(paths, tmp_path, "win32", f)
    assert a.status()["installed"] is False and f.calls == []  # chưa có file định nghĩa → không gọi schtasks
    assert a.argv()[0].endswith("pythonw.exe") and a.argv()[1] == "-c" and a.argv()[3:] == ["daemon", "run", "--home", str(paths.home)]
    xml = a.windows_xml()
    for s in ("<LogonTrigger>", "<UserId>PC\\phuong</UserId>", "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", "<RestartOnFailure>",
              "<MultipleInstancesPolicy>IgnoreNew", "pythonw.exe</Command>", "<RunLevel>LeastPrivilege"):
        assert s in xml, s
    r = a.install(dry_run=True)
    assert r["dry_run"] and r["changed"] and not a.definition_path().exists() and not any(c[1] == "/Create" for c in f.calls)
    r = a.install()
    assert r["changed"] and r["method"] == "taskscheduler" and a.name in f.tasks
    assert a.definition_path().read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")  # UTF-16 có BOM cho schtasks /XML
    n = len(f.calls)
    r = a.install()
    assert not r["changed"] and "không đổi" in r["actions"][0]
    assert not any(c[1] == "/Create" for c in f.calls[n:])
    st = a.status()
    assert st["installed"] and st["method"] == "taskscheduler" and "đã cài" in status_text(st)
    r = a.uninstall()
    assert r["changed"] and a.name not in f.tasks and not a.definition_path().exists()
    r = a.uninstall()
    assert not r["changed"] and "không có gì" in r["actions"][0]
    assert "chưa cài" in status_text(a.status())


def test_windows_fallback_startup_when_schtasks_fails(paths, tmp_path):
    f = FakeOS(fail_create=True)
    a = _au(paths, tmp_path, "win32", f)
    r = a.install()
    assert r["method"] == "startup" and r["changed"] and any("Access is denied" in n for n in r["notes"])
    sp = a.startup_path()
    assert sp.exists() and "Startup" in str(sp) and 'start "" /min' in sp.read_text()
    # cmd.exe đọc .cmd theo code page OEM → phải chuyển UTF-8 TRƯỚC dòng `start` (HUB_HOME có dấu)
    lines = sp.read_text(encoding="utf-8").splitlines()
    assert lines.index("chcp 65001 >nul") < next(i for i, x in enumerate(lines) if x.startswith("start "))
    assert not a.definition_path().exists()  # XML bỏ đi: status không báo nhầm task
    st = a.status()
    assert st["installed"] and st["method"] == "startup"
    r = a.install()  # vẫn lỗi schtasks → file Startup trùng nội dung → không đổi
    assert not r["changed"]
    # schtasks hết lỗi → chuyển sang Task Scheduler và dọn file Startup
    f.fail_create = False
    r = a.install()
    assert r["method"] == "taskscheduler" and not sp.exists()
    a.uninstall()
    assert not a.status()["installed"]


def test_windows_is_gui_exe(tmp_path):
    _pe(tmp_path / "gui.exe", 2)
    _pe(tmp_path / "cui.exe", 3)
    (tmp_path / "empty.exe").write_bytes(b"")
    (tmp_path / "junk.exe").write_bytes(b"MZ" + bytes(200))
    assert au_mod._is_gui_exe(tmp_path / "gui.exe")
    for n in ("cui.exe", "empty.exe", "junk.exe", "missing.exe"):
        assert not au_mod._is_gui_exe(tmp_path / n), n


def test_windows_console_copy_pythonw_uses_base_pythonw(paths, tmp_path):
    """Hồi quy (máy thật Windows 11, uv 0.11): `.venv\\Scripts\\pythonw.exe` là BẢN SAO console của python.exe → task bật
    cửa sổ Windows Terminal ở mỗi lần đăng nhập và `logs/daemon.out` không được ghi. Phải dùng pythonw GUI của python gốc +
    `__PYVENV_LAUNCHER__` trỏ về python của venv."""
    venv_py = tmp_path / "venv" / "Scripts" / "python.exe"
    _pe(venv_py, 3)
    _pe(venv_py.with_name("pythonw.exe"), 3)  # uv: pythonw.exe giống hệt python.exe
    base_py = tmp_path / "base" / "python.exe"
    _pe(base_py, 3)
    env = {"USERNAME": "phuong", "USERDOMAIN": "PC", "APPDATA": str(tmp_path / "appdata")}
    a = Autostart(paths, platform="win32", runner=FakeOS(), user_home=tmp_path / "user", env=env, python=str(venv_py),
                  base_python=str(base_py))
    assert a.argv() == [str(venv_py), "-m", "cfharness", "daemon", "run", "--home", str(paths.home)]  # không có GUI nào
    _pe(base_py.with_name("pythonw.exe"), 2)
    argv = a.argv()
    assert argv[0] == str(base_py.with_name("pythonw.exe")) and argv[1] == "-c" and argv[3:5] == ["daemon", "run"]
    assert f"os.environ['__PYVENV_LAUNCHER__']={str(venv_py)!r}" in argv[2]
    assert '"' not in argv[2]  # chỉ nháy đơn: list2cmdline/cmd.exe không phải thoát nháy kép
    assert f"<Command>{argv[0]}</Command>" in a.windows_xml()
    # python không venv (pythonw GUI cạnh nó) → không đặt __PYVENV_LAUNCHER__
    b = Autostart(paths, platform="win32", runner=FakeOS(), env=env, python=str(base_py), base_python=str(base_py))
    assert b.argv()[0] == str(base_py.with_name("pythonw.exe")) and "__PYVENV_LAUNCHER__" not in b.argv()[2]


@pytest.mark.parametrize("codes, calls, sleeps, exit_code", [
    ([0], 1, 0, 0),  # `daemon stop` → thoát 0, không chạy lại
    ([1, 3, 0], 3, 2, 0),  # crash 2 lần rồi chạy được
    ([1] * 10, 6, 5, 1),  # hết 5 lần chạy lại → thoát bằng mã lần cuối
])
def test_windows_boot_restarts_on_crash(monkeypatch, codes, calls, sleeps, exit_code):
    """Hồi quy (máy thật): RestartOnFailure của Task Scheduler KHÔNG chạy lại tiến trình thoát mã ≠ 0 (chỉ khi task không
    khởi động được) → mồi `-c` tự chạy lại daemon crash, giống KeepAlive (macOS) / Restart=on-failure (Linux)."""
    import os
    import subprocess
    import sys
    import time

    seen: list[list[str]] = []
    slept: list[float] = []
    it = iter(codes)
    monkeypatch.setattr(subprocess, "call", lambda c: seen.append(c) or next(it))
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(sys, "argv", ["-c", "daemon", "run", "--home", "H"])
    monkeypatch.setenv("__PYVENV_LAUNCHER__", "x")  # ghi nhận trạng thái gốc → mồi đặt env xong vẫn được dọn sau test
    monkeypatch.delenv("__PYVENV_LAUNCHER__")
    with pytest.raises(SystemExit) as ex:
        exec(au_mod._win_boot(r"C:\venv\Scripts\python.exe"), {})
    assert ex.value.code == exit_code and len(seen) == calls and slept == [au_mod.BOOT_DELAY_S] * sleeps
    assert seen[0] == [sys.executable, "-m", "cfharness", "daemon", "run", "--home", "H"]
    assert os.environ["__PYVENV_LAUNCHER__"] == r"C:\venv\Scripts\python.exe"


# ------------------------------------------------------------------ macOS
def test_macos_launchagent(paths, tmp_path, monkeypatch):
    f = FakeOS()
    a = _au(paths, tmp_path, "darwin", f)
    monkeypatch.setattr(a, "_gui", lambda: "gui/501")
    d = plistlib.loads(a.launchd_plist())
    assert d["Label"] == a.name and d["RunAtLoad"] is True and d["KeepAlive"] == {"SuccessfulExit": False}
    assert d["EnvironmentVariables"]["CFHARNESS_HOME"] == str(paths.home) and d["ProgramArguments"] == a.argv()
    assert d["StandardErrorPath"].endswith("daemon.out")
    assert a.definition_path().parent.name == "LaunchAgents" and a.definition_path().name == f"{a.name}.plist"
    r = a.install()
    assert r["changed"] and a.name in f.loaded and ["/bin/launchctl", "bootstrap", "gui/501", str(a.definition_path())] in f.calls
    r = a.install()
    assert not r["changed"]
    assert a.status()["installed"] and a.status()["active"]
    # plist đổi (vd đổi python) → bootout + ghi + bootstrap lại
    a.python = str(tmp_path / "py2" / "python3")
    r = a.install()
    assert r["changed"] and r["actions"][0].startswith("launchctl bootout")
    r = a.uninstall(dry_run=True)
    assert r["changed"] and a.definition_path().exists()
    a.uninstall()
    assert not a.definition_path().exists() and a.name not in f.loaded


# ------------------------------------------------------------------ Linux
def test_linux_systemd_user(paths, tmp_path):
    f = FakeOS()
    a = _au(paths, tmp_path, "linux", f)
    unit = a.systemd_unit()
    assert "Restart=on-failure" in unit and "WantedBy=default.target" in unit and "ExecStart=" in unit and "CFHARNESS_HOME=" in unit
    r = a.install()
    assert r["changed"] and a.name in f.enabled and a.definition_path().parent.name == "user"
    assert ["systemctl", "--user", "daemon-reload"] in f.calls
    assert not a.install()["changed"]
    assert a.status()["installed"]
    a.uninstall()
    assert not a.definition_path().exists() and a.name not in f.enabled
    assert Autostart(paths, platform="sunos5", runner=f).status()["method"] == "unsupported"
    with pytest.raises(ValueError):
        Autostart(paths, platform="sunos5", runner=f).install()


def test_names_differ_per_home(tmp_path):
    a = Autostart(HubPaths(tmp_path / "a"), platform="win32", runner=FakeOS())
    b = Autostart(HubPaths(tmp_path / "b"), platform="win32", runner=FakeOS())
    assert a.name != b.name and a.name.startswith("\\cfharness\\daemon-")


# ------------------------------------------------------------------ CLI
def test_cli_install_service_dry_run_json(paths, tmp_path, monkeypatch, capsys):
    f = FakeOS()

    def make(p, **kw):
        return _au(p, tmp_path, "win32", f)

    monkeypatch.setattr(au_mod, "Autostart", make)
    code = cli_mod.main(["--home", str(paths.home), "daemon", "install-service", "--dry-run", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["dry_run"] and out["changed"] and out["method"] == "taskscheduler"
    assert not any(c[1] == "/Create" for c in f.calls)
    code = cli_mod.main(["--home", str(paths.home), "daemon", "install-service"])
    txt = capsys.readouterr().out
    assert code == 0 and "đã cài" in txt and "lệnh:" in txt
    code = cli_mod.main(["--home", str(paths.home), "daemon", "status", "--json"])
    st = json.loads(capsys.readouterr().out)
    assert code == 1 and st["daemon_running"] is False and st["autostart"]["installed"] is True
    code = cli_mod.main(["--home", str(paths.home), "daemon", "uninstall-service"])
    assert code == 0 and "đã gỡ" in capsys.readouterr().out
