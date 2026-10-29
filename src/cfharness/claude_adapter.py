"""Chuẩn hoá stdout `claude -p --output-format stream-json --verbose` thành event hub (FR-RT-04).

Mỗi run (một tiến trình `claude`) dùng MỘT `ClaudeStreamAdapter`; gọi `feed(line)` cho từng dòng.
Adapter có trạng thái: tool đang mở (để tính `duration_s`), usage luỹ kế, kết quả cuối.

Ánh xạ (BRD FR-RT-04):
  system/init (lần đầu)          → run.started + agent.status(working)  [+ isolation.violation nếu MCP lạ]
  system/init (lần 2+, cùng tiến trình: thông báo task nền mở lượt mới sau `result`)
                                 → status.update{status:"init", init_count}  [vẫn kiểm cô lập]
  system/status                  → status.update
  system/vcs_state_changed       → status.update{status:"vcs_state_changed", vcs_kind, branch}  (CLI 2.1.311+)
  system/hook_started|response   → hook.event (debug)                    [+ isolation.violation nếu hook lạ]
  system/thinking_tokens         → reasoning.progress
  system/permission_denied       → permission.denied        (spike A-01)
  system/task_*                  → task.background (detail)  (Bash chạy như task nền)
  control_response               → control.response (debug)
  control_request                → control.request (debug; {request_id, subtype, tool_name, host}) — CLI 2.1.303+ hỏi quyền qua
                                   stdout (vd can_use_tool SandboxNetworkAccess); ClaudeCliRuntime trả control_response qua stdin
  tool_progress                  → tool.progress (debug, KHÔNG ghi DB; heartbeat 30 s của tool chạy lâu, CLI 2.1.311+)
  user isReplay (stream-json input) → lần đầu (prompt) bỏ qua; các lần sau → steer.ack (M2, spike A-02)
  system/<khác>                  → raw.unknown
  stream_event message_start     → turn.start (+ usage.tick)
  stream_event content_block_start(tool_use) → tool.generating
  stream_event content_block_delta text/thinking → message.delta / reasoning.delta
  stream_event message_delta     → usage.tick (throttle)
  assistant text                 → message.complete
  assistant tool_use             → tool.start (+ subagent.spawn với Task/Agent)
  user tool_result               → tool.complete
  rate_limit_event               → ratelimit.update
  result                         → usage.tick (số chuẩn) + run.completed|run.failed + agent.status
  type lạ                        → raw.unknown (không làm hỏng run)
  dòng không phải JSON           → raw.invalid
Dòng có `parent_tool_use_id` → envelope `subagent_id`.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .events import HubEvent, make_event
from .redact import redact

# Giá tham khảo $/1M token (input, output) để ƯỚC TÍNH chi phí giữa lượt.
# Số chuẩn luôn lấy từ `result.total_cost_usd`.
PRICES: dict[str, tuple[float, float]] = {
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (1.0, 5.0),
}
DEFAULT_CONTEXT_WINDOW = 200_000
MESSAGE_COMPLETE_MAX = 8000
RESULT_PREVIEW_MAX = 500
BACKGROUND_TASK_SUBTYPES = frozenset({"task_started", "task_notification", "task_updated", "background_tasks_changed"})
KNOWN_STREAM_EVENTS = frozenset(
    {"message_start", "content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop", "ping"}
)


def _price_for(model: str | None) -> tuple[float, float]:
    m = (model or "").lower()
    for key, price in PRICES.items():
        if key in m:
            return price
    return PRICES["sonnet"]


def summarize_tool_input(name: str, inp: Any) -> str:
    """Rút gọn input của tool thành một dòng: `ls -la`, `src/x.py`…"""
    if not isinstance(inp, dict):
        return str(inp)[:80]
    for key in ("command", "file_path", "pattern", "path", "query", "url", "prompt", "description"):
        if key in inp and inp[key]:
            return str(inp[key]).replace("\n", " ")[:80]
    if inp:
        k, v = next(iter(inp.items()))
        return f"{k}={json.dumps(v, ensure_ascii=False)}"[:80]
    return ""


def tool_result_text(content: Any) -> str:
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
    return str(content or "").strip()


def classify_outcome(result: dict[str, Any]) -> str:
    subtype = str(result.get("subtype") or "")
    if subtype == "success" and not result.get("is_error"):
        return "completed"
    if "budget" in subtype:
        return "budget_exceeded"
    return "failed"


class ClaudeStreamAdapter:
    def __init__(
        self,
        agent_id: str | None,
        run_id: str | None = None,
        *,
        allowed_hooks: Iterable[str] | None = None,
        allowed_mcp_servers: Iterable[str] | None = None,
        allowed_plugin_prefixes: Iterable[str] | None = None,
        require_no_auto_memory: bool = False,
        allowed_mcp_prefixes: Iterable[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        usage_tick_interval_s: float = 1.0,
    ) -> None:
        self.agent_id = agent_id
        self.run_id = run_id
        self.allowed_hooks = set(allowed_hooks) if allowed_hooks is not None else None
        self.allowed_mcp = set(allowed_mcp_servers) if allowed_mcp_servers is not None else None
        self.allowed_plugin_prefixes = tuple(allowed_plugin_prefixes) if allowed_plugin_prefixes is not None else None
        self.allowed_mcp_prefixes = tuple(allowed_mcp_prefixes or ())  # run connector: "claude.ai " (hook guard chặn tool ở lớp dưới)
        self.require_no_auto_memory = require_no_auto_memory
        self.result_count = 0
        self.init_count = 0  # số `system/init` (lượt mới do thông báo task nền sau `result` → init thứ 2+)
        self.replay_count = 0  # số tin user CLI phát lại (`isReplay`): #1 là prompt, các tin sau là steer
        self.clock = clock
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.usage_tick_interval_s = usage_tick_interval_s
        self.started_clock = clock()

        self.session_id: str | None = None
        self.model: str | None = None
        self.init: dict[str, Any] | None = None
        self.result: dict[str, Any] | None = None
        self.context_window = DEFAULT_CONTEXT_WINDOW
        self.open_tools: dict[str, tuple[str, float]] = {}
        self.violations: list[dict[str, Any]] = []
        self._violation_keys: set[str] = set()
        self.unknown_count = 0
        self.line_count = 0
        self.last_text: str | None = None
        self.ttft_ms: int | None = None
        # usage luỹ kế
        self._done_out = 0
        self._cur_in = 0
        self._cur_out = 0
        self._cost_est = 0.0
        self._last_tick_clock: float | None = None

    @property
    def terminal(self) -> bool:
        return self.result is not None

    # ------------------------------------------------------------------ helpers
    def _ev(self, type_: str, payload: dict[str, Any], *, subagent_id: str | None = None) -> HubEvent:
        return make_event(
            type_,
            self.agent_id,
            payload,
            run_id=self.run_id,
            session_id=self.session_id,
            subagent_id=subagent_id,
            source="cli",
            ts=self.now(),
        )

    def _violation(self, kind: str, name: str, sub: str | None) -> list[HubEvent]:
        key = f"{kind}:{name}"
        if key in self._violation_keys:
            return []
        self._violation_keys.add(key)
        v = {"kind": kind, "name": name}
        self.violations.append(v)
        return [self._ev("isolation.violation", v, subagent_id=sub)]

    def _usage_tick(self, *, final: dict[str, Any] | None = None, force: bool = False) -> list[HubEvent]:
        now = self.clock()
        if final is None and not force and self._last_tick_clock is not None:
            if now - self._last_tick_clock < self.usage_tick_interval_s:
                return []
        self._last_tick_clock = now
        if final is not None:
            u = final.get("usage") or {}
            inp = u.get("input_tokens", 0)
            cache_read = u.get("cache_read_input_tokens", 0)
            cache_create = u.get("cache_creation_input_tokens", 0)
            out = u.get("output_tokens", 0)
            cost = float(final.get("total_cost_usd") or 0.0)
            return [
                self._ev(
                    "usage.tick",
                    {
                        "input_tokens": inp,
                        "cache_read_input_tokens": cache_read,
                        "cache_creation_input_tokens": cache_create,
                        "output_tokens": out,
                        "total_tokens": inp + cache_read + cache_create + out,
                        "cost_usd": cost,
                        "estimated": False,
                    },
                )
            ]
        in_p, out_p = _price_for(self.model)
        out = self._done_out + self._cur_out
        cost = self._cost_est + (self._cur_in * in_p * 0.3 + self._cur_out * out_p) / 1e6
        return [
            self._ev(
                "usage.tick",
                {
                    "input_tokens": self._cur_in,
                    "output_tokens": out,
                    "total_tokens": self._cur_in + out,
                    "cost_usd": round(cost, 6),
                    "context_tokens": self._cur_in + self._cur_out,
                    "context_window": self.context_window,
                    "estimated": True,
                },
            )
        ]

    # ------------------------------------------------------------------ main
    def feed(self, line: str | bytes | dict[str, Any]) -> list[HubEvent]:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        if isinstance(line, str):
            line = line.strip()
            if not line:
                return []
            self.line_count += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                return [self._ev("raw.invalid", {"line": line[:2000]})]
            if not isinstance(obj, dict):
                return [self._ev("raw.invalid", {"line": line[:2000]})]
        else:
            self.line_count += 1
            obj = line
        if isinstance(obj.get("session_id"), str) and obj["session_id"]:
            self.session_id = obj["session_id"]
        t = obj.get("type")
        handler = getattr(self, f"_on_{t}", None) if isinstance(t, str) else None
        if handler is None:
            return self._unknown(obj)
        return handler(obj)

    def _unknown(self, obj: dict[str, Any]) -> list[HubEvent]:
        self.unknown_count += 1
        return [self._ev("raw.unknown", {"line": obj})]

    # -- system
    def _on_system(self, obj: dict) -> list[HubEvent]:
        st = obj.get("subtype")
        sub = obj.get("parent_tool_use_id")
        if st == "init":
            return self._on_init(obj)
        if st == "status":
            return [self._ev("status.update", {"status": obj.get("status")}, subagent_id=sub)]
        if st == "vcs_state_changed":
            return [self._ev("status.update", {"status": st, "vcs_kind": obj.get("kind"), "branch": obj.get("branch")}, subagent_id=sub)]
        if st in ("hook_started", "hook_response"):
            name = obj.get("hook_name") or "?"
            payload = {
                "phase": "started" if st == "hook_started" else "response",
                "hook_name": name,
                "hook_event": obj.get("hook_event"),
                "hook_id": obj.get("hook_id"),
            }
            if st == "hook_response":
                payload.update({"outcome": obj.get("outcome"), "exit_code": obj.get("exit_code")})
            out = [self._ev("hook.event", payload, subagent_id=sub)]
            if self.allowed_hooks is not None and name not in self.allowed_hooks and obj.get("hook_event") not in self.allowed_hooks:
                out += self._violation("hook", name, sub)
            return out
        if st == "permission_denied":
            return [
                self._ev(
                    "permission.denied",
                    {k: obj.get(k) for k in ("tool_name", "tool_use_id", "decision_reason_type", "message")},
                    subagent_id=sub,
                )
            ]
        if st in BACKGROUND_TASK_SUBTYPES:
            return [self._ev("task.background", {"subtype": st, **{k: v for k, v in obj.items() if k not in ("type", "subtype", "uuid", "session_id")}}, subagent_id=sub)]
        if st in ("commands_changed", "session_title_changed"):
            return [self._ev("status.update", {"status": st, "title": obj.get("title")}, subagent_id=sub)]
        if st == "thinking_tokens":
            return [
                self._ev(
                    "reasoning.progress",
                    {"estimated_tokens": obj.get("estimated_tokens"), "delta": obj.get("estimated_tokens_delta")},
                    subagent_id=sub,
                )
            ]
        return self._unknown(obj)

    def _on_init(self, obj: dict) -> list[HubEvent]:
        self.init_count += 1
        if self.init is None:
            self.init = obj
        self.model = obj.get("model") or self.model
        mcp = []
        for s in obj.get("mcp_servers") or []:
            if isinstance(s, dict):
                mcp.append({"name": s.get("name"), "status": s.get("status")})
            else:
                mcp.append({"name": str(s), "status": None})
        payload = {
            "session_id": obj.get("session_id"),
            "cwd": obj.get("cwd"),
            "model": self.model,
            "tools": obj.get("tools") or [],
            "mcp_servers": mcp,
            "permission_mode": obj.get("permissionMode"),
            "claude_code_version": obj.get("claude_code_version"),
            "api_key_source": obj.get("apiKeySource"),
            "plugins": [p.get("name") for p in obj.get("plugins") or [] if isinstance(p, dict)],
            "skills_count": len(obj.get("skills") or []),
            "slash_commands_count": len(obj.get("slash_commands") or []),
            "memory_paths": obj.get("memory_paths"),
        }
        if self.init_count == 1:
            out = [
                self._ev("run.started", payload),
                self._ev("agent.status", {"status": "working", "current_run_id": self.run_id}),
            ]
        else:  # lượt mới trong CÙNG tiến trình (vd thông báo task nền sau `result`) — run đã bắt đầu, chỉ báo trạng thái
            out = [self._ev("status.update", {"status": "init", "init_count": self.init_count, "model": self.model})]
        if self.allowed_mcp is not None:
            for s in mcp:
                known = bool(self.allowed_mcp_prefixes) and str(s["name"]).startswith(self.allowed_mcp_prefixes)
                if s["name"] not in self.allowed_mcp and not known:
                    out += self._violation("mcp_server", str(s["name"]), None)
        if self.allowed_plugin_prefixes is not None:
            for name in payload["plugins"]:
                if not str(name).startswith(self.allowed_plugin_prefixes):
                    out += self._violation("plugin", str(name), None)
        if self.require_no_auto_memory and obj.get("memory_paths"):
            out += self._violation("auto_memory", json.dumps(obj["memory_paths"], ensure_ascii=False), None)
        return out

    # -- stream_event (partial messages)
    def _on_stream_event(self, obj: dict) -> list[HubEvent]:
        ev = obj.get("event") or {}
        et = ev.get("type")
        sub = obj.get("parent_tool_use_id")
        if et not in KNOWN_STREAM_EVENTS:
            return self._unknown(obj)
        if et == "message_start":
            msg = ev.get("message") or {}
            self.model = msg.get("model") or self.model
            u = msg.get("usage") or {}
            self._cur_in = u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0)
            self._cur_out = u.get("output_tokens", 0)
            if self.ttft_ms is None:
                self.ttft_ms = int((self.clock() - self.started_clock) * 1000)
            return [self._ev("turn.start", {"model": self.model, "ttft_ms": self.ttft_ms}, subagent_id=sub), *self._usage_tick()]
        if et == "content_block_start":
            cb = ev.get("content_block") or {}
            if cb.get("type") == "tool_use":
                return [self._ev("tool.generating", {"tool_id": cb.get("id"), "name": cb.get("name")}, subagent_id=sub)]
            return []
        if et == "content_block_delta":
            d = ev.get("delta") or {}
            dt = d.get("type")
            if dt == "text_delta" and d.get("text"):
                return [self._ev("message.delta", {"text": d["text"]}, subagent_id=sub)]
            if dt == "thinking_delta" and (d.get("thinking") or d.get("estimated_tokens")):
                return [
                    self._ev("reasoning.delta", {"text": d.get("thinking", ""), "estimated_tokens": d.get("estimated_tokens")}, subagent_id=sub)
                ]
            return []
        if et == "message_delta":
            u = ev.get("usage") or {}
            if "output_tokens" in u:
                self._cur_out = u["output_tokens"]
            tick = self._usage_tick(force=True)
            in_p, out_p = _price_for(self.model)
            self._cost_est += (self._cur_in * in_p * 0.3 + self._cur_out * out_p) / 1e6
            self._done_out += self._cur_out
            self._cur_in = self._cur_out = 0
            return tick
        return []

    # -- assistant / user
    def _on_assistant(self, obj: dict) -> list[HubEvent]:
        msg = obj.get("message") or {}
        sub = obj.get("parent_tool_use_id")
        out: list[HubEvent] = []
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            bt = block.get("type")
            if bt == "tool_use":
                tid = block.get("id")
                if tid in self.open_tools:
                    continue
                name = block.get("name", "?")
                inp = block.get("input") or {}
                self.open_tools[tid] = (name, self.clock())
                out.append(
                    self._ev(
                        "tool.start",
                        {"tool_id": tid, "name": name, "input": redact(inp), "args_preview": summarize_tool_input(name, redact(inp))},
                        subagent_id=sub,
                    )
                )
                if name in ("Task", "Agent"):
                    out.append(
                        self._ev(
                            "subagent.spawn",
                            {
                                "parent_tool_use_id": tid,
                                "type": inp.get("subagent_type", "subagent"),
                                "description": inp.get("description") or str(inp.get("prompt", ""))[:80],
                            },
                            subagent_id=sub,
                        )
                    )
            elif bt == "text" and block.get("text"):
                text = block["text"]
                if sub is None:
                    self.last_text = text
                out.append(
                    self._ev(
                        "message.complete",
                        {"text": text[:MESSAGE_COMPLETE_MAX], "truncated": len(text) > MESSAGE_COMPLETE_MAX, "message_id": msg.get("id")},
                        subagent_id=sub,
                    )
                )
        return out

    def _on_user(self, obj: dict) -> list[HubEvent]:
        msg = obj.get("message") or {}
        content = msg.get("content")
        if obj.get("isReplay"):
            return self._on_replay(obj, content)
        if not isinstance(content, list):
            return []
        sub = obj.get("parent_tool_use_id")
        out: list[HubEvent] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id")
            name, start = self.open_tools.pop(tid, ("?", self.clock()))
            text = tool_result_text(block.get("content"))
            out.append(
                self._ev(
                    "tool.complete",
                    {
                        "tool_id": tid,
                        "name": name,
                        "duration_s": round(max(0.0, self.clock() - start), 2),
                        "is_error": bool(block.get("is_error")),
                        "result_preview": redact(text[:RESULT_PREVIEW_MAX]),
                    },
                    subagent_id=sub,
                )
            )
        return out

    def _on_replay(self, obj: dict, content: Any) -> list[HubEvent]:
        """Tin user được CLI phát lại (`--replay-user-messages`). Tin đầu là prompt; tin sau là steer đã được
        đưa vào ngữ cảnh → `steer.ack` (spike A-02: phát lại ĐÚNG lúc tin được tiêu thụ)."""
        if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return []
        self.replay_count += 1
        if self.replay_count == 1:
            return []
        if isinstance(content, list):
            text = "\n".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = str(content or "")
        return [self._ev("steer.ack", {"text": redact(text[:RESULT_PREVIEW_MAX]), "uuid": obj.get("uuid"), "index": self.replay_count - 1})]

    def _on_control_response(self, obj: dict) -> list[HubEvent]:
        return [self._ev("control.response", {"response": obj.get("response")})]

    def _on_control_request(self, obj: dict) -> list[HubEvent]:
        """CLI hỏi quyền qua stdout (CLI 2.1.303+: `can_use_tool` SandboxNetworkAccess khi Bash trong sandbox ra host ngoài
        allowlist). Chỉ ghi lại — câu trả lời do `ClaudeCliRuntime` gửi qua stdin sau khi guard quyết."""
        req = obj.get("request") if isinstance(obj.get("request"), dict) else {}
        inp = req.get("input") if isinstance(req.get("input"), dict) else {}
        payload = {"request_id": obj.get("request_id"), "subtype": req.get("subtype"), "tool_name": req.get("tool_name")}
        if "host" in inp:
            payload["host"] = str(inp.get("host"))[:255]
        return [self._ev("control.request", payload)]

    def _on_tool_progress(self, obj: dict) -> list[HubEvent]:
        """Heartbeat của tool chạy lâu (Bash, 30 s/lần). `parent_tool_use_id` ở đây là CHÍNH tool đang chạy (không phải subagent)."""
        return [self._ev("tool.progress", {"tool_id": obj.get("parent_tool_use_id") or obj.get("tool_use_id"), "name": obj.get("tool_name"),
                                           "elapsed_s": obj.get("elapsed_time_seconds"), "heartbeat": bool(obj.get("heartbeat"))})]

    def _on_rate_limit_event(self, obj: dict) -> list[HubEvent]:
        return [self._ev("ratelimit.update", {"rate_limit_info": obj.get("rate_limit_info") or {}})]

    # -- result
    def _on_result(self, obj: dict) -> list[HubEvent]:
        self.result = obj
        self.result_count += 1
        cost_basis = None
        for mu in (obj.get("modelUsage") or {}).values():
            if isinstance(mu, dict):
                if mu.get("contextWindow"):
                    self.context_window = mu["contextWindow"]
                cost_basis = cost_basis or mu.get("costBasis")
        outcome = classify_outcome(obj)
        summary = obj.get("result") if isinstance(obj.get("result"), str) else self.last_text
        payload = {
            "outcome": outcome,
            "subtype": obj.get("subtype"),
            "is_error": bool(obj.get("is_error")),
            "cost_usd": float(obj.get("total_cost_usd") or 0.0),
            "cost_basis": cost_basis or "unknown",
            "usage": obj.get("usage") or {},
            "model_usage": obj.get("modelUsage") or {},
            "duration_ms": obj.get("duration_ms"),
            "duration_api_ms": obj.get("duration_api_ms"),
            "num_turns": obj.get("num_turns"),
            "summary": summary,
            "permission_denials": obj.get("permission_denials") or [],
            "terminal_reason": obj.get("terminal_reason"),
            "stop_reason": obj.get("stop_reason"),
            "subagent_stats": obj.get("subagent_stats"),
            "errors": obj.get("errors"),
        }
        out = self._usage_tick(final=obj)
        out.append(self._ev("run.completed" if outcome == "completed" else "run.failed", payload))
        out.append(self._ev("agent.status", {"status": "idle" if outcome != "failed" else "error", "current_run_id": None}))
        return out


def adapt_lines(agent_id: str | None, lines: Iterable[str], **kw: Any) -> list[HubEvent]:
    """Tiện ích: chạy cả file/list dòng qua một adapter mới."""
    ad = ClaudeStreamAdapter(agent_id, **kw)
    out: list[HubEvent] = []
    for line in lines:
        out.extend(ad.feed(line))
    return out
