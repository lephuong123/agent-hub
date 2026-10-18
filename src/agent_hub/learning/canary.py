"""Pipeline L3 tầng team/global/USER: eval trước → canary/shadow → promoted | tự rollback (FR-LN-05 L3, FR-LN-16, Q-22) — M5-A.

Không có bước nào hỏi người (chỉ event `learning.canary` / `learning.promoted` / `learning.rolled_back` + digest).

1. **Eval trước áp dụng** (`gate`): golden task của mọi agent bị ảnh hưởng (team: thành viên; global/USER: mọi agent) có
   golden task; nhiều hơn `learning.tier_eval_sample` (≥ 3) → mẫu gồm agent khởi nguồn. Mỗi agent chạy
   `hub.evals.gate.check(proposal + agent_id)` của M4-B trên bản sao home (ứng viên tầng team/global dựng vào
   `.tier_overlay` — `Pipeline.materialize`). Một agent tụt → `eval_failed`; không agent nào có golden → `deferred_no_eval`.
2. **Canary** (memory/skill tầng team/global): `Ledger.apply` ghi bản sống, rồi agent canary (mặc định agent khởi nguồn)
   dùng bản mới; mọi agent khác bị ghim về bản trước (`knowledge/tiers.py`) → prompt không đổi (giữ cache).
   **Shadow** (USER profile — một hồ sơ cho mọi agent, không canary theo agent được): eval ở bước 1 là shadow (chạy song
   song trên bản sao, không ảnh hưởng run thật), rồi áp dụng cho mọi agent + theo dõi các agent đã eval.
3. **Cửa sổ** `learning.canary_runs` run làm việc (adhoc/task) của agent theo dõi hoặc `learning.canary_hours` giờ, cái nào
   tới trước. Chỉ số = regression monitor của M4-B (`RegressionMonitor.metrics` + `evaluate` thuần) với ngưỡng
   `learning.canary_regression` (mặc định: tỉ lệ correction/failure > 2× mốc 7 ngày và ≥ 2 sự kiện). Kiểm ngay khi run của
   agent theo dõi kết thúc hoặc có Signal mới, và ở vòng nền (hết giờ).
4. Xấu hơn → `Ledger.rollback(entry, actor="canary")` TRƯỚC khi lan (agent khác chưa từng thấy); hết cửa sổ không xấu hơn
   → `promoted` (bỏ ghim — mọi agent dùng bản mới từ session mới). Entry bị rollback từ nơi khác (người, regression monitor,
   eval_gate) → canary kết thúc `rolled_back`.
Một đích (file memory / cây skill) chỉ có MỘT canary đang chạy; proposal khác cùng đích bị hoãn tới khi canary kết thúc.

**Thay đổi của NGƯỜI** (proposal `created_by=user`, ledger `actor=user` — quyết định coordinator, hardening): KHÔNG eval trước,
KHÔNG ghim — áp dụng NGAY cho cả team/mọi agent, mở bản ghi mode **`watch`** (theo dõi mọi agent bị ảnh hưởng). Xấu hơn →
KHÔNG rollback, chỉ `regression.detected{human_change, source=canary}` (digest + Slack) và kết thúc `warned`. Canary không bao
giờ rollback entry `actor=user`; `Ledger.rollback(actor="canary")` cũng tự từ chối (H-04).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..errors import Conflict, HubError, NotFound
from ..events import TERMINAL_RUN_EVENTS, HubEvent, make_event
from ..store import now_iso

if TYPE_CHECKING:
    from ..hub import Hub
    from .service import LearningService

log = logging.getLogger("agent_hub.learning")

TIER_SCOPES = ("team", "global")
WORK_KINDS = ("adhoc", "task")
DONE_STATUSES = ("completed", "failed", "crashed", "protocol_violation", "timed_out", "cancelled", "blocked", "budget_exceeded")


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def is_tier_proposal(prop: dict[str, Any]) -> bool:
    return prop.get("scope") in TIER_SCOPES or prop.get("target") == "user_profile"


class CanaryManager:
    """`hub.learning.canary`."""

    def __init__(self, hub: "Hub", svc: "LearningService") -> None:
        self.hub = hub
        self.svc = svc

    @property
    def conn(self):
        return self.hub.store.conn

    # ------------------------------------------------------------------ settings
    def cfg(self) -> dict[str, Any]:
        g = self.hub.config.get
        reg = dict(g("learning.canary_regression") or {})
        return {"runs": int(g("learning.canary_runs") or 5), "hours": float(g("learning.canary_hours") or 24), "regression": reg,
                "sample": max(3, int(g("learning.tier_eval_sample") or 3)), "user_mode": str(g("learning.user_profile_mode") or "shadow")}

    def mode_for(self, prop: dict[str, Any]) -> str:
        if prop.get("created_by") == "user":
            return "watch"  # thay đổi của người: áp dụng ngay cho mọi agent, chỉ theo dõi + cảnh báo
        return "shadow" if prop.get("target") == "user_profile" and self.cfg()["user_mode"] == "shadow" else "canary"

    # ------------------------------------------------------------------ agent bị ảnh hưởng
    def affected_agents(self, prop: dict[str, Any]) -> list[str]:
        if prop.get("scope") == "team" and prop.get("target") != "user_profile":
            ids = [m["agent_id"] for m in self.hub.teams.members(prop["team_id"])] if prop.get("team_id") else []
        else:
            ids = [a["id"] for a in self.hub.agents.list()]
        live = {a["id"] for a in self.hub.agents.list()}
        return sorted(a for a in dict.fromkeys(ids) if a in live)

    def canary_agent(self, prop: dict[str, Any], affected: list[str], with_golden: list[str]) -> str | None:
        origin = prop.get("agent_id")
        if origin and origin in affected:
            return origin
        return (with_golden or affected or [None])[0]

    def _golden(self):
        return getattr(getattr(self.hub, "evals", None), "golden", None)

    def _gate(self):
        g = getattr(getattr(self.hub, "evals", None), "gate", None)
        return g if g is not None and hasattr(g, "check") else None

    def sample(self, prop: dict[str, Any]) -> tuple[list[str], list[str]]:
        """(agent bị ảnh hưởng, mẫu agent có golden task để eval)."""
        affected = self.affected_agents(prop)
        golden = self._golden()
        with_golden = [a for a in affected if golden is not None and golden.active_for(a)]
        n = self.cfg()["sample"]
        if len(with_golden) <= n:
            return affected, with_golden
        origin = prop.get("agent_id")
        first = [origin] if origin in with_golden else []
        rest = [a for a in with_golden if a not in first]
        rnd = random.Random(prop.get("id") or "")
        return affected, first + sorted(rnd.sample(rest, n - len(first)))

    # ------------------------------------------------------------------ 1. eval trước áp dụng
    def gate(self, prop: dict[str, Any]) -> Any:
        """Kết quả dict (đồng bộ) hoặc coroutine (chạy golden task bằng `claude`)."""
        gate = self._gate()
        if gate is None:
            return {"status": "deferred_no_eval", "scores": {}, "reason": "eval gate (M4-B) chưa sẵn sàng"}
        affected, chosen = self.sample(prop)
        if not affected:
            return {"status": "deferred_no_eval", "scores": {}, "reason": "không có agent nào bị ảnh hưởng (team rỗng?)"}
        if not chosen:
            golden = self._golden()
            for a in ([prop["agent_id"]] if prop.get("agent_id") in affected else affected[:1]):
                try:
                    golden.suggest(a, reason="deferred_no_eval", proposal_id=prop.get("id"))
                except Exception:  # pragma: no cover
                    log.debug("golden.suggest lỗi", exc_info=True)
            return {"status": "deferred_no_eval", "scores": {}, "reason": f"no_golden_tasks: chưa agent nào trong {len(affected)} agent bị ảnh hưởng có golden task (Q-23)"}
        return self._gate_many(prop, affected, chosen)

    async def _gate_many(self, prop: dict[str, Any], affected: list[str], chosen: list[str]) -> dict[str, Any]:
        from .gate import normalize

        gate = self._gate()
        assert gate is not None

        async def one(a: str) -> dict[str, Any]:
            p = dict(prop, agent_id=a, eval_agent_id=a, origin_agent_id=prop.get("agent_id"))
            r = gate.check(p)
            if asyncio.iscoroutine(r) or hasattr(r, "__await__"):
                r = await r
            return normalize(r)

        res = await asyncio.gather(*(one(a) for a in chosen), return_exceptions=True)
        per: dict[str, Any] = {}
        for a, r in zip(chosen, res):
            per[a] = {"status": "deferred_no_eval", "reason": f"eval_error: {r!r}"[:200]} if isinstance(r, BaseException) else \
                {k: r.get(k) for k in ("status", "scores", "reason", "eval_id")}
        failed = [a for a, v in per.items() if v["status"] == "eval_failed"]
        passed = [a for a, v in per.items() if v["status"] == "passed"]
        befores = [v["scores"].get("before") for v in per.values() if v["status"] in ("passed", "eval_failed") and isinstance(v.get("scores"), dict) and v["scores"].get("before") is not None]
        afters = [v["scores"].get("after") for v in per.values() if v["status"] in ("passed", "eval_failed") and isinstance(v.get("scores"), dict) and v["scores"].get("after") is not None]
        scores = {"before": round(sum(befores) / len(befores), 4) if befores else None, "after": round(sum(afters) / len(afters), 4) if afters else None,
                  "per_agent": per, "agents": chosen, "affected": affected}
        if failed:
            why = "; ".join(f"{a}: {per[a].get('reason')}" for a in failed)
            return {"status": "eval_failed", "scores": scores, "reason": f"eval tụt ở {len(failed)}/{len(chosen)} agent — {why}"[:500]}
        if passed:
            return {"status": "passed", "scores": scores, "reason": f"eval đạt ở {len(passed)}/{len(chosen)} agent ({', '.join(passed)})"}
        return {"status": "deferred_no_eval", "scores": scores, "reason": "; ".join(f"{a}: {v.get('reason')}" for a, v in per.items())[:500]}

    # ------------------------------------------------------------------ 2. mở canary/shadow
    def busy(self, plan: dict[str, Any]) -> dict[str, Any] | None:
        ch = plan.get("change")
        path = str(getattr(ch, "path", "") or "")
        if not path:
            return None
        r = self.conn.execute("SELECT * FROM tier_canaries WHERE status='canary' AND path=? LIMIT 1", (path,)).fetchone()
        return self.hub.store._row("tier_canaries", r) if r else None

    def open(self, prop: dict[str, Any], plan: dict[str, Any], verdict: dict[str, Any]) -> str:
        """Gọi bởi Pipeline NGAY TRƯỚC `Ledger.apply` (cùng lượt đồng bộ — không run nào dựng prompt giữa hai bước):
        ghi canary (ghim agent khác về bản trước). Trả canary id."""
        ch = plan["change"]
        mode = self.mode_for(prop)
        scores = verdict.get("scores") or {}
        affected = scores.get("affected") or self.affected_agents(prop)
        with_golden = scores.get("agents") or []
        canary_agent = self.canary_agent(prop, affected, with_golden)
        watch = [canary_agent] if mode == "canary" else (with_golden or affected)
        before = self.svc.ledger._current_bytes(ch.kind, ch.path, ch.agent_id)
        cfg = self.cfg()
        now = datetime.now(timezone.utc)
        cid = "cn_" + uuid.uuid4().hex[:10]
        self.hub.store._insert("tier_canaries", {
            "id": cid, "proposal_id": prop["id"], "mode": mode, "scope": prop["scope"], "owner_id": plan.get("owner"), "target": prop["target"],
            "target_ref": prop.get("target_ref"), "blob_kind": ch.kind, "path": str(ch.path) if ch.path else None,
            "before_blob": self.svc.ledger.blobs.put(before), "canary_agent_id": canary_agent if mode == "canary" else None,
            "origin_agent_id": prop.get("agent_id"), "watch_agents": [a for a in watch if a], "status": "canary", "created_by": prop.get("created_by"),
            "started_at": _utc(now), "deadline": _utc(now + timedelta(hours=cfg["hours"])), "eval": {k: verdict.get(k) for k in ("status", "reason")} | {"scores": {k: v for k, v in scores.items() if k != "per_agent"}},
        })
        return cid

    def attach_entry(self, cid: str, prop: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
        self.hub.store._update("tier_canaries", "id", cid, {"ledger_entry_id": entry["id"], "after_blob": entry.get("after_blob")})
        c = self.get(cid)
        self.svc.pipeline._update(prop["id"], status="canary")
        cfg = self.cfg()
        self._emit("learning.canary", c, {"phase": "started", "window_runs": cfg["runs"], "window_hours": cfg["hours"],
                                          "detail": (f"canary trên {c['canary_agent_id']} — agent khác giữ bản cũ" if c["mode"] == "canary"
                                                     else f"watch: thay đổi của người, áp dụng ngay cho mọi agent, chỉ theo dõi {', '.join(c['watch_agents'] or [])}" if c["mode"] == "watch"
                                                     else f"shadow: đã eval trên bản sao, áp dụng cho mọi agent, theo dõi {', '.join(c['watch_agents'] or [])}")})
        return c

    def supersede(self, cid: str, reason: str) -> None:
        """Thay đổi của người ghi đè đích đang canary → kết thúc canary (bỏ ghim), không rollback."""
        c = self.get(cid)
        self._finish(c, "superseded", reason)
        self._emit("learning.canary", c, {"phase": "superseded", "detail": reason})

    def abort(self, cid: str, reason: str) -> None:
        self.hub.store._update("tier_canaries", "id", cid, {"status": "failed", "finished_at": now_iso(), "reason": reason[:500]})

    # ------------------------------------------------------------------ 3. theo dõi
    def on_event(self, ev: HubEvent) -> None:
        if not ev.agent_id:
            return
        if (ev.type in TERMINAL_RUN_EVENTS and ev.source == "hub") or ev.type == "learning.signal":
            ids = [c["id"] for c in self.list("canary") if ev.agent_id in (c.get("watch_agents") or [])]
            for cid in ids:
                self.svc._call_soon(self._safe_check, cid)

    def _safe_check(self, cid: str) -> None:
        try:
            self.check(cid)
        except Exception:  # pragma: no cover - không làm chết listener
            log.exception("kiểm canary %s lỗi", cid)

    def tick(self) -> list[dict[str, Any]]:
        return [self.check(c["id"]) for c in self.list("canary")]

    def _runs(self, agents: list[str], *, after: str | None = None, before: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        if not agents:
            return []
        q = ",".join("?" for _ in agents)
        where = [f"agent_id IN ({q})", f"kind IN ({','.join('?' for _ in WORK_KINDS)})", f"status IN ({','.join('?' for _ in DONE_STATUSES)})"]
        args: list[Any] = [*agents, *WORK_KINDS, *DONE_STATUSES]
        if after:
            where.append("queued_at>?")
            args.append(after)
        if before:
            where.append("queued_at<?")
            args.append(before)
        sql = "SELECT id, agent_id, status, cost_usd, queued_at FROM runs WHERE " + " AND ".join(where) + " ORDER BY queued_at" + (" LIMIT ?" if limit else "")
        if limit:
            args.append(limit)
        return [dict(r) for r in self.conn.execute(sql, args)]

    def observe(self, c: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """(cửa sổ, mốc, vi phạm) — chỉ số và luật của regression monitor M4-B."""
        from ..learning_ops.regression import RegressionMonitor, evaluate

        cfg = self.cfg()
        mon = getattr(getattr(self.hub, "learning_ops", None), "regression", None) or RegressionMonitor(self.hub)
        agents = list(c.get("watch_agents") or [])
        start = c["started_at"]
        win = mon.metrics(self._runs(agents, after=start, limit=cfg["runs"]))
        days = int(cfg["regression"].get("baseline_days", 7))
        sd = _parse(start) or datetime.now(timezone.utc)
        base = mon.metrics(self._runs(agents, after=_utc(sd - timedelta(days=days)), before=start))
        return win, base, evaluate(win, base, cfg["regression"])

    def check(self, cid: str) -> dict[str, Any]:
        c = self.get(cid)
        if c["status"] != "canary":
            return {"canary_id": cid, "action": "none", "status": c["status"]}
        e = self.svc.ledger.get(c["ledger_entry_id"]) if c.get("ledger_entry_id") else None
        if e is None:
            return {"canary_id": cid, "action": "none", "status": "canary"}
        if e.get("rolled_back_by"):
            rb = self.svc.ledger.get(e["rolled_back_by"])
            why = f"entry đã bị rollback bởi {rb.get('actor')} ({rb['id']})"
            self._finish(c, "rolled_back", why)
            self._emit("learning.canary", c, {"phase": "rolled_back", "detail": why, "actor": rb.get("actor")})
            return {"canary_id": cid, "action": "rolled_back_elsewhere", "reason": why}
        win, base, viol = self.observe(c)
        self.hub.store._update("tier_canaries", "id", cid, {"observed": win, "baseline": base})
        if viol:
            return self._rollback(c, viol, win, base)
        cfg = self.cfg()
        deadline = _parse(c.get("deadline"))
        if win["runs"] >= cfg["runs"] or (deadline is not None and datetime.now(timezone.utc) >= deadline):
            return self.promote(cid, why=f"{win['runs']} run trong cửa sổ, không xấu hơn mốc" if win["runs"] >= cfg["runs"] else f"hết {cfg['hours']:g} h, {win['runs']} run, không xấu hơn mốc")
        return {"canary_id": cid, "action": "watching", "runs": win["runs"], "window_runs": cfg["runs"]}

    @staticmethod
    def _describe(viol: list[dict[str, Any]]) -> str:
        v = viol[0]
        if v["metric"] == "cost":
            return f"chi phí/run {v['baseline_avg']:.4f} → {v['window_avg']:.4f} USD"
        name = {"correction": "tỉ lệ correction/huỷ", "failure": "tỉ lệ run lỗi"}[v["metric"]]
        return f"{name} {v['baseline_rate'] * 100:.0f}% → {v['window_rate'] * 100:.0f}% ({v['window_events']}/{v['window_runs']} run)"

    # ------------------------------------------------------------------ 4. kết thúc
    def _rollback(self, c: dict[str, Any], viol: list[dict[str, Any]], win: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
        e = self.svc.ledger.get(c["ledger_entry_id"]) if c.get("ledger_entry_id") else {}
        from .ledger import AUTO_CHANGE_ACTORS

        if c["mode"] == "watch" or c.get("created_by") == "user" or (e and str(e.get("actor") or "") not in AUTO_CHANGE_ACTORS):
            return self._warn_human(c, viol, win, e)
        where = f"canary {c['canary_agent_id']}" if c["mode"] == "canary" else "shadow"
        reason = f"hồi quy ở {where}: {self._describe(viol)} — rollback trước khi lan"
        try:
            try:
                r = self.svc.ledger.rollback(c["ledger_entry_id"], actor="canary", reason=reason)
            except Conflict as e:
                if "đã được rollback" in str(e):
                    raise
                r = self.svc.ledger.rollback(c["ledger_entry_id"], actor="canary", reason=reason, chain=True)
        except Conflict as ce:
            if not (getattr(ce, "data", None) or {}).get("human_change"):
                log.warning("rollback canary %s lỗi: %s", c["id"], ce)
                self.hub.store._update("tier_canaries", "id", c["id"], {"reason": f"rollback lỗi: {ce}"[:500]})
                return {"canary_id": c["id"], "action": "rollback_failed", "error": str(ce)}
            # H-04: người đã sửa đích sau entry → Ledger không ghi và đã cảnh báo; kết thúc (bỏ ghim), không thử lại
            why = f"hồi quy ở {where}: {self._describe(viol)} — KHÔNG rollback: {ce}"
            self._finish(c, "warned", why)
            self._emit("learning.canary", c, {"phase": "warned", "detail": why, "violations": viol, "human_change": True})
            return {"canary_id": c["id"], "action": "warned_human", "reason": why, "violations": viol}
        except HubError as e:
            log.warning("rollback canary %s lỗi: %s", c["id"], e)
            self.hub.store._update("tier_canaries", "id", c["id"], {"reason": f"rollback lỗi: {e}"[:500]})
            return {"canary_id": c["id"], "action": "rollback_failed", "error": str(e)}
        self._finish(c, "rolled_back", reason)
        self._emit("learning.canary", c, {"phase": "rolled_back", "detail": reason, "violations": viol, "observed": {k: win.get(k) for k in ("runs", "correction", "failure")},
                                          "rollback_entry_id": r["id"], "actor": "canary"})
        return {"canary_id": c["id"], "action": "rollback", "reason": reason, "rollback_entry_id": r["id"], "violations": viol}

    def _warn_human(self, c: dict[str, Any], viol: list[dict[str, Any]], win: dict[str, Any], e: dict[str, Any]) -> dict[str, Any]:
        """Thay đổi của NGƯỜI xấu hơn mốc → chỉ cảnh báo (digest + Slack), không rollback (quyết định coordinator)."""
        reason = (f"hồi quy sau thay đổi tầng {c['scope']} của người ({', '.join(c.get('watch_agents') or [])}): {self._describe(viol)}"
                  " — thay đổi do người tạo, KHÔNG tự rollback")
        self._finish(c, "warned", reason)
        ev = make_event("regression.detected", c.get("origin_agent_id") or (c.get("watch_agents") or [None])[0], {
            "ledger_entry_id": c.get("ledger_entry_id"), "violations": viol, "rolled_back": False, "human_change": True,
            "actor": (e or {}).get("actor") or c.get("created_by"), "source": "canary", "canary_id": c["id"], "reason": reason,
            "scope": c["scope"], "owner": c.get("owner_id"), "target": c["target"], "target_ref": c.get("target_ref"),
            "hint": f"agent-hub learn rollback {c.get('ledger_entry_id')}"})
        if c["scope"] == "team":
            ev.team_id = c.get("owner_id")
        self.hub.publish(ev)
        self._emit("learning.canary", c, {"phase": "warned", "detail": reason, "violations": viol, "human_change": True,
                                          "observed": {k: win.get(k) for k in ("runs", "correction", "failure")}})
        return {"canary_id": c["id"], "action": "warned_human", "reason": reason, "violations": viol}

    def promote(self, cid: str, why: str = "") -> dict[str, Any]:
        c = self.get(cid)
        if c["status"] != "canary":
            raise Conflict(f"canary {cid} đang ở trạng thái {c['status']}")
        self._finish(c, "promoted", why)
        if c.get("proposal_id"):
            self.svc.pipeline._update(c["proposal_id"], status="promoted")
        try:
            e = self.svc.ledger.get(c["ledger_entry_id"])
            flags = list(dict.fromkeys(list(e.get("flags") or []) + ["promoted"]))
            import json

            self.conn.execute("UPDATE ledger_entries SET flags=? WHERE id=?", (json.dumps(flags), e["id"]))
        except (NotFound, Exception):  # pragma: no cover
            pass
        affected = self.affected_agents({"scope": c["scope"], "team_id": c["owner_id"] if c["scope"] == "team" else None, "target": c["target"]})
        self._emit("learning.promoted", c, {"detail": why, "agents": affected, "observed": c.get("observed"),
                                            "summary": f"{c['scope']}:{c.get('owner_id') or 'global'} {c['target']} {c.get('target_ref') or ''} → promoted cho {len(affected)} agent",
                                            "rollback_cmd": f"agent-hub learn rollback {c['ledger_entry_id']}"})
        return {"canary_id": cid, "action": "promoted", "reason": why, "agents": affected}

    def _finish(self, c: dict[str, Any], status: str, reason: str) -> None:
        self.hub.store._update("tier_canaries", "id", c["id"], {"status": status, "finished_at": now_iso(), "reason": reason[:500]})
        # proposal cùng đích đang chờ canary này → thử lại ở vòng nền kế tiếp
        self.conn.execute("UPDATE proposals SET not_before=? WHERE status='deferred_rate_limited' AND block_reason='canary_active'", (now_iso(),))

    def _emit(self, etype: str, c: dict[str, Any], extra: dict[str, Any]) -> None:
        agent = c.get("canary_agent_id") or c.get("origin_agent_id")
        ev = make_event(etype, agent, {"canary_id": c["id"], "proposal_id": c.get("proposal_id"), "ledger_entry_id": c.get("ledger_entry_id"), "mode": c["mode"],
                                       "scope": c["scope"], "owner": c.get("owner_id"), "target": c["target"], "target_ref": c.get("target_ref"),
                                       "canary_agent": c.get("canary_agent_id"), "watch_agents": c.get("watch_agents"), "risk_class": "L3", **extra})
        if c["scope"] == "team":
            ev.team_id = c.get("owner_id")
        self.hub.publish(ev)

    # ------------------------------------------------------------------ đọc
    def get(self, cid: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM tier_canaries WHERE id=?", (cid,)).fetchone()
        if r is None:
            rows = self.conn.execute("SELECT * FROM tier_canaries WHERE id LIKE ? LIMIT 2", (cid + "%",)).fetchall()
            if len(rows) != 1:
                raise NotFound(f"không tìm thấy canary '{cid}'")
            r = rows[0]
        return self.hub.store._row("tier_canaries", r)  # type: ignore[return-value]

    def list(self, status: str | None = None, agent_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        sql = "SELECT * FROM tier_canaries" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY started_at DESC, rowid DESC LIMIT ?"
        rows = [self.hub.store._row("tier_canaries", r) for r in self.conn.execute(sql, (*args, int(limit)))]
        if agent_id:
            rows = [r for r in rows if r and (agent_id in (r.get("watch_agents") or []) or r.get("origin_agent_id") == agent_id)]
        out = []
        cfg = self.cfg()
        for r in rows:
            if r is None:
                continue
            obs = r.get("observed") or {}
            r["progress"] = f"{obs.get('runs', 0)}/{cfg['runs']} run" if r["status"] == "canary" else None
            out.append(r)
        return out


def entry_hash(text: str) -> str:
    """Khoá pin một dòng (mục) của USER.md dùng chung (FR-LN-14)."""
    return hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()[:12]
