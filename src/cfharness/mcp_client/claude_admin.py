"""Thêm / gỡ / đăng nhập / đăng xuất MCP server do `claude` quản lý, theo TÀI KHOẢN Claude (plan-mcp-claude-native PR3).

`hub.mcp_claude` (`ClaudeMcpAdmin`) chạy `claude mcp …` với `CLAUDE_CONFIG_DIR` = thư mục cấu hình của tài khoản (tài khoản token →
`HUB_HOME/claude-config/<id>/`; `machine` / `custom` / chưa cấu hình → cấu hình claude MẶC ĐỊNH của user OS, KHÔNG đặt biến) — đúng
thư mục mà run của tài khoản đó nạp (runs.py `_claude_mcp_dir`). Env luôn bỏ `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY`/biến phiên
Claude Code bao ngoài (`claude mcp add-json … -` từ chối đọc stdin trong phiên Claude Code); cwd trung lập
`HUB_HOME/claude-config/.mcp-cwd` (không nạp `.mcp.json` project của cwd daemon).

- `add`: `claude mcp add-json -s user <tên> -` — JSON (url/headers/command/args/env) CHỈ qua stdin, không bao giờ argv; rồi tạo dòng
  registry `source=claude` (`claude_source.register`, cùng đường `mcp.add{source:claude}` của PR2). Header/env (secret) chỉ cho tài
  khoản token: thư mục `claude-config/<id>/` bị chặn với agent; `.claude.json` của cấu hình mặc định (machine) thì CHƯA (PR1) → từ chối.
- `remove`: `claude mcp remove -s user <tên>`; xoá mềm dòng registry CHỈ khi không còn tài khoản nào (kể cả cấu hình mặc định) có server
  cùng tên — registry là của hub, dùng chung mọi tài khoản. Agent còn gán + đây là bản cuối → Conflict trừ `force`.
- `login_start|finish|cancel`: PTY `claude mcp login <tên> --no-browser` (accounts/login.py) → link authorize; người dán URL chuyển hướng
  → `finish` gõ vào CLI; kết quả LUÔN kiểm lại bằng `claude mcp list`. Lượt `l_…`, ≤ 10 phút, mỗi tài khoản một lượt.
- `logout`: `claude mcp logout <tên>`. `status`: parse chữ `claude mcp list` (cache 30 s, lưu `hub_state mcp.claude_status` cho doctor)
  → {tên: connected|needs_auth|failed|unknown}, CHỈ server user-scope của thư mục đó (`claude_source.read_servers`).

KHÔNG BAO GIỜ đưa vào log / audit / thông báo lỗi / kết quả RPC: url đầy đủ, header, env, args, URL chuyển hướng (chứa code dùng một
lần). Chữ của CLI chỉ trả sau khi che theo giá trị + bỏ mọi URL (`_clean_cli`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import Conflict, HubError, NotFound
from ..events import iso
from ..redact import mask_values, register_secret_values
from . import claude_source as cs
from .config import NAME_RE, RESERVED_NAMES

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.mcp_client")

LOGIN_TTL_S = 600.0
STATUS_TTL_S = 30.0
CACHE_KEY = "mcp.claude_status"  # hub_state: {khoá cấu hình: {checked_at, servers: {tên: trạng thái}}} — ∈ hub_transfer.MACHINE_STATE
DEFAULT_KEY = "default"  # khoá của cấu hình claude mặc định (machine / custom / chưa cấu hình)
STATUSES = ("connected", "needs_auth", "failed", "unknown")
TRANSPORTS = ("http", "sse", "stdio")
MAX_LOGINS = 4
_HEADER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_URL_RE = re.compile(r"\w+://\S+")
_STRIP_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE")


def _clean_cli(text: str, *secrets: str | None) -> str:
    """Chữ stdout/stderr của `claude mcp …` an toàn để trả người: che theo giá trị (secret đã biết + giá trị vừa truyền), bỏ URL,
    một dòng, ≤ 300 ký tự."""
    s = str(text or "")
    for v in secrets:
        if v and len(v) >= 3:
            s = s.replace(v, "…")
    s = _URL_RE.sub("<url>", mask_values(s))
    s = " ".join(s.split())
    return s[:300]


def parse_list(text: str, names: set[str] | frozenset[str]) -> dict[str, str]:
    """Chữ `claude mcp list` → {tên: trạng thái} cho các tên trong `names` (server user-scope). Dòng dạng
    `<tên>: <url|lệnh> (HTTP) - ✓ Connected` / `! Needs authentication` / `✘ Failed to connect — …` (claude 2.1.329). Tên không có dòng
    → không có khoá (bên gọi coi là unknown). Không bao giờ giữ phần url/lệnh."""
    out: dict[str, str] = {}
    for raw in str(text or "").splitlines():
        line = raw.strip()
        for n in names:
            if not line.startswith(n + ": "):
                continue
            rest = line[len(n) + 2 :]
            low = rest.lower()
            if "needs authentication" in low or "needs auth" in low or "needs-auth" in low:
                st = "needs_auth"
            elif "✓ connected" in low or low.endswith("connected"):
                st = "connected"
            elif "failed" in low or "✘" in rest or "✗" in rest:
                st = "failed"
            else:
                st = "unknown"
            out[n] = st
            break
    return out


def validate_name(name: Any) -> str:
    from ..names import HUB_MCP_SERVER_NAMES

    n = str(name or "").strip()
    if not NAME_RE.match(n):
        raise HubError("tên MCP: chữ, số, '-', '_', bắt đầu bằng chữ/số, ≤ 63 ký tự", code=-32602)
    if n in RESERVED_NAMES or n.lower() in {x.lower() for x in HUB_MCP_SERVER_NAMES}:
        raise HubError(f"'{n}' là tên dành riêng của hub", code=-32602)
    return n


class _Login:
    def __init__(self, handle: Any, key: str, aid: str | None, name: str, ttl_s: float) -> None:
        self.handle = handle
        self.key = key
        self.aid = aid
        self.name = name
        self.deadline = time.monotonic() + ttl_s
        self.expires_at = iso(datetime.now(timezone.utc) + timedelta(seconds=ttl_s))
        self.timer: asyncio.TimerHandle | None = None

    def brief(self, login_id: str) -> dict[str, Any]:
        return {"login_id": login_id, "url": self.handle.url, "expires_at": self.expires_at, "account": self.aid, "name": self.name}


class ClaudeMcpAdmin:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.login_ttl_s = LOGIN_TTL_S
        self.status_ttl_s = STATUS_TTL_S
        self.cmd_timeout_s = 60.0
        self.list_timeout_s = 90.0
        self._logins: dict[str, _Login] = {}
        self._cache: dict[str, tuple[float, dict[str, str]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ tài khoản → thư mục cấu hình
    def _resolve(self, account_id: Any, *, create: bool = False) -> tuple[str | None, Path | None, str]:
        """(id tài khoản, thư mục cấu hình | None = mặc định, khoá cache). `create` dựng thư mục tài khoản token (0700 + symlink)."""
        aid, cdir = cs.account_config_dir(self.hub, str(account_id).strip() if account_id else None)
        if cdir is None:
            return aid, None, DEFAULT_KEY
        if create or cdir.exists():
            cdir = self.hub.accounts.ensure_config_dir(aid)  # type: ignore[attr-defined]  # dựng lại symlink (idempotent)
        return aid, cdir, str(aid)

    def _claude_cfg(self) -> dict[str, Any]:
        return dict(self.hub.config.get("runtime.claude") or {})

    def _binary(self) -> str:
        return str(self._claude_cfg().get("binary") or "claude")

    def _env(self, cdir: Path | None) -> dict[str, str]:
        from ..runtime.claude_cli import child_env

        env = child_env(self._claude_cfg())
        for k in list(env):
            if k in _STRIP_ENV or k.startswith("CLAUDE_CODE_"):
                env.pop(k, None)
        if cdir is not None:
            env["CLAUDE_CONFIG_DIR"] = str(cdir)
        else:
            from ..transfer_sessions import TEST_ENV

            if os.environ.get("CFHARNESS_TESTING") == "1" and os.environ.get(TEST_ENV):  # test: cấu hình mặc định = thư mục tạm
                env["CLAUDE_CONFIG_DIR"] = os.environ[TEST_ENV]
        return env

    def _cwd(self) -> str:
        d = Path(self.hub.paths.home) / "claude-config" / ".mcp-cwd"
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        return str(d)

    async def _run(self, args: list[str], cdir: Path | None, *, stdin: str | None = None, timeout: float | None = None) -> tuple[int, str, str]:
        from ..oscompat import exec_argv, no_window_kwargs

        argv = exec_argv([self._binary(), *args])
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self._env(cdir), cwd=self._cwd(), **no_window_kwargs())
        except OSError as e:
            raise HubError(f"không chạy được `{self._binary()}` ({type(e).__name__}) — kiểm tra cài đặt Claude Code / runtime.claude.binary") from e
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(stdin.encode("utf-8") if stdin is not None else None),
                                                  float(timeout or self.cmd_timeout_s))
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:  # pragma: no cover
                pass
            raise HubError(f"`claude {' '.join(args[:2])}` quá thời gian") from None
        return int(proc.returncode or 0), out_b.decode("utf-8", "replace"), err_b.decode("utf-8", "replace")

    def _lock(self, key: str) -> asyncio.Lock:
        lk = self._locks.get(key)
        if lk is None:
            lk = self._locks[key] = asyncio.Lock()
        return lk

    def _label(self, aid: str | None) -> str:
        return cs.account_label(aid)

    def _servers(self, cdir: Path | None) -> dict[str, dict[str, Any]]:
        return cs.read_servers(cdir)

    def _require_server(self, aid: str | None, cdir: Path | None, name: str) -> dict[str, Any]:
        have = self._servers(cdir)
        if name not in have:
            raise NotFound(f"không có MCP '{name}' trong cấu hình claude của tài khoản {self._label(aid)}", data={"available": sorted(have)})
        return have[name]

    def _busy(self, key: str, name: str) -> list[str]:
        """Run đang chạy dùng MCP `name` nguồn claude dưới cấu hình `key`."""
        runs = getattr(self.hub, "runs", None)
        active = getattr(runs, "active", None) or {}
        out = []
        for a in list(active.values()):
            if name not in (getattr(a, "claude_mcp", None) or ()):
                continue
            sess = getattr(a, "claude_session", None)
            if (key == DEFAULT_KEY and sess in (None, "machine", "custom")) or sess == key:
                out.append(a.run_id)
        return sorted(out)

    def _require_idle(self, key: str, name: str, what: str) -> None:
        busy = self._busy(key, name)
        if busy:
            raise Conflict(f"không {what} được: {len(busy)} run đang dùng MCP '{name}' của tài khoản này ({', '.join(busy[:3])}) — chờ "
                           "các run đó xong hoặc dừng chúng rồi thử lại", data={"runs": busy})

    def _invalidate(self, key: str) -> None:
        self._cache.pop(key, None)

    # ------------------------------------------------------------------ thêm / gỡ
    async def add(self, account_id: Any, name: Any, transport: Any, *, url: Any = None, command: Any = None, args: Any = None,
                  headers: Any = None, env: Any = None, register: bool = True, trust: Any = None, description: Any = None) -> dict[str, Any]:
        name = validate_name(name)
        tr = str(transport or "").strip().lower() or ("stdio" if command else "http")
        if tr not in TRANSPORTS:
            raise HubError("transport phải là http | sse | stdio", code=-32602)
        entry: dict[str, Any] = {"type": tr}
        secrets: list[str] = []
        if tr in ("http", "sse"):
            u = str(url or "").strip()
            if not re.match(r"^https?://[^\s/]+", u) or any(c.isspace() for c in u) or len(u) > 2048:
                raise HubError("transport http/sse cần url http(s)://… hợp lệ", code=-32602)
            if command or args or env:
                raise HubError("transport http/sse không nhận command/args/env", code=-32602)
            entry["url"] = u
            secrets.append(u)
            if headers:
                if not isinstance(headers, dict):
                    raise HubError("headers phải là object {tên: giá trị}", code=-32602)
                hs: dict[str, str] = {}
                for k, v in headers.items():
                    if not _HEADER_RE.match(str(k)) or not isinstance(v, str) or not v or "\n" in v or "\r" in v or len(v) > 8192:
                        raise HubError(f"header không hợp lệ: '{str(k)[:64]}'", code=-32602)
                    hs[str(k)] = v
                    secrets.append(v)
                entry["headers"] = hs
        else:
            c = str(command or "").strip()
            if not c or "\n" in c:
                raise HubError("transport stdio cần command", code=-32602)
            if url or headers:
                raise HubError("transport stdio không nhận url/headers", code=-32602)
            entry["command"] = c
            al = args or []
            if not isinstance(al, list) or not all(isinstance(a, str) for a in al):
                raise HubError("args phải là danh sách chuỗi", code=-32602)
            entry["args"] = list(al)
            secrets.extend([c, *al])
            if env:
                if not isinstance(env, dict):
                    raise HubError("env phải là object {KHOÁ: giá trị}", code=-32602)
                ev: dict[str, str] = {}
                for k, v in env.items():
                    if not _ENV_RE.match(str(k)) or not isinstance(v, str):
                        raise HubError(f"biến env không hợp lệ: '{str(k)[:64]}'", code=-32602)
                    ev[str(k)] = v
                    secrets.append(v)
                entry["env"] = ev
        # che THEO GIÁ TRỊ ngay (log/lỗi/audit) — chỉ giá trị header/env (secret thật); url/lệnh/args không (chữ chung như `npx` sẽ bị
        # che khắp nơi) mà được gỡ khỏi mọi chữ trả về bằng `_clean_cli` + danh sách trắng audit
        register_secret_values(*(entry.get("headers") or {}).values(), *(entry.get("env") or {}).values())
        from ..guard.service import is_hub_mcp_config

        try:
            port = self.hub.config.get("mcp_server.http_port")
        except KeyError:  # pragma: no cover
            port = None
        if is_hub_mcp_config(entry, int(port) if port else None):
            raise HubError(f"MCP '{name}' trỏ về chính hub — không thêm làm MCP của agent (H-18)")
        aid, cdir, key = self._resolve(account_id)
        if cdir is None and (entry.get("headers") or entry.get("env")):
            raise HubError("cấu hình claude mặc định (tài khoản 'Đăng nhập của máy') lưu header/env RÕ trong ~/.claude.json mà agent chưa bị "
                           "chặn đọc — thêm server cần secret tĩnh bằng `cfharness mcp add` (nguồn hub, secret store), hoặc chọn một tài "
                           "khoản token (thư mục cấu hình riêng, agent không đọc được)", code=-32602)
        row = self.hub.mcp.get_row(name, include_deleted=False)  # type: ignore[attr-defined]
        if row is not None and (row.get("config") or {}).get("source") != "claude":
            raise Conflict(f"đã có MCP '{name}' nguồn hub trong registry — đặt tên khác (hai server cùng tên sẽ đè nhau trong run)")
        async with self._lock(key):
            if name in self._servers(cdir):
                raise Conflict(f"đã có MCP '{name}' trong cấu hình claude của tài khoản {self._label(aid)}")
            aid, cdir, key = self._resolve(account_id, create=True)
            rc, out, err = await self._run(["mcp", "add-json", "-s", "user", name, "-"], cdir, stdin=json.dumps(entry, ensure_ascii=False))
            self._invalidate(key)
            if rc != 0:
                raise HubError(f"`claude mcp add-json` lỗi: {_clean_cli(err or out, *secrets) or f'exit {rc}'}")
        info = self._servers(cdir).get(name)
        if info is None:
            raise HubError(f"`claude mcp add-json` báo xong nhưng chưa thấy '{name}' trong cấu hình claude của tài khoản {self._label(aid)}")
        res: dict[str, Any] = {"account": aid, "name": name, "transport": info["transport"], "host": info.get("host"), "registered": None,
                               "warnings": []}
        if register:
            extra: dict[str, Any] = {}
            if trust is not None:
                extra["trust"] = trust
            if description:
                extra["description"] = str(description)[:500]
            try:
                res["registered"] = cs.register(self.hub, {"name": name, "source": "claude", **extra}, aid, info=info)
            except HubError as e:
                res["warnings"].append(f"đã thêm vào claude nhưng chưa đăng ký registry: {e.message}")
        if tr == "stdio":
            res["warnings"].append("server stdio không cần đăng nhập; lệnh chạy dưới quyền user OS của hub khi run dùng nó")
        return res

    def _others_with(self, name: str, key: str) -> list[str]:
        """Khoá cấu hình KHÁC `key` còn có server `name` (mặc định + mọi tài khoản token có thư mục)."""
        out = []
        if key != DEFAULT_KEY and name in self._servers(None):
            out.append(DEFAULT_KEY)
        acc = getattr(self.hub, "accounts", None)
        if acc is not None:
            for a in acc.list().get("accounts") or []:
                aid = a.get("id")
                if a.get("kind") != "token" or aid == key:
                    continue
                d = acc.config_dir(aid)
                if d is not None and d.exists() and name in self._servers(d):
                    out.append(aid)
        return out

    async def remove(self, account_id: Any, name: Any, *, force: bool = False) -> dict[str, Any]:
        name = validate_name(name)
        aid, cdir, key = self._resolve(account_id)
        async with self._lock(key):
            self._require_server(aid, cdir, name)
            self._require_idle(key, name, "gỡ MCP")
            others = self._others_with(name, key)
            row = self.hub.mcp.get_row(name, include_deleted=False)  # type: ignore[attr-defined]
            mine = row is not None and (row.get("config") or {}).get("source") == "claude"
            if mine and not others and not force:
                users = self.hub.mcp.agents_using(name)  # type: ignore[attr-defined]
                if users:
                    raise Conflict(f"MCP '{name}' đang được agent dùng: {', '.join(users)} và không tài khoản nào khác còn server này — gỡ "
                                   "khỏi agent trước, hoặc dùng force", data={"agents": users})
            rc, out, err = await self._run(["mcp", "remove", "-s", "user", name], cdir)
            self._invalidate(key)
            if rc != 0:
                raise HubError(f"`claude mcp remove` lỗi: {_clean_cli(err or out) or f'exit {rc}'}")
            for lid, s in list(self._logins.items()):
                if s.key == key and s.name == name:
                    await self._close_login(lid)
        registry = "none"
        if mine:
            if others:
                registry = "kept"
            else:
                self.hub.mcp.remove(name, force=force)  # type: ignore[attr-defined]
                registry = "removed"
        return {"account": aid, "name": name, "registry": registry, "other_accounts": others}

    # ------------------------------------------------------------------ đăng nhập / đăng xuất
    def _pending(self, key: str) -> list[dict[str, Any]]:
        return [s.brief(lid) for lid, s in self._logins.items() if s.key == key and time.monotonic() <= s.deadline]

    async def login_start(self, account_id: Any, name: Any) -> dict[str, Any]:
        name = validate_name(name)
        aid, cdir, key = self._resolve(account_id)
        info = self._require_server(aid, cdir, name)
        if info["transport"] == "stdio":
            raise HubError(f"MCP '{name}' là stdio — không có đăng nhập OAuth (chỉ http/sse)")
        for lid, s in list(self._logins.items()):  # mỗi tài khoản một lượt; tổng tối đa MAX_LOGINS
            if s.key == key:
                await self._close_login(lid)
        while len(self._logins) >= MAX_LOGINS:
            await self._close_login(next(iter(self._logins)))
        from ..accounts.login import find_any_url, mcp_login_args, start_login

        handle = await start_login(self._binary(), self._env(cdir), args=mcp_login_args(name), url_finder=find_any_url,
                                   what=f"claude mcp login {name}", cwd=self._cwd())
        if not str(handle.url or "").startswith("https://"):
            await handle.close()
            raise HubError("`claude mcp login` không đưa link https hợp lệ — thử lại")
        login_id = "l_" + uuid.uuid4().hex[:12]
        s = _Login(handle, key, aid, name, self.login_ttl_s)
        self._logins[login_id] = s
        try:
            s.timer = asyncio.get_running_loop().call_later(self.login_ttl_s, self._expire_soon, login_id)
        except RuntimeError:  # pragma: no cover
            pass
        return s.brief(login_id)

    async def login_finish(self, login_id: Any, redirect_url: Any) -> dict[str, Any]:
        url = str(redirect_url or "").strip()
        if not url:
            raise HubError("thiếu URL chuyển hướng — dán địa chỉ trên thanh trình duyệt sau khi đồng ý cấp quyền", code=-32602)
        register_secret_values(url)  # chứa code dùng một lần — che theo giá trị ngay (log/lỗi)
        s = self._logins.get(str(login_id or ""))
        if s is not None and time.monotonic() > s.deadline:
            await self._close_login(str(login_id))
            s = None
        if s is None:
            raise NotFound("lượt đăng nhập MCP không tồn tại hoặc đã hết hạn (10 phút) — bấm đăng nhập lại")
        try:
            await s.handle.submit_redirect(url)
        finally:
            await self._close_login(str(login_id))
        self._invalidate(s.key)
        st = await self.status(s.aid, refresh=True)
        cur = next((x for x in st["servers"] if x["name"] == s.name), None)
        status = (cur or {}).get("status") or "unknown"
        if status == "needs_auth":
            raise HubError(f"`claude mcp login {s.name}` đã chạy nhưng server vẫn báo cần đăng nhập — máy hub có thể không lưu được token "
                           "(Keychain). Thử lại, hoặc chạy `claude mcp login` trong terminal của user hub.")
        return {"account": s.aid, "name": s.name, "status": status, "ok": status == "connected"}

    async def login_cancel(self, login_id: Any) -> dict[str, Any]:
        await self._close_login(str(login_id or ""))
        return {"ok": True}

    async def logout(self, account_id: Any, name: Any) -> dict[str, Any]:
        name = validate_name(name)
        aid, cdir, key = self._resolve(account_id)
        async with self._lock(key):
            self._require_server(aid, cdir, name)
            self._require_idle(key, name, "đăng xuất MCP")
            rc, out, err = await self._run(["mcp", "logout", name], cdir)
            self._invalidate(key)
        if rc != 0:
            raise HubError(f"`claude mcp logout` lỗi: {_clean_cli(err or out) or f'exit {rc}'}")
        return {"account": aid, "name": name, "ok": True}

    async def _close_login(self, login_id: str) -> bool:
        s = self._logins.pop(login_id, None)
        if s is None:
            return False
        if s.timer is not None:
            s.timer.cancel()
        try:
            await s.handle.close()
        except Exception:  # pragma: no cover — close idempotent, lỗi đóng không được che kết quả
            log.exception("không đóng được lượt đăng nhập MCP %s", login_id)
        return True

    def _expire_soon(self, login_id: str) -> None:
        from ..aiotasks import spawn

        try:
            spawn(self._close_login(login_id), name=f"mcp-claude-login-expire:{login_id}")
        except RuntimeError:  # pragma: no cover
            self._logins.pop(login_id, None)

    async def close_all(self) -> None:
        for lid in list(self._logins):
            await self._close_login(lid)

    # ------------------------------------------------------------------ trạng thái
    def _save_cache(self, key: str, servers: dict[str, str]) -> None:
        try:
            c = self.hub.store.state_get(CACHE_KEY)
            c = dict(c) if isinstance(c, dict) else {}
            c[key] = {"checked_at": iso(), "servers": dict(servers)}
            self.hub.store.state_set(CACHE_KEY, c)
        except Exception:  # best-effort: cache cho doctor/web, không phải nguồn sự thật
            log.debug("không lưu được cache trạng thái MCP claude", exc_info=True)

    def _stored(self, key: str) -> dict[str, Any] | None:
        try:
            c = self.hub.store.state_get(CACHE_KEY)
        except Exception:  # best-effort: không có cache = chưa kiểm
            log.debug("không đọc được cache trạng thái MCP claude", exc_info=True)
            return None
        e = c.get(key) if isinstance(c, dict) else None
        return e if isinstance(e, dict) else None

    async def status(self, account_id: Any = None, *, refresh: bool = False, cached: bool = False) -> dict[str, Any]:
        """Server user-scope của cấu hình claude của tài khoản + trạng thái. `cached=True`: KHÔNG chạy claude (web vẽ mỗi lần tải
        trang) — trạng thái từ cache RAM/hub_state, chưa có → unknown. `refresh=True`: bỏ cache."""
        aid, cdir, key = self._resolve(account_id)
        have = self._servers(cdir)
        statuses: dict[str, str] = {}
        checked_at: str | None = None
        err: str | None = None
        hit = self._cache.get(key)
        if hit and not refresh and time.monotonic() - hit[0] < self.status_ttl_s:
            statuses = hit[1]
            checked_at = (self._stored(key) or {}).get("checked_at")
        elif cached:
            e = self._stored(key) or {}
            statuses = {k: v for k, v in (e.get("servers") or {}).items() if v in STATUSES}
            checked_at = e.get("checked_at")
        elif have:
            async with self._lock(key):
                try:
                    rc, out, er = await self._run(["mcp", "list"], cdir, timeout=self.list_timeout_s)
                except HubError as e:
                    rc, out, er, err = -1, "", "", e.message
                if rc == 0:
                    statuses = parse_list(out, frozenset(have))
                    self._cache[key] = (time.monotonic(), statuses)
                    self._save_cache(key, statuses)
                    checked_at = iso()
                elif err is None:
                    err = f"`claude mcp list` lỗi: {_clean_cli(er or out) or f'exit {rc}'}"
        rows = {r["name"]: r for r in self.hub.mcp.list()}  # type: ignore[attr-defined]
        servers = []
        for n in sorted(have):
            r = rows.get(n)
            src = ((r or {}).get("config") or {}).get("source")
            servers.append({"name": n, "transport": have[n]["transport"], "host": have[n].get("host"), "hub": have[n]["hub"],
                            "status": statuses.get(n, "unknown"), "registered": r is not None,
                            "registered_source": None if r is None else ("claude" if src == "claude" else "hub"),
                            "agents": self.hub.mcp.agents_using(n) if r is not None else []})  # type: ignore[attr-defined]
        return {"account": aid, "account_label": self._label(aid), "config_path": str(cs.claude_json_path(cdir)), "servers": servers,
                "checked_at": checked_at, "error": err, "logins": self._pending(key)}


__all__ = ["CACHE_KEY", "DEFAULT_KEY", "ClaudeMcpAdmin", "parse_list", "validate_name"]
