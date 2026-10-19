"""Curator pha 1 — tất định, không gọi model (FR-LN-08; theo Hermes `agent/curator.py` `apply_automatic_transitions`).

Luật (mỗi skill tầng agent, trừ skill `pinned`):
- mốc hoạt động = `last_used_at`, chưa dùng bao giờ → `created_at`;
- chưa từng dùng (`use_count == 0`) mà còn trẻ hơn `stale_after_days` → không đụng (stale → active nếu lỡ stale);
- mốc ≤ now − `archive_after_days` (30) và chưa archived → ARCHIVE (xoá mềm vào `.archive/`, qua pipeline của M4-A:
  snapshot + LedgerEntry `actor=curator`, khôi phục bằng `learn rollback`);
- mốc ≤ now − `stale_after_days` (14) và đang active → `stale`;
- dùng lại sau khi stale → `active`.
Chạy: định kỳ `curator.interval_days` (7) khi daemon RẢNH (không run nào đang chạy ≥ `curator.min_idle_min`), lần đầu chỉ
gieo mốc rồi hoãn một chu kỳ (như Hermes); `cfharness curator run [--dry-run]` chạy ngay. Báo cáo
`HUB_HOME/logs/curator/<ts>/REPORT.md` + event `curator.report` (vào digest).

Cổng sang M4-A (`hub.knowledge`), dò lúc chạy:
- `hub.knowledge.list_skills("agent", owner)` → bản ghi {name, owner_id, pinned, state, use_count, last_used_at, created_at};
- `hub.knowledge.set_state("agent", owner, name, state)` (stale/active);
- archive: `hub.learning.ledger.apply(Change(op="archive", kind="tree", archive=True, actor="curator"))` — hàm áp dụng duy nhất
  của A (snapshot + LedgerEntry `actor=curator`, khôi phục bằng `learn rollback`).
Thiếu cổng → chỉ báo cáo (như dry-run), ghi rõ lý do.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..events import make_event
from ..store import now_iso
from .ports import knowledge

if TYPE_CHECKING:
    from ..hub import Hub


def _parse(ts: Any) -> datetime | None:
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def plan_transitions(rows: list[dict[str, Any]], now: datetime, *, stale_after_days: int = 14, archive_after_days: int = 30) -> list[dict[str, Any]]:
    """HÀM THUẦN: skill → hành động {owner_id, name, action: stale|archive|reactivate, anchor, reason}."""
    stale_cut = now - timedelta(days=stale_after_days)
    arch_cut = now - timedelta(days=archive_after_days)
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("pinned"):
            continue
        state = r.get("state") or "active"
        if state in ("archived", "deleted"):
            continue
        anchor = _parse(r.get("last_used_at")) or _parse(r.get("created_at")) or now
        base = {"owner_id": r.get("owner_id") or r.get("agent_id"), "name": r["name"], "anchor": anchor.isoformat(), "state": state}
        if int(r.get("use_count") or 0) == 0 and anchor > stale_cut:
            if state == "stale":
                out.append({**base, "action": "reactivate", "reason": "chưa dùng nhưng còn trẻ"})
            continue
        if anchor <= arch_cut:
            out.append({**base, "action": "archive", "reason": f"không dùng {(now - anchor).days} ngày (≥ {archive_after_days})"})
        elif anchor <= stale_cut and state == "active":
            out.append({**base, "action": "stale", "reason": f"không dùng {(now - anchor).days} ngày (≥ {stale_after_days})"})
        elif anchor > stale_cut and state == "stale":
            out.append({**base, "action": "reactivate", "reason": "được dùng lại"})
    return out


async def _call(fn: Any, *a: Any, **kw: Any) -> Any:
    r = fn(*a, **kw)
    if inspect.isawaitable(r):
        r = await r
    return r


class Curator:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.running = False

    def cfg(self) -> dict[str, Any]:
        return dict(self.hub.config.get("curator") or {})

    # ------------------------------------------------------------------ cổng knowledge
    async def _skills(self, agent_id: str | None) -> tuple[list[dict[str, Any]], str | None]:
        k = knowledge(self.hub)
        fn = None
        for name in ("list_skills", "skills"):
            fn = getattr(k, name, None) if k is not None else None
            if callable(fn):
                break
        if not callable(fn):
            return [], "hub.knowledge.list_skills chưa có (M4-A) — curator chỉ báo cáo"
        agents = [agent_id] if agent_id else [a["id"] for a in self.hub.store.list_agents()]
        rows: list[dict[str, Any]] = []
        for a in agents:
            try:
                got = await _call(fn, "agent", a)  # KnowledgeStore.list_skills(scope, owner) của M4-A
            except TypeError:
                got = await _call(fn, scope="agent", owner_id=a)
            for r in got or []:
                d = r.model_dump() if hasattr(r, "model_dump") else dict(r)
                d.setdefault("owner_id", d.get("agent_id") or a)
                if d.get("scope", "agent") == "agent":
                    rows.append(d)
        return rows, None

    async def _apply(self, act: dict[str, Any]) -> tuple[bool, str | None]:
        k = knowledge(self.hub)
        owner, name = act["owner_id"], act["name"]
        try:
            if act["action"] == "archive":
                fn = getattr(k, "archive_skill", None)
                if callable(fn):
                    await _call(fn, owner, name, actor="curator", reason=act["reason"])
                    return True, None
                ledger = getattr(getattr(self.hub, "learning", None), "ledger", None)
                if ledger is None or not hasattr(k, "skill_dir"):
                    return False, "hub.learning.ledger chưa có (M4-A)"
                from ..learning.ledger import Change  # hàm áp dụng DUY NHẤT của A: snapshot + LedgerEntry actor=curator

                ch = Change(scope="agent", owner_id=owner, target="skill", target_ref=name, op="archive", actor="curator", kind="tree",
                            path=k.skill_dir("agent", owner, name), archive=True, agent_id=owner, risk_class="L2",
                            reason=act["reason"], summary=f"curator archive skill {name}: {act['reason']}")
                e = await _call(ledger.apply, ch)
                act["ledger_entry_id"] = (e or {}).get("id")
            else:
                fn = getattr(k, "set_state", None) or getattr(k, "set_skill_state", None)
                if not callable(fn):
                    return False, "hub.knowledge.set_state chưa có"
                await _call(fn, "agent", owner, name, "stale" if act["action"] == "stale" else "active")
            return True, None
        except Exception as e:  # một skill lỗi không chặn cả lượt
            return False, repr(e)[:300]

    # ------------------------------------------------------------------ chạy
    def should_run_now(self, now: datetime | None = None) -> bool:
        c = self.cfg()
        if not c.get("enabled", True) or self.running:
            return False
        now = now or datetime.now(timezone.utc)
        st = self.hub.store.state_get("curator.state") or {}
        last = _parse(st.get("last_run_at"))
        if last is None:  # lần đầu: gieo mốc, hoãn một chu kỳ (Hermes)
            self.hub.store.state_set("curator.state", {**st, "last_run_at": now.isoformat(), "last_summary": "gieo mốc lần đầu — chạy sau một chu kỳ; `cfharness curator run --dry-run` để xem trước"})
            return False
        if now - last < timedelta(days=float(c.get("interval_days", 7))):
            return False
        return self.idle_for_min() >= float(c.get("min_idle_min", 30))

    def idle_for_min(self) -> float:
        if self.hub.runs.active:
            return 0.0
        r = self.hub.store.conn.execute("SELECT MAX(COALESCE(ended_at, queued_at)) FROM runs").fetchone()
        last = _parse(r[0]) if r else None
        if last is None:
            return 1e9
        return (datetime.now(timezone.utc) - last).total_seconds() / 60

    async def run(self, *, dry_run: bool = False, agent_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
        c = self.cfg()
        now = now or datetime.now(timezone.utc)
        self.running = True
        try:
            rows, note = await self._skills(agent_id)
            plan = plan_transitions(rows, now, stale_after_days=int(c.get("stale_after_days", 14)), archive_after_days=int(c.get("archive_after_days", 30)))
            done: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []
            effective_dry = dry_run or note is not None
            if not effective_dry:
                for act in plan:
                    ok, err = await self._apply(act)
                    (done if ok else errors).append({**act, **({"error": err} if err else {})})
            counts = {"checked": len(rows), "pinned_skipped": sum(1 for r in rows if r.get("pinned")),
                      "marked_stale": sum(1 for a in (plan if effective_dry else done) if a["action"] == "stale"),
                      "archived": sum(1 for a in (plan if effective_dry else done) if a["action"] == "archive"),
                      "reactivated": sum(1 for a in (plan if effective_dry else done) if a["action"] == "reactivate"),
                      "errors": len(errors)}
            report = {"ts": now.isoformat(), "dry_run": effective_dry, "requested_dry_run": dry_run, "agent_id": agent_id, "note": note,
                      "counts": counts, "plan": plan, "applied": done, "errors": errors}
            path = self._write_report(report)
            report["report_path"] = str(path)
            if not dry_run:
                st = self.hub.store.state_get("curator.state") or {}
                self.hub.store.state_set("curator.state", {**st, "last_run_at": now.isoformat(), "last_summary": counts, "last_report": str(path)})
            self.hub.publish(make_event("curator.report", agent_id, {
                "dry_run": effective_dry, "note": note, **counts, "report_path": str(path),
                "stale": [f"{a['owner_id']}/{a['name']}" for a in plan if a["action"] == "stale"][:20],
                "archived": [f"{a['owner_id']}/{a['name']}" for a in (plan if effective_dry else done) if a["action"] == "archive"][:20],
            }))
            return report
        finally:
            self.running = False

    def _write_report(self, rep: dict[str, Any]) -> Path:
        d = self.hub.paths.home / "logs" / "curator" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        d.mkdir(parents=True, exist_ok=True)
        (d / "run.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        c = rep["counts"]
        lines = [f"# Curator pha 1 — {rep['ts']}" + ("  (DRY-RUN — không thay đổi gì)" if rep["dry_run"] else ""), ""]
        if rep.get("note"):
            lines += [f"> {rep['note']}", ""]
        lines += [f"- Đã kiểm: {c['checked']} skill (bỏ qua pinned: {c['pinned_skipped']})",
                  f"- Chuyển stale: {c['marked_stale']} · Archive (xoá mềm): {c['archived']} · Kích hoạt lại: {c['reactivated']} · Lỗi: {c['errors']}", ""]
        for title, key in (("Kế hoạch" if rep["dry_run"] else "Đã áp dụng", "plan" if rep["dry_run"] else "applied"), ("Lỗi", "errors")):
            items = rep.get(key) or []
            if items:
                lines.append(f"## {title}")
                for a in items:
                    lines.append(f"- `{a['owner_id']}/{a['name']}` → **{a['action']}** — {a['reason']}" + (f" (lỗi: {a['error']})" if a.get("error") else ""))
                lines.append("")
        lines.append("Khôi phục skill đã archive: `cfharness learn rollback <entry_id>` (ledger actor=curator).")
        (d / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return d / "REPORT.md"

    def status(self) -> dict[str, Any]:
        st = self.hub.store.state_get("curator.state") or {}
        return {"enabled": bool(self.cfg().get("enabled", True)), "interval_days": self.cfg().get("interval_days", 7), "running": self.running,
                "idle_min": round(self.idle_for_min(), 1), **st, "knowledge_ready": callable(getattr(knowledge(self.hub), "list_skills", None)) and getattr(getattr(self.hub, "learning", None), "ledger", None) is not None, "checked_at": now_iso()}
