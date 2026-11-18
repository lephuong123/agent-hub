"""Nhật ký kiểm toán (FR-OB-08, FR-MC-03, FR-SG-08).

- Bảng `audit_log` append-only (trigger chặn UPDATE/DELETE — purge là thao tác phá huỷ, FR-SG-08).
- Hàm hợp đồng cho luồng B: `audit.record(kind, agent_id, run_id, data)` — dùng `AuditLog` đã cài
  cho hub (daemon). Dữ liệu được che secret (`redact` + giá trị trong secret store) trước khi ghi.
- `channel`: `cli` | `tui` | `mcp:<tên client>` | `runtime` | `hook`; `actor`: `user` (Q-11 một người dùng)
  | `agent:<id>` | `hub`.

L4: mỗi dòng nối vào chuỗi băm (`audit_chain.insert_chained` — writer DUY NHẤT của `audit_log`; neo + kiểm ở `audit_chain.AuditChain`).

Không phát event cho mỗi dòng audit (tránh nhân đôi luồng); riêng tool ghi có event `tool.write`
(phát trong `mcp_client.wiring`).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

from .audit_chain import has_chain, insert_chained
from .redact import redact

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("cfharness.audit")

MAX_DATA_CHARS = 8000
OUTCOMES = ("ok", "error", "denied")
# L3 (A1/A3): dòng do service tự ghi — kết quả hiển nhiên theo loại. Từ chối quyền / đăng nhập hỏng = denied; thao tác của người đã
# thực hiện xong (quyết approval, trust/grant/sandbox/bảo vệ, purge, đổi policy, ghép nối chat) = ok. Loại khác (phát hiện, thông
# báo…) để NULL — không đoán.
DENIED_KINDS = frozenset({"auth.denied", "manager.denied", "web.login_failed", "web.login_locked"})
OK_PREFIXES = ("approval.", "guard.trust", "guard.rule_grant", "guard.sandbox", "guard.repo_deny", "guard.protect", "guard.integrity_accept",
               "agent.purged", "policy.changed", "telegram.", "slack.", "question.", "web.login", "web.logout", "web.totp_", "read:")
# trường ngữ cảnh do daemon đặt cho lượt RPC (`trace.REQ_CTX`) → cột audit cùng tên
CTX_FIELDS = ("principal", "declared_channel", "session_ref", "ip", "caller_run_id")


def default_outcome(kind: str) -> str | None:
    if kind in DENIED_KINDS:
        return "denied"
    return "ok" if kind.startswith(OK_PREFIXES) else None


class AuditLog:
    def __init__(self, store: "Store", *, mask: Callable[[str], str] | None = None) -> None:
        self.store = store
        self.mask = mask or (lambda s: s)
        # người nghe sau mỗi dòng (vd admin_audit: bản METADATA cho người quản trị máy) — lỗi của tap không ảnh hưởng ghi audit
        self.taps: list[Callable[[str, str, str, dict[str, Any]], None]] = []
        self._lock = threading.RLock()
        self._chained: bool | None = None  # L4: bảng đã có cột chuỗi băm (v30) — hỏi PRAGMA một lần

    def record(
        self,
        kind: str,
        agent_id: str | None = None,
        run_id: str | None = None,
        data: dict[str, Any] | None = None,
        *,
        actor: str | None = None,
        channel: str | None = None,
        target: str | None = None,
        summary: str | None = None,
        outcome: str | None = None,
        error_code: str | None = None,
        duration_ms: float | None = None,
        **ctx_over: Any,
    ) -> int:
        """`principal`/`declared_channel`/`session_ref`/`ip`/`caller_run_id` (L3) mặc định lấy từ ngữ cảnh lượt RPC (`trace.current()`,
        do daemon chứng thực) — truyền tường minh để ghi đè. `actor` thiếu → `data.actor` → `data.by` (A5: service ghi `by`) → `hub`."""
        data = dict(data or {})
        actor = actor or data.pop("actor", None) or (str(data["by"]) if data.get("by") else None) or "hub"
        channel = channel or data.pop("channel", None) or "runtime"
        if outcome not in OUTCOMES:
            outcome = default_outcome(kind)
        from .trace import current as _cur

        ctx = _cur() or {}
        extra = {k: (ctx_over[k] if k in ctx_over else ctx.get(k)) for k in CTX_FIELDS}
        extra = {k: (str(v)[:120] if v is not None else None) for k, v in extra.items()}
        payload = self.mask(json.dumps(redact(data), ensure_ascii=False, default=str))
        if len(payload) > MAX_DATA_CHARS:
            payload = json.dumps({"truncated": True, "head": payload[:MAX_DATA_CHARS]}, ensure_ascii=False)
        from .store import now_iso
        from .trace import current_req_id

        values = {"ts": now_iso(), "kind": kind, "actor": actor, "channel": channel, "agent_id": agent_id, "run_id": run_id,
                  "target": (str(target) if target is not None else None), "summary": self.mask(summary or "")[:500] or None, "data": payload,
                  "req_id": current_req_id(), "outcome": outcome, "error_code": (str(error_code)[:40] if error_code else None),
                  "duration_ms": (round(float(duration_ms), 1) if duration_ms is not None else None), **extra}
        with self._lock:  # L4: đọc dòng cuối + ghi trong cùng transaction (audit_chain.insert_chained); khoá chặn hai luồng của daemon chen nhau
            if self._chained is None:
                self._chained = has_chain(self.store.conn)
            rid = insert_chained(self.store.conn, values, chained=self._chained)
        for tap in self.taps:
            try:
                tap(kind, actor, channel, data)
            except Exception:  # pragma: no cover
                log.exception("audit tap lỗi")
        return rid

    def query(
        self,
        *,
        kind: str | None = None,
        agent_id: str | None = None,
        run_id: str | None = None,
        channel: str | None = None,
        limit: int = 100,
        principal: str | None = None,
        outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        where, args = [], []
        for col, val in (("agent_id", agent_id), ("run_id", run_id), ("channel", channel), ("principal", principal), ("outcome", outcome)):
            if val is not None:
                where.append(f"{col}=?")
                args.append(val)
        if kind is not None:
            if kind.endswith("*"):
                where.append("kind LIKE ?")
                args.append(kind[:-1] + "%")
            else:
                where.append("kind=?")
                args.append(kind)
        sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        out = []
        for r in self.store.conn.execute(sql, args):
            d = dict(r)
            try:
                d["data"] = json.loads(d["data"]) if d.get("data") else {}
            except json.JSONDecodeError:  # pragma: no cover
                pass
            out.append(d)
        return out


# ---------------------------------------------------------------- hàm hợp đồng (module-level)
_installed: AuditLog | None = None


def install(audit_log: AuditLog | None) -> None:
    """Hub gọi khi khởi tạo; mọi `audit.record(...)` trong tiến trình daemon ghi vào hub này."""
    global _installed
    _installed = audit_log


def installed() -> AuditLog | None:
    return _installed


def record(kind: str, agent_id: str | None, run_id: str | None, data: dict[str, Any] | None = None, **kw: Any) -> int | None:
    """Ghi một dòng audit. Hợp đồng với luồng B (docs/plan/M1.md mục 2).

    `data` có thể kèm `actor`/`channel` (được tách ra cột riêng). Gọi ngoài daemon (chưa cài) →
    chỉ log cảnh báo và trả None — tiến trình khác phải gọi RPC `audit.record` của daemon.
    """
    if _installed is None:
        log.warning("audit.record(%s) gọi khi chưa có hub — bỏ qua; dùng RPC audit.record", kind)
        return None
    return _installed.record(kind, agent_id, run_id, data, **kw)
