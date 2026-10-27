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

Hồ sơ phê duyệt (docs/dev-notes/approval-profiles.md):
- `routed_to` (hợp đồng với web/chat): "manager" = giao điều phối viên (lớp người cho phép, ngoài danh sách "không bao giờ"),
  "human" = người, NULL = approval cũ. Giao → event `approval.delegated` + đánh thức điều phối viên; không có thông báo người
  (Telegram/Slack) cho tới khi chuyển lên người. Điều phối viên quyết qua `manager_decide` (allow/deny/escalate, kèm lý do, audit
  `manager:<agent>`); quá `approval.delegate_to_manager.timeout_s`, điều phối viên không sẵn sàng, hoặc từ chối quyết → `routed_to=human`
  + `approval.escalated` (thông báo người như approval.open). Người quyết được BẤT KỲ lúc nào.
- `timeout_action=deny_and_continue` (opt-in theo agent): hết giờ giữ → agent nhận từ chối, run chạy tiếp, approval vẫn chờ người;
  người duyệt sau → resume session với vé một lần (run đã kết thúc) hoặc nhắn vào run đang chạy (vé dùng khi agent làm lại đúng thao tác).
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

log = logging.getLogger("cfharness.approvals")

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
        bus = getattr(hub, "bus", None)
        if bus is not None and hasattr(bus, "add_listener"):
            bus.add_listener(self._on_event)

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
        if isinstance(d.get("verdict_targets"), str):
            try:
                d["verdict_targets"] = json.loads(d["verdict_targets"])
            except json.JSONDecodeError:
                d["verdict_targets"] = []
        return d

    def get(self, approval_id: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
        if r is None:
            rows = self.conn.execute("SELECT * FROM approval_requests WHERE id LIKE ? LIMIT 2", (approval_id + "%",)).fetchall()
            r = rows[0] if len(rows) == 1 else None
        if r is None:
            raise NotFound(f"không tìm thấy approval '{approval_id}'")
        return self._row(r)  # type: ignore[return-value]

    def list(self, status: str | None = "pending", *, run_id: str | None = None, agent_id: str | None = None, limit: int = 100,
             routed_to: str | None = None) -> list[dict[str, Any]]:
        where, args = [], []
        if routed_to:
            where.append("routed_to=?")
            args.append(routed_to)
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
        routed_to: str = "human",
        route_note: str | None = None,
    ) -> dict[str, Any]:
        aid = "ap_" + secrets.token_hex(5)
        routed_to = "manager" if routed_to == "manager" else "human"
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
            "routed_to": routed_to,
            "delegated_at": now_iso() if routed_to == "manager" else None,
            "verdict_targets": json.dumps([str(t) for t in (verdict.get("targets") or [])][:20], ensure_ascii=False),
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
                    "approve_cmd": f"cfharness approve {aid}",
                    "routed_to": routed_to,
                },
                run_id=run["id"],
                session_id=run.get("session_id"),
            )
        )
        self.hub.agents.set_status(run["agent_id"], "waiting", current_run_id=run["id"], reason=f"approval {aid}")
        audit_record("approval.open", run["agent_id"], run["id"], {"approval_id": aid, "tool": tool_name, "action_fingerprint": fingerprint, "verdict": verdict, "channel": channel,
                                                                   "routed_to": routed_to, "route_note": route_note})
        if routed_to == "manager":
            self._delegated(out)
        return out

    # ------------------------------------------------------------------ giao điều phối viên (hồ sơ phê duyệt)
    def manager_ready(self, asking_agent: str | None) -> str | None:
        """Id điều phối viên nhận được approval: được chỉ định, không phải agent hỏi, không tạm dừng/xoá, không ESTOP, nhịp bật."""
        mgr = getattr(self.hub, "manager", None)
        mid = mgr.agent_id() if mgr is not None else None
        if not mid or mid == asking_agent or self.hub.runs.estop.get("on"):
            return None
        try:
            if mgr.perms is not None and asking_agent in mgr.perms.manager_ids():
                return None  # agent từng là điều phối viên — người quyết
        except Exception:  # pragma: no cover
            return None
        row = self.hub.store.get_agent(mid)
        if row is None or row.get("archived_at") or row.get("status") == "paused":
            return None
        try:
            if self.hub.config.get("manager.heartbeat") is False:
                return None  # không ai đánh thức điều phối viên → người
        except KeyError:
            pass
        return mid

    def delegation_timeout_s(self) -> float:
        from .guard.profiles import delegation_cfg

        return float(delegation_cfg(self.hub.config)["timeout_s"])

    def _delegated(self, row: dict[str, Any]) -> None:
        mgr = getattr(self.hub, "manager", None)
        mid = mgr.agent_id() if mgr is not None else None
        payload = {"request_id": row["id"], "routed_to": "manager", "manager": mid, "tool": row["tool_name"], "preview": row["preview"],
                   "destructive_class": row.get("destructive_class"), "matched_rule": row.get("matched_rule"), "risk_level": row.get("risk_level"),
                   "reason": row.get("reason"), "timeout_s": self.delegation_timeout_s()}
        self.hub.publish(make_event("approval.delegated", row["agent_id"], payload, run_id=row["run_id"], session_id=row.get("session_id")))
        audit_record("approval.delegated", row["agent_id"], row["run_id"], {"approval_id": row["id"], "manager": mid, "matched_rule": row.get("matched_rule"),
                                                                            "destructive_class": row.get("destructive_class")})
        if mgr is not None and hasattr(mgr, "_push"):
            try:
                mgr._push("approval_delegated", f"apd:{row['id']}",
                          f"approval {row['id']} DELEGATED TO YOU: agent {row['agent_id']} wants {row['tool_name']} `{str(row['preview'])[:160]}` "
                          f"(class {row.get('destructive_class')}, rule {row.get('matched_rule')}: {str(row.get('reason') or '')[:120]}) — "
                          f"decide with approval_decide or pass it to the human with approval_escalate", approval_id=row["id"])
                if hasattr(mgr, "wake_soon"):
                    mgr.wake_soon()
            except Exception:  # pragma: no cover — đánh thức lỗi: quá hạn sẽ tự chuyển người
                log.debug("không đánh thức được điều phối viên", exc_info=True)

    def escalate(self, approval_id: str, *, by: str = "hub", note: Any = None, channel: str = "hub") -> dict[str, Any]:
        """Approval đang giao điều phối viên → người (`routed_to=human`, `approval.escalated`, thông báo người như approval.open)."""
        row = self.get(approval_id)
        if row["status"] not in PENDING:
            raise Conflict(f"approval {row['id']} đã {row['status']}")
        if row.get("routed_to") != "manager":
            raise Conflict(f"approval {row['id']} không đang chờ điều phối viên (routed_to={row.get('routed_to')})")
        n = str(redact(str(note)))[:600] if note else None
        cur = self.conn.execute("UPDATE approval_requests SET routed_to='human', escalated_at=?, escalation_note=? WHERE id=? AND routed_to='manager' "
                                "AND status IN ('open','waiting_human')", (now_iso(), n, row["id"]))
        if cur.rowcount != 1:
            raise Conflict(f"approval {row['id']} vừa đổi trạng thái ở kênh khác")
        row = self.get(row["id"])
        self.hub.publish(make_event(
            "approval.escalated", row["agent_id"],
            {"request_id": row["id"], "routed_to": "human", "by": by, "note": n, "tool": row["tool_name"], "preview": row["preview"],
             "destructive_class": row.get("destructive_class"), "matched_rule": row.get("matched_rule"), "risk_level": row.get("risk_level"),
             "reason": row.get("reason"), "protected_resource_id": row.get("protected_resource_id"), "status": row["status"],
             "approve_cmd": f"cfharness approve {row['id']}"},
            run_id=row["run_id"], session_id=row.get("session_id")))
        audit_record("approval.escalated", row["agent_id"], row["run_id"], {"approval_id": row["id"], "by": by, "channel": channel, "note": n, "actor": by})
        return row

    def manager_decide(self, approval_id: str, decision: str, *, manager_id: str, reason: str) -> dict[str, Any]:
        """Điều phối viên quyết approval ĐANG giao cho nó (luật đầy đủ ở manager/permissions.check_rpc — kiểm lại ở đây, lớp 3)."""
        from .guard.profiles import delegable

        row = self.get(approval_id)
        why = self.manager_block(row, manager_id)
        if why is None and decision in ("allow", "approve", "allowed"):
            v = {"verdict": row.get("risk_level"), "rule": row.get("matched_rule"), "klass": row.get("destructive_class"),
                 "protected_id": row.get("protected_resource_id"), "targets": row.get("verdict_targets") or []}
            from .guard.profiles import allowed_roots

            run = self.hub.store.get_run(row["run_id"]) or {}
            why = delegable(v, self.hub.config, tool_name=row["tool_name"], tool_input=row["tool_input"] if isinstance(row["tool_input"], dict) else {},
                            roots=allowed_roots(self.hub), cwd=run.get("cwd"))
        if why:
            from .errors import Blocked

            raise Blocked(f"điều phối viên không quyết được approval {row['id']}: {why}", data={"reason": "manager_forbidden"})
        text = str(redact(str(reason or ""))).strip()[:1000]
        if not text:
            raise HubError("cần lý do (reason) cho quyết định của điều phối viên")
        actor = f"manager:{manager_id}"
        if decision == "escalate":
            return self.escalate(row["id"], by=actor, note=text, channel="manager")
        if decision not in ("allow", "approve", "allowed", "deny", "reject", "denied"):
            raise HubError("decision phải là allow|deny|escalate")
        return self.resolve(row["id"], "allowed" if decision in ("allow", "approve", "allowed") else "denied", by=actor, channel="manager",
                            note=f"[điều phối viên] {text}")

    def manager_block(self, row: dict[str, Any], manager_id: str) -> str | None:
        """Lý do điều phối viên KHÔNG được đụng approval này (None = được)."""
        if row["status"] not in PENDING:
            return f"approval đã {row['status']}"
        if row.get("routed_to") != "manager":
            return f"approval không giao cho điều phối viên (routed_to={row.get('routed_to') or '-'}) — chỉ người quyết"
        if self.hub.runs.estop.get("on"):
            return "ESTOP đang bật"
        mgr = getattr(self.hub, "manager", None)
        mids = set(mgr.perms.manager_ids()) if mgr is not None else set()
        if row["agent_id"] == manager_id or row["agent_id"] in mids:
            return "approval của chính run điều phối viên — chỉ người quyết"
        run = self.hub.store.get_run(row["run_id"]) or {}
        if str((run.get("run_opts") or {}).get("hub_self") or "") == "manager":
            return "approval của run điều phối viên — chỉ người quyết"
        if mgr is None or mgr.agent_id() != manager_id:
            return "không còn là điều phối viên được chỉ định"
        return None

    def check_delegations(self, now: float | None = None) -> list[str]:
        """Approval giao điều phối viên quá hạn / điều phối viên không còn sẵn sàng → người."""
        now = now or time.time()
        limit = self.delegation_timeout_s()
        out = []
        for r in self.list("pending", routed_to="manager", limit=500):
            since = _parse_ts(r.get("delegated_at")) or _parse_ts(r["opened_at"]) or now
            if self.manager_ready(r["agent_id"]) is None:
                note = "điều phối viên không còn sẵn sàng (đổi/huỷ chỉ định, tạm dừng, ESTOP hoặc tắt nhịp)"
            elif now - since >= limit:
                note = f"điều phối viên chưa quyết sau {int(limit // 60)} phút"
            else:
                continue
            try:
                self.escalate(r["id"], by="hub", note=note)
                out.append(r["id"])
            except Conflict:
                pass
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

    def to_waiting_human(self, approval_id: str, *, continued: bool = False) -> dict[str, Any]:
        row = self.get(approval_id)
        if row["status"] != "open":
            return row
        data: dict[str, Any] = {"status": "waiting_human", "waiting_human_at": now_iso()}
        if continued:
            data["timeout_action"] = "deny_and_continue"
        self._update(approval_id, data)
        self.hub.publish(
            make_event(
                "approval.waiting_human",
                row["agent_id"],
                {
                    "request_id": approval_id,
                    "run_id": row["run_id"],
                    "since": row["opened_at"],
                    "preview": row["preview"],
                    # tool/luật/lý do: client (web/TUI/Telegram) hiện "vì sao cần duyệt" mà không phải tra lại approval
                    "tool": row.get("tool_name"),
                    "matched_rule": row.get("matched_rule"),
                    "destructive_class": row.get("destructive_class"),
                    "reason": row.get("reason"),
                    "protected_resource_id": row.get("protected_resource_id"),
                    "lease_held": not continued,
                    "repo_lock_held": True,
                    "continued": continued,  # deny_and_continue: run chạy tiếp (agent đã nhận từ chối), approval vẫn chờ
                    "routed_to": row.get("routed_to"),
                    "approve_cmd": f"cfharness approve {approval_id}",
                },
                run_id=row["run_id"],
                session_id=row.get("session_id"),
            )
        )
        if continued:
            self._refresh_agent_status(row["agent_id"], row["run_id"], ignore_continued=True)
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
        elif row.get("timeout_action") == "deny_and_continue":
            self._late_decision(self.get(aid))
        else:
            self.hub.runs.on_approval_resolved(row["run_id"])
        if decision != "allowed" or fut is None:
            self._refresh_agent_status(row["agent_id"], row["run_id"])
        return self.get(aid)

    def _late_decision(self, row: dict[str, Any]) -> None:
        """deny_and_continue: người quyết SAU khi agent đã nhận từ chối và chạy tiếp. Từ chối → không làm gì thêm (agent đã bỏ qua).
        Cho phép → run còn chạy: nhắn vào run (vé một lần dùng khi agent làm lại ĐÚNG thao tác); run đã kết thúc: resume session kèm vé."""
        if row["status"] != "allowed" or row.get("resume_run_id"):
            return
        run = self.hub.store.get_run(row["run_id"]) or {}
        msg = (f"[cfharness] A human has now APPROVED request {row['id']} ({row['tool_name']}: `{row['preview']}`) that was denied earlier "
               "because nobody answered in time. It is approved to run exactly ONCE: run exactly that same action now, then continue.")
        if run.get("status") in ("queued", "running"):
            try:
                asyncio.get_running_loop().create_task(self._steer_late(run["id"], msg))
            except RuntimeError:  # ngoài event loop (test thuần): run kết thúc → listener resume
                pass
            return
        self._resume_late(row, run, msg)

    async def _steer_late(self, run_id: str, msg: str) -> None:
        from .steer import steer

        try:
            await steer(self.hub, run_id, msg, by="hub", channel="approval")
        except Exception:  # run đã qua `result` → resume khi run kết thúc (listener `_on_event`)
            log.debug("không nhắn được vào run %s — resume khi kết thúc", run_id, exc_info=True)

    def _resume_late(self, row: dict[str, Any], run: dict[str, Any], msg: str) -> None:
        if not run or not run.get("session_id") or row.get("resume_run_id") or row.get("ticket_used_at"):
            return
        try:
            new = self.hub.runs.submit(
                run["agent_id"], msg + " Then finish with a short summary.", cwd=run.get("cwd"), budget_usd=run.get("max_budget_usd"), model=run.get("model"),
                kind=run.get("kind") or "adhoc", resume_session_id=run["session_id"], parent_run_id=run["id"], resume_reason="approval",
                resume_ticket_id=row["id"], check_paused=False)
        except HubError as e:
            self.hub.publish(make_event("error", run["agent_id"], {"code": "resume_failed", "message": e.message}, run_id=run["id"]))
            return
        self._update(row["id"], {"resume_run_id": new["id"]})
        self.hub.publish(make_event("run.resumed", run["agent_id"], {"from_run_id": run["id"], "run_id": new["id"], "reason": "approval", "ticket": row["id"],
                                                                    "late": True}, run_id=run["id"], session_id=run.get("session_id")))

    def _on_event(self, ev: Any) -> None:
        """Run kết thúc: approval deny_and_continue đã được người cho phép nhưng chưa dùng vé → resume session (như waiting_human)."""
        try:
            from .events import TERMINAL_RUN_EVENTS

            if ev.type not in TERMINAL_RUN_EVENTS or ev.source != "hub" or not ev.run_id or ev.type == "run.waiting_human":
                return
            rows = self.conn.execute("SELECT * FROM approval_requests WHERE run_id=? AND status='allowed' AND timeout_action='deny_and_continue' "
                                     "AND resume_run_id IS NULL AND ticket_used_at IS NULL", (ev.run_id,)).fetchall()
            if not rows:
                return
            run = self.hub.store.get_run(ev.run_id) or {}
            if run.get("continued_in_run_id"):
                return
            row = self._row(rows[0])
            msg = (f"[cfharness] A human has now APPROVED request {row['id']} ({row['tool_name']}: `{row['preview']}`) that was denied earlier "
                   "because nobody answered in time. It is approved to run exactly ONCE: run exactly that same action now, then continue the original task.")
            try:
                asyncio.get_running_loop().call_soon(self._resume_late, row, run, msg)
            except RuntimeError:
                self._resume_late(row, run, msg)
        except Exception:  # pragma: no cover — listener không được làm hỏng bus
            log.exception("approvals listener lỗi với %s", getattr(ev, "type", "?"))

    def _refresh_agent_status(self, agent_id: str, run_id: str, *, ignore_continued: bool = False) -> None:
        q = "SELECT COUNT(*) FROM approval_requests WHERE agent_id=? AND status IN ('open','waiting_human')"
        if ignore_continued:
            q += " AND (timeout_action IS NULL OR timeout_action != 'deny_and_continue')"
        pending = self.conn.execute(q, (agent_id,)).fetchone()[0]
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
        max_n = self.hub.config.get("notify.reminder_max_count")
        out = []
        for r in self.list("pending", limit=1000):
            if r.get("routed_to") == "manager":
                continue  # đang chờ điều phối viên — người chưa được hỏi (quá hạn → approval.escalated)
            if isinstance(max_n, int) and int(r.get("reminder_count") or 0) >= max_n:
                continue  # đã nhắc đủ số lần người đặt — yêu cầu vẫn chờ (Q-04), chỉ thôi làm phiền
            last = _parse_ts(r.get("last_reminded_at")) or _parse_ts(r.get("escalated_at")) or _parse_ts(r["opened_at"]) or now
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
                    {"request_id": r["id"], "reminder_count": n, "wait_s": wait_s, "status": r["status"], "preview": r["preview"], "reason": r.get("reason"),
                     "routed_to": r.get("routed_to"), "approve_cmd": f"cfharness approve {r['id']}"},
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
                self.check_delegations()
                self.remind_due()
                from .recovery import purge_expired

                purge_expired(self.hub)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("vòng nhắc approval lỗi")


# ============================================================================ thống kê (`cfharness approvals stats`)
def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    m = len(xs) // 2
    return round(xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2, 1)


def approval_stats(hub: "Hub", days: float = 30) -> dict[str, Any]:
    """Nhóm approval trong `days` ngày theo luật / agent / tool / kết quả / người quyết + thời gian chờ (giây). Chỉ đọc."""
    from datetime import timedelta

    since = (datetime.now(timezone.utc) - timedelta(days=float(days))).strftime("%Y-%m-%dT%H:%M:%S")
    rows = [dict(r) for r in hub.store.conn.execute("SELECT * FROM approval_requests WHERE opened_at >= ? ORDER BY opened_at", (since,))]
    now = time.time()

    def waited(r: dict[str, Any]) -> float | None:
        if r.get("wait_s") is not None:
            return float(r["wait_s"])
        if r["status"] in PENDING:
            t = _parse_ts(r["opened_at"])
            return round(now - t, 1) if t else None
        return None

    def group(key: Any) -> list[dict[str, Any]]:
        g: dict[str, dict[str, Any]] = {}
        for r in rows:
            k = str(key(r) or "-")
            e = g.setdefault(k, {"key": k, "count": 0, "allowed": 0, "denied": 0, "cancelled": 0, "pending": 0, "_w": []})
            e["count"] += 1
            st = r["status"]
            e["pending" if st in PENDING else st] = e.get("pending" if st in PENDING else st, 0) + 1
            w = waited(r)
            if w is not None:
                e["_w"].append(w)
        out = []
        for e in g.values():
            ws = e.pop("_w")
            e["wait_median_s"] = _median(ws)
            e["wait_max_s"] = round(max(ws), 1) if ws else None
            out.append(e)
        return sorted(out, key=lambda e: (-e["count"], e["key"]))

    def decider(r: dict[str, Any]) -> str:
        by = str(r.get("resolved_by") or "")
        if r["status"] in PENDING:
            return f"pending:{r.get('routed_to') or 'human'}"
        if by.startswith("manager:"):
            return "manager"
        return "hub" if by == "hub" else ("human" if by else "-")

    ws = [w for w in (waited(r) for r in rows) if w is not None]
    buckets = {"<1m": 0, "1-5m": 0, "5-30m": 0, "30m-2h": 0, ">2h": 0}
    for w in ws:
        buckets["<1m" if w < 60 else "1-5m" if w < 300 else "5-30m" if w < 1800 else "30m-2h" if w < 7200 else ">2h"] += 1
    # quyết định tự động của guard (không mở approval) — để thấy profile giảm bao nhiêu lần hỏi
    auto = [dict(r) for r in hub.store.conn.execute(
        "SELECT classifier, COUNT(*) AS n FROM guard_decisions WHERE ts >= ? AND action='allow' AND verdict IN ('destructive','uncertain') "
        "AND approval_request_id IS NULL GROUP BY classifier", (since,))]
    return {
        "days": days, "since": since, "total": len(rows),
        "pending": sum(1 for r in rows if r["status"] in PENDING),
        "wait": {"median_s": _median(ws), "max_s": round(max(ws), 1) if ws else None, "buckets": buckets},
        "by_rule": group(lambda r: r.get("matched_rule")),
        "by_agent": group(lambda r: r.get("agent_id")),
        "by_tool": group(lambda r: r.get("tool_name")),
        "by_outcome": group(lambda r: r["status"]),
        "by_decider": group(decider),
        "by_routed_to": group(lambda r: r.get("routed_to") or "legacy"),
        "auto_allowed_without_approval": auto,
    }
