"""ApprovalRequest cho thao tác phá huỷ (FR-RT-09, FR-SG-04) — luồng B của M1.

Vòng đời: `open → waiting_human → allowed | denied | cancelled` (KHÔNG có `expired`, không tự từ chối).
- `open`: hook/permission tool đang GIỮ tool call chờ người, tối đa `approval.timeout_s` (300 s, Q-04).
- Quá thời gian giữ → `waiting_human`: hook trả deny có thông điệp chờ, hub dừng tiến trình gọn, run `waiting_human`
  (nhả slot đồng thời, GIỮ khoá repo — Q-21), nhắc lại theo `notify.reminder_interval_s`, chờ không giới hạn.
- Người `approve` → nếu hook còn giữ: trả allow, tool chạy NGAY (vé dùng luôn). Nếu run đã tạm dừng: tạo run mới
  `--resume <session>` kèm vé cho phép ĐÚNG MỘT lần (`action_fingerprint`); argv của run resume bỏ đúng các mẫu
  `--disallowedTools` khớp thao tác (spike: hook allow không thắng deny tĩnh).
- Người `deny` → agent nhận từ chối (trực tiếp hoặc qua lượt resume) và làm tiếp cách khác; ghi Signal `reject`.
Mọi quyết định ghi audit (`audit.record`, hợp đồng với luồng A) với `channel`, `wait_s`, `action_fingerprint`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .errors import Conflict, HubError, NotFound
from .events import make_event
from .redact import redact
from .store import now_iso

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger("agent_hub.approvals")

PENDING = ("open", "waiting_human")
FINAL = ("allowed", "denied", "cancelled")


def _parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def audit_record(kind: str, agent_id: str | None, run_id: str | None, data: dict[str, Any]) -> None:
    """Gọi `audit.record` của luồng A; nếu chưa có thì hàm tạm cùng chữ ký (ghi log)."""
    try:
        from . import audit

        if audit.installed() is not None:
            audit.record(kind, agent_id, run_id, data)
            return
    except Exception:  # pragma: no cover - A chưa có module/đổi API
        log.debug("audit.record không dùng được", exc_info=True)
    log.info("AUDIT %s agent=%s run=%s %s", kind, agent_id, run_id, json.dumps(redact(data), ensure_ascii=False, default=str)[:2000])


def action_preview(tool_name: str, tool_input: dict[str, Any] | None, limit: int = 300) -> str:
    """Tóm tắt thao tác ĐÃ CHE cho người xem/Slack. Tool MCP: chỉ tên (không đưa dữ liệu MCP nội bộ ra ngoài)."""
    ti = tool_input or {}
    if tool_name.startswith("mcp__"):
        return tool_name
    if tool_name == "Bash":
        s = str(ti.get("command") or "")
    elif "file_path" in ti or "notebook_path" in ti:
        s = f"{tool_name} {ti.get('file_path') or ti.get('notebook_path')}"
    else:
        s = f"{tool_name} {json.dumps(ti, ensure_ascii=False)[:limit]}"
    s = str(redact(s))
    return s if len(s) <= limit else s[:limit] + "…"


class ApprovalService:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.holds: dict[str, asyncio.Future[str]] = {}
        self._task: asyncio.Task | None = None

    @property
    def store(self):
        return self.hub.store

    @property
    def conn(self):
        return self.hub.store.conn

    # ------------------------------------------------------------------ rows
    def _row(self, r: Any) -> dict[str, Any] | None:
        if r is None:
            return None
        d = dict(r)
        if d.get("tool_input"):
            try:
                d["tool_input"] = json.loads(d["tool_input"])
            except json.JSONDecodeError:
                pass
        d["preview"] = action_preview(d["tool_name"], d["tool_input"] if isinstance(d["tool_input"], dict) else {})
        return d

    def get(self, approval_id: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
        if r is None:
            rows = self.conn.execute("SELECT * FROM approval_requests WHERE id LIKE ? LIMIT 2", (approval_id + "%",)).fetchall()
            r = rows[0] if len(rows) == 1 else None
        if r is None:
            raise NotFound(f"không tìm thấy approval '{approval_id}'")
        return self._row(r)  # type: ignore[return-value]

    def list(self, status: str | None = "pending", *, run_id: str | None = None, agent_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where, args = [], []
        if status == "pending":
            where.append("status IN ('open','waiting_human')")
        elif status and status != "all":
            where.append("status=?")
            args.append(status)
        if run_id:
            where.append("run_id=?")
            args.append(run_id)
        if agent_id:
            where.append("agent_id=?")
            args.append(agent_id)
        sql = "SELECT * FROM approval_requests" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY opened_at DESC LIMIT ?"
        args.append(int(limit))
        return [self._row(r) for r in self.conn.execute(sql, args)]  # type: ignore[misc]

    def _update(self, approval_id: str, data: dict[str, Any]) -> None:
        sets = ", ".join(f"{k}=?" for k in data)
        self.conn.execute(f"UPDATE approval_requests SET {sets} WHERE id=?", (*data.values(), approval_id))

    # ------------------------------------------------------------------ open / hold
    def find_pending(self, run_id: str, fingerprint: str) -> dict[str, Any] | None:
        r = self.conn.execute(
            "SELECT * FROM approval_requests WHERE run_id=? AND action_fingerprint=? AND status IN ('open','waiting_human') ORDER BY opened_at DESC LIMIT 1",
            (run_id, fingerprint),
        ).fetchone()
        return self._row(r)

    def open(
        self,
        run: dict[str, Any],
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str | None,
        fingerprint: str,
        verdict: dict[str, Any],
        channel: str,
    ) -> dict[str, Any]:
        aid = "ap_" + secrets.token_hex(5)
        row = {
            "id": aid,
            "run_id": run["id"],
            "agent_id": run["agent_id"],
            "session_id": run.get("session_id"),
            "kind": "destructive",
            "channel": channel,
            "tool_name": tool_name,
            "tool_input": json.dumps(redact(tool_input), ensure_ascii=False, default=str),
            "tool_use_id": tool_use_id,
            "action_fingerprint": fingerprint,
            "destructive_class": verdict.get("klass"),
            "matched_rule": verdict.get("rule"),
            "risk_level": verdict.get("verdict"),
            "reason": verdict.get("reason"),
            "protected_resource_id": verdict.get("protected_id"),
            "status": "open",
            "opened_at": now_iso(),
        }
        cols = ", ".join(row)
        self.conn.execute(f"INSERT INTO approval_requests ({cols}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
        out = self.get(aid)
        self.hub.publish(
            make_event(
                "approval.open",
                run["agent_id"],
                {
                    "request_id": aid,
                    "kind": "destructive",
                    "tool": tool_name,
                    "preview": out["preview"],
                    "destructive_class": verdict.get("klass"),
                    "matched_rule": verdict.get("rule"),
                    "risk_level": verdict.get("verdict"),
                    "reason": verdict.get("reason"),
                    "protected_resource_id": verdict.get("protected_id"),
                    "timeout_s": self.timeout_s(),
                    "approve_cmd": f"agent-hub approve {aid}",
                },
                run_id=run["id"],
                session_id=run.get("session_id"),
            )
        )
        self.hub.agents.set_status(run["agent_id"], "waiting", current_run_id=run["id"], reason=f"approval {aid}")
        audit_record("approval.open", run["agent_id"], run["id"], {"approval_id": aid, "tool": tool_name, "action_fingerprint": fingerprint, "verdict": verdict, "channel": channel})
        return out

    def timeout_s(self) -> float:
        return float(self.hub.config.get("approval.timeout_s"))

    async def hold(self, approval_id: str, timeout_s: float) -> str:
        """Giữ tới khi người quyết hoặc hết giờ. Trả 'allowed' | 'denied' | 'cancelled' | 'timeout'."""
        row = self.get(approval_id)
        if row["status"] in FINAL:
            return row["status"]
        fut = self.holds.get(approval_id)
        if fut is None or fut.done():
            fut = asyncio.get_running_loop().create_future()
            self.holds[approval_id] = fut
        self.hub.runs.pause_clock(row["run_id"])
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=max(0.0, timeout_s))
        except asyncio.TimeoutError:
            return "timeout"
        finally:
            self.holds.pop(approval_id, None)
            self.hub.runs.resume_clock(row["run_id"])

    def to_waiting_human(self, approval_id: str) -> dict[str, Any]:
        row = self.get(approval_id)
        if row["status"] != "open":
            return row
        self._update(approval_id, {"status": "waiting_human", "waiting_human_at": now_iso()})
        self.hub.publish(
            make_event(
                "approval.waiting_human",
                row["agent_id"],
                {
                    "request_id": approval_id,
                    "run_id": row["run_id"],
                    "since": row["opened_at"],
                    "preview": row["preview"],
                    "lease_held": True,
                    "repo_lock_held": True,
                    "approve_cmd": f"agent-hub approve {approval_id}",
                },
                run_id=row["run_id"],
                session_id=row.get("session_id"),
            )
        )
        return self.get(approval_id)

    # ------------------------------------------------------------------ tickets
    def consume_ticket(self, session_id: str | None, fingerprint: str, run_id: str) -> dict[str, Any] | None:
        """Vé cho phép một lần: approval `allowed` chưa dùng, cùng session + đúng dấu vân tay → đánh dấu đã dùng."""
        if not session_id:
            return None
        r = self.conn.execute(
            "SELECT * FROM approval_requests WHERE session_id=? AND action_fingerprint=? AND status='allowed' AND ticket_used_at IS NULL ORDER BY resolved_at LIMIT 1",
            (session_id, fingerprint),
        ).fetchone()
        if r is None:
            return None
        self._update(r["id"], {"ticket_used_at": now_iso(), "ticket_run_id": run_id})
        return self._row(r)

    def unused_tickets(self, session_id: str | None) -> list[dict[str, Any]]:
        if not session_id:
            return []
        rows = self.conn.execute(
            "SELECT * FROM approval_requests WHERE session_id=? AND status='allowed' AND ticket_used_at IS NULL", (session_id,)
        ).fetchall()
        return [self._row(r) for r in rows]  # type: ignore[misc]

    # ------------------------------------------------------------------ resolve
    def resolve(self, approval_id: str, decision: str, *, by: str = "user", channel: str = "cli", note: str | None = None) -> dict[str, Any]:
        if decision in ("approve", "allow"):
            decision = "allowed"
        elif decision in ("deny", "reject"):
            decision = "denied"
        if decision not in FINAL:
            raise HubError("decision phải là allow|deny|cancel")
        row = self.get(approval_id)
        if row["status"] in FINAL:
            raise Conflict(f"approval {row['id']} đã {row['status']} (bởi {row.get('resolved_by')} qua {row.get('resolved_channel')})")
        aid = row["id"]
        opened = _parse_ts(row["opened_at"]) or time.time()
        wait_s = round(time.time() - opened, 3)
        self._update(aid, {"status": decision, "resolved_at": now_iso(), "resolved_by": by, "resolved_channel": channel, "wait_s": wait_s, "note": note})
        self.hub.publish(
            make_event(
                "approval.resolved",
                row["agent_id"],
                {"request_id": aid, "decision": decision, "by": by, "channel": channel, "wait_s": wait_s, "note": note, "tool": row["tool_name"], "preview": row["preview"]},
                run_id=row["run_id"],
                session_id=row.get("session_id"),
            )
        )
        audit_record(
            "approval.resolved",
            row["agent_id"],
            row["run_id"],
            {"approval_id": aid, "decision": decision, "actor": by, "channel": channel, "wait_s": wait_s, "action_fingerprint": row["action_fingerprint"], "tool": row["tool_name"], "note": note},
        )
        if decision == "denied":
            self._signal_reject(row, note)
        fut = self.holds.get(aid)
        if fut is not None and not fut.done():
            fut.set_result(decision)
        else:
            self.hub.runs.on_approval_resolved(row["run_id"])
        if decision != "allowed" or fut is None:
            self._refresh_agent_status(row["agent_id"], row["run_id"])
        return self.get(aid)

    def _refresh_agent_status(self, agent_id: str, run_id: str) -> None:
        pending = self.conn.execute("SELECT COUNT(*) FROM approval_requests WHERE agent_id=? AND status IN ('open','waiting_human')", (agent_id,)).fetchone()[0]
        if pending:
            return
        active = [a for a in self.hub.runs.active.values() if a.agent_id == agent_id and a.handle is not None]
        if active:
            self.hub.agents.set_status(agent_id, "working", current_run_id=active[0].run_id, reason="approval resolved")

    def _signal_reject(self, row: dict[str, Any], note: str | None) -> None:
        """Signal `reject` (FR-RT-09) — bảng `signals` của luồng A (nếu đã có)."""
        try:
            sid = "sg_" + secrets.token_hex(6)
            self.conn.execute(
                "INSERT INTO signals (id, agent_id, run_id, kind, source, evidence, created_at) VALUES (?,?,?,?,?,?,?)",
                (sid, row["agent_id"], row["run_id"], "reject", "hub", json.dumps({"approval_id": row["id"], "tool": row["tool_name"], "note": note}, ensure_ascii=False), now_iso()),
            )
            # M4-A: báo vòng học (trigger review — FR-LN-02)
            self.hub.publish(make_event("learning.signal", row["agent_id"], {"signal_id": sid, "kind": "reject", "source": "approval"}, run_id=row["run_id"]))
        except Exception:
            log.debug("chưa ghi được signal reject", exc_info=True)

    def cancel_for_run(self, run_id: str, reason: str) -> int:
        rows = self.list("pending", run_id=run_id)
        for r in rows:
            try:
                self.resolve(r["id"], "cancelled", by="hub", channel="runtime", note=reason)
            except Conflict:
                pass
        return len(rows)

    def pending_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self.list("pending", run_id=run_id)

    def all_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self.list("all", run_id=run_id)

    # ------------------------------------------------------------------ reminders (FR-SG-04 bước 3, FR-SG-09)
    def remind_due(self, now: float | None = None) -> list[str]:
        now = now or time.time()
        base = float(self.hub.config.get("notify.reminder_interval_s"))
        cap = float(self.hub.config.get("notify.reminder_max_interval_s"))
        out = []
        for r in self.list("pending", limit=1000):
            last = _parse_ts(r.get("last_reminded_at")) or _parse_ts(r["opened_at"]) or now
            interval = min(base * (2 ** int(r.get("reminder_count") or 0)), max(base, cap))
            if now - last < interval:
                continue
            n = int(r.get("reminder_count") or 0) + 1
            self._update(r["id"], {"reminder_count": n, "last_reminded_at": now_iso()})
            wait_s = round(now - (_parse_ts(r["opened_at"]) or now), 1)
            self.hub.publish(
                make_event(
                    "approval.reminder",
                    r["agent_id"],
                    {"request_id": r["id"], "reminder_count": n, "wait_s": wait_s, "status": r["status"], "preview": r["preview"], "reason": r.get("reason"), "approve_cmd": f"agent-hub approve {r['id']}"},
                    run_id=r["run_id"],
                    session_id=r.get("session_id"),
                )
            )
            out.append(r["id"])
        return out

    async def loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(float(self.hub.config.get("approval.reminder_check_s")))
                self.remind_due()
                from .recovery import purge_expired

                purge_expired(self.hub)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("vòng nhắc approval lỗi")
