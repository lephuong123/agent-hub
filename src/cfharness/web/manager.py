"""Trang "Điều phối" (M7 — luồng MGR-UI): chat với điều phối viên (agent manager) qua conversation kênh `web`.

RPC dùng (hợp đồng `docs/plan/M7-manager.md`, MGR-CORE cung cấp): `manager.get|set|chat|conversations|history`, event
`manager.reply{conversation_id, run_id, text}`, `manager.action{tool, target, summary, audit_id}`; cộng `events.subscribe`
(agent = điều phối viên, level detail) cho chữ chạy (`message.delta`) và kết thúc run.

- `GET /manager[?c=<conv>|?new=1][&w=<luồng>][&run=<run>]`: lịch sử + thẻ hành động + nhịp tick + ô nhập. Chưa có điều phối viên →
  hướng dẫn + form chỉ định (action `manager_set`, HUMAN_ONLY, hộp xác nhận).
- Luồng việc (docs/dev-notes/workstreams.md): thanh bên = Sảnh (hội thoại web không gắn luồng) + các luồng đang mở (chấm trạng thái,
  số tin chưa đọc) + luồng đã đóng. `?w=<luồng>` mở hội thoại web gần nhất của luồng (chưa có → hội thoại mới trong luồng, ô nhập
  mang `w`). Panel phải trong luồng = STATE.md (sửa được) + task của luồng + hội thoại kênh khác. Thao tác: `ws_create|ws_edit|ws_close|ws_delete|
  ws_reopen|ws_state|conv_rename|conv_move|conv_archive` (HUMAN_ONLY ở daemon). Mở hội thoại → `manager.conversation_read`.
- `POST /manager/send`: `post_guard` (CSRF + Origin + khoá phiên WT-1) → `manager.chat(channel=web)` (`_audit.channel=web`).
  Có header `X-Ahub-Fetch` (app.js) → JSON (mục đã vẽ sẵn phía server); không → 303 về trang (form thuần).
  Đính kèm (multipart, trường `attach`, chọn bằng nút 📎 / kéo–thả / dán ảnh): ảnh PNG/JPEG/GIF/WebP (nhận theo magic bytes) → `images`,
  tệp văn bản UTF-8 hoặc tài liệu PDF/Office (nhận theo nội dung, `manager/doc_extract.py`) → `files`; nhị phân khác → báo lỗi,
  KHÔNG gửi gì. Giới hạn = của daemon (`manager/service.py`), daemon kiểm lại.
- `POST /manager/stop` (P0-6): dừng lượt trả lời đang chạy (chỉ run của điều phối viên thuộc hội thoại điều phối) — `run.stop`, audit kênh `web`.
- `GET /sse/manager?since=&epoch=`: luồng JSON `{t: delta|draft|seg|think|tool|reply|action|run_end|tick|status|question, seq}`; `seq` = seq
  event gốc (client bỏ trùng, nối lại với `since=<seq cuối>`). HTML chỉ do server vẽ: macro autoescape + `present.md()` (đã cứng hoá).
- `GET /partials/manager`: khung trạng thái (htmx làm mới).
- `GET /partials/manager/tabs?w=&c=`: hàng tab luồng (`c` = hội thoại đang mở → đánh dấu đã đọc) — chấm "đang chạy / cần bạn / đang chờ / đã xong" mỗi luồng (app.js làm mới khi SSE báo
  `status`/`run_end`/`reply`/`tick`, và hỏi lại mỗi 20 s vì task của agent khác không đi qua luồng SSE này).
- Đổi tab/hội thoại không tải lại cả trang: app.js tải `/manager?…` bằng fetch, thay `#mgr-grid` + danh sách hội thoại, `pushState`.
Chữ của điều phối viên là DỮ LIỆU: hiển thị qua `present.md()` đã cứng hoá (không HTML thô, không `<img>`, link ngoài có `rel` + tên
miền; link nội bộ chỉ từ `mgr_target`) — đó là đầu ra DUY NHẤT được `|safe`/innerHTML. Tin của người vẫn là chữ thô `pre-wrap` (test XSS).
"""

from __future__ import annotations

import base64
import json
from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from ..events import TERMINAL_RUN_EVENTS
from ..rpc import METHOD_NOT_FOUND, DaemonUnavailable, RpcError
from ..tui.model import short_tool
from . import present as P
from .app import CHANNEL, _stream, err_text, flash_redirect, post_guard, render, rpc, session_of, ws

HISTORY_LIMIT = 200
HEARTBEAT = "heartbeat"  # = manager.service.HEARTBEAT_CHANNEL (không import service ở web)
ACTIONS_SCAN = 3000  # số event gần nhất của điều phối viên quét để lấy thẻ `manager.action`
PENDING_SCAN = 5000  # event tối đa của MỘT lượt đang chạy đọc lại khi tải trang (tool đang dùng + checklist)
MAX_TEXT = 8000
ATTACH_FIELD = "attach"


async def _attachments(form: Any) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str], list[str]]:
    """Tệp đính kèm của form → (ảnh, tệp văn bản/tài liệu — dạng `{name, data_b64}` cho `manager.chat`, tên hiển thị, lỗi).
    Phân loại theo NỘI DUNG (magic bytes ảnh; PDF/Office theo `doc_ext`; còn lại phải là UTF-8 không NUL), không tin MIME trình duyệt."""
    from ..manager.doc_extract import doc_ext
    from ..manager.service import FILE_MAX_BYTES, FILE_MAX_COUNT, IMAGE_MAX_BYTES, IMAGE_MAX_COUNT, _image_ext, is_text_bytes

    images: list[dict[str, str]] = []
    files: list[dict[str, str]] = []
    names: list[str] = []
    errors: list[str] = []
    for up in form.getlist(ATTACH_FIELD):
        if isinstance(up, str) or not getattr(up, "filename", None):
            continue  # ô chọn tệp rỗng (form thuần không chọn gì) gửi một phần tên rỗng
        name = str(up.filename)
        data = await up.read(FILE_MAX_BYTES + 1)
        if not data:
            errors.append(f"{name}: tệp rỗng")
            continue
        if _image_ext(data):
            if len(data) > IMAGE_MAX_BYTES:
                errors.append(f"{name}: ảnh quá lớn (tối đa {IMAGE_MAX_BYTES // 1024 // 1024} MB)")
                continue
            lst, cap, what = images, IMAGE_MAX_COUNT, "ảnh"
        else:
            if len(data) > FILE_MAX_BYTES:
                errors.append(f"{name}: tệp quá lớn (tối đa {FILE_MAX_BYTES // 1024 // 1024} MB)")
                continue
            if not doc_ext(name, data) and not is_text_bytes(data):
                errors.append(f"{name}: chưa hỗ trợ loại tệp này (chỉ ảnh PNG/JPEG/GIF/WebP, tệp văn bản UTF-8 và PDF/Excel/Word/PowerPoint)")
                continue
            lst, cap, what = files, FILE_MAX_COUNT, "tệp"
        if len(lst) >= cap:
            errors.append(f"tối đa {cap} {what} mỗi tin — bỏ {name}")
            continue
        lst.append({"name": name, "data_b64": base64.b64encode(data).decode("ascii")})
        names.append(name)
    return images, files, names, errors


async def mgr_state(request: Request) -> dict[str, Any]:
    """`manager.get` + hàng agent (trạng thái live). `available=False` khi daemon chưa có `manager.*` (MGR-CORE chưa cài)."""
    try:
        g = await rpc(request, "manager.get") or {}
    except RpcError as e:
        if e.code == METHOD_NOT_FOUND:
            return {"available": False, "agent_id": None}
        raise
    aid = g.get("agent_id")
    row = None
    if aid:
        try:
            snap = await rpc(request, "tui.snapshot")
            row = next((r for r in snap.get("agents") or [] if r.get("id") == aid), None)
        except RpcError:
            row = None
    return {"available": True, "agent_id": aid, "row": row, "last_tick": g.get("last_tick"), "next_tick": g.get("next_tick"),
            "tick": g.get("tick") or {}, "conversations": [P.mgr_conv(c) for c in g.get("conversations") or [] if isinstance(c, dict)]}


async def _web_convs(request: Request) -> list[dict[str, Any]]:
    try:
        rows = await rpc(request, "manager.conversations", {"channel": CHANNEL}) or []
    except RpcError:
        return []
    out = [P.mgr_conv(c) for c in rows if isinstance(c, dict)]
    return sorted([c for c in out if c["id"]], key=lambda c: str(c.get("updated_at") or ""), reverse=True)


async def _workstreams(request: Request) -> dict[str, Any]:
    """Dữ liệu thanh bên luồng việc: luồng đang mở (+ số tin chưa đọc gộp mọi kênh), số luồng đã đóng, hội thoại SẢNH (web).
    Daemon cũ chưa có `workstream.*` → rỗng (trang vẫn chạy như trước)."""
    try:
        active = await rpc(request, "workstream.list", {"status": "active"}) or []
        closed = await rpc(request, "workstream.list", {"status": "closed", "limit": 50}) or []
        convs = await rpc(request, "manager.conversations", {"limit": 500}) or []
    except RpcError:
        return {"enabled": False, "active": [], "closed": [], "lobby": []}
    unread: dict[str, int] = {}
    busy: dict[str | None, int] = {}  # luồng (None = Sảnh) → số hội thoại có lượt điều phối viên đang chạy (kể cả nhịp của luồng)
    for c in convs:
        if not isinstance(c, dict):
            continue
        if c.get("running") and (c.get("workstream_id") or c.get("channel") != "heartbeat"):
            busy[c.get("workstream_id")] = busy.get(c.get("workstream_id"), 0) + 1
        if c.get("workstream_id") and c.get("channel") != "heartbeat":
            unread[c["workstream_id"]] = unread.get(c["workstream_id"], 0) + int(c.get("unread") or 0)
    for w in active + closed:
        w["unread"] = unread.get(w["id"], 0)
        w["dot"], w["dot_label"] = tab_state(w.get("tasks") or {}, int(w.get("needs_human") or 0), busy.get(w["id"], 0), bool(w.get("conversations")))
    lobby = sorted([P.mgr_conv(c) | {"unread": int(c.get("unread") or 0), "running": bool(c.get("running"))} for c in convs
                    if isinstance(c, dict) and c.get("channel") == CHANNEL and not c.get("workstream_id")],
                   key=lambda c: str(c.get("updated_at") or ""), reverse=True)
    ldot, lstate = tab_state({}, 0, busy.get(None, 0), bool(lobby))
    return {"enabled": True, "active": active, "closed": closed, "lobby": lobby, "lobby_unread": sum(c["unread"] for c in lobby),
            "lobby_dot": ldot, "lobby_state": lstate,
            "by_id": {w["id"]: w for w in active + closed}, "convs": {c["id"]: c for c in convs if isinstance(c, dict)}}


def tab_state(tasks: dict[str, Any], needs_human: int, busy: int, has_convs: bool) -> tuple[str, str]:
    """Trạng thái một tab luồng → (lớp chấm, chữ cho tooltip/đọc màn hình). Ưu tiên: cần người > đang chạy > còn task chờ > xong.
    "Đang chạy" = điều phối viên đang trả lời/xử lý trong luồng HOẶC có task của luồng đang chạy."""
    running = int(tasks.get("running") or 0)
    if tasks.get("blocked") or needs_human:
        parts = [f"{tasks['blocked']} task bị chặn"] if tasks.get("blocked") else []
        parts += [f"{needs_human} việc cần bạn"] if needs_human else []
        return "danger", "Cần bạn — " + ", ".join(parts)
    if busy or running:
        parts = ["điều phối viên đang xử lý"] if busy else []
        parts += [f"{running} task đang chạy"] if running else []
        return "run", "Đang chạy — " + ", ".join(parts)
    if tasks.get("open"):
        return "idle", f"Đang chờ — {tasks['open']} task chưa xong, không có gì đang chạy"
    if tasks.get("total") or has_convs:
        return "done", "Đã xong — không có gì đang chạy"
    return "none", "Chưa có việc"


async def _actions(request: Request, agent_id: str) -> list[dict[str, Any]]:
    """Thẻ `manager.action` gần nhất của điều phối viên (event lưu DB), cũ → mới."""
    try:
        evs = await rpc(request, "events.query", {"agent_id": agent_id, "limit": ACTIONS_SCAN}) or []
    except RpcError:
        return []
    acts = [P.mgr_action_item(e) for e in evs if e.get("type") == "manager.action"]
    return sorted(acts, key=lambda a: str(a.get("ts") or ""))


def _timeline(msgs: list[dict[str, Any]], acts: list[dict[str, Any]], qs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Tin nhắn + thẻ hành động của các run thuộc hội thoại (+ câu hỏi của điều phối viên), theo thời gian. Thẻ hành động liên tiếp của
    cùng một lượt gom thành MỘT khối "Đã làm N việc" (UX v2)."""
    runs = {m.get("run_id") for m in msgs if m.get("run_id")}
    items = list(msgs) + [a for a in acts if a.get("run_id") in runs]
    items += [{"kind": "question", "q": q, "ts": q.get("created_at"), "run_id": q.get("run_id")} for q in qs or []]

    def key(it: dict[str, Any]) -> tuple[str, int]:
        return (str(it.get("ts") or ""), {"user": 0, "note": 0, "action": 1, "manager": 2, "question": 3}.get(it["kind"], 4))

    out: list[dict[str, Any]] = []
    for it in sorted(items, key=key):
        prev = out[-1] if out else None
        if it["kind"] == "action":
            if prev and prev["kind"] == "actions" and prev.get("run_id") == it.get("run_id"):
                prev["items"].append(it)
                prev["ok"] = prev["ok"] and it["ok"]
                continue
            out.append({"kind": "actions", "run_id": it.get("run_id"), "items": [it], "ok": it["ok"], "ts": it.get("ts")})
            continue
        out.append(it)
    return with_days(out)


def with_days(items: list[dict[str, Any]], today: Any = None) -> list[dict[str, Any]]:
    """P2: vạch ngăn theo ngày ("Hôm nay", "Hôm qua", "Thứ Hai, 10/11/2026") trước mục ĐẦU TIÊN của mỗi ngày (mục không có giờ giữ ngày
    trước). Tin tới qua SSE sau nửa đêm không tự thêm vạch (tải lại trang thì có)."""
    out: list[dict[str, Any]] = []
    cur = ""
    for it in items:
        key, lab = P.day_label(it.get("ts"), today)
        if key and key != cur:
            out.append({"kind": "day", "key": key, "text": lab})
            cur = key
        out.append(it)
    return out


def pending_info(events: list[dict[str, Any]]) -> dict[str, Any]:
    """P2: tải lại trang khi lượt điều phối viên CÒN chạy → khung "đang trả lời" hiện ngay tool đang dùng (hoặc vừa dùng) và checklist
    TodoWrite gần nhất, lấy từ event đã lưu (`events.query` theo run). Chữ thuần (template autoescape)."""
    open_: dict[str, str] = {}
    last = ""
    todo: dict[str, Any] | None = None
    for ev in sorted(events, key=lambda e: (str(e.get("ts") or ""), int(e.get("seq") or 0))):
        if ev.get("subagent_id"):
            continue
        t = ev.get("type")
        p = ev.get("payload") or {}
        if t == "tool.start":
            ti = P.todo_item(ev)
            if ti:
                todo = ti
                continue
            inp = p.get("input") if isinstance(p.get("input"), dict) else None
            v = P.tool_view(str(p.get("name") or "?"), inp, summary=str(p.get("args_preview") or ""))
            last = " ".join(x for x in (v.get("verb"), v.get("target")) if x)[:120] or short_tool(str(p.get("name") or "?"))
            open_[str(p.get("tool_id") or "")] = last
        elif t == "tool.complete":
            open_.pop(str(p.get("tool_id") or ""), None)
    if open_:
        label = "Đang dùng " + list(open_.values())[-1] + (f" (+{len(open_) - 1})" if len(open_) > 1 else "") + "…"
    else:
        label = f"Vừa dùng {last}" if last else ""
    return {"tool": label, "todo": todo}


def question_from_event(ev: dict[str, Any]) -> dict[str, Any]:
    """Event `question.*` (payload `wire`) → dict câu hỏi đủ cho thẻ (không cần gọi lại RPC trong luồng SSE)."""
    p = ev.get("payload") or {}
    t = ev.get("type") or ""
    q = {"id": p.get("question_id"), "agent_id": ev.get("agent_id"), "run_id": ev.get("run_id"), "task_id": p.get("task_id"),
         "conversation_id": p.get("conversation_id"), "text": p.get("text") or "", "options": p.get("options") or [], "multi": bool(p.get("multi")),
         "allow_free_text": p.get("allow_free_text") is not False, "status": p.get("status") or "open", "routed_to": p.get("routed_to"),
         "created_at": ev.get("ts"), "escalation_note": p.get("note")}
    if t == "question.answered":
        q.update({"status": "answered", "answer": {"keys": p.get("keys") or [], "labels": p.get("labels") or [], "text": p.get("text") or ""},
                  "answered_by": p.get("by"), "text": ""})
    elif t == "question.cancelled":
        q["status"] = "cancelled"
    return q


async def manager_page(request: Request) -> Response:
    q = request.query_params
    st = await mgr_state(request)
    ctx: dict[str, Any] = {"nav": "manager", "m": st, "convs": [], "conv": None, "items": [], "side_actions": [], "pending": None,
                           "agents": [], "templates": []}
    status = await rpc(request, "hub.status")
    ctx.update({"since": status.get("last_seq") or 0, "epoch": status.get("epoch") or ""})
    if not st["available"] or not st["agent_id"]:
        ctx["agents"] = [a for a in await rpc(request, "agent.list", {}) or [] if not a.get("archived_at")]
        try:
            ctx["templates"] = [t for t in await rpc(request, "template.list") or [] if str(t.get("name") or "").startswith("manager")]
        except RpcError:
            pass
        return await render(request, "manager.html", ctx)
    convs = await _web_convs(request)
    wsd = await _workstreams(request)
    ws_id = str(q.get("w") or "").strip() or None
    if q.get("c") and not ws_id:  # hội thoại thuộc luồng → hiện trong ngữ cảnh luồng
        ws_id = (wsd.get("convs", {}).get(q.get("c")) or {}).get("workstream_id")
    if ws_id and ws_id not in wsd.get("by_id", {}):
        try:  # tra theo mã (vd ?w=LH-55166)
            ws_id = (await rpc(request, "workstream.get", {"id": ws_id}) or {}).get("id")
        except RpcError:
            ws_id = None
    if ws_id and not q.get("c") and not q.get("new"):  # ?w= → hội thoại web gần nhất của luồng (chưa có → mới trong luồng)
        try:
            wc = await rpc(request, "manager.conversations", {"channel": CHANNEL, "workstream": ws_id, "limit": 1}) or []
        except RpcError:
            wc = []
        cid = wc[0]["id"] if wc else None
    else:
        lobby = wsd.get("lobby") if wsd.get("enabled") else convs
        cid = None if q.get("new") else (q.get("c") or (lobby[0]["id"] if lobby else None))
    msgs: list[dict[str, Any]] = []
    if cid:
        try:
            raw = await rpc(request, "manager.history", {"conversation_id": cid, "limit": HISTORY_LIMIT}) or []
            msgs = [m for m in (P.mgr_message(x) for x in raw if isinstance(x, dict)) if m]
        except RpcError as e:
            if q.get("c"):
                sess = session_of(request)
                if sess:
                    sess.flash("error", f"Không mở được cuộc trò chuyện {cid}: {err_text(e)}")
            cid = None
    acts = await _actions(request, st["agent_id"])
    from .app import questions as _questions

    conv_qs = [q for q in await _questions(request, "all", agent_id=st["agent_id"]) if cid and q.get("conversation_id") == cid and q["status"] != "cancelled"]
    items = _timeline(msgs, acts, conv_qs)
    # tin cuối của người chưa có trả lời + run còn chạy → khung "đang trả lời" (chữ chạy tới qua SSE)
    pending = None
    pinfo: dict[str, Any] = {}
    want = q.get("run") or next((m.get("run_id") for m in reversed(msgs) if m["kind"] == "user"), None)
    if want and not any(m["kind"] == "manager" and m.get("run_id") == want for m in msgs):
        try:
            run = await rpc(request, "run.get", {"id": want})
            if P.is_live_run(run):
                pending = run["id"]
                pinfo = pending_info(await rpc(request, "events.query", {"run_id": pending, "limit": PENDING_SCAN}) or [])
        except RpcError:
            pass
    wsx = None
    if ws_id:
        try:
            wsx = await rpc(request, "workstream.get", {"id": ws_id})
        except RpcError:
            wsx = None
    if cid:
        try:
            await rpc(request, "manager.conversation_read", {"conversation_id": cid})
            for c in wsd.get("lobby") or []:
                if c["id"] == cid:
                    c["unread"] = 0
        except (RpcError, DaemonUnavailable):
            pass
    cur_conv = (wsd.get("convs") or {}).get(cid) if cid else None
    ws_convs = [P.mgr_conv(c) | {"channel": c.get("channel"), "running": bool(c.get("running"))} for c in (wsd.get("convs") or {}).values()
                if wsx and c.get("workstream_id") == wsx["id"] and c.get("channel") != "heartbeat"]
    ws_convs.sort(key=lambda c: str(c.get("updated_at") or ""), reverse=True)
    ctx.update({"convs": convs, "conv": cid, "items": items, "side_actions": list(reversed(acts))[:12], "pending": pending, "pinfo": pinfo,
                "conv_runs": sorted({m["run_id"] for m in msgs if m.get("run_id")} | ({pending} if pending else set())),
                "wsd": wsd, "wsx": wsx, "ws_convs": ws_convs, "cur_conv": cur_conv,
                "ws_web_convs": [c | {"unread": 0 if c["id"] == cid else int(((wsd.get("convs") or {}).get(c["id"]) or {}).get("unread") or 0)}
                                 for c in ws_convs if c.get("channel") == CHANNEL],
                "cur_title": (cur_conv or {}).get("title")})
    return await render(request, "manager.html", ctx)


async def partial_tabs(request: Request) -> Response:
    """Hàng tab luồng (htmx/app.js làm mới khi có run bắt đầu/kết thúc, tin trả lời, luồng đổi — và hỏi lại định kỳ cho task)."""
    cid = str(request.query_params.get("c") or "").strip()
    if cid:  # hội thoại đang mở trên màn hình: tin mới tới trong lúc xem không tính là chưa đọc (giống lúc tải trang)
        try:
            await rpc(request, "manager.conversation_read", {"conversation_id": cid})
        except (RpcError, DaemonUnavailable):
            pass
    wsd = await _workstreams(request)
    wid = str(request.query_params.get("w") or "").strip()
    wsx = (wsd.get("by_id") or {}).get(wid) if wid else None
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_manager_tabs.html", {"wsd": wsd, "wsx": wsx, "csrf": sess.csrf if sess else ""},
                                                  headers={"Cache-Control": "no-store"})


async def partial_status(request: Request) -> Response:
    st = await mgr_state(request)
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_manager_status.html", {"m": st, "csrf": sess.csrf if sess else "",
                                                                                    "compact": request.query_params.get("compact") == "1"})


def _mod(request: Request) -> Any:
    """Module macro của _manager.html KÈM ngữ cảnh csrf (thẻ câu hỏi có form) — khoá phiên không bao giờ vào HTML."""
    sess = session_of(request)
    return ws(request).templates.env.get_template("_manager.html").make_module({"csrf": sess.csrf if sess else "", "path": "/manager"})


async def manager_send(request: Request) -> Response:
    _, form = await post_guard(request)
    want_json = bool(request.headers.get("x-ahub-fetch"))
    text = str(form.get("text") or "").strip()
    cid = str(form.get("c") or "").strip() or None
    wsid = str(form.get("w") or "").strip() or None
    back = "/manager" + (f"?c={cid}" if cid else f"?w={wsid}&new=1" if wsid else "")

    def fail(msg: str, code: int = 400) -> Response:
        if want_json:
            return JSONResponse({"error": msg}, status_code=code)
        return flash_redirect(request, back, "error", msg)

    images, files, names, errors = await _attachments(form)
    if errors:
        return fail("Không gửi được tệp đính kèm — " + "; ".join(errors))
    if not text and not names:
        return fail("Chưa nhập nội dung")
    if len(text) > MAX_TEXT:
        return fail(f"Tin quá dài ({len(text)} ký tự, tối đa {MAX_TEXT})")
    params: dict[str, Any] = {"channel": CHANNEL, "text": text}
    voice = str(form.get("voice") or "").strip()
    if voice in ("dictation", "conversation"):  # tin nói bằng giọng (static/voice.js) — daemon bỏ phím tắt duyệt, thêm ghi chú
        params["voice"] = voice
    if cid:
        params["conversation_id"] = cid
    elif wsid:
        params["workstream"] = wsid
    if images:
        params["images"] = images
    if files:
        params["files"] = files
    try:
        res = await rpc(request, "manager.chat", params, audit=True) or {}
    except RpcError as e:
        if e.code == METHOD_NOT_FOUND:
            return fail("Daemon này chưa có chức năng điều phối viên (manager.*) — cập nhật cfharness.")
        return fail(err_text(e))
    except DaemonUnavailable as e:
        return fail(err_text(e), 503)
    conv, run_id = res.get("conversation_id") or cid, res.get("run_id")
    if want_json:
        mod = _mod(request)
        user = {"kind": "user", "text": (text + (f"\n📎 {', '.join(names)}" if names else "")).strip(), "ts": None, "run_id": run_id}
        return JSONResponse({"conversation_id": conv, "run_id": run_id, "steered": bool(res.get("steered")), "html_user": str(mod.item(user)),
                             "html_pending": str(mod.pending(run_id)) if run_id else ""})
    return RedirectResponse(f"/manager?c={conv}" + (f"&run={run_id}" if run_id else ""), status_code=303)


async def manager_stop(request: Request) -> Response:
    """P0-6: nút Dừng của khung "đang trả lời". `post_guard` (CSRF + Origin + khoá phiên WT-1) → chỉ dừng run CỦA ĐIỀU PHỐI VIÊN thuộc
    một hội thoại điều phối (run khác → 403) → `run.stop` với `_audit.channel=web`. Không hộp xác nhận (dừng an toàn, gửi lại được)."""
    _, form = await post_guard(request)
    want_json = bool(request.headers.get("x-ahub-fetch"))
    rid = str(form.get("run") or "").strip()
    cid = str(form.get("c") or "").strip() or None
    back = "/manager" + (f"?c={cid}" if cid else "")

    def done(msg: str, code: int = 200, kind: str = "ok") -> Response:
        if want_json:
            return JSONResponse({"ok": code == 200, **({"error": msg} if code != 200 else {"message": msg})}, status_code=code)
        return flash_redirect(request, back, kind if code == 200 else "error", msg)

    if not rid:
        return done("Thiếu lượt cần dừng", 400)
    st = await mgr_state(request)
    try:
        run = await rpc(request, "run.get", {"id": rid}) or {}
    except RpcError as e:
        return done(err_text(e), 404)
    except DaemonUnavailable as e:
        return done(err_text(e), 503)
    opts = run.get("run_opts") if isinstance(run.get("run_opts"), dict) else {}
    in_conv = bool(opts.get("manager_conversation"))
    if not in_conv and cid:  # daemon chưa ghi run_opts (stub/bản cũ) → run phải nằm trong lịch sử hội thoại đang mở
        try:
            hist = await rpc(request, "manager.history", {"conversation_id": cid, "limit": HISTORY_LIMIT}) or []
            in_conv = any(isinstance(m, dict) and m.get("run_id") == run.get("id") for m in hist)
        except RpcError:
            in_conv = False
    if not st.get("agent_id") or run.get("agent_id") != st["agent_id"] or not in_conv:
        return done("Chỉ dừng được lượt trả lời của điều phối viên trong hội thoại này", 403)
    if not P.is_live_run(run):
        return done("Lượt này đã kết thúc")
    try:
        await rpc(request, "run.stop", {"id": run["id"]}, audit=True)
    except RpcError as e:
        return done(err_text(e), 409)
    except DaemonUnavailable as e:
        return done(err_text(e), 503)
    return done("Đã dừng điều phối viên")


DRAFT_EVERY_S = 0.15  # P1-1: bản nháp markdown của câu trả lời đang chạy chữ — tối đa ~7 lần/giây mỗi luồng SSE
DRAFT_MAX = 24_000  # chữ đang chạy dài hơn → thôi gửi nháp (đỡ CPU), client chỉ nối delta thô
STREAM_KEEP = 200_000  # trần bộ đệm chữ của một lượt (RAM)


def _norm(s: str) -> str:
    return " ".join(s.split())


def _converter(request: Request, clock: Any = None) -> Any:
    """Event → message SSE của trang Điều phối. MỌI message mang `seq` của event gốc (client bỏ trùng khi nối lại).
    Chữ của điều phối viên: `delta` = chữ thô (client gán textContent); `draft`/`reply` = HTML của `present.md()` (đã cứng hoá) —
    chỉ đầu ra này được chèn bằng innerHTML. `think`/`tool` không bao giờ mang HTML (client gán textContent)."""
    import time as _time

    mod = _mod(request)
    now = clock or _time.monotonic
    bufs: dict[str, dict[str, Any]] = {}  # run → {done: [đoạn đã xong], cur: đoạn đang chạy, at: lần gửi nháp cuối}

    def full(b: dict[str, Any]) -> str:
        parts = [x for x in b["done"] if x.strip()] + ([b["cur"]] if b["cur"].strip() else [])
        return "\n\n".join(parts)

    def buf(rid: str) -> dict[str, Any]:
        b = bufs.get(rid)
        if b is None:
            b = bufs[rid] = {"done": [], "cur": "", "at": 0.0}
            while len(bufs) > 16:  # lượt cũ không có `manager.reply` (bị huỷ…) — đừng giữ mãi
                bufs.pop(next(iter(bufs)))
        return b

    def draft(rid: str, seq: Any, b: dict[str, Any], force: bool = False) -> list[dict[str, Any]]:
        text = full(b)
        if not text.strip() or len(text) > DRAFT_MAX or (not force and now() - b["at"] < DRAFT_EVERY_S):
            return []
        b["at"] = now()
        return [{"t": "draft", "run": rid, "seq": seq, "html": P.md(text)}]

    def conv(ev: dict[str, Any]) -> list[dict[str, Any]]:
        out = _conv(ev)
        for m in out:
            m.setdefault("seq", ev.get("seq"))
        return out

    def _conv(ev: dict[str, Any]) -> list[dict[str, Any]]:
        t = ev.get("type") or ""
        p = ev.get("payload") or {}
        rid = ev.get("run_id")
        seq = ev.get("seq")
        sub = bool(ev.get("subagent_id"))
        if t == "message.delta":
            if sub:
                return []
            text = str(p.get("text") or "")
            out: list[dict[str, Any]] = [{"t": "delta", "run": rid, "text": text}]
            if rid:
                b = buf(rid)
                if len(b["cur"]) + sum(len(x) for x in b["done"]) < STREAM_KEEP:
                    b["cur"] += text
                out += draft(rid, seq, b)
            return out
        if t == "message.complete":  # hết một đoạn trả lời (giữa các lần dùng tool) → client xuống đoạn mới
            if sub or not rid:
                return []
            b = buf(rid)
            b["done"].append(str(p.get("text") or "") or b["cur"])
            b["cur"] = ""
            return [{"t": "seg", "run": rid}] + draft(rid, seq, b, force=True)
        if t == "reasoning.delta":
            if sub:
                return []
            return [{"t": "think", "run": rid, "text": str(p.get("text") or "")}]
        if t in ("tool.start", "tool.complete"):
            if sub:
                return []
            name = str(p.get("name") or "?")
            inp = p.get("input") if isinstance(p.get("input"), dict) else None
            v = P.tool_view(name, inp, summary=str(p.get("args_preview") or "")) if t == "tool.start" else {}
            label = " ".join(x for x in (v.get("verb"), v.get("target")) if x)[:120]
            msgs = [{"t": "tool", "run": rid, "id": str(p.get("tool_id") or ""), "name": short_tool(name), "label": label or short_tool(name),
                     "done": t == "tool.complete", "error": bool(p.get("is_error"))}]
            todo = P.todo_item(ev)
            if todo:  # P2: checklist TodoWrite trong khung "đang trả lời" — HTML do server vẽ (macro tx_todo, autoescape)
                txm = ws(request).templates.env.get_template("_transcript.html").module
                msgs.append({"t": "todo", "run": rid, "html": str(txm.tx_todo(todo))})
            return msgs
        if t == "manager.reply":
            run = p.get("run_id") or rid
            notify = p.get("kind") == "notify" or bool(p.get("notify"))
            text = str(p.get("text") or "")
            if not notify and run in bufs:
                # câu trả lời cuối (`run.summary`) thường chỉ là đoạn CUỐI — giữ toàn bộ chữ đã chạy (lời dẫn + các đoạn) nếu nó chứa câu trả lời
                b = bufs.pop(run)
                streamed = full(b)
                if streamed.strip():
                    text = streamed if _norm(text) in _norm(streamed) else streamed + "\n\n" + text
            it = {"kind": "manager", "text": text, "ts": ev.get("ts"), "run_id": run, "notify": notify, "tick": p.get("kind") == "tick",
                  "agent": ev.get("agent_id"),
                  "cut": P.cut_note("Tin", len(str(p.get("text") or "")), run) if p.get("truncated") else ""}
            return [{"t": "reply", "run": run, "conv": p.get("conversation_id"), "notify": notify, "html": str(mod.item(it))}]
        if t == "manager.action":
            it = P.mgr_action_item(ev)
            group = {"kind": "actions", "run_id": rid, "items": [it], "ok": it["ok"], "ts": it.get("ts")}
            return [{"t": "action", "run": rid, "html": str(mod.item(group)), "one": str(mod.item(it)), "ok": it["ok"], "side": str(mod.side_action(it))}]
        if t.startswith("question.") and t != "question.reminder":
            q = question_from_event(ev)
            if not q.get("conversation_id"):
                return []
            if t == "question.answered":  # thẻ đã trả lời: lấy lại nội dung câu hỏi từ thẻ đang có phía trình duyệt — chỉ thay trạng thái
                return [{"t": "question", "conv": q["conversation_id"], "dom": f"q-{q['id']}", "answered": True, "html": str(mod.answered(q))}]
            return [{"t": "question", "conv": q["conversation_id"], "dom": f"q-{q['id']}", "html": str(mod.item({"kind": "question", "q": q}))}]
        if t in TERMINAL_RUN_EVENTS and ev.get("source") == "hub" and rid:
            outcome = p.get("outcome") or t.removeprefix("run.")
            label, tone = P.run_status(str(outcome))
            note = {"kind": "note", "tone": tone, "run_id": rid, "text": f"Lượt xử lý kết thúc: {label}"}
            return [{"t": "run_end", "run": rid, "ok": outcome == "completed", "html": str(mod.item(note))}]
        if t.startswith("manager.tick"):
            return [{"t": "tick"}]
        if t in ("run.queued", "run.started", "workstream.updated"):
            return [{"t": "status", "run": rid}]
        return []

    return conv


async def sse_manager(request: Request) -> Response:
    st = await mgr_state(request)
    if not st.get("agent_id"):
        raise HTTPException(404, "Chưa có điều phối viên")
    q = request.query_params
    params = {"agent_id": st["agent_id"], "since_seq": int(q.get("since") or 0), "epoch": q.get("epoch") or None, "level": "detail"}
    return StreamingResponse(_stream(request, params, _converter(request)), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def mgr_set_confirm(f: Any) -> str:
    aid = str(f.get("agent_id") or "?")
    return (f"Chỉ định agent {aid} làm ĐIỀU PHỐI VIÊN?\n\nĐiều phối viên được TỰ tạo/sửa/xoá mềm agent và team, đổi tool/MCP/model, giao task, "
            "chạy/dừng run — không cần hỏi bạn. Nó KHÔNG được: duyệt thao tác phá huỷ, sửa chính sách/guard/golden task, ngoại lệ sandbox, "
            "xoá vĩnh viễn, tắt ESTOP, sửa cấu hình của chính nó, đặt bypassPermissions. Mọi thao tác ghi nhật ký `manager:" + aid + "` và rollback được.")


def _tick_ok(r: Any, f: Any) -> str:
    r = r or {}
    if r.get("run_id"):
        return f"Đã chạy một nhịp — run {r['run_id']} ({r.get('events', 0)} sự kiện đang chờ)"
    return f"Nhịp không chạy: {TICK_SKIP.get(str(r.get('skipped') or ''), r.get('skipped') or '?')}"


TICK_SKIP = {"no_manager": "chưa có điều phối viên", "estop": "ESTOP đang bật", "manager_missing": "agent điều phối viên không còn",
             "manager_paused": "agent điều phối viên đang tạm dừng", "manager_busy": "điều phối viên đang bận một lượt khác — sự kiện giữ cho nhịp sau",
             "heartbeat_off": "nhịp đang tắt (manager.heartbeat)", "nothing_to_do": "không có việc gì"}


def _ws_goto(res: Any, f: Any) -> str:
    wid = (res or {}).get("id") or f.get("id")
    return f"/manager?w={wid}" if wid else "/manager"


def _conv_goto(res: Any, f: Any) -> str:
    return f"/manager?c={(res or {}).get('id') or f.get('conversation_id')}"


def _opt(f: Any, k: str) -> str | None:
    v = str(f.get(k) or "").strip()
    return v or None


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    """Thêm thao tác ghi của trang Điều phối vào bảng `ACTIONS` (HUMAN_ONLY ở daemon). Chỉ định/huỷ, đóng luồng, lưu trữ hội thoại
    qua hộp xác nhận; tạo/sửa luồng, STATE, đổi tên/chuyển hội thoại làm ngay (dễ sửa lại)."""
    # chạy nhịp ngay (manager.tick force) — HUMAN_ONLY, daemon tự audit; một run thật nên luôn qua hộp xác nhận
    actions["manager_tick"] = Action(
        "manager.tick", lambda f: {"force": True}, _tick_ok,
        confirm="Chạy một nhịp của điều phối viên NGAY?\nĐiều phối viên xem các sự kiện đang chờ + việc còn mở và xử lý (một run thật, tốn tiền "
                "như một nhịp thường). Không chạy khi ESTOP bật hoặc nó đang bận.",
        goto=lambda r, f: f"/manager?c={r['conversation_id']}" if (r or {}).get("conversation_id") else "/manager")
    actions["ws_create"] = Action(
        "workstream.create", lambda f: {"title": _opt(f, "title"), "key": _opt(f, "key"), "sprint": _opt(f, "sprint")},  # tên trống → hub tự đặt
        lambda r, f: f"Đã mở luồng {(r or {}).get('label') or ''}", goto=_ws_goto)
    actions["ws_edit"] = Action(
        "workstream.update", lambda f: {"id": _req(f, "id", "luồng"), "title": _req(f, "title", "tên luồng")},  # "LH-1 Tên" → đổi cả mã
        "Đã lưu luồng", goto=_ws_goto)
    actions["ws_state"] = Action(
        "workstream.update", lambda f: {"id": _req(f, "id", "luồng"), "state": str(f.get("state") or "")},
        "Đã lưu STATE.md — điều phối viên đọc lại ở session mới", goto=_ws_goto)
    actions["ws_close"] = Action(
        "workstream.update", lambda f: {"id": _req(f, "id", "luồng"), "status": "closed"}, "Đã đóng luồng",
        confirm=lambda f: f"Đóng luồng {f.get('label') or f.get('id')}?\nTask đang chạy vẫn chạy tiếp; luồng chuyển xuống mục Đã đóng và mở lại được.",
        goto=lambda r, f: "/manager")
    actions["ws_delete"] = Action(
        "workstream.delete", lambda f: {"id": _req(f, "id", "luồng")},
        lambda r, f: f"Đã xoá luồng {(r or {}).get('label') or ''} — {((r or {}).get('detached') or {}).get('conversations', 0)} hội thoại về Sảnh, "
                     f"{((r or {}).get('detached') or {}).get('tasks', 0)} task bỏ gắn luồng",
        confirm=lambda f: (f"XOÁ HẲN luồng {f.get('label') or f.get('id')}?\nHội thoại của luồng chuyển về Sảnh, task giữ nguyên nhưng bỏ gắn luồng. "
                           "Ghi chú luồng (STATE.md) chỉ còn trong nhật ký audit. Không hoàn tác được — muốn cất đi thì dùng Đóng luồng."),
        danger=True, goto=lambda r, f: "/manager")
    actions["ws_reopen"] = Action(
        "workstream.update", lambda f: {"id": _req(f, "id", "luồng"), "status": "active"}, "Đã mở lại luồng", goto=_ws_goto)
    actions["conv_rename"] = Action(
        "manager.conversation_update", lambda f: {"conversation_id": _req(f, "conversation_id", "hội thoại"), "title": _req(f, "title", "tên")},
        "Đã đổi tên hội thoại", goto=_conv_goto)
    actions["conv_move"] = Action(
        "manager.conversation_update", lambda f: {"conversation_id": _req(f, "conversation_id", "hội thoại"), "workstream": str(f.get("workstream") or "")},
        lambda r, f: "Đã chuyển hội thoại " + ("về Sảnh" if not f.get("workstream") else "sang luồng"), goto=_conv_goto)
    actions["conv_archive"] = Action(
        "manager.conversation_update", lambda f: {"conversation_id": _req(f, "conversation_id", "hội thoại"), "archived": True},
        "Đã lưu trữ hội thoại", confirm="Lưu trữ hội thoại này? Nó biến khỏi danh sách (lịch sử vẫn giữ trong hub).",
        goto=lambda r, f: f"/manager?w={f.get('w')}" if f.get("w") else "/manager")
    actions["manager_set"] = Action(
        "manager.set", lambda f: {"agent_id": _req(f, "agent_id", "agent")},
        lambda r, f: f"Đã chỉ định {f.get('agent_id')} làm điều phối viên", confirm=mgr_set_confirm, danger=True,
        goto=lambda r, f: "/manager")
    actions["manager_unset"] = Action(
        "manager.set", lambda f: {"agent_id": None}, "Đã huỷ chỉ định điều phối viên — hub không còn điều phối viên",
        confirm=lambda f: f"Huỷ chỉ định điều phối viên {f.get('current') or ''}?\nAgent vẫn còn (chỉ mất vai trò); nhịp tick dừng, "
                          "tin chat/Telegram không còn được chuyển cho nó.", danger=True, goto=lambda r, f: "/manager")


def routes() -> list[Any]:
    from starlette.routing import Route

    return [
        Route("/manager", manager_page),
        Route("/manager/send", manager_send, methods=["POST"]),
        Route("/manager/stop", manager_stop, methods=["POST"]),
        Route("/partials/manager", partial_status),
        Route("/partials/manager/tabs", partial_tabs),
        Route("/sse/manager", sse_manager),
    ]


__all__ = ["manager_page", "manager_send", "manager_stop", "sse_manager", "partial_status", "mgr_state", "register_actions", "routes", "json"]
