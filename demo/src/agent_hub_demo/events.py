"""Envelope event của hub — dùng chung cho mọi viewer (TUI, web, CLI).

Thiết kế theo research/notes/05-ui-observability.md §7.2:
- `seq` tăng dần theo từng agent, `epoch` đổi mỗi lần hub khởi động lại
  → viewer biết khi nào seq bị reset và phải tải lại snapshot.
- `payload` tự do theo từng `type`; các khoá chuẩn được ghi chú bên dưới.

Phần này được viết để TÁI DÙNG nguyên vẹn cho bản thật.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

EventType = Literal[
    # trạng thái tổng quát của agent: payload {status: idle|working|waiting|error|stopped, detail?}
    "agent.status",
    # bắt đầu một lượt chạy (= một tiến trình `claude -p`): {model, task?, tools?, mcp_servers?, cwd?}
    "turn.start",
    # chữ trả lời stream từng mẩu: {text}
    "message.delta",
    # suy nghĩ (thinking) stream: {text, estimated_tokens?}
    "reasoning.delta",
    # tool bắt đầu: {tool_use_id, name, input, summary}
    "tool.start",
    # tool xong: {tool_use_id, name, duration_s, is_error, summary}
    "tool.complete",
    # usage giữa lượt (ước tính): {input_tokens, output_tokens, total_tokens, cost_usd,
    #   context_tokens, context_window, context_percent, tps, estimated}
    "usage.tick",
    # kết thúc lượt — số CHUẨN: {status, cost_usd, total_tokens, duration_ms, num_turns, result}
    "turn.complete",
    # câu hỏi ngược cần người trả lời (approval/clarify): {request_id, kind, tool, command, reason}
    "request.open",
    # request đã được trả lời/huỷ: {request_id, decision: approved|denied|cancelled, by}
    "request.cancel",
    # agent sinh subagent: {subagent_id, name, task}
    "subagent.spawn",
    # đề xuất tự cải thiện: {proposal_id, scope: agent|team, kind: memory|skill, title, diff, auto}
    "learning.proposal",
    # đề xuất đã áp dụng/từ chối/rollback: {proposal_id, action: applied|rejected|rolled_back, by}
    "learning.applied",
    # tin nhắn người dùng gửi vào phiên (steer): {text, by}   [mở rộng so với danh sách tối thiểu]
    "user.message",
    # lỗi: {message, code?, fatal?}
    "error",
]

ALL_EVENT_TYPES: tuple[str, ...] = EventType.__args__  # type: ignore[attr-defined]

# Event "nặng" (delta token) — kênh firehose cho màn tổng quan KHÔNG gửi các loại này.
DELTA_TYPES = frozenset({"message.delta", "reasoning.delta"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HubEvent(BaseModel):
    v: int = SCHEMA_VERSION
    agent_id: str
    session_id: str | None = None
    run_id: str | None = None
    seq: int = 0  # do bus gán khi publish
    epoch: str = ""  # do bus gán khi publish
    ts: datetime = Field(default_factory=utcnow)
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_delta(self) -> bool:
        return self.type in DELTA_TYPES


def make_event(
    type: EventType,
    agent_id: str,
    payload: dict[str, Any] | None = None,
    *,
    session_id: str | None = None,
    run_id: str | None = None,
    ts: datetime | None = None,
) -> HubEvent:
    return HubEvent(
        agent_id=agent_id,
        session_id=session_id,
        run_id=run_id,
        type=type,
        payload=payload or {},
        ts=ts or utcnow(),
    )
