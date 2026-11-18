"""Dòng thời gian của một yêu cầu / run / task — `cfharness trace <id>` + RPC `trace.get` (L6, docs/dev-notes/plan-logging.md B6).

Ghép theo `ts`:
- **audit** (`audit_log` theo `req_id` — index — và theo `run_id` — index `audit_run`): kind, actor, kênh, principal, kết quả, đích, tóm tắt.
- **log** (log_query.query, lọc theo `req_id ∈ …` hoặc `run_id ∈ …`, trong cửa sổ thời gian của yêu cầu, có trần byte).
- **event** CHỈ dạng tóm tắt: loại, level, nguồn + vài trường trạng thái theo danh sách trắng (`EVENT_KEYS`, giá trị là từ khoá/id ngắn) —
  KHÔNG BAO GIỜ payload/chữ. Bỏ event `detail`/`debug` (delta, tiến độ). Theo `run_id` (index `events_run`) + event mang đúng `req_id` mà không
  thuộc run (cửa sổ rowid tìm nhị phân theo `ts` — `events.req_id` không index).
- **guard** (`guard_decisions` theo `run_id`, index): tool, verdict, action, luật — không bao giờ input.
- **approval** (`approval_requests` theo `run_id`, index): trạng thái, tool, lớp, ai quyết — không input/lý do/ghi chú.
- **run**: run gốc + run con (`parent_run_id`, `continued_in_run_id`, run có `req_id` là lệnh con — `parent_req_id` trong log).

`id`: `r_<ULID>` = req_id; `r_<hex>` = run; `t_<hex>` = task. Trần: số mục (`limit`), event mỗi run, số run, byte log quét, số dòng event quét
trong cửa sổ rowid. Mục tiêu < 2 s trên hub một tháng dữ liệu (đo trong tests/test_debug_tools.py).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .log_query import Filters, iso_utc
from .log_query import query as log_query
from .trace import req_id_ms, valid_req_id

ID_RE = re.compile(r"^[A-Za-z0-9_\-]{3,64}$")
DEFAULT_LIMIT = 1000
MAX_LIMIT = 5000
MAX_RUNS = 50
EVENTS_PER_RUN = 300
EVENT_SCAN_ROWS = 200_000
LOG_SCAN_BYTES = 32 * 1024 * 1024
REQ_WINDOW_S = 3600  # yêu cầu không gắn run: cửa sổ tìm event/log sau mốc của req_id
SKIP_EVENT_LEVELS = ("detail", "debug")
# trường payload được phép vào tóm tắt event — giá trị CHỈ là từ khoá/id ngắn (`_SAFE`) hoặc số/bool
EVENT_KEYS = ("name", "tool", "tool_name", "status", "outcome", "verdict", "action", "klass", "decision", "kind", "subtype", "code",
              "reason_code", "state", "is_error", "approval_id", "task_id", "attempt")
_SAFE = re.compile(r"^[A-Za-z0-9_\-:.]{1,64}$")


def classify(ident: str) -> str:
    if valid_req_id(ident):
        return "req"
    if ident.startswith("t_"):
        return "task"
    return "run"


def _safe_val(v: Any) -> Any:
    if isinstance(v, bool) or isinstance(v, (int, float)):
        return v
    if isinstance(v, str) and _SAFE.match(v):
        return v
    return None


def event_summary(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    for k in EVENT_KEYS:
        v = _safe_val(payload.get(k))
        if v is not None:
            out[k] = v
    return out


def _rows(conn: sqlite3.Connection, sql: str, args: tuple[Any, ...] | list[Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, tuple(args))]


def _in(vals: list[str]) -> str:
    return ",".join("?" * len(vals))


def _rowid_at(conn: sqlite3.Connection, ts: str) -> int | None:
    """rowid đầu tiên của `events` có ts ≥ `ts` (tìm nhị phân trên rowid — rowid tăng theo thời gian ghi). None = không có."""
    lo_hi = conn.execute("SELECT MIN(rowid), MAX(rowid) FROM events").fetchone()
    if not lo_hi or lo_hi[0] is None:
        return None
    lo, hi = int(lo_hi[0]), int(lo_hi[1])
    last = conn.execute("SELECT ts FROM events WHERE rowid=?", (hi,)).fetchone()
    if last is None or last[0] < ts:
        return None
    while lo < hi:  # f(x) = ts của dòng đầu có rowid ≥ x — không giảm theo x; tìm x nhỏ nhất có f(x) ≥ ts
        mid = (lo + hi) // 2
        r = conn.execute("SELECT ts FROM events WHERE rowid>=? ORDER BY rowid LIMIT 1", (mid,)).fetchone()
        if r is not None and r[0] >= ts:
            hi = mid
        else:
            lo = mid + 1
    r = conn.execute("SELECT rowid FROM events WHERE rowid>=? ORDER BY rowid LIMIT 1", (lo,)).fetchone()
    return int(r[0]) if r else None


class _Builder:
    def __init__(self, conn: sqlite3.Connection, logs_dir: Path | None, *, limit: int, log_bytes: int, include_logs: bool) -> None:
        self.conn = conn
        self.logs_dir = logs_dir
        self.limit = limit
        self.log_bytes = log_bytes
        self.include_logs = include_logs and logs_dir is not None
        self.items: list[dict[str, Any]] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self.req_ids: set[str] = set()
        self.notes: list[str] = []
        self._seen_audit: set[int] = set()
        self._seen_events: set[tuple[str, int]] = set()

    # ------------------------------------------------------------------ nguồn
    def add_runs(self, rows: list[dict[str, Any]], *, relation: str) -> list[str]:
        new: list[str] = []
        for r in rows:
            if r["id"] in self.runs:
                continue
            if len(self.runs) >= MAX_RUNS:
                self.notes.append(f"quá {MAX_RUNS} run — bỏ phần còn lại")
                break
            self.runs[r["id"]] = {**r, "relation": relation}
            new.append(r["id"])
            if r.get("req_id"):
                self.req_ids.add(r["req_id"])
        return new

    def run_rows(self, where: str, args: list[Any]) -> list[dict[str, Any]]:
        return _rows(self.conn, "SELECT id, agent_id, task_id, kind, status, outcome, queued_at, started_at, ended_at, parent_run_id,"
                                " continued_in_run_id, req_id, cost_usd, duration_ms FROM runs WHERE " + where + f" LIMIT {MAX_RUNS}", args)

    def children(self) -> None:
        """Run con: tiếp nối (`parent_run_id`/`continued_in_run_id`) tới 3 tầng."""
        frontier = list(self.runs)
        for _ in range(3):
            if not frontier:
                break
            ids = frontier[:MAX_RUNS]
            rows = self.run_rows(f"parent_run_id IN ({_in(ids)})", ids)
            cont = [self.runs[i].get("continued_in_run_id") for i in ids if self.runs.get(i, {}).get("continued_in_run_id")]
            if cont:
                rows += self.run_rows(f"id IN ({_in(cont)})", cont)
            frontier = self.add_runs(rows, relation="child")

    def audits(self) -> None:
        cols = ("id, ts, kind, actor, channel, principal, outcome, error_code, duration_ms, agent_id, run_id, target, summary, req_id")
        rows: list[dict[str, Any]] = []
        reqs = sorted(self.req_ids)[:200]
        if reqs:
            rows += _rows(self.conn, f"SELECT {cols} FROM audit_log WHERE req_id IN ({_in(reqs)}) ORDER BY id LIMIT ?", [*reqs, self.limit])
        runs = list(self.runs)[:MAX_RUNS]
        if runs:
            rows += _rows(self.conn, f"SELECT {cols} FROM audit_log WHERE run_id IN ({_in(runs)}) ORDER BY id LIMIT ?", [*runs, self.limit])
        for r in rows:
            if r["id"] in self._seen_audit:
                continue
            self._seen_audit.add(r["id"])
            self.items.append({"ts": r["ts"], "source": "audit", "label": r["kind"], "req_id": r.get("req_id"), "run_id": r.get("run_id"),
                               "agent_id": r.get("agent_id"), "outcome": r.get("outcome"),
                               "detail": {k: r[k] for k in ("actor", "channel", "principal", "error_code", "duration_ms", "target", "summary")
                                          if r.get(k) not in (None, "")}})

    def _event_item(self, r: dict[str, Any]) -> dict[str, Any] | None:
        key = (r["epoch"], int(r["seq"]))
        if key in self._seen_events:
            return None
        self._seen_events.add(key)
        import json

        try:
            payload = json.loads(r.get("payload") or "{}")
        except ValueError:
            payload = {}
        return {"ts": r["ts"], "source": "event", "label": r["type"], "req_id": r.get("req_id"), "run_id": r.get("run_id"),
                "agent_id": r.get("agent_id"), "level": r.get("level"),
                "detail": {"src": r.get("source"), **event_summary(payload)}}

    def events(self, task_id: str | None = None) -> None:
        cols = "epoch, seq, ts, type, level, source, agent_id, run_id, req_id, payload"
        skip = _in(list(SKIP_EVENT_LEVELS))
        for rid in list(self.runs)[:MAX_RUNS]:
            rows = _rows(self.conn, f"SELECT {cols} FROM events WHERE run_id=? AND level NOT IN ({skip}) ORDER BY epoch, seq LIMIT ?",
                         [rid, *SKIP_EVENT_LEVELS, EVENTS_PER_RUN + 1])
            if len(rows) > EVENTS_PER_RUN:
                self.notes.append(f"run {rid}: chỉ hiện {EVENTS_PER_RUN} event đầu")
                rows = rows[:EVENTS_PER_RUN]
            for r in rows:
                it = self._event_item(r)
                if it:
                    self.items.append(it)
        if task_id:
            rows = _rows(self.conn, f"SELECT {cols} FROM events WHERE task_id=? AND run_id IS NULL AND level NOT IN ({skip}) ORDER BY epoch, seq LIMIT ?",
                         [task_id, *SKIP_EVENT_LEVELS, EVENTS_PER_RUN])
            for r in rows:
                it = self._event_item(r)
                if it:
                    self.items.append(it)

    def req_events(self, reqs: list[str], t_from: str, t_to: str) -> None:
        """Event mang req_id thuộc `reqs` mà KHÔNG thuộc run đã gom (vd task.status, approval của yêu cầu) — quét cửa sổ rowid theo ts."""
        if not reqs:
            return
        lo = _rowid_at(self.conn, t_from)
        if lo is None:
            return
        hi_r = _rowid_at(self.conn, t_to)
        hi = (hi_r if hi_r is not None else int(self.conn.execute("SELECT MAX(rowid) FROM events").fetchone()[0]) + 1)
        if hi - lo > EVENT_SCAN_ROWS:
            hi = lo + EVENT_SCAN_ROWS
            self.notes.append("cửa sổ event quá lớn — chỉ quét phần đầu")
        skip = _in(list(SKIP_EVENT_LEVELS))
        rows = _rows(self.conn, f"SELECT epoch, seq, ts, type, level, source, agent_id, run_id, req_id, payload FROM events"
                                f" WHERE rowid>=? AND rowid<? AND req_id IN ({_in(reqs)}) AND level NOT IN ({skip}) LIMIT ?",
                     [lo, hi, *reqs, *SKIP_EVENT_LEVELS, EVENTS_PER_RUN])
        for r in rows:
            if r.get("run_id") in self.runs:
                continue
            it = self._event_item(r)
            if it:
                self.items.append(it)

    def guards(self) -> None:
        runs = list(self.runs)[:MAX_RUNS]
        if not runs:
            return
        for r in _rows(self.conn, f"SELECT ts, run_id, agent_id, channel, tool_name, matched_rule, classifier, verdict, action, approval_request_id"
                                  f" FROM guard_decisions WHERE run_id IN ({_in(runs)}) ORDER BY id LIMIT ?", [*runs, self.limit]):
            self.items.append({"ts": r["ts"], "source": "guard", "label": f"{r['verdict']} → {r['action']}", "run_id": r["run_id"],
                               "agent_id": r.get("agent_id"),
                               "detail": {k: r[k] for k in ("tool_name", "matched_rule", "classifier", "channel", "approval_request_id") if r.get(k)}})

    def approvals(self) -> None:
        runs = list(self.runs)[:MAX_RUNS]
        if not runs:
            return
        for r in _rows(self.conn, f"SELECT id, run_id, agent_id, tool_name, destructive_class, matched_rule, status, opened_at, resolved_at,"
                                  f" resolved_by, resolved_channel FROM approval_requests WHERE run_id IN ({_in(runs)}) ORDER BY opened_at LIMIT 200", runs):
            base = {"run_id": r["run_id"], "agent_id": r.get("agent_id")}
            self.items.append({"ts": r["opened_at"], "source": "approval", "label": f"mở {r['id']}", **base,
                               "detail": {k: r[k] for k in ("tool_name", "destructive_class", "matched_rule") if r.get(k)}})
            if r.get("resolved_at"):
                self.items.append({"ts": r["resolved_at"], "source": "approval", "label": f"{r['status']} {r['id']}", **base,
                                   "detail": {k: r[k] for k in ("resolved_by", "resolved_channel") if r.get(k)}})

    def run_items(self) -> None:
        for rid, r in self.runs.items():
            base = {"run_id": rid, "agent_id": r.get("agent_id"), "req_id": r.get("req_id")}
            meta = {k: r[k] for k in ("kind", "task_id", "parent_run_id", "relation") if r.get(k)}
            self.items.append({"ts": r["queued_at"], "source": "run", "label": f"xếp hàng {rid}", **base, "detail": meta})
            if r.get("started_at"):
                self.items.append({"ts": r["started_at"], "source": "run", "label": f"bắt đầu {rid}", **base, "detail": {}})
            if r.get("ended_at"):
                self.items.append({"ts": r["ended_at"], "source": "run", "label": f"kết thúc {rid}", **base, "outcome": r.get("outcome"),
                                   "detail": {k: r[k] for k in ("status", "outcome", "duration_ms", "cost_usd") if r.get(k) is not None}})

    def logs(self, t_from: str | None, t_to: str | None, extra_reqs: set[str] | None = None) -> set[str]:
        """Dòng log theo req_id/run_id trong cửa sổ. Trả các req_id CON (dòng có `parent_req_id` ∈ req đã biết)."""
        if not self.include_logs:
            return set()
        reqs = set(self.req_ids) | set(extra_reqs or ())
        any_of = {"req_id": reqs, "run_id": set(self.runs), "parent_req_id": reqs}
        if not any(any_of.values()):
            return set()
        res = log_query(self.logs_dir, Filters(since=t_from, until=t_to, any_of=any_of), limit=self.limit, max_bytes=self.log_bytes)  # type: ignore[arg-type]
        if res["truncated"]:
            self.notes.append("log: chạm trần byte quét — dòng cũ hơn có thể thiếu")
        if res["more"]:
            self.notes.append("log: nhiều dòng hơn giới hạn — chỉ hiện các dòng mới nhất")
        kids: set[str] = set()
        for d in res["rows"]:
            if d.get("parent_req_id") in reqs and d.get("req_id") and d["req_id"] not in reqs:
                kids.add(str(d["req_id"]))
            if d.get("req_id") not in reqs and d.get("run_id") not in self.runs:
                continue  # chỉ là dòng của lệnh con — lấy ở lượt sau
            self.items.append({"ts": d.get("ts"), "source": "log", "label": f"{d.get('level')} {d.get('logger')}", "req_id": d.get("req_id"),
                               "run_id": d.get("run_id"), "agent_id": d.get("agent_id"), "level": d.get("level"),
                               "detail": {k: d[k] for k in ("msg", "proc", "event", "method", "duration_ms", "outcome", "code", "exc") if d.get(k) not in (None, "")}})
        return kids

    # ------------------------------------------------------------------ kết quả
    def window(self, base_ts: str | None) -> tuple[str | None, str | None]:
        starts = [r["queued_at"] for r in self.runs.values() if r.get("queued_at")]
        ends = [r.get("ended_at") for r in self.runs.values()]
        lo = min([x for x in [base_ts, *starts] if x], default=None)
        if any(e is None for e in ends) and self.runs:
            hi = None  # còn run chưa xong → tới hiện tại
        else:
            last = max([x for x in [base_ts, *ends] if x], default=None)
            hi = _shift(last, 120) if last else None
        if lo:
            lo = _shift(lo, -5)
        if hi is None and base_ts and not self.runs:
            hi = _shift(base_ts, REQ_WINDOW_S)
        return lo, hi


def _shift(ts: str, seconds: float) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return iso_utc(dt + timedelta(seconds=seconds))


def build(conn: sqlite3.Connection, logs_dir: Path | None, ident: str, *, limit: int = DEFAULT_LIMIT, log_bytes: int = LOG_SCAN_BYTES,
          include_logs: bool = True) -> dict[str, Any]:
    """Dòng thời gian của `ident`. Kết quả `{id, kind, found, items[{ts, source, label, req_id?, run_id?, agent_id?, level?, outcome?, detail}],
    runs[{id, agent_id, kind, status, outcome, relation, req_id}], req_ids, notes, truncated}` — sắp theo ts."""
    ident = (ident or "").strip()
    if not ID_RE.match(ident):
        raise ValueError("id không hợp lệ (req_id r_…, run_id hoặc task_id)")
    limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    b = _Builder(conn, logs_dir, limit=limit, log_bytes=log_bytes, include_logs=include_logs)
    kind = classify(ident)
    base_ts: str | None = None
    task_id: str | None = None
    if kind == "req":
        b.req_ids.add(ident)
        ms = req_id_ms(ident)
        base_ts = iso_utc(datetime.fromtimestamp(ms / 1000, timezone.utc)) if ms is not None else None
        b.add_runs(b.run_rows("req_id=?", [ident]), relation="root")
    elif kind == "task":
        task_id = ident
        b.add_runs(b.run_rows("task_id=? ORDER BY queued_at", [ident]), relation="task")
        t = conn.execute("SELECT created_at FROM tasks WHERE id=?", (ident,)).fetchone()
        base_ts = t[0] if t else None
    else:
        b.add_runs(b.run_rows("id=?", [ident]), relation="root")
    b.children()
    t_from, t_to = b.window(base_ts)
    # lệnh con (agent/điều phối viên gọi trong run, điểm vào nội bộ) — chỉ biết qua `parent_req_id` trong log; một tầng
    kids = b.logs(t_from, t_to) if kind == "req" or b.runs else set()
    if kids:
        kid_list = sorted(kids)[:100]
        b.add_runs(b.run_rows(f"req_id IN ({_in(kid_list)})", kid_list), relation="child_req")
        b.req_ids |= set(kid_list)
        b.children()
        t_from, t_to = b.window(base_ts)
        b.items = [it for it in b.items if it["source"] != "log"]
        b.logs(t_from, t_to)
    b.run_items()
    b.audits()
    b.events(task_id)
    if t_from:
        b.req_events(sorted(b.req_ids)[:200], t_from, t_to or iso_utc(datetime.now(timezone.utc) + timedelta(seconds=1)))
    b.guards()
    b.approvals()
    items = sorted(b.items, key=lambda it: (it.get("ts") or "", _ORDER.get(it["source"], 9)))
    truncated = len(items) > limit
    found = bool(items) or bool(b.runs)
    runs = [{k: r.get(k) for k in ("id", "agent_id", "kind", "status", "outcome", "relation", "req_id", "parent_run_id", "task_id")} for r in b.runs.values()]
    return {"id": ident, "kind": kind, "found": found, "items": items[:limit], "runs": runs, "req_ids": sorted(b.req_ids),
            "notes": list(dict.fromkeys(b.notes)), "truncated": truncated, "window": {"from": t_from, "to": t_to}}


_ORDER = {"audit": 0, "run": 1, "approval": 2, "guard": 3, "event": 4, "log": 5}


# ---------------------------------------------------------------------------- văn bản
def _detail_text(d: dict[str, Any]) -> str:
    parts = []
    for k, v in d.items():
        if k == "exc":
            continue
        s = str(v)
        if k in ("msg", "summary"):
            parts.append(s[:200])
        else:
            parts.append(f"{k}={s[:80]}")
    return " · ".join(parts)


def text(res: dict[str, Any]) -> str:
    """Dòng thời gian dạng chữ (đã qua `termsafe.clean` — chữ log/audit là DỮ LIỆU)."""
    from .termsafe import one_line

    head = {"req": "yêu cầu", "run": "run", "task": "task"}.get(res.get("kind") or "", "")
    out = [f"Dòng thời gian {head} {res.get('id')} — {len(res.get('items') or [])} mục, {len(res.get('runs') or [])} run"]
    if not res.get("found"):
        out.append("(không tìm thấy gì — kiểm lại id; log/event cũ có thể đã bị dọn theo retention)")
    for it in res.get("items") or []:
        ts = str(it.get("ts") or "")[:23].replace("T", " ")
        src = it.get("source") or ""
        extra = []
        if it.get("outcome"):
            extra.append(str(it["outcome"]))
        if it.get("run_id") and it["source"] not in ("run",):
            extra.append(str(it["run_id"]))
        line = f"{ts}  {src:<8} {one_line(str(it.get('label') or ''), 200)}"
        if extra:
            line += "  [" + ", ".join(one_line(x, 80) for x in extra) + "]"
        det = _detail_text(it.get("detail") or {})
        if det:
            line += "  " + one_line(det, 300)
        out.append(line[:400])
    for n in res.get("notes") or []:
        out.append(f"! {n}")
    if res.get("truncated"):
        out.append("! nhiều mục hơn giới hạn — dùng --limit hoặc --json")
    return "\n".join(out)


__all__ = ["build", "text", "classify", "event_summary", "EVENT_KEYS"]
