"""MCP client registry cho agent (FR-MC-01..06) — luồng A của M1.

Hàm hợp đồng cho luồng B (docs/plan/M1.md):
- `build_agent_mcp_servers(agent) -> dict[str, dict]`: dạng `mcpServers` cho `claude --mcp-config`,
  CHỈ gồm server gán cho agent, ĐÃ thay secret (chỉ ghi vào file runtime 0600, xoá sau run).
  Server thiếu/đã xoá mềm hoặc secret thiếu → `HubError` (fail-closed, run báo lỗi rõ ràng).
- `tool_annotations(server, tool) -> dict | None`: annotations đã probe (`readOnlyHint`, `destructiveHint`,
  `title`, `description`, `suspicious`) + `trust` của server, `require_approval`, `selected`.
  `None` = server lạ hoặc tool chưa thấy khi probe (tool mới → coi như cần duyệt, FR-MC-03).
- `agent_mcp_tool_rules(agent) -> {"allowed": [...], "disallowed": [...], "warnings": [...]}`: chuyển
  include/exclude/trust/readOnlyHint thành tên `mcp__<server>__<tool>` cho `--allowedTools/--disallowedTools`
  (FR-MC-02/03). Tool `destructiveHint`/`require_approval` KHÔNG nằm trong allowed (để guard quyết).

`agent` nhận dict (dòng `agents`, có `mcp_servers` + `home_path`), đối tượng có thuộc tính `mcp_servers`,
hoặc chuỗi id agent. Registry được chọn theo HUB_HOME suy từ `home_path` (`<HUB_HOME>/agents/<id>`),
nếu không có thì registry cài gần nhất, cuối cùng là HUB_HOME hiện hành (đọc DB chế độ chỉ đọc).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .registry import McpRegistry

_REGISTRIES: dict[str, "McpRegistry"] = {}
_LAST: list[str] = []


def register_registry(home: Path, registry: "McpRegistry") -> None:
    key = str(Path(home).resolve())
    _REGISTRIES[key] = registry
    if key in _LAST:
        _LAST.remove(key)
    _LAST.append(key)


def _usable(reg: "McpRegistry") -> bool:
    try:
        reg.conn.execute("SELECT 1")
        return True
    except (sqlite3.ProgrammingError, sqlite3.OperationalError):
        return False


def _readonly_registry(home: Path) -> "McpRegistry":
    from ..config import HubConfig
    from ..paths import HubPaths
    from ..secrets import SecretStore
    from .registry import McpRegistry

    paths = HubPaths(Path(home))
    if not paths.db.exists():
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE mcp_servers (name TEXT PRIMARY KEY, config TEXT, trust TEXT, annotations_cache TEXT, deleted_at TEXT, last_probe_at TEXT)")
    else:
        conn = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
    try:
        env_fallback = bool(HubConfig(paths.config).get("secrets.env_fallback"))
    except Exception:  # pragma: no cover
        env_fallback = True
    return McpRegistry(lambda: conn, paths, SecretStore(paths.home / "secrets", env_fallback=env_fallback), writable=False)


def registry_for(home: str | Path | None = None) -> "McpRegistry":
    if home is not None:
        key = str(Path(home).resolve())
        reg = _REGISTRIES.get(key)
        if reg is not None and _usable(reg):
            return reg
        return _readonly_registry(Path(key))
    for key in reversed(_LAST):
        reg = _REGISTRIES[key]
        if _usable(reg):
            return reg
    from ..paths import HubPaths

    return _readonly_registry(HubPaths.resolve().home)


def _agent_info(agent: Any, home: str | Path | None) -> tuple[list[str], str | Path | None]:
    if isinstance(agent, str):
        reg = registry_for(home)
        row = reg.conn.execute("SELECT mcp_servers, home_path FROM agents WHERE id=?", (agent,)).fetchone()
        if row is None:
            from ..errors import NotFound

            raise NotFound(f"không tìm thấy agent '{agent}'")
        import json

        return list(json.loads(row["mcp_servers"] or "[]")), home or Path(row["home_path"]).parent.parent
    if isinstance(agent, dict):
        servers = agent.get("mcp_servers") or []
        hp = agent.get("home_path")
    else:
        servers = getattr(agent, "mcp_servers", None) or []
        hp = getattr(agent, "home_path", None)
    if home is None and hp:
        home = Path(hp).parent.parent
    return list(servers), home


def build_agent_mcp_servers(agent: Any, *, home: str | Path | None = None) -> dict[str, dict[str, Any]]:
    servers, h = _agent_info(agent, home)
    if not servers:
        return {}
    return registry_for(h).build_for_agent_servers(servers)


def tool_annotations(server: str, tool: str, *, home: str | Path | None = None) -> dict[str, Any] | None:
    return registry_for(home).tool_annotations(server, tool)


def agent_mcp_tool_rules(agent: Any, *, home: str | Path | None = None) -> dict[str, list[str]]:
    servers, h = _agent_info(agent, home)
    if not servers:
        return {"allowed": [], "disallowed": [], "warnings": []}
    return registry_for(h).tool_rules_for_servers(servers)


__all__ = ["build_agent_mcp_servers", "tool_annotations", "agent_mcp_tool_rules", "registry_for", "register_registry"]
