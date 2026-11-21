"""Chuyển MCP server OAuth của HUB sang MCP do `claude` quản lý theo tài khoản (docs/dev-notes/plan-mcp-claude-native.md PR4).

Từ v0.2.28 (giai đoạn B) hub không còn OAuth/proxy riêng — dòng OAuth nguồn hub còn sót CHỈ chạy lại được sau khi chuyển; mọi thứ cần
về cấu hình cũ (url/transport/`auth.client_id`, tên bản ghi `MCP_OAUTH_*`) nằm ở `legacy_oauth.py`.

`mcp.migrate_to_claude{name, account_id, keep_hub_token?}` (HUMAN_ONLY, MUTATING, audit chỉ danh sách trắng):

1. Kiểm: dòng registry nguồn hub, http/sse, `auth.type == oauth` (server secret tĩnh — header/env/args — GIỮ nguồn hub: secret store +
   mcp.json 0600 của run, không chuyển); client OAuth có `client_secret_ref` → từ chối (claude nhận client secret qua Keychain khi thêm
   tay, không qua JSON); không run nào của agent gán server đang chạy.
2. Dựng entry claude: url + header (đã thay `${…}` — chỉ trong bộ nhớ, đi qua stdin của `claude mcp add-json`) + `oauth.clientId` khi hub
   dùng client đăng ký tay (DCR → bỏ, claude tự đăng ký). Header + tài khoản `machine` → từ chối (luật PR3: `~/.claude.json` chưa chặn đọc).
3. `hub.mcp_claude.add(..., register=False, allow_hub_row=True)` (đã có sẵn cùng tên/host trong cấu hình của tài khoản → dùng lại, không thêm).
4. `McpRegistry.convert_to_claude` — giữ tên/trust/tools/annotations/agent gán. Lỗi ở bước này → gỡ entry vừa thêm (hoàn tác).
5. Mở lượt `claude mcp login` (người hoàn tất bằng `mcp.claude_login_finish` / web hàng tài khoản). Lỗi mở lượt KHÔNG hoàn tác chuyển nguồn.

Token `MCP_OAUTH_*` của hub CHỈ bị xoá khi server đã `connected` dưới đúng cấu hình claude của tài khoản (`on_connected`, gọi từ
`ClaudeMcpAdmin.login_finish` + `status` làm mới) — trừ khi `keep_hub_token` — hoặc khi người gọi rõ `drop_hub_token`. Trạng thái chờ ở
`hub_state mcp.migrations` (∈ `hub_transfer.MACHINE_STATE`). Không bao giờ trả/log/audit url đầy đủ, header, token.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..errors import Conflict, HubError
from ..events import iso
from .config import SOURCE_CLAUDE

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.mcp_client")

STATE_KEY = "mcp.migrations"  # {tên: {account, key, at, keep_hub_token}} — chờ đăng nhập claude rồi xoá token hub
STATIC_NOTE = ("server dùng secret tĩnh (header/env/args) — giữ nguồn hub (secret store, mcp.json 0600 của run); chỉ server OAuth "
               "mới chuyển sang claude")
DEPRECATION_NOTE = ("OAuth do hub tự giữ đã gỡ từ v0.2.28 — server OAuth kiểu cũ không chạy tới khi chuyển sang claude: "
                    "`cfharness mcp migrate-to-claude <tên> --account <id>` (web: trang MCP của server)")


def _state(hub: "Hub") -> dict[str, Any]:
    v = hub.store.state_get(STATE_KEY)
    return dict(v) if isinstance(v, dict) else {}


def _save(hub: "Hub", st: dict[str, Any]) -> None:
    hub.store.state_set(STATE_KEY, st)


def hub_oauth_servers(hub: "Hub") -> list[str]:
    """Server nguồn hub dùng OAuth của hub (chưa xoá mềm) — cần chuyển (doctor/web/cảnh báo lúc daemon lên)."""
    from .legacy_oauth import legacy_rows

    return legacy_rows(hub.mcp.list())  # type: ignore[attr-defined]


def _check(hub: "Hub", name: str) -> tuple[Any, dict[str, Any]]:
    from .legacy_oauth import is_oauth

    row = hub.mcp.require(name)  # type: ignore[attr-defined]
    conf = row.get("config") or {}
    if conf.get("source") == SOURCE_CLAUDE:
        raise Conflict(f"MCP '{name}' đã là nguồn claude — đăng nhập: `cfharness mcp claude login {name} --account <id>`")
    if conf.get("transport") not in ("http", "sse"):
        raise HubError(f"MCP '{name}' là stdio — {STATIC_NOTE}")
    if not is_oauth(conf):
        raise HubError(f"MCP '{name}' không dùng OAuth của hub — {STATIC_NOTE}")
    cfg = hub.mcp.config_of(row)  # type: ignore[attr-defined]
    if cfg.auth is not None and cfg.auth.client_secret_ref:
        raise HubError(f"MCP '{name}' dùng client OAuth có client_secret — chưa chuyển tự động được (claude nhận client secret qua Keychain "
                       "khi thêm tay: `claude mcp add --client-id … --client-secret` dưới tài khoản đó, rồi `cfharness mcp add --source claude`). "
                       "Giữ nguồn hub tới khi đó.")
    return cfg, row


def _busy_runs(hub: "Hub", name: str) -> list[str]:
    users = set(hub.mcp.agents_using(name))  # type: ignore[attr-defined]
    active = getattr(getattr(hub, "runs", None), "active", None) or {}
    return sorted(a.run_id for a in list(active.values()) if a.agent_id in users)


def plan(hub: "Hub", name: str, account_id: Any) -> dict[str, Any]:
    """Xem trước (không ghi): server đủ điều kiện chưa, tài khoản đích, agent đang gán. Lỗi = HubError giải thích vì sao không chuyển."""
    from . import claude_source as cs

    cfg, _ = _check(hub, name)
    aid, cdir = cs.account_config_dir(hub, str(account_id).strip() if account_id else None)
    if cdir is None and cfg.headers:
        raise HubError(f"MCP '{name}' có header — tài khoản 'Đăng nhập của máy' (cấu hình claude mặc định) lưu header RÕ trong ~/.claude.json "
                       "mà agent chưa bị chặn đọc; chọn một tài khoản token", code=-32602)
    warnings = []
    if cfg.auth is not None and cfg.auth.client_id:
        warnings.append("client OAuth đăng ký tay: redirect của claude là http://localhost:<cổng>/callback — thêm địa chỉ này vào ứng dụng "
                        "OAuth ở nhà cung cấp nếu đăng nhập báo sai redirect_uri")
    if cfg.auth is not None and cfg.auth.scopes:
        warnings.append("scope đặt tay của hub không chuyển được — claude xin scope theo server")
    return {"name": name, "account": aid, "account_label": cs.account_label(aid), "transport": cfg.transport,
            "host": cs._host(cfg.url) if "${" not in str(cfg.url or "") else None, "agents": hub.mcp.agents_using(name),  # type: ignore[attr-defined]
            "trust": cfg.trust, "warnings": warnings}


async def migrate(hub: "Hub", name: Any, account_id: Any, *, keep_hub_token: bool = False, start_login: bool = True) -> dict[str, Any]:
    from . import claude_source as cs
    from .claude_admin import DEFAULT_KEY, validate_name
    from .legacy_oauth import load_record

    name = validate_name(name)
    pl = plan(hub, name, account_id)
    busy = _busy_runs(hub, name)
    if busy:
        raise Conflict(f"không chuyển được: {len(busy)} run đang dùng MCP '{name}' ({', '.join(busy[:3])}) — chờ xong hoặc dừng rồi thử lại",
                       data={"runs": busy})
    cfg, _ = _check(hub, name)
    admin = hub.mcp_claude  # type: ignore[attr-defined]
    aid, cdir = cs.account_config_dir(hub, str(account_id).strip() if account_id else None)
    key = DEFAULT_KEY if cdir is None else str(aid)
    entry = hub.secrets.resolve(cfg.claude_entry())  # type: ignore[attr-defined]  # thiếu secret → SecretMissing (chưa ghi gì)
    oauth = {"clientId": cfg.auth.client_id} if cfg.auth is not None and cfg.auth.client_id else None
    existing = cs.read_servers(cdir).get(name)
    added = False
    if existing is not None:
        if existing.get("transport") != cfg.transport or existing.get("host") != cs._host(entry.get("url")):
            raise Conflict(f"cấu hình claude của tài khoản {cs.account_label(aid)} đã có MCP '{name}' khác (transport/host) — gỡ nó "
                           f"(`cfharness mcp claude rm {name} --account {aid or 'machine'}`) hoặc đổi tên rồi thử lại")
        info = existing
    else:
        res = await admin.add(account_id or aid, name, cfg.transport, url=entry.get("url"), headers=entry.get("headers") or None,
                              oauth=oauth, register=False, allow_hub_row=True)
        added = True
        info = {"transport": res["transport"], "host": res.get("host"), "hub": False}
    try:
        reg = hub.mcp.convert_to_claude(name, info)  # type: ignore[attr-defined]
    except Exception:
        if added:
            try:
                await admin.remove(account_id or aid, name, force=True)
            except Exception:  # best-effort: hoàn tác hỏng → báo người gỡ tay; lỗi gốc vẫn được ném lại
                log.exception("không hoàn tác được MCP %s vừa thêm vào claude", name)
        raise
    had_token = bool((load_record(hub.secrets, name) or {}).get("tokens"))  # type: ignore[attr-defined]
    st = _state(hub)
    st[name] = {"account": aid, "key": key, "at": iso(), "keep_hub_token": bool(keep_hub_token)}
    _save(hub, st)
    out: dict[str, Any] = {"name": name, "account": aid, "account_label": cs.account_label(aid), "transport": info["transport"],
                           "host": info.get("host"), "added": added, "agents": reg.get("agents") or [], "trust": reg.get("trust"),
                           "status": "migrated", "hub_token": ("kept" if keep_hub_token else "pending_login") if had_token else "none",
                           "login": None, "warnings": list(pl["warnings"])}
    if start_login:
        try:
            out["login"] = await admin.login_start(account_id or aid, name)
        except HubError as e:
            out["warnings"].append(f"chưa mở được lượt đăng nhập: {e.message} — đăng nhập sau: cfharness mcp claude login {name}"
                                   + (f" --account {aid}" if aid else ""))
    return out


def on_connected(hub: "Hub", key: str, names: list[str]) -> list[str]:
    """`names` vừa `connected` dưới cấu hình claude `key` → xoá token OAuth hub của các server đang chờ chuyển (trừ `keep_hub_token`).
    Trả tên đã xoá token."""
    if not names:
        return []
    st = _state(hub)
    if not st:
        return []
    dropped = []
    for n in names:
        e = st.get(n)
        if not isinstance(e, dict) or e.get("key") != key:
            continue
        st.pop(n, None)
        if e.get("keep_hub_token"):
            continue
        if _drop(hub, n, why="login_ok"):
            dropped.append(n)
    _save(hub, st)
    return dropped


def _drop(hub: "Hub", name: str, *, why: str) -> bool:
    from .legacy_oauth import delete_record

    gone = bool(delete_record(hub.secrets, name))  # type: ignore[attr-defined]
    if gone:
        try:
            hub.audit.record("mcp.migrate_hub_token_deleted", None, None, {"name": name, "reason": why})  # type: ignore[attr-defined]
        except Exception:  # best-effort: audit lỗi không được để token sống lại
            log.exception("không ghi được audit xoá token OAuth hub của %s", name)
    return gone


def drop_hub_token(hub: "Hub", name: Any) -> dict[str, Any]:
    """Người xoá rõ token OAuth hub của server ĐÃ chuyển sang claude (vd đăng nhập xong ngoài hub)."""
    from .claude_admin import validate_name

    name = validate_name(name)
    row = hub.mcp.require(name)  # type: ignore[attr-defined]
    if (row.get("config") or {}).get("source") != SOURCE_CLAUDE:
        raise HubError(f"MCP '{name}' vẫn là nguồn hub — chuyển trước (`cfharness mcp migrate-to-claude {name} --account <id>`); hoặc gỡ "
                       f"hẳn: `cfharness mcp rm {name}`")
    st = _state(hub)
    st.pop(name, None)
    _save(hub, st)
    gone = _drop(hub, name, why="explicit")
    return {"name": name, "status": "hub_token_deleted" if gone else "no_hub_token", "hub_token_deleted": gone}


__all__ = ["DEPRECATION_NOTE", "STATE_KEY", "drop_hub_token", "hub_oauth_servers", "migrate", "on_connected", "plan"]
