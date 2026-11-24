"""Hợp đồng v2 trang Điều phối (GĐ1 — phương án B, `docs/dev-notes/dieu-phoi-island.md`): MỘT luồng SSE mỗi trang + ảnh chụp JSON.

- `GET /api/manager/snapshot?w=&c=&new=&run=` → JSON (hội thoại, luồng + trạng thái, mục dòng thời gian — chữ điều phối viên là HTML đã
  cứng hoá, cache theo id —, việc đang chờ người, `cursor`). `cursor` lấy TRƯỚC khi đọc dữ liệu → mở SSE với `since=<cursor>` không hở.
- `GET /sse/manager/v2?tab=&c=&w=&since=` → luồng `data: {"t": …}` có `id: <boot>.<n>` trên MỌI message. Nối lại (EventSource tự gửi
  `Last-Event-ID`, hoặc `?since=`) → phát lại phần đã lỡ từ ring, không mất không trùng. Hở quá ring / web khởi động lại / daemon đổi
  epoch → message `resync` (client tải lại snapshot).
- `POST /api/manager/watch {tab, c, w, op=add|set}` → đổi phạm vi của luồng: `ws.status`/`tabs.patch`/`tick` cho MỌI luồng;
  `msg.*`/`tool.*`/`run.*`/`act.item`/`action.*` chỉ của hội thoại/luồng client đã mở.
- `POST /api/manager/act/<approve|deny|answer>` → như `/office/do/<tên>` (cùng `plan_action`, bước xác nhận) + khoá idempotency `idem`
  (gửi lại cùng khoá → cùng kết quả, không làm hai lần).

Kiến trúc: MỘT `Feed` mỗi tiến trình web (lazy, tự dừng khi không còn ai xem) giữ MỘT subscription daemon (`level=summary` mọi agent +
`detail_agent_id` = điều phối viên — chữ chạy chỉ của điều phối viên), đổi event → message v2 MỘT lần, giữ ring (id tăng đơn điệu) rồi
phân phát cho từng luồng SSE theo phạm vi. Chữ của agent là DỮ LIỆU: `msg.delta` = chữ thô (client gán textContent / markdown phía client
+ DOMPurify), `msg.done.item.html` = `present.md()` đã cứng hoá — HTML DUY NHẤT trong hợp đồng.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from ..events import TERMINAL_RUN_EVENTS
from ..rpc import DaemonUnavailable, RpcError
from ..tui.model import short_tool
from . import present as P

log = logging.getLogger("cfharness.web.manager_feed")

V = 1  # phiên bản hợp đồng (snapshot.v, hello.v)
RING_MAX = 5000  # message v2 giữ để phát lại khi nối lại (~ vài phút chữ chạy 100 token/s)
RING_BYTES = 8_000_000
IDLE_STOP_S = 120.0  # không còn luồng SSE / snapshot nào trong chừng này giây → đóng subscription daemon
WS_DEBOUNCE_S = 0.3  # gom các event đổi trạng thái luồng trước khi tính lại (một lần 3 RPC)
WATCH_TTL_S = 600.0  # phạm vi của một tab trình duyệt nhớ lại chừng này sau khi ngắt (EventSource nối lại cùng URL)
HEARTBEAT_S = 15.0
RUN_CACHE = 512
HTML_CACHE = 2000
IDEM_TTL_S = 600.0
PING = '{"t":"ping"}'  # nhịp giữ luồng (mỗi HEARTBEAT_S khi rảnh) — client không áp gì, chỉ đo im lặng
LOBBY = "_"  # khoá phạm vi của Sảnh (hội thoại không gắn luồng)
MAX_WATCH = 64

# event làm đổi chấm/số chưa đọc của tab luồng → tính lại `ws.status` (gom WS_DEBOUNCE_S)
WS_TRIGGERS = ("run.queued", "run.started", "workstream.updated", "manager.reply", "manager.designated")
WS_PREFIXES = ("task.", "question.", "approval.")

Call = Callable[[str, dict[str, Any]], Awaitable[Any]]


# ============================================================================ id
def make_id(boot: str, n: int) -> str:
    return f"{boot}.{n}"


def parse_id(raw: str | None) -> tuple[str, int] | None:
    """`<boot>.<n>` → (boot, n); sai dạng → None."""
    if not raw:
        return None
    boot, _, n = str(raw).strip().rpartition(".")
    if not boot or not n.isdigit():
        return None
    return boot, int(n)


# ============================================================================ HTML cache theo id
_HTML: OrderedDict[str, str] = OrderedDict()


def md_cached(key: str | None, text: str) -> str:
    """HTML đã cứng hoá (`present.md`) của chữ điều phối viên, cache theo id mục (`m<id>` tin đã lưu, `e<seq>` trả lời qua SSE).
    Kèm băm ngắn của chữ trong khoá: id trùng mà chữ khác (không xảy ra với tin đã lưu) vẫn đúng."""
    text = text or "(không có nội dung)"
    if not key:
        return P.md(text)
    k = key + ":" + hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=6).hexdigest()
    hit = _HTML.get(k)
    if hit is not None:
        _HTML.move_to_end(k)
        return hit
    html = P.md(text, cache=False)
    _HTML[k] = html
    while len(_HTML) > HTML_CACHE:
        _HTML.popitem(last=False)
    return html


# ============================================================================ mục dòng thời gian → JSON
def _files(text: str, agent: str | None) -> list[dict[str, str]]:
    if not agent:
        return []
    return [{"path": p, "name": P.path_name(p)} for p in P.mentioned_paths(text)]


def item_json(it: dict[str, Any], agent: str | None = None) -> dict[str, Any] | None:
    """Mục của `manager._timeline` → JSON cho island. Chỉ `html` của mục `manager` là HTML (đã cứng hoá); còn lại là chữ thô."""
    k = it.get("kind")
    base = {"kind": k, "ts": it.get("ts"), "run_id": it.get("run_id")}
    if k == "user":
        return base | {"id": f"m{it['id']}" if it.get("id") is not None else None, "text": it.get("text") or ""}
    if k == "manager":
        mid = f"m{it['id']}" if it.get("id") is not None else it.get("_id")
        text = str(it.get("text") or "")
        ag = it.get("agent") or agent
        return base | {"id": mid, "text": text, "html": md_cached(mid, text), "notify": bool(it.get("notify")), "tick": bool(it.get("tick")),
                       "cut": it.get("cut") or "", "agent": ag, "files": _files(text, ag)}
    if k == "note":
        # cùng id với ghi chú client tự thêm khi nhận `run.end` lỗi ("n<run>") → ảnh chụp + phần phát lại sau cursor không ra hai ghi chú
        return base | {"id": f"n{it['run_id']}" if it.get("run_id") else None, "text": it.get("text") or "", "tone": it.get("tone") or "info"}
    if k == "day":
        return {"kind": "day", "id": f"d{it.get('key')}", "key": it.get("key"), "text": it.get("text")}
    if k == "action":
        return action_json(it)
    if k == "actions":
        acts = [action_json(a) for a in it.get("items") or []]
        return base | {"id": f"g{(it.get('items') or [{}])[0].get('audit_id') or it.get('run_id')}", "ok": bool(it.get("ok")), "items": acts}
    if k == "question":
        q = question_json(it.get("q") or {})
        return base | {"id": f"q-{q['id']}", "q": q}
    return None


def action_json(a: dict[str, Any]) -> dict[str, Any]:
    tgt = a.get("target") or {}
    return {"kind": "action", "summary": a.get("summary") or "", "tool": a.get("tool") or "", "ok": bool(a.get("ok", True)),
            "error": a.get("error"), "ts": a.get("ts"), "run_id": a.get("run_id"), "audit_id": a.get("audit_id"),
            "conversation_id": a.get("conversation_id"),
            # kind/id: island dựng "thẻ việc sống" cho đích task/run (`web/flow.py`, `/api/manager/flow?work=`)
            "target": {"href": tgt.get("href"), "label": tgt.get("label"), "kind": tgt.get("kind") if tgt.get("href") else None,
                       "id": tgt.get("id") if tgt.get("href") else None} if isinstance(tgt, dict) else {"href": None, "label": str(tgt)}}


QUESTION_KEYS = ("id", "agent_id", "run_id", "task_id", "conversation_id", "text", "options", "multi", "allow_free_text", "status", "routed_to",
                 "created_at", "escalation_note", "answer", "answered_by")


def question_json(q: dict[str, Any]) -> dict[str, Any]:
    out = {k: q.get(k) for k in QUESTION_KEYS}
    opts = []
    for i, o in enumerate(out.get("options") or []):
        if isinstance(o, dict):
            opts.append({"key": str(o.get("key") or i + 1), "label": str(o.get("label") or ""), "description": str(o.get("description") or "")})
        else:
            opts.append({"key": str(i + 1), "label": str(o), "description": ""})
    out["options"] = opts
    return out


APPROVALS_HREF = "/approvals"  # duyệt thao tác phá huỷ: trang Duyệt (hai bước, luật "luôn cho phép"…) hoặc /api/manager/act/approve|deny


def approval_json(ap: dict[str, Any]) -> dict[str, Any]:
    """Thao tác chờ người duyệt (phá huỷ/không chắc) của lượt điều phối viên → thẻ. Toàn văn thao tác (`present.action_view`, không cắt)."""
    v = P.action_view(ap)
    return {"kind": "approval", "id": ap.get("id"), "run_id": ap.get("run_id"), "agent_id": ap.get("agent_id"), "status": ap.get("status"),
            "tool": v.get("tool"), "text": v.get("text"), "reason": ap.get("reason"), "created_at": ap.get("opened_at") or ap.get("created_at"),
            "href": APPROVALS_HREF}


def tab_json(w: dict[str, Any]) -> dict[str, Any]:
    return {"ws": w.get("id"), "key": w.get("key"), "title": w.get("title"), "label": w.get("label") or w.get("key") or w.get("title"),
            "status": w.get("status") or "active", "sprint": w.get("sprint"), "dot": w.get("dot"), "dot_label": w.get("dot_label"),
            "unread": int(w.get("unread") or 0), "needs_human": int(w.get("needs_human") or 0), "tasks": w.get("tasks") or {}}


def tabs_from(wsd: dict[str, Any]) -> dict[str, Any]:
    """`manager.workstreams_data` → {active:[tab], closed:[tab], lobby: tab}."""
    lobby = {"ws": LOBBY, "key": None, "title": "Sảnh", "label": "Sảnh", "status": "active", "sprint": None, "dot": wsd.get("lobby_dot") or "none",
             "dot_label": wsd.get("lobby_state") or "", "unread": int(wsd.get("lobby_unread") or 0), "needs_human": 0, "tasks": {}}
    return {"enabled": bool(wsd.get("enabled")), "lobby": lobby, "active": [tab_json(w) for w in wsd.get("active") or []],
            "closed": [tab_json(w) for w in wsd.get("closed") or []]}


# ============================================================================ event → message v2 (thuần, có trạng thái theo run)
@dataclass
class Scope:
    all: bool = False
    conv: str | None = None
    ws: str | None = None
    side: bool = False  # `act.item`: tới cả tab chỉ mở hội thoại KHÁC của cùng luồng ("Hành động gần đây" theo luồng/Sảnh)


ALL = Scope(all=True)


class Converter:
    """Event hub (dict wire) + ngữ cảnh run → [(message, Scope)]. Không trạng thái theo kết nối: `msg.done` mang chữ ĐÃ LƯU của câu trả lời
    (không gộp chữ đã chạy) → trình duyệt nối lại giữa chừng / tải lại trang thấy cùng một mục. `mgr` = điều phối viên hiện tại (event agent
    khác chỉ dùng cho trạng thái luồng)."""

    def __init__(self) -> None:
        self.mgr: str | None = None

    def convert(self, ev: dict[str, Any], ctx: Scope | None = None, conv_ws: dict[str, str] | None = None) -> list[tuple[dict[str, Any], Scope]]:
        t = str(ev.get("type") or "")
        p = ev.get("payload") or {}
        rid = ev.get("run_id")
        mine = bool(self.mgr) and ev.get("agent_id") == self.mgr
        sub = bool(ev.get("subagent_id"))
        sc = ctx or Scope()
        here = {"run": rid, "conv": sc.conv, "ws": sc.ws}
        if t.startswith("manager.tick"):
            return [({"t": "tick", "state": p.get("state") or t.removeprefix("manager.tick").lstrip(".") or None}, ALL)]
        if t.startswith("question.") and t != "question.reminder":
            return self._question(ev, conv_ws or {})
        if t in ("approval.waiting_human", "approval.resolved") and mine and rid:
            aid = p.get("request_id") or p.get("approval_id")
            if t == "approval.waiting_human":
                ap = {"kind": "approval", "id": aid, "run_id": rid, "agent_id": ev.get("agent_id"), "status": "waiting_human",
                      "tool": p.get("tool"), "text": str(p.get("preview") or ""), "reason": p.get("reason"), "created_at": ev.get("ts"),
                      "href": APPROVALS_HREF}
                return [({"t": "action.pending", **here, "action": ap}, sc)]
            return [({"t": "action.resolved", **here, "kind": "approval", "id": aid, "status": p.get("decision") or "resolved"}, sc)]
        if not mine:
            return []
        if t == "message.delta":
            if sub or not rid:
                return []
            return [({"t": "msg.delta", **here, "text": str(p.get("text") or "")}, sc)]
        if t == "message.complete":
            if sub or not rid:
                return []
            return [({"t": "msg.seg", **here}, sc)]
        if t == "reasoning.delta":
            return [] if sub else [({"t": "think.delta", **here, "text": str(p.get("text") or "")}, sc)]
        if t in ("tool.start", "tool.complete"):
            if sub:
                return []
            name = str(p.get("name") or "?")
            tid = str(p.get("tool_id") or "")
            if t == "tool.complete":
                return [({"t": "tool.end", **here, "id": tid, "name": short_tool(name), "error": bool(p.get("is_error"))}, sc)]
            inp = p.get("input") if isinstance(p.get("input"), dict) else None
            v = P.tool_view(name, inp, summary=str(p.get("args_preview") or ""))
            label = " ".join(x for x in (v.get("verb"), v.get("target")) if x)[:120]
            out = [({"t": "tool.start", **here, "id": tid, "name": short_tool(name), "label": label or short_tool(name)}, sc)]
            todo = P.todo_item(ev)
            if todo:
                out.append(({"t": "todo", **here, "todo": todo.get("todo")}, sc))
            return out
        if t == "manager.reply":
            # chữ = ĐÚNG chữ đã lưu (`text` của event = tin trong `manager_messages`), dựng qua CÙNG `present.mgr_message` như ảnh chụp, id
            # `m<message_id>` → bản tới qua SSE và bản đọc lại sau khi tải trang là MỘT mục (cùng id, chữ, HTML). Lời dẫn đã chạy chữ giữa các
            # lần dùng tool (không thuộc kết quả cuối) chỉ hiện lúc đang chạy — như sau khi tải lại.
            run = p.get("run_id") or rid
            role = "notify" if p.get("kind") == "notify" or p.get("notify") else "tick" if p.get("kind") == "tick" else "manager"
            conv = p.get("conversation_id") or sc.conv
            ws = sc.ws if sc.conv == conv and sc.ws else (conv_ws or {}).get(conv) if conv else sc.ws
            it = P.mgr_message({"role": role, "text": str(p.get("text") or ""), "ts": ev.get("ts"), "run_id": run, "id": p.get("message_id"),
                                "truncated": bool(p.get("truncated"))}) or {}
            it |= {"_id": f"e{ev.get('seq')}", "agent": ev.get("agent_id")}
            return [({"t": "msg.done", "run": run, "conv": conv, "ws": ws, "item": item_json(it)}, Scope(conv=conv, ws=ws))]
        if t == "manager.message":
            # tin NGƯỜI (gửi từ tab/thiết bị/kênh khác) → khung đang mở hội thoại thêm ngay; khung đã gửi thay tin lạc quan bằng bản đã lưu
            conv = p.get("conversation_id") or sc.conv
            run = p.get("run_id") or rid
            if not conv or p.get("role", "user") != "user":
                return []
            ws = sc.ws if sc.conv == conv and sc.ws else (conv_ws or {}).get(conv) or sc.ws
            it = P.mgr_message({"role": "user", "text": str(p.get("text") or ""), "ts": ev.get("ts"), "run_id": run, "id": p.get("message_id")}) or {}
            return [({"t": "msg.user", "run": run, "conv": conv, "ws": ws, "item": item_json(it)}, Scope(conv=conv, ws=ws))]
        if t == "manager.action":
            it = action_json(P.mgr_action_item(ev))
            conv = it.get("conversation_id") or sc.conv
            return [({"t": "act.item", "run": rid, "conv": conv, "ws": sc.ws, "item": it}, Scope(conv=conv, ws=sc.ws, side=True))]
        if t in TERMINAL_RUN_EVENTS and ev.get("source") == "hub" and rid:
            outcome = str(p.get("outcome") or t.removeprefix("run."))
            label, tone = P.run_status(outcome)
            return [({"t": "run.end", **here, "ok": outcome == "completed", "outcome": outcome, "label": label, "tone": tone}, sc)]
        if t in ("run.queued", "run.started") and rid:
            return [({"t": "run.start", **here, "state": t.removeprefix("run.")}, sc)]
        return []

    def _question(self, ev: dict[str, Any], conv_ws: dict[str, str]) -> list[tuple[dict[str, Any], Scope]]:
        from .manager import question_from_event

        q = question_from_event(ev)
        conv = q.get("conversation_id")
        if not conv:
            return []
        ws = conv_ws.get(conv)
        sc = Scope(conv=conv, ws=ws)
        t = ev.get("type")
        if t in ("question.answered", "question.cancelled"):
            return [({"t": "action.resolved", "run": ev.get("run_id"), "conv": conv, "ws": ws, "kind": "question", "id": q.get("id"),
                      "status": q["status"], "answer": q.get("answer"), "answered_by": q.get("answered_by")}, sc)]
        if t in ("question.open", "question.escalated"):
            return [({"t": "action.pending", "run": ev.get("run_id"), "conv": conv, "ws": ws, "action": {"kind": "question", **question_json(q)}}, sc)]
        return []


# ============================================================================ phạm vi của một tab trình duyệt
@dataclass
class Watch:
    """Phạm vi `msg.*`/`tool.*`/`action.*` của MỘT tab trình duyệt. `convs`/`ws` là dict theo thứ tự thêm (dùng như tập có thứ tự): quá
    MAX_WATCH thì bỏ cái CŨ nhất — thêm lại một id là đưa nó về cuối (cái đang mở không bị bỏ). `live` = số luồng SSE đang giữ đối tượng
    này: còn luồng thì không bao giờ bị dọn theo TTL (luồng giữ tham chiếu — dọn rồi tạo mới là luồng mất phạm vi)."""

    convs: dict[str, None] = field(default_factory=dict)
    ws: dict[str, None] = field(default_factory=dict)
    side: dict[str, None] = field(default_factory=dict)  # luồng (Sảnh "_") của các hội thoại đã mở — chỉ cho `act.item` (Scope.side)
    seen: float = field(default_factory=time.monotonic)
    live: int = 0

    def wants(self, s: Scope) -> bool:
        return (s.all or (s.conv is not None and s.conv in self.convs) or (s.ws is not None and s.ws in self.ws)
                or (s.side and s.ws is not None and s.ws in self.side))

    @staticmethod
    def _put(d: dict[str, None], ids: list[str]) -> None:
        for x in ids:
            if x:
                d.pop(x, None)
                d[x] = None
        while len(d) > MAX_WATCH:
            d.pop(next(iter(d)))

    def add(self, convs: list[str], wss: list[str], side: list[str] | None = None) -> None:
        self._put(self.convs, convs)
        self._put(self.ws, wss)
        self._put(self.side, [*wss, *(side or [])])

    def clear(self) -> None:
        self.convs.clear()
        self.ws.clear()
        self.side.clear()


@dataclass
class Entry:
    n: int
    raw: str  # JSON đã tuần tự (một lần cho mọi luồng)
    scope: Scope


# ============================================================================ Feed (một mỗi tiến trình web)
class Feed:
    def __init__(self, call: Call, open_sub: Callable[[dict[str, Any]], Any], *, idle_stop_s: float = IDLE_STOP_S,
                 ring_max: int = RING_MAX) -> None:
        self.call = call
        self.open_sub = open_sub
        self.boot = secrets.token_hex(4)
        self.n = 0
        self.ring: deque[Entry] = deque()
        self.ring_bytes = 0
        self.ring_max = ring_max
        self.idle_stop_s = idle_stop_s
        self.conv = Converter()
        self.epoch: str | None = None
        self.hub_seq: int | None = None
        self.ready = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.clients = 0
        self.touched = time.monotonic()
        self._wake = asyncio.Event()
        self.runs: OrderedDict[str, Scope] = OrderedDict()
        self.conv_ws: dict[str, str] = {}
        self.tabs: dict[str, Any] | None = None
        self._ws_task: asyncio.Task | None = None
        self._ws_dirty = False
        self.watches: dict[str, Watch] = {}
        self.idem: OrderedDict[str, tuple[float, int, dict[str, Any]]] = OrderedDict()
        self._sub: Any = None
        self._reaper: asyncio.TimerHandle | None = None
        self.restarts = 0

    # ---------------------------------------------------------------- ring
    @property
    def cursor(self) -> str:
        return make_id(self.boot, self.n)

    @property
    def first_n(self) -> int:
        return self.ring[0].n if self.ring else self.n + 1

    def append(self, msg: dict[str, Any], scope: Scope) -> Entry:
        self.n += 1
        e = Entry(self.n, json.dumps(msg, ensure_ascii=False, separators=(",", ":")), scope)
        self.ring.append(e)
        self.ring_bytes += len(e.raw)
        while self.ring and (len(self.ring) > self.ring_max or self.ring_bytes > RING_BYTES):
            old = self.ring.popleft()
            self.ring_bytes -= len(old.raw)
        wake, self._wake = self._wake, asyncio.Event()
        wake.set()
        return e

    def since(self, n: int) -> list[Entry]:
        """Mục có id > n (ring sắp xếp tăng dần)."""
        if not self.ring or n >= self.n:
            return []
        k = min(len(self.ring), self.n - n)  # n liên tục trong ring → k mục CUỐI (không chép cả ring mỗi lần đánh thức)
        L = len(self.ring)
        return [self.ring[i] for i in range(L - k, L)]

    def gap(self, last: tuple[str, int] | None) -> str | None:
        """Lý do phải resync khi nối lại từ `last` (None = không cần)."""
        if last is None:
            return None
        boot, n = last
        if boot != self.boot:
            return "boot"  # tiến trình web khởi động lại — id cũ vô nghĩa
        if n > self.n:
            return "boot"
        if n + 1 < self.first_n:
            return "gap"  # phần đã lỡ không còn trong ring
        return None

    async def wait(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---------------------------------------------------------------- vòng subscription
    async def ensure(self, timeout: float = 3.0) -> None:
        """Bật subscription daemon nếu chưa chạy và chờ tới khi đã đăng ký (cursor có nghĩa từ đây)."""
        self.touched = time.monotonic()
        if self.task is None or self.task.done():
            self.ready.clear()
            self.task = asyncio.create_task(self._run())
        if self.clients == 0:
            self.reap_later()  # chỉ có ảnh chụp, không luồng nào mở sau đó → vẫn phải tắt subscription khi rảnh
        if not self.ready.is_set():
            await asyncio.wait_for(asyncio.shield(self.ready.wait()), timeout)

    def idle(self) -> bool:
        return self.clients == 0 and time.monotonic() - self.touched > self.idle_stop_s

    def reap_later(self) -> None:
        """Hẹn kiểm tra rảnh: hub im lặng thì `_one` không bao giờ tới chỗ kiểm `idle()` (chỉ kiểm sau mỗi event) → bộ hẹn giờ này huỷ
        subscription daemon khi không còn ai xem đủ `idle_stop_s`."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover
            return
        if self._reaper is not None:
            self._reaper.cancel()
        wait = max(0.05, self.idle_stop_s - (time.monotonic() - self.touched)) + 0.05
        self._reaper = loop.call_later(wait, self._reap)

    def _reap(self) -> None:
        self._reaper = None
        if self.clients:
            return  # luồng còn mở — luồng đóng sẽ hẹn lại
        if not self.idle():
            self.reap_later()
            return
        if self.task is not None and not self.task.done():
            self.task.cancel()

    async def _manager_id(self) -> str | None:
        try:
            return (await self.call("manager.get", {}) or {}).get("agent_id")
        except RpcError:
            return None

    async def _run(self) -> None:
        backoff = 0.5
        try:
            while True:
                try:
                    await self._one()
                    backoff = 0.5
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # daemon tắt / mất kết nối → thử lại có giãn cách
                    log.debug("feed điều phối: subscription lỗi %s", type(e).__name__, exc_info=True)
                if self.idle():
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
                if self.idle():
                    break
                self.restarts += 1
        finally:
            self.ready.clear()
            if self._ws_task and not self._ws_task.done():
                self._ws_task.cancel()

    async def _one(self) -> None:
        mgr = await self._manager_id()
        if mgr != self.conv.mgr and self.conv.mgr is not None:
            self.append({"t": "resync", "reason": "manager"}, ALL)
        self.conv.mgr = mgr
        params: dict[str, Any] = {"level": "summary"}
        if mgr:
            params["detail_agent_id"] = mgr
        if self.hub_seq is not None:
            params.update({"since_seq": self.hub_seq, "epoch": self.epoch})
        sub = self.open_sub(params)
        self._sub = sub
        try:
            async for msg in sub:
                if "subscribed" in msg:
                    info = msg["subscribed"] or {}
                    ep = info.get("epoch")
                    if self.epoch is not None and ep and ep != self.epoch:
                        self.conv = Converter()
                        self.conv.mgr = mgr
                        self.runs.clear()
                        self.append({"t": "resync", "reason": "epoch"}, ALL)
                    elif info.get("truncated"):
                        self.append({"t": "resync", "reason": "gap"}, ALL)
                    self.epoch = ep or self.epoch
                    self.ready.set()
                    if self.tabs is None:
                        self.kick_tabs()
                    continue
                if "end" in msg:
                    return
                await self.on_event(msg)
                if self.idle():
                    return
                if msg.get("type") == "manager.designated":
                    return  # điều phối viên đổi → đăng ký lại với `detail_agent_id` mới (từ hub_seq, không mất event)
        finally:
            try:
                sub.close()
            except Exception:  # best-effort: đóng subscription đã đứt, lỗi đóng không có gì để xử lý  # pragma: no cover
                pass
            self._sub = None

    async def run_ctx(self, ev: dict[str, Any]) -> Scope | None:
        rid = ev.get("run_id")
        if not rid or ev.get("agent_id") != self.conv.mgr:
            return None
        sc = self.runs.get(rid)
        if sc is not None:
            return sc
        try:
            run = await self.call("run.get", {"id": rid}) or {}
        except (RpcError, DaemonUnavailable, OSError):
            run = {}
        opts = run.get("run_opts") if isinstance(run.get("run_opts"), dict) else {}
        conv = opts.get("manager_conversation")
        ws = opts.get("manager_workstream") or (self.conv_ws.get(conv) if conv else None) or (LOBBY if conv else None)
        sc = Scope(conv=conv, ws=ws)
        self.runs[rid] = sc
        while len(self.runs) > RUN_CACHE:
            self.runs.popitem(last=False)
        return sc

    async def on_event(self, ev: dict[str, Any]) -> None:
        if isinstance(ev.get("seq"), int):
            self.hub_seq = ev["seq"]
        t = str(ev.get("type") or "")
        ctx = await self.run_ctx(ev)
        for msg, scope in self.conv.convert(ev, ctx, self.conv_ws):
            self.append(msg, scope)
        if t in WS_TRIGGERS or t.startswith(WS_PREFIXES) or t in TERMINAL_RUN_EVENTS:
            self.kick_tabs()

    # ---------------------------------------------------------------- trạng thái luồng (ws.status / tabs.patch)
    def kick_tabs(self) -> None:
        self._ws_dirty = True
        if self._ws_task is None or self._ws_task.done():
            self._ws_task = asyncio.create_task(self._tabs_loop())

    async def _tabs_loop(self) -> None:
        while self._ws_dirty:
            self._ws_dirty = False
            await asyncio.sleep(WS_DEBOUNCE_S)
            try:
                await self.refresh_tabs()
            except Exception as e:  # pragma: no cover — daemon tắt giữa chừng
                log.debug("feed điều phối: không tính được trạng thái luồng: %s", e)

    async def refresh_tabs(self) -> dict[str, Any]:
        from .manager import workstreams_data

        wsd = await workstreams_data(self.call)
        self.conv_ws = {cid: (c.get("workstream_id") or LOBBY) for cid, c in (wsd.get("convs") or {}).items() if isinstance(c, dict)}
        tabs = tabs_from(wsd)
        self.emit_tabs(tabs)
        return tabs

    def emit_tabs(self, tabs: dict[str, Any]) -> None:
        old = self.tabs
        self.tabs = tabs
        if old is None:
            return  # lần đầu: client đã có trong snapshot
        prev = {t["ws"]: t for t in [old["lobby"], *old["active"], *old["closed"]]}
        for tb in [tabs["lobby"], *tabs["active"], *tabs["closed"]]:
            if prev.get(tb["ws"]) != tb:
                self.append({"t": "ws.status", **tb}, ALL)
        order = ([t["ws"] for t in old["active"]], [t["ws"] for t in old["closed"]])
        if order != ([t["ws"] for t in tabs["active"]], [t["ws"] for t in tabs["closed"]]):
            self.append({"t": "tabs.patch", "active": [t["ws"] for t in tabs["active"]], "closed": [t["ws"] for t in tabs["closed"]]}, ALL)

    # ---------------------------------------------------------------- phạm vi
    def watch(self, key: str, convs: list[str], wss: list[str], *, replace: bool = False) -> Watch:
        now = time.monotonic()
        for k in [k for k, w in self.watches.items() if not w.live and now - w.seen > WATCH_TTL_S]:
            self.watches.pop(k, None)
        w = self.watches.get(key)
        if w is None:
            w = self.watches[key] = Watch()
        elif replace:
            w.clear()  # đặt lại TẠI CHỖ: luồng SSE đang chạy giữ chính đối tượng này
        w.add(convs, wss, [self.conv_ws[c] for c in convs if c in self.conv_ws])
        w.seen = now
        return w

    # ---------------------------------------------------------------- idempotency
    def idem_get(self, key: str) -> tuple[int, dict[str, Any]] | None:
        now = time.monotonic()
        while self.idem and now - next(iter(self.idem.values()))[0] > IDEM_TTL_S:
            self.idem.popitem(last=False)
        hit = self.idem.get(key)
        return (hit[1], hit[2]) if hit else None

    def idem_put(self, key: str, status: int, body: dict[str, Any]) -> None:
        self.idem[key] = (time.monotonic(), status, body)
        while len(self.idem) > 1000:
            self.idem.popitem(last=False)

    async def close(self) -> None:
        if self._sub is not None:
            try:
                self._sub.close()
            except Exception:  # best-effort: đóng subscription khi dừng feed, lỗi đóng bỏ qua  # pragma: no cover
                pass
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        for t in (self.task, self._ws_task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # best-effort: chờ task vừa huỷ kết thúc, lỗi của nó đã log trong task
                    pass


# ============================================================================ luồng SSE của một trang
def sse_block(raw: str, eid: str | None) -> str:
    return (f"id: {eid}\n" if eid else "") + f"data: {raw}\n\n"


async def stream(feed: Feed, watch: Watch, last: tuple[str, int] | None, *, heartbeat_s: float = HEARTBEAT_S,
                 hello: dict[str, Any] | None = None, gone: Callable[[], Awaitable[bool]] | None = None) -> AsyncIterator[str]:
    """Luồng SSE v2: `retry`, `hello`, (resync nếu hở) rồi phần lỡ từ ring, rồi live. Mỗi message có `id: <boot>.<n>`. Message ngoài phạm vi
    không gửi nhưng id vẫn tiến (khối `id:` không có data ở nhịp ping) → nối lại không phải quét lại phần đã bỏ."""
    feed.clients += 1
    watch.live += 1
    try:
        yield "retry: 3000\n\n"
        yield sse_block(json.dumps({"t": "hello", "v": V, "boot": feed.boot, "cursor": feed.cursor, "epoch": feed.epoch, **(hello or {})},
                                   ensure_ascii=False), None)
        why = feed.gap(last)
        if last is None or why:
            if why:
                yield sse_block(json.dumps({"t": "resync", "reason": why}), feed.cursor)
            pos = feed.n
        else:
            pos = last[1]
        sent = pos
        last_out = time.monotonic()  # lần cuối gửi message có data — nhịp ping tính theo HẠN từ đây, không theo "wait hết giờ"
        while True:
            watch.seen = feed.touched = time.monotonic()  # mỗi vòng (không chỉ nhịp ping): hub bận liên tục vẫn là "đang xem"
            batch = feed.since(pos)
            if pos + 1 < feed.first_n and pos < feed.n:  # luồng này chậm tới mức ring đã trôi qua → resync
                yield sse_block(json.dumps({"t": "resync", "reason": "gap"}), feed.cursor)
                pos = sent = feed.n
                last_out = time.monotonic()
                continue
            for e in batch:
                pos = e.n
                if watch.wants(e.scope):
                    sent = e.n
                    last_out = time.monotonic()
                    yield sse_block(e.raw, make_id(feed.boot, e.n))
            if pos < feed.n:
                continue
            # Hub bận với event NGOÀI phạm vi tab này đánh thức wait() liên tục mà không có gì để gửi → nếu chỉ ping khi wait hết giờ thì
            # client không nhận gì và watchdog tưởng luồng chết. Vì vậy chờ tối đa phần CÒN LẠI của hạn, quá hạn là ping ngay.
            left = heartbeat_s - (time.monotonic() - last_out)
            if left > 0 and await feed.wait(left):
                continue
            if gone is not None and await gone():
                return
            # nhịp là message THẬT (có data): client đo im lặng để nhận ra luồng chết lặng (TCP nửa mở, proxy cắt) mà EventSource không
            # báo lỗi. Kèm `id` khi phần bỏ qua (ngoài phạm vi) đã tiến → nối lại không quét lại phần đã bỏ.
            last_out = time.monotonic()
            if sent < pos:
                yield sse_block(PING, make_id(feed.boot, pos))
                sent = pos
            else:
                yield sse_block(PING, None)
            if feed.task is None or feed.task.done():
                try:
                    await feed.ensure()
                except Exception:
                    log.debug("feed điều phối: khởi động lại feed lỗi", exc_info=True)
    finally:
        feed.clients -= 1
        watch.live -= 1
        watch.seen = feed.touched = time.monotonic()  # TTL tính từ lúc ngắt (EventSource nối lại cùng tab trong WATCH_TTL_S)
        if feed.clients == 0:
            feed.reap_later()


# ============================================================================ route
def feed_of(request: Request) -> Feed:
    app = request.app
    feed = getattr(app.state, "mgr_feed", None)
    if feed is None:
        from ..tui.client import subscribe
        from .app import ws as _ws

        st = _ws(request)
        feed = Feed(lambda m, p: st.client.call(m, p), lambda params: subscribe(st.client, params))
        app.state.mgr_feed = feed
    return feed


def _ids(q: Any, name: str) -> list[str]:
    out: list[str] = []
    for v in q.getlist(name):
        out += [x.strip() for x in str(v).split(",") if x.strip()]
    return out[:MAX_WATCH]


def _watch_key(request: Request, tab: str) -> str:
    from .app import session_of

    sess = session_of(request)
    sid = getattr(sess, "id", None) or getattr(sess, "csrf", "") if sess else ""
    return hashlib.blake2b(f"{sid}:{tab}".encode(), digest_size=8).hexdigest()


async def sse_v2(request: Request) -> Response:
    q = request.query_params
    feed = feed_of(request)
    try:
        await feed.ensure()
    except (asyncio.TimeoutError, DaemonUnavailable, OSError) as e:
        raise HTTPException(503, f"Không kết nối được hub: {e}")
    tab = str(q.get("tab") or "").strip()[:64] or secrets.token_hex(6)
    w = feed.watch(_watch_key(request, tab), _ids(q, "c"), _ids(q, "w"))
    last = parse_id(request.headers.get("last-event-id") or q.get("since"))

    async def gone() -> bool:
        return await request.is_disconnected()

    return StreamingResponse(stream(feed, w, last, hello={"tab": tab}, gone=gone), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


async def api_watch(request: Request) -> Response:
    """POST {tab, c, w, op=add|set}: thêm/đặt phạm vi `msg.*` của luồng SSE của tab (cùng phiên). CSRF + khoá phiên như mọi POST."""
    from .app import post_guard

    _, form = await post_guard(request)
    tab = str(form.get("tab") or "").strip()[:64]
    if not tab:
        return JSONResponse({"error": "Thiếu tab"}, status_code=400)
    feed = feed_of(request)
    w = feed.watch(_watch_key(request, tab), _ids(form, "c"), _ids(form, "w"), replace=form.get("op") == "set")
    return JSONResponse({"ok": True, "c": sorted(w.convs), "w": sorted(w.ws)}, headers={"Cache-Control": "no-store"})


IDEM_ACTIONS = ("approve", "deny", "answer")


async def api_act(request: Request) -> Response:
    """POST /api/manager/act/<approve|deny|answer>: như `/office/do/<tên>` (cùng `plan_action`, trả `need: confirm|confirm2` khi cần xác nhận)
    + `idem`: kết quả CUỐI (không phải bước xác nhận) nhớ IDEM_TTL_S theo (phiên, idem) — gửi lại cùng khoá trả đúng kết quả cũ, không gọi
    RPC lần hai (client lạc quan, mạng chập chờn)."""
    from .office import office_do

    from .app import post_guard

    name = request.path_params["name"]
    if name not in IDEM_ACTIONS:
        return JSONResponse({"ok": False, "error": "Không có thao tác này"}, status_code=404)
    try:  # CSRF/Origin/khoá phiên TRƯỚC khi tra cache idem — bản phát lại cũng phải qua cùng cửa
        _, form = await post_guard(request)
    except HTTPException as e:
        return JSONResponse({"ok": False, "error": str(e.detail)}, status_code=e.status_code, headers={"Cache-Control": "no-store"})
    idem = str(form.get("idem") or "").strip()[:80]
    feed = feed_of(request)
    key = _watch_key(request, f"idem:{name}:{idem}") if idem else ""
    if key:
        hit = feed.idem_get(key)
        if hit:
            return JSONResponse(hit[1] | {"replayed": True}, status_code=hit[0], headers={"Cache-Control": "no-store"})
    resp = await office_do(request)
    if key and (200 <= resp.status_code < 300 or resp.status_code == 409):  # chỉ kết quả CUỐI; 4xx (CSRF, thiếu tham số) thử lại được
        try:
            body = json.loads(bytes(resp.body))
        except Exception:  # best-effort: thân không phải JSON → không lưu kết quả idempotency  # pragma: no cover
            body = None
        if isinstance(body, dict) and "need" not in body:
            feed.idem_put(key, resp.status_code, body)
    return resp


async def build_snapshot(request: Request, *, tab: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    """(ctx của `manager.page_ctx`, ảnh chụp JSON) — dùng chung cho `GET /api/manager/snapshot` và trang island (`coord.coord_page`, nhúng
    sẵn ảnh chụp đầu tiên vào HTML → mở trang không phải chờ thêm một lượt fetch)."""
    from .manager import page_ctx

    feed = feed_of(request)
    q = request.query_params
    try:
        await feed.ensure()
        if tab:
            # phạm vi của tab ĐĂNG KÝ TRƯỚC khi lấy cursor: mọi message sau cursor của đích này chắc chắn tới luồng SSE của tab (luồng
            # duyệt ring tới đâu kiểm `wants` tới đó). `?c=` → đúng hội thoại; `?w=`/Sảnh (chưa biết hội thoại nào) → cả luồng.
            c0 = str(q.get("c") or "").strip()
            feed.watch(_watch_key(request, tab), [c0] if c0 else [], [] if c0 else [str(q.get("w") or "").strip() or LOBBY])
        cursor = feed.cursor
    except (asyncio.TimeoutError, DaemonUnavailable, OSError):
        cursor = None  # vẫn trả ảnh chụp; client mở SSE không since (resync khi có)
    ctx = await page_ctx(request)
    aps: list[dict[str, Any]] = []
    runs = set(ctx.get("conv_runs") or [])
    if runs and (ctx.get("m") or {}).get("agent_id"):
        from .app import rpc

        try:  # thao tác phá huỷ đang chờ người của các lượt thuộc hội thoại này
            aps = [a for a in await rpc(request, "approval.list", {"status": "waiting_human", "agent_id": ctx["m"]["agent_id"], "limit": 50}) or []
                   if a.get("run_id") in runs]
        except RpcError:
            aps = []
    snap = snapshot_json(ctx, cursor, feed, approvals=aps)
    if tab and snap.get("conv"):
        feed.watch(_watch_key(request, tab), [snap["conv"]], [])
    return ctx, snap


async def api_snapshot(request: Request) -> Response:
    """Ảnh chụp JSON của trang (cùng dữ liệu với `GET /manager`). `cursor` = id SSE lấy TRƯỚC khi đọc → không hở với luồng."""
    tab = str(request.query_params.get("tab") or "").strip()[:64]
    _, snap = await build_snapshot(request, tab=tab)
    return JSONResponse(snap, headers={"Cache-Control": "no-store"})


async def api_status(request: Request) -> Response:
    """`GET /api/manager/status` → `{"manager": …}` (khối trạng thái điều phối viên của island: tên, trạng thái, nhịp, chi phí hôm nay)."""
    from .manager import mgr_state

    try:
        st = await mgr_state(request)
    except (RpcError, DaemonUnavailable) as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return JSONResponse({"manager": manager_json(st), "hub": await hub_flags(request)}, headers={"Cache-Control": "no-store"})


async def hub_flags(request: Request) -> dict[str, bool]:
    """ESTOP hub / quản trị cho dải cảnh báo của island — cùng nguồn (đã cache theo phiên) với huy hiệu menu `base.html`."""
    from .app import nav_badge, session_of

    sess = session_of(request)
    if sess is None:
        return {"estop": False, "admin_estop": False}
    try:
        b = await nav_badge(request, sess)
    except (RpcError, DaemonUnavailable, OSError):
        return {"estop": False, "admin_estop": False}
    return {"estop": bool(b.get("estop")), "admin_estop": bool(b.get("admin_estop"))}


def manager_json(m: dict[str, Any]) -> dict[str, Any]:
    """Trạng thái điều phối viên (`manager.mgr_state`) → JSON. Chữ thô; nhãn/tông trạng thái tính sẵn ở server (cùng bảng với trang cũ)."""
    row = m.get("row") or {}
    ic, label, tone = P.agent_status(row.get("status") or "idle")
    name = row.get("name") or m.get("agent_id")
    return {"available": bool(m.get("available")), "agent_id": m.get("agent_id"), "name": name, "avatar": P.avatar_tone(name),
            "status": row.get("status") or "idle", "status_label": label, "status_tone": tone, "status_icon": ic,
            "cost_today": P.fmt_cost(row.get("cost_today_usd") or 0), "last_tick": m.get("last_tick"), "next_tick": m.get("next_tick")}


WS_KEYS = ("id", "key", "title", "label", "status", "sprint", "state", "tasks", "needs_human", "open_tasks", "recent_done", "created_at",
           "created_by", "closed_at", "last_activity", "state_updated_at")


def _count(v: Any) -> int:
    return len(v) if isinstance(v, (list, tuple, dict)) else int(v or 0) if isinstance(v, (int, float, str)) and str(v).isdigit() else 0


def snapshot_json(ctx: dict[str, Any], cursor: str | None, feed: Feed | None = None, *, approvals: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    m = ctx.get("m") or {}
    out: dict[str, Any] = {"v": V, "cursor": cursor, "boot": feed.boot if feed else None, "epoch": ctx.get("epoch"),
                           "manager": manager_json(m)}
    if not m.get("available") or not m.get("agent_id"):
        out["setup"] = {"agents": [{"id": a.get("id"), "name": a.get("name")} for a in ctx.get("agents") or []],
                        "templates": [t.get("name") for t in ctx.get("templates") or []]}
        return out
    wsd = ctx.get("wsd") or {}
    tabs = tabs_from(wsd)
    if feed is not None:
        feed.conv_ws.update({cid: (c.get("workstream_id") or LOBBY) for cid, c in (wsd.get("convs") or {}).items() if isinstance(c, dict)})
    aid = m.get("agent_id")
    ws_id = ctx.get("ws_id")
    convs = ctx.get("ws_web_convs") if ws_id else (wsd.get("lobby") if wsd.get("enabled") else ctx.get("convs"))
    items = [j for j in (item_json(it, aid) for it in ctx.get("items") or []) if j]
    pend_actions = [{"kind": "question", **question_json(q)} for q in ctx.get("conv_qs") or [] if q.get("status") in ("open", "with_manager", "pending")]
    pend_actions += [approval_json(a) for a in approvals or []]
    wsx = ctx.get("wsx")
    pinfo = ctx.get("pinfo") or {}
    out.update({
        "conv": ctx.get("conv"), "ws": ws_id, "scope": ws_id or LOBBY, "title": ctx.get("cur_title"),
        "conversations": [{"id": c.get("id"), "title": c.get("title"), "updated_at": c.get("updated_at"), "count": c.get("count"),
                           "unread": int(c.get("unread") or 0), "running": bool(c.get("running"))} for c in convs or []],
        "workstreams": tabs,
        "items": items,
        "pending": {"run": ctx.get("pending"), "tool": pinfo.get("tool") or "", "todo": (pinfo.get("todo") or {}).get("todo")}
        if ctx.get("pending") else None,
        "actions": pend_actions,
        "side_actions": [action_json(a) for a in ctx.get("side_actions") or []],
        "conv_runs": ctx.get("conv_runs") or [],
        "workstream": ({k: wsx.get(k) for k in WS_KEYS} | {"conversations": _count(wsx.get("conversations"))} if isinstance(wsx, dict) else None),
        "ws_convs": [{"id": c.get("id"), "title": c.get("title"), "channel": c.get("channel"), "running": bool(c.get("running")),
                      "updated_at": c.get("updated_at")} for c in ctx.get("ws_convs") or []],
    })
    return out


def routes() -> list[Any]:
    from starlette.routing import Route

    return [
        Route("/api/manager/snapshot", api_snapshot),
        Route("/api/manager/status", api_status),
        Route("/api/manager/watch", api_watch, methods=["POST"]),
        Route("/api/manager/act/{name}", api_act, methods=["POST"]),
        Route("/sse/manager/v2", sse_v2),
        *_flow_routes(),
    ]


def _flow_routes() -> list[Any]:
    from .flow import routes as _r  # /api/manager/flow — khay "Cần bạn", "Đội đang làm gì", thẻ việc sống

    return _r()
