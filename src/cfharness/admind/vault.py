"""Kho secret của admind (C4, BR-SA-03): mỗi secret một file 0600 trong thư mục 0700 của root. Không bao giờ vào DB/log/audit.

Tên: `totp.<admin_id>`, `cf.token`, `claude.<account_id>`, `notify.telegram`, `notify.slack`, `smtp.password`… (regex `NAME_RE`).
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}(\.[A-Za-z0-9_-]{1,64})?$")
MAX_BYTES = 16 * 1024


class VaultError(ValueError):
    pass


class Vault:
    def __init__(self, directory: Path | str) -> None:
        self.dir = Path(directory)

    def _p(self, name: str) -> Path:
        if not NAME_RE.match(name or ""):
            raise VaultError(f"tên secret không hợp lệ: {name!r}")
        return self.dir / name

    def set(self, name: str, value: str) -> None:
        data = value.encode("utf-8")
        if not data or len(data) > MAX_BYTES:
            raise VaultError("secret rỗng hoặc quá lớn")
        p = self._p(name)
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=".tmp-")
        try:
            try:
                os.fchmod(fd, 0o600)  # type: ignore[attr-defined]
            except (AttributeError, OSError):  # pragma: no cover — Windows
                pass
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, p)

    def get(self, name: str) -> str | None:
        p = self._p(name)
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(p, flags)
        except FileNotFoundError:
            return None
        try:
            return os.read(fd, MAX_BYTES + 1)[:MAX_BYTES].decode("utf-8")
        finally:
            os.close(fd)

    def has(self, name: str) -> bool:
        return self._p(name).exists()

    def set_at(self, name: str) -> float | None:
        try:
            return self._p(name).stat().st_mtime
        except FileNotFoundError:
            return None

    def delete(self, name: str) -> bool:
        try:
            self._p(name).unlink()
            return True
        except FileNotFoundError:
            return False

    def age_days(self, name: str, now: float | None = None) -> float | None:
        t = self.set_at(name)
        return None if t is None else max(0.0, ((now or time.time()) - t) / 86400)
