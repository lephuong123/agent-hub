"""Tool MCP của M4-A trên server `cfharness` (FR-MS-02 tool learning, FR-LN-13): xem học, rollback một chạm, review thủ công,
xem tri thức. Gắn bằng `register_tools(srv, hub)` ở cuối `HubMcp.build()`.

Không có tool duyệt proposal (không có hàng chờ duyệt — FR-LN-04/05). Công tắc học/pin là chính sách an toàn: KHÔNG có
tool MCP sửa (chỉ CLI/TUI của người — FR-SG-05).
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import Context
from pydantic import BaseModel, ConfigDict, Field


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class LearnFeedOut(BaseModel):
    events: list[dict[str, Any]] = Field(description="event học mới nhất trước: learning.applied/rolled_back/deferred/eval_failed, guard.blocked (học), review.*")


class LearnStatusOut(_Open):
    today: dict[str, Any]
    pending_approval_queue: int = Field(0, description="luôn 0 — thay đổi tự áp dụng, người chỉ rollback/pin")


class LedgerOut(BaseModel):
    entries: list[dict[str, Any]]


class RollbackOut(_Open):
    id: str = Field(description="ledger entry MỚI (op=rollback) — rollback tiếp được")
    rollback_of: str | None = None


class ReviewOut(_Open):
    id: str
    agent_id: str
    status: str


class KnowledgeOut(_Open):
    agent_id: str
    skills: list[dict[str, Any]]


class TierOut(_Open):
    scope: str
    owner: str
    memory: dict[str, Any]
    skills: list[dict[str, Any]]
    canaries: list[dict[str, Any]]


class CanariesOut(BaseModel):
    canaries: list[dict[str, Any]] = Field(description="canary/shadow tầng team/global/USER: id, mode, đích, agent canary, tiến độ, hạn, kết quả")


LEARN_READ_ONLY = ("hub_learn_feed", "hub_learn_status", "hub_learn_ledger", "hub_knowledge_get", "hub_knowledge_tier", "hub_learn_canaries")


def register_tools(srv: Any, hub: Any) -> None:
    from .server import _ann

    @srv.tool(description="Dòng sự kiện học của hub (báo chứ không hỏi): thay đổi đã tự áp dụng (kèm lệnh rollback), đã rollback, bị chặn, bị hoãn, review.", annotations=_ann(True))
    async def hub_learn_feed(
        ctx: Context,
        agent_id: Annotated[str | None, Field(description="lọc theo agent")] = None,
        days: Annotated[int, Field(description="số ngày gần đây", ge=1, le=30)] = 1,
        limit: Annotated[int, Field(description="tối đa số event", ge=1, le=500)] = 100,
    ) -> LearnFeedOut:
        return LearnFeedOut(events=await hub.call("learn.feed", {"agent": agent_id, "days": days, "limit": limit}, ctx=ctx))

    @srv.tool(description="Tổng quan học hôm nay: số proposal theo trạng thái, đã áp dụng, rollback, review đang chạy/chờ, proposal bị hoãn. Không có hàng chờ duyệt.", annotations=_ann(True))
    async def hub_learn_status(ctx: Context, agent_id: Annotated[str | None, Field(description="lọc theo agent")] = None) -> LearnStatusOut:
        return LearnStatusOut(**await hub.call("learn.status", {"agent": agent_id}, ctx=ctx))

    @srv.tool(description="Ledger thay đổi tri thức/cấu hình (append-only): id entry, actor, op, đích, tóm tắt, đã rollback chưa.", annotations=_ann(True))
    async def hub_learn_ledger(
        ctx: Context,
        agent_id: Annotated[str | None, Field(description="lọc theo agent")] = None,
        limit: Annotated[int, Field(description="tối đa", ge=1, le=500)] = 50,
    ) -> LedgerOut:
        return LedgerOut(entries=await hub.call("learn.ledger", {"agent": agent_id, "limit": limit}, ctx=ctx))

    @srv.tool(description="Rollback MỘT ledger entry (khôi phục đúng nội dung trước; tạo entry mới, rollback tiếp được). chain=true rollback cả thay đổi sau cùng đích.",
              annotations=_ann(False, destructive=False, idempotent=False))
    async def hub_learn_rollback(
        ctx: Context,
        entry_id: Annotated[str, Field(description="id ledger entry (le_…)")],
        reason: Annotated[str | None, Field(description="lý do")] = None,
        chain: Annotated[bool, Field(description="rollback cả các entry sau cùng đích")] = False,
    ) -> RollbackOut:
        hub.require_control(ctx, "hub_learn_rollback")
        return RollbackOut(**await hub.call("learn.rollback", {"id": entry_id, "reason": reason, "chain": chain}, ctx=ctx, audit=True))

    @srv.tool(description="Yêu cầu agent tự review (fork session run gần nhất) với trọng tâm tuỳ chọn (tiếng Anh). Thay đổi rút ra được tự áp dụng.",
              annotations=_ann(False, destructive=False, idempotent=False))
    async def hub_learn_refine(
        ctx: Context,
        agent_id: Annotated[str, Field(description="id agent")],
        focus: Annotated[str | None, Field(description="trọng tâm review (tiếng Anh)")] = None,
    ) -> ReviewOut:
        hub.require_control(ctx, "hub_learn_refine")
        return ReviewOut(**await hub.call("learn.refine", {"agent": agent_id, "focus": focus}, ctx=ctx, audit=True))

    @srv.tool(description="Tri thức tầng agent: hạn mức MEMORY.md/USER.md, danh sách skill (mô tả, người tạo, pin, số lần dùng), skill đã archive.", annotations=_ann(True))
    async def hub_knowledge_get(ctx: Context, agent_id: Annotated[str, Field(description="id agent")]) -> KnowledgeOut:
        return KnowledgeOut(**await hub.call("knowledge.show", {"agent": agent_id}, ctx=ctx))

    # ------------------------------------------------------------------ M5-A: tầng team/global, USER, canary
    @srv.tool(description="Tri thức tầng team hoặc global (bản sống): memory từng dòng (khoá, pin), skill, thành viên, canary đang chạy. "
                          "scope=global gồm hồ sơ người dùng dùng chung (USER.md).", annotations=_ann(True))
    async def hub_knowledge_tier(
        ctx: Context,
        scope: Annotated[str, Field(description="team | global", pattern="^(team|global)$")],
        team_id: Annotated[str | None, Field(description="id team (scope=team)")] = None,
    ) -> TierOut:
        return TierOut(**await hub.call("knowledge.tier", {"scope": scope, "team": team_id}, ctx=ctx))

    @srv.tool(description="Canary/shadow của thay đổi tầng team/global/USER: đang chạy (mặc định) hoặc theo trạng thái promoted/rolled_back. "
                          "Không cần duyệt — hết cửa sổ (5 run hoặc 24 h) tự promoted hoặc tự rollback.", annotations=_ann(True))
    async def hub_learn_canaries(
        ctx: Context,
        status: Annotated[str | None, Field(description="canary | promoted | rolled_back | failed; bỏ trống = đang chạy")] = "canary",
        agent_id: Annotated[str | None, Field(description="lọc theo agent canary/được theo dõi")] = None,
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> CanariesOut:
        return CanariesOut(canaries=await hub.call("learn.canaries", {"status": status, "agent": agent_id, "limit": limit}, ctx=ctx))
