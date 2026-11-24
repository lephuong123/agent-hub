"""Cài admind + SAC lên máy (plan-superadmin §7). Thuần: dựng plist + danh sách lệnh; `apply` (root) mới ghi /Library và bootstrap.

- admind: root, `com.cfharness.admind`, chạy `/opt/cfharness/bin/cfharness admind serve` (bản cài root-owned — admind KHÔNG bao giờ
  chạy code nằm trong thư mục user ghi được).
- SAC: user hệ thống ẩn `_cfh_sac` (UID < 500, không shell, không home thật), `com.cfharness.sac`, `cfharness superadmin serve`.
"""

from __future__ import annotations

import plistlib
from pathlib import Path
from typing import Callable

from ..systems.autostart import LAUNCH_DAEMONS_DIR, SYSTEM_PREFIX
from .paths import PLIST_LABEL, RUN_DIR, SAC_USER

SAC_LABEL = "com.cfharness.sac"
SAC_STATE_DIR = "/var/lib/cfharness-sac"
PROGRAM = f"{SYSTEM_PREFIX}/bin/cfharness"
SAC_UID_DEFAULT = 412
PATH_ENV = f"{SYSTEM_PREFIX}/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def admind_plist(program: str = PROGRAM) -> bytes:
    return plistlib.dumps({
        "Label": PLIST_LABEL,
        "ProgramArguments": [program, "admind", "serve"],
        "EnvironmentVariables": {"PATH": PATH_ENV},
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": "/var/log/cfharness-admind.log",
        "StandardErrorPath": "/var/log/cfharness-admind.log",
        "ProcessType": "Interactive",
    })


def sac_plist(program: str = PROGRAM, port: int = 8700) -> bytes:
    return plistlib.dumps({
        "Label": SAC_LABEL,
        "UserName": SAC_USER,
        "ProgramArguments": [program, "superadmin", "serve", "--port", str(port)],
        "EnvironmentVariables": {"PATH": PATH_ENV, "HOME": SAC_STATE_DIR},
        "WorkingDirectory": SAC_STATE_DIR,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": f"{SAC_STATE_DIR}/sac.log",
        "StandardErrorPath": f"{SAC_STATE_DIR}/sac.log",
    })


def sac_user_cmds(uid: int = SAC_UID_DEFAULT) -> list[list[str]]:
    u = f"/Users/{SAC_USER}"
    return [
        ["dscl", ".", "-create", u],
        ["dscl", ".", "-create", u, "UniqueID", str(uid)],
        ["dscl", ".", "-create", u, "PrimaryGroupID", str(uid)],
        ["dscl", ".", "-create", u, "UserShell", "/usr/bin/false"],
        ["dscl", ".", "-create", u, "NFSHomeDirectory", "/var/empty"],
        ["dscl", ".", "-create", u, "RealName", "cfharness Super Admin Console"],
        ["dscl", ".", "-create", u, "IsHidden", "1"],
        ["dscl", ".", "-create", f"/Groups/{SAC_USER}"],
        ["dscl", ".", "-create", f"/Groups/{SAC_USER}", "PrimaryGroupID", str(uid)],
        ["mkdir", "-p", SAC_STATE_DIR],
        ["chown", f"{SAC_USER}:{SAC_USER}", SAC_STATE_DIR],
        ["chmod", "700", SAC_STATE_DIR],
    ]


def plan(*, port: int = 8700, directory: Path = LAUNCH_DAEMONS_DIR) -> dict[str, object]:
    a = directory / f"{PLIST_LABEL}.plist"
    s = directory / f"{SAC_LABEL}.plist"
    cmds = [*sac_user_cmds(),
            ["mkdir", "-p", RUN_DIR],
            ["install", "-m", "0644", "-o", "root", "-g", "wheel", "<admind.plist>", str(a)],
            ["install", "-m", "0644", "-o", "root", "-g", "wheel", "<sac.plist>", str(s)],
            ["launchctl", "bootstrap", "system", str(a)],
            ["launchctl", "bootstrap", "system", str(s)]]
    return {"admind_plist": a, "sac_plist": s, "commands": cmds, "admind": admind_plist(), "sac": sac_plist(port=port)}


def apply(runner: Callable[[list[str]], tuple[int, str, str]], *, port: int = 8700, directory: Path = LAUNCH_DAEMONS_DIR) -> list[str]:
    """Chạy bằng root. Idempotent: user/nhóm đã có thì dscl báo lỗi vô hại; plist ghi đè; bootstrap lỗi 'already loaded' bỏ qua."""
    p = plan(port=port, directory=directory)
    out: list[str] = []
    for c in sac_user_cmds():
        rc, _o, e = runner(c)
        out.append(f"{'ok' if rc == 0 else 'bỏ qua'}: {' '.join(c)}" + (f" ({e.strip()[:80]})" if rc else ""))
    Path(RUN_DIR).mkdir(parents=True, exist_ok=True)
    for path, data in ((p["admind_plist"], p["admind"]), (p["sac_plist"], p["sac"])):
        Path(path).write_bytes(data)  # type: ignore[arg-type]
        Path(path).chmod(0o644)  # type: ignore[arg-type]
        rc, _o, e = runner(["launchctl", "bootstrap", "system", str(path)])
        out.append(f"bootstrap {path}: {'ok' if rc == 0 else e.strip()[:120]}")
    return out
