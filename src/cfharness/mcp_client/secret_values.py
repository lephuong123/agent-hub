"""Heuristic nhận diện giá trị trông như secret trong một entry `mcpServers` của claude (env / header / args / query của url).

Dùng bởi `claude_redact` (che theo giá trị). Trước v0.2.32 là `importer.py` (import `~/.claude.json` vào registry nguồn hub — đã gỡ).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
