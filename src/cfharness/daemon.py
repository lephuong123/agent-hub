"""Hub daemon asyncio: máy chủ JSON-RPC 2.0 trên Unix socket (`HUB_HOME/hub.sock`).

Daemon là writer DUY NHẤT của `hub.db`. CLI/TUI/MCP chỉ là client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import auth as _auth
from . import transport
from .errors import BLOCKED, HubError, SYSTEM_ERROR
from .events import TERMINAL_RUN_EVENTS
from .hub import Hub
from .logs import setup_logging
from .paths import HubPaths
from .oscompat import IS_WINDOWS, JOBS, detached_kwargs, ensure_hidden_console, same_user
from .rpc import INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR, DaemonUnavailable, RpcClient, encode
from .runtime import AgentRuntime

log = logging.getLogger("cfharness.daemon")
Handler = Callable[[dict[str, Any]], Awaitable[Any] | Any]


class Daemon:
    def __init__(self, paths: HubPaths, *, runtime: AgentRuntime | None = None) -> None:
        self.paths = paths
        self.hub = Hub(paths, runtime=runtime)
        # H-01: token người (file 0600 `<socket>.auth`, sinh lại mỗi lần khởi tạo) + xác định người gọi theo pid ngang hàng
        self.auth_token: str | None = None
        try:
            self.auth_token = _auth.issue_token(paths.socket)
        except OSError:  # pragma: no cover — không có token → mọi method nhạy cảm qua socket bị từ chối (fail-closed)
            log.exception("không tạo được token người cạnh socket %s", paths.socket)
        self.callers = _auth.CallerResolver(self.hub)
        self._mutating: frozenset[str] = frozenset()
        self.server: asyncio.base_events.Server | None = None
        self.stopping = asyncio.Event()
        self.methods: dict[str, Handler] = {
            "hub.ping": lambda p: {"pong": True, "pid": os.getpid()},
            "hub.status": lambda p: self.hub.status(),
            "hub.shutdown": self._shutdown,
            "auth.reset": self._auth_reset,
            "agent.create": lambda p: self.hub.agents.create(p.get("config") or {}, p.get("soul")),
            "agent.list": lambda p: self._agent_list(p),
            "agent.get": lambda p: self.hub.agents.show(_req(p, "id")),
            "agent.update": lambda p: self.hub.agents.update(_req(p, "id"), p.get("changes") or {}, soul=p.get("soul")),
            "agent.delete": lambda p: self.hub.agents.remove(_req(p, "id")),
            "agent.restore": lambda p: self.hub.agents.restore(_req(p, "id")),
            "agent.pause": lambda p: self._pause(p, True),
            "agent.resume": lambda p: self._pause(p, False),
            "agent.revisions": lambda p: self.hub.store.list_revisions(_req(p, "id")),
            "run.start": lambda p: self.hub.runs.submit(
                _req(p, "agent"), _req(p, "prompt"), cwd=p.get("cwd"), budget_usd=p.get("budget_usd"), model=p.get("model")
            ),
            "run.list": lambda p: self.hub.store.list_runs(agent_id=p.get("agent"), status=p.get("status"), limit=int(p.get("limit", 50))),
            "run.get": self._run_get,
            "run.stop": lambda p: self.hub.runs.stop(_req(p, "id")),
            "run.wait": self._run_wait,
            "run.logs": self._run_logs,
            "estop.set": lambda p: self.hub.runs.set_estop(bool(_req(p, "on")), reason=p.get("reason"), stop_running=bool(p.get("stop_running"))),
            "estop.get": lambda p: self.hub.runs.estop,
            "config.get": self._config_get,
            "config.set": self._config_set,
            "events.query": lambda p: [
                e.to_wire() for e in self.hub.store.query_events(run_id=p.get("run_id"), agent_id=p.get("agent_id"), limit=int(p.get("limit", 10_000)))
            ],
        }
        # M1 luồng B: approval.*, guard.*, notify.*, run.continue|resume, agent.purge (guard/rpc.py)
        from .guard.rpc import register_rpc as _register_b

        _register_b(self)
        # M2: tui.snapshot, run.steer, template.*, agent.create_from_template, run.takeover* (tui/rpc.py)
        from .tui.rpc import register_rpc as _register_m2

        _register_m2(self)
        # M3: team.*, task.*, taskself.call, run.revert|diff, workspace.*, report.*, agent.clone|export|import (dispatch/rpc.py)
        from .dispatch.rpc import register_rpc as _register_m3

        _register_m3(self)
        # M4-B: golden.*, eval.*, digest.*, curator.*, regression.* (learning_ops/rpc.py — tự bọc audit method ghi)
        from .learning_ops.rpc import register_rpc as _register_m4b

        _register_m4b(self)
        # M4-A: learn.*, knowledge.*, learnself.call (MCP hub-self); bọc run.stop → Signal cancel (learning/rpc.py)
        from .learning.rpc import register_rpc as _register_m4a

        _register_m4a(self)
        # M5-B: curation.*, report.kpi|overhead, telemetry.* (curation/rpc.py — tự bọc audit method ghi)
        from .curation.rpc import register_rpc as _register_m5b

        _register_m5b(self)
        # UX: question.list|get|answer|escalate|cancel (questions/rpc.py — tự bọc audit method ghi)
        from .questions.rpc import register_rpc as _register_questions

        _register_questions(self)
        # Hệ thống ngoài hub: system.list|get|scan|add|rm|lock|job (systems/rpc.py — tự bọc audit method ghi)
        from .systems.rpc import register_rpc as _register_systems

        _register_systems(self)
        # Lịch chạy tự động: schedule.list|get|preview|create|update|pause|resume|rm|run_now (schedules/rpc.py — tự bọc audit method ghi)
        from .schedules.rpc import register_rpc as _register_schedules

        _register_schedules(self)
        # M1 luồng A: thêm mcp.*/secret.*/audit.*/feedback.* và bọc audit các method thay đổi trạng thái.
        # GIỮ Ở CUỐI __init__ — method thêm sau dòng này sẽ không được audit tự động.
        from .mcp_server.rpc import register_rpc

        register_rpc(self)
        # SEC: guard.integrity.* (toàn vẹn policy/golden/config/hub.db) — method KHÔNG ghi trạng thái nghiệp vụ, tự ghi audit
        from .guard.integrity import register_rpc as _register_integrity

        _register_integrity(self)
        # Telegram: telegram.* (setup/allow/…) + gắn daemon cho bot (lệnh chat đi qua `call_as`) — chat/rpc.py
        from .chat.rpc import register_rpc as _register_tg

        _register_tg(self)
        # Slack 2 chiều: slack.* (setup/allow/…) + gắn daemon cho bot Slack — chat/slack/rpc.py
        from .chat.slack.rpc import register_rpc as _register_slack

        _register_slack(self)
        # M7 MGR-CORE: manager.* + managerself.call (tool hub-self của run manager) — manager/rpc.py (tự bọc audit method ghi)
        from .manager.rpc import register_rpc as _register_manager

        _register_manager(self)
        self._mutating = _auth.all_mutating()

    # ------------------------------------------------------------------ handlers
    def _agent_list(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.hub.agents.list(include_archived=bool(p.get("archived")))
        for r in rows:
            live = self.hub.aggregator.agents.get(r["id"])
            r["last_tool"] = live.last_tool if live else None
            r["cost_today_usd"] = round(self.hub.store.cost_today(r["id"]), 8)
        ov = self.hub.guard.sandbox_overrides()  # SEC: nhãn cảnh báo ngoại lệ sandbox cho TUI/web
        trusts = self.hub.guard.trust_all()
        for r in rows:
            r["sandbox_overrides"] = ov.get(r["id"]) or {}
            r["trust"] = trusts.get(r["id"])
        return rows

    def _pause(self, p: dict[str, Any], paused: bool) -> dict[str, Any]:
        res = self.hub.agents.set_paused(_req(p, "id"), paused)
        if not paused:
            self.hub.runs.tick()
        return res

    def _run_get(self, p: dict[str, Any]) -> dict[str, Any]:
        run = self.hub.store.find_run(_req(p, "id"))
        if run is None:
            raise HubError(f"không tìm thấy run '{p['id']}'", code=-32004)
        if p.get("events"):
            run["events"] = [e.to_wire() for e in self.hub.store.query_events(run_id=run["id"])]
        return run

    async def _run_wait(self, p: dict[str, Any]) -> dict[str, Any]:
        run = self.hub.store.find_run(_req(p, "id"))
        if run is None:
            raise HubError(f"không tìm thấy run '{p['id']}'", code=-32004)
        return await self.hub.runs.wait(run["id"], p.get("timeout"))

    def _run_logs(self, p: dict[str, Any]) -> dict[str, Any]:
        run = self.hub.store.find_run(_req(p, "id"))
        if run is None:
            raise HubError(f"không tìm thấy run '{p['id']}'", code=-32004)
        path = run.get("stderr_log_path")
        text = Path(path).read_text(encoding="utf-8", errors="replace") if path and Path(path).exists() else ""
        return {"run_id": run["id"], "stderr": text, "error": run.get("error"), "raw_log_path": run.get("raw_log_path")}

    def _config_get(self, p: dict[str, Any]) -> Any:
        try:
            return self.hub.config.get(p.get("key"))
        except KeyError:
            raise HubError(f"không có khoá cấu hình: {p.get('key')}", code=-32004) from None

    def _config_set(self, p: dict[str, Any]) -> Any:
        key = _req(p, "key")
        try:
            value = self.hub.config.set(key, p.get("value"))
        except ValueError as e:
            raise HubError(str(e)) from None
        from .events import make_event

        self.hub.publish(make_event("hub.config_changed", None, {"key": key, "value": value}))
        if key.startswith("hub."):
            self.hub.runs.tick()  # vd tăng max_concurrent_runs → khởi chạy run đang chờ ngay
        return {key: value}

    def _auth_reset(self, p: dict[str, Any]) -> dict[str, Any]:
        self.hub.runs.clear_auth_block()
        return {"ok": True}

    def _shutdown(self, p: dict[str, Any]) -> dict[str, Any]:
        asyncio.get_event_loop().call_soon(self.stopping.set)
        return {"stopping": True}

    # ------------------------------------------------------------------ server
    # ------------------------------------------------------------------ xác thực người gọi (H-01, H-05)
    def _caller_for(self, method: str, params: dict[str, Any], peer: int | None, via_socket: bool) -> _auth.Caller:
        token = params.pop(_auth.AUTH_PARAM, None) if isinstance(params, dict) else None
        if not via_socket:
            return _auth.Caller(via="internal")
        if method not in _auth.HUMAN_ONLY and method not in self._mutating and method not in _auth.SCOPED_READ:
            return _auth.Caller(via="socket", pid=peer, token_ok=_auth.token_ok(token, self.auth_token))  # đường nóng: guard.check, đọc
        return self.callers.resolve(peer, token, self.auth_token)

    def _deny_caller(self, method: str, caller: _auth.Caller, why: str | None = None) -> HubError:
        from .approvals import audit_record
        from .events import make_event

        if caller.manager:  # M7: điều phối viên vượt quyền — audit actor manager:<agent>, lỗi nêu lý do cụ thể
            info = {"method": method, "reason": why, **caller.to_dict()}
            try:
                self.hub.publish(make_event("auth.denied", caller.agent_id, info, run_id=caller.run_id, source="hub"))
                self.hub.audit.record("auth.denied", caller.agent_id, caller.run_id, info, actor=f"manager:{caller.manager}", channel="manager",
                                      target=method, summary=f"manager bị từ chối: {method} — {why}")
            except Exception:  # pragma: no cover
                log.exception("không ghi được auth.denied (manager)")
            return HubError(f"'{method}' bị từ chối cho điều phối viên: {why}. Đây là giới hạn an toàn do người đặt — báo người nếu cần.",
                            code=BLOCKED, data={"reason": "manager_forbidden", "detail": why, "caller": caller.to_dict()})
        detail = why
        why = "lời gọi đến từ tiến trình của một run agent" if caller.is_agent else "thiếu token người"
        if detail:
            why = f"{detail} ({why})"
        info = {"method": method, **caller.to_dict(), **({"reason": detail} if detail else {})}
        try:
            self.hub.publish(make_event("auth.denied", caller.agent_id, info, run_id=caller.run_id, source="hub"))
            audit_record("auth.denied", agent_id=caller.agent_id, run_id=caller.run_id, data=info)
        except Exception:  # pragma: no cover
            log.exception("không ghi được auth.denied")
        return HubError(
            f"'{method}' chỉ dành cho NGƯỜI (CLI/TUI/MCP của người tự gửi token ở {_auth.token_path(self.paths.socket)}) — "
            f"bị từ chối: {why}. Agent không được tự duyệt, sửa chính sách hay mạo danh người.",
            code=BLOCKED,
            data={"reason": "human_only", "caller": caller.to_dict()},
        )

    def _agent_propose(self, params: dict[str, Any], caller: _auth.Caller) -> Any:
        """H-05: `learn.propose` từ agent (hoặc thiếu token) → created_by=agent, đi qua guard học đầy đủ, không mạo danh người."""
        def _r(k: str) -> Any:
            if params.get(k) is None:
                raise _ParamError(f"thiếu tham số '{k}'")
            return params[k]

        res = self.hub.learning.pipeline.propose(
            target=_r("target"), op=_r("op"), payload=params.get("payload") or {}, agent_id=caller.agent_id or params.get("agent"),
            scope=params.get("scope") or "agent", team_id=params.get("team"), target_ref=params.get("target_ref"),
            rationale=params.get("rationale") or "", created_by="agent", run_id=caller.run_id,
        )
        return res

    async def call_as(self, caller: _auth.Caller, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Gọi method thay mặt một kênh của người KHÔNG qua socket (bot Telegram trong daemon — chat/bot.py).
        Vẫn đi qua `_dispatch`: cổng HUMAN_ONLY theo `caller`, CURRENT_CALLER, bọc audit. Trả phản hồi JSON-RPC."""
        req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": dict(params or {})}
        return await self._dispatch(req, as_caller=caller)  # type: ignore[return-value]

    async def _dispatch(self, req: Any, *, peer: int | None = None, via_socket: bool = False, as_caller: _auth.Caller | None = None) -> dict[str, Any] | None:
        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "Invalid Request"}}
        rid = req.get("id")
        method = req["method"]
        params = req.get("params") or {}
        fn = self.methods.get(method)
        if fn is None:
            if isinstance(params, dict):
                params.pop(_auth.AUTH_PARAM, None)
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": METHOD_NOT_FOUND, "message": f"không có method {method}"}}
        ctx_token = None
        try:
            if as_caller is not None:  # kênh người trong daemon (Telegram): danh tính do kênh xác lập, không tin `_auth`
                if isinstance(params, dict):
                    params.pop(_auth.AUTH_PARAM, None)
                caller = as_caller
                if caller.manager and isinstance(params, dict):
                    # M7: thao tác của điều phối viên — audit actor `manager:<agent>` (không tin `_audit` từ đâu khác)
                    params["_audit"] = {"channel": "manager", "actor": f"manager:{caller.manager}", "run_id": caller.run_id}
                elif not caller.human and isinstance(params, dict):
                    params["_audit"] = {"channel": caller.via, "actor": "unknown"}
            else:
                caller = self._caller_for(method, params, peer, via_socket)
            if not caller.human and caller.via == "socket" and isinstance(params, dict) and (method in _auth.HUMAN_ONLY or "_audit" in params or caller.is_agent):
                # mọi lời gọi không phải người: audit ghi đúng là agent (không tin `_audit.actor` do client tự khai)
                params["_audit"] = {"channel": "agent" if caller.is_agent else "socket", "actor": "agent", "run_id": caller.run_id}
            if caller.manager:
                # M7: người gọi = điều phối viên (chỉ dựng trong daemon). Chỉ MANAGER_ALLOWED + kiểm trường cấm theo method.
                if method not in _auth.MANAGER_ALLOWED:
                    raise self._deny_caller(method, caller, why="method ngoài quyền điều phối viên (MANAGER_ALLOWED)")
                why = self.hub.manager.check_rpc(caller, method, params if isinstance(params, dict) else {})
                if why:
                    raise self._deny_caller(method, caller, why=why)
            elif not caller.human and (why := self.hub.manager.agent_target_block(method, params if isinstance(params, dict) else {})
                                       or self.hub.manager.run_socket_block(caller, method)):
                # M7 MGR-3: agent/tiến trình không phải người không điều khiển được run/agent điều phối viên
                raise self._deny_caller(method, caller, why=why)
            elif method in _auth.HUMAN_ONLY and not caller.human:
                if method in _auth.DOWNGRADE:
                    params.pop("_audit", None)
                    return {"jsonrpc": "2.0", "id": rid, "result": self._agent_propose(params, caller)}
                raise self._deny_caller(method, caller)
            ctx_token = _auth.CURRENT_CALLER.set(caller)
            res = fn(params)
            if asyncio.iscoroutine(res):
                res = await res
            return {"jsonrpc": "2.0", "id": rid, "result": res}
        except HubError as e:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": e.code, "message": e.message, "data": e.data}}
        except _ParamError as e:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": INVALID_PARAMS, "message": str(e)}}
        except Exception as e:
            log.exception("lỗi xử lý %s", method)
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": SYSTEM_ERROR, "message": f"lỗi nội bộ: {e!r}"}}
        finally:
            if ctx_token is not None:
                _auth.CURRENT_CALLER.reset(ctx_token)

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = transport.peer_pid(writer.get_extra_info("socket"))
        if IS_WINDOWS and (peer is None or not same_user(peer)):
            # TCP loopback: mọi user trên máy nối được — chỉ nhận tiến trình CÙNG user (file socket 0600 trên POSIX)
            log.warning("từ chối kết nối từ pid %s (không xác định được / khác user)", peer)
            writer.close()
            return
        try:
            while not reader.at_eof():
                line = await reader.readline()
                if not line:
                    break
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    writer.write(encode({"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "Parse error"}}))
                    await writer.drain()
                    continue
                if isinstance(req, dict) and req.get("method") == "events.subscribe":
                    if isinstance(req.get("params"), dict):
                        req["params"].pop(_auth.AUTH_PARAM, None)
                    await self._subscribe(req, reader, writer)
                    break
                resp = await self._dispatch(req, peer=peer, via_socket=True)
                if resp is not None and "id" in req:
                    writer.write(encode(resp))
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                writer.close()
            except Exception:  # pragma: no cover
                pass

    async def _subscribe(self, req: dict[str, Any], reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        p = req.get("params") or {}
        agent_id, run_id = p.get("agent_id"), p.get("run_id")
        if run_id:
            run = self.hub.store.find_run(run_id)
            if run is None:
                writer.write(encode({"jsonrpc": "2.0", "id": req.get("id"), "error": {"code": -32004, "message": f"không tìm thấy run '{run_id}'"}}))
                await writer.drain()
                return
            run_id, agent_id = run["id"], run["agent_id"]
        since = p.get("since_seq")
        if p.get("epoch") and p["epoch"] != self.hub.bus.epoch:
            since = 0  # epoch đổi → client reset watermark
        sub = self.hub.bus.subscribe(
            agent_id,
            run_id=run_id,
            since_seq=since,
            level=p.get("level", "summary"),
            backfill=self.hub.backfill(run_id=run_id, agent_id=agent_id),
        )
        until_end = bool(p.get("until_run_end")) and run_id is not None
        writer.write(
            encode(
                {
                    "jsonrpc": "2.0",
                    "id": req.get("id"),
                    "result": {"epoch": self.hub.bus.epoch, "last_seq": self.hub.bus.last_seq, "truncated": sub.truncated, "subscription": sub.id},
                }
            )
        )
        await writer.drain()
        # run đã kết thúc từ trước và không có gì để replay → đóng ngay
        if until_end:
            run = self.hub.store.get_run(run_id)  # type: ignore[arg-type]
            if run and run["status"] not in ("queued", "running") and sub.queue.empty():
                writer.write(encode({"jsonrpc": "2.0", "method": "events.end", "params": {"reason": "run_finished", "status": run["status"]}}))
                await writer.drain()
                sub.close()
                return
        eof_task = asyncio.ensure_future(reader.read())  # client đóng kết nối
        reason = "closed"
        try:
            while True:
                get_task = asyncio.ensure_future(sub.get())
                done, _ = await asyncio.wait({get_task, eof_task}, return_when=asyncio.FIRST_COMPLETED)
                if eof_task in done:
                    get_task.cancel()
                    reason = "client_closed"
                    break
                ev = get_task.result()
                if ev is None:
                    reason = sub.closed_reason or "closed"
                    break
                writer.write(encode({"jsonrpc": "2.0", "method": "event", "params": ev.to_wire()}))
                await writer.drain()
                if until_end and ev.type in TERMINAL_RUN_EVENTS and ev.run_id == run_id and ev.source == "hub":
                    reason = "run_finished"
                    break
            if reason != "client_closed":
                writer.write(encode({"jsonrpc": "2.0", "method": "events.end", "params": {"reason": reason}}))
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            eof_task.cancel()
            sub.close()

    async def serve(self) -> None:
        sock = self.paths.socket
        if sock.exists():
            try:
                RpcClient(sock, timeout=2).call("hub.ping")
                raise SystemExit(f"daemon đã chạy (socket {sock})")
            except DaemonUnavailable:
                sock.unlink(missing_ok=True)
            except Exception:
                sock.unlink(missing_ok=True)
        self.hub.start()
        self.server = await transport.start_server(self._handle_conn, sock, limit=16 * 1024 * 1024)
        self.paths.pidfile.write_text(str(os.getpid()), encoding="utf-8")
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stopping.set)
            except (NotImplementedError, RuntimeError):  # pragma: no cover
                pass
        log.info("daemon sẵn sàng tại %s (epoch %s)", sock, self.hub.bus.epoch)
        try:
            await self.stopping.wait()
        finally:
            log.info("daemon đang dừng")
            self.server.close()
            await self.hub.stop()
            JOBS.close_all()
            sock.unlink(missing_ok=True)
            self.paths.pidfile.unlink(missing_ok=True)


class _ParamError(Exception):
    pass


def _req(p: dict[str, Any], key: str) -> Any:
    if key not in p or p[key] is None:
        raise _ParamError(f"thiếu tham số '{key}'")
    return p[key]


def run_foreground(paths: HubPaths) -> None:
    paths.ensure()
    if sys.stdout is None or sys.stderr is None:  # pythonw (dịch vụ tự khởi động trên Windows): không có console → ghi daemon.out
        out = open(paths.logs_dir / "daemon.out", "a", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = sys.stdout or out
        sys.stderr = sys.stderr or out
    console = ensure_hidden_console()  # Windows/pythonw: console ẩn cho claude + hậu duệ (không bật cửa sổ, CTRL_BREAK chạy được)
    setup_logging(paths.logs_dir)
    if console not in ("posix", "existing"):
        logging.getLogger("cfharness.daemon").info("không có console → đã lấy console ẩn (%s)", console)
    from .redact import install_log_filter, install_stderr_masking

    install_stderr_masking()  # H-12: log thư viện ra stderr (daemon.out) cũng che
    install_log_filter()  # H-12: log của daemon che theo giá trị secret
    install_log_filter(logging.getLogger("cfharness"))
    asyncio.run(Daemon(paths).serve())


def spawn_background(paths: HubPaths, wait_s: float = 15.0) -> int:
    """Khởi chạy daemon nền (tách session), chờ socket sẵn sàng. Trả pid."""
    paths.ensure()
    out = open(paths.logs_dir / "daemon.out", "ab")
    env = dict(os.environ)
    env["CFHARNESS_HOME"] = str(paths.home)
    proc = subprocess.Popen(
        [sys.executable, "-m", "cfharness", "daemon", "run"],
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=out,
        **detached_kwargs(),
        env=env,
        close_fds=True,
    )
    client = RpcClient(paths.socket, timeout=2)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"daemon thoát sớm (mã {proc.returncode}); xem {paths.logs_dir / 'daemon.out'}")
        try:
            client.call("hub.ping")
            return proc.pid
        except DaemonUnavailable:
            time.sleep(0.1)
    raise RuntimeError(f"daemon không sẵn sàng sau {wait_s}s; xem {paths.logs_dir / 'daemon.out'}")
