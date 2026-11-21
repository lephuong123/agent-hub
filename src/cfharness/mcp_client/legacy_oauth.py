"""Server MCP OAuth KIỂU CŨ (nguồn hub, `auth.type: oauth`) — phần còn lại sau khi gỡ OAuth + proxy loopback của hub (v0.2.28,
docs/dev-notes/plan-mcp-claude-native.md PR4 giai đoạn B).

Từ v0.2.28 đăng nhập OAuth của MCP do `claude` giữ theo tài khoản (server `source=claude`, RPC `mcp.claude_*`). Hub KHÔNG còn đăng nhập,
làm mới token, chèn `Authorization: Bearer` hay chạy proxy. Dòng registry `auth.type: oauth` nguồn hub còn sót trên hub cũ:

- vẫn đọc được (cấu hình hợp lệ) — `cfharness mcp migrate-to-claude <tên> --account <id>` chuyển sang claude (chỉ cần url/transport/
  `auth.client_id` + xoá bản ghi `MCP_OAUTH_*` sau khi đăng nhập claude xong — `migrate.py`);
- run của agent gán server đó bị từ chối TRƯỚC spawn (`needs_migration_error`, fail-closed — không lặng lẽ bỏ server);
- doctor báo lỗi, web hiện "cần chuyển sang claude", daemon lên ghi MỘT dòng cảnh báo. Không tự xoá dòng hay secret.

Không thêm dòng OAuth nguồn hub mới (`McpRegistry.add`/`update` từ chối — dùng `cfharness mcp claude add`).
RPC `mcp.oauth_*` + `cfharness mcp login|logout` = stub trả lỗi hướng dẫn trong MỘT bản (gỡ hẳn ở bản sau).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Callable

from ..errors import HubError
from ..redact import register_secret_values

if TYPE_CHECKING:
    from ..secrets import SecretStore

log = logging.getLogger("cfharness.mcp_client")

REMOVED_IN = "v0.2.28"
STATUS = "needs_migration"
STATUS_VI = "cần chuyển sang claude"
STUB_RPCS = ("mcp.oauth_start", "mcp.oauth_callback", "mcp.oauth_status", "mcp.oauth_disconnect", "mcp.oauth_refresh")
_SENSITIVE = ("access_token", "refresh_token", "client_secret", "id_token")


def migrate_cmd(name: str | None = None) -> str:
    return f"cfharness mcp migrate-to-claude {name or '<tên>'} --account <id>"


def removed_message(name: str | None = None) -> str:
    return f"Đăng nhập OAuth do hub giữ đã gỡ từ {REMOVED_IN} — chuyển server sang claude: {migrate_cmd(name)}"


ADD_REFUSED = (f"OAuth do hub giữ đã gỡ từ {REMOVED_IN} — server cần đăng nhập OAuth thêm vào claude theo tài khoản: "
               "`cfharness mcp claude add <tên> --account <id> --url <url>` rồi `cfharness mcp claude login <tên> --account <id>` "
               "(web: Cài đặt → Tài khoản Claude → MCP)")


def secret_name(server: str) -> str:
    """Tên secret của bản ghi OAuth kiểu cũ (`MCP_OAUTH_<TÊN>_<băm>`) — giữ đúng công thức cũ để tìm/xoá bản ghi còn sót."""
    h = hashlib.sha256(server.encode("utf-8")).hexdigest()[:6].upper()
    base = "".join(ch if ch.isalnum() else "_" for ch in server.upper()).strip("_") or "X"
    return f"MCP_OAUTH_{base[:90]}_{h}"


def is_oauth(cfg: Any) -> bool:
    if cfg is None:
        return False
    auth = cfg.get("auth") if isinstance(cfg, dict) else getattr(cfg, "auth", None)
    if auth is None:
        return False
    t = auth.get("type") if isinstance(auth, dict) else getattr(auth, "type", None)
    return t == "oauth"


def needs_migration(cfg: Any) -> bool:
    """Dòng nguồn hub dùng OAuth của hub (đã gỡ) — `cfg` là dict cấu hình hoặc `McpServerConfig`."""
    if not is_oauth(cfg):
        return False
    src = cfg.get("source") if isinstance(cfg, dict) else getattr(cfg, "source", None)
    return src != "claude"


def needs_migration_error(name: str) -> HubError:
    return HubError(f"MCP '{name}' dùng OAuth do hub giữ — đã gỡ từ {REMOVED_IN}; run không chạy tới khi chuyển server sang claude: "
                    f"{migrate_cmd(name)}", data={"server": name, "oauth": STATUS})


def load_record(store: "SecretStore", server: str) -> dict[str, Any] | None:
    """Bản ghi `MCP_OAUTH_*` còn sót (chỉ để biết có token và để nạp giá trị vào bộ che theo giá trị)."""
    raw = store.get(secret_name(server))
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(rec, dict):
        return None
    tok = rec.get("tokens") or {}
    vals = [tok.get(k) for k in _SENSITIVE if isinstance(tok, dict) and isinstance(tok.get(k), str)]
    for c in (rec.get("clients") or {}).values() if isinstance(rec.get("clients"), dict) else ():
        if isinstance(c, dict) and isinstance(c.get("client_secret"), str):
            vals.append(c["client_secret"])
    register_secret_values(*vals)
    return rec


def delete_record(store: "SecretStore", server: str) -> bool:
    return store.delete(secret_name(server))


def legacy_rows(rows: list[dict[str, Any]]) -> list[str]:
    return [r["name"] for r in rows if not r.get("deleted_at") and needs_migration(r.get("config") or {})]


def warn_on_start(hub: Any) -> list[str]:
    """Daemon lên: MỘT dòng cảnh báo liệt kê server OAuth kiểu cũ (không xoá gì)."""
    try:
        names = legacy_rows(hub.mcp.list())
    except Exception:  # best-effort: cảnh báo không được làm hỏng khởi động
        log.debug("không liệt kê được MCP OAuth kiểu cũ", exc_info=True)
        return []
    if names:
        log.warning("MCP OAuth do hub giữ đã gỡ từ %s — %d server cần chuyển sang claude (run của agent gán chúng sẽ bị từ chối): %s; "
                    "lệnh: %s", REMOVED_IN, len(names), ", ".join(names), migrate_cmd())
    return names


def rpc_stubs() -> dict[str, Callable[[dict[str, Any]], Any]]:
    """`mcp.oauth_*` — giữ tên MỘT bản để client cũ nhận lỗi hướng dẫn thay vì "method không tồn tại"."""

    def stub(p: dict[str, Any]) -> Any:
        n = p.get("name")
        name = n if isinstance(n, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", n) else None
        raise HubError(removed_message(name), data={"oauth": "removed"})

    return {m: stub for m in STUB_RPCS}


__all__ = ["ADD_REFUSED", "REMOVED_IN", "STATUS", "STATUS_VI", "STUB_RPCS", "delete_record", "is_oauth", "legacy_rows", "load_record",
           "migrate_cmd", "needs_migration", "needs_migration_error", "removed_message", "rpc_stubs", "secret_name", "warn_on_start"]
