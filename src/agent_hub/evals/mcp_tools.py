"""Tool MCP của M4-B trên server `agent-hub` (FR-LN-10, FR-LN-13): `hub_golden_*`, `hub_eval_list`, `hub_learning_digest`,
resource `hub://digest`. Gắn bằng `register_tools(srv, hub)` ở cuối `HubMcp.build()`.

Golden task là chính sách an toàn do NGƯỜI quản lý (Q-07): tool ghi cần token `control` (người dùng qua Claude Code);
agent của hub không có MCP server này, và guard coi `mcp__*__hub_golden_(add|rm|edit|from_run)` gọi từ agent là
`policy_change` (forbidden).
"""

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context
from pydantic import BaseModel, ConfigDict, Field


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class GoldenOut(_Open):
    id: str
    agent_id: str
    title: str | None = None
    active: bool = True
    version: int = 1


class GoldenListOut(BaseModel):
    tasks: list[dict[str, Any]]
    counts: dict[str, int] = Field(default_factory=dict, description="số golden task đang hoạt động theo agent")


class EvalListOut(BaseModel):
    evals: list[dict[str, Any]]


class DigestOut(_Open):
    id: str | None = None
    text: str = Field(description="nội dung digest (markdown Slack)")
    counts: dict[str, Any] = Field(default_factory=dict)


CheckerKind = Literal["regex", "contains", "json_schema", "command", "llm_judge", "all"]


def register_tools(srv: Any, hub: Any) -> None:
    from ..mcp_server.server import _ann

    @srv.tool(
        description="NGƯỜI DÙNG tạo golden task cho agent (bộ chấm eval gate). checker = cách chấm do người chọn: "
                    "{kind:regex,pattern} | {kind:contains,all:[..]} | {kind:json_schema,schema} | {kind:command,cmd} | "
                    "{kind:llm_judge,rubric,pass_threshold} | {kind:all,checks:[..]}. Agent không được tự gọi.",
        annotations=_ann(False, destructive=False, idempotent=False),
    )
    async def hub_golden_add(
        ctx: Context,
        agent_id: Annotated[str, Field(description="id agent")],
        prompt: Annotated[str, Field(description="đầu vào giao cho agent")],
        checker: Annotated[dict[str, Any], Field(description="cách chấm (xem mô tả tool)")],
        title: Annotated[str | None, Field(description="tiêu đề ngắn")] = None,
        context: Annotated[str | None, Field(description="ngữ cảnh chèn trước prompt")] = None,
        source_run_id: Annotated[str | None, Field(description="đánh dấu từ run thật (tuỳ chọn) — prompt vẫn lấy theo tham số prompt")] = None,
    ) -> GoldenOut:
        hub.require_control(ctx, "hub_golden_add")
        p = {"agent_id": agent_id, "prompt": prompt, "checker": checker, "title": title, "context": context}
        if source_run_id:
            res = await hub.call("golden.from_run", {"run_id": source_run_id, "checker": checker, "title": title, "prompt": prompt, "context": context}, ctx=ctx, audit=True)
        else:
            res = await hub.call("golden.add", p, ctx=ctx, audit=True)
        return GoldenOut(**res)

    @srv.tool(description="Danh sách golden task (lọc theo agent) + số golden task theo agent.", annotations=_ann(True))
    async def hub_golden_list(ctx: Context, agent_id: Annotated[str | None, Field(description="lọc theo agent")] = None) -> GoldenListOut:
        return GoldenListOut(**await hub.call("golden.list", {"agent_id": agent_id}, ctx=ctx))

    @srv.tool(description="Xem một golden task: prompt, cách chấm, kết quả eval gần đây.", annotations=_ann(True))
    async def hub_golden_show(ctx: Context, golden_id: Annotated[str, Field(description="id golden task")]) -> GoldenOut:
        return GoldenOut(**await hub.call("golden.show", {"id": golden_id}, ctx=ctx))

    @srv.tool(description="NGƯỜI DÙNG xoá mềm một golden task (khôi phục được bằng CLI `golden restore`).", annotations=_ann(False, destructive=False, idempotent=True))
    async def hub_golden_rm(ctx: Context, golden_id: Annotated[str, Field(description="id golden task")]) -> GoldenOut:
        hub.require_control(ctx, "hub_golden_rm")
        return GoldenOut(**await hub.call("golden.rm", {"id": golden_id}, ctx=ctx, audit=True))

    @srv.tool(description="Các lần eval gần đây (gate trước/sau áp dụng, theo lô, thủ công): kết quả, điểm trước/sau, chi phí.", annotations=_ann(True))
    async def hub_eval_list(ctx: Context, agent_id: Annotated[str | None, Field(description="lọc theo agent")] = None,
                            limit: Annotated[int, Field(description="số dòng", ge=1, le=200)] = 20) -> EvalListOut:
        return EvalListOut(evals=await hub.call("eval.list", {"agent_id": agent_id, "limit": limit}, ctx=ctx))

    @srv.tool(description="Digest học gần nhất (đã áp dụng, rollback, eval_failed, hoãn, bị chặn, gợi ý golden task, lệnh rollback). "
                          "fresh=true dựng digest mới từ lần trước tới giờ (không gửi Slack).", annotations=_ann(True))
    async def hub_learning_digest(ctx: Context, fresh: Annotated[bool, Field(description="dựng mới thay vì đọc bản gần nhất")] = False) -> DigestOut:
        if fresh:
            d = await hub.call("digest.run", {"no_send": True}, ctx=ctx)
        else:
            d = await hub.call("digest.show", {}, ctx=ctx) or {}
            if not d:
                d = await hub.call("digest.run", {"no_send": True}, ctx=ctx)
        return DigestOut(id=d.get("id"), text=d.get("text") or "", counts=d.get("counts") or {})

    @srv.resource("hub://digest", name="digest", title="Digest học gần nhất", mime_type="text/markdown")
    async def digest_resource() -> str:
        d = await hub.call("digest.show", {}) or {}
        return d.get("text") or "(chưa có digest — `agent-hub digest run`)"


READ_ONLY_TOOLS_B = ("hub_golden_list", "hub_golden_show", "hub_eval_list", "hub_learning_digest")
