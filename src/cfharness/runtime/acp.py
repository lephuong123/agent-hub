"""`AcpRuntime`: agent nói Agent Client Protocol qua stdio — mặc định Grok Build (`grok agent stdio`).

Đo thật: docs/spikes/codex-grok/grok/REPORT-grok.md (K-07), docs/spikes/agy-spike-report.md (cùng giao thức với `agy acp`).
Trình tự: initialize → session/new {cwd, mcpServers} (hoặc session/load khi resume) → session/prompt → chờ phản hồi `stopReason`.
- Mỗi `session/request_permission` → guard (`RpcRuntime.ask`); chỉ trả `allow_once` / `reject_once` — KHÔNG BAO GIỜ `*_always`
  (Grok lưu "luôn cho phép" theo project, vượt khỏi quyết định của hub).
- MCP của hub qua `session/new.mcpServers` (không cần trust thư mục, không đụng `~/.grok/config.toml`).
- Cô lập (đo K-04, K-09): `GROK_HOME` RIÊNG của hub (`<HUB_HOME>/runtime-homes/grok/config.toml` do hub viết: tắt quét cấu hình
  Claude Code/Cursor + plugin đã cài, không [mcp_servers]/rules của người dùng) + `GROK_AUTH_PATH` trỏ `~/.grok/auth.json` (mượn phiên
  `grok login`, không chép token); env tắt bộ nhớ liên phiên + tự cập nhật; `--no-leader` = tiến trình riêng của run.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from .base import RunHandle, RunSpec, RuntimeError_
from .rpcbase import RpcRuntime, StreamSynth, hub_mcp_servers, load_native_session, mcp_tool_name, save_native_session

# biến môi trường Grok Build 1.0.92 (đo K-04): tắt nạp cấu hình Claude Code/Cursor của máy + bộ nhớ liên phiên + tự cập nhật
_COMPAT_PARTS = ("SKILLS", "MCPS", "RULES", "AGENTS", "HOOKS")
GROK_ISOLATION_ENV = {
    "GROK_DISABLE_AUTOUPDATER": "1",
    "GROK_MEMORY": "0",
    **{f"GROK_CLAUDE_{p}_ENABLED": "0" for p in _COMPAT_PARTS},
    **{f"GROK_CURSOR_{p}_ENABLED": "0" for p in _COMPAT_PARTS},
}
_CLAUDE_MODEL_HINTS = ("opus", "sonnet", "haiku", "fable", "claude-")


def _installed_plugin_names() -> list[str]:
    """Plugin Grok có thể nạp (Claude Code cài qua marketplace + plugin cài thẳng cho Grok) → tên để `[plugins] disabled`."""
    names: set[str] = set()
    try:
        data = json.loads((Path.home() / ".claude" / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
        for key in (data.get("plugins") or {}) if isinstance(data, dict) else ():
            names.add(str(key).split("@", 1)[0])
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    for d in (Path.home() / ".grok" / "installed-plugins",):
        try:
            names.update(p.name for p in d.iterdir() if p.is_dir())
        except OSError:
            pass
    return sorted(n for n in names if n)


def grok_home_config(disabled_plugins: list[str]) -> str:
    """config.toml cho GROK_HOME riêng của hub (đo K-09): tắt quét cấu hình Claude/Cursor + plugin đã cài. KHÔNG có [mcp_servers]."""
    cells = "".join(f"{k} = false\n" for k in ("skills", "rules", "agents", "mcps", "hooks"))
    plugins = ", ".join(json.dumps(n) for n in disabled_plugins)
    return ("# do cfharness sinh mỗi lần chạy run grok — sửa runtime.grok trong cấu hình hub, không sửa file này\n"
            f"[compat.claude]\n{cells}\n[compat.cursor]\n{cells}\n[plugins]\ndisabled = [{plugins}]\n")


def prepare_grok_home(home: Path, extra_disabled: list[str] | None = None) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    text = grok_home_config(sorted(set(_installed_plugin_names()) | set(extra_disabled or [])))
    cfg = home / "config.toml"
    try:
        if cfg.read_text(encoding="utf-8") == text:
            return home
    except OSError:
        pass
    cfg.write_text(text, encoding="utf-8")
    return home


def native_model(model: str | None) -> str | None:
    """Model của agent nếu là model của runtime này (không phải alias Claude mặc định)."""
    if not model or model == "default" or model.startswith(_CLAUDE_MODEL_HINTS):
        return None
    return model


def acp_mcp_servers(spec: RunSpec) -> list[dict[str, Any]]:
    """mcp.json kiểu Claude → `McpServer[]` của ACP (stdio: env là danh sách {name, value}; http: headers danh sách)."""
    out: list[dict[str, Any]] = []
    for name, e in hub_mcp_servers(spec).items():
        if e.get("type") in (None, "stdio") and e.get("command"):
            out.append({"name": name, "command": str(e["command"]), "args": [str(a) for a in e.get("args") or []],
                        "env": [{"name": str(k), "value": str(v)} for k, v in (e.get("env") or {}).items()]})
        elif e.get("type") in ("http", "sse") and e.get("url"):
            out.append({"type": e["type"], "name": name, "url": str(e["url"]),
                        "headers": [{"name": str(k), "value": str(v)} for k, v in (e.get("headers") or {}).items()]})
    return out


def map_tool(tool_call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """toolCall của Grok → (tên tool kiểu Claude, input) cho guard/classifier và hiển thị. Đo K-07: `rawInput.variant`."""
    raw = tool_call.get("rawInput") if isinstance(tool_call.get("rawInput"), dict) else {}
    meta = (tool_call.get("_meta") or {}).get("x.ai/tool") or {}
    variant = raw.get("variant")
    if variant == "Bash" or (variant is None and tool_call.get("kind") == "execute"):
        cmd = raw.get("command") or (meta.get("input") or {}).get("command") or ""
        return "Bash", {"command": str(cmd), **({"description": raw["description"]} if raw.get("description") else {})}
    if variant == "Write":
        return "Write", {"file_path": str(raw.get("file_path") or ""), "content": str(raw.get("content") or "")}
    if variant in ("SearchReplace", "Edit", "MultiEdit"):
        return "Edit", {"file_path": str(raw.get("file_path") or ""), "old_string": str(raw.get("old_string") or ""),
                        "new_string": str(raw.get("new_string") or "")}
    if variant == "UseTool":
        full = str(raw.get("tool_name") or "")
        server, _, tool = full.partition("__")
        inp = raw.get("tool_input") if isinstance(raw.get("tool_input"), dict) else {}
        return (mcp_tool_name(server, tool) if tool else f"mcp__{full}"), inp
    if tool_call.get("kind") == "edit":
        locs = tool_call.get("locations") or []
        path = (locs[0] or {}).get("path") if locs and isinstance(locs[0], dict) else (meta.get("input") or {}).get("path")
        return "Edit", {"file_path": str(path or "")}
    name = str(meta.get("label") or meta.get("name") or tool_call.get("title") or "tool")
    return f"grok:{name}", {k: v for k, v in raw.items() if k != "variant"}


def _content_text(content: Any, raw_output: Any) -> str:
    parts: list[str] = []
    for c in content or []:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "content" and isinstance(c.get("content"), dict):
            parts.append(str(c["content"].get("text") or ""))
        elif c.get("type") == "diff":
            parts.append(f"(diff) {c.get('path')}")
    if not parts and raw_output is not None:
        parts.append(json.dumps(raw_output, ensure_ascii=False, default=str)[:4000])
    return "\n".join(p for p in parts if p)


class AcpRuntime(RpcRuntime):
    name = "grok"
    kind = "grok"

    def capabilities(self) -> dict[str, bool]:
        return {"steer": True, "approval": True, "fork": False, "resume": True, "json_schema": False, "budget": False}

    def _argv(self, spec: RunSpec) -> list[str]:
        if spec.agents_json or spec.plugin_dirs or spec.json_schema:
            raise RuntimeError_("runtime grok không hỗ trợ subagents / plugin Claude Code / result_schema")
        argv = [self.binary(), "agent", "--no-leader"]
        model = native_model(spec.model) or self.cfg.get("model")
        if model:
            argv += ["-m", str(model)]
        effort = spec.effort or self.cfg.get("effort")
        if effort:
            argv += ["--reasoning-effort", {"max": "xhigh"}.get(str(effort), str(effort))]
        return [*argv, *[str(a) for a in self.cfg.get("extra_args") or []], "stdio"]

    def _env(self, spec: RunSpec) -> dict[str, str]:
        env = super()._env(spec)
        if self.cfg.get("isolate", True):
            own = self.cfg.get("env") or {}
            for k, v in GROK_ISOLATION_ENV.items():
                if k not in own:  # người đặt rõ trong runtime.grok.env thắng
                    env[k] = v
            if "GROK_HOME" not in own and self.cfg.get("hub_home"):
                # GROK_HOME của hub (config.toml do hub viết: không [mcp_servers]/rules/plugin của người dùng) + MƯỢN phiên
                # `grok login` qua GROK_AUTH_PATH (đo K-09g: cached_token xác thực được, auth.json không đổi)
                user_home = Path(os.environ.get("GROK_HOME") or (Path.home() / ".grok"))
                env["GROK_HOME"] = str(prepare_grok_home(Path(self.cfg["hub_home"]) / "runtime-homes" / "grok",
                                                         list(self.cfg.get("disabled_plugins") or [])))
                env.setdefault("GROK_AUTH_PATH", str(user_home / "auth.json"))  # tài khoản thêm từ hub đã đặt GROK_AUTH_PATH (rpcbase)
        return env

    async def _drive(self, h: RunHandle) -> None:
        spec = h.spec
        rpc = h.internal["rpc"]
        synth: StreamSynth = h.internal["synth"]
        init = await rpc.request("initialize", {"protocolVersion": 1, "clientInfo": {"name": "cfharness", "version": "1"},
                                                "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                                                       "terminal": False}}, timeout=60)
        h.internal["agent_caps"] = (init or {}).get("agentCapabilities") or {}
        mcp = acp_mcp_servers(spec)
        native = load_native_session(spec, self.kind, spec.resume_session_id) if spec.resume_session_id else None
        sid: str | None = None
        if native and h.internal["agent_caps"].get("loadSession"):
            h.internal["replaying"] = True  # session/load phát lại lịch sử qua session/update — không dịch thành event mới
            try:
                await rpc.request("session/load", {"sessionId": native, "cwd": str(spec.cwd), "mcpServers": mcp}, timeout=120)
                sid = native
            except Exception as e:  # noqa: BLE001 — phiên cũ không mở được → phiên mới, báo rõ
                h.internal["load_error"] = str(e)
            finally:
                h.internal["replaying"] = False
        if sid is None:
            res = await rpc.request("session/new", {"cwd": str(spec.cwd), "mcpServers": mcp}, timeout=120)
            sid = str((res or {}).get("sessionId") or "")
            if not sid:
                raise RuntimeError_("session/new không trả sessionId")
        h.internal["native_session"] = sid
        save_native_session(spec, self.kind, sid)
        synth.init(model=h.internal.get("model") or native_model(spec.model) or self.cfg.get("model"), tools=[],
                   mcp_servers=[m["name"] for m in mcp], version=h.cli_version and f"grok {h.cli_version}", permission_mode="default")
        prompt = spec.prompt
        if spec.system_prompt and not native:
            prompt = f"<hub_instructions>\n{spec.system_prompt}\n</hub_instructions>\n\n{prompt}"
        if h.internal.get("load_error"):
            prompt = f"(Note: the previous conversation could not be restored.)\n\n{prompt}"
        synth.replay_user(spec.prompt)
        h.internal["prompts"] = [asyncio.ensure_future(self._prompt(h, prompt))]
        final: dict[str, Any] = {}
        while h.internal["prompts"]:  # steer thêm prompt vào cùng session — chờ tới khi mọi prompt xong
            fut = h.internal["prompts"][0]
            try:
                final = await fut or {}
            finally:
                h.internal["prompts"].remove(fut)
        h.internal["accepting"] = False
        self._result(h, final)

    async def _prompt(self, h: RunHandle, text: str) -> dict[str, Any]:
        rpc = h.internal["rpc"]
        return await rpc.request("session/prompt", {"sessionId": h.internal["native_session"],
                                                    "prompt": [{"type": "text", "text": text}]}) or {}

    def _result(self, h: RunHandle, res: dict[str, Any]) -> None:
        synth: StreamSynth = h.internal["synth"]
        meta = res.get("_meta") or {}
        u = meta.get("usage") or {}
        cached = int(u.get("cachedReadTokens") or 0)
        usage = {"input_tokens": max(0, int(u.get("inputTokens") or 0) - cached), "cache_read_input_tokens": cached,
                 "cache_creation_input_tokens": int(u.get("cacheCreationTokens") or 0), "output_tokens": int(u.get("outputTokens") or 0)}
        cost = float(u.get("costUsdTicks") or 0) / 1e10  # 1 tick = 1e-10 USD (giá API tham khảo; gói thuê bao không trả theo lượt)
        stop = str(res.get("stopReason") or "")
        category = meta.get("cancellationCategory")
        if h.stop_requested:
            ok, subtype = False, "error_during_execution"
        elif stop in ("end_turn", "") or (stop == "cancelled" and category == "PermissionRejected"):
            ok, subtype = True, "success"  # bị guard từ chối tool → Grok kết thúc lượt; run vẫn xong bình thường
        elif stop == "max_turn_requests":
            ok, subtype = False, "error_max_turns"
        else:
            ok, subtype = False, "error_during_execution"
        synth.result(ok=ok, subtype=subtype, usage=usage, cost_usd=round(cost, 6), num_turns=u.get("numTurns"),
                     stop_reason=(f"{stop}:{category}" if category else stop) or None, duration_ms=self._elapsed_ms(h),
                     error=None if ok else f"grok stopReason={stop or '?'}", model=meta.get("modelId"))

    def _on_notify(self, h: RunHandle, method: str, params: dict[str, Any]) -> None:
        if method == "_x.ai/models/update" and params.get("currentModelId"):
            h.internal["model"] = params["currentModelId"]
            return
        if method != "session/update" or h.internal.get("replaying"):
            return
        synth: StreamSynth = h.internal["synth"]
        u = params.get("update") or {}
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            synth.text_delta(str((u.get("content") or {}).get("text") or ""))
        elif kind == "agent_thought_chunk":
            synth.thought_delta(str((u.get("content") or {}).get("text") or ""))
        elif kind == "tool_call":
            tid = str(u.get("toolCallId") or "")
            if tid and tid not in h.adapter.open_tools:
                name, inp = map_tool(u)
                synth.tool_start(tid, name, inp)
        elif kind == "tool_call_update" and u.get("status") in ("completed", "failed"):
            tid = str(u.get("toolCallId") or "")
            if tid and tid not in h.adapter.open_tools:
                name, inp = map_tool(u)
                synth.tool_start(tid, name, inp)
            synth.tool_end(tid, _content_text(u.get("content"), u.get("rawOutput")), u.get("status") == "failed")

    async def _on_request(self, h: RunHandle, method: str, params: dict[str, Any], rid: Any) -> Any:
        if method != "session/request_permission":
            from .rpcbase import RpcError

            raise RpcError(-32601, f"cfharness không hỗ trợ {method}")
        tc = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
        options = params.get("options") or []
        name, inp = map_tool(tc)
        tid = str(tc.get("toolCallId") or "") or None
        if tid and tid not in h.adapter.open_tools:
            h.internal["synth"].tool_start(tid, name, inp)
        allow, _msg = await self.ask(h, name, inp, tid)
        want = "allow_once" if allow else "reject_once"
        for o in options:
            if isinstance(o, dict) and o.get("kind") == want:
                return {"outcome": {"outcome": "selected", "optionId": o["optionId"]}}
        return {"outcome": {"outcome": "cancelled"}}  # không có lựa chọn "một lần" phù hợp → huỷ (không chọn *_always)

    async def _send_steer(self, h: RunHandle, text: str) -> None:
        if not h.internal.get("native_session"):
            raise RuntimeError_("phiên chưa sẵn sàng")
        h.internal["prompts"].append(asyncio.ensure_future(self._prompt(h, text)))
        h.internal["synth"].replay_user(text)

    async def _interrupt_turn(self, h: RunHandle) -> None:
        sid = h.internal.get("native_session")
        if sid:
            await h.internal["rpc"].notify("session/cancel", {"sessionId": sid})
