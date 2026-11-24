"""Che theo GIÁ TRỊ các secret nằm trong cấu hình MCP do `claude` giữ (plan-mcp-claude-native PR1 — phần còn lại).

Server MCP nguồn claude giữ header/env/args (và userinfo / query của url) trong `<config_dir>/.claude.json` `mcpServers` (cả
`projects[*].mcpServers`) và `remote-settings.json` `managedMcpServers`. Tiến trình `claude` của run nạp chúng → lỗi MCP có thể
in lại giá trị (vd header) ra stdout → event/log/DB. Module này đọc các giá trị trông như secret và đăng ký với `redact`
(`register_secret_values`) để mọi đầu ra che chúng.

- Nguồn: cấu hình mặc định (`claude_source.claude_json_path(None)` + `remote_settings_path(None)`) và MỌI thư mục tài khoản token
  `HUB_HOME/claude-config/a_xxxxxxxx/`.
- Chọn giá trị theo cùng heuristic `secret_values` (`_Extractor`: khoá nhạy cảm, `Bearer …`, entropy cao — env, header,
  args, query của url) + mật khẩu userinfo của url. Ngắn hơn `redact.MIN_VALUE_LEN` thì bỏ (redact tự lọc).
- Làm mới rẻ: chỉ đọc lại file khi `(mtime_ns, size)` đổi (`refresh`), gọi lúc `Hub.start` và mỗi lần run khởi động.
- KHÔNG BAO GIỜ log/trả về giá trị — `refresh` chỉ trả số file đã đọc lại.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

from ..redact import MIN_VALUE_LEN, register_secret_values

log = logging.getLogger("cfharness.mcp_client")

MAX_BYTES = 16 * 1024 * 1024


def entry_secret_values(entry: Any) -> list[str]:
    """Giá trị trông như secret trong MỘT entry mcpServers (hàm thuần). Không bao giờ log kết quả."""
    from .secret_values import _Extractor

    if not isinstance(entry, dict):
        return []
    ex = _Extractor("x")
    env = entry.get("env")
    if isinstance(env, dict):
        for k, v in env.items():
            ex.kv(str(k), v)
    headers = entry.get("headers")
    if isinstance(headers, dict):
        for k, v in headers.items():
            ex.kv(str(k), v)
    args = entry.get("args")
    if isinstance(args, list):
        ex.args(list(args))
    url = entry.get("url")
    pw = None
    if isinstance(url, str) and url:
        try:
            ex.url(url)
            pw = urlsplit(url).password
        except ValueError:  # best-effort: url hỏng — không có userinfo/query để che
            pw = None
    out = list(ex.secrets.values())
    if pw:
        out += [pw, unquote(pw)]
    return [v for v in out if isinstance(v, str) and len(v.strip()) >= MIN_VALUE_LEN]


def _server_maps(data: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for key in ("mcpServers", "managedMcpServers"):
        m = data.get(key)
        if isinstance(m, dict):
            yield m
    projects = data.get("projects")
    if isinstance(projects, dict):
        for proj in projects.values():
            m = proj.get("mcpServers") if isinstance(proj, dict) else None
            if isinstance(m, dict):
                yield m


def file_secret_values(path: Path) -> list[str]:
    """Mọi giá trị secret của các server trong một file cấu hình claude. File thiếu/hỏng/quá lớn → []."""
    try:
        if path.stat().st_size > MAX_BYTES:
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # best-effort: file thiếu/hỏng = không có giá trị (không log nội dung)
        return []
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for servers in _server_maps(data):
        for entry in servers.values():
            out.extend(entry_secret_values(entry))
    return out


def config_files(hub_home: Path) -> list[Path]:
    """File cấu hình claude có thể chứa server MCP: mặc định + mỗi thư mục tài khoản token."""
    from ..accounts import claude_config
    from . import claude_source

    files = [claude_source.claude_json_path(None), claude_source.remote_settings_path(None)]
    root = claude_config.root(hub_home)
    try:
        dirs = sorted(d for d in root.iterdir() if claude_config.ID_RE.match(d.name) and d.is_dir() and not d.is_symlink())
    except OSError:  # best-effort: chưa có thư mục claude-config
        dirs = []
    for d in dirs:
        files += [claude_source.claude_json_path(d), claude_source.remote_settings_path(d)]
    return files


class ClaudeConfigRedactor:
    """`hub.claude_redact`: đăng ký giá trị secret của cấu hình claude với bộ che; đọc lại file khi stat đổi."""

    def __init__(self, hub_home: Path) -> None:
        self.hub_home = Path(hub_home)
        self._lock = threading.Lock()
        self._stamps: dict[str, tuple[int, int]] = {}

    def refresh(self, extra: Iterable[Path | None] = ()) -> int:
        """Đọc lại file đã đổi (mtime/size) → đăng ký giá trị. `extra`: thư mục cấu hình bổ sung (vd của tài khoản đang dùng).
        Trả số file đã đọc lại (không bao giờ giá trị)."""
        from . import claude_source

        files = config_files(self.hub_home)
        for d in extra:
            if d is not None:
                files += [claude_source.claude_json_path(d), claude_source.remote_settings_path(d)]
        n = 0
        with self._lock:
            for p in dict.fromkeys(files):
                try:
                    st = p.stat()
                    stamp = (st.st_mtime_ns, st.st_size)
                except OSError:  # best-effort: file không có
                    self._stamps.pop(str(p), None)
                    continue
                if self._stamps.get(str(p)) == stamp:
                    continue
                vals = file_secret_values(p)
                if vals:
                    register_secret_values(*vals)
                self._stamps[str(p)] = stamp
                n += 1
        return n


__all__ = ["ClaudeConfigRedactor", "config_files", "entry_secret_values", "file_secret_values"]
