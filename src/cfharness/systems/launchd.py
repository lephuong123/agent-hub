"""launchd (macOS) cho hệ thống ngoài hub: đọc plist trong `~/Library/LaunchAgents` + gọi `launchctl` miền `gui/<uid>`.

Hàm đọc plist là THUẦN. `Launchctl` nhận `runner(argv, timeout) -> (rc, stdout, stderr)` để test thay bằng bản giả — test KHÔNG
bao giờ gọi `launchctl` thật. Lệnh chạy trong DAEMON (ngoài sandbox của run): agent trong run không gọi được `launchctl`
(sandbox + classifier `schedule_persistence`), chỉ đi qua RPC `system.job` có kiểm quyền.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from ..oscompat import uid_tag

Runner = Callable[[list[str], float], tuple[int, str, str]]
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


def default_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "", f"hết giờ sau {timeout:g} s"
    except OSError as e:
        return 127, "", str(e)
    return cp.returncode, cp.stdout, cp.stderr


def _schedule(d: dict[str, Any]) -> tuple[str, str]:
    """(kind, mô tả tiếng Việt). kind: service (chạy thường trực) | scheduled (hẹn giờ) | on_demand."""
    if d.get("StartInterval"):
        s = int(d["StartInterval"])
        return "scheduled", (f"mỗi {s // 60} phút" if s % 60 == 0 and s >= 60 else f"mỗi {s} giây")
    cal = d.get("StartCalendarInterval")
    if cal:
        items = cal if isinstance(cal, list) else [cal]
        parts = []
        for c in items[:8]:
            if not isinstance(c, dict):
                continue
            hm = f"{int(c['Hour']):02d}:{int(c.get('Minute', 0)):02d}" if "Hour" in c else (f"phút {int(c['Minute'])} mỗi giờ" if "Minute" in c else "")
            wd = f" thứ {int(c['Weekday']) + 1 if int(c['Weekday']) else 'CN'}" if "Weekday" in c else ""
            day = f" ngày {int(c['Day'])}" if "Day" in c else ""
            parts.append((hm + wd + day).strip() or "theo lịch")
        return "scheduled", "lúc " + ", ".join(parts) + (" …" if len(items) > 8 else "")
    if d.get("KeepAlive"):
        return "service", "chạy thường trực (KeepAlive)"
    if d.get("RunAtLoad"):
        return "service", "chạy khi nạp (RunAtLoad)"
    return "on_demand", "chỉ khi được gọi"


def parse_plist(path: Path) -> dict[str, Any] | None:
    """Job từ một file plist; None nếu hỏng/không có Label hợp lệ."""
    try:
        with path.open("rb") as f:
            d = plistlib.load(f)
    except Exception:
        # plistlib khắt khe hơn launchd (vd `--` trong chú thích XML) — chuẩn hoá bằng plutil của macOS rồi đọc lại
        try:
            cp = subprocess.run(["/usr/bin/plutil", "-convert", "xml1", "-o", "-", str(path)], capture_output=True, timeout=5,
                                stdin=subprocess.DEVNULL)
            d = plistlib.loads(cp.stdout) if cp.returncode == 0 else None
        except Exception:
            d = None
    if not isinstance(d, dict):
        return None
    label = str(d.get("Label") or "")
    if not LABEL_RE.match(label):
        return None
    args = [str(a) for a in (d.get("ProgramArguments") or []) if isinstance(a, (str, int, float))]
    if d.get("Program") and not args:
        args = [str(d["Program"])]
    kind, sched = _schedule(d)
    return {
        "label": label,
        "plist": str(path),
        "program": args,
        "cwd": str(d.get("WorkingDirectory") or "") or None,
        "kind": kind,
        "schedule": sched,
        "stdout": str(d.get("StandardOutPath") or "") or None,
        "stderr": str(d.get("StandardErrorPath") or "") or None,
        "disabled_in_plist": bool(d.get("Disabled")),
    }


def load_jobs(launch_agents_dir: Path) -> list[dict[str, Any]]:
    out = []
    try:
        files = sorted(launch_agents_dir.glob("*.plist"))
    except OSError:
        return []
    for p in files:
        j = parse_plist(p)
        if j:
            out.append(j)
    return out


_WIN_ABS = re.compile(r"^[A-Za-z]:[\\/]")


def is_abs_token(tok: str) -> bool:
    r"""Token là đường dẫn tuyệt đối: `/…` (POSIX) hoặc `C:\…` / `C:/…` (Windows)."""
    return tok.startswith("/") or bool(_WIN_ABS.match(tok))


def job_paths(job: dict[str, Any]) -> list[str]:
    """Đường dẫn tuyệt đối job chạm tới (cwd + đối số dạng đường dẫn) — để gán job vào hệ thống."""
    out = [job["cwd"]] if job.get("cwd") else []
    for a in job.get("program") or []:
        for tok in str(a).split():
            if is_abs_token(tok):
                out.append(tok)
    return out


class Launchctl:
    """Backend job của macOS. Cùng giao diện với `taskscheduler.TaskScheduler` (Windows) — service chỉ dùng: `jobs(dir)`, `state()`,
    `disabled()`, `pause(label)`, `resume(label, nguồn)`, `restart(label)`, `label_ok(label)`, `name`, `noun`."""

    name = "launchd"
    noun = "job launchd"

    def __init__(self, binary: str = "/bin/launchctl", runner: Runner | None = None, timeout: float = 15.0, uid: int | None = None) -> None:
        self.bin = binary
        self.runner = runner or default_runner
        self.timeout = timeout
        self.domain = f"gui/{uid_tag() if uid is None else uid}"

    @staticmethod
    def label_ok(label: str) -> bool:
        return bool(LABEL_RE.match(str(label or "")))

    @staticmethod
    def jobs(launch_agents_dir: Path) -> list[dict[str, Any]]:
        return load_jobs(launch_agents_dir)

    def _run(self, *args: str) -> tuple[int, str, str]:
        return self.runner([self.bin, *args], self.timeout)

    def state(self) -> dict[str, dict[str, Any]]:
        """label → {loaded, pid, last_exit} cho job đang nạp (`launchctl list`)."""
        rc, out, _ = self._run("list")
        res: dict[str, dict[str, Any]] = {}
        if rc != 0:
            return res
        for line in out.splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            pid, status, label = parts
            res[label.strip()] = {"loaded": True, "pid": int(pid) if pid.strip().isdigit() else None,
                                  "last_exit": int(status) if status.strip().lstrip("-").isdigit() else None}
        return res

    def disabled(self) -> set[str]:
        rc, out, _ = self._run("print-disabled", self.domain)
        if rc != 0:
            return set()
        return {m.group(1) for m in re.finditer(r'"([^"]+)"\s*=>\s*(?:disabled|true)', out)}

    def pause(self, label: str) -> tuple[bool, str]:
        """Gỡ khỏi launchd (dừng tiến trình) + `disable` để không tự nạp lại khi đăng nhập/khởi động lại máy."""
        rc, _, err = self._run("bootout", f"{self.domain}/{label}")
        # 3/113 = không có job đang nạp (đã tắt sẵn) — vẫn coi là tạm dừng được
        if rc not in (0, 3, 113) and "No such process" not in err and "Could not find service" not in err:
            return False, f"bootout lỗi ({rc}): {err.strip()[:300]}"
        rc2, _, err2 = self._run("disable", f"{self.domain}/{label}")
        if rc2 != 0:
            return False, f"disable lỗi ({rc2}): {err2.strip()[:300]}"
        return True, "đã gỡ khỏi launchd + disable"

    def resume(self, label: str, plist: str) -> tuple[bool, str]:
        rc, _, err = self._run("enable", f"{self.domain}/{label}")
        if rc != 0:
            return False, f"enable lỗi ({rc}): {err.strip()[:300]}"
        rc2, _, err2 = self._run("bootstrap", self.domain, plist)
        # 5/17/37 = đã nạp sẵn
        if rc2 not in (0, 5, 17, 37) and "already" not in err2.lower():
            return False, f"bootstrap lỗi ({rc2}): {err2.strip()[:300]}"
        return True, "đã enable + nạp lại vào launchd"

    def restart(self, label: str) -> tuple[bool, str]:
        rc, _, err = self._run("kickstart", "-k", f"{self.domain}/{label}")
        if rc != 0:
            return False, f"kickstart lỗi ({rc}): {err.strip()[:300]}"
        return True, "đã khởi động lại (kickstart -k)"
