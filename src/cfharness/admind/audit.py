"""Nhật ký quản trị bất biến (FR-SA-AUD): chỉ nối thêm, mỗi dòng chứa băm dòng trước.

`hash = sha256(prev_hash ‖ canonical_json(CHAIN_FIELDS))`. Dòng đầu: `prev_hash = "genesis"`. `verify()` duyệt theo `seq`, báo chỗ gãy
đầu tiên (`seq_gap`, `prev_mismatch`, `hash_mismatch`). Tham số đã che secret trước khi ghi (`scrub`). Không có thao tác xoá (AUD-04).
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from .db import SacDB

GENESIS = "genesis"
CHAIN_FIELDS = ("seq", "ts", "host_id", "actor_id", "actor_email", "role", "ip", "op", "target", "params", "outcome", "error_code",
                "job_id", "approver_id")
SECRET_KEYS = ("password", "token", "secret", "code", "totp", "api_key", "key", "otp")
OUTCOMES = ("ok", "denied", "error", "pending")


def scrub(v: Any, depth: int = 0) -> Any:
    """Che secret theo tên khoá (BR-SA-03) + cắt chuỗi dài. Giữ nguyên kiểu để lọc được."""
    if depth > 4:
        return "…"
    if isinstance(v, dict):
        out = {}
        for k, x in list(v.items())[:50]:
            ks = str(k)
            out[ks] = "***" if any(s in ks.lower() for s in SECRET_KEYS) and x not in (None, "", False) else scrub(x, depth + 1)
        return out
    if isinstance(v, (list, tuple)):
        return [scrub(x, depth + 1) for x in list(v)[:50]]
    if isinstance(v, str):
        return v[:300]
    return v


def _canon(row: dict[str, Any]) -> str:
    return json.dumps({k: row.get(k) for k in CHAIN_FIELDS}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def row_hash(prev: str, row: dict[str, Any]) -> str:
    return hashlib.sha256((prev + "\x1e" + _canon(row)).encode("utf-8")).hexdigest()


class AdminAudit:
    def __init__(self, db: SacDB) -> None:
        self.db = db

    def record(self, *, op: str, outcome: str, actor: dict[str, Any] | None = None, ip: str | None = None, target: str | None = None,
               params: Any = None, error_code: str | None = None, job_id: str | None = None, approver_id: str | None = None,
               ts: float | None = None) -> int:
        if outcome not in OUTCOMES:
            outcome = "error"
        a = actor or {}
        with self.db.tx() as c:
            last = c.execute("SELECT seq, hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
            seq = (int(last["seq"]) + 1) if last else 1
            prev = str(last["hash"]) if last else GENESIS
            row = {"seq": seq, "ts": round(float(ts if ts is not None else time.time()), 3), "host_id": self.db.host_id,
                   "actor_id": a.get("id"), "actor_email": a.get("email"), "role": a.get("role"), "ip": (ip or None) and str(ip)[:64],
                   "op": op[:64], "target": (target or None) and str(target)[:200],
                   "params": json.dumps(scrub(params), ensure_ascii=False, sort_keys=True, default=str) if params is not None else None,
                   "outcome": outcome, "error_code": error_code, "job_id": job_id, "approver_id": approver_id}
            h = row_hash(prev, row)
            c.execute("INSERT INTO audit(seq, ts, host_id, actor_id, actor_email, role, ip, op, target, params, outcome, error_code, job_id,"
                      " approver_id, prev_hash, hash) VALUES (:seq,:ts,:host_id,:actor_id,:actor_email,:role,:ip,:op,:target,:params,"
                      ":outcome,:error_code,:job_id,:approver_id,:prev_hash,:hash)", {**row, "prev_hash": prev, "hash": h})
        return seq

    def verify(self) -> dict[str, Any]:
        prev, expect, n = GENESIS, 1, 0
        for r in self.db.all("SELECT * FROM audit ORDER BY seq"):
            n += 1
            if int(r["seq"]) != expect:
                return {"ok": False, "reason": "seq_gap", "seq": expect, "rows": n}
            if r["prev_hash"] != prev:
                return {"ok": False, "reason": "prev_mismatch", "seq": r["seq"], "rows": n}
            if row_hash(prev, r) != r["hash"]:
                return {"ok": False, "reason": "hash_mismatch", "seq": r["seq"], "rows": n}
            prev, expect = r["hash"], expect + 1
        return {"ok": True, "rows": n}

    def search(self, *, actor_id: str | None = None, target: str | None = None, op_prefix: str | None = None, since: float | None = None,
               until: float | None = None, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        q, p = ["1=1"], []
        if actor_id:
            q.append("actor_id=?"), p.append(actor_id)
        if target:
            q.append("target=?"), p.append(target)
        if op_prefix:
            q.append("op LIKE ?"), p.append(op_prefix.replace("%", "") + "%")
        if since is not None:
            q.append("ts>=?"), p.append(float(since))
        if until is not None:
            q.append("ts<?"), p.append(float(until))
        rows = self.db.all(f"SELECT seq, ts, actor_id, actor_email, role, ip, op, target, params, outcome, error_code, job_id, approver_id "
                           f"FROM audit WHERE {' AND '.join(q)} ORDER BY seq DESC LIMIT ? OFFSET ?",
                           (*p, max(1, min(int(limit), 2000)), max(0, int(offset))))
        for r in rows:
            try:
                r["params"] = json.loads(r["params"]) if r["params"] else None
            except ValueError:
                r["params"] = None
        return rows
