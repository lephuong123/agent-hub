"""Viết lại đường dẫn máy cũ trong TRI THỨC sau khi nhập hub (PR5 của docs/dev-notes/plan-hub-clone.md §6.3 — việc dở của PR1).

Lúc nhập (ngoại tuyến), tri thức thuộc ledger KHÔNG được ghi tay: chỉ liệt kê vào `hub_state.import_followups {files, graph_rows, map, status}`.
Khi daemon lên (`Hub.start` → `process`), từng mục được viết lại bằng bản đồ đường dẫn của lần nhập (`map`) qua `Ledger.apply` — HÀM ÁP DỤNG
DUY NHẤT — với actor `import` (`ledger.IMPORT_ACTOR`):
- `MEMORY.md`/`USER.md` tầng agent, `memory.md` tầng team, `MEMORY.md`/`USER.md` tầng global → Change `file` (rollback THEO MỤC như mọi memory);
- `skills/<tên>/…` (agent/team/global) → Change `tree` cho đúng file đổi (giữ `created_by`/use_count/pin);
- `SOUL.md` của agent → Change `config` (chỉ SOUL);
- `config.yaml` của agent = BẢN SAO của hàng `agents` trong hub.db (đã viết lại lúc nhập) → dựng lại từ DB, không qua ledger (không có ngữ
  nghĩa mới; nguồn sự thật là DB);
- graph memory: fact/thực thể SỐNG (chưa gỡ, chưa đóng hiệu lực) còn đường dẫn cũ → một Change `graph` cho mỗi tầng `(scope, owner)`, sửa
  hàng tại chỗ (`fact`/`name`/`aliases`/`summary` + `folded`/`canon`/`alias_keys`) → rollback THEO HÀNG khôi phục đúng hàng cũ. Episode
  (`mem_episodes` = bằng chứng gốc) và fact đã đóng hiệu lực là LỊCH SỬ → giữ nguyên chữ cũ (như bảng append-only).
- `.archive/` (skill đã lưu trữ), `trash/`… → giữ nguyên (lịch sử), ghi `skipped`.

`import` KHÔNG thuộc `AUTO_CHANGE_ACTORS`: thay đổi do NGƯỜI khởi xướng (lệnh nhập) → regression monitor/eval gate/canary không bao giờ tự
rollback nó (Ledger.rollback từ bên gọi tự động → Conflict human_change); người rollback được như mọi entry.
Idempotent: mục không còn đường dẫn cũ → `unchanged`, không tạo entry. Lỗi một mục không chặn mục khác (`errors`, `status=partial` → thử lại ở
lần daemon lên sau). Kết quả → `import_followups.results` + audit `hub.import_followups` (số đếm) + `hub doctor`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .pathmap import PathMap

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger("cfharness.import_followups")

KEY = "import_followups"
REASON = "import: viết lại đường dẫn của máy cũ sau khi nhập hub (hub import)"


def _pm(fu: dict[str, Any]) -> PathMap:
    pairs = [(str(x.get("from") or ""), str(x.get("to") or "")) for x in fu.get("map") or [] if isinstance(x, dict)]
    return PathMap([(o, n) for o, n in pairs if o and n])


def _has(pm: PathMap, text: str) -> bool:
    return pm.rewrite(text, syntax="text") != text


def _unit(hub: "Hub", rel: str) -> tuple[str, Any] | None:
    """rel (tương đối HUB_HOME, `/`) → (loại, khoá). Loại: memory|skill|soul|agent_config|skip."""
    home = hub.paths.home
    full = home.joinpath(*rel.split("/"))
    agents = {}
    for r in hub.store.conn.execute("SELECT id, home_path FROM agents WHERE archived_at IS NULL"):
        try:
            agents[Path(r[1]).resolve()] = r[0]
        except OSError:  # pragma: no cover
            continue
    rp = full.resolve() if full.exists() else full
    for ah, aid in agents.items():
        try:
            sub = rp.relative_to(ah)
        except ValueError:
            continue
        return _tier_unit("agent", aid, sub.parts)
    parts = tuple(rel.split("/"))
    if parts[0] == "teams" and len(parts) >= 3:
        return _tier_unit("team", parts[1], parts[2:])
    if parts[0] == "global" and len(parts) >= 2:
        return _tier_unit("global", None, parts[1:])
    return ("skip", "không thuộc tri thức của agent/team/global")


def _tier_unit(scope: str, owner: str | None, sub: tuple[str, ...]) -> tuple[str, Any]:
    if not sub:
        return ("skip", "không phải file tri thức")
    if sub[0] == ".archive":
        return ("skip", "skill đã lưu trữ (lịch sử) — giữ nguyên")
    if sub[0] == "skills" and len(sub) >= 3:
        return ("skill", (scope, owner, sub[1], "/".join(sub[2:])))
    if len(sub) == 1:
        name = sub[0]
        if scope == "team" and name == "memory.md":
            return ("memory", (scope, owner, "memory"))
        if scope in ("agent", "global") and name in ("MEMORY.md", "USER.md"):
            return ("memory", (scope, owner, "user" if name == "USER.md" else "memory"))
        if scope == "agent" and name == "SOUL.md":
            return ("soul", owner)
        if scope == "agent" and name == "config.yaml":
            return ("agent_config", owner)
    return ("skip", "không thuộc ledger (giữ nguyên)")


def _memory(hub: "Hub", pm: PathMap, key: tuple[str, str | None, str]) -> dict[str, Any] | None:
    from .learning.ledger import IMPORT_ACTOR, Change

    scope, owner, store = key
    ks = hub.knowledge
    path = ks.memory_path(scope, owner, store)
    if not path.exists():
        return None
    old = path.read_text(encoding="utf-8")
    new = pm.rewrite(old, syntax="text")
    if new == old:
        return None
    target = "user_profile" if (scope == "global" and store == "user") else "memory"
    e = hub.learning.ledger.apply(Change(scope=scope, owner_id=owner, target=target, target_ref=path.name, op="patch", actor=IMPORT_ACTOR,
                                         kind="file", path=path, after=new.encode("utf-8"), agent_id=owner if scope == "agent" else None,
                                         reason=REASON, summary=f"{path.name}: đổi đường dẫn máy cũ → máy này"))
    return e


def _skill(hub: "Hub", pm: PathMap, scope: str, owner: str | None, name: str, rels: list[str]) -> dict[str, Any] | None:
    from .learning.ledger import IMPORT_ACTOR, Change

    ks = hub.knowledge
    d = ks.skill_dir(scope, owner, name)
    files: dict[str, bytes | None] = {}
    for r in sorted(set(rels)):
        p = d.joinpath(*r.split("/"))
        try:
            old = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new = pm.rewrite(old, syntax="text")
        if new != old:
            files[r] = new.encode("utf-8")
    if not files:
        return None
    return hub.learning.ledger.apply(Change(scope=scope, owner_id=owner, target="skill", target_ref=name, op="patch", actor=IMPORT_ACTOR,
                                            kind="tree", path=d, files=files, agent_id=owner if scope == "agent" else None, reason=REASON,
                                            summary=f"skill {name}: đổi đường dẫn máy cũ → máy này ({len(files)} file)"))


def _soul(hub: "Hub", pm: PathMap, aid: str) -> dict[str, Any] | None:
    from .learning.ledger import IMPORT_ACTOR, Change

    row = hub.agents.get_row(aid)
    p = Path(row["home_path"]) / "SOUL.md"
    if not p.exists():
        return None
    old = p.read_text(encoding="utf-8")
    new = pm.rewrite(old, syntax="text")
    if new == old:
        return None
    return hub.learning.ledger.apply(Change(scope="agent", owner_id=aid, target="config", target_ref="soul", op="patch", actor=IMPORT_ACTOR,
                                            kind="config", config_changes={}, soul=new, agent_id=aid, reason=REASON,
                                            summary="SOUL.md: đổi đường dẫn máy cũ → máy này"))


def _agent_config(hub: "Hub", pm: PathMap, aid: str) -> str:
    """Bản sao `agents/<id>/config.yaml` dựng lại từ hàng DB (đã viết lại đường dẫn lúc nhập)."""
    row = hub.agents.get_row(aid)
    home = Path(row["home_path"])
    p = home / "config.yaml"
    before = p.read_text(encoding="utf-8") if p.exists() else ""
    hub.agents._write_config_file(home, hub.agents.config_of(row))
    after = p.read_text(encoding="utf-8")
    if _has(pm, after):
        raise RuntimeError("config.yaml dựng từ DB vẫn còn đường dẫn máy cũ")
    return "rewritten" if after != before else "unchanged"


def graph_rows(conn: Any, pm: PathMap) -> list[dict[str, Any]]:
    """Hàng graph SỐNG còn đường dẫn cũ: [{table, row}] — fact (`fact`), thực thể (`name`/`aliases`/`summary`)."""
    needles = pm.needles()
    if not needles:
        return []
    out: list[dict[str, Any]] = []
    tabs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    specs = (("mem_facts", ("fact",)), ("mem_entities", ("name", "aliases", "summary")))
    for table, cols in specs:
        if table not in tabs:
            continue
        cond = " OR ".join(f'IFNULL("{c}", \'\') LIKE ?' for c in cols for _ in needles)
        args = [f"%{n}%" for _c in cols for n in needles]
        q = f"SELECT * FROM {table} WHERE retracted_by IS NULL AND invalidated_at IS NULL AND ({cond}) ORDER BY id"
        for r in conn.execute(q, args):
            out.append({"table": table, "row": {k: r[k] for k in r.keys() if k != "rowid"}})
    return out


def _rewrite_row(pm: PathMap, table: str, row: dict[str, Any]) -> dict[str, Any] | None:
    from .knowledge.graph.store import _alias_keys
    from .knowledge.graph.text import canon, folded

    new = dict(row)
    if table == "mem_facts":
        new["fact"] = pm.rewrite(str(row.get("fact") or ""), syntax="text")
        new["folded"] = folded(new["fact"])
    else:
        new["name"] = pm.rewrite(str(row.get("name") or ""), syntax="text")
        if row.get("summary"):
            new["summary"] = pm.rewrite(str(row["summary"]), syntax="text")
        try:
            al = json.loads(row.get("aliases") or "[]")
        except ValueError:
            al = []
        al = [pm.rewrite(str(a), syntax="text") for a in al if isinstance(a, str)]
        new["aliases"] = json.dumps(al, ensure_ascii=False)
        new["alias_keys"] = _alias_keys(al)
        new["canon"] = canon(new["name"])
        new["folded"] = folded(" ".join([new["name"], *al]))
    return new if new != row else None


def _graph(hub: "Hub", pm: PathMap) -> list[dict[str, Any]]:
    from .knowledge.graph.store import graph_key
    from .learning.ledger import IMPORT_ACTOR, Change

    tiers: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for m in graph_rows(hub.store.conn, pm):
        new = _rewrite_row(pm, m["table"], m["row"])
        if new is None:
            continue
        tiers.setdefault((str(m["row"]["scope"]), str(m["row"]["owner_id"])), []).append({"table": m["table"], "id": new["id"], "row": new})
    out: list[dict[str, Any]] = []
    for (scope, owner), rows in sorted(tiers.items()):
        key = graph_key(scope, None if scope == "global" else owner)
        e = hub.learning.ledger.apply(Change(scope=scope, owner_id=None if scope == "global" else owner, target="graph", target_ref=key,
                                             op="patch", actor=IMPORT_ACTOR, kind="graph", graph_rows=rows, graph_key=key,
                                             agent_id=owner if scope == "agent" else None, reason=REASON,
                                             summary=f"graph {key}: đổi đường dẫn máy cũ → máy này ({len(rows)} hàng)"))
        out.append({"key": key, "rows": len(rows), "entry": e["id"]})
    return out


def process(hub: "Hub") -> dict[str, Any] | None:
    """Xử lý `hub_state.import_followups` (gọi ở `Hub.start`; chạy lại an toàn). None khi không có gì phải làm."""
    fu = hub.store.state_get(KEY)
    if not isinstance(fu, dict) or fu.get("status") == "done" or getattr(hub, "learning", None) is None:
        return None
    pm = _pm(fu)
    from .store import now_iso

    res: dict[str, Any] = {"rewritten": [], "unchanged": [], "skipped": [], "errors": [], "entries": [], "graph": [], "at": now_iso()}
    files = list(dict.fromkeys([*(fu.get("files") or []), *(fu.get("retry") or [])]))
    skills: dict[tuple[str, str | None, str], list[str]] = {}
    skill_rels: dict[tuple[str, str | None, str], list[str]] = {}
    if pm:
        for rel in files:
            try:
                u = _unit(hub, str(rel))
            except Exception as e:  # pragma: no cover
                log.debug("import_followups: không phân loại được đường dẫn", exc_info=True)
                res["errors"].append({"path": rel, "error": str(e)[:300]})
                continue
            kind, key = u if u else ("skip", "?")
            try:
                if kind == "skip":
                    res["skipped"].append({"path": rel, "reason": key})
                elif kind == "skill":
                    scope, owner, name, r = key
                    skills.setdefault((scope, owner, name), []).append(r)
                    skill_rels.setdefault((scope, owner, name), []).append(rel)
                elif kind == "memory":
                    e = _memory(hub, pm, key)
                    _done(res, rel, e)
                elif kind == "soul":
                    _done(res, rel, _soul(hub, pm, key))
                elif kind == "agent_config":
                    (res["rewritten"] if _agent_config(hub, pm, key) == "rewritten" else res["unchanged"]).append(rel)
            except Exception as e:
                log.warning("viết lại đường dẫn %s lỗi: %s", rel, e, exc_info=True)
                res["errors"].append({"path": rel, "error": str(e)[:300]})
        for k, rels in skills.items():
            try:
                e = _skill(hub, pm, k[0], k[1], k[2], rels)
                for rel in skill_rels[k]:
                    _done(res, rel, e, record=False)
                if e is not None:
                    res["entries"].append(e["id"])
            except Exception as ex:
                log.warning("viết lại đường dẫn skill %s lỗi: %s", k[2], ex, exc_info=True)
                res["errors"] += [{"path": rel, "error": str(ex)[:300]} for rel in skill_rels[k]]
        try:
            res["graph"] = _graph(hub, pm)
            res["entries"] += [g["entry"] for g in res["graph"]]
        except Exception as e:
            log.warning("viết lại đường dẫn graph memory lỗi: %s", e, exc_info=True)
            res["errors"].append({"path": "graph", "error": str(e)[:300]})
    remaining_graph = len(graph_rows(hub.store.conn, pm)) if pm else 0
    status = "partial" if res["errors"] else "done"
    fu2 = {**fu, "status": status, "processed_at": res["at"], "retry": [x["path"] for x in res["errors"] if x["path"] != "graph"],
           "graph_rows_remaining": remaining_graph,
           "results": {k: (res[k] if k in ("errors", "skipped", "graph") else len(res[k])) for k in ("rewritten", "unchanged", "skipped", "errors",
                                                                                                   "entries", "graph")}}
    hub.store.state_set(KEY, fu2)
    try:
        hub.audit.record("hub.import_followups", None, None,
                         {"status": status, "rewritten": len(res["rewritten"]), "unchanged": len(res["unchanged"]), "skipped": len(res["skipped"]),
                          "errors": len(res["errors"]), "ledger_entries": len(res["entries"]), "graph_tiers": len(res["graph"]),
                          "graph_rows_remaining": remaining_graph},
                         actor="import", channel="daemon", summary="viết lại đường dẫn tri thức sau khi nhập hub")
    except Exception:  # pragma: no cover
        log.exception("không ghi được audit hub.import_followups")
    if res["rewritten"] or res["errors"]:
        log.info("sau nhập hub: viết lại %d file tri thức (%d entry ledger), %d lỗi", len(res["rewritten"]), len(res["entries"]), len(res["errors"]))
    return {**res, "status": status, "graph_rows_remaining": remaining_graph}


def _done(res: dict[str, Any], rel: str, e: dict[str, Any] | None, *, record: bool = True) -> None:
    if e is None:
        res["unchanged"].append(rel)
    else:
        res["rewritten"].append(rel)
        if record:
            res["entries"].append(e["id"])


__all__ = ["KEY", "process", "graph_rows"]
