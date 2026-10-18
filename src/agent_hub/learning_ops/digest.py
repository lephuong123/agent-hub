"""Digest học ("báo chứ không hỏi", FR-LN-13, Q-18).

- Lịch: hằng ngày `digest.schedule` (mặc định "08:00" GIỜ MÁY) — scheduler gọi `maybe_run()`; `agent-hub learn digest`
  / RPC `digest.run` chạy ngay. Kỳ = từ cuối digest trước (hoặc 24 h) tới hiện tại.
- Nguồn: bảng `events` (append-only): `learning.applied|rolled_back|eval_failed|deferred|canary|promoted`, `guard.blocked`,
  `golden.suggested`, `curator.report`, `eval.completed`, `regression.detected`; chi phí học = run kind review/eval/
  eval_judge/curator trong kỳ.
- Nội dung theo TEAM → AGENT: đã áp dụng (diff rút gọn + lệnh `agent-hub learn rollback <entry>`), đã rollback (lý do,
  actor), eval_failed (điểm trước/sau), hoãn (`deferred_*`), bị chặn (`guard.blocked`, 🔴 nếu chạm chính sách an toàn),
  canary (M5 — placeholder), gợi ý golden task (Q-23: kèm run ứng viên + lệnh `golden from-run`), curator, chi phí học.
- Lưu bảng `digests`; gửi Slack qua `notify/` của M1-B (event `learning.digest`, có thử lại/backoff); TUI gọi
  `digest.tui_open` khi mở: lần đầu trong ngày → trả các digest CHƯA XEM (gộp nếu nhiều ngày không mở).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..events import iso, make_event
from ..store import now_iso

if TYPE_CHECKING:
    from ..hub import Hub

DIGEST_TYPES = [
    "learning.applied", "learning.rolled_back", "learning.eval_failed", "learning.deferred", "learning.canary", "learning.promoted",
    "guard.blocked", "golden.suggested", "curator.report", "eval.completed", "regression.detected",
]
LEARNING_RUN_KINDS = ("review", "eval", "eval_judge", "curator")
POLICY_REASONS = ("policy_touch", "self_escalation", "bounds", "safety_policy", "policy")


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _clip(s: Any, n: int = 160) -> str:
    t = " ".join(str(s or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _diff_stat(diff: Any) -> str:
    if not diff:
        return ""
    if isinstance(diff, dict):
        return _clip(json.dumps(diff, ensure_ascii=False), 80)
    lines = str(diff).splitlines()
    add = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    rem = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    return f"+{add}/−{rem} dòng" if (add or rem) else _clip(diff, 80)


class DigestService:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub

    def cfg(self) -> dict[str, Any]:
        return dict(self.hub.config.get("digest") or {})

    # ------------------------------------------------------------------ lịch
    def due(self, now_local: datetime | None = None) -> bool:
        c = self.cfg()
        if not c.get("enabled", True):
            return False
        now_local = now_local or _local_now()
        hh, mm = parse_hhmm(c.get("schedule"), (8, 0))
        if (now_local.hour, now_local.minute) < (hh, mm):
            return False
        last = self.hub.store.state_get("digest.last_local_date")
        return last != now_local.date().isoformat()

    async def maybe_run(self) -> dict[str, Any] | None:
        if not self.due():
            return None
        return self.run(scheduled=True)

    # ------------------------------------------------------------------ dựng
    def collect(self, start: str, end: str, *, agent_id: str | None = None, rowids: tuple[int, int] | None = None) -> dict[str, Any]:
        """Event trong kỳ. `rowids=(sau, tới)`: mốc theo rowid bảng events (không sót/trùng event cùng mili-giây)."""
        conn = self.hub.store.conn
        if rowids is not None:
            q = f"SELECT type, agent_id, run_id, ts, payload FROM events WHERE rowid>? AND rowid<=? AND type IN ({','.join('?' for _ in DIGEST_TYPES)})"
            args: list[Any] = [rowids[0], rowids[1], *DIGEST_TYPES]
        else:
            q = f"SELECT type, agent_id, run_id, ts, payload FROM events WHERE ts>? AND ts<=? AND type IN ({','.join('?' for _ in DIGEST_TYPES)})"
            args = [start, end, *DIGEST_TYPES]
        if agent_id:
            q += " AND agent_id=?"
            args.append(agent_id)
        rows = conn.execute(q + " ORDER BY ts", args).fetchall()
        agents: dict[str, dict[str, list[dict[str, Any]]]] = {}
        hub_items: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            p = json.loads(r["payload"] or "{}")
            item = {"ts": r["ts"], "run_id": r["run_id"], **p}
            bucket = agents.setdefault(r["agent_id"], {}) if r["agent_id"] else hub_items
            bucket.setdefault(r["type"], []).append(item)
        # guard.blocked ở runtime (không phải vòng học) chỉ đếm, không liệt kê từng cái
        cost_rows = conn.execute(
            f"SELECT agent_id, kind, COALESCE(SUM(cost_usd),0), COUNT(*) FROM runs WHERE queued_at>? AND queued_at<=? AND kind IN ({','.join('?' for _ in LEARNING_RUN_KINDS)}) GROUP BY agent_id, kind",
            (start, end, *LEARNING_RUN_KINDS),
        ).fetchall()
        costs: dict[str, dict[str, Any]] = {}
        for a, kind, c, n in cost_rows:
            if agent_id and a != agent_id:
                continue
            d = costs.setdefault(a, {"total": 0.0})
            d[kind] = {"usd": round(float(c), 6), "runs": int(n)}
            d["total"] = round(d["total"] + float(c), 6)
        teams: dict[str, list[str]] = {}
        for t_id, a in conn.execute("SELECT team_id, agent_id FROM team_members"):
            teams.setdefault(a, []).append(t_id)
        golden_counts = self.hub.evals.golden.counts()
        return {"period_start": start, "period_end": end, "agents": agents, "hub": hub_items, "costs": costs, "teams": teams, "golden_counts": golden_counts}

    def counts(self, data: dict[str, Any]) -> dict[str, int]:
        c = {"applied": 0, "rolled_back": 0, "eval_failed": 0, "deferred": 0, "blocked": 0, "canary": 0, "golden_suggested": 0, "regressions": 0, "human_warnings": 0}
        for buckets in list(data["agents"].values()) + [data["hub"]]:
            c["applied"] += len(buckets.get("learning.applied", []))
            c["rolled_back"] += len(buckets.get("learning.rolled_back", []))
            c["eval_failed"] += len(buckets.get("learning.eval_failed", []))
            c["deferred"] += len(buckets.get("learning.deferred", []))
            c["blocked"] += len([b for b in buckets.get("guard.blocked", []) if self._is_learning_block(b)])
            c["canary"] += len(buckets.get("learning.canary", [])) + len(buckets.get("learning.promoted", []))
            c["golden_suggested"] += len(buckets.get("golden.suggested", []))
            c["regressions"] += len(buckets.get("regression.detected", []))
            c["human_warnings"] += len([x for x in buckets.get("regression.detected", []) if x.get("human_change")])
        c["learning_cost_usd"] = round(sum(v.get("total", 0) for v in data["costs"].values()), 4)  # type: ignore[assignment]
        return c

    @staticmethod
    def _is_learning_block(b: dict[str, Any]) -> bool:
        return b.get("source") == "learning" or bool(b.get("proposal_id"))

    def render(self, data: dict[str, Any]) -> str:
        cnt = self.counts(data)
        start, end = data["period_start"], data["period_end"]
        head = (f"*agent-hub — digest học* ({_fmt_ts(start)} → {_fmt_ts(end)})\n"
                f"Áp dụng {cnt['applied']} · rollback {cnt['rolled_back']} · eval_failed {cnt['eval_failed']} · hoãn {cnt['deferred']} · "
                f"bị chặn {cnt['blocked']} · gợi ý golden {cnt['golden_suggested']} · chi phí học ${cnt['learning_cost_usd']:.4f}")
        agent_ids = sorted(set(data["agents"]) | set(data["costs"]))
        if not agent_ids and not data["hub"]:
            return head + "\n_Không có thay đổi học nào trong kỳ._"
        by_team: dict[str, list[str]] = {}
        for a in agent_ids:
            for t in data["teams"].get(a) or ["(không thuộc team)"]:
                by_team.setdefault(t, []).append(a)
        out = [head]
        for team in sorted(by_team, key=lambda t: (t.startswith("("), t)):
            out.append(f"\n*Team {team}*" if not team.startswith("(") else f"\n*{team}*")
            for a in by_team[team]:
                out += self._render_agent(a, data["agents"].get(a) or {}, data["costs"].get(a), data["golden_counts"].get(a, 0))
        hub_items = data["hub"]
        if hub_items.get("curator.report") or hub_items.get("guard.blocked"):
            out.append("\n*Toàn hub*")
            for r in hub_items.get("curator.report", []):
                out.append(_curator_line(r))
            rt = [b for b in hub_items.get("guard.blocked", [])]
            if rt:
                out.append(f"• guard chặn {len(rt)} thao tác ở runtime (xem `agent-hub guard decisions`)")
        return "\n".join(out)

    def _render_agent(self, a: str, b: dict[str, list[dict[str, Any]]], cost: dict[str, Any] | None, golden_n: int) -> list[str]:
        lines = [f"`{a}`" + (f" — chi phí học ${cost['total']:.4f}" if cost else "") + ("" if golden_n else " — ⚠️ chưa có golden task")]
        for e in b.get("learning.applied", []):
            entry = e.get("ledger_entry_id") or e.get("entry_id")
            what = f"{e.get('risk_class') or ''} {e.get('op') or ''} {e.get('target') or ''} `{e.get('target_ref') or ''}`".strip()
            extra = _diff_stat(e.get("diff") or e.get("diff_summary"))
            ev = ""
            if e.get("eval_before") is not None or e.get("eval_after") is not None:
                ev = f" (eval {e.get('eval_before')} → {e.get('eval_after')})"
            flag = " [unevaluated]" if "unevaluated" in (e.get("flags") or []) else ""
            lines.append(f"  ✅ áp dụng: {_clip(what, 90)}{(' ' + extra) if extra else ''}{ev}{flag}" + (f" — rollback: `agent-hub learn rollback {entry}`" if entry else ""))
        for e in b.get("learning.rolled_back", []):
            lines.append(f"  ↩️ rollback `{e.get('ledger_entry_id') or e.get('entry_id')}` (actor={e.get('actor')}): {_clip(e.get('reason'), 140)}")
        for e in b.get("regression.detected", []):
            if e.get("human_change"):
                lines.append(f"  ⚠️ cảnh báo (thay đổi của NGƯỜI, không tự rollback): {_clip(e.get('reason'), 160)} — nếu muốn: `{e.get('hint')}`")
            elif not e.get("rolled_back"):
                lines.append(f"  ⚠️ phát hiện hồi quy nhưng rollback lỗi: {_clip(e.get('reason'))} ({_clip(e.get('error'), 80)})")
        for e in b.get("learning.eval_failed", []):
            sc = e.get("scores") or {}
            s_b = sc.get("before", e.get("before", e.get("eval_before")))
            s_a = sc.get("after", e.get("after", e.get("eval_after")))
            lines.append(f"  ❌ eval_failed proposal `{e.get('proposal_id')}`: điểm {s_b} → {s_a} — không áp dụng" + (f" ({_clip(e.get('reason'), 80)})" if e.get("reason") else ""))
        for e in b.get("learning.deferred", []):
            lines.append(f"  ⏸ hoãn proposal `{e.get('proposal_id')}` ({e.get('reason') or e.get('status')})")
        for e in b.get("guard.blocked", []):
            red = any(k in str(e.get("reason") or e.get("rule") or "") for k in POLICY_REASONS) or e.get("policy")
            lines.append(f"  {'🔴' if red else '⛔'} bị chặn{' (chính sách an toàn)' if red else ''}: {_clip(e.get('detail') or e.get('reason'), 140)}"
                         + (f" — proposal `{e['proposal_id']}`" if e.get("proposal_id") else ""))
        for e in b.get("learning.canary", []) + b.get("learning.promoted", []):
            lines.append(f"  🐤 canary: {_clip(e, 120)}")
        sugg = b.get("golden.suggested", [])
        if sugg:
            last = sugg[-1]
            cands = last.get("candidates") or []
            why = "thay đổi L3 bị hoãn vì chưa có golden task (Q-23)" if any(s.get("reason") == "deferred_no_eval" for s in sugg) else "nên có golden task để chấm thay đổi"
            lines.append(f"  💡 gợi ý golden task: {why}. Ứng viên:")
            for c in cands[:3]:
                lines.append(f"     – run `{c['run_id']}` ({c.get('reason')}): {_clip(c.get('prompt_preview'), 80)} → `agent-hub golden from-run {c['run_id']} --judge \"<rubric>\"`")
            if not cands:
                lines.append(f"     – chưa có run phù hợp; viết tay: `agent-hub golden add {a} --prompt \"…\" --regex \"…\"`")
        evs = b.get("eval.completed", [])
        if evs:
            st: dict[str, int] = {}
            for e in evs:
                st[e.get("status") or "?"] = st.get(e.get("status") or "?", 0) + 1
            lines.append("  📊 eval: " + ", ".join(f"{k} {v}" for k, v in sorted(st.items())))
        for r in b.get("curator.report", []):
            lines.append("  " + _curator_line(r))
        if len(lines) == 1:
            lines.append("  (không có thay đổi)")
        return lines

    # ------------------------------------------------------------------ chạy
    def run(self, *, scheduled: bool = False, since: str | None = None, agent_id: str | None = None, send: bool = True) -> dict[str, Any]:
        end = now_iso()
        last = self.hub.store.state_get("digest.last_end")
        start = since or last or iso(datetime.now(timezone.utc) - timedelta(hours=float(self.cfg().get("default_period_h", 24))))
        max_rowid = int(self.hub.store.conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM events").fetchone()[0])
        last_rowid = self.hub.store.state_get("digest.last_rowid")
        rowids = (int(last_rowid), max_rowid) if (since is None and last and last_rowid is not None) else None
        data = self.collect(start, end, agent_id=agent_id, rowids=rowids)
        text = self.render(data)
        cnt = self.counts(data)
        did = "dg_" + uuid.uuid4().hex[:10]
        empty = all(v == 0 for k, v in cnt.items() if k != "learning_cost_usd")
        will_send = send and (not empty or bool(self.cfg().get("send_empty", False)))
        self.hub.store.conn.execute(
            "INSERT INTO digests (id, period_start, period_end, created_at, scheduled, agent_id, counts, text, data, slack_status) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (did, start, end, end, 1 if scheduled else 0, agent_id, json.dumps(cnt), text, json.dumps(data, ensure_ascii=False, default=str)[:500_000],
             "queued" if will_send else "skipped"),
        )
        if agent_id is None:
            self.hub.store.state_set("digest.last_end", end)
            self.hub.store.state_set("digest.last_rowid", max_rowid)
        if scheduled:
            self.hub.store.state_set("digest.last_local_date", _local_now().date().isoformat())
        self.hub.publish(make_event("learning.digest", None, {"digest_id": did, "period_start": start, "period_end": end, **cnt,
                                                              "send": will_send, "text": text if will_send else None, "scheduled": scheduled}))
        return {"id": did, "period_start": start, "period_end": end, "counts": cnt, "text": text, "slack": "queued" if will_send else "skipped"}

    def on_notify(self, ev) -> None:
        """Listener: cập nhật trạng thái gửi Slack của digest (notify.sent / notify.failed của M1-B)."""
        if ev.type in ("notify.sent", "notify.failed") and (ev.payload or {}).get("for") == "learning.digest" \
                and (ev.payload or {}).get("channel", "slack") == "slack":  # Telegram (chat/) không đổi slack_status
            did = ev.payload.get("digest_id")
            status = "sent" if ev.type == "notify.sent" else ("failed" if ev.payload.get("retry_in_s") is None else "retrying")
            if did:
                self.hub.store.conn.execute("UPDATE digests SET slack_status=? WHERE id=?", (status, did))
            else:
                self.hub.store.conn.execute("UPDATE digests SET slack_status=? WHERE id=(SELECT id FROM digests WHERE slack_status IN ('queued','retrying') ORDER BY created_at LIMIT 1)", (status,))

    # ------------------------------------------------------------------ đọc / TUI
    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.hub.store.conn.execute("SELECT id, period_start, period_end, created_at, scheduled, agent_id, counts, slack_status, tui_seen_at FROM digests ORDER BY created_at DESC LIMIT ?", (limit,))
        return [{**dict(r), "counts": json.loads(r["counts"] or "{}")} for r in rows]

    def get(self, ref: str | None = None) -> dict[str, Any] | None:
        conn = self.hub.store.conn
        r = conn.execute("SELECT * FROM digests WHERE id LIKE ? ORDER BY created_at DESC LIMIT 1", ((ref or "") + "%",)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["counts"] = json.loads(d["counts"] or "{}")
        d["data"] = json.loads(d["data"] or "{}")
        return d

    def tui_open(self, *, mark: bool = True) -> dict[str, Any]:
        """TUI mở: lần đầu trong ngày (giờ máy) → trả digest chưa xem (gộp), rồi đánh dấu đã xem."""
        today = _local_now().date().isoformat()
        first_today = self.hub.store.state_get("digest.tui_last_open_date") != today
        conn = self.hub.store.conn
        unseen = [dict(r) for r in conn.execute("SELECT id, period_start, period_end, counts, text FROM digests WHERE tui_seen_at IS NULL AND agent_id IS NULL ORDER BY created_at")]
        def _nonempty(d: dict[str, Any]) -> bool:
            c = json.loads(d["counts"] or "{}")
            return any(v for k, v in c.items() if k != "learning_cost_usd")

        unseen_all = unseen
        unseen = [d for d in unseen if _nonempty(d)]  # digest rỗng không bật lên (vẫn đánh dấu đã xem)
        show = first_today and bool(unseen)
        if mark:
            self.hub.store.state_set("digest.tui_last_open_date", today)
            if first_today and unseen_all:
                conn.execute("UPDATE digests SET tui_seen_at=? WHERE tui_seen_at IS NULL AND agent_id IS NULL", (now_iso(),))
        return {"show": show, "first_open_today": first_today, "digests": [{**d, "counts": json.loads(d["counts"] or "{}")} for d in unseen] if show else [],
                "text": "\n\n".join(d["text"] for d in unseen) if show else ""}


def parse_hhmm(v: Any, default: tuple[int, int]) -> tuple[int, int]:
    """"08:00" → (8, 0). YAML 1.1 đọc `17:41` (không nháy) thành số cơ số 60 (1061) → hiểu là phút từ nửa đêm."""
    if isinstance(v, int) and not isinstance(v, bool):
        return divmod(v % (24 * 60), 60)
    try:
        hh, mm = (int(x) for x in str(v).split(":")[:2])
        return hh, mm
    except (TypeError, ValueError):
        return default


def _fmt_ts(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%d/%m %H:%M")
    except ValueError:
        return ts


def _curator_line(r: dict[str, Any]) -> str:
    return (f"🧹 curator{' (dry-run)' if r.get('dry_run') else ''}: kiểm {r.get('checked', 0)} skill, stale {r.get('marked_stale', 0)}, "
            f"archive {r.get('archived', 0)}, kích hoạt lại {r.get('reactivated', 0)}" + (f" — {r['note']}" if r.get("note") else ""))
