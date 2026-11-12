"""Luồng việc (workstream) của điều phối viên — mỗi epic/việc lớn một luồng (docs/dev-notes/workstreams.md).

Vấn đề: một sprint nhiều epic, người nhắn hết vào một hội thoại → các epic chen nhau, session xoay chỉ mang theo 6 tin cuối nên
điều phối viên "quên" epic đang nói, kết quả tick đổ vào hội thoại bất kỳ. Luồng = đơn vị trên task:

- `workstreams` (migration v24): `id ws_…`, `key` (mã epic, vd. LH-55166 — tra không phân biệt hoa thường), `title`, `sprint`,
  `status active|closed`, `state_md` = STATE.md của luồng (quyết định, PR, câu hỏi mở — điều phối viên giữ cập nhật, người sửa được).
- Hội thoại (`manager_conversations.workstream_id`) và task (`tasks.workstream_id`, task con kế thừa từ task gốc) gắn vào luồng.
  Hội thoại không gắn luồng = **sảnh**.
- Session mới (hội thoại mới / xoay session) của hội thoại trong luồng được nạp `context_block` (STATE + task đang mở) thay vì chỉ
  vài tin cuối; sảnh được nạp danh sách luồng đang mở để gán việc đúng luồng.
- Tick gom sự kiện theo luồng (service.py), thông báo của task trong luồng về hội thoại của luồng (`home_conversation`).
"""

from __future__ import annotations

import re
import secrets
from typing import TYPE_CHECKING, Any

from ..errors import Conflict, HubError, NotFound
from ..events import make_event
from ..store import now_iso

if TYPE_CHECKING:
    from .service import ManagerService

STATE_MAX = 12000
TITLE_MAX = 120
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,39}$")
STATUSES = ("active", "closed")
# Tên đặt sẵn khi người mở luồng không nhập tên
RANDOM_WORDS = ("Mây", "Sao", "Gió", "Sen", "Trúc", "Tùng", "Cam", "Lam", "Ngọc", "Đào", "Biển", "Núi", "Sông", "Nắng", "Trăng", "Hồng")
# Tên bắt đầu bằng mã Jira ("LH-55166 Monitor", "LH-55166: Monitor") → tách mã ra `key`
LEAD_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]+-\d+)(?:\s*[:·|–—-]\s*|\s+|$)(.*)$")


def split_key(title: str) -> tuple[str | None, str]:
    m = LEAD_KEY_RE.match(title.strip())
    if not m:
        return None, title.strip()
    return m.group(1).upper(), m.group(2).strip()
OPEN_TASK = ("triage", "todo", "ready", "running", "review", "blocked")


class Workstreams:
    def __init__(self, svc: "ManagerService") -> None:
        self.svc = svc
        self.hub = svc.hub

    @property
    def conn(self):
        return self.hub.store.conn

    # ------------------------------------------------------------------ đọc
    def row(self, ref: Any) -> dict[str, Any]:
        wid = self.hub.tasks.resolve_workstream(ref)
        if wid is None:
            raise HubError("thiếu luồng việc (id ws_… hoặc mã, vd. LH-55166)")
        return dict(self.conn.execute("SELECT * FROM workstreams WHERE id=?", (wid,)).fetchone())

    def maybe(self, ref: Any) -> dict[str, Any] | None:
        try:
            return self.row(ref) if ref else None
        except (NotFound, HubError):
            return None

    def label(self, w: dict[str, Any]) -> str:
        return f"{w['key']} · {w['title']}" if w.get("key") else str(w["title"])

    def _counts(self, wid: str) -> dict[str, int]:
        out = {s: 0 for s in ("open", "running", "blocked", "done")}
        for st, n in self.conn.execute("SELECT status, COUNT(*) FROM tasks WHERE workstream_id=? AND deleted_at IS NULL AND status != 'archived' "
                                       "GROUP BY status", (wid,)):
            if st == "done":
                out["done"] += n
            else:
                out["open"] += n
                if st == "running":
                    out["running"] += n
                if st == "blocked":
                    out["blocked"] += n
        out["total"] = out["open"] + out["done"]
        return out

    def _needs(self, wid: str) -> int:
        """Việc cần NGƯỜI trong luồng: câu hỏi đang mở cho người (hội thoại hoặc task của luồng) + task bị chặn."""
        n = self.conn.execute(
            "SELECT COUNT(*) FROM questions q WHERE q.status='open' AND (q.conversation_id IN (SELECT id FROM manager_conversations WHERE workstream_id=?) "
            "OR q.task_id IN (SELECT id FROM tasks WHERE workstream_id=?))", (wid, wid)).fetchone()[0]
        return int(n)

    def out(self, w: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
        d = {k: w.get(k) for k in ("id", "key", "title", "sprint", "status", "created_by", "created_at", "updated_at", "closed_at", "state_updated_at", "state_by")}
        d["label"] = self.label(w)
        d["tasks"] = self._counts(w["id"])
        d["needs_human"] = self._needs(w["id"])
        convs = self.conn.execute("SELECT id, channel, title, updated_at FROM manager_conversations WHERE workstream_id=? AND archived_at IS NULL "
                                  "AND channel != 'heartbeat' ORDER BY updated_at DESC", (w["id"],)).fetchall()
        d["conversations"] = [dict(c) for c in convs] if full else len(convs)
        last = max([w.get("updated_at") or ""] + [c["updated_at"] or "" for c in convs])
        d["last_activity"] = last or None
        if full:
            d["state"] = w.get("state_md") or ""
            d["open_tasks"] = [{k: t.get(k) for k in ("id", "title", "status", "assignee_agent_id", "team_id", "block_reason", "updated_at")}
                               for t in self.hub.tasks.list(workstream=w["id"], status=list(OPEN_TASK), limit=50)]
            d["recent_done"] = [{k: t.get(k) for k in ("id", "title", "result_summary", "done_at")}
                                for t in self.hub.tasks.list(workstream=w["id"], status="done", limit=200)[-10:]]
        return d

    def list(self, status: str | None = "active", *, limit: int = 100) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM workstreams", []
        if status and status != "all":
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(int(limit))
        rows = [self.out(dict(r)) for r in self.conn.execute(sql, args)]
        rows.sort(key=lambda x: x.get("last_activity") or "", reverse=True)
        return rows

    def get(self, ref: Any) -> dict[str, Any]:
        return self.out(self.row(ref), full=True)

    # ------------------------------------------------------------------ ghi
    def _check_key(self, key: Any, *, exclude: str | None = None) -> str | None:
        key = str(key or "").strip()
        if not key:
            return None
        if not KEY_RE.match(key):
            raise HubError(f"mã luồng '{key[:40]}' không hợp lệ (chữ/số/._-/, ≤ 40 ký tự, vd. LH-55166)")
        if key.startswith("ws_"):
            raise HubError("mã luồng không được bắt đầu bằng 'ws_'")
        r = self.conn.execute("SELECT id FROM workstreams WHERE lower(key)=lower(?) AND status='active' AND id != ?", (key, exclude or "")).fetchone()
        if r:
            raise Conflict(f"đã có luồng đang mở với mã '{key}' ({r[0]})")
        return key

    def _publish(self, w: dict[str, Any], action: str, by: str) -> None:
        self.hub.publish(make_event("workstream.updated", self.svc.agent_id(), {"workstream_id": w["id"], "key": w.get("key"), "title": w.get("title"),
                                                                               "status": w.get("status"), "action": action, "by": by}))

    def create(self, title: Any, *, key: Any = None, sprint: Any = None, state: Any = None, by: str = "user") -> dict[str, Any]:
        title = " ".join(str(title or "").split())[:TITLE_MAX]
        if not key and title:
            k, rest = split_key(title)
            if k:
                key, title = k, rest
        key = self._check_key(key)
        if not title:
            title = key or self._random_title()
        wid = "ws_" + secrets.token_hex(4)
        now = now_iso()
        st = str(state or "")[:STATE_MAX] or None
        self.conn.execute("INSERT INTO workstreams (id, key, title, sprint, status, state_md, state_updated_at, state_by, created_by, created_at, updated_at) "
                          "VALUES (?,?,?,?, 'active', ?,?,?,?,?,?)",
                          (wid, key, title, (str(sprint).strip()[:40] or None) if sprint else None, st, now if st else None, by if st else None, by, now, now))
        w = self.row(wid)
        self._publish(w, "created", by)
        return self.out(w, full=True)

    def _random_title(self) -> str:
        used = {r[0] for r in self.conn.execute("SELECT title FROM workstreams WHERE status='active'")}
        for _ in range(50):
            t = f"Luồng {secrets.choice(RANDOM_WORDS)} {secrets.randbelow(90) + 10}"
            if t not in used:
                return t
        return "Luồng " + secrets.token_hex(2)

    def update(self, ref: Any, changes: dict[str, Any], *, by: str = "user") -> dict[str, Any]:
        w = self.row(ref)
        sets: dict[str, Any] = {}
        if "title" in changes and changes["title"] is not None:
            t = " ".join(str(changes["title"]).split())[:TITLE_MAX]
            if "key" not in changes:  # đổi tên kiểu "LH-1 Tên mới" → cập nhật cả mã
                k, rest = split_key(t)
                if k:
                    sets["key"] = self._check_key(k, exclude=w["id"])
                    t = rest or k
            if not t:
                raise HubError("tên luồng không được rỗng")
            sets["title"] = t
        if "key" in changes:
            sets["key"] = self._check_key(changes["key"], exclude=w["id"])
        if "sprint" in changes:
            sets["sprint"] = str(changes["sprint"] or "").strip()[:40] or None
        if changes.get("status") is not None:
            s = str(changes["status"])
            if s not in STATUSES:
                raise HubError("status phải là active|closed")
            if s == "active" and w["status"] != "active" and w.get("key"):
                self._check_key(w["key"], exclude=w["id"])
            sets["status"] = s
            sets["closed_at"] = now_iso() if s == "closed" else None
        if "state" in changes and changes["state"] is not None:
            sets["state_md"] = str(changes["state"])[:STATE_MAX]
            sets["state_updated_at"] = now_iso()
            sets["state_by"] = by
        if not sets:
            return self.out(w, full=True)
        sets["updated_at"] = now_iso()
        self.conn.execute(f"UPDATE workstreams SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), w["id"]))
        w = self.row(w["id"])
        action = "closed" if sets.get("status") == "closed" else "reopened" if sets.get("status") == "active" else "state" if set(sets) <= {"state_md", "state_updated_at", "state_by", "updated_at"} else "updated"
        self._publish(w, action, by)
        return self.out(w, full=True)

    def set_state(self, ref: Any, text: Any, *, by: str = "user") -> dict[str, Any]:
        return self.update(ref, {"state": str(text or "")}, by=by)

    def attach_conversation(self, cid: str, ref: Any, *, by: str = "user") -> dict[str, Any]:
        """Gắn hội thoại vào luồng (`ref` rỗng → về sảnh). Hội thoại nhịp (heartbeat) không gắn tay được."""
        c = self.svc._conv_row(cid)
        if c["channel"] == "heartbeat":
            raise HubError("hội thoại nhịp tự động không gắn luồng bằng tay")
        wid = self.row(ref)["id"] if ref else None
        self.conn.execute("UPDATE manager_conversations SET workstream_id=? WHERE id=?", (wid, cid))
        if wid:
            self.conn.execute("UPDATE workstreams SET updated_at=? WHERE id=?", (now_iso(), wid))
            self._publish(self.row(wid), "conversation", by)
        return self.svc._conv_out(self.svc._conv_row(cid))

    def touch(self, wid: str | None) -> None:
        if wid:
            self.conn.execute("UPDATE workstreams SET updated_at=? WHERE id=?", (now_iso(), wid))

    # ------------------------------------------------------------------ định tuyến
    def of_conversation(self, cid: Any) -> str | None:
        if not cid:
            return None
        r = self.conn.execute("SELECT workstream_id FROM manager_conversations WHERE id=?", (str(cid),)).fetchone()
        return str(r[0]) if r and r[0] else None

    def of_task(self, task_id: Any) -> str | None:
        if not task_id:
            return None
        r = self.conn.execute("SELECT workstream_id FROM tasks WHERE id=?", (str(task_id),)).fetchone()
        return str(r[0]) if r and r[0] else None

    def home_conversation(self, wid: str, *, create: bool = True) -> dict[str, Any] | None:
        """Hội thoại của NGƯỜI trong luồng hoạt động gần nhất (điều phối viên hiện tại). Chưa có → tạo hội thoại web cho luồng (trang
        Điều phối là nơi xem luồng) khi `create`."""
        aid = self.svc.agent_id()
        if not aid:
            return None
        r = self.conn.execute("SELECT id FROM manager_conversations WHERE workstream_id=? AND agent_id=? AND channel != 'heartbeat' "
                              "AND archived_at IS NULL ORDER BY updated_at DESC LIMIT 1", (wid, aid)).fetchone()
        if r:
            return self.svc._conv_row(r[0])
        if not create:
            return None
        w = self.maybe(wid)
        if w is None:
            return None
        c = self.svc._new_conv("web", aid, self.label(w), workstream_id=wid)
        return c

    # ------------------------------------------------------------------ ngữ cảnh cho prompt (tiếng Anh)
    def context_block(self, wid: str, key: str) -> str:
        """Khối đầu session mới của hội thoại trong luồng: luồng là gì, STATE.md (ghi chú của điều phối viên/người — DỮ LIỆU), task mở."""
        from .prompts import frame

        w = self.maybe(wid)
        if w is None:
            return ""
        d = self.out(w, full=True)
        lines = [f"Workstream: {d['label']} (id {w['id']}" + (f", sprint {w['sprint']}" if w.get("sprint") else "") + f", status {w['status']})."]
        state = (w.get("state_md") or "").strip()
        tasks = [f"- {t['id']} [{t['status']}] {t['title']}" + (f" (blocked: {t['block_reason']})" if t.get("block_reason") else "")
                 for t in d["open_tasks"][:25]]
        done = [f"- {t['id']} done: {t['title']}" for t in d["recent_done"][-5:]]
        body = ["STATE.md (notes kept by you and the owner):", state or "(empty — write it with workstream_state_set)", "", "Open tasks:"]
        body += tasks or ["(none)"]
        if done:
            body += ["Recently done:"] + done
        return (f"[cfharness · key {key}] This conversation belongs to a WORKSTREAM. " + lines[0] + "\n" + frame(body, key) + "\n"
                "Everything the owner says here is about this workstream unless they name another one. Tasks you create here are filed "
                "under it automatically. Keep STATE.md current with workstream_state_set (short: goal, decisions, branches/PRs, open "
                "questions, next step) whenever something important changes, so a fresh session can continue from it.\n\n")

    def lobby_block(self, key: str) -> str:
        """Khối đầu session mới của hội thoại SẢNH (không gắn luồng): danh sách luồng đang mở để gán việc đúng chỗ."""
        from .prompts import frame

        ws = self.list("active", limit=30)
        if not ws:
            return ""
        lines = [f"- {w['key'] or w['id']}: {w['title']}" + (f" (sprint {w['sprint']})" if w.get("sprint") else "")
                 + f" — tasks {w['tasks']['done']}/{w['tasks']['total']} done" + (f", {w['tasks']['blocked']} blocked" if w["tasks"]["blocked"] else "")
                 for w in ws]
        return (f"[cfharness · key {key}] This is the LOBBY conversation (not tied to a workstream). Open workstreams:\n" + frame(lines, key) + "\n"
                "When the owner's request belongs to one of them, pass workstream=<key> to task_create and say in one line which "
                "workstream you filed it under. For a new epic/large piece of work, workstream_create it first. To move THIS conversation "
                "into a workstream (the owner says it is about one), use conversation_set_workstream.\n\n")
