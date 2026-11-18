"""Chuỗi băm nhật ký kiểm toán (L4, docs/dev-notes/plan-logging.md §4 A6).

Mỗi dòng `audit_log` từ migration v30 mang:
- `prev_hash` = `row_hash` của dòng ngay trước (theo id). Dòng chuỗi ĐẦU TIÊN: `"genesis:" + sha256(canonical_json(dòng cuối trước v30))`,
  hoặc `"genesis"` khi bảng trống;
- `row_hash` = `sha256(prev_hash ‖ canonical_json(CHAIN_COLUMNS))` — `canonical_json` = JSON khoá sắp xếp, không khoảng trắng, UTF-8.

`CHAIN_COLUMNS` là HỢP ĐỒNG: thêm/bớt/đổi tên cột trong danh sách = thay đổi phá vỡ (mọi dòng đã ghi sẽ không kiểm lại được). Cột mới của
`audit_log` sau này KHÔNG tự vào chuỗi — muốn băm thì phải thêm phiên bản chuỗi mới (vd `CHAIN_COLUMNS_V2` + cột ghi phiên bản).

Ghi: MỌI chỗ INSERT vào `audit_log` đi qua `insert_chained` (AuditLog.record, hub_freeze ngoại tuyến, hub_transfer trên stage). Đọc dòng cuối
và ghi trong CÙNG transaction (`BEGIN IMMEDIATE` nếu người gọi chưa mở) → hai writer không thể nối vào cùng một dòng. id được cấp tường minh
= max(sqlite_sequence, MAX(id)) + 1 để băm được cả id; nhờ vậy trong vùng có chuỗi id LIÊN TIẾP — khoảng hở = có dòng bị xoá.

Neo (`AuditChain.anchor`): định kỳ (`audit.anchor_interval_s`, mặc định 3600 s), lúc daemon khởi động và lúc dừng, ghi `(last_id, row_hash, ts)` vào
- `HUB_HOME/audit-anchors.jsonl` (0600, chỉ append; nằm trong `backup.INCLUDE_FILES` → đi cùng bản sao lưu/gói xuất, khôi phục về đúng bản khớp DB);
- file metadata admin (`admin_audit`, kind `audit.anchor`, chỉ id + băm) khi bộ xuất trạng thái admin bật — bản này root sở hữu thư mục, người
  có quyền ghi hub.db không sửa được;
- event `audit.anchored` (→ Telegram/Slack) CHỈ khi `audit.anchor_notify: true` (mặc định tắt — Q4).
Không neo vào `integrity_baseline`: bảng đó nằm ngay trong hub.db, ai sửa được `audit_log` thì sửa được luôn bản neo → không thêm bảo vệ gì.

Kiểm (`verify_db`): duyệt theo id, báo chỗ gãy ĐẦU TIÊN (`unhashed_row`, `id_gap`, `prev_mismatch`, `hash_mismatch`) + đối chiếu các bản neo
gần nhất (`anchor_missing` = id đã neo không còn → bị xoá; `anchor_mismatch` = băm khác → bị sửa rồi tính lại chuỗi). Gãy → event
`audit.tamper_detected` (payload chỉ id/mã lý do) + dòng audit cùng tên. Daemon khởi động kiểm phần đuôi (`audit.verify_on_start_rows`).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger("cfharness.audit_chain")

# HỢP ĐỒNG — đổi danh sách này là thay đổi phá vỡ (xem docstring module).
CHAIN_COLUMNS: tuple[str, ...] = (
    "id", "ts", "kind", "actor", "channel", "agent_id", "run_id", "target", "summary", "data",
    "req_id", "outcome", "error_code", "duration_ms", "principal", "declared_channel", "session_ref", "ip", "caller_run_id",
)
INT_COLUMNS = frozenset({"id"})
REAL_COLUMNS = frozenset({"duration_ms"})
GENESIS = "genesis"
ANCHORS_FILE = "audit-anchors.jsonl"
ANCHOR_VERSION = 1
ANCHORS_CHECKED = 50  # số bản neo gần nhất đem đối chiếu
ANCHORS_MAX_READ = 8 * 1024 * 1024  # file lớn hơn → chỉ đọc phần đuôi
BREAK_REASONS = ("unhashed_row", "id_gap", "prev_mismatch", "hash_mismatch", "archive_missing")  # L5: archive_missing = dòng đầu DB sau mốc lưu trữ không nối được manifest
ANCHOR_REASONS = ("anchor_missing", "anchor_mismatch")
_HEX64 = frozenset("0123456789abcdef")
_SELECT_COLS = ", ".join(CHAIN_COLUMNS) + ", prev_hash, row_hash"


def is_hash(v: Any) -> bool:
    return isinstance(v, str) and len(v) == 64 and set(v) <= _HEX64


# ============================================================================ băm (thuần)
def normalize(row: Mapping[str, Any]) -> dict[str, Any]:
    """Giá trị đúng kiểu như SQLite trả lại (TEXT → str, id → int, duration_ms → float) để băm lúc ghi = băm lúc đọc."""
    out: dict[str, Any] = {}
    for c in CHAIN_COLUMNS:
        v = row.get(c)
        if v is None:
            out[c] = None
        elif c in INT_COLUMNS:
            out[c] = int(v)
        elif c in REAL_COLUMNS:
            out[c] = float(v)
        else:
            out[c] = v if isinstance(v, str) else str(v)
    return out


def canonical_json(row: Mapping[str, Any]) -> bytes:
    return json.dumps(normalize(row), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_hash(prev_hash: str, row: Mapping[str, Any]) -> str:
    return hashlib.sha256(prev_hash.encode("utf-8") + canonical_json(row)).hexdigest()


def prev_for(last: Mapping[str, Any] | None) -> str:
    """`prev_hash` cho dòng nối sau `last` (dòng cuối hiện có, None = bảng trống)."""
    if last is None:
        return GENESIS
    if last.get("row_hash"):
        return str(last["row_hash"])
    return GENESIS + ":" + hashlib.sha256(canonical_json(last)).hexdigest()


# ============================================================================ ghi
def has_chain(conn: sqlite3.Connection) -> bool:
    return any(r[1] == "row_hash" for r in conn.execute("PRAGMA table_info(audit_log)"))


def _as_dict(cur: sqlite3.Cursor, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {d[0]: row[i] for i, d in enumerate(cur.description)}


def last_row(conn: sqlite3.Connection) -> dict[str, Any] | None:
    cur = conn.execute(f"SELECT {_SELECT_COLS} FROM audit_log ORDER BY id DESC LIMIT 1")
    return _as_dict(cur, cur.fetchone())


def _next_id(conn: sqlite3.Connection, last: Mapping[str, Any] | None) -> int:
    seq = 0
    try:
        r = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='audit_log'").fetchone()
        seq = int(r[0]) if r and r[0] is not None else 0
    except sqlite3.Error:  # pragma: no cover — bảng không AUTOINCREMENT
        pass
    return max(seq, int(last["id"]) if last else 0) + 1


def insert_chained(conn: sqlite3.Connection, values: Mapping[str, Any], *, chained: bool | None = None) -> int:
    """INSERT một dòng `audit_log` có nối chuỗi. `values` = các cột (trừ id/prev_hash/row_hash). Người gọi đang trong transaction → ghi
    trong đó (đã giữ khoá ghi nếu đã ghi gì; nếu chưa thì INSERT đầu tiên lấy khoá); chưa → tự mở `BEGIN IMMEDIATE`.
    DB chưa lên v30 (stage của gói cũ) → INSERT thường (dòng trước chuỗi; migration v30 nối genesis từ nó). `chained` = kết quả `has_chain`
    người gọi đã biết (bỏ một PRAGMA mỗi dòng)."""
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN IMMEDIATE")
    try:
        if not (has_chain(conn) if chained is None else chained):
            cols = [c for c in CHAIN_COLUMNS if c != "id" and values.get(c) is not None]
            cur = conn.execute(f"INSERT INTO audit_log ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", [values[c] for c in cols])
            rid = int(cur.lastrowid or 0)
        else:
            last = last_row(conn)
            rid = _next_id(conn, last)
            row = normalize({**values, "id": rid})
            prev = prev_for(last)
            h = compute_hash(prev, row)
            cols = [*CHAIN_COLUMNS, "prev_hash", "row_hash"]
            conn.execute(f"INSERT INTO audit_log ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                         [*(row[c] for c in CHAIN_COLUMNS), prev, h])
        if own:
            conn.execute("COMMIT")
        return rid
    except BaseException:
        if own and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


# ============================================================================ kiểm
def _iter_rows(conn: sqlite3.Connection, start_id: int) -> Iterable[dict[str, Any]]:
    cur = conn.execute(f"SELECT {_SELECT_COLS} FROM audit_log WHERE id >= ? ORDER BY id", (start_id,))
    names = [d[0] for d in cur.description]
    while True:
        batch = cur.fetchmany(1000)
        if not batch:
            return
        for r in batch:
            yield dict(zip(names, r))


def archive_cutoff(conn: sqlite3.Connection) -> int:
    """L5 (A9): id lớn nhất đã chuyển ra `audit-archive/` (`hub_state['audit.archive_cutoff']`, 0 = chưa lưu trữ)."""
    try:
        r = conn.execute("SELECT value FROM hub_state WHERE key='audit.archive_cutoff'").fetchone()
        return int(json.loads(r[0])) if r else 0
    except (sqlite3.Error, ValueError, TypeError):
        return 0


def verify_chain(conn: sqlite3.Connection, *, since_id: int | None = None, tail: int | None = None,
                 archive: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Kiểm chuỗi từ `since_id` (hoặc `tail` dòng cuối; mặc định toàn bảng). Dòng trước điểm bắt đầu được TIN (không kiểm lại).

    L5 (A9): dòng đã lưu trữ không còn trong DB → dòng đầu tiên còn lại nối với mục manifest có `last_id = id - 1` (`archive` =
    MANIFEST.json đã đọc): `prev_hash` phải bằng `next_prev` của mục đó. Có mốc lưu trữ mà không có mục nối được → `archive_missing`."""
    out: dict[str, Any] = {"ok": True, "checked": 0, "unchained": 0, "first_id": None, "last_id": None, "chain_start_id": None, "break": None}
    if not has_chain(conn):
        out["no_chain"] = True
        return out
    start = 1
    if since_id is not None:
        start = max(1, int(since_id))
    elif tail is not None:
        mx = conn.execute("SELECT MAX(id) FROM audit_log").fetchone()[0] or 0
        start = max(1, int(mx) - max(1, int(tail)) + 1)
    cur = conn.execute(f"SELECT {_SELECT_COLS} FROM audit_log WHERE id < ? ORDER BY id DESC LIMIT 1", (start,))
    prev = _as_dict(cur, cur.fetchone())
    started = bool(prev and prev.get("row_hash"))
    prev_id: int | None = int(prev["id"]) if prev else None
    exp_prev = prev_for(prev)
    cutoff = archive_cutoff(conn)
    if cutoff:
        out["archive_cutoff"] = cutoff

    def brk(rid: int, reason: str, expected: Any, actual: Any) -> dict[str, Any]:
        out["ok"] = False
        out["break"] = {"id": rid, "reason": reason, "expected": expected, "actual": actual}
        return out

    if prev is None:
        first = conn.execute("SELECT MIN(id) FROM audit_log WHERE id >= ?", (start,)).fetchone()[0]
        entries = list((archive or {}).get("archives") or [])
        link = next((e for e in entries if isinstance(e, dict) and first is not None and e.get("last_id") == int(first) - 1), None)
        if link is not None:
            prev_id, exp_prev, started = int(link["last_id"]), str(link.get("next_prev") or ""), bool(link.get("chained"))
            out["archive_link_id"] = prev_id
        elif cutoff and first is not None and int(first) > 1:
            return brk(int(first), "archive_missing", f"manifest last_id={int(first) - 1}", None)
    if started:
        cs = conn.execute("SELECT MIN(id) FROM audit_log WHERE row_hash IS NOT NULL").fetchone()[0]
        out["chain_start_id"] = cs
    for r in _iter_rows(conn, start):
        res = _walk_row(r, out, prev_id, exp_prev, started)
        if res is not None:
            return brk(*res)
        prev_id, started = int(r["id"]), started or bool(r.get("row_hash"))
        exp_prev = prev_for(r)
    return out


def _walk_row(r: Mapping[str, Any], out: dict[str, Any], prev_id: int | None, exp_prev: str, started: bool) -> tuple[int, str, Any, Any] | None:
    """Một bước kiểm chuỗi (dùng chung cho DB và file lưu trữ). Trả (id, lý do, mong đợi, thực tế) khi gãy."""
    rid = int(r["id"])
    out["checked"] += 1
    out["first_id"] = out["first_id"] or rid
    out["last_id"] = rid
    if not r.get("row_hash"):
        if started:
            return rid, "unhashed_row", "row_hash", None
        out["unchained"] += 1
        return None
    if started and prev_id is not None and rid != prev_id + 1:
        return rid, "id_gap", prev_id + 1, rid
    if r.get("prev_hash") != exp_prev:
        return rid, "prev_mismatch", exp_prev, r.get("prev_hash")
    h = compute_hash(str(r["prev_hash"]), r)
    if h != r["row_hash"]:
        return rid, "hash_mismatch", h, r["row_hash"]
    if not started:
        out["chain_start_id"] = out["chain_start_id"] or rid
    return None


# ============================================================================ bản neo
def anchors_path(home: Path | str) -> Path:
    return Path(home) / ANCHORS_FILE


def read_anchors(path: Path) -> dict[str, Any]:
    """Đọc file neo (không theo symlink; file lớn → phần đuôi). Dòng hỏng chỉ đếm."""
    out: dict[str, Any] = {"exists": False, "anchors": [], "bad_lines": 0}
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return out
    except OSError as e:
        out["error"] = f"open_failed:{e.errno or 0}"
        return out
    try:
        size = os.fstat(fd).st_size
        if size > ANCHORS_MAX_READ:
            os.lseek(fd, size - ANCHORS_MAX_READ, os.SEEK_SET)
        raw = os.read(fd, ANCHORS_MAX_READ)
    finally:
        os.close(fd)
    out["exists"] = True
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > ANCHORS_MAX_READ and lines:
        lines = lines[1:]  # dòng đầu có thể bị cắt
    for ln in lines:
        if not ln.strip():
            continue
        try:
            a = json.loads(ln)
        except ValueError:
            out["bad_lines"] += 1
            continue
        lid = a.get("last_id") if isinstance(a, dict) else None
        if not isinstance(lid, int) or isinstance(lid, bool) or lid < 1 or not is_hash(a.get("row_hash")):
            out["bad_lines"] += 1
            continue
        out["anchors"].append({"last_id": lid, "row_hash": a["row_hash"], "ts": str(a.get("ts") or "")[:40], "reason": str(a.get("reason") or "")[:20]})
    return out


def check_anchors(conn: sqlite3.Connection, anchors: list[dict[str, Any]], *, limit: int = ANCHORS_CHECKED,
                  archived: Mapping[int, str] | None = None) -> dict[str, Any]:
    """`archived` (L5) = {id: row_hash} các bản neo trỏ vào dòng đã lưu trữ (manifest ghi lại lúc lưu trữ, sau khi đã đối chiếu)."""
    recent = anchors[-limit:] if limit else anchors
    out: dict[str, Any] = {"ok": True, "checked": len(recent), "latest": (recent[-1] if recent else None), "problems": []}
    for a in recent:
        r = conn.execute("SELECT row_hash FROM audit_log WHERE id=?", (a["last_id"],)).fetchone()
        if r is None and archived and a["last_id"] in archived:
            r = (archived[a["last_id"]],)
            out["archived"] = int(out.get("archived") or 0) + 1
        if r is None:
            out["problems"].append({"id": a["last_id"], "reason": "anchor_missing", "expected": a["row_hash"], "actual": None})
        elif r[0] != a["row_hash"]:
            out["problems"].append({"id": a["last_id"], "reason": "anchor_mismatch", "expected": a["row_hash"], "actual": r[0]})
    out["ok"] = not out["problems"]
    return out


def verify_db(db: Path | str, home: Path | str, *, since_id: int | None = None, tail: int | None = None,
              archive: bool = False) -> dict[str, Any]:
    """Kiểm chuỗi + bản neo bằng kết nối CHỈ ĐỌC riêng (chạy được trong luồng phụ hoặc khi daemon dừng). `archive=True` (L5) còn kiểm
    từng file trong `audit-archive/` (sha256 + chuỗi + nối liền nhau và nối với DB)."""
    from . import audit_archive as AA

    t0 = time.monotonic()
    conn = sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True, timeout=10)
    try:
        man = AA.read_manifest(home)
        out = verify_chain(conn, since_id=since_id, tail=tail, archive=man)
        af = read_anchors(anchors_path(home))
        anc = check_anchors(conn, af["anchors"], archived=AA.archived_anchors(man))
        anc.update({"file": af["exists"], "bad_lines": af["bad_lines"], "total": len(af["anchors"])})
        out["anchors"] = anc
        out["ok"] = bool(out["ok"] and anc["ok"])
        if man.get("archives"):
            out["archive_files"] = len(man["archives"])
        if archive:
            arc = AA.verify_archives(home, man, conn=conn)
            out["archive"] = arc
            out["ok"] = bool(out["ok"] and arc["ok"])
    finally:
        conn.close()
    out["duration_ms"] = round((time.monotonic() - t0) * 1000, 1)
    return out


def tamper_payload(res: dict[str, Any]) -> dict[str, Any]:
    """Payload event/audit: CHỈ id + mã lý do (không băm, không nội dung dòng)."""
    p: dict[str, Any] = {"checked": int(res.get("checked") or 0)}
    b = res.get("break")
    if b:
        p["break_id"] = int(b["id"])
        p["reason"] = b["reason"] if b.get("reason") in BREAK_REASONS else "other"
    probs = ((res.get("anchors") or {}).get("problems") or [])[:5]
    if probs:
        p["anchors"] = [{"id": int(x["id"]), "reason": x["reason"] if x.get("reason") in ANCHOR_REASONS else "other"} for x in probs]
    from .audit_archive import ARCHIVE_REASONS, FILE_RE

    aprobs = ((res.get("archive") or {}).get("problems") or [])[:5]
    if aprobs:  # L5: chỉ tên file đúng mẫu `<yyyy-mm>[-<id>].jsonl.gz` + mã lý do
        p["archive"] = [{"file": str(x.get("file")) if FILE_RE.match(str(x.get("file") or "")) else "?",
                         "reason": x["reason"] if x.get("reason") in ARCHIVE_REASONS else "other"} for x in aprobs]
    return p


def signature(res: dict[str, Any]) -> str:
    return json.dumps(tamper_payload(res) | {"checked": 0}, sort_keys=True)


# ============================================================================ dịch vụ trong daemon
class AuditChain:
    TAMPER_STATE = "audit.tamper_reported"

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._task: asyncio.Task[Any] | None = None
        self._last_anchor_id: int | None = None
        self.last_verify: dict[str, Any] | None = None

    @property
    def path(self) -> Path:
        return anchors_path(self.hub.paths.home)

    def cfg(self, key: str, default: Any) -> Any:
        try:
            v = self.hub.config.get(f"audit.{key}")
        except KeyError:
            return default
        return default if v is None else v

    # ------------------------------------------------------------------ neo
    def _busy(self) -> bool:
        """Đang sao lưu / xuất gói: KHÔNG neo (bản neo ghi sau khi DB đã chụp sẽ trỏ tới id mà bản sao không có → báo nhầm khi khôi phục).
        Đọc đuôi chuỗi TRƯỚC khi kiểm hàm này → không còn kẽ hở."""
        try:
            from . import backup, hub_transfer
            from .runs import exporting

            return backup._LOCK.locked() or hub_transfer._LOCK.locked() or exporting(self.hub)
        except Exception:  # pragma: no cover
            log.debug("không xác định được trạng thái sao lưu/xuất gói", exc_info=True)
            return False

    def tail(self) -> dict[str, Any] | None:
        r = self.hub.store.conn.execute("SELECT id, row_hash, ts FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        if r is None or not r[1]:
            return None
        return {"last_id": int(r[0]), "row_hash": str(r[1]), "row_ts": str(r[2])}

    def anchor(self, reason: str = "interval", *, force: bool = False) -> dict[str, Any] | None:
        try:
            t = self.tail()
        except sqlite3.Error:
            log.exception("không đọc được đuôi audit_log để neo")
            return None
        if t is None:
            return None
        if self._busy():
            log.debug("đang sao lưu/xuất gói — bỏ lượt neo audit")
            return None
        if self._last_anchor_id is None:
            last = read_anchors(self.path)["anchors"]
            self._last_anchor_id = last[-1]["last_id"] if last else 0
        if not force and t["last_id"] == self._last_anchor_id:
            return None
        from .events import iso

        rec = {"v": ANCHOR_VERSION, "last_id": t["last_id"], "row_hash": t["row_hash"], "ts": iso(), "reason": reason}
        line = (json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                os.write(fd, line)
                try:
                    os.fsync(fd)
                except OSError:  # pragma: no cover
                    pass
            finally:
                os.close(fd)
            if os.name == "posix":
                os.chmod(self.path, 0o600)
        except OSError as e:
            log.warning("không ghi được bản neo audit %s: %s", self.path, e)
            return None
        self._last_anchor_id = t["last_id"]
        aa = getattr(self.hub, "admin_audit", None)
        if aa is not None:
            try:
                aa.tap("audit.anchor", "hub", "internal", {"anchor_id": t["last_id"], "anchor_hash": t["row_hash"]})
            except Exception:  # pragma: no cover
                log.exception("không ghi được bản neo vào metadata admin")
        if self.cfg("anchor_notify", False):
            from .events import make_event

            self.hub.publish(make_event("audit.anchored", None, {"last_id": t["last_id"], "row_hash": t["row_hash"], "reason": reason},
                                        source="hub"))
        return rec

    # ------------------------------------------------------------------ kiểm
    def verify(self, *, since_id: int | None = None, tail: int | None = None, report: bool = True, dedupe: bool = False,
               archive: bool = False) -> dict[str, Any]:
        res = verify_db(self.hub.paths.db, self.hub.paths.home, since_id=since_id, tail=tail, archive=archive)
        self.last_verify = {k: res.get(k) for k in ("ok", "checked", "last_id")}
        if not res["ok"] and report:
            self.report(res, dedupe=dedupe)
        return res

    def report(self, res: dict[str, Any], *, dedupe: bool = False) -> None:
        sig = signature(res)
        if dedupe:
            try:
                if self.hub.store.state_get(self.TAMPER_STATE) == sig:
                    return
            except Exception:  # pragma: no cover
                log.debug("không đọc được trạng thái tamper đã báo", exc_info=True)
        payload = tamper_payload(res)
        log.error("audit_log bị sửa/xoá ngoài hub: %s", payload)
        try:
            from .events import make_event

            self.hub.publish(make_event("audit.tamper_detected", None, payload, source="hub"))
        except Exception:  # pragma: no cover
            log.exception("không phát được audit.tamper_detected")
        try:
            self.hub.audit.record("audit.tamper_detected", None, None, dict(payload), actor="hub", channel="runtime")  # type: ignore[attr-defined]
            self.hub.store.state_set(self.TAMPER_STATE, sig)
        except Exception:  # pragma: no cover
            log.exception("không ghi được audit.tamper_detected")

    # ------------------------------------------------------------------ vòng đời
    def start(self) -> None:
        rows = int(self.cfg("verify_on_start_rows", 1000) or 0)
        if rows > 0:
            try:  # sửa trong lúc daemon dừng → phát hiện ngay khi lên
                self.verify(tail=rows, dedupe=True)
            except Exception:
                log.exception("không kiểm được chuỗi băm audit lúc khởi động")
        self.anchor("start")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._loop(), name="audit-anchor")

    async def _loop(self) -> None:
        while True:
            iv = max(1.0, float(self.cfg("anchor_interval_s", 3600) or 3600))
            await asyncio.sleep(iv)
            try:
                self.anchor("interval")
            except Exception:  # pragma: no cover
                log.exception("lượt neo audit lỗi")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        try:
            self.anchor("stop")
        except Exception:  # pragma: no cover
            log.exception("không neo được audit khi dừng")


def attach(hub: "Hub") -> AuditChain:
    hub.audit_chain = AuditChain(hub)  # type: ignore[attr-defined]
    return hub.audit_chain  # type: ignore[attr-defined]


__all__ = ["ANCHORS_FILE", "CHAIN_COLUMNS", "GENESIS", "AuditChain", "anchors_path", "attach", "canonical_json", "check_anchors",
           "compute_hash", "insert_chained", "prev_for", "read_anchors", "tamper_payload", "verify_chain", "verify_db"]
