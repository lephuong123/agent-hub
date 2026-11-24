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
from .trace import current_req_id as _current_req_id

log = logging.getLogger("cfharness.hub")


class Hub:
    def __init__(self, paths: HubPaths, *, runtime: AgentRuntime | None = None) -> None:
        self.paths = paths
        paths.ensure()
        self.config = HubConfig(paths.config)
        from .logs import LevelController

        self.log_levels = LevelController()  # L1: logs.level/levels + level tạm, áp lúc chạy (logs.level_loop)
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
        # tự resume có giới hạn run ad-hoc dừng vì lỗi tạm thời (crashed/timed_out/rate_limited/mạng) — auto_resume.py
        from .auto_resume import AutoResumer

        self.auto_resume = AutoResumer(self)
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
        # Hệ thống ngoài hub (`hub.systems`: app trong ~/Workspace + job launchd/Task Scheduler) — systems/service.py
        from .systems import attach as _attach_systems

        _attach_systems(self)
        # Lịch chạy tự động (`hub.schedules`: cron → điều phối viên/agent/task; vòng nền tự start ở `hub.started`) — schedules/service.py
        from .schedules import attach as _attach_schedules

        _attach_schedules(self)
        # Danh sách tài khoản Claude (`hub.accounts`: token setup-token nhiều tài khoản, NGƯỜI bấm đổi — không tự đổi) — accounts/service.py
        from .accounts import attach as _attach_accounts

        _attach_accounts(self)
        # Tổng quan máy (Q6): xuất bản tóm tắt CHỈ SỐ LIỆU ra thư mục chung cho admin (`hub.admin_export`; tắt mặc định) — admin_overview.py
        from .admin_overview import attach as _attach_admin_export

        _attach_admin_export(self)
        # Sao lưu HUB_HOME (`hub.backups`: tạo/kiểm/liệt kê, vòng nền theo backup.schedule) — backup.py, docs/dev-notes/backup.md
        from .backup import attach as _attach_backup

        _attach_backup(self)
        # Thư mục dùng chung với "Không gian số" (`hub.filehub`: giám sát binary sync, trạng thái, presence, steer, báo sau run) — filehub/
        from .filehub import attach as _attach_filehub

        _attach_filehub(self)
        # Quản trị máy (plan-admin-users): ESTOP admin bằng file cờ (`hub.admin_control`), chính sách root (`hub.admin_policy`), audit metadata
        # (`hub.admin_audit`), cảnh báo + lịch sử cho hub của admin (`hub.admin_watch`) — admin_control.py, admin_policy.py, admin_audit.py, admin_watch.py
        from .admin_audit import attach as _attach_admin_audit
        from .admin_control import attach as _attach_admin_control
        from .admin_policy import attach as _attach_admin_policy
        from .admin_watch import attach as _attach_admin_watch

        _attach_admin_control(self)
        _attach_admin_policy(self)
        _attach_admin_audit(self)
        _attach_admin_watch(self)
        # Super Admin Console: hộp thư "Việc được giao" của Team user (`hub.team_inbox`) — team_inbox.py
        from .team_inbox import attach as _attach_team_inbox

        _attach_team_inbox(self)
        # L4: chuỗi băm audit — neo định kỳ/khi dừng vào HUB_HOME/audit-anchors.jsonl (+ metadata admin), kiểm đuôi lúc khởi động — audit_chain.py
        from .audit_chain import attach as _attach_audit_chain

        _attach_audit_chain(self)
        # L5: cảnh báo theo mẫu audit (`hub.audit_watch`, row_tap của hub.audit) + lưu trữ audit cũ ra file (`hub.audit_archive`, tắt mặc
        # định — audit.archive_after_days) — audit_watch.py, audit_archive.py
        from .audit_archive import attach as _attach_audit_archive
        from .audit_watch import attach as _attach_audit_watch

        _attach_audit_watch(self)
        _attach_audit_archive(self)
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
        if ev.req_id is None:  # L2: event của run lấy req_id của run (RunManager._run bind nó), còn lại = lượt xử lý đang bind
            ev.req_id = _current_req_id()
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
        try:  # S8-B: ledger write-ahead — hoàn tất / bỏ / gắn cờ thay đổi học dở dang khi daemon chết giữa Ledger.apply
            self.learning.ledger.recover_pending()
        except Exception:  # pragma: no cover
            log.exception("không khôi phục được ledger_pending")
        try:  # H-12: mcp.json (secret đã resolve) của run cũ còn sót khi daemon chết → xoá
            from .guard.integrity import cleanup_runtime_dirs

            gone = cleanup_runtime_dirs(self, keep=set(self.runs.active))
            if gone:
                log.info("đã xoá %d thư mục runtime còn sót", len(gone))
        except Exception:  # pragma: no cover
            log.exception("không dọn được runtime/")
        self.integrity.start()  # SEC: so policy/ với bản chuẩn + vòng kiểm nền
        try:  # plan-mcp-claude-native PR1: giá trị header/env/args MCP trong cấu hình claude (mặc định + mọi tài khoản) → bộ che
            self.claude_redact.refresh()  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            log.exception("không nạp được giá trị secret của cấu hình claude vào bộ che")
        try:  # L4: kiểm đuôi chuỗi băm audit (sửa/xoá khi daemon dừng) + neo + vòng neo định kỳ
            self.audit_chain.start()  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            log.exception("không khởi động được chuỗi băm audit")
        try:  # L5: nạp tập principal đã thấy + vòng báo "hết cảnh báo"; vòng lưu trữ audit (chỉ chạy khi audit.archive_after_days > 0)
            self.audit_watch.start()  # type: ignore[attr-defined]
            self.audit_archive.start()  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover
            log.exception("không khởi động được audit_watch/audit_archive")
        try:  # hub clone PR5: lần đầu lên sau `hub import` → viết lại đường dẫn máy cũ trong tri thức qua Ledger (actor import)
            from .import_followups import process as _import_followups

            _import_followups(self)
        except Exception:  # pragma: no cover — không chặn daemon khởi động; còn `status != done` → thử lại lần sau
            log.exception("không xử lý được import_followups")
        try:  # P2.4: đăng ký HUB_HOME vào registry hub của user OS → hub khác chặn đọc/ghi home này
            from .confine import register_home

            register_home(self.paths.home)
        except Exception:  # pragma: no cover
            log.exception("không ghi được registry hub")
        self._warn_if_sandbox_disabled()
        try:  # R2-1(c): .pth/sitecustomize lạ trong venv của hub → cảnh báo đỏ
            from .guard.integrity import check_python_env

            check_python_env(self)
        except Exception:  # pragma: no cover
            log.exception("không kiểm được môi trường Python")
        self.publish(make_event("hub.started", None, {"epoch": self.bus.epoch, "version": __version__, "reconciled_runs": n}))
        self.dispatcher.start()  # M3: vòng tick dispatcher (cần event loop đang chạy)
        if self.runs.auth_block:
            self.runs.start_auth_watch()  # cờ chặn đăng nhập còn từ lần chạy trước → kiểm lại định kỳ, tự gỡ khi đã đăng nhập
        self.auto_resume.start()  # hẹn lại lượt tự resume đã lên lịch trước khi daemon dừng
        self.manager.start()  # M7: vòng nhịp điều phối viên (chỉ tick khi có việc)
        try:  # dò pdftoppm trong thread nền (system prompt của run đầu tiên đã có kết quả thật; không chạy tiến trình con trên event loop)
            from .pdf_support import warm as _warm_pdf

            _warm_pdf(self.config)
        except Exception:  # pragma: no cover - best-effort
            log.debug("dò pdftoppm lỗi", exc_info=True)
        self.questions.start()  # UX: chuyển câu hỏi quá hạn của điều phối viên lên người + nhắc lại
        self.compat.check_on_start()  # hardening H-10: phiên bản CLI → hub_state.cli_compat (header TUI)
        try:  # vòng nhắc approval + purge retention (cần event loop đang chạy)
            import asyncio

            self._bg.append(asyncio.get_running_loop().create_task(self.approvals.loop(), name="approvals-loop"))
        except RuntimeError:
            pass
        try:  # L1: level log đổi lúc chạy (config logs.level/levels + `cfharness logs level … --for`)
            import asyncio

            from .logs import level_loop

            self._bg.append(asyncio.get_running_loop().create_task(level_loop(self), name="log-levels"))
        except RuntimeError:
            pass
        try:  # web còn chạy code cũ sau cập nhật (macOS/Linux) → dừng để launchd/daemon chạy lại bằng code mới — web/reload.py
            import asyncio

            from .web.reload import stale_web_loop

            self._bg.append(asyncio.get_running_loop().create_task(stale_web_loop(self), name="stale-web-loop"))
        except RuntimeError:
            pass
        from .session_search import start_backfill  # v17: chỉ mục hội thoại cũ theo lô nền (session_search.py)

        start_backfill(self)
        try:  # graph memory: vector bge-m3 cho fact (chỉ khi cài extra `cfharness[vector]`) — embed nền theo lô, không chặn run
            from .knowledge.graph.embed import start_vector_loop

            start_vector_loop(self)
        except Exception:  # pragma: no cover
            log.exception("không khởi động được vòng vector graph memory")

    async def stop(self) -> None:
        self.publish(make_event("hub.stopping", None, {"epoch": self.bus.epoch}))
        await self.dispatcher.stop()  # M3
        self.auto_resume.stop()  # lịch hẹn vẫn ở hub_state, lần khởi động sau chạy tiếp
        await self.manager.stop()  # M7
        await self.questions.stop()
        await self.filehub.stop()  # type: ignore[attr-defined]  # SIGTERM tiến trình sync (dừng êm, không bỏ dở commit)
        await self.accounts.close_all_logins()  # type: ignore[attr-defined]  # kill tiến trình `claude setup-token` đang chờ code
        if getattr(self, "mcp_claude", None) is not None:
            await self.mcp_claude.close_all()  # type: ignore[attr-defined]  # kill `claude mcp login` đang chờ URL chuyển hướng
        await self.runs.shutdown()
        self.integrity.stop()
        self.audit_watch.stop()  # type: ignore[attr-defined]
        self.audit_archive.stop()  # type: ignore[attr-defined]
        self.audit_chain.stop()  # type: ignore[attr-defined]  # L4: neo dòng audit cuối trước khi đóng DB
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
            log.debug("không ghi được audit guard.sandbox_disabled", exc_info=True)

    def _account_brief(self) -> dict[str, Any] | None:
        try:
            return self.accounts.active_brief()  # type: ignore[attr-defined]
        except Exception:  # pragma: no cover — hub.status không được hỏng vì secret store
            log.exception("không đọc được tài khoản Claude đang dùng")
            return None

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
            "rate_limit_gate": self.store.state_get("rate_limit_gate"),  # dispatcher không claim tới `until` (hạn mức đã rejected)
            "teams_estop": [r[0] for r in self.store.conn.execute("SELECT id FROM teams WHERE estop=1 AND archived_at IS NULL")],  # M3
            "auth_block": self.runs.auth_block,
            "account": self._account_brief(),  # tài khoản Claude đang dùng {id, label} | None (accounts/)
            "agents": len(self.store.list_agents()),
            "cost_today_usd": round(self.store.cost_today(), 8),
            "epoch_usage": self.aggregator.snapshot(),
            "subscribers": self.bus.subscriber_count,
            "sandbox_enabled": bool((self.config.get("guard.sandbox") or {}).get("enabled", True)),
            "migrated_to": self.runs.frozen,  # chuyển nhà (hub_freeze.py): hub đóng băng — banner ở CLI/TUI/web
            "log_errors": self._log_errors(),  # L6 (B5): WARNING/ERROR 1 h/24 h theo logger, rpc.slow, lỗi không ai bắt — chỉ con số
        }

    def _log_errors(self) -> dict[str, Any]:
        try:
            from .log_query import error_stats

            return error_stats(self)
        except Exception:  # best-effort: hub.status không được hỏng vì bộ đếm
            log.exception("không đọc được bộ đếm lỗi log")
            return {}
