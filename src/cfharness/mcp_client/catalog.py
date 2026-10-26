"""Danh mục MCP server để duyệt & cài (web `/mcp/browse`, RPC `mcp.catalog`).

Nguồn: MCP Registry chính thức `GET https://registry.modelcontextprotocol.io/v0/servers?search=&limit=&cursor=&version=latest`
(một mục: `GET /v0/servers/<tên url-encode>/versions/latest`). Dạng trả về (đã kiểm bằng curl 2026-10-26):
`{"servers":[{"server":{name,title?,description,version,repository{url},websiteUrl?,remotes:[{type: streamable-http|sse, url,
headers:[{name,description,isRequired,isSecret,value}], variables:{tên:{description,isRequired,default}}}],
packages:[{registryType: npm|pypi|oci|nuget|mcpb, identifier, version, runtimeHint, transport{type},
environmentVariables:[{name,description,isRequired,isSecret,default}], packageArguments:[{type: positional|named, name?, value?}]}]},
"_meta":{...}}], "metadata":{nextCursor?, count}}`.

- Cache: RAM + đĩa (`HUB_HOME/cache/mcp_catalog/`), TTL `CACHE_TTL_S`. Lỗi mạng → bản cache cũ (kể cả hết hạn) → danh sách
  tuyển chọn đóng gói sẵn (`CURATED`, chỉ gồm mục đã kiểm trên registry/npm/PyPI).
- Chỉ ĐỌC dữ liệu từ mạng; không chạy gì. Phương án cài (`install_options`) chỉ dựng cấu hình đề xuất — cài thật đi qua
  `mcp.add` (probe + trust mặc định untrusted) như thêm tay.
- `check_oauth(url)`: remote không có header bắt buộc mà `initialize` trả 401 → cần đăng nhập OAuth (web cài với
  `auth: {type: oauth}`, người bấm Kết nối — docs/dev-notes/mcp-oauth.md).
Test thay `_http_get_json`/`_http_status` (không gọi mạng thật).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..secrets import normalize_name

log = logging.getLogger("cfharness.mcp_client.catalog")

REGISTRY_URL = "https://registry.modelcontextprotocol.io"
CACHE_TTL_S = 600.0
TIMEOUT_S = 8.0
LIMIT_MAX = 50
USER_AGENT = "cfharness-mcp-catalog/1"
REMOTE_TRANSPORTS = {"streamable-http": "http", "http": "http", "sse": "sse"}
SUPPORTED_PACKAGES = ("npm", "pypi")

_mem: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


# ============================================================================ danh sách tuyển chọn (offline)
def _remote(name: str, title: str, description: str, version: str, url: str, repo: str, *, oauth: bool = False,
            headers: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"server": {"name": name, "title": title, "description": description, "version": version, "repository": {"url": repo},
                       "remotes": [{"type": "streamable-http", "url": url, **({"headers": headers} if headers else {})}]},
            "_meta": {"cfharness/curated": {"oauth": oauth}}}


def _package(name: str, title: str, description: str, version: str, rtype: str, ident: str, repo: str, *,
             env: list[dict[str, Any]] | None = None, args: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    pkg: dict[str, Any] = {"registryType": rtype, "identifier": ident, "version": version, "transport": {"type": "stdio"}}
    if env:
        pkg["environmentVariables"] = env
    if args:
        pkg["packageArguments"] = args
    return {"server": {"name": name, "title": title, "description": description, "version": version, "repository": {"url": repo},
                       "packages": [pkg]}, "_meta": {"cfharness/curated": {}}}


# Đã kiểm 2026-10-26: mục registry (playwright, github, atlassian, figma — tên/URL/phiên bản lấy nguyên từ registry);
# filesystem/fetch KHÔNG có trên registry (tên `modelcontextprotocol/…` là của hub) — gói đã kiểm trên npm
# (@modelcontextprotocol/server-filesystem 2.0.1) và PyPI (mcp-server-fetch 2026.10.24). Remote atlassian/figma/github trả
# 401 cho `initialize` không token (atlassian/github kèm WWW-Authenticate resource_metadata) → OAuth.
CURATED: list[dict[str, Any]] = [
    _package("io.github.microsoft/playwright-mcp", "Playwright", "Playwright Tools for MCP — điều khiển trình duyệt (mở trang, bấm, điền form, chụp ảnh).",
             "0.0.82", "npm", "@playwright/mcp", "https://github.com/microsoft/playwright-mcp"),
    _remote("io.github.github/github-mcp-server", "GitHub", "Connect AI assistants to GitHub - manage repos, issues, PRs, and workflows through natural language.",
            "2.0.3", "https://api.githubcopilot.com/mcp/", "https://github.com/github/github-mcp-server",
            headers=[{"name": "Authorization", "description": "Authorization header with authentication token (PAT or App token)", "isSecret": True}]),
    _package("modelcontextprotocol/server-filesystem", "Filesystem", "MCP server for filesystem access — đọc/ghi file trong các thư mục được cho phép.",
             "2.0.1", "npm", "@modelcontextprotocol/server-filesystem", "https://github.com/modelcontextprotocol/servers",
             args=[{"type": "positional", "valueHint": "thư mục được phép", "isRequired": True, "description": "Thư mục server được phép truy cập"}]),
    _package("modelcontextprotocol/server-fetch", "Fetch", "Fetch web content and convert it to markdown for LLMs.",
             "2026.10.24", "pypi", "mcp-server-fetch", "https://github.com/modelcontextprotocol/servers"),
    _remote("com.atlassian/atlassian-mcp-server", "Atlassian Rovo", "Connect to Atlassian Jira, Confluence, Loom, and more to search, create, and manage your work.",
            "2.0.0", "https://mcp.atlassian.com/v2/mcp", "https://github.com/atlassian/atlassian-mcp-server", oauth=True),
    _remote("com.figma.mcp/mcp", "Figma", "The Figma MCP server brings Figma design context directly into your AI workflow.",
            "1.0.3", "https://mcp.figma.com/mcp", "https://github.com/figma/mcp-server-guide", oauth=True),
]


# ============================================================================ HTTP (thay được trong test)
def _http_get_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — URL cố định https registry
        return json.loads(resp.read(4_000_000).decode("utf-8"))


def _http_status(url: str, timeout: float) -> int:
    """POST `initialize` tối thiểu (không header xác thực) → mã HTTP. Không đọc/ghi gì khác."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "cfharness", "version": "0"}}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                                                                         "Accept": "application/json, text/event-stream"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — chỉ http(s) (kiểm ở check_oauth)
            return int(resp.status)
    except urllib.error.HTTPError as e:
        return int(e.code)


# ============================================================================ cache
def _cache_file(cache_dir: Path | None, key: str) -> Path | None:
    if cache_dir is None:
        return None
    return cache_dir / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")


def _cache_get(key: str, cache_dir: Path | None, *, max_age: float | None) -> Any:
    now = time.time()
    with _lock:
        hit = _mem.get(key)
    if hit is not None and (max_age is None or now - hit[0] <= max_age):
        return hit[1]
    f = _cache_file(cache_dir, key)
    if f is not None and f.is_file():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if max_age is None or now - float(d.get("ts") or 0) <= max_age:
                with _lock:
                    _mem[key] = (float(d.get("ts") or 0), d.get("data"))
                return d.get("data")
        except (OSError, ValueError):
            return None
    return None


def _cache_put(key: str, data: Any, cache_dir: Path | None) -> None:
    ts = time.time()
    with _lock:
        _mem[key] = (ts, data)
    f = _cache_file(cache_dir, key)
    if f is None:
        return
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": ts, "key": key, "data": data}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(f)
    except OSError:  # pragma: no cover — cache đĩa là phụ
        log.debug("không ghi được cache catalog", exc_info=True)


def clear_cache() -> None:
    with _lock:
        _mem.clear()


# ============================================================================ chuẩn hoá
def _s(v: Any, n: int = 2000) -> str:
    return str(v)[:n] if isinstance(v, (str, int, float)) else ""


def _url(v: Any) -> str:
    """Link hiển thị (repo/website) chỉ nhận http(s) — chặn `javascript:` & co. từ dữ liệu bên thứ ba."""
    u = _s(v, 500).strip()
    return u if re.match(r"^https?://[^\s\"'<>]+$", u) else ""


def _field(d: dict[str, Any]) -> dict[str, Any]:
    return {"name": _s(d.get("name"), 200), "description": _s(d.get("description"), 500), "required": bool(d.get("isRequired")),
            "secret": bool(d.get("isSecret")), "default": _s(d.get("default") if d.get("default") is not None else d.get("value"), 500)}


def normalize(item: dict[str, Any], *, trusted_meta: bool = False) -> dict[str, Any] | None:
    """Một phần tử `servers[]` của registry → mục gọn, chỉ trường cần hiển thị/cài. Dữ liệu là của bên thứ ba: chỉ là CHỮ.
    `trusted_meta` chỉ bật cho `CURATED` (khoá `cfharness/curated` trong dữ liệu registry bị bỏ qua)."""
    srv = item.get("server") if isinstance(item, dict) else None
    if not isinstance(srv, dict) or not isinstance(srv.get("name"), str):
        return None
    meta = item.get("_meta") if isinstance(item.get("_meta"), dict) else {}
    official = meta.get("io.modelcontextprotocol.registry/official") if isinstance(meta, dict) else None
    curated = meta.get("cfharness/curated") if trusted_meta and isinstance(meta, dict) else None
    remotes = []
    for r in srv.get("remotes") or []:
        if not isinstance(r, dict) or not isinstance(r.get("url"), str):
            continue
        variables = r.get("variables") if isinstance(r.get("variables"), dict) else {}
        remotes.append({"type": _s(r.get("type"), 40), "transport": REMOTE_TRANSPORTS.get(str(r.get("type"))),
                        "url": _s(r.get("url"), 1000),
                        "headers": [_field(h) for h in r.get("headers") or [] if isinstance(h, dict) and h.get("name")],
                        "variables": [{**_field(v), "name": _s(k, 100)} for k, v in variables.items() if isinstance(v, dict)]})
    packages = []
    for p in srv.get("packages") or []:
        if not isinstance(p, dict) or not isinstance(p.get("identifier"), str):
            continue
        tr = p.get("transport") if isinstance(p.get("transport"), dict) else {}
        args = []
        for a in p.get("packageArguments") or []:
            if isinstance(a, dict):
                args.append({"type": _s(a.get("type"), 20) or "positional", "name": _s(a.get("name"), 200), "value": _s(a.get("value"), 500),
                             "hint": _s(a.get("valueHint"), 100), "description": _s(a.get("description"), 300), "required": bool(a.get("isRequired")),
                             "secret": bool(a.get("isSecret"))})
        packages.append({"registry_type": _s(p.get("registryType"), 20), "identifier": _s(p.get("identifier"), 300),
                         "version": _s(p.get("version"), 60), "runtime_hint": _s(p.get("runtimeHint"), 40), "transport": _s(tr.get("type"), 40) or "stdio",
                         "env": [_field(e) for e in p.get("environmentVariables") or [] if isinstance(e, dict) and e.get("name")], "args": args})
    repo = srv.get("repository") if isinstance(srv.get("repository"), dict) else {}
    out = {"name": srv["name"][:200], "title": _s(srv.get("title"), 200) or srv["name"].rsplit("/", 1)[-1],
           "description": _s(srv.get("description"), 1000), "version": _s(srv.get("version"), 60),
           "repository": _url(repo.get("url")), "website": _url(srv.get("websiteUrl")), "remotes": remotes, "packages": packages,
           "status": _s((official or {}).get("status"), 20) if isinstance(official, dict) else "",
           "curated": isinstance(curated, dict), "needs_oauth": bool((curated or {}).get("oauth")) if isinstance(curated, dict) else None}
    out["kinds"] = sorted({"remote" for _ in remotes} | {p["registry_type"] for p in packages if p["registry_type"]})
    out["installable"] = bool(install_options(out))
    return out


# ============================================================================ phương án cài
def suggest_name(registry_name: str) -> str:
    """`io.github.microsoft/playwright-mcp` → `playwright-mcp` (khớp NAME_RE của registry hub)."""
    base = registry_name.rsplit("/", 1)[-1] or registry_name
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", base).strip("-_")[:63] or "mcp"
    return s if s[0].isalnum() else "m" + s[:62]


def secret_name(server: str, key: str) -> str:
    """Tên secret cho trường bí mật — cùng quy ước với bộ tách secret của import (`MCP_<server>_<khoá>`)."""
    return normalize_name(f"MCP_{server}_{key}")


def _pkg_command(p: dict[str, Any]) -> tuple[str, list[str]] | None:
    ident, ver = p["identifier"], p.get("version") or ""
    if p["registry_type"] == "npm":
        return "npx", ["-y", f"{ident}@{ver}" if ver and ver != "latest" else ident]
    if p["registry_type"] == "pypi":
        return "uvx", [f"{ident}=={ver}" if ver and ver != "latest" else ident]
    return None


def install_options(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Các cách cài được hỗ trợ: remote http/sse, gói npm (`npx -y id@ver`), pypi (`uvx id==ver`) chạy stdio.
    Gói oci/nuget/mcpb hoặc gói tự chạy http → chưa hỗ trợ (hiển thị để người tự thêm tay)."""
    opts: list[dict[str, Any]] = []
    for i, r in enumerate(entry.get("remotes") or []):
        if not r.get("transport"):
            continue
        need_oauth = entry.get("needs_oauth")
        opts.append({"id": f"r{i}", "kind": "remote", "label": f"Remote {r['transport']} — {r['url']}", "transport": r["transport"], "url": r["url"],
                     "headers": r.get("headers") or [], "variables": r.get("variables") or [], "env": [], "args": [],
                     "needs_oauth": need_oauth if need_oauth is not None else None})
    for i, p in enumerate(entry.get("packages") or []):
        if p.get("registry_type") not in SUPPORTED_PACKAGES or p.get("transport") not in ("stdio", ""):
            continue
        cmd = _pkg_command(p)
        if cmd is None:
            continue
        fixed: list[str] = []
        fields: list[dict[str, Any]] = []
        for j, a in enumerate(p.get("args") or []):
            if a.get("value") and "{" not in a["value"] and not a.get("secret"):
                fixed += ([a["name"]] if a["type"] == "named" and a.get("name") else []) + [a["value"]]
            else:
                fields.append({"key": f"arg{j}", "name": a.get("name") or a.get("hint") or f"tham số {j + 1}", "named": a["type"] == "named" and a.get("name"),
                               "description": a.get("description") or "", "required": a.get("required"), "secret": a.get("secret"),
                               "default": "" if "{" in (a.get("value") or "") else a.get("value") or ""})
        opts.append({"id": f"p{i}", "kind": p["registry_type"], "label": f"{p['registry_type']} {p['identifier']}" + (f"@{p['version']}" if p.get("version") else ""),
                     "transport": "stdio", "command": cmd[0], "args": cmd[1] + fixed, "arg_fields": fields, "env": p.get("env") or [],
                     "headers": [], "variables": [], "needs_oauth": False})
    return opts


def check_oauth(option: dict[str, Any], *, timeout: float = 5.0) -> bool | None:
    """Remote KHÔNG có header bắt buộc mà `initialize` trả 401 → cần OAuth. None = không xác định (lỗi mạng, có header bắt buộc)."""
    if option.get("kind") != "remote" or option.get("needs_oauth") is not None:
        return option.get("needs_oauth")
    url = str(option.get("url") or "")
    if not url.startswith("https://") or "{" in url or _local_host(url):
        return None  # chỉ https công khai — daemon không bị dùng để gõ cửa dịch vụ cục bộ/nội bộ (dữ liệu registry là của bên thứ ba)
    if any(h.get("required") for h in option.get("headers") or []):
        return False
    key = "oauth:" + url
    hit = _cache_get(key, None, max_age=CACHE_TTL_S)
    if hit is not None:
        return bool(hit)
    try:
        code = _http_status(url, timeout)
    except (OSError, ValueError) as e:
        log.info("kiểm OAuth %s lỗi: %r", url, e)
        return None
    res = code == 401
    _cache_put(key, res, None)
    return res


def _local_host(url: str) -> bool:
    import ipaddress

    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified


# ============================================================================ truy vấn
def _offline(search: str, limit: int) -> list[dict[str, Any]]:
    q = (search or "").strip().lower()
    out = []
    for it in CURATED:
        e = normalize(it, trusted_meta=True)
        if e and (not q or q in (e["name"] + " " + e["title"] + " " + e["description"]).lower()):
            out.append(e)
    return out[:limit]


def search(query: str = "", *, limit: int = 20, cursor: str | None = None, cache_dir: Path | None = None, offline: bool = False,
           timeout: float = TIMEOUT_S) -> dict[str, Any]:
    """Tìm trong MCP Registry. Trả `{servers, next_cursor, source: registry|cache|stale-cache|offline, error?}`."""
    limit = max(1, min(int(limit or 20), LIMIT_MAX))
    q = (query or "").strip()[:200]
    if offline:
        return {"servers": _offline(q, limit), "next_cursor": None, "source": "offline", "query": q}
    params = {"limit": str(limit), "version": "latest"}
    if q:
        params["search"] = q
    if cursor:
        params["cursor"] = str(cursor)[:500]
    url = f"{REGISTRY_URL}/v0/servers?{urllib.parse.urlencode(params)}"
    hit = _cache_get(url, cache_dir, max_age=CACHE_TTL_S)
    source = "cache"
    err = None
    if hit is None:
        try:
            raw = _http_get_json(url, timeout)
            if not isinstance(raw, dict) or not isinstance(raw.get("servers"), list):
                raise ValueError("registry trả dữ liệu không đúng dạng")
            meta = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
            hit = {"servers": [e for e in (normalize(x) for x in raw["servers"]) if e], "next_cursor": meta.get("nextCursor") or None}
            _cache_put(url, hit, cache_dir)
            source = "registry"
        except (OSError, ValueError) as e:  # URLError/HTTPError/timeout là OSError
            err = f"không tải được MCP Registry: {type(e).__name__}: {e}"[:300]
            hit = _cache_get(url, cache_dir, max_age=None)
            source = "stale-cache"
            if hit is None:
                return {"servers": _offline(q, limit), "next_cursor": None, "source": "offline", "error": err, "query": q}
    out = {"servers": hit["servers"], "next_cursor": hit.get("next_cursor"), "source": source, "query": q}
    if err:
        out["error"] = err
    return out


def get(name: str, *, cache_dir: Path | None = None, offline: bool = False, timeout: float = TIMEOUT_S) -> dict[str, Any] | None:
    """Một mục theo tên registry (bản mới nhất). Ưu tiên danh sách tuyển chọn khi offline / lỗi mạng."""
    name = (name or "").strip()[:200]
    if not name:
        return None
    cur = next((e for e in (normalize(x, trusted_meta=True) for x in CURATED) if e and e["name"] == name), None)
    if offline:
        return cur
    url = f"{REGISTRY_URL}/v0/servers/{urllib.parse.quote(name, safe='')}/versions/latest"
    hit = _cache_get(url, cache_dir, max_age=CACHE_TTL_S)
    if hit is not None:
        return _merge_curated(hit, cur)
    try:
        raw = _http_get_json(url, timeout)
        e = normalize(raw) if isinstance(raw, dict) else None
        if e is not None and e["name"] != name:
            return cur  # registry trả mục khác tên → không tin
    except urllib.error.HTTPError as ex:
        if ex.code == 404:
            return cur
        e = None
    except (OSError, ValueError):
        e = None
    if e is None:
        return cur or _cache_get(url, cache_dir, max_age=None)
    _cache_put(url, e, cache_dir)
    return _merge_curated(e, cur)


def _merge_curated(e: dict[str, Any], cur: dict[str, Any] | None) -> dict[str, Any]:
    if cur and e.get("needs_oauth") is None and cur.get("needs_oauth") is not None:
        e = {**e, "needs_oauth": cur["needs_oauth"]}
        e["installable"] = bool(install_options(e))
    return e


__all__ = ["CURATED", "search", "get", "install_options", "check_oauth", "normalize", "suggest_name", "secret_name", "clear_cache"]
