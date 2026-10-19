"""Steer giữa chừng (FR-RT-06, spike A-02) — M2.

`steer(hub, ref, text)` gửi một tin user vào stdin stream-json của tiến trình `claude` ĐANG chạy:
- CLI chèn tin ở ranh giới gọi model kế tiếp (sau `tool_result` đang chờ) trong CÙNG lượt → một `result`.
- Hub phát `user.message` (ai gửi, kênh, nội dung đã che secret) ngay khi ghi vào stdin; adapter phát `steer.ack`
  khi CLI phát lại tin (`isReplay`) — tức lúc tin thật sự vào ngữ cảnh.
- M0 đóng stdin sau `result` → run đã qua `result` (hoặc không chạy) KHÔNG nhận steer: báo `Conflict`
  kèm gợi ý `cfharness run continue <run> "…"`. Runtime giữ stdin mở tới khi tin steer đang chờ được phát lại.

`ref` là id run (tiền tố được) hoặc id agent (→ run đang chạy của agent).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .errors import Conflict, HubError, NotFound
from .events import make_event
from .redact import redact
from .runtime import RuntimeError_

if TYPE_CHECKING:
    from .hub import Hub

STEER_MAX_CHARS = 8000


def resolve_run(hub: "Hub", ref: str) -> dict[str, Any]:
    run = hub.store.find_run(ref)
    if run is not None:
        return run
    agent = hub.store.get_agent(ref)
    if agent is not None:
        for ar in hub.runs.active.values():
            if ar.agent_id == agent["id"] and ar.handle is not None:
                return hub.store.get_run(ar.run_id)  # type: ignore[return-value]
        if agent.get("current_run_id"):
            r = hub.store.get_run(agent["current_run_id"])
            if r:
                return r
        raise Conflict(f"agent '{ref}' không có run đang chạy")
    raise NotFound(f"không tìm thấy run hoặc agent '{ref}'")


async def steer(hub: "Hub", ref: str, text: str, *, by: str = "user", channel: str = "cli") -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise HubError("nội dung steer rỗng")
    if len(text) > STEER_MAX_CHARS:
        raise HubError(f"nội dung steer quá dài ({len(text)} > {STEER_MAX_CHARS} ký tự)")
    run = resolve_run(hub, ref)
    rid = run["id"]
    hint = f'— dùng `cfharness run continue {rid} "<tin>"` để tiếp tục hội thoại ở run mới'
    if run["status"] != "running":
        raise Conflict(f"run {rid} không đang chạy (status={run['status']}) {hint}")
    ar = hub.runs.active.get(rid)
    if ar is None or ar.handle is None or ar.runtime is None:
        raise Conflict(f"run {rid} chưa khởi chạy xong tiến trình — thử lại sau giây lát")
    if not ar.runtime.capabilities().get("steer"):
        raise Conflict(f"runtime '{ar.runtime.name}' không hỗ trợ steer (cần runtime.claude.input_format=stream-json) {hint}")
    try:
        await ar.runtime.send(ar.handle, text)
    except (RuntimeError_, NotImplementedError, BrokenPipeError, ConnectionResetError, OSError) as e:
        raise Conflict(f"run {rid} không nhận thêm tin: {e} {hint}") from None
    source = channel.split(":", 1)[0] if channel.split(":", 1)[0] in ("cli", "tui", "mcp", "hub") else "hub"
    ev = hub.publish(
        make_event(
            "user.message",
            run["agent_id"],
            {"text": redact(text), "by": by, "channel": channel, "steer": True},
            run_id=rid,
            session_id=run.get("session_id"),
            source=source,
        )
    )
    n = int(run.get("steer_count") or 0) + 1
    hub.store.update_run(rid, {"steer_count": n})
    return {"run_id": rid, "agent_id": run["agent_id"], "sent": True, "seq": ev.seq, "epoch": ev.epoch, "steer_count": n}
