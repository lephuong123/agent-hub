"""Rào chắn chi phí còn hở (docs/dev-notes/cost-guardrails.md) — hàm dùng chung cho `runs.py`, dispatcher, schedules.

- `manager_budget_scope`: trần ngày RIÊNG của điều phối viên (`manager.budget_usd_per_day`, nhánh `manager` được kiểm toàn vẹn,
  `config.set` ∈ HUMAN_ONLY). Thêm một scope `manager` vào `RunManager.budget_scopes` — áp CÙNG với `limits.budget_usd_per_day`
  của agent (nếu có), không thay thế.
- `flag_once`: cờ "một lần/ngày" cho `budget.warning`/`budget.exceeded` (khoá `hub_state.budget_flags`, dùng chung với dispatcher).
- `estimated_run_cost`: chi phí ƯỚC LƯỢNG của run kết thúc KHÔNG có `result` (crashed, bị giết khi quá giờ…) từ usage luỹ kế của
  adapter (cùng công thức `usage.tick` estimated). Không có thì ngân sách chuỗi/ngày tính thiếu phần đã tiêu.
- `interrupted_streak`: số run `interrupted` liên tiếp đi tới run này theo `parent_run_id` (trần requeue của dispatcher).
- `schedule_fire_cost`: chi phí thật trung bình của các lượt gần đây của một lịch (ước tính $/ngày; chưa đủ lịch sử → None).

Bất biến: hết ngân sách chỉ chặn run MỚI (submit/claim), không đụng run đang chạy hay approval đang chờ (Q-04).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .hub import Hub

COST_BASIS_ESTIMATED = "estimated_no_result"


def flag_once(store: Any, key: str) -> bool:
    """True đúng một lần mỗi ngày (giờ máy) cho mỗi `key` (vd `warn:manager:mgr`, `exceeded:hub:hub`)."""
    day = datetime.now().strftime("%Y-%m-%d")
    flags = dict(store.state_get("budget_flags", {}) or {})
    if flags.get(key) == day:
        return False
    flags[key] = day
    store.state_set("budget_flags", flags)
    return True


def manager_budget_scope(hub: "Hub", agent_id: str) -> tuple[str, str, float, float] | None:
    """(`manager`, agent, đã dùng hôm nay, trần) khi `agent_id` là điều phối viên đang được chỉ định và có trần riêng."""
    lim = hub.config.get("manager.budget_usd_per_day")
    if not lim:
        return None
    mgr = getattr(hub, "manager", None)
    if mgr is None or not mgr.is_manager(agent_id):
        return None
    return ("manager", agent_id, hub.store.cost_today(agent_id), float(lim))


def estimated_run_cost(handle: Any) -> float | None:
    """Ước lượng $ đã tiêu của tiến trình (theo token stream_event đã thấy). None khi không có số nào."""
    ad = getattr(handle, "adapter", None)
    fn = getattr(ad, "estimated_cost_usd", None)
    if fn is None:
        return None
    try:
        v = float(fn())
    except Exception:  # pragma: no cover - không bao giờ làm hỏng _finalize
        return None
    return round(v, 6) if v > 0 else None


def interrupted_streak(store: Any, run: dict[str, Any], limit: int = 100) -> int:
    """Số run `interrupted` LIÊN TIẾP (theo chuỗi `parent_run_id`, gồm cả `run`) — dừng ở run đầu tiên có kết quả khác."""
    n, cur = 0, run
    for _ in range(limit):
        if (cur.get("outcome") or cur.get("status")) != "interrupted":
            break
        n += 1
        pid = cur.get("parent_run_id")
        if not pid:
            break
        cur = store.get_run(pid) or {}
    return n


def schedule_fire_cost(conn: Any, schedule_id: str | None, *, recent: int, min_fires: int) -> float | None:
    """Trung bình `cost_usd` của ≤ `recent` lượt đã xong gần nhất; None khi < `min_fires` lượt có chi phí."""
    if not schedule_id or recent <= 0:
        return None
    rows = conn.execute(
        "SELECT cost_usd FROM schedule_fires WHERE schedule_id=? AND status IN ('completed','failed') AND cost_usd IS NOT NULL "
        "ORDER BY id DESC LIMIT ?", (schedule_id, int(recent))).fetchall()
    vals = [float(r[0]) for r in rows if r[0] is not None]
    if len(vals) < max(1, int(min_fires)):
        return None
    return sum(vals) / len(vals)
