"""Chuyển stdout `claude -p --output-format stream-json --verbose [--include-partial-messages]`
thành event của hub.

Mỗi lượt chạy (một tiến trình `claude`) dùng MỘT `ClaudeStreamAdapter`; gọi `feed(line)` cho từng
dòng JSON đọc được từ stdout. Adapter có trạng thái (theo dõi tool đang chạy, block nào đã stream
delta để không nhân đôi chữ khi gặp message `assistant` đầy đủ, usage luỹ kế…).

Bảng ánh xạ (xem thêm README):
  system/init                          → turn.start (+ agent.status working)
  system/status                        → (bỏ qua; chỉ là "requesting")
  system/hook_* , rate_limit_event     → bỏ qua (hook của máy — hub nên cô lập)
  stream_event content_block_delta
      text_delta                       → message.delta
      thinking_delta                   → reasoning.delta
  stream_event message_start/delta     → usage.tick (ước tính)
  assistant (tool_use)                 → tool.start
  assistant (text/thinking) khi KHÔNG có partial messages → message.delta / reasoning.delta
  user (tool_result)                   → tool.complete
  result                               → usage.tick (chuẩn) + turn.complete + agent.status idle|error
                                          (+ error nếu is_error)

Phần này được viết để TÁI DÙNG cho bản thật.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .events import HubEvent, make_event

# Giá tham khảo $/1M token (input, output) để ƯỚC TÍNH chi phí giữa lượt.
# Số chuẩn luôn lấy từ `result.total_cost_usd`.
PRICES: dict[str, tuple[float, float]] = {
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}
DEFAULT_CONTEXT_WINDOW = 200_000


def _price_for(model: str | None) -> tuple[float, float]:
    m = (model or "").lower()
    for key, price in PRICES.items():
        if key in m:
            return price
    return PRICES["sonnet"]


def summarize_tool_input(name: str, inp: dict[str, Any]) -> str:
    """Rút gọn input của tool thành một dòng cho UI: `Bash ls`, `Read src/x.py`…"""
    if not isinstance(inp, dict):
        return str(inp)[:80]
    for key in ("command", "file_path", "pattern", "path", "query", "url", "prompt", "description"):
        if key in inp and inp[key]:
            return str(inp[key]).replace("\n", " ")[:80]
    if inp:
        k, v = next(iter(inp.items()))
        return f"{k}={json.dumps(v, ensure_ascii=False)}"[:80]
    return ""


def summarize_tool_result(content: Any, limit: int = 120) -> str:
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif isinstance(c, dict):
                parts.append(f"[{c.get('type', '?')}]")
            else:
                parts.append(str(c))
        content = "\n".join(parts)
    text = str(content or "").strip()
    lines = text.splitlines()
    first = lines[0] if lines else ""
    more = f" (+{len(lines) - 1} dòng)" if len(lines) > 1 else ""
    if len(first) > limit:
        first = first[: limit - 1] + "…"
    return first + more


class ClaudeStreamAdapter:
    def __init__(
        self,
        agent_id: str,
        run_id: str | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        task: str | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.run_id = run_id
        self.clock = clock
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.task = task
        self.session_id: str | None = None
        self.model: str | None = None
        self.context_window = DEFAULT_CONTEXT_WINDOW
        self.started_at: float | None = None
        # tool_use_id -> (name, input, start_clock)
        self.open_tools: dict[str, tuple[str, dict, float]] = {}
        # message id đã nhận delta qua stream_event (để không nhân đôi khi gặp `assistant`)
        self._streamed_text: set[str] = set()
        self._streamed_thinking: set[str] = set()
        # usage luỹ kế qua các message của lượt
        self._done_msgs_out = 0
        self._cur_msg_in = 0
        self._cur_msg_out = 0
        self._cur_ctx = 0
        self._cost_est = 0.0
        self._last_tps_clock: float | None = None
        self._last_tps_out = 0
        self.tps = 0.0

    # ------------------------------------------------------------------ helpers
    def _ev(self, type_: str, payload: dict[str, Any]) -> HubEvent:
        return make_event(
            type_,  # type: ignore[arg-type]
            self.agent_id,
            payload,
            session_id=self.session_id,
            run_id=self.run_id,
            ts=self.now(),
        )

    def _usage_tick(self, *, final: dict[str, Any] | None = None) -> HubEvent:
        if final is not None:
            usage = final.get("usage") or {}
            inp = (
                usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
            )
            out = usage.get("output_tokens", 0)
            cost = float(final.get("total_cost_usd") or 0.0)
            estimated = False
        else:
            inp = self._cur_msg_in
            out = self._done_msgs_out + self._cur_msg_out
            in_p, out_p = _price_for(self.model)
            cost = self._cost_est + (self._cur_msg_in * in_p * 0.3 + self._cur_msg_out * out_p) / 1e6
            estimated = True
        # tok/s theo output token
        now = self.clock()
        if self._last_tps_clock is not None and now > self._last_tps_clock:
            d_out = out - self._last_tps_out
            if d_out >= 0:
                self.tps = d_out / (now - self._last_tps_clock)
        self._last_tps_clock, self._last_tps_out = now, out
        ctx = self._cur_ctx
        return self._ev(
            "usage.tick",
            {
                "input_tokens": inp,
                "output_tokens": out,
                "total_tokens": inp + out,
                "cost_usd": cost if not estimated else round(cost, 6),
                "context_tokens": ctx,
                "context_window": self.context_window,
                "context_percent": round(100 * ctx / self.context_window, 1) if self.context_window else 0,
                "tps": round(self.tps, 1),
                "estimated": estimated,
            },
        )

    # ------------------------------------------------------------------ main
    def feed(self, line: str | dict[str, Any]) -> list[HubEvent]:
        if isinstance(line, str):
            line = line.strip()
            if not line:
                return []
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                return [self._ev("error", {"message": f"Dòng không phải JSON: {line[:80]}", "fatal": False})]
        else:
            obj = line
        if obj.get("session_id"):
            self.session_id = obj["session_id"]
        t = obj.get("type")
        handler = getattr(self, f"_on_{t}", None)
        if handler is None:
            return []
        return handler(obj)

    def _on_system(self, obj: dict) -> list[HubEvent]:
        if obj.get("subtype") != "init":
            return []
        self.model = obj.get("model")
        self.started_at = self.clock()
        payload = {
            "model": self.model,
            "task": self.task,
            "tools": obj.get("tools", []),
            "mcp_servers": [
                s.get("name") if isinstance(s, dict) else s for s in obj.get("mcp_servers", [])
            ],
            "cwd": obj.get("cwd"),
            "permission_mode": obj.get("permissionMode"),
            "claude_code_version": obj.get("claude_code_version"),
        }
        return [
            self._ev("turn.start", payload),
            self._ev("agent.status", {"status": "working", "detail": self.task}),
        ]

    def _on_stream_event(self, obj: dict) -> list[HubEvent]:
        ev = obj.get("event") or {}
        et = ev.get("type")
        msg_id = obj.get("api_message_id")
        if et == "message_start":
            msg = ev.get("message") or {}
            self.model = msg.get("model") or self.model
            u = msg.get("usage") or {}
            self._cur_msg_in = (
                u.get("input_tokens", 0)
                + u.get("cache_creation_input_tokens", 0)
                + u.get("cache_read_input_tokens", 0)
            )
            self._cur_msg_out = u.get("output_tokens", 0)
            self._cur_ctx = self._cur_msg_in
            return [self._usage_tick()]
        if et == "content_block_delta":
            d = ev.get("delta") or {}
            dt = d.get("type")
            if dt == "text_delta":
                if msg_id:
                    self._streamed_text.add(msg_id)
                text = d.get("text", "")
                return [self._ev("message.delta", {"text": text})] if text else []
            if dt == "thinking_delta":
                if msg_id:
                    self._streamed_thinking.add(msg_id)
                text = d.get("thinking", "")
                est = d.get("estimated_tokens")
                if text or est:
                    return [self._ev("reasoning.delta", {"text": text, "estimated_tokens": est})]
            return []
        if et == "message_delta":
            u = ev.get("usage") or {}
            if "output_tokens" in u:
                self._cur_msg_out = u["output_tokens"]
                self._cur_ctx = self._cur_msg_in + self._cur_msg_out
            tick = self._usage_tick()
            # message kết thúc → chốt phần output và chi phí ước tính
            in_p, out_p = _price_for(self.model)
            self._cost_est += (self._cur_msg_in * in_p * 0.3 + self._cur_msg_out * out_p) / 1e6
            self._done_msgs_out += self._cur_msg_out
            self._cur_msg_in = self._cur_msg_out = 0
            return [tick]
        return []

    def _on_assistant(self, obj: dict) -> list[HubEvent]:
        msg = obj.get("message") or {}
        msg_id = msg.get("id")
        out: list[HubEvent] = []
        for block in msg.get("content") or []:
            bt = block.get("type")
            if bt == "tool_use":
                tid = block.get("id")
                if tid in self.open_tools:
                    continue
                name = block.get("name", "?")
                inp = block.get("input") or {}
                self.open_tools[tid] = (name, inp, self.clock())
                out.append(
                    self._ev(
                        "tool.start",
                        {
                            "tool_use_id": tid,
                            "name": name,
                            "input": inp,
                            "summary": summarize_tool_input(name, inp),
                            "parent_tool_use_id": obj.get("parent_tool_use_id"),
                        },
                    )
                )
                if name in ("Task", "Agent"):
                    out.append(
                        self._ev(
                            "subagent.spawn",
                            {
                                "subagent_id": tid,
                                "name": inp.get("subagent_type", "subagent"),
                                "task": inp.get("description") or inp.get("prompt", "")[:80],
                            },
                        )
                    )
            elif bt == "text" and msg_id not in self._streamed_text and block.get("text"):
                out.append(self._ev("message.delta", {"text": block["text"]}))
            elif bt == "thinking" and msg_id not in self._streamed_thinking and block.get("thinking"):
                out.append(self._ev("reasoning.delta", {"text": block["thinking"]}))
        return out

    def _on_user(self, obj: dict) -> list[HubEvent]:
        msg = obj.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            return []
        out: list[HubEvent] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id")
            name, _inp, start = self.open_tools.pop(tid, ("?", {}, self.clock()))
            out.append(
                self._ev(
                    "tool.complete",
                    {
                        "tool_use_id": tid,
                        "name": name,
                        "duration_s": round(max(0.0, self.clock() - start), 2),
                        "is_error": bool(block.get("is_error")),
                        "summary": summarize_tool_result(block.get("content")),
                    },
                )
            )
        return out

    def _on_result(self, obj: dict) -> list[HubEvent]:
        for mu in (obj.get("modelUsage") or {}).values():
            if mu.get("contextWindow"):
                self.context_window = mu["contextWindow"]
        is_error = bool(obj.get("is_error")) or obj.get("subtype") != "success"
        tick = self._usage_tick(final=obj)
        p = tick.payload
        out = [tick]
        if is_error:
            msg = obj.get("result") or obj.get("subtype") or "lỗi không rõ"
            out.append(self._ev("error", {"message": str(msg), "code": obj.get("subtype"), "fatal": True}))
        out.append(
            self._ev(
                "turn.complete",
                {
                    "status": "error" if is_error else "success",
                    "subtype": obj.get("subtype"),
                    "cost_usd": p["cost_usd"],
                    "input_tokens": p["input_tokens"],
                    "output_tokens": p["output_tokens"],
                    "total_tokens": p["total_tokens"],
                    "duration_ms": obj.get("duration_ms"),
                    "num_turns": obj.get("num_turns"),
                    "result": obj.get("result"),
                    "permission_denials": obj.get("permission_denials", []),
                },
            )
        )
        out.append(self._ev("agent.status", {"status": "error" if is_error else "idle"}))
        return out


def adapt_lines(agent_id: str, lines: list[str], **kw: Any) -> list[HubEvent]:
    """Tiện ích: chạy cả file/list dòng qua một adapter mới."""
    ad = ClaudeStreamAdapter(agent_id, **kw)
    out: list[HubEvent] = []
    for line in lines:
        out.extend(ad.feed(line))
    return out
