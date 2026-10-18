"""Theo dõi hồi quy + tự rollback (FR-LN-17).

Với mỗi ledger entry đã áp dụng (của M4-A) cho agent: cửa sổ quan sát = `eval.regression.watch_runs` run TIẾP THEO của agent
(chỉ run làm việc thật: kind adhoc/task), mốc so sánh = run trong `baseline_days` ngày TRƯỚC thay đổi. Chỉ số:
- `correction` : Signal `correction`/`reject`/`cancel`/`edit_output`/`undo` gắn run trong cửa sổ, cộng run bị người huỷ
  (status `cancelled`), đếm theo run (một run tính một lần);
- `failure`    : run `failed|crashed|protocol_violation|timed_out`;
- `cost`       : chi phí trung bình/run.
Hồi quy khi (tỉ lệ cửa sổ > tỉ lệ mốc × (1 + `threshold_pct`/100)) VÀ số sự kiện trong cửa sổ ≥ `min_events`
(mốc = 0 → chỉ cần đủ `min_events`). Chi phí: trung bình > mốc × (1 + `cost_threshold_pct`/100), cần ≥ `min_runs_cost` run
mỗi phía. Entry gắn cờ `unevaluated` (không có golden task) dùng ngưỡng chặt hơn × `unevaluated_factor`.

Phát hiện hồi quy → rollback thay đổi GẦN NHẤT còn hiệu lực của agent có thể gây ra (đang theo dõi, theo thứ tự ngược thời
gian) qua `learning.ledger.rollback(entry_id, actor="regression_monitor", reason)`; A phát `learning.rolled_back`.
Trạng thái theo dõi lưu ở bảng `regression_watch` (watching → passed | rolled_back | warned | gone).
CHỈ tự rollback entry do vòng học/agent tạo (actor learning/review/agent/curator — `ports.AUTO_ROLLBACK_ACTORS`);
thay đổi của NGƯỜI (actor user…) chỉ được cảnh báo (`regression.detected{human_change: true}` → digest + Slack).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..events import HubEvent, iso, make_event
from ..store import now_iso
from .ports import is_auto_change, ledger_list, ledger_rollback

if TYPE_CHECKING:
    from ..hub import Hub

WORK_KINDS = ("adhoc", "task")
CORRECTION_KINDS = ("correction", "reject", "cancel", "edit_output", "undo")
FAIL_STATUSES = ("failed", "crashed", "protocol_violation", "timed_out")
DONE_STATUSES = ("completed", "failed", "crashed", "protocol_violation", "timed_out", "cancelled", "blocked", "budget_exceeded")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def evaluate(window: dict[str, Any], baseline: dict[str, Any], cfg: dict[str, Any], *, unevaluated: bool = False) -> list[dict[str, Any]]:
    """HÀM THUẦN: so cửa sổ với mốc → danh sách vi phạm (rỗng = không hồi quy)."""
    thr = float(cfg.get("threshold_pct", 50)) / 100.0
    if unevaluated:
        thr *= float(cfg.get("unevaluated_factor", 0.5))
    min_events = int(cfg.get("min_events", 2))
    out: list[dict[str, Any]] = []
    n_w, n_b = int(window.get("runs") or 0), int(baseline.get("runs") or 0)
    if n_w == 0:
        return out
    for metric in ("correction", "failure"):
        ev_w = int(window.get(metric) or 0)
        rate_w = ev_w / n_w
        rate_b = (int(baseline.get(metric) or 0) / n_b) if n_b else 0.0
        if ev_w >= min_events and rate_w > rate_b * (1 + thr) + 1e-9:
            out.append({"metric": metric, "window_rate": round(rate_w, 4), "baseline_rate": round(rate_b, 4), "window_events": ev_w,
                        "window_runs": n_w, "baseline_runs": n_b, "threshold_pct": round(thr * 100, 1)})
    cthr = float(cfg.get("cost_threshold_pct", 50)) / 100.0
    if unevaluated:
        cthr *= float(cfg.get("unevaluated_factor", 0.5))
    min_cost_runs = int(cfg.get("min_runs_cost", 3))
    cw, cb = window.get("cost_avg"), baseline.get("cost_avg")
    if cw is not None and cb and n_w >= min_cost_runs and n_b >= min_cost_runs and cw > cb * (1 + cthr):
        out.append({"metric": "cost", "window_avg": round(cw, 6), "baseline_avg": round(cb, 6), "window_runs": n_w, "baseline_runs": n_b,
                    "threshold_pct": round(cthr * 100, 1)})
    return out


class RegressionMonitor:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._pending: set[str] = set()

    def cfg(self) -> dict[str, Any]:
        return dict((self.hub.config.get("eval") or {}).get("regression") or {})

    # ------------------------------------------------------------------ bus
    def on_event(self, ev: HubEvent) -> None:
        """Run làm việc của agent kết thúc → đánh dấu agent cần kiểm (chạy ở tick của scheduler)."""
        if ev.type in ("run.completed", "run.failed", "run.cancelled") and ev.agent_id and ev.source == "hub":
            self._pending.add(ev.agent_id)
        elif ev.type == "learning.signal" and ev.agent_id:
            self._pending.add(ev.agent_id)
        elif ev.type == "learning.applied" and ev.agent_id:
            self._pending.add(ev.agent_id)

    def take_pending(self) -> set[str]:
        p, self._pending = self._pending, set()
        return p

    # ------------------------------------------------------------------ metrics
    def _runs(self, agent_id: str, *, after: str | None = None, before: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        where = ["agent_id=?", f"kind IN ({','.join('?' for _ in WORK_KINDS)})", f"status IN ({','.join('?' for _ in DONE_STATUSES)})"]
        args: list[Any] = [agent_id, *WORK_KINDS, *DONE_STATUSES]
        if after:
            where.append("queued_at>?")
            args.append(after)
        if before:
            where.append("queued_at<?")
            args.append(before)
        sql = "SELECT id, status, cost_usd, queued_at FROM runs WHERE " + " AND ".join(where) + " ORDER BY queued_at" + (" LIMIT ?" if limit else "")
        if limit:
            args.append(limit)
        return [dict(r) for r in self.hub.store.conn.execute(sql, args)]

    def metrics(self, runs: list[dict[str, Any]]) -> dict[str, Any]:
        ids = [r["id"] for r in runs]
        corr: set[str] = {r["id"] for r in runs if r["status"] == "cancelled"}
        if ids:
            q = (f"SELECT DISTINCT run_id FROM signals WHERE run_id IN ({','.join('?' for _ in ids)}) "
                 f"AND kind IN ({','.join('?' for _ in CORRECTION_KINDS)})")
            corr |= {r[0] for r in self.hub.store.conn.execute(q, (*ids, *CORRECTION_KINDS))}
        costs = [float(r["cost_usd"]) for r in runs if r.get("cost_usd") is not None]
        return {"runs": len(runs), "correction": len(corr), "failure": sum(1 for r in runs if r["status"] in FAIL_STATUSES),
                "cost_avg": (sum(costs) / len(costs)) if costs else None, "run_ids": ids[-20:]}

    # ------------------------------------------------------------------ main
    async def sync_watches(self, agent_id: str | None = None) -> None:
        """Đưa entry mới của ledger vào danh sách theo dõi; entry đã bị rollback → `gone`."""
        cfg = self.cfg()
        since = iso(datetime.now(timezone.utc) - timedelta(days=int(cfg.get("track_days", 14))))
        rows = await ledger_list(self.hub, agent_id=agent_id, since=since)
        conn = self.hub.store.conn
        known = {r[0]: r[1] for r in conn.execute("SELECT entry_id, status FROM regression_watch")}
        for e in rows:
            eid = str(e.get("id"))
            if not e.get("agent_id") or e.get("is_rollback") or e.get("actor") in ("regression_monitor", "eval_gate", "rollback"):
                continue
            if e.get("rolled_back"):
                if known.get(eid) == "watching":
                    conn.execute("UPDATE regression_watch SET status='gone', updated_at=? WHERE entry_id=?", (now_iso(), eid))
                continue
            if eid in known:
                continue
            flags = e.get("flags") or []
            conn.execute(
                "INSERT INTO regression_watch (entry_id, agent_id, applied_at, target, target_ref, risk_class, unevaluated, status, created_at, updated_at, actor) "
                "VALUES (?,?,?,?,?,?,?, 'watching', ?, ?, ?)",
                (eid, e["agent_id"], e.get("applied_at") or now_iso(), e.get("target"), e.get("target_ref"), e.get("risk_class"),
                 1 if "unevaluated" in flags else 0, now_iso(), now_iso(), e.get("actor")),
            )

    async def check_agent(self, agent_id: str) -> list[dict[str, Any]]:
        """Kiểm mọi entry đang theo dõi của agent. Trả danh sách hành động (rollback/passed)."""
        await self.sync_watches(agent_id)
        cfg = self.cfg()
        watch_runs = int(cfg.get("watch_runs", 10))
        base_days = int(cfg.get("baseline_days", 7))
        conn = self.hub.store.conn
        watches = [dict(r) for r in conn.execute(
            "SELECT * FROM regression_watch WHERE agent_id=? AND status='watching' ORDER BY applied_at DESC", (agent_id,))]
        actions: list[dict[str, Any]] = []
        for w in watches:
            applied = w["applied_at"]
            window_runs = self._runs(agent_id, after=applied, limit=watch_runs)
            ad = _parse(applied) or datetime.now(timezone.utc)
            base_runs = self._runs(agent_id, after=iso(ad - timedelta(days=base_days)), before=applied)
            win, base = self.metrics(window_runs), self.metrics(base_runs)
            viol = evaluate(win, base, cfg, unevaluated=bool(w.get("unevaluated")))
            conn.execute("UPDATE regression_watch SET observed=?, baseline=?, updated_at=? WHERE entry_id=?",
                         (json.dumps(win), json.dumps(base), now_iso(), w["entry_id"]))
            if viol:
                # thay đổi gần nhất còn hiệu lực DO VÒNG HỌC/AGENT tạo (watches sắp giảm dần theo thời gian);
                # thay đổi của NGƯỜI không bao giờ bị tự rollback — chỉ cảnh báo (digest/Slack)
                auto = [x for x in watches if is_auto_change(x)]
                if auto:
                    act = await self._rollback(auto[0], viol, win, base, trigger_entry=w["entry_id"])
                else:
                    act = self._warn_human(w, viol)
                actions.append(act)
                break  # trạng thái đổi — lần kiểm sau tính lại với phần còn lại
            if win["runs"] >= watch_runs:
                conn.execute("UPDATE regression_watch SET status='passed', updated_at=? WHERE entry_id=?", (now_iso(), w["entry_id"]))
                actions.append({"entry_id": w["entry_id"], "action": "passed"})
        return actions

    @staticmethod
    def _describe(w: dict[str, Any], viol: list[dict[str, Any]]) -> str:
        v = viol[0]
        if v["metric"] == "cost":
            detail = f"chi phí/run {v['baseline_avg']:.4f} → {v['window_avg']:.4f} USD"
        else:
            name = {"correction": "tỉ lệ correction/huỷ", "failure": "tỉ lệ run lỗi"}[v["metric"]]
            detail = f"{name} {v['baseline_rate'] * 100:.0f}% → {v['window_rate'] * 100:.0f}% ({v['window_events']}/{v['window_runs']} run)"
        return f"hồi quy sau thay đổi {w.get('target') or ''}:{w.get('target_ref') or ''} — {detail}"

    def _warn_human(self, w: dict[str, Any], viol: list[dict[str, Any]]) -> dict[str, Any]:
        """Thay đổi của NGƯỜI: KHÔNG rollback, ghi cảnh báo (một lần) → digest + Slack."""
        reason = self._describe(w, viol) + f" — thay đổi do người tạo (actor={w.get('actor')}), KHÔNG tự rollback"
        self.hub.store.conn.execute("UPDATE regression_watch SET status='warned', reason=?, updated_at=? WHERE entry_id=?", (reason, now_iso(), w["entry_id"]))
        self.hub.publish(make_event("regression.detected", w["agent_id"], {"ledger_entry_id": w["entry_id"], "violations": viol, "rolled_back": False,
                                                                            "human_change": True, "actor": w.get("actor"), "reason": reason,
                                                                            "hint": f"agent-hub learn rollback {w['entry_id']}"}))
        return {"entry_id": w["entry_id"], "action": "warned_human", "reason": reason, "violations": viol}

    async def _rollback(self, w: dict[str, Any], viol: list[dict[str, Any]], win: dict[str, Any], base: dict[str, Any], *, trigger_entry: str) -> dict[str, Any]:
        reason = self._describe(w, viol)
        r = await ledger_rollback(self.hub, w["entry_id"], actor="regression_monitor", reason=reason)
        if r.get("human_change"):
            # H-04: người đã sửa đích sau entry (hoặc chuỗi chứa entry của người) → Ledger KHÔNG ghi và đã phát
            # regression.detected{human_change}; ngừng theo dõi entry (cảnh báo một lần, không thử lại mỗi vòng)
            why = reason + f" — {r.get('error')}"
            self.hub.store.conn.execute("UPDATE regression_watch SET status='warned', reason=?, updated_at=? WHERE entry_id=?", (why[:1000], now_iso(), w["entry_id"]))
            return {"entry_id": w["entry_id"], "action": "warned_human", "reason": why, "violations": viol}
        ok = bool(r.get("ok", True)) and not r.get("error")
        self.hub.store.conn.execute(
            "UPDATE regression_watch SET status=?, reason=?, updated_at=? WHERE entry_id=?",
            ("rolled_back" if ok else "watching", reason + ("" if ok else f" (rollback lỗi: {r.get('error')})"), now_iso(), w["entry_id"]),
        )
        self.hub.publish(make_event("regression.detected", w["agent_id"], {"ledger_entry_id": w["entry_id"], "trigger_entry_id": trigger_entry,
                                                                            "violations": viol, "rolled_back": ok, "reason": reason, "error": r.get("error")}))
        return {"entry_id": w["entry_id"], "action": "rollback" if ok else "rollback_failed", "reason": reason, "violations": viol}

    async def check_all(self) -> list[dict[str, Any]]:
        await self.sync_watches()
        agents = {r[0] for r in self.hub.store.conn.execute("SELECT DISTINCT agent_id FROM regression_watch WHERE status='watching'")}
        out = []
        for a in sorted(agents):
            out += await self.check_agent(a)
        return out

    def list(self, agent_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if agent_id:
            where.append("agent_id=?")
            args.append(agent_id)
        if status:
            where.append("status=?")
            args.append(status)
        rows = self.hub.store.conn.execute("SELECT * FROM regression_watch" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY applied_at DESC LIMIT 200", args)
        out = []
        for r in rows:
            d = dict(r)
            for k in ("observed", "baseline"):
                if d.get(k):
                    d[k] = json.loads(d[k])
            out.append(d)
        return out
