"""Đọc trạng thái THẬT của máy cho admind (nguồn sự thật — plan-superadmin §2.6). Test thay `Machine` bằng bản giả.

- nhóm `cfharness-users`, plist LaunchDaemon `com.cfharness.{daemon,web}.<u>` (+ cổng web trong argv), file trạng thái của hub
  (`admin_overview.read_dir` — bộ đọc đã gia cố: O_NOFOLLOW, ≤ 64 KB, kiểm chủ file, lọc SPEC), ingress tunnel.
"""

from __future__ import annotations

import os
import plistlib
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import admin_control as AC
from .. import admin_overview as AO
from .. import admin_users as AU
from ..systems.autostart import LAUNCH_DAEMONS_DIR

if TYPE_CHECKING:
    from .dispatch import State


class Machine:
    def __init__(self, state: "State") -> None:
        self.state = state

    def users_group(self) -> list[str]:
        return AC.group_members(AC.USERS_GROUP)

    def plists(self) -> set[str]:
        return AU.installed_plists(LAUNCH_DAEMONS_DIR)

    def plist_ports(self, user: str) -> dict[str, int | None]:
        """Cổng web từ plist `com.cfharness.web.<u>` (`--port N`); MCP từ cấu hình hub không đọc được (0700) → None."""
        p = LAUNCH_DAEMONS_DIR / f"com.cfharness.web.{user}.plist"
        try:
            with open(p, "rb") as f:
                argv = plistlib.load(f).get("ProgramArguments") or []
        except (OSError, plistlib.InvalidFileException, ValueError):
            return {"web": None, "mcp": None}
        web = None
        for i, a in enumerate(argv):
            if a == "--port" and i + 1 < len(argv):
                try:
                    web = int(argv[i + 1])
                except ValueError:
                    pass
        return {"web": web, "mcp": None}

    def status(self) -> dict[str, Any]:
        return AO.read_dir(AC.shared_root() / "status")

    def os_user_exists(self, user: str) -> bool:
        try:
            import pwd

            pwd.getpwnam(user)
            return True
        except (ImportError, KeyError):
            return False

    def home_size_gb(self, user: str) -> float | None:  # pragma: no cover — du chậm, chỉ chạy theo yêu cầu
        r = self.state.runner.run(["du", "-sk", f"/Users/{user}"], timeout=120) if self.state.runner else None
        try:
            return round(int(r.out.split()[0]) / 1024 / 1024, 2) if r and r.ok else None
        except (ValueError, IndexError):
            return None

    def disk(self) -> dict[str, float]:
        try:
            u = shutil.disk_usage("/Users" if os.path.isdir("/Users") else "/")
            return {"total_gb": round(u.total / 2**30, 1), "free_gb": round(u.free / 2**30, 1)}
        except OSError:
            return {}

    def ingress(self) -> Any:
        from .ops_system import setting
        from .tunnel import TunnelConfig

        return TunnelConfig(setting(self.state.db, "cf.ingress_path"), self.state.runner).load()


def machine(state: "State") -> Machine:
    m = state.extra.get("machine")
    if m is None:
        m = state.extra["machine"] = Machine(state)
    return m


def user_dir(user: str) -> Path:
    return Path("/Users") / user
