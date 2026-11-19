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
        # L5: người nghe nhận CẢ dòng đã ghi (cột đã che + `id` + `data_obj` = dict data trước khi che) — `audit_watch` cần principal/outcome/
        # target mà tap 4 tham số cũ không có. Cùng quy tắc: lỗi của tap bị log, không bao giờ làm hỏng lượt ghi audit.
        self.row_taps: list[Callable[[dict[str, Any]], None]] = []
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
        if self.row_taps:
            row = {**values, "id": rid, "data_obj": data}
            for rtap in self.row_taps:
                try:
                    rtap(row)
                except Exception:
                    log.exception("audit row tap lỗi")
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
        actor: str | None = None,
        req_id: str | None = None,
        kind_prefix: str | None = None,
        since: str | None = None,
        until: str | None = None,
        q: str | None = None,
        before_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """L5 (A7): lọc theo cột (index `principal,ts`/`outcome,ts`/`req_id`/`kind,ts`/`run_id`), `kind` có `*` cuối hoặc `kind_prefix` =
        tiền tố, `since`/`until` = ISO UTC (CLI/web đổi `24h` trước), `q` = chuỗi con trong `summary`/`target` (KHÔNG BAO GIỜ trong `data`),
        `before_id` = phân trang lùi (trang sau = id nhỏ nhất của trang trước). Mới nhất trước, `limit` ≤ `QUERY_MAX`."""
        where, args = build_where(self.store.conn, kind=kind, agent_id=agent_id, run_id=run_id, channel=channel, principal=principal,
                                  outcome=outcome, actor=actor, req_id=req_id, kind_prefix=kind_prefix, since=since, until=until, q=q,
                                  before_id=before_id)
        sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(int(limit), QUERY_MAX)))
        out = []
        for r in self.store.conn.execute(sql, args):
            d = dict(r)
            try:
                d["data"] = json.loads(d["data"]) if d.get("data") else {}
            except json.JSONDecodeError:  # pragma: no cover
                pass
            out.append(d)
        return out

    def activity(self, *, days: int = 7, now: Any = None, top: int = 30) -> dict[str, Any]:
        """L5 (A7) thẻ "Hoạt động theo người": số dòng theo principal × ngày (giờ máy) trong `days` ngày gần nhất + số dòng `denied`.
        Chỉ đọc cột `principal`/`ts`/`outcome` (không nội dung); quét theo khoảng id tìm nhị phân từ mốc thời gian (không quét cả bảng)."""
        from datetime import datetime, timedelta, timezone

        days = max(1, min(int(days or 7), 31))
        now_l = (now or datetime.now(timezone.utc)).astimezone()
        first_day = (now_l - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        labels = [(first_day + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
        since = first_day.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        lo = first_id_at(self.store.conn, since)
        counts: dict[str, dict[str, int]] = {}
        denied: dict[str, int] = {}
        if lo is not None:
            for p, ts, oc in self.store.conn.execute(
                    "SELECT principal, ts, outcome FROM audit_log WHERE id >= ? AND ts >= ? AND principal IS NOT NULL", (lo, since)):
                try:
                    day = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
                except ValueError:
                    continue
                c = counts.setdefault(str(p), {})
                c[day] = c.get(day, 0) + 1
                if oc == "denied":
                    denied[str(p)] = denied.get(str(p), 0) + 1
        rows = [{"principal": p, "counts": [c.get(d, 0) for d in labels], "total": sum(c.values()), "denied": denied.get(p, 0)}
                for p, c in counts.items()]
        rows.sort(key=lambda r: (-r["total"], r["principal"]))
        return {"days": labels, "rows": rows[:top], "principals": len(rows)}

    def export(self, path: Any, *, fmt: str = "jsonl", **filters: Any) -> dict[str, Any]:
        return export_conn(self.store.conn, path, fmt=fmt, **filters)


QUERY_MAX = 1000
EXPORT_FORMATS = ("jsonl", "csv")
FILTER_KEYS = ("kind", "agent_id", "run_id", "channel", "principal", "outcome", "actor", "req_id", "kind_prefix", "since", "until", "q", "before_id")
_ID_MARGIN = 1000  # ts không đơn điệu tuyệt đối (đồng hồ chỉnh lùi) → nới khoảng id tìm được rồi vẫn lọc đúng theo ts


def export_conn(conn: Any, path: Any, *, fmt: str = "jsonl", **filters: Any) -> dict[str, Any]:
    """L5 (A7): ghi các dòng khớp bộ lọc (cũ → mới) ra `path` (file MỚI, 0600, O_EXCL|O_NOFOLLOW) — gồm MỌI cột kể cả `prev_hash`/
    `row_hash`; `data` giữ NGUYÊN chuỗi JSON đã lưu (đã che lúc ghi) để băm lại được. Trả số dòng/byte/sha256."""
    import csv
    import hashlib
    import io
    import os

    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"định dạng phải là {'|'.join(EXPORT_FORMATS)}")
    where, args = build_where(conn, **{k: v for k, v in filters.items() if k in FILTER_KEYS})
    sql = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    h = hashlib.sha256()
    n = size = 0
    first = last = None
    try:
        with os.fdopen(fd, "wb") as f:
            cur = conn.execute(sql, args)
            cols = [d[0] for d in cur.description]
            if fmt == "csv":
                buf = io.StringIO()
                csv.writer(buf).writerow(cols)
                b = buf.getvalue().encode("utf-8")
                f.write(b)
                h.update(b)
                size += len(b)
            while True:
                batch = cur.fetchmany(1000)
                if not batch:
                    break
                buf = io.StringIO()
                w = csv.writer(buf) if fmt == "csv" else None
                for r in batch:
                    d = dict(zip(cols, r))
                    if w is not None:
                        w.writerow(["" if d[c] is None else d[c] for c in cols])
                    else:
                        buf.write(json.dumps(d, ensure_ascii=False, separators=(",", ":")) + "\n")
                    n += 1
                    first = d["id"] if first is None else first
                    last = d["id"]
                b = buf.getvalue().encode("utf-8")
                f.write(b)
                h.update(b)
                size += len(b)
        if os.name == "posix":
            os.chmod(str(path), 0o600)
    except BaseException:
        try:
            os.unlink(str(path))
        except OSError:
            pass
        raise
    return {"rows": n, "bytes": size, "sha256": h.hexdigest(), "format": fmt, "first_id": first, "last_id": last}


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def first_id_at(conn: Any, ts: str) -> int | None:
    """id nhỏ nhất (gần đúng, nới `_ID_MARGIN`) có `ts >= ts` — tìm nhị phân trên khoá chính (id tăng cùng thời gian). None = không có dòng."""
    r = conn.execute("SELECT MIN(id), MAX(id) FROM audit_log").fetchone()
    if not r or r[0] is None:
        return None
    lo, hi = int(r[0]), int(r[1])
    while lo < hi:
        mid = (lo + hi) // 2
        row = conn.execute("SELECT id, ts FROM audit_log WHERE id >= ? ORDER BY id LIMIT 1", (mid,)).fetchone()
        if row is None or str(row[1]) >= ts:
            hi = mid
        else:
            lo = int(row[0]) + 1
    return max(1, lo - _ID_MARGIN)


def last_id_at(conn: Any, ts: str) -> int | None:
    """id lớn nhất (gần đúng, nới `_ID_MARGIN`) có `ts <= ts`."""
    r = conn.execute("SELECT MIN(id), MAX(id) FROM audit_log").fetchone()
    if not r or r[0] is None:
        return None
    lo, hi = int(r[0]), int(r[1])
    while lo < hi:
        mid = (lo + hi + 1) // 2
        row = conn.execute("SELECT id, ts FROM audit_log WHERE id <= ? ORDER BY id DESC LIMIT 1", (mid,)).fetchone()
        if row is None or str(row[1]) > ts:
            hi = mid - 1
        else:
            lo = int(row[0])
    return lo + _ID_MARGIN


def build_where(conn: Any, *, kind: str | None = None, agent_id: str | None = None, run_id: str | None = None, channel: str | None = None,
                principal: str | None = None, outcome: str | None = None, actor: str | None = None, req_id: str | None = None,
                kind_prefix: str | None = None, since: str | None = None, until: str | None = None, q: str | None = None,
                before_id: int | None = None) -> tuple[list[str], list[Any]]:
    where: list[str] = []
    args: list[Any] = []
    for col, val in (("agent_id", agent_id), ("run_id", run_id), ("channel", channel), ("principal", principal), ("outcome", outcome),
                     ("actor", actor), ("req_id", req_id)):
        if val not in (None, ""):
            where.append(f"{col}=?")
            args.append(str(val))
    if kind not in (None, ""):
        if str(kind).endswith("*"):
            kind_prefix = kind_prefix or str(kind)[:-1]
        else:
            where.append("kind=?")
            args.append(str(kind))
    if kind_prefix not in (None, ""):
        where.append("kind LIKE ? ESCAPE '\\'")
        args.append(_like_escape(str(kind_prefix)) + "%")
    if since not in (None, ""):
        lo = first_id_at(conn, str(since))
        where.append("id >= ?")
        args.append(lo if lo is not None else 1 << 62)
        where.append("ts >= ?")
        args.append(str(since))
    if until not in (None, ""):
        hi = last_id_at(conn, str(until))
        if hi is not None:
            where.append("id <= ?")
            args.append(hi)
        where.append("ts <= ?")
        args.append(str(until))
    if before_id not in (None, ""):
        where.append("id < ?")
        args.append(int(before_id))
    if q not in (None, ""):
        pat = "%" + _like_escape(str(q)[:200]) + "%"
        where.append("(summary LIKE ? ESCAPE '\\' OR target LIKE ? ESCAPE '\\')")
        args += [pat, pat]
    return where, args


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
