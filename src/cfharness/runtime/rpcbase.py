"""Nền chung cho runtime nói JSON-RPC qua stdio (ACP của `grok agent stdio`, `codex app-server`).

Báo cáo đo: docs/spikes/codex-grok-spike-report.md. Ý tưởng:
- Mỗi run MỘT tiến trình (Windows: Job Object như `ClaudeCliRuntime` → dừng được cả cây, kể cả MCP stdio con).
- Hub là client: gửi request, trả lời *server request* (hỏi quyền) và nhận notification.
- Notification được dịch sang dòng stream-json KIỂU CLAUDE (`system/init`, `assistant`, `user tool_result`, `result`…) rồi đưa qua
  CHÍNH `ClaudeStreamAdapter` → event hub giống hệt run Claude (UI, DB, chi phí, cô lập MCP không phải viết lại).
- Hỏi quyền → `spec.control_handler` (RunManager → `GuardService.check` kênh `control`), FAIL-CLOSED: lỗi/hết giờ/không có
  bộ xử lý = từ chối.
- Raw log (`runs/<id>.jsonl`) ghi NGUYÊN VĂN message hai chiều `{"dir":"in"|"out","msg":…}` (FR-RT-04: giữ bằng chứng thô).
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from ..events import HubEvent, make_event
from ..oscompat import IS_WINDOWS, JOBS, exec_argv, kill_tree, new_group_kwargs
from .base import AgentRuntime, RawLog, RunHandle, RunSpec, RuntimeError_, make_adapter

log = logging.getLogger("cfharness.runtime.rpc")

STREAM_LIMIT = 32 * 1024 * 1024
HUB_GUARD_SERVER = "hub-guard"  # permission-prompt MCP của Claude — runtime khác hỏi quyền qua kênh riêng, không cần server này
_DONE = object()


class RpcError(Exception):
    def __init__(self, code: Any, message: str, data: Any = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.data = code, message, data


# --------------------------------------------------------------------------- tiện ích dùng chung
def hub_mcp_servers(spec: RunSpec) -> dict[str, dict[str, Any]]:
    """Server MCP của hub cho run (mcp.json do RunManager ghi, dạng Claude `{"mcpServers": {...}}`), bỏ `hub-guard`."""
    p = spec.mcp_config_path
    if p is None or not Path(p).exists():
        return {}
    try:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError_(f"không đọc được cấu hình MCP của run ({p}): {e}") from e
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    return {str(n): dict(e) for n, e in servers.items() if n != HUB_GUARD_SERVER and isinstance(e, dict)}


def mcp_tool_name(server: str, tool: str) -> str:
    """Tên tool MCP theo quy ước Claude (`mcp__<server>__<tool>`) — guard/classifier và luật tĩnh dùng đúng dạng này."""
    return f"mcp__{server}__{tool}"


def native_session_store(spec: RunSpec) -> Path:
    """Bảng hub session → id phiên của runtime (Grok sessionId / Codex threadId): runtime tự cấp id, không nhận id của hub."""
    return spec.raw_log_path.parent / "native-sessions"


def save_native_session(spec: RunSpec, kind: str, native_id: str) -> None:
    d = native_session_store(spec)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{spec.session_id}.json").write_text(json.dumps({"kind": kind, "id": native_id, "run_id": spec.run_id}), encoding="utf-8")


def load_native_session(spec: RunSpec, kind: str, hub_session_id: str) -> str | None:
    f = native_session_store(spec) / f"{hub_session_id}.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return str(d["id"]) if isinstance(d, dict) and d.get("kind") == kind and d.get("id") else None


class StreamSynth:
    """Dựng dòng stream-json kiểu Claude và đưa vào `ClaudeStreamAdapter` của run. Mọi dòng mang session_id CỦA HUB."""

    def __init__(self, handle: RunHandle, push: Callable[[HubEvent], None]) -> None:
        self.h = handle
        self.push = push
        self.sid = handle.spec.session_id
        self._text: list[str] = []
        self._thought: list[str] = []
        self._msg_n = 0
        self.last_text = ""
        self.turn_started = False

    def feed(self, obj: dict[str, Any]) -> None:
        obj.setdefault("session_id", self.sid)
        for ev in self.h.adapter.feed(obj):
            self.push(ev)

    def init(self, *, model: str | None, tools: list[str], mcp_servers: list[str], version: str | None, permission_mode: str) -> None:
        self.feed({"type": "system", "subtype": "init", "cwd": str(self.h.spec.cwd), "model": model, "tools": tools,
                   "mcp_servers": [{"name": n, "status": "connected"} for n in mcp_servers], "permissionMode": permission_mode,
                   "claude_code_version": version})

    def replay_user(self, text: str) -> None:
        """Tin user đã vào ngữ cảnh: lần đầu (prompt) adapter bỏ qua, các lần sau → `steer.ack`."""
        self.feed({"type": "user", "isReplay": True, "uuid": str(uuid.uuid4()), "message": {"role": "user", "content": text}})

    def turn_start(self) -> None:
        if not self.turn_started:
            self.turn_started = True
            self.feed({"type": "stream_event", "event": {"type": "message_start", "message": {"usage": {}}}})

    def text_delta(self, chunk: str) -> None:
        if not chunk:
            return
        self.turn_start()
        self._text.append(chunk)
        self.feed({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": chunk}}})

    def thought_delta(self, chunk: str) -> None:
        if not chunk:
            return
        self.turn_start()
        self._thought.append(chunk)
        self.feed({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": chunk}}})

    def flush(self, final_text: str | None = None) -> None:
        """Đóng khối chữ đang gom thành `assistant` (message.complete). `final_text` = bản đầy đủ runtime gửi (ưu tiên)."""
        blocks: list[dict[str, Any]] = []
        thought = "".join(self._thought)
        if thought.strip():
            blocks.append({"type": "thinking", "thinking": thought})
        text = final_text if final_text is not None else "".join(self._text)
        if text.strip():
            blocks.append({"type": "text", "text": text})
            self.last_text = text
        self._text, self._thought = [], []
        if blocks:
            self._msg_n += 1
            self.feed({"type": "assistant", "message": {"id": f"msg-{self._msg_n}", "role": "assistant", "content": blocks}})

    def tool_start(self, tool_id: str, name: str, inp: dict[str, Any]) -> None:
        self.flush()
        self.turn_start()
        self.feed({"type": "assistant", "message": {"id": f"tool-{tool_id}", "role": "assistant",
                                                    "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}]}})

    def tool_end(self, tool_id: str, text: str, is_error: bool) -> None:
        if tool_id not in self.h.adapter.open_tools:
            return
        self.feed({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": text, "is_error": is_error}]}})

    def close_open_tools(self, why: str) -> None:
        for tid in list(self.h.adapter.open_tools):
            self.tool_end(tid, why, True)

    def control_request(self, rid: str, tool_name: str, inp: dict[str, Any]) -> None:
        self.feed({"type": "control_request", "request_id": rid,
                   "request": {"subtype": "can_use_tool", "tool_name": tool_name, "input": inp}})

    def result(self, *, ok: bool, subtype: str, usage: dict[str, int], cost_usd: float, num_turns: int | None,
               stop_reason: str | None, duration_ms: int, error: str | None = None, model: str | None = None) -> None:
        self.flush()
        self.close_open_tools("(tool không có kết quả: lượt đã kết thúc)")
        obj: dict[str, Any] = {
            "type": "result", "subtype": subtype, "is_error": not ok, "total_cost_usd": cost_usd, "usage": usage,
            "num_turns": num_turns, "result": self.last_text or (error or ""), "stop_reason": stop_reason, "duration_ms": duration_ms,
        }
        if model:
            obj["modelUsage"] = {model: {"inputTokens": usage.get("input_tokens", 0), "outputTokens": usage.get("output_tokens", 0),
                                         "costUSD": cost_usd, "costBasis": "subscription" if not cost_usd else "estimate"}}
        if error:
            obj["errors"] = [error]
        self.feed(obj)


class RpcProc:
    """Một tiến trình JSON-RPC 2.0 (mỗi message một dòng). Request của hub → Future; request của server → `on_request`."""

    def __init__(self, proc: asyncio.subprocess.Process, raw: RawLog | None,
                 on_request: Callable[[str, dict[str, Any], Any], Awaitable[Any]],
                 on_notify: Callable[[str, dict[str, Any]], None]) -> None:
        self.proc = proc
        self.raw = raw
        self.on_request = on_request
        self.on_notify = on_notify
        self._nid = 0
        self._pending: dict[Any, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()
        self.closed = asyncio.Event()

    def _log(self, direction: str, msg: dict[str, Any]) -> None:
        if self.raw is None:  # phiên ngắn không thuộc run (trạng thái/đăng nhập tài khoản) — không ghi raw log
            return
        self.raw.write(json.dumps({"t": round(time.time(), 3), "dir": direction, "msg": msg}, ensure_ascii=False).encode("utf-8"))

    async def _write(self, msg: dict[str, Any]) -> None:
        if self.proc.stdin is None or self.proc.stdin.is_closing():
            raise RpcError("closed", "stdin đã đóng")
        self._log("out", msg)
        self.proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        self._nid += 1
        rid = self._nid
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return await (asyncio.wait_for(fut, timeout) if timeout else fut)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _answer(self, rid: Any, method: str, params: dict[str, Any]) -> None:
        try:
            result = await self.on_request(method, params, rid)
            msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "result": result}
        except asyncio.CancelledError:
            raise
        except RpcError as e:
            msg = {"jsonrpc": "2.0", "id": rid, "error": {"code": e.code if isinstance(e.code, int) else -32603, "message": e.message}}
        except Exception as e:  # noqa: BLE001 — trả lỗi cho server, không làm sập vòng đọc
            log.warning("server request %s lỗi: %r", method, e)
            msg = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": f"cfharness: {type(e).__name__}"}}
        try:
            await self._write(msg)
        except (RpcError, BrokenPipeError, ConnectionResetError) as e:
            log.warning("không trả lời được server request %s: %r", method, e)

    async def pump(self) -> None:
        assert self.proc.stdout is not None
        try:
            while True:
                try:
                    line = await self.proc.stdout.readline()
                except ValueError:  # dòng vượt STREAM_LIMIT
                    continue
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    if self.raw is None:
                        continue
                    self.raw.write(json.dumps({"dir": "in-raw", "line": line.decode("utf-8", "replace")[:2000]}).encode("utf-8"))
                    continue
                if not isinstance(msg, dict):
                    continue
                self._log("in", msg)
                method = msg.get("method")
                if method is None and "id" in msg:  # response
                    fut = self._pending.get(msg["id"])
                    if fut is not None and not fut.done():
                        if "error" in msg:
                            err = msg.get("error") or {}
                            fut.set_exception(RpcError(err.get("code"), str(err.get("message") or "lỗi"), err.get("data")))
                        else:
                            fut.set_result(msg.get("result"))
                elif isinstance(method, str) and "id" in msg:  # server request (hỏi quyền…) — trả lời nền, không chặn vòng đọc
                    t = asyncio.get_running_loop().create_task(self._answer(msg["id"], method, msg.get("params") or {}))
                    self._tasks.add(t)
                    t.add_done_callback(self._tasks.discard)
                elif isinstance(method, str):
                    try:
                        self.on_notify(method, msg.get("params") or {})
                    except Exception:  # noqa: BLE001 — một notification lạ không được làm hỏng run
                        log.exception("xử lý notification %s lỗi", method)
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RpcError("closed", "tiến trình runtime đã thoát"))
            for t in list(self._tasks):
                t.cancel()

    def close_stdin(self) -> None:
        if self.proc.stdin is not None and not self.proc.stdin.is_closing():
            self.proc.stdin.close()


class RpcRuntime(AgentRuntime):
    """Khung chung: spawn + Job Object, vòng đọc, hàng event, dừng cả cây. Lớp con cài đặt `_argv`, `_env`, `_drive`, `_on_notify`,
    `_on_request`, `_send_steer`, `_interrupt_turn`."""

    kind: str = "rpc"

    def __init__(self, cfg: dict[str, Any] | None = None) -> None:
        self.cfg = dict(cfg or {})

    # -- lớp con
    @abc.abstractmethod
    def _argv(self, spec: RunSpec) -> list[str]: ...

    def _env(self, spec: RunSpec) -> dict[str, str]:
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.cfg.get("env") or {}).items()})
        env.update(spec.env_extra or {})
        if self.cfg.get("hub_home"):  # tài khoản Đang dùng của runtime (Cài đặt → Tài khoản AI): CODEX_HOME / GROK_AUTH_PATH riêng
            from .accounts import account_env

            env.update(account_env(self.cfg["hub_home"], self.kind))
        if spec.path_extra:
            env["PATH"] = os.pathsep.join([env.get("PATH", ""), *spec.path_extra])
        return env

    @abc.abstractmethod
    async def _drive(self, h: RunHandle) -> None:
        """Mở phiên + gửi prompt + chờ lượt xong; phát `result` qua `h.internal['synth']`."""

    @abc.abstractmethod
    def _on_notify(self, h: RunHandle, method: str, params: dict[str, Any]) -> None: ...

    @abc.abstractmethod
    async def _on_request(self, h: RunHandle, method: str, params: dict[str, Any], rid: Any) -> Any: ...

    async def _send_steer(self, h: RunHandle, text: str) -> None:
        raise RuntimeError_(f"runtime '{self.name}' không hỗ trợ steer")

    async def _interrupt_turn(self, h: RunHandle) -> None:
        return None

    # -- AgentRuntime
    def binary(self) -> str:
        """`runtime.<kind>.binary`; để mặc định (tên lệnh) mà không có trong PATH → chỗ cài quen thuộc (Volta/npm, ~/.grok/bin)."""
        b = str(self.cfg.get("binary") or self.kind)
        if b == self.kind:
            import shutil

            if not shutil.which(b):
                from .accounts import candidates

                found = candidates(self.kind)
                if found:
                    return found[0]
        return b

    def version(self) -> str | None:
        """`<binary> --version` (cache 10 phút). Chặn — RunManager gọi trong thread (`asyncio.to_thread`)."""
        return binary_version(self.binary())

    async def start(self, spec: RunSpec) -> RunHandle:
        if spec.fork:
            raise RuntimeError_(f"runtime '{self.name}' không hỗ trợ fork phiên")
        argv = exec_argv(self._argv(spec))
        spec.cwd.mkdir(parents=True, exist_ok=True)
        spec.stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_f = open(spec.stderr_log_path, "ab")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(spec.cwd), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=stderr_f,
                env=self._env(spec), **new_group_kwargs(suspended=IS_WINDOWS), limit=STREAM_LIMIT,
            )
        except OSError as e:
            stderr_f.close()
            raise RuntimeError_(f"không spawn được `{argv[0]}`: {e} (cfharness doctor kiểm runtime.{self.kind}.binary)") from e
        if IS_WINDOWS:
            try:
                JOBS.create(spec.run_id).adopt(proc.pid, resume=True)
            except OSError as e:  # không có job = không dừng được hậu duệ → không cho chạy (fail-closed)
                proc.kill()
                stderr_f.close()
                raise RuntimeError_(f"không gán được Job Object cho `{argv[0]}`: {e}") from e
        h = RunHandle(run_id=spec.run_id, spec=spec, adapter=make_adapter(spec), pid=proc.pid, argv_redacted=list(argv),
                      cli_version=_VERSIONS.get(self.binary(), (0.0, None))[1])
        queue: asyncio.Queue = asyncio.Queue()
        raw = RawLog(spec.raw_log_path)
        synth = StreamSynth(h, queue.put_nowait)
        rpc = RpcProc(proc, raw, lambda m, p, rid: self._on_request(h, m, p, rid), lambda m, p: self._on_notify(h, m, p))
        h.internal.update(proc=proc, stderr_f=stderr_f, queue=queue, raw=raw, synth=synth, rpc=rpc, t0=time.monotonic(),
                          accepting=True)
        loop = asyncio.get_running_loop()
        h.internal["pump"] = loop.create_task(rpc.pump())
        h.internal["driver"] = loop.create_task(self._drive_safe(h))
        return h

    async def _drive_safe(self, h: RunHandle) -> None:
        synth: StreamSynth = h.internal["synth"]
        try:
            await self._drive(h)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — lỗi giao thức → run.failed có lý do, không treo
            log.warning("run %s (%s) lỗi: %r", h.run_id, self.name, e)
            if not h.adapter.terminal:
                msg = e.message if isinstance(e, RpcError) else str(e)
                tail = _tail(h.spec.stderr_log_path, 600).strip()
                if tail:  # vd "Not logged in" / "Authentication required" — lý do thật thường chỉ có ở stderr
                    msg = f"{msg} | stderr: {tail[-400:]}"
                synth.result(ok=False, subtype="error_during_execution", usage={}, cost_usd=0.0, num_turns=None, stop_reason="error",
                             duration_ms=self._elapsed_ms(h), error=f"{self.name}: {msg}"[:2000])
        finally:
            h.internal["accepting"] = False
            await self._shutdown(h)

    async def wait_or_exit(self, h: RunHandle, fut: "asyncio.Future[Any]") -> Any:
        """Chờ `fut` (vd turn/completed) nhưng thôi chờ khi tiến trình runtime thoát — không treo run vì CLI chết giữa lượt."""
        closed = asyncio.ensure_future(h.internal["rpc"].closed.wait())
        try:
            await asyncio.wait({fut, closed}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            closed.cancel()
        if fut.done():
            return fut.result()
        raise RpcError("closed", "tiến trình runtime đã thoát giữa lượt")

    def _elapsed_ms(self, h: RunHandle) -> int:
        return int((time.monotonic() - h.internal["t0"]) * 1000)

    async def _shutdown(self, h: RunHandle, grace_s: float = 5.0) -> None:
        """Đóng stdin → chờ thoát → giết cả cây (Job Object / process group)."""
        proc: asyncio.subprocess.Process = h.internal["proc"]
        h.internal["rpc"].close_stdin()
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_s)
        except asyncio.TimeoutError:
            self._kill(h)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("run %s: tiến trình %s không thoát sau khi giết", h.run_id, proc.pid)
        if IS_WINDOWS:  # tiến trình con còn sót (MCP stdio, codex.exe con của .cmd) trong Job Object của run
            job = JOBS.get(h.run_id)
            if job is not None:
                job.terminate()

    def _kill(self, h: RunHandle) -> None:
        proc: asyncio.subprocess.Process = h.internal["proc"]
        if proc.returncode is not None:
            return
        if IS_WINDOWS:
            job = JOBS.get(h.run_id)
            if job is None or not job.terminate():
                kill_tree(proc.pid)
            return
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()

    async def events(self, handle: RunHandle) -> AsyncIterator[HubEvent]:
        queue: asyncio.Queue = handle.internal["queue"]
        driver: asyncio.Task = handle.internal["driver"]
        driver.add_done_callback(lambda _t: queue.put_nowait(_DONE))
        try:
            while True:
                ev = await queue.get()
                if ev is _DONE:
                    break
                yield ev
            while not queue.empty():  # event còn lại sau khi driver xong
                ev = queue.get_nowait()
                if ev is not _DONE:
                    yield ev
            proc: asyncio.subprocess.Process = handle.internal["proc"]
            handle.exit_code = proc.returncode if proc.returncode is not None else await proc.wait()
            if handle.stop_requested and handle.exit_code == 0:
                handle.exit_code = -2
            await asyncio.gather(handle.internal["pump"], return_exceptions=True)
        finally:
            JOBS.finished(handle.run_id)
            handle.raw_log_final = handle.internal["raw"].close()
            f = handle.internal.get("stderr_f")
            if f:
                f.close()
            handle.stderr_tail = _tail(handle.spec.stderr_log_path)

    async def send(self, handle: RunHandle, user_message: str) -> None:
        if not handle.internal.get("accepting"):
            raise RuntimeError_("run không nhận thêm tin (lượt đã xong)")
        await self._send_steer(handle, user_message)

    async def interrupt(self, handle: RunHandle) -> None:
        try:
            await asyncio.wait_for(self._interrupt_turn(handle), 10)
        except Exception as e:  # noqa: BLE001 — best-effort: ngắt mềm hỏng thì giết tiến trình (an toàn hơn)
            log.warning("interrupt %s lỗi: %r — giết tiến trình", handle.run_id, e)
            self._kill(handle)

    async def stop(self, handle: RunHandle, grace_s: float = 10.0) -> None:
        handle.stop_requested = True
        proc: asyncio.subprocess.Process | None = handle.internal.get("proc")
        if proc is None or proc.returncode is not None:
            return
        try:
            await asyncio.wait_for(self._interrupt_turn(handle), min(grace_s, 5.0))
        except Exception:  # noqa: BLE001 — best-effort: ngắt mềm hỏng → vẫn giết bên dưới
            pass
        try:
            await asyncio.wait_for(asyncio.shield(handle.internal["driver"]), timeout=grace_s)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — best-effort: hết giờ chờ → giết bên dưới
            pass
        if proc.returncode is None:
            self._kill(handle)

    # -- hỏi quyền qua guard (fail-closed)
    async def ask(self, h: RunHandle, tool_name: str, tool_input: dict[str, Any], tool_use_id: str | None) -> tuple[bool, str]:
        rid = "perm-" + uuid.uuid4().hex[:10]
        h.internal["synth"].control_request(rid, tool_name, tool_input)
        allow = tool_allowlist(h.spec)
        if allow is not None and not tool_name.startswith("mcp__") and tool_name not in allow:
            # `tool_policy.tools` của agent/run (vd khung strict của điều phối viên) — như `--tools` của Claude: ngoài danh sách = KHÔNG có
            msg = f"[cfharness] Tool {tool_name} is not allowed for this agent (allowed: {', '.join(sorted(allow)) or 'none'})."
            h.internal["synth"].feed({"type": "system", "subtype": "permission_denied", "tool_name": tool_name, "tool_use_id": tool_use_id,
                                      "message": msg})
            return False, msg
        handler = h.spec.control_handler
        timeout = float(self.cfg.get("approval_timeout_s") or 1800)
        try:
            if handler is None:
                raise RuntimeError_("run không có bộ xử lý quyền")
            d = await asyncio.wait_for(handler({"subtype": "can_use_tool", "tool_name": tool_name, "input": tool_input,
                                                "tool_use_id": tool_use_id}, rid), timeout)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return False, f"[cfharness] No decision within {int(timeout)}s, so this was denied."
        except Exception as e:  # noqa: BLE001 — fail-closed
            log.warning("hỏi quyền %s lỗi: %r", rid, e)
            return False, "[cfharness] The hub could not evaluate this request, so it was denied (fail-closed)."
        if isinstance(d, dict) and d.get("behavior") == "allow":
            return True, ""
        msg = str((d or {}).get("message") or "[cfharness] denied") if isinstance(d, dict) else "[cfharness] denied"
        h.internal["synth"].feed({"type": "system", "subtype": "permission_denied", "tool_name": tool_name, "tool_use_id": tool_use_id,
                                  "message": msg})
        return False, msg


_VERSIONS: dict[str, tuple[float, str | None]] = {}


def binary_version(binary: str, cache_s: float = 600.0) -> str | None:
    """Dòng đầu của `<binary> --version` (vd `codex-cli 0.172.0`, `grok 1.0.92 (b06756fd6256)`) → số phiên bản; None = không chạy được."""
    import re
    import subprocess

    from ..oscompat import no_window_kwargs

    hit = _VERSIONS.get(binary)
    if hit is not None and time.monotonic() - hit[0] < cache_s:
        return hit[1]
    try:
        out = subprocess.run(exec_argv([binary, "--version"]), capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                             **no_window_kwargs()).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    m = re.search(r"(\d+\.\d+\.\d+)", out or "")
    v = m.group(1) if m else None
    _VERSIONS[binary] = (time.monotonic(), v)
    return v


WRITE_TOOLS = frozenset({"Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"})
WEB_TOOLS = frozenset({"WebFetch", "WebSearch"})


def tool_allowlist(spec: RunSpec) -> set[str] | None:
    """`tool_policy.tools` (tên tool kiểu Claude) — None = không giới hạn (guard quyết từng lần)."""
    tools = (spec.tool_policy or {}).get("tools")
    return {str(t) for t in tools} if isinstance(tools, list) else None


def _tail(p: Path, n: int = 4000) -> str:
    try:
        data = p.read_bytes()
    except OSError:
        return ""
    return data[-n:].decode("utf-8", "replace")


def event(handle: RunHandle, type_: str, payload: dict[str, Any]) -> HubEvent:
    return make_event(type_, handle.spec.agent_id, payload, run_id=handle.run_id, session_id=handle.spec.session_id)
