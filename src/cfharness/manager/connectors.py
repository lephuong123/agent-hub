"""Connector claude.ai cho run của điều phối viên (`manager.capabilities.connectors`) — docs/dev-notes/manager-connectors.md.

Sự thật đã kiểm với `claude` 2.1.310 (Windows): connector claude.ai (server "claude.ai <Tên>", tool `mcp__claude_ai_<slug>__*`)
CHỈ được nạp khi nguồn setting có `user` VÀ không có `--strict-mcp-config`. `--setting-sources ""|project|local`, hay
`ENABLE_CLAUDEAI_MCP_SERVERS=true`, đều không nạp. Không có cách nạp riêng connector → run connector nạp kèm cấu hình user:
hook (`~/.claude/settings.json`), plugin bật, MCP trong `~/.claude.json`, CLAUDE.md của user.

Bù lại:
- `enabledPlugins` của user bị đặt false trong settings hub sinh (cờ `--settings` thắng nguồn user);
- (PR7 plan-mcp-claude-native) guard KHÔNG lọc MCP/connector theo agent nữa — mọi run làm việc nạp mọi MCP của tài khoản; danh sách
  connector ở đây chỉ quyết định run dùng phiên claude.ai (connector chỉ nạp khi có phiên) + `--setting-sources user`;
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


def user_claude_extras(home: Path | None = None, config_dir: Path | None = None) -> dict[str, list[str]]:
    """Hook, plugin bật, MCP server mà nguồn setting `user` sẽ nạp thêm vào run (chỉ đọc, không sửa file của người).

    `config_dir` = thư mục cấu hình claude riêng của tài khoản đang dùng (accounts/claude_config.py, CLAUDE_CONFIG_DIR của run
    nếu run chạy bằng phiên của tài khoản đó): đọc cả `<config_dir>/settings.json` + `<config_dir>/.claude.json` lẫn cấu hình
    mặc định (`~/.claude/settings.json`, `~/.claude.json`) — run có thể rơi về cấu hình mặc định khi tài khoản chưa có phiên."""
    h = home or Path(os.path.expanduser("~"))
    pairs = [(h / ".claude" / "settings.json", h / ".claude.json")]
    if config_dir is not None:
        pairs.append((Path(config_dir) / "settings.json", Path(config_dir) / ".claude.json"))
    hooks: set[str] = set()
    plugins: set[str] = set()
    servers: set[str] = set()
    for sp, cp in pairs:
        settings = _read_json(sp)
        cj = _read_json(cp)
        hk = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
        pl = settings.get("enabledPlugins") if isinstance(settings.get("enabledPlugins"), dict) else {}
        sv = cj.get("mcpServers") if isinstance(cj.get("mcpServers"), dict) else {}
        hooks |= {str(k) for k in hk}
        plugins |= {str(k) for k, v in pl.items() if v}
        servers |= {str(k) for k in sv}
    # server do tổ chức cấp (remote-settings.json managedMcpServers / managed-mcp.json) — claude nạp dưới nguồn `user`
    from ..mcp_client.claude_source import managed_servers

    for cd in ([None] if config_dir is None else [None, Path(config_dir)]):
        servers |= set(managed_servers(cd))
    return {"hooks": sorted(hooks), "plugins": sorted(plugins), "mcp_servers": sorted(servers)}


def run_setup(connectors: tuple[str, ...], home: Path | None = None, config_dir: Path | None = None) -> dict[str, Any]:
    """Phần thêm vào spec của run connector: cờ cô lập ghi đè, mảnh settings, hook/MCP coi là đã biết."""
    ex = user_claude_extras(home, config_dir)
    return {
        "isolation_overrides": dict(ISOLATION_OVERRIDES),
        "settings_patch": {"enabledPlugins": {p: False for p in ex["plugins"]}} if ex["plugins"] else {},
        "allowed_hooks": ex["hooks"],  # adapter so khớp cả hook_event
        "allowed_mcp": ex["mcp_servers"],
        "allowed_mcp_prefixes": [CONNECTOR_SERVER_PREFIX, "plugin:"],
        "connectors": list(connectors),
    }
