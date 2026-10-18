"""Thông báo Slack (FR-SG-09, Q-16).

- Gửi khi: `approval.open`, `approval.waiting_human`, `approval.reminder`, `guard.blocked`, `run.repeated_failure`,
  `learning.rolled_back`, `learning.digest` (M4-B, digest 08:00), `golden.suggested` khi L3 hoãn vì thiếu golden task
  (M4-B, Q-23) (lọc theo `notify.slack.events`).
- Cấu hình `notify.slack`: `webhook_secret` (tên secret chứa Incoming Webhook URL, mặc định `SLACK_WEBHOOK_URL`) hoặc
  `bot_token_secret` (mặc định `SLACK_BOT_TOKEN`) + `channel`. `webhook_url` đặt thẳng trong config chỉ để thử.
  Chưa có URL/token → tắt êm (không lỗi, không event).
- Nội dung: agent, run, thao tác ĐÃ CHE (tool MCP chỉ tên — không đưa dữ liệu MCP nội bộ/secret), lý do phân loại,
  lệnh duyệt `agent-hub approve <id>`.
- Gửi lỗi → log + event `notify.failed` + thử lại có backoff (`backoff_base_s` × 2^n, tối đa `max_attempts`);
  request vẫn chờ, không ảnh hưởng run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from ..events import HubEvent
from ..redact import redact_str
from .base import QueuedNotifier, clip as _clip

log = logging.getLogger("agent_hub.notify")


def format_message(ev: HubEvent) -> str | None:
    p = ev.payload or {}
    agent, run = ev.agent_id or "-", ev.run_id or "-"
    rid = p.get("request_id")
    if ev.type == "approval.open":
        return (
            f":warning: *agent-hub* — agent `{agent}` muốn chạy thao tác PHÁ HUỶ, đang giữ chờ duyệt\n"
            f"• run `{run}` · request `{rid}`\n• thao tác: `{_clip(p.get('preview'))}`\n"
            f"• lý do: {_clip(p.get('reason'), 200)} ({p.get('matched_rule')})\n"
            f"• duyệt: `agent-hub approve {rid}` · từ chối: `agent-hub deny {rid}` (hoặc TUI / MCP `hub_approve`)"
        )
    if ev.type == "approval.waiting_human":
        return (
            f":double_vertical_bar: *agent-hub* — request `{rid}` của agent `{agent}` chưa có người duyệt → run `{run}` "
            f"chuyển *waiting_human* (đã dừng tiến trình, giữ khoá repo, KHÔNG tự từ chối)\n"
            f"• thao tác: `{_clip(p.get('preview'))}`\n• duyệt: `agent-hub approve {rid}`"
        )
    if ev.type == "approval.reminder":
        mins = int(float(p.get("wait_s") or 0) // 60)
        return (
            f":bell: *agent-hub* — nhắc lần {p.get('reminder_count')}: request `{rid}` (agent `{agent}`, run `{run}`) "
            f"vẫn chờ người duyệt sau {mins} phút\n• thao tác: `{_clip(p.get('preview'))}`\n• duyệt: `agent-hub approve {rid}`"
        )
    if ev.type == "guard.blocked":
        return (
            f":no_entry: *agent-hub* — guard chặn agent `{agent}` (run `{run}`): {_clip(p.get('detail') or p.get('reason'), 200)}\n"
            f"• thao tác: `{_clip(p.get('preview'))}`"
        )
    if ev.type == "guard.env_tamper":
        files = (p.get("added") or []) + (p.get("changed") or [])
        return (f":rotating_light: *agent-hub* — file chạy-khi-khởi-động (.pth/sitecustomize) trong môi trường Python của hub bị thêm/sửa:\n"
                f"• `{_clip(', '.join(files), 400)}`\n• nếu do bạn cài gói: `agent-hub guard integrity accept`")
    if ev.type == "guard.tamper_detected":
        what = p.get("target")
        files = (p.get("changed") or []) + [m.get("path") for m in (p.get("moved") or []) if isinstance(m, dict)] + (p.get("keys") or [])
        act = "đã KHÔI PHỤC bản chuẩn" if (p.get("restored") or p.get("moved")) else "chỉ phát hiện (kiểm tra tay)"
        return (
            f":rotating_light: *agent-hub* — `{what}` bị ghi NGOÀI RPC ({act})\n• chi tiết: `{_clip(', '.join(str(f) for f in files if f) or str(p), 300)}`\n"
            f"• bản bị sửa lưu ở `trash/tamper/`; nếu chính bạn sửa tay: `agent-hub guard integrity accept`"
        )
    if ev.type == "auth.denied":
        return (
            f":no_entry: *agent-hub* — từ chối `{p.get('method')}` (chỉ dành cho người) từ pid {p.get('pid')} "
            f"(run `{run}`, agent `{agent}`; bằng chứng: {_clip(', '.join(p.get('evidence') or []) or 'thiếu token', 200)})"
        )
    if ev.type == "run.repeated_failure":
        return (
            f":x: *agent-hub* — agent `{agent}` lỗi {p.get('count')} run liên tiếp (gần nhất `{run}`: "
            f"{_clip(p.get('error'), 200)})"
        )
    if ev.type == "learning.rolled_back":
        return f":leftwards_arrow_with_hook: *agent-hub* — rollback học của agent `{agent}`: {_clip(p.get('reason'), 200)}"
    # M4-B: digest hằng ngày (FR-LN-13) — nội dung do learning_ops/digest.py dựng (đã tóm tắt, không chứa dữ liệu MCP)
    if ev.type == "learning.digest":
        if not p.get("send") or not p.get("text"):
            return None
        t = redact_str(str(p["text"]))
        return t if len(t) <= 3900 else t[:3900] + "\n…(xem đầy đủ: `agent-hub digest show`)"
    # M4-B: hồi quy sau thay đổi của NGƯỜI — không tự rollback, chỉ cảnh báo
    if ev.type == "regression.detected":
        if not p.get("human_change"):
            return None
        return (f":warning: *agent-hub* — agent `{agent}`: {_clip(p.get('reason'), 300)}\n"
                f"• thay đổi này do NGƯỜI tạo nên hub không tự rollback; nếu muốn: `{p.get('hint')}`")
    # M4-B: thay đổi L3 bị hoãn vì agent chưa có golden task (Q-23) → nhắc người chọn golden task
    if ev.type == "golden.suggested":
        if p.get("reason") != "deferred_no_eval":
            return None
        lines = [f":bulb: *agent-hub* — thay đổi cấu hình/L3 của agent `{agent}` bị HOÃN (`deferred_no_eval`): agent chưa có golden task.",
                 "Chọn golden task để vòng học áp dụng được (agent không tự tạo — Q-07):"]
        for c in (p.get("candidates") or [])[:3]:
            lines.append(f"• run `{c.get('run_id')}` ({c.get('reason')}): {_clip(c.get('prompt_preview'), 100)} → `agent-hub golden from-run {c.get('run_id')} --judge \"<rubric>\"`")
        if not p.get("candidates"):
            lines.append(f"• viết tay: `agent-hub golden add {agent} --prompt \"…\" --regex \"…\"`")
        return "\n".join(lines)
    return None


class SlackNotifier(QueuedNotifier):
    """Kênh Slack: hàng đợi/backoff/`notify.sent|failed` ở `QueuedNotifier` (notify/base.py)."""

    channel = "slack"

    def target(self) -> dict[str, str] | None:
        c = self.cfg()
        if not c.get("enabled", True):
            return None
        url = c.get("webhook_url") or self._secret(c.get("webhook_secret"))
        if url:
            return {"kind": "webhook", "url": str(url)}
        token = self._secret(c.get("bot_token_secret"))
        if token and c.get("channel"):
            return {"kind": "bot", "token": token, "channel": str(c["channel"]), "api": str(c.get("api_base") or "https://slack.com/api")}
        return None

    def render(self, ev: HubEvent) -> str | None:
        return format_message(ev)

    # ------------------------------------------------------------------ send
    def _post(self, target: dict[str, str], text: str) -> None:
        timeout = float(self.cfg().get("timeout_s") or 10)
        if target["kind"] == "webhook":
            req = urllib.request.Request(target["url"], data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace").strip()
                if r.status != 200 or (body and body != "ok"):
                    raise RuntimeError(f"webhook trả {r.status}: {body[:200]}")
            return
        req = urllib.request.Request(
            target["api"].rstrip("/") + "/chat.postMessage",
            data=json.dumps({"channel": target["channel"], "text": text}).encode(),
            headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {target['token']}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read() or b"{}")
            if not data.get("ok"):
                raise RuntimeError(f"chat.postMessage lỗi: {data.get('error')}")

    async def _send(self, target: dict[str, Any], ev: HubEvent, msg: Any) -> dict[str, Any]:
        await asyncio.to_thread(self._post, target, msg)
        return {"kind": target["kind"]}

    async def send_now(self, text: str) -> dict[str, Any]:
        """Gửi một tin (cho `agent-hub notify test`) — không thử lại."""
        t = self.target()
        if t is None:
            return {"sent": False, "reason": "Slack chưa cấu hình (notify.slack.webhook_secret / bot_token_secret+channel)"}
        try:
            await asyncio.to_thread(self._post, t, text)
            return {"sent": True, "kind": t["kind"]}
        except Exception as e:
            return {"sent": False, "reason": _clip(e, 300)}
