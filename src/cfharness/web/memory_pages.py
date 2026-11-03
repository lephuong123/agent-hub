"""Trang quản lý + xem memory của hệ thống và graph memory (`/memory…`) — client mỏng của daemon (docs/dev-notes/web-memory.md).

- `GET /memory`: mọi tầng — toàn hub (USER.md + MEMORY.md), từng team (memory.md), từng agent (MEMORY.md, USER.md): thanh ký tự/hạn mức,
  số mục, số mục ghim, canary đang chạy, lần đổi gần nhất (link ledger); graph theo tầng (`learn.graph_stats`).
- `GET /memory/{scope}/{owner}`: các mục `§` của một tầng (owner tầng global là `-`); thêm / sửa / xoá mục và ghim/bỏ ghim mục — ghi qua
  `learn.propose` (created_by=user → guard + ledger) và `knowledge.pin`, KHÔNG BAO GIỜ ghi file. Guard chặn → hiện luật + lý do.
  Tầng team/global: canary đang chạy + phiên bản từng agent thấy (`knowledge.versions`).
- `GET /memory/graph`: tìm + lọc fact (RPC mới `learn.graph_view`, learning/graph_view.py), xem trước / áp dụng consolidation.
- `GET /memory/graph/fact/{id}` (`learn.graph_fact`): chuỗi thay thế, cùng cạnh, lượt dùng, ledger; rollback (xác nhận), ghim.
- `GET /memory/graph/entity/{id}`: alias + fact của thực thể. `GET /memory/graph/view`: hình node-link (static/memory_graph.js, SVG tự viết);
  dữ liệu JSON ở `GET /partials/memory/graph` (cần phiên — chưa đăng nhập 401; không gia hạn phiên như mọi `/partials/`).
- `POST /memory/act/{op}`: `post_guard` (CSRF + Origin + khoá phiên WT-1), xác nhận phía server cho thao tác nhạy cảm, RPC `audit=True`
  (`_audit.channel=web` — daemon audit vì method nằm trong `MUTATING`).
Chữ memory/fact là DỮ LIỆU không tin được: template autoescape (không `|safe`), JS chỉ gán `textContent`, JSON escape `<>&`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import quote, urlencode

from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from ..rpc import RpcError
from .app import carry, err_text, flash_redirect, post_guard, render, rpc, safe_next, session_of

GLOBAL_OWNER = "-"
SCOPES = ("agent", "team", "global")
RESULT_KEY = "memory_result"
OPS = ("add", "edit", "remove", "pin", "graph_pin", "fact_pin", "fact_rollback", "consolidate_preview", "consolidate")
CONF_STEPS = ("", "0.3", "0.5", "0.7", "0.9")


# ============================================================================ hàm thuần
def entry_key(text: str) -> str:
    """Khoá một mục memory — cùng công thức `learning.canary.entry_hash` (khoá pin `memory_entry`)."""
    return hashlib.sha1(" ".join(str(text).split()).encode("utf-8")).hexdigest()[:12]


def tier_url(scope: str, owner: str | None) -> str:
    o = GLOBAL_OWNER if scope == "global" or not owner else owner
    return f"/memory/{quote(scope, safe='')}/{quote(str(o), safe='')}"


def tier_label(scope: str, owner: str | None) -> str:
    if scope == "global":
        return "toàn hub"
    return f"{'agent' if scope == 'agent' else 'team'} {owner or '?'}"


def tier_params(scope: str, owner: str | None) -> dict[str, Any]:
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


def files_of(scope: str) -> list[tuple[str, str]]:
    """[(store, tên file)] của một tầng — cùng bố cục `KnowledgeStore.memory_path`."""
    if scope == "team":
        return [("memory", "memory.md")]
    if scope == "global":
        return [("user", "USER.md"), ("memory", "MEMORY.md")]
    return [("memory", "MEMORY.md"), ("user", "USER.md")]


def usage(chars: int, limit: int) -> dict[str, Any]:
    pct = round(100.0 * chars / limit, 1) if limit else 0.0
    return {"chars": chars, "limit": limit, "pct": min(pct, 100.0), "raw_pct": pct, "tone": "danger" if pct >= 95 else "warn" if pct >= 80 else "ok"}


def propose_params(op: str, scope: str, owner: str | None, store: str, *, content: str = "", old_text: str = "") -> dict[str, Any]:
    """Thao tác mục memory của NGƯỜI → params `learn.propose` (pipeline + guard + ledger, created_by=user)."""
    if store not in ("memory", "user"):
        raise ValueError("kho phải là memory hoặc user")
    if scope == "team" and store == "user":
        raise ValueError("team không có USER.md")
    content = content.replace("\r\n", "\n").strip()
    action = {"add": "add", "edit": "patch", "remove": "remove"}[op]
    if action in ("add", "patch") and not content:
        raise ValueError("nhập nội dung mục")
    if action in ("patch", "remove") and not old_text:
        raise ValueError("thiếu mục cần sửa/xoá")
    payload: dict[str, Any] = {"store": store, "action": action}
    if content:
        payload["content"] = content
    if old_text:
        payload["old_text"] = old_text
    target = "user_profile" if scope == "global" and store == "user" else "memory"
    return {**tier_params(scope, owner), "target": target, "op": {"add": "add", "patch": "patch", "remove": "delete"}[action],
            "payload": payload, "created_by": "user", "rationale": f"web: human {op} of a {store} entry"}


def edit_url(back: str, key: str, store: str) -> str:
    """URL tầng mở sẵn form sửa mục `key` (giữ path của `back`, thay query `edit`/`store`)."""
    from urllib.parse import parse_qsl, urlsplit, urlunsplit

    u = urlsplit(back)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k not in ("edit", "store")]
    q += [("edit", key), ("store", store)]
    return urlunsplit(("", "", u.path, urlencode(q), "e-" + quote(key, safe="")))


def parse_tier(v: str | None) -> tuple[str, str | None] | None:
    """Ô chọn tầng của trình duyệt graph: `agent:<id>` | `team:<id>` | `global` | '' (mọi tầng)."""
    v = (v or "").strip()
    if not v:
        return None
    if v == "global":
        return ("global", None)
    sc, _, ow = v.partition(":")
    if sc in ("agent", "team") and ow:
        return (sc, ow)
    return None


def graph_params(q: Any, *, limit: int | None = None) -> dict[str, Any]:
    """Query string trang graph → params `learn.graph_view` (bỏ khoá lạ, kẹp số)."""
    p: dict[str, Any] = {}
    if (q.get("agent") or "").strip():
        p["agent"] = q.get("agent").strip()
    t = parse_tier(q.get("tier"))
    if t:
        p["scope"], p["owner"] = t[0], t[1]
    for k in ("query", "entity"):
        v = (q.get(k) or q.get("q" if k == "query" else "_") or "").strip()
        if v:
            p[k] = v[:500]
    if q.get("history") == "1":
        p["include_history"] = True
    try:
        mc = float(q.get("min_conf") or "")
        p["min_confidence"] = max(0.0, min(mc, 1.0))
    except ValueError:
        pass
    try:
        p["hops"] = max(1, min(int(q.get("hops") or 1), 2))
    except ValueError:
        p["hops"] = 1
    try:
        n = int(q.get("limit") or limit or 150)
    except ValueError:
        n = 150
    p["limit"] = max(10, min(n, 300))
    return p


def safe_json(data: Any) -> str:
    """JSON không thể đóng thẻ HTML/script dù bị nhúng nhầm: `<`, `>`, `&`, U+2028/2029 thành `\\uXXXX`."""
    s = json.dumps(data, ensure_ascii=False, default=str)
    return (s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def fact_safety(text: Any) -> dict[str, Any]:
    """Câu fact hiện cho NGƯỜI: che secret + cờ quét lúc nạp (như recall). Bị cờ → template giấu sau <details> kèm cảnh báo."""
    from ..knowledge.graph.retrieval import safe_fact

    t, flags = safe_fact(str(text or ""))
    return {"text": t, "flags": flags}


def label_safety(text: Any) -> tuple[str | None, list[str]]:
    """Tên thực thể / quan hệ hiện cho NGƯỜI: cùng luật `learn.graph_view` — che secret, bị cờ quét lúc nạp → None (template ghi
    "(tên bị ẩn)"). Không bao giờ đưa tên vào URL (link theo id thực thể)."""
    if text is None or text == "":
        return (None if text is None else ""), []
    s = fact_safety(text)
    return (None if s["flags"] else s["text"]), list(s["flags"])


def safe_plan(res: Any) -> Any:
    """Kết quả `learn.graph_consolidate` dry-run: tên thực thể trong kế hoạch gộp (`entity`/`into`) qua `label_safety`."""
    if not isinstance(res, dict):
        return res
    tiers = []
    for t in res.get("tiers") or []:
        plan = []
        for o in (t.get("plan") or []) if isinstance(t, dict) else []:
            if isinstance(o, dict) and o.get("action") == "merge_entity":
                o = dict(o)
                for k in ("entity", "into"):
                    o[k] = label_safety(o.get(k))[0]
            plan.append(o)
        tiers.append({**t, "plan": plan} if isinstance(t, dict) else t)
    return {**res, "tiers": tiers}


def result_view(res: Any, what: str) -> dict[str, Any]:
    from .skill_pages import result_view as _rv

    return _rv(res, what)


# ============================================================================ đọc: tổng quan
async def _ids(request: Request) -> tuple[list[str], list[str]]:
    agents = [a["id"] for a in await rpc(request, "agent.list", {}) or []]
    try:
        teams = [t["id"] for t in await rpc(request, "team.list", {}) or []]
    except RpcError:
        teams = []
    return agents, teams


async def _soft(request: Request, method: str, params: dict[str, Any], default: Any) -> Any:
    try:
        return await rpc(request, method, params)
    except RpcError:
        return default


def _pop_result(request: Request) -> dict[str, Any] | None:
    sess = session_of(request)
    return sess.extra.pop(RESULT_KEY, None) if sess is not None else None


async def _tier_files(request: Request, scope: str, owner: str | None, pins: set[tuple[str, str, str, str]]) -> list[dict[str, Any]]:
    """[{store, name, entries[{text, key, pinned}], chars, limit, …}] của một tầng (bản SỐNG)."""
    out = []
    okey = "global" if scope == "global" else str(owner)
    if scope == "agent":
        for store, name in files_of("agent"):
            v = await rpc(request, "knowledge.memory", {"agent": owner, "target": store}) or {}
            ents = [{"text": e, "key": entry_key(e), "pinned": ("agent", okey, "memory_entry", entry_key(e)) in pins} for e in v.get("entries") or []]
            out.append({"store": store, "name": name, "entries": ents, **usage(int(v.get("chars") or 0), int(v.get("limit") or 0))})
        return out
    t = await rpc(request, "knowledge.tier", tier_params(scope, owner)) or {}
    mem = t.get("memory") or {}
    for store, name in files_of(scope):
        v = mem.get(name) or {}
        ents = [{"text": e.get("text") or "", "key": e.get("key") or entry_key(e.get("text") or ""), "pinned": bool(e.get("pinned"))} for e in v.get("entries") or []]
        out.append({"store": store, "name": name, "entries": ents, **usage(int(v.get("chars") or 0), int(v.get("limit") or 0))})
    return out


def _last_change(ledger: list[dict[str, Any]], scope: str, owner: str | None, name: str) -> dict[str, Any] | None:
    for e in ledger:  # mới nhất trước
        eo = e.get("owner_id") or e.get("agent_id")
        if e.get("scope") == scope and (scope == "global" or eo == owner) and (e.get("target_ref") == name or (name == "USER.md" and e.get("target") == "user_profile")):
            return e
    return None


def _canaries_for(canaries: list[dict[str, Any]], scope: str, owner: str | None, target: str | None = None) -> list[dict[str, Any]]:
    out = []
    for c in canaries:
        if c.get("scope") != scope or (scope != "global" and c.get("owner_id") != owner):
            continue
        if target == "memory" and c.get("target") not in ("memory", "user_profile"):
            continue
        if target == "graph" and c.get("target") != "graph":
            continue
        out.append(c)
    return out


async def _ledger_memory(request: Request) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for target in ("memory", "user_profile"):
        rows += await _soft(request, "learn.ledger", {"target": target, "limit": 500}, []) or []
    rows.sort(key=lambda e: e.get("applied_at") or "", reverse=True)
    return rows


def _pin_set(pins: list[dict[str, Any]]) -> set[tuple[str, str, str, str]]:
    return {(p.get("scope") or "", p.get("owner_id") or "", p.get("target") or "", p.get("ref") or "") for p in pins or []}


async def memory_page(request: Request) -> Response:
    agents, teams = await _ids(request)
    pins = _pin_set(await _soft(request, "knowledge.pins", {}, []))
    canaries = await _soft(request, "learn.canaries", {"status": "canary"}, []) or []
    ledger = await _ledger_memory(request)
    stats = await _soft(request, "learn.graph_stats", {}, None)
    gtiers = {(t.get("scope"), t.get("owner")): t for t in (stats or {}).get("tiers") or []}
    tiers: list[dict[str, Any]] = []
    for scope, owners in (("global", [None]), ("team", teams), ("agent", agents)):
        for owner in owners:
            try:
                files = await _tier_files(request, scope, owner, pins)
            except RpcError as e:
                files = []
                err = err_text(e)
            else:
                err = None
            for f in files:
                f["pinned_count"] = sum(1 for e in f["entries"] if e["pinned"])
                f["last"] = _last_change(ledger, scope, owner, f["name"])
            okey = "global" if scope == "global" else owner
            tiers.append({"scope": scope, "owner": owner, "label": tier_label(scope, owner), "url": tier_url(scope, owner), "files": files, "error": err,
                          "canaries": _canaries_for(canaries, scope, owner), "graph": gtiers.get((scope, okey)),
                          "graph_pinned": (scope, okey or "", "graph", f"graph://{scope}/{okey}") in pins})
    vec = (stats or {}).get("vectors") or {}
    return await render(request, "memory.html", {"nav": "memory", "tiers": tiers, "stats": stats, "vectors": vec, "result": _pop_result(request)})


def _pop_draft(request: Request) -> dict[str, Any]:
    sess = session_of(request)
    return (sess.extra.pop("memory_draft", None) if sess is not None else None) or {}


# ============================================================================ đọc: một tầng
def _path_tier(request: Request) -> tuple[str, str | None]:
    scope, owner = request.path_params["scope"], request.path_params["owner"]
    if scope not in SCOPES:
        raise HTTPException(404, "Không có tầng memory này")
    return scope, (None if scope == "global" else owner)


async def tier_page(request: Request) -> Response:
    scope, owner = _path_tier(request)
    if scope == "agent":
        await rpc(request, "agent.get", {"id": owner})  # NotFound → 404
    pins = _pin_set(await _soft(request, "knowledge.pins", tier_params(scope, owner) if scope != "global" else {"scope": "global"}, []))
    files = await _tier_files(request, scope, owner, pins)
    ledger = [e for e in await _ledger_memory(request) if e.get("scope") == scope and (scope == "global" or (e.get("owner_id") or e.get("agent_id")) == owner)][:15]
    canaries = _canaries_for(await _soft(request, "learn.canaries", {"status": "canary"}, []) or [], scope, owner, "memory")
    versions: list[dict[str, Any]] = []
    if scope == "agent":
        v = await _soft(request, "knowledge.versions", {"agent": owner}, {}) or {}
        versions = [{"agent": owner, **x} for x in v.get("versions") or [] if x.get("target") in ("memory", "user_profile")]
    else:
        members: list[str] = []
        if scope == "team":
            t = await _soft(request, "knowledge.tier", {"scope": "team", "team": owner}, {}) or {}
            members = list(t.get("members") or [])
        elif canaries:
            members = (await _ids(request))[0]
        if canaries or scope == "team":
            for a in members[:40]:
                v = await _soft(request, "knowledge.versions", {"agent": a}, {}) or {}
                versions += [{"agent": a, **x} for x in v.get("versions") or []
                             if x.get("target") in ("memory", "user_profile") and x.get("scope") == scope and (scope == "global" or x.get("owner") == owner)]
    okey = "global" if scope == "global" else str(owner)
    gstats = await _soft(request, "learn.graph_stats", {}, None)
    graph = next((t for t in (gstats or {}).get("tiers") or [] if t.get("scope") == scope and t.get("owner") == okey), None)
    return await render(request, "memory_tier.html", {
        "nav": "memory", "scope": scope, "owner": owner, "label": tier_label(scope, owner), "url": tier_url(scope, owner), "files": files,
        "ledger": ledger, "canaries": canaries, "versions": versions, "graph": graph, "graph_pinned": (scope, okey, "graph", f"graph://{scope}/{okey}") in pins,
        "edit_key": request.query_params.get("edit") or "", "edit_store": request.query_params.get("store") or "", "result": _pop_result(request),
        "draft": _pop_draft(request),
        "tier_q": "global" if scope == "global" else f"{scope}:{owner}"})


# ============================================================================ đọc: graph
async def _tier_options(request: Request) -> list[tuple[str, str]]:
    agents, teams = await _ids(request)
    return [("global", "toàn hub")] + [(f"team:{t}", f"team {t}") for t in teams] + [(f"agent:{a}", f"agent {a}") for a in agents]


def _named(view: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = {n["id"]: n for n in view.get("nodes") or []}
    rows = []
    for e in view.get("edges") or []:
        s, d = nodes.get(e.get("src")) or {}, nodes.get(e.get("dst") or "") or {}
        rows.append({**e, "src_name": s.get("name"), "dst_name": d.get("name") if e.get("dst") else None, "src_blocked": s.get("blocked"),
                     "dst_blocked": d.get("blocked")})
    return rows


async def graph_page(request: Request) -> Response:
    q = request.query_params
    params = graph_params(q, limit=150)
    view, error = None, None
    try:
        view = await rpc(request, "learn.graph_view", params)
    except RpcError as e:
        if e.code == -32601:
            raise
        error = err_text(e)
    stats = await _soft(request, "learn.graph_stats", {}, None)
    agents, _teams = await _ids(request)
    keep = ("q", "agent", "tier", "history", "min_conf", "hops", "entity", "limit")
    qs = urlencode({k: v for k, v in q.items() if k in keep})
    qs_noent = urlencode({k: v for k, v in q.items() if k in keep and k != "entity"})
    return await render(request, "memory_graph.html", {"nav": "memory", "q": dict(q), "view": view, "rows": _named(view or {}), "error": error,
                                                       "tiers": await _tier_options(request), "agents": agents, "stats": stats, "conf_steps": CONF_STEPS,
                                                       "qs": qs, "qs_noent": qs_noent, "result": _pop_result(request)})


async def graph_view_page(request: Request) -> Response:
    q = request.query_params
    agents, _teams = await _ids(request)
    params = graph_params(q, limit=150)
    src = "/partials/memory/graph?" + urlencode({k: v for k, v in q.items() if k in ("q", "agent", "tier", "history", "min_conf", "hops", "entity", "limit")})
    return await render(request, "memory_graph_view.html", {"nav": "memory", "q": dict(q), "src": src, "tiers": await _tier_options(request), "agents": agents,
                                                            "conf_steps": CONF_STEPS, "params": params})


async def graph_json(request: Request) -> Response:
    """JSON cho hình node-link. `/partials/` → chưa đăng nhập 401, không gia hạn phiên. Lỗi nghiệp vụ → JSON {error} 400/404."""
    params = graph_params(request.query_params, limit=150)
    try:
        view = await rpc(request, "learn.graph_view", params)
    except RpcError as e:
        code = 404 if e.code == -32004 else 400
        return Response(safe_json({"error": err_text(e)}), status_code=code, media_type="application/json")
    return Response(safe_json(view), media_type="application/json")


async def fact_page(request: Request) -> Response:
    h = await rpc(request, "learn.graph_fact", {"id": request.path_params["id"]})
    f = h.get("fact") or {}
    scope, owner = f.get("scope") or "agent", f.get("owner")
    own = None if scope == "global" else owner
    pins = _pin_set(await _soft(request, "knowledge.pins", tier_params(scope, own) if scope != "global" else {"scope": "global"}, []))
    okey = "global" if scope == "global" else str(owner)
    pinned = (scope, okey, "graph_fact", f.get("id") or "") in pins
    safe = {x["id"]: fact_safety(x.get("fact")) for x in [f, *(h.get("superseded_by") or []), *(h.get("supersedes") or []), *(h.get("same_edge") or [])] if x.get("id")}
    # tên thực thể + quan hệ cũng là văn bản không tin được (sửa ngoài pipeline) → che + quét như `learn.graph_view`
    f = dict(f)
    label_flags: list[str] = []
    for k in ("src", "dst", "relation"):
        f[k], fl = label_safety(f.get(k))
        label_flags += fl
    f["label_flags"] = sorted(set(label_flags))
    return await render(request, "memory_fact.html", {"nav": "memory", "h": h, "f": f, "pinned": pinned, "safe": safe, "scope": scope, "owner": own,
                                                      "tier_label": tier_label(scope, own), "tier_url": tier_url(scope, own), "result": _pop_result(request)})


async def entity_page(request: Request) -> Response:
    eid = request.path_params["id"]
    view = await rpc(request, "learn.graph_view", {"entity": eid, "hops": 1, "include_history": True, "limit": 300})
    nodes = {n["id"]: n for n in view.get("nodes") or []}
    center = nodes.get(view.get("center") or "")
    if center is None:
        raise HTTPException(404, "Không tìm thấy thực thể")
    rows = [r for r in _named(view) if center["id"] in (r.get("src"), r.get("dst"))]
    rows.sort(key=lambda r: (not r.get("valid"), r.get("relation") or "", -(r.get("confidence") or 0)))
    neigh = sorted((n for n in nodes.values() if n["id"] != center["id"]), key=lambda n: -(n.get("degree") or 0))
    scope, owner = center.get("scope") or "agent", center.get("owner")
    own = None if scope == "global" else owner
    return await render(request, "memory_entity.html", {"nav": "memory", "e": center, "rows": rows, "neigh": neigh, "truncated": view.get("truncated"),
                                                        "tier_label": tier_label(scope, own), "tier_url": tier_url(scope, own)})


# ============================================================================ ghi
def _form_tier(f: FormData) -> tuple[str, str | None]:
    scope = str(f.get("scope") or "")
    if scope not in SCOPES:
        raise ValueError("tầng không hợp lệ")
    owner = None if scope == "global" else (str(f.get("owner") or "").strip() or None)
    if scope != "global" and not owner:
        raise ValueError("thiếu agent/team")
    return scope, owner


def _confirm(op: str, f: FormData, scope: str | None, owner: str | None) -> tuple[str, bool] | None:
    where = tier_label(scope or "agent", owner) if scope else ""
    store = "USER.md" if f.get("store") == "user" else ("memory.md" if scope == "team" else "MEMORY.md")
    if op == "remove":
        return (f"Xoá mục này khỏi {store} của tầng {where}?\n\n{str(f.get('preview') or '')[:600]}\n\nGhi qua ledger — rollback được.", True)
    if op in ("add", "edit") and scope in ("team", "global"):
        return (f"{'Thêm' if op == 'add' else 'Sửa'} mục trong {store} của tầng {where}?\nÁp dụng NGAY cho mọi agent của tầng này (canary chỉ theo dõi).", False)
    if op == "fact_rollback":
        return (f"Rollback ledger entry {f.get('entry')} đã tạo fact này?\nRollback theo LÔ: mọi fact/thực thể cùng lần ghi đó bị gỡ; fact cũ bị nó đóng "
                "hiệu lực được mở lại. Hàng đã bị đổi sau đó được bỏ qua.", True)
    if op == "consolidate":
        return ("Áp dụng consolidation graph" + (f" cho tầng {where}" if scope else " cho mọi tầng") + "?\nGộp thực thể trùng + fact gần trùng qua "
                "pipeline (created_by=curator, ledger, rollback được). Fact/graph đã ghim được bỏ qua.", True)
    if op == "graph_pin" and f.get("on") != "1":
        return (f"Bỏ ghim graph tầng {where}? Consolidation và bên ghi khác lại được sửa graph này.", False)
    return None


async def memory_act(request: Request) -> Response:
    op = request.path_params["op"]
    if op not in OPS:
        raise HTTPException(404, "Không có thao tác này")
    sess, form = await post_guard(request)
    back = safe_next(form.get("next"), "/memory")
    scope: str | None = None
    owner: str | None = None
    try:
        if op in ("consolidate_preview", "consolidate"):
            t = parse_tier(str(form.get("tier_pick") or ""))
            if t:
                scope, owner = t
            elif form.get("scope"):
                scope, owner = _form_tier(form)
        else:
            scope, owner = _form_tier(form)
        params, method, what = await _build(request, op, form, scope, owner)
    except ValueError as e:
        return flash_redirect(request, back, "error", f"Chưa đủ thông tin: {e}")
    except RpcError as e:
        return flash_redirect(request, back, "error", err_text(e))
    c = _confirm(op, form, scope, owner)
    if c and form.get("confirm") != "yes":
        return await render(request, "confirm.html", {"nav": "memory", "message": c[0], "fields": carry(form), "action": f"/memory/act/{op}",
                                                      "danger": c[1], "back": back, "note": None})
    try:
        res = await rpc(request, method, params, audit=True)
    except RpcError as e:
        return flash_redirect(request, back, "error", err_text(e))
    if op == "consolidate_preview":
        return await render(request, "memory_consolidate.html", {"nav": "memory", "res": safe_plan(res or {}), "scope": scope, "owner": owner,
                                                                 "where": tier_label(scope, owner) if scope else "mọi tầng", "back": back})
    if op == "consolidate":
        items = (res or {}).get("tiers") or []
        n_ok = sum(1 for t in items if t.get("status") in ("applied", "canary"))
        bad = [t for t in items if t.get("status") not in ("applied", "canary", "dry_run")]
        text = f"Consolidation: {n_ok} tầng đã gộp" + (f"; {len(bad)} tầng không áp dụng ({', '.join(str(t.get('status')) for t in bad)})" if bad else "")
        if not items:
            text = "Consolidation: không có gì để gộp"
        return flash_redirect(request, back, "error" if bad and not n_ok else "ok", text)
    if op in ("pin", "graph_pin", "fact_pin"):
        return flash_redirect(request, back, "ok", f"Đã {what}")
    if op == "fact_rollback":
        r = res or {}
        partial = "partial" in (r.get("flags") or []) if isinstance(r.get("flags"), list) else "partial" in str(r.get("flags") or "")
        sess.flash("ok", f"Đã rollback entry {form.get('entry')} — entry rollback {r.get('id') or '?'}"
                   + (" (một phần: có hàng đã đổi sau đó được bỏ qua)" if partial else ""))
        return flash_redirect(request, back)
    v = result_view(res, what)
    sess.flash("ok" if v["ok"] else "error", v["text"])
    sess.extra[RESULT_KEY] = v
    if not v["ok"] and op in ("add", "edit"):
        # guard chặn: quay lại form sửa/thêm với chữ đã nhập (giữ trong phiên — không đưa vào URL)
        sess.extra["memory_draft"] = {"store": form.get("store"), "key": form.get("key") or "", "content": str(form.get("content") or "")[:20000]}
        if op == "edit" and form.get("key"):
            # mở lại đúng form sửa (khoá + kho trong URL — chỉ hash, không phải chữ) để bản nháp hiện ra, không bị _pop_draft nuốt
            back = edit_url(back, str(form.get("key")), str(form.get("store") or "memory"))
    return flash_redirect(request, back)


async def _build(request: Request, op: str, f: FormData, scope: str | None, owner: str | None) -> tuple[dict[str, Any], str, str]:
    if op in ("add", "edit", "remove"):
        assert scope is not None
        store = str(f.get("store") or "memory")
        old = ""
        if op in ("edit", "remove"):
            key = str(f.get("key") or "")
            files = await _tier_files(request, scope, owner, set())
            hit = [e["text"] for fl in files if fl["store"] == store for e in fl["entries"] if e["key"] == key]
            if len(hit) != 1:
                raise ValueError("mục này đã đổi hoặc không còn — tải lại trang")
            old = hit[0]
        p = propose_params(op, scope, owner, store, content=str(f.get("content") or ""), old_text=old)
        return p, "learn.propose", {"add": "thêm mục memory", "edit": "sửa mục memory", "remove": "xoá mục memory"}[op]
    if op == "pin":
        assert scope is not None
        store = str(f.get("store") or "memory")
        on = f.get("on") == "1"
        name = "USER.md" if store == "user" else "MEMORY.md"
        return ({**tier_params(scope, owner), "target": "memory_entry", "ref": str(f.get("key") or ""), "on": on, "file": name}, "knowledge.pin",
                f"{'ghim' if on else 'bỏ ghim'} mục memory")
    if op == "graph_pin":
        assert scope is not None
        on = f.get("on") == "1"
        return {**tier_params(scope, owner), "target": "graph", "ref": "-", "on": on}, "knowledge.pin", f"{'ghim' if on else 'bỏ ghim'} graph {tier_label(scope, owner)}"
    if op == "fact_pin":
        assert scope is not None
        on = f.get("on") == "1"
        fid = str(f.get("fact") or "").strip()
        if not fid:
            raise ValueError("thiếu fact")
        return {**tier_params(scope, owner), "target": "graph_fact", "ref": fid, "on": on}, "knowledge.pin", f"{'ghim' if on else 'bỏ ghim'} fact {fid}"
    if op == "fact_rollback":
        entry = str(f.get("entry") or "").strip()
        if not entry:
            raise ValueError("thiếu ledger entry")
        return {"id": entry, "reason": "web: human rollback of a graph fact"}, "learn.rollback", f"rollback {entry}"
    p: dict[str, Any] = {"dry_run": op == "consolidate_preview"}
    if scope:
        p["scope"] = scope
        p["owner"] = "global" if scope == "global" else owner
    return p, "learn.graph_consolidate", "consolidation"


def routes() -> list[Any]:
    from starlette.routing import Route

    return [
        Route("/memory", memory_page),
        Route("/memory/graph", graph_page),
        Route("/memory/graph/view", graph_view_page),
        Route("/memory/graph/fact/{id}", fact_page),
        Route("/memory/graph/entity/{id}", entity_page),
        Route("/partials/memory/graph", graph_json),
        Route("/memory/act/{op}", memory_act, methods=["POST"]),
        Route("/memory/{scope}/{owner}", tier_page),
    ]


__all__ = ["routes", "entry_key", "tier_url", "tier_label", "tier_params", "propose_params", "graph_params", "parse_tier", "safe_json", "usage", "files_of"]
