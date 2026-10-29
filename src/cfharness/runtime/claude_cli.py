"""`ClaudeCliRuntime`: chạy agent bằng `claude -p … --output-format stream-json --verbose` (FR-RT-02/03/07).

Bộ dựng argv (`build_argv`) là hàm thuần, test được. Các cờ cô lập/permission lấy từ cấu hình
`runtime.claude.isolation.*` — KHÔNG hard-code tổ hợp chưa kiểm (spike A-01..A-05).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
import signal
import subprocess
import time
from typing import Any, AsyncIterator

from ..events import HubEvent, make_event
from ..oscompat import IS_WINDOWS, JOBS, exec_argv, interrupt_group, kill_tree, new_group_kwargs, no_window_kwargs
from ..redact import redact_argv
from .base import AgentRuntime, OneShotRequest, OneShotResult, RawLog, RunHandle, RunSpec, RuntimeError_, make_adapter

log = logging.getLogger("cfharness.runtime.claude")

SIGKILL = getattr(signal, "SIGKILL", 9)  # Windows không có SIGKILL — chỉ dùng làm nhãn bước cuối của `stop`

STREAM_LIMIT = 64 * 1024 * 1024  # một dòng stream-json có thể rất dài (init, tool_result lớn)


class ArgvError(RuntimeError_):
    pass


# Cờ nhận MỘT giá trị (giá trị không bị coi là cờ khi kiểm ma trận). Cờ variadic (--tools/--allowedTools/--disallowedTools)
# luôn đứng cuối argv; giá trị của chúng không bắt đầu bằng '-'.
_FLAGS_WITH_VALUE = frozenset({
    "--input-format", "--output-format", "--resume", "--session-id", "--name", "--append-system-prompt", "--system-prompt",
    "--system-prompt-snapshot", "--model", "--effort", "--fallback-model", "--max-budget-usd", "--mcp-config", "--settings",
    "--setting-sources", "--permission-mode", "--permission-prompts", "--permission-prompt-tool", "--add-dir", "--json-schema",
    "--agents", "--worktree",
})


def argv_flags(argv: list[str]) -> list[str]:
    """Tên các cờ trong argv (bỏ binary, bỏ giá trị của cờ một-giá-trị; `--x=y` → `--x`)."""
    out: list[str] = []
    skip = False
    for a in argv[1:]:
        if skip:
            skip = False
            continue
        if a.startswith("-") and len(a) > 1:
            name = a.split("=", 1)[0]
            out.append(name)
            if "=" not in a and name in _FLAGS_WITH_VALUE:
                skip = True
    return out


def check_flag_matrix(argv: list[str], claude_cfg: dict[str, Any], cli_version: str | None) -> None:
    """H-10 (FR-RT-02/15): từ chối spawn khi argv có cờ không thuộc ma trận của phiên bản CLI đang dùng.
    Phiên bản không có trong `runtime.claude.flag_matrix` → bỏ qua (FR-RT-15: phiên bản lạ chỉ cảnh báo vàng)."""
    matrix = claude_cfg.get("flag_matrix") or {}
    if not cli_version or not isinstance(matrix, dict) or cli_version not in matrix:
        return
    allowed = set(matrix.get(cli_version) or [])
    bad = sorted({f for f in argv_flags(argv) if f not in allowed})
    if bad:
        raise ArgvError(
            f"cờ không có trong ma trận tương thích của claude {cli_version}: {', '.join(bad)} "
            f"(runtime.claude.flag_matrix — thêm sau khi kiểm `claude --help` và contract test)"
        )


def _matrix_has(claude_cfg: dict[str, Any], cli_version: str | None, flag: str) -> bool | None:
    """True/False nếu phiên bản có trong flag_matrix; None = không biết (phiên bản lạ / không đọc được)."""
    matrix = claude_cfg.get("flag_matrix") or {}
    if not cli_version or not isinstance(matrix, dict) or cli_version not in matrix:
        return None
    return flag in set(matrix.get(cli_version) or [])


def chrome_argv(browser: Any, claude_cfg: dict[str, Any], cli_version: str | None) -> str | None:
    """Cờ Claude in Chrome cho run (docs/dev-notes/chrome.md; `tool_policy.browser` đã qua kill switch ở RunManager).
    - chrome|auto → `--chrome`; phiên bản CÓ trong flag_matrix mà thiếu `--chrome`: `chrome` → ArgvError, `auto` → bỏ cờ (còn Playwright);
      phiên bản lạ → vẫn truyền (FR-RT-15: chỉ cảnh báo vàng).
    - còn lại → `--no-chrome` khi `runtime.claude.chrome.pass_no_chrome` và phiên bản CHẮC CHẮN có cờ (flag_matrix)."""
    if browser in ("chrome", "auto"):
        has = _matrix_has(claude_cfg, cli_version, "--chrome")
        if has is False:
            if browser == "chrome":
                raise ArgvError(f"tool_policy.browser=chrome nhưng claude {cli_version} không có cờ --chrome trong flag_matrix "
                                "(nâng claude hoặc đổi --browser auto|playwright)")
            return None
        return "--chrome"
    cc = claude_cfg.get("chrome") if isinstance(claude_cfg.get("chrome"), dict) else {}
    if cc.get("pass_no_chrome", True) is not False and _matrix_has(claude_cfg, cli_version, "--no-chrome"):
        return "--no-chrome"
    return None


def build_argv(spec: RunSpec,claude_cfg: dict[str, Any], cli_version: str | None = None) -> tuple[list[str], bytes | None]:
    """Dựng argv cho `claude` từ RunSpec + cấu hình `runtime.claude`.

    Trả (argv, stdin_bytes).
    - `input_format: stream-json` (mặc định, theo spike A-02/A-03): `--input-format stream-json
      --replay-user-messages`; prompt gửi qua stdin dạng một dòng user message, stdin giữ mở tới khi có `result`.
    - `input_format: text`: prompt đi qua argv ngay sau `-p`; nếu prompt bắt đầu bằng '-' (dễ bị hiểu
      nhầm là cờ) thì đưa qua stdin.
    """
    iso = {**(claude_cfg.get("isolation") or {}), **(spec.isolation_overrides or {})}
    tp = spec.tool_policy or {}
    argv: list[str] = [str(claude_cfg.get("binary") or "claude"), "-p"]
    stdin: bytes | None = None
    input_format = claude_cfg.get("input_format", "text")
    if input_format == "stream-json":
        argv += ["--input-format", "stream-json"]
        stdin = user_message_line(spec.prompt)
    elif input_format == "text":
        if spec.prompt.startswith("-"):
            stdin = spec.prompt.encode("utf-8")
        else:
            argv.append(spec.prompt)
    else:
        raise ArgvError(f"runtime.claude.input_format không hợp lệ: {input_format}")
    argv += ["--output-format", "stream-json", "--verbose"]
    if input_format == "stream-json":
        argv.append("--replay-user-messages")
    if claude_cfg.get("include_partial_messages", True):
        argv.append("--include-partial-messages")
    if claude_cfg.get("include_hook_events"):
        argv.append("--include-hook-events")

    # Session (FR-RT-05)
    if spec.resume_session_id:
        argv += ["--resume", spec.resume_session_id]
        if spec.fork:
            argv.append("--fork-session")
            # M4-A: hub cấp id session MỚI cho bản fork (đã kiểm với CLI 2.1.302: --session-id đi cùng --fork-session)
            if spec.session_id and spec.session_id != spec.resume_session_id and claude_cfg.get("fork_session_id", True):
                argv += ["--session-id", spec.session_id]
    else:
        argv += ["--session-id", spec.session_id]

    if spec.display_name:
        argv += ["--name", spec.display_name]

    # Persona (FR-RT-10)
    if spec.system_prompt:
        mode = claude_cfg.get("system_prompt_mode", "append")
        if mode not in ("append", "replace"):
            raise ArgvError(f"runtime.claude.system_prompt_mode không hợp lệ: {mode}")
        argv += ["--append-system-prompt" if mode == "append" else "--system-prompt", spec.system_prompt]
    snap = claude_cfg.get("system_prompt_snapshot")
    if snap is not None:
        if snap not in ("on", "off"):
            raise ArgvError("runtime.claude.system_prompt_snapshot phải là on|off|null")
        argv += ["--system-prompt-snapshot", snap]

    # Model (FR-RT-11)
    if spec.model:
        argv += ["--model", spec.model]
    if spec.effort:
        argv += ["--effort", spec.effort]
    if spec.fallback_model:
        argv += ["--fallback-model", spec.fallback_model]

    # Ngân sách (FR-RT-08)
    if spec.max_budget_usd is not None:
        if spec.max_budget_usd <= 0:
            raise ArgvError("max_budget_usd phải > 0")
        argv += ["--max-budget-usd", _fmt_usd(spec.max_budget_usd)]

    # Cô lập cấu hình (FR-RT-03) — mọi cờ đều cấu hình được
    if spec.mcp_config_path is not None:
        argv += ["--mcp-config", str(spec.mcp_config_path)]
    if iso.get("strict_mcp_config", True):
        argv.append("--strict-mcp-config")
    if iso.get("pass_settings_file", True) and spec.settings_path is not None:
        argv += ["--settings", str(spec.settings_path)]
    ss = iso.get("setting_sources")
    if ss is not None:
        if isinstance(ss, (list, tuple)):
            ss = ",".join(ss)
        argv += ["--setting-sources", str(ss)]

    # Quyền tool (FR-RT-02, FR-RT-09 lớp tĩnh, NFR-15)
    pm = tp.get("permission_mode") or claude_cfg.get("default_permission_mode")
    if pm == "bypassPermissions" and not spec.trust_full:
        if not tp.get("allow_bypass"):
            raise ArgvError("bypassPermissions bị từ chối: agent phải khai báo rõ tool_policy.allow_bypass=true (NFR-15)")
        if spec.workspace_kind not in ("scratch", "worktree"):
            raise ArgvError("bypassPermissions bị từ chối: chỉ cho phép trên workspace scratch/worktree, không trên repo thật (NFR-15)")
    if pm:
        argv += ["--permission-mode", pm]
    if tp.get("permission_prompts"):
        argv += ["--permission-prompts", str(tp["permission_prompts"])]
    if spec.permission_prompt_tool:
        argv += ["--permission-prompt-tool", spec.permission_prompt_tool]

    for d in spec.add_dirs:
        argv += ["--add-dir", str(d)]

    # M3: output có cấu trúc (FR-RT-14), subagent động (FR-RT-13), worktree (FR-RT-12)
    if spec.json_schema:
        argv += ["--json-schema", json.dumps(spec.json_schema, ensure_ascii=False, separators=(",", ":"))]
    if spec.agents_json:
        argv += ["--agents", json.dumps(spec.agents_json, ensure_ascii=False, separators=(",", ":"))]
    if spec.worktree:
        argv += ["--worktree", spec.worktree]
    if spec.no_session_persistence:  # M4-B: run eval (FR-LN-10)
        argv.append("--no-session-persistence")
    chrome_flag = chrome_argv(tp.get("browser"), claude_cfg, cli_version)
    if chrome_flag:
        argv.append(chrome_flag)

    extra = iso.get("extra_args") or []
    if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
        raise ArgvError("runtime.claude.isolation.extra_args phải là danh sách chuỗi")
    from ..guard.runtime import check_extra_args  # H-18 (SEC): mọi biến thể bypass + cờ ghi đè cấu hình an toàn

    why = check_extra_args(extra, spec.workspace_kind, guard_on=spec.permission_prompt_tool is not None or bool(spec.allowed_hooks))
    if why:
        raise ArgvError(why)
    argv += extra

    # Các cờ nhận nhiều giá trị (variadic) đặt CUỐI để không nuốt nhầm đối số khác.
    if tp.get("tools") is not None:
        tools = tp["tools"]
        argv += ["--tools", ",".join(tools) if tools else ""]
    if tp.get("allowed_tools"):
        argv += ["--allowedTools", *tp["allowed_tools"]]
    if tp.get("disallowed_tools"):
        argv += ["--disallowedTools", *tp["disallowed_tools"]]
    check_flag_matrix(argv, claude_cfg, cli_version)
    return argv, stdin


def oneshot_argv(req: "OneShotRequest", claude_cfg: dict[str, Any], *, mcp_path: str, settings_path: str, cli_version: str | None = None) -> list[str]:
    """argv cho một lượt `claude -p --output-format json --json-schema … --no-session-persistence --tools ""` (judge/curator)."""
    argv = [
        str(claude_cfg.get("binary") or "claude"), "-p", "--output-format", "json",
        "--json-schema", json.dumps(req.schema, separators=(",", ":")),
        "--no-session-persistence",
        "--model", req.model,
        "--system-prompt", req.system,
        "--setting-sources", "",
        "--settings", settings_path,
        "--strict-mcp-config", "--mcp-config", mcp_path,
    ]
    if req.budget_usd:
        argv += ["--max-budget-usd", _fmt_usd(float(req.budget_usd))]
    argv += ["--tools", ""]
    check_flag_matrix(argv, claude_cfg, cli_version)
    return argv


def parse_oneshot_output(stdout: bytes) -> dict[str, Any]:
    for line in reversed(stdout.decode("utf-8", "replace").strip().splitlines()):
        try:
            data = json.loads(line)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return {}


def user_message_line(text: str) -> bytes:
    return (json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False) + "\n").encode("utf-8")


def control_interrupt_line(request_id: str) -> bytes:
    return (json.dumps({"type": "control_request", "request_id": request_id, "request": {"subtype": "interrupt"}}) + "\n").encode("utf-8")


def control_response_line(request_id: str, response: dict[str, Any] | None, *, error: str | None = None) -> bytes:
    """Trả lời `control_request` của CLI qua stdin stream-json. Hình dạng đối chiếu mã CLI 2.1.312
    (`StructuredIO.injectControlResponse`): `success` → `response.response` được parse theo schema của yêu cầu — với
    `can_use_tool`: `{behavior:"allow", updatedInput?}` | `{behavior:"deny", message}`; `error` → yêu cầu bị reject."""
    if error is not None:
        body: dict[str, Any] = {"subtype": "error", "request_id": request_id, "error": error}
    else:
        body = {"subtype": "success", "request_id": request_id, "response": response or {}}
    return (json.dumps({"type": "control_response", "response": body}, ensure_ascii=False) + "\n").encode("utf-8")


def _deny(message: str) -> dict[str, Any]:
    return {"behavior": "deny", "message": message}


def _fmt_usd(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def child_env(claude_cfg: dict[str, Any], base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for k in claude_cfg.get("env_strip") or []:
        env.pop(k, None)
    for k, v in (claude_cfg.get("env_extra") or {}).items():
        env[str(k)] = str(v)
    return env


_version_cache: dict[str, tuple[float, str | None]] = {}


def claude_version(binary: str = "claude", cache_s: float = 600) -> str | None:
    """`claude --version` → '2.1.302' (cache, FR-RT-15). None nếu không chạy được."""
    now = time.monotonic()
    hit = _version_cache.get(binary)
    if hit and now - hit[0] < cache_s:
        return hit[1]
    try:
        out = subprocess.run(exec_argv([binary, "--version"]), capture_output=True, encoding="utf-8", errors="replace", timeout=20, **no_window_kwargs())
        v = out.stdout.strip().split()[0] if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        v = None
    _version_cache[binary] = (now, v)
    return v


class ClaudeCliRuntime(AgentRuntime):
    name = "claude"

    def __init__(self, claude_cfg: dict[str, Any]) -> None:
        self.cfg = claude_cfg

    def capabilities(self) -> dict[str, bool]:
        return {
            "steer": self.cfg.get("input_format") == "stream-json",  # A-02 xác nhận; RPC run.steer (M2)
            "approval": False,  # M1, A-01
            "fork": True,
            "resume": True,
            "json_schema": True,
            "budget": True,
        }

    def version(self) -> str | None:
        return claude_version(self.cfg.get("binary") or "claude", float(self.cfg.get("version_cache_s", 600)))

    async def oneshot(self, req: "OneShotRequest") -> "OneShotResult":
        import tempfile

        from .base import OneShotResult

        t0 = time.monotonic()
        out_b, err_b, rc = b"", b"", -1
        with tempfile.TemporaryDirectory(prefix="cfharness-oneshot-") as td:
            mcp_path, settings_path = os.path.join(td, "mcp.json"), os.path.join(td, "settings.json")
            with open(mcp_path, "w", encoding="utf-8") as f:
                f.write('{"mcpServers":{}}')
            with open(settings_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(self.cfg.get("settings") or {"autoMemoryEnabled": False}))
            binary = str(self.cfg.get("binary") or "claude")
            # binary khác `claude` mặc định (judge/curator giả trong test) → không có phiên bản trong ma trận → không kiểm cờ
            version = self.version() if binary == "claude" else None
            argv = exec_argv(oneshot_argv(req, self.cfg, mcp_path=mcp_path, settings_path=settings_path, cli_version=version))
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                            stderr=asyncio.subprocess.PIPE, cwd=td, env=child_env(self.cfg), **no_window_kwargs())
                out_b, err_b = await asyncio.wait_for(proc.communicate(req.prompt.encode("utf-8")), req.timeout_s)
                rc = proc.returncode or 0
            except (asyncio.TimeoutError, OSError) as e:
                if proc is not None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                return OneShotResult(False, None, 0.0, int((time.monotonic() - t0) * 1000), f"lỗi gọi {binary}: {e!r}"[:300])
        data = parse_oneshot_output(out_b)
        cost = float(data.get("total_cost_usd") or 0.0)
        res = data.get("structured_output")
        if not isinstance(res, dict) and isinstance(data.get("result"), str):
            try:
                res = json.loads(data["result"])
            except json.JSONDecodeError:
                res = None
        ok = rc == 0 and not data.get("is_error") and isinstance(res, dict)
        err = "" if ok else str(data.get("result") or err_b.decode("utf-8", "replace")[-300:] or f"exit {rc}")[:300]
        return OneShotResult(ok, res if ok else None, cost, int((time.monotonic() - t0) * 1000), err, data)

    async def start(self, spec: RunSpec) -> RunHandle:
        cli_version = self.version()
        argv, stdin_bytes = build_argv(spec, self.cfg, cli_version)  # H-10: kiểm ma trận cờ theo phiên bản
        argv = exec_argv(argv)
        spec.cwd.mkdir(parents=True, exist_ok=True)
        spec.stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_f = open(spec.stderr_log_path, "ab")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(spec.cwd),
                stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr_f,
                env={**child_env(self.cfg), **(spec.env_extra or {})},
                # process group riêng → dừng cả tiến trình con (MCP stdio). Windows: spawn treo, gán Job Object rồi mới chạy
                **new_group_kwargs(suspended=IS_WINDOWS),
                limit=STREAM_LIMIT,
            )
        except OSError as e:
            stderr_f.close()
            raise RuntimeError_(f"không spawn được `{argv[0]}`: {e}") from e
        if IS_WINDOWS:
            try:
                JOBS.create(spec.run_id).adopt(proc.pid, resume=True)
            except OSError as e:  # không có job = không nhận ra hậu duệ của run → không cho chạy (fail-closed)
                proc.kill()
                stderr_f.close()
                raise RuntimeError_(f"không gán được Job Object cho `{argv[0]}`: {e}") from e
        stream_input = self.cfg.get("input_format", "text") == "stream-json"
        if stdin_bytes is not None and proc.stdin is not None:
            proc.stdin.write(stdin_bytes)
            await proc.stdin.drain()
            if not stream_input:
                proc.stdin.close()
        h = RunHandle(
            run_id=spec.run_id,
            spec=spec,
            adapter=make_adapter(spec),
            pid=proc.pid,
            argv_redacted=_redact_for_storage(argv, spec),
            cli_version=cli_version,
        )
        h.internal.update(proc=proc, stderr_f=stderr_f, stream_input=stream_input)
        return h

    def _stdin_open(self, handle: RunHandle) -> bool:
        proc = handle.internal.get("proc")
        return bool(handle.internal.get("stream_input")) and proc is not None and proc.stdin is not None and not proc.stdin.is_closing()

    def _close_stdin(self, handle: RunHandle) -> None:
        if self._stdin_open(handle):
            handle.internal["proc"].stdin.close()

    async def send(self, handle: RunHandle, user_message: str) -> None:
        """Gửi thêm tin user vào phiên đang chạy (steer, spike A-02). Chỉ khi input_format=stream-json."""
        if not self._stdin_open(handle):
            raise RuntimeError_("run không nhận thêm tin (stdin đã đóng hoặc input_format=text)")
        proc = handle.internal["proc"]
        # M2: tin chưa được CLI phát lại (steer.ack) → KHÔNG đóng stdin ở `result` kế tiếp, để CLI kịp chạy
        # tin đó (gửi sát/sau `result` thì CLI mở lượt mới, spike A-02 "twoturns"). Hết tin chờ → đóng như M0.
        handle.internal["steer_pending"] = int(handle.internal.get("steer_pending") or 0) + 1
        try:
            proc.stdin.write(user_message_line(user_message))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            handle.internal["steer_pending"] -= 1
            raise RuntimeError_(f"không gửi được tin vào stdin: {e}") from e

    def _close_if_no_ack(self, handle: RunHandle, replay_count: int) -> None:
        if handle.adapter.replay_count == replay_count and handle.internal.get("steer_pending"):
            handle.internal["steer_pending"] = 0
            self._close_stdin(handle)

    async def events(self, handle: RunHandle) -> AsyncIterator[HubEvent]:
        proc: asyncio.subprocess.Process = handle.internal["proc"]
        raw = RawLog(handle.spec.raw_log_path)
        assert proc.stdout is not None
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError:  # dòng vượt STREAM_LIMIT
                    yield make_event("raw.invalid", handle.spec.agent_id, {"line": "(dòng quá dài, bỏ qua)"}, run_id=handle.run_id)
                    continue
                if not line:
                    break
                raw.write(line)
                if b'"control_request"' in line:
                    self._on_control_request(handle, line)
                before = handle.adapter.result_count
                for ev in handle.adapter.feed(line):
                    if ev.type == "steer.ack" and handle.internal.get("steer_pending"):
                        handle.internal["steer_pending"] -= 1
                    yield ev
                if (
                    handle.adapter.result_count > before
                    and not handle.internal.get("keep_stdin_open")
                    and not handle.internal.get("steer_pending")
                ):
                    # M0: một lượt mỗi tiến trình — đóng stdin sau `result` để CLI thoát
                    # (M2: trừ khi còn tin steer chưa được phát lại — xem `send`)
                    self._close_stdin(handle)
                elif handle.adapter.result_count > before and handle.internal.get("steer_pending"):
                    # phòng hờ: CLI không phát lại tin steer trong `steer_ack_grace_s` → đóng stdin như M0
                    asyncio.get_running_loop().call_later(
                        float(self.cfg.get("steer_ack_grace_s", 30)), self._close_if_no_ack, handle, handle.adapter.replay_count
                    )
            handle.exit_code = await proc.wait()
        finally:
            for t in list(handle.internal.get("control_tasks") or ()):
                t.cancel()
            JOBS.finished(handle.run_id)
            handle.raw_log_final = raw.close()
            f = handle.internal.get("stderr_f")
            if f:
                f.close()
            handle.stderr_tail = _tail(handle.spec.stderr_log_path)

    # ------------------------------------------------------------------ control_request của CLI (2.1.303+)
    def _on_control_request(self, handle: RunHandle, line: bytes) -> None:
        """CLI hỏi qua stdout và CHỜ `control_response` trên stdin (không trả lời = lệnh treo, vd Bash ra mạng trong sandbox).
        Quyết định chạy nền (không chặn đọc stdout: heartbeat/tool khác vẫn chảy); mọi lỗi → deny (FAIL-CLOSED)."""
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(obj, dict) or obj.get("type") != "control_request" or not isinstance(obj.get("request_id"), str):
            return
        task = asyncio.get_running_loop().create_task(self._answer_control(handle, obj))
        tasks: set[asyncio.Task] = handle.internal.setdefault("control_tasks", set())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def _answer_control(self, handle: RunHandle, obj: dict[str, Any]) -> None:
        rid = obj["request_id"]
        req = obj.get("request") if isinstance(obj.get("request"), dict) else {}
        if req.get("subtype") != "can_use_tool":
            line = control_response_line(rid, None, error=f"cfharness: control_request '{req.get('subtype')}' không được hỗ trợ")
        else:
            decision: Any = None
            handler = handle.spec.control_handler
            timeout = float(self.cfg.get("control_request_timeout_s") or 1800)
            try:
                if handler is None:
                    raise RuntimeError_("run không có bộ xử lý control_request")
                decision = await asyncio.wait_for(handler(req, rid), timeout)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                decision = _deny(f"[cfharness] No decision within {int(timeout)}s, so this was denied.")
            except Exception as e:  # noqa: BLE001 — fail-closed
                log.warning("control_request %s lỗi: %r", rid, e)
                decision = _deny("[cfharness] The hub could not evaluate this request, so it was denied (fail-closed).")
            if not isinstance(decision, dict) or decision.get("behavior") not in ("allow", "deny"):
                decision = _deny("[cfharness] Invalid decision, so this was denied (fail-closed).")
            if decision["behavior"] == "allow":
                inp = req.get("input") if isinstance(req.get("input"), dict) else {}
                upd = decision.get("updatedInput")
                decision = {"behavior": "allow", "updatedInput": upd if isinstance(upd, dict) else inp}
            else:
                decision = _deny(str(decision.get("message") or "[cfharness] denied"))
            line = control_response_line(rid, decision)
        if not self._stdin_open(handle):
            log.warning("không trả lời được control_request %s: stdin đã đóng", rid)
            return
        proc = handle.internal["proc"]
        try:
            proc.stdin.write(line)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            log.warning("không ghi được control_response %s: %r", rid, e)

    def _signal(self, handle: RunHandle, sig: int) -> bool:
        proc = handle.internal.get("proc")
        if proc is None or proc.returncode is not None:
            return False
        if IS_WINDOWS:
            # SIGINT/SIGTERM → CTRL_BREAK cho process group; SIGKILL → TerminateJobObject (cả hậu duệ, như killpg)
            if sig == SIGKILL:
                job = JOBS.get(handle.run_id)
                return job.terminate() if job is not None else kill_tree(proc.pid)
            return interrupt_group(proc.pid)
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return False
        except PermissionError:
            proc.send_signal(sig)
        return True

    async def interrupt(self, handle: RunHandle) -> None:
        """Ngắt lượt hiện tại. stream-json: `control_request interrupt` qua stdin (tiến trình sống, spike A-03);
        ngược lại SIGINT."""
        if self._stdin_open(handle):
            proc = handle.internal["proc"]
            try:
                proc.stdin.write(control_interrupt_line("int-" + uuid.uuid4().hex[:8]))
                await proc.stdin.drain()
                return
            except (BrokenPipeError, ConnectionResetError):
                pass
        self._signal(handle, signal.SIGINT)

    async def stop(self, handle: RunHandle, grace_s: float = 10.0) -> None:
        """Dừng run (FR-RT-07, theo spike A-03): stream-json → control interrupt + đóng stdin, chờ thoát;
        rồi SIGINT → chờ grace → SIGTERM → chờ 5 s → SIGKILL, cho cả process group."""
        handle.stop_requested = True
        proc: asyncio.subprocess.Process | None = handle.internal.get("proc")
        if proc is None:
            return
        if self._stdin_open(handle):
            await self.interrupt(handle)
            self._close_stdin(handle)
            try:
                await asyncio.wait_for(proc.wait(), timeout=min(grace_s, 5.0))
            except asyncio.TimeoutError:
                pass
        for sig, wait_s in ((signal.SIGINT, grace_s), (signal.SIGTERM, 5.0), (SIGKILL, 5.0)):
            if not self._signal(handle, sig):
                break
            try:
                await asyncio.wait_for(proc.wait(), timeout=wait_s)
                break
            except asyncio.TimeoutError:
                continue
        # dọn tiến trình con còn sót trong group (Windows: trong Job Object của run)
        if IS_WINDOWS:
            job = JOBS.get(handle.run_id)
            if job is not None:
                job.terminate()
            return
        try:
            os.killpg(proc.pid, SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass



def _redact_for_storage(argv: list[str], spec: RunSpec) -> list[str]:
    out = redact_argv(argv)
    # không lưu nguyên prompt/system prompt dài trong argv (đã có ở bảng runs / hash)
    for i, a in enumerate(out):
        if i > 0 and out[i - 1] in ("--append-system-prompt", "--system-prompt"):
            out[i] = f"<system-prompt {len(a)} chars>"
        elif a == spec.prompt and len(a) > 200:
            out[i] = a[:200] + "…"
    return out


def _tail(path, n: int = 4000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
