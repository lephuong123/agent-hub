"""Run ad-hoc (FR-TK-01), dispatcher giới hạn đồng thời toàn cục (FR-TK-07) và ESTOP toàn cục (FR-TK-10).

Vòng đời run: queued → running → completed | failed | cancelled | budget_exceeded | timed_out | interrupted
                                  → waiting_human (M1: thao tác phá huỷ chờ người quá `approval.timeout_s`; tiến trình
                                    đã dừng, slot đồng thời được nhả, khoá thư mục giữ) → resumed (tiếp tục ở run mới).
M1-B: resume/continue (`--resume <session>`, FR-RT-05/FR-TK-14), khôi phục restart (`recovery.py`, FR-RT-19),
guard (hook PreToolUse + hub-guard permission tool, FR-RT-09) được nối trong `_prepare_spec`.
Dispatcher: hàng đợi FIFO; một run được khởi chạy khi
  (a) ESTOP tắt, (b) số run đang chạy < `hub.max_concurrent_runs` (đọc lại mỗi tick — đổi setting có hiệu lực ngay),
  (c) số run đang chạy của agent < `limits.max_concurrent_runs` của agent, (d) agent không `paused`.
M3: run gắn task/team (`task_id`, `team_id`, `run_opts`: json_schema/read_only/hub_self/…), điều tiết theo rate limit
(FR-RT-16: gần ngưỡng → đồng thời còn `ratelimit.throttle_concurrency`), ngân sách ngày hub/team/agent (FR-OB-03),
khoá theo GỐC REPO + checkpoint trước run + diff sau run (FR-RT-12, `workspace.py`), heartbeat/lease (FR-TK-06),
outcome mới `crashed` (tiến trình chết không có `result`), `rate_limited`, `blocked` (agent gọi task_block),
`protocol_violation`; `result_json`/`schema_valid` từ `structured_output` (FR-RT-14), `--agents` (FR-RT-13).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .agents import load_mcp_registry
from .errors import Blocked, HubError, NotFound
from .events import TERMINAL_RUN_EVENTS, HubEvent, make_event
from .logs import run_logger, set_run_context
from .prompt import build_system_prompt, prompt_hash
from .runtime import AgentRuntime, RunHandle, RunSpec, RuntimeError_, make_runtime
from .store import now_iso

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger("cfharness.runs")

# guard.enabled=false: tool được cho phép sẵn khi agent không giới hạn `tools` (không còn ai trả lời yêu cầu quyền của CLI)
GUARD_OFF_ALLOWED_TOOLS = ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Read", "Grep", "Glob", "WebFetch", "WebSearch", "TodoWrite")

AUTH_PATTERNS = re.compile(
    r"(not logged in|please run /login|run `?claude auth login|invalid api key|authentication_error|"
    r"oauth token (has )?expired|login required|unauthorized)",
    re.I,
)
TERMINAL_STATUSES = ("completed", "failed", "cancelled", "budget_exceeded", "timed_out", "interrupted", "resumed",
                     "crashed", "rate_limited", "blocked", "protocol_violation")  # M3: 4 outcome cuối
RATE_LIMIT_PATTERNS = re.compile(r"(rate[ _-]?limit|usage limit|too many requests|\b429\b|overloaded_error|\b529\b)", re.I)
RESUME_INTERRUPTED_PROMPT = (
    "[cfharness] Your previous turn was interrupted because the cfharness daemon restarted. "
    "Continue the task from where you left off. Re-check the current state before repeating any action that may already have run."
)


@dataclass
class ActiveRun:
    run_id: str
    agent_id: str
    handle: RunHandle | None = None
    runtime: AgentRuntime | None = None
    task: asyncio.Task | None = None
    result: dict[str, Any] | None = None
    timed_out: bool = False
    violations: list[dict[str, Any]] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    # M1-B
    pause_approval_id: str | None = None  # run sẽ chuyển waiting_human khi tiến trình dừng
    timer: asyncio.TimerHandle | None = None
    deadline: float | None = None  # loop.time() hết max_runtime_s
    clock_paused_at: float | None = None
    holds: int = 0
    lock_key: str | None = None
    static_deny: list[str] = field(default_factory=list)
    # M3
    task_id: str | None = None
    team_id: str | None = None
    crashed: bool = False  # dispatcher phát hiện tiến trình chết (lease hết hạn)
    last_hb: float = 0.0
    checkpoint: dict[str, Any] | None = None
    diff_done: bool = False
    kind: str = "adhoc"  # M4-A: run review không tính vào max_concurrent_runs của agent
    entered: bool = False  # hardening H-21: coroutine `_run` đã bắt đầu (khoá đặt chỗ trong tick được `_run` nhả)


class RunManager:
    def __init__(self, hub: "Hub", runtime: AgentRuntime | None = None) -> None:
        self.hub = hub
        self._runtime = runtime  # None → tạo từ cấu hình mỗi lần spawn
        self.queue: deque[str] = deque()
        self.active: dict[str, ActiveRun] = {}  # run đang chạy
        self.waiters: dict[str, asyncio.Event] = {}
        self._warned_versions: set[str] = set()
        self.shutting_down = False
        from .workspace import WorkspaceLocks

        self.locks = WorkspaceLocks(hub.store)  # M3: khoá theo gốc repo (FR-RT-12), thay bản tối thiểu M1-B

    # ------------------------------------------------------------------ state
    @property
    def store(self):
        return self.hub.store

    @property
    def estop(self) -> dict[str, Any]:
        return self.store.state_get("estop", {"on": False})

    @property
    def auth_block(self) -> dict[str, Any] | None:
        return self.store.state_get("auth_block")

    def runtime(self) -> AgentRuntime:
        return self._runtime or make_runtime(self.hub.config.data)

    def running_count(self, agent_id: str | None = None, exclude_kind: str | None = None) -> int:
        return sum(1 for a in self.active.values() if (agent_id is None or a.agent_id == agent_id) and (exclude_kind is None or a.kind != exclude_kind))

    def active_runs(self, agent_id: str) -> int:
        queued = sum(1 for rid in self.queue if (r := self.store.get_run(rid)) and r["agent_id"] == agent_id)
        return queued + self.running_count(agent_id)

    # ------------------------------------------------------------------ M3: sức chứa, rate limit, ngân sách ngày
    def effective_limit(self) -> int:
        """`hub.max_concurrent_runs`, giảm còn `ratelimit.throttle_concurrency` khi gần chạm hạn mức (FR-RT-16)."""
        limit = int(self.hub.config.get("hub.max_concurrent_runs"))
        rl = self.store.state_get("rate_limit", {}) or {}
        if rl.get("throttled"):
            limit = min(limit, int(self.hub.config.get("ratelimit.throttle_concurrency") or 1))
        return max(1, limit)

    def capacity_left(self) -> int:
        return self.effective_limit() - len(self.active) - len(self.queue)

    def budget_scopes(self, agent_id: str, team_id: str | None) -> list[tuple[str, str, float, float | None]]:
        """[(scope, owner, đã dùng hôm nay, hạn mức ngày)] cho hub → team → agent (FR-OB-03)."""
        out: list[tuple[str, str, float, float | None]] = []
        hub_lim = self.hub.config.get("budget.hub_usd_per_day")
        if hub_lim:
            out.append(("hub", "hub", self.store.cost_today(), float(hub_lim)))
        if team_id:
            team = self.hub.teams.find(team_id)
            if team is not None:
                lim = self.hub.teams.policies(team).budget_usd_per_day
                if lim:
                    out.append(("team", team_id, self.hub.teams.cost_today(team_id), float(lim)))
        ag = self.store.get_agent(agent_id) or {}
        lim = (ag.get("limits") or {}).get("budget_usd_per_day")
        if lim:
            out.append(("agent", agent_id, self.store.cost_today(agent_id), float(lim)))
        return out

    def _budget_warn(self, agent_id: str, team_id: str | None) -> None:
        pct = float(self.hub.config.get("budget.warn_pct") or 80) / 100.0
        from datetime import datetime as _dt

        day = _dt.now().strftime("%Y-%m-%d")
        flags = dict(self.store.state_get("budget_flags", {}) or {})
        changed = False
        for scope, owner, used, lim in self.budget_scopes(agent_id, team_id):
            if lim and used >= pct * lim and flags.get(f"warn:{scope}:{owner}") != day:
                flags[f"warn:{scope}:{owner}"] = day
                changed = True
                ev = make_event("budget.warning", agent_id if scope == "agent" else None, {"scope": scope, "owner": owner, "used": round(used, 6), "limit": lim, "pct": round(100 * used / lim, 1)})
                ev.team_id = team_id if scope == "team" else None
                self.hub.publish(ev)
        if changed:
            self.store.state_set("budget_flags", flags)

    # ------------------------------------------------------------------ submit
    def submit(
        self,
        agent_id: str,
        prompt: str,
        *,
        cwd: str | None = None,
        budget_usd: float | None = None,
        model: str | None = None,
        kind: str = "adhoc",
        resume_session_id: str | None = None,
        parent_run_id: str | None = None,
        resume_reason: str | None = None,
        resume_ticket_id: str | None = None,
        check_paused: bool = True,
        task_id: str | None = None,
        team_id: str | None = None,
        run_opts: dict[str, Any] | None = None,
        max_runtime_s: float | None = None,
        manager_issue: bool = False,
    ) -> dict[str, Any]:
        if not prompt or not prompt.strip():
            raise HubError("prompt rỗng")
        agent = self.hub.agents.get_row(agent_id)
        if cwd:  # MGR-1: workspace ad-hoc phải trong gốc cho phép, không trong vùng bảo vệ
            from .workspace import validate_workspace
            validate_workspace(self.hub, cwd, "cwd của run")
        if parent_run_id:  # M3: chuỗi resume/continue giữ nguyên task/team/tuỳ chọn run
            parent = self.store.get_run(parent_run_id) or {}
            if parent.get("agent_id") == agent_id:
                task_id = task_id or parent.get("task_id")
                team_id = team_id or parent.get("team_id")
                run_opts = run_opts if run_opts is not None else parent.get("run_opts")
                max_runtime_s = max_runtime_s or parent.get("max_runtime_s")
        # M7 MGR-3: vai trò manager (hub-self `manager`, hội thoại) CHỈ cho run do ManagerService tạo (`manager_issue`) hoặc chuỗi tiếp
        # nối NỘI BỘ của hub (sau approval / daemon restart) từ một run như vậy — continue/resume/steer từ nơi khác KHÔNG thừa kế.
        mgr_keep = False
        if run_opts and (run_opts.get("hub_self") == "manager" or any(k in run_opts for k in MANAGER_OPT_KEYS)):
            mgr = getattr(self.hub, "manager", None)
            mgr_keep = manager_issue or bool(parent_run_id and resume_reason in MANAGER_INTERNAL_RESUME and mgr is not None and mgr.is_issued(parent_run_id))
            if not mgr_keep:
                run_opts = {k: v for k, v in run_opts.items() if k not in MANAGER_OPT_KEYS and not (k == "hub_self" and v == "manager")} or None
        if team_id:
            team = self.hub.teams.find(team_id)
            if team is not None and team.get("estop"):
                raise Blocked(f"ESTOP của team '{team_id}' đang bật — không khởi chạy run mới. Lý do: {team.get('estop_reason') or '(không ghi)'}")
        # M7 MGR-4: ESTOP team chặn MỌI run của agent thành viên, bất kể đường vào (ad-hoc, task không team, team khác)
        est = self.store.conn.execute(
            "SELECT t.id, t.estop_reason FROM team_members m JOIN teams t ON t.id = m.team_id WHERE m.agent_id=? AND t.estop=1 AND t.archived_at IS NULL LIMIT 1",
            (agent_id,),
        ).fetchone()
        if est is not None:
            raise Blocked(f"agent '{agent_id}' thuộc team '{est[0]}' đang ESTOP — không khởi chạy run nào của agent này. Lý do: {est[1] or '(không ghi)'}",
                          data={"team_estop": est[0]})
        es = self.estop
        if es.get("on"):
            raise Blocked(f"ESTOP đang bật — không khởi chạy run mới. Lý do: {es.get('reason') or '(không ghi)'}", data={"estop": es})
        ab = self.auth_block
        if ab:
            raise Blocked(
                "claude chưa đăng nhập — chạy `claude auth login`, rồi `cfharness doctor` để mở khoá. "
                f"Chi tiết: {ab.get('message', '')[:200]}",
                data={"auth_block": ab},
            )
        if agent["status"] == "paused" and check_paused:
            raise Blocked(f"agent '{agent_id}' đang paused — `cfharness agent resume {agent_id}` trước")
        if cwd is not None and not Path(cwd).expanduser().is_dir():
            raise HubError(f"cwd không tồn tại: {cwd}")
        max_budget = self._budget_for(agent, budget_usd, team_id)
        run_id = "r_" + uuid.uuid4().hex[:12]
        session_id = resume_session_id or str(uuid.uuid4())
        if resume_session_id and (run_opts or {}).get("fork"):  # M4-A: fork → session MỚI, session gốc không đổi
            session_id = str(uuid.uuid4())
        row = {
            "id": run_id,
            "agent_id": agent_id,
            "agent_config_revision": agent["config_revision"],
            "kind": kind,
            "prompt": prompt,
            "session_id": session_id,
            "cwd": str(Path(cwd).expanduser().resolve()) if cwd else None,
            "model": model,
            "max_budget_usd": max_budget,
            "status": "queued",
            "queued_at": now_iso(),
            "resumed_from_session": resume_session_id,
            "forked_from_session": resume_session_id if (run_opts or {}).get("fork") else None,
            "parent_run_id": parent_run_id,
            "resume_reason": resume_reason,
            "resume_ticket_id": resume_ticket_id,
            "task_id": task_id,
            "team_id": team_id,
            "run_opts": run_opts or None,
            "max_runtime_s": max_runtime_s,
        }
        self.store.insert_run(row)
        if mgr_keep:
            self.hub.manager.mark_issued(run_id, (run_opts or {}).get("manager_conversation"))
        if task_id:
            self.hub.dispatcher.on_run_submitted(task_id, run_id, parent_run_id)
        seq_before = self.hub.bus.last_seq
        self.hub.publish(
            make_event(
                "run.queued",
                agent_id,
                {"kind": kind, "session_id": session_id, "max_budget_usd": max_budget, "prompt_preview": prompt[:200], "resume": bool(resume_session_id), "parent_run_id": parent_run_id, "resume_reason": resume_reason},
                run_id=run_id,
                session_id=session_id,
            )
        )
        self.queue.append(run_id)
        self.waiters[run_id] = asyncio.Event()
        self.tick()
        out = self.store.get_run(run_id) or row
        out["seq_before"] = seq_before
        out["epoch"] = self.hub.bus.epoch
        return out

    def reserved_budget(self, scope: str, owner: str) -> float:
        """H-19: phần ngân sách ngày đang GIỮ CHỖ bởi run chưa kết thúc (queued/running) = Σ(max_budget_usd − cost đã ghi).
        Chi phí của run chỉ vào `cost_today` lúc kết thúc → không trừ phần này thì nhiều run song song vượt trần."""
        where = "status IN ('queued','running') AND max_budget_usd IS NOT NULL"
        args: tuple[Any, ...] = ()
        if scope == "agent":
            where += " AND agent_id=?"
            args = (owner,)
        elif scope == "team":
            where += " AND team_id=?"
            args = (owner,)
        r = self.store.conn.execute(f"SELECT COALESCE(SUM(MAX(max_budget_usd - COALESCE(cost_usd, 0), 0)), 0) FROM runs WHERE {where}", args).fetchone()
        return float(r[0] or 0)

    def _budget_for(self, agent: dict[str, Any], budget_usd: float | None, team_id: str | None = None) -> float | None:
        """Trần `--max-budget-usd` của run = min(ngân sách run, phần còn lại của ngày ở hub/team/agent) (FR-OB-03).
        Phần còn lại TRỪ phần đang giữ chỗ của run chưa kết thúc (H-19) → tổng trần các run song song ≤ hạn mức ngày."""
        limits = agent.get("limits") or {}
        per_run = budget_usd or limits.get("max_budget_usd_per_run") or self.hub.config.get("runs.default_max_budget_usd")
        for scope, owner, used, day in self.budget_scopes(agent["id"], team_id):
            if not day:
                continue
            reserved = self.reserved_budget(scope, owner)
            if used < float(day) and round(float(day) - used - reserved, 6) <= 0:
                label = {"agent": f"agent '{owner}'", "team": f"team '{owner}'", "hub": "toàn hub"}[scope]
                raise Blocked(f"{label}: ngân sách ngày còn lại (${float(day) - used:.4f}) đang được giữ chỗ cho run đang chạy — chờ run kết thúc",
                              data={"scope": scope, "owner": owner, "reserved": round(reserved, 6), "reason": "budget_reserved"})
            remaining = round(float(day) - used - reserved, 6)
            if remaining <= 0:
                ev = make_event("budget.exceeded", agent["id"] if scope == "agent" else None, {"scope": scope, "owner": owner, "used": used, "limit": day})
                ev.team_id = team_id if scope == "team" else None
                self.hub.publish(ev)
                label = {"agent": f"agent '{owner}'", "team": f"team '{owner}'", "hub": "toàn hub"}[scope]
                raise Blocked(f"{label} đã dùng hết ngân sách ngày (${used:.4f}/${day})", data={"scope": scope, "owner": owner})
            per_run = min(float(per_run), remaining) if per_run else remaining
        return round(float(per_run), 4) if per_run else None

    # ------------------------------------------------------------------ dispatcher
    def tick(self) -> list[str]:
        """Khởi chạy các run trong hàng đợi khi đủ điều kiện. Trả danh sách run vừa khởi chạy."""
        started: list[str] = []
        if self.shutting_down or self.estop.get("on"):
            return started
        limit = self.effective_limit()  # M3: điều tiết theo rate limit (FR-RT-16)
        skipped: list[str] = []
        while self.queue and len(self.active) < limit:
            rid = self.queue.popleft()
            run = self.store.get_run(rid)
            if run is None or run["status"] != "queued":
                continue
            agent = self.store.get_agent(run["agent_id"])
            if agent is None or agent.get("archived_at"):
                self._finish_without_start(rid, run["agent_id"], "cancelled", "agent không còn tồn tại")
                continue
            if (run.get("run_opts") or {}).get("eval_overlay"):  # M4-B: run eval trên BẢN SAO home + cấu hình ứng viên (FR-LN-10)
                from .evals.overlay import apply_overlay

                agent = apply_overlay(agent, run["run_opts"]["eval_overlay"])
            agent_limit = int((agent.get("limits") or {}).get("max_concurrent_runs") or 1)
            is_review = run.get("kind") == "review"  # M4-A: review fork không chiếm slot của agent (run mới không bị chặn)
            if agent["status"] == "paused" or (not is_review and self.running_count(agent["id"], exclude_kind="review") >= agent_limit):
                skipped.append(rid)
                continue
            lock_key = self._lock_key(run, agent)
            # H-21: GIỮ khoá repo ĐỒNG BỘ ngay trong tick, trước create_task — task/run khác trong cùng tick (dispatcher)
            # thấy khoá ngay, không spawn rồi chết `workspace_locked` (run waiting_human vẫn giữ khoá theo session)
            if not self.locks.try_acquire(lock_key, run["session_id"], rid):
                skipped.append(rid)
                continue
            ar = ActiveRun(rid, agent["id"], task_id=run.get("task_id"), team_id=run.get("team_id"), kind=run.get("kind") or "adhoc")
            ar.lock_key = lock_key
            self.active[rid] = ar
            ar.task = asyncio.get_event_loop().create_task(self._run(ar, run, agent))
            ar.task.add_done_callback(lambda _t, ar=ar, sid=run["session_id"]: self._release_unstarted(ar, sid))
            started.append(rid)
        # giữ thứ tự FIFO cho run bị bỏ qua
        for rid in reversed(skipped):
            self.queue.appendleft(rid)
        return started

    # ------------------------------------------------------------------ launch
    def _prepare_spec(self, run: dict[str, Any], agent: dict[str, Any]) -> RunSpec:
        hub = self.hub
        cfg = hub.config.data
        claude_cfg = cfg["runtime"]["claude"]
        iso = claude_cfg.get("isolation") or {}
        home = Path(agent["home_path"])
        run_id = run["id"]
        opts = dict(run.get("run_opts") or {})
        if run.get("cwd"):
            cwd, wkind = Path(run["cwd"]), ("worktree" if opts.get("workspace_kind") == "worktree" else "dir")
            if _in_agent_workspaces(cwd, home):  # M7: thư mục cố định trong workspaces/ của agent (hội thoại manager) = scratch
                wkind = "scratch"
        elif agent.get("workspace_default"):
            cwd, wkind = Path(agent["workspace_default"]).expanduser(), "dir"
        else:
            cwd, wkind = home / "workspaces" / run_id, "scratch"
        if wkind in ("dir", "worktree") and cwd.exists():  # R2-5 (SEC): kiểm lại lúc spawn — chống TOCTOU (đổi thành symlink sau khi đặt)
            from .workspace import validate_workspace

            validate_workspace(hub, str(cwd.resolve()), "cwd của run (kiểm lại lúc spawn)")
        if opts.get("eval_overlay"):  # M4-B: seed file của golden task vào workspace scratch của bản sao
            from .evals.overlay import seed_workspace

            seed_workspace(cwd, opts["eval_overlay"])
        rt_dir = home / "runtime" / run_id
        rt_dir.mkdir(parents=True, exist_ok=True)
        # settings.json do hub sinh — kiểm hợp lệ trước khi spawn vì `-p` bỏ qua im lặng file lỗi (C-09)
        settings = claude_cfg.get("settings") or {}
        if not isinstance(settings, dict):
            raise RuntimeError_("runtime.claude.settings phải là mapping JSON")
        guard_cfg = dict(cfg.get("guard") or {})
        guard_on = bool(guard_cfg.get("enabled"))
        # guard.enabled=false chỉ tắt hook phân loại/hỏi duyệt; permissions.deny tĩnh + sandbox OS vẫn áp (tắt riêng: guard.sandbox.enabled)
        from .guard import runtime as gr

        settings = gr.build_settings(
            settings,
            run_id=run_id,
            socket=hub.paths.socket,
            policy_dir=hub.paths.home / "policy",
            guard_cfg=guard_cfg,
            approval_timeout_s=float(hub.config.get("approval.timeout_s")),
            cwd=cwd,  # hardening SEC: sandbox không chặn ghi workspace của run
            sandbox_overrides=hub.guard.run_sandbox_overrides(agent["id"]),  # SEC: ngoại lệ theo agent (chỉ người) + host MCP của agent
            hook=guard_on,
        )
        # hardening H-11 (FR-RT-17): hook QUAN SÁT fail-OPEN (PostToolUse/UserPromptSubmit/Stop) — hàm riêng, không đụng
        # phần guard (fail-closed) của settings; tắt bằng runtime.claude.observe_hooks.enabled=false
        from .observe_hook import add_observe_hooks

        settings, observe_events = add_observe_hooks(
            settings, run_id=run_id, socket_path=str(hub.paths.socket), hub_home=str(hub.paths.home),
            python=str(guard_cfg.get("python") or sys.executable), cfg=claude_cfg.get("observe_hooks"),
        )
        settings_path = rt_dir / "settings.json"
        _write_json_0600(settings_path, settings)
        json.loads(settings_path.read_text(encoding="utf-8"))
        # mcp.json: server của agent (luồng A — đã thay secret) + hub-guard (luồng B)
        servers = self._agent_mcp_servers(agent)
        tp = dict(agent.get("tool_policy") or {})
        rules = self._agent_mcp_tool_rules(agent)
        if rules.get("allowed"):
            tp["allowed_tools"] = list(dict.fromkeys(list(tp.get("allowed_tools") or []) + rules["allowed"]))
        if rules.get("disallowed"):
            tp["disallowed_tools"] = list(dict.fromkeys(list(tp.get("disallowed_tools") or []) + rules["disallowed"]))
        perm_tool = None
        env_extra: dict[str, str] = {}
        allowed_hooks = list(iso.get("allowed_hooks") or []) + observe_events
        allowed_mcp = list(agent.get("mcp_servers") or []) + list(iso.get("allowed_mcp_servers") or [])
        static: list[str] = []
        if guard_on:
            from .guard import runtime as gr

            mcp_ms = int((claude_cfg.get("env_extra") or {}).get("MCP_TOOL_TIMEOUT") or 900000)
            servers[gr.GUARD_SERVER] = gr.guard_mcp_entry(run_id, hub.paths.socket, hub.paths.home, guard_cfg, mcp_ms)
            if guard_cfg.get("permission_prompt_tool", True):
                perm_tool = gr.PERMISSION_TOOL
            allowed_hooks += ["PreToolUse"]
            allowed_mcp += [gr.GUARD_SERVER]
            env_extra = {"CFHARNESS_RUN_ID": run_id}
            # lớp tĩnh: mẫu luôn-phá-huỷ; bỏ ĐÚNG mẫu khớp thao tác đã được người duyệt (vé chưa dùng)
            tickets = hub.approvals.unused_tickets(run.get("session_id")) if run.get("resumed_from_session") else []
            repo_deny = hub.guard.repo_deny_patterns(str(cwd))  # MGR-2: mẫu chặn theo repo (người đặt), áp cho mọi agent
            all_deny = list(dict.fromkeys(list(tp.get("disallowed_tools") or []) + gr.static_disallowed(guard_cfg) + repo_deny))
            keep, removed = gr.strip_for_tickets(all_deny, tickets)
            tp["disallowed_tools"] = keep
            static = keep
            if removed:
                self.hub.publish(make_event("guard.decision", agent["id"], {"tool": "(argv)", "action": "static_rules_lifted_once", "removed": removed, "tickets": [t["id"] for t in tickets]}, run_id=run_id))
        else:
            # guard.enabled=false: không còn kênh xin quyền (hook/permission-prompt-tool) → `claude -p` tự TỪ CHỐI mọi tool cần hỏi
            # (Bash, Edit/Write ngoài cwd…) và agent chỉ còn đọc. Cho phép sẵn tool của agent; vẫn giữ mẫu luôn-phá-huỷ + mẫu chặn
            # theo repo (disallowed) + sandbox OS + permissions.deny tĩnh.
            if tp.get("permission_mode") not in ("plan", "bypassPermissions") and not opts.get("read_only"):
                work = list(tp.get("tools") or []) if tp.get("tools") is not None else list(GUARD_OFF_ALLOWED_TOOLS)
                tp["allowed_tools"] = list(dict.fromkeys(list(tp.get("allowed_tools") or []) + work))
            static = list(dict.fromkeys(list(tp.get("disallowed_tools") or []) + gr.static_disallowed(guard_cfg) + hub.guard.repo_deny_patterns(str(cwd))))
            tp["disallowed_tools"] = static
        # M3 — hub-self MCP (FR-MS-08): tool task_* theo vai trò, ràng với run_id
        # M7: run của điều phối viên (không có task) dùng hub-self vai trò `manager` — chỉ khi agent đúng là manager được chỉ định
        is_mgr = (opts.get("hub_self") == "manager" and getattr(hub, "manager", None) is not None and hub.manager.is_manager(agent["id"])
                  and hub.manager.is_issued(run_id))  # MGR-3: chỉ run do hội thoại/tick tạo
        if opts.get("hub_self") == "manager" and not is_mgr:
            opts["hub_self"] = None
        if (run.get("task_id") or is_mgr) and opts.get("hub_self"):
            from .mcp_server.hub_self import SERVER_NAME, server_entry

            servers[SERVER_NAME] = server_entry(run_id, hub.paths.socket, hub.paths.home, str(opts["hub_self"]), guard_cfg)
            allowed_mcp.append(SERVER_NAME)
            tp["allowed_tools"] = list(dict.fromkeys(list(tp.get("allowed_tools") or []) + [f"mcp__{SERVER_NAME}"]))
            env_extra = {**env_extra, "CFHARNESS_RUN_ID": run_id}
        if is_mgr:  # §9 khung `manager.capabilities.mcp=read_only_trusted`: tool MCP ghi (đã probe) bị chặn tĩnh cho run manager
            mgr_deny = hub.manager.perms.mcp_run_disallowed(agent)
            if mgr_deny:
                tp["disallowed_tools"] = list(dict.fromkeys(list(tp.get("disallowed_tools") or []) + mgr_deny))
                tp["allowed_tools"] = [x for x in tp.get("allowed_tools") or [] if x not in mgr_deny]
        if opts.get("disallowed_tools"):
            tp["disallowed_tools"] = list(dict.fromkeys(list(tp.get("disallowed_tools") or []) + list(opts["disallowed_tools"])))
        # M4-A (FR-RT-10): system prompt = quy ước + SOUL + memory snapshot + chỉ mục skill; run resume/fork dùng lại đúng
        # prompt của session (giữ cache). Run review: chỉ MCP hub-self vai trò learning; run có skill: hub-self knowledge.
        from .learning.runtime import apply_mcp as _learning_mcp, system_prompt_for as _learning_prompt

        system_prompt, k_info = _learning_prompt(hub, run, agent)
        perm_tool, env_extra = _learning_mcp(hub, run, agent, servers=servers, tp=tp, allowed_mcp=allowed_mcp, env_extra=env_extra,
                                             perm_tool=perm_tool, guard_cfg=guard_cfg, info=k_info)
        mcp_path = rt_dir / "mcp.json"
        _write_json_0600(mcp_path, {"mcpServers": servers})
        ar = self.active.get(run_id)
        if ar is not None:
            ar.static_deny = static
        prompt = run["prompt"]
        if is_mgr:  # R2-3: khoá tiêu đề tin thật chỉ đưa vào stdin của run manager, không bao giờ vào DB
            prompt = hub.manager.render_prompt(run_id, prompt)
        return RunSpec(
            run_id=run_id,
            agent_id=agent["id"],
            prompt=prompt,
            cwd=cwd,
            session_id=run["session_id"],
            raw_log_path=hub.paths.runs_dir / f"{run_id}.jsonl",
            stderr_log_path=hub.paths.runs_dir / f"{run_id}.stderr.log",
            resume_session_id=run.get("resumed_from_session"),
            fork=bool(opts.get("fork")),  # M4-A: review fork (FR-LN-03) — --resume <gốc> --fork-session --session-id <mới>
            model=run.get("model") or agent.get("model"),
            effort=agent.get("effort"),
            fallback_model=agent.get("fallback_model"),
            system_prompt=system_prompt,
            tool_policy=tp,
            mcp_config_path=mcp_path,
            settings_path=settings_path,
            max_budget_usd=run.get("max_budget_usd"),
            display_name=f"{agent['id']}:{run_id}",
            workspace_kind=wkind,  # type: ignore[arg-type]
            allowed_hooks=allowed_hooks,
            allowed_mcp_servers=allowed_mcp,
            allowed_plugin_prefixes=list(iso.get("allowed_plugin_prefixes") or []) if iso.get("allowed_plugin_prefixes") is not None else None,
            require_no_auto_memory=bool(iso.get("require_no_auto_memory")),
            permission_prompt_tool=perm_tool,
            env_extra=env_extra,
            json_schema=opts.get("json_schema") or agent.get("result_schema"),  # M3 FR-RT-14
            agents_json=agent.get("subagents") or None,  # M3 FR-RT-13
            worktree=opts.get("worktree") if wkind == "worktree" else None,
            no_session_persistence=bool(opts.get("no_session_persistence")),  # M4-B: run eval
        )

    def _agent_mcp_servers(self, agent: dict[str, Any]) -> dict[str, Any]:
        """Hợp đồng A: `mcp_client.build_agent_mcp_servers(agent)` (đã thay secret). Chưa có → registry.yaml của M0."""
        try:
            from .mcp_client import build_agent_mcp_servers
        except ImportError:  # pragma: no cover
            reg = load_mcp_registry(self.hub.paths)
            return {name: reg[name] for name in agent.get("mcp_servers") or [] if name in reg}
        return dict(build_agent_mcp_servers(agent, home=self.hub.paths.home) or {})

    def _agent_mcp_tool_rules(self, agent: dict[str, Any]) -> dict[str, list[str]]:
        if not agent.get("mcp_servers"):
            return {}
        try:
            from .mcp_client import agent_mcp_tool_rules
        except ImportError:  # pragma: no cover
            return {}
        return agent_mcp_tool_rules(agent, home=self.hub.paths.home) or {}

    def _release_unstarted(self, ar: ActiveRun, session_id: str | None) -> None:
        """Task bị huỷ trước khi `_run` kịp chạy (finally không chạy) → nhả khoá đã đặt chỗ, gỡ khỏi active."""
        if ar.entered:
            return
        try:
            self.locks.release(ar.lock_key, session_id)
        except Exception:  # pragma: no cover - store đã đóng khi tắt
            pass
        self.active.pop(ar.run_id, None)

    def lock_pending(self, key: str | None, session_id: str | None) -> bool:
        """Có run ĐANG XẾP HÀNG (chưa khởi chạy) sẽ lấy khoá `key` cho session khác không — dispatcher không submit thêm
        run ghi cùng repo (H-21: task thứ hai chờ `workspace_locked` mà không có run nào)."""
        if key is None:
            return False
        for rid in self.queue:
            r = self.store.get_run(rid)
            if r is None or r["status"] != "queued" or r.get("session_id") == session_id:
                continue
            ag = self.store.get_agent(r["agent_id"])
            if ag is not None and self._lock_key(r, ag) == key:
                return True
        return False

    def _lock_key(self, run: dict[str, Any], agent: dict[str, Any]) -> str | None:
        if not self.hub.config.get("guard.repo_lock"):
            return None
        if (run.get("run_opts") or {}).get("read_only") or _agent_read_only(agent):
            return None  # FR-RT-12: run chỉ đọc không bị khoá
        if run.get("cwd"):
            if _in_agent_workspaces(Path(run["cwd"]), Path(agent["home_path"])):
                return None  # M7: scratch cố định của agent (hội thoại manager) — không phải repo người
            return self.locks.key_for(run["cwd"], "dir")
        if agent.get("workspace_default"):
            return self.locks.key_for(agent["workspace_default"], "dir")
        return None

    async def _check_cli_version(self, runtime: AgentRuntime, agent_id: str, run_id: str) -> str | None:
        v = await asyncio.to_thread(runtime.version)
        if runtime.name == "claude" and v is None:
            raise RuntimeError_("không chạy được `claude --version` — kiểm `claude` có trong PATH (cfharness doctor)")
        tested = self.hub.config.get("runtime.claude.tested_versions") or []
        if v and v not in tested and v not in self._warned_versions:
            self._warned_versions.add(v)
            self.hub.publish(make_event("cli.version_warning", None, {"version": v, "matrix": tested}))
        return v

    async def _run(self, ar: ActiveRun, run: dict[str, Any], agent: dict[str, Any]) -> None:
        hub = self.hub
        run_id, agent_id = ar.run_id, ar.agent_id
        set_run_context(run_id, agent_id)  # H-20: mọi log trong task này có run_id/agent_id
        rlog = run_logger(log, run_id, agent_id, task_id=ar.task_id)
        spec: RunSpec | None = None
        ar.entered = True
        try:
            ar.lock_key = ar.lock_key or self._lock_key(run, agent)  # đã đặt chỗ trong tick (H-21); cùng session → kế thừa
            if not self.locks.try_acquire(ar.lock_key, run["session_id"], run_id):
                raise HubError(f"thư mục {ar.lock_key} đang bị khoá bởi run khác (workspace_locked)")
            runtime = self.runtime()
            ar.runtime = runtime
            spec = self._prepare_spec(run, agent)
            await self._checkpoint(ar, run, spec)
            cli_version = await self._check_cli_version(runtime, agent_id, run_id)
            handle = await runtime.start(spec)
            ar.handle = handle
            hub.store.update_run(
                run_id,
                {
                    "status": "running",
                    "started_at": now_iso(),
                    "pid": handle.pid,
                    "pid_start_time": handle.started_at,
                    "argv_redacted": handle.argv_redacted,
                    "cwd": str(spec.cwd),
                    "system_prompt_hash": prompt_hash(spec.system_prompt or ""),
                    "cli_version": cli_version or handle.cli_version,
                    "model": spec.model,
                    "raw_log_path": str(spec.raw_log_path) + ".gz",
                    "stderr_log_path": str(spec.stderr_log_path),
                },
            )
            hub.store.upsert_session(spec.session_id, agent_id, run_id, forked_from=spec.resume_session_id if spec.fork else None)
            hub.agents.set_status(agent_id, "working", current_run_id=run_id)
            max_rt = run.get("max_runtime_s") or (agent.get("limits") or {}).get("max_runtime_s")
            if max_rt:
                ar.deadline = asyncio.get_event_loop().time() + float(max_rt)
                ar.timer = asyncio.get_event_loop().call_later(float(max_rt), self._on_timeout, ar)
            async for ev in runtime.events(handle):
                self._on_event(ar, ev)
            if ar.checkpoint is not None:
                await self._diff(ar)  # M3: diff có trước event kết thúc run
            await self._finalize(ar)
        except asyncio.CancelledError:
            if ar.handle is not None and ar.runtime is not None:
                await ar.runtime.stop(ar.handle, grace_s=2)
            self._finalize_error(ar, "interrupted", "daemon dừng khi run đang chạy")
            raise
        except (RuntimeError_, HubError, OSError) as e:
            rlog.warning("run %s lỗi khởi chạy: %s", run_id, e)
            self._finalize_error(ar, "failed", str(e))
        except Exception as e:  # pragma: no cover - lỗi không lường trước
            rlog.exception("run %s lỗi", run_id)
            self._finalize_error(ar, "failed", f"lỗi nội bộ: {e!r}")
        finally:
            if ar.timer:
                ar.timer.cancel()
            if spec is not None:
                shutil.rmtree(Path(agent["home_path"]) / "runtime" / run_id, ignore_errors=True)
            if ar.checkpoint is not None and not ar.diff_done:
                await self._diff(ar)
            self.active.pop(run_id, None)
            final = self.store.get_run(run_id) or {}
            if final.get("status") != "waiting_human":
                self.locks.release(ar.lock_key, run.get("session_id"))
            ar.done.set()
            w = self.waiters.pop(run_id, None)
            if w:
                w.set()
            if final.get("status") == "waiting_human" and not self.shutting_down:
                self.on_approval_resolved(run_id)  # đã có quyết định trong lúc dừng → resume ngay
            self.tick()

    # ------------------------------------------------------------------ M3: heartbeat, checkpoint, diff
    def _heartbeat(self, ar: ActiveRun) -> None:
        """FR-TK-06: heartbeat = hub thấy event mới từ stdout; ghi DB tối đa mỗi `tasks.heartbeat_write_s`."""
        import time as _t

        now = _t.monotonic()
        if now - ar.last_hb < float(self.hub.config.get("tasks.heartbeat_write_s") or 5):
            return
        ar.last_hb = now
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        ttl = float(self.hub.config.get("tasks.lease_ttl_s") or 120)
        hb = now_iso()
        lease = (_dt.now(_tz.utc) + _td(seconds=ttl)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self.store.update_run(ar.run_id, {"last_heartbeat_at": hb, "lease_expires_at": lease})
        if ar.task_id:
            self.store.conn.execute("UPDATE tasks SET last_heartbeat_at=?, lease_expires_at=? WHERE id=? AND current_run_id=?", (hb, lease, ar.task_id, ar.run_id))

    def checkpoint_dir(self, run_id: str) -> Path:
        return self.hub.paths.home / "checkpoints" / run_id

    async def _checkpoint(self, ar: ActiveRun, run: dict[str, Any], spec: RunSpec) -> None:
        """FR-RT-12: checkpoint trước run ghi vào repo thật (workspace dir, không chỉ đọc). Không commit/stash."""
        if spec.workspace_kind != "dir" or not self.hub.config.get("workspace.checkpoint") or ar.lock_key is None:
            return
        from .workspace import take_checkpoint

        wc = self.hub.config.get("workspace")
        try:
            cp = await asyncio.to_thread(take_checkpoint, spec.cwd, self.checkpoint_dir(ar.run_id), max_file_bytes=int(wc["max_file_bytes"]), max_files=int(wc["max_files"]))
        except Exception as e:  # checkpoint lỗi không chặn run, nhưng ghi lại (không revert được)
            run_logger(log, ar.run_id, ar.agent_id).warning("checkpoint run %s lỗi: %r", ar.run_id, e)
            self.store.update_run(ar.run_id, {"checkpoint": {"kind": "error", "error": repr(e)[:300]}, "workspace_key": ar.lock_key})
            return
        ar.checkpoint = cp
        self.store.update_run(ar.run_id, {"checkpoint": {k: v for k, v in cp.items() if k not in ("dirty", "files")} | {"dirty_count": len(cp.get("dirty") or cp.get("files") or {})}, "workspace_key": ar.lock_key})
        import json as _json

        d = self.checkpoint_dir(ar.run_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "checkpoint.json").write_text(_json.dumps(cp, ensure_ascii=False), encoding="utf-8")
        ev = make_event("workspace.checkpoint", ar.agent_id, {"kind": cp["kind"], "root": cp["root"], "head": cp.get("head"), "branch": cp.get("branch"), "dirty": len(cp.get("dirty") or {})}, run_id=ar.run_id)
        ev.task_id, ev.team_id = ar.task_id, ar.team_id
        self.hub.publish(ev)

    async def _diff(self, ar: ActiveRun) -> None:
        from .workspace import compute_diff

        ar.diff_done = True
        try:
            diff = await asyncio.to_thread(compute_diff, ar.checkpoint, self.checkpoint_dir(ar.run_id), max_file_bytes=int(self.hub.config.get("workspace.max_file_bytes")))
        except Exception as e:
            run_logger(log, ar.run_id, ar.agent_id).warning("diff run %s lỗi: %r", ar.run_id, e)
            self.store.update_run(ar.run_id, {"diff": {"kind": "error", "error": repr(e)[:300]}})
            return
        self.store.update_run(ar.run_id, {"diff": diff})
        ev = make_event("workspace.diff", ar.agent_id, {"kind": diff["kind"], "stat": diff.get("stat"), "files": [f"{f['status']}:{f['path']}" for f in diff.get("files", [])[:50]], "revertible": diff.get("revertible"), "commits_made": diff.get("commits_made")}, run_id=ar.run_id)
        ev.task_id, ev.team_id = ar.task_id, ar.team_id
        self.hub.publish(ev)

    def _on_timeout(self, ar: ActiveRun) -> None:
        ar.timed_out = True
        if ar.handle is not None and ar.runtime is not None:
            asyncio.ensure_future(ar.runtime.stop(ar.handle, grace_s=float(self.hub.config.get("runs.stop_grace_s"))))

    def _on_event(self, ar: ActiveRun, ev: HubEvent) -> None:
        # Trạng thái agent do hub tính (nhiều run/agent), không lấy agent.status từ adapter.
        if ev.type == "agent.status":
            return
        self._heartbeat(ar)
        if ar.task_id and ev.task_id is None:
            ev.task_id = ar.task_id
            ev.team_id = ar.team_id
        if ev.type in ("run.completed", "run.failed"):
            ar.result = dict(ev.payload)
            # sự kiện kết thúc chính thức do hub phát trong _finalize (sau khi ghi DB)
            return
        if ev.type == "run.started":
            sid = ev.payload.get("session_id")
            data: dict[str, Any] = {"model": ev.payload.get("model") or None}
            if ev.payload.get("claude_code_version"):
                data["cli_version"] = ev.payload["claude_code_version"]
            self.store.update_run(ar.run_id, {k: v for k, v in data.items() if v})
            if sid and ar.handle and sid != ar.handle.spec.session_id and not ar.handle.spec.resume_session_id:
                self.hub.publish(
                    make_event("error", ar.agent_id, {"code": "session_mismatch", "message": f"session CLI {sid} ≠ session hub cấp"}, run_id=ar.run_id)
                )
        if ev.type == "isolation.violation":
            ar.violations.append(ev.payload)
        self.hub.publish(ev)

    async def _finalize(self, ar: ActiveRun) -> None:
        h = ar.handle
        assert h is not None
        res = ar.result
        if ar.pause_approval_id and not ar.timed_out:
            self._finalize_paused(ar)
            return
        if ar.timed_out:
            outcome = "timed_out"
        elif ar.crashed and res is None:
            outcome = "crashed"
        elif res is not None and h.stop_requested and res["outcome"] == "failed":
            outcome = "cancelled"  # SIGINT/interrupt → result error_during_execution/aborted_tools (spike A-03)
        elif res is not None:
            outcome = res["outcome"]
            raw = getattr(h.adapter, "result", None) or {}
            if outcome == "failed" and (raw.get("api_error_status") in (429, 529) or RATE_LIMIT_PATTERNS.search(f"{res.get('summary') or ''} {res.get('errors') or ''}")):
                outcome = "rate_limited"  # FR-RT-16: không tính lỗi, requeue có backoff
        elif h.stop_requested:
            outcome = "cancelled"
        else:
            outcome = "crashed"  # M3: tiến trình chết/bị giết, không có `result` (FR-TK-05/06)
        if outcome == "crashed" and ar.runtime is not None:
            # dọn tiến trình con mồ côi trong process group của run đã chết (vd Bash `sleep` của agent)
            try:
                await ar.runtime.stop(h, grace_s=0.5)
            except Exception:  # pragma: no cover
                pass
        sig = (self.store.get_run(ar.run_id) or {}).get("task_signal") or {}
        if outcome == "completed" and sig.get("op") == "block":
            outcome = "blocked"  # agent gọi hub-self task_block (FR-TK-03)
        error = None
        if res is None and outcome in ("failed", "crashed"):
            error = f"tiến trình kết thúc (exit={h.exit_code}) không có event result. stderr: {h.stderr_tail[-800:]}"
        elif res is not None and outcome != "completed":
            error = res.get("summary") or "; ".join(str(x) for x in res.get("errors") or []) or res.get("subtype")
        self._detect_auth(ar, (res or {}).get("summary") or "", h.stderr_tail, outcome)
        data: dict[str, Any] = {
            "status": outcome,
            "outcome": outcome,
            "ended_at": now_iso(),
            "exit_code": h.exit_code,
            "error": error,
            "isolation_violations": ar.violations or None,
            "raw_log_path": str(h.raw_log_final) if h.raw_log_final else None,
        }
        if res is not None:
            # total_cost_usd luỹ kế theo session (spike A-03) → chi phí run = chênh lệch so với mốc trước
            c = self._record_cost(h, res)
            res.update(c)
            data["session_cost_total_usd"] = c["session_cost_total_usd"]
            data.update(
                {
                    "cost_usd": res.get("cost_usd"),
                    "cost_basis": res.get("cost_basis"),
                    "usage": res.get("usage"),
                    "model_usage": res.get("model_usage"),
                    "duration_ms": res.get("duration_ms"),
                    "num_turns": res.get("num_turns"),
                    "summary": res.get("summary"),
                    "permission_denials": res.get("permission_denials"),
                    "terminal_reason": res.get("terminal_reason"),
                }
            )
            mu = res.get("model_usage") or {}
            if mu:
                data["model"] = next(iter(mu))
            # M3: output có cấu trúc (FR-RT-14) + thống kê subagent (FR-RT-13)
            raw = getattr(h.adapter, "result", None) or {}
            if raw.get("structured_output") is not None:
                data["result_json"] = raw["structured_output"]
                res["result_json"] = raw["structured_output"]
            if h.spec.json_schema:
                data["schema_valid"] = 1 if raw.get("structured_output") is not None else 0
            if res.get("subagent_stats"):
                data["subagent_stats"] = res["subagent_stats"]
        self.store.update_run(ar.run_id, data)
        payload = {
            **(res or {}),
            "outcome": outcome,
            "error": error,
            "isolation_violations": ar.violations,
            "exit_code": h.exit_code,
        }
        etype = {"completed": "run.completed", "blocked": "run.completed", "cancelled": "run.cancelled"}.get(outcome, "run.failed")
        fin = make_event(etype, ar.agent_id, payload, run_id=ar.run_id, session_id=h.spec.session_id)
        fin.task_id, fin.team_id = ar.task_id, ar.team_id
        self.hub.publish(fin)
        self._set_agent_after(ar.agent_id, ar.run_id, outcome)
        if res is not None:
            self._budget_warn(ar.agent_id, ar.team_id)
        if outcome in ("failed", "crashed"):
            self._check_repeated_failure(ar.agent_id, ar.run_id, error)

    def _record_cost(self, h: RunHandle, res: dict[str, Any]) -> dict[str, Any]:
        total = float(res.get("cost_usd") or 0.0)
        sess = self.store.get_session(h.spec.resume_session_id if h.spec.fork else h.spec.session_id)  # M4-A: fork luỹ kế từ session gốc
        prev = float(sess["cumulative_cost_usd"]) if sess and h.spec.resume_session_id else 0.0
        run_cost = round(max(0.0, total - prev), 8) if total >= prev else total
        self.store.set_session_cost(h.spec.session_id, total)
        return {"cost_usd": run_cost, "session_cost_total_usd": total}

    def _finalize_paused(self, ar: ActiveRun) -> None:
        """Tiến trình dừng vì thao tác phá huỷ chờ người (FR-SG-04): run → waiting_human, nhả slot, giữ khoá thư mục."""
        h = ar.handle
        assert h is not None
        data: dict[str, Any] = {"status": "waiting_human", "outcome": None, "ended_at": now_iso(), "exit_code": h.exit_code, "waiting_approval_id": ar.pause_approval_id, "raw_log_path": str(h.raw_log_final) if h.raw_log_final else None}
        if ar.result is not None:
            data.update(self._record_cost(h, ar.result))
            data.update({"usage": ar.result.get("usage"), "model_usage": ar.result.get("model_usage"), "num_turns": ar.result.get("num_turns"), "duration_ms": ar.result.get("duration_ms"), "permission_denials": ar.result.get("permission_denials")})
        self.store.update_run(ar.run_id, data)
        for r in self.hub.approvals.pending_for_run(ar.run_id):
            if r["status"] == "open":
                self.hub.approvals.to_waiting_human(r["id"])
        self.hub.publish(
            make_event(
                "run.waiting_human",
                ar.agent_id,
                {"outcome": "waiting_human", "approval_id": ar.pause_approval_id, "cost_usd": data.get("cost_usd"), "slot_released": True, "repo_lock_held": ar.lock_key, "approve_cmd": f"cfharness approve {ar.pause_approval_id}"},
                run_id=ar.run_id,
                session_id=h.spec.session_id,
            )
        )
        self.hub.agents.set_status(ar.agent_id, "waiting", current_run_id=ar.run_id, reason=f"waiting_human {ar.pause_approval_id}")

    def _check_repeated_failure(self, agent_id: str, run_id: str, error: str | None) -> None:
        n = int(self.hub.config.get("notify.repeated_failure_threshold") or 0)
        if n <= 0:
            return
        last = self.store.list_runs(agent_id=agent_id, limit=n)
        if len(last) >= n and all(r.get("outcome") == "failed" for r in last):
            self.hub.publish(make_event("run.repeated_failure", agent_id, {"count": n, "error": (error or "")[:300]}, run_id=run_id))

    def _finalize_error(self, ar: ActiveRun, outcome: str, error: str) -> None:
        run = self.store.get_run(ar.run_id)
        if run and (run["status"] in TERMINAL_STATUSES or run["status"] == "waiting_human"):
            return
        if outcome == "interrupted" and (ar.pause_approval_id or self.hub.approvals.pending_for_run(ar.run_id)):
            # daemon dừng khi đang giữ chờ người → run chờ người (không resume tự động, Q-09)
            aid = ar.pause_approval_id or self.hub.approvals.pending_for_run(ar.run_id)[0]["id"]
            for r in self.hub.approvals.pending_for_run(ar.run_id):
                self.hub.approvals.to_waiting_human(r["id"])
            self.store.update_run(ar.run_id, {"status": "waiting_human", "ended_at": now_iso(), "waiting_approval_id": aid, "error": error})
            self.hub.publish(make_event("run.waiting_human", ar.agent_id, {"outcome": "waiting_human", "approval_id": aid, "error": error}, run_id=ar.run_id))
            return
        self.store.update_run(ar.run_id, {"status": outcome, "outcome": outcome, "ended_at": now_iso(), "error": error})
        etype = "run.interrupted" if outcome == "interrupted" else "run.failed"
        self.hub.publish(make_event(etype, ar.agent_id, {"outcome": outcome, "error": error}, run_id=ar.run_id))
        self._set_agent_after(ar.agent_id, ar.run_id, outcome)

    def _finish_without_start(self, run_id: str, agent_id: str, outcome: str, reason: str) -> None:
        self.store.update_run(run_id, {"status": outcome, "outcome": outcome, "ended_at": now_iso(), "error": reason})
        self.hub.publish(make_event("run.cancelled", agent_id, {"outcome": outcome, "error": reason}, run_id=run_id))
        w = self.waiters.pop(run_id, None)
        if w:
            w.set()

    def _set_agent_after(self, agent_id: str, run_id: str, outcome: str) -> None:
        others = [a.run_id for a in self.active.values() if a.agent_id == agent_id and a.run_id != run_id and a.handle is not None]
        status = "working" if others else ("error" if outcome in ("failed", "crashed") else "idle")
        self.hub.agents.set_status(agent_id, status, current_run_id=others[0] if others else None, reason=f"run {outcome}")

    def _detect_auth(self, ar: ActiveRun, text: str, stderr: str, outcome: str) -> None:
        if outcome == "completed":
            return
        blob = f"{text}\n{stderr}"
        m = AUTH_PATTERNS.search(blob)
        if not m:
            return
        info = {"message": blob.strip()[-500:], "run_id": ar.run_id, "at": now_iso()}
        self.store.state_set("auth_block", info)
        self.hub.publish(make_event("auth.blocked", None, info))

    def clear_auth_block(self) -> None:
        self.store.state_set("auth_block", None)

    # ------------------------------------------------------------------ control
    async def stop(self, run_id: str) -> dict[str, Any]:
        run = self.store.find_run(run_id)
        if run is None:
            raise NotFound(f"không tìm thấy run '{run_id}'")
        rid = run["id"]
        if run["status"] == "waiting_human":
            # người huỷ run đang chờ duyệt: huỷ approval, nhả khoá, run → cancelled
            self.hub.approvals.cancel_for_run(rid, "run bị huỷ")
            self.store.update_run(rid, {"status": "cancelled", "outcome": "cancelled", "error": "người dùng huỷ khi đang chờ duyệt"})
            self.locks.release_session(run.get("session_id"))
            self.hub.publish(make_event("run.cancelled", run["agent_id"], {"outcome": "cancelled", "error": "huỷ khi waiting_human"}, run_id=rid))
            self._set_agent_after(run["agent_id"], rid, "cancelled")
            return self.store.get_run(rid)  # type: ignore[return-value]
        if run["status"] == "queued":
            try:
                self.queue.remove(rid)
            except ValueError:
                pass
            self._finish_without_start(rid, run["agent_id"], "cancelled", "người dùng huỷ khi đang chờ")
            return self.store.get_run(rid)  # type: ignore[return-value]
        ar = self.active.get(rid)
        if ar is None or ar.handle is None or ar.runtime is None:
            raise HubError(f"run '{rid}' không đang chạy (status={run['status']})")
        grace = float(self.hub.config.get("runs.stop_grace_s"))
        asyncio.ensure_future(ar.runtime.stop(ar.handle, grace_s=grace))
        return run

    async def set_estop(self, on: bool, *, reason: str | None = None, stop_running: bool = False) -> dict[str, Any]:
        state = {"on": bool(on), "reason": reason, "stop_running": bool(stop_running and on), "at": now_iso()}
        self.store.state_set("estop", state)
        self.hub.publish(make_event("hub.estop", None, state))
        if on and stop_running:
            grace = float(self.hub.config.get("runs.stop_grace_s"))
            for ar in list(self.active.values()):
                if ar.handle is not None and ar.runtime is not None:
                    asyncio.ensure_future(ar.runtime.stop(ar.handle, grace_s=grace))
        if not on:
            self.tick()
        return state

    async def wait(self, run_id: str, timeout: float | None = None) -> dict[str, Any]:
        w = self.waiters.get(run_id)
        if w is not None:
            await asyncio.wait_for(w.wait(), timeout)
        return self.store.get_run(run_id)  # type: ignore[return-value]

    async def shutdown(self) -> None:
        """Daemon dừng: dừng mọi run đang chạy → `interrupted` và ghi danh sách để lần khởi động sau tự resume
        (Q-09); run đang giữ chờ người → `waiting_human`; run còn trong hàng đợi giữ `queued` (khởi động lại sẽ xếp lại)."""
        self.shutting_down = True
        interrupted = [ar.run_id for ar in self.active.values() if ar.handle is not None]
        for ar in list(self.active.values()):
            if ar.task is not None:
                ar.task.cancel()
        for ar in list(self.active.values()):
            if ar.task is not None:
                try:
                    await asyncio.wait_for(ar.task, 15)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    pass
        pending = [rid for rid in interrupted if (self.store.get_run(rid) or {}).get("status") == "interrupted"]
        self.store.state_set("pending_resume", sorted(set(list(self.store.state_get("pending_resume", []) or []) + pending)))
        self.queue.clear()

    def reconcile_on_start(self) -> int:
        """FR-RT-19 — xem `recovery.reconcile`."""
        from .recovery import reconcile

        return reconcile(self.hub)["interrupted"]

    # ------------------------------------------------------------------ M1-B: waiting_human / resume / continue
    def static_deny_for(self, run_id: str) -> list[str]:
        ar = self.active.get(run_id)
        return list(ar.static_deny) if ar else []

    def pause_for_human(self, run_id: str, approval_id: str, *, restart: bool = False) -> None:
        """Dừng tiến trình gọn để chờ người (hoặc để khởi động lại với vé khi luật deny tĩnh chặn thao tác đã duyệt)."""
        ar = self.active.get(run_id)
        if ar is None or ar.handle is None or ar.runtime is None:
            return
        if ar.pause_approval_id is None:
            ar.pause_approval_id = approval_id
        grace = float(self.hub.config.get("runs.stop_grace_s"))
        delay = float(self.hub.config.get("approval.pause_delay_s"))
        loop = asyncio.get_event_loop()
        loop.call_later(delay, lambda: asyncio.ensure_future(ar.runtime.stop(ar.handle, grace_s=grace)))  # type: ignore[union-attr]

    def pause_clock(self, run_id: str) -> None:
        """Dừng đồng hồ max_runtime_s khi đang giữ chờ người (thời gian chờ người không tính, như approval_human_wait)."""
        ar = self.active.get(run_id)
        if ar is None:
            return
        ar.holds += 1
        if ar.holds == 1 and ar.timer is not None:
            ar.timer.cancel()
            ar.timer = None
            ar.clock_paused_at = asyncio.get_event_loop().time()

    def resume_clock(self, run_id: str) -> None:
        ar = self.active.get(run_id)
        if ar is None:
            return
        ar.holds = max(0, ar.holds - 1)
        if ar.holds == 0 and ar.clock_paused_at is not None:
            loop = asyncio.get_event_loop()
            waited = loop.time() - ar.clock_paused_at
            ar.clock_paused_at = None
            run = self.store.get_run(run_id) or {}
            self.store.update_run(run_id, {"human_wait_s": round(float(run.get("human_wait_s") or 0) + waited, 3)})
            if ar.deadline is not None:
                ar.deadline += waited
                ar.timer = loop.call_later(max(0.0, ar.deadline - loop.time()), self._on_timeout, ar)

    def on_approval_resolved(self, run_id: str) -> dict[str, Any] | None:
        """Gọi khi một approval của run được quyết. Nếu run đang `waiting_human` và KHÔNG còn approval chờ → resume
        session với lời nhắn quyết định (allow → vé một lần; deny → agent làm tiếp cách khác)."""
        run = self.store.get_run(run_id)
        if run is None or run["status"] != "waiting_human" or run.get("continued_in_run_id"):
            return None
        aps = self.hub.approvals.all_for_run(run_id)
        if any(a["status"] in ("open", "waiting_human") for a in aps):
            return None
        decided = [a for a in aps if a["status"] in ("allowed", "denied")]
        if not decided:
            # tất cả bị huỷ → kết thúc run
            self.store.update_run(run_id, {"status": "cancelled", "outcome": "cancelled", "error": "approval bị huỷ"})
            self.locks.release_session(run.get("session_id"))
            self.hub.publish(make_event("run.cancelled", run["agent_id"], {"outcome": "cancelled"}, run_id=run_id))
            self._set_agent_after(run["agent_id"], run_id, "cancelled")
            return None
        lines = []
        for a in decided:
            what = f"{a['tool_name']}: `{a['preview']}`"
            if a["status"] == "allowed":
                lines.append(f"- APPROVED (request {a['id']}): {what}. It is approved to run exactly ONCE. Run exactly that same action now.")
            else:
                note = f" Note from the human: {a['note']}." if a.get("note") else ""
                lines.append(f"- DENIED (request {a['id']}): {what}.{note} Do not attempt it again or work around it.")
        prompt = (
            "[cfharness] Your session was paused while waiting for a human decision on a destructive action. The human decided:\n"
            + "\n".join(lines)
            + "\nThen continue the original task from where you stopped and finish with a short summary."
        )
        ticket = next((a["id"] for a in decided if a["status"] == "allowed"), None)
        try:
            new = self.submit(
                run["agent_id"], prompt, cwd=run.get("cwd"), budget_usd=run.get("max_budget_usd"), model=run.get("model"),
                kind=run.get("kind") or "adhoc", resume_session_id=run["session_id"], parent_run_id=run_id,
                resume_reason="approval", resume_ticket_id=ticket, check_paused=False,
            )
        except HubError as e:
            self.hub.publish(make_event("error", run["agent_id"], {"code": "resume_failed", "message": e.message}, run_id=run_id))
            return None
        self.store.update_run(run_id, {"status": "resumed", "outcome": "resumed", "continued_in_run_id": new["id"]})
        for a in decided:
            self.hub.approvals._update(a["id"], {"resume_run_id": new["id"]})
        self.hub.publish(make_event("run.resumed", run["agent_id"], {"from_run_id": run_id, "run_id": new["id"], "reason": "approval", "ticket": ticket}, run_id=run_id, session_id=run["session_id"]))
        return new

    def continue_run(self, ref: str, prompt: str, *, reason: str = "continue", budget_usd: float | None = None) -> dict[str, Any]:
        """FR-TK-14: run mới cùng agent, `--resume <session>` (không fork), cùng cwd. `ref` = id run (tiền tố) hoặc session."""
        run = self.store.find_run(ref)
        if run is None:
            r = self.store.conn.execute("SELECT id FROM runs WHERE session_id=? ORDER BY queued_at DESC LIMIT 1", (ref,)).fetchone()
            run = self.store.get_run(r[0]) if r else None
        if run is None:
            raise NotFound(f"không tìm thấy run/session '{ref}'")
        if not run.get("session_id"):
            raise HubError(f"run {run['id']} không có session")
        busy = self.store.conn.execute(
            "SELECT id, status FROM runs WHERE session_id=? AND status IN ('queued','running','waiting_human') LIMIT 1", (run["session_id"],)
        ).fetchone()
        if busy:
            raise HubError(f"session đang bận: run {busy[0]} ({busy[1]}) — chờ xong hoặc duyệt/huỷ trước")
        return self.submit(
            run["agent_id"], prompt, cwd=run.get("cwd"), budget_usd=budget_usd, model=run.get("model"), kind=run.get("kind") or "adhoc",
            resume_session_id=run["session_id"], parent_run_id=run["id"], resume_reason=reason,
        )


# M7 MGR-3: khoá run_opts của vai trò manager + lý do tiếp nối NỘI BỘ của hub được giữ vai trò
MANAGER_OPT_KEYS = ("manager_conversation", "manager_kind")
MANAGER_INTERNAL_RESUME = ("approval", "daemon_restart")


def _in_agent_workspaces(cwd: Path, home: Path) -> bool:
    try:
        return Path(cwd).resolve().is_relative_to((Path(home) / "workspaces").resolve())
    except OSError:  # pragma: no cover
        return False


def _agent_read_only(agent: dict[str, Any]) -> bool:
    """Agent chỉ đọc: permission_mode=plan, hoặc `tools` không có tool ghi nào (FR-RT-12: không khoá)."""
    tp = agent.get("tool_policy") or {}
    if tp.get("permission_mode") == "plan":
        return True
    tools = tp.get("tools")
    if tools is not None:
        return not any(t.split("(")[0] in ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit") for t in tools)
    return False


def _write_json_0600(path: Path, data: Any) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_terminal_event(ev: HubEvent) -> bool:
    return ev.type in TERMINAL_RUN_EVENTS

