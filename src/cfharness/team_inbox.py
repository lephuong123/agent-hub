"""Hộp thư "Việc được giao" từ Team user của Super Admin Console (FR-SA-TEAM-06, 07, 09; plan-superadmin B3).

- admind ghi `teams/<team>/inbox/<u>/<id>.json` (thư mục chủ = u, 0500; file 0400 — hub CHỈ ĐỌC). Hub quét mỗi `SCAN_S`.
- Việc mới → event `team.inbox_new` (web/Telegram/Slack báo cho NGƯỜI). Không có gì tự tới agent: người bấm **Nhận** (chọn agent hoặc điều
  phối viên) → hub tạo task; **Từ chối** kèm lý do; **Nộp** kèm đường dẫn kết quả. Đây là ngoại lệ duy nhất của "không chữ tự do từ quản trị
  tới agent" (BR-SA-02) và luôn đi qua quyết định của người.
- Trạng thái ghi về `teams/<team>/status/<u>/status.json` (file của chính hub) — admind đối chiếu, bảng team cập nhật ≤ 1 phút.
RPC (∈ HUMAN_ONLY, không MANAGER_ALLOWED): `inbox.list`, `inbox.accept {id, agent?}`, `inbox.reject {id, reason}`, `inbox.submit {id, result_path, note}`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import admin_control as AC
from .errors import HubError

if TYPE_CHECKING:
    from .daemon import Daemon
    from .events import HubEvent
    from .hub import Hub

log = logging.getLogger("cfharness.team_inbox")

SCAN_S = 30.0
MAX_FILE = 64 * 1024
MAX_ITEMS = 300
ID_RE = re.compile(r"^tt_[0-9a-f]{8,32}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
STATE_KEY = "team_inbox"
METHODS = ("inbox.list", "inbox.accept", "inbox.reject", "inbox.submit")
MUTATING_INBOX = ("inbox.accept", "inbox.reject", "inbox.submit")
TITLE_MAX, BODY_MAX = 200, 8000


def _text(v: Any, n: int) -> str:
    from .termsafe import clean

    return clean(v)[:n] if isinstance(v, str) else ""


def read_item(path: Path, me: str) -> dict[str, Any] | None:
    """File do admind (root) ghi → kiểm: file thường, ≤ 64 KB, không symlink; lọc trường."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE:
            return None
        if hasattr(os, "getuid") and st.st_uid not in (0, os.getuid()):  # type: ignore[attr-defined]
            return None
        raw = os.read(fd, MAX_FILE + 1)
    finally:
        os.close(fd)
    try:
        d = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(d, dict) or not ID_RE.match(str(d.get("id") or "")) or d.get("assignee") != me:
        return None
    return {"id": d["id"], "team": _text(d.get("team_name"), 80), "team_slug": str(d.get("team") or ""), "title": _text(d.get("title"), TITLE_MAX),
            "body": _text(d.get("description"), BODY_MAX), "due": _text(d.get("due"), 20), "priority": _text(d.get("priority"), 10),
            "from": _text(d.get("from"), 80), "created_ts": float(d.get("created_ts") or 0) if isinstance(d.get("created_ts"), (int, float)) else 0.0,
            "review_note": _text(d.get("review_note"), 1000), "state_admin": _text(d.get("state"), 20)}


class TeamInbox:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._task: asyncio.Task[Any] | None = None
        hub.bus.add_listener(self._on_event)

    # ------------------------------------------------------------------ đọc
    def root(self) -> Path:
        return AC.shared_root() / "teams"

    def items(self) -> list[dict[str, Any]]:
        me = AC.os_user()
        out: list[dict[str, Any]] = []
        r = self.root()
        try:
            slugs = sorted(x for x in os.listdir(r) if SLUG_RE.match(x))
        except OSError:
            return out
        for slug in slugs:
            d = r / slug / "inbox" / me
            try:
                names = sorted(x for x in os.listdir(d) if x.endswith(".json") and ID_RE.match(x[:-5]))
            except OSError:
                continue
            for n in names[:MAX_ITEMS]:
                it = read_item(d / n, me)
                if it and it["id"] == n[:-5]:
                    it["team_slug"] = slug
                    out.append(it)
        st = self.state()
        for it in out:
            it.update({k: v for k, v in (st.get(it["id"]) or {}).items() if k in ("state", "hub_task_id", "agent", "result_path", "reason", "note")})
            it.setdefault("state", "new")
            if it.get("hub_task_id"):
                try:
                    it["hub_task_status"] = self.hub.tasks.get(it["hub_task_id"]).get("status")
                except Exception:  # best-effort: task đã xoá
                    it["hub_task_status"] = None
        return out

    def state(self) -> dict[str, Any]:
        return dict(self.hub.store.state_get(STATE_KEY) or {})

    def _save(self, st: dict[str, Any]) -> None:
        self.hub.store.state_set(STATE_KEY, st)

    def _item(self, tid: str) -> dict[str, Any]:
        if not ID_RE.match(tid or ""):
            raise HubError("id việc không hợp lệ", code=-32602)
        for it in self.items():
            if it["id"] == tid:
                return it
        raise HubError("không có việc này trong hộp thư", code=-32004)

    # ------------------------------------------------------------------ hành động của người
    def accept(self, tid: str, agent: str | None) -> dict[str, Any]:
        it = self._item(tid)
        if it.get("state") not in ("new", "returned"):
            raise HubError(f"việc đang ở trạng thái {it.get('state')}")
        ag = agent or (self.hub.manager.agent_id() if getattr(self.hub, "manager", None) else None)
        if not ag:
            raise HubError("chọn agent nhận việc (hub chưa có điều phối viên)")
        body = it["body"] + (f"\n\nHạn: {it['due']}" if it.get("due") else "") + (f"\n\nNhận xét của trưởng team: {it['review_note']}"
                                                                                   if it.get("review_note") else "")
        task = self.hub.tasks.create({"title": it["title"] or "Việc team", "body": body, "assignee": ag,
                                      "idempotency_key": f"teamtask:{tid}:{int(time.time())}"}, created_by="human")
        st = self.state()
        st[tid] = {"state": "doing", "hub_task_id": task["id"], "agent": ag, "team": it["team_slug"], "ts": time.time()}
        self._save(st)
        self.write_status()
        return {"task_id": task["id"]}

    def reject(self, tid: str, reason: str) -> dict[str, Any]:
        it = self._item(tid)
        if it.get("state") not in ("new", "returned"):
            raise HubError(f"việc đang ở trạng thái {it.get('state')}")
        reason = _text(reason, 500).strip()
        if not reason:
            raise HubError("cần lý do từ chối")
        st = self.state()
        st[tid] = {"state": "rejected", "reason": reason, "team": it["team_slug"], "ts": time.time()}
        self._save(st)
        self.write_status()
        return {"ok": True}

    def submit(self, tid: str, result_path: str, note: str) -> dict[str, Any]:
        it = self._item(tid)
        if it.get("state") not in ("doing",):
            raise HubError("chỉ nộp được việc đang làm")
        st = self.state()
        cur = dict(st.get(tid) or {})
        cur.update({"state": "submitted", "result_path": _text(result_path, 500), "note": _text(note, 1000), "ts": time.time()})
        st[tid] = cur
        self._save(st)
        self.write_status()
        return {"ok": True}

    # ------------------------------------------------------------------ đồng bộ
    def sync(self) -> int:
        """Quét hộp thư: việc mới → event; việc admind trả lại (state_admin=returned) → về 'returned'; ghi file trạng thái."""
        items = self.items()
        st = self.state()
        changed = False
        for it in items:
            cur = st.get(it["id"])
            if cur is None:
                st[it["id"]] = {"state": "new", "team": it["team_slug"], "ts": time.time()}
                changed = True
                from .events import make_event

                self.hub.publish(make_event("team.inbox_new", None, {"id": it["id"], "team": it["team_slug"]}))
            elif it.get("state_admin") == "returned" and cur.get("state") == "submitted":
                cur["state"] = "returned"
                changed = True
            elif it.get("state_admin") == "done" and cur.get("state") != "done":
                cur["state"] = "done"
                changed = True
        if changed:
            self._save(st)
        self.write_status(items)
        return len(items)

    def write_status(self, items: list[dict[str, Any]] | None = None) -> None:
        me = AC.os_user()
        items = items if items is not None else self.items()
        by: dict[str, dict[str, Any]] = {}
        for it in items:
            prog = None
            if it.get("hub_task_status") == "done":
                prog = 100
            by.setdefault(it["team_slug"], {})[it["id"]] = {k: it.get(k) for k in ("state", "hub_task_id", "hub_task_status", "result_path",
                                                                                     "reason", "note")} | {"progress": prog}
        for slug, tasks in by.items():
            d = self.root() / slug / "status" / me
            if not d.is_dir() or d.is_symlink():
                continue
            from .admin_overview import write_atomic

            try:
                write_atomic(d, "status.json", {"schema": 1, "user": me, "updated_ts": time.time(), "tasks": tasks}, group=AC.ADMIN_GROUP)
            except OSError:
                log.debug("team_inbox: không ghi được trạng thái team %s", slug, exc_info=True)

    # ------------------------------------------------------------------ vòng nền
    def _on_event(self, ev: "HubEvent") -> None:
        try:
            if ev.type == "hub.started":
                self._task = asyncio.get_running_loop().create_task(self._loop(), name="team-inbox")
            elif ev.type == "hub.stopping" and self._task:
                self._task.cancel()
        except RuntimeError:  # best-effort: không có loop (Hub dùng đồng bộ trong test/CLI)
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                if self.root().is_dir():
                    self.sync()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover — vòng nền không được chết
                log.exception("team_inbox lỗi")
            await asyncio.sleep(SCAN_S)


def attach(hub: "Hub") -> None:
    hub.team_inbox = TeamInbox(hub)  # type: ignore[attr-defined]


def register_rpc(daemon: "Daemon") -> None:
    from .mcp_server.rpc import wrap_with_audit

    hub = daemon.hub
    ib: TeamInbox = hub.team_inbox  # type: ignore[attr-defined]

    async def lst(p: dict[str, Any]) -> dict[str, Any]:
        return {"items": await asyncio.to_thread(ib.items)}

    async def accept(p: dict[str, Any]) -> dict[str, Any]:
        return ib.accept(str(p.get("id") or ""), (str(p["agent"]) if p.get("agent") else None))

    async def reject(p: dict[str, Any]) -> dict[str, Any]:
        return ib.reject(str(p.get("id") or ""), str(p.get("reason") or ""))

    async def submit(p: dict[str, Any]) -> dict[str, Any]:
        return ib.submit(str(p.get("id") or ""), str(p.get("result_path") or ""), str(p.get("note") or ""))

    daemon.methods["inbox.list"] = lst
    for name, fn in (("inbox.accept", accept), ("inbox.reject", reject), ("inbox.submit", submit)):
        daemon.methods[name] = wrap_with_audit(hub, name, fn)
