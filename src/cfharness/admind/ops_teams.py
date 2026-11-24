"""Team user (FR-SA-TEAM): team, thành viên, bảng việc (kanban), giao việc vào hộp thư hub thành viên, duyệt / trả lại, đồng bộ trạng thái.

Cột: `unassigned` Chưa giao · `pending` Chờ nhận · `doing` Đang làm · `review` Chờ duyệt · `done` Xong · `returned` Trả lại.
Giao việc = admind ghi `teams/<slug>/inbox/<u>/<id>.json` (thư mục chủ u 0500, file 0400 — hub chỉ đọc). Hub trả trạng thái qua
`teams/<slug>/status/<u>/status.json` (chủ u). `sync(state)` (30 s) đối chiếu → cập nhật bảng, mở khoá việc phụ thuộc, cảnh báo quá hạn.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .. import admin_control as AC
from . import rbac
from .alerts import sync_alerts
from .db import dumps, loads, new_id
from .ops import OpError, op
from .ptypes import Name, ObjId, Params, Slug, clean_text
from .shared import write_atomic

COLUMNS = ("unassigned", "pending", "doing", "review", "done", "returned")
COLUMN_VI = {"unassigned": "Chưa giao", "pending": "Chờ nhận", "doing": "Đang làm", "review": "Chờ duyệt", "done": "Xong", "returned": "Trả lại"}
MEMBER_ROLES = ("lead", "member", "observer")
PRIORITY = Literal["low", "normal", "high", "urgent"]


def teams_root() -> Path:
    return AC.shared_root() / "teams"


def _chown(p: Path, user: str, mode: int, group: str | None = None) -> None:
    # Chỉ admind chạy root mới đặt chủ/quyền hẹp: root vẫn ghi được vào thư mục 0500. Không phải root (máy dev, CI) mà chmod 0500
    # thì lần ghi sau vào chính thư mục đó bị PermissionError.
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):  # type: ignore[attr-defined]
        return
    try:
        import grp
        import pwd

        uid = pwd.getpwnam(user).pw_uid
        gid = grp.getgrnam(group).gr_gid if group else -1
        os.chown(p, uid, gid)  # type: ignore[attr-defined]
    except (ImportError, KeyError, OSError, AttributeError):  # best-effort: máy dev/Windows/test — không có user thật
        pass
    try:
        os.chmod(p, mode)
    except OSError:  # best-effort: Windows
        pass


def ensure_member_dirs(slug: str, user: str) -> None:
    r = teams_root()
    for d in (r, r / slug, r / slug / "inbox", r / slug / "status"):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o755)
        except OSError:  # best-effort: Windows
            pass
    ib, st = r / slug / "inbox" / user, r / slug / "status" / user
    ib.mkdir(exist_ok=True)
    st.mkdir(exist_ok=True)
    _chown(ib, user, 0o500)
    _chown(st, user, 0o2750, AC.ADMIN_GROUP)


def write_inbox(state: Any, task: dict[str, Any]) -> None:
    t = state.db.one("SELECT * FROM teams WHERE id=?", (task["team_id"],))
    u = state.db.one("SELECT os_user FROM hub_users WHERE id=?", (task["assignee_id"],))
    if not t or not u:
        return
    ensure_member_dirs(t["slug"], u["os_user"])
    creator = state.auth.account(task["created_by"]) if str(task["created_by"]).startswith("adm_") else None
    p = teams_root() / t["slug"] / "inbox" / u["os_user"] / f"{task['id']}.json"
    try:  # file cũ 0400 (Windows không thay được file chỉ-đọc) → xoá trước, ghi lại nguyên tử
        os.chmod(p, 0o600)
        p.unlink()
    except OSError:  # best-effort: chưa có file
        pass
    write_atomic(p, json.dumps({"schema": 1, "id": task["id"], "team": t["slug"], "team_name": t["name"], "assignee": u["os_user"],
                                "title": task["title"], "description": task["description"], "due": task["due"], "priority": task["priority"],
                                "from": (creator or {}).get("name") or "Trưởng team", "created_ts": task["created_at"],
                                "review_note": task["review_note"], "state": {"returned": "returned", "done": "done"}.get(task["state"], "assigned")},
                               ensure_ascii=False).encode("utf-8"), 0o400)
    _chown(p, u["os_user"], 0o400)


def remove_inbox(state: Any, task: dict[str, Any]) -> None:
    t = state.db.one("SELECT slug FROM teams WHERE id=?", (task["team_id"],))
    u = state.db.one("SELECT os_user FROM hub_users WHERE id=?", (task["assignee_id"],)) if task.get("assignee_id") else None
    if t and u:
        try:
            (teams_root() / t["slug"] / "inbox" / u["os_user"] / f"{task['id']}.json").unlink()
        except OSError:  # best-effort: đã không còn
            pass


# ============================================================================ quyền theo team
def _team_ok(ctx, team_id: str, need: str = "read") -> dict[str, Any]:
    t = ctx.db.one("SELECT * FROM teams WHERE id=? OR slug=?", (team_id, team_id))
    if not t:
        raise OpError("not_found", "không có team")
    if ctx.account and ctx.account["role"] == rbac.TEAM_LEAD and t["id"] not in rbac.scope_of(ctx.account).get("teams", []):
        raise OpError("forbidden", "ngoài team được giao")
    return t


@op("team.list", group="team_board", risk="read")
def team_list(ctx, p) -> list[dict[str, Any]]:
    """Danh sách team (Team Lead: team của mình)."""
    mine = rbac.scope_of(ctx.account).get("teams", []) if ctx.account and ctx.account["role"] == rbac.TEAM_LEAD else None
    out = []
    for t in ctx.db.all("SELECT * FROM teams WHERE state!='archived' ORDER BY name"):
        if mine is not None and t["id"] not in mine:
            continue
        t["members"] = ctx.db.one("SELECT COUNT(*) AS n FROM team_members WHERE team_id=?", (t["id"],))["n"]
        t["open_tasks"] = ctx.db.one("SELECT COUNT(*) AS n FROM team_tasks WHERE team_id=? AND state!='done'", (t["id"],))["n"]
        t["channel"] = loads(t["channel"], {})
        t["budget"] = loads(t["budget"], {})
        out.append(t)
    return out


class TeamIdP(Params):
    id: str = Field(min_length=1, max_length=40)


@op("team.get", group="team_board", risk="read", params=TeamIdP)
def team_get(ctx, p: TeamIdP) -> dict[str, Any]:
    """Team + thành viên (đang làm gì) + bảng việc + quá hạn + chi phí team (TEAM-12)."""
    from .collector import snapshot

    t = _team_ok(ctx, p.id)
    members = ctx.db.all("SELECT m.*, u.os_user, u.full_name, u.state AS user_state FROM team_members m JOIN hub_users u ON u.id=m.user_id "
                         "WHERE m.team_id=? ORDER BY m.role, u.os_user", (t["id"],))
    cost = 0.0
    for m in members:
        m["skills"] = loads(m["skills"], [])
        h = ctx.db.one("SELECT * FROM hubs WHERE user_id=?", (m["user_id"],))
        d = snapshot(h)
        m["running"] = (d.get("runs") or {}).get("running") if (h or {}).get("status") == "ok" else 0
        m["cost_today"] = (d.get("cost_usd") or {}).get("today")
        cost += float(m["cost_today"] or 0)
        m["open_tasks"] = ctx.db.one("SELECT COUNT(*) AS n FROM team_tasks WHERE team_id=? AND assignee_id=? AND state IN ('pending','doing','returned')",
                                     (t["id"], m["user_id"]))["n"]
        m["alerts"] = ctx.db.one("SELECT COUNT(*) AS n FROM alerts WHERE subject_kind='user' AND subject=? AND state!='resolved'", (m["user_id"],))["n"]
    tasks = ctx.db.all("SELECT * FROM team_tasks WHERE team_id=? ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 "
                       "ELSE 3 END, created_at", (t["id"],))
    names = {m["user_id"]: m["os_user"] for m in members}
    today = time.strftime("%Y-%m-%d", time.localtime(ctx.state.clock()))
    for x in tasks:
        x["depends_on"] = loads(x["depends_on"], [])
        x["assignee"] = names.get(x["assignee_id"] or "")
        x["overdue"] = bool(x["due"] and x["due"] < today and x["state"] != "done")
        x["blocked"] = any((ctx.db.one("SELECT state FROM team_tasks WHERE id=?", (d,)) or {}).get("state") != "done" for d in x["depends_on"])
    t["channel"], t["budget"] = loads(t["channel"], {}), loads(t["budget"], {})
    return {"team": t, "members": members, "tasks": tasks, "columns": [{"key": c, "label": COLUMN_VI[c]} for c in COLUMNS],
            "cost_today": round(cost, 4), "overdue": sum(1 for x in tasks if x["overdue"]), "folder": f"{teams_root()}/{t['slug']}/shared"}


class TeamUpsertP(Params):
    id: ObjId | None = None
    slug: Slug | None = None
    name: Name
    description: str = Field(default="", max_length=2000)
    goal: str = Field(default="", max_length=2000)
    lead_user_id: ObjId | None = None
    starts_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    ends_on: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    telegram_chat: str | None = Field(default=None, max_length=40, pattern=r"^-?\d{1,20}$|^@[A-Za-z0-9_]{4,40}$")
    slack_channel: str | None = Field(default=None, max_length=40, pattern=r"^[A-Z0-9]{8,20}$|^#[a-z0-9_-]{1,40}$")
    budget_usd_per_day: float | None = Field(default=None, ge=0, le=100000)


@op("team.upsert", group="team_admin", risk="normal", params=TeamUpsertP, target=lambda p: p.id or p.slug)
def team_upsert(ctx, p: TeamUpsertP) -> dict[str, Any]:
    """Tạo / sửa team (TEAM-01)."""
    ch = {k: v for k, v in (("telegram_chat", p.telegram_chat), ("slack_channel", p.slack_channel)) if v}
    budget = {"usd_per_day": p.budget_usd_per_day} if p.budget_usd_per_day is not None else {}
    if p.id:
        t = ctx.db.one("SELECT id FROM teams WHERE id=?", (p.id,))
        if not t:
            raise OpError("not_found", "không có team")
        ctx.db.exec("UPDATE teams SET name=?, description=?, goal=?, lead_user_id=?, starts_on=?, ends_on=?, channel=?, budget=? WHERE id=?",
                    (clean_text(p.name), clean_text(p.description), clean_text(p.goal), p.lead_user_id, p.starts_on, p.ends_on, dumps(ch),
                     dumps(budget), p.id))
        tid = p.id
    else:
        if not p.slug:
            raise OpError("invalid", "cần mã team (slug)")
        if ctx.db.one("SELECT id FROM teams WHERE slug=?", (p.slug,)):
            raise OpError("conflict", "trùng mã team")
        tid = new_id("t")
        ctx.db.exec("INSERT INTO teams(id, host_id, slug, name, description, goal, lead_user_id, starts_on, ends_on, channel, budget, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (tid, ctx.db.host_id, p.slug, clean_text(p.name), clean_text(p.description), clean_text(p.goal),
                                                         p.lead_user_id, p.starts_on, p.ends_on, dumps(ch), dumps(budget), ctx.state.clock()))
    if p.lead_user_id:
        ctx.db.exec("INSERT INTO team_members(team_id, user_id, role, joined_at) VALUES (?,?,'lead',?) ON CONFLICT(team_id, user_id) DO UPDATE "
                    "SET role='lead'", (tid, p.lead_user_id, ctx.state.clock()))
        ctx.db.exec("UPDATE team_members SET role='member' WHERE team_id=? AND role='lead' AND user_id!=?", (tid, p.lead_user_id))
    return {"id": tid, "redirect": f"/teams/{tid}"}


@op("team.archive", group="team_admin", risk="normal", params=TeamIdP, target=lambda p: p.id)
def team_archive(ctx, p: TeamIdP) -> dict[str, Any]:
    """Giải tán team — giữ lịch sử việc và báo cáo."""
    t = _team_ok(ctx, p.id)
    ctx.db.exec("UPDATE teams SET state='archived' WHERE id=?", (t["id"],))
    return {"ok": True, "redirect": "/teams"}


class MemberP(Params):
    team_id: ObjId
    user: str = Field(min_length=1, max_length=64)
    role: Literal["lead", "member", "observer"] = "member"
    skills: list[str] = Field(default_factory=list, max_length=20)


@op("team.member_set", group="team_admin", risk="normal", params=MemberP, target=lambda p: p.team_id)
def member_set(ctx, p: MemberP) -> dict[str, Any]:
    """Thêm / đổi vai trò thành viên (TEAM-02, 03). Tạo sẵn thư mục hộp thư + trạng thái của thành viên."""
    from .ops_users import find_user

    t = _team_ok(ctx, p.team_id)
    u = find_user(ctx, p.user)
    skills = [clean_text(s)[:40] for s in p.skills if s.strip()]
    ctx.db.exec("INSERT INTO team_members(team_id, user_id, role, skills, joined_at) VALUES (?,?,?,?,?) ON CONFLICT(team_id, user_id) DO UPDATE "
                "SET role=excluded.role, skills=excluded.skills", (t["id"], u["id"], p.role, json.dumps(skills, ensure_ascii=False), ctx.state.clock()))
    if p.role == "lead":
        ctx.db.exec("UPDATE teams SET lead_user_id=? WHERE id=?", (u["id"], t["id"]))
        ctx.db.exec("UPDATE team_members SET role='member' WHERE team_id=? AND role='lead' AND user_id!=?", (t["id"], u["id"]))
    ensure_member_dirs(t["slug"], u["os_user"])
    return {"ok": True}


class MemberRemoveP(Params):
    team_id: ObjId
    user: str = Field(min_length=1, max_length=64)


@op("team.member_remove", group="team_admin", risk="normal", params=MemberRemoveP, target=lambda p: p.team_id)
def member_remove(ctx, p: MemberRemoveP) -> dict[str, Any]:
    """Bớt thành viên — việc chưa xong về 'Chưa giao'; thu hồi hộp thư."""
    from .ops_users import find_user

    t = _team_ok(ctx, p.team_id)
    u = find_user(ctx, p.user)
    for x in ctx.db.all("SELECT * FROM team_tasks WHERE team_id=? AND assignee_id=? AND state!='done'", (t["id"], u["id"])):
        remove_inbox(ctx.state, x)
        ctx.db.exec("UPDATE team_tasks SET state='unassigned', assignee_id=NULL, updated_at=? WHERE id=?", (ctx.state.clock(), x["id"]))
    ctx.db.exec("DELETE FROM team_members WHERE team_id=? AND user_id=?", (t["id"], u["id"]))
    return {"ok": True}


# ============================================================================ bảng việc
class TaskCreateP(Params):
    team_id: ObjId
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=8000)
    assignee: str | None = Field(default=None, max_length=64)
    due: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    priority: PRIORITY = "normal"
    depends_on: list[ObjId] = Field(default_factory=list, max_length=20)


def _assignee(ctx, t: dict[str, Any], key: str | None) -> str | None:
    if not key:
        return None
    m = ctx.db.one("SELECT m.user_id FROM team_members m JOIN hub_users u ON u.id=m.user_id WHERE m.team_id=? AND (u.id=? OR u.os_user=?) "
                   "AND m.role!='observer'", (t["id"], key, key))
    if not m:
        raise OpError("invalid", "người nhận phải là thành viên (không phải quan sát) của team")
    return m["user_id"]


def _deps_done(ctx, deps: list[str]) -> bool:
    return all((ctx.db.one("SELECT state FROM team_tasks WHERE id=?", (d,)) or {}).get("state") == "done" for d in deps)


@op("team.task_create", group="team_board", risk="normal", params=TaskCreateP, scope="team", target=lambda p: p.team_id)
def task_create(ctx, p: TaskCreateP) -> dict[str, Any]:
    """Tạo việc (TEAM-05). Có người nhận và việc phụ thuộc đã xong → giao ngay vào hộp thư hub (TEAM-06, 08)."""
    t = _team_ok(ctx, p.team_id, "write")
    who = _assignee(ctx, t, p.assignee)
    for d in p.depends_on:
        if not ctx.db.one("SELECT id FROM team_tasks WHERE id=? AND team_id=?", (d, t["id"])):
            raise OpError("invalid", "việc phụ thuộc phải thuộc cùng team")
    now = ctx.state.clock()
    tid = new_id("tt")
    ctx.db.exec("INSERT INTO team_tasks(id, team_id, title, description, created_by, assignee_id, due, priority, state, depends_on, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (tid, t["id"], clean_text(p.title), clean_text(p.description), ctx.actor_id, who, p.due, p.priority,
                                                     "unassigned", json.dumps(p.depends_on), now, now))
    if who and _deps_done(ctx, p.depends_on):
        _assign(ctx, tid)
    return {"id": tid}


def _assign(ctx, tid: str) -> None:
    ctx.db.exec("UPDATE team_tasks SET state='pending', reject_reason='', updated_at=? WHERE id=?", (ctx.state.clock(), tid))
    write_inbox(ctx.state, ctx.db.one("SELECT * FROM team_tasks WHERE id=?", (tid,)))


class TaskAssignP(Params):
    id: ObjId
    assignee: str = Field(min_length=1, max_length=64)


def _task(ctx, tid: str) -> tuple[dict[str, Any], dict[str, Any]]:
    x = ctx.db.one("SELECT * FROM team_tasks WHERE id=?", (tid,))
    if not x:
        raise OpError("not_found", "không có việc")
    return x, _team_ok(ctx, x["team_id"], "write")


@op("team.task_assign", group="team_board", risk="normal", params=TaskAssignP, target=lambda p: p.id)
def task_assign(ctx, p: TaskAssignP) -> dict[str, Any]:
    """Giao (hoặc giao lại) việc cho thành viên → hộp thư hub của họ."""
    x, t = _task(ctx, p.id)
    if x["state"] not in ("unassigned", "pending", "returned"):
        raise OpError("conflict", "chỉ giao được việc chưa giao / chờ nhận / bị trả lại")
    if not _deps_done(ctx, loads(x["depends_on"], [])):
        raise OpError("conflict", "việc phụ thuộc chưa xong")
    who = _assignee(ctx, t, p.assignee)
    if x["assignee_id"] and x["assignee_id"] != who:
        remove_inbox(ctx.state, x)
    ctx.db.exec("UPDATE team_tasks SET assignee_id=? WHERE id=?", (who, p.id))
    _assign(ctx, p.id)
    return {"ok": True}


class ReviewP(Params):
    id: ObjId
    approve: bool
    note: str = Field(default="", max_length=1000)


@op("team.task_review", group="team_board", risk="normal", params=ReviewP, target=lambda p: p.id)
def task_review(ctx, p: ReviewP) -> dict[str, Any]:
    """Duyệt → Xong (mở việc phụ thuộc) / Trả lại kèm nhận xét → về hộp thư thành viên (TEAM-09)."""
    x, _t = _task(ctx, p.id)
    if x["state"] != "review":
        raise OpError("conflict", "chỉ duyệt được việc đang chờ duyệt")
    st = "done" if p.approve else "returned"
    ctx.db.exec("UPDATE team_tasks SET state=?, review_note=?, updated_at=? WHERE id=?", (st, clean_text(p.note), ctx.state.clock(), p.id))
    write_inbox(ctx.state, ctx.db.one("SELECT * FROM team_tasks WHERE id=?", (p.id,)))
    if p.approve:
        release_dependents(ctx.state, x["team_id"])
    return {"state": st}


@op("team.task_delete", group="team_board", risk="normal", params=TeamIdP, target=lambda p: p.id)
def task_delete(ctx, p: TeamIdP) -> dict[str, Any]:
    """Huỷ việc (gỡ khỏi hộp thư)."""
    x, _t = _task(ctx, p.id)
    remove_inbox(ctx.state, x)
    ctx.db.exec("DELETE FROM team_tasks WHERE id=?", (p.id,))
    return {"ok": True}


def release_dependents(state: Any, team_id: str) -> int:
    """Việc có người nhận, đang 'Chưa giao' vì chờ phụ thuộc → phụ thuộc xong thì giao (TEAM-08)."""
    n = 0
    for x in state.db.all("SELECT * FROM team_tasks WHERE team_id=? AND state='unassigned' AND assignee_id IS NOT NULL", (team_id,)):
        deps = loads(x["depends_on"], [])
        if deps and all((state.db.one("SELECT state FROM team_tasks WHERE id=?", (d,)) or {}).get("state") == "done" for d in deps):
            state.db.exec("UPDATE team_tasks SET state='pending', updated_at=? WHERE id=?", (state.clock(), x["id"]))
            write_inbox(state, state.db.one("SELECT * FROM team_tasks WHERE id=?", (x["id"],)))
            n += 1
    return n


# ============================================================================ đồng bộ trạng thái từ hub (TEAM-07)
def read_status(slug: str, user: str) -> dict[str, Any]:
    from ..admin_profile import _read

    p = teams_root() / slug / "status" / user / "status.json"
    d = _read(p, user) or {}
    tasks = d.get("tasks") if isinstance(d.get("tasks"), dict) else {}
    return {k: v for k, v in tasks.items() if isinstance(v, dict) and isinstance(k, str) and k.startswith("tt_")}


def sync(state: Any) -> int:
    from .teams_notify import send_team

    n = 0
    today = time.strftime("%Y-%m-%d", time.localtime(state.clock()))
    for t in state.db.all("SELECT * FROM teams WHERE state='active'"):
        for m in state.db.all("SELECT m.user_id, u.os_user FROM team_members m JOIN hub_users u ON u.id=m.user_id WHERE m.team_id=?", (t["id"],)):
            st = read_status(t["slug"], m["os_user"])
            for tid, s in st.items():
                x = state.db.one("SELECT * FROM team_tasks WHERE id=? AND team_id=? AND assignee_id=?", (tid, t["id"], m["user_id"]))
                if not x:
                    continue
                hs = str(s.get("state") or "")
                new = None
                if hs == "doing" and x["state"] == "pending":
                    new = "doing"
                elif hs == "submitted" and x["state"] in ("doing", "pending", "returned"):
                    new = "review"
                elif hs == "rejected" and x["state"] == "pending":
                    new = "unassigned"
                prog = s.get("progress") if isinstance(s.get("progress"), int) and 0 <= s["progress"] <= 100 else None
                rp = str(s.get("result_path") or "")[:500] or x["result_path"]
                if new or prog != x["progress"] or (s.get("hub_task_id") and s.get("hub_task_id") != x["hub_task_id"]):
                    state.db.exec("UPDATE team_tasks SET state=?, progress=?, result_path=?, hub_task_id=?, reject_reason=?, updated_at=? WHERE id=?",
                                  (new or x["state"], prog, rp, str(s.get("hub_task_id") or "")[:40] or x["hub_task_id"],
                                   clean_text(str(s.get("reason") or ""))[:500] if new == "unassigned" else x["reject_reason"], state.clock(), tid))
                    n += 1
                    if new in ("review", "unassigned") and loads(t["channel"], {}):
                        send_team(state, loads(t["channel"], {}), f"Team {t['name']}: «{x['title']}» — "
                                  + ("chờ duyệt" if new == "review" else f"{m['os_user']} từ chối"))
        overdue = state.db.one("SELECT COUNT(*) AS n FROM team_tasks WHERE team_id=? AND due IS NOT NULL AND due<? AND state!='done'", (t["id"], today))
        sync_alerts(state, "team", t["id"], {"team_overdue"} if overdue["n"] else set(), managed={"team_overdue"})
    return n
