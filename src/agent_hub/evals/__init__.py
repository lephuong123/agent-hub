"""Đánh giá (M4-B): golden task do người dùng quản lý + eval gate (FR-LN-10, Q-07, Q-23).

`hub.evals` (gắn bởi `learning_ops.wiring.attach`):
- `hub.evals.golden` : `GoldenStore` — CRUD golden task (chỉ người), gợi ý ứng viên (`golden.suggested`).
- `hub.evals.gate`   : `EvalGate` — `check(proposal)`, `post_apply(entry)`, `run_batch_l1()`, `run_manual(agent)`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .gate import EvalGate, GateVerdict
from .golden import GoldenStore

if TYPE_CHECKING:
    from ..hub import Hub

__all__ = ["EvalService", "EvalGate", "GateVerdict", "GoldenStore"]


class EvalService:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.golden = GoldenStore(hub)
        self.gate = EvalGate(hub)

    # hợp đồng docs/plan/M4.md: `evals.gate.check(proposal)` / `evals.gate.post_apply(entry)`
    async def check(self, proposal):  # pragma: no cover - uỷ quyền
        return await self.gate.check(proposal)

    async def post_apply(self, entry, **kw):  # pragma: no cover - uỷ quyền
        return await self.gate.post_apply(entry, **kw)
