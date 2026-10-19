"""RPC của M4-B: `golden.*`, `eval.*`, `digest.*`, `curator.*`, `regression.*` (hợp đồng M4 mục 5).

`register_rpc(daemon)` gọi TRƯỚC `mcp_server.rpc.register_rpc`. Method thay đổi trạng thái được bọc audit ngay tại đây
(`wrap_with_audit` của M1-A) — KHÔNG cần thêm vào `MUTATING`. Người tạo golden task = `user:<channel>` lấy từ `_audit`
(CLI = `cli`, MCP = `mcp:<client>`), không lấy từ tham số (không giả mạo được).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable

from ..errors import HubError, NotFound

if TYPE_CHECKING:
    from ..daemon import Daemon

MUTATING_B = (
    "golden.add", "golden.from_run", "golden.update", "golden.rm", "golden.restore", "golden.dismiss",
    "eval.run", "eval.check", "eval.post_apply", "eval.batch", "digest.run", "curator.run", "regression.check",
)


def _req(p: dict[str, Any], key: str) -> Any:
    if p.get(key) is None:
        raise HubError(f"thiếu tham số '{key}'", code=-32602)
    return p[key]


class LearningOpsRpc:
    def __init__(self, daemon: "Daemon") -> None:
        self.hub = daemon.hub

    @property
    def golden(self):
        return self.hub.evals.golden

    @property
    def gate(self):
        return self.hub.evals.gate

    @property
    def ops(self):
        return self.hub.learning_ops

    @staticmethod
    def _who(p: dict[str, Any]) -> str:
        return "user:" + str(p.get("_by") or "cli")

    # ------------------------------------------------------------------ golden
    def golden_add(self, p: dict[str, Any]) -> dict[str, Any]:
        data = {k: p.get(k) for k in ("agent_id", "prompt", "context", "checker", "files", "title", "output_schema", "tags") if p.get(k) is not None}
        data["agent_id"] = p.get("agent_id") or p.get("agent")
        return self.golden.add(data, created_by=self._who(p))

    def golden_from_run(self, p: dict[str, Any]) -> dict[str, Any]:
        return self.golden.from_run(_req(p, "run_id"), _req(p, "checker"), title=p.get("title"), prompt=p.get("prompt"), context=p.get("context"), created_by=self._who(p))

    def golden_list(self, p: dict[str, Any]) -> dict[str, Any]:
        rows = self.golden.list(p.get("agent_id") or p.get("agent"), include_inactive=bool(p.get("all")))
        return {"tasks": rows, "counts": self.golden.counts()}

    def golden_show(self, p: dict[str, Any]) -> dict[str, Any]:
        g = self.golden.get(_req(p, "id"))
        g["recent_results"] = [dict(r) for r in self.hub.store.conn.execute(
            "SELECT eval_id, variant, sample, score, passed, outcome, run_id, cost_usd, created_at FROM eval_results WHERE golden_task_id=? ORDER BY created_at DESC LIMIT 20", (g["id"],))]
        return g

    # ------------------------------------------------------------------ eval
    async def eval_run(self, p: dict[str, Any]) -> dict[str, Any]:
        return await self.gate.run_manual(p.get("agent_id") or _req(p, "agent"), n=p.get("n"), task_ids=p.get("tasks"))

    async def eval_check(self, p: dict[str, Any]) -> dict[str, Any]:
        """Chạy gate trước áp dụng cho một proposal (dict) — KHÔNG áp dụng gì (dùng thử/nghiệm thu; A gọi in-process)."""
        prop = p.get("proposal")
        if prop is None and p.get("proposal_id"):
            prop = self._proposal(str(p["proposal_id"]))
        if not isinstance(prop, dict):
            raise HubError("cần 'proposal' (object) hoặc 'proposal_id'")
        return (await self.gate.check(prop)).to_dict()

    async def eval_post_apply(self, p: dict[str, Any]) -> dict[str, Any]:
        entry = p.get("entry")
        if entry is None:
            eid = _req(p, "entry_id")
            from .ports import ledger_list

            rows = [e for e in await ledger_list(self.hub) if str(e.get("id")) == str(eid)]
            if not rows:
                raise NotFound(f"không tìm thấy ledger entry '{eid}'")
            entry = rows[0]
        return (await self.gate.post_apply(entry, auto_rollback=not p.get("no_rollback"))).to_dict()

    async def eval_batch(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        return await self.gate.run_batch_l1(since=p.get("since"))

    def eval_list(self, p: dict[str, Any]) -> list[dict[str, Any]]:
        where, args = [], []
        if p.get("agent_id") or p.get("agent"):
            where.append("agent_id=?")
            args.append(p.get("agent_id") or p.get("agent"))
        if p.get("kind"):
            where.append("kind=?")
            args.append(p["kind"])
        sql = ("SELECT id, kind, agent_id, proposal_id, ledger_entry_ids, status, before_score, after_score, drop_pct, cost_usd, reason, created_at, finished_at "
               "FROM eval_runs" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?")
        args.append(int(p.get("limit") or 30))
        return [dict(r) for r in self.hub.store.conn.execute(sql, args)]

    def eval_show(self, p: dict[str, Any]) -> dict[str, Any]:
        ref = _req(p, "id")
        r = self.hub.store.conn.execute("SELECT * FROM eval_runs WHERE id LIKE ? ORDER BY created_at DESC LIMIT 1", (ref + "%",)).fetchone()
        if r is None:
            raise NotFound(f"không tìm thấy eval '{ref}'")
        d = dict(r)
        for k in ("detail", "verdict", "ledger_entry_ids"):
            if d.get(k):
                d[k] = json.loads(d[k])
        d["results"] = [dict(x) for x in self.hub.store.conn.execute(
            "SELECT golden_task_id, variant, sample, score, passed, outcome, run_id, judge_run_ids, cost_usd, detail FROM eval_results WHERE eval_id=? ORDER BY variant, golden_task_id, sample", (d["id"],))]
        for x in d["results"]:
            x["detail"] = json.loads(x["detail"] or "{}")
        return d

    def _proposal(self, pid: str) -> dict[str, Any]:
        learning = getattr(self.hub, "learning", None)
        for name in ("get_proposal", "proposal"):
            fn = getattr(learning, name, None) or getattr(getattr(learning, "proposals", None), "get", None)
            if callable(fn):
                r = fn(pid)
                return r.model_dump() if hasattr(r, "model_dump") else dict(r)
        raise HubError("không đọc được proposal — lõi học (M4-A) chưa sẵn sàng; truyền 'proposal' trực tiếp")

    # ------------------------------------------------------------------ bảng method
    def methods(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        g = lambda: self.golden  # noqa: E731
        return {
            "golden.add": self.golden_add,
            "golden.from_run": self.golden_from_run,
            "golden.list": self.golden_list,
            "golden.show": self.golden_show,
            "golden.update": lambda p: g().update(_req(p, "id"), p.get("changes") or {}, created_by=self._who(p)),
            "golden.rm": lambda p: g().remove(_req(p, "id"), created_by=self._who(p)),
            "golden.restore": lambda p: g().restore(_req(p, "id"), created_by=self._who(p)),
            "golden.candidates": lambda p: g().candidates(p.get("agent_id") or _req(p, "agent"), limit=int(p.get("limit") or 5)),
            "golden.dismiss": lambda p: g().dismiss(_req(p, "run_id")),
            "eval.run": self.eval_run,
            "eval.check": self.eval_check,
            "eval.post_apply": self.eval_post_apply,
            "eval.batch": self.eval_batch,
            "eval.list": self.eval_list,
            "eval.show": self.eval_show,
            "digest.run": lambda p: self.ops.digest.run(since=p.get("since"), agent_id=p.get("agent_id"), send=not p.get("no_send")),
            "digest.list": lambda p: self.ops.digest.list(int(p.get("limit") or 20)),
            "digest.show": lambda p: self.ops.digest.get(p.get("id")) or {},
            "digest.tui_open": lambda p: self.ops.digest.tui_open(mark=not p.get("peek")),
            "curator.run": lambda p: self.ops.curator.run(dry_run=bool(p.get("dry_run")), agent_id=p.get("agent_id")),
            "curator.status": lambda p: self.ops.curator.status(),
            "regression.list": lambda p: self.ops.regression.list(p.get("agent_id"), p.get("status")),
            "regression.check": lambda p: (self.ops.regression.check_agent(p["agent_id"]) if p.get("agent_id") else self.ops.regression.check_all()),
        }


def _with_by(fn: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
    """Ghi kênh gọi (`_audit.channel`) vào `_by` TRƯỚC khi wrap audit gỡ `_audit` khỏi params."""

    def g(p: dict[str, Any]) -> Any:
        meta = p.get("_audit") if isinstance(p.get("_audit"), dict) else {}
        p = {k: v for k, v in p.items() if k != "_by"}
        p["_by"] = str(meta.get("channel") or "cli")
        return fn(p)

    return g


def register_rpc(daemon: "Daemon") -> LearningOpsRpc:
    from ..mcp_server.rpc import wrap_with_audit

    rpc = LearningOpsRpc(daemon)
    for name, fn in rpc.methods().items():
        if name in MUTATING_B:
            daemon.methods[name] = _with_by(wrap_with_audit(daemon.hub, name, fn))
        else:
            daemon.methods[name] = fn
    daemon.learning_ops_rpc = rpc  # type: ignore[attr-defined]
    return rpc
