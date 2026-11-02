"""Graph memory PR quality — độ chính xác tăng theo thời gian (docs/dev-notes/graph-memory.md § PR quality).

1. **Độ tin cậy theo KẾT QUẢ run** (không theo độ phổ biến): mỗi lần fact được ĐƯA cho một run (prefetch vào tin user đầu, tool
   `memory_recall`) ghi một dòng `mem_fact_uses` (`record_uses`). `settle()` (vòng nền) chốt từng dòng:
   - run bị coi là XẤU → `down` (−`confidence_down`): thay đổi tri thức do run tạo (proposal.run_id) bị cơ chế TỰ ĐỘNG rollback
     (`regression_monitor`/`eval_gate`/`canary`), run nằm trong cửa sổ hồi quy (`regression_watch.observed.run_ids`) của một entry
     đã bị rollback, hoặc run có Signal correction (chỉ số của regression monitor). Dòng đã `up` mà sau đó run thành xấu → đổi sang
     `down` đúng một lần (trừ cả phần đã cộng).
   - run `completed`, đã kết thúc ≥ `settle_h` giờ mà không xấu → `up` (+`confidence_up`).
   - run lỗi/huỷ/không có run (người tra cứu) → `neutral` (lỗi hạ tầng không nói gì về fact).
   Một fact chỉ được tính MỘT lần mỗi run (prefetch + recall cùng run = một lượt). Độ tin cậy kẹp trong [`min`, `max`].
   `confidence`/`hits`/`last_used` là cột VOLATILE (ngoài ledger, rollback không khôi phục) — ngoại lệ có chủ đích như `mem_vectors`.
   Suy giảm theo tuổi chỉ ảnh hưởng HẠNG lúc đọc (`retrieval.decay`), không bao giờ xoá.
2. **Consolidation** (`consolidate()`, vòng nền mỗi `consolidate.interval_h`): theo từng tầng —
   - gộp thực thể trùng: cùng khoá `canon` sau khi bỏ ký tự không phải chữ/số ("Agent-Hub" ≡ "agent hub"), hoặc tên của thực thể này
     là alias của thực thể kia → `merge_entity` (giữ thực thể cũ nhất);
   - gộp fact gần trùng: cùng (src, relation), còn hiệu lực, câu fact gần giống (Jaccard từ ≥ `fact_jaccard`, hoặc cosine vector
     bge-m3 ≥ `fact_cosine` khi có) → `supersede_fact` (fact bị gộp đóng hiệu lực, `invalidated_by` = fact giữ lại; giữ fact có độ tin cậy
     cao nhất, hoà thì mới nhất), độ tin cậy fact giữ = max của nhóm.
   Mọi thay đổi đi qua `pipeline.propose(target="graph", created_by="curator")` → guard + ledger (rollback theo hàng) + canary. Không bao
   giờ xoá episode (consolidation không có thao tác nào chạm `mem_episodes`). Fact/graph người đã pin → bỏ qua.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterable

from ..store import now_iso

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.learning.graph_quality")

CHANNELS = ("prefetch", "recall")
AUTO_ROLLBACK_CALLERS = ("regression_monitor", "eval_gate", "canary")
CORRECTION_KINDS = ("correction", "reject", "cancel", "edit_output", "undo")  # = learning_ops.regression.CORRECTION_KINDS
TERMINAL = ("completed", "failed", "crashed", "protocol_violation", "timed_out", "cancelled", "blocked", "budget_exceeded")
DEFAULTS: dict[str, Any] = {
    "confidence_up": 0.03, "confidence_down": 0.1, "confidence_min": 0.05, "confidence_max": 0.95, "settle_h": 6, "lookback_days": 14,
    "settle_interval_s": 300,
}
CONSOLIDATE_DEFAULTS: dict[str, Any] = {"enabled": True, "interval_h": 24, "fact_jaccard": 0.8, "fact_cosine": 0.95, "max_ops": 150}
_WORD = re.compile(r"\w+", re.U)


def record_uses(conn: Any, fact_ids: Iterable[str], *, run_id: str | None, agent_id: str | None, channel: str, now: str | None = None) -> int:
    """Ghi lượt fact được đưa cho run (một lần mỗi (fact, run)). Không có run (người tra cứu) → không ghi (không có kết quả để học)."""
    if not run_id:
        return 0
    ids = list(dict.fromkeys(i for i in fact_ids if i))
    if not ids:
        return 0
    have = {r[0] for r in conn.execute(f"SELECT fact_id FROM mem_fact_uses WHERE run_id=? AND fact_id IN ({','.join('?' * len(ids))})", (run_id, *ids))}
    now = now or now_iso()
    n = 0
    for fid in ids:
        if fid in have:
            continue
        conn.execute("INSERT INTO mem_fact_uses (fact_id, run_id, agent_id, channel, used_at) VALUES (?,?,?,?,?)", (fid, run_id, agent_id, channel, now))
        n += 1
    return n


def _parse(ts: Any) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def entity_key(name: Any) -> str:
    """Khoá gộp thực thể: canon (NFC + gấp dấu + chữ thường) rồi bỏ mọi ký tự không phải chữ/số."""
    from ..knowledge.graph.text import canon

    return "".join(_WORD.findall(canon(name))).replace("_", "")


def jaccard(a: str, b: str) -> float:
    from ..knowledge.graph.text import canon

    x, y = set(_WORD.findall(canon(a))), set(_WORD.findall(canon(b)))
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


class GraphQuality:
    """`hub.learning.graph_quality`."""

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._last_settle = 0.0

    @property
    def conn(self) -> Any:
        return self.hub.store.conn

    def cfg(self, key: str) -> Any:
        try:
            v = self.hub.config.get(f"graph_memory.quality.{key}")
        except Exception:
            v = None
        return DEFAULTS[key] if v is None else v

    def ccfg(self, key: str) -> Any:
        try:
            v = self.hub.config.get(f"graph_memory.consolidate.{key}")
        except Exception:
            v = None
        return CONSOLIDATE_DEFAULTS[key] if v is None else v

    # ================================================================== độ tin cậy theo kết quả
    def _bad_runs(self, run_ids: list[str]) -> dict[str, str]:
        """run → lý do xấu (thay đổi do run tạo bị tự rollback | trong cửa sổ hồi quy bị rollback | correction)."""
        out: dict[str, str] = {}
        if not run_ids:
            return out
        for i in range(0, len(run_ids), 400):
            chunk = run_ids[i:i + 400]
            q = ",".join("?" * len(chunk))
            for (rid,) in self.conn.execute(
                    f"SELECT DISTINCT p.run_id FROM proposals p JOIN ledger_entries e ON e.proposal_id=p.id JOIN ledger_entries rb ON rb.id=e.rolled_back_by "
                    f"WHERE p.run_id IN ({q}) AND rb.actor IN ({','.join('?' * len(AUTO_ROLLBACK_CALLERS))})", (*chunk, *AUTO_ROLLBACK_CALLERS)):
                out.setdefault(rid, "change_rolled_back")
            for (rid,) in self.conn.execute(f"SELECT DISTINCT run_id FROM signals WHERE run_id IN ({q}) AND kind IN ({','.join('?' * len(CORRECTION_KINDS))})",
                                            (*chunk, *CORRECTION_KINDS)):
                out.setdefault(rid, "correction")
        wanted = set(run_ids)
        for (obs,) in self.conn.execute("SELECT observed FROM regression_watch WHERE status='rolled_back' AND observed IS NOT NULL"):
            try:
                ids = (json.loads(obs) or {}).get("run_ids") or []
            except (TypeError, ValueError):
                continue
            for rid in ids:
                if rid in wanted:
                    out.setdefault(rid, "regression_window")
        return out

    def _bump(self, fact_id: str, delta: float) -> None:
        lo, hi = float(self.cfg("confidence_min")), float(self.cfg("confidence_max"))
        self.conn.execute("UPDATE mem_facts SET confidence=MIN(?, MAX(?, confidence + ?)) WHERE id=?", (hi, lo, float(delta), fact_id))

    def settle(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Chốt các lượt dùng chưa chốt (và lật `up` → `down` khi run hoá ra xấu). Idempotent."""
        now = now or datetime.now(timezone.utc)
        since = _iso(now - timedelta(days=float(self.cfg("lookback_days"))))
        settle_before = now - timedelta(hours=float(self.cfg("settle_h")))
        up, down = float(self.cfg("confidence_up")), float(self.cfg("confidence_down"))
        rows = [dict(r) for r in self.conn.execute(
            "SELECT u.id, u.fact_id, u.run_id, u.outcome, u.delta, r.status, r.ended_at FROM mem_fact_uses u LEFT JOIN runs r ON r.id=u.run_id "
            "WHERE (u.outcome IS NULL OR u.outcome='up') AND u.used_at>=?", (since,))]
        bad = self._bad_runs(sorted({r["run_id"] for r in rows if r["run_id"]}))
        stats = {"up": 0, "down": 0, "neutral": 0, "flipped": 0, "pending": 0}
        ts = _iso(now)
        with self.hub.store.tx():
            for r in rows:
                if r["run_id"] in bad:
                    delta = -down - (float(r["delta"] or 0) if r["outcome"] == "up" else 0.0)
                    self._bump(r["fact_id"], delta)
                    self.conn.execute("UPDATE mem_fact_uses SET outcome='down', delta=?, settled_at=? WHERE id=?", (-down, ts, r["id"]))
                    stats["flipped" if r["outcome"] == "up" else "down"] += 1
                    continue
                if r["outcome"] == "up":
                    continue
                if r["status"] is None and r["run_id"]:  # run không còn (đã purge) → trung lập
                    self.conn.execute("UPDATE mem_fact_uses SET outcome='neutral', settled_at=? WHERE id=?", (ts, r["id"]))
                    stats["neutral"] += 1
                    continue
                if r["status"] not in TERMINAL:
                    stats["pending"] += 1
                    continue
                ended = _parse(r["ended_at"])
                if ended is not None and ended > settle_before:
                    stats["pending"] += 1  # còn trong thời gian chờ correction/rollback
                    continue
                if r["status"] == "completed":
                    self._bump(r["fact_id"], up)
                    self.conn.execute("UPDATE mem_fact_uses SET outcome='up', delta=?, settled_at=? WHERE id=?", (up, ts, r["id"]))
                    stats["up"] += 1
                else:
                    self.conn.execute("UPDATE mem_fact_uses SET outcome='neutral', settled_at=? WHERE id=?", (ts, r["id"]))
                    stats["neutral"] += 1
        return stats

    # ================================================================== consolidation
    def _tiers(self) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in self.conn.execute(
            "SELECT DISTINCT scope, owner_id FROM mem_entities WHERE retracted_by IS NULL AND invalidated_at IS NULL ORDER BY scope, owner_id")]

    def _vectors(self, fact_ids: list[str]) -> dict[str, list[float]]:
        from ..knowledge.graph.embed import get_embedder, unpack

        emb = get_embedder(self.hub)
        if emb is None or not fact_ids:
            return {}
        out: dict[str, list[float]] = {}
        for i in range(0, len(fact_ids), 400):
            chunk = fact_ids[i:i + 400]
            for fid, blob, dim in self.conn.execute(
                    f"SELECT item_id, vec, dim FROM mem_vectors WHERE item_kind='fact' AND model=? AND model_version=? AND item_id IN ({','.join('?' * len(chunk))})",
                    (emb.model, emb.version, *chunk)):
                out[fid] = unpack(blob, dim)
        return out

    def plan_tier(self, scope: str, owner: str) -> dict[str, Any]:
        """Thao tác consolidation cho một tầng (THUẦN — không ghi). Trả {ops, merges, supersedes, conf: {fact_id: confidence mới}}."""
        from ..knowledge.graph.embed import cosine
        from ..knowledge.graph.store import _aliases

        ks = self.hub.knowledge
        own = None if scope == "global" else owner
        ents = [dict(r) for r in self.conn.execute(
            "SELECT * FROM mem_entities WHERE scope=? AND owner_id=? AND retracted_by IS NULL AND invalidated_at IS NULL ORDER BY created_at, rowid",
            (scope, owner))]
        ops: list[dict[str, Any]] = []
        merged: dict[str, str] = {}  # id thực thể bị gộp → id thực thể giữ
        by_key: dict[str, dict[str, Any]] = {}
        for e in ents:
            keys = {entity_key(e["name"]), *(entity_key(a) for a in _aliases(e))} - {""}
            keep = next((by_key[k] for k in keys if k in by_key and by_key[k]["id"] != e["id"]), None)
            if keep is not None:
                merged[e["id"]] = keep["id"]
                ops.append({"action": "merge_entity", "entity": e["name"], "into": keep["name"]})
                for k in keys:
                    by_key.setdefault(k, keep)
            else:
                for k in keys:
                    by_key.setdefault(k, e)
        # fact gần trùng — trên trạng thái SAU khi gộp thực thể (src/dst ánh xạ về thực thể giữ)
        facts = [dict(r) for r in self.conn.execute(
            "SELECT * FROM mem_facts WHERE scope=? AND owner_id=? AND retracted_by IS NULL AND invalidated_at IS NULL ORDER BY ingested_at, rowid",
            (scope, owner))]
        pinned = {f["id"] for f in facts if ks.is_pinned(scope, own, "graph_fact", f["id"])}
        vecs = self._vectors([f["id"] for f in facts])
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for f in facts:
            groups.setdefault((merged.get(f["src_entity_id"], f["src_entity_id"]), f["relation"]), []).append(f)
        jac, cos = float(self.ccfg("fact_jaccard")), float(self.ccfg("fact_cosine"))
        supersedes: list[tuple[str, str]] = []
        conf: dict[str, float] = {}
        moved = {f["id"] for f in facts if f["src_entity_id"] in merged or (f.get("dst_entity_id") or "") in merged}
        for (_src, _rel), fs in groups.items():
            if len(fs) < 2:
                continue
            # giữ: độ tin cậy cao nhất, hoà → mới nhất
            fs = sorted(fs, key=lambda f: (float(f.get("confidence") or 0), f.get("ingested_at") or ""), reverse=True)
            done: set[str] = set()
            for i, keep in enumerate(fs):
                if keep["id"] in done:
                    continue
                for other in fs[i + 1:]:
                    if other["id"] in done or other["id"] in pinned:
                        continue
                    kd, od = merged.get(keep.get("dst_entity_id") or "", keep.get("dst_entity_id")), merged.get(other.get("dst_entity_id") or "", other.get("dst_entity_id"))
                    if kd and od and kd != od:
                        continue  # khác đích → hai sự thật khác nhau (works_on A / works_on B)
                    sim = jaccard(keep["fact"], other["fact"])
                    vs = cosine(vecs[keep["id"]], vecs[other["id"]]) if keep["id"] in vecs and other["id"] in vecs else 0.0
                    if sim >= jac or vs >= cos:
                        if keep["id"] in moved or other["id"] in moved:
                            continue  # fact sắp được chép sang thực thể gộp (id mới) — để lần consolidation sau
                        supersedes.append((other["id"], keep["id"]))
                        done.add(other["id"])
                        conf[keep["id"]] = max(conf.get(keep["id"], float(keep.get("confidence") or 0)), float(other.get("confidence") or 0))
        for drop, keep_id in supersedes:
            ops.append({"action": "supersede_fact", "fact_id": drop, "by": keep_id})
        mx = int(self.ccfg("max_ops"))
        ops = ops[:mx]
        return {"scope": scope, "owner": owner, "ops": ops, "merges": sum(1 for o in ops if o["action"] == "merge_entity"),
                "supersedes": sum(1 for o in ops if o["action"] == "supersede_fact"), "conf": conf}

    def consolidate(self, *, dry_run: bool = False, scope: str | None = None, owner: str | None = None) -> dict[str, Any]:
        """Một lượt consolidation mọi tầng (hoặc một tầng). Ghi qua pipeline (created_by=curator) — mỗi tầng một proposal."""
        from ..errors import Blocked, HubError

        ks = self.hub.knowledge
        out: list[dict[str, Any]] = []
        for sc, ow in self._tiers():
            if scope and sc != scope or (owner and ow != owner):
                continue
            own = None if sc == "global" else ow
            from ..knowledge.graph import graph_key

            if ks.is_pinned(sc, own, "graph", graph_key(sc, own)):
                out.append({"scope": sc, "owner": ow, "status": "skipped", "reason": "pinned"})
                continue
            plan = self.plan_tier(sc, ow)
            if not plan["ops"]:
                continue
            item = {"scope": sc, "owner": ow, "merges": plan["merges"], "supersedes": plan["supersedes"], "ops": len(plan["ops"])}
            if dry_run:
                out.append({**item, "status": "dry_run", "plan": plan["ops"]})
                continue
            agent_id = ow if sc == "agent" else None
            if agent_id and self.hub.store.get_agent(agent_id) is None:
                out.append({**item, "status": "skipped", "reason": "agent không còn"})
                continue
            try:
                res = self.hub.learning.pipeline.propose(target="graph", op="patch", agent_id=agent_id, scope=sc, team_id=ow if sc == "team" else None,
                                                         payload={"operations": plan["ops"]}, created_by="curator",
                                                         rationale=f"graph consolidation: {plan['merges']} entity merge(s), {plan['supersedes']} near-duplicate fact(s)")
            except (Blocked, HubError) as e:
                out.append({**item, "status": "blocked", "reason": e.message[:300]})
                continue
            st = res.get("status")
            if st == "applied":
                for fid, c in plan["conf"].items():  # độ tin cậy fact giữ = max của nhóm (cột VOLATILE)
                    self.conn.execute("UPDATE mem_facts SET confidence=MAX(confidence, ?) WHERE id=?", (c, fid))
            out.append({**item, "status": st, "proposal_id": res.get("proposal_id"), "reason": res.get("block_reason") or res.get("reason")})
        self.hub.store.state_set("graph.consolidated_at", now_iso())
        return {"tiers": out, "dry_run": dry_run}

    # ================================================================== vòng nền (learning_ops tick)
    def consolidate_due(self) -> bool:
        if not bool(self.ccfg("enabled")):
            return False
        last = _parse(self.hub.store.state_get("graph.consolidated_at"))
        return last is None or datetime.now(timezone.utc) - last >= timedelta(hours=float(self.ccfg("interval_h")))

    def tick(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        t = time.monotonic()
        if t - self._last_settle >= float(self.cfg("settle_interval_s")):
            self._last_settle = t
            s = self.settle()
            if any(s[k] for k in ("up", "down", "flipped")):
                out["settle"] = s
        if self.consolidate_due():
            c = self.consolidate()
            if c["tiers"]:
                out["consolidate"] = c
        return out

    # ================================================================== thống kê / lịch sử (CLI `learn graph`)
    def stats(self, agent_id: str | None = None) -> dict[str, Any]:
        where, args = "", []
        if agent_id:
            tiers = self.hub.knowledge.versions.graph_tiers(agent_id)
            from ..knowledge.graph import owner_key

            where = " AND (" + " OR ".join("(scope=? AND owner_id=?)" for _ in tiers) + ")"
            for sc, ow in tiers:
                args += [sc, owner_key(sc, ow)]
        c = self.conn

        def one(sql: str, *a: Any) -> Any:
            return c.execute(sql, a).fetchone()[0]

        tiers_out = []
        for sc, ow, live, inval, conf in c.execute(
                "SELECT scope, owner_id, SUM(invalidated_at IS NULL), SUM(invalidated_at IS NOT NULL), AVG(CASE WHEN invalidated_at IS NULL THEN confidence END) "
                f"FROM mem_facts WHERE retracted_by IS NULL{where} GROUP BY scope, owner_id ORDER BY scope, owner_id", args):
            tiers_out.append({"scope": sc, "owner": ow, "facts": int(live or 0), "history": int(inval or 0), "avg_confidence": round(float(conf), 3) if conf is not None else None,
                              "entities": int(one("SELECT COUNT(*) FROM mem_entities WHERE scope=? AND owner_id=? AND retracted_by IS NULL AND invalidated_at IS NULL", sc, ow)),
                              "episodes": int(one("SELECT COUNT(*) FROM mem_episodes WHERE scope=? AND owner_id=? AND retracted_by IS NULL", sc, ow))})
        uses = {r[0] or "open": int(r[1]) for r in c.execute("SELECT outcome, COUNT(*) FROM mem_fact_uses GROUP BY outcome")}
        vec = {f"{r[0]}@{r[1]}": int(r[2]) for r in c.execute("SELECT model, model_version, COUNT(*) FROM mem_vectors GROUP BY model, model_version")}
        return {"tiers": tiers_out, "uses": uses, "vectors": vec, "consolidated_at": self.hub.store.state_get("graph.consolidated_at"),
                "extractions_today": int(one("SELECT COUNT(*) FROM runs WHERE kind='graph_extract' AND queued_at>=?", _iso(datetime.now(timezone.utc) - timedelta(days=1))))}

    def fact_history(self, fact_id: str) -> dict[str, Any]:
        """Một fact + chuỗi thay thế (trước/sau), các fact khác cùng (src, relation) theo thời gian, episode nguồn (chỉ tham chiếu), ledger, lượt dùng."""
        from ..errors import NotFound
        from ..knowledge.graph import GraphStore

        gs = GraphStore(self.conn)
        f = gs.get_fact(fact_id)
        if f is None:
            rows = self.conn.execute("SELECT id FROM mem_facts WHERE id LIKE ?", (fact_id + "%",)).fetchall()
            if len(rows) != 1:
                raise NotFound(f"không tìm thấy fact '{fact_id}'" + (" (tiền tố trùng nhiều fact)" if rows else ""))
            f = gs.get_fact(rows[0][0])
        assert f is not None
        names = {r[0]: r[1] for r in self.conn.execute("SELECT id, name FROM mem_entities WHERE id IN (?, ?)", (f["src_entity_id"], f.get("dst_entity_id") or ""))}

        def brief(x: dict[str, Any]) -> dict[str, Any]:
            return {k: x.get(k) for k in ("id", "fact", "valid_from", "valid_to", "ingested_at", "invalidated_at", "invalidated_by", "confidence", "hits",
                                          "created_by", "ledger_entry_id", "invalidated_entry_id", "retracted_by")}

        chain_after = []
        cur = f
        seen = {f["id"]}
        while cur.get("invalidated_by") and cur["invalidated_by"] not in seen:
            nxt = gs.get_fact(cur["invalidated_by"])
            if nxt is None:
                break
            seen.add(nxt["id"])
            chain_after.append(brief(nxt))
            cur = nxt
        before = [brief(dict(r)) for r in self.conn.execute("SELECT * FROM mem_facts WHERE invalidated_by=? ORDER BY ingested_at", (f["id"],))]
        same_edge = [brief(dict(r)) for r in self.conn.execute(
            "SELECT * FROM mem_facts WHERE scope=? AND owner_id=? AND src_entity_id=? AND relation=? AND id!=? ORDER BY COALESCE(valid_from, ingested_at), rowid",
            (f["scope"], f["owner_id"], f["src_entity_id"], f["relation"], f["id"]))]
        ep = gs.get_episode(f["source_episode_id"]) if f.get("source_episode_id") else None
        uses = [dict(r) for r in self.conn.execute("SELECT run_id, channel, used_at, outcome, delta FROM mem_fact_uses WHERE fact_id=? ORDER BY id DESC LIMIT 50", (f["id"],))]
        entries = [e for e in (f.get("ledger_entry_id"), f.get("invalidated_entry_id"), f.get("retracted_by")) if e]
        return {"fact": {**brief(f), "scope": f["scope"], "owner": f["owner_id"], "relation": f["relation"], "src": names.get(f["src_entity_id"]),
                         "dst": names.get(f.get("dst_entity_id") or "")},
                "superseded_by": chain_after, "supersedes": before, "same_edge": same_edge,
                "episode": {k: ep.get(k) for k in ("id", "source_kind", "source_ref", "run_id", "session_id", "created_at")} if ep else None,
                "ledger_entries": entries, "uses": uses,
                "rollback_hint": f"cfharness learn graph rollback {f['id']}" if f.get("ledger_entry_id") and not f.get("retracted_by") else None}
