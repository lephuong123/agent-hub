"""Bộ phân loại: lệnh Windows tạo lịch/dịch vụ/tiến trình NGOÀI Job Object của run (Task Scheduler, sc, reg Run, WMI, PowerShell
cmdlet) = `schedule_persistence` phá huỷ — tương đương `launchctl submit` trên macOS. Lệnh chỉ đọc không bị chặn."""

from __future__ import annotations

import base64

import pytest

from cfharness.guard.classifier import GuardContext, classify
from cfharness.guard.policy import PolicyStore


@pytest.fixture
def ctx(tmp_path):
    return GuardContext(cwd=tmp_path, policy=PolicyStore(tmp_path / "h").load())


def _enc(ps: str) -> str:
    return base64.b64encode(ps.encode("utf-16-le")).decode()


@pytest.mark.parametrize("cmd", [
    'schtasks /create /tn x /tr "node a.js" /sc minute',
    "schtasks.exe /Run /TN \\CF\\x",
    "schtasks //change //tn x //disable",
    "C:/Windows/System32/schtasks.exe /delete /tn x /f",
    "sc create svc binPath= C:/x.exe",
    "sc.exe config svc start= auto",
    r"reg add HKCU\Software\Microsoft\Windows\CurrentVersion\Run /v x /d C:\x.exe",
    "wmic process call create notepad.exe",
    'powershell -Command "Register-ScheduledTask -TaskName x -Action $a"',
    "powershell.exe -NoProfile -c Start-ScheduledTask -TaskName x",
    'pwsh -c "Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=\'x\'}"',
    'powershell -c "Start-Process cmd -Verb RunAs"',
    "powershell -c New-Service -Name x -BinaryPathName C:/x.exe",
    'powershell -c "Set-ItemProperty HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run -Name x -Value y"',
    "cmd //c schtasks /create /tn x /tr y /sc onlogon",
    'cmd /c "sc start svc"',
    'python -c "import os; os.system(\'schtasks /create /tn x /tr y /sc daily\')"',
])
def test_windows_persistence_destructive(ctx, cmd):
    v = classify("Bash", {"command": cmd}, ctx)
    assert v.verdict == "destructive" and v.rule in ("schedule_persistence", "registry_write"), (cmd, v)


def test_encoded_powershell(ctx):
    v = classify("Bash", {"command": f"powershell -NoProfile -EncodedCommand {_enc('Register-ScheduledTask -TaskName x')}"}, ctx)
    assert v.verdict == "destructive" and v.rule == "schedule_persistence"
    assert "Job Object" in v.reason


def test_registry_write_non_run_key(ctx):
    v = classify("Bash", {"command": r"reg add HKCU\Software\Foo /v x /d 1"}, ctx)
    assert v.verdict == "destructive" and v.rule == "registry_write"


@pytest.mark.parametrize("cmd", [
    "schtasks /query /fo list",
    "sc query svc",
    r"reg query HKCU\Software\Foo",
    "wmic process list brief",
])
def test_windows_read_only_not_persistence(ctx, cmd):
    v = classify("Bash", {"command": cmd}, ctx)
    assert v.rule not in ("schedule_persistence", "registry_write"), (cmd, v)
