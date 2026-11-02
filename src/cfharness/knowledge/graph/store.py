"""`GraphStore` — đọc graph memory + lập kế hoạch thay đổi (THUẦN, không ghi) + phép biến đổi hàng cho Ledger.

Nguyên tắc (giống kho memory file): module này KHÔNG tự ghi tri thức. `plan_ops` tính các hàng sẽ thêm/sửa; người ghi DUY NHẤT là
`learning.ledger.Ledger.apply(Change(kind="graph"))` (gọi `write_rows`). Rollback theo HÀNG (`invert_rows`) — như `invert_memory`:
hàng entry tạo thì gỡ (`retracted_by`), hàng entry đóng hiệu lực thì mở lại; hàng đã bị thay đổi SAU entry thì bỏ qua.

Thao tác (`operations`, mỗi phần tử một dict `action`):
- `add_entity` {name, type?, aliases?[], summary?}            — thực thể đã có (theo `canon`/alias) → chỉ thêm alias mới.
- `add_alias` {entity, alias}
- `add_fact` {src, relation, dst?, fact, valid_from?, valid_to?, confidence?, src_type?, dst_type?, multi?}
    · trùng fact còn hiệu lực (cùng src, relation, dst, câu fact sau `canon`) → BỎ (ghi vào `dropped`), không lỗi;
    · mâu thuẫn: còn fact hiệu lực khác trên cùng (src, relation) và không `multi` → fact cũ bị ĐÓNG hiệu lực (invalidate,
      `valid_to` = `valid_from` của fact mới hoặc bây giờ), không bao giờ xoá.
- `invalidate_fact` {fact_id} | {src, relation, dst?}         — đóng hiệu lực (thông tin đã sai/hết hạn).
- `invalidate_entity` {entity}
- `merge_entity` {entity, into}                               — (consolidation) gộp thực thể trùng: tên + alias của `entity` thành alias
      của `into`; mỗi fact còn hiệu lực chạm `entity` được CHÉP sang `into` (giữ valid_from/valid_to/ingested_at/độ tin cậy/provenance)
      và bản cũ bị đóng với `invalidated_by` = bản chép (liên kết thay thế); `entity` bị đóng (`invalidated_at`). Không xoá gì.
- `supersede_fact` {fact_id, by}                              — (consolidation) fact gần trùng: đóng `fact_id`, `invalidated_by` = `by`
      (fact giữ lại). Thời gian thế giới (`valid_to`) giữ nguyên — trùng lặp không có nghĩa là hết hiệu lực.
"""

from __future__ import annotations

import json
import sqlite3
import unicodedata
import uuid
from typing import Any, Iterable

from ...redact import redact_str
from ...store import now_iso
from ..store import KnowledgeError
from .schema import has_fts
from .text import canon, folded, nfc, norm_relation

TABLES = ("mem_episodes", "mem_entities", "mem_facts")
ENTRY = "$ENTRY"  # chỗ giữ id ledger entry — Ledger.apply thay bằng id thật
KEY_PREFIX = "graph://"
# Cột thay đổi ngoài ledger (đếm dùng, điểm tin cậy cập nhật theo kết quả run): không thuộc trạng thái ledger, không bị rollback
# khôi phục, không làm rollback theo hàng "thấy bị sửa sau".
VOLATILE = frozenset({"hits", "last_used", "confidence"})
SNAPSHOT_VOLATILE = frozenset({"hits", "last_used"})
ACTIONS = ("add_entity", "add_alias", "add_fact", "invalidate_fact", "invalidate_entity", "merge_entity", "supersede_fact")
MAX_OPS = 200
# Trần độ dài (review): tên/alias/kiểu/tóm tắt thực thể KHÔNG có trần thì văn bản sau mốc `threats.MAX_SCAN_CHARS` (64 KiB) của
# lần quét lúc ghi không bao giờ được quét. Tổng văn bản mới của MỘT lô < MAX_SCAN_CHARS để guard quét trọn lô.
MAX_NAME_CHARS = 200
MAX_TYPE_CHARS = 64
MAX_SUMMARY_CHARS = 1000
MAX_BATCH_TEXT = 60_000
# Cờ nội bộ trên thao tác: CHỈ trích xuất (created_by=review) gắn khi bằng chứng có lượt NGƯỜI. Không có cờ này, bên ghi không
# phải người không đóng được fact người khai (created_by=user) — chống MINJA: agent lặp lại nội dung web/tool độc.
USER_GROUNDED = "_user_grounded"
SECTION = {"mem_facts": "facts", "mem_entities": "entities"}
SNAPSHOT_FILE = ".graph_snapshot.json"  # bản chụp graph trong BẢN SAO home của eval (evals/overlay.make_home) — home thật không có


def owner_key(scope: str, owner: str | None) -> str:
    if scope == "global":
        return "global"
    if not owner:
        raise KnowledgeError(f"scope {scope} cần owner", "invalid")
    return str(owner)


def graph_key(scope: str, owner: str | None) -> str:
    """Đường dẫn tổng hợp của graph một tầng — dùng làm `path` canary (một đích một canary), `target_ref` ledger."""
    return f"{KEY_PREFIX}{scope}/{owner_key(scope, owner)}"


def is_graph_key(path: Any) -> bool:
    return str(path or "").startswith(KEY_PREFIX)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _clean_text(s: Any) -> str:
    return nfc(redact_str(str(s or "")))


def _aliases(row: dict[str, Any]) -> list[str]:
    v = row.get("aliases")
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = []
    return list(v or [])


def _alias_keys(names: Iterable[str]) -> str:
    keys = sorted({canon(n) for n in names if canon(n)})
    return "|" + "|".join(keys) + "|" if keys else ""


def strip(row: dict[str, Any] | None, cols: frozenset[str] = VOLATILE) -> dict[str, Any] | None:
    return None if row is None else {k: v for k, v in row.items() if k not in cols and k != "rowid"}


class GraphStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ================================================================== đọc
    def _one(self, table: str, rid: str) -> dict[str, Any] | None:
        r = self.conn.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
        return dict(r) if r else None

    def get_entity(self, rid: str) -> dict[str, Any] | None:
        return self._one("mem_entities", rid)

    def get_fact(self, rid: str) -> dict[str, Any] | None:
        return self._one("mem_facts", rid)

    def get_episode(self, rid: str) -> dict[str, Any] | None:
        return self._one("mem_episodes", rid)

    def find_entity(self, scope: str, owner: str | None, name: str) -> dict[str, Any] | None:
        """Thực thể CÒN DÙNG theo tên chuẩn (canon) hoặc alias trong đúng tầng."""
        c = canon(name)
        if not c:
            return None
        ok = owner_key(scope, owner)
        r = self.conn.execute("SELECT * FROM mem_entities WHERE scope=? AND owner_id=? AND canon=? AND retracted_by IS NULL AND invalidated_at IS NULL "
                              "ORDER BY created_at LIMIT 1", (scope, ok, c)).fetchone()
        if r is None:
            r = self.conn.execute("SELECT * FROM mem_entities WHERE scope=? AND owner_id=? AND alias_keys LIKE ? AND retracted_by IS NULL "
                                  "AND invalidated_at IS NULL ORDER BY created_at LIMIT 1", (scope, ok, f"%|{c}|%")).fetchone()
        return dict(r) if r else None

    def live_facts(self, scope: str, owner: str | None, src_entity_id: str, relation: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM mem_facts WHERE scope=? AND owner_id=? AND src_entity_id=? AND retracted_by IS NULL AND invalidated_at IS NULL"
        args: list[Any] = [scope, owner_key(scope, owner), src_entity_id]
        if relation is not None:
            sql += " AND relation=?"
            args.append(relation)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY ingested_at, rowid", args)]

    @staticmethod
    def visible(row: dict[str, Any] | None, hidden: Iterable[str] = ()) -> dict[str, Any] | None:
        """Hàng như agent thấy: bỏ hàng đã gỡ; `hidden` = ledger entry đang canary mà agent bị ghim về bản trước — hàng entry đó tạo
        bị ẩn, phép đóng hiệu lực của entry đó coi như chưa xảy ra."""
        if row is None or row.get("retracted_by"):
            return None
        hid = set(hidden or ())
        if row.get("ledger_entry_id") in hid:
            return None
        if row.get("invalidated_entry_id") and row["invalidated_entry_id"] in hid:
            # valid_to giữ nguyên: ghim chỉ ẩn thay đổi hệ thống, không đoán lại thời gian thế giới
            row = dict(row, invalidated_at=None, invalidated_by=None, invalidated_entry_id=None)
        return row

    def _tier_where(self, tiers: list[tuple[str, str | None]], alias: str = "") -> tuple[str, list[Any]]:
        a = f"{alias}." if alias else ""
        parts, args = [], []
        for sc, ow in tiers:
            parts.append(f"({a}scope=? AND {a}owner_id=?)")
            args += [sc, owner_key(sc, ow)]
        return "(" + " OR ".join(parts) + ")" if parts else "0", args

    def facts(self, tiers: list[tuple[str, str | None]], *, entity_id: str | None = None, relation: str | None = None,
              include_invalid: bool = False, hidden: Iterable[str] = (), limit: int = 1000) -> list[dict[str, Any]]:
        where, args = self._tier_where(tiers)
        sql = f"SELECT * FROM mem_facts WHERE {where}"
        if entity_id:
            sql += " AND (src_entity_id=? OR dst_entity_id=?)"
            args += [entity_id, entity_id]
        if relation:
            sql += " AND relation=?"
            args.append(norm_relation(relation))
        out = []
        for r in self.conn.execute(sql + " ORDER BY ingested_at DESC, rowid DESC LIMIT ?", (*args, int(limit))):
            v = self.visible(dict(r), hidden)
            if v is not None and (include_invalid or not v.get("invalidated_at")):
                out.append(v)
        return out

    def neighbors(self, entity_id: str, tiers: list[tuple[str, str | None]], *, hops: int = 1, hidden: Iterable[str] = (),
                  include_invalid: bool = False, max_facts: int = 200) -> dict[str, Any]:
        """Mở rộng 1–2 bước từ một thực thể: các fact còn hiệu lực chạm thực thể + thực thể đầu kia."""
        hops = max(1, min(int(hops), 2))
        seen_e: dict[str, dict[str, Any]] = {}
        seen_f: dict[str, dict[str, Any]] = {}
        frontier = [entity_id]
        for _ in range(hops):
            nxt: list[str] = []
            for eid in frontier:
                for f in self.facts(tiers, entity_id=eid, include_invalid=include_invalid, hidden=hidden, limit=max_facts):
                    if f["id"] in seen_f:
                        continue
                    seen_f[f["id"]] = f
                    for other in (f["src_entity_id"], f.get("dst_entity_id")):
                        if other and other != eid and other not in seen_e and other != entity_id:
                            nxt.append(other)
                    if len(seen_f) >= max_facts:
                        break
            for oid in nxt:
                e = self.visible(self.get_entity(oid), hidden)
                if e is not None:
                    seen_e[oid] = e
            frontier = nxt
        return {"entity": self.visible(self.get_entity(entity_id), hidden), "entities": list(seen_e.values()), "facts": list(seen_f.values())}

    def search(self, tiers: list[tuple[str, str | None]], query: str, *, limit: int = 10, hidden: Iterable[str] = (),
               include_invalid: bool = False) -> list[dict[str, Any]]:
        """Tìm fact bằng FTS5 (bm25) trên cột gấp dấu; không có FTS5/từ quá ngắn → LIKE. Xếp hạng lai (RRF, vector, độ tin cậy ×
        độ mới) thuộc PR retrieval — đây chỉ là kênh từ khoá."""
        words = [w for w in folded(query).split() if w]
        if not words:
            return []
        where, args = self._tier_where(tiers, "f")
        rows: list[sqlite3.Row] | None = None
        long_words = [w for w in words if len(w) >= 3]
        if has_fts(self.conn) and long_words:
            match = " OR ".join('"' + w.replace('"', '""') + '"' for w in long_words)
            try:
                rows = self.conn.execute(f"SELECT f.* FROM mem_facts_fts JOIN mem_facts f ON f.rowid=mem_facts_fts.rowid "
                                         f"WHERE mem_facts_fts MATCH ? AND {where} ORDER BY rank LIMIT ?", (match, *args, int(limit) * 4)).fetchall()
            except sqlite3.OperationalError:
                rows = None
        if rows is None:
            # LIKE chỉ khi không dùng được FTS5 (không có / câu hỏi toàn từ < 3 ký tự). Từ 1 ký tự ("I", "a") khớp MỌI fact → bỏ
            words = long_words or [w for w in words if len(w) >= 2] or words
            like = " OR ".join("f.folded LIKE ?" for _ in words)
            rows = self.conn.execute(f"SELECT f.* FROM mem_facts f WHERE ({like}) AND {where} ORDER BY f.ingested_at DESC LIMIT ?",
                                     (*[f"%{w}%" for w in words], *args, int(limit) * 4)).fetchall()
        out = []
        for r in rows:
            v = self.visible(dict(r), hidden)
            if v is not None and (include_invalid or not v.get("invalidated_at")):
                out.append(v)
            if len(out) >= limit:
                break
        return out

    # ================================================================== kế hoạch (thuần — không ghi)
    def plan_ops(self, scope: str, owner: str | None, ops: list[dict[str, Any]], *, episode: dict[str, Any] | None = None,
                 agent_id: str | None = None, run_id: str | None = None, created_by: str = "agent", now: str | None = None) -> dict[str, Any]:
        if scope not in ("agent", "team", "global"):
            raise KnowledgeError(f"scope không hợp lệ: {scope}", "invalid")
        ok = owner_key(scope, owner)
        if not isinstance(ops, list) or not ops:
            raise KnowledgeError("operations rỗng", "invalid")
        if len(ops) > MAX_OPS:
            raise KnowledgeError(f"tối đa {MAX_OPS} thao tác mỗi lô", "invalid")
        now = now or now_iso()
        rows: dict[str, dict[str, Any]] = {}  # "table:id" → hàng SAU
        batch_ent: dict[str, dict[str, Any]] = {}  # canon/alias → hàng thực thể (mới hoặc đã sửa trong lô)
        new_texts: list[str] = []
        dropped: list[dict[str, Any]] = []
        invalidated: list[str] = []
        lines: list[str] = []
        stats = {"facts_added": 0, "entities_added": 0, "invalidated": 0, "aliases_added": 0}
        longest = 0

        def put(table: str, row: dict[str, Any]) -> dict[str, Any]:
            rows[f"{table}:{row['id']}"] = row
            return row

        def cur_fact(fid: str) -> dict[str, Any] | None:
            return rows.get(f"mem_facts:{fid}") or self.get_fact(fid)

        def remember(e: dict[str, Any]) -> None:
            for n in [e["name"], *_aliases(e)]:
                if canon(n):
                    batch_ent[canon(n)] = e

        def bounded(s: str, n: int, what: str) -> str:
            if len(s) > n:
                raise KnowledgeError(f"{what} {len(s)} > {n} ký tự", "too_long")
            return s

        def protected(f: dict[str, Any], op: dict[str, Any]) -> bool:
            """Fact người khai chỉ bị đóng tự động bởi người, hoặc bởi trích xuất có bằng chứng là lượt người."""
            return f.get("created_by") == "user" and created_by != "user" and not op.get(USER_GROUNDED)

        def entity(name: Any, etype: Any = None, create: bool = True) -> dict[str, Any]:
            nm = bounded(_clean_text(name), MAX_NAME_CHARS, "tên thực thể")
            c = canon(nm)
            if not c:
                raise KnowledgeError("tên thực thể rỗng", "invalid")
            if c in batch_ent:
                return batch_ent[c]
            e = self.find_entity(scope, ok, nm)
            if e is not None:
                remember(e)
                return e
            if not create:
                raise KnowledgeError(f"không có thực thể '{nm}' trong graph {graph_key(scope, ok)}", "not_found")
            et = bounded(_clean_text(etype), MAX_TYPE_CHARS, "kiểu thực thể") or None
            e = put("mem_entities", {"id": _new_id("ge"), "scope": scope, "owner_id": ok, "name": nm, "canon": c, "type": et,
                                     "aliases": "[]", "alias_keys": "", "summary": None, "folded": folded(nm), "created_by": created_by, "agent_id": agent_id,
                                     "created_at": now, "updated_at": now, "invalidated_at": None, "invalidated_entry_id": None,
                                     "ledger_entry_id": ENTRY, "retracted_by": None})
            new_texts.append(nm)
            if et:
                new_texts.append(et)  # kiểu cũng vào ngữ cảnh (known_entities) — quét như tên
            stats["entities_added"] += 1
            lines.append(f"+ entity {nm}" + (f" ({e['type']})" if e["type"] else ""))
            remember(e)
            return e

        def add_alias(e: dict[str, Any], alias: Any) -> dict[str, Any]:
            al = bounded(_clean_text(alias), MAX_NAME_CHARS, "alias")
            c = canon(al)
            if not c or c == e["canon"] or c in {canon(x) for x in _aliases(e)}:
                return e
            other = batch_ent.get(c) or self.find_entity(scope, ok, al)
            if other is not None and other["id"] != e["id"]:
                raise KnowledgeError(f"alias '{al}' đã là tên/alias của thực thể khác ({other['name']}) — gộp thực thể là việc của consolidation", "alias_conflict")
            names = [*_aliases(e), al]
            e = put("mem_entities", dict(e, aliases=json.dumps(names, ensure_ascii=False), alias_keys=_alias_keys(names),
                                         folded=folded(" ".join([e["name"], *names])), updated_at=now))
            new_texts.append(al)
            stats["aliases_added"] += 1
            lines.append(f"+ alias {e['name']} = {al}")
            remember(e)
            return e

        def close(f: dict[str, Any], by: str | None, valid_to: str | None, why: str, *, keep_valid: bool = False) -> None:
            vt = f.get("valid_to") if keep_valid else (f.get("valid_to") or valid_to or now)
            put("mem_facts", dict(f, invalidated_at=now, invalidated_by=by, invalidated_entry_id=ENTRY, valid_to=vt))
            invalidated.append(f["id"])
            stats["invalidated"] += 1
            lines.append(f"- {why}: {f['fact']}")

        ep_id = None
        if episode and str(episode.get("text") or "").strip():
            text = unicodedata.normalize("NFC", redact_str(str(episode["text"])))  # giữ xuống dòng (nguồn thô), chỉ NFC + che secret
            ep_id = _new_id("gp")
            put("mem_episodes", {"id": ep_id, "scope": scope, "owner_id": ok, "agent_id": agent_id, "run_id": episode.get("run_id") or run_id,
                                 "session_id": episode.get("session_id"), "source_kind": str(episode.get("source_kind") or "session"),
                                 "source_ref": episode.get("source_ref"), "text": text, "folded": folded(text), "created_by": created_by,
                                 "created_at": now, "ledger_entry_id": ENTRY, "retracted_by": None})
            lines.append(f"+ episode {ep_id} ({len(text)} chars)")

        for n, op in enumerate(ops, 1):
            if not isinstance(op, dict):
                raise KnowledgeError(f"thao tác #{n} phải là object", "invalid")
            act = str(op.get("action") or "")
            try:
                if act == "add_entity":
                    e = entity(op.get("name"), op.get("type"))
                    for al in op.get("aliases") or []:
                        e = add_alias(e, al)
                    if op.get("summary") and not e.get("summary"):
                        s = bounded(_clean_text(op["summary"]), MAX_SUMMARY_CHARS, "tóm tắt thực thể")
                        e = put("mem_entities", dict(e, summary=s, updated_at=now))
                        new_texts.append(s)
                        remember(e)
                elif act == "add_alias":
                    add_alias(entity(op.get("entity"), create=False), op.get("alias"))
                elif act == "add_fact":
                    text = _clean_text(op.get("fact"))
                    rel = norm_relation(op.get("relation"))
                    if not text:
                        raise KnowledgeError("add_fact cần fact", "invalid")
                    if not rel:
                        raise KnowledgeError("add_fact cần relation", "invalid")
                    src = entity(op.get("src"), op.get("src_type"))
                    dst = entity(op.get("dst"), op.get("dst_type")) if op.get("dst") else None
                    live = [cur_fact(f["id"]) or f for f in self.live_facts(scope, ok, src["id"], rel)]
                    live = [f for f in live if not f.get("invalidated_at")]
                    live += [r for k, r in rows.items() if k.startswith("mem_facts:") and r["src_entity_id"] == src["id"] and r["relation"] == rel
                             and not r.get("invalidated_at") and r not in live]
                    same = [f for f in live if (f.get("dst_entity_id") or None) == (dst["id"] if dst else None) and canon(f["fact"]) == canon(text)]
                    if same:
                        dropped.append({"op": n, "reason": "duplicate", "fact": text, "existing": same[0]["id"]})
                        continue
                    vf = _clean_text(op.get("valid_from")) or None
                    fid = _new_id("gf")
                    if not op.get("multi"):
                        guarded = [f for f in live if protected(f, op)]
                        if guarded:  # mâu thuẫn fact người khai: fact người thắng, fact mới bị bỏ (người sửa qua CLI / lượt người)
                            dropped.append({"op": n, "reason": "conflicts_with_user_fact", "fact": text, "existing": guarded[0]["id"]})
                            continue
                        for f in live:
                            close(f, fid, vf, "superseded")
                    try:
                        conf = float(op["confidence"]) if op.get("confidence") is not None else (0.7 if created_by == "user" else 0.5)
                    except (TypeError, ValueError):
                        raise KnowledgeError("confidence phải là số 0..1", "invalid") from None
                    put("mem_facts", {"id": fid, "scope": scope, "owner_id": ok, "src_entity_id": src["id"], "relation": rel,
                                      "dst_entity_id": dst["id"] if dst else None, "fact": text, "folded": folded(text), "valid_from": vf,
                                      "valid_to": _clean_text(op.get("valid_to")) or None, "ingested_at": now, "invalidated_at": None,
                                      "invalidated_by": None, "invalidated_entry_id": None, "source_episode_id": ep_id, "created_by": created_by,
                                      "agent_id": agent_id, "confidence": max(0.0, min(1.0, conf)), "hits": 0, "last_used": None,
                                      "ledger_entry_id": ENTRY, "retracted_by": None})
                    new_texts.append(text)
                    longest = max(longest, len(text))
                    stats["facts_added"] += 1
                    lines.append(f"+ fact [{src['name']} -{rel}-> {dst['name'] if dst else '·'}] {text}")
                elif act == "invalidate_fact":
                    if op.get("fact_id"):
                        f = cur_fact(str(op["fact_id"]))
                        if f is None or f["scope"] != scope or f["owner_id"] != ok or f.get("retracted_by"):
                            raise KnowledgeError(f"không có fact '{op['fact_id']}' trong graph {graph_key(scope, ok)}", "not_found")
                        targets = [] if f.get("invalidated_at") else [f]
                        if targets and protected(f, op):
                            raise KnowledgeError(f"fact '{f['id']}' do người khai — chỉ người (hoặc lượt người) đóng được", "protected_user_fact")
                    else:
                        src = entity(op.get("src"), create=False)
                        rel = norm_relation(op.get("relation"))
                        dst_c = canon(op["dst"]) if op.get("dst") else None
                        targets = [cur_fact(f["id"]) or f for f in self.live_facts(scope, ok, src["id"], rel or None)]
                        targets = [f for f in targets if not f.get("invalidated_at")]
                        if dst_c:
                            d = entity(op["dst"], create=False)
                            targets = [f for f in targets if f.get("dst_entity_id") == d["id"]]
                        if not targets:
                            raise KnowledgeError("không có fact còn hiệu lực khớp để đóng", "not_found")
                        if all(protected(f, op) for f in targets):
                            raise KnowledgeError("fact khớp do người khai — chỉ người (hoặc lượt người) đóng được", "protected_user_fact")
                        targets = [f for f in targets if not protected(f, op)]
                    for f in targets:
                        close(f, None, _clean_text(op.get("valid_to")) or None, "invalidated")
                elif act == "invalidate_entity":
                    e = entity(op.get("entity"), create=False)
                    put("mem_entities", dict(e, invalidated_at=now, invalidated_entry_id=ENTRY, updated_at=now))
                    stats["invalidated"] += 1
                    lines.append(f"- entity {e['name']}")
                elif act == "merge_entity":
                    e = entity(op.get("entity"), create=False)
                    into = entity(op.get("into"), create=False)
                    if e["id"] == into["id"]:
                        raise KnowledgeError("merge_entity: entity và into là cùng một thực thể", "invalid")
                    names = [*_aliases(into)]
                    keys = {into["canon"], *(canon(x) for x in names)}
                    for n in [e["name"], *_aliases(e)]:
                        if canon(n) and canon(n) not in keys:
                            names.append(n)
                            keys.add(canon(n))
                    into = put("mem_entities", dict(into, aliases=json.dumps(names, ensure_ascii=False), alias_keys=_alias_keys(names),
                                                    folded=folded(" ".join([into["name"], *names])), type=into.get("type") or e.get("type"),
                                                    summary=into.get("summary") or e.get("summary"), updated_at=now))
                    put("mem_entities", dict(e, invalidated_at=now, invalidated_entry_id=ENTRY, updated_at=now))
                    for n in [e["name"], *_aliases(e)]:
                        if canon(n):
                            batch_ent[canon(n)] = into
                    remember(into)
                    touching = [cur_fact(r[0]) for r in self.conn.execute(
                        "SELECT id FROM mem_facts WHERE scope=? AND owner_id=? AND (src_entity_id=? OR dst_entity_id=?) AND retracted_by IS NULL "
                        "AND invalidated_at IS NULL ORDER BY rowid", (scope, ok, e["id"], e["id"]))]
                    touching += [r for k, r in rows.items() if k.startswith("mem_facts:") and e["id"] in (r["src_entity_id"], r.get("dst_entity_id"))
                                 and r not in touching]
                    for f in [x for x in touching if x is not None and not x.get("invalidated_at")]:
                        src_id = into["id"] if f["src_entity_id"] == e["id"] else f["src_entity_id"]
                        dst_id = into["id"] if f.get("dst_entity_id") == e["id"] else f.get("dst_entity_id")
                        twins = [cur_fact(g["id"]) or g for g in self.live_facts(scope, ok, src_id, f["relation"])]
                        twins += [r for k, r in rows.items() if k.startswith("mem_facts:") and r["src_entity_id"] == src_id and r["relation"] == f["relation"]]
                        twin = [g for g in twins if not g.get("invalidated_at") and (g.get("dst_entity_id") or None) == (dst_id or None)
                                and canon(g["fact"]) == canon(f["fact"]) and g["id"] != f["id"]]
                        if twin:
                            close(f, twin[0]["id"], None, "merged duplicate", keep_valid=True)
                            continue
                        nid = _new_id("gf")
                        put("mem_facts", dict(f, id=nid, src_entity_id=src_id, dst_entity_id=dst_id, invalidated_at=None, invalidated_by=None,
                                              invalidated_entry_id=None, hits=0, last_used=None, ledger_entry_id=ENTRY, retracted_by=None))
                        close(f, nid, None, "moved to merged entity", keep_valid=True)
                    stats["entities_merged"] = stats.get("entities_merged", 0) + 1
                    lines.append(f"~ merge entity {e['name']} -> {into['name']}")
                elif act == "supersede_fact":
                    f = cur_fact(str(op.get("fact_id") or ""))
                    by = cur_fact(str(op.get("by") or ""))
                    for x, nm in ((f, "fact_id"), (by, "by")):
                        if x is None or x["scope"] != scope or x["owner_id"] != ok or x.get("retracted_by") or x.get("invalidated_at"):
                            raise KnowledgeError(f"supersede_fact: '{op.get(nm)}' không phải fact còn hiệu lực trong graph {graph_key(scope, ok)}", "not_found")
                    assert f is not None and by is not None
                    if f["id"] == by["id"]:
                        raise KnowledgeError("supersede_fact: fact_id và by trùng nhau", "invalid")
                    close(f, by["id"], None, "merged duplicate", keep_valid=True)
                else:
                    raise KnowledgeError(f"action graph không hợp lệ: {act!r} (được: {', '.join(ACTIONS)})", "invalid")
            except KnowledgeError as e:
                raise KnowledgeError(f"thao tác #{n} ({act}): {e.message}", e.reason) from None
        total = sum(len(t) + 1 for t in new_texts)
        if total > MAX_BATCH_TEXT:
            raise KnowledgeError(f"lô graph {total} > {MAX_BATCH_TEXT} ký tự văn bản mới — chia nhỏ lô", "batch_too_large")
        changed = [k for k in rows if not k.startswith("mem_episodes:")]
        return {"scope": scope, "owner": ok, "key": graph_key(scope, ok), "rows": [{"table": k.split(":", 1)[0], "id": r["id"], "row": r} for k, r in rows.items()],
                "changed": len(changed), "new_texts": new_texts, "dropped": dropped, "invalidated": invalidated, "longest_fact": longest,
                "stats": stats, "lines": lines, "episode_id": ep_id}

    # ================================================================== trạng thái + ghi (chỉ Ledger gọi)
    def rows_state(self, keys: Iterable[str]) -> dict[str, Any]:
        """{"table:id": hàng (bỏ cột VOLATILE) | None} — trạng thái ledger (blob before/after) của các hàng một entry chạm."""
        out: dict[str, Any] = {}
        for k in sorted(set(keys)):
            table, rid = k.split(":", 1)
            if table not in TABLES:
                raise KnowledgeError(f"bảng graph không hợp lệ: {table}", "invalid")
            out[k] = strip(self._one(table, rid))
        return out

    @staticmethod
    def dump_state(state: dict[str, Any]) -> bytes:
        return json.dumps(state, sort_keys=True, ensure_ascii=False, indent=1, default=str).encode("utf-8")

    @staticmethod
    def load_state(data: bytes | None) -> dict[str, Any]:
        return json.loads((data or b"{}").decode("utf-8")) if data else {}

    def write_rows(self, rows: list[dict[str, Any]], entry_id: str) -> None:
        """Upsert từng hàng (thay `$ENTRY` bằng `entry_id`). Cập nhật không đụng cột VOLATILE (đếm dùng/độ tin cậy)."""
        for m in rows:
            table = m["table"]
            if table not in TABLES:
                raise KnowledgeError(f"bảng graph không hợp lệ: {table}", "invalid")
            row = {k: (entry_id if v == ENTRY else v) for k, v in m["row"].items() if k != "rowid"}
            cols = list(row)
            upd = [c for c in cols if c not in VOLATILE and c != "id"]
            sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)}) ON CONFLICT(id) DO UPDATE SET "
                   + ", ".join(f"{c}=excluded.{c}" for c in upd))
            self.conn.execute(sql, tuple(row[c] for c in cols))

    def invert_rows(self, before: dict[str, Any], after: dict[str, Any], current: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        """Đảo ngược entry theo HÀNG trên trạng thái hiện tại → (hàng cần ghi, khoá bỏ qua). Hàng hiện tại ≠ hàng entry để lại (đã bị
        thay đổi sau) → bỏ qua. Hàng entry tạo → gỡ (`retracted_by=$ENTRY`); thực thể còn được fact sống ngoài lô tham chiếu → bỏ qua."""
        out: list[dict[str, Any]] = []
        skipped: list[str] = []
        retract_facts: set[str] = set()
        for k in sorted(after):
            if current.get(k) != after[k]:
                skipped.append(k)
                continue
            table, rid = k.split(":", 1)
            b = before.get(k)
            if b is None:
                if after[k] is None:
                    continue
                if table == "mem_facts":
                    retract_facts.add(rid)
                out.append({"table": table, "id": rid, "row": dict(after[k], retracted_by=ENTRY)})
            else:
                out.append({"table": table, "id": rid, "row": dict(b)})
        final: list[dict[str, Any]] = []
        for m in out:
            if m["table"] == "mem_entities" and m["row"].get("retracted_by") == ENTRY:
                refs = [r[0] for r in self.conn.execute("SELECT id FROM mem_facts WHERE (src_entity_id=? OR dst_entity_id=?) AND retracted_by IS NULL",
                                                        (m["id"], m["id"]))]
                if any(r not in retract_facts for r in refs):
                    skipped.append(f"mem_entities:{m['id']}")
                    continue
            final.append(m)
        return final, skipped

    # ================================================================== bản chụp cho eval (bản sao home)
    def snapshot(self, tiers: list[tuple[str, str | None]], hidden: Iterable[str] = ()) -> dict[str, Any]:
        """Graph agent nhìn thấy (tầng agent + team + global, theo ghim canary), tất định — ghi vào bản sao home của eval và băm vào
        fingerprint. Không chứa văn bản episode (chỉ fact/thực thể đi vào ngữ cảnh) và cột đếm dùng."""
        where, args = self._tier_where(tiers)
        snap: dict[str, Any] = {"version": 1, "tiers": [[s, owner_key(s, o)] for s, o in tiers], "entities": {}, "facts": {}}
        for table, sec in SECTION.items():
            for r in self.conn.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY id", args):
                v = self.visible(dict(r), hidden)
                if v is not None:
                    snap[sec][v["id"]] = strip(v, SNAPSHOT_VOLATILE)
        return snap

    @staticmethod
    def dump_snapshot(snap: dict[str, Any]) -> bytes:
        return json.dumps(snap, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")

    @staticmethod
    def apply_to_snapshot(snap: dict[str, Any], rows: list[dict[str, Any]], entry_id: str = "candidate") -> dict[str, Any]:
        for m in rows:
            sec = SECTION.get(m["table"])
            if sec is None:
                continue
            row = {k: (entry_id if v == ENTRY else v) for k, v in m["row"].items()}
            if row.get("retracted_by"):
                snap[sec].pop(m["id"], None)
            else:
                snap[sec][m["id"]] = strip(row, SNAPSHOT_VOLATILE)
        return snap

    @staticmethod
    def revert_on_snapshot(snap: dict[str, Any], before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        for k in after:
            table, rid = k.split(":", 1)
            sec = SECTION.get(table)
            if sec is None:
                continue
            b = before.get(k)
            if b is None or b.get("retracted_by"):
                snap[sec].pop(rid, None)
            else:
                snap[sec][rid] = strip(b, SNAPSHOT_VOLATILE)
        return snap

    # ================================================================== vòng đời agent (purge / clone / export / import)
    def purge_owner(self, scope: str, owner: str | None) -> dict[str, int]:
        """Xoá CỨNG mọi hàng của một tầng (purge agent = quên hẳn). FTS đồng bộ bằng trigger; vector + lượt dùng theo id."""
        ok = owner_key(scope, owner)
        out = {}
        has_uses = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mem_fact_uses'").fetchone() is not None
        for table, kind in (("mem_facts", "fact"), ("mem_entities", "entity"), ("mem_episodes", "episode")):
            ids = [r[0] for r in self.conn.execute(f"SELECT id FROM {table} WHERE scope=? AND owner_id=?", (scope, ok))]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                self.conn.execute(f"DELETE FROM mem_vectors WHERE item_kind=? AND item_id IN ({','.join('?' * len(chunk))})", (kind, *chunk))
                if kind == "fact" and has_uses:
                    self.conn.execute(f"DELETE FROM mem_fact_uses WHERE fact_id IN ({','.join('?' * len(chunk))})", chunk)
            out[table] = self.conn.execute(f"DELETE FROM {table} WHERE scope=? AND owner_id=?", (scope, ok)).rowcount
        return out

    def export_owner(self, scope: str, owner: str | None) -> dict[str, list[dict[str, Any]]]:
        """Hàng sống (chưa gỡ, kể cả fact đã đóng hiệu lực — lịch sử) của một tầng, để clone/export."""
        ok = owner_key(scope, owner)
        return {t: [strip(dict(r), frozenset()) for r in self.conn.execute(f"SELECT * FROM {t} WHERE scope=? AND owner_id=? AND retracted_by IS NULL "
                                                                               "ORDER BY rowid", (scope, ok))] for t in TABLES}  # type: ignore[misc]

    def import_rows(self, data: dict[str, list[dict[str, Any]]], scope: str, owner: str | None, *, agent_id: str | None = None,
                    scrub: Any = None) -> dict[str, int]:
        """Chép hàng (từ `export_owner`) sang tầng mới với id MỚI (tham chiếu nội bộ ánh xạ lại). Không qua ledger — như chép MEMORY.md
        khi clone/import agent: agent đích mới tạo, chưa có lịch sử để rollback."""
        ok = owner_key(scope, owner)
        idmap: dict[str, str] = {}
        prefix = {"mem_episodes": "gp", "mem_entities": "ge", "mem_facts": "gf"}
        for t in TABLES:
            for r in data.get(t) or []:
                idmap[str(r["id"])] = _new_id(prefix[t])
        counts = {}
        allowed = {t: {c[1] for c in self.conn.execute(f"PRAGMA table_info({t})")} for t in TABLES}
        for t in TABLES:
            n = 0
            for r in data.get(t) or []:
                row = {k: v for k, v in dict(r).items() if k in allowed[t]}
                row["id"] = idmap[str(r["id"])]
                row["scope"], row["owner_id"] = scope, ok
                for ref in ("src_entity_id", "dst_entity_id", "source_episode_id", "invalidated_by"):
                    if row.get(ref):
                        row[ref] = idmap.get(str(row[ref]))
                for col in ("ledger_entry_id", "invalidated_entry_id", "retracted_by"):
                    if col in allowed[t]:
                        row[col] = None
                if agent_id is not None and "agent_id" in allowed[t]:
                    row["agent_id"] = agent_id
                if scrub is not None:
                    for col in ("text", "fact", "name", "summary"):
                        if isinstance(row.get(col), str):
                            row[col] = scrub(row[col])
                    names = [scrub(a) for a in _aliases(row)] if t == "mem_entities" else []
                    if t == "mem_entities":
                        row.update(aliases=json.dumps(names, ensure_ascii=False), alias_keys=_alias_keys(names), canon=canon(row["name"]))
                # cột tìm kiếm/khoá dựng lại từ văn bản (gói import có thể bị sửa tay/che secret)
                if t == "mem_facts":
                    row["folded"] = folded(row.get("fact"))
                elif t == "mem_entities":
                    row["folded"] = folded(" ".join([str(row.get("name") or ""), *_aliases(row)]))
                    row["canon"] = canon(row.get("name"))
                else:
                    row["folded"] = folded(row.get("text"))
                if t == "mem_facts" and not row.get("src_entity_id"):
                    continue
                cols = list(row)
                self.conn.execute(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", tuple(row[c] for c in cols))
                n += 1
            counts[t] = n
        return counts

    # ================================================================== vector (tuỳ chọn — PR retrieval dùng)
    def put_vector(self, kind: str, item_id: str, model: str, model_version: str, vec: list[float]) -> None:
        import struct

        blob = struct.pack(f"<{len(vec)}f", *vec)
        self.conn.execute("INSERT INTO mem_vectors (item_kind, item_id, model, model_version, dim, vec, created_at) VALUES (?,?,?,?,?,?,?) "
                          "ON CONFLICT(item_kind, item_id, model) DO UPDATE SET model_version=excluded.model_version, dim=excluded.dim, "
                          "vec=excluded.vec, created_at=excluded.created_at", (kind, item_id, model, model_version, len(vec), blob, now_iso()))

    def get_vector(self, kind: str, item_id: str, model: str) -> tuple[str, list[float]] | None:
        import struct

        r = self.conn.execute("SELECT model_version, dim, vec FROM mem_vectors WHERE item_kind=? AND item_id=? AND model=?", (kind, item_id, model)).fetchone()
        return None if r is None else (r[0], list(struct.unpack(f"<{r[1]}f", r[2])))

    def needs_embedding(self, model: str, model_version: str, limit: int = 500) -> list[str]:
        """Fact sống chưa có vector của (model, phiên bản) này — đổi model/phiên bản → chỉ mục lại."""
        return [r[0] for r in self.conn.execute(
            "SELECT f.id FROM mem_facts f LEFT JOIN mem_vectors v ON v.item_kind='fact' AND v.item_id=f.id AND v.model=? AND v.model_version=? "
            "WHERE f.retracted_by IS NULL AND v.item_id IS NULL ORDER BY f.rowid LIMIT ?", (model, model_version, int(limit)))]
