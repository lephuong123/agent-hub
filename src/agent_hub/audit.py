"""Nhật ký kiểm toán (FR-OB-08, FR-MC-03, FR-SG-08).

- Bảng `audit_log` append-only (trigger chặn UPDATE/DELETE — purge là thao tác phá huỷ, FR-SG-08).
- Hàm hợp đồng cho luồng B: `audit.record(kind, agent_id, run_id, data)` — dùng `AuditLog` đã cài
  cho hub (daemon). Dữ liệu được che secret (`redact` + giá trị trong secret store) trước khi ghi.
- `channel`: `cli` | `tui` | `mcp:<tên client>` | `runtime` | `hook`; `actor`: `user` (Q-11 một người dùng)
  | `agent:<id>` | `hub`.

Không phát event cho mỗi dòng audit (tránh nhân đôi luồng); riêng tool ghi có event `tool.write`
(phát trong `mcp_client.wiring`).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Callable

from .redact import redact

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("agent_hub.audit")

MAX_DATA_CHARS = 8000


class AuditLog:
    def __init__(self, store: "Store", *, mask: Callable[[str], str] | None = None) -> None:
        self.store = store
        self.mask = mask or (lambda s: s)

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
    ) -> int:
        data = dict(data or {})
        actor = actor or data.pop("actor", None) or "hub"
        channel = channel or data.pop("channel", None) or "runtime"
        payload = self.mask(json.dumps(redact(data), ensure_ascii=False, default=str))
        if len(payload) > MAX_DATA_CHARS:
            payload = json.dumps({"truncated": True, "head": payload[:MAX_DATA_CHARS]}, ensure_ascii=False)
        from .store import now_iso

        cur = self.store.conn.execute(
            "INSERT INTO audit_log (ts, kind, actor, channel, agent_id, run_id, target, summary, data) VALUES (?,?,?,?,?,?,?,?,?)",
            (now_iso(), kind, actor, channel, agent_id, run_id, target, self.mask(summary or "")[:500] or None, payload),
        )
        return int(cur.lastrowid or 0)

    def query(
        self,
        *,
        kind: str | None = None,
        agent_id: str | None = None,
        run_id: str | None = None,
        channel: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where, args = [], []
        for col, val in (("agent_id", agent_id), ("run_id", run_id), ("channel", channel)):
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
