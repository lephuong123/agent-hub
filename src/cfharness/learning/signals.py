"""Thu Signal có cấu trúc (FR-LN-01) — `hub.learning.signals`.

Nguồn đã có từ M1/M3 (ghi thẳng bảng `signals` + event `learning.signal`): `hub_feedback`/`cfharness feedback`
(correction/good/edit_output), từ chối approval (reject — M1-B), reviewer `request_changes` (reject — M3).
Nguồn thêm ở M4 (listener trên bus, không cần hook — A-11 phương án dự phòng: suy từ event stream-json):
- `correction` (source=steer): tin steer của người (`user.message` steer=true) hoặc `run continue` có cụm phủ định
  (regex `learning.steer_correction_regex`, tiếng Việt + Anh);
- `cancel`: người dừng run (RPC `run.stop` — CLI/TUI/MCP) — bằng chứng: tool đang chạy;
- `undo`: `run revert` (event `run.reverted`);
- `edit_output`: người SỬA file agent vừa ghi — phát hiện khi run kế tiếp chụp checkpoint cùng repo
  (`workspace.checkpoint`), so nội dung hiện tại với bản sau run trước (`workspace.modified_since`) → diff;
- `failure`: cùng lỗi tool (`tool.complete` is_error, cùng tool + thông điệp chuẩn hoá) lặp ở ≥ `learning.failure_min_runs` run;
- `repeat_pattern`: cùng n-gram tool (n = `learning.repeat_pattern_n`) ở ≥ n run khác nhau trong cửa sổ gần đây
  (không lặp lại khi Signal cũ cùng dấu vân tay chưa được review xử lý).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..events import HubEvent, make_event
from ..redact import redact
from ..store import now_iso

if TYPE_CHECKING:
    from ..hub import Hub

_NUM = re.compile(r"\b\d+\b|0x[0-9a-f]+|[0-9a-f]{8,}|/[^\s'\"]+", re.I)


def normalize_error(msg: str) -> str:
    return _NUM.sub("#", (msg or "").strip().lower())[:200]


class SignalCollector:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub

    @property
    def conn(self):
        return self.hub.store.conn

    # ------------------------------------------------------------------ ghi (API dùng chung, cả B gọi)
    def record(self, agent_id: str, kind: str, evidence: dict[str, Any] | None = None, *, run_id: str | None = None, source: str = "detector",
               task_id: str | None = None, team_id: str | None = None, fingerprint: str | None = None) -> str:
        sid = "sg_" + uuid.uuid4().hex[:12]
        ev = redact(dict(evidence or {}))
        if run_id and (task_id is None or team_id is None):
            r = self.hub.store.get_run(run_id) or {}
            task_id, team_id = task_id or r.get("task_id"), team_id or r.get("team_id")
        self.conn.execute(
            "INSERT INTO signals (id, agent_id, team_id, run_id, task_id, kind, source, evidence, fingerprint, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, agent_id, team_id, run_id, task_id, kind, source, json.dumps(ev, ensure_ascii=False, default=str), fingerprint, now_iso()),
        )
        e = make_event("learning.signal", agent_id, {"signal_id": sid, "kind": kind, "source": source}, run_id=run_id)
        e.task_id, e.team_id = task_id, team_id
        self.hub.publish(e)
        return sid

    def get(self, sid: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["evidence"] = json.loads(d.get("evidence") or "{}")
        return d

    def many(self, ids: list[str]) -> list[dict[str, Any]]:
        return [s for s in (self.get(i) for i in ids) if s is not None]

    def unprocessed(self, agent_id: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT id FROM signals WHERE agent_id=? AND processed_by_review_id IS NULL AND kind NOT IN ('good','approve') "
                                 "ORDER BY created_at DESC LIMIT ?", (agent_id, limit)).fetchall()
        return self.many([r[0] for r in rows])

    def list(self, agent_id: str | None = None, kind: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where, args = [], []
        if agent_id:
            where.append("agent_id=?")
            args.append(agent_id)
        if kind:
            where.append("kind=?")
            args.append(kind)
        sql = "SELECT id FROM signals" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?"
        return self.many([r[0] for r in self.conn.execute(sql, (*args, int(limit)))])

    def mark_processed(self, ids: list[str], review_id: str) -> None:
        for i in ids:
            self.conn.execute("UPDATE signals SET processed_by_review_id=? WHERE id=? AND processed_by_review_id IS NULL", (review_id, i))

    def _open_fp(self, agent_id: str, kind: str, fp: str) -> bool:
        return self.conn.execute("SELECT 1 FROM signals WHERE agent_id=? AND kind=? AND fingerprint=? AND processed_by_review_id IS NULL LIMIT 1",
                                 (agent_id, kind, fp)).fetchone() is not None

    # ------------------------------------------------------------------ bộ phát hiện
    def _correction_rx(self) -> re.Pattern[str] | None:
        rx = self.hub.config.get("learning.steer_correction_regex")
        try:
            return re.compile(rx) if rx else None
        except re.error:
            return None

    def on_event(self, ev: HubEvent) -> None:
        t = ev.type
        if t == "user.message" and ev.payload.get("steer") and ev.agent_id:
            rx = self._correction_rx()
            text = str(ev.payload.get("text") or "")
            if rx and rx.search(text):
                self.record(ev.agent_id, "correction", {"text": text[:2000], "by": ev.payload.get("by"), "channel": ev.payload.get("channel")},
                            run_id=ev.run_id, source="steer")
        elif t == "run.queued" and ev.agent_id and ev.payload.get("resume_reason") == "continue":
            rx = self._correction_rx()
            r = self.hub.store.get_run(ev.run_id or "") or {}
            text = str(r.get("prompt") or ev.payload.get("prompt_preview") or "")
            if rx and rx.search(text):
                self.record(ev.agent_id, "correction", {"text": text[:2000], "channel": "continue"}, run_id=ev.payload.get("parent_run_id") or ev.run_id, source="steer")
        elif t == "run.reverted" and ev.agent_id:
            self.record(ev.agent_id, "undo", {"files": ev.payload.get("files"), "warnings": ev.payload.get("warnings")}, run_id=ev.run_id, source="hub")
        elif t == "workspace.checkpoint" and ev.run_id:
            try:
                self.detect_human_edits(ev.run_id)
            except Exception:  # pragma: no cover - phát hiện là phụ, không làm hỏng run
                pass

    def human_stop(self, run_id: str, channel: str = "cli") -> str | None:
        """Người dừng run đang chạy/chờ (FR-LN-01 `cancel`)."""
        r = self.hub.store.find_run(run_id)
        if r is None or r.get("status") not in ("running", "queued", "waiting_human") or r.get("kind") in ("review", "eval", "eval_judge", "curator"):
            return None
        live = self.hub.aggregator.agents.get(r["agent_id"])
        return self.record(r["agent_id"], "cancel", {"at": now_iso(), "tool": getattr(live, "last_tool", None), "channel": channel, "status": r.get("status")},
                           run_id=r["id"], source=channel.split(":", 1)[0] if channel else "cli")

    def detect_human_edits(self, new_run_id: str) -> list[str]:
        """edit_output: file run TRƯỚC (cùng repo) ghi đã bị sửa trước khi run mới bắt đầu → người sửa output."""
        from ..workspace import modified_since

        new = self.hub.store.get_run(new_run_id) or {}
        key = new.get("workspace_key")
        if not key:
            return []
        prev = self.conn.execute("SELECT id FROM runs WHERE workspace_key=? AND id!=? AND diff IS NOT NULL AND ended_at IS NOT NULL AND reverted_at IS NULL "
                                 "ORDER BY ended_at DESC LIMIT 1", (key, new_run_id)).fetchone()
        if prev is None:
            return []
        p = self.hub.store.get_run(prev[0]) or {}
        diff = p.get("diff") if isinstance(p.get("diff"), dict) else None
        if not diff or p.get("kind") in ("review", "eval"):
            return []
        fp = f"edit:{p['id']}"
        if self._open_fp(p["agent_id"], "edit_output", fp) or self.conn.execute("SELECT 1 FROM signals WHERE fingerprint=?", (fp,)).fetchone():
            return []
        changed = modified_since(diff)
        if not changed:
            return []
        store_dir = self.hub.runs.checkpoint_dir(p["id"])
        parts = []
        for f in diff.get("files") or []:
            if f["path"] not in changed:
                continue
            ab = f.get("after_blob")
            agent_out = (store_dir / "blobs" / ab).read_bytes().decode("utf-8", "replace") if ab and (store_dir / "blobs" / ab).exists() else ""
            cur_p = Path(diff["root"]) / f["path"]
            human = cur_p.read_text(encoding="utf-8", errors="replace") if cur_p.exists() else ""
            parts.append("".join(difflib.unified_diff(agent_out.splitlines(True), human.splitlines(True), f"agent/{f['path']}", f"human/{f['path']}")))
        sid = self.record(p["agent_id"], "edit_output", {"files": changed, "diff": "".join(parts)[:8000], "detected_at_run": new_run_id},
                          run_id=p["id"], source="detector", fingerprint=fp)
        return [sid]

    def on_run_terminal(self, run: dict[str, Any]) -> list[str]:
        """failure + repeat_pattern sau mỗi run thường (không phải review/eval)."""
        if run.get("kind") in ("review", "eval", "eval_judge", "curator"):
            return []
        out: list[str] = []
        agent_id = run["agent_id"]
        events = self.hub.store.query_events(run_id=run["id"], types=["tool.start", "tool.complete"], limit=5000)
        # failure: lỗi tool cùng dấu vân tay ở ≥ N run
        need = int(self.hub.config.get("learning.failure_min_runs") or 2)
        names: dict[str, str] = {}
        for e in events:
            if e.type == "tool.start":
                names[str(e.payload.get("tool_id"))] = str(e.payload.get("name"))
        for e in events:
            if e.type != "tool.complete" or not e.payload.get("is_error"):
                continue
            tool = str(e.payload.get("name") or names.get(str(e.payload.get("tool_id"))) or "?")
            msg = normalize_error(str(e.payload.get("result_preview") or e.payload.get("error") or ""))
            fp = "fail:" + hashlib.sha256(f"{tool}|{msg}".encode()).hexdigest()[:16]
            seen = self.conn.execute("SELECT COUNT(DISTINCT run_id) FROM signals WHERE agent_id=? AND kind='failure_obs' AND fingerprint=?", (agent_id, fp)).fetchone()[0]
            if not self.conn.execute("SELECT 1 FROM signals WHERE agent_id=? AND kind='failure_obs' AND fingerprint=? AND run_id=?", (agent_id, fp, run["id"])).fetchone():
                self.conn.execute("INSERT INTO signals (id, agent_id, run_id, kind, source, evidence, fingerprint, processed_by_review_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                  ("sg_" + uuid.uuid4().hex[:12], agent_id, run["id"], "failure_obs", "detector", json.dumps({"tool": tool, "message": msg}), fp, "-", now_iso()))
                seen += 1
            if seen >= need and not self._open_fp(agent_id, "failure", fp):
                out.append(self.record(agent_id, "failure", {"tool": tool, "message": msg, "runs": seen}, run_id=run["id"], fingerprint=fp))
        # repeat_pattern: n-gram tool qua các run
        n = int(self.hub.config.get("learning.repeat_pattern_n") or 3)
        seq = [str(e.payload.get("name")) for e in events if e.type == "tool.start"]
        grams = {tuple(seq[i : i + n]) for i in range(0, max(0, len(seq) - n + 1))}
        if grams:
            window = int(self.hub.config.get("learning.repeat_pattern_window_runs") or 20)
            recent = [r["id"] for r in self.hub.store.list_runs(agent_id=agent_id, limit=window) if r["id"] != run["id"] and r.get("kind") not in ("review", "eval")]
            counts = {g: 1 for g in grams}
            for rid in recent:
                s = [str(e.payload.get("name")) for e in self.hub.store.query_events(run_id=rid, types=["tool.start"], limit=5000)]
                rg = {tuple(s[i : i + n]) for i in range(0, max(0, len(s) - n + 1))}
                for g in grams & rg:
                    counts[g] += 1
            for g, c in counts.items():
                if c >= n and len(set(g)) > 0:
                    fp = "rep:" + hashlib.sha256("|".join(g).encode()).hexdigest()[:16]
                    if not self._open_fp(agent_id, "repeat_pattern", fp):
                        out.append(self.record(agent_id, "repeat_pattern", {"pattern": list(g), "runs": c}, run_id=run["id"], fingerprint=fp))
        return out
