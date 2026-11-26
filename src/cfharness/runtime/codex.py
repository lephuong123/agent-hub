"""`CodexRuntime`: OpenAI Codex CLI qua `codex app-server` (JSON-RPC stdio) — đăng nhập ChatGPT của CHÍNH người dùng (`codex login`).

Đo thật: docs/spikes/codex-grok/codex/REPORT-codex.md (X-07). Trình tự:
initialize → initialized → thread/start (hoặc thread/resume) → turn/start → chờ notification `turn/completed`.
- Hỏi quyền: `item/commandExecution/requestApproval`, `item/fileChange/requestApproval`, `mcpServer/elicitation/request`
  (`_meta.codex_approval_kind = "mcp_tool_call"`) → guard (`RpcRuntime.ask`); trả `accept` / `decline` — không bao giờ
  `acceptForSession` hay sửa execpolicy (quyết định của hub là theo từng lần).
- Sandbox OS: `windows.sandbox="mxc"` BẮT BUỘC (đo X-09: thiếu mxc thì lệnh hub đã *accept* chạy NGOÀI sandbox, kể cả read-only;
  javis-os dùng "unelevated" — đo X-02: treo + ghi được ra ngoài cwd → không dùng).
- Cô lập: `features.apps=false` (connector ChatGPT đi ngoài proxy mạng của sandbox), `project_doc_max_bytes=0` (không nạp AGENTS.md
  của repo), MCP của người dùng tắt từng cái (`config/read` → `mcp_servers.<tên>.enabled=false`, đo X-11).
  `~/.codex/AGENTS.md` VẪN được nạp (không có cờ tắt, đo X-11f) → `cfharness doctor` cảnh báo.
- Chi phí: gói ChatGPT không tính tiền theo lượt → `total_cost_usd = 0`; token vẫn ghi đủ.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .acp import native_model
from .base import RunHandle, RunSpec, RuntimeError_
from .rpcbase import (WEB_TOOLS, WRITE_TOOLS, RpcError, RpcRuntime, StreamSynth, hub_mcp_servers, load_native_session, mcp_tool_name,
                      save_native_session, tool_allowlist)

DEFAULT_CONFIG: dict[str, Any] = {
    "features": {"apps": False},
    "project_doc_max_bytes": 0,
}


def codex_mcp_config(spec: RunSpec, approval_mode: str = "prompt") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, e in hub_mcp_servers(spec).items():
        if e.get("type") in (None, "stdio") and e.get("command"):
            ent: dict[str, Any] = {"command": str(e["command"]), "args": [str(a) for a in e.get("args") or []]}
            if e.get("env"):
                ent["env"] = {str(k): str(v) for k, v in e["env"].items()}
        elif e.get("type") in ("http", "sse") and e.get("url"):
            ent = {"url": str(e["url"])}
            if e.get("headers"):
                ent["http_headers"] = {str(k): str(v) for k, v in e["headers"].items()}
        else:
            continue
        ent["default_tools_approval_mode"] = approval_mode  # mọi tool MCP của hub hỏi qua elicitation → guard quyết
        out[name] = ent
    return out


def sandbox_mode(spec: RunSpec, cfg: dict[str, Any]) -> str:
    if (spec.tool_policy or {}).get("permission_mode") == "plan":
        return "read-only"
    allow = tool_allowlist(spec)
    if allow is not None and not (allow & WRITE_TOOLS):  # khung chỉ đọc (vd strict của điều phối viên) → sandbox chỉ đọc
        return "read-only"
    return str(cfg.get("sandbox") or "workspace-write")


def _inner_command(params: dict[str, Any]) -> str:
    """Lệnh người đọc được: `commandActions[].command` (lệnh gốc model gửi), không phải chuỗi bọc `powershell.exe -Command '…'`."""
    acts = [str(a.get("command")) for a in params.get("commandActions") or [] if isinstance(a, dict) and a.get("command")]
    return " && ".join(acts) if acts else str(params.get("command") or "")


def _change_tool(change: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    kind = (change.get("kind") or {}).get("type") if isinstance(change.get("kind"), dict) else change.get("kind")
    path = str(change.get("path") or "")
    if kind == "add":
        return "Write", {"file_path": path, "content": str(change.get("diff") or "")}
    if kind == "delete":
        return "Bash", {"command": f"rm {path}", "description": "codex: xoá file"}
    return "Edit", {"file_path": path, "old_string": "", "new_string": str(change.get("diff") or "")}


def _item_tool(item: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    t = item.get("type")
    if t == "commandExecution":
        return "Bash", {"command": _inner_command(item)}
    if t == "fileChange":
        ch = [c for c in item.get("changes") or [] if isinstance(c, dict)]
        if len(ch) == 1:
            return _change_tool(ch[0])
        return "Edit", {"file_path": ", ".join(str(c.get("path")) for c in ch)}
    if t == "mcpToolCall":
        return mcp_tool_name(str(item.get("server") or "?"), str(item.get("tool") or "?")), dict(item.get("arguments") or {})
    if t == "webSearch":
        return "WebSearch", {"query": str(item.get("query") or "")}
    return None


def _item_result(item: dict[str, Any]) -> tuple[str, bool]:
    t = item.get("type")
    status = str(item.get("status") or "")
    if t == "commandExecution":
        out = str(item.get("aggregatedOutput") or "")
        code = item.get("exitCode")
        return (out or f"(status {status}, exit {code})"), status != "completed" or (code not in (None, 0))
    if t == "mcpToolCall":
        if item.get("error"):
            return str((item["error"] or {}).get("message") or item["error"]), True
        res = item.get("result") or {}
        texts = [str(c.get("text")) for c in res.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(texts), status not in ("completed", "")
    if t == "fileChange":
        return f"(file change {status})", status != "completed"
    return status, status not in ("completed", "")


class CodexRuntime(RpcRuntime):
    name = "codex"
    kind = "codex"

    def capabilities(self) -> dict[str, bool]:
        return {"steer": True, "approval": True, "fork": False, "resume": True, "json_schema": False, "budget": False}

    def _argv(self, spec: RunSpec) -> list[str]:
        if spec.agents_json or spec.plugin_dirs or spec.json_schema:
            raise RuntimeError_("runtime codex không hỗ trợ subagents / plugin Claude Code / result_schema")
        argv = [self.binary()]
        ws = self.cfg.get("windows_sandbox", "mxc")
        if ws:
            argv += ["-c", f'windows.sandbox="{ws}"']
        return [*argv, *[str(a) for a in self.cfg.get("extra_args") or []], "app-server"]

    def _thread_params(self, spec: RunSpec) -> dict[str, Any]:
        cfg: dict[str, Any] = {**DEFAULT_CONFIG, **(self.cfg.get("config") or {})}
        effort = spec.effort or self.cfg.get("effort")
        if effort:
            cfg["model_reasoning_effort"] = {"max": "xhigh"}.get(str(effort), str(effort))
        ws = self.cfg.get("windows_sandbox", "mxc")
        if ws:
            cfg["windows"] = {**(cfg.get("windows") or {}), "sandbox": ws}
        allow = tool_allowlist(spec)
        if allow is not None and not (allow & WEB_TOOLS):
            cfg["web_search"] = "disabled"  # khung không có web (vd strict) — tool web_search của codex không qua kênh duyệt
        mcp = codex_mcp_config(spec, str(self.cfg.get("mcp_approval_mode") or "prompt"))
        if mcp:
            cfg["mcp_servers"] = mcp
        roots = [str(p) for p in spec.add_dirs or []]
        if roots:
            cfg["sandbox_workspace_write"] = {**(cfg.get("sandbox_workspace_write") or {}), "writable_roots": roots}
        # ephemeral=false: thread được lưu → `thread/resume` ở tiến trình sau (đo X-10)
        p: dict[str, Any] = {"cwd": str(spec.cwd), "approvalPolicy": str(self.cfg.get("approval_policy") or "untrusted"),
                             "sandbox": sandbox_mode(spec, self.cfg), "config": cfg, "ephemeral": False}
        model = native_model(spec.model) or self.cfg.get("model")
        if model:
            p["model"] = str(model)
        if spec.system_prompt:
            p["developerInstructions"] = spec.system_prompt
        return p

    async def _drive(self, h: RunHandle) -> None:
        spec = h.spec
        rpc = h.internal["rpc"]
        synth: StreamSynth = h.internal["synth"]
        await rpc.request("initialize", {"clientInfo": {"name": "cfharness", "version": "1"}}, timeout=60)
        await rpc.notify("initialized", {})
        params = self._thread_params(spec)
        # MCP của người dùng (~/.codex/config.toml, lớp system) KHÔNG tắt được bằng mcp_servers={} — tắt TỪNG server theo tên
        # (đo X-11d). Tên lấy từ config/read; lỗi đọc → vẫn chạy (features.apps=false đã chặn connector ChatGPT).
        try:
            cr = await rpc.request("config/read", {"includeLayers": True}, timeout=30)
        except RpcError:
            cr = None
        ours = params["config"].setdefault("mcp_servers", {})
        foreign = [str(n) for n in (((cr or {}).get("config") or {}).get("mcp_servers") or {}) if n not in ours]
        for n in foreign:
            ours[n] = {"enabled": False}
        h.internal["disabled_mcp"] = foreign
        native = load_native_session(spec, self.kind, spec.resume_session_id) if spec.resume_session_id else None
        thread: dict[str, Any] | None = None
        if native:
            try:
                res = await rpc.request("thread/resume", {**params, "threadId": native}, timeout=120)
                thread = (res or {}).get("thread")
            except RpcError as e:
                h.internal["load_error"] = e.message
        if thread is None:
            res = await rpc.request("thread/start", params, timeout=120)
            thread = (res or {}).get("thread") or {}
        tid = str(thread.get("id") or "")
        if not tid:
            raise RuntimeError_("thread/start không trả thread.id")
        h.internal["thread_id"] = tid
        save_native_session(spec, self.kind, tid)
        synth.init(model=params.get("model"), tools=[],
                   mcp_servers=[n for n, e in (params["config"].get("mcp_servers") or {}).items() if e.get("enabled", True)],
                   version=h.cli_version and f"codex-cli {h.cli_version}", permission_mode=params["approvalPolicy"])
        prompt = spec.prompt
        if h.internal.get("load_error"):
            prompt = f"(Note: the previous conversation could not be restored.)\n\n{prompt}"
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        h.internal["turn_done"] = done
        synth.replay_user(spec.prompt)
        res = await rpc.request("turn/start", {"threadId": tid, "input": [{"type": "text", "text": prompt}]}, timeout=120)
        h.internal["turn_id"] = ((res or {}).get("turn") or {}).get("id")
        turn = await self.wait_or_exit(h, done)
        h.internal["accepting"] = False
        self._result(h, turn or {})

    def _result(self, h: RunHandle, turn: dict[str, Any]) -> None:
        synth: StreamSynth = h.internal["synth"]
        total = (h.internal.get("token_usage") or {}).get("total") or {}
        cached = int(total.get("cachedInputTokens") or 0)
        usage = {"input_tokens": max(0, int(total.get("inputTokens") or 0) - cached), "cache_read_input_tokens": cached,
                 "cache_creation_input_tokens": int(total.get("cacheWriteInputTokens") or 0),
                 "output_tokens": int(total.get("outputTokens") or 0)}
        status = str(turn.get("status") or "")
        err = turn.get("error") or h.internal.get("last_error")
        ok = status == "completed" and not h.stop_requested
        synth.result(ok=ok, subtype="success" if ok else "error_during_execution", usage=usage, cost_usd=0.0, num_turns=1,
                     stop_reason=status or None, duration_ms=self._elapsed_ms(h),
                     error=None if ok else f"codex turn {status or '?'}: {(err or {}).get('message') if isinstance(err, dict) else err or ''}"[:2000],
                     model=h.internal.get("model"))

    def _on_notify(self, h: RunHandle, method: str, params: dict[str, Any]) -> None:
        synth: StreamSynth = h.internal["synth"]
        if method == "item/agentMessage/delta":
            synth.text_delta(str(params.get("delta") or ""))
        elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            synth.thought_delta(str(params.get("delta") or ""))
        elif method == "item/started":
            item = params.get("item") or {}
            h.internal.setdefault("items", {})[str(item.get("id"))] = item
            mapped = _item_tool(item)
            if mapped and item.get("id"):
                synth.tool_start(str(item["id"]), *mapped)
        elif method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage":
                synth.flush(final_text=str(item.get("text") or ""))
            elif _item_tool(item) is not None and item.get("id"):
                if str(item["id"]) not in h.adapter.open_tools:
                    synth.tool_start(str(item["id"]), *_item_tool(item))  # type: ignore[misc]
                synth.tool_end(str(item["id"]), *_item_result(item))
            h.internal.get("items", {}).pop(str(item.get("id")), None)
        elif method == "thread/tokenUsage/updated":
            h.internal["token_usage"] = params.get("tokenUsage") or {}
        elif method == "account/rateLimits/updated":
            synth.feed({"type": "rate_limit_event", "rate_limit_info": params.get("rateLimits") or {}})
        elif method == "error":
            h.internal["last_error"] = params.get("error") or params
        elif method == "thread/started":
            h.internal["model"] = (params.get("thread") or {}).get("model") or h.internal.get("model")
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            if turn.get("id") and h.internal.get("turn_id") and turn["id"] != h.internal["turn_id"]:
                return
            done = h.internal.get("turn_done")
            if done is not None and not done.done():
                done.set_result(turn)

    async def _on_request(self, h: RunHandle, method: str, params: dict[str, Any], rid: Any) -> Any:
        synth: StreamSynth = h.internal["synth"]
        if method == "item/commandExecution/requestApproval":
            item_id = str(params.get("itemId") or "") or None
            inp = {"command": _inner_command(params)}
            if item_id and item_id not in h.adapter.open_tools:
                synth.tool_start(item_id, "Bash", inp)
            allow, _ = await self.ask(h, "Bash", inp, item_id)
            return {"decision": "accept" if allow else "decline"}
        if method == "item/fileChange/requestApproval":
            item_id = str(params.get("itemId") or "")
            item = (h.internal.get("items") or {}).get(item_id) or {}
            changes = [c for c in item.get("changes") or [] if isinstance(c, dict)]
            if not changes:  # không biết file nào → không cho (fail-closed)
                return {"decision": "decline"}
            for c in changes:
                name, inp = _change_tool(c)
                allow, _ = await self.ask(h, name, inp, item_id)
                if not allow:
                    return {"decision": "decline"}
            return {"decision": "accept"}
        if method == "mcpServer/elicitation/request":
            meta = params.get("_meta") or {}
            if meta.get("codex_approval_kind") != "mcp_tool_call":
                return {"action": "decline"}  # form hỏi thông tin của server MCP — hub không điền hộ người
            name = mcp_tool_name(str(params.get("serverName") or "?"), str(meta.get("tool_name") or "?"))
            inp = meta.get("tool_params") if isinstance(meta.get("tool_params"), dict) else {}
            allow, _ = await self.ask(h, name, inp, None)
            return {"action": "accept" if allow else "decline"}
        raise RpcError(-32601, f"cfharness không hỗ trợ {method}")

    async def _send_steer(self, h: RunHandle, text: str) -> None:
        tid, turn = h.internal.get("thread_id"), h.internal.get("turn_id")
        if not tid or not turn:
            raise RuntimeError_("lượt chưa bắt đầu")
        await h.internal["rpc"].request("turn/steer", {"threadId": tid, "expectedTurnId": turn,
                                                        "input": [{"type": "text", "text": text}]}, timeout=30)
        h.internal["synth"].replay_user(text)

    async def _interrupt_turn(self, h: RunHandle) -> None:
        tid, turn = h.internal.get("thread_id"), h.internal.get("turn_id")
        if tid and turn:
            await h.internal["rpc"].request("turn/interrupt", {"threadId": tid, "turnId": turn}, timeout=10)
            h.internal["synth"].close_open_tools("(bị ngắt)")  # đo X-07d: lệnh đang chạy không có item/completed sau interrupt
