"""Cổng gọi eval gate của M4-B (hợp đồng docs/plan/M4.md mục 1).

- `check(hub, proposal)` TRƯỚC khi áp dụng L3 → `{status: passed|eval_failed|deferred_no_eval, scores, reason}`.
- `post_apply(hub, entry)` SAU khi áp dụng L2 (ngay) — B tự rollback khi tụt (actor `eval_gate`), A không rollback lại.
B cài `hub.evals.gate` (async). Chưa có B → bản tạm: L3 → `deferred_no_eval`, post-apply → `passed` (cờ `unevaluated`).
Kết quả có thể là awaitable (B chạy golden task bằng `claude`) — pipeline xử lý tiếp trong task nền.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..hub import Hub


def _gate(hub: "Hub") -> Any | None:
    g = getattr(getattr(hub, "evals", None), "gate", None)
    return g if g is not None and hasattr(g, "check") else None


def normalize(v: Any) -> dict[str, Any]:
    if v is None:
        return {"status": "deferred_no_eval", "reason": "gate trả None"}
    if hasattr(v, "to_dict"):
        v = v.to_dict()
    elif hasattr(v, "model_dump"):
        v = v.model_dump()
    elif not isinstance(v, dict):
        v = {"status": getattr(v, "status", "deferred_no_eval"), "scores": getattr(v, "scores", {}), "reason": getattr(v, "reason", "")}
    v = dict(v)
    v.setdefault("status", "deferred_no_eval")
    v.setdefault("scores", {})
    return v


def check(hub: "Hub", proposal: dict[str, Any]) -> Any:
    g = _gate(hub)
    if g is None:
        return {"status": "deferred_no_eval", "scores": {}, "reason": "stub: eval gate (M4-B) chưa sẵn sàng"}
    return g.check(proposal)


def post_apply(hub: "Hub", entry: dict[str, Any]) -> Any:
    g = _gate(hub)
    if g is None or not hasattr(g, "post_apply"):
        return {"status": "passed", "scores": {}, "reason": "stub: eval gate (M4-B) chưa sẵn sàng", "flags": ["unevaluated"]}
    return g.post_apply(entry)
