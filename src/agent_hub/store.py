"""Kho SQLite (WAL) của hub. MỘT writer duy nhất là daemon (NFR-07).

Migration đơn giản: danh sách câu SQL theo phiên bản, áp dụng tuần tự, ghi `PRAGMA user_version`.
Bảng `events` là append-only: trigger chặn UPDATE/DELETE.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .events import HubEvent, iso

MIGRATIONS: list[str] = [
    # v1 — M0
    """
    CREATE TABLE agents (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        home_path TEXT NOT NULL,
        model TEXT,
        effort TEXT,
        fallback_model TEXT,
        tool_policy TEXT NOT NULL DEFAULT '{}',
        mcp_servers TEXT NOT NULL DEFAULT '[]',
        limits TEXT NOT NULL DEFAULT '{}',
        workspace_default TEXT,
        status TEXT NOT NULL DEFAULT 'idle',
        current_run_id TEXT,
        config_revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT,
        deleted_at TEXT,
        purge_after TEXT,
        trash_path TEXT
    );
    CREATE TABLE agent_revisions (
        agent_id TEXT NOT NULL REFERENCES agents(id),
        revision INTEGER NOT NULL,
        config_snapshot TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT 'user',
        created_at TEXT NOT NULL,
        PRIMARY KEY (agent_id, revision)
    );
    CREATE TABLE sessions (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL REFERENCES agents(id),
        first_run_id TEXT,
        last_run_id TEXT,
        forked_from TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        task_id TEXT,
        agent_id TEXT NOT NULL REFERENCES agents(id),
        agent_config_revision INTEGER,
        kind TEXT NOT NULL DEFAULT 'adhoc',
        prompt TEXT,
        session_id TEXT,
        resumed_from_session TEXT,
        pid INTEGER,
        pid_start_time REAL,
        argv_redacted TEXT,
        cwd TEXT,
        system_prompt_hash TEXT,
        cli_version TEXT,
        model TEXT,
        max_budget_usd REAL,
        status TEXT NOT NULL,
        outcome TEXT,
        queued_at TEXT NOT NULL,
        started_at TEXT,
        ended_at TEXT,
        usage TEXT,
        model_usage TEXT,
        cost_usd REAL,
        cost_basis TEXT,
        duration_ms INTEGER,
        num_turns INTEGER,
        summary TEXT,
        error TEXT,
        permission_denials TEXT,
        terminal_reason TEXT,
        isolation_violations TEXT,
        raw_log_path TEXT,
        stderr_log_path TEXT,
        exit_code INTEGER
    );
    CREATE INDEX runs_agent ON runs(agent_id, queued_at);
    CREATE INDEX runs_status ON runs(status);
    CREATE TABLE events (
        epoch TEXT NOT NULL,
        seq INTEGER NOT NULL,
        ts TEXT NOT NULL,
        type TEXT NOT NULL,
        level TEXT NOT NULL,
        source TEXT NOT NULL,
        agent_id TEXT,
        team_id TEXT,
        task_id TEXT,
        run_id TEXT,
        session_id TEXT,
        subagent_id TEXT,
        payload TEXT NOT NULL,
        PRIMARY KEY (epoch, seq)
    );
    CREATE INDEX events_run ON events(run_id, epoch, seq);
    CREATE INDEX events_agent ON events(agent_id, ts);
    CREATE TRIGGER events_no_update BEFORE UPDATE ON events
        BEGIN SELECT RAISE(ABORT, 'events là append-only'); END;
    CREATE TRIGGER events_no_delete BEFORE DELETE ON events
        BEGIN SELECT RAISE(ABORT, 'events là append-only'); END;
    CREATE TABLE hub_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # v2 — spike A-03: `total_cost_usd` sau --resume là LUỸ KẾ theo session → giữ mốc để tính chênh lệch
    """
    ALTER TABLE sessions ADD COLUMN cumulative_cost_usd REAL NOT NULL DEFAULT 0;
    ALTER TABLE runs ADD COLUMN session_cost_total_usd REAL;
    """,
    # v3 — M1 luồng A: registry MCP (FR-MC-01), audit append-only (FR-OB-08), signals (FR-LN-01 phần ghi),
    # idempotency cho MCP server (FR-MS-07)
    """
    CREATE TABLE mcp_servers (
        name TEXT PRIMARY KEY,
        transport TEXT NOT NULL,
        config TEXT NOT NULL,
        trust TEXT NOT NULL DEFAULT 'untrusted',
        tools_include TEXT NOT NULL DEFAULT '[]',
        tools_exclude TEXT NOT NULL DEFAULT '[]',
        annotations_cache TEXT,
        last_probe_at TEXT,
        probe_error TEXT,
        source TEXT NOT NULL DEFAULT 'manual',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        deleted_at TEXT,
        trash_path TEXT
    );
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        actor TEXT NOT NULL,
        channel TEXT NOT NULL,
        agent_id TEXT,
        run_id TEXT,
        target TEXT,
        summary TEXT,
        data TEXT
    );
    CREATE INDEX audit_kind ON audit_log(kind, ts);
    CREATE INDEX audit_run ON audit_log(run_id);
    CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log là append-only'); END;
    CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
        BEGIN SELECT RAISE(ABORT, 'audit_log là append-only'); END;
    CREATE TABLE signals (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        team_id TEXT,
        run_id TEXT,
        task_id TEXT,
        kind TEXT NOT NULL,
        source TEXT NOT NULL,
        evidence TEXT NOT NULL DEFAULT '{}',
        processed_by_review_id TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX signals_agent ON signals(agent_id, created_at);
    CREATE TABLE idempotency_keys (
        key TEXT PRIMARY KEY,
        method TEXT NOT NULL,
        result TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
    # v4 — M1 luồng B: phê duyệt thao tác phá huỷ (FR-RT-09, FR-SG-04), quyết định guard append-only (FR-SG-03/08),
    # resume/continue (FR-RT-05, FR-TK-14, FR-RT-19). Tài nguyên được bảo vệ nằm ở HUB_HOME/policy/ (FR-SG-05), không ở DB.
    """
    CREATE TABLE approval_requests (
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        session_id TEXT,
        kind TEXT NOT NULL DEFAULT 'destructive',
        channel TEXT,
        tool_name TEXT NOT NULL,
        tool_input TEXT,
        tool_use_id TEXT,
        action_fingerprint TEXT NOT NULL,
        destructive_class TEXT,
        matched_rule TEXT,
        risk_level TEXT,
        reason TEXT,
        protected_resource_id TEXT,
        status TEXT NOT NULL,
        opened_at TEXT NOT NULL,
        waiting_human_at TEXT,
        reminder_count INTEGER NOT NULL DEFAULT 0,
        last_reminded_at TEXT,
        resolved_at TEXT,
        resolved_by TEXT,
        resolved_channel TEXT,
        wait_s REAL,
        note TEXT,
        false_positive INTEGER,
        ticket_used_at TEXT,
        ticket_run_id TEXT,
        resume_run_id TEXT
    );
    CREATE INDEX approvals_status ON approval_requests(status, opened_at);
    CREATE INDEX approvals_run ON approval_requests(run_id);
    CREATE INDEX approvals_session ON approval_requests(session_id, action_fingerprint);
    CREATE TABLE guard_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        run_id TEXT,
        agent_id TEXT,
        channel TEXT,
        tool_name TEXT NOT NULL,
        input_redacted TEXT,
        matched_rule TEXT,
        classifier TEXT NOT NULL DEFAULT 'static',
        verdict TEXT NOT NULL,
        action TEXT NOT NULL,
        reason TEXT,
        approval_request_id TEXT
    );
    CREATE INDEX guard_decisions_run ON guard_decisions(run_id, ts);
    CREATE TRIGGER guard_decisions_no_update BEFORE UPDATE ON guard_decisions
        BEGIN SELECT RAISE(ABORT, 'guard_decisions là append-only'); END;
    CREATE TRIGGER guard_decisions_no_delete BEFORE DELETE ON guard_decisions
        BEGIN SELECT RAISE(ABORT, 'guard_decisions là append-only'); END;
    ALTER TABLE runs ADD COLUMN parent_run_id TEXT;
    ALTER TABLE runs ADD COLUMN resume_reason TEXT;
    ALTER TABLE runs ADD COLUMN resume_ticket_id TEXT;
    ALTER TABLE runs ADD COLUMN waiting_approval_id TEXT;
    ALTER TABLE runs ADD COLUMN continued_in_run_id TEXT;
    ALTER TABLE runs ADD COLUMN human_wait_s REAL;
    CREATE INDEX runs_session ON runs(session_id, queued_at);
    """,
    # v5 — M2: steer (FR-RT-06) đếm theo run; takeover bằng Claude Code tương tác (FR-UI-10);
    # index cho firehose/ảnh chụp của TUI (`tui.snapshot`, FR-UI-01/09)
    """
    ALTER TABLE runs ADD COLUMN steer_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE runs ADD COLUMN takeover_of_run_id TEXT;
    CREATE INDEX events_type_ts ON events(type, ts);
    """,
    # v6 — M3: team (FR-TM-01..04), task + DAG + comment (FR-TK-02..13), run gắn task/team, lease/heartbeat (FR-TK-06),
    # workspace checkpoint/diff/revert (FR-RT-12), output có cấu trúc (FR-RT-14), subagent (FR-RT-13), clone (FR-AG-06)
    """
    CREATE TABLE teams (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        charter TEXT NOT NULL DEFAULT '',
        policies TEXT NOT NULL DEFAULT '{}',
        estop INTEGER NOT NULL DEFAULT 0,
        estop_reason TEXT,
        estop_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT,
        deleted_at TEXT,
        purge_after TEXT,
        trash_path TEXT
    );
    CREATE TABLE team_members (
        team_id TEXT NOT NULL REFERENCES teams(id),
        agent_id TEXT NOT NULL REFERENCES agents(id),
        role TEXT NOT NULL,
        max_in_progress INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        PRIMARY KEY (team_id, agent_id)
    );
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        team_id TEXT,
        assignee_agent_id TEXT,
        role TEXT,
        kind TEXT NOT NULL DEFAULT 'task',
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 0,
        created_by TEXT NOT NULL DEFAULT 'human',
        workspace TEXT,
        overrides TEXT,
        result_schema TEXT,
        max_retries INTEGER,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        rate_limit_hits INTEGER NOT NULL DEFAULT 0,
        review_rounds INTEGER NOT NULL DEFAULT 0,
        idempotency_key TEXT UNIQUE,
        block_reason TEXT,
        wait_reason TEXT,
        not_before TEXT,
        origin_task_id TEXT,
        decomposed INTEGER NOT NULL DEFAULT 0,
        plan_key TEXT,
        current_run_id TEXT,
        retry_from_run_id TEXT,
        claimed_at TEXT,
        lease_expires_at TEXT,
        last_heartbeat_at TEXT,
        result_summary TEXT,
        result_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        done_at TEXT,
        archived_at TEXT,
        deleted_at TEXT,
        purge_after TEXT
    );
    CREATE INDEX tasks_status ON tasks(status, priority, created_at);
    CREATE INDEX tasks_team ON tasks(team_id, status);
    CREATE INDEX tasks_origin ON tasks(origin_task_id);
    CREATE TABLE task_links (
        parent_id TEXT NOT NULL,
        child_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (parent_id, child_id)
    );
    CREATE INDEX task_links_child ON task_links(child_id);
    CREATE TABLE task_comments (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        author TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'comment',
        text TEXT NOT NULL,
        run_id TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX task_comments_task ON task_comments(task_id, created_at);
    ALTER TABLE runs ADD COLUMN team_id TEXT;
    ALTER TABLE runs ADD COLUMN run_opts TEXT;
    ALTER TABLE runs ADD COLUMN max_runtime_s REAL;
    ALTER TABLE runs ADD COLUMN lease_expires_at TEXT;
    ALTER TABLE runs ADD COLUMN last_heartbeat_at TEXT;
    ALTER TABLE runs ADD COLUMN workspace_key TEXT;
    ALTER TABLE runs ADD COLUMN checkpoint TEXT;
    ALTER TABLE runs ADD COLUMN diff TEXT;
    ALTER TABLE runs ADD COLUMN result_json TEXT;
    ALTER TABLE runs ADD COLUMN schema_valid INTEGER;
    ALTER TABLE runs ADD COLUMN subagent_stats TEXT;
    ALTER TABLE runs ADD COLUMN task_signal TEXT;
    ALTER TABLE runs ADD COLUMN reverted_at TEXT;
    CREATE INDEX runs_task ON runs(task_id, queued_at);
    CREATE INDEX runs_team ON runs(team_id, ended_at);
    CREATE INDEX events_task ON events(task_id, epoch, seq);
    ALTER TABLE agents ADD COLUMN subagents TEXT;
    ALTER TABLE agents ADD COLUMN result_schema TEXT;
    ALTER TABLE agents ADD COLUMN cloned_from TEXT;
    """,
    # v7 — M4-A lõi học: chỉ mục skill + pin (FR-LN-11, FR-SG-06), review job (FR-LN-02/03), proposal (FR-LN-04),
    # ledger append-only + snapshot content-addressed (FR-LN-06), dấu vân tay Signal (FR-LN-01), run fork (FR-RT-05)
    """
    CREATE TABLE skills (
        scope TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        path TEXT NOT NULL,
        created_by TEXT NOT NULL DEFAULT 'user',
        pinned INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'active',
        use_count INTEGER NOT NULL DEFAULT 0,
        last_used_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        deleted_at TEXT,
        purge_after TEXT,
        archived_path TEXT,
        PRIMARY KEY (scope, owner_id, name)
    );
    CREATE TABLE knowledge_pins (
        scope TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        target TEXT NOT NULL,
        ref TEXT NOT NULL,
        created_by TEXT NOT NULL DEFAULT 'user',
        created_at TEXT NOT NULL,
        PRIMARY KEY (scope, owner_id, target, ref)
    );
    CREATE TABLE review_jobs (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        trigger TEXT NOT NULL,
        signal_ids TEXT NOT NULL DEFAULT '[]',
        focus TEXT,
        status TEXT NOT NULL,
        source_run_id TEXT,
        source_session_id TEXT,
        run_id TEXT,
        not_before TEXT,
        reason TEXT,
        result TEXT,
        cost_usd REAL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        ended_at TEXT
    );
    CREATE INDEX review_jobs_agent ON review_jobs(agent_id, status);
    CREATE TABLE proposals (
        id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        agent_id TEXT,
        team_id TEXT,
        target TEXT NOT NULL,
        op TEXT NOT NULL,
        target_ref TEXT,
        payload TEXT NOT NULL DEFAULT '{}',
        diff TEXT,
        size_chars INTEGER,
        rationale TEXT,
        evidence_signal_ids TEXT NOT NULL DEFAULT '[]',
        created_by TEXT NOT NULL,
        review_job_id TEXT,
        run_id TEXT,
        risk_class TEXT,
        status TEXT NOT NULL,
        block_reason TEXT,
        block_detail TEXT,
        absorbed_into TEXT,
        not_before TEXT,
        eval TEXT,
        ledger_entry_id TEXT,
        superseded_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX proposals_agent ON proposals(agent_id, created_at);
    CREATE INDEX proposals_status ON proposals(status, not_before);
    CREATE TABLE snapshots (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        scope TEXT NOT NULL,
        owner_id TEXT,
        target TEXT NOT NULL,
        target_ref TEXT,
        blob TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TRIGGER snapshots_no_delete BEFORE DELETE ON snapshots
        BEGIN SELECT RAISE(ABORT, 'snapshots là append-only'); END;
    CREATE TABLE ledger_entries (
        id TEXT PRIMARY KEY,
        proposal_id TEXT,
        scope TEXT NOT NULL,
        owner_id TEXT,
        agent_id TEXT,
        target TEXT NOT NULL,
        target_ref TEXT,
        op TEXT NOT NULL,
        risk_class TEXT,
        snapshot_id TEXT,
        before_blob TEXT,
        after_blob TEXT,
        blob_kind TEXT NOT NULL DEFAULT 'file',
        actor TEXT NOT NULL,
        reason TEXT,
        summary TEXT,
        applied_at TEXT NOT NULL,
        eval_before REAL,
        eval_after REAL,
        flags TEXT NOT NULL DEFAULT '[]',
        rolled_back_by TEXT,
        rollback_of TEXT,
        config_revision INTEGER
    );
    CREATE INDEX ledger_agent ON ledger_entries(agent_id, applied_at);
    CREATE INDEX ledger_target ON ledger_entries(scope, owner_id, target, target_ref, applied_at);
    CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger_entries
        BEGIN SELECT RAISE(ABORT, 'ledger_entries là append-only'); END;
    CREATE TRIGGER ledger_core_immutable BEFORE UPDATE OF id, proposal_id, scope, owner_id, agent_id, target, target_ref, op, before_blob, after_blob, actor, applied_at, rollback_of ON ledger_entries
        BEGIN SELECT RAISE(ABORT, 'ledger_entries: trường lõi bất biến'); END;
    ALTER TABLE signals ADD COLUMN fingerprint TEXT;
    CREATE INDEX signals_fp ON signals(agent_id, kind, fingerprint);
    ALTER TABLE runs ADD COLUMN forked_from_session TEXT;
    """,
    # v8 — M4-B đánh giá & vận hành học: golden task do người quản lý (FR-LN-10, Q-07), eval gate + kết quả (FR-LN-10),
    # theo dõi hồi quy (FR-LN-17), digest (FR-LN-13), gợi ý golden task (Q-23)
    """
    CREATE TABLE IF NOT EXISTS golden_tasks (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        title TEXT,
        prompt TEXT NOT NULL,
        context TEXT,
        files TEXT,
        checker TEXT NOT NULL,
        output_schema TEXT,
        tags TEXT,
        source_run_id TEXT,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        version INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX IF NOT EXISTS golden_agent ON golden_tasks(agent_id, active);
    CREATE TABLE IF NOT EXISTS golden_suggestions (
        run_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        reason TEXT,
        proposal_id TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS eval_runs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        proposal_id TEXT,
        ledger_entry_ids TEXT,
        status TEXT NOT NULL,
        samples INTEGER,
        before_score REAL,
        after_score REAL,
        drop_pct REAL,
        cost_usd REAL,
        reason TEXT,
        verdict TEXT,
        detail TEXT,
        created_at TEXT NOT NULL,
        finished_at TEXT
    );
    CREATE INDEX IF NOT EXISTS eval_runs_agent ON eval_runs(agent_id, created_at);
    CREATE TABLE IF NOT EXISTS eval_results (
        id TEXT PRIMARY KEY,
        eval_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        golden_task_id TEXT NOT NULL,
        golden_version INTEGER NOT NULL DEFAULT 1,
        variant TEXT NOT NULL,
        fingerprint TEXT,
        sample INTEGER NOT NULL DEFAULT 0,
        score REAL NOT NULL,
        passed INTEGER NOT NULL DEFAULT 0,
        outcome TEXT,
        run_id TEXT,
        judge_run_ids TEXT,
        cost_usd REAL,
        detail TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS eval_results_cache ON eval_results(agent_id, golden_task_id, fingerprint, created_at);
    CREATE INDEX IF NOT EXISTS eval_results_eval ON eval_results(eval_id);
    CREATE TABLE IF NOT EXISTS eval_entry_log (
        entry_id TEXT PRIMARY KEY,
        eval_id TEXT,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS regression_watch (
        entry_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        applied_at TEXT NOT NULL,
        target TEXT,
        target_ref TEXT,
        risk_class TEXT,
        unevaluated INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        observed TEXT,
        baseline TEXT,
        reason TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS regression_agent ON regression_watch(agent_id, status);
    CREATE TABLE IF NOT EXISTS digests (
        id TEXT PRIMARY KEY,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        created_at TEXT NOT NULL,
        scheduled INTEGER NOT NULL DEFAULT 0,
        agent_id TEXT,
        counts TEXT,
        text TEXT NOT NULL,
        data TEXT,
        slack_status TEXT,
        tui_seen_at TEXT
    );
    """,
    # v9 — M4-B: actor của ledger entry trong theo dõi hồi quy (chỉ tự rollback thay đổi do vòng học/agent; của người → cảnh báo)
    """
    ALTER TABLE regression_watch ADD COLUMN actor TEXT;
    """,
    # v10 — M5-A: canary/shadow cho thay đổi tầng team/global/USER (FR-LN-16) + phiên bản tri thức gán theo agent (FR-RT-10)
    """
    CREATE TABLE IF NOT EXISTS tier_canaries (
        id TEXT PRIMARY KEY,
        proposal_id TEXT,
        ledger_entry_id TEXT,
        mode TEXT NOT NULL,
        scope TEXT NOT NULL,
        owner_id TEXT,
        target TEXT NOT NULL,
        target_ref TEXT,
        blob_kind TEXT,
        path TEXT,
        before_blob TEXT,
        after_blob TEXT,
        canary_agent_id TEXT,
        origin_agent_id TEXT,
        watch_agents TEXT,
        status TEXT NOT NULL,
        created_by TEXT,
        started_at TEXT NOT NULL,
        deadline TEXT,
        finished_at TEXT,
        reason TEXT,
        eval TEXT,
        observed TEXT,
        baseline TEXT
    );
    CREATE INDEX IF NOT EXISTS tier_canaries_status ON tier_canaries(status, path);
    """,
    # v11 — hardening SEC: bản chuẩn toàn vẹn policy/ + config.yaml (guard/integrity.py) — phát hiện/khôi phục ghi ngoài RPC
    """
    CREATE TABLE IF NOT EXISTS integrity_baseline (
        path TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        sha256 TEXT,
        content BLOB,
        updated_at TEXT NOT NULL
    );
    """,
    # v12 — hardening LRN (H-09, NFR-28/Q-10): retention 30 ngày cho events. `events` vẫn append-only, TRỪ hàng cũ hơn mốc
    # `retention_cutoff('events')` do job retention của daemon đặt (recovery.purge_retention). Không có mốc → không xoá được gì.
    """
    CREATE TABLE IF NOT EXISTS retention_cutoff (
        name TEXT PRIMARY KEY,
        cutoff TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    DROP TRIGGER IF EXISTS events_no_delete;
    CREATE TRIGGER events_no_delete BEFORE DELETE ON events
        WHEN OLD.ts >= COALESCE((SELECT cutoff FROM retention_cutoff WHERE name='events'), '')
        BEGIN SELECT RAISE(ABORT, 'events là append-only (chỉ job retention xoá được event quá hạn)'); END;
    """,
    # v13 — M7 MGR-CORE: hội thoại liên tục với điều phối viên (manager) theo kênh (session claude `--resume`) + tin nhắn.
    # Chỉ định manager nằm ở `hub_state.manager` (agent không ghi được hub.db) — không có bảng riêng.
    """
    CREATE TABLE IF NOT EXISTS manager_conversations (
        id TEXT PRIMARY KEY,
        channel TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        session_id TEXT,
        turns INTEGER NOT NULL DEFAULT 0,
        session_turns INTEGER NOT NULL DEFAULT 0,
        last_run_id TEXT,
        title TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT
    );
    CREATE INDEX IF NOT EXISTS manager_conv_channel ON manager_conversations(channel, updated_at);
    CREATE TABLE IF NOT EXISTS manager_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL REFERENCES manager_conversations(id),
        role TEXT NOT NULL,
        text TEXT NOT NULL,
        run_id TEXT,
        ts TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS manager_msg_conv ON manager_messages(conversation_id, id);
    """,
    # v14 — UX: câu hỏi có lựa chọn của agent (questions/). Agent trong task hỏi → điều phối viên (`with_manager`) → nếu không
    # chốt được thì người (`open`). Không có `expired` (không tự trả lời thay người). `resumed_at`: đã nối tiếp công việc sau khi trả lời.
    """
    CREATE TABLE IF NOT EXISTS questions (
        id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        run_id TEXT,
        session_id TEXT,
        task_id TEXT,
        conversation_id TEXT,
        source TEXT NOT NULL,
        text TEXT NOT NULL,
        options TEXT NOT NULL DEFAULT '[]',
        multi INTEGER NOT NULL DEFAULT 0,
        allow_free_text INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL,
        routed_to TEXT NOT NULL,
        escalated_at TEXT,
        escalation_note TEXT,
        answer TEXT,
        answered_by TEXT,
        answered_channel TEXT,
        answered_at TEXT,
        resumed_at TEXT,
        resumed_run_id TEXT,
        reminder_count INTEGER NOT NULL DEFAULT 0,
        last_reminded_at TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS questions_status ON questions(status, created_at);
    CREATE INDEX IF NOT EXISTS questions_task ON questions(task_id);
    CREATE INDEX IF NOT EXISTS questions_run ON questions(run_id);
    """,
]

JSON_COLS = {
    "agents": ("tool_policy", "mcp_servers", "limits", "subagents", "result_schema"),
    "runs": ("argv_redacted", "usage", "model_usage", "permission_denials", "isolation_violations",
             "run_opts", "checkpoint", "diff", "result_json", "subagent_stats", "task_signal"),  # M3: 6 cột cuối
    # M3
    "teams": ("policies",),
    "tasks": ("workspace", "overrides", "result_schema", "result_json"),
    # M4-A
    "review_jobs": ("signal_ids", "result"),
    "proposals": ("payload", "evidence_signal_ids", "eval"),
    "ledger_entries": ("flags",),
    # M5-A
    "tier_canaries": ("watch_agents", "eval", "observed", "baseline"),
    # UX — câu hỏi
    "questions": ("options", "answer"),
}


def now_iso() -> str:
    return iso()


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def _mask_secrets(v: Any) -> Any:
    """H-12 (SEC): che theo GIÁ TRỊ secret hub biết (redact.register_secret_values); không có secret → trả nguyên."""
    from .redact import mask_obj

    return mask_obj(v)


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ infra
    @property
    def schema_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    @property
    def journal_mode(self) -> str:
        return str(self.conn.execute("PRAGMA journal_mode").fetchone()[0])

    def migrate(self) -> int:
        cur = self.schema_version
        for i, sql in enumerate(MIGRATIONS[cur:], start=cur + 1):
            with self.tx():
                for stmt in _split_sql(sql):
                    self.conn.execute(stmt)
                self.conn.execute(f"PRAGMA user_version={i}")
        return self.schema_version

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def _row(self, table: str, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        d = dict(row)
        for c in JSON_COLS.get(table, ()):
            if d.get(c) is not None:
                d[c] = json.loads(d[c])
        return d

    def _insert(self, table: str, data: dict[str, Any]) -> None:
        data = _mask_secrets(data)  # H-12: che theo giá trị secret trước khi vào DB
        data = {k: (_dumps(v) if k in JSON_COLS.get(table, ()) and v is not None else v) for k, v in data.items()}
        cols = ", ".join(data)
        qs = ", ".join("?" for _ in data)
        self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(data.values()))

    def _update(self, table: str, key: str, key_val: Any, data: dict[str, Any]) -> None:
        if not data:
            return
        data = _mask_secrets(data)  # H-12
        data = {k: (_dumps(v) if k in JSON_COLS.get(table, ()) and v is not None else v) for k, v in data.items()}
        sets = ", ".join(f"{k}=?" for k in data)
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE {key}=?", (*data.values(), key_val))

    # ------------------------------------------------------------------ hub_state
    def state_get(self, key: str, default: Any = None) -> Any:
        r = self.conn.execute("SELECT value FROM hub_state WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def state_set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO hub_state(key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, _dumps(value), now_iso()),
        )

    # ------------------------------------------------------------------ events
    def insert_event(self, ev: HubEvent) -> None:
        self.conn.execute(
            "INSERT INTO events (epoch, seq, ts, type, level, source, agent_id, team_id, task_id, run_id, "
            "session_id, subagent_id, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ev.epoch,
                ev.seq,
                iso(ev.ts),
                ev.type,
                ev.level,
                ev.source,
                ev.agent_id,
                ev.team_id,
                ev.task_id,
                ev.run_id,
                ev.session_id,
                ev.subagent_id,
                _dumps(_mask_secrets(ev.payload)),
            ),
        )

    def query_events(
        self,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        epoch: str | None = None,
        since_seq: int | None = None,
        types: list[str] | None = None,
        limit: int = 10_000,
    ) -> list[HubEvent]:
        where, args = [], []
        if run_id is not None:
            where.append("run_id=?")
            args.append(run_id)
        if agent_id is not None:
            where.append("agent_id=?")
            args.append(agent_id)
        if epoch is not None:
            where.append("epoch=?")
            args.append(epoch)
        if since_seq is not None:
            where.append("seq>?")
            args.append(since_seq)
        if types:
            where.append(f"type IN ({','.join('?' for _ in types)})")
            args.extend(types)
        sql = "SELECT * FROM events" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts, epoch, seq LIMIT ?"
        args.append(limit)
        out = []
        for r in self.conn.execute(sql, args):
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(HubEvent(**d))
        return out

    # ------------------------------------------------------------------ agents
    def insert_agent(self, row: dict[str, Any]) -> None:
        self._insert("agents", row)

    def update_agent(self, agent_id: str, data: dict[str, Any]) -> None:
        self._update("agents", "id", agent_id, data)

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        return self._row("agents", self.conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())

    def list_agents(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agents" + ("" if include_archived else " WHERE archived_at IS NULL") + " ORDER BY id"
        return [self._row("agents", r) for r in self.conn.execute(sql)]  # type: ignore[misc]

    def add_revision(self, agent_id: str, revision: int, snapshot: dict[str, Any], actor: str = "user") -> None:
        self.conn.execute(
            "INSERT INTO agent_revisions(agent_id, revision, config_snapshot, actor, created_at) VALUES (?,?,?,?,?)",
            (agent_id, revision, _dumps(snapshot), actor, now_iso()),
        )

    def list_revisions(self, agent_id: str) -> list[dict[str, Any]]:
        out = []
        for r in self.conn.execute("SELECT * FROM agent_revisions WHERE agent_id=? ORDER BY revision", (agent_id,)):
            d = dict(r)
            d["config_snapshot"] = json.loads(d["config_snapshot"])
            out.append(d)
        return out

    # ------------------------------------------------------------------ runs
    def insert_run(self, row: dict[str, Any]) -> None:
        self._insert("runs", row)

    def update_run(self, run_id: str, data: dict[str, Any]) -> None:
        self._update("runs", "id", run_id, data)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self._row("runs", self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def find_run(self, prefix: str) -> dict[str, Any] | None:
        r = self.get_run(prefix)
        if r:
            return r
        rows = self.conn.execute("SELECT * FROM runs WHERE id LIKE ? LIMIT 2", (prefix + "%",)).fetchall()
        return self._row("runs", rows[0]) if len(rows) == 1 else None

    def list_runs(self, *, agent_id: str | None = None, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        where, args = [], []
        if agent_id:
            where.append("agent_id=?")
            args.append(agent_id)
        if status:
            where.append("status=?")
            args.append(status)
        sql = "SELECT * FROM runs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY queued_at DESC LIMIT ?"
        args.append(limit)
        return [self._row("runs", r) for r in self.conn.execute(sql, args)]  # type: ignore[misc]

    def cost_since(self, since: datetime, agent_id: str | None = None) -> float:
        sql = "SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE ended_at >= ?"
        args: list[Any] = [iso(since)]
        if agent_id:
            sql += " AND agent_id=?"
            args.append(agent_id)
        return float(self.conn.execute(sql, args).fetchone()[0])

    def cost_today(self, agent_id: str | None = None) -> float:
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.cost_since(start.astimezone(timezone.utc), agent_id)

    # ------------------------------------------------------------------ sessions
    def upsert_session(self, session_id: str, agent_id: str, run_id: str, forked_from: str | None = None) -> None:
        now = now_iso()
        self.conn.execute(
            "INSERT INTO sessions(id, agent_id, first_run_id, last_run_id, forked_from, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_run_id=excluded.last_run_id, updated_at=excluded.updated_at",
            (session_id, agent_id, run_id, run_id, forked_from, now, now),
        )

    def set_session_cost(self, session_id: str, cumulative: float) -> None:
        self.conn.execute("UPDATE sessions SET cumulative_cost_usd=?, updated_at=? WHERE id=?", (cumulative, now_iso(), session_id))

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(r) if r else None


def _split_sql(script: str) -> list[str]:
    """Tách script thành câu lệnh, giữ nguyên khối BEGIN…END của trigger."""
    out, buf, depth = [], [], 0
    for line in script.splitlines():
        s = line.strip()
        if not s:
            continue
        buf.append(line)
        up = s.upper()
        if up.startswith("BEGIN") or " BEGIN " in f" {up} ":
            depth += 1
        if up.startswith("END;") or up.endswith("END;"):
            depth -= 1
        if s.endswith(";") and depth == 0:
            out.append("\n".join(buf))
            buf = []
    if buf and "".join(buf).strip():
        out.append("\n".join(buf))
    return out


def purge_after(days: int) -> str:
    return iso(datetime.now(timezone.utc) + timedelta(days=days))
