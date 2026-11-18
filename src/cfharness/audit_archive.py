"""Lưu trữ nhật ký kiểm toán ra file (L5, docs/dev-notes/plan-logging.md §4 A9). TẮT mặc định (Q3: giữ audit vĩnh viễn trong DB).

Bật bằng `audit.archive_after_days: N` (> 0). Job nền của daemon (`AuditArchiver`, mỗi `audit.archive_tick_s`) dồn các dòng `audit_log` thuộc
những THÁNG (UTC, theo `ts`) đã trọn vẹn trước mốc `bây giờ − N ngày` ra `HUB_HOME/audit-archive/<yyyy-mm>.jsonl.gz` — mỗi tháng một file
(tháng chưa trọn chờ sang tháng sau). Thứ tự cho mỗi tháng (an toàn khi daemon chết giữa chừng):

1. đọc các dòng LIÊN TIẾP từ `audit.archive_cutoff + 1`, KIỂM chuỗi băm của chúng (nối với file lưu trữ trước) — gãy → không lưu trữ
   (không "rửa" dấu vết bị sửa vào file); đối chiếu các bản neo (`audit-anchors.jsonl`) trỏ vào khoảng này;
2. ghi file tạm 0600 (thư mục 0700) → fsync → sha256 → đổi tên; mỗi dòng = đủ `CHAIN_COLUMNS` + `prev_hash` + `row_hash` (băm lại được);
3. ghi `MANIFEST.json` (tạm + đổi tên): `first_id`/`last_id`/`rows`/`first_prev`/`first_hash`/`last_hash`/`next_prev`/`chained`/`sha256`
   + các bản neo trong khoảng (`anchors`: id + băm đã đối chiếu);
4. trong MỘT transaction: đặt `hub_state['audit.archive_cutoff'] = last_id` rồi `DELETE … WHERE id <= last_id` — trigger v31
   `audit_no_delete` chỉ cho xoá dòng có id ≤ mốc này;
5. dòng audit `audit.archived` (id/số dòng/sha256).

Không bao giờ lưu trữ dòng cuối cùng của bảng (`insert_chained` cần dòng cuối để nối chuỗi). `audit verify` nối dòng đầu còn lại trong DB với
`next_prev` của mục manifest có `last_id = id − 1`; bản neo trỏ vào dòng đã lưu trữ được đối chiếu với `anchors` của manifest (neo vẫn có
giá trị); `audit verify --archive` còn băm lại từng file (sha256 + chuỗi + nối liền). Không lưu trữ khi đang sao lưu/xuất gói/đóng băng.
`audit-archive/` ∈ `backup.INCLUDE_DIRS` (đi cùng bản sao lưu + gói `.cfhx`), sandbox denyRead/denyWrite + `sensitive_read` như `logs/`.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Mapping

from .audit_chain import CHAIN_COLUMNS, _SELECT_COLS, _walk_row, archive_cutoff, normalize, prev_for, read_anchors

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger("cfharness.audit_archive")

ARCHIVE_DIR = "audit-archive"
MANIFEST = "MANIFEST.json"
MANIFEST_VERSION = 1
MANIFEST_MAX = 8 * 1024 * 1024
FILE_RE = re.compile(r"^\d{4}-\d{2}(?:-\d{1,19})?\.jsonl\.gz$")
CUTOFF_KEY = "audit.archive_cutoff"
ARCHIVE_REASONS = ("file_missing", "bad_name", "sha_mismatch", "bad_row", "count_mismatch", "manifest_mismatch", "id_gap", "unhashed_row",
                   "prev_mismatch", "hash_mismatch")
MAX_MONTHS_PER_RUN = 24


def archive_dir(home: Path | str) -> Path:
    return Path(home) / ARCHIVE_DIR


def _ensure_dir(home: Path | str) -> Path:
    d = archive_dir(home)
    d.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(d, 0o700)
    return d


def read_manifest(home: Path | str) -> dict[str, Any]:
    """MANIFEST.json (không theo symlink, trần cỡ). Không có → `{"archives": []}`; hỏng → thêm `error` (verify coi như không có mục nào)."""
    p = archive_dir(home) / MANIFEST
    try:
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {"v": MANIFEST_VERSION, "archives": []}
    except OSError as e:
        return {"v": MANIFEST_VERSION, "archives": [], "error": f"open_failed:{e.errno or 0}"}
    try:
        raw = os.read(fd, MANIFEST_MAX + 1)
    finally:
        os.close(fd)
    try:
        man = json.loads(raw.decode("utf-8")) if len(raw) <= MANIFEST_MAX else None
    except ValueError:
        man = None
    if not isinstance(man, dict) or not isinstance(man.get("archives"), list):
        return {"v": MANIFEST_VERSION, "archives": [], "error": "bad_manifest"}
    man["archives"] = sorted((e for e in man["archives"] if isinstance(e, dict) and isinstance(e.get("first_id"), int)
                              and isinstance(e.get("last_id"), int)), key=lambda e: e["first_id"])
    return man


def write_manifest(home: Path | str, man: Mapping[str, Any]) -> None:
    d = _ensure_dir(home)
    tmp = d / f".{MANIFEST}.tmp"
    data = json.dumps(man, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, d / MANIFEST)


def archived_anchors(man: Mapping[str, Any]) -> dict[int, str]:
    out: dict[int, str] = {}
    for e in man.get("archives") or []:
        for a in e.get("anchors") or []:
            if isinstance(a, dict) and isinstance(a.get("id"), int) and isinstance(a.get("row_hash"), str):
                out[a["id"]] = a["row_hash"]
    return out


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _line(row: Mapping[str, Any]) -> bytes:
    d = normalize(row)
    d["prev_hash"] = row.get("prev_hash")
    d["row_hash"] = row.get("row_hash")
    return (json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def iter_archive_rows(path: Path) -> Iterator[dict[str, Any] | None]:
    """Dòng của một file lưu trữ (None = dòng hỏng)."""
    with gzip.open(path, "rb") as f:
        for ln in f:
            try:
                d = json.loads(ln)
            except ValueError:
                yield None
                continue
            yield d if isinstance(d, dict) and isinstance(d.get("id"), int) else None


# ============================================================================ kiểm file lưu trữ
def verify_archives(home: Path | str, man: Mapping[str, Any] | None = None, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Băm lại mọi file trong manifest: sha256, chuỗi băm trong file, khớp số liệu manifest, các file nối liền nhau
    (`first_id = last_id trước + 1`, `prev_hash` dòng đầu = `next_prev` file trước). Nối với DB do `verify_chain` kiểm."""
    man = read_manifest(home) if man is None else man
    out: dict[str, Any] = {"ok": True, "files": 0, "rows": 0, "problems": []}
    if man.get("error"):
        out["ok"] = False
        out["problems"].append({"file": MANIFEST, "reason": "manifest_mismatch", "detail": man["error"]})
        return out
    d = archive_dir(home)
    prev_e: Mapping[str, Any] | None = None

    def bad(e: Mapping[str, Any], reason: str, **kw: Any) -> None:
        out["ok"] = False
        out["problems"].append({"file": str(e.get("file") or "?"), "reason": reason, **kw})

    for e in man.get("archives") or []:
        out["files"] += 1
        name = str(e.get("file") or "")
        linked = prev_e is not None and prev_e.get("last_id") == e["first_id"] - 1
        if prev_e is not None and not linked:
            bad(e, "id_gap", expected=int(prev_e["last_id"]) + 1, actual=e["first_id"])
        cur_prev = prev_e
        prev_e = e
        if not FILE_RE.match(name):
            bad(e, "bad_name")
            continue
        p = d / name
        if not p.is_file() or p.is_symlink():
            bad(e, "file_missing")
            continue
        if _sha_file(p) != e.get("sha256"):
            bad(e, "sha_mismatch")
            continue
        exp_prev = str(cur_prev.get("next_prev")) if linked and cur_prev is not None else str(e.get("first_prev") or "")
        started = bool(cur_prev.get("chained")) if linked and cur_prev is not None else False
        st: dict[str, Any] = {"checked": 0, "unchained": 0, "first_id": None, "last_id": None, "chain_start_id": None}
        prev_id: int | None = int(cur_prev["last_id"]) if linked and cur_prev is not None else None
        last: dict[str, Any] | None = None
        broke = False
        for r in iter_archive_rows(p):
            if r is None:
                bad(e, "bad_row")
                broke = True
                break
            res = _walk_row(r, st, prev_id, exp_prev, started)
            if res is not None:
                bad(e, res[1], id=res[0])
                broke = True
                break
            prev_id, started = int(r["id"]), started or bool(r.get("row_hash"))
            exp_prev = prev_for(r)
            last = r
        if broke:
            continue
        out["rows"] += st["checked"]
        if (st["checked"] != e.get("rows") or st["first_id"] != e["first_id"] or st["last_id"] != e["last_id"]
                or (last or {}).get("row_hash") != e.get("last_hash") or (last is not None and prev_for(last) != e.get("next_prev"))):
            bad(e, "count_mismatch" if st["checked"] != e.get("rows") else "manifest_mismatch")
    return out


# ============================================================================ job lưu trữ
class AuditArchiver:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._task: asyncio.Task[Any] | None = None
        self.last: dict[str, Any] | None = None

    def cfg(self, key: str, default: Any) -> Any:
        try:
            v = self.hub.config.get(f"audit.{key}")
        except KeyError:
            return default
        return default if v is None else v

    @property
    def home(self) -> Path:
        return self.hub.paths.home

    def _busy(self) -> bool:
        from .runs import export_quiesced

        if export_quiesced(self.hub):
            return True
        chain = getattr(self.hub, "audit_chain", None)
        return bool(chain is not None and chain._busy())

    def status(self) -> dict[str, Any]:
        man = read_manifest(self.home)
        arcs = man.get("archives") or []
        return {"enabled": int(self.cfg("archive_after_days", 0) or 0) > 0, "after_days": int(self.cfg("archive_after_days", 0) or 0),
                "cutoff": archive_cutoff(self.hub.store.conn), "files": len(arcs), "rows": sum(int(e.get("rows") or 0) for e in arcs),
                "last": self.last}

    def run_once(self, now: datetime | None = None) -> dict[str, Any]:
        days = int(self.cfg("archive_after_days", 0) or 0)
        if days <= 0:
            return {"skipped": "off"}
        if self._busy():
            return {"skipped": "busy"}
        now = now or datetime.now(timezone.utc)
        lim = (now - timedelta(days=days)).astimezone(timezone.utc)
        boundary = lim.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        out: dict[str, Any] = {"archived": [], "boundary": boundary}
        for _ in range(MAX_MONTHS_PER_RUN):
            try:
                r = self._one(boundary)
            except Exception as e:  # noqa: BLE001 — job nền: ghi log, lần sau thử lại (dòng chưa xoá thì không mất gì)
                log.exception("lưu trữ audit lỗi")
                out["error"] = type(e).__name__
                break
            if r is None:
                break
            if r.get("error"):
                out["error"] = r["error"]
                out["error_id"] = r.get("id")
                break
            out["archived"].append(r)
        self.last = {"at": now.isoformat(timespec="seconds"), "files": len(out["archived"]), "error": out.get("error")}
        return out

    def _one(self, boundary: str) -> dict[str, Any] | None:
        conn = self.hub.store.conn
        cutoff = archive_cutoff(conn)
        mx = conn.execute("SELECT MAX(id) FROM audit_log").fetchone()[0]
        first = conn.execute("SELECT id, ts FROM audit_log WHERE id > ? ORDER BY id LIMIT 1", (cutoff,)).fetchone()
        if mx is None or first is None or int(first[0]) >= int(mx) or str(first[1]) >= boundary:
            return None
        month = str(first[1])[:7]
        if not re.match(r"^\d{4}-\d{2}$", month):
            return {"error": "bad_ts", "id": int(first[0])}
        man = read_manifest(self.home)
        if man.get("error"):
            return {"error": "bad_manifest"}
        arcs = man["archives"]
        done = next((e for e in arcs if e["first_id"] == int(first[0])), None)
        if done is not None:  # daemon chết sau khi ghi manifest, trước khi xoá → kiểm file rồi xoá tiếp
            p = archive_dir(self.home) / str(done.get("file"))
            row = conn.execute("SELECT row_hash FROM audit_log WHERE id=?", (done["last_id"],)).fetchone()
            if not p.is_file() or _sha_file(p) != done.get("sha256") or row is None or row[0] != done.get("last_hash"):
                return {"error": "manifest_mismatch", "id": done["first_id"]}
            self._delete(cutoff, int(done["last_id"]))
            return {"file": done["file"], "first_id": done["first_id"], "last_id": done["last_id"], "rows": done.get("rows"), "resumed": True}
        link = next((e for e in arcs if e["last_id"] == int(first[0]) - 1), None)
        if link is not None:
            prev_id: int | None = int(link["last_id"])
            exp_prev, started = str(link.get("next_prev") or ""), bool(link.get("chained"))
        elif cutoff == 0:
            prev_id, exp_prev, started = None, prev_for(None), False
        else:
            return {"error": "archive_missing", "id": int(first[0])}
        names = {str(e.get("file")) for e in arcs}
        name = f"{month}.jsonl.gz" if f"{month}.jsonl.gz" not in names else f"{month}-{int(first[0])}.jsonl.gz"
        d = _ensure_dir(self.home)
        tmp = d / f".{name}.tmp"
        anchors = {a["last_id"]: a["row_hash"] for a in read_anchors(self.home / "audit-anchors.jsonl")["anchors"]}
        st: dict[str, Any] = {"checked": 0, "unchained": 0, "first_id": None, "last_id": None, "chain_start_id": None}
        first_row: dict[str, Any] | None = None
        last: dict[str, Any] | None = None
        hit: list[dict[str, Any]] = []
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        err: dict[str, Any] | None = None
        rows = self._rows(conn, cutoff, int(mx))
        try:
            with os.fdopen(fd, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=6) as gz:
                    for r in rows:
                        if str(r["ts"])[:7] != month or str(r["ts"]) >= boundary:
                            break
                        res = _walk_row(r, st, prev_id, exp_prev, started)
                        if res is not None:
                            err = {"error": res[1], "id": res[0]}
                            break
                        rid = int(r["id"])
                        if rid in anchors:
                            if anchors[rid] != r.get("row_hash"):
                                err = {"error": "anchor_mismatch", "id": rid}
                                break
                            hit.append({"id": rid, "row_hash": anchors[rid]})
                        gz.write(_line(r))
                        first_row = first_row or r
                        prev_id, started = rid, started or bool(r.get("row_hash"))
                        exp_prev = prev_for(r)
                        last = r
                raw.flush()
                os.fsync(raw.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        finally:
            rows.close()  # type: ignore[attr-defined]  # đóng cursor SELECT trước khi mở transaction xoá
        if err is not None or last is None or first_row is None:
            tmp.unlink(missing_ok=True)
            if err is not None:
                log.error("không lưu trữ audit: chuỗi băm gãy ở dòng #%s (%s) — chạy `cfharness audit verify`", err.get("id"), err.get("error"))
            return err
        sha = _sha_file(tmp)
        os.replace(tmp, d / name)
        entry = {"file": name, "month": month, "first_id": int(first_row["id"]), "last_id": int(last["id"]), "rows": st["checked"],
                 "first_prev": first_row.get("prev_hash"), "first_hash": first_row.get("row_hash"), "last_hash": last.get("row_hash"),
                 "next_prev": prev_for(last), "chained": bool(last.get("row_hash")), "sha256": sha, "bytes": (d / name).stat().st_size,
                 "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "anchors": hit}
        write_manifest(self.home, {"v": MANIFEST_VERSION, "archives": [*arcs, entry]})
        self._delete(cutoff, entry["last_id"])
        try:
            self.hub.audit.record("audit.archived", None, None,  # type: ignore[attr-defined]
                                  {k: entry[k] for k in ("file", "first_id", "last_id", "rows", "sha256")}, actor="hub", channel="runtime",
                                  target=name, summary="audit.archived", outcome="ok", principal="hub:audit_archive")
        except Exception:  # pragma: no cover
            log.exception("không ghi được audit.archived")
        log.info("đã lưu trữ audit #%s…#%s (%d dòng) → %s", entry["first_id"], entry["last_id"], entry["rows"], name)
        return {k: entry[k] for k in ("file", "first_id", "last_id", "rows", "sha256")}

    @staticmethod
    def _rows(conn: sqlite3.Connection, cutoff: int, mx: int) -> Iterator[dict[str, Any]]:
        cur = conn.execute(f"SELECT {_SELECT_COLS} FROM audit_log WHERE id > ? AND id < ? ORDER BY id", (cutoff, mx))
        try:
            names = [c[0] for c in cur.description]
            while True:
                batch = cur.fetchmany(1000)
                if not batch:
                    return
                for r in batch:
                    yield dict(zip(names, r))
        finally:
            cur.close()

    def _delete(self, old_cutoff: int, last_id: int) -> int:
        with self.hub.store.tx() as c:
            self.hub.store.state_set(CUTOFF_KEY, int(last_id))
            n = c.execute("DELETE FROM audit_log WHERE id > ? AND id <= ?", (old_cutoff, int(last_id))).rowcount
        return int(n or 0)

    # ------------------------------------------------------------------ vòng đời
    def start(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._loop(), name="audit-archive")

    async def _loop(self) -> None:
        await asyncio.sleep(120)
        while True:
            try:
                if int(self.cfg("archive_after_days", 0) or 0) > 0:
                    self.run_once()
            except Exception:  # pragma: no cover
                log.exception("lượt lưu trữ audit lỗi")
            await asyncio.sleep(max(60.0, float(self.cfg("archive_tick_s", 3600) or 3600)))

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None


def attach(hub: "Hub") -> AuditArchiver:
    hub.audit_archive = AuditArchiver(hub)  # type: ignore[attr-defined]
    return hub.audit_archive  # type: ignore[attr-defined]


__all__ = ["ARCHIVE_DIR", "ARCHIVE_REASONS", "AuditArchiver", "CHAIN_COLUMNS", "FILE_RE", "archive_dir", "archived_anchors", "attach",
           "read_manifest", "verify_archives", "write_manifest"]
