"""MCP server của hub (FR-MS-01..07): tool `hub_*` cho Claude Code / client MCP khác.

Tiến trình MỎNG: mọi thao tác đi qua JSON-RPC tới daemon (writer duy nhất). Tool mô tả tiếng Việt,
input/output JSON Schema sinh từ Pydantic, annotations `readOnlyHint`/`destructiveHint` đúng ngữ nghĩa.

Quyền (FR-MS-06, Q-11 một người dùng/một máy): stdio = `control`; HTTP theo token `read`/`control`.
Tool không readOnly bị từ chối với token `read`. Thao tác thay đổi được daemon ghi audit với
`channel=mcp:<tên client>`.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Callable, Literal

import anyio
from mcp.server.mcpserver import Context
from pydantic import BaseModel, ConfigDict, Field

from .. import __version__
from ..rpc import DaemonUnavailable, RpcError
from .guide import GUIDE_MD, INSTRUCTIONS

log = logging.getLogger("cfharness.mcp_server")

Caller = Callable[[str, dict[str, Any]], Any]
Level = Literal["read", "control"]


# ============================================================================ output models
class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class StatusOut(_Open):
    version: str = Field(description="phiên bản hub")
    epoch: str = Field(description="epoch hiện tại của daemon (đổi khi daemon khởi động lại)")
    running: int = Field(description="số run đang chạy")
    queued: int = Field(description="số run đang chờ")
    estop: dict[str, Any] = Field(description="trạng thái dừng khẩn cấp")
    cost_today_usd: float = Field(description="tổng chi phí hôm nay (USD)")
    open_requests: list[dict[str, Any]] = Field(default_factory=list, description="phê duyệt đang chờ người")
    access: str = Field(description="mức quyền của kết nối này: read | control")


class AgentSummary(_Open):
    id: str
    name: str
    role: str = ""
    description: str = ""
    model: str | None = None
    status: str
    current_run_id: str | None = None
    last_tool: str | None = None
    cost_today_usd: float | None = None


class AgentListOut(BaseModel):
    agents: list[AgentSummary]


class AgentOut(_Open):
    id: str
    name: str
    status: str
    config_revision: int


class AgentWriteOut(_Open):
    agent_id: str
    revision: int | None = None
    status: str | None = None
    idempotent_replay: bool = False


class RunOut(BaseModel):
    run_id: str | None = Field(default=None, description="id run — dùng cho hub_events/hub_run_status/hub_cancel (giao cho team: run hiện tại nếu đã có)")
    agent_id: str | None = None
    task_id: str | None = Field(default=None, description="M3: giao cho team → id task gốc; theo dõi bằng hub_events(task_id=…)")
    warnings: list[str] = Field(default_factory=list)
    status: str = Field(description="queued | running | …")
    epoch: str | None = Field(default=None, description="truyền lại cho hub_events")
    after_seq: int | None = Field(default=None, description="truyền làm after_seq cho lần hub_events đầu")
    idempotent_replay: bool = Field(default=False, description="true = trùng idempotency_key, trả run cũ")


class RunStatusOut(_Open):
    run_id: str
    agent_id: str
    status: str
    done: bool
    open_requests: list[dict[str, Any]] = Field(default_factory=list)


class EventsOut(BaseModel):
    run_id: str | None = None
    task_id: str | None = None
    events: list[dict[str, Any]] = Field(description="event rút gọn (seq, ts, type, payload)")
    latest_seq: int = Field(description="truyền làm after_seq ở lần gọi sau")
    epoch: str = Field(description="truyền lại ở lần gọi sau")
    truncated: bool = False
    has_more: bool = Field(default=False, description="còn event chưa trả — gọi lại ngay")
    status: str
    done: bool = Field(description="true = run đã kết thúc và đã trả hết event")
    open_requests: list[dict[str, Any]] = Field(default_factory=list, description="phê duyệt đang chờ người")
    result: dict[str, Any] | None = Field(default=None, description="khi done: status, summary, cost_usd, error…")


class SteerOut(_Open):
    run_id: str
    agent_id: str | None = None
    sent: bool = True
    steer_count: int | None = Field(default=None, description="số tin steer đã gửi vào run này")
    seq: int | None = Field(default=None, description="seq của event user.message — chờ steer.ack qua hub_events")


class CancelOut(_Open):
    run_id: str
    status: str


class RequestsOut(BaseModel):
    requests: list[dict[str, Any]]


class DecisionOut(_Open):
    request_id: str
    decision: str


class FeedbackOut(_Open):
    signal_id: str
    kind: str
    agent_id: str
    run_id: str


class EstopOut(_Open):
    on: bool


class PolicyOut(BaseModel):
    guard: dict[str, Any] = Field(description="chính sách guard: danh mục phá huỷ, tài nguyên được bảo vệ")
    learning: dict[str, Any] | None = Field(default=None, description="policy/learning.yaml: công tắc học, config_bounds, rate")


class MemorySearchOut(BaseModel):
    query: str
    results: list[dict[str, Any]] = Field(description="kind=memory|skill|transcript, score, text (đoạn trích), agent_id/run_id")
    total: int


class SessionSearchOut(_Open):
    mode: str
    hits: list[dict[str, Any]] | None = Field(default=None, description="mode=search: id, run_id, session_id/conversation_id, agent_id, role, ts, snippet, score")
    messages: list[dict[str, Any]] | None = Field(default=None, description="mode=around|read: tin theo thứ tự thời gian (anchor=true là tin gốc)")
    threads: list[dict[str, Any]] | None = Field(default=None, description="mode=browse: luồng gần đây (thread, agent_id, messages, runs, title)")
    next_offset: int | None = None


# M3
class TeamOut(_Open):
    id: str
    name: str
    members: list[dict[str, Any]] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)


class TeamListOut(BaseModel):
    teams: list[dict[str, Any]]


class TaskOut(_Open):
    id: str
    title: str
    status: str


class TaskListOut(BaseModel):
    tasks: list[dict[str, Any]]


class CommentOut(_Open):
    comment_id: str
    task_id: str


# ============================================================================ server
def _ann(read_only: bool, destructive: bool = False, idempotent: bool | None = None):
    from mcp.types import ToolAnnotations

    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=(False if read_only else destructive),
        idempotentHint=(True if read_only else idempotent),
        openWorldHint=False,
    )


class HubMcp:
    def __init__(self, caller: Caller, *, default_level: Level = "control", tokens: dict[str, str] | None = None, transport: str = "stdio") -> None:
        self.caller = caller
        self.default_level = default_level
        # token → level (HTTP)
        self.tokens = {v: k for k, v in (tokens or {}).items() if k in ("read", "control") and v}
        self.transport = transport

    # -------------------------------------------------------------- helpers
    def level(self, ctx: Any) -> str:
        headers = None
        try:
            headers = ctx.headers if ctx is not None else None
        except Exception:
            headers = None
        if headers is None:
            return self.default_level
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        return self.tokens.get(tok, "none")

    def channel(self, ctx: Any) -> str:
        name = None
        try:
            cp = ctx.session.client_params
            name = cp.client_info.name if cp and cp.client_info else None
        except Exception:
            name = None
        return f"mcp:{name or 'unknown'}" + (":http" if self.transport == "http" else "")

    def require_control(self, ctx: Any, tool: str) -> None:
        from mcp.server.mcpserver.exceptions import ToolError

        lvl = self.level(ctx)
        if lvl != "control":
            raise ToolError(f"{tool}: kết nối mức '{lvl}' không có quyền điều khiển — cần token 'control'")

    async def call(self, method: str, params: dict[str, Any] | None = None, *, ctx: Any = None, audit: bool = False) -> Any:
        from mcp.server.mcpserver.exceptions import ToolError

        params = dict(params or {})
        if audit:
            params["_audit"] = {"channel": self.channel(ctx), "actor": "user"}
        try:
            return await anyio.to_thread.run_sync(lambda: self.caller(method, params))
        except RpcError as e:
            detail = ""
            if isinstance(e.data, dict) and e.data.get("fields"):
                detail = " (" + "; ".join(f"{f['field']}: {f['error']}" for f in e.data["fields"]) + ")"
            raise ToolError(e.message + detail) from None
        except DaemonUnavailable as e:
            raise ToolError(f"daemon cfharness không chạy: {e}") from None

    # -------------------------------------------------------------- build
    def build(self):
        from mcp.server import MCPServer

        srv = MCPServer("cfharness", title="cfharness", instructions=INSTRUCTIONS, version=__version__)
        hub = self

        @srv.tool(description="Trạng thái hub: phiên bản, epoch, số run đang chạy/chờ, ESTOP, chi phí hôm nay, phê duyệt đang chờ. Gọi đầu tiên.", annotations=_ann(True))
        async def hub_status(ctx: Context) -> StatusOut:
            st = await hub.call("hub.status", ctx=ctx)
            reqs = await hub.call("mcp.open_requests", ctx=ctx)
            keep = {k: st.get(k) for k in ("version", "epoch", "running", "queued", "estop", "cost_today_usd", "max_concurrent_runs", "agents", "auth_block", "runtime")}
            return StatusOut(**keep, open_requests=reqs or [], access=hub.level(ctx))

        @srv.tool(description="Liệt kê agent của hub (id, tên, vai trò, mô tả để chọn agent, model, trạng thái, run hiện tại, chi phí hôm nay).", annotations=_ann(True))
        async def hub_list_agents(
            ctx: Context,
            include_archived: Annotated[bool, Field(description="kể cả agent đã xoá mềm")] = False,
            team: Annotated[str | None, Field(description="chỉ agent là thành viên team này")] = None,
            status: Annotated[str | None, Field(description="lọc trạng thái: idle|working|waiting|paused|error|disabled")] = None,
        ) -> AgentListOut:
            rows = await hub.call("agent.list", {"archived": include_archived}, ctx=ctx)
            if status:
                rows = [r for r in rows if r.get("status") == status]
            if team:
                t = await hub.call("team.get", {"id": team}, ctx=ctx)
                ids = {m["agent_id"] for m in t.get("members") or []}
                rows = [r for r in rows if r["id"] in ids]
            keys = AgentSummary.model_fields.keys()
            return AgentListOut(agents=[AgentSummary(**{k: r.get(k) for k in keys if k in r}) for r in rows])

        @srv.tool(description="Xem cấu hình hiệu lực của một agent: model, persona (SOUL), MCP, giới hạn, revision, run gần nhất.", annotations=_ann(True))
        async def hub_agent_get(ctx: Context, agent_id: Annotated[str, Field(description="id agent")]) -> AgentOut:
            ag = await hub.call("agent.get", {"id": agent_id}, ctx=ctx)
            return AgentOut(**ag)

        @srv.tool(
            description="Tạo agent mới. Persona (soul) nên viết tiếng Anh. Trả agent_id và revision. Có idempotency_key để retry an toàn.",
            annotations=_ann(False, destructive=False, idempotent=False),
        )
        async def hub_agent_create(
            ctx: Context,
            agent_id: Annotated[str, Field(description="slug: chữ thường, số, '-', '_'")],
            name: Annotated[str | None, Field(description="tên hiển thị (mặc định = agent_id)")] = None,
            role: Annotated[str, Field(description="vai trò ngắn")] = "",
            description: Annotated[str, Field(description="mô tả năng lực — dùng để chọn agent")] = "",
            model: Annotated[str, Field(description="opus | sonnet | haiku | fable | id đầy đủ claude-…")] = "sonnet",
            soul: Annotated[str | None, Field(description="nội dung SOUL.md (persona, tiếng Anh); bỏ trống = mặc định")] = None,
            mcp_servers: Annotated[list[str] | None, Field(description="tên MCP server trong registry")] = None,
            max_budget_usd_per_run: Annotated[float | None, Field(description="trần chi phí mỗi run (USD)", gt=0)] = None,
            budget_usd_per_day: Annotated[float | None, Field(description="trần chi phí mỗi ngày (USD)", gt=0)] = None,
            idempotency_key: Annotated[str | None, Field(description="khoá chống tạo trùng khi retry")] = None,
        ) -> AgentWriteOut:
            hub.require_control(ctx, "hub_agent_create")
            cfg: dict[str, Any] = {"id": agent_id, "name": name or agent_id, "role": role, "description": description, "model": model}
            if mcp_servers:
                cfg["mcp_servers"] = mcp_servers
            limits = {k: v for k, v in (("max_budget_usd_per_run", max_budget_usd_per_run), ("budget_usd_per_day", budget_usd_per_day)) if v is not None}
            if limits:
                cfg["limits"] = limits
            res = await hub.call("mcp.agent_create", {"config": cfg, "soul": soul, "idempotency_key": idempotency_key}, ctx=ctx, audit=True)
            return AgentWriteOut(**res)

        @srv.tool(
            description="Sửa agent (tăng revision). patch là object các trường cần đổi: name, role, description, model, effort, fallback_model, tool_policy, mcp_servers, limits, workspace_default.",
            annotations=_ann(False, destructive=False, idempotent=True),
        )
        async def hub_agent_update(
            ctx: Context,
            agent_id: Annotated[str, Field(description="id agent")],
            patch: Annotated[dict[str, Any], Field(description="trường cần đổi, vd {\"model\": \"haiku\", \"limits\": {\"max_runtime_s\": 600}}")],
            soul: Annotated[str | None, Field(description="thay toàn bộ SOUL.md (tiếng Anh)")] = None,
        ) -> AgentWriteOut:
            hub.require_control(ctx, "hub_agent_update")
            ag = await hub.call("agent.update", {"id": agent_id, "changes": patch, "soul": soul}, ctx=ctx, audit=True)
            return AgentWriteOut(agent_id=ag["id"], revision=ag["config_revision"], status=ag["status"])

        @srv.tool(
            description="Xoá mềm (archive) một agent — chuyển vào thùng rác, khôi phục được; restore=true để khôi phục. "
                        "Agent còn là thành viên team → bị từ chối kèm danh sách team, trừ khi detach_teams=true (gỡ khỏi mọi team trước).",
            annotations=_ann(False, destructive=True, idempotent=True),
        )
        async def hub_agent_archive(
            ctx: Context,
            agent_id: Annotated[str, Field(description="id agent")],
            restore: Annotated[bool, Field(description="true = khôi phục agent đã archive")] = False,
            detach_teams: Annotated[bool, Field(description="gỡ agent khỏi mọi team trước khi archive")] = False,
        ) -> AgentWriteOut:
            hub.require_control(ctx, "hub_agent_archive")
            if restore:
                ag = await hub.call("agent.restore", {"id": agent_id}, ctx=ctx, audit=True)
            else:
                ag = await hub.call("agent.archive", {"id": agent_id, "detach_teams": detach_teams}, ctx=ctx, audit=True)
            extra = {"detached_teams": [t["team_id"] for t in ag["detached_teams"]]} if ag.get("detached_teams") else {}
            return AgentWriteOut(agent_id=ag["id"], revision=ag["config_revision"], status="restored" if restore else "archived", **extra)

        @srv.tool(
            description="Giao việc cho một agent (agent=…) HOẶC cho một team (team=…; lead tách việc, worker làm, kết quả tổng hợp). Trả NGAY (không chờ làm xong) — sau đó lặp hub_events(run_id=…) hoặc hub_events(task_id=…) với team tới done=true. Dùng idempotency_key khi retry để không tạo trùng.",
            annotations=_ann(False, destructive=False, idempotent=False),
        )
        async def hub_run(
            ctx: Context,
            prompt: Annotated[str, Field(description="nội dung việc giao")],
            agent: Annotated[str | None, Field(description="id agent (xem hub_list_agents)")] = None,
            team: Annotated[str | None, Field(description="id team (xem hub_team_list) — thay cho agent")] = None,
            title: Annotated[str | None, Field(description="tiêu đề ngắn của task (khi giao cho team)")] = None,
            cwd: Annotated[str | None, Field(description="thư mục làm việc tuyệt đối (repo); bỏ trống = workspace của agent/team/scratch")] = None,
            budget_usd: Annotated[float | None, Field(description="trần chi phí mỗi run (USD)", gt=0)] = None,
            model: Annotated[str | None, Field(description="ghi đè model")] = None,
            result_schema: Annotated[dict[str, Any] | None, Field(description="JSON Schema cho output có cấu trúc (--json-schema)")] = None,
            idempotency_key: Annotated[str | None, Field(description="khoá chống tạo trùng khi retry")] = None,
        ) -> RunOut:
            from mcp.server.mcpserver.exceptions import ToolError

            hub.require_control(ctx, "hub_run")
            if bool(agent) == bool(team):
                raise ToolError("cần đúng một trong agent hoặc team")
            if team:
                p: dict[str, Any] = {"team": team, "prompt": prompt, "title": title, "budget_usd": budget_usd, "model": model, "result_schema": result_schema,
                                     "idempotency_key": idempotency_key, "workspace": {"kind": "dir", "path": cwd} if cwd else None, "created_by": hub.channel(ctx)}
                res = await hub.call("mcp.team_run", {k: v for k, v in p.items() if v is not None}, ctx=ctx, audit=True)
                return RunOut(**{k: res.get(k) for k in RunOut.model_fields if k in res})
            res = await hub.call(
                "mcp.run",
                {"agent": agent, "prompt": prompt, "cwd": cwd, "budget_usd": budget_usd, "model": model, "idempotency_key": idempotency_key},
                ctx=ctx,
                audit=True,
            )
            return RunOut(**{k: res.get(k) for k in RunOut.model_fields if k in res})

        @srv.tool(description="Trạng thái một run: status, outcome, summary, chi phí, lỗi, phê duyệt đang chờ. Không chờ.", annotations=_ann(True))
        async def hub_run_status(ctx: Context, run_id: Annotated[str, Field(description="id run (hoặc tiền tố duy nhất)")]) -> RunStatusOut:
            res = await hub.call("mcp.run_status", {"run_id": run_id}, ctx=ctx)
            return RunStatusOut(**res)

        @srv.tool(
            description="Long-poll event của một run (run_id) HOẶC một task và các task con (task_id): trả ngay khi có event mới, khi xong, hoặc sau tối đa wait_s (≤ 25) giây với events=[]. Truyền lại latest_seq và epoch của lần trước. done=true kèm result khi kết thúc (task: kết quả tổng hợp).",
            annotations=_ann(True),
        )
        async def hub_events(
            ctx: Context,
            run_id: Annotated[str | None, Field(description="id run")] = None,
            task_id: Annotated[str | None, Field(description="id task (M3) — thay cho run_id")] = None,
            agent_id: Annotated[str | None, Field(description="id agent — theo dõi run hiện tại (hoặc gần nhất) của agent, thay cho run_id")] = None,
            after_seq: Annotated[int, Field(description="chỉ lấy event có seq lớn hơn (latest_seq lần trước); 0 = từ đầu", ge=0)] = 0,
            epoch: Annotated[str | None, Field(description="epoch lần trước; khác epoch hiện tại → trả lại từ đầu")] = None,
            wait_s: Annotated[float, Field(description="thời gian chờ tối đa (giây), ≤ 25", ge=0, le=25)] = 20,
            level: Annotated[Literal["summary", "detail"], Field(description="summary (mặc định, gọn) | detail (gồm delta token)")] = "summary",
        ) -> EventsOut:
            from mcp.server.mcpserver.exceptions import ToolError

            if sum(bool(x) for x in (run_id, task_id, agent_id)) != 1:
                raise ToolError("cần đúng một trong run_id, task_id hoặc agent_id")
            if agent_id:
                run_id = (await hub.call("mcp.agent_run", {"agent_id": agent_id}, ctx=ctx))["run_id"]
            if task_id:
                res = await hub.call("mcp.task_events", {"task_id": task_id, "after_seq": after_seq, "epoch": epoch, "wait_s": wait_s, "level": level}, ctx=ctx)
                return EventsOut(**res)
            res = await hub.call("mcp.events", {"run_id": run_id, "after_seq": after_seq, "epoch": epoch, "wait_s": wait_s, "level": level}, ctx=ctx)
            return EventsOut(**res)

        @srv.tool(description="Dừng một run (run_id) HOẶC mọi run đang chạy/chờ của một task và task con (task_id). Phần việc dở bị bỏ; session được giữ để tiếp tục sau.",
                  annotations=_ann(False, destructive=True, idempotent=True))
        async def hub_cancel(
            ctx: Context,
            run_id: Annotated[str | None, Field(description="id run")] = None,
            task_id: Annotated[str | None, Field(description="id task — dừng run của task và các task con")] = None,
        ) -> CancelOut:
            from mcp.server.mcpserver.exceptions import ToolError

            hub.require_control(ctx, "hub_cancel")
            if bool(run_id) == bool(task_id):
                raise ToolError("cần đúng một trong run_id hoặc task_id")
            if task_id:
                r = await hub.call("mcp.cancel_task", {"task_id": task_id}, ctx=ctx, audit=True)
                return CancelOut(run_id=",".join(r["stopped_runs"]), status=r["status"], task_id=task_id, stopped_runs=r["stopped_runs"])
            r = await hub.call("run.stop", {"id": run_id}, ctx=ctx, audit=True)
            return CancelOut(run_id=r["id"], status="stop_requested" if r.get("status") == "running" else r.get("status", "unknown"))

        @srv.tool(
            description="Gửi thêm một tin nhắn (steer) vào run ĐANG CHẠY — agent nhận ở ranh giới lượt kế tiếp (event steer.ack). "
                        "Run đã kết thúc → lỗi, dùng tiếp tục run (run continue) thay vào đó. run_id có thể là id agent (run hiện tại).",
            annotations=_ann(False, destructive=False, idempotent=False),
        )
        async def hub_steer(
            ctx: Context,
            run_id: Annotated[str, Field(description="id run đang chạy (hoặc id agent → run hiện tại)")],
            text: Annotated[str, Field(description="nội dung tin nhắn gửi cho agent")],
        ) -> SteerOut:
            hub.require_control(ctx, "hub_steer")
            r = await hub.call("run.steer", {"id": run_id, "text": text, "channel": hub.channel(ctx), "by": "user"}, ctx=ctx, audit=True)
            return SteerOut(**r)

        @srv.tool(description="Danh sách yêu cầu phê duyệt đang mở (chỉ thao tác phá huỷ), kể cả waiting_human và thời gian đã chờ.", annotations=_ann(True))
        async def hub_open_requests(ctx: Context, run_id: Annotated[str | None, Field(description="lọc theo run")] = None) -> RequestsOut:
            return RequestsOut(requests=await hub.call("mcp.open_requests", {"run_id": run_id}, ctx=ctx) or [])

        async def _decide(ctx: Any, method: str, request_id: str, note: str | None, decision: str) -> DecisionOut:
            from mcp.server.mcpserver.exceptions import ToolError

            params = {"id": request_id, "request_id": request_id, "note": note, "channel": hub.channel(ctx), "actor": "user"}
            try:
                res = await hub.call(method, params, ctx=ctx)
            except ToolError as e:
                if "không có method" in str(e):
                    raise ToolError("phê duyệt chưa sẵn sàng trên daemon này (approval.* chưa cài)") from None
                raise
            res = res if isinstance(res, dict) else {"result": res}
            return DecisionOut(**{"request_id": request_id, "decision": decision, **{k: v for k, v in res.items() if k not in ("request_id", "decision")}})

        @srv.tool(
            description="CHO PHÉP một thao tác phá huỷ đang chờ (đúng một lần). CHỈ gọi khi người dùng đã đồng ý rõ ràng.",
            annotations=_ann(False, destructive=True, idempotent=True),
        )
        async def hub_approve(
            ctx: Context,
            request_id: Annotated[str, Field(description="id yêu cầu (từ open_requests)")],
            note: Annotated[str | None, Field(description="ghi chú của người duyệt")] = None,
        ) -> DecisionOut:
            hub.require_control(ctx, "hub_approve")
            return await _decide(ctx, "approval.approve", request_id, note, "allow")

        @srv.tool(description="TỪ CHỐI một thao tác phá huỷ đang chờ; agent nhận lý do và tiếp tục theo hướng khác.", annotations=_ann(False, destructive=False, idempotent=True))
        async def hub_deny(
            ctx: Context,
            request_id: Annotated[str, Field(description="id yêu cầu (từ open_requests)")],
            note: Annotated[str | None, Field(description="lý do từ chối (agent sẽ đọc)")] = None,
        ) -> DecisionOut:
            hub.require_control(ctx, "hub_deny")
            return await _decide(ctx, "approval.deny", request_id, note, "deny")

        @srv.tool(
            description="Phản hồi về kết quả một run để agent học: kind=good|wrong|partial, note giải thích, corrected_output (tuỳ chọn) là bản đúng.",
            annotations=_ann(False, destructive=False, idempotent=False),
        )
        async def hub_feedback(
            ctx: Context,
            run_id: Annotated[str, Field(description="id run")],
            kind: Annotated[Literal["good", "wrong", "partial"], Field(description="đánh giá: good | wrong | partial")],
            note: Annotated[str | None, Field(description="giải thích ngắn (vd 'phải dùng định dạng dd/mm/yyyy')")] = None,
            corrected_output: Annotated[str | None, Field(description="bản output đúng (nếu có)")] = None,
        ) -> FeedbackOut:
            hub.require_control(ctx, "hub_feedback")
            res = await hub.call(
                "feedback.record",
                {"run_id": run_id, "kind": kind, "note": note, "corrected_output": corrected_output, "source": "mcp"},
                ctx=ctx,
                audit=True,
            )
            return FeedbackOut(**res)

        @srv.tool(
            description="Dừng khẩn cấp toàn hub (ESTOP): on=true chặn mọi run mới, stop_running=true dừng cả run đang chạy; on=false để bỏ.",
            annotations=_ann(False, destructive=True, idempotent=True),
        )
        async def hub_estop(
            ctx: Context,
            on: Annotated[bool, Field(description="bật/tắt ESTOP")],
            reason: Annotated[str | None, Field(description="lý do")] = None,
            stop_running: Annotated[bool, Field(description="dừng luôn các run đang chạy")] = False,
            team: Annotated[str | None, Field(description="chỉ ESTOP team này (FR-TK-10); bỏ trống = toàn hub")] = None,
        ) -> EstopOut:
            hub.require_control(ctx, "hub_estop")
            p: dict[str, Any] = {"on": on, "reason": reason, "stop_running": stop_running}
            if team:
                p["team"] = team
            return EstopOut(**await hub.call("estop.set", p, ctx=ctx, audit=True))

        @srv.tool(
            description="Chính sách an toàn hiện hành (CHỈ ĐỌC): danh mục thao tác phá huỷ, tài nguyên được bảo vệ, công tắc học và "
                        "config_bounds. Không có tool MCP nào sửa chính sách — chỉ người qua CLI/TUI.",
            annotations=_ann(True),
        )
        async def hub_policy_get(ctx: Context) -> PolicyOut:
            guard = await hub.call("guard.policy", ctx=ctx)
            try:
                learning = await hub.call("learn.policy_get", {}, ctx=ctx)
            except Exception:  # luồng học chưa cài
                learning = None
            return PolicyOut(guard=guard if isinstance(guard, dict) else {"policy": guard}, learning=learning if isinstance(learning, dict) else None)

        @srv.tool(
            description="Tìm trong memory (MEMORY/USER các tầng agent→team→global), skill và transcript (prompt/summary run, không gồm phiên kỹ thuật) "
                        "— tìm theo từ khoá, KHÔNG gọi LLM. scope: all|memory|skills|transcripts.",
            annotations=_ann(True),
        )
        async def hub_memory_search(
            ctx: Context,
            query: Annotated[str, Field(description="từ khoá cần tìm")],
            agent_id: Annotated[str | None, Field(description="giới hạn trong một agent (bỏ trống = mọi agent)")] = None,
            scope: Annotated[Literal["all", "memory", "skills", "transcripts"], Field(description="phạm vi tìm")] = "all",
            limit: Annotated[int, Field(description="số kết quả tối đa", ge=1, le=50)] = 10,
        ) -> MemorySearchOut:
            res = await hub.call("memory.search", {"query": query, "agent_id": agent_id, "scope": scope, "limit": limit}, ctx=ctx)
            return MemorySearchOut(**res)

        @srv.tool(
            description="Tìm kiếm hội thoại (SQLite FTS5, KHÔNG gọi LLM) trên toàn bộ nội dung run + hội thoại điều phối viên: prompt, tin steer, "
                        "câu trả lời hoàn chỉnh của agent, lời gọi/kết quả tool (đã che secret). Gõ có dấu hay không dấu đều khớp. "
                        "mode: search (query → hit xếp hạng + đoạn trích + message id) | around (message_id → `window` tin trước/sau) | "
                        "read (run_id / session_id / conversation_id → transcript phân trang) | browse (luồng gần đây). "
                        "Lọc: agent_id, team_id, kind, role, since/until (ISO hoặc 7d/12h). Mặc định ẩn phiên kỹ thuật (review/eval/curator).",
            annotations=_ann(True),
        )
        async def hub_session_search(
            ctx: Context,
            mode: Annotated[Literal["search", "around", "read", "browse"], Field(description="chế độ")] = "search",
            query: Annotated[str | None, Field(description="từ khoá (mode=search); \"cụm từ\" trong ngoặc kép")] = None,
            message_id: Annotated[int | None, Field(description="id tin (mode=around)")] = None,
            window: Annotated[int, Field(description="số tin trước/sau (mode=around)", ge=1, le=50)] = 5,
            run_id: Annotated[str | None, Field(description="run (mode=read, hoặc lọc)")] = None,
            session_id: Annotated[str | None, Field(description="session claude (mode=read, hoặc lọc)")] = None,
            conversation_id: Annotated[str | None, Field(description="hội thoại điều phối viên mc_… (mode=read)")] = None,
            agent_id: Annotated[str | None, Field(description="lọc theo agent")] = None,
            team_id: Annotated[str | None, Field(description="lọc theo team")] = None,
            kind: Annotated[str | None, Field(description="lọc theo loại run (adhoc, task, chat, …; manager_chat = tin hội thoại điều phối viên)")] = None,
            role: Annotated[str | None, Field(description="user|steer|assistant|tool_call|tool_result|manager|…")] = None,
            since: Annotated[str | None, Field(description="từ thời điểm (ISO, YYYY-MM-DD, hoặc 7d/12h/30m)")] = None,
            until: Annotated[str | None, Field(description="đến thời điểm")] = None,
            include_technical: Annotated[bool, Field(description="kể cả phiên kỹ thuật review/eval/curator")] = False,
            limit: Annotated[int, Field(description="số mục tối đa", ge=1, le=200)] = 20,
            offset: Annotated[int, Field(description="phân trang", ge=0)] = 0,
        ) -> SessionSearchOut:
            params = {"mode": mode, "query": query, "message_id": message_id, "window": window, "run_id": run_id, "session_id": session_id,
                      "conversation_id": conversation_id, "agent_id": agent_id, "team_id": team_id, "kind": kind, "role": role, "since": since,
                      "until": until, "include_technical": include_technical, "limit": limit, "offset": offset}
            res = await hub.call("session.search", {k: v for k, v in params.items() if v is not None}, ctx=ctx)
            return SessionSearchOut(**res)

        # ---------------------------------------------------------------- M3: team & task (FR-MS-02, FR-MS-08)
        @srv.tool(description="Liệt kê team: thành viên + vai trò, chính sách, ESTOP, số task theo trạng thái, chi phí hôm nay.", annotations=_ann(True))
        async def hub_team_list(ctx: Context, include_archived: Annotated[bool, Field(description="kể cả team đã archive")] = False) -> TeamListOut:
            return TeamListOut(teams=await hub.call("team.list", {"include_archived": include_archived}, ctx=ctx))

        @srv.tool(description="Tổng quan một team: thành viên + trạng thái, task theo trạng thái, chi phí hôm nay/tuần, ngân sách còn lại, ESTOP.", annotations=_ann(True))
        async def hub_team_get(ctx: Context, team_id: Annotated[str, Field(description="id team")]) -> TeamOut:
            return TeamOut(**await hub.call("team.get", {"id": team_id}, ctx=ctx))

        @srv.tool(
            description="Tạo team. members: [{agent_id, role: lead|worker|reviewer|verifier, max_in_progress?}]; policies: review_required, failure_limit, budget_usd_per_day, auto_decompose, default_assignee, workspace.",
            annotations=_ann(False, destructive=False, idempotent=False),
        )
        async def hub_team_create(
            ctx: Context,
            team_id: Annotated[str, Field(description="slug")],
            name: Annotated[str | None, Field(description="tên hiển thị")] = None,
            charter: Annotated[str, Field(description="mục tiêu/cách làm việc — chèn vào prompt thành viên (nên viết tiếng Anh)")] = "",
            policies: Annotated[dict[str, Any] | None, Field(description="chính sách team")] = None,
            members: Annotated[list[dict[str, Any]] | None, Field(description="thành viên ban đầu")] = None,
        ) -> TeamOut:
            hub.require_control(ctx, "hub_team_create")
            cfg = {"id": team_id, "name": name or team_id, "charter": charter, "policies": policies or {}, "members": members or []}
            return TeamOut(**await hub.call("team.create", {"config": cfg}, ctx=ctx, audit=True))

        @srv.tool(description="Sửa team: name, charter, policies (gộp với chính sách hiện có).", annotations=_ann(False, destructive=False, idempotent=True))
        async def hub_team_update(
            ctx: Context,
            team_id: Annotated[str, Field(description="id team")],
            patch: Annotated[dict[str, Any], Field(description="vd {\"policies\": {\"budget_usd_per_day\": 2}}")],
        ) -> TeamOut:
            hub.require_control(ctx, "hub_team_update")
            return TeamOut(**await hub.call("team.update", {"id": team_id, "changes": patch}, ctx=ctx, audit=True))

        @srv.tool(description="Thêm/sửa (role=lead|worker|reviewer|verifier) hoặc gỡ (remove=true) thành viên team. Mỗi team tối đa 1 lead.", annotations=_ann(False, destructive=False, idempotent=True))
        async def hub_team_member_set(
            ctx: Context,
            team_id: Annotated[str, Field(description="id team")],
            agent_id: Annotated[str, Field(description="id agent")],
            role: Annotated[Literal["lead", "worker", "reviewer", "verifier"], Field(description="vai trò")] = "worker",
            max_in_progress: Annotated[int | None, Field(description="số task chạy cùng lúc tối đa của thành viên", ge=1)] = None,
            remove: Annotated[bool, Field(description="true = gỡ khỏi team")] = False,
        ) -> TeamOut:
            hub.require_control(ctx, "hub_team_member_set")
            if remove:
                return TeamOut(**await hub.call("team.member_rm", {"team": team_id, "agent": agent_id}, ctx=ctx, audit=True))
            return TeamOut(**await hub.call("team.member_set", {"team": team_id, "agent": agent_id, "role": role, "max_in_progress": max_in_progress}, ctx=ctx, audit=True))

        @srv.tool(
            description="Tạo task: giao cho team (lead tách việc/định tuyến theo vai trò) hoặc assignee; parents = task phải xong trước; workspace_dir = repo ghi thẳng (khoá + checkpoint + diff). Trả task_id; theo dõi bằng hub_events(task_id).",
            annotations=_ann(False, destructive=False, idempotent=False),
        )
        async def hub_task_create(
            ctx: Context,
            title: Annotated[str, Field(description="tiêu đề")],
            body: Annotated[str, Field(description="mô tả việc")] = "",
            team: Annotated[str | None, Field(description="id team")] = None,
            assignee: Annotated[str | None, Field(description="id agent")] = None,
            parents: Annotated[list[str] | None, Field(description="id task cha (DAG)")] = None,
            priority: Annotated[int, Field(description="ưu tiên (cao chạy trước)")] = 0,
            workspace_dir: Annotated[str | None, Field(description="thư mục/repo làm việc tuyệt đối")] = None,
            decompose: Annotated[bool | None, Field(description="ép (không) cho lead tách việc; bỏ trống = theo chính sách team")] = None,
            budget_usd: Annotated[float | None, Field(description="trần chi phí mỗi run", gt=0)] = None,
            result_schema: Annotated[dict[str, Any] | None, Field(description="JSON Schema output có cấu trúc")] = None,
            idempotency_key: Annotated[str | None, Field(description="khoá chống tạo trùng")] = None,
        ) -> RunOut:
            hub.require_control(ctx, "hub_task_create")
            p = {"title": title, "body": body, "team": team, "assignee": assignee, "parents": parents, "priority": priority, "decompose": decompose,
                 "budget_usd": budget_usd, "result_schema": result_schema, "idempotency_key": idempotency_key, "created_by": hub.channel(ctx),
                 "workspace": {"kind": "dir", "path": workspace_dir} if workspace_dir else None}
            res = await hub.call("mcp.team_run", {k: v for k, v in p.items() if v is not None}, ctx=ctx, audit=True)
            return RunOut(**{k: res.get(k) for k in RunOut.model_fields if k in res})

        @srv.tool(description="Liệt kê task (lọc team/status/assignee/origin=task gốc). status: triage|todo|ready|running|review|done|blocked|archived.", annotations=_ann(True))
        async def hub_task_list(
            ctx: Context,
            team: Annotated[str | None, Field(description="id team")] = None,
            status: Annotated[str | None, Field(description="trạng thái")] = None,
            assignee: Annotated[str | None, Field(description="id agent")] = None,
            origin: Annotated[str | None, Field(description="chỉ task con của task gốc này")] = None,
            limit: Annotated[int, Field(ge=1, le=500)] = 100,
        ) -> TaskListOut:
            rows = await hub.call("task.list", {"team": team, "status": status, "assignee": assignee, "origin": origin, "limit": limit}, ctx=ctx)
            return TaskListOut(tasks=rows)

        @srv.tool(description="Chi tiết task: nội dung, cha/con (DAG), task con đã tách, comment, các run (lần thử), kết quả.", annotations=_ann(True))
        async def hub_task_get(ctx: Context, task_id: Annotated[str, Field(description="id task")]) -> TaskOut:
            return TaskOut(**await hub.call("task.get", {"id": task_id}, ctx=ctx))

        @srv.tool(description="Bình luận vào task (agent đọc khi được spawn); steer=true chuyển thành tin nhắn tới run đang chạy.", annotations=_ann(False, destructive=False, idempotent=False))
        async def hub_task_comment(
            ctx: Context,
            task_id: Annotated[str, Field(description="id task")],
            text: Annotated[str, Field(description="nội dung")],
            steer: Annotated[bool, Field(description="gửi luôn vào run đang chạy (steer)")] = False,
        ) -> CommentOut:
            hub.require_control(ctx, "hub_task_comment")
            return CommentOut(**await hub.call("task.comment", {"id": task_id, "text": text, "steer": steer, "author": hub.channel(ctx)}, ctx=ctx, audit=True))

        @srv.resource("hub://huong-dan", name="huong-dan", title="Hướng dẫn giao việc qua cfharness", mime_type="text/markdown")
        def huong_dan() -> str:
            return GUIDE_MD

        @srv.resource("hub://agents", name="agents", title="Danh sách agent (snapshot)", mime_type="application/json")
        async def agents_snapshot() -> str:
            rows = await hub.call("agent.list", {})
            keys = AgentSummary.model_fields.keys()
            return json.dumps([{k: r.get(k) for k in keys} for r in rows], ensure_ascii=False, indent=2)

        # M4-B: hub_golden_*, hub_eval_list, hub_learning_digest, resource hub://digest (evals/mcp_tools.py)
        from ..evals.mcp_tools import register_tools as _register_m4b_tools

        _register_m4b_tools(srv, hub)
        # M4-A: hub_learn_feed|status|ledger|rollback|refine, hub_knowledge_get (mcp_server/learning_tools.py)
        from .learning_tools import register_tools as _register_m4a_tools

        _register_m4a_tools(srv, hub)
        return srv


def build_server(caller: Caller, **kw: Any):
    return HubMcp(caller, **kw).build()


# Tool chỉ đọc — dùng cho kiểm tra/ghi chú (token `read` chỉ gọi được nhóm này)
READ_ONLY_TOOLS = ("hub_status", "hub_list_agents", "hub_agent_get", "hub_run_status", "hub_events", "hub_open_requests",
                   "hub_team_list", "hub_team_get", "hub_task_list", "hub_task_get",  # M3: 4 tool trên
                   "hub_golden_list", "hub_golden_show", "hub_eval_list", "hub_learning_digest",  # M4-B
                   "hub_learn_feed", "hub_learn_status", "hub_learn_ledger", "hub_knowledge_get",  # M4-A
                   "hub_knowledge_tier", "hub_learn_canaries",  # M5-A
                   "hub_policy_get", "hub_memory_search",  # hardening H-14
                   "hub_session_search")  # v17: tìm kiếm hội thoại FTS5
