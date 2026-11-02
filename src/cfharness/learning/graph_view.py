"""`learn.graph_view` — đồ thị con CHỈ ĐỌC của graph memory cho NGƯỜI (trang web `/memory/graph`, docs/dev-notes/web-memory.md).

Một RPC phục vụ cả danh sách fact của trình duyệt graph lẫn hình node-link: thực thể = node, fact = cạnh (fact không có đích là
"thuộc tính" của node — chỉ hiện trong panel, đếm vào bậc). Hạt giống:
- `entity` (id hoặc tên/alias): fact chạm thực thể, mở rộng 1–2 bước (`GraphStore.neighbors`);
- `query`: xếp hạng lai như agent (`Retriever.search`: FTS5 + vector khi model đã nạp + thực thể + mở rộng) — KHÔNG `touch`
  (người xem không làm đổi `hits`/`last_used`, không ghi `mem_fact_uses`);
- không có gì: fact mới nhất của các tầng.

Phạm vi nhìn như `memory.recall`: `agent` → đúng tầng + ghim canary agent đó thấy (`graph_tiers`, `graph_hidden`); bỏ trống → mọi tầng
(người). Lọc thêm `scope`/`owner` (một tầng), `min_confidence`, `include_history`. Mọi văn bản trả ra (câu fact, tên/alias/kiểu/tóm tắt
thực thể) qua `redact_str` + `threats.scan_load` như lúc nạp vào ngữ cảnh agent: bị cờ → không trả văn bản (`blocked` + `flags`).
Văn bản episode KHÔNG BAO GIỜ được trả (chỉ id). Method HUMAN_ONLY (như `learn.graph_*`); không thay đổi trạng thái.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..errors import HubError, NotFound

if TYPE_CHECKING:
    from ..hub import Hub

MAX_NODES = 300
DEFAULT_NODES = 150
MAX_FACTS = 600
FACT_COLS = ("id", "relation", "valid_from", "valid_to", "ingested_at", "invalidated_at", "invalidated_by", "created_by", "ledger_entry_id",
             "invalidated_entry_id", "source_episode_id", "hits", "last_used")


def _safe(text: Any) -> tuple[str | None, list[str]]:
    """(văn bản đã che secret | None nếu bị cờ, cờ). Cùng luật `retrieval.safe_fact` (quét lúc nạp)."""
    from ..knowledge.graph.retrieval import safe_fact

    if text is None or text == "":
        return (None if text is None else "", [])
    t, flags = safe_fact(str(text))
    return (None if flags else t), flags


def _int(v: Any, default: int, lo: int, hi: int, name: str) -> int:
    if v is None or v == "":
        return default
    try:
        return max(lo, min(int(v), hi))
    except (TypeError, ValueError):
        raise HubError(f"{name} phải là số nguyên", code=-32602) from None


def _float(v: Any, name: str) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise HubError(f"{name} phải là số", code=-32602) from None


def resolve_tiers(hub: "Hub", p: dict[str, Any]) -> tuple[list[tuple[str, str | None]], set[str]]:
    """(tầng, entry ẩn) theo `agent` (tầm nhìn của agent đó) và bộ lọc `scope`/`owner`."""
    from ..knowledge.graph.retrieval import tiers_all

    agent = p.get("agent") or p.get("agent_id")
    if agent:
        if hub.store.get_agent(str(agent)) is None:
            raise NotFound(f"không tìm thấy agent '{agent}'")
        v = hub.knowledge.versions
        tiers, hidden = list(v.graph_tiers(str(agent))), set(v.graph_hidden(str(agent)))
    else:
        tiers, hidden = list(tiers_all(hub.store.conn)), set()
        # tầng chỉ có thực thể (chưa có fact) vẫn liệt kê được khi lọc đúng tầng
    scope = p.get("scope") or None
    owner = p.get("owner") or None
    if scope:
        if scope not in ("agent", "team", "global"):
            raise HubError("scope: agent | team | global", code=-32602)
        if scope != "global" and not owner:
            raise HubError(f"scope {scope} cần owner", code=-32602)
        want = (scope, None if scope == "global" else str(owner))
        if agent:
            tiers = [t for t in tiers if t == want]
        else:
            tiers = [want]
    return tiers, hidden


def _entity_row(hub: "Hub", ref: str, tiers: list[tuple[str, str | None]], hidden: set[str]) -> dict[str, Any] | None:
    from ..knowledge.graph import GraphStore

    gs = GraphStore(hub.store.conn)
    e = gs.visible(gs.get_entity(ref), hidden)
    if e is not None:
        return e if (e["scope"], None if e["scope"] == "global" else e["owner_id"]) in tiers else None
    for sc, ow in tiers:
        e = gs.visible(gs.find_entity(sc, ow, ref), hidden)
        if e is not None:
            return e
    return None


def _node(e: dict[str, Any]) -> dict[str, Any]:
    from ..knowledge.graph.store import _aliases

    name, fl = _safe(e.get("name"))
    flags = list(fl)
    aliases = []
    for a in _aliases(e)[:20]:
        t, f = _safe(a)
        if t is not None:
            aliases.append(t)
        else:
            flags += f
    etype, ft = _safe(e.get("type"))
    summary, fs = _safe(e.get("summary"))
    flags += ft + fs
    return {"id": e["id"], "name": name, "type": etype, "aliases": aliases, "summary": summary, "scope": e["scope"], "owner": e["owner_id"],
            "created_by": e.get("created_by"), "created_at": e.get("created_at"), "ledger_entry_id": e.get("ledger_entry_id"),
            "invalidated_at": e.get("invalidated_at"), "blocked": bool(flags), "flags": sorted(set(flags)), "degree": 0, "facts": 0}


def _edge(f: dict[str, Any]) -> dict[str, Any]:
    text, flags = _safe(f.get("fact"))
    rel, fr = _safe(f.get("relation"))
    flags = list(flags) + list(fr)
    out = {k: f.get(k) for k in FACT_COLS}
    out.update({"src": f["src_entity_id"], "dst": f.get("dst_entity_id"), "fact": text, "relation": rel, "scope": f["scope"], "owner": f["owner_id"],
                "confidence": round(float(f.get("confidence") if f.get("confidence") is not None else 0.5), 3), "valid": not f.get("invalidated_at"),
                "blocked": bool(flags), "flags": sorted(set(flags))})
    if f.get("score") is not None:
        out["score"] = f["score"]
    return out


def graph_view(hub: "Hub", p: dict[str, Any]) -> dict[str, Any]:
    """`learn.graph_view {agent?, scope?, owner?, entity?, query?, hops?, limit?, include_history?, min_confidence?}` →
    `{nodes, edges, center, tiers, truncated, mode}`. `limit` = trần số node (mặc định 150, tối đa 300)."""
    from ..knowledge.graph import GraphStore
    from ..knowledge.graph.provider import _cfg, provider_for

    tiers, hidden = resolve_tiers(hub, p)
    limit = _int(p.get("limit"), DEFAULT_NODES, 1, MAX_NODES, "limit")
    hops = _int(p.get("hops"), 1, 1, 2, "hops")
    history = bool(p.get("include_history"))
    min_conf = _float(p.get("min_confidence"), "min_confidence")
    query = str(p.get("query") or "").strip()[:2000]
    ent_ref = str(p.get("entity") or "").strip()
    gs = GraphStore(hub.store.conn)
    center = None
    mode = "recent"
    facts: list[dict[str, Any]] = []
    if not tiers:
        return {"nodes": [], "edges": [], "center": None, "tiers": [], "truncated": False, "mode": mode, "query": query}
    if ent_ref:
        mode = "entity"
        e = _entity_row(hub, ent_ref, tiers, hidden)
        if e is None:
            raise NotFound(f"không tìm thấy thực thể '{ent_ref}' trong phạm vi đang xem")
        center = e["id"]
        facts = gs.neighbors(e["id"], tiers, hops=hops, hidden=hidden, include_invalid=history, max_facts=MAX_FACTS)["facts"]
        facts.sort(key=lambda f: (f["src_entity_id"] != center and f.get("dst_entity_id") != center, f.get("invalidated_at") is not None,
                                  -(float(f.get("confidence") or 0))))
    elif query:
        mode = "query"
        prov = provider_for(hub)
        r = prov.retriever()
        qvec = None
        emb = prov.embedder()
        if emb is not None and getattr(emb, "ready", False):
            try:
                qvec = emb.embed([query])[0]
            except Exception:
                qvec = None
        ranked = r.search(tiers, query, hidden=hidden, limit=50, hops=hops, include_history=history, query_vec=qvec,
                          half_life_days=float(_cfg(hub, "recall.half_life_days", 90)))
        by_id = {f["id"]: f for f in ranked}
        rows = [gs.visible(gs.get_fact(fid), hidden) for fid in by_id]
        facts = []
        for row in rows:
            if row is not None:
                row = dict(row, score=by_id[row["id"]]["score"])
                facts.append(row)
    else:
        facts = gs.facts(tiers, include_invalid=history, hidden=hidden, limit=MAX_FACTS)
    if min_conf is not None:
        facts = [f for f in facts if float(f.get("confidence") or 0) >= min_conf]
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    truncated = False

    def node(eid: str) -> dict[str, Any] | None:
        if eid in nodes:
            return nodes[eid]
        e = gs.visible(gs.get_entity(eid), hidden)
        if e is None:
            return None
        nodes[eid] = _node(e)
        return nodes[eid]

    if center is not None:
        node(center)
    for f in facts:
        ends = [x for x in (f["src_entity_id"], f.get("dst_entity_id")) if x]
        new = [x for x in dict.fromkeys(ends) if x not in nodes]
        if len(nodes) + len(new) > limit:
            truncated = True
            continue
        ok = all(node(x) is not None for x in ends)
        if not ok:
            continue
        e = _edge(f)
        edges.append(e)
        for x in dict.fromkeys(ends):
            nodes[x]["degree"] += 1
            nodes[x]["facts"] += 1
        if len(edges) >= MAX_FACTS:
            truncated = True
            break
    tiers_out = [{"scope": sc, "owner": "global" if sc == "global" else ow} for sc, ow in tiers]
    return {"nodes": list(nodes.values()), "edges": edges, "center": center, "tiers": tiers_out, "truncated": truncated, "mode": mode,
            "query": query, "hops": hops, "include_history": history, "agent": p.get("agent") or p.get("agent_id")}


__all__ = ["graph_view", "resolve_tiers", "MAX_NODES", "DEFAULT_NODES"]
