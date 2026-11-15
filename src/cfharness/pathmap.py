"""Viết lại đường dẫn tuyệt đối trong `hub.db` + file cấu hình theo NHIỀU cặp `cũ → mới` — lõi dùng chung của
`home migrate` (home_migrate.py: đổi HUB_HOME) và `hub import` (hub_transfer.py: nhập hub từ máy khác, đổi HUB_HOME + `$HOME`
+ `--map-path`).

- Thay trong MỘT lượt quét (một regex hợp mọi đường dẫn cũ, dài trước): chữ vừa thay không bao giờ bị cặp khác thay tiếp
  (vd `/Users/a/.cfharness → /Users/a/hub2` rồi `/Users/a → /home/b` không biến `/Users/a/hub2` thành `/home/b/hub2`).
- Chỉ khớp ĐÚNG đường dẫn: theo sau là hết chuỗi, phân cách đường dẫn, dấu nháy, khoảng trắng, phân cách JSON — không khớp `<cũ>2`.
- DB: mọi cột chữ của bảng KHÔNG append-only (`APPEND_ONLY`), trừ cột lịch sử (`HISTORY_COLS`), bảng ảo FTS + bảng bóng của nó,
  và bảng người gọi loại thêm (`skip_tables`, vd tri thức graph `mem_*` thuộc ledger).
- File: `REWRITE_FILES` (cấu hình cấp hub) được viết lại + cập nhật `integrity_baseline` khi file đang khớp bản chuẩn; file chữ khác
  có đường dẫn cũ (memory/skill/config agent — thuộc ledger) chỉ được LIỆT KÊ để người/luồng sau xử lý.
Chỉ biết dạng đường dẫn của hệ điều hành ĐANG chạy (Windows: thêm biến thể JSON-escape `\\\\` và dạng `/`) — nhập khác hệ điều hành
là việc của bản sau (docs/dev-notes/plan-hub-clone.md §5b).
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from .oscompat import IS_WINDOWS

APPEND_ONLY = frozenset({"events", "audit_log", "guard_decisions", "snapshots", "ledger_entries", "integrity_baseline", "sqlite_sequence",
                         "session_messages"})  # v17: văn bản hội thoại là lịch sử; FTS chỉ đồng bộ theo INSERT/DELETE
HISTORY_COLS = frozenset({("runs", "argv_redacted"), ("runs", "diff")})
REWRITE_FILES = ("config.yaml", "policy/*.yaml", "policy/golden/*", "mcp/registry.yaml", "checkpoints/*/checkpoint.json")
SCAN_SKIP_DIRS = ("checkpoints", "runs", "logs", "snapshots", "ledger", "prompts", "policy/history", "trash/tamper", "worktrees", "evals", ".git")
TEXT_SUFFIX = (".yaml", ".yml", ".json", ".md", ".txt", ".toml")
MAX_SCAN = 2 * 1024 * 1024


def _variants(old: str, new: str) -> list[tuple[str, str]]:
    """Cặp (cũ, mới) cần thay. Windows: thêm dạng JSON-escape (`\\` trong cột JSON của DB) và dạng `/`."""
    out = [(old, new)]
    if IS_WINDOWS:
        bs = chr(92)
        out += [(old.replace(bs, bs * 2), new.replace(bs, bs * 2)), (old.replace(bs, "/"), new.replace(bs, "/"))]
    return list(dict.fromkeys(out))


class PathMap:
    """Bản đồ đường dẫn `cũ → mới` (nhiều cặp). Cặp trùng `cũ` → cặp SAU thắng (người ghi đè bản đồ tự tính)."""

    def __init__(self, pairs: Iterable[tuple[str, str]]) -> None:
        m: dict[str, str] = {}
        for old, new in pairs:
            o = _norm(old)
            n = _norm(new)
            if not o or o == n:
                m.pop(o, None)
                continue
            m[o] = n
        self.pairs: list[tuple[str, str]] = sorted(m.items(), key=lambda p: len(p[0]), reverse=True)
        repl: dict[str, str] = {}
        for o, n in self.pairs:
            for vo, vn in _variants(o, n):
                repl.setdefault(vo, vn)
        self._repl = repl
        self._re: re.Pattern[str] | None = None
        if repl:
            sep = r"/\\" if IS_WINDOWS else "/"
            alt = "|".join(re.escape(o) for o in sorted(repl, key=len, reverse=True))
            self._re = re.compile("(?:" + alt + r")(?=$|[" + sep + r"\"'\s,}\]:;)])")

    def __bool__(self) -> bool:
        return bool(self.pairs)

    def needles(self) -> list[str]:
        """Mọi dạng đường dẫn cũ cần tìm (LIKE trong DB, bytes trong file)."""
        return list(self._repl)

    def rewrite(self, s: str) -> str:
        if self._re is None:
            return s
        return self._re.sub(lambda m: self._repl[m.group(0)], s)

    def as_list(self) -> list[dict[str, str]]:
        return [{"from": o, "to": n} for o, n in self.pairs]


def _norm(p: str) -> str:
    s = str(p or "")
    while len(s) > 1 and s.endswith(("/", "\\")) and not re.fullmatch(r"[A-Za-z]:[\\/]", s):
        s = s[:-1]
    return s


# ============================================================================ DB
def db_targets(conn: sqlite3.Connection, skip_tables: Iterable[str] = ()) -> list[tuple[str, str]]:
    """(bảng, cột) chữ được phép viết lại. `skip_tables`: tên bảng hoặc tiền tố kết thúc bằng `*` (vd `mem_*`)."""
    skips = list(skip_tables)
    out: list[tuple[str, str]] = []
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    # bảng ảo (FTS5) + bảng bóng của nó (`<tên>_data|_idx|_config…`, có bảng WITHOUT ROWID) — không ghi tay vào chỉ mục
    virtual = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL%'")]
    for t in tables:
        if t in APPEND_ONLY or any(t == v or t.startswith(v + "_") for v in virtual) or _skipped(t, skips):
            continue
        for col in conn.execute(f'PRAGMA table_info("{t}")'):
            name, decl = col[1], (col[2] or "").upper()
            if (t, name) in HISTORY_COLS or any(x in decl for x in ("INT", "REAL", "BLOB")):
                continue
            out.append((t, name))
    return out


def _skipped(table: str, skips: list[str]) -> bool:
    return any(table.startswith(s[:-1]) if s.endswith("*") else table == s for s in skips)


def db_rewrite(db: Path, pm: PathMap, *, apply: bool, skip_tables: Iterable[str] = ()) -> dict[str, int]:
    """Đếm (và nếu `apply` thì ghi) số giá trị có đường dẫn cũ theo `bảng.cột`. Một transaction; lỗi → ROLLBACK."""
    counts: dict[str, int] = {}
    if not db.exists() or not pm:
        return counts
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        if apply:
            conn.execute("BEGIN IMMEDIATE")
        needles = pm.needles()
        for t, c in db_targets(conn, skip_tables):
            like = " OR ".join(f'"{c}" LIKE ?' for _ in needles)
            rows = conn.execute(f'SELECT rowid, "{c}" FROM "{t}" WHERE typeof("{c}")=\'text\' AND ({like})', [f"%{o}%" for o in needles]).fetchall()
            n = 0
            for rowid, val in rows:
                nv = pm.rewrite(val)
                if nv != val:
                    n += 1
                    if apply:
                        conn.execute(f'UPDATE "{t}" SET "{c}"=? WHERE rowid=?', (nv, rowid))
            if n:
                counts[f"{t}.{c}"] = n
        if apply:
            conn.execute("COMMIT")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        if apply and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return counts


def db_count_matches(db: Path, pm: PathMap, tables: Iterable[str]) -> int:
    """Số hàng (mọi cột chữ) của các bảng `tables` (tên hoặc tiền tố `x*`) còn chứa đường dẫn cũ — báo cáo phần KHÔNG tự viết lại."""
    if not db.exists() or not pm:
        return 0
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        wanted = list(tables)
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND sql NOT LIKE 'CREATE VIRTUAL%'")]
        total = 0
        for t in names:
            if not _skipped(t, wanted):
                continue
            cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{t}")') if not any(x in (c[2] or "").upper() for x in ("INT", "REAL", "BLOB"))]
            if not cols:
                continue
            conds = " OR ".join(f'(typeof("{c}")=\'text\' AND "{c}" LIKE ?)' for c in cols for _ in pm.needles())
            args = [f"%{o}%" for _c in cols for o in pm.needles()]
            total += int(conn.execute(f'SELECT COUNT(*) FROM "{t}" WHERE {conds}', args).fetchone()[0])
        return total
    finally:
        conn.close()


# ============================================================================ file
def _rel_matches(rel: str, pats: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(rel, p) for p in pats)


def scan_files(home: Path, pm: PathMap, *, rewrite_globs: tuple[str, ...] = REWRITE_FILES,
               skip_dirs: tuple[str, ...] = SCAN_SKIP_DIRS) -> tuple[list[str], list[str]]:
    """(file cấp hub sẽ viết lại, file khác chứa đường dẫn cũ) — tương đối với `home`, luôn dạng `/`."""
    rewrite: list[str] = []
    review: list[str] = []
    if not pm:
        return rewrite, review
    needles = [o.encode() for o in pm.needles()]
    for dirpath, _dirnames, filenames in os.walk(home, followlinks=False):
        rel_dir = os.path.relpath(dirpath, home).replace(os.sep, "/")  # rel luôn dạng `/` (khớp REWRITE_FILES, baseline)
        rel_dir = "" if rel_dir == "." else rel_dir
        for fn in filenames:
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            full = Path(dirpath) / fn
            is_target = _rel_matches(rel, rewrite_globs) and not rel.startswith("policy/history/")
            if not is_target:
                if not fn.endswith(TEXT_SUFFIX) or any(rel == d or rel.startswith(d + "/") for d in skip_dirs):
                    continue
            try:
                if full.is_symlink() or full.stat().st_size > MAX_SCAN:
                    continue
                b = full.read_bytes()
            except OSError:
                continue
            if any(n in b for n in needles):
                (rewrite if is_target else review).append(rel)
    return sorted(rewrite), sorted(review)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def rewrite_files(home: Path, rels: list[str], pm: PathMap, *, db: Path | None = None) -> list[str]:
    """Viết lại file (giữ quyền); cập nhật integrity_baseline cho file đang khớp bản chuẩn. Trả danh sách bản chuẩn đã cập nhật."""
    db = db if db is not None else home / "hub.db"
    conn = sqlite3.connect(str(db), isolation_level=None) if db.exists() else None
    updated: list[str] = []
    try:
        has_baseline = conn is not None and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='integrity_baseline'").fetchone() is not None
        for rel in rels:
            p = home / rel
            raw = p.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            nb = pm.rewrite(text).encode("utf-8")
            if nb == raw:
                continue
            mode = p.stat().st_mode & 0o777
            tmp = p.with_name(p.name + ".migrate-tmp")
            tmp.write_bytes(nb)
            os.chmod(tmp, mode)
            os.replace(tmp, p)
            if conn is not None and has_baseline:
                row = conn.execute("SELECT sha256 FROM integrity_baseline WHERE path=?", (rel,)).fetchone()
                if row is not None and row[0] == _sha(raw):
                    conn.execute("UPDATE integrity_baseline SET sha256=?, content=?, updated_at=? WHERE path=?",
                                 (_sha(nb), nb, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), rel))
                    updated.append(rel)
    finally:
        if conn is not None:
            conn.close()
    return updated


__all__ = ["PathMap", "APPEND_ONLY", "HISTORY_COLS", "REWRITE_FILES", "SCAN_SKIP_DIRS", "db_targets", "db_rewrite", "db_count_matches",
           "scan_files", "rewrite_files"]
