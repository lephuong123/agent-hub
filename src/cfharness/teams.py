"""Team (FR-TM-01..04): CRUD, thành viên & vai trò, chính sách, ESTOP theo team, xoá mềm, tổng quan.

Team có `charter` (chèn vào prompt của thành viên khi làm task của team), `policies` (FR-TM-03), `estop`.
Một agent thuộc được nhiều team; mỗi team tối đa 1 lead. Xoá team = xoá mềm (archive + `HUB_HOME/teams/<id>` vào
`trash/teams/`), có task đang chạy thì từ chối trừ `cancel_open_tasks`.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from .agents import ID_RE
from .errors import Conflict, HubError, NotFound
from .events import make_event
from .store import now_iso, purge_after

if TYPE_CHECKING:
    from .hub import Hub

ROLES = ("lead", "worker", "reviewer", "verifier")
OPEN_TASK_STATUSES = ("triage", "todo", "ready", "running", "review")


class TeamPolicies(BaseModel):
    review_required: bool = False
    failure_limit: int = Field(default=2, ge=1)
    budget_usd_per_day: float | None = Field(default=None, gt=0)
    auto_decompose: bool = True
    default_assignee: str | None = None
    max_review_rounds: int = Field(default=3, ge=1)
    # workspace mặc định cho task của team: {"kind": "dir", "path": "/repo"} | {"kind": "scratch"}
    workspace: dict[str, Any] | None = None
    # FR-LN-15: công tắc học — thuộc chính sách an toàn, chỉ người đổi (M4 dùng)
    learning: dict[str, Any] = Field(default_factory=lambda: {"enabled": True, "auto_apply": True})

    @field_validator("workspace")
    @classmethod
    def _ws(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is not None and v.get("kind") not in ("dir", "scratch", "worktree"):
            raise ValueError("workspace.kind phải là dir|scratch|worktree")
        return v


def _day_start() -> datetime:
    return datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TeamService:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub

    @property
    def store(self):
        return self.hub.store

    @property
    def conn(self):
        return self.hub.store.conn

    def _publish(self, type_: str, team_id: str, payload: dict[str, Any]) -> None:
        ev = make_event(type_, None, {"team_id": team_id, **payload})
        ev.team_id = team_id
        self.hub.publish(ev)

    def home(self, team_id: str) -> Path:
        return self.hub.paths.home / "teams" / team_id

    # ------------------------------------------------------------------ đọc
    def row(self, team_id: str, *, allow_archived: bool = False) -> dict[str, Any]:
        r = self.store._row("teams", self.conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone())
        if r is None or (r.get("archived_at") and not allow_archived):
            raise NotFound(f"không tìm thấy team '{team_id}'")
        r["estop"] = bool(r["estop"])
        return r

    def find(self, team_id: str | None) -> dict[str, Any] | None:
        if not team_id:
            return None
        try:
            return self.row(team_id, allow_archived=True)
        except NotFound:
            return None

    def policies(self, team: dict[str, Any] | str | None) -> TeamPolicies:
        if team is None:
            return TeamPolicies()
        if isinstance(team, str):
            team = self.row(team, allow_archived=True)
        return TeamPolicies(**(team.get("policies") or {}))

    def members(self, team_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT m.*, a.status AS agent_status, a.role AS agent_role, a.description AS agent_description, a.archived_at AS agent_archived_at "
            "FROM team_members m LEFT JOIN agents a ON a.id = m.agent_id WHERE m.team_id=? ORDER BY CASE m.role WHEN 'lead' THEN 0 WHEN 'worker' THEN 1 ELSE 2 END, m.created_at",
            (team_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def lead(self, team_id: str) -> str | None:
        r = self.conn.execute("SELECT agent_id FROM team_members WHERE team_id=? AND role='lead'", (team_id,)).fetchone()
        return r[0] if r else None

    def member(self, team_id: str, agent_id: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM team_members WHERE team_id=? AND agent_id=?", (team_id, agent_id)).fetchone()
        return dict(r) if r else None

    def cost_since(self, team_id: str, since: datetime) -> float:
        r = self.conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM runs WHERE team_id=? AND ended_at >= ?", (team_id, _iso(since))).fetchone()
        return float(r[0])

    def cost_today(self, team_id: str) -> float:
        return self.cost_since(team_id, _day_start())

    def counts(self, team_id: str) -> dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute("SELECT status, COUNT(*) FROM tasks WHERE team_id=? AND deleted_at IS NULL GROUP BY status", (team_id,))}

    def _summary(self, t: dict[str, Any]) -> dict[str, Any]:
        t["members"] = [{"agent_id": m["agent_id"], "role": m["role"], "max_in_progress": m["max_in_progress"]} for m in self.members(t["id"])]
        t["counts"] = self.counts(t["id"])
        t["cost_today_usd"] = round(self.cost_today(t["id"]), 8)
        return t

    def list(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM teams" + ("" if include_archived else " WHERE archived_at IS NULL") + " ORDER BY id"
        out = []
        for r in self.conn.execute(sql):
            t = self.store._row("teams", r)
            t["estop"] = bool(t["estop"])
            out.append(self._summary(t))
        return out

    def show(self, team_id: str) -> dict[str, Any]:
        """FR-TM-04: thành viên + trạng thái, task theo trạng thái, chi phí hôm nay/tuần, ngân sách còn lại, ESTOP."""
        t = self._summary(self.row(team_id, allow_archived=True))
        t["members"] = [
            {"agent_id": m["agent_id"], "role": m["role"], "max_in_progress": m["max_in_progress"], "agent_status": m.get("agent_status"),
             "agent_role": m.get("agent_role"), "description": m.get("agent_description"),
             "in_progress": self.in_progress(team_id, m["agent_id"])}
            for m in self.members(team_id)
        ]
        t["cost_week_usd"] = round(self.cost_since(team_id, _day_start() - timedelta(days=6)), 8)
        budget = self.policies(t).budget_usd_per_day
        t["budget_usd_per_day"] = budget
        t["budget_remaining_usd"] = None if budget is None else round(max(0.0, budget - t["cost_today_usd"]), 8)
        t["running_runs"] = [r[0] for r in self.conn.execute("SELECT id FROM runs WHERE team_id=? AND status IN ('queued','running','waiting_human')", (team_id,))]
        return t

    def in_progress(self, team_id: str, agent_id: str) -> int:
        r = self.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE team_id=? AND assignee_agent_id=? AND status='running' AND current_run_id IS NOT NULL", (team_id, agent_id)
        ).fetchone()
        return int(r[0])

    # ------------------------------------------------------------------ ghi
    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        tid = str(data.get("id") or "")
        if not ID_RE.match(tid):
            raise HubError("id team phải là slug: chữ thường, số, '-' hoặc '_', ≤ 63 ký tự")
        if self.conn.execute("SELECT 1 FROM teams WHERE id=?", (tid,)).fetchone():
            raise Conflict(f"team đã tồn tại: {tid}")
        try:
            pol = TeamPolicies(**(data.get("policies") or {}))
        except ValidationError as e:
            raise HubError("chính sách team không hợp lệ: " + "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from None
        now = now_iso()
        self.store._insert(
            "teams",
            {"id": tid, "name": data.get("name") or tid, "charter": data.get("charter") or "", "policies": pol.model_dump(), "estop": 0, "created_at": now, "updated_at": now},
        )
        home = self.home(tid)
        (home / "skills").mkdir(parents=True, exist_ok=True)
        if not (home / "memory.md").exists():
            (home / "memory.md").write_text("# Team memory\n", encoding="utf-8")
        self._publish("team.created", tid, {"name": data.get("name") or tid})
        for m in data.get("members") or []:
            self.member_set(tid, m["agent_id"], m.get("role", "worker"), m.get("max_in_progress", 1))
        return self.show(tid)

    def update(self, team_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        t = self.row(team_id)
        unknown = [k for k in changes if k not in ("name", "charter", "policies")]
        if unknown:
            raise HubError(f"trường không sửa được: {', '.join(unknown)}")
        data: dict[str, Any] = {}
        if "name" in changes:
            data["name"] = changes["name"] or team_id
        if "charter" in changes:
            data["charter"] = changes["charter"] or ""
        if "policies" in changes:
            merged = {**(t.get("policies") or {}), **(changes["policies"] or {})}
            try:
                data["policies"] = TeamPolicies(**merged).model_dump()
            except ValidationError as e:
                raise HubError("chính sách team không hợp lệ: " + "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from None
        if data:
            data["updated_at"] = now_iso()
            self.store._update("teams", "id", team_id, data)
            self._publish("team.updated", team_id, {"changed": sorted(k for k in data if k != "updated_at")})
            self.hub.dispatcher.kick()
        return self.show(team_id)

    def member_set(self, team_id: str, agent_id: str, role: str = "worker", max_in_progress: int | None = None) -> dict[str, Any]:
        self.row(team_id)
        self.hub.agents.get_row(agent_id)
        if role not in ROLES:
            raise HubError(f"vai trò phải là một trong {ROLES}")
        cur = self.member(team_id, agent_id)
        if role == "lead":
            lead = self.lead(team_id)
            if lead and lead != agent_id:
                raise Conflict(f"team '{team_id}' đã có lead '{lead}' — mỗi team tối đa 1 lead")
        mip = int(max_in_progress if max_in_progress is not None else (cur or {}).get("max_in_progress") or 1)
        if mip < 1:
            raise HubError("max_in_progress phải ≥ 1")
        if cur:
            self.conn.execute("UPDATE team_members SET role=?, max_in_progress=? WHERE team_id=? AND agent_id=?", (role, mip, team_id, agent_id))
        else:
            self.conn.execute(
                "INSERT INTO team_members(team_id, agent_id, role, max_in_progress, created_at) VALUES (?,?,?,?,?)", (team_id, agent_id, role, mip, now_iso())
            )
        self._publish("team.member", team_id, {"agent_id": agent_id, "role": role, "max_in_progress": mip, "op": "update" if cur else "add"})
        self.hub.dispatcher.kick()
        return self.show(team_id)

    def member_rm(self, team_id: str, agent_id: str) -> dict[str, Any]:
        self.row(team_id)
        if not self.member(team_id, agent_id):
            raise NotFound(f"agent '{agent_id}' không thuộc team '{team_id}'")
        self.conn.execute("DELETE FROM team_members WHERE team_id=? AND agent_id=?", (team_id, agent_id))
        self._publish("team.member", team_id, {"agent_id": agent_id, "op": "remove"})
        return self.show(team_id)

    async def set_estop(self, team_id: str, on: bool, *, reason: str | None = None, stop_running: bool = False) -> dict[str, Any]:
        self.row(team_id)
        self.store._update("teams", "id", team_id, {"estop": 1 if on else 0, "estop_reason": reason if on else None, "estop_at": now_iso(), "updated_at": now_iso()})
        state = {"on": bool(on), "team": team_id, "reason": reason, "stop_running": bool(stop_running and on), "at": now_iso()}
        self._publish("team.estop", team_id, state)
        if on and stop_running:
            await self.stop_team_runs(team_id)
        if not on:
            self.hub.dispatcher.kick()
        return state

    async def stop_team_runs(self, team_id: str) -> list[str]:
        ids = [r[0] for r in self.conn.execute("SELECT id FROM runs WHERE team_id=? AND status IN ('queued','running','waiting_human')", (team_id,))]
        out = []
        for rid in ids:
            try:
                await self.hub.runs.stop(rid)
                out.append(rid)
            except HubError:
                pass
        return out

    async def archive(self, team_id: str, *, cancel_open_tasks: bool = False) -> dict[str, Any]:
        t = self.row(team_id)
        running = self.conn.execute("SELECT id FROM tasks WHERE team_id=? AND status IN ('running','review') AND deleted_at IS NULL", (team_id,)).fetchall()
        open_ = self.conn.execute(
            f"SELECT id FROM tasks WHERE team_id=? AND status IN ({','.join('?' * len(OPEN_TASK_STATUSES))}) AND deleted_at IS NULL", (team_id, *OPEN_TASK_STATUSES)
        ).fetchall()
        if running and not cancel_open_tasks:
            raise Conflict(f"team '{team_id}' còn {len(running)} task đang chạy — dừng trước hoặc dùng --cancel-open-tasks")
        if cancel_open_tasks:
            for (tid,) in open_:
                await self.hub.tasks.archive(tid, reason="team archived", stop_running=True)
        home = self.home(team_id)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = self.hub.paths.trash_dir / "teams" / f"{team_id}__{ts}"
        if home.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(home), str(dest))
        days = int(self.hub.config.get("retention.soft_delete_days"))
        now = now_iso()
        self.store._update("teams", "id", team_id, {"archived_at": now, "deleted_at": now, "purge_after": purge_after(days), "trash_path": str(dest), "updated_at": now})
        self._publish("team.archived", team_id, {"soft": True, "trash_path": str(dest), "cancelled_tasks": len(open_) if cancel_open_tasks else 0, "name": t["name"]})
        return self.show(team_id)

    def restore(self, team_id: str) -> dict[str, Any]:
        t = self.row(team_id, allow_archived=True)
        if not t.get("archived_at"):
            raise HubError(f"team '{team_id}' không ở trạng thái đã archive")
        home = self.home(team_id)
        tp = Path(t["trash_path"]) if t.get("trash_path") else None
        if tp and tp.exists():
            if home.exists():
                raise Conflict(f"không khôi phục được: {home} đã tồn tại")
            home.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tp), str(home))
        self.store._update("teams", "id", team_id, {"archived_at": None, "deleted_at": None, "purge_after": None, "trash_path": None, "updated_at": now_iso()})
        self._publish("team.restored", team_id, {})
        return self.show(team_id)
