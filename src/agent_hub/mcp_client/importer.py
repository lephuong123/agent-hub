"""Import MCP server từ `~/.claude.json` (FR-MC-04). Tham khảo Hermes `hermes_cli/agent_import.py`.

- CHỈ ĐỌC file nguồn (mở chế độ đọc; không bao giờ ghi lại).
- Đọc `mcpServers` cấp user; tuỳ chọn `projects[<đường dẫn tuyệt đối>].mcpServers` (`--project`).
- Giá trị trông như secret trong env / header / args / query của url được tách ra secret store,
  registry chỉ giữ `${NAME}` (FR-MC-05). Giá trị đã là `${VAR}` giữ nguyên.
- MCP import mặc định `trust: trusted` (Q-05) — tool ghi chạy không hỏi nhưng có audit.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..errors import HubError
from ..secrets import is_ref, normalize_name

SECRET_KEY_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|AUTH|CREDENTIAL|COOKIE|SESSION|PRIVATE)", re.I)
AUTH_PREFIX_RE = re.compile(r"^(Bearer|Basic|Token|Bot)\s+(\S.*)$", re.I)
_PATHLIKE = re.compile(r"^(/|~|\./|\.\./|[A-Za-z]:\\)|://|^[\w.-]+@[\w.-]+$")


def entropy_bits(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def looks_secret_value(v: str) -> bool:
    """Chuỗi entropy cao, không khoảng trắng, không phải đường dẫn/URL → coi là secret."""
    if not isinstance(v, str) or len(v) < 20 or " " in v or is_ref(v) or _PATHLIKE.search(v):
        return False
    classes = sum(bool(re.search(p, v)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]"))
    return entropy_bits(v) >= 3.5 and classes >= 2


def read_claude_servers(path: Path, project: str | None = None) -> dict[str, dict[str, Any]]:
    """Đọc `mcpServers` (user + project nếu có). CHỈ ĐỌC."""
    if not path.is_file():
        raise HubError(f"không tìm thấy {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise HubError(f"{path} không phải JSON hợp lệ: {e}") from None
    if not isinstance(data, dict):
        raise HubError(f"{path}: nội dung không phải object JSON")
    servers: dict[str, dict[str, Any]] = {}
    user = data.get("mcpServers")
    if isinstance(user, dict):
        servers.update({k: v for k, v in user.items() if isinstance(v, dict)})
    if project:
        projects = data.get("projects") or {}
        key = str(Path(project).expanduser().resolve())
        proj = projects.get(key) or projects.get(project) or {}
        ps = proj.get("mcpServers") if isinstance(proj, dict) else None
        if isinstance(ps, dict):
            servers.update({k: v for k, v in ps.items() if isinstance(v, dict)})
    return servers


class _Extractor:
    def __init__(self, server: str) -> None:
        self.server = server
        self.secrets: dict[str, str] = {}

    def _name(self, key: str) -> str:
        base = normalize_name(f"MCP_{self.server}_{key}")
        n, i = base, 2
        while n in self.secrets:
            n, i = f"{base}_{i}", i + 1
        return n

    def take(self, key: str, value: str) -> str:
        name = self._name(key)
        self.secrets[name] = value
        return f"${{{name}}}"

    def kv(self, key: str, value: Any) -> Any:
        if not isinstance(value, str) or value == "" or is_ref(value) or "${" in value:
            return value
        m = AUTH_PREFIX_RE.match(value)
        if m and (SECRET_KEY_RE.search(key) or looks_secret_value(m.group(2))):
            return f"{m.group(1)} {self.take(key, m.group(2))}"
        if SECRET_KEY_RE.search(key) or looks_secret_value(value):
            return self.take(key, value)
        return value

    def args(self, args: list[Any]) -> list[Any]:
        out: list[Any] = []
        hide_next_key: str | None = None
        for a in args:
            if not isinstance(a, str):
                out.append(a)
                continue
            if hide_next_key is not None:
                key, hide_next_key = hide_next_key, None
                if not a.startswith("-"):
                    out.append(a if is_ref(a) or "${" in a else self.take(key, a))
                    continue
            if a.startswith("-") and "=" in a:
                flag, val = a.split("=", 1)
                key = flag.lstrip("-")
                if SECRET_KEY_RE.search(key) or looks_secret_value(val):
                    out.append(f"{flag}={self.take(key, val)}" if not is_ref(val) else a)
                    continue
                out.append(a)
                continue
            if a.startswith("-") and SECRET_KEY_RE.search(a.lstrip("-")):
                out.append(a)
                hide_next_key = a.lstrip("-")
                continue
            # dạng KEY=VALUE (vd `-e TOKEN=abc` của docker)
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", a):
                k, v = a.split("=", 1)
                out.append(f"{k}={self.kv(k, v)}")
                continue
            if looks_secret_value(a):
                out.append(self.take(f"ARG{len(out)}", a))
                continue
            out.append(a)
        return out

    def url(self, url: str) -> str:
        parts = urlsplit(url)
        q = parse_qsl(parts.query, keep_blank_values=True)
        if not q:
            return url
        changed = False
        nq = []
        for k, v in q:
            nv = self.kv(k, v)
            changed |= nv != v
            nq.append((k, nv))
        if not changed:
            return url
        # giữ `${…}` không bị mã hoá
        query = urlencode(nq, safe="${}")
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def convert_entry(name: str, entry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Entry Claude Code → (cấu hình registry, secrets tách ra, ghi chú)."""
    notes: list[str] = []
    ex = _Extractor(name)
    typ = str(entry.get("type") or ("stdio" if entry.get("command") else "http")).lower()
    if typ in ("streamable-http", "streamable_http"):
        typ = "http"
    cfg: dict[str, Any] = {"name": name, "trust": "trusted", "source": "claude-import"}
    if typ == "stdio":
        cfg.update(
            transport="stdio",
            command=entry.get("command"),
            args=ex.args(list(entry.get("args") or [])),
            env={k: ex.kv(k, v) for k, v in (entry.get("env") or {}).items()},
        )
    elif typ in ("http", "sse"):
        cfg.update(
            transport=typ,
            url=ex.url(str(entry.get("url") or "")),
            headers={k: ex.kv(k, v) for k, v in (entry.get("headers") or {}).items()},
        )
    else:
        raise HubError(f"server '{name}': loại '{typ}' không hỗ trợ")
    ignored = sorted(set(entry) - {"type", "command", "args", "env", "url", "headers"})
    if ignored:
        notes.append(f"bỏ qua trường không hỗ trợ: {', '.join(ignored)}")
    if ex.secrets:
        notes.append(f"{len(ex.secrets)} secret chuyển vào secret store: {', '.join(sorted(ex.secrets))}")
    return cfg, ex.secrets, notes
