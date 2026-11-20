"""MCP server nguồn `claude` (docs/dev-notes/plan-mcp-claude-native.md PR2): cấu hình + token do `claude` giữ trong thư mục cấu hình
của TÀI KHOẢN đang dùng; registry hub chỉ giữ metadata an toàn (`McpServerConfig.source="claude"`).

Sự thật đã kiểm (claude 2.1.329): chỉ `mcpServers` TẦNG ĐẦU (user-scope) của `<config_dir>/.claude.json` được nạp trong `-p`
(local-scope `projects[<cwd>].mcpServers` thì không); `--strict-mcp-config` chặn cả server này lẫn connector claude.ai;
`--disallowedTools "mcp__<s>__*"` gỡ hết tool của server khỏi ngữ cảnh (server vẫn connect); init `mcp_servers[].status` ∈
connected | needs-auth | pending.

File `.claude.json` CHỨA SECRET (header/env/args của server, token…). Ở đây chỉ đọc ra TÊN, transport, host của url — không bao
giờ trả, log hay lưu giá trị header/env/args/command. Không spawn `claude`.

Thư mục cấu hình theo tài khoản (accounts/claude_config.py): tài khoản token `a_xxxxxxxx` → `HUB_HOME/claude-config/<id>/.claude.json`
(CLAUDE_CONFIG_DIR của run); `machine` / `custom` / chưa cấu hình → cấu hình mặc định của user OS (`$CLAUDE_CONFIG_DIR/.claude.json`
hoặc `~/.claude.json`) — đúng file mà tiến trình `claude` của run sẽ nạp.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("cfharness.mcp_client")

MAX_BYTES = 16 * 1024 * 1024  # .claude.json có thể lớn (lịch sử project) — trần chống đọc file khổng lồ
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


def tool_server_name(name: str) -> str:
    """Tên server như trong tên tool `mcp__<server>__<tool>` (claude thay ký tự ngoài `[A-Za-z0-9_-]` bằng `_`)."""
    return _TOOL_NAME_RE.sub("_", str(name))


def default_claude_json() -> Path:
    """`.claude.json` của cấu hình mặc định (run KHÔNG đặt CLAUDE_CONFIG_DIR). Test: `CFHARNESS_TEST_CLAUDE_HOME/.claude.json`."""
    from ..transfer_sessions import TEST_ENV

    if os.environ.get("CFHARNESS_TESTING") == "1" and os.environ.get(TEST_ENV):
        return Path(os.environ[TEST_ENV]) / ".claude.json"
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env).expanduser() / ".claude.json"
    return Path.home() / ".claude.json"


def claude_json_path(config_dir: Path | None) -> Path:
    return Path(config_dir) / ".claude.json" if config_dir is not None else default_claude_json()


def _host(url: Any) -> str | None:
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        return None
    try:
        h = urlsplit(url).hostname
    except ValueError:
        return None
    return (h or "").lower() or None


def _transport(entry: dict[str, Any]) -> str:
    t = str(entry.get("type") or "").lower()
    if t in ("stdio", "http", "sse"):
        return t
    if t == "streamable-http":
        return "http"
    return "http" if entry.get("url") and not entry.get("command") else "stdio"


def read_servers(config_dir: Path | None) -> dict[str, dict[str, Any]]:
    """mcpServers user-scope của `<config_dir>/.claude.json` → {tên: {transport, host, hub}}. CHỈ các trường đó (không header/env/
    args/command/url đầy đủ); `hub` = server trỏ về CHÍNH hub (`cfharness mcp serve` / loopback cổng MCP — không được đăng ký).
    File thiếu/hỏng/không phải object → {} (người gọi coi như tài khoản không có server)."""
    from ..guard.service import is_hub_mcp_config

    p = claude_json_path(config_dir)
    try:
        if p.stat().st_size > MAX_BYTES:
            log.warning("bỏ qua .claude.json quá lớn (%d byte)", p.stat().st_size)
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:  # best-effort: file hỏng = không có server (không log nội dung)
        log.warning("không đọc được .claude.json của cấu hình claude: %s", type(e).__name__)
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, entry in servers.items():
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            continue
        tr = _transport(entry)
        try:
            hub = bool(is_hub_mcp_config(entry))
        except Exception:  # fail-closed: không xác định được → coi như server của hub (không cho đăng ký)
            hub = True
        out[name] = {"transport": tr, "host": _host(entry.get("url")) if tr != "stdio" else None, "hub": hub}
    return out


def account_config_dir(hub: Any, account_id: str | None = None) -> tuple[str | None, Path | None]:
    """(id tài khoản, thư mục cấu hình | None = cấu hình mặc định). `account_id` trống → tài khoản đang dùng. Tài khoản token
    lạ → NotFound (qua `accounts.get`)."""
    from ..accounts.service import CUSTOM, MACHINE

    acc = getattr(hub, "accounts", None)
    aid = account_id or (acc.active_id() if acc is not None else None)
    if not aid or aid in (MACHINE, CUSTOM):
        return aid, None
    if acc is not None:
        acc.get(aid)  # NotFound nếu không có tài khoản này
        return aid, acc.config_dir(aid)
    return aid, None  # pragma: no cover


def account_label(aid: str | None) -> str:
    return aid or "chưa cấu hình (cấu hình claude mặc định)"


__all__ = ["account_config_dir", "account_label", "claude_json_path", "default_claude_json", "read_servers", "tool_server_name"]
