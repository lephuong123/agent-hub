"""Đường dẫn của admind. HẰNG SỐ; chỉ test (`CFHARNESS_TESTING=1` + `CFHARNESS_TEST_ADMIND_ROOT`) đổi được."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = "/var/db/cfharness-superadmin"
RUN_DIR = "/var/run/cfharness"
SAC_USER = "_cfh_sac"
PLIST_LABEL = "com.cfharness.admind"


def _test_root() -> Path | None:
    if os.environ.get("CFHARNESS_TESTING") == "1" and os.environ.get("CFHARNESS_TEST_ADMIND_ROOT"):
        return Path(os.environ["CFHARNESS_TEST_ADMIND_ROOT"])
    return None


@dataclass(frozen=True)
class AdmindPaths:
    root: Path
    run_dir: Path

    @classmethod
    def default(cls) -> "AdmindPaths":
        t = _test_root()
        if t is not None:
            return cls(root=t, run_dir=t / "run")
        return cls(root=Path(ROOT), run_dir=Path(RUN_DIR))

    @property
    def db(self) -> Path:
        return self.root / "sac.db"

    @property
    def vault(self) -> Path:
        return self.root / "secrets"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def jobs_log(self) -> Path:
        return self.root / "jobs"

    @property
    def socket(self) -> Path:
        return self.run_dir / "admind.sock"

    def ensure(self) -> None:
        """Tạo thư mục với quyền chặt (root 0700; run_dir 0750 — nhóm của SAC được đi vào để nối socket)."""
        for p, mode in ((self.root, 0o700), (self.vault, 0o700), (self.backups, 0o700), (self.jobs_log, 0o700), (self.run_dir, 0o750)):
            p.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(p, mode)
            except OSError:  # pragma: no cover — Windows
                pass
