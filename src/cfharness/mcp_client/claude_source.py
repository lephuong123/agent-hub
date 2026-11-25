"""MCP bên thứ ba do `claude` tự quản lý theo tài khoản (`claude mcp add/login`, connector claude.ai) — hub CHỈ ĐỌC cấu hình.

Từ plan-mcp-claude-native PR7 hub không còn lớp chính sách MCP (registry, gán theo agent, trust, include/exclude, annotations, duyệt):
mọi run làm việc bỏ `--strict-mcp-config` nên claude nạp MỌI server user-scope + connector của tài khoản đang dùng. Module này chỉ:
- đọc danh sách server để liệt kê (`list_accounts` → RPC `mcp.list`), nhận ra server Playwright (chế độ Chrome / allowlist domain gỡ
  khỏi run) và server trỏ về CHÍNH hub (H-18: gỡ khỏi run);
- tìm thư mục cấu hình của tài khoản (`account_config_dir`).

Sự thật đã kiểm (claude 2.1.329 / 2.1.332): chỉ `mcpServers` TẦNG ĐẦU (user-scope) của `<config_dir>/.claude.json` được nạp trong `-p`
(local-scope `projects[<cwd>].mcpServers` thì không); `--strict-mcp-config` chặn cả server này lẫn connector claude.ai, còn
`--setting-sources ""` thì KHÔNG; `--disallowedTools "mcp__<s>__*"` gỡ hết tool của server khỏi ngữ cảnh (server vẫn connect); init
`mcp_servers[].status` ∈ connected | needs-auth | pending.

File `.claude.json` CHỨA SECRET (header/env/args của server, token…). Ở đây không bao giờ trả, log hay lưu giá trị header/env; lệnh
stdio chỉ trả sau khi che giá trị trông như secret (`secret_values`). Không spawn `claude` (`claude mcp list` mất ~50 s).

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


def read_servers(config_dir: Path | None, port: int | None = None) -> dict[str, dict[str, Any]]:
    """mcpServers user-scope của `<config_dir>/.claude.json` → {tên: {transport, host, hub, playwright}}. CHỈ các trường đó (không
    header/env/args/command/url đầy đủ); `hub` = server trỏ về CHÍNH hub (`cfharness mcp serve` / loopback cổng MCP `port` — cổng
    `mcp_server.http_port` của hub, đa instance — hoặc tên dành riêng của hub); `playwright` = server Playwright (theo tên/lệnh/URL).
    File thiếu/hỏng/không phải object → {} (người gọi coi như tài khoản không có server)."""
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
        servers = {}
    return _entries(servers, port=port)


def is_hub_name(name: str) -> bool:
    """Tên dành riêng của MCP server của hub (`names.HUB_MCP_SERVER_NAMES`: cfharness, agent-hub, hub…) — so cả dạng tên trong tên tool."""
    from .. import names as _names

    n = str(name).lower()
    return n in _names.HUB_MCP_SERVER_NAMES or tool_server_name(n) in {tool_server_name(x) for x in _names.HUB_MCP_SERVER_NAMES}


def _entries(servers: dict[str, Any], port: int | None = None, **extra: Any) -> dict[str, dict[str, Any]]:
    """{tên: {transport, host, hub, playwright, …extra}} — cờ tính từ command/args/url NGAY ở đây (không giữ lại lệnh/đối số)."""
    from ..browser import is_playwright_server
    from ..guard.service import is_hub_mcp_config

    out: dict[str, dict[str, Any]] = {}
    for name, entry in servers.items():
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            continue
        tr = _transport(entry)
        try:
            hub = bool(is_hub_mcp_config(entry, port)) or is_hub_name(name)
        except Exception:  # fail-closed: không xác định được → coi như server của hub (gỡ khỏi run)
            hub = True
        try:
            pw = bool(is_playwright_server(name, entry))
        except Exception:  # fail-closed: không xác định được → coi như Playwright (chỉ gỡ ở chế độ Chrome / allowlist)
            pw = True
        out[name] = {"transport": tr, "host": _host(entry.get("url")) if tr != "stdio" else None, "hub": hub, "playwright": pw, **extra}
    return out


def playwright_servers(config_dir: Path | None) -> set[str]:
    """Tên server Playwright claude sẽ nạp (user-scope `.claude.json` + do tổ chức cấp — `managedMcpServers` / `managed-mcp.json`),
    nhận theo tên hoặc lệnh/đối số/URL (`browser.is_playwright_server`). Chỉ trả TÊN. Dùng để gỡ Playwright khỏi run chế độ Chrome /
    allowlist domain (runs.py) và chặn gọi ở guard (`playwright_not_in_run`)."""
    return {n for n, e in all_servers(config_dir).items() if e.get("playwright")}


def _json_obj(p: Path) -> dict[str, Any]:
    try:
        if p.stat().st_size > MAX_BYTES:
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:  # best-effort: file hỏng = không có server (không log nội dung)
        log.warning("không đọc được cấu hình MCP quản lý của claude: %s", type(e).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def remote_settings_path(config_dir: Path | None) -> Path:
    """`remote-settings.json` (cài đặt tổ chức claude.ai tải về, có `managedMcpServers`) nằm TRONG thư mục cấu hình claude
    (`~/.claude` hoặc CLAUDE_CONFIG_DIR) — khác `.claude.json` (cạnh thư mục ở cấu hình mặc định)."""
    if config_dir is not None:
        return Path(config_dir) / "remote-settings.json"
    from ..transfer_sessions import TEST_ENV

    if os.environ.get("CFHARNESS_TESTING") == "1" and os.environ.get(TEST_ENV):
        return Path(os.environ[TEST_ENV]) / ".claude" / "remote-settings.json"
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(env).expanduser() if env else Path.home() / ".claude") / "remote-settings.json"


MANAGED_MCP_FILES = (Path("/Library/Application Support/ClaudeCode/managed-mcp.json"), Path("/etc/claude-code/managed-mcp.json"))


def managed_servers(config_dir: Path | None, port: int | None = None) -> dict[str, dict[str, Any]]:
    """Server MCP do TỔ CHỨC cấp, claude nạp ngoài `.claude.json` (đã thấy thật: `remote-settings.json.managedMcpServers` trên
    2.1.329; `managed-mcp.json` hệ thống). Chỉ tên/transport/host (`managed=True`) — để gỡ khỏi run không gán và đăng ký được."""
    out: dict[str, dict[str, Any]] = {}
    ms = _json_obj(remote_settings_path(config_dir)).get("managedMcpServers")
    if isinstance(ms, dict):
        out.update(_entries(ms, port=port, managed=True))
    if not (os.environ.get("CFHARNESS_TESTING") == "1"):
        for f in MANAGED_MCP_FILES:
            srv = _json_obj(f).get("mcpServers")
            if isinstance(srv, dict):
                out.update(_entries(srv, port=port, managed=True))
    return out


def all_servers(config_dir: Path | None, port: int | None = None) -> dict[str, dict[str, Any]]:
    """Mọi server MCP claude sẽ nạp từ cấu hình (user-scope `.claude.json` + do tổ chức cấp); `.claude.json` thắng khi trùng tên.
    `port` = `mcp_server.http_port` của hub (nhận server HTTP loopback trỏ về hub — `config_port`)."""
    return {**managed_servers(config_dir, port), **read_servers(config_dir, port)}


def config_port(hub: Any) -> int | None:
    """Cổng MCP HTTP của hub (`mcp_server.http_port`) — None = mặc định (`is_hub_mcp_config` dùng cổng mặc định + cổng cũ)."""
    try:
        v = hub.config.get("mcp_server.http_port")
        return int(v) if v else None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _stamp(p: Path) -> tuple[str, int, int] | None:
    try:
        st = p.stat()
        return (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return None


_CACHE: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}
_CACHE_MAX = 32


def cached_all_servers(config_dir: Path | None, port: int | None = None) -> dict[str, dict[str, Any]]:
    """`all_servers` có nhớ theo (đường dẫn, mtime, size) của mọi file nguồn + cổng — chỉ `stat` khi không đổi. Dùng ở đường nóng
    (guard.check mỗi tool): `.claude.json` của người dùng nặng có thể nhiều MB."""
    files = [claude_json_path(config_dir), remote_settings_path(config_dir)]
    if os.environ.get("CFHARNESS_TESTING") != "1":
        files += list(MANAGED_MCP_FILES)
    key = (str(config_dir), port, tuple(_stamp(f) for f in files))
    hit = _CACHE.get(key)
    if hit is None:
        hit = all_servers(config_dir, port)
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = hit
    return hit


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


def hub_servers(config_dir: Path | None, port: int | None = None) -> list[str]:
    """Tên server trong cấu hình claude (user-scope + tổ chức cấp) trỏ về CHÍNH hub (`cfharness mcp serve` / loopback cổng MCP `port`
    / tên dành riêng của hub) — run gỡ khỏi ngữ cảnh (`--disallowedTools`), guard cấm gọi: cho agent quyền điều khiển hub (H-18)."""
    return sorted(n for n, e in cached_all_servers(config_dir, port).items() if e.get("hub"))


# ------------------------------------------------------------------ danh sách thô cho RPC `mcp.list` (chỉ đọc)
def _secret_free(text: str, names: Any) -> str:
    for name in names:
        text = text.replace(f"${{{name}}}", "***")
    return text


def _redact_url(url: str) -> str:
    """URL trong đối số lệnh: bỏ userinfo, che giá trị query trông như secret."""
    from urllib.parse import urlunsplit

    from .secret_values import _Extractor

    try:
        parts = urlsplit(url)
        netloc = parts.hostname or ""
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        ex = _Extractor("x")
        out = ex.url(urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment)))
    except ValueError:
        return "***"
    return _secret_free(out, ex.secrets)


def stdio_target(entry: dict[str, Any]) -> str:
    """`command` + `args` của server stdio, giá trị trông như secret thay bằng `***` (cùng heuristic với bộ che `claude_redact`).
    Không bao giờ env."""
    from ..redact import redact
    from .secret_values import _Extractor

    cmd = str(entry.get("command") or "").strip()
    raw = entry.get("args")
    args = [str(a) for a in raw] if isinstance(raw, list) else []
    ex = _Extractor("x")
    out: list[str] = []
    for a in ex.args(args):
        a = _secret_free(str(a), ex.secrets)
        if a.lower().startswith(("http://", "https://")):
            a = _redact_url(a)
        out.append(a)
    return str(redact(" ".join(x for x in [cmd, *out] if x)))[:500]


def entry_target(entry: dict[str, Any], transport: str) -> str:
    """http/sse → host của URL; stdio → lệnh + đối số đã che."""
    if transport in ("http", "sse"):
        return _host(entry.get("url")) or ""
    return stdio_target(entry)


def raw_servers(config_dir: Path | None) -> tuple[list[dict[str, Any]], str | None]:
    """mcpServers user-scope của `.claude.json` → ([{name, transport, target}], lỗi | None). File không có → ([], None); JSON hỏng /
    không phải object → ([], <lỗi ngắn>). Không bao giờ header/env/giá trị secret."""
    p = claude_json_path(config_dir)
    try:
        if p.stat().st_size > MAX_BYTES:
            return [], f"file quá lớn (> {MAX_BYTES // (1024 * 1024)} MB)"
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], None
    except ValueError as e:
        return [], f"JSON không hợp lệ ({type(e).__name__})"
    except OSError as e:
        return [], f"không đọc được file ({type(e).__name__})"
    if not isinstance(data, dict):
        return [], "nội dung không phải object JSON"
    servers = data.get("mcpServers")
    if servers is None:
        return [], None
    if not isinstance(servers, dict):
        return [], "mcpServers không phải object"
    out = []
    for name in sorted(servers, key=str):
        entry = servers[name]
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            continue
        tr = _transport(entry)
        out.append({"name": name, "transport": tr, "target": entry_target(entry, tr)})
    return out, None


DEFAULT_LABEL = "Cấu hình claude mặc định của máy"


def list_accounts(hub: Any) -> dict[str, Any]:
    """RPC `mcp.list`: server MCP trong cấu hình MẶC ĐỊNH (id null — tài khoản `machine`/`custom`/chưa cấu hình) + MỖI tài khoản
    token. Chỉ đọc `.claude.json` (không chạy `claude mcp list`)."""
    rows: list[tuple[str | None, str, Path | None]] = [(None, DEFAULT_LABEL, None)]
    acc = getattr(hub, "accounts", None)
    if acc is not None:
        from ..accounts.service import CUSTOM, MACHINE

        try:
            items = acc.list().get("accounts") or []
        except Exception:  # best-effort: không đọc được danh sách tài khoản → chỉ cấu hình mặc định
            log.debug("mcp.list: không đọc được tài khoản claude", exc_info=True)
            items = []
        for a in items:
            aid = a.get("id")
            if not aid or aid in (MACHINE, CUSTOM):
                continue
            try:
                cdir = acc.config_dir(aid)
            except Exception:  # best-effort: tài khoản lỗi → bỏ qua
                log.debug("mcp.list: không có thư mục cấu hình của tài khoản", exc_info=True)
                continue
            if cdir is None:
                continue
            rows.append((str(aid), str(a.get("label") or aid), Path(cdir)))
    out = []
    for aid, label, cdir in rows:
        servers, err = raw_servers(cdir)
        out.append({"id": aid, "label": label, "config_path": str(claude_json_path(cdir)), "servers": servers, "error": err})
    return {"accounts": out}


def add_hint(config_dir: Path | str | None) -> str:
    """Một dòng hướng dẫn thêm server bằng `claude mcp add` (tài khoản không mặc định: kèm CLAUDE_CONFIG_DIR=<thư mục>)."""
    base = "claude mcp add -s user <tên> -- <lệnh> [đối số…]   |   claude mcp add -s user --transport http <tên> <URL>"
    return base if config_dir is None else f"CLAUDE_CONFIG_DIR={config_dir} {base}"


__all__ = ["DEFAULT_LABEL", "account_config_dir", "account_label", "add_hint", "all_servers", "claude_json_path", "default_claude_json",
           "cached_all_servers", "config_port", "hub_servers", "is_hub_name", "list_accounts", "managed_servers", "playwright_servers", "raw_servers", "read_servers", "remote_settings_path",
           "stdio_target", "tool_server_name"]
