"""Method JSON-RPC của luồng A trên daemon: `mcp.*`, `secret.*`, `audit.*`, `feedback.*`.

`register_rpc(daemon)` được gọi ở CUỐI `Daemon.__init__`:
1. thêm các method trên vào bảng method;
2. bọc các method THAY ĐỔI trạng thái bằng audit (FR-OB-08): sau khi thành công ghi `audit_log`
   với `channel` = `_audit.channel` trong params (MCP server gửi `mcp:<tên client>`), mặc định `cli`.
   `approval.*` KHÔNG bọc — luồng B tự ghi audit (hợp đồng mục 2); MCP server truyền `channel`/`actor`
   trong params của `approval.approve|deny` để B ghi đúng kênh.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import TYPE_CHECKING, Any, Callable

from ..bus import LEVEL_RANK
from ..errors import HubError, NotFound
from ..events import HubEvent, make_event
from ..mcp_client.oauth import rpc_methods as _oauth_methods
from ..redact import redact

if TYPE_CHECKING:
    from ..daemon import Daemon
    from ..hub import Hub

log = logging.getLogger("cfharness.mcp_server")

TERMINAL = ("completed", "failed", "cancelled", "budget_exceeded", "timed_out", "interrupted",
            "crashed", "rate_limited", "blocked", "protocol_violation", "resumed")  # M3: outcome mới
# method thay đổi trạng thái → audit (FR-OB-08). approval.* do luồng B tự audit.
MUTATING = frozenset(
    {
        "agent.create", "agent.update", "agent.delete", "agent.restore", "agent.pause", "agent.resume",
        "run.start", "run.stop", "run.resume", "run.continue",
        "estop.set", "config.set", "auth.reset", "hub.shutdown",
        "mcp.add", "mcp.update", "mcp.remove", "mcp.restore", "mcp.import", "mcp.assign", "mcp.run", "mcp.agent_create",
        # OAuth MCP (mcp_client/oauth.py) — audit KHÔNG mang code/state (xem wrap_with_audit)
        "mcp.oauth_start", "mcp.oauth_callback", "mcp.oauth_disconnect", "mcp.oauth_refresh",
        "secret.set", "secret.rm",
        "feedback.record",
        # M2
        "run.steer", "agent.create_from_template", "run.takeover_record",
        # M3
        "team.create", "team.update", "team.member_set", "team.member_rm", "team.archive", "team.restore", "team.estop",
        "task.create", "task.comment", "task.block", "task.unblock", "task.retry", "task.archive", "task.reassign", "task.link",
        "run.revert", "agent.clone", "agent.export", "agent.import", "mcp.team_run",
        # M4-A (learning/rpc.py)
        "learn.signal_add", "learn.refine", "learn.review_cancel", "learn.propose", "learn.retry", "learn.rollback", "learn.policy_set", "knowledge.pin",
        # M5-A
        "learn.canary_check",
        # graph memory (learning/graph_quality.py): bảo trì graph của NGƯỜI — trước đây chỉ nằm trong MUTATING_M4A nên không được bọc audit
        "learn.graph_consolidate", "learn.graph_settle",
        # skill manager của người (learning/skill_rpc.py)
        "knowledge.skill_save", "knowledge.skill_archive", "knowledge.skill_restore", "knowledge.skill_import", "knowledge.skill_promote",
        # hardening CLI (mcp_server/surface_rpc.py)
        "agent.archive", "agent.revert", "trash.restore", "trash.purge", "mcp.cancel_task",
        # dựng lại chỉ mục tìm kiếm hội thoại (session_search.schedule_reindex)
        "session.reindex",
        # hồ sơ phê duyệt (guard/rpc.py) — audit trong ApprovalService (tiền tố approval. không bọc lần hai)
        "approval.manager_decide",
        # L5 (A7): xuất audit ra HUB_HOME/exports/ — ghi file nên audit như thao tác ghi
        "audit.export",
    }
)
NOT_AUDITED_PREFIXES = ("approval.",)
# L3 (plan-logging.md A4): method ĐỌC dữ liệu nhạy cảm — daemon tự ghi `read:<method>` (target + số bản ghi, KHÔNG nội dung), gộp một
# dòng mỗi (principal, method, target) trong `READ_COALESCE_S` (`Daemon._record_read`). `run.get` chỉ khi có `events` (transcript).
SENSITIVE_READS = frozenset(
    {
        "secret.list", "run.get", "mcp.events", "session.search", "memory.search", "memory.search_self", "learn.graph_view",
        "guard.decisions", "audit.list", "hub.doctor", "logs.levels",
        "logs.query", "trace.get",  # L6: đọc log hệ thống / dòng thời gian (logs_rpc.py)
        "audit.activity",  # L5: thẻ "Hoạt động theo người" (principal × ngày)
    }
)
READ_COALESCE_S = 600.0
# mã lỗi audit (cột `error_code`) theo mã JSON-RPC — không bao giờ thông điệp/traceback
ERROR_CODES = {-32001: "user_error", -32004: "not_found", -32009: "conflict", -32003: "blocked", -32000: "system", -32602: "invalid_params"}
# dòng audit THẤT BẠI không bao giờ mang các khoá này (handler lỗi trước khi kịp gỡ thông tin đăng nhập khỏi params)
FAILURE_DROP_KEYS = frozenset({"token", "code", "state", "iss", "password", "secret", "passphrase", "key", "value", "api_key", "bot_token", "app_token"})
AUDITED_ATTR = "_cfharness_audited"  # dấu của wrap_with_audit (test "mọi method ghi đều có audit" dò qua chuỗi __wrapped__)
FEEDBACK_KINDS = {"good": "good", "wrong": "correction", "partial": "correction"}
TEXT_MAX = 2000


def _req(p: dict[str, Any], key: str) -> Any:
    if p.get(key) is None:
        raise HubError(f"thiếu tham số '{key}'", code=-32602)
    return p[key]


def _trunc(v: Any, n: int = TEXT_MAX) -> Any:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + f"… (+{len(v) - n} ký tự)"
    if isinstance(v, dict):
        return {k: _trunc(x, n) for k, x in v.items()}
    if isinstance(v, list):
        return [_trunc(x, n) for x in v[:50]]
    return v


def compact_event(ev: HubEvent | dict[str, Any]) -> dict[str, Any]:
    d = ev.to_wire() if isinstance(ev, HubEvent) else dict(ev)
    p = dict(d.get("payload") or {})
    if "args_preview" in p:
        p.pop("input", None)  # input đầy đủ có thể rất lớn; args_preview đủ cho client
    out = {k: d.get(k) for k in ("seq", "epoch", "ts", "type", "agent_id", "run_id") if d.get(k) is not None}
    if d.get("subagent_id"):
        out["subagent_id"] = d["subagent_id"]
    out["payload"] = _trunc(p)
    return out


class IntegrationRpc:
    def __init__(self, daemon: "Daemon") -> None:
        self.daemon = daemon
        self.hub: "Hub" = daemon.hub
        self._waiters: dict[str, set[asyncio.Event]] = {}
        self.hub.bus.add_listener(self._wake)

    # ------------------------------------------------------------------ helpers
    def _wake(self, ev: HubEvent) -> None:
        if ev.run_id and ev.run_id in self._waiters:
            for w in self._waiters[ev.run_id]:
                w.set()

    async def open_requests(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Approval đang mở — lấy từ RPC `approval.list` của luồng B (nếu đã có)."""
        fn = self.daemon.methods.get("approval.list")
        if fn is None:
            return []
        try:
            params: dict[str, Any] = {"status": "open"}
            if run_id:
                params["run_id"] = run_id
            res = fn(params)
            if asyncio.iscoroutine(res):
                res = await res
        except Exception as e:  # pragma: no cover — không để lỗi bên B làm hỏng hub_events
            log.warning("approval.list lỗi: %r", e)
            return []
        items = res.get("requests") if isinstance(res, dict) else res
        items = [r for r in (items or []) if isinstance(r, dict)]
        if run_id:
            items = [r for r in items if r.get("run_id") in (None, run_id)]
        return [r for r in items if r.get("status") not in ("allowed", "denied", "cancelled", "resolved")]

    def _find_run(self, run_id: str) -> dict[str, Any]:
        run = self.hub.store.find_run(run_id)
        if run is None:
            raise NotFound(f"không tìm thấy run '{run_id}'")
        return run

    @staticmethod
    def run_result(run: dict[str, Any]) -> dict[str, Any]:
        keys = ("status", "outcome", "summary", "cost_usd", "duration_ms", "num_turns", "error", "model", "ended_at", "task_id")
        out = {k: run.get(k) for k in keys}
        out["summary"] = _trunc(out["summary"], 8000)
        if run.get("result_json") is not None:
            out["result_json"] = run["result_json"]
        return out

    # ------------------------------------------------------------------ mcp.*
    async def mcp_add(self, p: dict[str, Any]) -> dict[str, Any]:
        cfg = dict(_req(p, "config"))
        if str(cfg.get("source") or "").lower() == "claude":
            return self._mcp_add_claude(cfg, p.get("account_id"))
        out = self.hub.mcp.add(cfg)
        if p.get("probe", True):
            out["probe"] = await self.hub.mcp.probe(out["name"], timeout_s=self._probe_timeout(p))
        return out

    async def mcp_update(self, p: dict[str, Any]) -> dict[str, Any]:
        out = self.hub.mcp.update(_req(p, "name"), p.get("patch") or {})
        if p.get("probe"):
            out["probe"] = await self.hub.mcp.probe(out["name"], timeout_s=self._probe_timeout(p))
        return out

    def _mcp_add_claude(self, cfg: dict[str, Any], account_id: Any) -> dict[str, Any]:
        """Đăng ký server NGUỒN claude (plan-mcp-claude-native PR2): phải có trong mcpServers user-scope của `.claude.json` của tài
        khoản (mặc định: đang dùng). transport + host lấy từ đó; không bao giờ đọc/lưu header/env/args. Không probe."""
        from ..mcp_client import claude_source as cs

        name = str(cfg.get("name") or "")
        aid, cdir = cs.account_config_dir(self.hub, str(account_id) if account_id else None)
        have = cs.read_servers(cdir)
        if name not in have:
            raise HubError(f"không có MCP '{name}' trong cấu hình claude của tài khoản {cs.account_label(aid)} "
                           f"({cs.claude_json_path(cdir)}) — thêm bằng `claude mcp add -s user …` dưới tài khoản đó trước",
                           data={"available": sorted(have), "account": aid})
        info = have[name]
        if info.get("hub"):
            raise HubError(f"MCP '{name}' trong cấu hình claude trỏ về chính hub — không đăng ký làm MCP của agent (H-18)")
        cfg = {k: v for k, v in cfg.items() if k not in ("command", "args", "env", "headers", "auth", "url", "transport")}
        cfg["transport"] = info["transport"]
        if info.get("host"):
            cfg["url"] = f"https://{info['host']}"
        out = self.hub.mcp.add(cfg)
        out["account"] = aid
        return out

    async def mcp_claude_servers(self, p: dict[str, Any]) -> dict[str, Any]:
        """MCP do claude quản lý trong cấu hình của tài khoản (mặc định: đang dùng) — CHỈ tên/transport/host + đã đăng ký/gán chưa."""
        from ..mcp_client import claude_source as cs

        aid, cdir = cs.account_config_dir(self.hub, str(p["account_id"]) if p.get("account_id") else None)
        have = cs.read_servers(cdir)
        rows = {r["name"]: r for r in self.hub.mcp.list()}
        out = []
        for name in sorted(have):
            r = rows.get(name)
            src = ((r or {}).get("config") or {}).get("source")
            out.append({"name": name, "transport": have[name]["transport"], "host": have[name].get("host"), "hub": have[name]["hub"],
                        "registered": r is not None, "registered_source": None if r is None else ("claude" if src == "claude" else "hub"),
                        "agents": self.hub.mcp.agents_using(name) if r is not None else []})
        path = cs.claude_json_path(cdir)
        return {"account": aid, "config_path": str(path), "exists": path.exists(), "servers": out}

    def _probe_timeout(self, p: dict[str, Any]) -> float:
        return float(p.get("timeout_s") or self.hub.config.get("mcp.probe_timeout_s"))

    async def mcp_probe(self, p: dict[str, Any]) -> dict[str, Any]:
        return await self.hub.mcp.probe(_req(p, "name"), timeout_s=self._probe_timeout(p))

    async def mcp_import(self, p: dict[str, Any]) -> dict[str, Any]:
        res = self.hub.mcp.import_claude(
            _req(p, "path"),
            project=p.get("project"),
            select=p.get("select"),
            dry_run=bool(p.get("dry_run")),
            overwrite=bool(p.get("overwrite")),
        )
        if p.get("probe") and not res["dry_run"]:
            for it in res["items"]:
                if it.get("status") in ("imported", "overwritten"):
                    pr = await self.hub.mcp.probe(it["name"], timeout_s=self._probe_timeout(p))
                    it["probe"] = {k: pr.get(k) for k in ("ok", "error", "elapsed_ms", "warnings")} | {"tools": len(pr.get("tools") or {})}
        return res

    def mcp_assign(self, p: dict[str, Any]) -> dict[str, Any]:
        server, agent_id = _req(p, "server"), _req(p, "agent")
        row = self.hub.agents.get_row(agent_id)
        cur = list(row.get("mcp_servers") or [])
        # gỡ được cả server đã xoá mềm / không còn trong registry (nếu không, agent mắc kẹt với tên mồ côi — thu hẹp, không cần server tồn tại)
        if not (p.get("unassign") and server in cur):
            self.hub.mcp.require(server)
        if p.get("unassign"):
            new = [s for s in cur if s != server]
        else:
            new = cur + ([server] if server not in cur else [])
        if new == cur:
            return self.hub.agents.show(agent_id)
        return self.hub.agents.update(agent_id, {"mcp_servers": new})

    def mcp_agent_config(self, p: dict[str, Any]) -> dict[str, Any]:
        """Cấu hình MCP sẽ dùng khi chạy agent — CHƯA thay secret (không bao giờ trả giá trị secret)."""
        from ..mcp_client import agent_mcp_tool_rules

        row = self.hub.agents.get_row(_req(p, "agent"))
        servers = {}
        missing: list[str] = []
        for name in row.get("mcp_servers") or []:
            r = self.hub.mcp.get_row(name)
            if r is None:
                missing.append(name)
                continue
            cfg = self.hub.mcp.config_of(r)
            servers[name] = cfg.claude_entry()
            missing += [f"{name}:${{{s}}}" for s in self.hub.secrets.missing(servers[name])]
            if cfg.auth is not None:
                st = self.hub.mcp.show(name).get("oauth_status")
                if st != "connected":
                    missing.append(f"{name}:oauth={st}")
        return {"mcpServers": servers, "tool_rules": agent_mcp_tool_rules(row), "problems": missing}

    async def mcp_catalog(self, p: dict[str, Any]) -> dict[str, Any]:
        """Danh mục MCP Registry (CHỈ ĐỌC, không chạy gì): `{search?, limit?, cursor?, offline?}` → `{servers, next_cursor, source, error?}`;
        `{name, check_auth?}` → `{server: mục | None, options: [...]}` (phương án cài; `check_auth` gọi `initialize` không token
        tới remote để gắn nhãn cần OAuth). Mạng chạy trong thread, có timeout + cache + danh sách tuyển chọn khi offline."""
        from ..mcp_client import catalog

        cache_dir = self.hub.paths.home / "cache" / "mcp_catalog"
        offline = bool(p.get("offline"))
        if p.get("name"):
            entry = await asyncio.to_thread(catalog.get, str(p["name"]), cache_dir=cache_dir, offline=offline)
            if entry is None:
                raise NotFound(f"không có '{p['name']}' trong MCP Registry")
            opts = catalog.install_options(entry)
            if p.get("check_auth") and not offline:
                for o in opts:
                    o["needs_oauth"] = await asyncio.to_thread(catalog.check_oauth, o)
            return {"server": entry, "options": opts, "suggested_name": catalog.suggest_name(entry["name"])}
        return await asyncio.to_thread(catalog.search, str(p.get("search") or ""), limit=int(p.get("limit") or 20), cursor=p.get("cursor"),
                                       cache_dir=cache_dir, offline=offline)

    def _idem_get(self, scope: str, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        r = self.hub.store.conn.execute("SELECT result FROM idempotency_keys WHERE key=?", (f"{scope}:{key}",)).fetchone()
        if r is None:
            return None
        out = json.loads(r[0])
        out["idempotent_replay"] = True
        return out

    def _idem_put(self, scope: str, key: str | None, method: str, result: dict[str, Any]) -> None:
        if not key:
            return
        from ..store import now_iso

        self.hub.store.conn.execute(
            "INSERT OR IGNORE INTO idempotency_keys(key, method, result, created_at) VALUES (?,?,?,?)",
            (f"{scope}:{key}", method, json.dumps(result, ensure_ascii=False, default=str), now_iso()),
        )

    def mcp_agent_create(self, p: dict[str, Any]) -> dict[str, Any]:
        """`agent.create` có idempotency_key (FR-MS-07)."""
        key = p.get("idempotency_key")
        prev = self._idem_get("agent", key)
        if prev is not None:
            return prev
        ag = self.hub.agents.create(_req(p, "config"), p.get("soul"))
        out = {"agent_id": ag["id"], "revision": ag["config_revision"], "status": ag["status"], "home_path": ag["home_path"], "idempotent_replay": False}
        self._idem_put("agent", key, "mcp.agent_create", out)
        return out

    async def mcp_run(self, p: dict[str, Any]) -> dict[str, Any]:
        key = p.get("idempotency_key")
        store = self.hub.store
        prev = self._idem_get("run", key)
        if prev is not None:
            run = store.get_run(prev["run_id"])
            if run:
                prev["status"] = run["status"]
            return prev
        from .. import capacity

        busy = capacity.busy_info(self.hub, str(_req(p, "agent")))
        run = self.hub.runs.submit(
            _req(p, "agent"), _req(p, "prompt"), cwd=p.get("cwd"), budget_usd=p.get("budget_usd"), model=p.get("model")
        )
        warnings: list[str] = []
        if busy is not None:  # trước đây chỉ trả `queued`, người gọi không biết run đang xếp sau run khác của agent
            alts = [x["id"] for x in capacity.idle_alternatives(self.hub, busy["agent"])]
            warnings.append(
                f"agent '{busy['agent']}' is busy ({busy['active_runs']}/{busy['max_concurrent_runs']} runs: "
                + ", ".join(x["run_id"] for x in busy["busy_with"]) + "), so this run waits behind them"
                + (f"; idle agents of the same kind: {', '.join(alts)}" if alts else f"; no idle agent of the same kind — clone one (e.g. {capacity.next_clone_id(self.hub, busy['agent'])})")
            )
        out = {
            "run_id": run["id"],
            "agent_id": run["agent_id"],
            "status": run["status"],
            "session_id": run.get("session_id"),
            "epoch": run.get("epoch"),
            "after_seq": run.get("seq_before"),
            "idempotent_replay": False,
            **({"warnings": warnings} if warnings else {}),
        }
        self._idem_put("run", key, "mcp.run", out)
        return out

    async def mcp_run_status(self, p: dict[str, Any]) -> dict[str, Any]:
        run = self._find_run(_req(p, "run_id"))
        keep = ("id", "agent_id", "status", "outcome", "summary", "cost_usd", "duration_ms", "num_turns", "error", "model",
                "queued_at", "started_at", "ended_at", "session_id", "max_budget_usd", "permission_denials", "result_json",
                "task_id", "team_id", "schema_valid", "reverted_at")  # M3
        out = {k: run.get(k) for k in keep if k in run}
        out["run_id"] = out.pop("id")
        out["summary"] = _trunc(out.get("summary"), 8000)
        out["done"] = run["status"] in TERMINAL
        diff = run.get("diff")  # M3 FR-RT-12: diff của run hiển thị qua hub_run_status
        if isinstance(diff, dict):
            out["diff"] = {"kind": diff.get("kind"), "stat": diff.get("stat"), "files": [f"{f['status']}:{f['path']}" for f in (diff.get("files") or [])[:100]],
                           "revertible": diff.get("revertible"), "revert_cmd": f"cfharness run revert {run['id']}"}
        out["open_requests"] = await self.open_requests(run["id"])
        return out

    async def mcp_events(self, p: dict[str, Any]) -> dict[str, Any]:
        """Long-poll event của một run (FR-MS-02/03). Trả ngay khi có event mới, run xong, hoặc hết `wait_s` (≤ 25 s)."""
        run = self._find_run(_req(p, "run_id"))
        rid = run["id"]
        cfg = self.hub.config.get("mcp_server")
        wait_s = max(0.0, min(float(p.get("wait_s", 20) or 0), float(cfg["max_wait_s"])))
        max_events = max(1, min(int(p.get("max_events") or cfg["max_events_per_call"]), 1000))
        max_rank = LEVEL_RANK.get(p.get("level") or "summary", 0)
        after = int(p.get("after_seq") or 0)
        epoch_in = p.get("epoch")
        cur_epoch = self.hub.bus.epoch
        full = after <= 0 or (epoch_in is not None and epoch_in != cur_epoch)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        waiter = asyncio.Event()
        self._waiters.setdefault(rid, set()).add(waiter)
        try:
            while True:
                waiter.clear()
                if full:
                    evs = self.hub.store.query_events(run_id=rid)
                else:
                    evs = self.hub.store.query_events(run_id=rid, epoch=cur_epoch, since_seq=after)
                evs = [e for e in evs if LEVEL_RANK.get(e.level, 0) <= max_rank]
                run = self.hub.store.get_run(rid) or run
                finished = run["status"] in TERMINAL
                remaining = deadline - loop.time()
                if evs or finished or remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(waiter.wait(), remaining)
                except asyncio.TimeoutError:
                    pass
        finally:
            ws = self._waiters.get(rid)
            if ws is not None:
                ws.discard(waiter)
                if not ws:
                    self._waiters.pop(rid, None)
        has_more = len(evs) > max_events
        evs = evs[:max_events]
        cur_evs = [e for e in evs if e.epoch == cur_epoch]
        latest = cur_evs[-1].seq if cur_evs else (0 if full else after)
        done = finished and not has_more
        out: dict[str, Any] = {
            "run_id": rid,
            "events": [compact_event(e) for e in evs],
            "latest_seq": latest,
            "epoch": cur_epoch,
            "truncated": False,
            "has_more": has_more,
            "status": run["status"],
            "done": done,
            "open_requests": await self.open_requests(rid),
            "result": self.run_result(run) if done else None,
        }
        return out

    # ------------------------------------------------------------------ secret.*
    def secret_set(self, p: dict[str, Any]) -> dict[str, Any]:
        name = self.hub.secrets.set(_req(p, "name"), _req(p, "value"))
        return {"name": name, "stored": True}

    def secret_list(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        used: dict[str, list[str]] = {}
        from ..secrets import refs_in

        for r in self.hub.mcp.list():
            for n in refs_in(r["config"]):
                used.setdefault(n, []).append(r["name"])
        items = self.hub.secrets.list()
        names = {i["name"] for i in items}
        for i in items:
            i["used_by"] = used.get(i["name"], [])
        missing = [{"name": n, "missing": True, "used_by": s} for n, s in sorted(used.items()) if n not in names]
        return items + missing

    def secret_rm(self, p: dict[str, Any]) -> dict[str, Any]:
        name = _req(p, "name")
        if not self.hub.secrets.delete(name):
            raise NotFound(f"không có secret '{name}'")
        return {"name": name, "deleted": True}

    # ------------------------------------------------------------------ audit.* / feedback.*
    def audit_record(self, p: dict[str, Any]) -> dict[str, Any]:
        aid = self.hub.audit.record(
            _req(p, "kind"), p.get("agent_id"), p.get("run_id"), p.get("data") or {},
            actor=p.get("actor"), channel=p.get("channel"), target=p.get("target"), summary=p.get("summary"),
        )
        return {"id": aid}

    def audit_list(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        """L5 (A7): lọc `kind` (có `*` cuối = tiền tố) / `kind_prefix`, `agent_id`, `run_id`, `channel`, `principal`, `actor`, `outcome`,
        `req_id`, `since`/`until` (ISO hoặc tương đối `24h`), `q` (chuỗi con trong summary/target — không bao giờ trong data), phân trang
        `before_id`; `limit` ≤ 1000."""
        f = _audit_filters(p)
        return self.hub.audit.query(**f, limit=int(p.get("limit") or 100))

    def audit_activity(self, p: dict[str, Any]) -> dict[str, Any]:
        """L5: số dòng audit theo principal × ngày (giờ máy) trong `days` ngày gần nhất (mặc định 7, ≤ 31) — chỉ đọc, HUMAN_ONLY."""
        return self.hub.audit.activity(days=int(p.get("days") or 7))

    async def audit_export(self, p: dict[str, Any]) -> dict[str, Any]:
        """L5 (A7): ghi `HUB_HOME/exports/audit-<ts>.<jsonl|csv>` (0600, thư mục 0700) — mọi cột kể cả `prev_hash`/`row_hash`, `data` giữ nguyên
        chuỗi đã che lúc ghi. Bộ lọc như `audit.list` (trừ `limit`/`before_id`). HUMAN_ONLY, bọc audit như thao tác ghi."""
        fmt = str(p.get("format") or "jsonl").lower()
        if fmt not in ("jsonl", "csv"):
            raise HubError("format phải là jsonl hoặc csv", code=-32602)
        f = _audit_filters(p)
        f.pop("before_id", None)
        d = self.hub.paths.home / "exports"
        d.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(d, 0o700)
        from datetime import datetime as _dt

        stamp = _dt.now().strftime("%Y%m%dT%H%M%S")
        path = d / f"audit-{stamp}.{fmt}"
        i = 1
        while path.exists():
            i += 1
            path = d / f"audit-{stamp}-{i}.{fmt}"
        res = await asyncio.to_thread(self._export_sync, path, fmt, f)
        return {"path": str(path), "name": path.name, **res, "filters": {k: v for k, v in f.items() if v not in (None, "")}}

    def _export_sync(self, path: Any, fmt: str, f: dict[str, Any]) -> dict[str, Any]:
        """Kết nối SQLite CHỈ ĐỌC riêng (chạy trong luồng phụ, không chặn vòng sự kiện của daemon)."""
        import sqlite3

        from ..audit import export_conn

        conn = sqlite3.connect(f"file:{self.hub.paths.db}?mode=ro", uri=True, timeout=10)
        try:
            return export_conn(conn, path, fmt=fmt, **f)
        finally:
            conn.close()

    async def audit_verify(self, p: dict[str, Any]) -> dict[str, Any]:
        """L4: kiểm chuỗi băm + bản neo (HUMAN_ONLY, chỉ đọc) — luồng phụ, kết nối SQLite chỉ đọc riêng. Gãy → `audit.tamper_detected`.
        L5: `archive=true` còn kiểm các file trong `audit-archive/` (sha256 + chuỗi + nối liền)."""
        since = p.get("since_id")
        if since is not None:
            try:
                since = int(since)
            except (TypeError, ValueError):
                raise HubError("since_id phải là số nguyên", code=-32602) from None
        chain = self.hub.audit_chain  # type: ignore[attr-defined]
        res = await asyncio.to_thread(chain.verify, since_id=since, report=False, archive=bool(p.get("archive")))
        if not res["ok"]:
            chain.report(res)
        return res

    def feedback_record(self, p: dict[str, Any]) -> dict[str, Any]:
        run = self._find_run(_req(p, "run_id"))
        verdict = str(_req(p, "kind")).lower()
        if verdict not in FEEDBACK_KINDS:
            raise HubError(f"kind phải là một trong {sorted(FEEDBACK_KINDS)}")
        kinds = [FEEDBACK_KINDS[verdict]]
        if p.get("corrected_output"):
            kinds.append("edit_output")
        source = p.get("source") or "cli"
        from ..store import now_iso

        evidence = {"verdict": verdict, "note": p.get("note"), "corrected_output": _trunc(p.get("corrected_output"), 20_000)}
        evidence = redact({k: v for k, v in evidence.items() if v is not None})
        ids = []
        for kind in kinds:
            sid = "sg_" + uuid.uuid4().hex[:12]
            self.hub.store.conn.execute(
                "INSERT INTO signals (id, agent_id, run_id, task_id, kind, source, evidence, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (sid, run["agent_id"], run["id"], run.get("task_id"), kind, source, json.dumps(evidence, ensure_ascii=False), now_iso()),
            )
            self.hub.publish(make_event("learning.signal", run["agent_id"], {"signal_id": sid, "kind": kind, "verdict": verdict, "source": source}, run_id=run["id"]))
            ids.append(sid)
        return {"signal_id": ids[0], "signal_ids": ids, "kind": kinds[0], "agent_id": run["agent_id"], "run_id": run["id"]}

    def feedback_list(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        where, args = [], []
        for col in ("agent_id", "run_id", "kind"):
            if p.get(col):
                where.append(f"{col}=?")
                args.append(p[col])
        sql = "SELECT * FROM signals" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?"
        args.append(int(p.get("limit") or 100))
        out = []
        for r in self.hub.store.conn.execute(sql, args):
            d = dict(r)
            d["evidence"] = json.loads(d["evidence"] or "{}")
            out.append(d)
        return out

    # ------------------------------------------------------------------ bảng method
    def methods(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        h = self.hub
        return {
            "mcp.add": self.mcp_add,
            "mcp.update": self.mcp_update,
            "mcp.remove": lambda p: h.mcp.remove(_req(p, "name"), force=bool(p.get("force"))),
            "mcp.restore": lambda p: h.mcp.restore(_req(p, "name")),
            "mcp.list": lambda p: h.mcp.with_oauth(h.mcp.list(include_deleted=bool(p.get("deleted")))),
            "mcp.show": lambda p: h.mcp.show(_req(p, "name")),
            "mcp.claude_servers": self.mcp_claude_servers,
            "mcp.probe": self.mcp_probe,
            "mcp.import": self.mcp_import,
            "mcp.assign": self.mcp_assign,
            "mcp.agent_config": self.mcp_agent_config,
            "mcp.catalog": self.mcp_catalog,
            "mcp.run": self.mcp_run,
            "mcp.agent_create": self.mcp_agent_create,
            "mcp.run_status": self.mcp_run_status,
            "mcp.events": self.mcp_events,
            "mcp.open_requests": lambda p: self.open_requests(p.get("run_id")),
            "secret.set": self.secret_set,
            "secret.list": self.secret_list,
            "secret.rm": self.secret_rm,
            "audit.record": self.audit_record,
            "audit.list": self.audit_list,
            "audit.verify": self.audit_verify,
            "audit.export": self.audit_export,
            "audit.activity": self.audit_activity,
            "feedback.record": self.feedback_record,
            "feedback.list": self.feedback_list,
            **_oauth_methods(h),
        }


AUDIT_FILTERS = ("kind", "kind_prefix", "agent_id", "run_id", "channel", "principal", "actor", "outcome", "req_id", "since", "until", "q", "before_id")


def _audit_filters(p: dict[str, Any]) -> dict[str, Any]:
    """Tham số lọc audit đã kiểm: `since`/`until` nhận ISO hoặc tương đối (`24h`, `30m`, `7d`) → ISO UTC; `outcome` ∈ ok|error|denied;
    `before_id` số nguyên. Chuỗi khác cắt 200 ký tự."""
    from ..audit import OUTCOMES
    from ..log_query import parse_time

    out: dict[str, Any] = {}
    for k in AUDIT_FILTERS:
        v = p.get(k)
        if v in (None, ""):
            continue
        if k in ("since", "until"):
            try:
                out[k] = parse_time(v)
            except ValueError as e:
                raise HubError(str(e), code=-32602) from None
        elif k == "before_id":
            try:
                out[k] = int(v)
            except (TypeError, ValueError):
                raise HubError("before_id phải là số nguyên", code=-32602) from None
        elif k == "outcome":
            if v not in OUTCOMES:
                raise HubError(f"outcome phải là {'|'.join(OUTCOMES)}", code=-32602)
            out[k] = v
        else:
            out[k] = str(v)[:200]
    return out


def _target_of(p: dict[str, Any]) -> str | None:
    for k in ("id", "name", "run_id", "request_id", "agent", "key", "server"):
        if p.get(k) is not None:
            return str(p[k])
    cfg = p.get("config")
    if isinstance(cfg, dict) and (cfg.get("id") or cfg.get("name")):
        return str(cfg.get("id") or cfg.get("name"))
    return None


def _result_brief(res: Any) -> dict[str, Any]:
    if not isinstance(res, dict):
        return {}
    keys = ("id", "run_id", "name", "status", "config_revision", "revision", "signal_id", "on", "deleted_at", "idempotent_replay")
    return {k: res[k] for k in keys if k in res}


def _audit_params(method: str, p: dict[str, Any]) -> dict[str, Any]:
    """Params đã gỡ giá trị bí mật/nặng (như trước L3) — dùng cho cả dòng thành công lẫn thất bại."""
    params = {k: v for k, v in p.items() if k not in ("value", "soul", "passphrase", "key")}  # passphrase: gói hub (hub_transfer); key: khoá cổng cfharness (filehub lane) — phòng hờ
    if method.startswith("mcp.oauth_"):  # code/state/iss của callback OAuth là thông tin đăng nhập một lần — không vào audit
        params = {k: v for k, v in params.items() if k in ("name", "redirect_uri", "error")}
    if "soul" in p:
        params["soul"] = "(updated)" if p.get("soul") else None
    if "prompt" in params:
        params["prompt"] = _trunc(params["prompt"], 500)
    if isinstance(params.get("images"), list):  # manager.chat: không ghi dữ liệu ảnh (base64) vào audit append-only
        params["images"] = [{"name": str((i or {}).get("name") or "")[:80], "b64_chars": len(str((i or {}).get("data_b64") or ""))}
                            for i in params["images"][:10] if isinstance(i, dict)]
    return params


def error_outcome(exc: BaseException) -> tuple[str, str]:
    """(outcome, error_code) của một lỗi: `Blocked` (quyền/ESTOP/ngân sách) → denied; `HubError` khác → error + mã; tham số sai →
    invalid_params; lỗi bất ngờ → error/system. Không bao giờ thông điệp hay traceback."""
    code = getattr(exc, "code", None) if isinstance(exc, HubError) else None
    if code == -32003:
        return "denied", "blocked"
    if isinstance(exc, HubError):
        return "error", ERROR_CODES.get(int(code or -32001), str(code))
    if type(exc).__name__ == "_ParamError":
        return "error", "invalid_params"
    return "error", "system"


def wrap_with_audit(hub: "Hub", method: str, fn: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
    """Bọc method GHI: một dòng audit cho MỖI lời gọi tới handler — thành công (`outcome=ok`) lẫn thất bại (`denied`/`error` + `error_code`,
    L3 A3), kèm `duration_ms`. Lỗi gốc luôn được ném lại. `channel`/`actor` từ `_audit` (daemon đã chứng thực/ép kênh), thiếu thì kênh
    daemon xác định cho lượt (`trace.current()['audit_channel']`)."""
    import time as _time

    async def wrapped(p: dict[str, Any]) -> Any:
        meta = p.pop("_audit", None) if isinstance(p, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        t0 = _time.perf_counter()
        res: Any = None
        exc: BaseException | None = None
        try:
            res = fn(p)
            if asyncio.iscoroutine(res):
                res = await res
        except BaseException as e:  # best-effort: ghi audit rồi ném lại nguyên trạng ở dưới
            exc = e
        dur = (_time.perf_counter() - t0) * 1000.0
        if exc is not None and not isinstance(exc, Exception):  # huỷ task / thoát: không phải kết quả thao tác
            raise exc
        try:
            # params SAU handler (handler gỡ thông tin đăng nhập trước khi audit — vd filehub code/state, accounts token); dòng lỗi còn bỏ
            # thêm mọi khoá kiểu thông tin đăng nhập (handler có thể lỗi TRƯỚC khi kịp gỡ)
            src = dict(p) if isinstance(p, dict) else {}
            if exc is not None:
                src = {k: v for k, v in src.items() if k not in FAILURE_DROP_KEYS}
            from ..trace import current as _cur

            ch = meta.get("channel") or (_cur() or {}).get("audit_channel") or "cli"
            outcome, code = ("ok", None) if exc is None else error_outcome(exc)
            data: dict[str, Any] = {"params": _audit_params(method, src)}
            if exc is None:
                data["result"] = _result_brief(res)
            hub.audit.record(
                method,
                src.get("agent") or src.get("agent_id") or (_target_of(src) if method.startswith("agent.") or method == "mcp.agent_create" else None),
                src.get("run_id") or (src.get("id") if method.startswith("run.") else None) or (res.get("run_id") if isinstance(res, dict) else None),
                data,
                actor=str(meta.get("actor") or "user"),
                channel=str(ch),
                target=_target_of(src),
                summary=method,
                outcome=outcome,
                error_code=code,
                duration_ms=dur,
            )
        except Exception:  # pragma: no cover — audit lỗi không được che kết quả thao tác đã xong
            log.exception("không ghi được audit cho %s", method)
        if exc is not None:
            raise exc
        return res

    setattr(wrapped, AUDITED_ATTR, method)
    wrapped.__wrapped__ = fn  # type: ignore[attr-defined]
    return wrapped


def is_audited(fn: Any) -> bool:
    """Handler (hoặc lớp bọc ngoài giữ `__wrapped__`) đã qua `wrap_with_audit`."""
    seen = 0
    while fn is not None and seen < 20:
        if getattr(fn, AUDITED_ATTR, None):
            return True
        fn = getattr(fn, "__wrapped__", None)
        seen += 1
    return False


def register_rpc(daemon: "Daemon") -> IntegrationRpc:
    rpc = IntegrationRpc(daemon)
    daemon.methods.update(rpc.methods())
    # hardening CLI: agent.delete{detach_teams}/archive/revert, trash.*, memory.search*, hook.*, mcp.agent_run|cancel_task
    from .surface_rpc import register as _register_surface

    _register_surface(daemon)
    for name in list(daemon.methods):
        if name in MUTATING and not name.startswith(NOT_AUDITED_PREFIXES):
            daemon.methods[name] = wrap_with_audit(daemon.hub, name, daemon.methods[name])
    daemon.integration = rpc  # type: ignore[attr-defined]
    return rpc
