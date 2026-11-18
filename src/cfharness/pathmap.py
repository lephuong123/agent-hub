r"""Viết lại đường dẫn tuyệt đối trong `hub.db` + file cấu hình theo NHIỀU cặp `cũ → mới` — lõi dùng chung của
`home migrate` (home_migrate.py: đổi HUB_HOME) và `hub import` (hub_transfer.py: nhập hub từ máy khác, đổi HUB_HOME + `$HOME`
+ `--map-path`, kể cả KHÁC hệ điều hành — PR4 của docs/dev-notes/plan-hub-clone.md §5b).

- Thay trong MỘT lượt quét (một regex hợp mọi dạng đường dẫn cũ, dài trước): chữ vừa thay không bao giờ bị cặp khác thay tiếp
  (vd `/Users/a/.cfharness → /Users/a/hub2` rồi `/Users/a → /home/b` không biến `/Users/a/hub2` thành `/home/b/hub2`).
- Chỉ khớp ĐÚNG đường dẫn: theo sau là hết chuỗi, phân cách đường dẫn, dấu nháy, khoảng trắng, phân cách JSON — không khớp `<cũ>2`.
- Biết CẢ HAI kiểu đường dẫn bất kể hệ điều hành đang chạy (kiểu của từng phía suy từ chính chuỗi: `C:\…`/`C:/…`/`\\máy\share` = Windows,
  `/…` = POSIX). Cặp Windows khớp 3 dạng: thô `C:\a\b`, JSON-escape `C:\\a\\b`, dạng `/` `C:/a/b`; chữ ổ đĩa không phân biệt hoa/thường.
- KHÁC kiểu (POSIX ↔ Windows): sau khi thay tiền tố, đổi dấu phân cách của PHẦN ĐUÔI — đúng token đường dẫn bắt đầu bằng tiền tố cũ,
  không đụng `/` ở chữ khác (URL, phép chia…). Token dừng ở: khoảng trắng (trừ khi chữ tiếp theo là một đoạn đường dẫn tương đối như
  `Application Support/x` — không bắt đầu bằng `/`, `\`, `-`, có dấu phân cách), dấu nháy, `` ` ``, `<>|,;:&`, ký tự điều khiển, `\` không
  thuộc dạng đang đổi (escape JSON như `\n`, `\"`), `)`/`]`/`}` không có cặp mở trong token. POSIX → Windows trong chuỗi JSON (giá trị DB
  là JSON hợp lệ, file `.json`) hoặc trong nháy kép YAML → dạng JSON-escape (`C:\\Users\\b`) để chuỗi vẫn hợp lệ; ngoài ra dạng thô.
- DB: mọi cột chữ của bảng KHÔNG append-only (`APPEND_ONLY`), trừ cột lịch sử (`HISTORY_COLS`), bảng ảo FTS + bảng bóng của nó,
  và bảng người gọi loại thêm (`skip_tables`, vd tri thức graph `mem_*` thuộc ledger).
- File: `REWRITE_FILES` (cấu hình cấp hub) được viết lại + cập nhật `integrity_baseline` khi file đang khớp bản chuẩn; file chữ khác
  có đường dẫn cũ (memory/skill/config agent — thuộc ledger) chỉ được LIỆT KÊ để người/luồng sau xử lý.
Cùng kiểu POSIX → POSIX giữ ĐÚNG hành vi trước PR4 (home migrate).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from .oscompat import IS_WINDOWS

APPEND_ONLY = frozenset({"events", "audit_log", "guard_decisions", "snapshots", "ledger_entries", "integrity_baseline", "sqlite_sequence",
                         "session_messages"})  # v17: văn bản hội thoại là lịch sử; FTS chỉ đồng bộ theo INSERT/DELETE
HISTORY_COLS = frozenset({("runs", "argv_redacted"), ("runs", "diff")})
REWRITE_FILES = ("config.yaml", "policy/*.yaml", "policy/golden/*", "mcp/registry.yaml", "checkpoints/*/checkpoint.json")
SCAN_SKIP_DIRS = ("checkpoints", "runs", "logs", "snapshots", "ledger", "prompts", "policy/history", "trash/tamper", "worktrees", "evals", ".git",
                  "audit-archive")  # L5: lịch sử audit có băm — không bao giờ viết lại
TEXT_SUFFIX = (".yaml", ".yml", ".json", ".md", ".txt", ".toml")
MAX_SCAN = 2 * 1024 * 1024

OsKind = Literal["posix", "windows"]
Syntax = Literal["auto", "json", "yaml", "text"]
_BS = "\\"
WIN_ABS_RE = re.compile(r"^(?:[A-Za-z]:(?:[\\/]|$)|\\\\[^\\/]+[\\/][^\\/]+)")
# ký tự kết thúc token đường dẫn ở phần đuôi (ngoài khoảng trắng/điều khiển/`\` lạ/ngoặc đóng lẻ)
_TAIL_STOP = frozenset("\"'`<>|,;:&")
_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = frozenset(")]}")


def host_os() -> OsKind:
    """Họ hệ điều hành ĐANG chạy theo kiểu đường dẫn (macOS/Linux = `posix`)."""
    return "windows" if IS_WINDOWS else "posix"


def path_style(p: str) -> OsKind | None:
    """Kiểu của một đường dẫn TUYỆT ĐỐI (None = không tuyệt đối theo kiểu nào) — không phụ thuộc OS đang chạy."""
    s = str(p or "")
    if WIN_ABS_RE.match(s):
        return "windows"
    if s.startswith("/") and not s.startswith("//"):
        return "posix"
    return None


def is_abs(p: str) -> bool:
    """Tuyệt đối theo kiểu POSIX HOẶC Windows (khác `os.path.isabs`: `C:\\x` là tuyệt đối cả trên macOS)."""
    return path_style(p) is not None


def to_style(p: str, os_kind: OsKind) -> str:
    """Chuẩn hoá dấu phân cách theo kiểu đích (không đổi tiền tố): Windows → `\\`, POSIX giữ nguyên."""
    return str(p).replace("/", _BS) if os_kind == "windows" else str(p)


@dataclass(frozen=True)
class _Form:
    """Một dạng chữ của đường dẫn cũ + cách thay. `sep`: dấu phân cách của dạng cũ (`/`, `\\`, hoặc `\\\\` khi JSON-escape)."""

    text: str
    sep: str
    win: bool  # dạng Windows: chữ ổ đĩa không phân biệt hoa/thường, `\` là ranh giới hợp lệ
    new_plain: str
    new_quoted: str
    dsep_plain: str
    dsep_quoted: str

    @property
    def ctx(self) -> bool:  # kết quả phụ thuộc ngữ cảnh (trong chuỗi JSON / nháy kép YAML hay không)
        return self.new_plain != self.new_quoted or self.dsep_plain != self.dsep_quoted


def _forms(o: str, n: str) -> list[_Form]:
    so = path_style(o) or "posix"
    sn = path_style(n) or "posix"
    if so == "posix":
        if sn == "posix":
            return [_Form(o, "/", False, n, n, "/", "/")]
        return [_Form(o, "/", False, n, n.replace(_BS, _BS * 2), _BS, _BS * 2)]
    oesc, ofwd = o.replace(_BS, _BS * 2), o.replace(_BS, "/")
    if sn == "windows":
        nesc, nfwd = n.replace(_BS, _BS * 2), n.replace(_BS, "/")
        return [_Form(o, _BS, True, n, n, _BS, _BS), _Form(oesc, _BS * 2, True, nesc, nesc, _BS * 2, _BS * 2),
                _Form(ofwd, "/", True, nfwd, nfwd, "/", "/")]
    return [_Form(o, _BS, True, n, n, "/", "/"), _Form(oesc, _BS * 2, True, n, n, "/", "/"), _Form(ofwd, "/", True, n, n, "/", "/")]


def _form_regex(f: _Form, cross: bool) -> str:
    t = f.text
    if f.win and len(t) >= 2 and t[1] == ":" and t[0].isalpha():
        lit = f"[{t[0].upper()}{t[0].lower()}]" + re.escape(t[1:])
    else:
        lit = re.escape(t)
    if f.win or cross:  # dạng Windows / POSIX → Windows: `\` sau tiền tố là dấu phân cách hoặc escape JSON
        bound = r"(?=$|[\\/\"'\s,}\]:;)])"
    else:  # POSIX → POSIX: đúng ranh giới trước PR4
        bound = r"(?=$|[/\"'\s,}\]:;)])"
    return f"({lit}){bound}"


def _looks_json(s: str) -> bool:
    t = s.lstrip()
    if not t or t[0] not in "{[\"":
        return False
    try:
        json.loads(s)
    except ValueError:
        return False
    return True


def _in_dquote(s: str, pos: int) -> bool:
    """`pos` nằm trong chuỗi nháy kép trên CÙNG dòng (đếm `"` không bị escape) — YAML nháy kép / JSON nhiều dòng."""
    start = s.rfind("\n", 0, pos) + 1
    inside = False
    i = start
    while i < pos:
        c = s[i]
        if c == _BS and inside:
            i += 2
            continue
        if c == '"':
            inside = not inside
        i += 1
    return inside


def _word_end(s: str, j: int) -> int:
    k = j
    while k < len(s) and not s[k].isspace() and s[k] not in _TAIL_STOP and ord(s[k]) >= 32:
        k += 1
    return k


def _tail_end(s: str, i: int, sep: str) -> int:
    """Chỉ số hết token đường dẫn bắt đầu ngay sau tiền tố cũ (xem docstring module)."""
    n = len(s)
    depth = 0
    while i < n:
        if s.startswith(sep, i):
            i += len(sep)
            continue
        c = s[i]
        if c == _BS or c in _TAIL_STOP or ord(c) < 32:
            break
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            if depth == 0:
                break
            depth -= 1
        elif c.isspace():
            if c != " ":
                break
            k = _word_end(s, i + 1)
            word = s[i + 1:k]
            if word and word[0] not in "/\\-~$" and sep in word:
                i += 1
                continue
            break
        i += 1
    return i


class PathMap:
    """Bản đồ đường dẫn `cũ → mới` (nhiều cặp, mỗi phía POSIX hoặc Windows). Cặp trùng `cũ` → cặp SAU thắng (người ghi đè bản đồ tự tính)."""

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
        forms: dict[str, tuple[_Form, bool]] = {}
        for o, n in self.pairs:
            cross = (path_style(o) or "posix") != (path_style(n) or "posix")
            for f in _forms(o, n):
                key = f.text[0].lower() + f.text[1:] if f.win and len(f.text) >= 2 and f.text[1] == ":" else f.text
                forms.setdefault(key, (f, cross or f.win))
        ordered = sorted(forms.values(), key=lambda x: len(x[0].text), reverse=True)
        self._forms: list[_Form] = [f for f, _c in ordered]
        self._re: re.Pattern[str] | None = None
        if ordered:
            self._re = re.compile("|".join(_form_regex(f, c) for f, c in ordered))

    def __bool__(self) -> bool:
        return bool(self.pairs)

    def needles(self) -> list[str]:
        """Mọi dạng đường dẫn cũ cần tìm (LIKE trong DB, bytes trong file) — dạng Windows kèm cả chữ ổ đĩa hoa lẫn thường."""
        out: list[str] = []
        for f in self._forms:
            out.append(f.text)
            if f.win and len(f.text) >= 2 and f.text[1] == ":" and f.text[0].isalpha():
                out.append(f.text[0].swapcase() + f.text[1:])
        return list(dict.fromkeys(out))

    def rewrite(self, s: str, *, syntax: Syntax = "auto") -> str:
        """Thay mọi đường dẫn cũ trong `s` (một lượt). `syntax` chỉ có tác dụng khi có cặp POSIX → Windows: `json` = mọi khớp nằm trong
        chuỗi JSON (dạng escape), `yaml` = escape khi trong nháy kép, `text` = dạng thô, `auto` = `json` nếu `s` là JSON hợp lệ."""
        if self._re is None:
            return s
        out: list[str] = []
        i = 0
        ctx: str | None = None
        while True:
            m = self._re.search(s, i)
            if m is None:
                break
            f = self._forms[(m.lastindex or 1) - 1]
            start, end = m.span()
            quoted = False
            if f.ctx:
                if ctx is None:
                    ctx = syntax if syntax != "auto" else ("json" if _looks_json(s) else "text")
                quoted = ctx == "json" or (ctx == "yaml" and _in_dquote(s, start))
            new = f.new_quoted if quoted else f.new_plain
            dsep = f.dsep_quoted if quoted else f.dsep_plain
            out.append(s[i:start])
            out.append(new)
            if dsep != f.sep:
                tend = _tail_end(s, end, f.sep)
                out.append(s[end:tend].replace(f.sep, dsep))
                i = tend
            else:
                i = end
        if not out:
            return s
        out.append(s[i:])
        return "".join(out)

    def as_list(self) -> list[dict[str, str]]:
        return [{"from": o, "to": n} for o, n in self.pairs]

    def target_styles(self) -> set[str]:
        return {path_style(n) or "posix" for _o, n in self.pairs}


def _norm(p: str) -> str:
    s = str(p or "")
    if path_style(s) == "windows":
        s = s.replace("/", _BS)
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


def syntax_for(rel: str) -> Syntax:
    """Cú pháp của file theo đuôi — quyết định dạng `\\` khi viết đường dẫn POSIX → Windows."""
    low = rel.lower()
    if low.endswith((".json", ".jsonl")):
        return "json"
    if low.endswith((".yaml", ".yml")):
        return "yaml"
    return "auto"


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
            nb = pm.rewrite(text, syntax=syntax_for(rel)).encode("utf-8")
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


__all__ = ["PathMap", "path_style", "is_abs", "host_os", "to_style", "syntax_for", "APPEND_ONLY", "HISTORY_COLS", "REWRITE_FILES", "SCAN_SKIP_DIRS", "db_targets", "db_rewrite", "db_count_matches",
           "scan_files", "rewrite_files"]
