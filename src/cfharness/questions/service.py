"""Câu hỏi có lựa chọn của agent (`hub.questions`) — một nguồn sự thật cho Telegram, web, CLI, TUI.

Vòng đời: `with_manager → open → answered | cancelled` (KHÔNG có `expired`: không bao giờ tự trả lời thay người).
- Agent trong TASK hỏi (`ask_human` hub-self) → `with_manager` nếu có điều phối viên sẵn sàng (`questions.via_manager`); điều phối
  viên trả lời (`question.answer`, chỉ khi `with_manager`) hoặc chuyển lên người (`question.escalate`). Quá `manager_timeout_s`,
  manager bị huỷ chỉ định/tạm dừng, hoặc ESTOP → tự chuyển lên người.
- Câu hỏi của chính điều phối viên (`ask_owner`) và agent ngoài task → `open` (người) ngay.
- Trả lời → nối tiếp công việc: task → bình luận `answer` + gỡ blocked (dispatcher chạy lại, prompt worker có bình luận);
  hội thoại điều phối viên → `manager.chat` (tin của người); run thường → `run.continue`. Nếu run hỏi còn đang chạy, việc nối tiếp
  chờ tới khi run/task kết thúc (listener bus).
- Nội dung câu hỏi/nhãn lựa chọn là DỮ LIỆU của agent (đã che secret, bỏ ký tự điều khiển) — kênh hiển thị tự lo khung an toàn.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
import unicodedata
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..errors import Conflict, HubError, NotFound
from ..events import TERMINAL_RUN_EVENTS, HubEvent, make_event
from ..store import now_iso

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.questions")

PENDING = ("with_manager", "open")
FINAL = ("answered", "cancelled")
MAX_OPTIONS = 6
ALLOW_KEY = "1"  # xin phép: phương án 1 = "Cho phép" (manager/owner_actions.OPTIONS)
TEXT_MAX = 2000
LABEL_MAX = 80
DESC_MAX = 200
ANSWER_MAX = 4000
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def clean(text: Any, limit: int, *, oneline: bool = False) -> str:
    """Che secret + bỏ ký tự điều khiển/định dạng vô hình (bidi, zero-width) — nhãn không giả được bố cục ở kênh hiển thị."""
    from ..redact import redact_str

    s = redact_str(str(text if text is not None else ""))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")
    s = _CTRL.sub(" ", s.replace("\r\n", "\n"))
    if oneline:
        s = " ".join(s.split())
    s = s.strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def normalize_options(raw: Any) -> list[dict[str, str]]:
    """`[str | {label, description?}]` → `[{key: "1".., label, description}]` (tối đa MAX_OPTIONS, bỏ trùng/rỗng)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise HubError("options phải là danh sách")
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for o in raw:
        if isinstance(o, dict):
            label, desc = o.get("label"), o.get("description")
        else:
            label, desc = o, None
        label = clean(label, LABEL_MAX, oneline=True)
        if not label or label.lower() in seen:
            continue
        seen.add(label.lower())
        out.append({"key": str(len(out) + 1), "label": label, "description": clean(desc, DESC_MAX, oneline=True) if desc else ""})
    if len(out) > MAX_OPTIONS:
        raise HubError(f"tối đa {MAX_OPTIONS} lựa chọn")
    return out


class QuestionService:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._task: asyncio.Task | None = None
        hub.bus.add_listener(self._on_event)

    @property
    def conn(self):
        return self.hub.store.conn

    def cfg(self, key: str, default: Any = None) -> Any:
        try:
            v = self.hub.config.get(f"questions.{key}")
        except KeyError:
            return default
        return default if v is None else v

    # ------------------------------------------------------------------ đọc
    def _row(self, r: Any) -> dict[str, Any] | None:
        d = self.hub.store._row("questions", r)
        if d is not None:
            d["multi"] = bool(d.get("multi"))
            d["allow_free_text"] = bool(d.get("allow_free_text"))
        return d

    def get(self, qid: str) -> dict[str, Any]:
        qid = str(qid or "")
        r = self.conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        if r is None and qid:
            rows = self.conn.execute("SELECT * FROM questions WHERE id LIKE ? LIMIT 2", (qid + "%",)).fetchall()
            r = rows[0] if len(rows) == 1 else None
        if r is None:
            raise NotFound(f"không tìm thấy câu hỏi '{qid}'")
        return self._row(r)  # type: ignore[return-value]

    def maybe(self, qid: str) -> dict[str, Any] | None:
        try:
            return self.get(qid)
        except NotFound:
            return None

    def list(self, status: str | None = "pending", *, agent_id: str | None = None, run_id: str | None = None, task_id: str | None = None,
             routed_to: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where, args = [], []
        if status == "pending":
            where.append("status IN ('with_manager','open')")
        elif status and status != "all":
            where.append("status=?")
            args.append(status)
        for col, v in (("agent_id", agent_id), ("run_id", run_id), ("task_id", task_id), ("routed_to", routed_to)):
            if v:
                where.append(f"{col}=?")
                args.append(v)
        sql = "SELECT * FROM questions" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?"
        args.append(int(limit))
        return [self._row(r) for r in self.conn.execute(sql, args)]  # type: ignore[misc]

    def pending_for_task(self, task_id: str | None) -> list[dict[str, Any]]:
        return self.list("pending", task_id=task_id) if task_id else []

    def counts(self) -> dict[str, int]:
        out = {"open": 0, "with_manager": 0}
        for st, n in self.conn.execute("SELECT status, COUNT(*) FROM questions WHERE status IN ('with_manager','open') GROUP BY status"):
            out[st] = int(n)
        return out

    def _update(self, qid: str, data: dict[str, Any]) -> None:
        self.hub.store._update("questions", "id", qid, data)

    def _event(self, etype: str, q: dict[str, Any], payload: dict[str, Any]) -> None:
        ev = make_event(etype, q["agent_id"], {"question_id": q["id"], **payload}, run_id=q.get("run_id"), session_id=q.get("session_id"))
        ev.task_id = q.get("task_id")
        self.hub.publish(ev)

    def _audit(self, kind: str, q: dict[str, Any], data: dict[str, Any], *, actor: str, channel: str) -> None:
        audit = getattr(self.hub, "audit", None)
        if audit is not None:
            audit.record(kind, q["agent_id"], q.get("run_id"), {"question_id": q["id"], **data}, actor=actor, channel=channel,
                         target=f"question:{q['id']}", summary=f"{kind} {q['id']}")

    # ------------------------------------------------------------------ định tuyến
    def manager_available(self, asking_agent: str | None) -> str | None:
        """Id điều phối viên nếu có thể nhận câu hỏi (được chỉ định, không phải người hỏi, không tạm dừng/xoá, không ESTOP)."""
        mgr = getattr(self.hub, "manager", None)
        mid = mgr.agent_id() if mgr is not None else None
        if not mid or mid == asking_agent or self.hub.runs.estop.get("on"):
            return None
        row = self.hub.store.get_agent(mid)
        if row is None or row.get("archived_at") or row.get("status") == "paused":
            return None
        return mid

    # ------------------------------------------------------------------ mở
    def open(self, run: dict[str, Any], text: Any, options: Any = None, *, multi: bool = False, allow_free_text: bool = True,
             source: str = "ask_tool", conversation_id: str | None = None, task_id: str | None = None,
             action: dict[str, Any] | None = None) -> dict[str, Any]:
        body = clean(text, TEXT_MAX)
        if not body:
            raise HubError("câu hỏi cần nội dung (question)")
        opts = normalize_options(options)
        if not opts:
            multi, allow_free_text = False, True  # không có lựa chọn → chỉ trả lời bằng chữ
        if len(opts) < 2:
            multi = False
        cap = int(self.cfg("max_open_per_run", 3))
        if run.get("id") and len(self.list("pending", run_id=run["id"])) >= cap:
            raise Conflict(f"run này đã có {cap} câu hỏi đang chờ — kết thúc lượt và chờ trả lời")
        task_id = task_id or run.get("task_id")
        from ..guard.profiles import gates_mode

        gated = gates_mode(self.hub.config) != "human"  # công tắc chung: mọi câu hỏi (không chỉ trong task) qua điều phối viên trước
        mid = self.manager_available(run.get("agent_id")) if ((task_id or gated) and self.cfg("via_manager", True)) else None
        qid = "q_" + secrets.token_hex(5)
        row = {
            "id": qid, "agent_id": run["agent_id"], "run_id": run.get("id"), "session_id": run.get("session_id"), "task_id": task_id,
            "conversation_id": conversation_id, "source": source, "text": body, "options": opts, "multi": int(bool(multi)),
            "allow_free_text": int(bool(allow_free_text)), "status": "with_manager" if mid else "open", "routed_to": "manager" if mid else "human",
            "created_at": now_iso(),
        }
        if action is not None:
            # xin phép (manager/owner_actions.py): CHỈ người trả lời — không bao giờ qua điều phối viên
            row.update({"action": action, "status": "open", "routed_to": "human"})
            mid = None
        self.hub.store._insert("questions", row)
        q = self.get(qid)
        self._event("question.open", q, self.wire(q))
        self._audit("question.open", q, {"source": source, "routed_to": q["routed_to"], "options": len(opts)}, actor=f"agent:{run['agent_id']}", channel="agent")
        if mid:
            self._push_manager(q)
        return q

    def wire(self, q: dict[str, Any]) -> dict[str, Any]:
        """Payload chung cho event/kênh hiển thị."""
        return {"routed_to": q["routed_to"], "status": q["status"], "text": q["text"], "options": q.get("options") or [], "multi": q["multi"],
                "allow_free_text": q["allow_free_text"], "source": q["source"], "task_id": q.get("task_id"), "conversation_id": q.get("conversation_id"),
                "answer_cmd": f"cfharness answer {q['id']}", "permission": (q.get("action") or {}).get("kind")}

    def _push_manager(self, q: dict[str, Any]) -> None:
        mgr = getattr(self.hub, "manager", None)
        if mgr is None:
            return
        opts = "; ".join(f"{o['key']}. {o['label']}" for o in q.get("options") or [])
        mgr._push("question", f"q:{q['id']}", f"question {q['id']} from {q['agent_id']} (task {q.get('task_id')}): {q['text'][:400]}"
                  + (f" — options: {opts}" if opts else "") + (" (multiple choice)" if q["multi"] else ""), question_id=q["id"], task_id=q.get("task_id"))

    # ------------------------------------------------------------------ chuyển lên người
    def escalate(self, qid: str, *, by: str = "hub", note: Any = None, channel: str = "hub") -> dict[str, Any]:
        q = self.get(qid)
        if q["status"] != "with_manager":
            raise Conflict(f"câu hỏi {q['id']} đang {q['status']} — chỉ chuyển lên người được khi đang chờ điều phối viên")
        n = clean(note, 600) if note else None
        self._update(q["id"], {"status": "open", "routed_to": "human", "escalated_at": now_iso(), "escalation_note": n})
        q = self.get(q["id"])
        self._event("question.escalated", q, {**self.wire(q), "by": by, "note": n})
        self._audit("question.escalated", q, {"by": by, "note": n}, actor=by if by != "hub" else "hub", channel=channel)
        return q

    # ------------------------------------------------------------------ trả lời
    def answer(self, qid: str, *, keys: Any = None, text: Any = None, by: str = "user", channel: str = "cli", resume: bool = True) -> dict[str, Any]:
        q = self.get(qid)
        if q["status"] in FINAL:
            raise Conflict(f"câu hỏi {q['id']} đã {q['status']}" + (f" (bởi {q.get('answered_by')} qua {q.get('answered_channel')})" if q.get("answered_by") else ""))
        if by.startswith("manager:") and q["status"] != "with_manager":
            raise Conflict(f"câu hỏi {q['id']} đã chuyển lên người — điều phối viên không trả lời thay")
        if q.get("action") and by != "user":
            raise Conflict(f"câu hỏi {q['id']} là xin phép — chỉ người trả lời")
        if isinstance(keys, (str, int)):
            keys = [k for k in re.split(r"[,\s]+", str(keys)) if k]
        keys = [str(k) for k in (keys or [])]
        valid = {o["key"]: o for o in q.get("options") or []}
        bad = [k for k in keys if k not in valid]
        if bad:
            raise HubError(f"lựa chọn không có: {', '.join(bad)} (có: {', '.join(valid) or 'không có lựa chọn — trả lời bằng chữ'})")
        keys = list(dict.fromkeys(keys))
        if len(keys) > 1 and not q["multi"]:
            raise HubError("câu hỏi này chỉ chọn một phương án")
        note = clean(text, ANSWER_MAX) if text else ""
        if not keys and not note:
            raise HubError("cần chọn một phương án hoặc nhập câu trả lời")
        if note and not keys and not q["allow_free_text"]:
            raise HubError("câu hỏi này chỉ nhận các phương án có sẵn")
        ans = {"keys": keys, "labels": [valid[k]["label"] for k in keys], "text": note}
        cur = self.conn.execute("UPDATE questions SET status='answered', answer=?, answered_by=?, answered_channel=?, answered_at=? "
                                "WHERE id=? AND status IN ('with_manager','open')",
                                (_dumps(ans), by, channel, now_iso(), q["id"]))
        if cur.rowcount != 1:
            raise Conflict(f"câu hỏi {q['id']} vừa được trả lời ở kênh khác")
        q = self.get(q["id"])
        self._event("question.answered", q, {"keys": keys, "labels": ans["labels"], "text": note[:1000], "by": by, "channel": channel,
                                             "routed_to": q["routed_to"], "conversation_id": q.get("conversation_id")})
        self._audit("question.answered", q, {"keys": keys, "by": by, "channel": channel, "text": note[:500]}, actor=by, channel=channel)
        if q.get("task_id"):
            try:
                self.hub.tasks.comment(q["task_id"], self.answer_text(q), author=by if by != "user" else "human", kind="answer", run_id=q.get("run_id"))
            except HubError:
                log.debug("không ghi được bình luận trả lời cho task %s", q.get("task_id"), exc_info=True)
        if not resume:  # bên gọi tự lo (trả lời ngay trong `manager.chat` — tin người đi kèm chính lượt đó)
            return q
        if q.get("action") and ALLOW_KEY in keys:
            self._spawn(self._act_then_continue(q, by=by, channel=channel))
        else:
            self._try_continue(q)
        return self.get(q["id"])

    async def answer_in_chat(self, qid: str, keys: list[str], *, channel: str) -> str:
        """Người trả lời ngay trong hội thoại điều phối viên ("1", "ok", "duyệt"…): trả lời + thực hiện thao tác xin phép (nếu cho phép)
        NGAY, đánh dấu đã nối tiếp và trả về ghi chú gắn vào CHÍNH lượt chat đó (không đẻ thêm lượt)."""
        from ..manager.owner_actions import execute

        q = self.answer(qid, keys=keys, by="user", channel=channel, resume=False)
        if q.get("action") and ALLOW_KEY in keys:
            try:
                res = await execute(self.hub, q["action"], by="user", channel=channel)
            except Exception as e:
                res = {"ok": False, "error": str(getattr(e, "message", None) or e)[:300]}
            self._update(q["id"], {"answer": {**(q.get("answer") or {}), "action_result": res}})
            q = self.get(q["id"])
            self._event("question.action", q, {"kind": q["action"].get("kind"), "target": q["action"].get("target"), **res})
        if q.get("task_id"):
            self._try_continue(q)
        else:
            self._mark_resumed(q, None)
        return self.continuation_text(self.get(q["id"]))

    async def _act_then_continue(self, q: dict[str, Any], *, by: str, channel: str) -> None:
        """Người chọn "Cho phép" → hub thực hiện thao tác đã chốt (quyền người), ghi kết quả vào answer rồi nối tiếp điều phối viên."""
        from ..manager.owner_actions import execute

        try:
            res = await execute(self.hub, q["action"], by=by, channel=channel)
        except Exception as e:  # thao tác lỗi vẫn phải nối tiếp (điều phối viên cần biết)
            log.warning("xin phép %s: thực hiện lỗi", q["id"], exc_info=True)
            res = {"ok": False, "error": str(getattr(e, "message", None) or e)[:300]}
        a = {**(q.get("answer") or {}), "action_result": res}
        self._update(q["id"], {"answer": a})
        q = self.get(q["id"])
        self._event("question.action", q, {"kind": (q.get("action") or {}).get("kind"), "target": (q.get("action") or {}).get("target"), **res})
        self._try_continue(q)

    def answer_text(self, q: dict[str, Any]) -> str:
        """Khung trả lời gửi cho agent (tiếng Anh, chữ do HUB sinh; phần chữ tự do đánh dấu rõ là của người trả lời)."""
        a = q.get("answer") or {}
        who = "the coordinator (manager agent)" if str(q.get("answered_by") or "").startswith("manager:") else "the human owner"
        parts = [f"[cfharness] Answer to your question {q['id']} from {who}."]
        if a.get("keys"):
            parts.append("Selected: " + "; ".join(f"{k}. {lbl}" for k, lbl in zip(a["keys"], a.get("labels") or [])) + ".")
        if a.get("text"):
            parts.append(f"Their note: \"{a['text']}\"")
        parts.append(f"Your question was: \"{q['text'][:500]}\"")
        return " ".join(parts)

    def cancel(self, qid: str, *, reason: str = "", by: str = "user", channel: str = "cli") -> dict[str, Any]:
        q = self.get(qid)
        if q["status"] in FINAL:
            raise Conflict(f"câu hỏi {q['id']} đã {q['status']}")
        self._update(q["id"], {"status": "cancelled", "answered_by": by, "answered_channel": channel, "answered_at": now_iso(),
                               "escalation_note": clean(reason, 300) if reason else q.get("escalation_note")})
        q = self.get(q["id"])
        self._event("question.cancelled", q, {"reason": reason[:300], "by": by})
        self._audit("question.cancelled", q, {"reason": reason[:300]}, actor=by, channel=channel)
        return q

    # ------------------------------------------------------------------ nối tiếp công việc
    def _try_continue(self, q: dict[str, Any]) -> bool:
        """Nối tiếp ngay nếu run/task hỏi đã dừng; nếu còn chạy, listener làm khi nó kết thúc. Trả True nếu đã nối tiếp."""
        if q["status"] != "answered" or q.get("resumed_at"):
            return False
        if q.get("action") and ALLOW_KEY in ((q.get("answer") or {}).get("keys") or []) and (q.get("answer") or {}).get("action_result") is None:
            return False  # người cho phép, hub đang thực hiện — `_act_then_continue` nối tiếp khi xong
        run = self.hub.store.get_run(q["run_id"]) if q.get("run_id") else None
        if q.get("task_id"):
            t = self.hub.tasks.maybe(q["task_id"])
            if t is None or t["status"] in ("done", "archived"):
                self._mark_resumed(q, None)
                return True
            if t["status"] != "blocked":
                return False  # run hỏi chưa kết thúc → task.status blocked sẽ gọi lại
            if self.pending_for_task(t["id"]):
                return False  # còn câu hỏi khác của task chưa trả lời
            self.hub.tasks.unblock(t["id"])
            self._mark_resumed(q, None)
            return True
        if run is not None and run.get("status") in ("queued", "running", "waiting_human"):
            return False
        if q["source"] == "manager" or q.get("conversation_id"):
            self._spawn(self._continue_manager(q))
            return True
        if run is not None:
            try:
                r = self.hub.runs.continue_run(run["id"], self.answer_text(q))
            except HubError as e:
                log.warning("không nối tiếp được run %s sau câu hỏi %s: %s", run["id"], q["id"], e.message)
                self._mark_resumed(q, None)
                return True
            self._mark_resumed(q, r.get("id"))
            return True
        self._mark_resumed(q, None)
        return True

    def _mark_resumed(self, q: dict[str, Any], run_id: str | None) -> None:
        self._update(q["id"], {"resumed_at": now_iso(), "resumed_run_id": run_id})

    async def _continue_manager(self, q: dict[str, Any]) -> None:
        mgr = getattr(self.hub, "manager", None)
        if mgr is None or not mgr.agent_id():
            self._mark_resumed(q, None)
            return
        conv = None
        if q.get("conversation_id"):
            try:
                conv = mgr._conv_row(q["conversation_id"])
            except NotFound:
                conv = None
        if conv is None or conv["channel"] == "heartbeat" or conv["agent_id"] != mgr.agent_id():
            conv = mgr._home_conversation()
        try:
            res = await mgr.chat(conv["channel"] if conv else "web", self.continuation_text(q), conversation_id=conv["id"] if conv else None)
            self._mark_resumed(q, res.get("run_id"))
        except HubError as e:
            log.warning("không gửi được trả lời câu hỏi %s cho điều phối viên: %s", q["id"], e.message)
            self._mark_resumed(q, None)

    def continuation_text(self, q: dict[str, Any]) -> str:
        a = q.get("answer") or {}
        pick = "; ".join(f"{k}. {lbl}" for k, lbl in zip(a.get("keys") or [], a.get("labels") or []))
        text = f"Trả lời câu hỏi {q['id']} của bạn (“{q['text'][:300]}”): " + (f"chọn {pick}" if pick else "") + (f" — {a['text']}" if a.get("text") else "")
        if q.get("action"):
            r = a.get("action_result")
            act = q["action"]
            if ALLOW_KEY not in (a.get("keys") or []):
                text += f"\n[hub] Người KHÔNG cho phép ({act.get('kind')} {act.get('target')}). Không thử lại cách khác; làm tiếp phần còn lại hoặc báo người."
            elif (r or {}).get("ok"):
                text += f"\n[hub] Đã thực hiện {act.get('kind')} cho {act.get('target')} với quyền người. Chạy lại/tiếp tục việc đang chờ ngay (task_retry, run_start…)."
            else:
                text += f"\n[hub] Người cho phép nhưng thực hiện LỖI: {(r or {}).get('error')}. Báo người ngắn gọn."
        return text

    def _spawn(self, coro: Any) -> None:
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # ngoài event loop (test thuần) — chạy đồng bộ
            asyncio.run(coro)

    # ------------------------------------------------------------------ listener bus
    def _on_event(self, ev: HubEvent) -> None:
        try:
            if ev.type == "task.status" and ev.task_id:
                to = (ev.payload or {}).get("to")
                if to == "blocked":
                    for r in self.conn.execute("SELECT * FROM questions WHERE task_id=? AND status='answered' AND resumed_at IS NULL", (ev.task_id,)).fetchall():
                        self._later(self._try_continue, self._row(r))
                elif to in ("done", "archived"):
                    for q in self.pending_for_task(ev.task_id):
                        self._later(self._cancel_quiet, q["id"], f"task {to}")
            elif ev.type in TERMINAL_RUN_EVENTS and ev.source == "hub" and ev.run_id and ev.type != "run.waiting_human":
                for r in self.conn.execute("SELECT * FROM questions WHERE run_id=? AND task_id IS NULL AND status='answered' AND resumed_at IS NULL", (ev.run_id,)).fetchall():
                    self._later(self._try_continue, self._row(r))
            elif ev.type == "manager.designated":
                self._later(self.reroute_manager)
        except Exception:  # pragma: no cover — listener không được làm hỏng bus
            log.exception("questions listener lỗi với %s", ev.type)

    def _cancel_quiet(self, qid: str, reason: str) -> None:
        try:
            self.cancel(qid, reason=reason, by="hub", channel="hub")
        except Conflict:
            pass

    def _later(self, fn: Any, *args: Any) -> None:
        """Chạy sau event hiện tại (không ghi DB/đổi trạng thái lồng trong lúc bus đang phát)."""
        try:
            asyncio.get_running_loop().call_soon(self._safe, fn, *args)
        except RuntimeError:
            self._safe(fn, *args)

    def _safe(self, fn: Any, *args: Any) -> None:
        try:
            fn(*args)
        except Exception:  # pragma: no cover
            log.exception("questions: lỗi khi nối tiếp")

    def reroute_manager(self) -> list[str]:
        """Điều phối viên đổi/bị huỷ/không sẵn sàng → câu hỏi đang chờ manager chuyển lên người."""
        out = []
        for q in self.list("with_manager", limit=500):
            if self.manager_available(q["agent_id"]) is None:
                self.escalate(q["id"], by="hub", note="điều phối viên không còn sẵn sàng")
                out.append(q["id"])
        return out

    # ------------------------------------------------------------------ vòng nền: quá hạn + nhắc lại
    def check_due(self, now: float | None = None) -> dict[str, list[str]]:
        now = now or time.time()
        escalated = self.reroute_manager()
        limit = float(self.cfg("manager_timeout_s", 900))
        for q in self.list("with_manager", limit=500):
            if now - (_parse_ts(q["created_at"]) or now) >= limit:
                self.escalate(q["id"], by="hub", note=f"điều phối viên chưa xử lý sau {int(limit // 60)} phút")
                escalated.append(q["id"])
        base = float(self.hub.config.get("notify.reminder_interval_s"))
        cap = float(self.hub.config.get("notify.reminder_max_interval_s"))
        reminded = []
        for q in self.list("open", limit=500):
            since = _parse_ts(q.get("escalated_at")) or _parse_ts(q["created_at"]) or now
            last = _parse_ts(q.get("last_reminded_at")) or since
            n = int(q.get("reminder_count") or 0)
            if now - last < min(base * (2 ** n), max(base, cap)):
                continue
            self._update(q["id"], {"reminder_count": n + 1, "last_reminded_at": now_iso()})
            self._event("question.reminder", q, {**self.wire(q), "reminder_count": n + 1, "wait_s": round(now - since, 1)})
            reminded.append(q["id"])
        return {"escalated": escalated, "reminded": reminded}

    def start(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(max(1.0, float(self.cfg("check_s", 30))))
            try:
                self.check_due()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("vòng câu hỏi lỗi")


def _dumps(v: Any) -> str:
    import json

    return json.dumps(v, ensure_ascii=False, default=str)
