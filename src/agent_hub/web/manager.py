"""Trang "Điều phối" (M7 — luồng MGR-UI): chat với điều phối viên (agent manager) qua conversation kênh `web`.

RPC dùng (hợp đồng `docs/plan/M7-manager.md`, MGR-CORE cung cấp): `manager.get|set|chat|conversations|history`, event
`manager.reply{conversation_id, run_id, text}`, `manager.action{tool, target, summary, audit_id}`; cộng `events.subscribe`
(agent = điều phối viên, level detail) cho chữ chạy (`message.delta`) và kết thúc run.

- `GET /manager[?c=<conv>|?new=1][&run=<run>]`: lịch sử + thẻ hành động + nhịp tick + ô nhập. Chưa có điều phối viên → hướng dẫn
  + form chỉ định (action `manager_set`, HUMAN_ONLY, hộp xác nhận).
- `POST /manager/send`: `post_guard` (CSRF + Origin + khoá phiên WT-1) → `manager.chat(channel=web)` (`_audit.channel=web`).
  Có header `X-Ahub-Fetch` (app.js) → JSON (mục đã vẽ sẵn phía server); không → 303 về trang (form thuần).
- `GET /sse/manager?since=&epoch=`: luồng JSON `{t: delta|reply|action|run_end|tick}`; mục HTML vẽ phía server bằng macro autoescape.
- `GET /partials/manager`: khung trạng thái (htmx làm mới).
Mọi chữ của điều phối viên là DỮ LIỆU: autoescape + `white-space: pre-wrap`, không markdown/HTML thô (test XSS).
"""

from __future__ import annotations

import json
from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse

from ..events import TERMINAL_RUN_EVENTS
from ..rpc import METHOD_NOT_FOUND, DaemonUnavailable, RpcError
from . import present as P
from .app import CHANNEL, _stream, err_text, flash_redirect, post_guard, render, rpc, session_of, ws

HISTORY_LIMIT = 200
ACTIONS_SCAN = 3000  # số event gần nhất của điều phối viên quét để lấy thẻ `manager.action`
MAX_TEXT = 8000


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
        return (str(it.get("ts") or ""), {"user": 0, "action": 1, "manager": 2, "question": 3}.get(it["kind"], 4))

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
    return out


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
    cid = None if q.get("new") else (q.get("c") or (convs[0]["id"] if convs else None))
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
    want = q.get("run") or next((m.get("run_id") for m in reversed(msgs) if m["kind"] == "user"), None)
    if want and not any(m["kind"] == "manager" and m.get("run_id") == want for m in msgs):
        try:
            run = await rpc(request, "run.get", {"id": want})
            if P.is_live_run(run):
                pending = run["id"]
        except RpcError:
            pass
    ctx.update({"convs": convs, "conv": cid, "items": items, "side_actions": list(reversed(acts))[:12], "pending": pending,
                "conv_runs": sorted({m["run_id"] for m in msgs if m.get("run_id")} | ({pending} if pending else set()))})
    return await render(request, "manager.html", ctx)


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
    back = "/manager" + (f"?c={cid}" if cid else "")

    def fail(msg: str, code: int = 400) -> Response:
        if want_json:
            return JSONResponse({"error": msg}, status_code=code)
        return flash_redirect(request, back, "error", msg)

    if not text:
        return fail("Chưa nhập nội dung")
    if len(text) > MAX_TEXT:
        return fail(f"Tin quá dài ({len(text)} ký tự, tối đa {MAX_TEXT})")
    params: dict[str, Any] = {"channel": CHANNEL, "text": text}
    if cid:
        params["conversation_id"] = cid
    try:
        res = await rpc(request, "manager.chat", params, audit=True) or {}
    except RpcError as e:
        if e.code == METHOD_NOT_FOUND:
            return fail("Daemon này chưa có chức năng điều phối viên (manager.*) — cập nhật agent-hub.")
        return fail(err_text(e))
    except DaemonUnavailable as e:
        return fail(err_text(e), 503)
    conv, run_id = res.get("conversation_id") or cid, res.get("run_id")
    if want_json:
        mod = _mod(request)
        user = {"kind": "user", "text": text, "ts": None, "run_id": run_id}
        return JSONResponse({"conversation_id": conv, "run_id": run_id, "steered": bool(res.get("steered")), "html_user": str(mod.item(user)),
                             "html_pending": str(mod.pending(run_id)) if run_id else ""})
    return RedirectResponse(f"/manager?c={conv}" + (f"&run={run_id}" if run_id else ""), status_code=303)


def _converter(request: Request) -> Any:
    mod = _mod(request)

    def conv(ev: dict[str, Any]) -> list[dict[str, Any]]:
        t = ev.get("type") or ""
        p = ev.get("payload") or {}
        rid = ev.get("run_id")
        if t == "message.delta":
            if ev.get("subagent_id"):
                return []
            return [{"t": "delta", "run": rid, "text": str(p.get("text") or "")}]
        if t == "manager.reply":
            run = p.get("run_id") or rid
            notify = p.get("kind") == "notify" or bool(p.get("notify"))
            it = {"kind": "manager", "text": str(p.get("text") or ""), "ts": ev.get("ts"), "run_id": run, "notify": notify,
                  "tick": p.get("kind") == "tick"}
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


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    """Thêm thao tác ghi của trang Điều phối vào bảng `ACTIONS` (HUMAN_ONLY ở daemon; luôn qua hộp xác nhận)."""
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
        Route("/partials/manager", partial_status),
        Route("/sse/manager", sse_manager),
    ]


__all__ = ["manager_page", "manager_send", "sse_manager", "partial_status", "mgr_state", "register_actions", "routes", "json"]
