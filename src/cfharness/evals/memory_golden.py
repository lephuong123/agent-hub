"""Golden task loại MEMORY (graph memory PR quality — docs/dev-notes/graph-memory.md § PR quality).

Golden task bình thường chạy agent rồi chấm output. Golden memory chấm TRUY HỒI, tất định, không gọi model và không chạy run:
câu hỏi (`prompt` của golden task, hoặc `checker.query`) → `Retriever.search` trên BẢN CHỤP graph của bản sao home
(`.graph_snapshot.json` — đúng graph mà eval gate băm vào fingerprint, theo ghim canary + ứng viên) → so với đáp án vàng.

`checker` (`validate_memory_checker`):
    {"kind": "memory_recall",
     "k": 5,                          # recall@k
     "gold_fact_ids": ["gf_…"],        # fact phải có trong top-k (id trong graph)
     "gold_contains": ["dark mode"],   # hoặc: chuỗi (gấp dấu, không phân biệt hoa thường) phải có trong một fact top-k
     "gold_answer": "…",               # câu trả lời vàng (ghi lại; `answer_hit` = có fact top-k chứa nó)
     "as_of": "2026-06-01",            # ngày thế giới của câu hỏi: quá khứ → tra cả lịch sử, chỉ tính fact CÒN HIỆU LỰC ngày đó
     "stale_fact_ids": [], "stale_contains": [],   # fact đã cũ (biết trước) — xuất hiện trong top-k = stale
     "forbidden_contains": [], "forbidden_fact_ids": [],   # thăm dò đầu độc: KHÔNG được có trong top-k lẫn trong graph
     "pass_threshold": 0.5}
Chỉ số: `recall_at_k` = phần đáp án vàng có trong top-k; `stale_rate` = phần top-k là fact đã hết hiệu lực tại `as_of` (mặc định hôm
nay) hoặc thuộc danh sách stale; điểm = recall × (1 − stale_rate); bị đầu độc → 0. Không có đáp án vàng (câu hỏi kiểm tra kiêng /
thăm dò) → recall = 1.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import HubError
from .checkers import Grade

KIND = "memory_recall"
_LISTS = ("gold_fact_ids", "gold_contains", "stale_fact_ids", "stale_contains", "forbidden_contains", "forbidden_fact_ids")


def _date(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def validate_memory_checker(spec: dict[str, Any]) -> dict[str, Any]:
    s = dict(spec)
    for k in _LISTS:
        v = s.get(k)
        if v is None:
            s[k] = []
        elif not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
            raise HubError(f"checker memory_recall: '{k}' phải là danh sách chuỗi")
    if not (s["gold_fact_ids"] or s["gold_contains"] or s.get("gold_answer") or s["forbidden_contains"] or s["forbidden_fact_ids"]):
        raise HubError("checker memory_recall cần đáp án vàng (gold_fact_ids / gold_contains / gold_answer) hoặc thăm dò (forbidden_*)")
    try:
        s["k"] = int(s.get("k") or 5)
    except (TypeError, ValueError):
        raise HubError("checker memory_recall: k phải là số nguyên") from None
    if not 1 <= s["k"] <= 20:
        raise HubError("checker memory_recall: k trong [1, 20]")
    if s.get("as_of") and _date(s["as_of"]) is None:
        raise HubError("checker memory_recall: as_of phải là ngày ISO (YYYY-MM-DD)")
    thr = s.get("pass_threshold", 0.5)
    if not isinstance(thr, (int, float)) or not 0 <= float(thr) <= 1:
        raise HubError("checker memory_recall: pass_threshold phải trong [0, 1]")
    s["pass_threshold"] = float(thr)
    return s


def is_memory_task(t: dict[str, Any]) -> bool:
    return isinstance(t.get("checker"), dict) and t["checker"].get("kind") == KIND


def _valid_at(f: dict[str, Any], ref: datetime) -> bool:
    vf, vt = _date(f.get("valid_from")), _date(f.get("valid_to"))
    if vf is not None and vf > ref:
        return False
    if vt is not None and vt <= ref:
        return False
    if f.get("invalidated_at") and not f.get("valid_to"):  # đóng do gộp trùng/không rõ thời điểm → không tính là hiệu lực
        return False
    return True


def grade_memory(spec: dict[str, Any], query: str, home: Path | None, *, now: datetime | None = None) -> Grade:
    from ..knowledge.graph import SNAPSHOT_FILE
    from ..knowledge.graph.retrieval import Retriever, load_snapshot, snapshot_conn, snapshot_tiers
    from ..knowledge.graph.text import canon

    s = validate_memory_checker(spec)
    q = str(s.get("query") or query or "").strip()
    snap = load_snapshot(Path(home) / SNAPSHOT_FILE) if home else None
    if not snap:
        snap = {"tiers": [], "entities": {}, "facts": {}}
    now = now or datetime.now(timezone.utc)
    as_of = _date(s.get("as_of"))
    past = as_of is not None and as_of.date() < now.date()
    ref = as_of or now
    k = s["k"]
    conn = snapshot_conn(snap)
    try:
        r = Retriever(conn)
        res = r.search(snapshot_tiers(snap), q, limit=50 if past else k, include_history=past, now=now) if q else []
    finally:
        conn.close()
    if past:  # câu hỏi theo thời gian: chỉ fact còn hiệu lực tại ngày đó
        res = [f for f in res if _valid_at(f, ref)]
    top = res[:k]
    texts = {f["id"]: canon(f.get("fact")) for f in top}
    gold_ids = list(dict.fromkeys(s["gold_fact_ids"]))
    gold_txt = [canon(x) for x in s["gold_contains"]]
    hits = sum(1 for g in gold_ids if g in texts) + sum(1 for g in gold_txt if any(g in t for t in texts.values()))
    n_gold = len(gold_ids) + len(gold_txt)
    answer = canon(s.get("gold_answer") or "")
    answer_hit = bool(answer) and any(answer in t for t in texts.values())
    if n_gold:
        recall = hits / n_gold
    elif answer:
        recall = 1.0 if answer_hit else 0.0
    else:
        recall = 1.0
    stale_txt = [canon(x) for x in s["stale_contains"]]
    stale = [f["id"] for f in top if f["id"] in s["stale_fact_ids"] or any(x in texts[f["id"]] for x in stale_txt) or not _valid_at(f, ref)]
    stale_rate = len(stale) / len(top) if top else 0.0
    forb_txt = [canon(x) for x in s["forbidden_contains"]]
    all_facts = {fid: canon(f.get("fact")) for fid, f in (snap.get("facts") or {}).items() if not f.get("invalidated_at")}
    poisoned_top = [f["id"] for f in top if f["id"] in s["forbidden_fact_ids"] or any(x in texts[f["id"]] for x in forb_txt)]
    poisoned_graph = [fid for fid, t in all_facts.items() if fid in s["forbidden_fact_ids"] or any(x in t for x in forb_txt)]
    poisoned = bool(poisoned_top or poisoned_graph)
    score = 0.0 if poisoned else recall * (1.0 - stale_rate)
    detail = {"kind": KIND, "query": q[:200], "k": k, "recall_at_k": round(recall, 4), "stale_rate": round(stale_rate, 4), "answer_hit": answer_hit,
              "retrieved": [f["id"] for f in top], "stale": stale, "as_of": s.get("as_of"), "history_mode": past,
              "poisoned": poisoned, "poisoned_in_top": poisoned_top, "poisoned_in_graph": poisoned_graph[:10]}
    return Grade(round(score, 4), (not poisoned) and score >= s["pass_threshold"] - 1e-9, detail)


def summarize(details: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Gộp chỉ số các mẫu memory: recall@k trung bình, stale-fact-rate trung bình, số thăm dò bị đầu độc."""
    ds = [d for d in details if isinstance(d, dict) and "recall_at_k" in d]
    if not ds:
        return None
    return {"samples": len(ds), "recall_at_k": round(sum(d["recall_at_k"] for d in ds) / len(ds), 4),
            "stale_rate": round(sum(d["stale_rate"] for d in ds) / len(ds), 4), "poisoned": sum(1 for d in ds if d.get("poisoned"))}


# ============================================================================ bộ mẫu (fixture) — EN / VI / trộn + thăm dò đầu độc
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "graph_memory_golden.json"


def load_fixture(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or FIXTURE).read_text(encoding="utf-8"))


def seed_fixture(hub: Any, agent_id: str, *, path: Path | None = None, created_by: str = "user") -> dict[str, Any]:
    """Ghi graph mẫu của fixture vào tầng agent (qua pipeline như người — `graph_write(created_by="user")`) rồi tạo golden memory task
    (chỉ người tạo golden — Q-07). `key` của fact trong fixture → id thật sau khi ghi. Dùng cho test + thử nghiệm thủ công."""
    fx = load_fixture(path)
    svc = hub.learning
    ids: dict[str, str] = {}
    for step in fx["writes"]:
        res = svc.graph_write(agent_id, step["ops"], scope="agent", created_by=created_by, rationale=step.get("note") or "golden fixture")
        if res.get("status") != "applied":
            raise HubError(f"fixture: ghi graph không áp dụng được: {res.get('status')} {res.get('block_reason') or res.get('reason')}")
        for op in step["ops"]:
            if op.get("key"):
                row = hub.store.conn.execute(
                    "SELECT id FROM mem_facts WHERE scope='agent' AND owner_id=? AND fact=? AND retracted_by IS NULL ORDER BY rowid DESC LIMIT 1",
                    (agent_id, _norm(op["fact"]))).fetchone()
                if row:
                    ids[op["key"]] = row[0]
    tasks = []
    for t in fx["tasks"]:
        chk = dict(t["checker"])
        for src, dst in (("gold_keys", "gold_fact_ids"), ("stale_keys", "stale_fact_ids")):
            if chk.get(src):
                chk[dst] = [ids[x] for x in chk.pop(src)]
        g = hub.evals.golden.add({"agent_id": agent_id, "title": t["title"], "prompt": t["query"], "checker": chk, "tags": ["memory", *t.get("tags", [])]},
                                 created_by=created_by)
        tasks.append(g)
    return {"fact_ids": ids, "tasks": tasks}


def _norm(text: str) -> str:
    from ..knowledge.graph.text import nfc
    from ..redact import redact_str

    return nfc(redact_str(text))
