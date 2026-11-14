"""Thực hiện tool hub-self vai trò `manager` (gọi từ `ManagerService.self_call`, đã ràng run ↔ người gọi).

Đọc: trả bản tóm tắt đã cắt (không secret). Ghi: kiểm quyền lớp 1 (`permissions.check_rpc`) → `Daemon.call_as(manager_caller)`
(daemon kiểm lại lớp 2 + bọc audit actor `manager:<agent>`) hoặc `Ledger.apply` (sửa cấu hình agent, rollback được) →
event `manager.action`. Từ chối → audit `manager.denied` + `manager.action{ok:false}` + lỗi có lý do cho manager.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import auth as _auth
from .. import capacity, templates
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
            out = neutralize_obj(_trunc(await self._maybe_await(fn(args)), 13000 if op == "workstream_get" else 1200),
                                 self.svc.run_key(self.run["id"], create=False))  # STATE.md luồng: đủ dài để viết lại
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
        if op.startswith("approval_"):
            return f"approval:{args.get('approval_id')}" if args.get("approval_id") else None
        if op == "ask_owner":
            return None
        if op == "ask_owner_permission":
            ident = args.get("approval_id") if args.get("kind") == "approve_action" else args.get("agent")
            return (f"approval:{ident}" if args.get("kind") == "approve_action" else f"agent:{ident}") if ident else None
        if op == "system_job":
            return f"system:{args.get('label')}" if args.get("label") else None
        if op.startswith("workstream_") or op == "conversation_set_workstream":
            r = res if isinstance(res, dict) else {}
            ident = r.get("workstream_id") or r.get("id") or args.get("id") or args.get("workstream")
            return f"workstream:{ident}" if ident else None
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
        from ..guard.profiles import gates_mode

        h = self.hub
        agents = []
        for r in h.store.list_agents():
            agents.append({"id": r["id"], "name": r.get("name"), "role": r.get("role"), "description": (r.get("description") or "")[:240],
                           "model": r.get("model"), "status": r.get("status"), "cost_today_usd": round(h.store.cost_today(r["id"]), 4),
                           "max_concurrent_runs": capacity.agent_limit(r), "free_slots": capacity.free_slots(h, r),
                           "manager": r["id"] == self.mid, "teams": self.svc.perms.teams_of(r["id"])})
        teams = [{"id": t["id"], "name": t.get("name"), "estop": bool(t.get("estop")), "counts": t.get("counts"),
                  "members": [{"agent_id": m.get("agent_id"), "role": m.get("role")} for m in t.get("members") or []]} for t in h.teams.list()]
        wsf = self.svc.ws.row(a["workstream"])["id"] if a.get("workstream") else None
        open_tasks = [{k: t.get(k) for k in ("id", "title", "team_id", "assignee_agent_id", "status", "wait_reason", "block_reason", "created_by", "updated_at",
                                             "workstream_id")}
                      for t in h.tasks.list(status=["triage", "todo", "ready", "running", "review", "blocked"], limit=40, workstream=wsf)]
        workstreams = [{"id": w["id"], "key": w.get("key"), "title": w["title"], "sprint": w.get("sprint"), "tasks": w["tasks"],
                        "needs_human": w["needs_human"]} for w in self.svc.ws.list("active", limit=30)]
        runs = [{k: r.get(k) for k in ("id", "agent_id", "kind", "status", "task_id", "queued_at", "started_at")}
                for st in ("running", "queued", "waiting_human") for r in h.store.list_runs(status=st, limit=20)]
        aps = [{"id": x["id"], "agent_id": x.get("agent_id"), "run_id": x.get("run_id"), "tool": x.get("tool_name"), "preview": str(x.get("preview") or "")[:200],
                "status": x.get("status"), "opened_at": x.get("opened_at"), "routed_to": x.get("routed_to")} for x in h.approvals.list("pending", limit=20)]
        return {"now": now_iso(), "you": self.mid, "estop": h.runs.estop, "cost_today_usd": round(h.store.cost_today(), 4), "agents": agents, "teams": teams,
                "workstreams": workstreams, "current_workstream": self._current_ws(),
                "open_tasks": open_tasks, "live_runs": runs, "approvals_waiting_human": aps,
                "gates_mode": gates_mode(h.config),
                "note": "Approvals with routed_to='manager' were delegated to YOU: decide them with approval_decide (or approval_escalate). "
                        "All other approvals are decided ONLY by the human: use ask_owner_permission (approve_action) if one blocks progress. "
                        "An agent with free_slots=0 is busy: work sent to it waits. Send it to an idle agent of the same kind, or agent_clone one. "
                        "free_slots=null / max_concurrent_runs=null means no limit."}

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
        wsf = self.svc.ws.row(a["workstream"])["id"] if a.get("workstream") else None
        rows = self.hub.tasks.list(team=a.get("team"), status=a.get("status"), assignee=a.get("assignee"), limit=int(a.get("limit") or 30), workstream=wsf)
        return [{k: t.get(k) for k in ("id", "title", "team_id", "assignee_agent_id", "status", "wait_reason", "block_reason", "result_summary", "created_by",
                                         "updated_at", "parents", "children", "workstream_id")} for t in rows]

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

    def op_system_list(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        return self.hub.systems.list()  # type: ignore[attr-defined]

    def op_system_get(self, a: dict[str, Any]) -> dict[str, Any]:
        n = int(self.hub.config.get("systems.log_tail_bytes") or 0) if a.get("log") else 0
        return self.hub.systems.get(str(a.get("id") or ""), log_bytes=n or None)  # type: ignore[attr-defined]

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

    async def op_agent_clone(self, a: dict[str, Any]) -> dict[str, Any]:
        """Thêm một agent cùng loại khi agent đang bận: chép cấu hình (chỉ các trường điều phối viên được đặt — đi qua đúng cổng của
        `agent_create`), SOUL và skill; mặc định vào mọi team của agent gốc cùng vai (lead → worker). Không chép memory."""
        from pathlib import Path

        from ..agents import _copy_tree
        from .permissions import AGENT_FIELDS, TOOL_POLICY_FIELDS

        src = str(a.get("src") or "").strip()
        if not src:
            raise HubError("agent_clone cần src (agent nguồn)")
        if src in self.svc.perms.manager_ids():
            self._denied("agent_clone", "agent.create", "không clone agent điều phối viên", a)
        aid = str(a.get("id") or "").strip() or capacity.next_clone_id(self.hub, src)
        row = self.hub.agents.get_row(src)
        cfg = self.hub.agents.config_of(row).model_dump()
        spec = {k: cfg[k] for k in AGENT_FIELDS if cfg.get(k) is not None}
        tp = dict(spec.get("tool_policy") or {})
        dropped = sorted(f"tool_policy.{k}" for k, v in tp.items() if k not in TOOL_POLICY_FIELDS and v not in (None, False, [], {}, ""))
        spec["tool_policy"] = {k: v for k, v in tp.items() if k in TOOL_POLICY_FIELDS and v is not None}
        # null trong limits = "bỏ giới hạn" — điều phối viên bị cấm đặt; bỏ trống để agent mới nhận mặc định/trần của hub
        spec["limits"] = {k: v for k, v in dict(spec.get("limits") or {}).items() if v is not None}
        spec["name"] = str(a.get("name") or aid)
        home = Path(row["home_path"])
        soul = (home / "SOUL.md").read_text(encoding="utf-8") if (home / "SOUL.md").exists() else None
        out = await self.op_agent_create({"id": aid, "spec": spec, "soul": soul, "rationale": a.get("rationale") or f"clone of busy {src}"})
        new_home = Path((self.hub.store.get_agent(aid) or {}).get("home_path") or "")
        if new_home.name:
            _copy_tree(home / "skills", new_home / "skills")
        self.hub.store.update_agent(aid, {"cloned_from": src})
        joined = []
        if a.get("join_teams", True):
            for team in self.svc.perms.teams_of(src):
                m = self.hub.teams.member(team, src) or {}
                role = "worker" if (m.get("role") or "worker") == "lead" else m.get("role")
                await self.op_team_member_add({"team": team, "agent": aid, "role": role})
                joined.append({"team": team, "role": role})
        res = {**out, "cloned_from": src, "teams": joined}
        if dropped:
            res["not_copied"] = dropped
            res["note"] = "owner-only settings were not copied; ask the owner if the clone needs them"
        return res

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

    # ------------------------------------------------------------------ agent bận → không xếp hàng lặng lẽ
    def _busy_gate(self, op: str, a: dict[str, Any], agent: str, method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Agent đích hết slot và người giao không nói rõ `if_busy='queue'` → KHÔNG giao, trả agent rảnh cùng loại + id gợi ý để clone.
        Trước đây việc nằm `queued` sau run đang chạy trong khi agent cùng loại rảnh (người dùng dặn: bận thì tạo thêm agent).
        Chạy SAU kiểm quyền: lời gọi sẽ bị từ chối (vd. giao cho điều phối viên) thì để `_rpc` từ chối + audit như cũ."""
        if not agent or str(a.get("if_busy") or "").lower() == "queue":
            return None
        if self.svc.perms.check_rpc(self.caller, method, params) if method in _auth.MANAGER_ALLOWED else True:
            return None
        b = capacity.busy_info(self.hub, agent)
        if b is None:
            return None
        alts = capacity.idle_alternatives(self.hub, agent, exclude=self.svc.perms.manager_ids())
        clone_id = capacity.next_clone_id(self.hub, agent)
        self._action(op, a, ok=False, error=f"agent_busy: {agent} đang chạy {b['active_runs']}/{b['max_concurrent_runs']} run — chưa giao")
        return {
            "started": False, "reason": "agent_busy", **b, "idle_alternatives": alts, "suggested_clone_id": clone_id,
            "next": ("Nothing was queued. Do one of these now instead of waiting: "
                     + ("(1) send it to an idle agent in idle_alternatives (prefer match=same_kind); " if alts else "")
                     + f"(2) agent_clone src='{agent}' id='{clone_id}' and send it to the clone; "
                     "(3) only when the work truly needs THIS agent (it must use the single shared Chrome session, or it needs this agent's "
                     "own session/memory) call again with if_busy='queue' and tell the owner it is waiting."),
        }

    async def op_task_create(self, a: dict[str, Any]) -> Any:
        t = {k: a[k] for k in ("title", "body", "team", "assignee", "role", "parents", "workspace", "priority", "budget_usd", "model", "decompose",
                               "extra_write_roots", "worktree") if a.get(k) is not None}
        t["budget_usd"] = self._clamp_budget(t.get("budget_usd"))
        ws_arg = a.get("workstream")
        if ws_arg is not None and str(ws_arg).strip().lower() in ("", "none", "lobby"):
            ws_id = None  # điều phối viên chủ động để task ngoài luồng
        elif ws_arg:
            ws_id = self.svc.ws.row(ws_arg)["id"]  # NotFound → lỗi rõ cho điều phối viên
        else:
            ws_id = self._current_ws()  # mặc định: luồng của hội thoại / nhịp đang chạy
        if ws_id:
            t["workstream_id"] = ws_id
        auto_team = None
        if t.get("assignee") and not t.get("team"):
            tm = self.svc.perms.teams_of(str(t["assignee"]))
            if len(tm) == 1:  # MGR-8: agent chỉ thuộc một team → gắn team đó; nhiều team → check_rpc từ chối, manager tự chọn
                t["team"] = auto_team = tm[0]
        if t.get("assignee") and not t.get("parents"):  # có task cha → còn chờ cha, lúc chạy agent có thể đã rảnh
            busy = self._busy_gate("task_create", a, str(t["assignee"]), "task.create", {"task": t, "created_by": self.actor})
            if busy is not None:
                return busy
        res = await self._rpc("task_create", "task.create", {"task": t, "created_by": self.actor}, a,
                              summary=f"giao task '{str(a.get('title') or '')[:80]}' cho {a.get('assignee') or a.get('team')}")
        tr = dict(self.hub.store.state_get("manager_tracked") or {})
        tr["tasks"] = (list(tr.get("tasks") or []) + [res.get("id")])[-200:]
        self.hub.store.state_set("manager_tracked", tr)
        origin, guessed = self._target_conv()
        if origin and not guessed:  # kết quả task báo về đúng hội thoại (thread Slack) đã giao nó
            self.svc.remember_origin("task", res.get("id"), origin["id"])
        warns = list(res.get("warnings") or [])
        if auto_team:
            warns.append(f"assignee is a member of team '{auto_team}' — task attached to that team (pass team= yourself next time)")
        if t.get("team") and not t.get("assignee") and not t.get("parents") and not self._team_has_free_worker(str(t["team"])):
            warns.append(f"every worker of team '{t['team']}' is busy, so this task waits: agent_clone one of them (it joins the team) "
                         "so the task starts now")
        w = self.svc.ws.maybe(ws_id) if ws_id else None
        return {"task_id": res.get("id"), "status": res.get("status"), "team_id": res.get("team_id"), "assignee": res.get("assignee_agent_id"),
                "workstream": self.svc.ws.label(w) if w else None,
                "warnings": warns,
                "next": "follow it with task_get until status is done"}

    def _team_has_free_worker(self, team: str) -> bool:
        for m in self.hub.teams.members(team):
            if m.get("role") == "lead" or m.get("agent_archived_at"):
                continue
            row = self.hub.store.get_agent(m["agent_id"])
            if row and row.get("status") != "paused" and (capacity.free_slots(self.hub, row) is None or capacity.free_slots(self.hub, row) > 0):
                return True
        return False

    async def op_task_cancel(self, a: dict[str, Any]) -> Any:
        return await self._rpc("task_cancel", "mcp.cancel_task", {"task_id": a.get("id"), "reason": a.get("reason")}, a, summary=f"huỷ task {a.get('id')}")

    async def op_task_reassign(self, a: dict[str, Any]) -> Any:
        res = await self._rpc("task_reassign", "task.reassign", {"id": a.get("id"), "assignee": a.get("assignee"), "by": self.actor,
                                                                 "reason": a.get("reason")}, a,
                              summary=f"giao task {a.get('id')} cho {a.get('assignee')}")
        return {k: (res or {}).get(k) for k in ("id", "status", "assignee_agent_id", "origin_task_id")}

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
        busy = self._busy_gate("run_start", a, str(a.get("agent") or ""), "run.start", p)
        if busy is not None:
            return busy
        res = await self._rpc("run_start", "run.start", p, a, summary=f"chạy {a.get('agent')}: {str(a.get('prompt') or '')[:80]}")
        tr = dict(self.hub.store.state_get("manager_tracked") or {})
        tr["runs"] = (list(tr.get("runs") or []) + [res.get("id")])[-200:]
        self.hub.store.state_set("manager_tracked", tr)
        origin, guessed = self._target_conv()
        if origin and not guessed:
            self.svc.remember_origin("run", res.get("id"), origin["id"])
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

    async def op_system_job(self, a: dict[str, Any]) -> Any:
        label, act = str(a.get("label") or ""), str(a.get("action") or "")
        verb = {"pause": "tạm dừng", "resume": "bật lại", "restart": "khởi động lại"}.get(act, act)
        res = await self._rpc("system_job", "system.job", {"label": label, "action": act, "reason": a.get("reason")}, a,
                              summary=f"{verb} job {label}" + (f": {str(a.get('reason'))[:100]}" if a.get("reason") else ""))
        return {k: (res or {}).get(k) for k in ("system", "label", "action", "status", "detail")}

    async def op_estop_on(self, a: dict[str, Any]) -> Any:
        p = {"on": True, "reason": f"[manager:{self.mid}] {str(a.get('reason') or '')[:300]}", "stop_running": bool(a.get("stop_running"))}
        if a.get("team"):
            p["team"] = a["team"]
        return await self._rpc("estop_on", "estop.set", p, a, summary=f"BẬT ESTOP{' team ' + str(a['team']) if a.get('team') else ''}: {str(a.get('reason') or '')[:120]}")

    # ================================================================== APPROVAL GIAO CHO ĐIỀU PHỐI VIÊN (approval-profiles.md)
    def op_approval_list(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        """Approval đang chờ — mặc định chỉ các approval GIAO cho điều phối viên (routed_to=manager). Lệnh/đích là DỮ LIỆU agent."""
        from ..guard.profiles import delegation_cfg

        mine = str(a.get("scope") or "delegated") != "all"
        rows = self.hub.approvals.list("pending", routed_to="manager" if mine else None, limit=int(a.get("limit") or 20))
        timeout = delegation_cfg(self.hub.config)["timeout_s"]
        out = []
        for r in rows:
            ti = r.get("tool_input") if isinstance(r.get("tool_input"), dict) else {}
            out.append({"id": r["id"], "agent_id": r["agent_id"], "run_id": r["run_id"], "tool": r["tool_name"], "routed_to": r.get("routed_to"),
                        "status": r["status"], "class": r.get("destructive_class"), "rule": r.get("matched_rule"), "risk": r.get("risk_level"),
                        "reason": str(r.get("reason") or "")[:300], "targets": (r.get("verdict_targets") or [])[:10],
                        "action": str(ti.get("command") or r.get("preview") or "")[:1500], "opened_at": r.get("opened_at"),
                        "delegated_at": r.get("delegated_at"), "escalates_after_s": timeout if r.get("routed_to") == "manager" else None})
        return out

    async def op_approval_decide(self, a: dict[str, Any]) -> Any:
        aid = str(a.get("approval_id") or "")
        dec = str(a.get("decision") or "")
        if dec not in ("allow", "deny"):
            self._denied("approval_decide", "approval.manager_decide", "decision phải là allow|deny (chuyển người: approval_escalate)", a)
        res = await self._rpc("approval_decide", "approval.manager_decide", {"id": aid, "decision": dec, "reason": a.get("reason")}, a,
                              summary=f"{'cho phép' if dec == 'allow' else 'từ chối'} approval {aid}: {str(a.get('reason') or '')[:120]}")
        return {"approval_id": aid, "status": (res or {}).get("status"),
                "next": "the agent's run continues (allowed: the action runs once; denied: it continues another way)"}

    async def op_approval_escalate(self, a: dict[str, Any]) -> Any:
        aid = str(a.get("approval_id") or "")
        res = await self._rpc("approval_escalate", "approval.manager_decide", {"id": aid, "decision": "escalate", "reason": a.get("note") or a.get("reason")}, a,
                              summary=f"chuyển approval {aid} lên người")
        return {"approval_id": aid, "routed_to": (res or {}).get("routed_to"), "next": "the human owner was notified; do not decide it yourself"}

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
        conv = self._owner_conversation()
        q = self.hub.questions.open(self.run, a.get("question"), a.get("options"), multi=bool(a.get("multi")), source="manager", conversation_id=conv)
        self._action("ask_owner", a, summary=f"hỏi người: {q['text'][:120]}")
        return {"question_id": q["id"], "status": q["status"], "note": "Asked. End your turn; the owner's answer arrives as an owner message."}

    async def op_ask_owner_permission(self, a: dict[str, Any]) -> Any:
        """Xin phép người (owner_actions): câu hỏi do HUB dựng từ dữ liệu thật; người chọn "Cho phép" → hub thực hiện với quyền người.
        `gates.mode` manager|off (người giao hết cổng): hub thực hiện NGAY thao tác đã chốt (vẫn qua `build` kiểm hợp lệ), audit actor manager."""
        from ..guard.profiles import gates_mode
        from . import owner_actions

        kind = str(a.get("kind") or "")
        action, desc = owner_actions.build(self.hub, kind, a)
        mode = gates_mode(self.hub.config)
        if mode != "human":
            res = await owner_actions.execute(self.hub, action, by=f"manager:{self.mid}", channel="manager")
            self._action("ask_owner_permission", a, summary=f"tự áp dụng (gates.mode={mode}): {kind} {action.get('target')}")
            return {"applied": bool(res.get("ok")), "error": res.get("error"), "did": desc[:400],
                    "note": f"gates.mode={mode}: the owner delegated this to you — applied directly, no question asked."}
        conv = self._owner_conversation()
        for q in self.hub.questions.list("open", limit=200):  # cùng thao tác đang chờ → không hỏi lại
            act = q.get("action") or {}
            if act.get("method") == action["method"] and act.get("params") == action["params"]:
                return {"question_id": q["id"], "status": q["status"], "note": "Already asked and still waiting. End your turn."}
        q = self.hub.questions.open(self.run, owner_actions.question_text(desc, a.get("reason")), owner_actions.OPTIONS, source="manager",
                                    conversation_id=conv, action=action)
        self._action("ask_owner_permission", a, summary=f"xin phép người: {kind} {action.get('target')}")
        return {"question_id": q["id"], "status": q["status"], "asked": desc[:400],
                "note": "Asked. End your turn now; the owner's decision and the result arrive as an owner message."}

    def _human_conv(self, cid: Any) -> dict[str, Any] | None:
        """Hội thoại của NGƯỜI (không phải nhịp heartbeat, chưa lưu trữ, của điều phối viên này) — không hợp lệ → None."""
        if not cid:
            return None
        try:
            c = self.svc._conv_row(str(cid))
        except NotFound:
            return None
        if c["channel"] == "heartbeat" or c.get("archived_at") or c["agent_id"] != self.mid:
            return None
        return c

    def _current_ws(self) -> str | None:
        """Luồng của lượt này: luồng của hội thoại chat / hội thoại nhịp của luồng (run_opts.manager_workstream). Luồng đã đóng vẫn tính."""
        opts = self.run.get("run_opts") or {}
        wid = opts.get("manager_workstream") or self.svc.ws.of_conversation(opts.get("manager_conversation"))
        return str(wid) if wid and self.svc.ws.maybe(wid) else None

    def _ws_conv(self, wid: str | None) -> dict[str, Any] | None:
        if not wid:
            return None
        try:
            return self._human_conv((self.svc.ws.home_conversation(str(wid)) or {}).get("id"))
        except Exception:  # pragma: no cover - định tuyến không được làm hỏng lượt
            return None

    def _target_conv(self, task_id: Any = None) -> tuple[dict[str, Any] | None, bool]:
        """Hội thoại nhận tin chủ động / câu hỏi của run này → (hội thoại, có_phải_đoán).
        1) run chat: chính hội thoại của nó. 2) lượt heartbeat: hội thoại đã giao `task_id` (hub tra, không tin chữ của agent) — nếu
        task thuộc LUỒNG mà hội thoại đó không thuộc luồng ấy (giao từ sảnh) → hội thoại của luồng. 3) mọi sự kiện của lượt cùng MỘT
        hội thoại gốc (`run_opts.tick_convs`). 4) nhịp của một luồng → hội thoại của luồng. 5) đoán: cuộc người hoạt động gần nhất (`guessed`)."""
        opts = self.run.get("run_opts") or {}
        own = self._human_conv(opts.get("manager_conversation"))
        if own:
            return own, False
        if task_id:
            wid = self.svc.ws.of_task(task_id)
            c = self._human_conv(self.svc.origin_conversation(task_id=task_id))
            if c and (not wid or c.get("workstream_id") == wid):
                return c, False
            c2 = self._ws_conv(wid)
            if c2:
                return c2, False
            if c:
                return c, False
        tc = [x for x in (opts.get("tick_convs") or []) if x]
        if len(tc) == 1:
            c = self._human_conv(tc[0])
            if c and (not opts.get("manager_workstream") or c.get("workstream_id") == opts.get("manager_workstream")):
                return c, False
        c = self._ws_conv(opts.get("manager_workstream"))
        if c:
            return c, False
        return self.svc._home_conversation(), True

    def _owner_conversation(self) -> str | None:
        conv, _ = self._target_conv()
        return conv["id"] if conv else None

    def op_notify_owner(self, a: dict[str, Any]) -> Any:
        from .service import TEXT_MAX

        text = str(a.get("text") or "").strip()
        if not text:
            raise HubError("notify_owner cần text")
        # lượt heartbeat: về hội thoại đã giao task (`task_id`) / hội thoại gốc duy nhất của các sự kiện; không biết → đoán
        # "hội thoại người hoạt động gần nhất" (`guessed` — Slack gửi DM, không chen vào thread của việc khác)
        conv, guessed = self._target_conv(a.get("task_id"))
        cid = conv["id"] if conv else None
        if cid:
            self.svc._add_msg(cid, "notify", text, self.run["id"])
        kind = str(a.get("kind") or "info")
        audit_id = self.hub.audit.record("manager.notify", self.mid, self.run["id"], {"text": text[:1000], "kind": kind, "conversation_id": cid},
                                         actor=self.actor, channel="manager", target=cid, summary="manager.notify")
        self.hub.publish(make_event("manager.reply", self.mid, {"conversation_id": cid, "run_id": self.run["id"], "text": text[:TEXT_MAX],
                                                               **({"truncated": True} if len(text) > TEXT_MAX else {}),
                                                               "channel": conv["channel"] if conv else None, "kind": "notify", "notify_kind": kind, "ok": True,
                                                               "conversation_guessed": bool(conv) and guessed},
                                    run_id=self.run["id"]))
        self._action("notify_owner", a, summary=f"báo người: {text[:120]}", audit_id=audit_id)
        return {"delivered_to": conv["channel"] if conv else None, "conversation_id": cid, "note": None if conv else "no human conversation yet — stored as event only"}


    # ================================================================== LUỒNG VIỆC (workstreams.py)
    def op_workstream_list(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        return self.svc.ws.list(str(a.get("status") or "active"), limit=int(a.get("limit") or 50))

    def op_workstream_get(self, a: dict[str, Any]) -> dict[str, Any]:
        ref = a.get("id") or self._current_ws()
        if not ref:
            raise HubError("workstream_get cần id (hội thoại này không thuộc luồng nào)")
        return self.svc.ws.get(ref)

    def _ws_audit(self, op: str, a: dict[str, Any], res: dict[str, Any], summary: str) -> None:
        aid = self.hub.audit.record("manager.workstream", self.mid, self.run["id"], {"op": op, "args": _trunc(a, 300), "workstream_id": res.get("id")},
                                    actor=self.actor, channel="manager", target=f"workstream:{res.get('id')}", summary=summary[:200])
        self._action(op, a, summary=summary, audit_id=aid, res=res)

    def op_workstream_create(self, a: dict[str, Any]) -> dict[str, Any]:
        res = self.svc.ws.create(a.get("title"), key=a.get("key"), sprint=a.get("sprint"), state=a.get("state"), by=self.actor)
        self._ws_audit("workstream_create", a, res, f"mở luồng {res['label']}")
        return {**{k: res.get(k) for k in ("id", "key", "title", "sprint", "status", "label")},
                "next": "file tasks under it with task_create(workstream=...); if this conversation is about it, conversation_set_workstream"}

    def op_workstream_update(self, a: dict[str, Any]) -> dict[str, Any]:
        ref = a.get("id") or self._current_ws()
        if not ref:
            raise HubError("workstream_update cần id")
        ch = {k: a[k] for k in ("title", "key", "sprint", "status") if k in a and a[k] is not None}
        res = self.svc.ws.update(ref, ch, by=self.actor)
        verb = "đóng" if ch.get("status") == "closed" else "mở lại" if ch.get("status") == "active" else "sửa"
        self._ws_audit("workstream_update", a, res, f"{verb} luồng {res['label']}")
        return {k: res.get(k) for k in ("id", "key", "title", "sprint", "status", "label")}

    def op_workstream_state_set(self, a: dict[str, Any]) -> dict[str, Any]:
        ref = a.get("id") or self._current_ws()
        if not ref:
            raise HubError("workstream_state_set cần id (hội thoại này không thuộc luồng nào)")
        text = str(a.get("text") or "").strip()
        if not text:
            raise HubError("workstream_state_set cần text (toàn bộ STATE.md mới)")
        res = self.svc.ws.set_state(ref, text, by=self.actor)
        self._ws_audit("workstream_state_set", a | {"text": f"({len(text)} chars)"}, res, f"cập nhật STATE luồng {res['label']}")
        return {"id": res["id"], "label": res["label"], "state_chars": len(res.get("state") or ""), "state_updated_at": res.get("state_updated_at")}

    def op_conversation_set_workstream(self, a: dict[str, Any]) -> dict[str, Any]:
        opts = self.run.get("run_opts") or {}
        conv = self._human_conv(opts.get("manager_conversation"))
        if conv is None:
            raise HubError("chỉ dùng trong hội thoại với người (không phải lượt nhịp)")
        w = self.svc.ws.row(a.get("workstream"))
        if conv.get("workstream_id") and conv["workstream_id"] != w["id"]:
            self._denied("conversation_set_workstream", None, "hội thoại đã thuộc luồng khác — chỉ người chuyển hội thoại giữa các luồng", a)
        out = self.svc.ws.attach_conversation(conv["id"], w["id"], by=self.actor)
        res = {"id": w["id"], "workstream_id": w["id"], "conversation_id": conv["id"], "label": self.svc.ws.label(w)}
        self._ws_audit("conversation_set_workstream", a, res, f"gắn hội thoại {conv['id']} vào luồng {res['label']}")
        return {**res, "conversation": {k: out.get(k) for k in ("id", "channel", "title")},
                "note": "From your next turn this conversation is about that workstream; tasks you create here are filed under it."}

    # ================================================================== LỊCH CHẠY TỰ ĐỘNG (schedules/)
    _SCHED_KEEP = ("id", "name", "kind", "target", "options", "expr", "describe", "status", "paused_reason", "deliver_to", "budget_usd", "created_by",
                   "next_run_at", "next_run_local", "last_run_at", "last_run_id", "last_task_id", "last_status", "fail_count", "run_count")

    def op_schedule_list(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.hub.schedules.list(status=a.get("status") or None, limit=50)  # type: ignore[attr-defined]
        out = []
        for r in rows:
            d = {k: r.get(k) for k in self._SCHED_KEEP}
            d["prompt"] = str(r.get("prompt") or "")[:300]
            d["yours"] = str(r.get("created_by") or "").startswith("manager:")
            out.append(d)
        return out

    def op_schedule_get(self, a: dict[str, Any]) -> dict[str, Any]:
        r = self.hub.schedules.get(str(a.get("id") or ""), fires=10)  # type: ignore[attr-defined]
        d = {k: r.get(k) for k in self._SCHED_KEEP}
        d.update({"prompt": str(r.get("prompt") or "")[:1500], "yours": str(r.get("created_by") or "").startswith("manager:"),
                  "fires": [{k: f.get(k) for k in ("fired_at", "status", "reason", "run_id", "task_id", "manual")} for f in r.get("fires") or []]})
        return d

    async def op_schedule_create(self, a: dict[str, Any]) -> Any:
        p = {k: a[k] for k in ("name", "kind", "expr", "prompt", "target", "team", "assignee", "budget_usd") if a.get(k) is not None}
        dt = a.get("deliver_to")
        if isinstance(dt, str):
            dt = {"conversation_id": dt} if dt.startswith("mc_") else {"channel": dt}
        if not dt:
            conv = self._owner_conversation()  # mặc định: hội thoại hiện tại của người (không bao giờ heartbeat)
            dt = {"conversation_id": conv} if conv else {}
        p["deliver_to"] = dt
        if p.get("budget_usd") is not None:
            p["budget_usd"] = self._clamp_budget(p["budget_usd"])
        if p.get("kind") == "task" and p.get("assignee") and not p.get("team"):
            tm = self.svc.perms.teams_of(str(p["assignee"]))
            if len(tm) > 1:
                self._denied("schedule_create", "schedule.create", f"'{p['assignee']}' thuộc team {', '.join(tm)} — truyền team=<một trong số đó>", a)
            if tm:
                p["team"] = tm[0]  # MGR-8: task do lịch tạo cũng chạy với charter/chính sách của team
        res = await self._rpc("schedule_create", "schedule.create", p, a,
                              summary=f"tạo lịch '{str(a.get('name') or a.get('prompt') or '')[:60]}' ({a.get('expr')})")
        return {k: (res or {}).get(k) for k in ("id", "name", "kind", "expr", "describe", "next_run_local", "deliver_to", "budget_usd")} | {
            "next": "tell the owner what/when/where in one line and how to stop it: schedule_pause or `cfharness schedule pause <id>`"}

    async def op_schedule_pause(self, a: dict[str, Any]) -> Any:
        res = await self._rpc("schedule_pause", "schedule.pause", {"id": a.get("id"), "reason": a.get("reason")}, a, summary=f"tạm dừng lịch {a.get('id')}")
        return {k: (res or {}).get(k) for k in ("id", "status", "name")}

    async def op_schedule_resume(self, a: dict[str, Any]) -> Any:
        res = await self._rpc("schedule_resume", "schedule.resume", {"id": a.get("id")}, a, summary=f"bật lại lịch {a.get('id')}")
        return {k: (res or {}).get(k) for k in ("id", "status", "name", "next_run_at")}

    async def op_schedule_rm(self, a: dict[str, Any]) -> Any:
        return await self._rpc("schedule_rm", "schedule.rm", {"id": a.get("id")}, a, summary=f"xoá lịch {a.get('id')}")

    async def op_schedule_run_now(self, a: dict[str, Any]) -> Any:
        res = await self._rpc("schedule_run_now", "schedule.run_now", {"id": a.get("id")}, a, summary=f"chạy ngay lịch {a.get('id')}")
        return {k: (res or {}).get(k) for k in ("id", "run_id", "task_id", "conversation_id")}
