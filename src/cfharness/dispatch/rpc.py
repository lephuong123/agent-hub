"""RPC của M3 trên daemon: `team.*`, `task.*`, `taskself.call` (hub-self MCP), `run.revert|diff`, `workspace.locks`,
`report.*`, `agent.clone|export|import`, `mcp.team_run`, `mcp.task_events`; `estop.set` nhận thêm `team`.

`register_rpc(daemon)` gọi TRƯỚC `mcp_server.rpc.register_rpc` (để method thay đổi trạng thái được bọc audit —
tên đã thêm vào `MUTATING`).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..errors import Conflict, HubError, NotFound
from ..events import HubEvent, make_event
from ..store import now_iso

if TYPE_CHECKING:
    from ..daemon import Daemon

TASK_DONE = ("done", "blocked", "archived")
LIVE_RUN = ("queued", "running", "waiting_human")


def _req(p: dict[str, Any], key: str) -> Any:
    if p.get(key) is None:
        raise HubError(f"thiếu tham số '{key}'", code=-32602)
    return p[key]


class OrchestrationRpc:
    def __init__(self, daemon: "Daemon") -> None:
        self.daemon = daemon
        self.hub = daemon.hub
        self._waiters: dict[str, set[asyncio.Event]] = {}
        self.hub.bus.add_listener(self._wake)

    def _wake(self, ev: HubEvent) -> None:
        if ev.task_id and self._waiters:
            for w in self._waiters.get(ev.task_id, ()):
                w.set()
            # sự kiện của task con → đánh thức người chờ task gốc
            t = self.hub.tasks.maybe(ev.task_id)
            root = (t or {}).get("origin_task_id")
            if root and root in self._waiters:
                for w in self._waiters[root]:
                    w.set()

    # ------------------------------------------------------------------ team
    def team_create(self, p: dict[str, Any]) -> dict[str, Any]:
        return self.hub.teams.create(_req(p, "config"))

    async def team_estop(self, p: dict[str, Any]) -> dict[str, Any]:
        return await self.hub.teams.set_estop(_req(p, "id"), bool(_req(p, "on")), reason=p.get("reason"), stop_running=bool(p.get("stop_running")))

    async def estop_set(self, p: dict[str, Any]) -> dict[str, Any]:
        if p.get("team"):
            return await self.hub.teams.set_estop(p["team"], bool(_req(p, "on")), reason=p.get("reason"), stop_running=bool(p.get("stop_running")))
        return await self.hub.runs.set_estop(bool(_req(p, "on")), reason=p.get("reason"), stop_running=bool(p.get("stop_running")))

    # ------------------------------------------------------------------ task
    def task_create(self, p: dict[str, Any]) -> dict[str, Any]:
        data = dict(p.get("task") or p)
        created_by = p.get("created_by") or ((p.get("_channel") or "human"))
        data.pop("created_by", None)
        return self.hub.tasks.create(data, created_by=str(created_by))

    async def task_comment(self, p: dict[str, Any]) -> dict[str, Any]:
        tid, text = _req(p, "id"), _req(p, "text")
        out = self.hub.tasks.comment(tid, text, author=p.get("author") or "human")
        if p.get("steer"):
            t = self.hub.tasks.row(tid)
            steer = self.daemon.methods.get("run.steer")
            if t.get("current_run_id") and steer is not None:
                try:
                    r = steer({"id": t["current_run_id"], "run_id": t["current_run_id"], "text": text})
                    if asyncio.iscoroutine(r):
                        await r
                    out["steered_run_id"] = t["current_run_id"]
                except HubError as e:
                    out["steer_error"] = e.message
            else:
                out["steer_error"] = "task không có run đang chạy" if steer is not None else "steer chưa sẵn sàng (M2)"
        return out

    async def task_archive(self, p: dict[str, Any]) -> dict[str, Any]:
        return await self.hub.tasks.archive(_req(p, "id"), reason=p.get("reason"), stop_running=not p.get("keep_running"))

    def _task_tree_ids(self, root_id: str) -> list[str]:
        ids, stack = [root_id], [root_id]
        while stack:
            cur = stack.pop()
            for s in self.hub.tasks.subtasks(cur):
                ids.append(s["id"])
                stack.append(s["id"])
        return ids

    def task_result(self, t: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": t["id"], "status": t["status"], "result_summary": t.get("result_summary"), "result_json": t.get("result_json"),
            "block_reason": t.get("block_reason"), "cost_usd": round(sum(self.hub.tasks.cost(i) for i in self._task_tree_ids(t["id"])), 8),
            "subtasks": [{k: s.get(k) for k in ("id", "title", "status", "assignee_agent_id", "block_reason")} for s in self.hub.tasks.subtasks(t["id"])],
        }

    async def task_events(self, p: dict[str, Any]) -> dict[str, Any]:
        """Long-poll event của một task VÀ các task con (FR-MS-03 cho task). `done` khi task gốc done/blocked/archived."""
        from ..bus import LEVEL_RANK
        from ..mcp_server.rpc import compact_event

        t = self.hub.tasks.row(_req(p, "task_id"))
        tid = t["id"]
        cfg = self.hub.config.get("mcp_server")
        wait_s = max(0.0, min(float(p.get("wait_s", 20) or 0), float(cfg["max_wait_s"])))
        max_events = max(1, min(int(p.get("max_events") or cfg["max_events_per_call"]), 1000))
        max_rank = LEVEL_RANK.get(p.get("level") or "summary", 0)
        after = int(p.get("after_seq") or 0)
        cur_epoch = self.hub.bus.epoch
        full = after <= 0 or (p.get("epoch") is not None and p.get("epoch") != cur_epoch)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        waiter = asyncio.Event()
        self._waiters.setdefault(tid, set()).add(waiter)
        try:
            while True:
                waiter.clear()
                ids = self._task_tree_ids(tid)
                qs = ",".join("?" * len(ids))
                if full:
                    rows = self.hub.store.conn.execute(f"SELECT * FROM events WHERE task_id IN ({qs}) ORDER BY ts, epoch, seq LIMIT 5000", ids).fetchall()
                else:
                    rows = self.hub.store.conn.execute(f"SELECT * FROM events WHERE task_id IN ({qs}) AND epoch=? AND seq>? ORDER BY seq LIMIT 5000", (*ids, cur_epoch, after)).fetchall()
                evs = []
                for r in rows:
                    d = dict(r)
                    d["payload"] = json.loads(d["payload"])
                    if LEVEL_RANK.get(d["level"], 0) <= max_rank:
                        evs.append(d)
                t = self.hub.tasks.row(tid)
                finished = t["status"] in TASK_DONE
                if evs or finished or loop.time() >= deadline:
                    break
                try:
                    await asyncio.wait_for(waiter.wait(), deadline - loop.time())
                except asyncio.TimeoutError:
                    pass
        finally:
            ws = self._waiters.get(tid)
            if ws is not None:
                ws.discard(waiter)
                if not ws:
                    self._waiters.pop(tid, None)
        has_more = len(evs) > max_events
        evs = evs[:max_events]
        cur = [e for e in evs if e["epoch"] == cur_epoch]
        latest = cur[-1]["seq"] if cur else (0 if full else after)
        done = finished and not has_more
        return {
            "task_id": tid, "events": [compact_event(e) for e in evs], "latest_seq": latest, "epoch": cur_epoch, "truncated": False,
            "has_more": has_more, "status": t["status"], "done": done,
            "open_requests": await self.daemon.integration.open_requests() if hasattr(self.daemon, "integration") else [],
            "result": self.task_result(t) if done else None,
        }

    # ------------------------------------------------------------------ MCP: giao việc cho team
    def mcp_team_run(self, p: dict[str, Any]) -> dict[str, Any]:
        """`hub_run(team=…)` / `hub_task_create`: tạo task (idempotency_key), dispatcher nhận ngay. Trả task_id + run hiện tại."""
        data = {k: p.get(k) for k in ("team", "assignee", "title", "body", "priority", "parents", "workspace", "budget_usd", "model", "max_runtime_s",
                                      "idempotency_key", "result_schema", "decompose", "max_retries", "role") if p.get(k) is not None}
        if p.get("prompt") and not data.get("body"):
            data["body"] = p["prompt"]
        seq_before = self.hub.bus.last_seq
        t = self.hub.tasks.create(data, created_by=str(p.get("created_by") or "mcp"))
        self.hub.dispatcher.tick()
        cur = self.hub.tasks.row(t["id"])
        run = self.hub.store.get_run(cur["current_run_id"]) if cur.get("current_run_id") and cur["current_run_id"] != "__claiming__" else None
        return {
            "task_id": t["id"], "status": cur["status"], "run_id": run["id"] if run else None, "agent_id": run["agent_id"] if run else cur.get("assignee_agent_id"),
            "epoch": self.hub.bus.epoch, "after_seq": seq_before, "idempotent_replay": bool(t.get("idempotent_replay")), "warnings": t.get("warnings") or [],
        }

    # ------------------------------------------------------------------ hub-self
    def taskself_call(self, p: dict[str, Any]) -> Any:
        return self.hub.dispatcher.self_call(_req(p, "run_id"), _req(p, "op"), p.get("args") or {})

    # ------------------------------------------------------------------ workspace
    def _run(self, ref: str) -> dict[str, Any]:
        run = self.hub.store.find_run(ref)
        if run is None:
            raise NotFound(f"không tìm thấy run '{ref}'")
        return run

    def run_diff(self, p: dict[str, Any]) -> dict[str, Any]:
        from ..workspace import modified_since

        run = self._run(_req(p, "id"))
        diff = run.get("diff")
        out = {"run_id": run["id"], "checkpoint": run.get("checkpoint"), "diff": diff, "reverted_at": run.get("reverted_at")}
        if diff and diff.get("kind") == "git":
            out["modified_since"] = modified_since(diff)
            if p.get("patch") and diff.get("patch_path") and Path(diff["patch_path"]).exists():
                out["patch"] = Path(diff["patch_path"]).read_text(encoding="utf-8")
        return out

    def run_revert(self, p: dict[str, Any]) -> dict[str, Any]:
        """`cfharness run revert <run>` (FR-RT-12): áp ngược diff của run, chỉ khi file không bị sửa tiếp."""
        from ..workspace import revert

        run = self._run(_req(p, "id"))
        diff = run.get("diff")
        if not diff or diff.get("kind") not in ("git", "dir"):
            raise HubError(f"run {run['id']} không có diff (không ghi vào workspace dir, hoặc checkpoint lỗi)")
        if run.get("reverted_at"):
            raise Conflict(f"run {run['id']} đã được revert lúc {run['reverted_at']}")
        if diff.get("checkpoint_purged_at"):
            raise HubError(f"checkpoint của run {run['id']} đã bị xoá theo retention ({diff['checkpoint_purged_at']}, "
                           "`retention.checkpoints_days`) — không revert được nữa; danh sách file vẫn xem được bằng `run diff`")
        busy = self.hub.store.conn.execute(
            "SELECT id, status FROM runs WHERE session_id=? AND status IN ('queued','running','waiting_human') LIMIT 1", (run.get("session_id"),)
        ).fetchone()
        if busy:
            raise Conflict(f"session của run còn đang chạy (run {busy[0]}, {busy[1]}) — dừng trước khi revert")
        key = run.get("workspace_key")
        if key:
            holder = self.hub.runs.locks.holder(key)
            if holder and holder.get("session_id") != run.get("session_id"):
                raise Conflict(f"repo {key} đang bị run {holder.get('run_id')} khoá ghi — chờ run đó xong")
        res = revert(diff, self.hub.runs.checkpoint_dir(run["id"]), dry_run=bool(p.get("dry_run")))
        if not p.get("dry_run"):
            self.hub.store.update_run(run["id"], {"reverted_at": now_iso()})
            ev = make_event("run.reverted", run["agent_id"], {"files": res["reverted"], "warnings": res["warnings"]}, run_id=run["id"])
            ev.task_id, ev.team_id = run.get("task_id"), run.get("team_id")
            self.hub.publish(ev)
        return {"run_id": run["id"], **res}

    # ------------------------------------------------------------------ report
    def report_get(self, p: dict[str, Any]) -> dict[str, Any]:
        from ..reports import parse_since, report

        return report(self.hub.store.conn, parse_since(p.get("since")), top=int(p.get("top") or 10))

    def report_cost(self, p: dict[str, Any]) -> dict[str, Any]:
        from ..reports import cost_breakdown, parse_since

        since = parse_since(p.get("since"), default_days=1)
        by = p.get("by") or "agent"
        rows = cost_breakdown(self.hub.store.conn, since, by)
        return {"since": since.isoformat(), "by": by, "rows": rows, "total_usd": round(sum(r["cost_usd"] for r in rows), 8)}

    # ------------------------------------------------------------------ agent clone/export/import
    def agent_clone(self, p: dict[str, Any]) -> dict[str, Any]:
        from ..agents import clone_agent

        return clone_agent(self.hub.agents, _req(p, "src"), _req(p, "dst"), with_memory=bool(p.get("with_memory")), name=p.get("name"))

    def agent_export(self, p: dict[str, Any]) -> dict[str, Any]:
        from ..agents import export_agent
        from ..redact import redact_str

        secrets = self.hub.secrets

        def scrub(text: str) -> str:
            return redact_str(secrets.mask_values(text))

        return export_agent(self.hub.agents, _req(p, "id"), _req(p, "path"), with_memory=bool(p.get("with_memory")), scrub=scrub)

    def agent_import(self, p: dict[str, Any]) -> dict[str, Any]:
        from ..agents import import_agent

        return import_agent(self.hub.agents, _req(p, "path"), new_id=p.get("id"), name=p.get("name"))

    # ------------------------------------------------------------------ bảng method
    def methods(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        h = self.hub
        return {
            "team.create": self.team_create,
            "team.list": lambda p: h.teams.list(include_archived=bool(p.get("include_archived") or p.get("archived"))),
            "team.get": lambda p: h.teams.show(_req(p, "id")),
            "team.update": lambda p: h.teams.update(_req(p, "id"), p.get("changes") or {}),
            "team.member_set": lambda p: h.teams.member_set(_req(p, "team"), _req(p, "agent"), p.get("role") or "worker", p.get("max_in_progress")),
            "team.member_rm": lambda p: h.teams.member_rm(_req(p, "team"), _req(p, "agent")),
            "team.archive": lambda p: h.teams.archive(_req(p, "id"), cancel_open_tasks=bool(p.get("cancel_open_tasks"))),
            "team.restore": lambda p: h.teams.restore(_req(p, "id")),
            "team.estop": self.team_estop,
            "estop.set": self.estop_set,
            "task.create": self.task_create,
            "task.list": lambda p: h.tasks.list(team=p.get("team"), status=p.get("status"), assignee=p.get("assignee"), origin=p.get("origin"),
                                                include_archived=bool(p.get("include_archived")), limit=int(p.get("limit") or 200)),
            "task.get": lambda p: h.tasks.get(_req(p, "id")),
            "task.comment": self.task_comment,
            "task.block": lambda p: h.tasks.block(_req(p, "id"), p.get("reason") or "blocked by human"),
            "task.unblock": lambda p: h.tasks.unblock(_req(p, "id")),
            "task.retry": lambda p: h.tasks.retry(_req(p, "id")),
            "task.archive": self.task_archive,
            "task.link": lambda p: h.tasks.link(_req(p, "parent"), _req(p, "child")),
            "task.events": lambda p: self.task_events({**p, "task_id": p.get("task_id") or p.get("id")}),
            "taskself.call": self.taskself_call,
            "run.diff": self.run_diff,
            "run.revert": self.run_revert,
            "workspace.locks": lambda p: h.runs.locks.list(),
            "report.get": self.report_get,
            "report.cost": self.report_cost,
            "agent.clone": self.agent_clone,
            "agent.export": self.agent_export,
            "agent.import": self.agent_import,
            "mcp.team_run": self.mcp_team_run,
            "mcp.task_events": lambda p: self.task_events(p),
            "dispatch.tick": lambda p: (h.dispatcher.tick(), {"ok": True})[1],
        }


def register_rpc(daemon: "Daemon") -> OrchestrationRpc:
    rpc = OrchestrationRpc(daemon)
    daemon.methods.update(rpc.methods())
    daemon.orchestration = rpc  # type: ignore[attr-defined]
    return rpc
