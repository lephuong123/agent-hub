"""Agent bận / agent rảnh cùng loại — dùng khi giao việc cho MỘT agent cụ thể.

Trước đây giao việc (`run_start`/`task_create` của điều phối viên, `hub_run` MCP) cho agent đã chạy đủ `limits.max_concurrent_runs` thì run
lặng lẽ nằm `queued` sau run đang chạy; người giao chỉ thấy `status: queued`, không biết vì sao, cũng không biết agent cùng loại
nào đang rảnh → việc xếp hàng dù người dùng đã dặn "bận thì tạo thêm agent".

`busy_info` = agent đã hết slot chưa (cùng phép đếm với dispatcher: run đang chạy + run đang chờ trong hàng của agent).
`idle_alternatives` = agent còn slot, cùng loại (cùng gốc tên `cf-web-coder-4` ~ `web-coder`, clone của nhau), cùng role, hoặc cùng
team. `next_clone_id` = id còn trống để clone (`<gốc>-<N>`).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .hub import Hub

_SUFFIX = re.compile(r"-\d+$")


def kind_key(agent_id: str) -> str:
    s = agent_id.lower()
    s = s.removeprefix("cf-")
    return _SUFFIX.sub("", s)


def agent_limit(row: dict[str, Any]) -> int:
    try:
        return max(1, int((row.get("limits") or {}).get("max_concurrent_runs") or 1))
    except (TypeError, ValueError):
        return 1


def free_slots(hub: Hub, row: dict[str, Any]) -> int:
    return max(0, agent_limit(row) - hub.runs.active_runs(row["id"]))


def _teams_of(hub: Hub, agent_id: str) -> set[str]:
    return {r[0] for r in hub.store.conn.execute(
        "SELECT t.id FROM team_members m JOIN teams t ON t.id=m.team_id WHERE m.agent_id=? AND t.archived_at IS NULL", (agent_id,))}


def busy_info(hub: Hub, agent_id: str) -> dict[str, Any] | None:
    """None khi agent còn slot (hoặc không tồn tại / bị pause — để lỗi thật do bước sau báo). Ngược lại: các run đang giữ slot."""
    row = hub.store.get_agent(agent_id)
    if row is None or row.get("archived_at") or row.get("status") == "paused":
        return None
    limit = agent_limit(row)
    active = hub.runs.active_runs(agent_id)
    if active < limit:
        return None
    holding = []
    for st in ("running", "queued"):
        for r in hub.store.list_runs(agent_id=agent_id, status=st, limit=10):
            holding.append({"run_id": r["id"], "status": r["status"], "task_id": r.get("task_id"), "since": r.get("started_at") or r.get("queued_at")})
    return {"agent": agent_id, "active_runs": active, "max_concurrent_runs": limit, "busy_with": holding}


def idle_alternatives(hub: Hub, agent_id: str, *, exclude: set[str] | frozenset[str] = frozenset(), limit: int = 5) -> list[dict[str, Any]]:
    src = hub.store.get_agent(agent_id) or {"id": agent_id}
    key, role, root = kind_key(agent_id), (src.get("role") or "").strip().lower(), src.get("cloned_from")
    src_teams = _teams_of(hub, agent_id)
    out: list[tuple[int, dict[str, Any]]] = []
    for r in hub.store.list_agents():
        aid = r["id"]
        if aid == agent_id or aid in exclude or r.get("status") == "paused":
            continue
        slots = free_slots(hub, r)
        if slots <= 0:
            continue
        match, score = [], 0
        if kind_key(aid) == key or r.get("cloned_from") == agent_id or root == aid or (root and r.get("cloned_from") == root):
            match.append("same_kind")
            score += 4
        if role and (r.get("role") or "").strip().lower() == role:
            match.append("same_role")
            score += 2
        shared = src_teams & _teams_of(hub, aid)
        if shared:
            match.append("same_team:" + ",".join(sorted(shared)))
            score += 1
        if match:
            out.append((score, {"id": aid, "role": r.get("role"), "description": (r.get("description") or "")[:160],
                                "free_slots": slots, "match": match}))
    out.sort(key=lambda x: (-x[0], x[1]["id"]))
    return [x[1] for x in out[:limit]]


def next_clone_id(hub: Hub, agent_id: str) -> str:
    """Số sau số lớn nhất đang dùng (`cf-web-coder-4/5` → `-6`, không lấp chỗ trống `-2`), tính cả agent đã archive (id vẫn bị giữ)."""
    stem = _SUFFIX.sub("", agent_id)
    nums = [1]
    for (aid,) in hub.store.conn.execute("SELECT id FROM agents WHERE id LIKE ? ESCAPE '\\'", (stem.replace("_", "\\_").replace("%", "\\%") + "-%",)):
        tail = aid[len(stem) + 1:]
        if tail.isdigit():
            nums.append(int(tail))
    n = max(nums) + 1
    while hub.store.get_agent(f"{stem}-{n}") is not None:
        n += 1
    return f"{stem}-{n}"
