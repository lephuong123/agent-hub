"""Directory hub cho app desktop (hợp đồng C5 — docs/dev-notes/desktop-app.md).

Hub của NGƯỜI QUẢN TRỊ máy phát một danh mục JSON các hub có origin công khai (https) trên máy, để app desktop nhập một lần thay vì
người dùng gõ từng URL. Danh mục KHÔNG cấp quyền gì: mỗi hub vẫn đòi đăng nhập riêng (link một lần/TOTP).

- Nguồn dữ liệu: file trạng thái của `admin_overview` (thư mục chung, bộ đọc `read_dir` đã lọc qua `SPEC`). Chỉ hub có
  `public_origin` (schema 3) hợp lệ mới vào danh mục; file hỏng / sai chủ / schema cũ không có origin → bỏ.
- Mỗi mục CHỈ `{id, name, user, origin}` đã kiểm lại (`entry`): id hex ≤ 16, name/user một dòng (bỏ ký tự điều khiển/bidi/vô hình)
  ≤ 60/≤ 32, origin `https://host[:port]` qua `web/public.parse_public_origin` (không IP, không path). ≤ 200 mục, trùng origin → bỏ mục sau.
  Không có trường nào khác (không chi phí, không trạng thái, không đường dẫn).
- Token: `HUB_HOME/auth/directory.json` (0600, thư mục `auth/` bị sandbox denyRead) chỉ giữ SHA-256 của token; so khớp bằng
  `hmac.compare_digest`. Token chỉ hiện MỘT lần khi tạo/xoay (`cfharness admin directory token [--rotate]`).
- Route công khai `GET /.well-known/cfharness-directory?t=<token>` ở web của hub admin (web/desktop.py): token sai/thiếu/chưa tạo, hub không
  phải admin → 404 JSON; giới hạn tốc độ theo IP + toàn cục ở tiến trình web.
- RPC (∈ HUMAN_ONLY, không ở MANAGER_ALLOWED, đều `require_admin`): `admin.directory` (xuất), `admin.directory_status`,
  `admin.directory_token{rotate}` (GHI — bọc audit, token không vào audit), `admin.directory_serve{token}` (web gọi; token sai → NotFound).
- Cấu hình `admin_directory.name` (tên danh mục, một dòng ≤ 60) — nhánh `admin_directory` ∈ `integrity.SECURITY_KEYS`, chỉ người đặt.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .errors import Conflict, NotFound
from .termsafe import one_line

if TYPE_CHECKING:
    from .daemon import Daemon
    from .hub import Hub
    from .paths import HubPaths

KIND = "cfharness.directory"
SCHEMA = 1
PATH = "/.well-known/cfharness-directory"
NAME_MAX = 60
USER_MAX = 32
ID_MAX = 16
HUBS_MAX = 200
TOKEN_FILE = "directory.json"
DEFAULT_NAME = "cfharness"
_ID_RE = re.compile(r"^[0-9a-f]{1,16}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{20,128}$")
MUTATING_DIRECTORY = ("admin.directory_token",)
READ_DIRECTORY = ("admin.directory", "admin.directory_status", "admin.directory_serve")


# ============================================================================ thuần
def clean_line(v: Any, limit: int) -> str | None:
    """Một dòng đã bỏ ký tự điều khiển/bidi/zero-width, gộp khoảng trắng, ≤ `limit` (cắt). Không phải chuỗi / rỗng → None."""
    if not isinstance(v, str) or len(v) > 8 * limit:
        return None
    s = one_line(v, limit)
    return s or None


def clean_origin(v: Any) -> str | None:
    """Chỉ `https://host[:port]` (tên miền, không IP, không path/query/userinfo) ở dạng chuẩn."""
    if not isinstance(v, str) or len(v) > 300:
        return None
    from .web.public import parse_public_origin

    try:
        o = parse_public_origin(v)
    except ValueError:
        return None
    return o


def entry(raw: Any) -> dict[str, str] | None:
    """Một mục danh mục đã kiểm — sai bất kỳ trường nào → None (bỏ mục)."""
    if not isinstance(raw, dict):
        return None
    hid, origin = raw.get("id"), clean_origin(raw.get("origin"))
    user = raw.get("user")
    if not isinstance(hid, str) or not _ID_RE.match(hid) or origin is None:
        return None
    if not isinstance(user, str) or len(user) > USER_MAX:
        return None
    from .admin_overview import USER_RE

    if not USER_RE.match(user):
        return None
    name = clean_line(raw.get("name"), NAME_MAX) or user
    return {"id": hid, "name": name, "user": user, "origin": origin}


def entries_from_overview(ov: dict[str, Any]) -> tuple[list[dict[str, str]], int]:
    """`admin_overview.read_dir(...)` → (mục hợp lệ, số mục bị bỏ). Chỉ hub đọc được (ok/stale/stopped) có `public_origin`."""
    out: list[dict[str, str]] = []
    dropped = 0
    seen: set[str] = set()
    for h in ov.get("hubs") or []:
        d = h.get("data") if isinstance(h, dict) else None
        if not d or h.get("state") not in ("ok", "stale", "stopped"):
            continue
        if not d.get("public_origin"):
            continue  # hub không public: không phải "bị bỏ", chỉ là không có trong danh mục
        e = entry({"id": d.get("hub_id") or h.get("hub_id"), "name": d.get("display_name"), "user": d.get("user") or h.get("user"),
                   "origin": d.get("public_origin")})
        if e is None or e["origin"] in seen:
            dropped += 1
            continue
        if len(out) >= HUBS_MAX:
            dropped += 1
            continue
        seen.add(e["origin"])
        out.append(e)
    return out, dropped


def document(name: Any, hubs: list[dict[str, str]], *, now: datetime | None = None) -> dict[str, Any]:
    """Tài liệu danh mục đúng hợp đồng C5 (mọi mục qua `entry` một lần nữa)."""
    ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    clean = [e for e in (entry(h) for h in hubs[:HUBS_MAX]) if e is not None]
    return {"kind": KIND, "schema": SCHEMA, "name": clean_line(name, NAME_MAX) or DEFAULT_NAME, "generated_at": ts, "hubs": clean}


def validate_setting(key: str, value: Any) -> None:
    """`config set admin_directory[.…]` — sai → ValueError."""
    vals = dict(value) if key == "admin_directory" and isinstance(value, dict) else {key.split(".", 1)[1]: value} if "." in key else None
    if vals is None:
        raise ValueError("admin_directory phải là object")
    for k, v in vals.items():
        if k == "name":
            if v is not None and (not isinstance(v, str) or len(v) > NAME_MAX or clean_line(v, NAME_MAX) != v):
                raise ValueError(f"admin_directory.name phải là chuỗi một dòng 1..{NAME_MAX} ký tự (không ký tự điều khiển/vô hình) hoặc null")
        else:
            raise ValueError(f"admin_directory.{k}: khoá không có (chỉ name)")


# ============================================================================ token
def token_path(paths: "HubPaths") -> Path:
    return paths.home / "auth" / TOKEN_FILE


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def read_token_state(paths: "HubPaths") -> dict[str, Any] | None:
    p = token_path(paths)
    try:
        if p.is_symlink():
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", d["sha256"]):
        return None
    return d


def write_token(paths: "HubPaths") -> tuple[str, dict[str, Any]]:
    """Sinh token mới (token cũ mất hiệu lực ngay). Trả (token, trạng thái lưu — chỉ băm)."""
    token = secrets.token_urlsafe(32)
    d = paths.home / "auth"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:  # pragma: no cover — Windows
        pass
    prev = read_token_state(paths) or {}
    state = {"sha256": _hash(token), "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
             "rev": int(prev.get("rev") or 0) + 1 if isinstance(prev.get("rev"), int) else 1}
    fd, tmp = tempfile.mkstemp(prefix=".directory.", suffix=".tmp", dir=str(d))
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, token_path(paths))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return token, state


def verify_token(paths: "HubPaths", token: Any) -> bool:
    """So khớp thời gian hằng (băm rồi `compare_digest`). Chưa có token / token sai dạng → False."""
    st = read_token_state(paths)
    if st is None or not isinstance(token, str) or not _TOKEN_RE.match(token):
        hmac.compare_digest("0" * 64, _hash(str(token)[:256]))  # cùng lượng việc cho mọi nhánh
        return False
    return hmac.compare_digest(st["sha256"], _hash(token))


# ============================================================================ dịch vụ
def directory_name(hub: "Hub") -> str:
    try:
        cfg = hub.config.get("admin_directory") or {}
    except KeyError:
        cfg = {}
    return clean_line(cfg.get("name"), NAME_MAX) or DEFAULT_NAME


def build(hub: "Hub") -> tuple[dict[str, Any], int]:
    from .admin_overview import overview

    hubs, dropped = entries_from_overview(overview(hub))
    return document(directory_name(hub), hubs), dropped


def base_origin(hub: "Hub") -> tuple[str, bool]:
    """(origin web của hub admin, công khai?) — `web.public_origin` nếu có, ngược lại origin cục bộ (chỉ dùng được trên máy này)."""
    from .web.public import parse_public_origin

    try:
        wcfg = hub.config.get("web") or {}
    except KeyError:
        wcfg = {}
    try:
        po = parse_public_origin(wcfg.get("public_origin"))
    except ValueError:
        po = None
    if po:
        return po, True
    port = wcfg.get("port")
    if not isinstance(port, int) or isinstance(port, bool):
        try:
            from .web.desktop import preferred_port

            port = preferred_port(hub.paths)
        except Exception:  # pragma: no cover
            port = 8765
    from .web.security import WEB_HOST

    return f"http://{WEB_HOST}:{port}", False


def register_rpc(daemon: "Daemon") -> None:
    """RPC `admin.directory*` — gọi từ `admin_rpc.register_rpc` (trước bước bọc audit của mcp_server/rpc)."""
    import asyncio

    from .admin_control import require_admin
    from .mcp_server.rpc import wrap_with_audit

    hub = daemon.hub

    async def export(p: dict[str, Any]) -> dict[str, Any]:
        require_admin("admin.directory")
        doc, _ = await asyncio.to_thread(build, hub)
        return doc

    async def status(p: dict[str, Any]) -> dict[str, Any]:
        require_admin("admin.directory_status")
        doc, dropped = await asyncio.to_thread(build, hub)
        st = read_token_state(hub.paths)
        origin, public = base_origin(hub)
        return {"name": doc["name"], "hubs": len(doc["hubs"]), "dropped": dropped, "token": st is not None,
                "token_created_at": (st or {}).get("created_at"), "token_rev": (st or {}).get("rev"),
                "origin": origin, "public": public, "path": PATH}

    async def token(p: dict[str, Any]) -> dict[str, Any]:
        require_admin("admin.directory_token")
        if read_token_state(hub.paths) is not None and not p.get("rotate"):
            raise Conflict("đã có token directory — chỉ hiện một lần lúc tạo; dùng --rotate để tạo token mới (token cũ mất hiệu lực)",
                           data={"reason": "token_exists"})
        tok, st = await asyncio.to_thread(write_token, hub.paths)
        origin, public = base_origin(hub)
        return {"url": f"{origin}{PATH}?t={tok}", "public": public, "created_at": st["created_at"], "rev": st["rev"], "rotated": bool(p.get("rotate"))}

    async def serve(p: dict[str, Any]) -> dict[str, Any]:
        """Cho web: token đúng → danh mục; sai/thiếu/chưa tạo/không phải admin → NotFound (route trả 404, không phân biệt lý do)."""
        from .admin_control import is_admin

        ok = await asyncio.to_thread(verify_token, hub.paths, p.get("token"))
        if not ok or not await asyncio.to_thread(is_admin):
            raise NotFound("không có")
        doc, _ = await asyncio.to_thread(build, hub)
        return doc

    methods: dict[str, Callable[[dict[str, Any]], Any]] = {"admin.directory": export, "admin.directory_status": status,
                                                            "admin.directory_token": token, "admin.directory_serve": serve}
    for m in MUTATING_DIRECTORY:
        methods[m] = wrap_with_audit(hub, m, methods[m])
    daemon.methods.update(methods)


__all__ = ["KIND", "PATH", "SCHEMA", "build", "clean_line", "clean_origin", "document", "entries_from_overview", "entry", "register_rpc",
           "read_token_state", "verify_token", "write_token", "MUTATING_DIRECTORY", "READ_DIRECTORY"]
