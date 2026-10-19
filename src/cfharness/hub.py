"""Lõi hub: nối Store + EventBus + Aggregator + AgentService + RunManager.

Dùng chung cho daemon (qua JSON-RPC) và cho test in-process. Mọi ghi DB đi qua đây (một writer).
"""

from __future__ import annotations

import logging
from typing import Any

from . import __version__
from .agents import AgentService
from .bus import Aggregator, EventBus
from .config import HubConfig
from .events import HubEvent, make_event
from .paths import HubPaths
from .runs import RunManager
from .runtime import AgentRuntime
from .store import Store

log = logging.getLogger("cfharness.hub")


class Hub:
    def __init__(self, paths: HubPaths, *, runtime: AgentRuntime | None = None) -> None:
        self.paths = paths
        paths.ensure()
        self.config = HubConfig(paths.config)
        self.store = Store(paths.db)
        bc = self.config.get("bus")
        self.bus = EventBus(ring_events=int(bc["ring_events"]), ring_bytes=int(bc["ring_bytes"]), subscriber_queue=int(bc["subscriber_queue"]))
        self.aggregator = Aggregator()
        self.bus.add_listener(self._persist)
        self.bus.add_listener(self.aggregator.apply)
        self.runs = RunManager(self, runtime)
        self.agents = AgentService(self.store, paths, self.config, self.publish, active_runs=self.runs.active_runs)
        self.agents.hub = self  # MGR-1: validate_workspace cần config/guard/paths của hub
        # M1 luồng A: self.secrets, self.audit, self.mcp + watcher tool.write (mcp_client/wiring.py)
        from .mcp_client.wiring import attach as _attach_integration

        _attach_integration(self)
        # M1 luồng B: approval (FR-SG-04), guard (FR-SG-03), Slack (FR-SG-09)
        from .approvals import ApprovalService
        from .guard.service import GuardService
        from .notify import SlackNotifier

        self.approvals = ApprovalService(self)
        self.guard = GuardService(self)
        self.notifier = SlackNotifier(self)
        self.bus.add_listener(self.notifier)
        # Telegram: `hub.telegram` (bot long polling + kênh thông báo cùng luồng sự kiện) — chat/bot.py, notify/telegram.py
        from .chat import attach as _attach_telegram

        _attach_telegram(self)
        # M3: team (FR-TM), task + DAG (FR-TK), dispatcher (claim/lease/retry/tách việc/review)
        from .dispatch import TaskDispatcher
        from .tasks import TaskService
        from .teams import TeamService

        self.teams = TeamService(self)
        self.tasks = TaskService(self)
        self.dispatcher = TaskDispatcher(self)
        # M4-A: kho tri thức tầng agent (`hub.knowledge`) + lõi học (`hub.learning`: Signal, review fork, proposal,
        # pipeline tự áp dụng, guard học, ledger/rollback) — learning/service.py
        from .learning import attach as _attach_m4a

        _attach_m4a(self)
        # M4-B: golden task + eval gate (`hub.evals`), hồi quy/curator/digest + lịch nền (`hub.learning_ops`) — learning_ops/wiring.py
        from .learning_ops.wiring import attach as _attach_m4b

        _attach_m4b(self)
        # M5-B: curator liên agent + pha 2 LLM (`hub.curation`), OTel tuỳ chọn (`hub.telemetry`) — curation/, telemetry/
        from .curation import attach as _attach_m5b

        _attach_m5b(self)
        # hardening CLI (H-10/H-11): kiểm phiên bản CLI lúc khởi động, cảnh báo raw.unknown, nạp hook.timeout đã spool
        from .runtime.compat import attach as _attach_compat

        _attach_compat(self)
        # M7 MGR-CORE: điều phối viên (`hub.manager`: chỉ định, hội thoại, heartbeat, kiểm quyền) — manager/service.py
        from .manager import attach as _attach_manager

        _attach_manager(self)
        # UX: câu hỏi có lựa chọn của agent (`hub.questions`: task → điều phối viên → người) — questions/service.py
        from .questions import attach as _attach_questions

        _attach_questions(self)
        self._bg: list[Any] = []
        # hardening SEC (H-02 lớp 3): toàn vẹn policy/ + config.yaml + hub.db (`hub.integrity`) — guard/integrity.py. GIỮ CUỐI __init__
        from .guard.integrity import attach as _attach_integrity

        _attach_integrity(self)

    # -- events
    def _persist(self, ev: HubEvent) -> None:
        if ev.persisted:
            try:
                self.store.insert_event(ev)
            except Exception:  # pragma: no cover - không để lỗi DB làm chết agent
                log.exception("không ghi được event %s", ev.type)

    def publish(self, ev: HubEvent) -> HubEvent:
        if ev.payload:  # H-12: che theo GIÁ TRỊ secret trước khi tới bus/DB/TUI/Slack/OTel
            from .redact import mask_obj

            ev.payload = mask_obj(ev.payload)
        return self.bus.publish(ev)

    def backfill(self, *, run_id: str | None, agent_id: str | None):
        def _f(since_seq: int) -> list[HubEvent]:
            return self.store.query_events(run_id=run_id, agent_id=agent_id, epoch=self.bus.epoch, since_seq=since_seq)

        return _f

    # -- lifecycle
    def start(self) -> None:
        n = self.runs.reconcile_on_start()
        try:  # H-12: mcp.json (secret đã resolve) của run cũ còn sót khi daemon chết → xoá
            from .guard.integrity import cleanup_runtime_dirs

            gone = cleanup_runtime_dirs(self, keep=set(self.runs.active))
            if gone:
                log.info("đã xoá %d thư mục runtime còn sót", len(gone))
        except Exception:  # pragma: no cover
            log.exception("không dọn được runtime/")
        self.integrity.start()  # SEC: so policy/ với bản chuẩn + vòng kiểm nền
        self._warn_if_sandbox_disabled()
        try:  # R2-1(c): .pth/sitecustomize lạ trong venv của hub → cảnh báo đỏ
            from .guard.integrity import check_python_env

            check_python_env(self)
        except Exception:  # pragma: no cover
            log.exception("không kiểm được môi trường Python")
        self.publish(make_event("hub.started", None, {"epoch": self.bus.epoch, "version": __version__, "reconciled_runs": n}))
        self.dispatcher.start()  # M3: vòng tick dispatcher (cần event loop đang chạy)
        self.manager.start()  # M7: vòng nhịp điều phối viên (chỉ tick khi có việc)
        self.questions.start()  # UX: chuyển câu hỏi quá hạn của điều phối viên lên người + nhắc lại
        self.compat.check_on_start()  # hardening H-10: phiên bản CLI → hub_state.cli_compat (header TUI)
        try:  # vòng nhắc approval + purge retention (cần event loop đang chạy)
            import asyncio

            self._bg.append(asyncio.get_running_loop().create_task(self.approvals.loop()))
        except RuntimeError:
            pass

    async def stop(self) -> None:
        self.publish(make_event("hub.stopping", None, {"epoch": self.bus.epoch}))
        await self.dispatcher.stop()  # M3
        await self.manager.stop()  # M7
        await self.questions.stop()
        await self.runs.shutdown()
        self.integrity.stop()
        for t in self._bg:
            t.cancel()
        await self.notifier.stop()
        self.bus.close_all()
        self.store.close()

    def _warn_if_sandbox_disabled(self) -> None:
        """SEC/WT-3: nhiều bất biến dựa vào sandbox OS — nếu sandbox TẮT bằng cấu hình (kể cả khi guard.enabled=false), cảnh báo đỏ + audit."""
        try:
            sb = self.config.get("guard.sandbox") or {}
        except KeyError:
            return
        if sb.get("enabled", True):
            return
        self.publish(make_event("guard.sandbox_disabled", None, {
            "message": "Sandbox OS cho Bash đang TẮT (guard.sandbox.enabled=false). Chỉ còn classifier làm lớp chặn đọc token/secret và ghi policy — rủi ro cao hơn.",
            "fail_closed": False,
        }, source="hub"))
        try:
            from .approvals import audit_record

            audit_record("guard.sandbox_disabled", agent_id=None, run_id=None, data={"enabled": False})
        except Exception:  # pragma: no cover
            pass

    def status(self) -> dict[str, Any]:
        cfg = self.config.data
        return {
            "version": __version__,
            "home": str(self.paths.home),
            "epoch": self.bus.epoch,
            "last_seq": self.bus.last_seq,
            "runtime": cfg["runtime"]["kind"],
            "max_concurrent_runs": cfg["hub"]["max_concurrent_runs"],
            "running": len(self.runs.active),
            "queued": len(self.runs.queue),
            "estop": self.runs.estop,
            "rate_limit": self.store.state_get("rate_limit"),  # M3 FR-RT-16
            "teams_estop": [r[0] for r in self.store.conn.execute("SELECT id FROM teams WHERE estop=1 AND archived_at IS NULL")],  # M3
            "auth_block": self.runs.auth_block,
            "agents": len(self.store.list_agents()),
            "cost_today_usd": round(self.store.cost_today(), 8),
            "epoch_usage": self.aggregator.snapshot(),
            "subscribers": self.bus.subscriber_count,
            "sandbox_enabled": bool((self.config.get("guard.sandbox") or {}).get("enabled", True)),
        }
