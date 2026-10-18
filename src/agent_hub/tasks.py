"""Task, DAG, comment (FR-TK-02..04, FR-TK-11) — phần DỮ LIỆU. Điều phối (claim, lease, retry, tách việc, review)
ở `dispatch/`.

Vòng đời (FR-TK-03): `triage → todo → ready → running → (review) → done | blocked | archived`. Chỉ hub chuyển trạng
thái (một writer); mỗi lần chuyển phát `task.status {task_id, from, to, reason}`.
- `triage`: task giao cho team (không chỉ định agent), team có lead và `auto_decompose` → lead tách việc.
- Task gốc đã tách (`kind=root`, `decomposed=1`) ở `running` tới khi mọi task con `done` → `done` với kết quả tổng hợp;
  task con `blocked` → task gốc `blocked` (lý do trỏ task con).
- `todo → ready` khi mọi cha `done` (dispatcher tick). Kết quả của cha được đưa vào prompt của con.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from .errors import Conflict, HubError, NotFound
from .events import make_event
from .store import now_iso

if TYPE_CHECKING:
    from .hub import Hub

STATUSES = ("triage", "todo", "ready", "running", "review", "done", "blocked", "archived")
OPEN = ("triage", "todo", "ready", "running", "review")
LIST_KEYS = (
    "id", "team_id", "assignee_agent_id", "role", "kind", "title", "status", "priority", "created_by", "origin_task_id", "decomposed",
    "current_run_id", "consecutive_failures", "attempts", "block_reason", "wait_reason", "not_before", "result_summary",
    "created_at", "updated_at", "started_at", "done_at",
)
WORKSPACE_KINDS = ("dir", "scratch", "worktree")


def _trunc(s: Any, n: int) -> Any:
    if isinstance(s, str) and len(s) > n:
        return s[:n] + "…"
    return s


class TaskService:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub

    @property
    def store(self):
        return self.hub.store

    @property
    def conn(self):
        return self.hub.store.conn

    # ------------------------------------------------------------------ events
    def publish(self, type_: str, task: dict[str, Any], payload: dict[str, Any], *, agent_id: str | None = None) -> None:
        ev = make_event(type_, agent_id or task.get("assignee_agent_id"), {"task_id": task["id"], **payload})
        ev.task_id = task["id"]
        ev.team_id = task.get("team_id")
        self.hub.publish(ev)

    # ------------------------------------------------------------------ đọc
    def row(self, task_id: str) -> dict[str, Any]:
        r = self.store._row("tasks", self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        if r is None:
            rows = self.conn.execute("SELECT * FROM tasks WHERE id LIKE ? LIMIT 2", (task_id + "%",)).fetchall()
            if len(rows) == 1:
                r = self.store._row("tasks", rows[0])
        if r is None:
            raise NotFound(f"không tìm thấy task '{task_id}'")
        return r

    def maybe(self, task_id: str | None) -> dict[str, Any] | None:
        if not task_id:
            return None
        r = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self.store._row("tasks", r)

    def parents(self, task_id: str) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT parent_id FROM task_links WHERE child_id=? ORDER BY created_at", (task_id,))]

    def children(self, task_id: str) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT child_id FROM task_links WHERE parent_id=? ORDER BY created_at", (task_id,))]

    def subtasks(self, root_id: str) -> list[dict[str, Any]]:
        return [self.store._row("tasks", r) for r in self.conn.execute("SELECT * FROM tasks WHERE origin_task_id=? ORDER BY created_at", (root_id,))]  # type: ignore[misc]

    def comments(self, task_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM task_comments WHERE task_id=? ORDER BY created_at, rowid", (task_id,))]

    def cost(self, task_id: str) -> float:
        return float(self.conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM runs WHERE task_id=?", (task_id,)).fetchone()[0])

    def _list_row(self, t: dict[str, Any]) -> dict[str, Any]:
        out = {k: t.get(k) for k in LIST_KEYS}
        out["result_summary"] = _trunc(out["result_summary"], 300)
        out["parents"] = self.parents(t["id"])
        out["children"] = self.children(t["id"])
        out["cost_usd"] = round(self.cost(t["id"]), 8)
        return out

    def list(
        self, *, team: str | None = None, status: str | list[str] | None = None, assignee: str | None = None, origin: str | None = None,
        include_archived: bool = False, limit: int = 200,
    ) -> list[dict[str, Any]]:
        where, args = ["deleted_at IS NULL"], []
        if team:
            where.append("team_id=?")
            args.append(team)
        if status:
            sts = [status] if isinstance(status, str) else list(status)
            where.append(f"status IN ({','.join('?' * len(sts))})")
            args += sts
        elif not include_archived:
            where.append("status != 'archived'")
        if assignee:
            where.append("assignee_agent_id=?")
            args.append(assignee)
        if origin:
            where.append("origin_task_id=?")
            args.append(origin)
        sql = "SELECT * FROM tasks WHERE " + " AND ".join(where) + " ORDER BY priority DESC, created_at ASC LIMIT ?"
        args.append(int(limit))
        return [self._list_row(self.store._row("tasks", r)) for r in self.conn.execute(sql, args)]  # type: ignore[arg-type]

    def get(self, task_id: str) -> dict[str, Any]:
        t = self.row(task_id)
        out = self._list_row(t)
        for k in ("body", "workspace", "overrides", "result_schema", "result_json", "max_retries", "idempotency_key", "retry_from_run_id", "review_rounds", "rate_limit_hits", "lease_expires_at", "last_heartbeat_at"):
            out[k] = t.get(k)
        out["result_summary"] = t.get("result_summary")
        out["parents"] = [self._brief(p) for p in self.parents(t["id"])]
        out["children"] = [self._brief(c) for c in self.children(t["id"])]
        out["subtasks"] = [{k: s.get(k) for k in ("id", "title", "status", "assignee_agent_id", "role", "block_reason")} for s in self.subtasks(t["id"])]
        out["comments"] = self.comments(t["id"])
        out["runs"] = [
            dict(r) for r in self.conn.execute(
                "SELECT id, agent_id, kind, status, outcome, cost_usd, queued_at, started_at, ended_at, session_id, parent_run_id, error FROM runs WHERE task_id=? ORDER BY queued_at",
                (t["id"],),
            )
        ]
        return out

    def last_work_run(self, task_id: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT id FROM runs WHERE task_id=? AND kind='task' ORDER BY queued_at DESC, rowid DESC LIMIT 1", (task_id,)).fetchone()
        return self.store.get_run(r[0]) if r else None

    def _brief(self, tid: str) -> dict[str, Any]:
        t = self.maybe(tid)
        return {"id": tid, "title": t["title"] if t else None, "status": t["status"] if t else None}

    # ------------------------------------------------------------------ tạo
    def create(self, data: dict[str, Any], *, created_by: str = "human") -> dict[str, Any]:
        key = data.get("idempotency_key")
        if key:
            r = self.conn.execute("SELECT id FROM tasks WHERE idempotency_key=?", (key,)).fetchone()
            if r:
                out = self.get(r[0])
                out["idempotent_replay"] = True
                return out
        title = (data.get("title") or "").strip()
        body = data.get("body") or ""
        if not title:
            if not body.strip():
                raise HubError("task cần title hoặc body")
            title = body.strip().splitlines()[0][:80]
        team_id = data.get("team") or data.get("team_id")
        assignee = data.get("assignee") or data.get("assignee_agent_id")
        team = None
        if team_id:
            team = self.hub.teams.row(team_id)
        if assignee:
            self.hub.agents.get_row(assignee)
            if team and not self.hub.teams.member(team_id, assignee):
                raise HubError(f"agent '{assignee}' không thuộc team '{team_id}' — `agent-hub team member add {team_id} {assignee}` trước")
        if not team_id and not assignee:
            raise HubError("task cần team hoặc assignee")
        parents = [self.row(p)["id"] for p in (data.get("parents") or [])]
        ws = data.get("workspace")
        if isinstance(ws, str):
            ws = {"kind": "dir", "path": ws} if ws not in WORKSPACE_KINDS else {"kind": ws}
        if ws is None and team is not None:
            ws = self.hub.teams.policies(team).workspace
        warnings: list[str] = []
        if ws:
            if ws.get("kind") not in WORKSPACE_KINDS:
                raise HubError("workspace.kind phải là dir|scratch|worktree")
            if ws.get("kind") == "dir":
                from pathlib import Path

                from .workspace import git_root

                p = Path(str(ws.get("path") or "")).expanduser()
                if not p.is_dir():
                    raise HubError(f"workspace dir không tồn tại: {ws.get('path')}")
                from .workspace import validate_workspace
                validate_workspace(self.hub, {"kind": "dir", "path": str(p)}, "workspace của task")
                ws = {**ws, "path": str(p.resolve())}
                if git_root(p) is None:
                    warnings.append("thư mục không phải repo git: hub chỉ khoá + ghi danh sách file bị sửa, KHÔNG hoàn tác được (run revert)")
        overrides = dict(data.get("overrides") or {})
        for k in ("model", "effort", "budget_usd", "max_runtime_s"):
            if data.get(k) is not None:
                overrides[k] = data[k]
        origin = data.get("origin_task_id")
        decompose = data.get("decompose")
        lead = self.hub.teams.lead(team_id) if team_id else None
        if decompose is None:
            decompose = bool(team is not None and not assignee and not origin and lead and self.hub.teams.policies(team).auto_decompose)
        if decompose and not lead:
            raise HubError(f"team '{team_id}' chưa có lead — không tách việc được")
        if decompose:
            status, kind = "triage", "root"
        else:
            kind = "task"
            status = "ready" if all(self.row(p)["status"] == "done" for p in parents) else "todo"
        tid = "t_" + uuid.uuid4().hex[:10]
        now = now_iso()
        row = {
            "id": tid, "team_id": team_id, "assignee_agent_id": assignee if not decompose else None, "role": data.get("role"), "kind": kind,
            "title": title, "body": body, "status": status, "priority": int(data.get("priority") or 0), "created_by": created_by,
            "workspace": ws, "overrides": overrides or None, "result_schema": data.get("result_schema"),
            "max_retries": data.get("max_retries"), "idempotency_key": key, "origin_task_id": origin, "plan_key": data.get("plan_key"),
            "created_at": now, "updated_at": now,
        }
        with self.store.tx():
            self.store._insert("tasks", row)
            for p in parents:
                self.conn.execute("INSERT OR IGNORE INTO task_links(parent_id, child_id, created_at) VALUES (?,?,?)", (p, tid, now))
        t = self.row(tid)
        self.publish("task.created", t, {"title": title, "status": status, "kind": kind, "parents": parents, "created_by": created_by})
        self.hub.dispatcher.kick()
        out = self.get(tid)
        out["warnings"] = warnings
        out["idempotent_replay"] = False
        return out

    def link(self, parent_id: str, child_id: str) -> dict[str, Any]:
        p, c = self.row(parent_id), self.row(child_id)
        if p["id"] == c["id"]:
            raise Conflict("không link task với chính nó")
        if p["id"] in self.descendants(c["id"]):
            raise Conflict(f"link {p['id']} → {c['id']} tạo chu trình — từ chối")
        if c["status"] not in ("todo", "ready", "triage"):
            raise HubError(f"task con {c['id']} đang {c['status']} — chỉ link khi con chưa chạy")
        self.conn.execute("INSERT OR IGNORE INTO task_links(parent_id, child_id, created_at) VALUES (?,?,?)", (p["id"], c["id"], now_iso()))
        if c["status"] == "ready" and p["status"] != "done":
            self.set_status(c["id"], "todo", reason=f"phụ thuộc {p['id']}")
        self.publish("task.linked", c, {"parent_id": p["id"]})
        return self.get(c["id"])

    def descendants(self, task_id: str) -> set[str]:
        seen: set[str] = set()
        stack = [task_id]
        while stack:
            cur = stack.pop()
            for ch in self.children(cur):
                if ch not in seen:
                    seen.add(ch)
                    stack.append(ch)
        return seen

    # ------------------------------------------------------------------ trạng thái
    def update(self, task_id: str, data: dict[str, Any]) -> None:
        data = {**data, "updated_at": now_iso()}
        self.store._update("tasks", "id", task_id, data)

    def set_status(self, task_id: str, to: str, *, reason: str | None = None, **fields: Any) -> dict[str, Any]:
        assert to in STATUSES, to
        t = self.row(task_id)
        frm = t["status"]
        data: dict[str, Any] = dict(fields)
        data["status"] = to
        if to == "running" and not t.get("started_at"):
            data["started_at"] = now_iso()
        if to == "done":
            data["done_at"] = now_iso()
            data.setdefault("block_reason", None)
        if to == "blocked":
            data["block_reason"] = reason
        if to in ("ready", "done", "blocked", "archived") and "wait_reason" not in data:
            data["wait_reason"] = None
        self.update(task_id, data)
        t2 = self.row(task_id)
        if frm != to:
            self.publish("task.status", t2, {"from": frm, "to": to, "reason": reason})
        return t2

    def comment(self, task_id: str, text: str, *, author: str = "human", kind: str = "comment", run_id: str | None = None) -> dict[str, Any]:
        t = self.row(task_id)
        if not text or not text.strip():
            raise HubError("comment rỗng")
        cid = "c_" + uuid.uuid4().hex[:10]
        self.conn.execute(
            "INSERT INTO task_comments(id, task_id, author, kind, text, run_id, created_at) VALUES (?,?,?,?,?,?,?)",
            (cid, t["id"], author, kind, text, run_id, now_iso()),
        )
        self.publish("task.comment", t, {"comment_id": cid, "author": author, "kind": kind, "text": _trunc(text, 500)})
        return {"comment_id": cid, "task_id": t["id"]}

    def block(self, task_id: str, reason: str) -> dict[str, Any]:
        t = self.row(task_id)
        if t["status"] in ("done", "archived"):
            raise HubError(f"task {t['id']} đang {t['status']}")
        return self.set_status(t["id"], "blocked", reason=reason or "blocked by human")

    def unblock(self, task_id: str) -> dict[str, Any]:
        t = self.row(task_id)
        if t["status"] != "blocked":
            raise HubError(f"task {t['id']} không ở trạng thái blocked (đang {t['status']})")
        if t["kind"] == "root" and t["decomposed"]:
            for s in self.subtasks(t["id"]):
                if s["status"] == "blocked":
                    self.unblock(s["id"])
            out = self.set_status(t["id"], "running", reason="unblock")
        elif t["kind"] == "root":
            out = self.set_status(t["id"], "triage", reason="unblock", consecutive_failures=0, not_before=None)
        else:
            to = "ready" if all(self.row(p)["status"] == "done" for p in self.parents(t["id"])) else "todo"
            out = self.set_status(t["id"], to, reason="unblock", consecutive_failures=0, not_before=None)
        self.hub.dispatcher.kick()
        return out

    def retry(self, task_id: str) -> dict[str, Any]:
        t = self.row(task_id)
        if t["status"] in ("running", "review") and t.get("current_run_id"):
            raise HubError(f"task {t['id']} đang chạy (run {t['current_run_id']})")
        if t["status"] == "blocked":
            return self.unblock(t["id"])
        if t["status"] in ("done", "archived"):
            out = self.set_status(t["id"], "triage" if t["kind"] == "root" else "ready", reason="retry", consecutive_failures=0, not_before=None, decomposed=0)
            self.hub.dispatcher.kick()
            return out
        self.update(t["id"], {"not_before": None, "consecutive_failures": 0})
        self.hub.dispatcher.kick()
        return self.row(t["id"])

    async def archive(self, task_id: str, *, reason: str | None = None, stop_running: bool = True) -> dict[str, Any]:
        t = self.row(task_id)
        if t["status"] == "archived":
            return t
        rid = t.get("current_run_id")
        out = self.set_status(t["id"], "archived", reason=reason or "archived", current_run_id=None, archived_at=now_iso())
        if rid and stop_running:
            run = self.store.get_run(rid)
            if run and run["status"] in ("queued", "running", "waiting_human"):
                try:
                    await self.hub.runs.stop(rid)
                except HubError:
                    pass
        if t["kind"] == "root":
            for s in self.subtasks(t["id"]):
                if s["status"] in OPEN or s["status"] == "blocked":
                    await self.archive(s["id"], reason=f"task gốc {t['id']} archived", stop_running=stop_running)
        return out

    # ------------------------------------------------------------------ prompt
    def _parent_context(self, t: dict[str, Any]) -> str:
        parts = []
        for pid in self.parents(t["id"]):
            p = self.row(pid)
            res = p.get("result_summary") or "(no summary)"
            block = f"### {p['title']} ({p['id']}, by {p.get('assignee_agent_id') or '-'})\n{_trunc(res, 6000)}"
            if p.get("result_json") is not None:
                block += "\nStructured result: " + _trunc(json.dumps(p["result_json"], ensure_ascii=False), 4000)
            parts.append(block)
        return "\n\n".join(parts)

    def _comments_block(self, t: dict[str, Any]) -> str:
        cs = self.comments(t["id"])
        return "\n".join(f"- [{c['created_at']}] {c['author']} ({c['kind']}): {_trunc(c['text'], 2000)}" for c in cs)

    def _team_block(self, t: dict[str, Any], agent_id: str) -> str:
        if not t.get("team_id"):
            return ""
        team = self.hub.teams.find(t["team_id"])
        if not team:
            return ""
        m = self.hub.teams.member(team["id"], agent_id)
        # charter team nằm trong SYSTEM PROMPT (khối (3), FR-RT-10 — hardening H-15), không lặp ở đây
        s = f"You are a member of team '{team['name']}' ({team['id']}) with role '{(m or {}).get('role', 'worker')}'."
        return s + (" Follow the team charter in your system prompt." if (team.get("charter") or "").strip() else "")

    def worker_prompt(self, t: dict[str, Any], agent_id: str, *, hub_self: bool) -> str:
        lines = [f"[agent-hub task {t['id']}] You have been assigned a task by agent-hub."]
        tb = self._team_block(t, agent_id)
        if tb:
            lines.append(tb)
        lines.append(f"\n## Task: {t['title']}\n{t.get('body') or ''}".rstrip())
        pc = self._parent_context(t)
        if pc:
            lines.append("\n## Results of prerequisite tasks (already done — build on them)\n" + pc)
        cb = self._comments_block(t)
        if cb:
            lines.append("\n## Comments on this task (oldest first — address any requested changes)\n" + cb)
        proto = ["\n## Protocol", "- Do the task yourself in the current working directory. Do not ask questions; make reasonable assumptions."]
        if hub_self:
            proto += [
                "- When done, call the tool `mcp__hub-self__task_complete` with a concise summary (and `result` if useful).",
                "- If you cannot proceed without a human decision, call `mcp__hub-self__task_block` with the reason.",
            ]
        proto.append("- Finish your final message with a short summary of what you did (this is the task result).")
        lines += proto
        return "\n".join(lines)

    def resume_prompt(self, t: dict[str, Any], outcome: str) -> str:
        return (
            f"[agent-hub task {t['id']}] Your previous attempt at this task ended unexpectedly (outcome: {outcome}). "
            "Continue the task from where you left off. Re-check the current state (files, results) before repeating any action "
            "that may already have run, then finish with a short summary of the whole task result."
        )

    def lead_prompt(self, t: dict[str, Any], lead_id: str) -> str:
        team = self.hub.teams.row(t["team_id"], allow_archived=True)
        roster = []
        for m in self.hub.teams.members(team["id"]):
            if m["agent_id"] == lead_id or m.get("agent_archived_at"):
                continue
            roster.append(f"- {m['agent_id']} (team role: {m['role']}; agent role: {m.get('agent_role') or '-'}): {m.get('agent_description') or ''}".rstrip())
        lines = [
            f"[agent-hub plan {t['id']}] You are the LEAD of team '{team['name']}' ({team['id']}). Break the request below into subtasks for your team.",
        ]
        lines.append("Team members you can assign (use the exact id):\n" + ("\n".join(roster) or "(none)"))
        lines.append(f"\n## Request: {t['title']}\n{t.get('body') or ''}".rstrip())
        cb = self._comments_block(t)
        if cb:
            lines.append("\n## Comments\n" + cb)
        lines.append(
            "\n## How to answer\n"
            "- Do NOT do the work yourself. Return only the plan as structured output.\n"
            "- Each subtask: `key` (short unique id), `title`, `body` (self-contained instructions — the worker sees only this plus the results of its parents), "
            "`assignee` (a member id from the list, matched by role/description; workers do the work, not reviewers), and `parents` (keys of subtasks whose output it needs).\n"
            "- Prefer few subtasks (2-5). Model real dependencies with `parents` (e.g. research before writing). Keep the plan acyclic.\n"
            "- `summary`: one sentence describing the plan."
        )
        return "\n".join(lines)

    def review_prompt(self, t: dict[str, Any], reviewer_id: str) -> str:
        lines = [f"[agent-hub review {t['id']}] Review the result of a task done by '{t.get('assignee_agent_id')}'. You have read-only intent: do NOT modify files."]
        tb = self._team_block(t, reviewer_id)
        if tb:
            lines.append(tb)
        lines.append(f"\n## Task: {t['title']}\n{t.get('body') or ''}".rstrip())
        lines.append(f"\n## Worker's result\n{_trunc(t.get('result_summary') or '(no summary)', 8000)}")
        if t.get("result_json") is not None:
            lines.append("Structured result: " + _trunc(json.dumps(t["result_json"], ensure_ascii=False), 4000))
        run = self.last_work_run(t["id"])
        diff = (run or {}).get("diff") if run else None
        if diff and diff.get("files"):
            lines.append("Files changed: " + ", ".join(f"{f['path']} ({f['status']})" for f in diff["files"][:50]))
        cb = self._comments_block(t)
        if cb:
            lines.append("\n## Comments\n" + cb)
        lines.append(
            "\n## How to answer\nReturn structured output: `verdict` = \"approve\" if the result satisfies the task, otherwise \"request_changes\"; "
            "`comment` = concrete feedback (what to fix, or why it is acceptable)."
        )
        return "\n".join(lines)
