"""RPC tài khoản Claude (hợp đồng: docs/dev-notes/plan-claude-accounts.md):

- `account.list {}` → `{active, accounts: [Account]}` (đọc)
- `account.login_start {label}` → `{login_id, url, expires_at}` · `account.login_finish {login_id, code}` → `{account}` ·
  `account.login_cancel {login_id}` → `{ok}`
- `account.add_token {label, token}` → `{account}` · `account.use {id}` → `{active, previous}` · `account.rename {id, label}` → `{account}` ·
  `account.rm {id}` → `{ok}` (đang dùng → Conflict; `machine` không xoá được) · `account.check {id}` → `{account}`
Account = `{id, label, kind: token|machine|custom, active, created_at, expires_at, last_used_at, status: ok|auth_error|rate_limited|expiring|unknown,
last_error, rate_limit: {until, utilization}|null, cost_today_usd}` — KHÔNG BAO GIỜ có token.
Ghi ∈ HUMAN_ONLY (không ở MANAGER_ALLOWED: đổi tài khoản chỉ do NGƯỜI). Tự bọc audit (`MUTATING_ACCOUNTS`); `token`/`code` bị gỡ khỏi
params TRƯỚC khi audit đọc.
Event: `account.added|removed|renamed|switched`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from ..errors import HubError

if TYPE_CHECKING:
    from ..daemon import Daemon

MUTATING_ACCOUNTS = ("account.login_start", "account.login_finish", "account.login_cancel", "account.add_token", "account.use",
                     "account.rename", "account.rm", "account.check")


def _req(p: dict[str, Any], key: str) -> Any:
    if p.get(key) in (None, ""):
        raise HubError(f"thiếu tham số '{key}'", code=-32602)
    return p[key]


def _take(p: dict[str, Any], key: str) -> Any:
    """Lấy và GỠ tham số bí mật khỏi params (wrap_with_audit ghi params còn lại vào audit append-only)."""
    v = p.pop(key, None)
    if v in (None, ""):
        raise HubError(f"thiếu tham số '{key}'", code=-32602)
    return v


def register_rpc(daemon: "Daemon") -> None:
    from ..mcp_server.rpc import wrap_with_audit

    s = daemon.hub.accounts  # type: ignore[attr-defined]

    def _add(p: dict[str, Any]) -> dict[str, Any]:
        token = _take(p, "token")
        return {"account": s.add_token(_req(p, "label"), token)}

    async def _finish(p: dict[str, Any]) -> dict[str, Any]:
        code = _take(p, "code")
        return await s.login_finish(str(_req(p, "login_id")), code)

    async def _check(p: dict[str, Any]) -> dict[str, Any]:
        return {"account": await s.check(str(_req(p, "id")))}

    methods: dict[str, Callable[[dict[str, Any]], Any]] = {
        "account.list": lambda p: s.list(),
        "account.login_start": lambda p: s.login_start(_req(p, "label")),
        "account.login_finish": _finish,
        "account.login_cancel": lambda p: s.login_cancel(str(_req(p, "login_id"))),
        "account.add_token": _add,
        "account.use": lambda p: s.use(str(_req(p, "id"))),
        "account.rename": lambda p: {"account": s.rename(str(_req(p, "id")), _req(p, "label"))},
        "account.rm": lambda p: s.remove(str(_req(p, "id"))),
        "account.check": _check,
    }
    for name in MUTATING_ACCOUNTS:
        methods[name] = wrap_with_audit(daemon.hub, name, methods[name])
    daemon.methods.update(methods)
