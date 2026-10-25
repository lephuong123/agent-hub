"""Tự khởi động daemon khi user đăng nhập: `cfharness daemon install-service|uninstall-service` (+ dòng trong `daemon status`).

| Hệ điều hành | Cơ chế | Định nghĩa |
|---|---|---|
| Windows | Task Scheduler, trigger ONLOGON của CHÍNH user (đăng ký bằng XML — không cần quyền admin), chạy `pythonw` GUI + mồi `-c` (không cửa sổ console; mồi chạy lại daemon crash — xem `launcher()`) `… -m cfharness daemon run`, không giới hạn thời gian chạy | `HUB_HOME/service/<tên>.xml` |
| Windows (dự phòng) | Nếu `schtasks /Create` lỗi: file `.cmd` trong thư mục Startup của user (`start "" pythonw …`) | `%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\<tên>.cmd` |
| macOS | LaunchAgent (`RunAtLoad`, `KeepAlive.SuccessfulExit=false` = chỉ chạy lại khi crash), `launchctl bootstrap gui/<uid>` | `~/Library/LaunchAgents/<label>.plist` |
| Linux | systemd `--user` (`Restart=on-failure`, `WantedBy=default.target`), `systemctl --user enable` | `~/.config/systemd/user/<tên>.service` |

Tên gắn băm 8 ký tự của HUB_HOME → nhiều HUB_HOME cùng máy không đè nhau. Idempotent: định nghĩa trùng nội dung và đã đăng ký
→ không làm gì (`changed=false`). `--dry-run` chỉ in kế hoạch. Mọi lệnh hệ điều hành đi qua `runner(argv, timeout)` để test
thay bằng bản giả — test KHÔNG gọi schtasks/launchctl/systemctl thật. `daemon stop` thoát mã 0 → không bị khởi động lại.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from ..paths import HubPaths
from .launchd import Runner


def _default_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
    from .taskscheduler import default_runner

    return default_runner(argv, timeout)


def _platform_key(platform: str) -> str:
    if platform.startswith("win"):
        return "windows"
    if platform == "darwin":
        return "macos"
    if platform.startswith("linux"):
        return "linux"
    return platform


class Autostart:
    """Kế hoạch + thực thi cài/gỡ dịch vụ tự khởi động cho một HUB_HOME."""

    def __init__(self, paths: HubPaths, *, platform: str | None = None, runner: Runner | None = None,
                 user_home: Path | None = None, env: dict[str, str] | None = None, python: str | None = None,
                 base_python: str | None = None, timeout: float = 20.0) -> None:
        self.paths = paths
        self.os = _platform_key(platform or sys.platform)
        self.runner = runner or _default_runner
        self.user_home = Path(user_home) if user_home else Path.home()
        self.env = dict(os.environ if env is None else env)
        self.python = python or sys.executable
        # python GỐC của venv (Windows: nơi có `pythonw.exe` GUI thật khi pythonw của venv là bản console — xem launcher())
        self.base_python = base_python or ((getattr(sys, "_base_executable", None) or sys.executable) if python is None else self.python)
        self.timeout = timeout
        self.tag = hashlib.sha1(str(paths.home).encode("utf-8")).hexdigest()[:8]

    # ------------------------------------------------------------------ định danh + đường dẫn
    @property
    def name(self) -> str:
        if self.os == "windows":
            return f"\\cfharness\\daemon-{self.tag}"
        if self.os == "macos":
            return f"com.cfharness.daemon.{self.tag}"
        return f"cfharness-daemon-{self.tag}.service"

    @property
    def method(self) -> str:
        return {"windows": "taskscheduler", "macos": "launchd", "linux": "systemd"}.get(self.os, "unsupported")

    def definition_path(self) -> Path:
        if self.os == "windows":
            return self.paths.home / "service" / f"daemon-{self.tag}.xml"
        if self.os == "macos":
            return self.user_home / "Library" / "LaunchAgents" / f"{self.name}.plist"
        return Path(self.env.get("XDG_CONFIG_HOME") or self.user_home / ".config") / "systemd" / "user" / self.name

    def startup_path(self) -> Path:
        appdata = self.env.get("APPDATA") or str(self.user_home / "AppData" / "Roaming")
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / f"cfharness-daemon-{self.tag}.cmd"

    def interpreter(self) -> str:
        """Tệp thực thi mà dịch vụ chạy (phần tử đầu của `argv()`)."""
        return self.launcher()[0]

    def launcher(self) -> list[str]:
        """Phần đầu của argv, trước `daemon run --home …`.

        Windows: phải là tệp PE hệ con GUI (`pythonw`), không thì Task Scheduler/Startup bật cửa sổ terminal ở mỗi lần đăng nhập
        (đóng cửa sổ = giết daemon) và stdout trỏ vào console ẩn nên `logs/daemon.out` không được ghi. Đã gặp thật (Windows 11,
        uv 0.11): `.venv\\Scripts\\pythonw.exe` do uv tạo là BẢN SAO console của `python.exe`.

        Windows chạy `[pythonw GUI, -c, <mồi>]`, chọn pythonw theo thứ tự: cạnh python hiện tại nếu là GUI (venv chuẩn của
        CPython/virtualenv, hoặc python không venv) → không thì `pythonw.exe` của python GỐC; khi đó mồi đặt
        `__PYVENV_LAUNCHER__` = python của venv (cơ chế chính thức của `venvwlauncher`) → tiến trình con có sys.prefix/
        sys.executable của venv (hook guard dùng sys.executable). Mồi chạy và CHỜ `<sys.executable> -m cfharness daemon run …`,
        con thoát mã ≠ 0 (crash, bị giết) → chạy lại sau 60 s, tối đa 5 lần; mã 0 (`daemon stop`) → thôi. Cần mồi vì
        `RestartOnFailure` của Task Scheduler CHỈ áp dụng khi task không KHỞI ĐỘNG được — đã kiểm thật: tiến trình thoát mã 1
        → task "Ready, Last Result 1", không chạy lại. Không tìm được pythonw GUI → `[python, -m, cfharness]` (có cửa sổ
        console, không tự chạy lại khi crash).
        """
        if self.os != "windows":
            return [self.python, "-m", "cfharness"]
        p = Path(self.python)
        w = p.with_name("pythonw.exe" if p.name.lower() == "python.exe" else p.stem + "w" + p.suffix)
        if _is_gui_exe(w):
            return [str(w), "-c", _win_boot(None)]
        bw = Path(self.base_python).with_name("pythonw.exe")
        if _is_gui_exe(bw):
            return [str(bw), "-c", _win_boot(None if _same_dir(bw.parent, p.parent) else str(p))]
        return [self.python, "-m", "cfharness"]

    def argv(self) -> list[str]:
        # --home tường minh: Task Scheduler không đặt được biến môi trường; `daemon run` tự đặt CFHARNESS_HOME cho tiến trình con
        return [*self.launcher(), "daemon", "run", "--home", str(self.paths.home)]

    # ------------------------------------------------------------------ nội dung định nghĩa (thuần)
    def _user_id(self) -> str:
        user = self.env.get("USERNAME") or "user"
        dom = self.env.get("USERDOMAIN")
        return f"{dom}\\{user}" if dom else user

    def windows_xml(self) -> str:
        argv = self.argv()
        uid = escape(self._user_id())
        return (
            '<?xml version="1.0" encoding="UTF-16"?>\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
            f"  <RegistrationInfo><Description>{escape(f'cfharness daemon (HUB_HOME={self.paths.home})')}</Description></RegistrationInfo>\n"
            f"  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{uid}</UserId></LogonTrigger></Triggers>\n"
            f'  <Principals><Principal id="Author"><UserId>{uid}</UserId><LogonType>InteractiveToken</LogonType>'
            "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>\n"
            "  <Settings>\n"
            "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
            "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
            "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
            "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
            "    <RestartOnFailure><Interval>PT1M</Interval><Count>5</Count></RestartOnFailure>\n"
            "    <StartWhenAvailable>true</StartWhenAvailable>\n"
            "    <Enabled>true</Enabled>\n"
            "  </Settings>\n"
            '  <Actions Context="Author"><Exec>'
            f"<Command>{escape(argv[0])}</Command><Arguments>{escape(subprocess.list2cmdline(argv[1:]))}</Arguments>"
            f"<WorkingDirectory>{escape(str(self.paths.home))}</WorkingDirectory></Exec></Actions>\n"
            "</Task>\n"
        )

    def startup_cmd(self) -> str:
        # File ghi UTF-8 nhưng cmd.exe đọc từng dòng .cmd theo code page OEM của console → đường dẫn có dấu (HUB_HOME/user
        # tiếng Việt) bị đọc sai thành thư mục rác và daemon chạy nhầm HUB_HOME (đã gặp thật). `chcp 65001` trước dòng `start`:
        # cmd giải mã các dòng SAU theo UTF-8.
        argv = self.argv()
        return ("@echo off\r\nrem cfharness: tu khoi dong daemon khi dang nhap (cfharness daemon uninstall-service de go)\r\n"
                "chcp 65001 >nul\r\n"
                f'start "" /min {subprocess.list2cmdline(argv)}\r\n')

    def launchd_plist(self) -> bytes:
        log = str(self.paths.logs_dir / "daemon.out")
        d = {
            "Label": self.name,
            "ProgramArguments": self.argv(),
            "EnvironmentVariables": {"CFHARNESS_HOME": str(self.paths.home)},
            "WorkingDirectory": str(self.paths.home),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},  # chỉ chạy lại khi crash; `daemon stop` (mã 0) thì thôi
            "ThrottleInterval": 30,
            "ProcessType": "Background",
            "StandardOutPath": log,
            "StandardErrorPath": log,
        }
        return plistlib.dumps(d)

    def systemd_unit(self) -> str:
        argv = " ".join(_systemd_quote(a) for a in self.argv())
        return (
            "[Unit]\n"
            f"Description=cfharness daemon (HUB_HOME={self.paths.home})\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"Environment={_systemd_quote('CFHARNESS_HOME=' + str(self.paths.home))}\n"
            f"WorkingDirectory={self.paths.home}\n"
            f"ExecStart={argv}\n"
            "Restart=on-failure\n"
            "RestartSec=30\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    # ------------------------------------------------------------------ chạy lệnh
    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        return self.runner(argv, self.timeout)

    def _gui(self) -> str:
        return f"gui/{os.getuid()}" if hasattr(os, "getuid") else "gui/0"

    @staticmethod
    def _same(path: Path, content: bytes) -> bool:
        try:
            return path.read_bytes() == content
        except OSError:
            return False

    @staticmethod
    def _write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def _base(self, dry_run: bool) -> dict[str, Any]:
        return {"os": self.os, "method": self.method, "name": self.name, "argv": self.argv(), "dry_run": dry_run,
                "changed": False, "actions": [], "notes": []}

    # ------------------------------------------------------------------ cài
    def install(self, *, dry_run: bool = False) -> dict[str, Any]:
        if self.os == "windows":
            return self._install_windows(dry_run)
        if self.os == "macos":
            return self._install_macos(dry_run)
        if self.os == "linux":
            return self._install_linux(dry_run)
        raise ValueError(f"chưa hỗ trợ tự khởi động trên hệ điều hành '{self.os}'")

    def _install_windows(self, dry_run: bool) -> dict[str, Any]:
        r = self._base(dry_run)
        path = self.definition_path()
        xml = self.windows_xml().encode("utf-16")  # schtasks /XML đọc UTF-16 có BOM ổn định nhất
        r["path"] = str(path)
        startup = self.startup_path()
        exists = path.exists() and self._run(["schtasks", "/Query", "/TN", self.name])[0] == 0
        if exists and self._same(path, xml):
            r["actions"].append(f"task {self.name} đã đăng ký, không đổi")
            if startup.exists():
                r["actions"].append(f"xoá file Startup dự phòng cũ {startup}")
                if not dry_run:
                    startup.unlink(missing_ok=True)
                    r["changed"] = True
            return r
        r["actions"].append(f"ghi {path}")
        r["actions"].append(f"schtasks /Create /TN {self.name} /XML {path} /F (khi đăng nhập, user {self._user_id()})")
        if dry_run:
            r["changed"] = True
            r["notes"].append(f"nếu schtasks lỗi: dự phòng bằng {startup}")
            return r
        self._write(path, xml)
        rc, out, err = self._run(["schtasks", "/Create", "/TN", self.name, "/XML", str(path), "/F"])
        r["changed"] = True
        if rc == 0:
            if startup.exists():
                startup.unlink(missing_ok=True)
                r["actions"].append(f"xoá file Startup dự phòng cũ {startup}")
            r["notes"].append("daemon sẽ tự chạy ở lần đăng nhập sau; chạy ngay: `cfharness daemon start`")
            return r
        # dự phòng: thư mục Startup của user (không cần Task Scheduler)
        path.unlink(missing_ok=True)
        r["notes"].append(f"schtasks /Create lỗi ({rc}): {(err or out).strip()[:300]} — dùng thư mục Startup thay thế")
        r["method"] = "startup"
        r["path"] = str(startup)
        content = self.startup_cmd().encode("utf-8")
        if self._same(startup, content):
            r["actions"].append(f"{startup} đã có, không đổi")
            r["changed"] = False
        else:
            self._write(startup, content)
            r["actions"].append(f"ghi {startup}")
        r["notes"].append("daemon sẽ tự chạy ở lần đăng nhập sau (cửa sổ cmd thu nhỏ chớp một lần); chạy ngay: `cfharness daemon start`")
        return r

    def _install_macos(self, dry_run: bool) -> dict[str, Any]:
        r = self._base(dry_run)
        path = self.definition_path()
        r["path"] = str(path)
        content = self.launchd_plist()
        target = f"{self._gui()}/{self.name}"
        loaded = path.exists() and self._run(["/bin/launchctl", "print", target])[0] == 0
        if loaded and self._same(path, content):
            r["actions"].append(f"LaunchAgent {self.name} đã nạp, không đổi")
            return r
        r["changed"] = True
        if loaded:
            r["actions"].append(f"launchctl bootout {target}")
        r["actions"] += [f"ghi {path}", f"launchctl bootstrap {self._gui()} {path}"]
        if dry_run:
            return r
        if loaded:
            self._run(["/bin/launchctl", "bootout", target])
        self._write(path, content)
        rc, out, err = self._run(["/bin/launchctl", "bootstrap", self._gui(), str(path)])
        if rc != 0:
            raise RuntimeError(f"launchctl bootstrap lỗi ({rc}): {(err or out).strip()[:300]} — plist đã ghi ở {path}, sẽ nạp ở lần đăng nhập sau")
        r["notes"].append("RunAtLoad: launchd khởi động daemon ngay (nếu daemon đang chạy sẵn, bản của launchd thử lại mỗi 30 s cho tới khi tiếp quản)")
        return r

    def _install_linux(self, dry_run: bool) -> dict[str, Any]:
        r = self._base(dry_run)
        path = self.definition_path()
        r["path"] = str(path)
        content = self.systemd_unit().encode("utf-8")
        enabled = path.exists() and self._run(["systemctl", "--user", "is-enabled", self.name])[1].strip() == "enabled"
        if enabled and self._same(path, content):
            r["actions"].append(f"unit {self.name} đã bật, không đổi")
            return r
        r["changed"] = True
        r["actions"] += [f"ghi {path}", "systemctl --user daemon-reload", f"systemctl --user enable {self.name}"]
        if dry_run:
            return r
        self._write(path, content)
        for argv in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", self.name]):
            rc, out, err = self._run(argv)
            if rc != 0:
                raise RuntimeError(f"{' '.join(argv)} lỗi ({rc}): {(err or out).strip()[:300]}")
        r["notes"].append(f"daemon sẽ tự chạy ở lần đăng nhập sau; chạy ngay: `systemctl --user start {self.name}` hoặc `cfharness daemon start`")
        r["notes"].append("muốn chạy cả khi chưa đăng nhập: `loginctl enable-linger $USER`")
        return r

    # ------------------------------------------------------------------ gỡ
    def uninstall(self, *, dry_run: bool = False) -> dict[str, Any]:
        r = self._base(dry_run)
        steps: list[tuple[str, Any]] = []
        if self.os == "windows":
            path, startup = self.definition_path(), self.startup_path()
            if self._run(["schtasks", "/Query", "/TN", self.name])[0] == 0:
                steps.append((f"schtasks /Delete /TN {self.name} /F", lambda: self._check(["schtasks", "/Delete", "/TN", self.name, "/F"])))
            for p in (path, startup):
                if p.exists():
                    steps.append((f"xoá {p}", lambda p=p: p.unlink(missing_ok=True)))
        elif self.os == "macos":
            path = self.definition_path()
            target = f"{self._gui()}/{self.name}"
            if self._run(["/bin/launchctl", "print", target])[0] == 0:
                # bootout dừng luôn daemon do launchd chạy — người chạy lại bằng `cfharness daemon start` nếu cần
                steps.append((f"launchctl bootout {target}", lambda: self._run(["/bin/launchctl", "bootout", target])))
            if path.exists():
                steps.append((f"xoá {path}", lambda: path.unlink(missing_ok=True)))
        elif self.os == "linux":
            path = self.definition_path()
            if path.exists():
                steps.append((f"systemctl --user disable {self.name}", lambda: self._run(["systemctl", "--user", "disable", self.name])))
                steps.append((f"xoá {path}", lambda: path.unlink(missing_ok=True)))
                steps.append(("systemctl --user daemon-reload", lambda: self._run(["systemctl", "--user", "daemon-reload"])))
        else:
            raise ValueError(f"chưa hỗ trợ tự khởi động trên hệ điều hành '{self.os}'")
        if not steps:
            r["actions"].append("chưa cài dịch vụ tự khởi động — không có gì để gỡ")
            return r
        r["changed"] = True
        for label, fn in steps:
            r["actions"].append(label)
            if not dry_run:
                fn()
        return r

    def _check(self, argv: list[str]) -> None:
        rc, out, err = self._run(argv)
        if rc != 0:
            raise RuntimeError(f"{' '.join(argv)} lỗi ({rc}): {(err or out).strip()[:300]}")

    # ------------------------------------------------------------------ trạng thái
    def status(self) -> dict[str, Any]:
        """Rẻ: chỉ gọi lệnh hệ điều hành khi file định nghĩa tồn tại (HUB_HOME tạm của test không bao giờ gọi)."""
        st: dict[str, Any] = {"os": self.os, "method": self.method, "name": self.name, "installed": False, "active": None, "path": None}
        if self.os == "windows":
            path = self.definition_path()
            if path.exists() and self._run(["schtasks", "/Query", "/TN", self.name])[0] == 0:
                st.update(installed=True, path=str(path), active=True)
            elif self.startup_path().exists():
                st.update(installed=True, method="startup", path=str(self.startup_path()))
            elif path.exists():
                st.update(path=str(path), detail="có file XML nhưng task chưa đăng ký — chạy lại `cfharness daemon install-service`")
        elif self.os == "macos":
            path = self.definition_path()
            if path.exists():
                st.update(installed=True, path=str(path), active=self._run(["/bin/launchctl", "print", f"{self._gui()}/{self.name}"])[0] == 0)
        elif self.os == "linux":
            path = self.definition_path()
            if path.exists():
                st.update(path=str(path))
                state = self._run(["systemctl", "--user", "is-enabled", self.name])[1].strip()
                st.update(installed=state == "enabled", active=state == "enabled", detail=state or None)
        return st


def status_text(st: dict[str, Any]) -> str:
    """Một dòng tiếng Việt cho `daemon status`."""
    if st.get("method") == "unsupported":
        return "tự khởi động: chưa hỗ trợ trên hệ điều hành này"
    if not st.get("installed"):
        extra = f" ({st['detail']})" if st.get("detail") else ""
        return f"tự khởi động: chưa cài{extra} — `cfharness daemon install-service`"
    how = {"taskscheduler": "Task Scheduler, khi đăng nhập", "startup": "thư mục Startup", "launchd": "LaunchAgent",
           "systemd": "systemd --user"}.get(st["method"], st["method"])
    warn = " — CHƯA NẠP" if st.get("active") is False else ""
    return f"tự khởi động: đã cài ({how}: {st['name'] if st['method'] != 'startup' else st['path']}){warn}"


BOOT_RESTARTS = 5  # số lần mồi Windows chạy lại daemon thoát mã ≠ 0 (như RestartOnFailure PT1M × 5)
BOOT_DELAY_S = 60


def _win_boot(venv_python: str | None) -> str:
    """Mồi `-c` một dòng (Task Scheduler/Startup không chứa được xuống dòng an toàn) cho `pythonw`: (tuỳ chọn) đặt
    `__PYVENV_LAUNCHER__` như `venvwlauncher`, chạy `<sys.executable> -m cfharness <argv>` và chờ; mã 0 → thoát 0; mã ≠ 0 →
    ngủ BOOT_DELAY_S rồi chạy lại, hết BOOT_RESTARTS lần → thoát bằng mã của lần cuối. `time.sleep` trả None (falsy) nên
    `next(...)` chỉ dừng ở lần mã 0 hoặc lần cuối. Chỉ dùng nháy đơn: list2cmdline/cmd.exe không phải thoát nháy kép;
    đường dẫn đi qua repr (nháy kép nếu đường dẫn có nháy đơn — vẫn đúng với CreateProcess)."""
    env = f"os.environ['__PYVENV_LAUNCHER__']={venv_python!r};" if venv_python else ""
    return ("import os,subprocess,sys,time;" + env +
            "c=[sys.executable,'-m','cfharness',*sys.argv[1:]];"
            f"sys.exit(next(r for n in range({BOOT_RESTARTS + 1}) if (r:=subprocess.call(c))==0 or n=={BOOT_RESTARTS} "
            f"or time.sleep({BOOT_DELAY_S})))")


def _is_gui_exe(path: Path) -> bool:
    """True khi `path` là tệp PE hệ con Windows GUI (2) — chạy không cần console. Đọc header, không chạy tệp."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    if head[:2] != b"MZ" or len(head) < 0x40:
        return False
    off = int.from_bytes(head[0x3C:0x40], "little")
    sub = off + 24 + 68  # chữ ký "PE\0\0" (4) + COFF header (20) → optional header; Subsystem ở +68 (PE32 và PE32+ như nhau)
    if head[off:off + 4] != b"PE\0\0" or len(head) < sub + 2:
        return False
    return int.from_bytes(head[sub:sub + 2], "little") == 2


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
    except OSError:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


def _systemd_quote(s: str) -> str:
    if s and not any(c in s for c in ' \t"\\\'$%;'):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'
