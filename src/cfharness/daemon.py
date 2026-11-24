"""Hub daemon asyncio: máy chủ JSON-RPC 2.0 trên Unix socket (`HUB_HOME/hub.sock`).

Daemon là writer DUY NHẤT của `hub.db`. CLI/TUI/MCP chỉ là client.
"""

from __future__ import annotations

import asyncio
import inspect
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
from . import trace as _trace
from . import transport
from .config import HubConfig
from .errors import BLOCKED, HubError, SYSTEM_ERROR
from .events import TERMINAL_RUN_EVENTS
from .hub import Hub
from .loop_watchdog import DEFAULT_STALL_S, LoopWatchdog
from .logs import (DEFAULT_BACKUP_COUNT, DEFAULT_MAX_AGE_DAYS, DEFAULT_MAX_BYTES, LEVELS, asyncio_exception_handler,
                   fault_file as _fault_file, install_crash_handlers, rotate_if_large, setup_logging)
from .paths import HubPaths
from .oscompat import IS_WINDOWS, JOBS, detached_kwargs, ensure_hidden_console, same_user
from .rpc import INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR, DaemonUnavailable, RpcClient, RpcError, encode
from .runtime import AgentRuntime

log = logging.getLogger("cfharness.daemon")
rpc_log = logging.getLogger("cfharness.rpc")  # B3 access log (plan-logging.md): một bản ghi metadata mỗi lượt RPC
DEFAULT_SLOW_RPC_MS = 2000
# method CHỜ LÂU theo thiết kế (long-poll / giữ chờ người duyệt) — không cảnh báo `rpc.slow`
SLOW_EXEMPT = frozenset({"run.wait", "mcp.events", "mcp.task_events", "guard.check"})
Handler = Callable[[dict[str, Any]], Awaitable[Any] | Any]
CLIENT_PARAM = "_client"  # L3: metadata client (web: session_ref/ip; MCP: transport/level) — daemon pop, chỉ nhận theo loại token
from .mcp_server.rpc import READ_COALESCE_S as _READ_COALESCE_S, SENSITIVE_READS as _SENSITIVE_READS  # noqa: E402
RPC_LIMIT = 16 * 1024 * 1024
LISTENER_CHECK_S = 2.0  # chu kỳ kiểm socket nghe còn mở (`Daemon._watch_listener`)
# RPC CHỈ ĐỌC chạy trên luồng đọc của Store (`Store.read`, kết nối `mode=ro`) thay vì trên event loop — một truy vấn chậm không còn
# làm đứng mọi RPC khác + luồng SSE. Chọn theo ĐO trên bản sao hub.db thật 1,5 GB (PR perf/daemon-offload-reads): handler ĐỒNG BỘ,
# chỉ SELECT qua `store.conn`, không đụng trạng thái RAM dùng chung (bus, RunManager, cache service). Thêm method vào đây → thêm vào
# `tests/test_daemon_offload.py` (so kết quả với đường cũ). KHÔNG thêm method ghi (đã chặn: method ∈ MUTATING bị bỏ qua + log lỗi).
OFFLOAD_READS = frozenset({
    "run.list",  # danh sách run (trang Run, TUI)
    "events.query",  # replay transcript một run / agent (đổi tab, mở run) — ~100 ms với run 4k event
    "task.list",  # bảng việc — mỗi task thêm 3 truy vấn con
    "learn.feed",  # quét `events` theo ts + LIKE — 0,4–0,7 s
    "learn.proposals",
    "report.get", "report.hangs", "report.kpi", "report.overhead", "report.cost",  # báo cáo quét `runs`/`events` — 0,1–0,6 s
    "approval.stats",
    "audit.list", "audit.activity",
    "system.list",  # chờ PowerShell/launchctl (tiến trình con, 1,3–2,6 s) — không phải SQLite nhưng cũng chặn loop
})
LOOP_LAG_INTERVAL_S = 1.0  # chu kỳ đo độ trễ event loop (`Daemon._loop_lag`)
from .perf import DEFAULT_LOOP_LAG_WARN_MS  # noqa: E402


class Daemon:
    def __init__(self, paths: HubPaths, *, runtime: AgentRuntime | None = None) -> None:
        self.paths = paths
        self.hub = Hub(paths, runtime=runtime)
        # H-01: token người (file 0600 `<socket>.auth`, sinh lại mỗi lần khởi tạo) + xác định người gọi theo pid ngang hàng.
        # KHÔNG ghi đè file khi socket đang có daemon sống: bản thứ hai (vd launchd thử tiếp quản trong lúc daemon chạy tay) sẽ
        # thoát ở `serve()`, nhưng nếu đã ghi file thì token trong file lệch với daemon đang chạy → mọi lệnh của người bị từ chối.
        self.auth_token: str | None = None
        # L3 (plan-logging.md A2): token RIÊNG cho tiến trình web / MCP server (`<socket>.web.auth`, `<socket>.mcp.auth`) — daemon biết lời
        # gọi đến từ web/MCP theo token khớp, không tin `_audit.channel`. Sinh lại cùng token người.
        self.client_tokens: dict[str, str] = {}
        from .paths import ensure_socket_dir

        ensure_socket_dir(paths.socket)  # đa instance P1: socket dự phòng trong thư mục riêng 0700 của user (UnsafeSocketDir → không khởi động)
        if not _live_daemon(paths.socket):
            try:
                self.auth_token = _auth.issue_token(paths.socket)
            except OSError:  # pragma: no cover — không có token → mọi method nhạy cảm qua socket bị từ chối (fail-closed)
                log.exception("không tạo được token người cạnh socket %s", paths.socket)
            for kind in _auth.TOKEN_KINDS[1:]:
                try:
                    self.client_tokens[kind] = _auth.issue_token(paths.socket, kind)
                except OSError:  # pragma: no cover — thiếu token web/mcp → client đó dùng token người (ghi os:<user>)
                    log.exception("không tạo được token %s cạnh socket %s", kind, paths.socket)
        self._reads: dict[tuple[str, str, str], list[Any]] = {}  # A4: cửa sổ gộp lượt đọc nhạy cảm (RAM; khởi động lại = dòng mới)
        self.callers = _auth.CallerResolver(self.hub)
        self._mutating: frozenset[str] = frozenset()
        self._slow_cache: tuple[float, float] = (float(DEFAULT_SLOW_RPC_MS), 0.0)
        self._lag_cache: tuple[float, float] = (float(DEFAULT_LOOP_LAG_WARN_MS), 0.0)
        self._watchdog: LoopWatchdog | None = None  # thread dump stack khi loop đứng (serve() khởi động)
        from .perf import DaemonPerf

        self.perf = DaemonPerf()  # thời gian mỗi RPC + ảnh chụp web (`perf.stats`/`perf.report`, perf.py)
        self.server: asyncio.base_events.Server | None = None
        # kết nối hỏi–đáp đang mở (client có thể giữ lại dùng tiếp — tui/client.py AsyncHubClient): đóng hết khi daemon dừng để client
        # thấy EOF, không gửi tiếp vào daemon đã dừng. Kết nối `events.subscribe` không nằm đây (tự kết thúc theo luồng của nó).
        self._rr_conns: set[asyncio.StreamWriter] = set()
        self.stopping = asyncio.Event()
        self.exit_code = 0  # ≠ 0 khi dừng để launchd chạy lại (cập nhật dưới LaunchAgent/LaunchDaemon — `SUPERVISED_RESTART_CODE`)
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
            "events.query": self._events_query,
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
        # Tài khoản Claude: account.list|login_start|login_finish|login_cancel|add_token|use|rename|rm|check (accounts/rpc.py — tự bọc audit method ghi)
        from .accounts.rpc import register_rpc as _register_accounts

        _register_accounts(self)
        # Cập nhật cfharness lên bản phát hành mới nhất: update.check|status|start (updater.py — tự bọc audit update.start)
        from .updater import register_rpc as _register_update

        _register_update(self)
        # Tổng quan máy (Q6): admin.overview — đọc thư mục trạng thái chung, CHỈ ĐỌC, HUMAN_ONLY (admin_overview.py)
        from .admin_overview import register_rpc as _register_admin

        _register_admin(self)
        # Super Admin Console: inbox.list|accept|reject|submit (HUMAN_ONLY) — team_inbox.py
        from .team_inbox import register_rpc as _register_inbox

        _register_inbox(self)
        # Vào/ra web + thiết bị tin cậy: web.notify (HUMAN_ONLY, tự audit + phát event → Telegram/Slack) — web_events.py
        from .web_events import register_rpc as _register_web_events

        _register_web_events(self)
        # Sao lưu: backup.list|create|verify|restore_plan (backup.py — tự bọc audit backup.create; khôi phục chỉ ngoại tuyến từ CLI)
        from .backup import register_rpc as _register_backup

        _register_backup(self)
        # Log hệ thống: logs.levels|level (logs_rpc.py, L1 — HUMAN_ONLY, tự bọc audit logs.level)
        from .logs_rpc import register_rpc as _register_logs

        _register_logs(self)
        # Gói chẩn đoán: support.plan|create|list (support_bundle.py, L7 — HUMAN_ONLY, tự bọc audit support.create)
        from .support_bundle import register_rpc as _register_support

        _register_support(self)
        # Xuất toàn bộ hub sang máy khác: hub.export (hub_transfer.py — làm lặng hub, ghi gói .cfhx, tự bọc audit; nhập chỉ ngoại tuyến từ CLI)
        from .hub_transfer import register_rpc as _register_hub_transfer

        _register_hub_transfer(self)
        # Chuyển nhà: hub.frozen (đọc) + hub.freeze|unfreeze (hub_freeze.py, HUMAN_ONLY, tự audit); hub.doctor (hub_doctor.py, chỉ đọc, HUMAN_ONLY)
        from .hub_doctor import register_rpc as _register_hub_doctor
        from .hub_freeze import register_rpc as _register_hub_freeze

        _register_hub_freeze(self)
        _register_hub_doctor(self)
        # Xuất/nhập hub từ WEB (PR6): hub.transfer_status|export_start|export_status|import_plan|import_start|import_status
        # (hub_transfer_web.py — HUMAN_ONLY; export_start/import_start tự bọc audit, gỡ passphrase; nhập = tiến trình `hub import-job` tách rời)
        from .hub_transfer_web import register_rpc as _register_hub_transfer_web

        _register_hub_transfer_web(self)
        # Xem/mở file agent đã thao tác (web chip file): file.read (chỉ đọc) + file.open (tác dụng phụ) — HUMAN_ONLY, tự audit (fileview.py)
        from .fileview import register_rpc as _register_files
        from .filehub.rpc import register_rpc as _register_filehub

        _register_filehub(self)  # thư mục chung Không gian số: filehub.status|list|link (đọc) + setup|login|add|remove|pause|resume (HUMAN_ONLY, tự audit)

        _register_files(self)
        # Đo hiệu năng: perf.stats (đọc tổng hợp) + perf.report (web đẩy ảnh chụp) — HUMAN_ONLY, chỉ RAM (perf.py)
        from .perf import register_rpc as _register_perf

        _register_perf(self)
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
        self._wrap_offload_reads()  # SAU mọi register_rpc: bọc ngoài cùng handler đã đăng ký

    # ------------------------------------------------------------------ đọc ngoài event loop
    def _wrap_offload_reads(self) -> None:
        """Bọc từng method ∈ `OFFLOAD_READS` để chạy trên luồng đọc của Store. Bỏ qua (log lỗi) method ghi hoặc handler async —
        lỗi lập trình, không được lặng lẽ chuyển sang luồng khác."""
        for method in sorted(OFFLOAD_READS):
            fn = self.methods.get(method)
            if fn is None:
                continue
            if method in self._mutating or inspect.iscoroutinefunction(fn):
                log.error("không chạy %s ngoài event loop: method ghi hoặc handler async", method)
                continue
            self.methods[method] = self._offloaded(method, fn)

    def _offloaded(self, method: str, fn: Handler) -> Handler:
        store = self.hub.store

        def _call(p: dict[str, Any]) -> Any:
            res = fn(p)
            if asyncio.iscoroutine(res):  # handler trả coroutine → không chạy được trên luồng đọc (không có loop)
                res.close()
                raise TypeError(f"{method}: handler trả coroutine, không chạy được ngoài event loop")
            return res

        async def offloaded(p: dict[str, Any]) -> Any:
            return await store.read(_call, p)

        offloaded.__wrapped__ = fn  # type: ignore[attr-defined]
        offloaded.offloaded = True  # type: ignore[attr-defined]
        return offloaded

    def _loop_stall_s(self) -> float:
        """`perf.loop_stall_dump_s` (mặc định 15; 0 = tắt watchdog dump stack). Gọi từ THREAD watchdog — chỉ đọc dict cấu hình."""
        try:
            return float(self.hub.config.get("perf.loop_stall_dump_s") or 0)
        except (KeyError, TypeError, ValueError):
            return DEFAULT_STALL_S

    def _loop_lag_warn_ms(self) -> float:
        """`perf.loop_lag_warn_ms` (mặc định 250; 0 = không cảnh báo) — đọc lại tối đa mỗi 5 s."""
        now = time.monotonic()
        if now >= self._lag_cache[1]:
            try:
                v = float(self.hub.config.get("perf.loop_lag_warn_ms") or 0)
            except (KeyError, TypeError, ValueError):
                v = float(DEFAULT_LOOP_LAG_WARN_MS)
            self._lag_cache = (v, now + 5.0)
        return self._lag_cache[0]

    async def _loop_lag(self, interval: float = LOOP_LAG_INTERVAL_S) -> None:
        """Đo độ trễ event loop: ngủ `interval` rồi so giờ thức thật với giờ hẹn. Trễ = loop bị chặn (code đồng bộ chậm trên loop).
        Nạp `loop.lag` vào `DaemonPerf` (`/perf`, `cfharness perf`); vượt `perf.loop_lag_warn_ms` → WARNING `loop.lag` trong hub.log.
        Một lần thức mỗi giây — gần như không tốn."""
        loop = asyncio.get_running_loop()
        while not self.stopping.is_set():
            t0 = loop.time()
            if self._watchdog is not None:
                self._watchdog.beat()
            await asyncio.sleep(interval)
            lag_ms = max(0.0, (loop.time() - t0 - interval) * 1000.0)
            self.perf.note_loop_lag(lag_ms)
            warn = self._loop_lag_warn_ms()
            if warn and lag_ms >= warn:
                log.warning("loop.lag event loop bị chặn %.0f ms", lag_ms, extra={"event": "loop.lag", "duration_ms": round(lag_ms, 1)})

    # ------------------------------------------------------------------ handlers
    def _agent_list(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.hub.agents.list(include_archived=bool(p.get("archived")))
        costs = self.hub.store.cost_today_by_agent()  # một truy vấn GROUP BY thay vì N lần cost_today
        for r in rows:
            live = self.hub.aggregator.agents.get(r["id"])
            r["last_tool"] = live.last_tool if live else None
            r["cost_today_usd"] = round(costs.get(r["id"], 0.0), 8)
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

    def _events_query(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """`events.query {run_id?, agent_id?, types?, order?, limit?, since?|since_seq?, epoch?, workstream?}`.
        `order="desc"` → N event GẦN NHẤT (mới → cũ; client tự đảo nếu cần cũ → mới). `since`/`since_seq` = chỉ event có seq lớn hơn
        (đi cùng `epoch` — seq đếm lại sau mỗi lần daemon khởi động). `types` = chỉ các loại event này (lọc ở SQL)."""
        since = p.get("since_seq", p.get("since"))
        return [
            e.to_wire() for e in self.hub.store.query_events(
                run_id=p.get("run_id"), agent_id=p.get("agent_id"), limit=int(p.get("limit", 10_000)),
                types=[str(t) for t in p["types"]] if isinstance(p.get("types"), list) else None,
                newest_first=p.get("order") == "desc",
                since_seq=None if since is None else int(since),
                epoch=str(p["epoch"]) if p.get("epoch") else None,
                manager_workstream=None if p.get("workstream") is None else str(p.get("workstream")),
            )
        ]

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
    def _cred_of(self, token: Any) -> str | None:
        """Loại token khớp (human | web | mcp) — so hằng thời gian với từng token; không khớp → None."""
        if _auth.token_ok(token, self.auth_token):
            return "human"
        for kind, tok in self.client_tokens.items():
            if _auth.token_ok(token, tok):
                return kind
        return None

    def _caller_for(self, method: str, params: dict[str, Any], peer: int | None, via_socket: bool) -> _auth.Caller:
        token = params.pop(_auth.AUTH_PARAM, None) if isinstance(params, dict) else None
        if not via_socket:
            return _auth.Caller(via="internal")
        cred = self._cred_of(token)
        if (method not in _auth.HUMAN_ONLY and method not in self._mutating and method not in _auth.SCOPED_READ
                and method not in _SENSITIVE_READS):
            return _auth.Caller(via="socket", pid=peer, token_ok=cred is not None, cred=cred)  # đường nóng: guard.check, đọc
        return self.callers.resolve(peer, token, self.auth_token, cred=cred)

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

        return self.hub.learning.agent_propose(
            target=_r("target"), op=_r("op"), payload=params.get("payload") or {}, agent_id=caller.agent_id or params.get("agent"),
            scope=params.get("scope") or "agent", team_id=params.get("team"), target_ref=params.get("target_ref"),
            rationale=params.get("rationale") or "", run_id=caller.run_id,
        )

    async def call_as(self, caller: _auth.Caller, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Gọi method thay mặt một kênh của người KHÔNG qua socket (bot Telegram trong daemon — chat/bot.py).
        Vẫn đi qua `_dispatch`: cổng HUMAN_ONLY theo `caller`, CURRENT_CALLER, bọc audit. Trả phản hồi JSON-RPC."""
        req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": dict(params or {})}
        return await self._dispatch(req, as_caller=caller)  # type: ignore[return-value]

    async def _dispatch(self, req: Any, *, peer: int | None = None, via_socket: bool = False, as_caller: _auth.Caller | None = None) -> dict[str, Any] | None:
        """Một lượt JSON-RPC. L2: bind `req_id` (trace.py) cho cả lượt, trả `_meta.req_id` trong phản hồi, ghi access log `cfharness.rpc`."""
        if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
            return {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "Invalid Request"}}
        params = req.get("params") or {}
        raw_trace = params.pop(_trace.TRACE_PARAM, None) if isinstance(params, dict) else None
        raw_client = params.pop(CLIENT_PARAM, None) if isinstance(params, dict) else None  # L3: chỉ nhận theo loại token (`clean_client_meta`)
        if raw_trace is None and as_caller is not None and _trace.current_req_id():
            req_id = _trace.current_req_id()  # call_as trong daemon (chat, điều phối viên…): cùng lượt với điểm vào đang bind
        else:
            req_id = _trace.accept(raw_trace)  # chỉ nhận đúng định dạng `r_<ULID>`; sai/thiếu → mã mới (không bao giờ là quyền)
        outer = _trace.current() or {}
        parent = outer.get("req_id")
        tok = _trace.set_ctx(req_id, parent_req_id=parent if parent != req_id else None)
        t0 = time.perf_counter()
        info: dict[str, Any] = {"caller": as_caller, "client": raw_client, "trace_given": raw_trace is not None,
                                "outer_channel": outer.get("channel"), "req_id": req_id}
        try:
            resp = await self._dispatch_inner(req, params, info, peer=peer, via_socket=via_socket, as_caller=as_caller)
            req_id = info["req_id"]  # có thể đã thay (agent/điều phối viên không được tự chọn req_id — `_bind_identity`)
            if resp is not None and req["method"] in _SENSITIVE_READS and "result" in resp:
                self._record_read(req["method"], info, resp["result"])
        finally:
            _trace.reset(tok)
        if resp is not None:
            resp["_meta"] = {"req_id": req_id}
            self._access_log(req["method"], req_id, info, resp, (time.perf_counter() - t0) * 1000.0)
        return resp

    def _bind_identity(self, method: str, caller: _auth.Caller, params: Any, info: dict[str, Any], *, call_as: bool) -> None:
        """L3 (A2): danh tính do daemon chứng thực → ngữ cảnh lượt (`trace.REQ_CTX`): `principal`, kênh đã chứng thực (`audit_channel`;
        ghi đè `_audit.channel` của NGƯỜI qua socket theo loại token), kênh client khai (`declared_channel`), `session_ref`/`ip` (chỉ từ token
        web), `caller_run_id`. Agent/điều phối viên trong run: req_id client gửi bị BỎ — mã mới, cha = req_id của run (không chen được vào
        trace của lệnh người)."""
        ctx = _trace.current()
        if ctx is None:  # pragma: no cover — _dispatch luôn đặt
            return
        meta = params.get("_audit") if isinstance(params, dict) and isinstance(params.get("_audit"), dict) else None
        declared = meta.get("channel") if meta else None
        client = _auth.clean_client_meta(caller, info.get("client"))
        attested = None if call_as else _auth.attested_channel(caller, declared)
        if attested is not None and meta is not None:
            meta["channel"] = attested
        if caller.is_agent and (caller.via == "socket" or (call_as and info.get("trace_given"))):
            run_req = None
            if caller.via == "socket" and caller.run_id:
                try:
                    run = self.hub.store.get_run(caller.run_id)
                    run_req = run.get("req_id") if run else None
                except Exception:  # best-effort: không đọc được run → chỉ cha là mã đang có
                    log.debug("không đọc được req_id của run %s", caller.run_id, exc_info=True)
            new = _trace.new_req_id()
            ctx["parent_req_id"] = run_req if _trace.valid_req_id(run_req) else ctx.get("parent_req_id")
            ctx["req_id"] = info["req_id"] = new
        ctx["principal"] = info["principal"] = _trace._clean_channel(_auth.principal_of(caller, client, internal_channel=info.get("outer_channel")))
        ctx["declared_channel"] = _trace._clean_channel(declared) if declared is not None else None
        ctx["session_ref"] = client.get("session_ref")
        ctx["ip"] = client.get("ip")
        ctx["caller_run_id"] = caller.run_id if caller.is_agent else None
        ctx["audit_channel"] = _trace._clean_channel((meta or {}).get("channel") if meta else attested) or None
        info["attested"] = attested

    def _record_read(self, method: str, info: dict[str, Any], result: Any) -> None:
        """A4: một dòng `read:<method>` (target + số bản ghi, KHÔNG nội dung) cho mỗi (principal, method, target) trong `READ_COALESCE_S`.
        Lượt đầu ghi ngay (count=1); các lượt sau trong cửa sổ gộp trong RAM rồi ghi MỘT dòng `coalesced` khi cửa sổ đóng (audit là
        append-only — không sửa được dòng đầu). Khởi động lại mất phần gộp dở → lượt sau ghi dòng mới."""
        params = info.get("params_snapshot") or {}
        if method == "run.get" and not params.get("events"):
            return
        caller = info.get("caller")
        principal = str(info.get("principal") or "unknown")
        target = _read_target(params)
        n = _result_count(result)
        now = time.monotonic()
        self._flush_reads(now)
        key = (principal, method, target or "")
        slot = self._reads.get(key)
        if slot is not None:
            slot[1] += 1
            slot[2] = n if n is not None else slot[2]
            return
        if len(self._reads) >= 2000:  # trần RAM: ghi sớm cửa sổ cũ nhất
            self._flush_reads(now, force_oldest=True)
        self._reads[key] = [now, 0, n, _actor_of(caller), info.get("channel")]
        self._write_read(method, target, principal, count=1, result_count=n, actor=_actor_of(caller), channel=info.get("channel"))

    def _write_read(self, method: str, target: str | None, principal: str, *, count: int, result_count: int | None, actor: str,
                    channel: Any, coalesced: bool = False) -> None:
        data: dict[str, Any] = {"count": count, "window_s": int(_READ_COALESCE_S)}
        if result_count is not None:
            data["result_count"] = result_count
        if coalesced:
            data["coalesced"] = True
        try:
            self.hub.audit.record(f"read:{method}", None, None, data, actor=actor, channel=str(channel or "cli"), target=target,
                                  summary=f"read:{method}", outcome="ok", principal=principal)
        except Exception:  # best-effort: audit đọc lỗi không được làm hỏng lượt đọc
            log.exception("không ghi được audit read:%s", method)

    def _flush_reads(self, now: float | None = None, *, force_oldest: bool = False, all_: bool = False) -> None:
        now = time.monotonic() if now is None else now
        oldest = min(self._reads, key=lambda k: self._reads[k][0]) if force_oldest and self._reads else None
        for key in list(self._reads):
            start, extra, n, actor, channel = self._reads[key]
            if all_ or key == oldest or now - start >= _READ_COALESCE_S:
                del self._reads[key]
                if extra:
                    self._write_read(key[1], key[2] or None, key[0], count=extra, result_count=n, actor=actor, channel=channel, coalesced=True)

    def _slow_rpc_ms(self) -> float:
        """`logs.slow_rpc_ms` (mặc định 2000; 0 = tắt) — đọc lại tối đa mỗi 5 s (không đọc config ở mỗi lời gọi)."""
        now = time.monotonic()
        if now >= self._slow_cache[1]:
            try:
                v = float(self.hub.config.get("logs.slow_rpc_ms") or 0)
            except (KeyError, TypeError, ValueError):
                v = float(DEFAULT_SLOW_RPC_MS)
            self._slow_cache = (v, now + 5.0)
        return self._slow_cache[0]

    def _access_log(self, method: str, req_id: str, info: dict[str, Any], resp: dict[str, Any], dur_ms: float) -> None:
        """B3: một bản ghi METADATA mỗi lượt (không params/kết quả). Đọc thành công = DEBUG; ghi / lỗi nghiệp vụ = INFO; bị từ chối
        (BLOCKED) = WARNING; chậm hơn `logs.slow_rpc_ms` = WARNING `rpc.slow` (trừ method chờ lâu theo thiết kế)."""
        err = resp.get("error")
        code = err.get("code") if isinstance(err, dict) else None
        outcome = "ok" if err is None else ("denied" if code == BLOCKED else "error")
        if outcome == "ok":
            level = logging.INFO if method in self._mutating else logging.DEBUG
        elif outcome == "denied":
            level = logging.WARNING
        else:
            level = logging.INFO
        if method not in SLOW_EXEMPT:
            self.perf.note(method, dur_ms)  # tổng hợp cuộn trong RAM (perf.py) — method chờ lâu theo thiết kế không tính
        slow_ms = self._slow_rpc_ms()
        slow = bool(slow_ms) and dur_ms >= slow_ms and method not in SLOW_EXEMPT
        if slow:
            level = max(level, logging.WARNING)
        if not rpc_log.isEnabledFor(level):
            return
        caller = info.get("caller")
        rpc_log.log(level, "%s %s %s %.1fms", "rpc.slow" if slow else "rpc", method, outcome, dur_ms,
                    extra={"event": "rpc.slow" if slow else "rpc", "method": method[:80], "req_id": req_id, "via": _via(caller),
                           "channel": info.get("channel"), "duration_ms": round(dur_ms, 1), "outcome": outcome, "code": code})

    async def _dispatch_inner(self, req: dict[str, Any], params: Any, info: dict[str, Any], *, peer: int | None, via_socket: bool,
                              as_caller: _auth.Caller | None) -> dict[str, Any] | None:
        rid = req.get("id")
        method = req["method"]
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
            info["caller"] = caller
            if method in _SENSITIVE_READS and isinstance(params, dict):
                info["params_snapshot"] = {k: v for k, v in params.items() if not k.startswith("_")}
            declared = None
            if isinstance(params, dict) and isinstance(params.get("_audit"), dict):
                declared = params["_audit"].get("channel")
            if not caller.human and caller.via == "socket" and isinstance(params, dict) and (method in _auth.HUMAN_ONLY or "_audit" in params or caller.is_agent):
                # mọi lời gọi không phải người: audit ghi đúng là agent (không tin `_audit.actor` do client tự khai)
                params["_audit"] = {"channel": "agent" if caller.is_agent else "socket", "actor": "agent", "run_id": caller.run_id}
            self._bind_identity(method, caller, params, info, call_as=as_caller is not None)
            if declared is not None and _trace.current() is not None:
                _trace.current()["declared_channel"] = _trace._clean_channel(declared)  # type: ignore[index]
            _set_channel(info, caller, params)
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
        self._rr_conns.add(writer)
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
                    self._rr_conns.discard(writer)
                    raw_trace = None
                    if isinstance(req.get("params"), dict):
                        req["params"].pop(_auth.AUTH_PARAM, None)
                        raw_trace = req["params"].pop(_trace.TRACE_PARAM, None)
                    with _trace.bind(_trace.accept(raw_trace), channel="subscribe"):
                        await self._subscribe(req, reader, writer)
                    break
                resp = await self._dispatch(req, peer=peer, via_socket=True)
                if resp is not None and "id" in req:
                    writer.write(encode(resp))
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._rr_conns.discard(writer)
            try:
                writer.close()
            except Exception:  # best-effort: đóng kết nối đã hỏng  # pragma: no cover
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
            detail_agent_id=None if agent_id or run_id or not p.get("detail_agent_id") else str(p["detail_agent_id"]),
        )
        until_end = bool(p.get("until_run_end")) and run_id is not None
        t0 = time.perf_counter()
        if rpc_log.isEnabledFor(logging.DEBUG):  # B3: luồng dài — chỉ ghi lúc mở + lúc đóng
            rpc_log.debug("rpc events.subscribe open", extra={"event": "rpc.stream_open", "method": "events.subscribe", "via": "socket",
                                                            "outcome": "open"})
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
                _stream_closed(t0, "run_finished")
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
            reason = "connection_lost"
        finally:
            eof_task.cancel()
            sub.close()
            _stream_closed(t0, reason)

    async def serve(self) -> None:
        sock = self.paths.socket
        if sock.exists():
            if _live_daemon(sock):
                raise SystemExit(f"daemon đã chạy (socket {sock})")
            sock.unlink(missing_ok=True)
        self.hub.start()
        self.server = await transport.start_server(self._handle_conn, sock, limit=RPC_LIMIT)
        self.paths.pidfile.write_text(str(os.getpid()), encoding="utf-8")
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(asyncio_exception_handler)  # L1: task chết không ai đợi → log ERROR thay vì chỉ in stderr
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stopping.set)
            except (NotImplementedError, RuntimeError):  # pragma: no cover
                pass
        log.info("daemon sẵn sàng tại %s (epoch %s)", sock, self.hub.bus.epoch)
        watch = asyncio.create_task(self._watch_listener())
        self._watchdog = LoopWatchdog(_fault_file, stall_s=self._loop_stall_s)
        self._watchdog.start()  # thread riêng: loop đứng quá perf.loop_stall_dump_s → dump stack vào faults.log
        lag = asyncio.create_task(self._loop_lag())
        try:
            await self.stopping.wait()
        finally:
            log.info("daemon đang dừng")
            watch.cancel()
            lag.cancel()
            self._watchdog.stop()
            self.server.close()
            for w in list(self._rr_conns):  # client đang giữ kết nối (AsyncHubClient) thấy EOF ngay
                try:
                    w.close()
                except Exception:  # best-effort  # pragma: no cover
                    pass
            self._rr_conns.clear()
            self._flush_reads(all_=True)  # A4: ghi nốt phần gộp lượt đọc nhạy cảm
            await self.hub.stop()
            JOBS.close_all()
            sock.unlink(missing_ok=True)
            self.paths.pidfile.unlink(missing_ok=True)


    async def _watch_listener(self, interval: float = LISTENER_CHECK_S) -> None:
        """Mở lại server khi socket nghe bị asyncio đóng sau lỗi accept (`transport.server_listening`).
        Không có cái này, daemon sống mà điếc: CLI/web/chat đều báo "daemon không chạy" cho tới khi giết tiến trình."""
        while not self.stopping.is_set():
            try:
                await asyncio.wait_for(self.stopping.wait(), interval)
                return
            except asyncio.TimeoutError:
                pass
            if self._reads:
                self._flush_reads()  # A4: cửa sổ gộp lượt đọc đã đóng → ghi dòng gộp
            if self.server is None or transport.server_listening(self.server):
                continue
            log.error("socket nghe của daemon đã bị đóng (accept lỗi) — mở lại server")
            try:
                self.server.close()
                self.server = await transport.start_server(self._handle_conn, self.paths.socket, limit=RPC_LIMIT)
                log.info("daemon nghe lại tại %s", self.paths.socket)
            except Exception:
                log.exception("mở lại server của daemon lỗi — thử lại sau %.0f s", interval)


def _stream_closed(t0: float, reason: str) -> None:
    dur_ms = (time.perf_counter() - t0) * 1000.0
    level = logging.WARNING if reason == "slow_consumer" else logging.DEBUG
    if rpc_log.isEnabledFor(level):
        rpc_log.log(level, "rpc events.subscribe close %s %.0fms", reason, dur_ms,
                    extra={"event": "rpc.stream_close", "method": "events.subscribe", "via": "socket", "duration_ms": round(dur_ms, 1),
                           "outcome": "closed", "reason": str(reason)[:40]})


def _read_target(p: dict[str, Any]) -> str | None:
    """Đích của lượt đọc: id/run/agent/tên (≤ 80 ký tự). Chuỗi tìm kiếm có thể là nội dung → chỉ ghi độ dài (`q:<n>ch`)."""
    for k in ("id", "run_id", "agent_id", "agent", "name", "kind"):
        v = p.get(k)
        if v is not None and not isinstance(v, (dict, list)):
            return str(v)[:80]
    for k in ("query", "q", "text"):
        v = p.get(k)
        if isinstance(v, str) and v:
            return f"q:{len(v)}ch"
    return None


def _result_count(res: Any) -> int | None:
    """Số bản ghi trả về khi hiển nhiên (độ dài list, hoặc list trong khoá quen thuộc) — không bao giờ nội dung."""
    if isinstance(res, list):
        return len(res)
    if isinstance(res, dict):
        for k in ("events", "results", "hits", "items", "rows", "facts", "entities", "secrets", "names"):
            if isinstance(res.get(k), list):
                return len(res[k])
    return None


def _actor_of(caller: "_auth.Caller | None") -> str:
    if caller is None:
        return "unknown"
    if caller.manager:
        return f"manager:{caller.manager}"
    if caller.is_agent:
        return "agent"
    return "user" if caller.human else "unknown"


def _via(caller: "_auth.Caller | None") -> str:
    """Loại người gọi cho access log: manager / agent / internal / socket / telegram:<chat> / slack:<chat> (+ ":human" khi là người)."""
    if caller is None:
        return "unknown"
    if caller.manager:
        return "manager"
    if caller.is_agent:
        return "agent"
    return f"{caller.via}:human" if caller.via == "socket" and caller.human else caller.via


def _set_channel(info: dict[str, Any], caller: "_auth.Caller", params: Any) -> None:
    """Kênh của lượt (log/event): `_audit.channel` (daemon đã ép cho agent/manager, chứng thực cho người — L3) → kênh chứng thực theo
    token → không có thì loại người gọi."""
    ch = (params.get("_audit") or {}).get("channel") if isinstance(params, dict) and isinstance(params.get("_audit"), dict) else None
    ch = ch if isinstance(ch, str) else (info.get("attested") or _via(caller))
    ctx = _trace.current()
    if ctx is not None:
        ctx["channel"] = _trace._clean_channel(ch)
        info["channel"] = ctx["channel"]


def _live_daemon(sock: Path) -> bool:
    """Socket có daemon đang trả lời không (`hub.ping`)."""
    if not sock.exists():
        return False
    try:
        RpcClient(sock, timeout=2).call("hub.ping")
        return True
    except Exception:  # best-effort: không ping được = daemon không chạy
        return False


class _ParamError(Exception):
    pass


def _req(p: dict[str, Any], key: str) -> Any:
    if key not in p or p[key] is None:
        raise _ParamError(f"thiếu tham số '{key}'")
    return p[key]


def _log_settings(paths: HubPaths) -> dict[str, Any]:
    """`logs.*` (NFR-21); config hỏng → mặc định (log không được làm daemon không khởi động)."""
    try:
        c = HubConfig(paths.config).get("logs") or {}
    except Exception:  # best-effort: config hỏng → mặc định (log không được làm daemon không khởi động)  # pragma: no cover
        c = {}
    return {"max_bytes": c.get("max_bytes", DEFAULT_MAX_BYTES), "backup_count": c.get("backup_count", DEFAULT_BACKUP_COUNT),
            "max_age_days": c.get("max_age_days", DEFAULT_MAX_AGE_DAYS), "level": LEVELS.get(str(c.get("level") or "info").lower(), logging.INFO)}


def _rotate_daemon_out(paths: HubPaths, ls: dict[str, Any]) -> None:
    """daemon.out là stdout/stderr thô của daemon (không xoay được khi đang chạy) → xoay lúc khởi động nếu quá max_bytes."""
    rotate_if_large(paths.logs_dir / "daemon.out", int(ls["max_bytes"] or 0), int(ls["backup_count"] or 0))


def run_foreground(paths: HubPaths) -> None:
    from .oscompat import set_private_umask

    set_private_umask()  # đa instance P1: file daemon tạo (hub.db, logs/, runs/, prompts/…) chỉ user này đọc được; claude con nhận lại umask gốc
    paths.ensure()
    _hold_while_updating(paths)
    from .hub_transfer_web import hold_while_importing

    hold_while_importing(paths)  # nhập hub từ web: job `hub import-job` đang ghi đè hub.db → chờ nó xong (nó tự khởi động lại daemon)
    global _INSTANCE_LOCK
    _INSTANCE_LOCK = acquire_instance_lock(paths, wait_s=3.0)
    if _INSTANCE_LOCK is None and supervisor() == "launchd":
        # Job launchd thua khoá (bản ngoài launchd đang giữ): CHỜ khoá thay vì thoát 1 — thoát 1 làm launchd chạy lại mỗi
        # ThrottleInterval vô hạn (sự cố runs=6511). Bản ngoài chết → job này tiếp quản ngay, hub không mất giám sát.
        print(f"daemon (launchd) chờ khoá {instance_lock_path(paths)} — bản khác đang chạy cho {paths.home}", file=sys.stderr)
        while _INSTANCE_LOCK is None:
            _INSTANCE_LOCK = acquire_instance_lock(paths, wait_s=LOCK_RETRY_S)
    if _INSTANCE_LOCK is None:
        # Bản khác đang chạy/đang khởi động trên CÙNG HUB_HOME (vd lệnh CLI tự khởi động hai lần cùng lúc):
        # thoát mã ≠ 0, không bao giờ chạy 2 daemon ghi một hub.db.
        print(f"daemon đã chạy cho {paths.home} (khoá {instance_lock_path(paths)}) — thoát", file=sys.stderr)
        raise SystemExit(1)
    tightened = paths.tighten_permissions()  # cài đặt cũ: siết HUB_HOME + mục cấp 1 (lỗi chỉ cảnh báo)
    ls = _log_settings(paths)
    if sys.stdout is None or sys.stderr is None:  # pythonw (dịch vụ tự khởi động trên Windows): không có console → ghi daemon.out
        _rotate_daemon_out(paths, ls)
        out = open(paths.logs_dir / "daemon.out", "a", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = sys.stdout or out
        sys.stderr = sys.stderr or out
    console = ensure_hidden_console()  # Windows/pythonw: console ẩn cho claude + hậu duệ (không bật cửa sổ, CTRL_BREAK chạy được)
    setup_logging(paths.logs_dir, ls["level"], max_bytes=ls["max_bytes"], backup_count=ls["backup_count"], max_age_days=ls["max_age_days"])
    install_crash_handlers(paths.logs_dir)  # L1: excepthook + faulthandler (logs/faults.log, SIGUSR1 = dump stack)
    if console not in ("posix", "existing"):
        logging.getLogger("cfharness.daemon").info("không có console → đã lấy console ẩn (%s)", console)
    if tightened:
        log.info("đã siết quyền (bỏ group/other) cho %d mục trong HUB_HOME: %s", len(tightened), ", ".join(tightened))
    from .redact import install_log_filter, install_stderr_masking

    install_stderr_masking()  # H-12: log thư viện ra stderr (daemon.out) cũng che
    install_log_filter()  # H-12: log của daemon che theo giá trị secret
    install_log_filter(logging.getLogger("cfharness"))
    d = Daemon(paths)
    asyncio.run(d.serve())
    if d.exit_code:
        raise SystemExit(d.exit_code)


# ---- chạy dưới launchd (LaunchAgent `daemon install-service` / LaunchDaemon `admin install-daemon`, cùng KeepAlive SuccessfulExit=false)
SUPERVISED_RESTART_CODE = 75  # EX_TEMPFAIL: thoát ≠ 0 → launchd chạy lại daemon (mã 0 = dừng hẳn)
SUPERVISOR_LABEL_PREFIX = "com.cfharness.daemon."
_UPDATE_BUSY_PHASES = ("starting", "waiting_daemon", "installing", "rolling_back")  # rolling_back: helper thay hub.db + cài lại bản cũ
_IS_MACOS = sys.platform == "darwin"
LOCK_RETRY_S = 30.0  # job launchd thua khoá: thử lấy lại mỗi chừng này giây (bằng ThrottleInterval của plist)


def supervisor() -> str | None:
    """"launchd" khi tiến trình này là job daemon do cfharness cài (launchd đặt `XPC_SERVICE_NAME` = Label của job), ngược lại None.
    Chỉ nhận nhãn của chính cfharness: shell Terminal cũng có `XPC_SERVICE_NAME` (giá trị khác)."""
    if _IS_MACOS and os.environ.get("XPC_SERVICE_NAME", "").startswith(SUPERVISOR_LABEL_PREFIX):
        return "launchd"
    return None


def _update_busy(paths: HubPaths) -> bool:
    try:
        st = json.loads((paths.home / "update" / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if st.get("state") not in ("starting", "running") or st.get("phase", "starting") not in _UPDATE_BUSY_PHASES:
        return False
    return time.time() - float(st.get("updated_at") or st.get("started_at") or 0) < 1200  # helper treo quá 20 phút → bỏ qua


def _hold_while_updating(paths: HubPaths, *, poll_s: float = 1.0, max_s: float = 1200.0) -> None:
    """launchd chạy lại daemon NGAY khi bản cũ thoát để cập nhật — lúc đó `uv` còn đang cài đè venv. Bản này chờ cài xong rồi thoát
    `SUPERVISED_RESTART_CODE` để launchd chạy lại bằng mã MỚI (module đã nạp trong tiến trình này là bản cũ)."""
    if not _update_busy(paths):
        return
    print("daemon: đang cập nhật cfharness — chờ cài xong rồi để launchd chạy lại bằng bản mới", file=sys.stderr)
    deadline = time.monotonic() + max_s
    while _update_busy(paths) and time.monotonic() < deadline:
        time.sleep(poll_s)
    raise SystemExit(SUPERVISED_RESTART_CODE)


def wait_supervised(paths: HubPaths, wait_s: float) -> int | None:
    """Chờ launchd tự chạy lại daemon (không tự bật bản thứ hai). Trả pid khi daemon trả lời, None khi hết giờ.
    Daemon đang khởi động có thể đã nhận kết nối mà chưa trả lời kịp (socket timeout) hoặc đóng kết nối giữa chừng — coi như chưa
    lên và thử lại, không ném lỗi (sự cố v0.2.27: helper cập nhật báo "khởi động lại daemon lỗi (mã 1)" dù daemon lên ngay sau đó)."""
    client = RpcClient(paths.socket, timeout=2)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            return int(client.call("hub.ping").get("pid") or 0)
        except (DaemonUnavailable, OSError, RpcError, ValueError):  # OSError gồm TimeoutError; ValueError: dòng JSON cụt
            time.sleep(0.5)
    return None


def _kickstart_autostart(paths: HubPaths) -> bool:
    """macOS + LaunchAgent tự khởi động của HUB_HOME này đã cài: `launchctl kickstart -k gui/<uid>/<label>` để launchd chạy
    daemon. True khi launchd nhận lệnh; False (chưa cài / job chưa nạp / lỗi) → bên gọi tự bật như cũ.
    Bản tự bật ngoài launchd giữ `daemon.lock` → job launchd thua khoá (sự cố sau `cfharness update`)."""
    if not _IS_MACOS:
        return False
    from .systems.autostart import Autostart

    au = Autostart(paths)
    if not au.definition_path().exists():  # rẻ: không gọi launchctl khi chưa cài (HUB_HOME tạm của test)
        return False
    rc, _, err = au._run(["/bin/launchctl", "kickstart", "-k", f"{au._gui()}/{au.name}"])
    if rc != 0:
        print(f"launchctl kickstart {au.name} lỗi ({rc}): {err.strip()[:200]} — tự khởi động daemon", file=sys.stderr)
        return False
    return True


def spawn_background(paths: HubPaths, wait_s: float = 15.0) -> int:
    """Khởi chạy daemon nền (tách session), chờ socket sẵn sàng. Trả pid."""
    paths.ensure()
    _rotate_daemon_out(paths, _log_settings(paths))
    out = open(paths.logs_dir / "daemon.out", "ab")
    env = dict(os.environ)
    env["CFHARNESS_HOME"] = str(paths.home)
    if instance_lock_held(paths):  # bản khác đang khởi động (chưa mở socket) → chờ nó, không bật thêm bản thứ hai
        return _wait_ready(paths, wait_s, None)
    if _kickstart_autostart(paths):  # đã cài LaunchAgent: để launchd chạy daemon (đúng --home), không bật bản ngoài launchd
        return _wait_ready(paths, wait_s, None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "cfharness", "daemon", "run"],
        stdin=subprocess.DEVNULL,
        stdout=out,
        stderr=out,
        **detached_kwargs(),
        env=env,
        close_fds=True,
    )
    return _wait_ready(paths, wait_s, proc)


def _wait_ready(paths: HubPaths, wait_s: float, proc: subprocess.Popen[bytes] | None) -> int:
    """Chờ socket trả lời `hub.ping`; trả pid của daemon ĐANG phục vụ (có thể là bản khác thắng khoá, không phải `proc`)."""
    client = RpcClient(paths.socket, timeout=2)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            if not instance_lock_held(paths):
                raise RuntimeError(f"daemon thoát sớm (mã {proc.returncode}); xem {paths.logs_dir / 'daemon.out'}")
            proc = None  # thua khoá: bản khác đang khởi động → chờ bản đó
        try:
            st = client.call("hub.ping")
            return int(st.get("pid") or (proc.pid if proc else 0))
        except DaemonUnavailable:
            time.sleep(0.1)
    raise RuntimeError(f"daemon không sẵn sàng sau {wait_s}s; xem {paths.logs_dir / 'daemon.out'}")


# ---- một daemon cho mỗi HUB_HOME (đa instance): khoá file độc quyền giữ suốt đời tiến trình daemon
_INSTANCE_LOCK: Any = None


def instance_lock_path(paths: HubPaths) -> Path:
    return paths.home / "daemon.lock"


def acquire_instance_lock(paths: HubPaths, wait_s: float = 0.0) -> Any:
    """Lấy khoá độc quyền `HUB_HOME/daemon.lock` (flock / msvcrt). Trả handle (giữ mở = giữ khoá; hệ điều hành tự nhả khi
    tiến trình chết, kể cả SIGKILL) hoặc None nếu bản khác đang giữ sau `wait_s` giây. Chờ ngắn để một lần dò
    (`instance_lock_held`) trùng lúc không làm daemon thật bỏ cuộc."""
    path = instance_lock_path(paths)
    deadline = time.monotonic() + wait_s
    while True:
        f = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+b")
        try:
            if IS_WINDOWS:  # pragma: no cover — chỉ chạy trên Windows
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            f.close()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)


def release_instance_lock(handle: Any) -> None:
    if handle is None:
        return
    try:
        if IS_WINDOWS:  # pragma: no cover
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:  # pragma: no cover
        pass
    handle.close()  # flock nhả khi đóng fd


def instance_lock_held(paths: HubPaths) -> bool:
    """Có daemon (đang chạy hoặc đang khởi động) giữ khoá của HUB_HOME này không."""
    if not instance_lock_path(paths).exists():
        return False
    h = acquire_instance_lock(paths)
    if h is None:
        return True
    release_instance_lock(h)
    return False
