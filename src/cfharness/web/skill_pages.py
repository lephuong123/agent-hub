"""Trang quản lý skill cho NGƯỜI (`/skills`) — client mỏng của RPC `knowledge.skill_*` (docs/dev-notes/skill-manager.md) + `knowledge.pin`.

- `GET /skills?agent=&scope=&state=&archived=1&q=`: bảng skill. Không chọn agent → `knowledge.skill_list` mode `all` (mọi agent + team +
  global); chọn agent → mode `effective` (tập skill agent thật sự thấy: agent che team che global). Lọc tầng/trạng thái/chữ ở server.
- `GET /skills/{scope}/{owner}/{name}[?file=references/x.md]`: chi tiết (`knowledge.skill_get`) — owner tầng global là `-`.
  SKILL.md và file hỗ trợ vẽ trong `<pre>` (autoescape, không bao giờ `|safe`). Skill đã archive (get → NotFound) → trang rút gọn từ
  `skill_list include_archived` với nút Khôi phục.
- `GET /skills/new`, `GET /skills/{scope}/{owner}/{name}/edit`: form tạo/sửa (mô tả + thân, hoặc cả SKILL.md, hoặc một file hỗ trợ),
  form nhập từ đường dẫn TRÊN MÁY DAEMON, form promote.
- `POST /skills/act/{op}` (op: save|archive|restore|import|promote|pin): `post_guard` (CSRF + Origin + khoá phiên WT-1), thao tác nhạy
  cảm qua trang xác nhận phía server (`confirm=yes`; JS `data-confirm` làm sẵn), RPC với `_audit.channel=web` (daemon audit: các method
  nằm trong `MUTATING`, và HUMAN_ONLY). Kết quả: `status: "blocked"` của guard là kết quả BÌNH THƯỜNG của RPC nhưng web hiện như LỖI
  (luật + lý do). Kết quả lần ghi gần nhất (proposal, ledger entry + link + cách rollback, canary) giữ trong `Session.extra` và hiện
  thành khối trên trang đích một lần.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlencode

from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from ..rpc import RpcError
from .app import carry, err_text, flash_redirect, post_guard, render, rpc, safe_next, session_of

SCOPES = (("agent", "Agent"), ("team", "Team"), ("global", "Toàn hub"))
STATES = (("active", "Đang dùng"), ("stale", "Lâu không dùng"), ("archived", "Đã archive"), ("deleted", "Đã xoá"))
STATE_TONE = {"active": "ok", "stale": "warn", "archived": "muted", "deleted": "muted"}
GLOBAL_OWNER = "-"  # chỗ của owner trong URL cho tầng global (owner = null)
OK_STATUS = ("applied", "canary", "promoted", "restored", "unchanged")
OPS = ("save", "archive", "restore", "import", "promote", "pin")
RESULT_KEY = "skill_result"


# ============================================================================ hàm thuần
def skill_url(scope: str, owner: str | None, name: str, *, suffix: str = "") -> str:
    o = owner if scope != "global" and owner else GLOBAL_OWNER
    return f"/skills/{quote(scope, safe='')}/{quote(str(o), safe='')}/{quote(name, safe='')}{suffix}"


def tier_params(scope: str, owner: str | None) -> dict[str, Any]:
    """(scope, owner) → params RPC chọn ĐÚNG một tầng (luôn kèm `scope` để `skill_get` không lấy bản hiệu lực)."""
    if scope == "agent":
        if not owner:
            raise ValueError("thiếu agent")
        return {"scope": "agent", "agent": owner}
    if scope == "team":
        if not owner:
            raise ValueError("thiếu team")
        return {"scope": "team", "team": owner}
    if scope == "global":
        return {"scope": "global"}
    raise ValueError(f"tầng '{scope}' không hợp lệ (agent | team | global)")


def path_tier(request: Request) -> tuple[str, str | None, str]:
    pp = request.path_params
    scope, owner, name = pp["scope"], pp["owner"], pp["name"]
    if scope not in {s for s, _ in SCOPES}:
        raise HTTPException(404, "Không có tầng skill này")
    return scope, (None if scope == "global" else owner), name


def tier_label(scope: str | None, owner: str | None) -> str:
    if scope == "global":
        return "toàn hub"
    return f"{scope or 'agent'} {owner or '?'}"


def filter_rows(rows: list[dict[str, Any]], *, scope: str = "", state: str = "", q: str = "", archived: bool = False) -> list[dict[str, Any]]:
    """Lọc phía server: tầng, trạng thái, chữ trong tên/mô tả (không phân biệt hoa thường). Mặc định ẩn archived/deleted."""
    q = (q or "").strip().lower()
    out = []
    for r in rows:
        st = r.get("state") or "active"
        if scope and r.get("scope") != scope:
            continue
        if state and st != state:
            continue
        if not state and not archived and st in ("archived", "deleted"):
            continue
        if q and q not in (r.get("name") or "").lower() and q not in (r.get("description") or "").lower():
            continue
        out.append(r)
    return sorted(out, key=lambda r: ({"agent": 0, "team": 1, "global": 2}.get(r.get("scope") or "", 3), r.get("owner") or "", r.get("name") or ""))


def result_view(res: Any, what: str) -> dict[str, Any]:
    """Kết quả RPC ghi → {ok, text, …} cho flash + khối kết quả. `blocked` (guard) / `deferred_*` / `eval_failed` = KHÔNG thành công."""
    r = res if isinstance(res, dict) else {}
    st = str(r.get("status") or "?")
    ok = st in OK_STATUS
    v: dict[str, Any] = {"ok": ok, "status": st, "what": what, "proposal_id": r.get("proposal_id"), "ledger_entry_id": r.get("ledger_entry_id"),
                         "canary_id": r.get("canary_id"), "mode": r.get("mode"), "rule": r.get("rule"), "reason": r.get("reason"),
                         "scope": r.get("scope"), "owner": r.get("owner"), "name": r.get("name"),
                         "restored_entry_id": r.get("restored_entry_id"), "imported_files": r.get("imported_files") or [],
                         "skipped": r.get("skipped") or [], "shadowed": bool(r.get("shadowed_by_agent_copy"))}
    src = r.get("source_archive")
    if isinstance(src, dict):
        v["source_archive"] = {"status": src.get("status"), "ledger_entry_id": src.get("ledger_entry_id"), "reason": src.get("reason")}
    if st == "blocked":
        text = f"Bị guard chặn — {what} KHÔNG được ghi. Luật: {r.get('rule') or '?'}" + (f" — {r['reason']}" if r.get("reason") else "")
    elif st == "unchanged":
        text = f"{what}: không có gì thay đổi"
    elif ok:
        text = f"Đã {what} ({st})"
        if r.get("ledger_entry_id"):
            text += f" — ledger {r['ledger_entry_id']}"
        if r.get("canary_id"):
            text += f" — canary {r['canary_id']} (mode {r.get('mode') or '?'})"
    else:
        text = f"{what} chưa được áp dụng: {st}" + (f" — {r['reason']}" if r.get("reason") else "")
    if r.get("proposal_id"):
        text += f" · proposal {r['proposal_id']}"
    v["text"] = text
    return v


def save_params(f: FormData) -> dict[str, Any]:
    """Form tạo/sửa → params `knowledge.skill_save`. Chế độ: `fields` (mô tả + thân) | `full` (cả SKILL.md) | `file` (một file hỗ trợ)."""
    from .app import _req

    name = _req(f, "name", "tên skill")
    scope = str(f.get("scope") or "agent")
    owner = str(f.get("agent") or "").strip() if scope == "agent" else str(f.get("team") or "").strip() if scope == "team" else None
    if scope == "agent" and not owner:
        raise ValueError("chọn agent sở hữu skill")
    if scope == "team" and not owner:
        raise ValueError("chọn team")
    p: dict[str, Any] = {**tier_params(scope, owner), "name": name}
    mode = str(f.get("mode") or "fields")
    if mode == "full":
        p["content"] = _nl(_req(f, "content", "nội dung SKILL.md"))
    elif mode == "file":
        p["file"] = _req(f, "file", "đường dẫn file hỗ trợ")
        p["content"] = _nl(str(f.get("file_content") or ""))
    elif mode == "fields":
        desc = " ".join(str(f.get("description") or "").split())
        body = _nl(str(f.get("body") or ""))
        if desc:
            p["description"] = desc
        if body.strip():
            p["body"] = body
        if not desc and not body.strip():
            raise ValueError("nhập mô tả và/hoặc nội dung")
    else:
        raise ValueError("chế độ lưu không hợp lệ")
    why = str(f.get("rationale") or "").strip()
    if why:
        p["rationale"] = why
    return p


def import_params(f: FormData) -> dict[str, Any]:
    from .app import _req

    scope = str(f.get("scope") or "agent")
    owner = str(f.get("agent") or "").strip() if scope == "agent" else str(f.get("team") or "").strip() if scope == "team" else None
    if scope in ("agent", "team") and not owner:
        raise ValueError("chọn agent" if scope == "agent" else "chọn team")
    p: dict[str, Any] = {**tier_params(scope, owner), "path": _req(f, "path", "đường dẫn trên máy daemon"), "overwrite": f.get("overwrite") == "1"}
    for k in ("name", "description"):
        v = " ".join(str(f.get(k) or "").split())
        if v:
            p[k] = v
    return p


def promote_params(f: FormData) -> dict[str, Any]:
    from .app import _req

    to = str(f.get("to") or "")
    if to not in ("team", "global"):
        raise ValueError("chọn đích: team hoặc toàn hub")
    p: dict[str, Any] = {"agent": _req(f, "agent"), "name": _req(f, "name"), "to": to, "archive_source": f.get("archive_source") == "1"}
    team = str(f.get("team") or "").strip()
    if to == "team" and team:
        p["team"] = team
    return p


def _nl(s: str) -> str:
    return s.replace("\r\n", "\n")


def _form_tier(f: FormData) -> tuple[str, str | None]:
    scope = str(f.get("scope") or "agent")
    owner = str(f.get("agent") or "").strip() if scope == "agent" else str(f.get("team") or "").strip() if scope == "team" else None
    return scope, owner or None


# ============================================================================ đọc
async def _ids(request: Request) -> tuple[list[str], list[str]]:
    agents = [a["id"] for a in await rpc(request, "agent.list", {}) or []]
    try:
        teams = [t["id"] for t in await rpc(request, "team.list", {}) or []]
    except RpcError:
        teams = []
    return agents, teams


def _pop_result(request: Request) -> dict[str, Any] | None:
    sess = session_of(request)
    return sess.extra.pop(RESULT_KEY, None) if sess is not None else None


async def _with_canary(request: Request, res: dict[str, Any] | None) -> dict[str, Any] | None:
    if res and res.get("canary_id"):
        try:
            c = await rpc(request, "learn.canary_get", {"id": res["canary_id"]}) or {}
            res["canary_status"] = c.get("status")
        except RpcError:
            pass
    return res


async def skills_page(request: Request) -> Response:
    q = request.query_params
    agent = (q.get("agent") or "").strip()
    scope = q.get("scope") or ""
    scope = scope if scope in {s for s, _ in SCOPES} else ""
    state = q.get("state") or ""
    state = state if state in {s for s, _ in STATES} else ""
    archived = q.get("archived") == "1" or state in ("archived", "deleted")
    params: dict[str, Any] = {"include_archived": archived}
    if agent:
        params["agent"] = agent
    data = await rpc(request, "knowledge.skill_list", params) or {}
    rows = filter_rows(data.get("skills") or [], scope=scope, state=state, q=q.get("q") or "", archived=archived)
    for r in rows:
        r["url"] = skill_url(r.get("scope") or "agent", r.get("owner"), r["name"])
    agents, teams = await _ids(request)
    return await render(request, "skills.html", {"nav": "skills", "rows": rows, "mode": data.get("mode"), "q": dict(q), "agents": agents, "teams": teams,
                                                 "scopes": SCOPES, "states": STATES, "tones": STATE_TONE, "archived": archived,
                                                 "result": await _with_canary(request, _pop_result(request)), "tier_label": tier_label})


async def _archived_row(request: Request, scope: str, owner: str | None, name: str) -> dict[str, Any] | None:
    try:
        data = await rpc(request, "knowledge.skill_list", {**tier_params(scope, owner), "include_archived": True}) or {}
    except (RpcError, ValueError):
        return None
    return next((r for r in data.get("skills") or [] if r.get("name") == name), None)


async def skill_page(request: Request) -> Response:
    scope, owner, name = path_tier(request)
    fparam = request.query_params.get("file") or ""
    params: dict[str, Any] = {**tier_params(scope, owner), "name": name, "ledger_limit": 20}
    if fparam:
        params["file"] = fparam
    result = await _with_canary(request, _pop_result(request))
    try:
        s = await rpc(request, "knowledge.skill_get", params)
    except RpcError as e:
        if e.code != -32004:
            raise
        row = await _archived_row(request, scope, owner, name)
        if row is None or (fparam and row.get("state") not in ("archived", "deleted")):
            raise
        return await render(request, "skill.html", {"nav": "skills", "s": None, "row": row, "scope": scope, "owner": owner, "name": name,
                                                    "url": skill_url(scope, owner, name), "result": result, "tier_label": tier_label, "tones": STATE_TONE})
    teams: list[str] = []
    if scope == "agent":
        _, teams = await _ids(request)
    prov = s.get("provenance") or {}
    return await render(request, "skill.html", {"nav": "skills", "s": s, "row": prov, "scope": scope, "owner": owner, "name": name,
                                                "url": skill_url(scope, owner, name), "file": s.get("file"), "teams": teams, "result": result,
                                                "tier_label": tier_label, "tones": STATE_TONE})


async def _form_ctx(request: Request, *, edit: dict[str, Any] | None, values: dict[str, Any] | None = None, error: str | None = None,
                    import_values: dict[str, Any] | None = None, import_error: str | None = None) -> dict[str, Any]:
    agents, teams = await _ids(request)
    try:
        limit = int(await rpc(request, "config.get", {"key": "learning.limits.skill_desc_chars"}) or 60)
    except (RpcError, TypeError, ValueError):
        limit = 60
    return {"nav": "skills", "edit": edit, "v": values or {}, "error": error, "iv": import_values or {}, "import_error": import_error,
            "agents": agents, "teams": teams, "desc_limit": limit, "tier_label": tier_label}


async def skill_new(request: Request) -> Response:
    q = request.query_params
    v = {"scope": q.get("scope") or ("agent" if q.get("agent") else ""), "agent": q.get("agent") or "", "team": q.get("team") or "", "mode": "fields"}
    v["scope"] = v["scope"] if v["scope"] in {s for s, _ in SCOPES} else "agent"
    return await render(request, "skill_form.html", await _form_ctx(request, edit=None, values=v, import_values=dict(v)))


async def skill_edit(request: Request) -> Response:
    scope, owner, name = path_tier(request)
    s = await rpc(request, "knowledge.skill_get", {**tier_params(scope, owner), "name": name, "ledger_limit": 1})
    fm = s.get("frontmatter") or {}
    v = {"scope": scope, "agent": owner if scope == "agent" else "", "team": owner if scope == "team" else "", "name": name,
         "description": fm.get("description") or "", "body": s.get("body") or "", "content": s.get("content") or "",
         "mode": request.query_params.get("mode") or "fields", "file": request.query_params.get("file") or ""}
    if v["file"]:
        try:
            got = await rpc(request, "knowledge.skill_get", {**tier_params(scope, owner), "name": name, "file": v["file"], "ledger_limit": 1})
            v["file_content"] = (got.get("file") or {}).get("content") or ""
        except RpcError:
            v["file_content"] = ""
        v["mode"] = "file"
    edit = {"scope": scope, "owner": owner, "name": name, "url": skill_url(scope, owner, name), "support_files": s.get("support_files") or []}
    return await render(request, "skill_form.html", await _form_ctx(request, edit=edit, values=v))


# ============================================================================ ghi
def _confirm(op: str, f: FormData) -> tuple[str, bool] | None:
    """(thông điệp, nguy hiểm) cho trang xác nhận phía server; None = không cần xác nhận."""
    scope, owner = _form_tier(f)
    name = str(f.get("name") or "?")
    where = tier_label(scope, owner)
    if op == "archive":
        return (f"Archive skill '{name}' ở tầng {where}?\nXoá mềm vào .archive (khôi phục được bằng nút Khôi phục hoặc rollback ledger).", True)
    if op == "restore":
        return (f"Khôi phục skill '{name}' ở tầng {where} (rollback lần archive gần nhất)?", False)
    if op == "promote":
        to = str(f.get("to") or "?")
        tgt = "toàn hub (mọi agent)" if to == "global" else f"team {str(f.get('team') or '').strip() or '(team duy nhất của agent)'}"
        return (f"Đưa skill '{name}' của agent {f.get('agent')} lên {tgt}?\nThay đổi tầng của NGƯỜI áp dụng NGAY cho mọi agent của tầng đó "
                "(không qua eval trước; canary chỉ theo dõi)."
                + ("\nBản ở tầng agent sẽ được archive sau khi áp dụng." if f.get("archive_source") == "1" else
                   "\nBản ở tầng agent vẫn giữ và tiếp tục che bản mới với chính agent này."), True)
    if op == "import":
        return (f"Nhập skill từ đường dẫn TRÊN MÁY DAEMON:\n{f.get('path') or '?'}\nvào tầng {where}?"
                + ("\nĐÃ CÓ skill cùng tên sẽ bị THAY CẢ CÂY (rollback được)." if f.get("overwrite") == "1" else ""), f.get("overwrite") == "1")
    if op == "save" and scope in ("team", "global"):
        return (f"Lưu skill '{name}' ở tầng {where}?\nÁp dụng NGAY cho mọi agent của tầng này (canary chỉ theo dõi).", False)
    return None


async def skill_act(request: Request) -> Response:
    op = request.path_params["op"]
    if op not in OPS:
        raise HTTPException(404, "Không có thao tác này")
    sess, form = await post_guard(request)
    back = safe_next(form.get("next"), "/skills")
    values = {k: form.get(k) for k in form.keys() if k not in ("csrf", "k", "confirm")}
    try:
        if op == "save":
            params, what = save_params(form), f"lưu skill {form.get('name')}"
        elif op == "import":
            params, what = import_params(form), "nhập skill"
        elif op == "promote":
            params, what = promote_params(form), f"promote skill {form.get('name')}"
        elif op == "pin":
            scope, owner = _form_tier(form)
            from .app import _req

            on = form.get("on") == "1"
            params = {**tier_params(scope, owner), "target": "skill", "ref": _req(form, "name"), "on": on}
            what = f"{'ghim' if on else 'bỏ ghim'} skill {form.get('name')}"
        else:
            from .app import _req

            scope, owner = _form_tier(form)
            params = {**tier_params(scope, owner), "name": _req(form, "name", "tên skill")}
            what = f"{'archive' if op == 'archive' else 'khôi phục'} skill {form.get('name')}"
    except ValueError as e:
        return await _fail(request, op, values, f"Chưa đủ thông tin: {e}", back)
    c = _confirm(op, form)
    if c and form.get("confirm") != "yes":
        return await render(request, "confirm.html", {"nav": "skills", "message": c[0], "fields": carry(form), "action": f"/skills/act/{op}",
                                                      "danger": c[1], "back": back, "note": None})
    method = {"save": "knowledge.skill_save", "archive": "knowledge.skill_archive", "restore": "knowledge.skill_restore", "import": "knowledge.skill_import",
              "promote": "knowledge.skill_promote", "pin": "knowledge.pin"}[op]
    try:
        res = await rpc(request, method, params, audit=True)
    except RpcError as e:
        return await _fail(request, op, values, err_text(e), back)
    if op == "pin":
        return flash_redirect(request, back, "ok", f"Đã {what}")
    v = result_view(res, what)
    sess.flash("ok" if v["ok"] else "error", v["text"])
    sess.extra[RESULT_KEY] = v
    if not v["ok"] and op in ("save", "import"):  # guard chặn: giữ form đã nhập để sửa
        return await _rerender(request, op, values, None, status=200)
    return flash_redirect(request, _dest(op, v, form, back))


def _dest(op: str, v: dict[str, Any], f: FormData, back: str) -> str:
    if not v["ok"]:
        return back
    if op in ("save", "import", "promote") and v.get("name") and v.get("scope"):
        return skill_url(v["scope"], v.get("owner"), v["name"])
    if op == "archive":
        scope, owner = _form_tier(f)
        return "/skills?" + urlencode({"archived": "1", **({"agent": owner} if scope == "agent" and owner else {})})
    if op == "restore" and v.get("name"):
        return skill_url(v.get("scope") or "agent", v.get("owner"), v["name"])
    return back


async def _rerender(request: Request, op: str, values: dict[str, Any], error: str | None, status: int) -> Response:
    edit = None
    if values.get("editing") == "1":
        scope, owner = str(values.get("scope") or "agent"), (str(values.get("agent") or values.get("team") or "") or None)
        edit = {"scope": scope, "owner": owner, "name": values.get("name"), "url": skill_url(scope, owner if scope != "global" else None, str(values.get("name") or "")),
                "support_files": []}
    if op == "import":
        ctx = await _form_ctx(request, edit=None, values={"scope": values.get("scope"), "agent": values.get("agent"), "team": values.get("team"), "mode": "fields"},
                              import_values=values, import_error=error)
    else:
        ctx = await _form_ctx(request, edit=edit, values=values, error=error, import_values={"scope": values.get("scope"), "agent": values.get("agent"),
                                                                                             "team": values.get("team")})
    sess = session_of(request)
    ctx["result"] = sess.extra.pop(RESULT_KEY, None) if sess is not None else None
    return await render(request, "skill_form.html", ctx, status=status)


async def _fail(request: Request, op: str, values: dict[str, Any], msg: str, back: str) -> Response:
    if op in ("save", "import"):  # form dài: vẽ lại, giữ chữ đã nhập
        return await _rerender(request, op, values, msg, status=400)
    return flash_redirect(request, back, "error", msg)


def routes() -> list[Any]:
    from starlette.routing import Route

    return [
        Route("/skills", skills_page),
        Route("/skills/new", skill_new),
        Route("/skills/act/{op}", skill_act, methods=["POST"]),
        Route("/skills/{scope}/{owner}/{name}", skill_page),
        Route("/skills/{scope}/{owner}/{name}/edit", skill_edit),
    ]


__all__ = ["routes", "skill_url", "tier_params", "filter_rows", "result_view", "save_params", "import_params", "promote_params"]
