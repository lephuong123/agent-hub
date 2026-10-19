"""Tên sản phẩm và tên CŨ (`cfharness`) — nguồn duy nhất cho guard/paths/auth khi đổi tên sang `cfharness`.

Guard phải nhận diện CẢ tên mới lẫn tên cũ VĨNH VIỄN: binary/module cũ có thể còn sót trong venv, home cũ có thể còn trên
đĩa. Dòng mang `rename:keep` không bị `scripts/rename_cfharness.py` thay — đừng gộp các literal tên cũ vào dòng khác.
"""

from __future__ import annotations

import re

PRODUCT = "cfharness"

# tên lệnh CLI (basename của argv[0]) mà classifier coi là CLI điều khiển hub
HUB_CLI_NAMES: tuple[str, ...] = ("cfharness", "agent-hub", "agent_hub")  # rename:keep
# package Python của hub (`python -m <mod>`, `import <mod>`)
HUB_MODULE_NAMES: tuple[str, ...] = ("cfharness", "agent_hub")  # rename:keep
# tên MCP server của hub (mcp__<server>__hub_*)
HUB_MCP_SERVER_NAMES: tuple[str, ...] = ("cfharness", "agent-hub", "agent_hub", "hub")  # rename:keep
# tiền tố biến môi trường của hub
HUB_ENV_PREFIXES: tuple[str, ...] = ("CFHARNESS_", "AGENT_HUB_")  # rename:keep
# tên thư mục HUB_HOME mặc định dưới ~ (mới trước)
HUB_HOME_DIRNAMES: tuple[str, ...] = (".cfharness", ".agent-hub")  # rename:keep
# tiền tố file socket dự phòng trong /tmp
HUB_SOCKET_PREFIXES: tuple[str, ...] = ("cfharness-", "agent-hub-")  # rename:keep

LEGACY_HOME_DIRNAME = ".agent-hub"  # rename:keep
LEGACY_ENV_HOME = "AGENT_HUB_HOME"  # rename:keep
LEGACY_ENV_SOCKET = "AGENT_HUB_SOCKET"  # rename:keep
LEGACY_ENV_RUN_ID = "AGENT_HUB_RUN_ID"  # rename:keep
LEGACY_EXPORT_FORMATS: tuple[str, ...] = ("agent-hub-agent/1",)  # rename:keep


def env_names(suffix: str) -> tuple[str, ...]:
    """`env_names("HOME")` → ("CFHARNESS_HOME", "CFHARNESS_HOME")."""
    return tuple(p + suffix for p in HUB_ENV_PREFIXES)


def alt(names: tuple[str, ...]) -> str:
    """Nhóm regex khớp một trong các tên (đã escape)."""
    return "(?:" + "|".join(re.escape(n) for n in names) + ")"
