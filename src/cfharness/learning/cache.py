"""Hiệu quả prompt cache (FR-OB-04): `cache_read / (input + cache_read + cache_creation)` theo run/agent từ `runs.usage`,
và số lần hash system prompt của agent đổi trong ngày (> `learning.cache_warn_hash_changes_per_day` → cảnh báo
`cache.warning`, `learn cache`, `doctor`)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..hub import Hub


def _since(days: int) -> str:
    d = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=max(0, days - 1))
    return d.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ratio(usage: dict[str, Any] | None) -> float | None:
    u = usage or {}
    inp = int(u.get("input_tokens") or 0)
    cr = int(u.get("cache_read_input_tokens") or 0)
    cc = int(u.get("cache_creation_input_tokens") or 0)
    tot = inp + cr + cc
    return round(cr / tot, 4) if tot else None


def hash_changes_today(hub: "Hub", agent_id: str) -> int:
    """Số hash system prompt KHÁC NHAU của các session mới hôm nay, trừ 1 (lần đầu không tính là 'đổi')."""
    n = hub.store.conn.execute(
        "SELECT COUNT(DISTINCT system_prompt_hash) FROM runs WHERE agent_id=? AND queued_at>=? AND system_prompt_hash IS NOT NULL "
        "AND resumed_from_session IS NULL AND kind NOT IN ('review','eval','eval_judge')", (agent_id, _since(1))).fetchone()[0]
    return max(0, int(n or 0) - 1)


def cache_stats(hub: "Hub", agent_id: str | None = None, days: int = 1) -> dict[str, Any]:
    q = "SELECT id, agent_id, kind, usage, system_prompt_hash, resumed_from_session FROM runs WHERE queued_at>=? AND usage IS NOT NULL"
    args: list[Any] = [_since(days)]
    if agent_id:
        q += " AND agent_id=?"
        args.append(agent_id)
    import json

    agents: dict[str, dict[str, Any]] = {}
    runs = []
    for r in hub.store.conn.execute(q + " ORDER BY queued_at", args):
        u = json.loads(r["usage"]) if isinstance(r["usage"], str) else (r["usage"] or {})
        a = agents.setdefault(r["agent_id"], {"input": 0, "cache_read": 0, "cache_creation": 0, "runs": 0})
        a["input"] += int(u.get("input_tokens") or 0)
        a["cache_read"] += int(u.get("cache_read_input_tokens") or 0)
        a["cache_creation"] += int(u.get("cache_creation_input_tokens") or 0)
        a["runs"] += 1
        runs.append({"run_id": r["id"], "agent_id": r["agent_id"], "kind": r["kind"], "cache_hit": ratio(u), "prompt_hash": r["system_prompt_hash"], "resumed": bool(r["resumed_from_session"])})
    lim = int(hub.config.get("learning.cache_warn_hash_changes_per_day") or 0)
    for aid, a in agents.items():
        tot = a["input"] + a["cache_read"] + a["cache_creation"]
        a["cache_hit"] = round(a["cache_read"] / tot, 4) if tot else None
        a["prompt_hash_changes_today"] = hash_changes_today(hub, aid)
        a["warning"] = bool(lim and a["prompt_hash_changes_today"] > lim)
    return {"days": days, "agents": agents, "runs": runs[-200:]}
