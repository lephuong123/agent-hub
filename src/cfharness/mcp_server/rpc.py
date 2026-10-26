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
        "task.create", "task.comment", "task.block", "task.unblock", "task.retry", "task.archive", "task.link",
        "run.revert", "agent.clone", "agent.export", "agent.import", "mcp.team_run",
        # M4-A (learning/rpc.py)
        "learn.signal_add", "learn.refine", "learn.review_cancel", "learn.propose", "learn.retry", "learn.rollback", "learn.policy_set", "knowledge.pin",
        # M5-A
        "learn.canary_check",
        # hardening CLI (mcp_server/surface_rpc.py)
        "agent.archive", "agent.revert", "trash.restore", "trash.purge", "mcp.cancel_task",
        # hồ sơ phê duyệt (guard/rpc.py) — audit trong ApprovalService (tiền tố approval. không bọc lần hai)
        "approval.manager_decide",
    }
)
NOT_AUDITED_PREFIXES = ("approval.",)
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
        out = self.hub.mcp.add(_req(p, "config"))
        if p.get("probe", True):
            out["probe"] = await self.hub.mcp.probe(out["name"], timeout_s=self._probe_timeout(p))
        return out

    async def mcp_update(self, p: dict[str, Any]) -> dict[str, Any]:
        out = self.hub.mcp.update(_req(p, "name"), p.get("patch") or {})
        if p.get("probe"):
            out["probe"] = await self.hub.mcp.probe(out["name"], timeout_s=self._probe_timeout(p))
        return out

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
        self.hub.mcp.require(server)
        row = self.hub.agents.get_row(agent_id)
        cur = list(row.get("mcp_servers") or [])
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
        run = self.hub.runs.submit(
            _req(p, "agent"), _req(p, "prompt"), cwd=p.get("cwd"), budget_usd=p.get("budget_usd"), model=p.get("model")
        )
        out = {
            "run_id": run["id"],
            "agent_id": run["agent_id"],
            "status": run["status"],
            "session_id": run.get("session_id"),
            "epoch": run.get("epoch"),
            "after_seq": run.get("seq_before"),
            "idempotent_replay": False,
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
        return self.hub.audit.query(
            kind=p.get("kind"), agent_id=p.get("agent_id"), run_id=p.get("run_id"), channel=p.get("channel"), limit=int(p.get("limit") or 100)
        )

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
            "feedback.record": self.feedback_record,
            "feedback.list": self.feedback_list,
            **_oauth_methods(h),
        }


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


def wrap_with_audit(hub: "Hub", method: str, fn: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
    async def wrapped(p: dict[str, Any]) -> Any:
        meta = p.pop("_audit", None) if isinstance(p, dict) else None
        meta = meta if isinstance(meta, dict) else {}
        res = fn(p)
        if asyncio.iscoroutine(res):
            res = await res
        try:
            params = {k: v for k, v in p.items() if k not in ("value", "soul")}
            if method.startswith("mcp.oauth_"):  # code/state/iss của callback OAuth là thông tin đăng nhập một lần — không vào audit
                params = {k: v for k, v in params.items() if k in ("name", "redirect_uri", "error")}
            if "soul" in p:
                params["soul"] = "(updated)" if p.get("soul") else None
            if "prompt" in params:
                params["prompt"] = _trunc(params["prompt"], 500)
            if isinstance(params.get("images"), list):  # manager.chat: không ghi dữ liệu ảnh (base64) vào audit append-only
                params["images"] = [{"name": str((i or {}).get("name") or "")[:80], "b64_chars": len(str((i or {}).get("data_b64") or ""))}
                                    for i in params["images"][:10] if isinstance(i, dict)]
            hub.audit.record(
                method,
                p.get("agent") or p.get("agent_id") or (_target_of(p) if method.startswith("agent.") or method == "mcp.agent_create" else None),
                p.get("run_id") or (p.get("id") if method.startswith("run.") else None) or (res.get("run_id") if isinstance(res, dict) else None),
                {"params": params, "result": _result_brief(res)},
                actor=str(meta.get("actor") or "user"),
                channel=str(meta.get("channel") or "cli"),
                target=_target_of(p),
                summary=method,
            )
        except Exception:  # pragma: no cover — audit lỗi không được che kết quả thao tác đã xong
            log.exception("không ghi được audit cho %s", method)
        return res

    return wrapped


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
