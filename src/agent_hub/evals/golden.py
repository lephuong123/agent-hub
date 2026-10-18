"""Golden task do NGƯỜI DÙNG quản lý (FR-LN-10, Q-07).

- Nguồn sự thật: bảng `golden_tasks` (daemon là writer duy nhất). Bản sao chỉ đọc cho người xem/diff:
  `HUB_HOME/policy/golden/<agent>.yaml` — nằm trong `policy/` nên thuộc CHÍNH SÁCH AN TOÀN (FR-SG-05): agent không ghi
  được (guard runtime), lệnh `agent-hub golden add|rm|edit|from-run` và tool MCP `hub_golden_*` ghi bị guard coi là
  `policy_change` (forbidden) khi agent tự gọi; vòng học chỉ được GỢI Ý (`golden.suggested`).
- Mỗi task: prompt đầu vào, ngữ cảnh (`context` — chèn trước prompt; `files` — seed vào workspace eval), cách chấm
  (`checker`, xem `checkers.py`), người tạo, ngày, `source_run_id` nếu đánh dấu từ run thật, `version` tăng khi sửa.
- Xoá = xoá mềm (`active=0`, `archived_at`) — khôi phục bằng `golden restore`.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..errors import HubError, NotFound
from ..events import make_event
from ..store import now_iso
from .checkers import validate_checker

if TYPE_CHECKING:
    from ..hub import Hub

JSON_FIELDS = ("checker", "files", "output_schema", "tags")
HUMAN_ACTORS_DENY = ("agent", "learning", "review", "curator", "eval_gate", "regression_monitor")


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
            doc = {"agent_id": agent_id, "note": "BẢN SAO CHỈ ĐỌC — sửa bằng `agent-hub golden …` (nguồn sự thật: hub.db)", "tasks": rows}
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
                "INSERT INTO golden_suggestions (run_id, agent_id, reason, proposal_id, status, created_at, updated_at) VALUES (?,?,?,?, 'open', ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET updated_at=excluded.updated_at, proposal_id=COALESCE(excluded.proposal_id, golden_suggestions.proposal_id)",
                (c["run_id"], agent_id, c.get("reason") or reason, proposal_id, now, now),
            )
        payload = {"reason": reason, "proposal_id": proposal_id, "candidates": cands[:5], "golden_count": len(self.active_for(agent_id)),
                   "hint": f"agent-hub golden from-run <run_id> --regex '…' | --judge '<rubric>'"}
        self.hub.publish(make_event("golden.suggested", agent_id, payload))
        return payload

    def dismiss(self, run_id: str) -> dict[str, Any]:
        now = now_iso()
        self.conn.execute(
            "INSERT INTO golden_suggestions (run_id, agent_id, reason, status, created_at, updated_at) "
            "SELECT id, agent_id, 'dismissed', 'dismissed', ?, ? FROM runs WHERE id=? "
            "ON CONFLICT(run_id) DO UPDATE SET status='dismissed', updated_at=excluded.updated_at", (now, now, run_id))
        return {"run_id": run_id, "status": "dismissed"}
