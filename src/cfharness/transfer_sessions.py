"""Session Claude Code của hub trong gói xuất/nhập (`hub_transfer.py`, PR3 của `docs/dev-notes/plan-hub-clone.md`).

`--resume <sid>` của `claude` đọc `<claude home>/projects/<encode_cwd(cwd)>/<sid>.jsonl` (+ thư mục `<sid>/` cạnh nó: subagent,
tool-results). File này nằm NGOÀI HUB_HOME → không có trong bản sao lưu; thiếu nó thì hội thoại điều phối viên / task retry / run
`pending_resume` không chạy tiếp được ở máy mới.

XUẤT (phần `claude/`, `sections.claude.optional=True` → importer cũ bỏ qua kèm cảnh báo):
- Chọn session theo profile TỪ DB ĐÃ CHỤP (`select_sessions`): `core` = session của hội thoại điều phối viên chưa lưu trữ, của task chưa
  xong (≠ done/archived), của run dở (`running|queued|waiting_human|interrupted` chưa được chạy tiếp) + run trong `pending_resume`/
  `auto_resume_pending`; `full` = MỌI session_id hub biết (`sessions`, `runs.session_id|resumed_from_session|forked_from_session`,
  `manager_conversations`). Session không thuộc hub KHÔNG BAO GIỜ bị lấy (chỉ tìm đúng tên `<sid>.jsonl` của danh sách trên).
- Tìm `<claude home>/projects/*/<sid>.jsonl` (một lượt liệt kê các thư mục project), không theo symlink (thư mục project, file, cây
  `<sid>/`), trần cỡ `MAX_FILE`/`MAX_TOTAL`, file biến mất/đọc lỗi giữa chừng → `skipped`.
- CHE secret theo GIÁ TRỊ (quyết định PR3: mặc định che): giá trị trong `HUB_HOME/secrets/` + sổ `redact` của tiến trình (gồm dạng
  JSON-escape) → `***`, trên từng dòng bytes. Chỉ nội dung transcript cũ bị thay (model đọc lại thấy `***` thay vì token) — `--resume`
  vẫn chạy; token thật không bao giờ nằm trong gói ngoài `secrets.enc`. Giới hạn: giá trị secret NHIỀU DÒNG xuất hiện dạng thô (không
  escape) trong file không phải JSON lines không bị che.
- `claude/index.json` = `{version, claude_home, redacted, sessions: {sid: {cwd, project_dir, files[], bytes, masked}}, skipped[]}`.

NHẬP (`install`, sau khi hub.db + file đã vào chỗ, TRƯỚC secret): với mỗi session, cwd mới = pathmap(cwd gốc) → thư mục đích
`<claude home đích>/projects/<encode_cwd(cwd mới)>/`; `.jsonl` viết lại TỪNG DÒNG (parse JSON → viết lại mọi chuỗi + khoá chứa đường dẫn cũ
theo cùng bản đồ của hub + cặp claude home nguồn → đích → dump gọn; dòng không parse được giữ nguyên); file khác chép nguyên. Không ghi
đè: sid đã có ở bất kỳ project nào của đích → bỏ + cảnh báo. File 0600 (`O_EXCL`), thư mục MỚI 0700. Trả nhật ký `created` để hoàn tác
khi lần nhập hỏng (`rollback`). Chế độ `clone` cũng nhập session (để hội thoại điều phối viên tiếp tục ở máy mới — hai hub từ đó độc lập).

Khác OS (PR4): cwd/đường dẫn trong transcript đổi kiểu theo `pathmap` (POSIX ↔ Windows); `encode_cwd` cùng một quy tắc cho cả hai
(`C:\\Users\\x` → `C--Users-x` — SUY TỪ MÃ, chưa kiểm trên Windows thật); claude home đích trên Windows = `%USERPROFILE%\\.claude`
(`Path.home()`), hoặc `CLAUDE_CONFIG_DIR`.

`encode_cwd` = quy tắc của Claude Code (đọc từ binary 2.1.325, khớp 3.655/3.655 thư mục thật trên máy dev): mọi đơn vị UTF-16 không phải
`[A-Za-z0-9]` → `-`; dài > 200 → 200 ký tự đầu + `-` + base36(|hash kiểu Java|) — Claude Code tìm thư mục dài theo TIỀN TỐ 200 ký tự rồi
kiểm cwd, nên hash lệch (bản build khác) vẫn tìm được; `install` dùng lại thư mục cùng tiền tố đã có ở đích nếu cwd khớp.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from .pathmap import PathMap, is_abs

SECTION = "claude"
PREFIX = "claude/"
INDEX_NAME = "index.json"
INDEX_MEMBER = PREFIX + INDEX_NAME
STAGE_DIR = "claude"  # thư mục trong stage nhập (không thuộc tập sao lưu → `_apply_stage` không đụng)
INDEX_VERSION = 1
MAX_FILE = 256 * 1024 * 1024  # một file transcript
MAX_TOTAL = 4 * 1024 * 1024 * 1024  # cả phần claude/ (trước nén)
MAX_INDEX = 32 * 1024 * 1024
MAX_SANITIZED = 200  # = Claude Code (vDe)
SID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
TEST_ENV = "CFHARNESS_TEST_CLAUDE_HOME"  # chỉ có hiệu lực khi CFHARNESS_TESTING=1 (conftest: thư mục tạm rỗng)
CORE_RUN_STATUSES = ("running", "queued", "waiting_human", "interrupted")
OPEN_TASK_EXCLUDE = ("done", "archived")


# ============================================================================ claude home + mã hoá cwd
def claude_home() -> Path:
    """Thư mục cấu hình Claude Code của người dùng: `CLAUDE_CONFIG_DIR` (claude tự đọc biến này) hoặc `~/.claude`. Chỉ là chỗ ĐỌC/GHI
    file session — hub không bao giờ đặt `CLAUDE_CONFIG_DIR` cho tiến trình `claude` (mất OAuth, spike M0)."""
    if os.environ.get("CFHARNESS_TESTING") == "1" and os.environ.get(TEST_ENV):
        return Path(os.environ[TEST_ENV])
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".claude"


def _utf16_units(s: str) -> list[int]:
    b = s.encode("utf-16-le", "surrogatepass")
    return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]


def _java_hash(s: str) -> int:
    """`(h << 5) - h + charCodeAt(i) | 0` trên đơn vị UTF-16 — hàm băm Claude Code dùng cho tên thư mục dài."""
    h = 0
    for u in _utf16_units(s):
        h = (h * 31 + u) & 0xFFFFFFFF
    return h - (1 << 32) if h >= (1 << 31) else h


def _b36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out


def sanitize(cwd: str) -> str:
    """`cwd.replace(/[^a-zA-Z0-9]/g, "-")` của JS — theo ĐƠN VỊ UTF-16 (ký tự ngoài BMP → 2 dấu `-`)."""
    return "".join(chr(u) if (48 <= u <= 57 or 65 <= u <= 90 or 97 <= u <= 122) else "-" for u in _utf16_units(cwd))


def encode_cwd(cwd: str) -> str:
    """Tên thư mục `projects/<…>` của Claude Code cho `cwd` (hàm thuần, xem docstring module)."""
    s = sanitize(cwd)
    if len(s) <= MAX_SANITIZED:
        return s
    return f"{s[:MAX_SANITIZED]}-{_b36(abs(_java_hash(cwd)))}"


def _first_cwd(jsonl: Path, limit: int = 200) -> str | None:
    """Trường `cwd` đầu tiên trong một transcript (đọc tối đa `limit` dòng)."""
    try:
        with open(jsonl, "rb") as f:
            for i, line in enumerate(f):
                if i >= limit:
                    break
                if b'"cwd"' not in line:
                    continue
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if isinstance(o, dict) and isinstance(o.get("cwd"), str) and o["cwd"]:
                    return o["cwd"]
    except OSError:
        return None
    return None


def project_dir_for(root: Path, cwd: str) -> Path:
    """Thư mục project của `cwd` dưới `root` (= `<claude home>/projects`). Tên dài (> 200): dùng lại thư mục cùng tiền tố 200 ký tự đã
    có nếu transcript trong đó có đúng `cwd` (giống cách Claude Code tìm), không thì tên tự tính."""
    name = encode_cwd(cwd)
    if len(name) > MAX_SANITIZED:
        pref = sanitize(cwd)[:MAX_SANITIZED] + "-"
        try:
            for e in sorted(os.scandir(root), key=lambda e: e.name):
                if e.name != name and e.name.startswith(pref) and e.is_dir(follow_symlinks=False):
                    for f in os.scandir(e.path):
                        if f.name.endswith(".jsonl") and f.is_file(follow_symlinks=False) and _first_cwd(Path(f.path)) == cwd:
                            return Path(e.path)
        except OSError:
            pass
    return root / name


def find_session(home: Path, cwd: str, sid: str) -> Path | None:
    """Hợp đồng với `claude --resume <sid>` chạy trong `cwd`: file transcript Claude Code sẽ đọc (None = không thấy).
    Thư mục chính xác `projects/<encode_cwd(cwd)>/`; tên dài → mọi thư mục cùng tiền tố 200 ký tự có transcript cwd khớp."""
    root = Path(home) / "projects"
    cand = root / encode_cwd(cwd) / f"{sid}.jsonl"
    if cand.is_file() and not cand.is_symlink():
        return cand
    s = sanitize(cwd)
    if len(s) > MAX_SANITIZED:
        pref = s[:MAX_SANITIZED] + "-"
        try:
            for e in os.scandir(root):
                p = Path(e.path) / f"{sid}.jsonl"
                if e.name.startswith(pref) and e.is_dir(follow_symlinks=False) and p.is_file() and _first_cwd(p) == cwd:
                    return p
        except OSError:
            return None
    return None


# ============================================================================ chọn session (từ DB đã chụp)
def _cols(c: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in c.execute(f'PRAGMA table_info("{table}")')}


def select_sessions(db: Path, profile: str) -> dict[str, dict[str, Any]]:
    """`{sid: {reasons: [...], cwd: runs.cwd mới nhất | None}}` — chỉ session_id hub biết (xem docstring module)."""
    out: dict[str, dict[str, Any]] = {}

    def add(sid: Any, reason: str) -> None:
        if isinstance(sid, str) and SID_RE.match(sid):
            out.setdefault(sid, {"reasons": [], "cwd": None})
            if reason not in out[sid]["reasons"]:
                out[sid]["reasons"].append(reason)

    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover
        return out
    try:
        tabs = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rcols = _cols(c, "runs") if "runs" in tabs else set()
        if "manager_conversations" in tabs:
            mc = _cols(c, "manager_conversations")
            where = "" if profile == "full" or "archived_at" not in mc else " WHERE archived_at IS NULL"
            for (sid,) in c.execute(f"SELECT session_id FROM manager_conversations{where}"):
                add(sid, "manager")
        if profile == "full":
            if "sessions" in tabs:
                for (sid,) in c.execute("SELECT id FROM sessions"):
                    add(sid, "session")
            for col in ("session_id", "resumed_from_session", "forked_from_session"):
                if col in rcols:
                    for (sid,) in c.execute(f'SELECT DISTINCT "{col}" FROM runs WHERE "{col}" IS NOT NULL'):
                        add(sid, "run")
        else:
            if "tasks" in tabs and "task_id" in rcols:
                q = ("SELECT DISTINCT r.session_id FROM runs r JOIN tasks t ON t.id = r.task_id WHERE r.session_id IS NOT NULL "
                     f"AND t.status NOT IN ({','.join('?' * len(OPEN_TASK_EXCLUDE))})")
                for (sid,) in c.execute(q, OPEN_TASK_EXCLUDE):
                    add(sid, "task")
            if "session_id" in rcols:
                cont = " AND continued_in_run_id IS NULL" if "continued_in_run_id" in rcols else ""
                q = f"SELECT DISTINCT session_id FROM runs WHERE session_id IS NOT NULL AND status IN ({','.join('?' * len(CORE_RUN_STATUSES))}){cont}"
                for (sid,) in c.execute(q, CORE_RUN_STATUSES):
                    add(sid, "run_open")
            if "hub_state" in tabs and "session_id" in rcols:
                ids: list[str] = []
                for key in ("pending_resume", "auto_resume_pending"):
                    row = c.execute("SELECT value FROM hub_state WHERE key=?", (key,)).fetchone()
                    try:
                        v = json.loads(row[0]) if row else None
                    except (TypeError, ValueError):
                        v = None
                    if isinstance(v, dict):
                        v = list(v)
                    ids += [x for x in (v or []) if isinstance(x, str)]
                for rid in ids:
                    r = c.execute("SELECT session_id FROM runs WHERE id=?", (rid,)).fetchone()
                    if r:
                        add(r[0], "pending_resume")
        if out and "session_id" in rcols and "cwd" in rcols:
            for sid in out:
                r = c.execute("SELECT cwd FROM runs WHERE session_id=? AND cwd IS NOT NULL ORDER BY queued_at DESC LIMIT 1", (sid,)).fetchone()
                if r:
                    out[sid]["cwd"] = r[0]
    except sqlite3.Error:
        pass
    finally:
        c.close()
    return out


def locate(home: Path, sids: Iterable[str]) -> dict[str, list[Path]]:
    """`{sid: [<projects>/<dir>/<sid>.jsonl, …]}` — một lượt liệt kê thư mục project, không theo symlink."""
    want = {f"{s}.jsonl": s for s in sids}
    out: dict[str, list[Path]] = {}
    root = Path(home) / "projects"
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            return out
        dirs = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return out
    for d in dirs:
        try:
            if not d.is_dir(follow_symlinks=False):
                continue
            for f in os.scandir(d.path):
                sid = want.get(f.name)
                if sid and f.is_file(follow_symlinks=False):
                    out.setdefault(sid, []).append(Path(f.path))
        except OSError:
            continue
    return out


def _session_tree(jsonl: Path) -> list[tuple[str, Path]]:
    """[(tên tương đối trong `claude/`, file)] của một session: `<sid>.jsonl` + cây `<sid>/` (không theo symlink)."""
    sid = jsonl.name[: -len(".jsonl")]
    out = [(f"{sid}.jsonl", jsonl)]
    sub = jsonl.parent / sid
    try:
        if not stat.S_ISDIR(os.lstat(sub).st_mode):
            return out
    except OSError:
        return out
    for dirpath, dirnames, filenames in os.walk(sub, followlinks=False):
        dp = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not (dp / d).is_symlink() and _safe_name(d))
        for fn in sorted(filenames):
            full = dp / fn
            if not _safe_name(fn):
                continue
            try:
                if not stat.S_ISREG(os.lstat(full).st_mode):
                    continue
            except OSError:
                continue
            out.append((f"{sid}/{full.relative_to(sub).as_posix()}", full))
    return out


def _safe_name(n: str) -> bool:
    return bool(n) and n not in (".", "..") and "/" not in n and "\\" not in n and "\x00" not in n


# ============================================================================ che secret theo giá trị
def secret_masker(home: Path) -> Callable[[bytes], bytes] | None:
    """Hàm che theo GIÁ TRỊ (bytes → bytes): secret trong `home/secrets/` + sổ `redact` của tiến trình, mọi dạng (thô, JSON-escape)."""
    from . import redact as R
    from .secrets import NAME_RE

    vals: set[str] = set()
    sd = Path(home) / "secrets"
    try:
        names = [n for n in os.listdir(sd) if NAME_RE.match(n)]
    except OSError:
        names = []
    for n in names:
        p = sd / n
        try:
            if p.is_file() and not p.is_symlink():
                v = p.read_bytes().decode("utf-8")
            else:
                continue
        except (OSError, UnicodeDecodeError):
            continue
        for x in {v, v.strip()}:
            if len(x.strip()) >= R.MIN_VALUE_LEN:
                vals |= R._variants(x)
    vals |= set(R._values)
    vals = {v for v in vals if len(v) >= R.MIN_VALUE_LEN}
    if not vals:
        return None
    rx = re.compile(b"|".join(re.escape(v.encode("utf-8")) for v in sorted(vals, key=len, reverse=True)))
    mask = R.MASK.encode()
    return lambda b: rx.sub(mask, b)


# ============================================================================ xuất
def export_into(add: Callable[[str, Any], dict[str, Any]], db: Path, home: Path, profile: str, tmpdir: Path,
                *, chome: Path | None = None) -> tuple[list[dict[str, Any]], dict[str, Any] | None, list[str]]:
    """Thêm phần `claude/` vào gói qua `add(tên, fileobj) -> meta` (của hub_transfer). Trả (meta các phần tử, mục `sections.claude` |
    None khi không có session nào, danh sách bỏ qua)."""
    chome = Path(chome) if chome is not None else claude_home()
    wanted = select_sessions(db, profile)
    skipped: list[str] = []
    files: list[dict[str, Any]] = []
    if not wanted:
        return files, None, skipped
    found = locate(chome, wanted)
    masker = secret_masker(home)
    index: dict[str, Any] = {"version": INDEX_VERSION, "claude_home": str(chome), "redacted": masker is not None, "profile": profile,
                             "sessions": {}, "missing": sorted(s for s in wanted if s not in found)}
    total = 0
    for sid in sorted(found):
        paths = found[sid]
        if len(paths) > 1:  # cùng sid ở nhiều project (hiếm: thư mục đổi tên) → lấy bản mới nhất
            paths = sorted(paths, key=lambda p: (p.stat().st_mtime if p.exists() else 0), reverse=True)
            skipped.append(f"{sid}: có ở {len(found[sid])} thư mục project — lấy bản mới nhất")
        jsonl = paths[0]
        cwd = _first_cwd(jsonl) or wanted[sid].get("cwd")
        entry: dict[str, Any] = {"cwd": cwd, "project_dir": jsonl.parent.name, "files": [], "bytes": 0, "masked": 0,
                                 "reasons": wanted[sid]["reasons"]}
        for rel, full in _session_tree(jsonl):
            name = PREFIX + rel
            try:
                size = os.lstat(full).st_size
                if size > MAX_FILE:
                    skipped.append(f"{name}: lớn hơn trần {MAX_FILE} byte")
                    continue
                if total + size > MAX_TOTAL:
                    skipped.append(f"{name}: vượt trần tổng phần claude/ ({MAX_TOTAL} byte)")
                    continue
                meta, changed = _add_file(add, name, full, masker, tmpdir)
            except (OSError, ValueError) as e:  # biến mất / đọc lỗi giữa chừng
                skipped.append(f"{name}: {e}")
                if rel.endswith(".jsonl") and "/" not in rel:
                    break  # không có transcript chính → bỏ cả session
                continue
            total += int(meta["size"])
            files.append(meta)
            entry["files"].append(rel)
            entry["bytes"] += int(meta["size"])
            entry["masked"] += int(changed)
        if entry["files"] and entry["files"][0] == f"{sid}.jsonl":
            index["sessions"][sid] = entry
    if not index["sessions"]:
        return files, None, skipped  # file lẻ (nếu có) vẫn trong manifest — importer chỉ đọc theo index
    index["skipped"] = skipped
    ib = json.dumps(index, ensure_ascii=False, indent=1).encode("utf-8")
    import io

    meta = add(INDEX_MEMBER, io.BytesIO(ib))
    files.append(meta)
    sec = {"prefix": PREFIX, "count": len(files), "bytes": sum(int(f["size"]) for f in files), "optional": True,
           "sessions": len(index["sessions"]), "redacted": index["redacted"]}
    return files, sec, skipped


def _add_file(add: Callable[[str, Any], dict[str, Any]], name: str, full: Path, masker: Callable[[bytes], bytes] | None,
              tmpdir: Path) -> tuple[dict[str, Any], bool]:
    fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as f:
        if masker is None:
            return add(name, f), False
        changed = False
        with tempfile.TemporaryFile(dir=tmpdir) as tmp:
            for line in f:
                nl = masker(line)
                changed = changed or nl != line
                tmp.write(nl)
            tmp.seek(0)
            return add(name, tmp), changed


# ============================================================================ nhập
def member_dest(parts: tuple[str, ...]) -> PurePosixPath | None:
    """Tên phần tử `claude/…` (đã tách, an toàn) → đường dẫn trong stage. Chỉ `index.json`, `<sid>.jsonl`, `<sid>/<…>`."""
    if len(parts) < 2 or parts[0] != SECTION:
        return None
    if len(parts) == 2:
        n = parts[1]
        if n == INDEX_NAME or (n.endswith(".jsonl") and SID_RE.match(n[: -len(".jsonl")])):
            return PurePosixPath(STAGE_DIR, n)
        return None
    if SID_RE.match(parts[1]) and all(_safe_name(p) for p in parts[2:]):
        return PurePosixPath(STAGE_DIR, *parts[1:])
    return None


def read_index(stage_claude: Path) -> dict[str, Any] | None:
    p = Path(stage_claude) / INDEX_NAME
    try:
        if not p.is_file() or p.stat().st_size > MAX_INDEX:
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or not isinstance(d.get("sessions"), dict):
        return None
    return d


def _entries(idx: dict[str, Any], stage_claude: Path) -> Iterable[tuple[str, dict[str, Any], list[str]]]:
    for sid, e in sorted(idx["sessions"].items()):
        if not isinstance(sid, str) or not SID_RE.match(sid) or not isinstance(e, dict):
            continue
        rels = [r for r in (e.get("files") or []) if isinstance(r, str)]
        ok = []
        for r in rels:
            parts = tuple(r.split("/"))
            if not (r == f"{sid}.jsonl" or (len(parts) >= 2 and parts[0] == sid and all(_safe_name(p) for p in parts))):
                continue
            src = Path(stage_claude).joinpath(*parts)
            if src.is_file() and not src.is_symlink():
                ok.append(r)
        if f"{sid}.jsonl" in ok:
            yield sid, e, ok


def session_map(pm: PathMap, idx: dict[str, Any], target: Path) -> PathMap:
    """Bản đồ đường dẫn cho transcript: bản đồ của hub + claude home nguồn → claude home đích."""
    pairs = list(pm.pairs)
    src = idx.get("claude_home")
    if isinstance(src, str) and is_abs(src):  # PR4: claude home nguồn có thể là `C:\\Users\\x\\.claude`
        pairs.append((src, str(target)))
    return PathMap(pairs)


def _existing_sids(root: Path) -> set[str]:
    out: set[str] = set()
    try:
        for d in os.scandir(root):
            if not d.is_dir(follow_symlinks=False):
                continue
            for f in os.scandir(d.path):
                if f.name.endswith(".jsonl"):
                    out.add(f.name[: -len(".jsonl")])
                elif SID_RE.match(f.name) and f.is_dir(follow_symlinks=False):
                    out.add(f.name)
    except OSError:
        pass
    return out


def plan(stage_claude: Path, target: Path, pm: PathMap) -> dict[str, Any]:
    """Kế hoạch (không ghi): số session, dung lượng, sid trùng ở đích, cwd chưa biết."""
    return _run(stage_claude, target, pm, apply=False)[0]


def install(stage_claude: Path, target: Path, pm: PathMap) -> tuple[dict[str, Any], list[Path]]:
    """Ghi session vào `<target>/projects/…` (xem docstring module). Trả (báo cáo, `created` để `rollback`). Lỗi giữa chừng → tự gỡ phần
    đã ghi của lần gọi này rồi ném lại."""
    return _run(stage_claude, target, pm, apply=True)


def _run(stage_claude: Path, target: Path, pm: PathMap, *, apply: bool) -> tuple[dict[str, Any], list[Path]]:
    target = Path(target)
    rep: dict[str, Any] = {"present": False, "target": str(target), "sessions": 0, "files": 0, "bytes": 0, "conflicts": [],
                           "cwd_unknown": [], "written": [], "redacted": None, "missing_at_export": 0}
    created: list[Path] = []
    idx = read_index(stage_claude) if Path(stage_claude).is_dir() else None
    if idx is None:
        return rep, created
    rep["present"] = True
    rep["redacted"] = bool(idx.get("redacted"))
    rep["missing_at_export"] = len(idx.get("missing") or [])
    root = target / "projects"
    existing = _existing_sids(root)
    spm = session_map(pm, idx, target)
    needles = [n.encode("utf-8") for n in spm.needles()]
    try:
        for sid, e, rels in _entries(idx, stage_claude):
            if sid in existing:
                rep["conflicts"].append(sid)
                continue
            cwd = e.get("cwd") if isinstance(e.get("cwd"), str) and is_abs(e.get("cwd") or "") else None
            if cwd is None:
                rep["cwd_unknown"].append(sid)
                pd = str(e.get("project_dir") or "")
                if not _safe_name(pd):
                    continue
                dest_dir = root / pd
            else:
                dest_dir = project_dir_for(root, spm.rewrite(cwd, syntax="text"))
            rep["sessions"] += 1
            for r in rels:
                src = Path(stage_claude).joinpath(*r.split("/"))
                rep["files"] += 1
                rep["bytes"] += src.stat().st_size
                if not apply:
                    continue
                dst = dest_dir.joinpath(*r.split("/"))
                _mkdirs(dst.parent, created)
                _write_new(src, dst, spm if r.endswith(".jsonl") else None, needles, created)
            if apply:
                rep["written"].append({"sid": sid, "dir": dest_dir.name})
    except BaseException:
        if apply:
            rollback(created)
        raise
    return rep, created


def _mkdirs(d: Path, created: list[Path]) -> None:
    missing: list[Path] = []
    p = d
    while not p.exists() and not p.is_symlink():
        missing.append(p)
        if p.parent == p:
            break
        p = p.parent
    for m in reversed(missing):
        m.mkdir(mode=0o700)
        os.chmod(m, 0o700)
        created.append(m)
    if d.is_symlink() or not d.is_dir():
        raise OSError(f"{d} không phải thư mục thường")


def _write_new(src: Path, dst: Path, pm: PathMap | None, needles: list[bytes], created: list[Path]) -> None:
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)  # không bao giờ ghi đè
    created.append(dst)
    with os.fdopen(fd, "wb") as out, open(src, "rb") as f:
        if pm is None or not pm:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                out.write(chunk)
        else:
            for line in f:
                out.write(rewrite_line(line, pm, needles))
    os.chmod(dst, 0o600)


def _walk(o: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(o, str):
        return fn(o)
    if isinstance(o, dict):
        return {(fn(k) if isinstance(k, str) else k): _walk(v, fn) for k, v in o.items()}
    if isinstance(o, list):
        return [_walk(x, fn) for x in o]
    return o


def rewrite_line(line: bytes, pm: PathMap, needles: list[bytes]) -> bytes:
    """Một dòng JSON lines: có đường dẫn cũ → parse, viết lại mọi chuỗi/khoá, dump gọn (giữ ký tự Unicode); không parse được → giữ nguyên."""
    if not any(n in line for n in needles):
        return line
    body = line.rstrip(b"\r\n")
    tail = line[len(body):]
    try:
        obj = json.loads(body)
    except ValueError:
        return line
    new = _walk(obj, pm.rewrite)
    if new == obj:
        return line
    return json.dumps(new, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + tail


def rollback(created: list[Path]) -> None:
    """Gỡ file/thư mục do `install` tạo (ngược thứ tự). Cố gắng hết sức — không ném."""
    for p in reversed(created):
        try:
            if p.is_dir() and not p.is_symlink():
                p.rmdir()
            else:
                p.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = ["claude_home", "encode_cwd", "sanitize", "find_session", "project_dir_for", "select_sessions", "locate", "secret_masker",
           "export_into", "member_dest", "read_index", "plan", "install", "rollback", "rewrite_line", "SECTION", "PREFIX", "STAGE_DIR"]
