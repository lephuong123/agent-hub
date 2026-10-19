"""RPC lịch chạy tự động (docs/dev-notes/schedules.md):

- `schedule.list {status?, created_by?, limit?}` → `[Schedule]`; `schedule.get {id, fires?}` → Schedule (+ `fires[]` lượt gần nhất)
  Schedule = `{id, name, kind: manager|agent|task, target, prompt, expr, describe, tz, status: active|paused|done, paused_reason, paused_by,
  deliver_to:{conversation_id?, channel?}, budget_usd, options:{team?, assignee?, workspace?}, created_by: user|manager:<agent>, last_run_at,
  last_run_id, last_task_id, last_status, next_run_at, fail_count, run_count, created_at, updated_at}`
- `schedule.create {name?, kind, target?|team?|assignee?, prompt, expr, deliver_to?, budget_usd?}` → Schedule
- `schedule.update {id, …như create}` (chỉ người) · `schedule.pause {id, reason?}` · `schedule.resume {id}` · `schedule.rm {id}` ·
  `schedule.run_now {id}` → `{id, fired, run_id?, task_id?, conversation_id?, …}`
- `schedule.preview {expr, count?}` (đọc) → `{expr, describe, next:[iso…], min_interval_s, runs_per_day}`
Ghi ∈ HUMAN_ONLY; điều phối viên qua MANAGER_ALLOWED (create|pause|resume|rm|run_now) + `ScheduleService.manager_check`. `created_by`
lấy từ người gọi (không nhận từ tham số). Tự bọc audit (`MUTATING_SCHEDULES`).
Event: `schedule.created|updated{action: edit|resume|rm}|paused|fired|skipped|failed`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..errors import HubError

if TYPE_CHECKING:
    from ..daemon import Daemon

MUTATING_SCHEDULES = ("schedule.create", "schedule.update", "schedule.pause", "schedule.resume", "schedule.rm", "schedule.run_now")


def _req(p: dict[str, Any], key: str) -> Any:
    if p.get(key) in (None, ""):
        raise HubError(f"thiếu tham số '{key}'", code=-32602)
    return p[key]


def _clean(p: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in p.items() if not k.startswith("_") and k not in ("created_by", "id")}


def preview(svc: Any, expr: str, count: int = 5) -> dict[str, Any]:
    from . import cron

    tzname = cron.local_tz_name()
    tz = cron.get_tz(tzname)
    now = svc.now()
    try:
        spec = cron.parse(expr, now=now, tz=tz)
    except cron.CronError as e:
        raise HubError(f"biểu thức lịch không hợp lệ: {e}", code=-32602) from None
    pts = list(cron.occurrences(spec, now, limit=max(1, min(int(count), 50)), tz=tz, anchor=now))
    return {"expr": spec.expr, "describe": cron.describe(spec), "tz": tzname, "next": [p.astimezone(tz).isoformat() for p in pts],
            "min_interval_s": cron.min_interval_s(spec, now=now, tz=tz), "runs_per_day": cron.runs_per_day(spec, now=now, tz=tz)}


def register_rpc(daemon: "Daemon") -> None:
    from ..mcp_server.rpc import wrap_with_audit

    s = daemon.hub.schedules  # type: ignore[attr-defined]
    methods: dict[str, Callable[[dict[str, Any]], Any]] = {
        "schedule.list": lambda p: s.list(status=p.get("status"), created_by=p.get("created_by"), limit=int(p.get("limit") or 100)),
        "schedule.get": lambda p: s.get(str(_req(p, "id")), fires=int(p.get("fires") if p.get("fires") is not None else 10)),
        "schedule.preview": lambda p: preview(s, str(_req(p, "expr")), int(p.get("count") or 5)),
        "schedule.create": lambda p: s.create(_clean(p)),
        "schedule.update": lambda p: s.update(str(_req(p, "id")), _clean(p)),
        "schedule.pause": lambda p: s.pause(str(_req(p, "id")), reason=p.get("reason")),
        "schedule.resume": lambda p: s.resume(str(_req(p, "id"))),
        "schedule.rm": lambda p: s.remove(str(_req(p, "id"))),
        "schedule.run_now": lambda p: s.run_now(str(_req(p, "id"))),
    }
    for name in MUTATING_SCHEDULES:
        methods[name] = wrap_with_audit(daemon.hub, name, methods[name])
    daemon.methods.update(methods)
