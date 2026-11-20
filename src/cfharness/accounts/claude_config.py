"""Thư mục cấu hình Claude riêng của từng tài khoản token — phiên claude.ai (connector) đi theo tài khoản.

`HUB_HOME/claude-config/<account_id>/` (0700, thư mục cha 0700) = `CLAUDE_CONFIG_DIR` của `claude auth login --claudeai`,
`claude auth status|logout` và của run có connector khi tài khoản đó đang dùng. Mỗi thư mục giữ phiên claude.ai RIÊNG
(Keychain macOS khoá theo thư mục cấu hình / `.credentials.json` trong thư mục). Tài khoản ảo `machine` dùng cấu hình mặc
định (KHÔNG đặt `CLAUDE_CONFIG_DIR`). docs/dev-notes/manager-connectors.md — "Mỗi tài khoản một connector riêng".

Bên trong mỗi thư mục, hub đặt symlink để session và cài đặt của người vẫn DÙNG CHUNG:
- `projects` → `<claude home mặc định>/projects` (tạo đích nếu thiếu) — session bắt đầu dưới tài khoản này `--resume` được
  dưới tài khoản khác (và ngược lại);
- `settings.json`, `CLAUDE.md` → bản mặc định NẾU có.
KHÔNG BAO GIỜ symlink `.claude.json` hay credentials. Symlink được dựng lại (idempotent) trước mỗi lần dùng.

Thư mục chứa OAuth credentials: agent không đọc/ghi được (sandbox + permissions.deny + classifier), không vào bản sao lưu,
gói xuất hub hay gói chẩn đoán; không bao giờ ghi nội dung của nó vào log/event/audit.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from ..errors import HubError

log = logging.getLogger("cfharness.accounts")

DIRNAME = "claude-config"
ID_RE = re.compile(r"^a_[0-9a-f]{8}$")  # id tài khoản token (AccountService.add_token)
LINK_DIRS = ("projects",)  # luôn có (tạo đích nếu thiếu)
LINK_FILES = ("settings.json", "CLAUDE.md")  # chỉ khi bản mặc định có


def default_home() -> Path:
    """Claude home mặc định của user OS chạy hub (`CLAUDE_CONFIG_DIR` của env daemon, hoặc `~/.claude`) — cùng chỗ với
    `transfer_sessions.claude_home` (test: `CFHARNESS_TEST_CLAUDE_HOME`)."""
    from ..transfer_sessions import claude_home

    return claude_home()


def root(hub_home: Path) -> Path:
    return Path(hub_home) / DIRNAME


def account_dir(hub_home: Path, account_id: str) -> Path | None:
    """Đường dẫn thư mục của tài khoản token (có thể chưa tồn tại); `machine`/`custom`/id lạ → None."""
    if not ID_RE.match(str(account_id or "")):
        return None
    return root(hub_home) / account_id


def _private_dir(p: Path) -> None:
    if p.is_symlink():
        raise HubError(f"{p} là symlink — từ chối dùng làm thư mục cấu hình claude")
    p.mkdir(mode=0o700, exist_ok=True)
    if os.name == "posix":
        os.chmod(p, 0o700)


def _inside(d: Path, hub_home: Path) -> bool:
    try:
        return Path(os.path.realpath(d)).parent == Path(os.path.realpath(root(hub_home)))
    except OSError:
        return False


def _link(link: Path, target: Path) -> None:
    """Đặt `link` → `target`. Symlink sai đích → thay; file/thư mục THẬT ở đó → để nguyên (không xoá dữ liệu)."""
    if link.is_symlink():
        if os.readlink(link) == str(target):
            return
        link.unlink()
    elif link.exists():
        log.warning("thư mục cấu hình claude: %s không phải symlink — để nguyên", link.name)
        return
    try:
        os.symlink(str(target), str(link), target_is_directory=target.is_dir())
    except OSError as e:  # Windows không có quyền tạo symlink → session không dùng chung (resume chéo tài khoản không được)
        log.warning("không tạo được symlink %s trong thư mục cấu hình claude: %s", link.name, type(e).__name__)


def ensure(hub_home: Path, account_id: str) -> Path:
    """Tạo (nếu thiếu) thư mục cấu hình của tài khoản token + symlink dùng chung. Trả đường dẫn. Id không hợp lệ → HubError."""
    d = account_dir(hub_home, account_id)
    if d is None:
        raise HubError(f"tài khoản '{account_id}' không có thư mục cấu hình claude riêng")
    r = root(hub_home)
    r.parent.mkdir(parents=True, exist_ok=True)
    _private_dir(r)
    _private_dir(d)
    if not _inside(d, hub_home):
        raise HubError(f"thư mục cấu hình claude của '{account_id}' nằm ngoài {r}")
    home = default_home()
    for name in LINK_DIRS:
        target = home / name
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("không tạo được %s/%s: %s", home, name, type(e).__name__)
            continue
        _link(d / name, target)
    for name in LINK_FILES:
        target = home / name
        if target.exists():
            _link(d / name, target)
        elif (d / name).is_symlink():  # bản mặc định đã bị xoá → bỏ symlink gãy
            (d / name).unlink()
    return d


def shared(d: Path) -> bool:
    """`<d>/projects` là symlink sang `projects` của claude home mặc định? Không (vd Windows không tạo được symlink) → session
    của tài khoản tách khỏi cây chung, `--resume` chéo tài khoản hỏng → hub không chạy run bằng thư mục này."""
    link = Path(d) / "projects"
    try:
        return link.is_symlink() and os.readlink(link) == str(default_home() / "projects")
    except OSError:
        return False


def exists(hub_home: Path, account_id: str) -> bool:
    d = account_dir(hub_home, account_id)
    return d is not None and d.is_dir() and not d.is_symlink()


def remove(hub_home: Path, account_id: str) -> bool:
    """Xoá thư mục cấu hình của tài khoản (CHỈ thư mục đó: từ chối symlink / thoát khỏi `claude-config/`). Symlink bên trong
    bị gỡ, KHÔNG đi theo (session dùng chung còn nguyên). Trả True nếu đã xoá."""
    d = account_dir(hub_home, account_id)
    if d is None or not os.path.lexists(d):
        return False
    if d.is_symlink() or not d.is_dir():
        raise HubError(f"thư mục cấu hình claude của '{account_id}' không phải thư mục thường — không xoá")
    if root(hub_home).is_symlink() or not _inside(d, hub_home):
        raise HubError(f"thư mục cấu hình claude của '{account_id}' nằm ngoài {root(hub_home)} — không xoá")
    shutil.rmtree(d)
    return True


__all__ = ["DIRNAME", "ID_RE", "account_dir", "default_home", "ensure", "exists", "remove", "root", "shared"]
