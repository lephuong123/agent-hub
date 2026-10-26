"""Envelope event của hub (BRD §9.1) và danh mục loại event (BRD §9.2).

Cải tiến so với demo:
- `seq` đơn điệu TOÀN HUB trong một `epoch` (một writer); `epoch` đổi mỗi lần daemon khởi động.
- Thêm `level` (summary/detail/debug), `source`, `team_id`, `task_id`, `subagent_id`.
- Loại event theo BRD (`run.started`, `run.completed`…), không còn `turn.complete` của demo.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

Level = Literal["summary", "detail", "debug"]
Source = Literal["cli", "hub", "tui", "mcp", "hook"]

# Loại event → level mặc định. Loại không có trong bảng vẫn được chấp nhận (mở rộng), level "summary".
EVENT_LEVELS: dict[str, str] = {
    # Agent
    "agent.created": "summary",
    "agent.updated": "summary",
    "agent.archived": "summary",
    "agent.deleted": "summary",
    "agent.restored": "summary",
    "agent.status": "summary",
    # Run
    "run.queued": "summary",
    "run.started": "summary",
    "run.completed": "summary",
    "run.failed": "summary",
    "run.cancelled": "summary",
    "run.interrupted": "summary",
    "isolation.violation": "summary",
    "permission.denied": "summary",
    "task.background": "detail",
    "control.response": "debug",
    # Turn & message
    "turn.start": "detail",
    "status.update": "detail",
    "message.delta": "detail",
    "reasoning.delta": "detail",
    "reasoning.progress": "detail",
    "message.complete": "summary",
    # Tool
    "tool.generating": "detail",
    "tool.start": "summary",
    "tool.complete": "summary",
    # Usage
    "usage.tick": "summary",
    "ratelimit.update": "summary",
    "budget.warning": "summary",
    "budget.exceeded": "summary",
    # Subagent
    "subagent.spawn": "summary",
    # Hub
    "hub.estop": "summary",
    "hub.started": "summary",
    "hub.stopping": "summary",
    "hub.config_changed": "summary",
    "cli.version_warning": "summary",
    "auth.blocked": "summary",
    "hook.event": "debug",
    "hook.timeout": "summary",  # hardening H-11: hook quan sát quá 2 s → fail-OPEN, ghi lại (FR-RT-17)
    "hook.observed": "debug",  # hardening H-11: hook quan sát PostToolUse/UserPromptSubmit/Stop (chỉ metadata)
    "cli.unknown_rate_alert": "summary",  # hardening H-10: raw.unknown > ngưỡng trong 1 giờ → cảnh báo đỏ + gợi ý ESTOP
    "raw.unknown": "debug",
    "raw.invalid": "debug",
    "runtime.stderr": "debug",
    "error": "summary",
    # M1 luồng A — tích hợp
    "tool.write": "summary",  # tool MCP không readOnlyHint (FR-MC-03), có audit
    "mcp.changed": "summary",  # registry MCP thêm/sửa/xoá mềm/probe/import
    "learning.signal": "summary",  # hub_feedback ghi Signal (FR-LN-01)
    # M1 luồng B — an toàn & vòng đời
    "approval.open": "summary",
    "approval.resolved": "summary",
    "approval.waiting_human": "summary",
    "approval.reminder": "summary",
    # hồ sơ phê duyệt (docs/dev-notes/approval-profiles.md) — tên CỐ ĐỊNH (hợp đồng với web/chat): approval giao điều phối viên
    # (request_id, routed_to="manager", manager, tool, preview, destructive_class, matched_rule, timeout_s) / chuyển lên người
    # (request_id, routed_to="human", by, note, reason) — sau escalated người nhận thông báo như approval.open
    "approval.delegated": "summary",
    "approval.escalated": "summary",
    "guard.decision": "debug",
    "guard.blocked": "summary",
    "guard.tamper_detected": "summary",  # hardening SEC: policy/config/hub.db bị ghi NGOÀI RPC (guard/integrity.py) → khôi phục + Slack
    "auth.denied": "summary",  # hardening SEC: method chỉ-người bị gọi từ tiến trình run/thiếu token (auth.py)
    "guard.env_tamper": "summary",  # hardening SEC R2-1: .pth/sitecustomize trong venv của hub bị thêm/sửa (chạy ngoài sandbox)
    "guard.sandbox_override": "summary",  # hardening SEC: người đặt/gỡ ngoại lệ sandbox theo agent (vd loopback)
    "guard.sandbox_disabled": "summary",  # hardening SEC/WT-3: sandbox OS tắt bằng cấu hình (nhiều bất biến dựa vào sandbox)
    "policy.changed": "summary",
    "run.waiting_human": "summary",  # tiến trình đã dừng, chờ người duyệt (slot nhả, khoá giữ)
    "run.resumed": "summary",  # run được tiếp tục ở run mới (--resume)
    "run.repeated_failure": "summary",
    "recovery.reconciled": "summary",
    "notify.sent": "debug",
    "notify.failed": "summary",
    "agent.purged": "summary",
    # M2 — htop / steer / takeover
    "user.message": "summary",  # steer: tin người gửi vào run đang chạy (FR-RT-06), payload {text, by, channel}
    "steer.ack": "summary",  # CLI phát lại tin steer (`isReplay`) đúng lúc đưa vào ngữ cảnh (spike A-02)
    "run.takeover": "summary",  # người tiếp quản session bằng `claude --resume` tương tác (FR-UI-10)
    # M3 — team & điều phối
    "team.created": "summary",
    "team.updated": "summary",
    "team.member": "summary",
    "team.archived": "summary",
    "team.restored": "summary",
    "team.estop": "summary",
    "task.created": "summary",
    "task.status": "summary",
    "task.comment": "summary",
    "task.linked": "summary",
    "task.signal": "summary",  # agent gọi hub-self task_complete/task_block/task_request_review
    "task.decomposed": "summary",
    "task.circuit_open": "summary",
    "run.protocol_violation": "summary",
    "run.reverted": "summary",
    "workspace.checkpoint": "debug",
    "workspace.diff": "summary",
    "ratelimit.throttle": "summary",
    "agent.cloned": "summary",
    "agent.imported": "summary",
    # M4-A — lõi học (FR-LN-01..07, 15, 18, 19; FR-OB-04)
    "review.started": "summary",  # review fork bắt đầu: review_id, run_id, trigger, signals
    "review.completed": "summary",  # review xong: outcome, proposals[], cost
    "learning.proposal": "summary",  # proposal mới: proposal_id, target, op, created_by
    "learning.applied": "summary",  # đã tự áp dụng: ledger_entry_id, snapshot_id, diff tóm tắt, lệnh rollback
    "learning.deferred": "summary",  # hoãn: rate_limited | no_eval
    "learning.eval_failed": "summary",  # L3 tụt eval → không áp dụng
    "learning.rolled_back": "summary",  # rollback entry: actor user/eval_gate/regression_monitor/canary
    "learning.canary": "summary",  # (M5)
    "learning.promoted": "summary",  # (M5)
    "cache.warning": "summary",  # FR-OB-04: hash system prompt đổi > N lần/ngày
    # M4-B — đánh giá & vận hành học
    "golden.added": "summary",  # người tạo golden task (FR-LN-10, Q-07)
    "golden.updated": "summary",
    "golden.removed": "summary",  # xoá mềm
    "golden.suggested": "summary",  # vòng học/gate CHỈ gợi ý run ứng viên (Q-23) — vào digest + Slack
    "eval.started": "summary",  # eval gate bắt đầu (check / post_apply / batch / manual)
    "eval.completed": "summary",  # kết quả gate: status, điểm trước/sau, chi phí
    "regression.detected": "summary",  # FR-LN-17: hồi quy → rollback (A phát learning.rolled_back)
    "learning.digest": "summary",  # FR-LN-13: digest đã dựng (Slack qua notify)
    "curator.report": "summary",  # FR-LN-08: curator pha 1
    # M5-B — curation liên agent (FR-LN-12) + pha 2 LLM (FR-LN-09)
    "curation.report": "summary",  # lượt liên agent xong: số cụm, proposal promote
    "curation.promotion": "summary",  # proposal promote lên team đã gửi vào pipeline (team_skill, members, status)
    "curation.members_archived": "summary",  # sau promoted: bản agent được archive (absorbed_into skill team)
    "curation.started": "summary",  # curator pha 2 bắt đầu (snapshot_id)
    "curation.completed": "summary",  # curator pha 2 xong: archived[], rollback_cmd, cost
    "curation.rolled_back": "summary",  # người hoàn tác cả lượt curation
    # M7 MGR-CORE — điều phối viên (manager/): hành động ghi, trả lời hội thoại, nhịp, chỉ định
    "manager.action": "summary",  # mọi thao tác ghi của manager (tool, target, summary, audit_id, ok, error?)
    "manager.reply": "summary",  # trả lời/ thông báo của manager tới kênh người (conversation_id, run_id, text, kind)
    "manager.tick": "summary",  # nhịp heartbeat: started|skipped (reason, events)
    "manager.designated": "summary",  # người chỉ định/huỷ chỉ định manager (agent_id, previous)
    # UX — câu hỏi có lựa chọn (questions/): agent hỏi → điều phối viên (routed_to=manager) hoặc người (routed_to=human)
    "question.open": "summary",  # question_id, routed_to, text, options[{key,label,description}], multi, allow_free_text, source
    "question.escalated": "summary",  # điều phối viên không chốt được / quá hạn → người (question_id, by, note)
    "question.answered": "summary",  # question_id, keys, text, by, channel
    "question.cancelled": "summary",  # question_id, reason
    "question.action": "summary",  # xin phép: người cho phép → hub thực hiện (question_id, kind, target, ok, error?)
    "question.reminder": "summary",  # nhắc người: question_id, reminder_count, wait_s
    # Hệ thống ngoài hub (systems/): thao tác job launchd/Task Scheduler (system, label, action pause|resume|restart, ok, by, reason, detail)
    "system.job": "summary",
    # Lịch chạy tự động (schedules/): schedule_id, name, kind, expr, status, next_run_at, created_by, by (+ action/reason/fire…)
    "schedule.created": "summary",
    "schedule.updated": "summary",  # action: edit|resume|rm
    "schedule.fired": "summary",  # fire_id, run_id|task_id, conversation_id, due_at, manual, catchup
    "schedule.skipped": "summary",  # reason: estop|overlap|missed|agent_paused|agent_archived|team_estop|no_manager|blocked: …
    "schedule.failed": "summary",  # reason, fail_count
    "schedule.paused": "summary",  # by (user|manager:<id>|hub — tự dừng sau lỗi liên tiếp), reason
}

# Không ghi vào bảng `events` (chỉ ring buffer + stdout thô) — FR-OB-01.
NOT_PERSISTED = frozenset({"message.delta", "reasoning.delta"})
TERMINAL_RUN_EVENTS = frozenset({"run.completed", "run.failed", "run.cancelled", "run.interrupted", "run.waiting_human"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_epoch() -> str:
    return f"e-{utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}-{secrets.token_hex(2)}"


class HubEvent(BaseModel):
    v: int = SCHEMA_VERSION
    epoch: str = ""  # do bus gán
    seq: int = 0  # do bus gán
    ts: datetime = Field(default_factory=utcnow)
    type: str
    level: str = "summary"
    source: str = "hub"
    agent_id: str | None = None
    team_id: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    subagent_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def persisted(self) -> bool:
        return self.type not in NOT_PERSISTED

    def to_wire(self) -> dict[str, Any]:
        d = self.model_dump(mode="json")
        d["ts"] = iso(self.ts)
        return d


def make_event(
    type: str,
    agent_id: str | None = None,
    payload: dict[str, Any] | None = None,
    *,
    run_id: str | None = None,
    session_id: str | None = None,
    subagent_id: str | None = None,
    source: str = "hub",
    level: str | None = None,
    ts: datetime | None = None,
) -> HubEvent:
    return HubEvent(
        type=type,
        level=level or EVENT_LEVELS.get(type, "summary"),
        source=source,
        agent_id=agent_id,
        run_id=run_id,
        session_id=session_id,
        subagent_id=subagent_id,
        payload=payload or {},
        ts=ts or utcnow(),
    )


def event_json_schema() -> dict[str, Any]:
    """NFR-23: schema envelope xuất từ Pydantic."""
    return HubEvent.model_json_schema()
