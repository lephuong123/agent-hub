"""Thao tác job (FR-SA-JOB-01, 02): xem, thử lại từ bước lỗi, huỷ (kèm dọn)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .ops import OpError, op
from .ptypes import ObjId, Params


class JobListP(Params):
    target: str | None = Field(default=None, max_length=200)
    state: Literal["queued", "awaiting_approval", "running", "succeeded", "failed", "cancelled"] | None = None
    limit: int = Field(default=100, ge=1, le=500)


@op("job.list", group="job", risk="read", params=JobListP)
def job_list(ctx, p: JobListP) -> list[dict[str, Any]]:
    """Danh sách job."""
    return ctx.state.jobs.list(target=p.target, state=p.state, limit=p.limit)


class JobIdP(Params):
    id: ObjId


@op("job.get", group="job", risk="read", params=JobIdP)
def job_get(ctx, p: JobIdP) -> dict[str, Any]:
    """Chi tiết job + từng bước + log đã che."""
    j = ctx.state.jobs.get(p.id)
    if not j:
        raise OpError("not_found", "không có job")
    return j


class RetryP(JobIdP):
    secrets: dict[str, str] = Field(default_factory=dict, max_length=5)


@op("job.retry", group="job", risk="normal", params=RetryP, target=lambda p: p.id)
def job_retry(ctx, p: RetryP) -> dict[str, Any]:
    """Thử lại từ bước lỗi (bước đã xong được bỏ qua)."""
    try:
        ctx.state.jobs.retry(p.id, secrets={k: v for k, v in p.secrets.items() if k in ("claude_token",)} or None)
    except ValueError as e:
        raise OpError("conflict", str(e)) from None
    return {"ok": True}


class JobCancelP(JobIdP):
    cleanup: bool = False


@op("job.cancel", group="job", risk="normal", params=JobCancelP, target=lambda p: p.id)
def job_cancel(ctx, p: JobCancelP) -> dict[str, Any]:
    """Huỷ job; `cleanup` = chạy `undo` các bước đã làm theo thứ tự ngược ("Huỷ và dọn")."""
    try:
        return ctx.state.jobs.cancel(p.id, cleanup=p.cleanup)
    except ValueError as e:
        raise OpError("conflict", str(e)) from None
