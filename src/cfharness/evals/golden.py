"""Golden task do NGƯỜI DÙNG quản lý (FR-LN-10, Q-07).

- Nguồn sự thật: bảng `golden_tasks` (daemon là writer duy nhất). Bản sao chỉ đọc cho người xem/diff:
  `HUB_HOME/policy/golden/<agent>.yaml` — nằm trong `policy/` nên thuộc CHÍNH SÁCH AN TOÀN (FR-SG-05): agent không ghi
  được (guard runtime), lệnh `cfharness golden add|rm|edit|from-run` và tool MCP `hub_golden_*` ghi bị guard coi là
  `policy_change` (forbidden) khi agent tự gọi; vòng học chỉ được GỢI Ý (`golden.suggested`).
- Mỗi task: prompt đầu vào, ngữ cảnh (`context` — chèn trước prompt; `files` — seed vào workspace eval), cách chấm
  (`checker`, xem `checkers.py`), người tạo, ngày, `source_run_id` nếu đánh dấu từ run thật, `version` tăng khi sửa.
- Xoá = xoá mềm (`active=0`, `archived_at`) — khôi phục bằng `golden restore`.
- Tự ĐỀ XUẤT (docs/dev-notes/golden-suggestions.md): `auto_suggest()` (vòng nền M4-B) quét task `done` đủ lâu mà người không sửa
  (Signal correction/edit_output/reject/cancel), không hoàn tác (`undo`, `run.reverted`) và không tiếp quản → bản nháp trong
  `golden_suggestions` (`gs_…`) + event `golden.suggestion_opened` (nút một chạm Telegram/Slack). Lọc: chống trùng theo hash prompt,
  đủ tín hiệu (prompt/kết quả đủ dài, run completed), không secret. CHỈ NGƯỜI quyết (`decide_suggestion`, `_require_human`, RPC
  HUMAN_ONLY): duyệt → `add()` (golden thật) + đánh thức proposal `deferred_no_eval`; từ chối → `dismissed` (không gợi ý lại).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..errors import Conflict, HubError, NotFound
from ..events import iso, make_event
from ..redact import redact_str
from ..store import now_iso
from .checkers import validate_checker

if TYPE_CHECKING:
    from ..hub import Hub

JSON_FIELDS = ("checker", "files", "output_schema", "tags")
HUMAN_ACTORS_DENY = ("agent", "learning", "review", "curator", "eval_gate", "regression_monitor")
# người đã can thiệp vào kết quả → task KHÔNG đủ "tốt" để làm golden (BRD FR-LN-01)
HUMAN_TOUCH_SIGNALS = ("correction", "edit_output", "undo", "reject", "cancel")
SUGGEST_DEFAULTS: dict[str, Any] = {
    "enabled": True, "every_min": 60, "min_age_h": 6, "lookback_days": 14, "max_open_per_agent": 3, "max_per_scan": 10,
    "min_prompt_chars": 20, "min_result_chars": 20,
}
RUBRIC = ("The output must accomplish the task above. The human accepted the reference result below without editing or "
          "reverting it; pass if the output reaches the same result (wording may differ), fail if it misses or contradicts it.\n"
          "Reference result:\n{ref}")


def prompt_hash(text: str) -> str:
    """Hash prompt đã chuẩn hoá (chữ thường, gộp khoảng trắng) — chống trùng golden/đề xuất."""
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


def has_secret(*texts: Any) -> bool:
    """Có secret (mẫu `redact` hoặc giá trị secret hub biết) → không đề xuất (prompt golden đi vào run eval + DB + chat)."""
    return any(isinstance(t, str) and t and redact_str(t) != t for t in texts)


def _sid() -> str:
    return "gs_" + uuid.uuid4().hex[:10]


def _srow(r: Any) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    if d.get("checker"):
        try:
            d["checker"] = json.loads(d["checker"])
        except ValueError:
            d["checker"] = None
    return d


def _row(r: Any) -> dict[str, Any] | None:
    if r is None:
        return None
    d = dict(r)
    for k in JSON_FIELDS:
        if d.get(k) is not None:
            d[k] = json.loads(d[k])
    d["active"] = bool(d.get("active"))
    return d


class GoldenStore:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub

    @property
    def conn(self):
        return self.hub.store.conn

    # ------------------------------------------------------------------ guard
    @staticmethod
    def _require_human(created_by: str | None) -> str:
        who = (created_by or "user").strip() or "user"
        if who.split(":")[0] in HUMAN_ACTORS_DENY:
            raise HubError(f"golden task thuộc chính sách an toàn: '{who}' không được tạo/sửa/xoá (chỉ người — Q-07). "
                           "Vòng học chỉ được gợi ý (golden.suggested).", code=-32003)
        return who

    # ------------------------------------------------------------------ read
    def get(self, ref: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM golden_tasks WHERE id=?", (ref,)).fetchone()
        if r is None:
            rows = self.conn.execute("SELECT * FROM golden_tasks WHERE id LIKE ?", (ref + "%",)).fetchall()
            if len(rows) != 1:
                raise NotFound(f"không tìm thấy golden task '{ref}'" + (" (tiền tố trùng nhiều task)" if rows else ""))
            r = rows[0]
        return _row(r)  # type: ignore[return-value]

    def list(self, agent_id: str | None = None, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        where, args = [], []
        if agent_id:
            where.append("agent_id=?")
            args.append(agent_id)
        if not include_inactive:
            where.append("active=1")
        sql = "SELECT * FROM golden_tasks" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY agent_id, created_at"
        return [_row(r) for r in self.conn.execute(sql, args)]  # type: ignore[misc]

    def active_for(self, agent_id: str) -> list[dict[str, Any]]:
        return self.list(agent_id)

    def counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute("SELECT agent_id, COUNT(*) FROM golden_tasks WHERE active=1 GROUP BY agent_id")}

    # ------------------------------------------------------------------ write (chỉ người)
    def add(self, data: dict[str, Any], *, created_by: str = "user") -> dict[str, Any]:
        who = self._require_human(created_by)
        agent_id = data.get("agent_id") or data.get("agent")
        if not agent_id:
            raise HubError("thiếu 'agent_id'")
        self.hub.agents.get_row(agent_id)
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            raise HubError("golden task cần 'prompt' (đầu vào cho agent)")
        checker = validate_checker(data.get("checker"))
        files = data.get("files") or None
        if files is not None and (not isinstance(files, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())):
            raise HubError("'files' phải là mapping đường dẫn tương đối → nội dung")
        for k in files or {}:
            if k.startswith("/") or ".." in Path(k).parts:
                raise HubError(f"'files': đường dẫn phải tương đối, không '..': {k}")
        gid = "g_" + uuid.uuid4().hex[:10]
        now = now_iso()
        row = {
            "id": gid, "agent_id": agent_id, "title": (data.get("title") or prompt.splitlines()[0])[:120], "prompt": prompt,
            "context": data.get("context") or None, "files": json.dumps(files, ensure_ascii=False) if files else None,
            "checker": json.dumps(checker, ensure_ascii=False), "output_schema": json.dumps(data["output_schema"], ensure_ascii=False) if data.get("output_schema") else None,
            "tags": json.dumps(data.get("tags") or [], ensure_ascii=False), "source_run_id": data.get("source_run_id"), "created_by": who,
            "created_at": now, "updated_at": now, "active": 1, "version": 1,
        }
        cols = ", ".join(row)
        self.conn.execute(f"INSERT INTO golden_tasks ({cols}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
        if data.get("source_run_id"):
            self.conn.execute("UPDATE golden_suggestions SET status='accepted', updated_at=? WHERE run_id=?", (now, data["source_run_id"]))
        self._mirror(agent_id)
        self.hub.publish(make_event("golden.added", agent_id, {"golden_task_id": gid, "title": row["title"], "checker": checker["kind"], "by": who, "source_run_id": row["source_run_id"]}))
        return self.get(gid)

    def from_run(self, run_ref: str, checker: dict[str, Any], *, title: str | None = None, prompt: str | None = None,
                 context: str | None = None, created_by: str = "user") -> dict[str, Any]:
        """Đánh dấu một run thật làm golden: lấy prompt gốc của run (sửa được), người chọn cách chấm."""
        run = self.hub.store.find_run(run_ref)
        if run is None:
            raise NotFound(f"không tìm thấy run '{run_ref}'")
        if run.get("kind") not in (None, "adhoc", "task"):
            raise HubError(f"run {run['id']} là run {run.get('kind')} — chỉ đánh dấu run adhoc/task làm golden")
        p = prompt or run.get("prompt") or ""
        return self.add({"agent_id": run["agent_id"], "prompt": p, "context": context, "checker": checker, "title": title, "source_run_id": run["id"]}, created_by=created_by)

    def update(self, ref: str, changes: dict[str, Any], *, created_by: str = "user") -> dict[str, Any]:
        who = self._require_human(created_by)
        g = self.get(ref)
        allowed = {"title", "prompt", "context", "checker", "files", "output_schema", "tags", "active"}
        bad = [k for k in changes if k not in allowed]
        if bad:
            raise HubError(f"không sửa được trường {bad}; được sửa: {sorted(allowed)}")
        upd: dict[str, Any] = {}
        for k, v in changes.items():
            if k == "checker":
                v = json.dumps(validate_checker(v), ensure_ascii=False)
            elif k in JSON_FIELDS:
                v = json.dumps(v, ensure_ascii=False) if v is not None else None
            elif k == "active":
                v = 1 if v else 0
            upd[k] = v
        if not upd:
            return g
        upd["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in upd)
        self.conn.execute(f"UPDATE golden_tasks SET {sets}, version=version+1 WHERE id=?", (*upd.values(), g["id"]))
        self._mirror(g["agent_id"])
        self.hub.publish(make_event("golden.updated", g["agent_id"], {"golden_task_id": g["id"], "fields": sorted(changes), "by": who}))
        return self.get(g["id"])

    def remove(self, ref: str, *, created_by: str = "user") -> dict[str, Any]:
        who = self._require_human(created_by)
        g = self.get(ref)
        now = now_iso()
        self.conn.execute("UPDATE golden_tasks SET active=0, archived_at=?, updated_at=? WHERE id=?", (now, now, g["id"]))
        self._mirror(g["agent_id"])
        self.hub.publish(make_event("golden.removed", g["agent_id"], {"golden_task_id": g["id"], "by": who, "soft": True}))
        return self.get(g["id"])

    def restore(self, ref: str, *, created_by: str = "user") -> dict[str, Any]:
        who = self._require_human(created_by)
        g = self.get(ref)
        self.conn.execute("UPDATE golden_tasks SET active=1, archived_at=NULL, updated_at=? WHERE id=?", (now_iso(), g["id"]))
        self._mirror(g["agent_id"])
        self.hub.publish(make_event("golden.updated", g["agent_id"], {"golden_task_id": g["id"], "fields": ["active"], "by": who}))
        return self.get(g["id"])

    def _mirror(self, agent_id: str) -> None:
        """Bản sao chỉ đọc trong `policy/golden/` (người xem/diff; thuộc chính sách an toàn)."""
        d = self.hub.paths.home / "policy" / "golden"
        try:
            d.mkdir(parents=True, exist_ok=True)
            rows = self.list(agent_id, include_inactive=True)
            doc = {"agent_id": agent_id, "note": "BẢN SAO CHỈ ĐỌC — sửa bằng `cfharness golden …` (nguồn sự thật: hub.db)", "tasks": rows}
            (d / f"{agent_id}.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
        except OSError:  # pragma: no cover
            pass

    # ------------------------------------------------------------------ gợi ý (agent/vòng học CHỈ được gợi ý)
    def candidates(self, agent_id: str, *, limit: int = 5, days: int = 14) -> list[dict[str, Any]]:
        """Run ứng viên làm golden: run có Signal `good`/`approve` trước, sau đó run hoàn tất gần đây; bỏ run đã là golden."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        used = {r[0] for r in self.conn.execute("SELECT source_run_id FROM golden_tasks WHERE source_run_id IS NOT NULL")}
        dismissed = {r[0] for r in self.conn.execute("SELECT run_id FROM golden_suggestions WHERE status='dismissed'")}
        good = [
            r[0] for r in self.conn.execute(
                "SELECT DISTINCT s.run_id FROM signals s JOIN runs r ON r.id=s.run_id WHERE s.agent_id=? AND s.kind IN ('good','approve') "
                "AND r.status='completed' AND s.created_at>=? ORDER BY s.created_at DESC", (agent_id, since))
        ]
        recent = [
            r[0] for r in self.conn.execute(
                "SELECT id FROM runs WHERE agent_id=? AND status='completed' AND kind IN ('adhoc','task') AND queued_at>=? "
                "ORDER BY queued_at DESC LIMIT 50", (agent_id, since))
        ]
        out: list[dict[str, Any]] = []
        for rid in list(dict.fromkeys(good + recent)):
            if rid in used or rid in dismissed or not rid:
                continue
            run = self.hub.store.get_run(rid) or {}
            out.append({"run_id": rid, "reason": "good" if rid in good else "completed", "prompt_preview": (run.get("prompt") or "")[:160],
                        "summary_preview": (run.get("summary") or "")[:160], "queued_at": run.get("queued_at")})
            if len(out) >= limit:
                break
        return out

    def suggest(self, agent_id: str, *, reason: str, proposal_id: str | None = None, run_ids: list[str] | None = None) -> dict[str, Any]:
        """Phát `golden.suggested` (vào digest + Slack). Không tạo golden task."""
        cands = self.candidates(agent_id)
        if run_ids:
            extra = [{"run_id": r, "reason": reason} for r in run_ids if r not in {c["run_id"] for c in cands}]
            cands = extra + cands
        now = now_iso()
        for c in cands:
            self.conn.execute(
                "INSERT INTO golden_suggestions (id, run_id, agent_id, reason, proposal_id, status, created_at, updated_at) VALUES (?,?,?,?,?, 'open', ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET updated_at=excluded.updated_at, proposal_id=COALESCE(excluded.proposal_id, golden_suggestions.proposal_id)",
                (_sid(), c["run_id"], agent_id, c.get("reason") or reason, proposal_id, now, now),
            )
        payload = {"reason": reason, "proposal_id": proposal_id, "candidates": cands[:5], "golden_count": len(self.active_for(agent_id)),
                   "hint": f"cfharness golden from-run <run_id> --regex '…' | --judge '<rubric>'"}
        self.hub.publish(make_event("golden.suggested", agent_id, payload))
        return payload

    def dismiss(self, run_id: str) -> dict[str, Any]:
        now = now_iso()
        self.conn.execute(
            "INSERT INTO golden_suggestions (id, run_id, agent_id, reason, status, created_at, updated_at) "
            "SELECT ?, id, agent_id, 'dismissed', 'dismissed', ?, ? FROM runs WHERE id=? "
            "ON CONFLICT(run_id) DO UPDATE SET status='dismissed', updated_at=excluded.updated_at", (_sid(), now, now, run_id))
        return {"run_id": run_id, "status": "dismissed"}

    # ------------------------------------------------------------------ tự đề xuất từ task done (người duyệt một chạm)
    def suggest_cfg(self) -> dict[str, Any]:
        return {**SUGGEST_DEFAULTS, **((self.hub.config.get("eval") or {}).get("golden_suggest") or {})}

    def suggestion(self, ref: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM golden_suggestions WHERE id=? OR run_id=?", (ref, ref)).fetchone()
        if r is None:
            pat = re.sub(r"[!%_]", lambda m: "!" + m.group(0), ref) + "%"  # `%`/`_` trong ref là chữ, không phải wildcard
            rows = self.conn.execute("SELECT * FROM golden_suggestions WHERE id LIKE ? ESCAPE '!'", (pat,)).fetchall() if len(ref) >= 4 else []
            if len(rows) != 1:
                raise NotFound(f"không tìm thấy đề xuất golden '{ref}'" + (" (tiền tố trùng nhiều đề xuất)" if rows else ""))
            r = rows[0]
        return _srow(r)  # type: ignore[return-value]

    def suggestions(self, agent_id: str | None = None, status: str | None = "open", limit: int = 50) -> list[dict[str, Any]]:
        where, args = [], []
        if agent_id:
            where.append("agent_id=?")
            args.append(agent_id)
        if status and status != "all":
            where.append("status=?")
            args.append(status)
        sql = "SELECT * FROM golden_suggestions" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC LIMIT ?"
        return [_srow(r) for r in self.conn.execute(sql, (*args, int(limit)))]  # type: ignore[misc]

    def _human_touched(self, task_id: str | None, run_ids: list[str]) -> bool:
        """Người đã sửa (Signal correction/edit_output/reject/cancel), hoàn tác (`undo`, `run.reverted`), tiếp quản, steer run
        (mọi tin steer của người, kể cả không khớp regex correction) hay bình luận vào task."""
        kinds = ",".join("?" * len(HUMAN_TOUCH_SIGNALS))
        marks = ",".join("?" * len(run_ids)) or "''"
        if self.conn.execute(f"SELECT 1 FROM signals WHERE kind IN ({kinds}) AND (task_id=? OR run_id IN ({marks})) LIMIT 1",
                             (*HUMAN_TOUCH_SIGNALS, task_id, *run_ids)).fetchone():
            return True
        if task_id and self.conn.execute("SELECT 1 FROM task_comments WHERE task_id=? AND kind='comment' AND author NOT LIKE 'agent%' LIMIT 1",
                                         (task_id,)).fetchone():
            return True
        return bool(run_ids) and self.conn.execute(
            f"SELECT 1 FROM events WHERE run_id IN ({marks}) AND (type IN ('run.reverted','run.takeover') OR (type='user.message' "
            "AND json_extract(payload,'$.steer') AND COALESCE(json_extract(payload,'$.by'),'user') NOT IN ('hub') "
            "AND COALESCE(json_extract(payload,'$.by'),'user') NOT LIKE 'agent%')) LIMIT 1", tuple(run_ids)).fetchone() is not None

    def _suggestion_touched(self, s: dict[str, Any]) -> bool:
        task_id = s.get("task_id") or (self.hub.store.get_run(s["run_id"]) or {}).get("task_id")
        run_ids = [r[0] for r in self.conn.execute("SELECT id FROM runs WHERE task_id=?", (task_id,))] if task_id else []
        return self._human_touched(task_id, list(dict.fromkeys([s["run_id"], *run_ids])))

    def _auto_dismiss(self, s: dict[str, Any], why: str) -> None:
        """Người chạm vào task/run SAU khi đề xuất mở (edit_output/undo tới muộn) → tự bỏ, không cần người bấm."""
        now = now_iso()
        self.conn.execute("UPDATE golden_suggestions SET status='dismissed', decided_by=?, decided_at=?, updated_at=? WHERE id=? AND status='open'",
                          (f"auto:{why}", now, now, s["id"]))
        self.hub.publish(make_event("golden.suggestion_decided", s["agent_id"],
                                    {"suggestion_id": s["id"], "decision": "reject", "by": f"auto:{why}", "channel": None, "golden_task_id": None},
                                    run_id=s["run_id"]))

    def _draft(self, t: dict[str, Any], c: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """Task done → (bản nháp, "ok") hoặc (None, lý do bỏ). Chỉ đọc."""
        title = str(t.get("title") or "").strip()
        body = str(t.get("body") or "").strip()
        prompt = title if not body else f"{title}\n\n{body}"
        result = str(t.get("result_summary") or "").strip()
        if t.get("kind") == "root" or not t.get("assignee_agent_id"):
            return None, "not_leaf"
        if len(prompt) < int(c["min_prompt_chars"]) or len(result) < int(c["min_result_chars"]):
            return None, "weak_signal"
        if has_secret(prompt, result):
            return None, "secret"
        runs = [dict(r) for r in self.conn.execute("SELECT id, status, kind FROM runs WHERE task_id=? ORDER BY queued_at", (t["id"],))]
        work = [r for r in runs if r.get("kind") in (None, "adhoc", "task")]
        if not work or work[-1]["status"] != "completed":
            return None, "no_completed_run"
        if self._human_touched(t["id"], [r["id"] for r in runs]):
            return None, "human_touched"
        checker = validate_checker({"kind": "llm_judge", "rubric": RUBRIC.format(ref=result[:1500])})
        return {"run_id": work[-1]["id"], "task_id": t["id"], "agent_id": t["assignee_agent_id"], "title": title[:120], "prompt": prompt,
                "checker": checker, "prompt_hash": prompt_hash(prompt)}, "ok"

    def auto_suggest(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Quét task `done` (đủ `min_age_h` để người kịp sửa/hoàn tác) → đề xuất. Hub (daemon) gọi; KHÔNG tạo golden task."""
        c = self.suggest_cfg()
        out: dict[str, Any] = {"suggested": [], "skipped": {}}
        if not c.get("enabled", True):
            return out
        now = now or datetime.now(timezone.utc)
        upto = iso(now - timedelta(hours=float(c["min_age_h"])))
        since = iso(now - timedelta(days=float(c["lookback_days"])))
        for o in self.suggestions(status="open", limit=500):
            if self._suggestion_touched(o):
                self._auto_dismiss(o, "human_touched")
                out["skipped"]["human_touched_after_open"] = out["skipped"].get("human_touched_after_open", 0) + 1
        seen_tasks = {r[0] for r in self.conn.execute("SELECT task_id FROM golden_suggestions WHERE task_id IS NOT NULL")}
        hashes = {r[0] for r in self.conn.execute("SELECT prompt_hash FROM golden_suggestions WHERE prompt_hash IS NOT NULL")}
        hashes |= {prompt_hash(r[0]) for r in self.conn.execute("SELECT prompt FROM golden_tasks")}
        used_runs = {r[0] for r in self.conn.execute("SELECT run_id FROM golden_suggestions")}
        used_runs |= {r[0] for r in self.conn.execute("SELECT source_run_id FROM golden_tasks WHERE source_run_id IS NOT NULL")}
        open_n = {r[0]: r[1] for r in self.conn.execute("SELECT agent_id, COUNT(*) FROM golden_suggestions WHERE status='open' GROUP BY agent_id")}
        rows = self.conn.execute("SELECT * FROM tasks WHERE status='done' AND deleted_at IS NULL AND done_at IS NOT NULL AND done_at<=? AND done_at>=? "
                                 "ORDER BY done_at DESC", (upto, since)).fetchall()
        for r in rows:
            if len(out["suggested"]) >= int(c["max_per_scan"]):
                break
            t = dict(r)
            if t["id"] in seen_tasks:
                continue
            d, why = self._draft(t, c)
            if d is not None and (d["prompt_hash"] in hashes or d["run_id"] in used_runs):
                d, why = None, "duplicate"
            if d is not None and open_n.get(d["agent_id"], 0) >= int(c["max_open_per_agent"]):
                d, why = None, "agent_cap"
            if d is not None and self.conn.execute("SELECT 1 FROM agents WHERE id=? AND deleted_at IS NULL", (d["agent_id"],)).fetchone() is None:
                d, why = None, "agent_gone"
            if d is None:
                out["skipped"][why] = out["skipped"].get(why, 0) + 1
                continue
            sid, ts = _sid(), now_iso()
            self.conn.execute(
                "INSERT INTO golden_suggestions (id, run_id, agent_id, reason, status, task_id, title, prompt, checker, prompt_hash, created_at, updated_at) "
                "VALUES (?,?,?, 'task_done', 'open', ?,?,?,?,?,?,?)",
                (sid, d["run_id"], d["agent_id"], d["task_id"], d["title"], d["prompt"], json.dumps(d["checker"], ensure_ascii=False), d["prompt_hash"], ts, ts))
            hashes.add(d["prompt_hash"])
            open_n[d["agent_id"]] = open_n.get(d["agent_id"], 0) + 1
            out["suggested"].append(sid)
            ev = make_event("golden.suggestion_opened", d["agent_id"], {"suggestion_id": sid, "task_id": d["task_id"], "title": d["title"],
                                                                        "golden_count": len(self.active_for(d["agent_id"]))}, run_id=d["run_id"])
            ev.task_id = d["task_id"]
            self.hub.publish(ev)
        return out

    def _suggestion_draft(self, s: dict[str, Any]) -> dict[str, Any]:
        """Đề xuất của gate (theo run, chưa có nháp) → dựng nháp từ run lúc duyệt, cùng bộ lọc với `auto_suggest`
        (run làm việc completed, đủ `min_age_h`, secret). Người chạm đã được `decide_suggestion` kiểm trước."""
        if s.get("prompt") and s.get("checker"):
            return {"prompt": s["prompt"], "checker": s["checker"], "title": s.get("title")}
        run = self.hub.store.get_run(s["run_id"]) or {}
        if run.get("status") != "completed" or run.get("kind") not in (None, "adhoc", "task"):
            raise HubError(f"đề xuất {s['id']}: run {s['run_id']} không phải run làm việc completed — không dựng golden")
        upto = iso(datetime.now(timezone.utc) - timedelta(hours=float(self.suggest_cfg()["min_age_h"])))
        if str(run.get("ended_at") or run.get("queued_at") or "9") > upto:
            raise HubError(f"đề xuất {s['id']}: run {s['run_id']} mới xong — chờ đủ {self.suggest_cfg()['min_age_h']} h để người kịp sửa/hoàn tác")
        prompt = str(run.get("prompt") or "").strip()
        ref = str(run.get("summary") or "").strip()
        if not prompt or len(ref) < 20:
            raise HubError(f"đề xuất {s['id']}: run {s['run_id']} thiếu prompt/kết quả để dựng golden — dùng `cfharness golden from-run {s['run_id']} …`")
        if has_secret(prompt, ref):
            raise HubError(f"đề xuất {s['id']}: run chứa secret — không dựng golden tự động")
        return {"prompt": prompt, "checker": validate_checker({"kind": "llm_judge", "rubric": RUBRIC.format(ref=ref[:1500])}), "title": None}

    def decide_suggestion(self, ref: str, decision: str, *, created_by: str = "user") -> dict[str, Any]:
        """CHỈ NGƯỜI (Q-07): `approve` → tạo golden task từ nháp; `reject` → không gợi ý lại. Mỗi quyết định vào audit."""
        who = self._require_human(created_by)
        if decision not in ("approve", "reject"):
            raise HubError("decision phải là 'approve' hoặc 'reject'")
        s = self.suggestion(ref)
        if s["status"] != "open":
            raise Conflict(f"đề xuất {s['id']} đã {s['status']}" + (f" (bởi {s['decided_by']})" if s.get("decided_by") else ""))
        ch = who.split(":", 1)[1] if ":" in who else "cli"
        now = now_iso()
        gid = None
        if decision == "approve":
            if self._suggestion_touched(s):  # edit_output/undo có thể tới sau lượt quét
                self._auto_dismiss(s, "human_touched")
                raise Conflict(f"đề xuất {s['id']}: người đã sửa/hoàn tác/steer/bình luận task hoặc run sau khi đề xuất — đã tự bỏ")
            d = self._suggestion_draft(s)
            gid = self.add({"agent_id": s["agent_id"], "prompt": d["prompt"], "checker": d["checker"], "title": d.get("title"),
                            "source_run_id": s["run_id"], "tags": ["auto_suggested"]}, created_by=who)["id"]
            # L3 đang hoãn vì thiếu golden (Q-23) → thử lại ở tick kế tiếp thay vì chờ tới 1 giờ
            self.conn.execute("UPDATE proposals SET not_before=? WHERE status='deferred_no_eval' AND (agent_id=? OR agent_id IS NULL)", (now, s["agent_id"]))
        status = "accepted" if decision == "approve" else "dismissed"
        self.conn.execute("UPDATE golden_suggestions SET status=?, decided_by=?, decided_channel=?, decided_at=?, golden_task_id=?, updated_at=? WHERE id=?",
                          (status, who, ch, now, gid, now, s["id"]))
        audit = getattr(self.hub, "audit", None)
        if audit is not None:
            audit.record("golden.suggestion_decided", s["agent_id"], s["run_id"],
                         {"suggestion_id": s["id"], "decision": decision, "golden_task_id": gid, "task_id": s.get("task_id")},
                         actor=who, channel=ch, target=s["id"], summary=f"{decision} đề xuất golden {s['id']}")
        self.hub.publish(make_event("golden.suggestion_decided", s["agent_id"],
                                    {"suggestion_id": s["id"], "decision": decision, "by": who, "channel": ch, "golden_task_id": gid}, run_id=s["run_id"]))
        return self.suggestion(s["id"])
