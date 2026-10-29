"""Chế độ 3D "Văn phòng" `/office` — phía server (hợp đồng: docs/dev-notes/office-3d.md).

- `GET /office`: trang `office.html` (cảnh 3D vẽ bằng `static/office.js` + Three.js đóng gói sẵn ở `static/vendor/`).
- `GET /office/state`: JSON gọn cho cảnh — trạng thái hub (+ chi phí hôm nay), team, agent (trạng thái hiển thị, TÊN tool cuối,
  dòng đầu task ≤ 80 ký tự, run đang chạy, số approval), số task + tối đa 5 task mỗi cột, approval đang chờ (preview ≤ 120),
  câu hỏi đang mở (≤ 120), run đang chạy. Mọi chữ tự do bị cắt Ở SERVER và client chỉ vẽ bằng `fillText` (chế độ 3D = toàn trang 3D).
  Chưa đăng nhập → 401 (không chuyển hướng: trang gọi bằng `fetch`).
- `GET /sse/office`: luồng event toàn hub (`level=summary`) qua `_stream()` của `app.py`, đổi thành khung JSON MỎNG theo danh sách
  trắng: chỉ id / tên tool / trạng thái / outcome — KHÔNG BAO GIỜ chữ tự do (text, input, args_preview, result_preview, reason…).

Giai đoạn 2 (duyệt + chat ngay trong 3D):
- `POST /office/do/{name}` → JSON: chỉ `OFFICE_ACTIONS` (approve, deny, approve_always, approve_rule, answer) — dùng ĐÚNG `Action` trong
  `app.ACTIONS` và CÙNG cổng `app.plan_action` với web 2D (xác nhận → bước 2 nặng / luôn cho phép / theo luật), sau `post_guard`
  (Origin, CSRF, khoá phiên `k`). Không gọi RPC ghi nào khi chưa có `confirm=yes` (và `confirm2=yes` khi có bước 2) — kể cả thao tác
  mà 2D không hỏi xác nhận (trả lời câu hỏi, cho phép theo luật): bấm nhầm trong cảnh 3D dễ hơn trên form.
- `GET /office/approval/{id}`, `GET /office/question/{id}`: chi tiết ĐẦY ĐỦ (thao tác người sắp cho phép / chữ câu hỏi) — CHỈ ở đây, không
  bao giờ trong `/sse/office`.
- `GET /office/chat`, `GET /sse/office/chat`: hội thoại kênh `web` với điều phối viên, JSON (bản JSON của `web/manager._converter`, không HTML).

Module này không đọc/ghi `hub.db`; chỉ gọi RPC (đọc: `tui.snapshot`, `question.*`, `team.list`, `task.list`, `manager.*`, `approval.get`,
`hub.status`, `events.query`; ghi: chỉ qua `ACTIONS` ở trên). Cảnh 3D không có link sang trang 2D.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from ..events import TERMINAL_RUN_EVENTS
from ..rpc import METHOD_NOT_FOUND, DaemonUnavailable, RpcError
from . import present as P
from .app import ACTIONS, GRANT_FOR, _stream, err_text, heavy_approval, plan_action, post_guard, questions, render, rpc, with_wait

NO_STORE = {"Cache-Control": "no-store"}
STATUSES = ("working", "waiting", "waiting_human", "asking", "idle", "error", "paused")
TASK_KEYS = ("ready", "running", "review", "blocked")
TASK_MAX = 80
PREVIEW_MAX = 120  # preview approval / chữ câu hỏi
TITLE_MAX = 60     # tiêu đề task trên bảng kanban
TASK_TOP = 5       # số task mỗi cột kanban
TOOL_MAX = 64
_TOOL_BAD = re.compile(r"[^A-Za-z0-9_\-:.]")
_ID_BAD = re.compile(r"[^A-Za-z0-9_\-:.]")

# ---------------------------------------------------------------------------- làm sạch
def safe_tool(v: Any) -> str | None:
    """Tên tool: chỉ `[A-Za-z0-9_\\-:.]`, ≤ 64 ký tự; rỗng → None."""
    if not isinstance(v, str):
        return None
    s = _TOOL_BAD.sub("", v)[:TOOL_MAX]
    return s or None


def safe_id(v: Any) -> str | None:
    """Định danh (agent/team/run): cùng bộ ký tự an toàn như tên tool, ≤ 128 ký tự."""
    if not isinstance(v, str):
        return None
    s = _ID_BAD.sub("", v)[:128]
    return s or None


def task_line(v: Any) -> str | None:
    """Dòng đầu của task/prompt, ≤ 80 ký tự, bỏ ký tự điều khiển/vô hình. Vẫn là DỮ LIỆU agent — client chỉ textContent/fillText."""
    if not isinstance(v, str):
        return None
    line = next((ln.strip() for ln in v.splitlines() if ln.strip()), "")
    line = "".join(ch for ch in line if unicodedata.category(ch)[0] != "C")
    if not line:
        return None
    return line if len(line) <= TASK_MAX else line[: TASK_MAX - 1] + "…"


def one_line(v: Any, n: int) -> str | None:
    """Chữ tự do → MỘT dòng ≤ n ký tự (gộp khoảng trắng, bỏ ký tự điều khiển/định dạng/bidi). Vẫn là DỮ LIỆU — client chỉ fillText."""
    if not isinstance(v, str):
        return None
    s = "".join(ch if unicodedata.category(ch)[0] != "C" else " " for ch in v)
    s = " ".join(s.split())
    if not s:
        return None
    return s if len(s) <= n else s[: n - 1] + "…"


def _num(v: Any) -> float:
    try:
        return round(float(v or 0), 6)
    except (TypeError, ValueError):
        return 0.0


def _wait_s(v: Any) -> int:
    try:
        return max(0, int(float(v or 0)))
    except (TypeError, ValueError):
        return 0


def _approvals(rows: Any) -> list[dict[str, Any]]:
    out = []
    for a in rows or []:
        if not isinstance(a, dict) or not safe_id(a.get("id")):
            continue
        out.append({
            "id": safe_id(a.get("id")),
            "agent": safe_id(a.get("agent_id")),
            "tool": safe_tool(a.get("tool_name") or a.get("tool")),
            "klass": safe_tool(a.get("destructive_class")),
            "status": _word(a.get("status")),
            "wait_s": _wait_s(a.get("wait_s")),
            "preview": one_line(a.get("preview"), PREVIEW_MAX),
        })
    return out[:20]


def _questions(rows: Any) -> list[dict[str, Any]]:
    out = []
    for q in rows or []:
        if not isinstance(q, dict) or not safe_id(q.get("id")):
            continue
        out.append({"id": safe_id(q.get("id")), "agent": safe_id(q.get("agent_id")), "status": _word(q.get("status")),
                    "text": one_line(q.get("text"), PREVIEW_MAX)})
    return out[:20]


def _tasks(rows: Any) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in TASK_KEYS}
    for t in rows or []:
        if not isinstance(t, dict):
            continue
        col = out.get(t.get("status"))  # type: ignore[arg-type]
        if col is None or len(col) >= TASK_TOP or not safe_id(t.get("id")):
            continue
        col.append({"id": safe_id(t.get("id")), "title": one_line(t.get("title"), TITLE_MAX) or "—",
                    "assignee": safe_id(t.get("assignee_agent_id"))})
    return out


def _runs(agent_rows: Any) -> list[dict[str, Any]]:
    out = []
    for r in agent_rows or []:
        rid = safe_id(r.get("current_run_id")) if isinstance(r, dict) else None
        if not rid or not r.get("run_started_at"):
            continue
        out.append({"id": rid, "agent": safe_id(r.get("id")), "started_at": one_line(r.get("run_started_at"), 40),
                    "cost_usd": _num(r.get("run_cost_usd"))})
    return out


def _rate_limited(rl: Any) -> bool:
    st = rl.get("status") if isinstance(rl, dict) else None
    return bool(st) and st not in ("allowed", "ok")


# ---------------------------------------------------------------------------- dữ liệu
async def _opt(request: Request, method: str, params: dict[str, Any] | None = None, default: Any = None) -> Any:
    """RPC đọc không bắt buộc: daemon cũ (METHOD_NOT_FOUND) hoặc lỗi nghiệp vụ → `default` (cảnh vẫn vẽ được)."""
    try:
        return await rpc(request, method, params or {})
    except RpcError:
        return default


def build_state(snap: dict[str, Any], qs: list[dict[str, Any]], teams: list[dict[str, Any]], manager_id: str | None,
                tasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Thuần: gộp kết quả RPC thành JSON `/office/state` (chỉ các khoá trong hợp đồng; chữ tự do đã cắt)."""
    h = snap.get("hub") or {}
    asking = {q.get("agent_id") for q in qs or [] if isinstance(q, dict)}
    membership: dict[str, tuple[str, str]] = {}
    task_counts = dict.fromkeys(TASK_KEYS, 0)
    team_rows = []
    for t in teams or []:
        tid = safe_id(t.get("id"))
        if not tid:
            continue
        team_rows.append({"id": tid, "name": str(t.get("name") or tid)[:80], "estop": bool(t.get("estop"))})
        for m in t.get("members") or []:
            aid = m.get("agent_id")
            if aid and aid not in membership:  # nhiều team → team đầu tiên theo `team.list`
                membership[aid] = (tid, str(m.get("role") or "") or None)  # type: ignore[assignment]
        for k in TASK_KEYS:
            task_counts[k] += int((t.get("counts") or {}).get(k) or 0)
    agents = []
    for r in snap.get("agents") or []:
        aid = safe_id(r.get("id"))
        if not aid:
            continue
        status = r.get("status") if r.get("status") in STATUSES else "idle"
        if aid in asking and status in ("idle", "error"):  # như overview_data: chờ người trả lời câu hỏi
            status = "asking"
        team, team_role = membership.get(aid, (None, None))
        agents.append({
            "id": aid,
            "name": str(r.get("name") or aid)[:80],
            "role": str(r.get("role") or "")[:80],
            "team": team,
            "team_role": team_role,
            "status": status,
            "tool": safe_tool(r.get("last_tool")),
            "task": task_line(r.get("task")),
            "run_id": safe_id(r.get("current_run_id")),
            "approvals": len(r.get("pending_approvals") or []),
            "is_manager": bool(manager_id) and aid == manager_id,
        })
    es = h.get("estop")
    return {
        "epoch": snap.get("epoch"),
        "last_seq": int(snap.get("last_seq") or 0),
        "hub": {
            "estop": bool(es.get("on")) if isinstance(es, dict) else bool(es),
            "running": int(h.get("running") or 0),
            "queued": int(h.get("queued") or 0),
            "rate_limited": _rate_limited(h.get("rate_limit")),
            "approvals_waiting_human": int(h.get("approvals_waiting_human") or 0),
            "cost_today_usd": _num(h.get("cost_today_usd")),
        },
        "manager_id": safe_id(manager_id),
        "teams": team_rows,
        "agents": agents,
        "task_counts": task_counts,
        "tasks": _tasks(tasks),
        "approvals": _approvals(snap.get("approvals")),
        "questions": _questions(qs),
        "runs": _runs(snap.get("agents")),
    }


async def office_state_data(request: Request) -> dict[str, Any]:
    snap = await rpc(request, "tui.snapshot") or {}
    qs = await questions(request, "pending")
    teams = await _opt(request, "team.list", {}, []) or []
    mgr = await _opt(request, "manager.get", {}, {}) or {}
    tasks = await _opt(request, "task.list", {"status": list(TASK_KEYS), "limit": 200}, []) or []
    return build_state(snap, qs, teams, mgr.get("agent_id") if isinstance(mgr, dict) else None, tasks if isinstance(tasks, list) else [])


# ---------------------------------------------------------------------------- SSE
# loại event → hàm lấy (tool, status, outcome) từ envelope; chỉ id/tên/trạng thái, không chữ tự do
RUN_END = ("run.completed", "run.failed", "run.cancelled", "run.interrupted", "run.waiting_human")
PLAIN = ("run.queued", "run.started", "subagent.spawn", "approval.open", "approval.waiting_human", "approval.resolved", "approval.delegated",
         "approval.escalated", "question.open", "question.answered", "question.cancelled", "question.escalated", "team.estop", "schedule.fired",
         "ratelimit.throttle", "learning.applied", "learning.rolled_back", "guard.blocked", "steer.ack")
ALLOWED = frozenset((*RUN_END, *PLAIN, "agent.status", "tool.start", "tool.complete", "task.status", "hub.estop", "manager.action"))
REFRESH = ("agent.status", "run.queued", "run.started", *RUN_END, "hub.estop", "team.estop", "task.status")
_STATUS_RE = re.compile(r"^[a-z_]{1,32}$")


def _word(v: Any) -> str | None:
    """Trạng thái/outcome: chỉ từ khoá ngắn `[a-z_]` (không bao giờ chữ tự do)."""
    return v if isinstance(v, str) and _STATUS_RE.match(v) else None


def office_frames(ev: dict[str, Any]) -> list[dict[str, Any]]:
    """Event hub → khung SSE mỏng cho cảnh 3D (danh sách trắng). Loại khác, hoặc event kết thúc run không do hub phát → bỏ."""
    t = ev.get("type")
    if t not in ALLOWED:
        return []
    if t in RUN_END and ev.get("source") != "hub":
        return []
    p = ev.get("payload") or {}
    if not isinstance(p, dict):
        p = {}
    tool = status = outcome = None
    if t == "agent.status":
        status = _word(p.get("status"))
    elif t in ("tool.start", "tool.complete"):
        tool = safe_tool(p.get("name"))
        if t == "tool.complete" and p.get("is_error"):
            status = "error"
    elif t in RUN_END:
        outcome = _word(p.get("outcome"))
    elif t == "task.status":
        status = _word(p.get("to"))
    elif t == "hub.estop":
        status = "on" if p.get("on") else "off"
    elif t == "team.estop":
        status = "on" if p.get("on") else "off"
    elif t == "manager.action":
        tool = safe_tool(p.get("tool"))
    team = ev.get("team_id") or (p.get("team_id") if t == "team.estop" else None)
    frame = {"t": "ev", "seq": ev.get("seq") if isinstance(ev.get("seq"), int) else None, "type": t, "agent": safe_id(ev.get("agent_id")),
             "team": safe_id(team), "tool": tool, "status": status, "outcome": outcome}
    out = [frame]
    if t in REFRESH or t.startswith(("approval.", "question.")):
        out.append({"t": "refresh"})
    return out


# ---------------------------------------------------------------------------- route
async def office_page(request: Request) -> Response:
    # trang ĐỘC LẬP toàn màn hình (không sidebar/thanh trên): chỉ canvas + nút "2D" nổi + danh sách sr-only
    return await render(request, "office.html", {"nav": "overview"})


async def office_state(request: Request) -> Response:
    return JSONResponse(await office_state_data(request), headers=NO_STORE)


async def sse_office(request: Request) -> Response:
    q = request.query_params
    since, epoch = q.get("since"), q.get("epoch")
    if not since or not since.isdigit():
        st = await rpc(request, "hub.status") or {}
        since, epoch = str(int(st.get("last_seq") or 0)), st.get("epoch")
    params = {"level": "summary", "since_seq": int(since), "epoch": epoch or None}
    conv: Callable[[dict[str, Any]], list[dict[str, Any]]] = office_frames
    return StreamingResponse(_stream(request, params, conv), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------- giai đoạn 2: thao tác ghi (JSON)
# tên 3D → tên trong `app.ACTIONS` (cùng Action, cùng cổng `plan_action` với 2D)
OFFICE_ACTIONS = {"approve": "approve", "deny": "deny", "approve_always": "approve_always", "approve_rule": "approve_rule", "answer": "question_answer"}
# thao tác 2D không có hộp xác nhận → cảnh 3D vẫn hỏi (bước `confirm`) trước khi gọi RPC
OFFICE_CONFIRM = {"approve_rule": "Cho phép agent này theo luật này (không hỏi lại)? Bước sau nêu rõ luật và thời hạn.",
                  "answer": "Gửi câu trả lời này cho agent?"}
NOT_FOUND_CODE = -32004


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status, headers=NO_STORE)


def _rpc_status(e: Exception) -> int:
    if isinstance(e, (DaemonUnavailable, OSError)) or (isinstance(e, RpcError) and e.code == METHOD_NOT_FOUND):
        return 503
    if isinstance(e, RpcError) and e.code == NOT_FOUND_CODE:
        return 404
    return 400


def _view(ap: dict[str, Any]) -> dict[str, Any]:
    """Toàn bộ thao tác (KHÔNG cắt — `present.action_view`): người phải thấy đủ thứ mình cho phép."""
    v = P.action_view(ap)
    return {"tool": v["tool"], "text": v["text"], "lines": v["lines"], "hidden_chars": v["hidden_chars"]}


async def office_do(request: Request) -> Response:
    name = request.path_params["name"]
    target = OFFICE_ACTIONS.get(name)
    if target is None or target not in ACTIONS:
        return _err("Không có thao tác này trong chế độ 3D", 404)
    try:
        _, form = await post_guard(request)  # Origin/Sec-Fetch-Site + CSRF + khoá phiên `k` (WT-1) — như /do/<action>
    except HTTPException as e:
        return _err(str(e.detail), e.status_code)
    act = ACTIONS[target]
    if not act.confirm and form.get("confirm") != "yes":
        try:
            act.build(form)
        except ValueError as e:
            return _err(f"Chưa đủ thông tin: {e}")
        return JSONResponse({"need": "confirm", "message": OFFICE_CONFIRM.get(name, "Thực hiện thao tác này?"), "danger": act.danger,
                             "note_label": act.note}, headers=NO_STORE)
    try:
        plan = await plan_action(request, target, form, act)
    except (DaemonUnavailable, OSError) as e:
        return _err(err_text(e), 503)
    if plan.step in ("invalid", "error"):
        return _err(plan.error or "Lỗi")
    if plan.step == "confirm":
        return JSONResponse({"need": "confirm", "message": plan.message, "danger": act.danger, "note_label": act.note}, headers=NO_STORE)
    if plan.step == "step2":
        ap = plan.ap or {}
        # bước 2 nêu ĐÚNG luật sẽ tạo (như approve2.html): agent + luật đã khớp
        return JSONResponse({"need": "confirm2", "kind": plan.kind, "view": _view(ap), "agent": ap.get("agent_id"),
                             "rule": ap.get("matched_rule") if plan.kind == "grant" else None,
                             "always": ap.get("always_offer") if plan.kind == "always" else None,
                             "grant_for": list(GRANT_FOR) if plan.kind == "grant" else None}, headers=NO_STORE)
    params = plan.params or {}
    try:
        res = await act.call(request, params) if act.call else await rpc(request, act.method, params, audit=act.audit)
    except (RpcError, DaemonUnavailable, OSError) as e:
        return _err(err_text(e), _rpc_status(e))
    msg = act.ok(res, form) if callable(act.ok) else act.ok
    return JSONResponse({"ok": True, "message": msg}, headers=NO_STORE)


# ---------------------------------------------------------------------------- giai đoạn 2: đọc chi tiết
async def office_approval(request: Request) -> Response:
    try:
        ap = await rpc(request, "approval.get", {"id": request.path_params["id"]})
    except (RpcError, DaemonUnavailable, OSError) as e:
        return _err(err_text(e), _rpc_status(e))
    ap = with_wait([ap])[0]
    classes = await _opt(request, "config.get", {"key": "notify.telegram.confirm_classes"}, []) or []
    return JSONResponse({"id": ap.get("id"), "agent": ap.get("agent_id"), "status": ap.get("status"), "routed_to": ap.get("routed_to"),
                         "tool": ap.get("tool_name"), "klass": ap.get("destructive_class"), "wait_s": ap.get("wait_s"), "view": _view(ap),
                         "always_offer": bool(ap.get("always_offer")), "can_rule": bool(ap.get("matched_rule")),
                         "rule": ap.get("matched_rule"), "heavy": heavy_approval(ap, classes)}, headers=NO_STORE)


async def office_question(request: Request) -> Response:
    try:
        q = await rpc(request, "question.get", {"id": request.path_params["id"]})
    except (RpcError, DaemonUnavailable, OSError) as e:
        return _err(err_text(e), _rpc_status(e))
    opts = [{"key": str(o.get("key") or ""), "label": str(o.get("label") or "")} for o in q.get("options") or [] if isinstance(o, dict)]
    return JSONResponse({"id": q.get("id"), "agent": q.get("agent_id"), "status": q.get("status"), "text": str(q.get("text") or ""),
                         "options": opts, "allow_text": bool(q.get("allow_free_text", True)), "multi": bool(q.get("multi"))}, headers=NO_STORE)


# ---------------------------------------------------------------------------- giai đoạn 2: chat với điều phối viên
CHAT_LIMIT = 50


def chat_messages(timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Thuần: dòng thời gian của trang Điều phối (`manager._timeline`) → tin JSON phẳng (≤ 50 gần nhất). Chữ là DỮ LIỆU agent."""
    out: list[dict[str, Any]] = []
    for it in timeline:
        k = it.get("kind")
        if k in ("user", "manager", "note"):
            out.append({"kind": k, "text": str(it.get("text") or ""), "ts": it.get("ts"), "run_id": it.get("run_id")})
        elif k in ("actions", "action"):
            for a in it.get("items") or [it]:
                out.append({"kind": "action", "text": str(a.get("summary") or ""), "ts": a.get("ts"), "run_id": a.get("run_id"),
                            "tool": a.get("tool") or None, "ok": bool(a.get("ok"))})
        elif k == "question":
            q = it.get("q") or {}
            out.append({"kind": "question", "text": str(q.get("text") or ""), "ts": it.get("ts"), "run_id": it.get("run_id"), "id": q.get("id"),
                        "status": q.get("status")})
    return out[-CHAT_LIMIT:]


async def office_chat(request: Request) -> Response:
    from .manager import HISTORY_LIMIT, _actions, _timeline, _web_convs, mgr_state

    try:
        st = await mgr_state(request)
    except (RpcError, DaemonUnavailable, OSError) as e:
        return _err(err_text(e), _rpc_status(e))
    aid = st.get("agent_id")
    body: dict[str, Any] = {"available": bool(st.get("available")) and bool(aid), "manager_id": safe_id(aid), "conversation_id": None,
                            "conversations": [], "messages": []}
    if not aid:
        return JSONResponse(body, headers=NO_STORE)
    convs = await _web_convs(request)
    body["conversations"] = [{"id": c.get("id"), "title": c.get("title") or "", "updated_at": c.get("updated_at")} for c in convs]
    want = request.query_params.get("c")
    cid = want or (convs[0]["id"] if convs else None)
    if not cid:
        return JSONResponse(body, headers=NO_STORE)
    try:
        raw = await rpc(request, "manager.history", {"conversation_id": cid, "limit": HISTORY_LIMIT}) or []
    except RpcError as e:
        if want:
            return _err(f"Không mở được cuộc trò chuyện {cid}: {err_text(e)}", _rpc_status(e))
        raw = []
    msgs = [m for m in (P.mgr_message(x) for x in raw if isinstance(x, dict)) if m]
    acts = await _actions(request, aid)
    conv_qs = [q for q in await questions(request, "all", agent_id=aid) if q.get("conversation_id") == cid and q.get("status") != "cancelled"]
    body.update({"conversation_id": cid, "messages": chat_messages(_timeline(msgs, acts, conv_qs))})
    return JSONResponse(body, headers=NO_STORE)


def chat_frames(conv: str | None = None) -> Callable[[dict[str, Any]], list[dict[str, Any]]]:
    """Bản JSON của `web/manager._converter` (KHÔNG HTML). `conv` → bỏ trả lời/câu hỏi thuộc hội thoại KHÁC."""
    from .manager import question_from_event

    def other(c: Any) -> bool:
        return bool(conv) and bool(c) and c != conv

    def f(ev: dict[str, Any]) -> list[dict[str, Any]]:
        t = ev.get("type") or ""
        p = ev.get("payload") or {}
        rid = ev.get("run_id")
        if t == "message.delta":
            if ev.get("subagent_id"):
                return []
            return [{"t": "delta", "run": rid, "text": str(p.get("text") or "")}]
        if t == "manager.reply":
            run = p.get("run_id") or rid
            if other(p.get("conversation_id")):
                return []
            notify = p.get("kind") == "notify" or bool(p.get("notify"))
            return [{"t": "reply", "run": run, "conv": p.get("conversation_id"), "text": str(p.get("text") or ""), "notify": notify}]
        if t == "manager.action":
            it = P.mgr_action_item(ev)
            return [{"t": "action", "run": rid, "tool": it["tool"] or None, "summary": it["summary"], "ok": bool(it["ok"])}]
        if t.startswith("question.") and t != "question.reminder":
            q = question_from_event(ev)
            if not q.get("conversation_id") or other(q["conversation_id"]):
                return []
            out = {"t": "question", "conv": q["conversation_id"], "id": q.get("id")}
            if t == "question.answered":
                out["answered"] = True
            return [out]
        if t in TERMINAL_RUN_EVENTS and ev.get("source") == "hub" and rid:
            return [{"t": "run_end", "run": rid, "outcome": str(p.get("outcome") or t.removeprefix("run."))}]
        if t.startswith("manager.tick"):
            return [{"t": "tick"}]
        return []

    return f


async def sse_office_chat(request: Request) -> Response:
    from .manager import mgr_state

    st = await mgr_state(request)
    if not st.get("agent_id"):
        raise HTTPException(404, "Chưa có điều phối viên")
    q = request.query_params
    since, epoch = q.get("since"), q.get("epoch")
    if not since or not since.isdigit():
        hs = await rpc(request, "hub.status") or {}
        since, epoch = str(int(hs.get("last_seq") or 0)), hs.get("epoch")
    params = {"agent_id": st["agent_id"], "since_seq": int(since), "epoch": epoch or None, "level": "detail"}
    return StreamingResponse(_stream(request, params, chat_frames(q.get("c") or None)), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def routes() -> list[Any]:
    from starlette.routing import Route

    return [
        Route("/office", office_page),
        Route("/office/state", office_state),
        Route("/sse/office", sse_office),
        Route("/office/do/{name}", office_do, methods=["POST"]),
        Route("/office/approval/{id}", office_approval),
        Route("/office/question/{id}", office_question),
        Route("/office/chat", office_chat),
        Route("/sse/office/chat", sse_office_chat),
    ]


__all__ = ["routes", "office_frames", "chat_frames", "chat_messages", "OFFICE_ACTIONS", "build_state", "safe_tool", "task_line", "one_line", "ALLOWED"]
