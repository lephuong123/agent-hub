"""Bố cục thư mục `HUB_HOME` (BRD §8.2).

HUB_HOME mặc định `~/.cfharness`, ghi đè bằng biến môi trường `CFHARNESS_HOME`.
Đổi tên sang cfharness: biến môi trường tên CŨ vẫn được đọc làm dự phòng; nếu home mặc định chưa có mà home CŨ
(`~/.cfharness`) còn → dùng home cũ (`legacy_home_in_use`) cho tới khi người chạy `home migrate` (`home_migrate.py`).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from . import names as _names
from .oscompat import IS_WINDOWS, uid_tag

ENV_HOME = "CFHARNESS_HOME"
ENV_SOCKET = "CFHARNESS_SOCKET"
DEFAULT_HOME_DIRNAME = ".cfharness"
SOCKET_PREFIX = "cfharness-"
# macOS giới hạn đường dẫn AF_UNIX ~104 byte.
_MAX_SOCK_PATH = 100


def resolve_home(home: str | os.PathLike[str] | None = None) -> Path:
    if home is not None:
        return Path(home).expanduser().resolve()
    for name in dict.fromkeys((ENV_HOME, _names.LEGACY_ENV_HOME)):
        env = os.environ.get(name)
        if env:
            return Path(env).expanduser().resolve()
    default = Path.home() / DEFAULT_HOME_DIRNAME
    legacy = legacy_default_home()
    if legacy is not None and not default.exists() and legacy.is_dir():
        return legacy.resolve()
    return default.resolve()


def legacy_default_home() -> Path | None:
    """Home mặc định tên CŨ (`~/.cfharness`) nếu khác home mặc định hiện tại; None nếu trùng (trước khi đổi tên)."""
    if _names.LEGACY_HOME_DIRNAME == DEFAULT_HOME_DIRNAME:
        return None
    return Path.home() / _names.LEGACY_HOME_DIRNAME


def default_homes() -> list[Path]:
    """Mọi home mặc định dưới ~ (mới + cũ) — sandbox chặn home KHÔNG dùng như vùng hub."""
    return [Path.home() / n for n in dict.fromkeys((DEFAULT_HOME_DIRNAME, *_names.HUB_HOME_DIRNAMES))]


def legacy_home_in_use(home: Path) -> bool:
    """True khi đang chạy trên home CŨ do chưa `home migrate` (không đặt biến môi trường/`--home`)."""
    legacy = legacy_default_home()
    return legacy is not None and home == legacy.resolve()


@dataclass(frozen=True)
class HubPaths:
    home: Path

    @classmethod
    def resolve(cls, home: str | os.PathLike[str] | None = None) -> "HubPaths":
        return cls(resolve_home(home))

    # -- tệp cấp hub
    @property
    def db(self) -> Path:
        return self.home / "hub.db"

    @property
    def config(self) -> Path:
        return self.home / "config.yaml"

    @property
    def pidfile(self) -> Path:
        return self.home / "hub.pid"

    @property
    def socket(self) -> Path:
        """Đường dẫn Unix socket điều khiển. Nếu `HUB_HOME/hub.sock` quá dài cho AF_UNIX
        thì dùng `/tmp/cfharness-<hash>.sock` (xác định theo HUB_HOME, client và daemon tính giống nhau)."""
        for name in dict.fromkeys((ENV_SOCKET, _names.LEGACY_ENV_SOCKET)):
            env = os.environ.get(name)
            if env:
                return Path(env)
        p = self.home / "hub.sock"
        if IS_WINDOWS or len(str(p)) <= _MAX_SOCK_PATH:  # Windows: file endpoint TCP (transport.py), không giới hạn độ dài
            return p
        digest = hashlib.sha256(str(self.home).encode()).hexdigest()[:12]
        return Path("/tmp") / f"{SOCKET_PREFIX}{uid_tag()}-{digest}.sock"

    @property
    def agents_dir(self) -> Path:
        return self.home / "agents"

    @property
    def trash_dir(self) -> Path:
        return self.home / "trash"

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def mcp_registry(self) -> Path:
        return self.home / "mcp" / "registry.yaml"

    def agent_home(self, agent_id: str) -> Path:
        return self.agents_dir / agent_id

    def ensure(self) -> None:
        for d in (
            self.home,
            self.agents_dir,
            self.trash_dir,
            self.runs_dir,
            self.logs_dir,
            self.home / "mcp",
            self.home / "global" / "skills",
            self.home / "secrets",
        ):
            d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.home / "secrets", 0o700)
        except OSError:
            pass
