"""Template agent có sẵn (FR-AG-07): researcher, coder, reviewer, ops.

Mỗi template là một file YAML đóng gói kèm package: `title` (mô tả tiếng Việt cho người), `config` (theo schema
`AgentConfig`, không có `id`/`name`) và `soul` (SOUL.md tiếng Anh, có chỗ `{name}`/`{role}`).
Dùng: `cfharness agent create <id> --template reviewer` (RPC `agent.create_from_template`), `cfharness template list|show`.
Ghi đè trường bất kỳ của template bằng các cờ của `agent create` (vd `--model haiku`).
"""

from __future__ import annotations

import copy
from importlib import resources
from typing import Any

import yaml

from ..errors import NotFound


def _dir():
    return resources.files(__name__)


def names() -> list[str]:
    return sorted(p.name[:-5] for p in _dir().iterdir() if p.name.endswith(".yaml"))


def load(name: str) -> dict[str, Any]:
    f = _dir() / f"{name}.yaml"
    if not f.is_file():
        raise NotFound(f"không có template '{name}' (có: {', '.join(names())})")
    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    data["name"] = name
    return data


def list_templates() -> list[dict[str, Any]]:
    out = []
    for n in names():
        t = load(n)
        c = t.get("config") or {}
        tp = c.get("tool_policy") or {}
        out.append(
            {
                "name": n,
                "title": t.get("title", ""),
                "role": c.get("role", ""),
                "model": c.get("model"),
                "permission_mode": tp.get("permission_mode"),
                "tools": tp.get("tools"),
            }
        )
    return out


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif v is not None:
            out[k] = copy.deepcopy(v)
    return out


def render(name: str, agent_id: str, overrides: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    """(config đầy đủ cho `AgentService.create`, SOUL.md) từ template + ghi đè."""
    t = load(name)
    cfg = _merge(t.get("config") or {}, overrides or {})
    cfg["id"] = agent_id
    cfg.setdefault("name", agent_id)
    soul = str(t.get("soul") or "").format(name=cfg["name"], role=cfg.get("role") or "general assistant")
    return cfg, soul
