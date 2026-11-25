"""Nối secret store, audit và bộ che theo cấu hình claude vào `Hub` (gọi từ `Hub.__init__`).

Từ plan-mcp-claude-native PR7 không còn registry MCP (`hub.mcp`), quản trị `claude mcp` (`hub.mcp_claude`) hay bộ theo dõi
`tool.write` theo annotations: MCP bên thứ ba do claude quản lý theo tài khoản, mọi agent dùng chung.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .. import audit as audit_mod
from ..audit import AuditLog
from ..secrets import SecretStore

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.mcp_client")


def attach(hub: "Hub") -> None:
    """Gắn `hub.secrets`, `hub.audit`, `hub.claude_redact` (che giá trị header/env/args MCP trong cấu hình claude)."""
    try:
        env_fallback = bool(hub.config.get("secrets.env_fallback"))
    except KeyError:  # pragma: no cover
        env_fallback = True
    secrets = SecretStore(hub.paths.home / "secrets", env_fallback=env_fallback)
    hub.secrets = secrets  # type: ignore[attr-defined]
    hub.audit = AuditLog(hub.store, mask=secrets.mask_values)  # type: ignore[attr-defined]
    audit_mod.install(hub.audit)  # type: ignore[attr-defined]
    from .claude_redact import ClaudeConfigRedactor

    hub.claude_redact = ClaudeConfigRedactor(hub.paths.home)  # type: ignore[attr-defined]
    # H-12: bộ che theo GIÁ TRỊ — nạp mọi secret đã lưu
    try:
        secrets.preload()
    except Exception:  # pragma: no cover — không để lỗi nạp che làm hỏng khởi động
        log.debug("không nạp được secret vào bộ che", exc_info=True)
