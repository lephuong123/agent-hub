"""Bố cục thư mục `HUB_HOME` (BRD §8.2).

HUB_HOME mặc định `~/.cfharness`, ghi đè bằng biến môi trường `CFHARNESS_HOME`.
Đổi tên sang cfharness: biến môi trường tên CŨ vẫn được đọc làm dự phòng; nếu home mặc định chưa có mà home CŨ
(`~/.cfharness`) còn → dùng home cũ (`legacy_home_in_use`) cho tới khi người chạy `home migrate` (`home_migrate.py`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
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
# Mục trong HUB_HOME phải riêng tư với user OS khác (đa instance P1, `docs/dev-notes/multi-instance.md`).
PRIVATE_ENTRIES = ("hub.db", "hub.db-wal", "hub.db-shm", "config.yaml", "logs", "runs", "prompts", "agents", "secrets", "auth",
                   "mcp_server", "checkpoints", "workspaces", "worktrees", "evals", "trash", "policy")

log = logging.getLogger("cfharness.paths")


class UnsafeSocketDir(RuntimeError):
    """Thư mục socket dự phòng không an toàn (của user khác / symlink / quyền rộng)."""


def fallback_socket_dir() -> Path:
    """Thư mục RIÊNG của user cho socket dự phòng: `/tmp/cfharness-<uid>/` (0700, chủ = user hiện tại).

    Không dùng `$TMPDIR`: biến này không ổn định giữa phiên GUI, SSH và LaunchDaemon (client và daemon phải tính RA CÙNG
    một đường dẫn), còn thư mục `/var/folders/…/T` bị macOS dọn định kỳ. Tên thư mục đoán được nhưng chủ + quyền được kiểm
    (`ensure_socket_dir`) và client kiểm chủ socket trước khi gửi token (`rpc.with_auth`)."""
    return Path("/tmp") / f"{SOCKET_PREFIX}{uid_tag()}"


def socket_private_dirs(sock: str | os.PathLike[str]) -> list[str]:
    """Thư mục socket dự phòng riêng (nếu socket nằm trong đó) — sandbox cấm ghi cả thư mục (agent không xoá/thay socket)."""
    d = Path(sock).parent
    return [str(d)] if not IS_WINDOWS and d == fallback_socket_dir() else []


def ensure_socket_dir(sock: Path) -> None:
    """Tạo thư mục chứa socket dự phòng (0700) và kiểm nó thuộc user hiện tại, không phải symlink.
    Không đụng tới thư mục khác (HUB_HOME, `CFHARNESS_SOCKET` do người đặt)."""
    d = sock.parent
    if IS_WINDOWS or d != fallback_socket_dir():
        return
    try:
        d.mkdir(mode=0o700)
    except FileExistsError:
        pass
    st = os.lstat(d)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise UnsafeSocketDir(f"thư mục socket {d} không thuộc user hiện tại hoặc là symlink — không dùng. "
                              f"Xoá nó (nếu là của bạn) hoặc đặt CFHARNESS_SOCKET=<đường dẫn riêng>.")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(d, 0o700)


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
        thì dùng `/tmp/cfharness-<uid>/cfharness-<hash>.sock` (thư mục riêng 0700 — `fallback_socket_dir`; xác định theo
        HUB_HOME, client và daemon tính giống nhau; tên file giữ tiền tố để guard nhận diện)."""
        for name in dict.fromkeys((ENV_SOCKET, _names.LEGACY_ENV_SOCKET)):
            env = os.environ.get(name)
            if env:
                return Path(env)
        p = self.home / "hub.sock"
        if IS_WINDOWS or len(str(p)) <= _MAX_SOCK_PATH:  # Windows: file endpoint TCP (transport.py), không giới hạn độ dài
            return p
        digest = hashlib.sha256(str(self.home).encode()).hexdigest()[:12]
        return fallback_socket_dir() / f"{SOCKET_PREFIX}{digest}.sock"

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
        for d in (self.home, self.home / "secrets"):
            try:
                os.chmod(d, 0o700)
            except OSError as e:  # pragma: no cover — FS không hỗ trợ chmod: chạy tiếp, báo
                log.warning("không đặt được quyền 0700 cho %s: %s", d, e)

    def tighten_permissions(self) -> list[str]:
        """Cài đặt cũ (trước P1: HUB_HOME 0755, hub.db/logs 0644): bỏ quyền group/other của HUB_HOME và các mục cấp 1 nhạy cảm.
        Không đệ quy (HUB_HOME 0700 đã chặn đi vào); lỗi chmod chỉ log cảnh báo. Trả danh sách mục đã siết."""
        if IS_WINDOWS:
            return []
        fixed: list[str] = []
        for p in (self.home, *(self.home / n for n in PRIVATE_ENTRIES)):
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid():
                continue
            mode = stat.S_IMODE(st.st_mode)
            if mode & 0o077:
                try:
                    os.chmod(p, mode & ~0o077)
                    fixed.append(str(p))
                except OSError as e:
                    log.warning("không siết được quyền %s: %s", p, e)
        return fixed
