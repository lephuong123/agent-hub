"""Eval gate (FR-LN-10) — hợp đồng với M4-A (docs/plan/M4.md mục "Hợp đồng" 1–2).

API (async, chạy trong event loop của daemon; `hub.evals` = `EvalService`, `hub.evals.gate` = `EvalGate`):
- `await gate.check(proposal) -> GateVerdict` — TRƯỚC khi áp dụng L3 (cấu hình agent…). Chạy golden task của agent với
  trạng thái HIỆN TẠI (baseline, có cache) và trạng thái ỨNG VIÊN (bản sao tạm của agent home + proposal), N lần/task,
  so điểm trung bình: tụt > `eval.drop_threshold` → `eval_failed` (+ Signal `failure` cho review). Không có golden task →
  `deferred_no_eval` + `golden.suggested` kèm run ứng viên (Q-23, vào digest + Slack). Không bao giờ đụng home thật.
- `await gate.post_apply(entry, auto_rollback=True) -> GateVerdict` — SAU khi áp dụng L2 (ngay) / L1 (theo lô hằng ngày,
  `run_batch_l1`). Trước = dựng lại trạng thái trước entry trên bản sao (hoặc điểm cache gần nhất); sau = trạng thái thật
  hiện tại (chép sang bản sao). Tụt → B TỰ rollback entry qua `learning.ledger.rollback(id, actor="eval_gate", …)`
  (A KHÔNG rollback lại lần nữa) — CHỈ entry do vòng học/agent tạo; entry của NGƯỜI chỉ cảnh báo (`regression.detected`
  `human_change`, cờ `human_change`). Không golden → `deferred_no_eval` (A gắn cờ `unevaluated`).
- `GateVerdict{status: passed|eval_failed|deferred_no_eval, scores, eval_id, reason, cost_usd, flags, rolled_back}`.

Ngân sách: `eval.budget_usd` cho cả lần gate (mọi run eval + judge), `eval.run_budget_usd` mỗi run; hết ngân sách giữa chừng
→ chấm trên các mẫu đã có, thiếu cặp trước/sau → `deferred_no_eval` (reason `eval_incomplete`).
Run eval là run `kind=eval` của chính agent (vào ngân sách ngày), `--no-session-persistence`, workspace scratch trong bản sao.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import HubError
from ..events import make_event
from ..store import now_iso
from . import overlay as ov
from .checkers import EvalOutput, Grade, grade_deterministic
from .judge import judge

if TYPE_CHECKING:
    from ..hub import Hub

VARIANTS_CURRENT = ("baseline", "after", "manual")


@dataclass
class GateVerdict:
    status: str  # passed | eval_failed | deferred_no_eval
    scores: dict[str, Any] = field(default_factory=dict)
    eval_id: str | None = None
    reason: str = ""
    cost_usd: float = 0.0
    flags: list[str] = field(default_factory=list)
    rolled_back: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    # tiện cho bên gọi dùng như dict
    def get(self, k: str, default: Any = None) -> Any:
        return getattr(self, k, default)

    def __getitem__(self, k: str) -> Any:
        return getattr(self, k)


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


class _Budget:
    def __init__(self, total: float | None) -> None:
        self.total = total
        self.spent = 0.0

    def left(self) -> float | None:
        return None if self.total is None else round(self.total - self.spent, 6)

    def exhausted(self) -> bool:
        return self.total is not None and self.spent >= self.total - 1e-9


class EvalGate:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ settings
    def cfg(self) -> dict[str, Any]:
        return dict(self.hub.config.get("eval") or {})

    def _lock(self, agent_id: str) -> asyncio.Lock:
        if agent_id not in self._locks:
            self._locks[agent_id] = asyncio.Lock()
        return self._locks[agent_id]

    @property
    def golden(self):
        return self.hub.evals.golden

    def _agent_budget(self, agent: dict[str, Any]) -> float | None:
        c = self.cfg()
        learning = agent.get("learning")
        if isinstance(learning, str):
            try:
                learning = json.loads(learning)
            except json.JSONDecodeError:
                learning = None
        v = (learning or {}).get("eval_budget_usd") if isinstance(learning, dict) else None
        return float(v if v is not None else c.get("budget_usd")) if (v is not None or c.get("budget_usd") is not None) else None

    # ------------------------------------------------------------------ public API
    async def check(self, proposal: dict[str, Any]) -> GateVerdict:
        p = dict(proposal.model_dump() if hasattr(proposal, "model_dump") else proposal)
        agent_id = p.get("agent_id") or (p.get("owner_id") if p.get("scope", "agent") == "agent" else None)
        if not agent_id:
            return GateVerdict("deferred_no_eval", reason="scope_not_supported: eval trước áp dụng cho team/global/USER thuộc M5")
        if not self.cfg().get("enabled", True):
            return GateVerdict("deferred_no_eval", reason="eval_disabled (eval.enabled=false)")
        agent = self.hub.agents.get_row(agent_id)
        tasks = self.golden.active_for(agent_id)
        if not tasks:
            self.golden.suggest(agent_id, reason="deferred_no_eval", proposal_id=p.get("id"))
            return GateVerdict("deferred_no_eval", reason="no_golden_tasks: agent chưa có golden task (Q-23) — đã gợi ý run ứng viên")
        async with self._lock(agent_id):
            eval_id = self._open("check", agent_id, proposal_id=p.get("id"), entries=None, detail={"target": p.get("target"), "target_ref": p.get("target_ref"), "op": p.get("op")})
            tmp = self._tmp(eval_id)
            budget = _Budget(self._agent_budget(agent))
            try:
                src = Path(agent["home_path"])
                base_home = ov.make_home(src, tmp / "baseline")
                base_cfg = ov.base_config(agent)
                cand_home = ov.make_home(src, tmp / "candidate")
                cand_cfg = ov.base_config(agent)
                try:
                    ov.materialize(self.hub, cand_home, cand_cfg, p)
                except (HubError, OSError) as e:
                    return self._close(eval_id, GateVerdict("eval_failed", reason=f"không dựng được cấu hình ứng viên: {e}"[:300]), agent_id)
                before = await self._eval_variant(eval_id, agent, tasks, base_home, base_cfg, "baseline", budget)
                after = await self._eval_variant(eval_id, agent, tasks, cand_home, cand_cfg, "candidate", budget)
                v = self._compare(before, after, budget)
                if v.status == "eval_failed":
                    from ..learning_ops.ports import record_signal

                    record_signal(self.hub, agent_id, "failure", {"source": "eval_gate", "proposal_id": p.get("id"), "eval_id": eval_id,
                                                                  "before": v.scores.get("before"), "after": v.scores.get("after"), "target": p.get("target"), "target_ref": p.get("target_ref")})
                return self._close(eval_id, v, agent_id)
            except asyncio.CancelledError:
                self._close(eval_id, GateVerdict("deferred_no_eval", reason="cancelled"), agent_id)
                raise
            except Exception as e:  # lỗi hạ tầng: không chặn vĩnh viễn, hoãn
                return self._close(eval_id, GateVerdict("deferred_no_eval", reason=f"eval_error: {e!r}"[:300]), agent_id)
            finally:
                self._cleanup(tmp)

    async def post_apply(self, entry: dict[str, Any], *, auto_rollback: bool = True) -> GateVerdict:
        return await self.post_apply_batch([entry], auto_rollback=auto_rollback, kind="post_apply")

    async def post_apply_batch(self, entries: list[dict[str, Any]], *, auto_rollback: bool = True, kind: str = "batch") -> GateVerdict:
        from ..learning_ops.ports import ledger_rollback, normalize_entry

        ents = sorted((normalize_entry(e) for e in entries), key=lambda e: e.get("applied_at") or "")
        if not ents:
            return GateVerdict("passed", reason="không có entry")
        agent_id = ents[0].get("agent_id")
        if not agent_id or any(e.get("agent_id") != agent_id for e in ents):
            return GateVerdict("deferred_no_eval", reason="post_apply theo lô chỉ cho entry của một agent")
        ids = [str(e.get("id")) for e in ents]
        if not self.cfg().get("enabled", True):
            return GateVerdict("deferred_no_eval", reason="eval_disabled", flags=["unevaluated"])
        agent = self.hub.agents.get_row(agent_id)
        tasks = self.golden.active_for(agent_id)
        if not tasks:
            self._log_entries(ids, None, "deferred_no_eval")
            self._maybe_suggest(agent_id, ents)
            return GateVerdict("deferred_no_eval", reason="no_golden_tasks", flags=["unevaluated"])
        async with self._lock(agent_id):
            eval_id = self._open(kind, agent_id, proposal_id=ents[-1].get("proposal_id"), entries=ids, detail={"targets": [f"{e.get('target')}:{e.get('target_ref')}" for e in ents]})
            tmp = self._tmp(eval_id)
            budget = _Budget(self._agent_budget(agent))
            try:
                src = Path(agent["home_path"])
                after_home = ov.make_home(src, tmp / "after")
                after_cfg = ov.base_config(agent)
                before_home = ov.make_home(src, tmp / "before")
                before_cfg = ov.base_config(agent)
                rebuilt = all(ov.materialize_before(self.hub, before_home, before_cfg, e) for e in reversed(ents))
                after = await self._eval_variant(eval_id, agent, tasks, after_home, after_cfg, "after", budget)
                if rebuilt:
                    before = await self._eval_variant(eval_id, agent, tasks, before_home, before_cfg, "before", budget)
                else:
                    before = self._cached_before(agent_id, tasks, ents[0].get("applied_at"))
                if before:
                    v = self._compare(before, after, budget)
                else:  # không có mốc trước: dùng ngưỡng tuyệt đối
                    a = _mean([t["mean"] for t in after.values() if t["mean"] is not None])
                    floor = float(self.cfg().get("min_score_abs", 0.5))
                    if a is None:
                        v = GateVerdict("deferred_no_eval", reason="eval_incomplete", flags=["unevaluated"])
                    else:
                        v = GateVerdict("passed" if a >= floor else "eval_failed", {"before": None, "after": round(a, 4), "drop": None, "per_task": self._per_task({}, after)},
                                        reason=f"no_baseline: so với ngưỡng tuyệt đối {floor}")
                v.cost_usd = round(budget.spent, 6)
                if v.status == "eval_failed" and auto_rollback:
                    from ..learning_ops.ports import is_auto_change

                    reason = f"eval tụt {_fmt(v.scores.get('before'))} → {_fmt(v.scores.get('after'))} (eval {eval_id})"
                    human = [e for e in ents if not is_auto_change(e)]
                    if human:  # thay đổi của NGƯỜI: không bao giờ tự rollback — chỉ cảnh báo (digest/Slack)
                        v.flags.append("human_change")
                        v.reason = (v.reason + f"; {len(human)} thay đổi của người không tự rollback").strip("; ")
                        self.hub.publish(make_event("regression.detected", agent_id, {
                            "ledger_entry_id": str(human[-1].get("id")), "rolled_back": False, "human_change": True, "actor": human[-1].get("actor"),
                            "source": "eval_gate", "reason": f"{reason} — thay đổi do người tạo, KHÔNG tự rollback",
                            "hint": f"cfharness learn rollback {human[-1].get('id')}"}))
                    for e in reversed(ents):
                        if e.get("rolled_back") or not is_auto_change(e):
                            continue
                        r = await ledger_rollback(self.hub, str(e["id"]), actor="eval_gate", reason=reason)
                        if r.get("ok", True) and not r.get("error"):
                            v.rolled_back.append(str(e["id"]))
                        elif r.get("human_change"):  # H-04: người đã sửa đích sau entry → Ledger không ghi, đã cảnh báo
                            if "human_change" not in v.flags:
                                v.flags.append("human_change")
                            v.reason = (v.reason + f"; không tự rollback {e['id']}: người đã sửa đích sau entry").strip("; ")
                        else:
                            v.reason = (v.reason + f"; rollback {e['id']} lỗi: {r.get('error')}").strip("; ")
                self._log_entries(ids, eval_id, v.status)
                return self._close(eval_id, v, agent_id)
            except asyncio.CancelledError:
                self._close(eval_id, GateVerdict("deferred_no_eval", reason="cancelled"), agent_id)
                raise
            except Exception as e:
                return self._close(eval_id, GateVerdict("deferred_no_eval", reason=f"eval_error: {e!r}"[:300], flags=["unevaluated"]), agent_id)
            finally:
                self._cleanup(tmp)

    async def run_batch_l1(self, *, since: str | None = None) -> list[dict[str, Any]]:
        """Eval theo lô hằng ngày cho entry L1 chưa chấm (FR-LN-10). Tụt → rollback cả lô của agent đó."""
        from ..learning_ops.ports import ledger_list

        since = since or (datetime.now(timezone.utc) - timedelta(days=int(self.cfg().get("batch_lookback_days", 2)))).isoformat()
        rows = await ledger_list(self.hub, risk_class="L1", since=since)
        done = {r[0] for r in self.hub.store.conn.execute("SELECT entry_id FROM eval_entry_log")}
        groups: dict[str, list[dict[str, Any]]] = {}
        for e in rows:
            if e.get("rolled_back") or e.get("is_rollback") or str(e.get("id")) in done or not e.get("agent_id"):
                continue
            groups.setdefault(e["agent_id"], []).append(e)
        out = []
        for agent_id, ents in groups.items():
            v = await self.post_apply_batch(ents, kind="batch")
            if v.status == "deferred_no_eval" or "unevaluated" in (v.flags or []):
                self._mark_unevaluated([str(e["id"]) for e in ents])  # H-17: L1 không chấm được → ngưỡng hồi quy chặt hơn
            out.append({"agent_id": agent_id, "entries": [e["id"] for e in ents], **v.to_dict()})
        self.hub.store.state_set("eval.last_batch", {"at": now_iso(), "agents": len(out)})
        return out

    def _mark_unevaluated(self, ids: list[str]) -> None:
        """Gắn cờ `unevaluated` vào `ledger_entries.flags` (trường được phép cập nhật) và `regression_watch.unevaluated`
        (watch đã có hoặc tạo sau đều thấy cờ) — FR-LN-10/17."""
        conn = self.hub.store.conn
        for eid in ids:
            r = conn.execute("SELECT flags FROM ledger_entries WHERE id=?", (eid,)).fetchone()
            if r is None:
                continue
            try:
                flags = json.loads(r[0] or "[]")
            except (TypeError, ValueError):
                flags = []
            if "unevaluated" not in flags:
                conn.execute("UPDATE ledger_entries SET flags=? WHERE id=?", (json.dumps([*flags, "unevaluated"]), eid))
            conn.execute("UPDATE regression_watch SET unevaluated=1 WHERE entry_id=?", (eid,))

    async def run_manual(self, agent_id: str, *, n: int | None = None, task_ids: list[str] | None = None) -> dict[str, Any]:
        """`cfharness eval run <agent>`: chấm trạng thái hiện tại (không so sánh, không áp dụng gì)."""
        agent = self.hub.agents.get_row(agent_id)
        tasks = [t for t in self.golden.active_for(agent_id) if not task_ids or t["id"] in task_ids]
        if not tasks:
            raise HubError(f"agent '{agent_id}' chưa có golden task — `cfharness golden add {agent_id} …`")
        async with self._lock(agent_id):
            eval_id = self._open("manual", agent_id, proposal_id=None, entries=None, detail={"tasks": [t["id"] for t in tasks]})
            tmp = self._tmp(eval_id)
            budget = _Budget(self._agent_budget(agent))
            try:
                home = ov.make_home(Path(agent["home_path"]), tmp / "current")
                res = await self._eval_variant(eval_id, agent, tasks, home, ov.base_config(agent), "manual", budget, n=n, use_cache=False)
                m = _mean([t["mean"] for t in res.values() if t["mean"] is not None])
                v = GateVerdict("passed", {"after": None if m is None else round(m, 4), "per_task": self._per_task({}, res)}, cost_usd=round(budget.spent, 6), reason="manual")
                self._close(eval_id, v, agent_id)
                return {"eval_id": eval_id, "score": v.scores["after"], "per_task": v.scores["per_task"], "cost_usd": v.cost_usd}
            finally:
                self._cleanup(tmp)

    # ------------------------------------------------------------------ core
    async def _eval_variant(self, eval_id: str, agent: dict[str, Any], tasks: list[dict[str, Any]], home: Path, cfg: dict[str, Any],
                            variant: str, budget: _Budget, *, n: int | None = None, use_cache: bool = True) -> dict[str, dict[str, Any]]:
        c = self.cfg()
        n = int(n or c.get("samples", 2))
        fp = ov.fingerprint(home, cfg, hub=self.hub, agent_id=agent["id"])  # gồm prompt thật + phiên bản tri thức team/global/USER
        cache_h = float(c.get("baseline_cache_h", 24))
        out: dict[str, dict[str, Any]] = {}
        for t in tasks:
            scores: list[float] = []
            completed = 0
            cached = self._cached(agent["id"], t, fp, cache_h) if (use_cache and variant in ("baseline", "after", "before")) else []
            for r in cached[:n]:
                scores.append(float(r["score"]))
                completed += 1 if r.get("outcome") == "completed" else 0
            out[t["id"]] = {"scores": scores, "cached": len(scores), "completed": completed, "fingerprint": fp}
        for i in range(n):
            for t in tasks:
                slot = out[t["id"]]
                if len(slot["scores"]) > i:
                    continue
                if budget.exhausted():
                    slot["budget_exhausted"] = True
                    continue
                g, outcome = await self._sample(eval_id, agent, t, home, cfg, variant, i, budget, fp)
                slot["scores"].append(g.score)
                slot["completed"] += 1 if outcome == "completed" else 0
        for slot in out.values():
            slot["mean"] = _mean(slot["scores"])
        return out

    async def _sample(self, eval_id: str, agent: dict[str, Any], t: dict[str, Any], home: Path, cfg: dict[str, Any], variant: str,
                      i: int, budget: _Budget, fp: str) -> tuple[Grade, str]:
        c = self.cfg()
        per_run = c.get("run_budget_usd")
        left = budget.left()
        if left is not None:
            per_run = min(float(per_run), left) if per_run else left
        prompt = (t["context"].strip() + "\n\n" + t["prompt"]) if t.get("context") else t["prompt"]
        overlay = {"home": str(home), "config": cfg, "files": t.get("files") or {}}
        opts: dict[str, Any] = {"eval_overlay": overlay, "no_session_persistence": True, "eval_id": eval_id, "golden_task_id": t["id"], "variant": variant}
        if t.get("output_schema"):
            opts["json_schema"] = t["output_schema"]
        run_id = None
        outcome = "failed"
        run_cost = 0.0
        try:
            run = self.hub.runs.submit(agent["id"], prompt, budget_usd=max(per_run, 0.01) if per_run else None, kind="eval", run_opts=opts)
            run_id = run["id"]
            timeout = float(c.get("run_timeout_s", 600))
            try:
                res = await self.hub.runs.wait(run_id, timeout)
            except asyncio.TimeoutError:
                await self.hub.runs.stop(run_id)
                res = await self.hub.runs.wait(run_id, 30)
            outcome = res.get("status") or "failed"
            run_cost = float(res.get("cost_usd") or 0.0)
            budget.spent += run_cost
            out = EvalOutput(text=res.get("summary") or "", structured=res.get("result_json"), outcome=outcome, run_id=run_id,
                             cwd=home / "workspaces" / run_id, prompt=prompt)
            if outcome != "completed":
                g = Grade(0.0, False, {"kind": t["checker"]["kind"], "error": f"run eval {outcome}: {(res.get('error') or '')[:200]}"})
            else:
                g = await self.grade(t["checker"], out, agent_id=agent["id"], budget=budget, eval_id=eval_id, golden_task_id=t["id"])
        except HubError as e:  # Blocked (ESTOP, ngân sách ngày, agent paused…)
            g = Grade(0.0, False, {"error": f"không chạy được run eval: {e}"[:300]})
            outcome = "not_started"
        self._record(eval_id, agent["id"], t, variant, i, fp, g, run_id, outcome, run_cost)
        return g, outcome

    async def grade(self, spec: dict[str, Any], out: EvalOutput, *, agent_id: str, budget: _Budget | None = None,
                    eval_id: str | None = None, golden_task_id: str | None = None) -> Grade:
        kind = spec["kind"]
        if kind == "all":
            parts = [await self.grade(s, out, agent_id=agent_id, budget=budget, eval_id=eval_id, golden_task_id=golden_task_id) for s in spec["checks"]]
            return Grade(sum(p.score for p in parts) / len(parts), all(p.passed for p in parts), {"kind": "all", "parts": [p.detail for p in parts]},
                         sum(p.cost_usd for p in parts), [r for p in parts for r in p.judge_run_ids])
        if kind == "llm_judge":
            jb = (self.cfg().get("judge") or {}).get("budget_usd")
            if budget is not None and budget.left() is not None:
                jb = min(float(jb), budget.left()) if jb else budget.left()  # type: ignore[arg-type]
                if jb is not None and jb <= 0:
                    return Grade(0.0, False, {"kind": "llm_judge", "error": "hết ngân sách eval trước khi chấm"})
            g = await judge(self.hub, agent_id=agent_id, spec=spec, prompt=out.prompt, output_text=out.text, budget_usd=jb, eval_id=eval_id, golden_task_id=golden_task_id)
            if budget is not None:
                budget.spent += g.cost_usd
            return g
        return await asyncio.to_thread(grade_deterministic, spec, out)

    def _compare(self, before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]], budget: _Budget) -> GateVerdict:
        thr = float(self.cfg().get("drop_threshold", 0.10))
        both = [k for k in after if (before.get(k) or {}).get("mean") is not None and after[k].get("mean") is not None]
        per_task = self._per_task(before, after)
        if not both:
            return GateVerdict("deferred_no_eval", {"per_task": per_task}, reason="eval_incomplete: thiếu cặp điểm trước/sau (ngân sách/lỗi)", cost_usd=round(budget.spent, 6))
        if all((before[k].get("completed") or 0) == 0 for k in both) and all(before[k].get("cached", 0) == 0 for k in both):
            return GateVerdict("deferred_no_eval", {"per_task": per_task}, reason="baseline_runs_failed: mọi run baseline lỗi — kiểm hạ tầng", cost_usd=round(budget.spent, 6))
        b = sum(before[k]["mean"] for k in both) / len(both)
        a = sum(after[k]["mean"] for k in both) / len(both)
        drop = (b - a) / b if b > 0 else (0.0 if a >= b else 1.0)
        status = "eval_failed" if drop > thr + 1e-9 else "passed"
        scores = {"before": round(b, 4), "after": round(a, 4), "drop": round(drop, 4), "threshold": thr, "per_task": per_task,
                  "partial": len(both) < len(after) or any(after[k].get("budget_exhausted") or before[k].get("budget_exhausted") for k in both)}
        reason = f"điểm {b:.2f} → {a:.2f} (tụt {drop * 100:.0f}%, ngưỡng {thr * 100:.0f}%)" if drop > 0 else f"điểm {b:.2f} → {a:.2f}"
        return GateVerdict(status, scores, reason=reason, cost_usd=round(budget.spent, 6))

    @staticmethod
    def _per_task(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> dict[str, Any]:
        keys = list(dict.fromkeys(list(before) + list(after)))
        out = {}
        for k in keys:
            b, a = before.get(k) or {}, after.get(k) or {}
            out[k] = {"before": None if b.get("mean") is None else round(b["mean"], 4), "after": None if a.get("mean") is None else round(a["mean"], 4),
                      "n_before": len(b.get("scores") or []), "n_after": len(a.get("scores") or [])}
        return out

    # ------------------------------------------------------------------ DB
    def _open(self, kind: str, agent_id: str, *, proposal_id: Any, entries: list[str] | None, detail: dict[str, Any]) -> str:
        eval_id = "ev_" + uuid.uuid4().hex[:10]
        c = self.cfg()
        self.hub.store.conn.execute(
            "INSERT INTO eval_runs (id, kind, agent_id, proposal_id, ledger_entry_ids, status, samples, created_at, detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (eval_id, kind, agent_id, None if proposal_id is None else str(proposal_id), json.dumps(entries) if entries else None, "running",
             int(c.get("samples", 2)), now_iso(), json.dumps(detail, ensure_ascii=False, default=str)),
        )
        self.hub.publish(make_event("eval.started", agent_id, {"eval_id": eval_id, "kind": kind, "proposal_id": proposal_id, "ledger_entry_ids": entries,
                                                               "golden_tasks": len(self.golden.active_for(agent_id)), "samples": int(c.get("samples", 2))}))
        return eval_id

    def _close(self, eval_id: str, v: GateVerdict, agent_id: str) -> GateVerdict:
        v.eval_id = eval_id
        s = v.scores or {}
        self.hub.store.conn.execute(
            "UPDATE eval_runs SET status=?, before_score=?, after_score=?, drop_pct=?, cost_usd=?, reason=?, finished_at=?, verdict=? WHERE id=?",
            (v.status, s.get("before"), s.get("after"), s.get("drop"), v.cost_usd, v.reason[:500], now_iso(), json.dumps(v.to_dict(), ensure_ascii=False, default=str), eval_id),
        )
        self.hub.publish(make_event("eval.completed", agent_id, {"eval_id": eval_id, "status": v.status, "before": s.get("before"), "after": s.get("after"),
                                                                 "drop": s.get("drop"), "cost_usd": v.cost_usd, "reason": v.reason[:300], "rolled_back": v.rolled_back}))
        return v

    def _record(self, eval_id: str, agent_id: str, t: dict[str, Any], variant: str, sample: int, fp: str, g: Grade, run_id: str | None, outcome: str,
                run_cost: float = 0.0) -> None:
        self.hub.store.conn.execute(
            "INSERT INTO eval_results (id, eval_id, agent_id, golden_task_id, golden_version, variant, fingerprint, sample, score, passed, outcome, run_id, "
            "judge_run_ids, cost_usd, detail, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("er_" + uuid.uuid4().hex[:10], eval_id, agent_id, t["id"], int(t.get("version") or 1), variant, fp, sample, round(g.score, 4), 1 if g.passed else 0, outcome, run_id,
             json.dumps(g.judge_run_ids) if g.judge_run_ids else None, round(g.cost_usd + run_cost, 8), json.dumps(g.detail, ensure_ascii=False, default=str)[:4000], now_iso()),
        )

    def _cached(self, agent_id: str, t: dict[str, Any], fp: str, cache_h: float) -> list[dict[str, Any]]:
        if cache_h <= 0:
            return []
        since = (datetime.now(timezone.utc) - timedelta(hours=cache_h)).isoformat()
        rows = self.hub.store.conn.execute(
            "SELECT score, outcome FROM eval_results WHERE agent_id=? AND golden_task_id=? AND golden_version=? AND fingerprint=? "
            "AND variant IN ('baseline','after','before','manual') AND outcome='completed' AND created_at>=? ORDER BY created_at DESC",
            (agent_id, t["id"], int(t.get("version") or 1), fp, since.replace("+00:00", "Z")),
        ).fetchall()
        return [dict(r) for r in rows]

    def _cached_before(self, agent_id: str, tasks: list[dict[str, Any]], applied_at: str | None) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for t in tasks:
            rows = self.hub.store.conn.execute(
                "SELECT score FROM eval_results WHERE agent_id=? AND golden_task_id=? AND golden_version=? AND variant IN ('baseline','after','manual') "
                "AND outcome='completed' AND created_at<=? ORDER BY created_at DESC LIMIT ?",
                (agent_id, t["id"], int(t.get("version") or 1), applied_at or now_iso(), int(self.cfg().get("samples", 2))),
            ).fetchall()
            if rows:
                xs = [float(r[0]) for r in rows]
                out[t["id"]] = {"scores": xs, "mean": _mean(xs), "cached": len(xs), "completed": len(xs)}
        return out

    def _log_entries(self, ids: list[str], eval_id: str | None, status: str) -> None:
        for i in ids:
            self.hub.store.conn.execute(
                "INSERT INTO eval_entry_log (entry_id, eval_id, status, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(entry_id) DO UPDATE SET eval_id=excluded.eval_id, status=excluded.status, created_at=excluded.created_at",
                (i, eval_id, status, now_iso()),
            )

    def _maybe_suggest(self, agent_id: str, ents: list[dict[str, Any]]) -> None:
        last = self.hub.store.state_get(f"golden.suggested_at:{agent_id}")
        now = datetime.now(timezone.utc)
        if last and now - datetime.fromisoformat(last) < timedelta(hours=float(self.cfg().get("suggest_every_h", 24))):
            return
        self.hub.store.state_set(f"golden.suggested_at:{agent_id}", now.isoformat())
        self.golden.suggest(agent_id, reason="unevaluated", proposal_id=ents[-1].get("proposal_id"))

    def _tmp(self, eval_id: str) -> Path:
        d = self.hub.paths.home / "evals" / "tmp" / eval_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _cleanup(self, tmp: Path) -> None:
        if not self.cfg().get("keep_tmp"):
            shutil.rmtree(tmp, ignore_errors=True)


def _fmt(x: Any) -> str:
    return "-" if x is None else f"{float(x):.2f}"
