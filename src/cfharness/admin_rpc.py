"""RPC `admin.*` của quản trị máy (plan-admin-users P1/P2; chi tiết `docs/dev-notes/admin-users.md`).

MỌI method ∈ `auth.HUMAN_ONLY` (không vào `MANAGER_ALLOWED`). Method phía ADMIN kiểm thêm `admin_control.require_admin`
(nhóm `cfharness-admin`): `admin.overview` (admin_overview.py), `admin.users`, `admin.user`, `admin.estop_list`, `admin.estop_set`,
`admin.alerts`, `admin.audit`, `admin.policy` (user khác). Phía USER (minh bạch): `admin.nav`, `admin.self`, `admin.setup_check`.
Ghi duy nhất: `admin.estop_set` (bật/gỡ cờ ESTOP admin — tự audit, ghi admin-log, đánh giá cảnh báo ngay).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .errors import HubError

if TYPE_CHECKING:
    from .daemon import Daemon

MUTATING_ADMIN = ("admin.estop_set",)
READ_ADMIN = ("admin.overview", "admin.users", "admin.user", "admin.estop_list", "admin.alerts", "admin.audit", "admin.policy",
              "admin.nav", "admin.self", "admin.setup_check")


def register_rpc(daemon: "Daemon") -> None:
    from . import admin_control as ac
    from .admin_overview import USER_RE, os_user, overview
    from .mcp_server.rpc import wrap_with_audit

    hub = daemon.hub

    def _user(p: dict[str, Any], key: str = "user", required: bool = True) -> str | None:
        u = p.get(key)
        if u is None and not required:
            return None
        if not isinstance(u, str) or not USER_RE.match(u):
            raise HubError(f"'{key}' không hợp lệ", code=-32602)
        return u

    def _status_dir() -> str:
        from .admin_overview import DEFAULT_DIR

        return str((hub.config.get("admin_export") or {}).get("dir") or DEFAULT_DIR)

    async def nav(p: dict[str, Any]) -> dict[str, Any]:
        adm = await asyncio.to_thread(ac.is_admin)
        managed = ac.shared_root().is_dir() or Path(_status_dir()).expanduser().is_dir()
        return {"is_admin": adm, "managed": managed, "admin_estop": dict(hub.store.state_get("admin_estop") or {"on": False})}

    async def self_view(p: dict[str, Any]) -> dict[str, Any]:
        """Trang minh bạch phía user: CHÍNH XÁC những gì hub này xuất cho admin + chính sách + ESTOP admin + admin-log của mình."""
        from .admin_overview import collect
        from .admin_policy import current

        exp = getattr(hub, "admin_export", None)
        me = os_user()
        preview = collect(hub, interval_s=exp.interval_s if exp else 60)
        audit_on = getattr(hub, "admin_audit", None)
        return {"user": me, "managed": ac.shared_root().is_dir(), "is_admin": await asyncio.to_thread(ac.is_admin),
                "export": {**(exp.status() if exp else {}), "preview": preview},
                "audit": {"enabled": bool(audit_on and audit_on._dir() is not None), "written": getattr(audit_on, "written", 0)},
                "policy": current(hub), "admin_estop": dict(hub.store.state_get("admin_estop") or {"on": False}),
                "admin_log": await asyncio.to_thread(ac.admin_log_read, me, limit=100)}

    async def users(p: dict[str, Any]) -> dict[str, Any]:
        from .admin_users import list_users

        ac.require_admin("admin.users")
        ov = await asyncio.to_thread(overview, hub)
        rows = await asyncio.to_thread(list_users, ov)
        w = getattr(hub, "admin_watch", None)
        act = list((w.active if w else {}).values())
        return {"dir": ov.get("dir"), "exists": ov.get("exists"), "users": rows, "totals": ov.get("totals"), "alerts": act,
                "flags": await asyncio.to_thread(ac.list_flags)}

    async def user(p: dict[str, Any]) -> dict[str, Any]:
        from .admin_audit import read_audit
        from .admin_policy import effective
        from .admin_users import list_users

        ac.require_admin("admin.user")
        u = _user(p)
        ov = await asyncio.to_thread(overview, hub)
        row = next((r for r in await asyncio.to_thread(list_users, ov) if r["user"] == u), None)
        if row is None:
            raise HubError(f"không có user '{u}' trong nhóm/thư mục trạng thái", code=-32004)
        w = getattr(hub, "admin_watch", None)
        days = int(p.get("days") or 30)
        return {"user": u, "row": row, "hubs": [h for h in ov.get("hubs") or [] if h.get("user") == u],
                "alerts": [a for a in (w.active if w else {}).values() if a.get("user") == u],
                "history": w.history(u, days=days) if w else None,
                "policy": await asyncio.to_thread(effective, u),
                "estop": await asyncio.to_thread(ac.read_flags, u),
                "admin_log": await asyncio.to_thread(ac.admin_log_read, u, limit=50),
                "audit": await asyncio.to_thread(read_audit, Path(_status_dir()), user=u, days=7, limit=200)}

    async def estop_list(p: dict[str, Any]) -> dict[str, Any]:
        ac.require_admin("admin.estop_list")
        return {"flags": await asyncio.to_thread(ac.list_flags), "dir": str(ac.control_dir())}

    async def estop_set(p: dict[str, Any]) -> dict[str, Any]:
        ac.require_admin("admin.estop_set")
        on = bool(p.get("on"))
        target = None if p.get("all") or p.get("user") in (None, "", "*") else _user(p)
        code = str(p.get("reason_code") or "other")
        res = await asyncio.to_thread(ac.set_flag, on, target, reason_code=code)
        w = getattr(hub, "admin_watch", None)
        if w is not None:
            try:
                w.evaluate()  # cảnh báo admin_estop_on bật / báo hồi phục (gỡ) ngay, không chờ nhịp
            except Exception:  # pragma: no cover
                pass
        ctl = getattr(hub, "admin_control", None)
        if ctl is not None:  # hub admin cũng là một hub user (cờ `estop` mọi user / cờ riêng của chính admin)
            ctl.check()
        return res

    async def alerts(p: dict[str, Any]) -> dict[str, Any]:
        ac.require_admin("admin.alerts")
        w = getattr(hub, "admin_watch", None)
        rows = hub.store.conn.execute("SELECT ts, payload FROM events WHERE type='admin.alert' ORDER BY ts DESC LIMIT ?",
                                      (max(1, min(int(p.get("limit") or 200), 2000)),)).fetchall()
        import json as _json

        recent = []
        for ts, payload in rows:
            try:
                d = _json.loads(payload)
            except (TypeError, ValueError):
                continue
            recent.append({"ts": ts, **{k: d.get(k) for k in ("user", "hub_id", "code", "state")}})
        return {"active": list((w.active if w else {}).values()), "recent": recent, "last_tick": getattr(w, "last_tick", None)}

    async def audit(p: dict[str, Any]) -> dict[str, Any]:
        from .admin_audit import read_audit

        ac.require_admin("admin.audit")
        u = _user(p, required=False)
        return await asyncio.to_thread(read_audit, Path(_status_dir()), user=u, days=int(p.get("days") or 7), limit=int(p.get("limit") or 500))

    async def policy(p: dict[str, Any]) -> dict[str, Any]:
        from .admin_policy import effective

        u = _user(p, required=False) or os_user()
        if u != os_user():
            ac.require_admin("admin.policy (user khác)")
        return await asyncio.to_thread(effective, u)

    async def setup_check(p: dict[str, Any]) -> dict[str, Any]:
        from .admin_users import setup_check as _check

        return await asyncio.to_thread(_check)

    methods: dict[str, Callable[[dict[str, Any]], Any]] = {
        "admin.nav": nav, "admin.self": self_view, "admin.users": users, "admin.user": user, "admin.estop_list": estop_list,
        "admin.estop_set": estop_set, "admin.alerts": alerts, "admin.audit": audit, "admin.policy": policy, "admin.setup_check": setup_check,
    }
    for m in MUTATING_ADMIN:
        methods[m] = wrap_with_audit(hub, m, methods[m])
    daemon.methods.update(methods)

    # `estop.set` của user: ESTOP admin vẫn bật thì kết quả nói rõ (user không gỡ được cờ admin)
    orig = daemon.methods.get("estop.set")
    if orig is not None:
        async def estop_set_user(p: dict[str, Any]) -> Any:
            res = orig(p)
            if asyncio.iscoroutine(res):
                res = await res
            adm = hub.store.state_get("admin_estop") or {}
            if isinstance(res, dict) and adm.get("on"):
                res = {**res, "admin_estop": adm, "note": "ESTOP quản trị máy vẫn bật — chỉ người quản trị máy gỡ được"}
            return res

        daemon.methods["estop.set"] = estop_set_user


__all__ = ["MUTATING_ADMIN", "READ_ADMIN", "register_rpc"]
