"""Cổng nối sang lõi học của M4-A (hợp đồng docs/plan/M4.md) — M4-B không sở hữu ledger/knowledge/proposal.

- Ledger: `hub.learning.ledger.list(...)` và `hub.learning.ledger.rollback(entry_id, actor, reason)`.
- Knowledge (curator pha 1): `hub.knowledge` — liệt kê skill tầng agent, đặt state, archive qua pipeline (ledger actor=curator).
- Signal: ghi Signal `failure` khi eval tụt (BRD FR-LN-10) — dùng API của A nếu có, không thì ghi thẳng bảng `signals` (v3).

Mọi hàm dò thuộc tính lúc gọi (A có thể gắn vào hub sau B) và chịu được việc A chưa có: trả rỗng / None.
Bản ghi ledger được chuẩn hoá (`normalize_entry`) vì tên trường của A có thể khác BRD §8.1 một chút.
"""

from __future__ import annotations

import inspect
import json
import uuid
from typing import TYPE_CHECKING, Any

from ..events import make_event
from ..store import now_iso

if TYPE_CHECKING:
    from ..hub import Hub


async def call(fn: Any, *a: Any, **kw: Any) -> Any:
    r = fn(*a, **kw)
    if inspect.isawaitable(r):
        r = await r
    return r


# Chỉ thay đổi do vòng học/agent tạo mới được TỰ rollback (regression_monitor/eval_gate). Thay đổi của NGƯỜI
# (actor user, hoặc actor khác ngoài danh sách) → chỉ cảnh báo qua digest/Slack, không bao giờ tự rollback.
AUTO_ROLLBACK_ACTORS = frozenset({"learning", "review", "agent", "curator"})


def is_auto_change(entry: dict[str, Any]) -> bool:
    return str(entry.get("actor") or "") in AUTO_ROLLBACK_ACTORS


def ledger(hub: "Hub") -> Any | None:
    lr = getattr(getattr(hub, "learning", None), "ledger", None)
    if lr is None:
        lr = getattr(hub, "ledger", None)
    return lr if lr is not None and hasattr(lr, "rollback") else None


def knowledge(hub: "Hub") -> Any | None:
    return getattr(hub, "knowledge", None)


def normalize_entry(e: Any) -> dict[str, Any]:
    d = e.model_dump() if hasattr(e, "model_dump") else dict(e)
    for k in ("flags",):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                d[k] = [d[k]]
    d.setdefault("flags", [])
    if not d.get("agent_id"):
        if d.get("scope", "agent") == "agent":
            d["agent_id"] = d.get("owner_id") or d.get("agent")
    d["applied_at"] = d.get("applied_at") or d.get("created_at") or d.get("ts")
    d["rolled_back"] = bool(d.get("rolled_back_by") or d.get("rolled_back_at") or d.get("status") == "rolled_back")
    d["is_rollback"] = d.get("op") == "rollback" or d.get("actor") in ("rollback",) or bool(d.get("rollback_of"))
    return d


async def ledger_list(hub: "Hub", **filters: Any) -> list[dict[str, Any]]:
    lr = ledger(hub)
    if lr is None or not hasattr(lr, "list"):
        return []
    try:
        rows = await call(lr.list, **{k: v for k, v in filters.items() if v is not None})
    except TypeError:
        rows = await call(lr.list)
    if isinstance(rows, dict):
        rows = rows.get("entries") or rows.get("items") or []
    out = [normalize_entry(r) for r in rows or []]
    for k, v in filters.items():
        if v is None or k in ("limit", "since"):
            continue
        out = [r for r in out if r.get(k) is None or r.get(k) == v]
    if filters.get("since"):
        out = [r for r in out if (r.get("applied_at") or "") >= str(filters["since"])]
    return out


async def ledger_rollback(hub: "Hub", entry_id: str, *, actor: str, reason: str) -> dict[str, Any]:
    lr = ledger(hub)
    if lr is None:
        return {"ok": False, "error": "ledger của M4-A chưa sẵn sàng (hub.learning.ledger)"}
    from ..errors import Conflict

    try:
        try:
            r = await call(lr.rollback, entry_id, actor=actor, reason=reason)
        except Conflict as e:
            if "đã được rollback" in str(e):
                return {"ok": False, "error": str(e), "already": True}
            if (getattr(e, "data", None) or {}).get("human_change"):  # H-04: đụng thay đổi của người → không ghi, Ledger đã cảnh báo
                return {"ok": False, "error": str(e), "human_change": True}
            # còn thay đổi sau cùng đích → rollback chuỗi (mới → cũ) để khôi phục đúng trạng thái trước entry
            r = await call(lr.rollback, entry_id, actor=actor, reason=reason, chain=True)
    except TypeError:
        r = await call(lr.rollback, entry_id, actor, reason)
    except Conflict as e:
        if (getattr(e, "data", None) or {}).get("human_change"):
            return {"ok": False, "error": str(e), "human_change": True}
        return {"ok": False, "error": str(e)[:300]}
    except Exception as e:  # rollback lỗi (đích bị sửa ngoài ledger…) → báo, không làm chết vòng nền
        return {"ok": False, "error": str(e)[:300]}
    out = dict(r) if isinstance(r, dict) else {"result": r}
    out.setdefault("ok", True)
    return out


def record_signal(hub: "Hub", agent_id: str, kind: str, evidence: dict[str, Any], *, run_id: str | None = None, source: str = "detector") -> str:
    learning = getattr(hub, "learning", None)
    sig = getattr(learning, "signals", None)
    fn = getattr(sig, "record", None)
    if callable(fn):
        try:
            r = fn(agent_id=agent_id, kind=kind, evidence=evidence, run_id=run_id, source=source)
            if isinstance(r, dict) and r.get("id"):
                return str(r["id"])
            if isinstance(r, str):
                return r
        except TypeError:
            pass
    sid = "sg_" + uuid.uuid4().hex[:12]
    hub.store.conn.execute(
        "INSERT INTO signals (id, agent_id, run_id, kind, source, evidence, created_at) VALUES (?,?,?,?,?,?,?)",
        (sid, agent_id, run_id, kind, source, json.dumps(evidence, ensure_ascii=False, default=str), now_iso()),
    )
    hub.publish(make_event("learning.signal", agent_id, {"signal_id": sid, "kind": kind, "source": source}, run_id=run_id))
    return sid
