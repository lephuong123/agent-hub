"""Thao tác tệp AN TOÀN trong cây làm việc lane — daemon (không sandbox) không bao giờ theo symlink ở BẤT KỲ cấp nào.

Agent ghi được vào cây của run, nên có thể thay một thư mục bằng symlink trỏ ra ngoài (vd `HUB_HOME/secrets`, thư mục của người dùng) rồi
gọi `fs_delete`/`fs_move` hoặc chờ một khung xoá/di chuyển của người. Mọi thao tác ở đây đi theo kiểu `openat`: mở GỐC cây bằng
`O_NOFOLLOW|O_DIRECTORY`, rồi từng thành phần bằng `dir_fd` + `O_NOFOLLOW` — thành phần nào là symlink (ELOOP/ENOTDIR) → `UnsafePath`.
Thao tác cuối (`unlink`/`rename`/`open`/`rmtree`/`stat`) cũng dùng `dir_fd` của thư mục cha đã mở, không bao giờ đường dẫn đầy đủ.
Một agent đổi thư mục giữa chừng chỉ làm thao tác trúng đúng inode thư mục đã mở (vẫn là thư mục THẬT trong cây lúc mở), không bao giờ
"đi xuyên" sang chỗ symlink trỏ tới.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat as _st
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ...errors import HubError
from .client import LaneError, norm_rel

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIR = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW
_UNSAFE = (errno.ELOOP, errno.ENOTDIR, getattr(errno, "EMLINK", -1))


class UnsafePath(HubError):
    """Đường dẫn đi qua symlink (hoặc không phải thư mục) trong cây làm việc — daemon từ chối thao tác."""


def _rel_parts(rel: str) -> list[str]:
    try:
        return norm_rel(rel, allow_root=False).split("/")
    except LaneError as e:
        raise HubError(e.message) from None


class SafeTree:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ------------------------------------------------------------------ mở thư mục theo từng thành phần
    def open_dir(self, parts: list[str], *, create: bool = False) -> int:
        try:
            fd = os.open(self.root, _DIR)
        except OSError as e:
            if e.errno in _UNSAFE:
                raise UnsafePath("gốc cây làm việc là symlink — từ chối") from None
            raise
        try:
            for i, x in enumerate(parts):
                try:
                    nfd = os.open(x, _DIR, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(x, 0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                    nfd = os.open(x, _DIR, dir_fd=fd)
                except OSError as e:
                    if e.errno in _UNSAFE:
                        raise UnsafePath(f"đường dẫn '{'/'.join(parts[:i + 1])}' trong cây làm việc là symlink/không phải thư mục — từ chối "
                                         "(daemon không bao giờ theo symlink)") from None
                    raise
                os.close(fd)
                fd = nfd
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def parent(self, rel: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
        parts = _rel_parts(rel)
        fd = self.open_dir(parts[:-1], create=create)
        try:
            yield fd, parts[-1]
        finally:
            os.close(fd)

    # ------------------------------------------------------------------ truy vấn
    def lstat(self, rel: str) -> os.stat_result | None:
        """lstat của `rel` (không theo symlink ở bất kỳ cấp nào). Không có / cha là symlink → None."""
        try:
            with self.parent(rel) as (fd, name):
                return os.stat(name, dir_fd=fd, follow_symlinks=False)
        except (OSError, HubError):
            return None

    def exists(self, rel: str) -> bool:
        return self.lstat(rel) is not None

    def is_file(self, rel: str) -> bool:
        st = self.lstat(rel)
        return st is not None and _st.S_ISREG(st.st_mode)

    def is_dir(self, rel: str) -> bool:
        st = self.lstat(rel)
        return st is not None and _st.S_ISDIR(st.st_mode)

    def check(self, rel: str, *, final_link_ok: bool = False) -> None:
        """Ném `UnsafePath` nếu một thành phần CHA đã có của `rel` là symlink (thành phần chưa có thì thôi). Chính `rel` là symlink → cũng ném,
        trừ khi `final_link_ok` (xoá: unlink chính symlink, không đi xuyên)."""
        parts = _rel_parts(rel)
        try:
            fd = self.open_dir([])
        except FileNotFoundError:
            return
        try:
            for i, x in enumerate(parts):
                try:
                    st = os.stat(x, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    return
                if _st.S_ISLNK(st.st_mode) and not (final_link_ok and i == len(parts) - 1):
                    raise UnsafePath(f"đường dẫn '{'/'.join(parts[:i + 1])}' trong cây làm việc là symlink — từ chối (daemon không bao giờ theo symlink)")
                if i == len(parts) - 1 or not _st.S_ISDIR(st.st_mode):
                    return
                nfd = os.open(x, _DIR, dir_fd=fd)
                os.close(fd)
                fd = nfd
        finally:
            os.close(fd)

    # ------------------------------------------------------------------ đọc
    def open_read(self, rel: str) -> tuple[int, os.stat_result]:
        """fd đọc của tệp thường `rel` + fstat. Symlink/không phải tệp thường → `UnsafePath`/OSError."""
        with self.parent(rel) as (fd, name):
            try:
                f = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=fd)
            except OSError as e:
                if e.errno in _UNSAFE:
                    raise UnsafePath(f"'{rel}' là symlink — không đọc") from None
                raise
        st = os.fstat(f)
        if not _st.S_ISREG(st.st_mode):
            os.close(f)
            raise UnsafePath(f"'{rel}' không phải tệp thường — không đọc")
        return f, st

    def read_bytes(self, rel: str) -> bytes:
        f, _st2 = self.open_read(rel)
        with os.fdopen(f, "rb") as fh:
            return fh.read()

    # ------------------------------------------------------------------ ghi
    def mkdirs(self, rel: str) -> None:
        os.close(self.open_dir(_rel_parts(rel), create=True))

    def write_atomic(self, rel: str, data: bytes, *, tmp_prefix: str) -> None:
        import uuid

        with self.parent(rel, create=True) as (fd, name):
            tmp = f"{tmp_prefix}{uuid.uuid4().hex}"
            f = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o644, dir_fd=fd)
            try:
                with os.fdopen(f, "wb") as fh:
                    fh.write(data)
                os.rename(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
            except BaseException:
                try:
                    os.unlink(tmp, dir_fd=fd)
                except OSError:
                    pass
                raise

    def remove(self, rel: str) -> bool:
        """Xoá `rel` (tệp/symlink: unlink chính nó; thư mục THẬT: rmtree theo fd — không theo symlink bên trong). Trả True nếu đã xoá."""
        with self.parent(rel) as (fd, name):
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if _st.S_ISDIR(st.st_mode):
                shutil.rmtree(name, dir_fd=fd)  # avoids_symlink_attacks (openat/fd) trên macOS/Linux
            else:
                os.unlink(name, dir_fd=fd)
            return True

    def rename(self, src: str, dst: str, *, overwrite: bool = True) -> bool:
        """Đổi tên trong cây (theo fd cả hai phía). `overwrite=False` và đích đã có → False (không làm gì)."""
        with self.parent(src) as (sfd, sn), self.parent(dst, create=True) as (dfd, dn):
            try:
                os.stat(sn, dir_fd=sfd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            if not overwrite:
                try:
                    os.stat(dn, dir_fd=dfd, follow_symlinks=False)
                    return False
                except FileNotFoundError:
                    pass
            os.rename(sn, dn, src_dir_fd=sfd, dst_dir_fd=dfd)
            return True


__all__ = ["SafeTree", "UnsafePath"]
