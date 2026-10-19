"""Thực hiện tool hub-self vai trò `manager` (gọi từ `ManagerService.self_call`, đã ràng run ↔ người gọi).

Đọc: trả bản tóm tắt đã cắt (không secret). Ghi: kiểm quyền lớp 1 (`permissions.check_rpc`) → `Daemon.call_as(manager_caller)`
(daemon kiểm lại lớp 2 + bọc audit actor `manager:<agent>`) hoặc `Ledger.apply` (sửa cấu hình agent, rollback được) →
event `manager.action`. Từ chối → audit `manager.denied` + `manager.action{ok:false}` + lỗi có lý do cho manager.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import auth as _auth
from .. import templates
from ..errors import Blocked, HubError, NotFound
from ..events import make_event
from ..store import now_iso

if TYPE_CHECKING:
    from .service import ManagerService

READ_RUN_FIELDS = ("id", "agent_id", "kind", "status", "outcome", "task_id", "team_id", "cost_usd", "queued_at", "started_at", "ended_at", "session_id", "parent_run_id")


def _trunc(v: Any, n: int = 600) -> Any:
    from .service import _trunc as t

    return t(v, n)


class ManagerOps:
    def __init__(self, svc: "ManagerService", run: dict[str, Any]) -> None:
        self.svc = svc
        self.hub = svc.hub
        self.run = run
        self.mid = run["agent_id"]
        self.actor = f"manager:{self.mid}"
        self.caller = _auth.manager_caller(self.mid, run["id"])

    # ------------------------------------------------------------------ khung
    async def call(self, op: str, args: dict[str, Any]) -> Any:
        from ..mcp_server.hub_self import MANAGER_READ_TOOLS, MANAGER_TOOLS

        if op not in MANAGER_TOOLS:
            self._denied(op, None, f"tool '{op}' không có trong bộ tool điều phối viên", args)
        fn = getattr(self, "op_" + op, None)
        if fn is None:  # pragma: no cover
            raise HubError(f"tool chưa hỗ trợ: {op}")
        if op in MANAGER_READ_TOOLS:
            from .prompts import UNTRUSTED_NOTE, neutralize_obj

            # MGR-5: mọi dữ liệu đọc về (kết quả agent con, bình luận, dữ liệu MCP) là KHÔNG tin cậy — gỡ tiêu đề giả + ghi chú
            out = neutralize_obj(_trunc(await self._maybe_await(fn(args)), 1200), self.svc.run_key(self.run["id"], create=False))
            if isinstance(out, dict):
                out["_untrusted"] = UNTRUSTED_NOTE
            return out
        try:
            res = await self._maybe_await(fn(args))
        except HubError as e:
            data = e.data if isinstance(e.data, dict) else {}
            if data.get("reason") != "manager_forbidden" and not data.get("manager_denied"):
                self._action(op, args, ok=False, error=e.message)
            raise
        return res

    @staticmethod
    async def _maybe_await(r: Any) -> Any:
        import asyncio

        if asyncio.iscoroutine(r):
            return await r
        return r

    def _target(self, op: str, args: dict[str, Any], res: Any = None) -> str | None:
        """Đích dạng "loại:id" cho thẻ `manager.action` (web dựng link nội bộ theo loại)."""
        if op in ("estop_on", "notify_owner"):
            return None
        if op.startswith("question_"):
            return f"question:{args.get('question_id')}" if args.get("question_id") else None
        if op == "ask_owner":
            return None
        if op in ("team_member_add", "team_member_rm"):
            return f"team:{args.get('team')}" if args.get("team") else None
        kind = op.split("_", 1)[0]
        r = res if isinstance(res, dict) else {}
        if kind == "learn":
            kind, ident = "agent", args.get("agent")
        elif op == "task_create":
            ident = r.get("id") or r.get("task_id")
        elif op == "run_start":
            ident = r.get("id") or args.get("agent")
            kind = "run" if r.get("id") else "agent"
        else:
            ident = args.get("id")
        return f"{kind}:{ident}" if ident else None

    def _last_audit(self, kind: str) -> int | None:
        r = self.hub.store.conn.execute("SELECT MAX(id) FROM audit_log WHERE actor=? AND kind=?", (self.actor, kind)).fetchone()
        return int(r[0]) if r and r[0] is not None else None

    def _action(self, op: str, args: dict[str, Any], *, ok: bool = True, summary: str | None = None, audit_id: int | None = None,
                error: str | None = None, res: Any = None) -> None:
        payload = {"tool": op, "target": self._target(op, args, res), "summary": (summary or op)[:300], "audit_id": audit_id, "ok": ok}
        if error:
            payload["error"] = error[:500]
        self.hub.publish(make_event("manager.action", self.mid, payload, run_id=self.run["id"]))

    def _denied(self, op: str, method: str | None, why: str, args: dict[str, Any]) -> None:
        aid = self.hub.audit.record("manager.denied", self.mid, self.run["id"], {"tool": op, "method": method, "reason": why, "args": _trunc(args, 300)},
                                    actor=self.actor, channel="manager", target=self._target(op, args), summary=f"manager bị từ chối: {op} — {why}")
        self._action(op, args, ok=False, error=why, audit_id=aid)
        raise Blocked(f"bị từ chối: {why}. Đây là giới hạn an toàn do người đặt — báo người (notify_owner) nếu thật sự cần, không tìm cách khác.",
                      data={"reason": "manager_forbidden", "manager_denied": True, "detail": why})

    async def _rpc(self, op: str, method: str, params: dict[str, Any], args: dict[str, Any], summary: str) -> Any:
        why = self.svc.perms.check_rpc(self.caller, method, params) if method in _auth.MANAGER_ALLOWED else f"method {method} ngoài quyền"
        if why:
            self._denied(op, method, why, args)
        d = self.svc.daemon
        if d is None:
            raise HubError("điều phối viên cần daemon (call_as) — không chạy được trong Hub thuần")
        resp = await d.call_as(self.caller, method, dict(params))
        if "error" in resp:
            err = resp["error"]
            raise HubError(err.get("message") or "lỗi", data=err.get("data"), code=err.get("code"))
        res = resp.get("result")
        self._action(op, args, summary=summary, audit_id=self._last_audit(method), res=res)
        return res

    # ================================================================== ĐỌC
    def op_hub_overview(self, a: dict[str, Any]) -> dict[str, Any]:
        h = self.hub
        agents = []
        for r in h.store.list_agents():
            agents.append({"id": r["id"], "name": r.get("name"), "role": r.get("role"), "description": (r.get("description") or "")[:240],
                           "model": r.get("model"), "status": r.get("status"), "cost_today_usd": round(h.store.cost_today(r["id"]), 4),
                           "manager": r["id"] == self.mid})
        teams = [{"id": t["id"], "name": t.get("name"), "estop": bool(t.get("estop")), "counts": t.get("counts"),
                  "members": [{"agent_id": m.get("agent_id"), "role": m.get("role")} for m in t.get("members") or []]} for t in h.teams.list()]
        open_tasks = [{k: t.get(k) for k in ("id", "title", "team_id", "assignee_agent_id", "status", "wait_reason", "block_reason", "created_by", "updated_at")}
                      for t in h.tasks.list(status=["triage", "todo", "ready", "running", "review", "blocked"], limit=40)]
        runs = [{k: r.get(k) for k in ("id", "agent_id", "kind", "status", "task_id", "queued_at", "started_at")}
                for st in ("running", "queued", "waiting_human") for r in h.store.list_runs(status=st, limit=20)]
        aps = [{"id": x["id"], "agent_id": x.get("agent_id"), "run_id": x.get("run_id"), "tool": x.get("tool_name"), "preview": str(x.get("preview") or "")[:200],
                "status": x.get("status"), "opened_at": x.get("opened_at")} for x in h.approvals.list("pending", limit=20)]
        return {"now": now_iso(), "you": self.mid, "estop": h.runs.estop, "cost_today_usd": round(h.store.cost_today(), 4), "agents": agents, "teams": teams,
                "open_tasks": open_tasks, "live_runs": runs, "approvals_waiting_human": aps,
                "note": "Approvals are decided ONLY by the human; tell the owner if one blocks progress."}

    def op_agent_get(self, a: dict[str, Any]) -> dict[str, Any]:
        r = self.hub.agents.show(str(a.get("id") or ""))
        keep = ("id", "name", "role", "description", "model", "effort", "fallback_model", "tool_policy", "mcp_servers", "limits", "workspace_default",
                "status", "config_revision", "archived_at", "skills", "cost_today_usd", "recent_runs")
        out = {k: r.get(k) for k in keep}
        out["soul"] = (r.get("soul") or "")[:3000]
        out["is_manager"] = r["id"] in self.svc.perms.manager_ids()
        return out

    def op_team_get(self, a: dict[str, Any]) -> dict[str, Any]:
        return self.hub.teams.show(str(a.get("id") or ""))

    def op_task_list(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.hub.tasks.list(team=a.get("team"), status=a.get("status"), assignee=a.get("assignee"), limit=int(a.get("limit") or 30))
        return [{k: t.get(k) for k in ("id", "title", "team_id", "assignee_agent_id", "status", "wait_reason", "block_reason", "result_summary", "created_by",
                                         "updated_at", "parents", "children")} for t in rows]

    def op_task_get(self, a: dict[str, Any]) -> dict[str, Any]:
        t = self.hub.tasks.get(str(a.get("id") or ""))
        t["comments"] = (t.get("comments") or [])[-15:]
        t["runs"] = (t.get("runs") or [])[-10:]
        return t

    def op_run_list(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.hub.store.list_runs(agent_id=a.get("agent"), status=a.get("status"), limit=int(a.get("limit") or 20))
        return [{k: r.get(k) for k in READ_RUN_FIELDS} for r in rows]

    def op_run_get(self, a: dict[str, Any]) -> dict[str, Any]:
        r = self.hub.store.find_run(str(a.get("id") or ""))
        if r is None:
            raise NotFound(f"không tìm thấy run '{a.get('id')}'")
        out = {k: r.get(k) for k in READ_RUN_FIELDS}
        out.update({"prompt": str(r.get("prompt") or "")[:1500], "result": str(r.get("summary") or "")[:3000], "error": r.get("error"),
                    "cwd": r.get("cwd"), "num_turns": r.get("num_turns")})
        return out

    def op_run_events(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        r = self.hub.store.find_run(str(a.get("id") or ""))
        if r is None:
            raise NotFound(f"không tìm thấy run '{a.get('id')}'")
        evs = self.hub.store.query_events(run_id=r["id"], limit=5000)
        lim = int(a.get("limit") or 40)
        out = []
        for e in evs[-lim:]:
            p = e.payload or {}
            brief = p.get("args_preview") or p.get("text") or p.get("summary") or p.get("message") or p.get("tool") or ""
            out.append({"type": e.type, "ts": str(e.ts)[:19], "brief": str(brief)[:300]})
        return out

    async def _read_method(self, method: str, params: dict[str, Any]) -> Any:
        d = self.svc.daemon
        if d is None or method not in d.methods:
            raise HubError(f"{method} không khả dụng")
        return await self._maybe_await(d.methods[method](dict(params)))

    async def op_learn_feed(self, a: dict[str, Any]) -> Any:
        rows = await self._read_method("learn.feed", {"agent": a.get("agent"), "days": int(a.get("days") or 1)})
        return [{"type": e.get("type"), "ts": e.get("ts"), "agent_id": e.get("agent_id"), "payload": _trunc(e.get("payload"), 200)} for e in (rows or [])[-40:]]

    async def op_digest_get(self, a: dict[str, Any]) -> Any:
        return await self._read_method("digest.show", {"id": a.get("id")})

    async def op_cost_report(self, a: dict[str, Any]) -> Any:
        return await self._read_method("report.cost", {"by": a.get("by") or "agent", "since": a.get("since")})

    def op_templates(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        return [t for t in templates.list_templates() if t["name"] != "manager"]

    def op_mcp_registry_list(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        own = self.hub.guard.hub_mcp_servers()
        out = []
        for m in self.hub.mcp.list():
            if m["name"] in own:
                continue
            out.append({"name": m["name"], "description": (m.get("description") or "")[:200], "transport": (m.get("config") or {}).get("type") or m.get("transport")})
        return out

    def _clamp_budget(self, v: Any) -> Any:
        cap = self.svc.perms.bounds().get("max_budget_usd_per_run")
        if cap is None:
            return v
        try:
            return min(float(v), float(cap)) if v is not None and float(v) > 0 else float(cap)
        except (TypeError, ValueError):
            return float(cap)

    # ================================================================== GHI
    async def op_agent_create(self, a: dict[str, Any]) -> dict[str, Any]:
        aid = str(a.get("id") or "").strip()
        spec = dict(a.get("spec") or {})
        soul = a.get("soul")
        if a.get("template"):
            if str(a["template"]) == "manager":
                self._denied("agent_create", "agent.create", "template 'manager' chỉ người dùng (điều phối viên không tạo điều phối viên)", a)
            why = self.svc.perms.check_agent_spec(aid, spec, soul, creating=True)  # ghi đè do manager đưa — kiểm trước khi trộn
            if why:
                self._denied("agent_create", "agent.create", why, a)
            cfg, tsoul = templates.render(str(a["template"]), aid, spec)
            soul = soul or tsoul
        else:
            cfg = {**spec, "id": aid}
            cfg.setdefault("name", aid)
        cfg["limits"] = self.svc.perms.clamp_limits(cfg.get("limits"))  # MGR-6: agent do manager tạo luôn có trần ≤ config_bounds
        res = await self._rpc("agent_create", "agent.create", {"config": cfg, "soul": soul}, a,
                              summary=f"tạo agent {aid}" + (f" từ template {a['template']}" if a.get("template") else ""))
        dis = list(((cfg.get("tool_policy") or {}).get("disallowed_tools")) or [])
        self.svc.perms.record_disallowed(aid, [], dis, creating=True)
        return {"id": res.get("id"), "name": res.get("name"), "role": res.get("role"), "model": res.get("model"), "tool_policy": res.get("tool_policy"),
                "mcp_servers": res.get("mcp_servers"), "config_revision": res.get("config_revision")}

    async def op_agent_update(self, a: dict[str, Any]) -> dict[str, Any]:
        from ..learning.ledger import Change

        aid = str(a.get("id") or "")
        changes = dict(a.get("changes") or {})
        soul = a.get("soul")
        params = {"id": aid, "changes": changes, "soul": soul}
        why = self.svc.perms.check_rpc(self.caller, "agent.update", params)
        if why:
            self._denied("agent_update", "agent.update", why, a)
        row = self.hub.agents.get_row(aid)
        before_dis = list(((row.get("tool_policy") or {}).get("disallowed_tools")) or [])
        cur = self.hub.agents.config_of(row).model_dump()
        eff = {k: v for k, v in changes.items() if k != "id" and (cur.get(k) != v if not isinstance(v, dict) else {**(cur.get(k) or {}), **v} != cur.get(k))}
        if not eff and (soul is None or soul == self.hub.agents.soul(aid)):
            return {"id": aid, "changed": [], "note": "không có gì thay đổi"}
        if soul is not None and soul == self.hub.agents.soul(aid):
            soul = None
        keys = sorted(eff) + (["soul"] if soul is not None else [])
        ledger = self.hub.learning.ledger
        entry = ledger.apply(Change(scope="agent", owner_id=aid, target="config", target_ref=",".join(keys), op="patch", actor=self.actor, kind="config",
                                    config_changes=eff, soul=soul, agent_id=aid, reason=str(a.get("rationale") or "")[:1000] or None,
                                    summary=f"điều phối viên {self.mid} sửa {', '.join(keys)} của {aid}", created_by="manager"))
        after = self.hub.agents.get_row(aid)
        self.svc.perms.record_disallowed(aid, before_dis, list(((after.get("tool_policy") or {}).get("disallowed_tools")) or []), creating=False)
        params_a = {"id": aid, "changes": _trunc(eff, 400), "soul": "(updated)" if soul is not None else None, "ledger_entry_id": entry["id"]}
        audit_id = self.hub.audit.record("agent.update", aid, self.run["id"], {"params": params_a, "result": {"config_revision": after.get("config_revision")}},
                                         actor=self.actor, channel="manager", target=aid, summary="agent.update")
        self._action("agent_update", a, summary=f"sửa {', '.join(keys)} của {aid} (ledger {entry['id']}, rollback được)", audit_id=audit_id)
        return {"id": aid, "changed": keys, "config_revision": after.get("config_revision"), "ledger_entry_id": entry["id"],
                "rollback": f"cfharness learn rollback {entry['id']}"}

    async def op_agent_archive(self, a: dict[str, Any]) -> Any:
        aid = str(a.get("id") or "")
        res = await self._rpc("agent_archive", "agent.archive", {"id": aid, "detach_teams": bool(a.get("detach_teams"))}, a, summary=f"xoá mềm agent {aid}")
        st = dict(self.hub.store.state_get("manager_archived") or {})
        st[aid] = self.mid  # MGR-7(c): manager chỉ khôi phục agent chính nó đã xoá mềm
        self.hub.store.state_set("manager_archived", st)
        return {"id": aid, "archived_at": (res or {}).get("archived_at"), "restore": "agent_restore"}

    async def op_agent_restore(self, a: dict[str, Any]) -> Any:
        aid = str(a.get("id") or "")
        res = await self._rpc("agent_restore", "agent.restore", {"id": aid}, a, summary=f"khôi phục agent {aid}")
        return {"id": aid, "status": (res or {}).get("status")}

    async def op_team_create(self, a: dict[str, Any]) -> Any:
        cfg = {k: a[k] for k in ("id", "name", "charter", "policies", "members") if a.get(k) is not None}
        res = await self._rpc("team_create", "team.create", {"config": cfg}, a, summary=f"tạo team {a.get('id')}")
        return {"id": res.get("id"), "members": [{"agent_id": m.get("agent_id"), "role": m.get("role")} for m in res.get("members") or []]}

    async def op_team_edit(self, a: dict[str, Any]) -> Any:
        res = await self._rpc("team_edit", "team.update", {"id": a.get("id"), "changes": dict(a.get("changes") or {})}, a, summary=f"sửa team {a.get('id')}")
        return {"id": res.get("id"), "name": res.get("name"), "policies": res.get("policies")}

    async def op_team_member_add(self, a: dict[str, Any]) -> Any:
        p = {"team": a.get("team"), "agent": a.get("agent"), "role": a.get("role") or "worker", "max_in_progress": a.get("max_in_progress")}
        res = await self._rpc("team_member_add", "team.member_set", p, a, summary=f"{a.get('agent')} vào team {a.get('team')} ({p['role']})")
        return {"team": res.get("id"), "members": [{"agent_id": m.get("agent_id"), "role": m.get("role")} for m in res.get("members") or []]}

    async def op_team_member_rm(self, a: dict[str, Any]) -> Any:
        res = await self._rpc("team_member_rm", "team.member_rm", {"team": a.get("team"), "agent": a.get("agent")}, a,
                              summary=f"gỡ {a.get('agent')} khỏi team {a.get('team')}")
        return {"team": res.get("id"), "members": [m.get("agent_id") for m in res.get("members") or []]}

    async def op_team_archive(self, a: dict[str, Any]) -> Any:
        await self._rpc("team_archive", "team.archive", {"id": a.get("id"), "cancel_open_tasks": bool(a.get("cancel_open_tasks"))}, a, summary=f"xoá mềm team {a.get('id')}")
        return {"id": a.get("id"), "archived": True}

    async def op_team_restore(self, a: dict[str, Any]) -> Any:
        await self._rpc("team_restore", "team.restore", {"id": a.get("id")}, a, summary=f"khôi phục team {a.get('id')}")
        return {"id": a.get("id"), "restored": True}

    async def op_task_create(self, a: dict[str, Any]) -> Any:
        t = {k: a[k] for k in ("title", "body", "team", "assignee", "role", "parents", "workspace", "priority", "budget_usd", "model", "decompose") if a.get(k) is not None}
        t["budget_usd"] = self._clamp_budget(t.get("budget_usd"))
        res = await self._rpc("task_create", "task.create", {"task": t, "created_by": self.actor}, a,
                              summary=f"giao task '{str(a.get('title') or '')[:80]}' cho {a.get('assignee') or a.get('team')}")
        tr = dict(self.hub.store.state_get("manager_tracked") or {})
        tr["tasks"] = (list(tr.get("tasks") or []) + [res.get("id")])[-200:]
        self.hub.store.state_set("manager_tracked", tr)
        return {"task_id": res.get("id"), "status": res.get("status"), "team_id": res.get("team_id"), "assignee": res.get("assignee_agent_id"),
                "warnings": res.get("warnings") or [], "next": "follow it with task_get until status is done"}

    async def op_task_cancel(self, a: dict[str, Any]) -> Any:
        return await self._rpc("task_cancel", "mcp.cancel_task", {"task_id": a.get("id")}, a, summary=f"huỷ task {a.get('id')}")

    async def op_task_retry(self, a: dict[str, Any]) -> Any:
        res = await self._rpc("task_retry", "task.retry", {"id": a.get("id")}, a, summary=f"thử lại task {a.get('id')}")
        return {"id": (res or {}).get("id"), "status": (res or {}).get("status")}

    async def op_task_comment(self, a: dict[str, Any]) -> Any:
        res = await self._rpc("task_comment", "task.comment", {"id": a.get("id"), "text": str(a.get("text") or ""), "author": self.actor,
                                                               "steer": bool(a.get("steer"))}, a, summary=f"bình luận task {a.get('id')}")
        return {k: (res or {}).get(k) for k in ("id", "task_id", "steered_run_id", "steer_error") if (res or {}).get(k) is not None} or {"ok": True}

    async def op_run_start(self, a: dict[str, Any]) -> Any:
        p = {k: a[k] for k in ("agent", "prompt", "cwd", "budget_usd", "model") if a.get(k) is not None}
        p["budget_usd"] = self._clamp_budget(p.get("budget_usd"))
        res = await self._rpc("run_start", "run.start", p, a, summary=f"chạy {a.get('agent')}: {str(a.get('prompt') or '')[:80]}")
        tr = dict(self.hub.store.state_get("manager_tracked") or {})
        tr["runs"] = (list(tr.get("runs") or []) + [res.get("id")])[-200:]
        self.hub.store.state_set("manager_tracked", tr)
        return {"run_id": res.get("id"), "status": res.get("status"), "next": "follow it with run_get until it ends"}

    async def op_run_steer(self, a: dict[str, Any]) -> Any:
        return await self._rpc("run_steer", "run.steer", {"id": a.get("id"), "text": str(a.get("text") or ""), "by": self.actor, "channel": "manager"}, a,
                               summary=f"gửi tin vào run {a.get('id')}")

    async def op_run_stop(self, a: dict[str, Any]) -> Any:
        res = await self._rpc("run_stop", "run.stop", {"id": a.get("id")}, a, summary=f"dừng run {a.get('id')}")
        return {"run_id": a.get("id"), "status": (res or {}).get("status")}

    async def op_learn_propose(self, a: dict[str, Any]) -> Any:
        aid = str(a.get("agent") or "")
        if aid in self.svc.perms.manager_ids() | {self.mid}:
            self._denied("learn_propose", "learn.propose", f"'{aid}' là agent điều phối viên — không đề xuất thay đổi cho agent manager", a)
        payload = dict(a.get("payload") or {})
        if a.get("target") == "config":
            why = self.svc.perms.check_agent_spec(aid, dict(payload.get("changes") or {}), payload.get("soul"), creating=False)
            if why:
                self._denied("learn_propose", "learn.propose", why, a)
        res = self.hub.learning.pipeline.propose(target=str(a.get("target") or ""), op=str(a.get("op") or ""), payload=payload, agent_id=aid,
                                                 scope=str(a.get("scope") or "agent"), rationale=f"[manager:{self.mid}] " + str(a.get("rationale") or ""),
                                                 created_by="agent", run_id=self.run["id"])
        audit_id = self.hub.audit.record("learn.propose", aid, self.run["id"], {"params": _trunc(a, 300), "result": {k: res.get(k) for k in ("id", "status") if isinstance(res, dict)}},
                                         actor=self.actor, channel="manager", target=aid, summary="learn.propose")
        self._action("learn_propose", a, summary=f"đề xuất {a.get('target')}/{a.get('op')} cho {aid}: {res.get('status') if isinstance(res, dict) else ''}", audit_id=audit_id)
        return _trunc(res, 400)

    async def op_learn_refine(self, a: dict[str, Any]) -> Any:
        return _trunc(await self._rpc("learn_refine", "learn.refine", {"agent": a.get("agent"), "focus": a.get("focus"), "run": a.get("run")}, a,
                                      summary=f"yêu cầu tinh chỉnh tri thức của {a.get('agent')}"), 400)

    async def op_estop_on(self, a: dict[str, Any]) -> Any:
        p = {"on": True, "reason": f"[manager:{self.mid}] {str(a.get('reason') or '')[:300]}", "stop_running": bool(a.get("stop_running"))}
        if a.get("team"):
            p["team"] = a["team"]
        return await self._rpc("estop_on", "estop.set", p, a, summary=f"BẬT ESTOP{' team ' + str(a['team']) if a.get('team') else ''}: {str(a.get('reason') or '')[:120]}")

    # ================================================================== CÂU HỎI (UX)
    def op_question_list(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        st = str(a.get("status") or "with_manager")
        keep = ("id", "agent_id", "task_id", "run_id", "status", "routed_to", "text", "options", "multi", "allow_free_text", "escalation_note", "answer",
                "answered_by", "created_at")
        return [{k: q.get(k) for k in keep} for q in self.hub.questions.list(st, limit=int(a.get("limit") or 20))]

    async def op_question_answer(self, a: dict[str, Any]) -> Any:
        qid = str(a.get("question_id") or "")
        res = await self._rpc("question_answer", "question.answer", {"id": qid, "keys": a.get("keys") or [], "text": a.get("text")}, a,
                              summary=f"trả lời câu hỏi {qid}")
        return {"question_id": qid, "status": (res or {}).get("status"), "next": "the asking agent is resumed with your answer"}

    async def op_question_escalate(self, a: dict[str, Any]) -> Any:
        qid = str(a.get("question_id") or "")
        res = await self._rpc("question_escalate", "question.escalate", {"id": qid, "note": a.get("note")}, a, summary=f"chuyển câu hỏi {qid} lên người")
        return {"question_id": qid, "status": (res or {}).get("status"), "next": "the human owner was asked; do not answer it yourself"}

    def op_ask_owner(self, a: dict[str, Any]) -> Any:
        opts = self.run.get("run_opts") or {}
        conv = opts.get("manager_conversation")
        try:
            if conv and self.svc._conv_row(conv)["channel"] == "heartbeat":
                conv = None
        except NotFound:
            conv = None
        if conv is None:
            home = self.svc._home_conversation()
            conv = home["id"] if home else None
        q = self.hub.questions.open(self.run, a.get("question"), a.get("options"), multi=bool(a.get("multi")), source="manager", conversation_id=conv)
        self._action("ask_owner", a, summary=f"hỏi người: {q['text'][:120]}")
        return {"question_id": q["id"], "status": q["status"], "note": "Asked. End your turn; the owner's answer arrives as an owner message."}

    def op_notify_owner(self, a: dict[str, Any]) -> Any:
        text = str(a.get("text") or "").strip()
        if not text:
            raise HubError("notify_owner cần text")
        opts = self.run.get("run_opts") or {}
        conv = None
        try:
            c = self.svc._conv_row(opts.get("manager_conversation") or "")
            conv = c if c["channel"] != "heartbeat" else None
        except NotFound:
            conv = None
        conv = conv or self.svc._home_conversation()
        cid = conv["id"] if conv else None
        if cid:
            self.svc._add_msg(cid, "notify", text, self.run["id"])
        kind = str(a.get("kind") or "info")
        audit_id = self.hub.audit.record("manager.notify", self.mid, self.run["id"], {"text": text[:1000], "kind": kind, "conversation_id": cid},
                                         actor=self.actor, channel="manager", target=cid, summary="manager.notify")
        self.hub.publish(make_event("manager.reply", self.mid, {"conversation_id": cid, "run_id": self.run["id"], "text": text[:4000],
                                                               "channel": conv["channel"] if conv else None, "kind": "notify", "notify_kind": kind, "ok": True},
                                    run_id=self.run["id"]))
        self._action("notify_owner", a, summary=f"báo người: {text[:120]}", audit_id=audit_id)
        return {"delivered_to": conv["channel"] if conv else None, "conversation_id": cid, "note": None if conv else "no human conversation yet — stored as event only"}

