"""Tệp cấu hình/đăng nhập của Claude Code mà agent KHÔNG được đọc (PR1 — docs/dev-notes/plan-mcp-claude-native.md).

`~/.claude.json` giữ `mcpServers` của `claude mcp add` (header/env có thể là secret dạng rõ) + trạng thái OAuth; `.credentials.json`
giữ token đăng nhập Claude (Linux/Windows; macOS dùng keychain). Agent chạy cùng user OS nên không có quyền file nào chặn hộ → chặn ở
3 lớp, áp cho MỌI hub kể cả agent trust_full và khi `guard.sandbox.enabled=false` (như `admin_shared`/`desktop_app_data`):
- sandbox `denyRead` (lệnh Bash của agent — `guard/runtime.sandbox_settings`, `confine.confine_only_sandbox`);
- `permissions.deny` Read/Grep/Glob (`guard/runtime.claude_files_deny_rules`, cả khi trust_full);
- classifier rule `claude_credentials` (forbidden, ∈ `TRUST_NEVER_RULES` + `NEVER_DOWNGRADE_RULES`).
Tiến trình `claude` của run vẫn tự đọc được: sandbox chỉ bọc lệnh Bash của agent, `permissions.deny` chỉ áp tool của agent.

Phạm vi (hẹp có chủ ý — KHÔNG chặn `~/.claude/projects`, `~/.claude/settings.json`…):
- `~/.claude.json` và mọi bản sao cùng thư mục `~/.claude.json*` (`.bak-…`, `.backup…`);
- `<thư mục cấu hình>/.credentials.json`, `<thư mục cấu hình>/.claude.json*`, `<thư mục cấu hình>/backups/` (Claude Code chép
  `.claude.json.backup.<ts>` vào đây) — thư mục cấu hình = `~/.claude` và `$CLAUDE_CONFIG_DIR` của daemon (nếu đặt);
- `HUB_HOME/claude-config/` (thư mục cấu hình theo tài khoản — vô hại nếu chưa có).
Hàm THUẦN theo `home`/`env`/`hub_home` (test truyền giá trị giả).
"""

from __future__ import annotations

import fnmatch
import functools
import os
from pathlib import Path

RULE = "claude_credentials"
REASON = "agent không được đọc cấu hình/đăng nhập của Claude Code (~/.claude.json, .credentials.json — có thể chứa secret MCP, token OAuth)"
JSON_NAME = ".claude.json"
CRED_NAME = ".credentials.json"
HUB_DIR = "claude-config"


def _home(home: str | Path | None) -> str:
    return str(home if home is not None else os.path.expanduser("~")).replace("\\", "/").rstrip("/")


def config_dirs(home: str | Path | None = None, env: dict[str, str] | None = None) -> list[str]:
    """Thư mục cấu hình Claude Code: `~/.claude` + `$CLAUDE_CONFIG_DIR` (chỉ khi là đường dẫn tuyệt đối)."""
    e = os.environ if env is None else env
    out = [f"{_home(home)}/.claude"]
    ccd = (e.get("CLAUDE_CONFIG_DIR") or "").strip()
    if ccd:
        ccd = os.path.expanduser(ccd).replace("\\", "/").rstrip("/")
        if ccd.startswith("/") or (len(ccd) > 2 and ccd[1] == ":"):
            out.append(ccd)
    return list(dict.fromkeys(out))


def json_parents(home: str | Path | None = None, env: dict[str, str] | None = None) -> list[str]:
    """Thư mục mà MỌI tệp tên `.claude.json*` trong đó đều bị chặn (bản chính + bản sao lưu cạnh nó)."""
    return list(dict.fromkeys([_home(home), *config_dirs(home, env)]))


def secret_files(home: str | Path | None = None, env: dict[str, str] | None = None) -> list[str]:
    out = [f"{p}/{JSON_NAME}" for p in json_parents(home, env)]
    out += [f"{d}/{CRED_NAME}" for d in config_dirs(home, env)]
    return list(dict.fromkeys(out))


def secret_dirs(home: str | Path | None = None, env: dict[str, str] | None = None, hub_home: str | Path | None = None) -> list[str]:
    out = [f"{d}/backups" for d in config_dirs(home, env)]
    if hub_home is not None:
        out.append(f"{str(hub_home).replace(chr(92), '/').rstrip('/')}/{HUB_DIR}")
    return list(dict.fromkeys(out))


def _canon(p: str) -> str:
    return p.replace("\\", "/").rstrip("/").casefold()


def _with_real(paths: list[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        out.append(p)
        try:
            out.append(os.path.realpath(p).replace("\\", "/"))
        except (OSError, ValueError):
            pass
    return list(dict.fromkeys(out))


@functools.lru_cache(maxsize=64)
def _roots(home: str, ccd: str, hub_home: str | None) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """(tệp, thư mục, cha của `.claude.json*`) đã chuẩn hoá + realpath — classifier gọi cho MỌI đối số Bash nên giữ đệm."""
    env = {"CLAUDE_CONFIG_DIR": ccd} if ccd else {}
    files = frozenset(_canon(f) for f in _with_real(secret_files(home, env)))
    dirs = frozenset(_canon(d) for d in _with_real(secret_dirs(home, env, hub_home)))
    parents = frozenset(_canon(p) for p in _with_real(json_parents(home, env)))
    return files, dirs, parents


def is_secret(path: str, home: str | Path | None = None, env: dict[str, str] | None = None, hub_home: str | Path | None = None) -> bool:
    """`path` (tuyệt đối) là/ nằm trong một tệp/thư mục bị chặn. Không phân biệt hoa/thường (APFS/NTFS), xét cả realpath."""
    if not path:
        return False
    cands = {_canon(path)}
    try:
        cands.add(_canon(os.path.realpath(path)))
    except (OSError, ValueError):
        pass
    e = os.environ if env is None else env
    files, dirs, parents = _roots(_home(home), e.get("CLAUDE_CONFIG_DIR") or "", None if hub_home is None else str(hub_home))
    jn = JSON_NAME.casefold()
    for c in cands:
        if c in files or any(c == d or c.startswith(d + "/") for d in dirs):
            return True
        par, _, base = c.rpartition("/")
        if base.startswith(jn) and par in parents:
            return True
    return False


def glob_may_touch(pattern: str, home: str | Path | None = None, env: dict[str, str] | None = None,
                   hub_home: str | Path | None = None) -> bool:
    """Mẫu glob TUYỆT ĐỐI có thể khớp một tệp bị chặn (vd Grep `glob` = `.claude.json*` trên `~`). `{…}` coi như `*`."""
    import re

    pat = re.sub(r"\{[^{}]*\}", "*", _canon(pattern))
    probes = [_canon(f) for f in secret_files(home, env)]
    probes += [f"{_canon(p)}/{JSON_NAME}.backup" for p in json_parents(home, env)]
    probes += [f"{_canon(d)}/x" for d in secret_dirs(home, env, hub_home)]
    return any(fnmatch.fnmatchcase(p, pat) for p in probes)


def deny_read_paths(hub_home: str | Path | None = None, home: str | Path | None = None, env: dict[str, str] | None = None) -> list[str]:
    """Đường dẫn cho sandbox `denyRead`: tệp + thư mục cố định, mẫu `<cha>/.claude.json*`, và các bản `.claude.json*` đang có
    (liệt kê lúc dựng settings — phòng khi sandbox nền tảng không hiểu glob)."""
    out = list(secret_files(home, env)) + list(secret_dirs(home, env, hub_home))
    for p in json_parents(home, env):
        out.append(f"{p}/{JSON_NAME}*")
        try:
            out += [f"{p}/{n}" for n in sorted(os.listdir(p)) if n.casefold().startswith(JSON_NAME.casefold())]
        except OSError:
            pass
    return list(dict.fromkeys(_with_real(out)))


__all__ = ["CRED_NAME", "HUB_DIR", "JSON_NAME", "REASON", "RULE", "config_dirs", "deny_read_paths", "glob_may_touch", "is_secret",
           "json_parents", "secret_dirs", "secret_files"]
