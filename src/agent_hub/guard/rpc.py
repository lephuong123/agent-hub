"""Bảng method JSON-RPC của luồng B (namespace `approval.*`, `guard.*`, `notify.*`, `run.continue|resume`, `agent.purge`).

Chữ ký (MCP server của luồng A gọi `approval.list|approve|deny` với các tham số này):
- `approval.list {status?: "open"|"pending"|"waiting_human"|"allowed"|"denied"|"cancelled"|"all", run_id?, agent_id?, limit?}`
  → list. `open` và `pending` đều = mọi request CHƯA quyết (open + waiting_human) — đúng nghĩa `open_requests`.
- `approval.get {id}`
- `approval.approve|deny {id | request_id, note?, channel? (vd "mcp:<client>"), actor?|by?}` → approval sau khi quyết.
- `guard.check` (hook/hub-guard), `guard.test {command|tool+input, cwd?, run_id?}`, `guard.policy`,
  `guard.protect.list|add|rm`, `notify.test {text?}`, `run.continue {id, prompt}`, `run.resume {id, prompt?}`,
  `agent.purge {id, confirm}` (confirm phải bằng id — người gõ lại).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..errors import HubError

if TYPE_CHECKING:
    from ..daemon import Daemon


def _id(p: dict[str, Any]) -> str:
    v = p.get("id") or p.get("request_id")
    if not v:
        raise HubError("thiếu tham số 'id'")
    return str(v)


def register_rpc(d: "Daemon") -> None:
    hub = d.hub

    def approval_list(p: dict[str, Any]) -> list[dict[str, Any]]:
        st = p.get("status") or "pending"
        if st == "open":
            st = "pending"
        return hub.approvals.list(st, run_id=p.get("run_id"), agent_id=p.get("agent_id"), limit=int(p.get("limit") or 100))

    def decide(decision: str):
        def _f(p: dict[str, Any]) -> dict[str, Any]:
            return hub.approvals.resolve(
                _id(p), decision, by=str(p.get("by") or p.get("actor") or "user"), channel=str(p.get("channel") or "cli"), note=p.get("note")
            )

        return _f

    def run_continue(p: dict[str, Any]) -> dict[str, Any]:
        prompt = p.get("prompt")
        if not prompt:
            raise HubError("thiếu tham số 'prompt'")
        return hub.runs.continue_run(_id(p), str(prompt), budget_usd=p.get("budget_usd"))

    def run_resume(p: dict[str, Any]) -> dict[str, Any]:
        from ..runs import RESUME_INTERRUPTED_PROMPT

        return hub.runs.continue_run(_id(p), str(p.get("prompt") or RESUME_INTERRUPTED_PROMPT), reason="manual_resume")

    def protect_rm(p: dict[str, Any]) -> dict[str, Any]:
        try:
            return hub.guard.protect_rm(p)
        except KeyError:
            raise HubError(f"không có tài nguyên bảo vệ '{p.get('id')}'", code=-32004) from None
        except ValueError as e:
            raise HubError(str(e)) from None

    def protect_add(p: dict[str, Any]) -> dict[str, Any]:
        try:
            return hub.guard.protect_add(p)
        except ValueError as e:
            raise HubError(str(e)) from None

    def agent_purge(p: dict[str, Any]) -> dict[str, Any]:
        from ..recovery import purge_agent

        aid = _id(p)
        if p.get("confirm") != aid:
            raise HubError(f"purge là thao tác phá huỷ — xác nhận bằng cách gõ lại id: --confirm {aid}")
        return purge_agent(hub, aid, by=str(p.get("by") or "user"), channel=str(p.get("channel") or "cli"))

    d.methods.update(
        {
            "approval.list": approval_list,
            "approval.get": lambda p: hub.approvals.get(_id(p)),
            "approval.approve": decide("allowed"),
            "approval.deny": decide("denied"),
            "guard.check": hub.guard.check,
            "guard.test": hub.guard.test,
            "guard.policy": lambda p: hub.guard.policy(),
            "guard.protect.list": lambda p: hub.guard.policy()["resources"],
            "guard.protect.add": protect_add,
            "guard.protect.rm": protect_rm,
            "guard.decisions": lambda p: [
                dict(r) for r in hub.store.conn.execute(
                    "SELECT * FROM guard_decisions" + (" WHERE run_id=?" if p.get("run_id") else "") + " ORDER BY id DESC LIMIT ?",
                    ((p["run_id"],) if p.get("run_id") else ()) + (int(p.get("limit") or 100),),
                )
            ],
            "notify.test": lambda p: hub.notifier.send_now(str(p.get("text") or ":white_check_mark: agent-hub: thử thông báo Slack")),
            "run.continue": run_continue,
            "run.resume": run_resume,
            "agent.purge": agent_purge,
        }
    )
