"""Trang web bổ sung cho ngang CLI/daemon (docs/dev-notes/web-extra-pages.md).

- **Lịch chạy** `/schedules`: danh sách `schedule.list`, tạo (`schedule.preview` kiểm biểu thức + hộp xác nhận phía server → `schedule.create`),
  tạm dừng / bật lại / chạy ngay / xoá (mềm) qua `ACTIONS` (`schedule.*` HUMAN_ONLY, daemon tự audit theo `_audit.channel=web`).
- **Tìm kiếm hội thoại** `/search`: `session.search` (FTS5) — lọc agent / khoảng thời gian / vai trò, đoạn trích tô `«…»` thành `<mark>`
  (không dùng `|safe`: tách đoạn ở server, template autoescape từng đoạn).
- **Thay đổi file của run**: `/partials/run/{id}/diff` (trang run tải lười bằng htmx) + `/runs/{id}/diff` (không cần JS) — `run.diff{patch}`,
  diff unified tách theo file, gập/mở, có trần kích thước; "Hoàn tác" = `run.revert` (hộp xác nhận nguy hiểm), "Kiểm tra" = `dry_run`.
- **Sức khoẻ** `/partials/health` (thẻ ở Cài đặt): chỉ RPC đọc sẵn có `hub.status` + `hub.compat` + `guard.integrity.status`
  (không có RPC `doctor` — `cfharness doctor` chạy `claude` cục bộ, không thêm RPC nặng).

Module này không đọc/ghi `hub.db`; chỉ gọi RPC như phần còn lại của web. `app.py` chỉ thêm route + `register_actions`.
"""

from __future__ import annotations

import re
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from ..rpc import METHOD_NOT_FOUND, DaemonUnavailable, RpcError
from .app import carry, err_text, flash_redirect, post_guard, render, rpc, session_of, ws

# ============================================================================ lịch chạy
KINDS = (("agent", "Một agent"), ("manager", "Điều phối viên"), ("task", "Task cho team / agent"))
SCHED_STATUS = {"active": ("Đang bật", "ok", "play"), "paused": ("Tạm dừng", "warn", "pause"), "done": ("Đã xong", "muted", "check")}
FIRE_STATUS = {"completed": ("xong", "ok"), "failed": ("lỗi", "danger"), "skipped": ("bỏ lượt", "muted"), "running": ("đang chạy", "info")}
EXPR_EXAMPLES = ("daily 08:00", "weekdays 08:30", "every 2h", "mỗi 15 phút", "weekly mon,thu 09:00", "0 */4 * * *", "at 2026-12-01 09:00")


def sched_target(r: dict[str, Any]) -> str:
    if r.get("kind") == "manager":
        return "Điều phối viên"
    if r.get("kind") == "agent":
        return f"agent {r.get('target') or '?'}"
    o = r.get("options") or {}
    parts = [f"team {o['team']}" if o.get("team") else "", f"agent {o['assignee']}" if o.get("assignee") else ""]
    return "task → " + " / ".join(p for p in parts if p)


def sched_dest(r: dict[str, Any]) -> str:
    dt = r.get("deliver_to") or {}
    return str(dt.get("conversation_id") or dt.get("channel") or "hội thoại gần nhất")


def schedule_params(form: Any) -> dict[str, Any]:
    """Form tạo lịch → tham số `schedule.create`. Kiểm biểu thức/đích để service làm (một nguồn sự thật)."""
    from .app import _num, _req

    kind = str(form.get("kind") or "agent").strip()
    if kind not in {k for k, _ in KINDS}:
        raise ValueError(f"loại lịch '{kind}' không hợp lệ")
    p: dict[str, Any] = {"kind": kind, "expr": _req(form, "expr", "biểu thức lịch"), "prompt": _req(form, "prompt", "việc cần làm mỗi lượt")}
    name = str(form.get("name") or "").strip()
    if name:
        p["name"] = name
    if kind == "agent":
        p["target"] = _req(form, "target", "agent chạy")
    elif kind == "task":
        team, assignee = str(form.get("team") or "").strip(), str(form.get("assignee") or "").strip()
        if not team and not assignee:
            raise ValueError("lịch kiểu task cần team hoặc agent nhận task")
        p.update({k: v for k, v in (("team", team), ("assignee", assignee)) if v})
    budget = _num(form.get("budget_usd"), float, "Ngân sách mỗi lượt")
    if budget is not None:
        p["budget_usd"] = budget
    dest = str(form.get("deliver_to") or "").strip()
    if dest:
        p["deliver_to"] = dest
    return p


async def _opt(request: Request, method: str, params: dict[str, Any] | None = None, default: Any = None) -> Any:
    try:
        return await rpc(request, method, params or {})
    except RpcError:
        return default


async def schedules_page(request: Request, *, values: dict[str, Any] | None = None, error: str | None = None, status: int = 200) -> Response:
    q = request.query_params
    flt = q.get("status") if q.get("status") in SCHED_STATUS else None
    unavailable = None
    try:
        rows = await rpc(request, "schedule.list", {"status": flt, "limit": 200}) or []
    except RpcError as e:
        if e.code != METHOD_NOT_FOUND:
            raise
        rows, unavailable = [], "Daemon đang chạy bản cũ chưa có lịch chạy — khởi động lại daemon (`cfharness daemon restart`)."
    agents = [a["id"] for a in await _opt(request, "agent.list", {}, []) if not a.get("archived_at")]
    teams = [t["id"] for t in await _opt(request, "team.list", {}, []) if not t.get("archived_at")]
    preview, preview_err = None, None
    expr = (q.get("preview") or "").strip()
    if expr:
        try:
            preview = await rpc(request, "schedule.preview", {"expr": expr, "count": 5})
        except RpcError as e:
            preview_err = err_text(e)
    for r in rows:
        r["_target"], r["_dest"] = sched_target(r), sched_dest(r)
    v = values or {"kind": "agent", "expr": expr}
    return await render(request, "schedules.html", {"nav": "schedules", "rows": rows, "agents": agents, "teams": teams, "kinds": KINDS,
                                                    "sched_status": SCHED_STATUS, "fire_status": FIRE_STATUS, "examples": EXPR_EXAMPLES,
                                                    "q": dict(q), "v": v, "error": error, "preview": preview, "preview_err": preview_err,
                                                    "unavailable": unavailable}, status=status)


async def schedules_get(request: Request) -> Response:
    return await schedules_page(request)


async def schedule_create(request: Request) -> Response:
    """Tạo lịch: lần 1 kiểm biểu thức (`schedule.preview`) + hiện trang xác nhận (mốc chạy tới, ước tính lượt/ngày, ngân sách);
    lần 2 (`confirm=yes`) gọi `schedule.create`. Lỗi → vẽ lại trang với giá trị đã nhập (400)."""
    _, form = await post_guard(request)
    values = {k: form.get(k) for k in form.keys() if k not in ("csrf", "k", "confirm")}
    try:
        p = schedule_params(form)
        if form.get("confirm") != "yes":
            pv = await rpc(request, "schedule.preview", {"expr": p["expr"], "count": 3})
            nxt = ", ".join(str(t)[:16].replace("T", " ") for t in pv.get("next") or []) or "không có mốc nào"
            budget = p.get("budget_usd")
            msg = (f"Tạo lịch chạy tự động?\nĐích: {sched_target({**p, 'options': p})}\nLịch: {pv.get('describe') or pv.get('expr')} "
                   f"(~{pv.get('runs_per_day') or 0:g} lượt/ngày, giờ máy {pv.get('tz') or '?'})\nCác lượt tới: {nxt}\n"
                   f"Ngân sách mỗi lượt: {('$' + format(budget, 'g')) if budget else 'mặc định (schedules.default_budget_usd)'}\n"
                   "Mỗi lượt là một run thật (tốn tiền) — tạm dừng hoặc xoá lịch ở trang Lịch chạy.")
            return await render(request, "confirm.html", {"nav": "schedules", "message": msg, "fields": carry(form), "action": "/schedules/create",
                                                          "danger": False, "back": "/schedules", "note": None})
        res = await rpc(request, "schedule.create", p, audit=True)
    except (RpcError, ValueError) as e:
        return await schedules_page(request, values=values, error=err_text(e), status=400)
    return flash_redirect(request, "/schedules", "ok", f"Đã tạo lịch '{res.get('name')}' ({res.get('id')}) — lượt tới {res.get('next_run_local') or '—'}")


# ============================================================================ tìm kiếm hội thoại
SINCE = (("", "Mọi lúc"), ("1d", "24 giờ qua"), ("7d", "7 ngày qua"), ("30d", "30 ngày qua"), ("90d", "90 ngày qua"))
ROLES = (("", "Mọi loại tin"), ("user", "Prompt / tin người"), ("assistant", "Trả lời của agent"), ("steer", "Chỉ đạo (steer)"),
         ("tool_call", "Gọi tool"), ("tool_result", "Kết quả tool"), ("manager", "Điều phối viên"))
ROLE_LABEL = {"user": "Người", "assistant": "Agent", "steer": "Chỉ đạo", "tool_call": "Gọi tool", "tool_result": "Kết quả tool",
              "manager": "Điều phối viên", "notify": "Thông báo", "tick": "Nhịp", "schedule": "Lịch"}
PAGE = 30


def snippet_parts(s: str) -> list[tuple[bool, str]]:
    """`«khớp»` (session_search._snippet) → [(là_đoạn_khớp, chữ)] — template tự escape từng đoạn, không bao giờ `|safe`."""
    out: list[tuple[bool, str]] = []
    for i, part in enumerate(re.split(r"«|»", s or "")):
        if part:
            out.append((i % 2 == 1, part))
    return out


def hit_link(h: dict[str, Any]) -> str | None:
    if h.get("run_id"):
        return f"/runs/{h['run_id']}"
    if h.get("conversation_id"):
        return f"/manager?c={h['conversation_id']}"
    return None


async def search_page(request: Request) -> Response:
    q = request.query_params
    query = (q.get("q") or "").strip()
    try:
        offset = max(0, int(q.get("offset") or 0))
    except ValueError:
        offset = 0
    agents = [a["id"] for a in await _opt(request, "agent.list", {}, [])]
    res: dict[str, Any] | None = None
    error = None
    if query:
        params: dict[str, Any] = {"mode": "search", "query": query, "limit": PAGE, "offset": offset}
        if q.get("agent"):
            params["agent_id"] = q["agent"]
        if q.get("since") in {k for k, _ in SINCE if k}:
            params["since"] = q["since"]
        if q.get("role") in {k for k, _ in ROLES if k}:
            params["role"] = q["role"]
        if q.get("technical") == "1":
            params["include_technical"] = True
        try:
            res = await rpc(request, "session.search", params)
        except RpcError as e:
            error = err_text(e) if e.code != METHOD_NOT_FOUND else "Daemon đang chạy bản cũ chưa có tìm kiếm hội thoại — khởi động lại daemon."
        for h in (res or {}).get("hits") or []:
            h["_parts"], h["_link"], h["_role"] = snippet_parts(h.get("snippet") or ""), hit_link(h), ROLE_LABEL.get(h.get("role") or "", h.get("role") or "")
    base = {k: v for k, v in q.items() if k != "offset" and v}
    return await render(request, "search.html", {"nav": "search", "query": query, "res": res, "error": error, "agents": agents, "q": dict(q),
                                                 "since_opts": SINCE, "role_opts": ROLES, "base": base, "offset": offset})


# ============================================================================ thay đổi file của run (diff / revert)
PATCH_MAX_CHARS = 600_000  # phần patch được phân tích; phần sau bị bỏ (báo "đã cắt")
FILE_MAX_LINES = 400  # dòng hiển thị tối đa mỗi file
MAX_FILES = 150
OPEN_IF_FEWER = 4  # ≤ N file → mở sẵn
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_BIN = re.compile(r"^Binary file (.+) differs$")


def split_patch(text: str) -> list[dict[str, Any]]:
    """Patch unified của `workspace._unified` → [{path, binary, lines: [(cls, text)], adds, dels}].

    Đếm dòng theo header `@@` để một dòng nội dung bắt đầu bằng `--- ` (vd xoá dòng `-- comment`) không bị hiểu nhầm là đầu file mới."""
    files: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    old = new = 0
    lines = (text or "").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if cur is not None and (old > 0 or new > 0):
            if ln.startswith("\\"):
                cur["lines"].append(("meta", ln))
            elif ln.startswith("+"):
                new -= 1
                cur["adds"] += 1
                cur["lines"].append(("add", ln))
            elif ln.startswith("-"):
                old -= 1
                cur["dels"] += 1
                cur["lines"].append(("del", ln))
            else:
                old, new = old - 1, new - 1
                cur["lines"].append(("ctx", ln))
            i += 1
            continue
        m = _BIN.match(ln)
        if m:
            files.append({"path": m.group(1), "binary": True, "lines": [], "adds": 0, "dels": 0})
            cur = None
            i += 1
            continue
        if ln.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            a, b = ln[4:], lines[i + 1][4:]
            path = b[2:] if b.startswith("b/") else (a[2:] if a.startswith("a/") else b)
            cur = {"path": path, "binary": False, "lines": [], "adds": 0, "dels": 0}
            files.append(cur)
            i += 2
            continue
        h = _HUNK.match(ln)
        if h and cur is not None:
            old = int(h.group(1)) if h.group(1) is not None else 1
            new = int(h.group(2)) if h.group(2) is not None else 1
            cur["lines"].append(("hunk", ln))
        elif cur is not None and ln.startswith("\\"):
            cur["lines"].append(("meta", ln))
        i += 1
    return files


FILE_STATUS = {"added": ("thêm", "ok"), "deleted": ("xoá", "danger"), "modified": ("sửa", "info")}


def diff_view(res: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """`run.diff` + trạng thái run → dữ liệu vẽ (thuần, có trần kích thước)."""
    d = res.get("diff") or {}
    patch = res.get("patch") or ""
    cut_patch = len(patch) > PATCH_MAX_CHARS
    parsed = {f["path"]: f for f in split_patch(patch[:PATCH_MAX_CHARS])}
    # sau khi hoàn tác, file đương nhiên khác bản "lúc run xong" — không phải xung đột
    modified = [] if res.get("reverted_at") else list(res.get("modified_since") or [])
    files = []
    for f in (d.get("files") or [])[:MAX_FILES]:
        p = parsed.get(f.get("path") or "") or {}
        lines = p.get("lines") or []
        files.append({"path": f.get("path"), "status": f.get("status"), "status_view": FILE_STATUS.get(f.get("status") or "", (f.get("status") or "?", "muted")),
                      "binary": bool(p.get("binary")), "lines": lines[:FILE_MAX_LINES], "more": max(0, len(lines) - FILE_MAX_LINES),
                      "adds": p.get("adds", 0), "dels": p.get("dels", 0), "has_patch": bool(p), "modified": f.get("path") in modified,
                      "revertible": f.get("revertible", d.get("kind") == "git")})
    kind = d.get("kind")
    why_not = None
    live = run.get("status") in ("queued", "running", "waiting_human")
    if not d:
        why_not = "none"
    elif res.get("reverted_at"):
        why_not = "Đã hoàn tác lúc " + str(res["reverted_at"])[:19].replace("T", " ") + "."
    elif kind != "git":
        why_not = "Workspace không phải repo git — hub chỉ ghi lại danh sách file bị đổi, không có bản trước để hoàn tác."
    elif d.get("checkpoint_purged_at"):
        why_not = "Bản lưu trước run (checkpoint) đã bị xoá theo retention (`retention.checkpoints_days`) — không hoàn tác được nữa."
    elif not d.get("revertible", True):
        why_not = "Có file quá lớn lúc checkpoint nên không đủ dữ liệu để hoàn tác."
    elif live:
        why_not = "Run (hoặc session của nó) còn đang chạy — dừng hoặc chờ xong rồi mới hoàn tác."
    elif not d.get("files"):
        why_not = "Run không sửa file nào."
    stat = d.get("stat") or {}
    return {"has": bool(d), "kind": kind, "root": d.get("root"), "files": files, "total_files": len(d.get("files") or []),
            "hidden_files": max(0, len(d.get("files") or []) - MAX_FILES), "cut_patch": cut_patch, "stat": stat, "modified": modified,
            "commits_made": bool(d.get("commits_made")), "head_before": (d.get("head_before") or "")[:10], "head_after": (d.get("head_after") or "")[:10],
            "truncated": bool(d.get("truncated")), "can_revert": why_not is None and not modified, "why_not": why_not, "reverted_at": res.get("reverted_at"),
            "open_all": len(files) <= OPEN_IF_FEWER}


async def _diff_ctx(request: Request) -> dict[str, Any]:
    rid = request.path_params["id"]
    run = await rpc(request, "run.get", {"id": rid})
    try:
        res = await rpc(request, "run.diff", {"id": run["id"], "patch": True}) or {}
    except RpcError as e:
        if e.code != METHOD_NOT_FOUND:
            raise
        res = {}
    return {"run": run, "dv": diff_view(res, run)}


async def partial_run_diff(request: Request) -> Response:
    """Khối "Thay đổi file" tải lười cho trang run (htmx `hx-trigger=load`, swap outerHTML). Run không ghi file → rỗng (khối biến mất)."""
    ctx = await _diff_ctx(request)
    if not ctx["dv"]["has"]:
        return HTMLResponse("")
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_run_diff.html", {**ctx, "csrf": sess.csrf if sess else "", "path": f"/runs/{ctx['run']['id']}"})


async def run_diff_page(request: Request) -> Response:
    ctx = await _diff_ctx(request)
    return await render(request, "run_diff.html", {**ctx, "nav": "runs"})


def _revert_ok(r: Any, f: Any) -> str:
    r = r or {}
    files = r.get("reverted") or []
    head = ("Kiểm tra: hoàn tác được " if r.get("dry_run") else "Đã hoàn tác ") + f"{len(files)} file"
    out = head + (": " + ", ".join(files[:8]) + (" …" if len(files) > 8 else "") if files else "")
    for w in r.get("warnings") or []:
        out += f" — lưu ý: {w}"
    return out


def revert_confirm(f: Any) -> str:
    return (f"Hoàn tác thay đổi file của run {f.get('id')}?\n"
            f"Ghi lại nội dung {f.get('files') or 'các'} file như TRƯỚC khi run bắt đầu (file run tạo mới sẽ bị xoá).\n"
            "Chỉ làm được khi các file này CHƯA bị sửa tiếp sau run: nếu bạn hoặc run khác đã sửa, hub từ chối và không ghi gì.\n"
            "Không đổi HEAD/nhánh git — commit của run (nếu có) vẫn còn.")


# ============================================================================ sức khoẻ (thẻ ở Cài đặt)
async def partial_health(request: Request) -> Response:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, level: str = "error") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "tone": "ok" if ok else ("warn" if level == "warning" else "danger")})

    try:
        st = await rpc(request, "hub.status") or {}
        add("Daemon", True, f"đang chạy · phiên bản {st.get('version')} · {st.get('running', 0)} run chạy, {st.get('queued', 0)} chờ")
        add("Đăng nhập claude", not st.get("auth_block"), "bình thường" if not st.get("auth_block") else
            "claude chưa đăng nhập — run bị chặn (chạy `claude auth login` rồi `cfharness doctor`)")
        add("Sandbox OS cho Bash", bool(st.get("sandbox_enabled")), "bật" if st.get("sandbox_enabled") else "đang TẮT (guard.sandbox.enabled)", "warning")
        add("Dừng khẩn cấp", not st.get("estop"), "tắt" if not st.get("estop") else "ĐANG BẬT — không run mới nào chạy", "warning")
        rl = st.get("rate_limit")
        if rl:
            add("Giới hạn tốc độ", False, "đang bị giới hạn" + (f" tới {rl.get('until')}" if isinstance(rl, dict) and rl.get("until") else ""), "warning")
        compat = await _opt(request, "hub.compat", {}, {}) or {}
        cli = compat.get("cli") or {}
        if cli:
            st_ = cli.get("status")
            add("Phiên bản claude CLI", st_ == "ok", f"{cli.get('version') or 'không rõ'}" + {"ok": " (đã kiểm)", "untested": " — chưa kiểm với hub",
                                                                                              "missing": " — không tìm thấy claude"}.get(st_ or "", ""),
                "error" if st_ == "missing" else "warning")
        ur = compat.get("unknown_rate") or {}
        if ur:
            add("Tỉ lệ event lạ (1 giờ)", not ur.get("alert"), f"{ur.get('unknown', 0)}/{ur.get('total', 0)} ({(ur.get('rate') or 0) * 100:.1f}%)")
        integ = await _opt(request, "guard.integrity.status", {}, {}) or {}
        n = len(integ.get("recent_events") or [])
        add("Toàn vẹn chính sách", n == 0, "bình thường" if n == 0 else f"{n} cảnh báo gần đây — xem `cfharness guard integrity`")
    except (RpcError, DaemonUnavailable, OSError) as e:
        add("Daemon", False, err_text(e))
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_health.html", {"checks": checks, "bad": sum(1 for c in checks if c["tone"] == "danger"),
                                                                           "csrf": sess.csrf if sess else ""})


# ============================================================================ đăng ký
def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    """Thêm thao tác ghi vào bảng `ACTIONS` của app (form POST `/do/<tên>`, CSRF + khoá phiên, `_audit.channel=web`)."""
    actions["schedule_pause"] = Action("schedule.pause", lambda f: {"id": _req(f, "id"), "reason": "người tạm dừng từ web"},
                                       lambda r, f: f"Đã tạm dừng lịch {f.get('name') or f.get('id')}",
                                       confirm=lambda f: f"Tạm dừng lịch '{f.get('name') or f.get('id')}'? Lịch không chạy cho tới khi bật lại.")
    actions["schedule_resume"] = Action("schedule.resume", lambda f: {"id": _req(f, "id")},
                                        lambda r, f: f"Đã bật lại lịch {f.get('name') or f.get('id')} — lượt tới {(r or {}).get('next_run_local') or '—'}",
                                        confirm=lambda f: f"Bật lại lịch '{f.get('name') or f.get('id')}'? Mỗi lượt là một run thật (tốn tiền).")
    actions["schedule_run_now"] = Action("schedule.run_now", lambda f: {"id": _req(f, "id")},
                                         lambda r, f: f"Đã chạy ngay lịch {f.get('name') or f.get('id')}"
                                                      + (f" — run {r.get('run_id')}" if (r or {}).get("run_id") else "")
                                                      + (f" — task {r.get('task_id')}" if (r or {}).get("task_id") else ""),
                                         confirm=lambda f: f"Chạy ngay MỘT lượt của lịch '{f.get('name') or f.get('id')}'? (tốn tiền như một lượt thường; "
                                                           "không đổi mốc chạy kế tiếp)",
                                         goto=lambda r, f: f"/runs/{r['run_id']}" if (r or {}).get("run_id") else
                                         (f"/tasks/{r['task_id']}" if (r or {}).get("task_id") else None))
    actions["schedule_rm"] = Action("schedule.rm", lambda f: {"id": _req(f, "id")}, lambda r, f: f"Đã xoá lịch {f.get('name') or f.get('id')}",
                                    confirm=lambda f: f"Xoá lịch '{f.get('name') or f.get('id')}'?\nXoá mềm: lịch ngừng chạy và biến khỏi danh sách; "
                                                      "lịch sử các lượt đã chạy vẫn được giữ. Không khôi phục được từ web/CLI — muốn chạy lại thì tạo lịch mới.",
                                    danger=True, goto=lambda r, f: "/schedules")
    actions["run_revert"] = Action("run.revert", lambda f: {"id": _req(f, "id")}, _revert_ok, confirm=revert_confirm, danger=True,
                                   goto=lambda r, f: f"/runs/{f.get('id')}#diff")
    # thử trước (dry_run): báo xung đột nếu có, không ghi file (daemon vẫn ghi audit `run.revert` — kèm `_audit.channel=web`)
    actions["run_revert_check"] = Action("run.revert", lambda f: {"id": _req(f, "id"), "dry_run": True}, _revert_ok,
                                         goto=lambda r, f: f"/runs/{f.get('id')}#diff")


def routes() -> list[Any]:
    from starlette.routing import Route

    return [
        Route("/schedules", schedules_get),
        Route("/schedules/create", schedule_create, methods=["POST"]),
        Route("/search", search_page),
        Route("/runs/{id}/diff", run_diff_page),
        Route("/partials/run/{id}/diff", partial_run_diff),
        Route("/partials/health", partial_health),
    ]


__all__ = ["register_actions", "routes", "split_patch", "diff_view", "snippet_parts", "schedule_params", "sched_target"]
