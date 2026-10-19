"""RPC hệ thống ngoài hub:

- `system.list {}` → `[System]`, System = `{id, path, title, added_at, jobs:[{label, kind: service|scheduled|on_demand, schedule, program, locked,
  status: running|idle|paused|not_loaded|error, status_vi, pid, last_exit, disabled, paused_by?}]}`
- `system.get {id, log_bytes?}` → System (+ `jobs[].log{stdout,stderr}` đã che secret khi `log_bytes`)
- `system.scan {apply?}` (HUMAN_ONLY) → `{roots, found:[{id, path, title, jobs}], new, gone, applied, registered}`
- `system.add {path, id?}` / `system.rm {id}` / `system.lock {label, on}` (HUMAN_ONLY)
- `system.job {label, action: pause|resume|restart, reason?}` (HUMAN_ONLY; điều phối viên qua MANAGER_ALLOWED + `check_rpc`) → trạng thái job
Event: `system.job{system, label, action, ok, by, reason, detail}`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..errors import HubError

if TYPE_CHECKING:
    from ..daemon import Daemon

MUTATING_SYSTEMS = ("system.scan", "system.add", "system.rm", "system.lock", "system.job")


def _req(p: dict[str, Any], key: str) -> Any:
    if p.get(key) is None:
        raise HubError(f"thiếu tham số '{key}'", code=-32602)
    return p[key]


def _by() -> str:
    from ..auth import current_caller

    c = current_caller()
    return f"manager:{c.manager}" if c is not None and c.manager else "user"


def register_rpc(daemon: "Daemon") -> None:
    from ..mcp_server.rpc import wrap_with_audit

    s = daemon.hub.systems  # type: ignore[attr-defined]

    async def _job(p: dict[str, Any]) -> dict[str, Any]:
        return await s.job(str(_req(p, "label")), str(_req(p, "action")), by=_by(), reason=p.get("reason"))

    methods: dict[str, Callable[[dict[str, Any]], Any]] = {
        "system.list": lambda p: s.list(),
        "system.get": lambda p: s.get(str(_req(p, "id")), log_bytes=int(p.get("log_bytes") or 0) or None),
        "system.scan": lambda p: s.scan(apply=bool(p.get("apply")), by=_by()),
        "system.add": lambda p: s.add(str(_req(p, "path")), sid=p.get("id"), by=_by()),
        "system.rm": lambda p: s.remove(str(_req(p, "id"))),
        "system.lock": lambda p: s.lock(str(_req(p, "label")), bool(p.get("on", True))),
        "system.job": _job,
    }
    for name in MUTATING_SYSTEMS:
        methods[name] = wrap_with_audit(daemon.hub, name, methods[name])
    daemon.methods.update(methods)
