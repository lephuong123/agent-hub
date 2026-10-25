"""Task Scheduler (Windows) cho hệ thống ngoài hub — bản tương đương `launchd.py` trên macOS.

- ĐỌC bằng PowerShell `Get-ScheduledTask` + `Get-ScheduledTaskInfo` → JSON (không phụ thuộc ngôn ngữ hệ điều hành như
  `schtasks /Query /FO CSV`); bỏ thư mục `\\Microsoft\\*`; chỉ task chạy dưới CHÍNH user hiện tại (`Principal.UserId` khớp tên/SID).
- GHI bằng `schtasks.exe` (`/End`, `/Change /DISABLE|/ENABLE`, `/Run`) — chỉ dựa vào mã thoát.
- Label = đường dẫn task bỏ `\\` đầu (vd `MyApp\\nightly backup`); `/TN` = `\\` + label. Mọi lệnh là argv (không qua shell).

Như `Launchctl`: nhận `runner(argv, timeout) -> (rc, stdout, stderr)` để test thay bằng bản giả — test KHÔNG bao giờ gọi
PowerShell/schtasks thật. Lệnh chạy trong DAEMON: agent trong run không được tự gọi schtasks/Register-ScheduledTask (classifier
`schedule_persistence` — tiến trình do Task Scheduler tạo nằm NGOÀI Job Object của run, như `launchctl submit` thoát sandbox).
"""

from __future__ import annotations

import base64
import json
import ntpath
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from ..oscompat import IS_WINDOWS
from .launchd import Runner

# không ký tự điều khiển, không ký tự cấm trong tên task, không `..`; ký tự đầu là chữ/số/{ (tên task kiểu GUID)
LABEL_RE = re.compile(r'^(?!.*\.\.)[A-Za-z0-9{][^\x00-\x1f"<>|*?:/]{0,250}$')
_NO_WINDOW = 0x08000000
# LastTaskResult "chưa chạy lần nào" / "đang chạy" / "đã sẵn sàng"… (SCHED_S_*) — không phải lỗi
_NOT_ERROR = {0, 0x41300, 0x41301, 0x41302, 0x41303, 0x41304, 0x41305, 0x41306}
_DAYS = ("CN", "2", "3", "4", "5", "6", "7")  # bitmask DaysOfWeek: bit0 = Chủ nhật

_QUERY = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$me = [Security.Principal.WindowsIdentity]::GetCurrent()
$name = $me.Name; $short = $name.Split('\')[-1]; $sid = $me.User.Value
$out = @(Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\Microsoft\*' } | ForEach-Object {
  $u = [string]$_.Principal.UserId
  $mine = ($u -ieq $name) -or ($u -ieq $short) -or ($u -ieq $sid) -or ($u.Split('\')[-1] -ieq $short)
  $info = if ($mine) { $_ | Get-ScheduledTaskInfo } else { $null }
  [pscustomobject]@{
    path = $_.TaskPath + $_.TaskName; mine = [bool]$mine; state = [string]$_.State
    last = if ($info) { [int64]$info.LastTaskResult } else { $null }
    actions = @($_.Actions | Where-Object { $_.Execute } | ForEach-Object { [pscustomobject]@{ exe = $_.Execute; args = $_.Arguments; cwd = $_.WorkingDirectory } })
    triggers = @($_.Triggers | ForEach-Object { [pscustomobject]@{ kind = $_.CimClass.CimClassName; start = $_.StartBoundary;
      every = $_.Repetition.Interval; days = $_.DaysOfWeek; enabled = $_.Enabled } })
  }
})
ConvertTo-Json -InputObject $out -Depth 5 -Compress
"""


def default_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Như `launchd.default_runner` nhưng đọc UTF-8 và không bật cửa sổ console."""
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW if IS_WINDOWS else 0)
    except subprocess.TimeoutExpired:
        return 124, "", f"hết giờ sau {timeout:g} s"
    except OSError as e:
        return 127, "", str(e)
    return cp.returncode, cp.stdout, cp.stderr


def _iso_duration_s(v: Any) -> int | None:
    """`PT15M`, `PT1H30M`, `P1D` → giây."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", str(v or ""))
    if not m or not any(m.groups()):
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def _hm(start: Any) -> str:
    m = re.search(r"T(\d{2}):(\d{2})", str(start or ""))
    return f"{m.group(1)}:{m.group(2)}" if m else ""


def _schedule(triggers: list[dict[str, Any]]) -> tuple[str, str]:
    """(kind, mô tả tiếng Việt) như `launchd._schedule`. kind: service | scheduled | on_demand."""
    trs = [t for t in triggers if isinstance(t, dict) and t.get("enabled") is not False]
    for t in trs:
        s = _iso_duration_s(t.get("every"))
        if s:
            return "scheduled", (f"mỗi {s // 60} phút" if s % 60 == 0 and s >= 60 else f"mỗi {s} giây")
    parts = []
    for t in trs:
        kind = str(t.get("kind") or "")
        if kind in ("MSFT_TaskDailyTrigger", "MSFT_TaskTimeTrigger"):
            parts.append(_hm(t.get("start")) or "theo lịch")
        elif kind == "MSFT_TaskWeeklyTrigger":
            mask = int(t.get("days") or 0)
            wd = ",".join(_DAYS[i] for i in range(7) if mask >> i & 1)
            parts.append((_hm(t.get("start")) + (f" thứ {wd}" if wd else "")).strip() or "theo lịch")
        elif kind == "MSFT_TaskMonthlyTrigger" or kind == "MSFT_TaskMonthlyDOWTrigger":
            parts.append((_hm(t.get("start")) + " hằng tháng").strip())
    if parts:
        return "scheduled", "lúc " + ", ".join(parts[:8]) + (" …" if len(parts) > 8 else "")
    kinds = {str(t.get("kind") or "") for t in trs}
    if "MSFT_TaskLogonTrigger" in kinds:
        return "service", "chạy khi đăng nhập (At log on)"
    if "MSFT_TaskBootTrigger" in kinds:
        return "service", "chạy khi khởi động máy (At startup)"
    if kinds - {""}:
        return "scheduled", "theo sự kiện"
    return "on_demand", "chỉ khi được gọi"


def split_args(s: str | None) -> list[str]:
    """Tách dòng lệnh kiểu Windows (ngoặc kép), bỏ ngoặc — đủ để nhận đường dẫn trong đối số."""
    if not s:
        return []
    try:
        toks = shlex.split(s, posix=False)
    except ValueError:
        toks = s.split()
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] == '"' else t for t in toks]


_REDIR_RE = re.compile(r'(?<![\^\w])([12]?)>>?\s*(&[12]|"[^"]*"|[^\s"&|<>()]+)')
_CMD_C_RE = re.compile(r"^\s*(?:/[a-bd-jl-z]\S*\s+)*/[ck]\s*(.*)$", re.I | re.S)
NO_LOG_NOTE = ("Task Scheduler không lưu stdout/stderr của task. Muốn xem log qua `cfharness system show --log`, cho action chạy "
               "qua cmd và chuyển hướng ra file, vd: cmd.exe /c \"node server.js >> C:\\logs\\app.log 2>&1\"")


def redirect_logs(exe: str | None, args: str | None, cwd: str | None = None) -> tuple[str | None, str | None]:
    """(stdout, stderr) suy ra từ chuyển hướng trong action `cmd /c …` (`>`, `>>`, `1>`, `2>`, `2>&1`); (None, None) nếu không có.

    Chỉ nhận action chạy qua `cmd.exe` (/c hoặc /k) — với exe khác, `>` trong đối số không phải chuyển hướng. `%VAR%` được mở
    theo môi trường hiện tại (cùng user với task); đường dẫn tương đối tính từ thư mục làm việc của task. `nul` bị bỏ qua.
    """
    name = ntpath.basename(str(exe or "").strip().strip('"')).lower()
    if name not in ("cmd", "cmd.exe"):
        return None, None
    m = _CMD_C_RE.match(str(args or ""))
    if not m:
        return None, None
    line = m.group(1).strip()
    if len(line) >= 2 and line[0] == line[-1] == '"':  # cmd /c "…": cmd bỏ cặp ngoặc ngoài cùng
        line = line[1:-1]
    out: dict[str, str | None] = {"1": None, "2": None}
    dup: dict[str, str] = {}
    for fd, target in _REDIR_RE.findall(line):
        fd = fd or "1"
        if target.startswith("&"):
            dup[fd] = target[1]
            continue
        p = ntpath.expandvars(target.strip('"'))
        if not p or p.lower() in ("nul", "nul:", "con"):
            out[fd] = None
            continue
        cwd_s = str(cwd or "").strip('"')
        if not ntpath.isabs(p) and not os.path.isabs(p) and cwd_s:
            p = os.path.join(cwd_s, p)
        out[fd] = p
    for fd, src in dup.items():  # 2>&1 (hoặc 1>&2): cùng file với luồng kia
        out[fd] = out.get(src)
    return out["1"], out["2"]


def parse_task(t: dict[str, Any]) -> dict[str, Any] | None:
    """Job (cùng khoá với `launchd.parse_plist`) từ một task đã chiếu ra JSON; None nếu không phải của user/không hợp lệ."""
    if not isinstance(t, dict) or not t.get("mine"):
        return None
    label = str(t.get("path") or "").lstrip("\\")
    if not LABEL_RE.match(label):
        return None
    acts = t.get("actions") or []
    acts = [acts] if isinstance(acts, dict) else [a for a in acts if isinstance(a, dict)]
    program: list[str] = []
    cwd = None
    stdout = stderr = None
    for a in acts:
        program += [str(a.get("exe") or "").strip('"')] + split_args(a.get("args"))
        cwd = cwd or (str(a.get("cwd") or "").strip('"') or None)
        if stdout is None and stderr is None:
            stdout, stderr = redirect_logs(a.get("exe"), a.get("args"), a.get("cwd"))
    trs = t.get("triggers") or []
    kind, sched = _schedule([trs] if isinstance(trs, dict) else list(trs))
    return {
        "label": label,
        "plist": "\\" + label,  # nguồn của job (tên task đầy đủ) — giữ khoá `plist` để service dùng chung
        "program": [p for p in program if p],
        "cwd": cwd,
        "kind": kind,
        "schedule": sched,
        "stdout": stdout,  # Task Scheduler không có log riêng — chỉ có khi action `cmd /c … > file` (redirect_logs)
        "stderr": stderr,
        "disabled_in_plist": str(t.get("state")) == "Disabled",
        "_state": str(t.get("state") or ""),
        "_last": t.get("last"),
    }


class TaskScheduler:
    """Cùng giao diện với `launchd.Launchctl`: `jobs(dir)`, `state()`, `disabled()`, `pause`, `resume`, `restart`, `label_ok`."""

    name = "taskscheduler"
    noun = "task"
    no_log_note = NO_LOG_NOTE

    def __init__(self, schtasks: str = "schtasks", powershell: str = "powershell", runner: Runner | None = None,
                 timeout: float = 15.0, cache_s: float = 2.0) -> None:
        self.schtasks = schtasks
        self.powershell = powershell
        self.runner = runner or default_runner
        self.timeout = timeout
        self.cache_s = cache_s
        self._cache: tuple[float, list[dict[str, Any]]] | None = None

    @staticmethod
    def label_ok(label: str) -> bool:
        return bool(LABEL_RE.match(str(label or "")))

    # ------------------------------------------------------------------ đọc
    def _query(self) -> list[dict[str, Any]]:
        if self._cache and time.monotonic() - self._cache[0] < self.cache_s:
            return self._cache[1]
        enc = base64.b64encode(_QUERY.encode("utf-16-le")).decode("ascii")
        # liệt kê + Get-ScheduledTaskInfo từng task chậm hơn một lệnh launchctl → gấp đôi timeout
        rc, out, _ = self.runner([self.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
                                 self.timeout * 2)
        jobs: list[dict[str, Any]] = []
        if rc == 0 and out.strip():
            try:
                data = json.loads(out.strip().lstrip("﻿"))
            except json.JSONDecodeError:
                data = []
            for t in [data] if isinstance(data, dict) else (data or []):
                j = parse_task(t)
                if j:
                    jobs.append(j)
        self._cache = (time.monotonic(), jobs)
        return jobs

    def jobs(self, _source_dir: Path | None = None) -> list[dict[str, Any]]:
        return [{k: v for k, v in j.items() if not k.startswith("_")} for j in self._query()]

    def state(self) -> dict[str, dict[str, Any]]:
        """label → {loaded, pid, last_exit, running} cho task đang bật (task tắt = không nạp, như job bootout trên launchd)."""
        res: dict[str, dict[str, Any]] = {}
        for j in self._query():
            if j["_state"] == "Disabled":
                continue
            last = j["_last"]
            last = None if last is None or int(last) in _NOT_ERROR else int(last)
            res[j["label"]] = {"loaded": True, "pid": None, "last_exit": last, "running": j["_state"] == "Running"}
        return res

    def disabled(self) -> set[str]:
        return {j["label"] for j in self._query() if j["_state"] == "Disabled"}

    # ------------------------------------------------------------------ ghi
    def _run(self, *args: str) -> tuple[int, str, str]:
        self._cache = None
        return self.runner([self.schtasks, *args], self.timeout)

    def pause(self, label: str) -> tuple[bool, str]:
        """Dừng lần chạy hiện tại (`/End`, không chạy cũng được) + tắt task để không chạy lại theo lịch/khi đăng nhập."""
        tn = "\\" + label
        self._run("/End", "/TN", tn)
        rc, out, err = self._run("/Change", "/TN", tn, "/DISABLE")
        if rc != 0:
            return False, f"schtasks /Change /DISABLE lỗi ({rc}): {(err or out).strip()[:300]}"
        return True, "đã dừng (schtasks /End) + tắt task (/DISABLE)"

    def resume(self, label: str, _source: str | None = None) -> tuple[bool, str]:
        tn = "\\" + label
        kind = next((j["kind"] for j in self._query() if j["label"] == label), None)
        rc, out, err = self._run("/Change", "/TN", tn, "/ENABLE")
        if rc != 0:
            return False, f"schtasks /Change /ENABLE lỗi ({rc}): {(err or out).strip()[:300]}"
        if kind == "service":  # như bootstrap job KeepAlive/RunAtLoad trên launchd: chạy luôn, không chờ lần đăng nhập sau
            rc2, out2, err2 = self._run("/Run", "/TN", tn)
            if rc2 != 0:
                return False, f"đã bật task nhưng /Run lỗi ({rc2}): {(err2 or out2).strip()[:300]}"
            return True, "đã bật task (/ENABLE) + chạy ngay (/Run)"
        return True, "đã bật task (/ENABLE)"

    def restart(self, label: str) -> tuple[bool, str]:
        tn = "\\" + label
        self._run("/End", "/TN", tn)  # không đang chạy → lỗi, bỏ qua
        rc, out, err = self._run("/Run", "/TN", tn)
        if rc != 0:
            return False, f"schtasks /Run lỗi ({rc}): {(err or out).strip()[:300]}"
        return True, "đã khởi động lại (schtasks /End + /Run)"
