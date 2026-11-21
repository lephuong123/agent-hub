"""Hồ sơ SPEC v3 + báo cáo ngày của hub cho Super Admin Console (FR-SA-PRO, FR-SA-RPT; plan-superadmin §3).

Phía HUB (ghi, trong vòng xuất `AdminExporter`):
- `status/<u>/profile.json` mỗi `PROFILE_EVERY_S`: hạ tầng, truy cập, agent (tên/vai trò/model/tin cậy/MCP/trạng thái), task theo trạng thái,
  chi phí 30 ngày theo agent/model; mức `Hoạt động` (việc đang làm + gần đây: tiêu đề, agent, trạng thái, thời lượng, câu kết quả) CHỈ khi
  `control/visibility.json` bật `activity` (admind ghi — SYS-03).
- `report/<u>/<YYYY-MM-DD>.json` chốt sau `REPORT_AT` (23:59 giờ máy); hub tắt lúc chốt → lần chạy sau bù ngày hôm trước.
Chữ tự do (tên agent, tiêu đề, câu kết quả) đều một dòng, cắt độ dài, qua `redact` — vẫn là DỮ LIỆU KHÔNG TIN CẬY ở phía đọc.

Phía ADMIND (đọc): `read_profile(user)`, `read_report(user, day)` — O_NOFOLLOW, ≤ 256 KB, chủ file = user, lọc lại cấu trúc (`clean_*`).
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import admin_control as AC

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger("cfharness.admin_profile")

SCHEMA = 3
PROFILE_EVERY_S = 300
REPORT_AT = (23, 59)
MAX_BYTES = 256 * 1024
TITLE_MAX = 200
SUMMARY_MAX = 300
ITEMS_MAX = 200
FAILED = ("failed", "crashed", "timed_out", "budget_exceeded", "rate_limited", "protocol_violation")


def _line(v: Any, n: int) -> str | None:
    if not isinstance(v, str) or not v.strip():
        return None
    from .redact import redact_str
    from .termsafe import one_line

    return one_line(redact_str(v[: n * 4]), n) or None


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10**9 else None


def _summary(text: Any) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        from .task_report import parse

        first = (parse(text) or {}).get("outcome") or text
    except Exception:  # best-effort: khung báo cáo lạ → dùng nguyên văn (đã cắt + redact)
        first = text
    return _line(str(first), SUMMARY_MAX)


# ============================================================================ dựng (phía hub)
def _agents(hub: "Hub") -> list[dict[str, Any]]:
    g = getattr(hub, "guard", None)
    out = []
    for a in hub.store.list_agents():
        try:
            lim = json.loads(a.get("limits") or "{}") if isinstance(a.get("limits"), str) else (a.get("limits") or {})
            mcp = json.loads(a.get("mcp_servers") or "[]") if isinstance(a.get("mcp_servers"), str) else (a.get("mcp_servers") or [])
        except ValueError:
            lim, mcp = {}, []
        out.append({"name": _line(a.get("name") or a.get("id"), 60), "role": _line(a.get("role"), 60), "model": _line(a.get("model") or "", 40),
                    "trust": "full" if (g and g.trust_of(a["id"])) else "normal", "status": _line(a.get("status"), 20),
                    "budget_usd": _num((lim or {}).get("max_budget_usd")),
                    "mcp": [x for x in (_line(m if isinstance(m, str) else (m or {}).get("name"), 40) for m in (mcp or [])[:30]) if x]})
    return out[:100]


def build_profile(hub: "Hub", *, activity: bool, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now()
    conn = hub.store.conn
    since = (now - timedelta(days=30)).astimezone(timezone.utc).isoformat()
    names = {a["id"]: a.get("name") or a["id"] for a in hub.store.list_agents()}
    tasks = {r[0]: int(r[1]) for r in conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall()}
    by_agent = [{"name": _line(names.get(r[0], r[0]), 60), "usd": round(float(r[1] or 0), 4)}
                for r in conn.execute("SELECT agent_id, SUM(cost_usd) FROM runs WHERE queued_at>=? GROUP BY agent_id ORDER BY 2 DESC LIMIT 30",
                                      (since,)).fetchall()]
    by_model = [{"model": _line(r[0] or "mặc định", 40), "usd": round(float(r[1] or 0), 4)}
                for r in conn.execute("SELECT model, SUM(cost_usd) FROM runs WHERE queued_at>=? GROUP BY model ORDER BY 2 DESC LIMIT 20",
                                      (since,)).fetchall()]
    devices = 0
    try:
        from .web.devices import DeviceStore

        devices = len(DeviceStore(hub.paths).list())
    except Exception:  # best-effort: chưa có kho thiết bị tin cậy
        devices = 0
    out: dict[str, Any] = {
        "schema": SCHEMA, "user": AC.os_user(), "generated_ts": time.time(),
        "infra": {"workspace_root": _line(str(hub.config.get("workspace.confine.root") or ""), 200)},
        "access": {"trusted_devices": devices},
        "agents": _agents(hub), "tasks": tasks, "cost_by_agent": by_agent, "cost_by_model": by_model,
    }
    if activity:
        rows = conn.execute("SELECT r.id, r.agent_id, r.status, r.outcome, r.started_at, r.ended_at, r.duration_ms, r.summary, t.title "
                            "FROM runs r LEFT JOIN tasks t ON t.id=r.task_id ORDER BY r.queued_at DESC LIMIT 30").fetchall()
        out["activity"] = {"recent": [{"title": _line(r[8], TITLE_MAX) or "(việc không tiêu đề)", "agent": _line(names.get(r[1], r[1]), 60),
                                       "status": _line(r[3] or r[2], 30), "started_at": _line(r[4] or "", 40),
                                       "duration_s": round((r[6] or 0) / 1000), "summary": _summary(r[7])} for r in rows]}
    return out


def build_daily(hub: "Hub", day: str, *, activity: bool) -> dict[str, Any]:
    """Báo cáo một ngày (giờ máy) — RPT-01..04."""
    d0 = datetime.strptime(day, "%Y-%m-%d").astimezone()
    a, b = d0.astimezone(timezone.utc).isoformat(), (d0 + timedelta(days=1)).astimezone(timezone.utc).isoformat()
    conn = hub.store.conn
    names = {x["id"]: x.get("name") or x["id"] for x in hub.store.list_agents()}
    runs = conn.execute("SELECT r.id, r.agent_id, r.status, r.outcome, r.started_at, r.ended_at, r.cost_usd, r.summary, t.title, r.task_id "
                        "FROM runs r LEFT JOIN tasks t ON t.id=r.task_id WHERE r.queued_at>=? AND r.queued_at<? ORDER BY r.queued_at",
                        (a, b)).fetchall()
    hours = sorted({datetime.fromisoformat(r[4]).astimezone().hour for r in runs if r[4]})
    outcome = [r[3] for r in runs]
    tasks_done = int(conn.execute("SELECT COUNT(*) FROM tasks WHERE status='done' AND updated_at>=? AND updated_at<?", (a, b)).fetchone()[0] or 0) \
        if _has_col(conn, "tasks", "updated_at") else None
    stuck = int(conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('blocked','review')").fetchone()[0] or 0)
    doing = int(conn.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('running','ready','todo')").fetchone()[0] or 0)
    acts: dict[str, int] = {}
    for k, n in conn.execute("SELECT kind, COUNT(*) FROM audit_log WHERE ts>=? AND ts<? AND actor IN ('user','human') GROUP BY kind", (a, b)).fetchall():
        acts[str(k)[:60]] = int(n)
    appr = int(conn.execute("SELECT COUNT(*) FROM approval_requests WHERE opened_at>=? AND opened_at<?", (a, b)).fetchone()[0] or 0) \
        if _has_table(conn, "approval_requests") else 0
    out: dict[str, Any] = {
        "schema": 1, "user": AC.os_user(), "day": day, "generated_ts": time.time(), "level": "activity" if activity else "metrics",
        "runs": {"total": len(runs), "completed": sum(o == "completed" for o in outcome), "failed": sum(o in FAILED for o in outcome),
                 "cancelled": sum(o == "cancelled" for o in outcome)},
        "tasks": {"done": tasks_done, "in_progress": doing, "stuck": stuck},
        "agents_used": sorted({x for x in (_line(names.get(r[1], r[1]), 60) for r in runs) if x})[:50],
        "active_hours": hours, "approvals": appr, "cost_usd": round(sum(float(r[6] or 0) for r in runs), 4), "human_actions": acts,
    }
    if activity:
        out["items"] = [{"title": _line(r[8], TITLE_MAX) or "(việc không tiêu đề)", "agent": _line(names.get(r[1], r[1]), 60),
                         "status": _line(r[3] or r[2], 30), "start": _line(r[4] or "", 40), "end": _line(r[5] or "", 40),
                         "cost_usd": round(float(r[6] or 0), 4), "summary": _summary(r[7])} for r in runs[:ITEMS_MAX]]
    return out


def _has_col(conn: Any, table: str, col: str) -> bool:
    try:
        return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})").fetchall())
    except Exception:  # best-effort: bảng không có
        return False


def _has_table(conn: Any, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _write(path: Path, data: dict[str, Any], group: str | None) -> None:
    from .admin_overview import write_atomic

    write_atomic(path.parent, path.name, data, group=group)


class ProfileExporter:
    """Gắn vào `AdminExporter.export_once` (cùng vòng, cùng luồng SQLite)."""

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.last_profile = 0.0
        self.reported: set[str] = set()

    def tick(self, status_dir: Path, group: str | None, *, now: datetime | None = None) -> None:
        user = AC.os_user()
        udir = status_dir / user
        if not udir.is_dir() or udir.is_symlink():
            return  # chưa được cấp thư mục riêng (bố cục mới) → không xuất hồ sơ
        vis = AC.visibility()
        now = now or datetime.now()
        if time.time() - self.last_profile >= PROFILE_EVERY_S:
            _write(udir / "profile.json", build_profile(self.hub, activity=vis["activity"], now=now), group)
            self.last_profile = time.time()
        rdir = AC.shared_root() / "report" / user
        if not rdir.is_dir() or rdir.is_symlink():
            return
        days = [(now - timedelta(days=1)).strftime("%Y-%m-%d")]
        if (now.hour, now.minute) >= REPORT_AT:
            days.append(now.strftime("%Y-%m-%d"))
        for day in days:
            if day in self.reported or (rdir / f"{day}.json").exists():
                self.reported.add(day)
                continue
            _write(rdir / f"{day}.json", build_daily(self.hub, day, activity=vis["activity"]), group)
            self.reported.add(day)


# ============================================================================ đọc (phía admind)
def _read(path: Path, owner: str) -> dict[str, Any] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            return None
        raw = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    from .admin_overview import _owner_name

    if (_owner_name(st.st_uid) or owner) != owner:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) and obj.get("user") == owner else None


def clean_profile(p: dict[str, Any]) -> dict[str, Any]:
    def lst(v: Any, f: Any, n: int) -> list[Any]:
        return [x for x in (f(i) for i in (v if isinstance(v, list) else [])[:n]) if x]

    def agent(a: Any) -> dict[str, Any] | None:
        if not isinstance(a, dict):
            return None
        return {"name": _line(a.get("name"), 60), "role": _line(a.get("role"), 60), "model": _line(a.get("model"), 40),
                "trust": "full" if a.get("trust") == "full" else "normal", "status": _line(a.get("status"), 20),
                "budget_usd": _num(a.get("budget_usd")), "mcp": lst(a.get("mcp"), lambda m: _line(m, 40), 30)}

    def money(k: str) -> Any:
        return lambda x: {k: _line(x.get(k), 60), "usd": _num(x.get("usd"))} if isinstance(x, dict) else None

    def recent(x: Any) -> dict[str, Any] | None:
        if not isinstance(x, dict):
            return None
        return {"title": _line(x.get("title"), TITLE_MAX), "agent": _line(x.get("agent"), 60), "status": _line(x.get("status"), 30),
                "started_at": _line(x.get("started_at"), 40), "duration_s": _int(x.get("duration_s")), "summary": _line(x.get("summary"), SUMMARY_MAX)}

    out: dict[str, Any] = {
        "schema": _int(p.get("schema")), "generated_ts": _num(p.get("generated_ts")),
        "infra": {"workspace_root": _line((p.get("infra") or {}).get("workspace_root"), 200)},
        "access": {"trusted_devices": _int((p.get("access") or {}).get("trusted_devices"))},
        "agents": lst(p.get("agents"), agent, 100),
        "tasks": {str(k)[:20]: _int(v) for k, v in list((p.get("tasks") or {}).items())[:20] if _int(v) is not None}
        if isinstance(p.get("tasks"), dict) else {},
        "cost_by_agent": lst(p.get("cost_by_agent"), money("name"), 30), "cost_by_model": lst(p.get("cost_by_model"), money("model"), 20),
    }
    act = p.get("activity")
    if isinstance(act, dict):
        out["activity"] = {"recent": lst(act.get("recent"), recent, 30)}
    return out


def clean_report(r: dict[str, Any]) -> dict[str, Any]:
    def sub(d: Any, keys: tuple[str, ...]) -> dict[str, Any]:
        d = d if isinstance(d, dict) else {}
        return {k: _int(d.get(k)) for k in keys}

    items = []
    for x in (r.get("items") if isinstance(r.get("items"), list) else [])[:ITEMS_MAX]:
        if isinstance(x, dict):
            items.append({"title": _line(x.get("title"), TITLE_MAX), "agent": _line(x.get("agent"), 60), "status": _line(x.get("status"), 30),
                          "start": _line(x.get("start"), 40), "end": _line(x.get("end"), 40), "cost_usd": _num(x.get("cost_usd")),
                          "summary": _line(x.get("summary"), SUMMARY_MAX)})
    acts = r.get("human_actions") if isinstance(r.get("human_actions"), dict) else {}
    out = {"day": _line(r.get("day"), 10), "level": "activity" if r.get("level") == "activity" else "metrics",
           "runs": sub(r.get("runs"), ("total", "completed", "failed", "cancelled")), "tasks": sub(r.get("tasks"), ("done", "in_progress", "stuck")),
           "agents_used": [x for x in (_line(a, 60) for a in (r.get("agents_used") or [])[:50] if isinstance(r.get("agents_used"), list)) if x],
           "active_hours": [h for h in (r.get("active_hours") or []) if isinstance(h, int) and 0 <= h <= 23][:24],
           "approvals": _int(r.get("approvals")), "cost_usd": _num(r.get("cost_usd")),
           "human_actions": {str(k)[:60]: _int(v) for k, v in list(acts.items())[:50] if _int(v) is not None}}
    if out["level"] == "activity":
        out["items"] = items
    return out


def read_profile(user: str) -> dict[str, Any] | None:
    if not AC.USER_RE.match(user or ""):
        return None
    p = _read(AC.shared_root() / "status" / user / "profile.json", user)
    return clean_profile(p) if p else None


def read_report(user: str, day: str) -> dict[str, Any] | None:
    if not AC.USER_RE.match(user or "") or not (len(day) == 10 and day[4] == "-" and day[7] == "-" and day.replace("-", "").isdigit()):
        return None
    r = _read(AC.shared_root() / "report" / user / f"{day}.json", user)
    return clean_report(r) if r else None
