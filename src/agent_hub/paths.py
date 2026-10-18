"""Bố cục thư mục `HUB_HOME` (BRD §8.2).

HUB_HOME mặc định `~/.agent-hub`, ghi đè bằng biến môi trường `AGENT_HUB_HOME`.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

ENV_HOME = "AGENT_HUB_HOME"
ENV_SOCKET = "AGENT_HUB_SOCKET"
# macOS giới hạn đường dẫn AF_UNIX ~104 byte.
_MAX_SOCK_PATH = 100


def resolve_home(home: str | os.PathLike[str] | None = None) -> Path:
    if home is not None:
        return Path(home).expanduser().resolve()
    env = os.environ.get(ENV_HOME)
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / ".agent-hub").resolve()


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
        thì dùng `/tmp/agent-hub-<hash>.sock` (xác định theo HUB_HOME, client và daemon tính giống nhau)."""
        env = os.environ.get(ENV_SOCKET)
        if env:
            return Path(env)
        p = self.home / "hub.sock"
        if len(str(p)) <= _MAX_SOCK_PATH:
            return p
        digest = hashlib.sha256(str(self.home).encode()).hexdigest()[:12]
        return Path("/tmp") / f"agent-hub-{os.getuid()}-{digest}.sock"

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
