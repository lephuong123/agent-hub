"""Connector claude.ai cho run của điều phối viên (`manager.capabilities.connectors`) — docs/dev-notes/manager-connectors.md.

Sự thật đã kiểm với `claude` 2.1.310 (Windows): connector claude.ai (server "claude.ai <Tên>", tool `mcp__claude_ai_<slug>__*`)
CHỈ được nạp khi nguồn setting có `user` VÀ không có `--strict-mcp-config`. `--setting-sources ""|project|local`, hay
`ENABLE_CLAUDEAI_MCP_SERVERS=true`, đều không nạp. Không có cách nạp riêng connector → run connector nạp kèm cấu hình user:
hook (`~/.claude/settings.json`), plugin bật, MCP trong `~/.claude.json`, CLAUDE.md của user.

Bù lại:
- `enabledPlugins` của user bị đặt false trong settings hub sinh (cờ `--settings` thắng nguồn user);
- hook guard (`classify_mcp`, `GuardContext.connectors`) chặn cứng (forbidden) mọi tool MCP không thuộc agent và không phải
  connector người cho phép; tool connector tên dạng xoá vẫn qua approval;
- hook/MCP/plugin của user được khai báo là "đã biết" cho kiểm cô lập hậu nghiệm (không bật `isolation.violation` mỗi lượt chat).
Chỉ áp khi guard bật (không có hook guard = không có lớp chặn MCP lạ → không bật connector).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ISOLATION_OVERRIDES = {"setting_sources": "user", "strict_mcp_config": False}
CONNECTOR_SERVER_PREFIX = "claude.ai "  # tên server trong system/init


def _read_json(path: Path) -> dict[str, Any]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def user_claude_extras(home: Path | None = None) -> dict[str, list[str]]:
    """Hook, plugin bật, MCP server mà nguồn setting `user` sẽ nạp thêm vào run (chỉ đọc, không sửa file của người)."""
    h = home or Path(os.path.expanduser("~"))
    settings = _read_json(h / ".claude" / "settings.json")
    cj = _read_json(h / ".claude.json")
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    plugins = settings.get("enabledPlugins") if isinstance(settings.get("enabledPlugins"), dict) else {}
    servers = cj.get("mcpServers") if isinstance(cj.get("mcpServers"), dict) else {}
    return {
        "hooks": sorted(str(k) for k in hooks),
        "plugins": sorted(str(k) for k, v in plugins.items() if v),
        "mcp_servers": sorted(str(k) for k in servers),
    }


def run_setup(connectors: tuple[str, ...], home: Path | None = None) -> dict[str, Any]:
    """Phần thêm vào spec của run connector: cờ cô lập ghi đè, mảnh settings, hook/MCP coi là đã biết."""
    ex = user_claude_extras(home)
    return {
        "isolation_overrides": dict(ISOLATION_OVERRIDES),
        "settings_patch": {"enabledPlugins": {p: False for p in ex["plugins"]}} if ex["plugins"] else {},
        "allowed_hooks": ex["hooks"],  # adapter so khớp cả hook_event
        "allowed_mcp": ex["mcp_servers"],
        "allowed_mcp_prefixes": [CONNECTOR_SERVER_PREFIX, "plugin:"],
        "connectors": list(connectors),
    }
