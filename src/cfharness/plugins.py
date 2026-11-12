"""Plugin Claude Code cho run của agent (trường `plugins` của agent) — docs/dev-notes/agent-plugins.md.

Run của agent chạy cô lập (`--setting-sources ""`), nên `enabledPlugins` trong `~/.claude/settings.json` KHÔNG được nạp.
Hub nạp từng plugin người khai báo cho agent bằng `--plugin-dir <installPath>`. Đã kiểm với `claude` 2.1.322 (Windows):
`--setting-sources "" --strict-mcp-config --plugin-dir <dir>` → system/init có plugin (source `<tên>@inline`), skill
`<tên>:*`, lệnh, subagent và hook của plugin; MCP server của plugin (`.mcp.json`) KHÔNG được nạp vì `--strict-mcp-config`.

Phân giải tên → thư mục: `~/.claude/plugins/installed_plugins.json` (CLI ghi khi `claude plugin install`).
- `tên@marketplace` → đúng mục đó; `tên` → mục duy nhất có tên đó (nhiều marketplace trùng tên → lỗi, phải ghi rõ).
- Đường dẫn tuyệt đối tới thư mục có `.claude-plugin/plugin.json` → dùng nguyên (plugin tự viết, chưa cài).
Plugin chưa cài / không tìm thấy → `PluginError` (run không chạy) thay vì âm thầm chạy thiếu plugin.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SPEC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}(@[A-Za-z0-9][A-Za-z0-9._-]{0,127})?$")


class PluginError(ValueError):
    pass


@dataclass
class ResolvedPlugin:
    spec: str
    name: str
    path: Path
    hook_events: list[str] = field(default_factory=list)


def valid_spec(spec: str) -> bool:
    return bool(SPEC_RE.match(spec)) or _is_abs(spec)


def _is_abs(spec: str) -> bool:
    return os.path.isabs(spec) or bool(re.match(r"^[A-Za-z]:[\\/]", spec))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def claude_dir(home: Path | None = None) -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env and home is None:
        return Path(env)
    return (home or Path(os.path.expanduser("~"))) / ".claude"


def installed(home: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """`{tên@marketplace: [mục cài]}` từ installed_plugins.json (v2; v1 là `{key: mục}`)."""
    d = _read_json(claude_dir(home) / "plugins" / "installed_plugins.json")
    raw = d.get("plugins") if isinstance(d.get("plugins"), dict) else {}
    out: dict[str, list[dict[str, Any]]] = {}
    for k, v in raw.items():
        items = v if isinstance(v, list) else [v]
        out[str(k)] = [x for x in items if isinstance(x, dict) and x.get("installPath")]
    return out


def _manifest_name(path: Path) -> str | None:
    m = _read_json(path / ".claude-plugin" / "plugin.json")
    n = m.get("name")
    return str(n) if isinstance(n, str) and n else None


def _hook_events(path: Path) -> list[str]:
    """Sự kiện hook của plugin (hooks/hooks.json, hoặc `hooks` trong plugin.json) — để kiểm cô lập hậu nghiệm coi là đã biết."""
    events: set[str] = set()
    for src in (_read_json(path / "hooks" / "hooks.json"), _read_json(path / ".claude-plugin" / "plugin.json")):
        h = src.get("hooks")
        if isinstance(h, dict):
            hooks = h.get("hooks") if isinstance(h.get("hooks"), dict) else h
            events |= {str(k) for k in hooks}
    return sorted(events)


def _pick(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Nhiều mục cài (scope user/project…): ưu tiên `user`, rồi mục cập nhật mới nhất."""
    users = [e for e in entries if e.get("scope") == "user"] or entries
    return sorted(users, key=lambda e: str(e.get("lastUpdated") or e.get("installedAt") or ""))[-1]


def resolve(specs: list[str], home: Path | None = None) -> list[ResolvedPlugin]:
    out: list[ResolvedPlugin] = []
    inst: dict[str, list[dict[str, Any]]] | None = None
    for spec in specs:
        spec = str(spec).strip()
        if not spec:
            continue
        if _is_abs(spec):
            p = Path(spec)
            name = _manifest_name(p)
            if not name:
                raise PluginError(f"plugin '{spec}': không có .claude-plugin/plugin.json hợp lệ")
            out.append(ResolvedPlugin(spec, name, p, _hook_events(p)))
            continue
        if not SPEC_RE.match(spec):
            raise PluginError(f"plugin '{spec}': tên không hợp lệ (dùng tên@marketplace, tên, hoặc đường dẫn tuyệt đối)")
        if inst is None:
            inst = installed(home)
        if "@" in spec:
            keys = [spec] if spec in inst else []
        else:
            keys = [k for k in inst if k.split("@", 1)[0] == spec]
        if not keys or not inst.get(keys[0]):
            raise PluginError(f"plugin '{spec}' chưa được cài cho Claude Code (cài bằng `claude plugin install {spec}`)")
        if len(keys) > 1:
            raise PluginError(f"plugin '{spec}' có ở nhiều marketplace ({', '.join(sorted(keys))}) — ghi rõ tên@marketplace")
        p = Path(str(_pick(inst[keys[0]])["installPath"]))
        if not p.is_dir():
            raise PluginError(f"plugin '{spec}': thư mục cài không còn ({p}) — cài lại bằng `claude plugin install {keys[0]}`")
        out.append(ResolvedPlugin(spec, _manifest_name(p) or keys[0].split("@", 1)[0], p, _hook_events(p)))
    return out
