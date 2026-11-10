"""Sao lưu / khôi phục HUB_HOME (`cfharness backup …`, thẻ "Sao lưu" ở Cài đặt web). Chi tiết: `docs/dev-notes/backup.md`.

Bản sao lưu = MỘT file `HUB_HOME/backups/<id>.tar.gz` (0600, thư mục 0700) + file kèm `<id>.json` (manifest + sha256 của archive):
- `hub.db` chụp bằng SQLite ONLINE BACKUP API từ một kết nối chỉ đọc riêng (WAL: không chặn daemon ghi, ảnh nhất quán một thời điểm)
  — không bao giờ chép file thô đang mở.
- Thư mục/tệp trong `INCLUDE` (cấu hình, chính sách, tri thức agent/team/global, ledger blob, snapshot, trash, prompt đóng băng).
- MẶC ĐỊNH BỎ (`EXCLUDED`): `secrets/`, `auth/`, `mcp_server/` (token), và dữ liệu tạm/lớn tái tạo được (runs, checkpoints, logs,
  workspaces, worktrees, evals, models, update, backups). Không mã hoá: stdlib không có mã hoá đối xứng và gói `cryptography` không
  phải dependency trực tiếp → bỏ hẳn secret thay vì chép ra. Sau khi khôi phục sang máy khác phải đặt lại secret + đăng nhập web lại.
- Manifest (`MANIFEST.json`, phần tử CUỐI của tar): sha256 + cỡ từng file, `user_version` của DB, phiên bản cfharness, loại
  (`manual|auto|pre-update|pre-restore`).

Tạo (`create`) chạy trong daemon (RPC `backup.create` ∈ HUMAN_ONLY, tự audit; vòng nền theo `backup.schedule`) hoặc ngoại tuyến
(bản "pre-restore" do CLI tạo khi daemon đã dừng). Kiểm (`verify`): sha256 archive + từng file + `PRAGMA integrity_check` trên bản sao
DB giải nén ra thư mục tạm. Khôi phục (`restore`) CHỈ ngoại tuyến từ CLI của người: daemon phải dừng (giữ khoá `daemon.lock` suốt quá
trình), không chạy trong run, kiểm trước, chụp hiện trạng thành bản "pre-restore", đồng bộ tập INCLUDE về đúng bản sao lưu (giữ nguyên
phần bị loại như `agents/*/workspaces`), thay `hub.db`, rồi ghi lại bản chuẩn toàn vẹn (`integrity_baseline`) khớp với đĩa.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Iterator

from . import __version__
from .errors import Conflict, HubError, NotFound
from .paths import HubPaths

if TYPE_CHECKING:
    from .daemon import Daemon
    from .events import HubEvent
    from .hub import Hub

log = logging.getLogger("cfharness.backup")

FORMAT = "cfharness-backup"
SCHEMA = 1
MANIFEST = "MANIFEST.json"
DB_MEMBER = "hub.db"
BACKUP_DIRNAME = "backups"
KINDS = ("manual", "auto", "pre-update", "pre-restore")
SAFETY_KINDS = ("pre-update", "pre-restore")
# Mục cấp 1 được sao lưu (ngoài hub.db). Mục khác của HUB_HOME KHÔNG vào bản sao.
INCLUDE_FILES = ("config.yaml",)
INCLUDE_DIRS = ("policy", "agents", "global", "teams", "ledger", "snapshots", "mcp", "trash", "prompts")
# Trong agents/<id>/: thư mục chạy/tạm — không sao lưu, và khôi phục không đụng tới
AGENT_SKIP = ("runtime", "workspaces", ".tier_overlay")
EXCLUDED = ("secrets", "auth", "mcp_server", "runs", "checkpoints", "logs", "workspaces", "worktrees", "evals", "models", "update",
            BACKUP_DIRNAME, "hub.sock", "hub.sock.auth", "hub.pid", "daemon.lock", "cli_history")
ID_RE = re.compile(r"^(\d{8}T\d{6})-(manual|auto|pre-update|pre-restore)(?:-([a-z0-9][a-z0-9-]{0,30}))?$")
LABEL_RE = re.compile(r"[^a-z0-9-]+")
MUTATING_BACKUP = ("backup.create",)
DEFAULT_KEEP = 7
DEFAULT_KEEP_SAFETY = 3


# ============================================================================ tiện ích
def _iso(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), timezone.utc).isoformat(timespec="seconds")


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_dir(paths: HubPaths) -> Path:
    return paths.home / BACKUP_DIRNAME


def ensure_dir(paths: HubPaths) -> Path:
    d = backup_dir(paths)
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:  # pragma: no cover
        pass
    return d


def _cfg(cfg: dict[str, Any] | None, key: str, default: Any) -> Any:
    v = (cfg or {}).get(key)
    return default if v is None else v


def latest_schema_version() -> int:
    from .store import MIGRATIONS

    return len(MIGRATIONS)


def db_user_version(db: Path) -> int | None:
    if not db.is_file():
        return None
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return int(c.execute("PRAGMA user_version").fetchone()[0])
    finally:
        c.close()


def included(rel: PurePosixPath) -> bool:
    """`rel` (tương đối HUB_HOME) thuộc tập được sao lưu?"""
    parts = rel.parts
    if not parts:
        return False
    if len(parts) == 1 and parts[0] in INCLUDE_FILES:
        return True
    if parts[0] not in INCLUDE_DIRS or len(parts) < 2:
        return False
    if parts[0] == "agents" and len(parts) >= 3 and parts[2] in AGENT_SKIP:
        return False
    return True


def iter_included(home: Path) -> Iterator[tuple[PurePosixPath, Path]]:
    """(rel, đường dẫn) của mọi file THƯỜNG trong tập sao lưu. Không theo symlink, bỏ symlink/file đặc biệt."""
    for name in INCLUDE_FILES:
        p = home / name
        try:
            if stat.S_ISREG(os.lstat(p).st_mode):
                yield PurePosixPath(name), p
        except OSError:
            pass
    for top in INCLUDE_DIRS:
        root = home / top
        try:
            if not stat.S_ISDIR(os.lstat(root).st_mode):
                continue
        except OSError:
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dp = Path(dirpath)
            keep = []
            for d in sorted(dirnames):
                full = dp / d
                rel = PurePosixPath(full.relative_to(home).as_posix())
                if full.is_symlink() or not included(rel / "x"):
                    continue
                keep.append(d)
            dirnames[:] = keep
            for fn in sorted(filenames):
                full = dp / fn
                rel = PurePosixPath(full.relative_to(home).as_posix())
                try:
                    if not stat.S_ISREG(os.lstat(full).st_mode) or not included(rel):
                        continue
                except OSError:
                    continue
                yield rel, full


def snapshot_db(db: Path, dest: Path) -> int | None:
    """Chụp `db` sang `dest` bằng SQLite online backup API (kết nối chỉ đọc riêng, một bước = ảnh nhất quán). Trả user_version."""
    if not db.is_file():
        return None
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)  # pages=-1: chép hết trong một bước (giữ một read transaction — WAL không chặn writer)
        dst.execute("PRAGMA journal_mode=DELETE")  # file độc lập, không kèm -wal
        uv = int(dst.execute("PRAGMA user_version").fetchone()[0])
    finally:
        dst.close()
        src.close()
    os.chmod(dest, 0o600)
    return uv


class _HashReader:
    def __init__(self, f: Any) -> None:
        self.f = f
        self.h = hashlib.sha256()
        self.n = 0

    def read(self, n: int = -1) -> bytes:
        b = self.f.read(n)
        self.h.update(b)
        self.n += len(b)
        return b


def _tarinfo(name: str, size: int) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode, ti.mtime, ti.type = size, 0o600, int(time.time()), tarfile.REGTYPE
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def _new_id(kind: str, label: str | None, existing: set[str]) -> str:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    lab = LABEL_RE.sub("-", (label or "").strip().lower()).strip("-")[:30] or None
    bid = f"{stamp}-{kind}" + (f"-{lab}" if lab else "")
    while bid in existing or not ID_RE.match(bid):
        time.sleep(1.0)  # cùng giây (hiếm): id theo giây để sắp xếp ổn định
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        bid = f"{stamp}-{kind}" + (f"-{lab}" if lab else "")
    return bid


def archive_path(paths: HubPaths, bid: str) -> Path:
    if not ID_RE.match(str(bid or "")):
        raise HubError(f"id bản sao lưu '{bid}' không hợp lệ", code=-32602)
    return backup_dir(paths) / f"{bid}.tar.gz"


def sidecar_path(paths: HubPaths, bid: str) -> Path:
    return archive_path(paths, bid).with_name(f"{bid}.json")


# ============================================================================ tạo
_LOCK = threading.Lock()


def create(paths: HubPaths, *, kind: str = "manual", label: str | None = None, cfg: dict[str, Any] | None = None,
           by: str = "user") -> dict[str, Any]:
    """Tạo bản sao lưu. Chạy được trong daemon (to_thread) lẫn ngoại tuyến. Trả sidecar (manifest + archive)."""
    if kind not in KINDS:
        raise HubError(f"loại bản sao lưu '{kind}' không hợp lệ", code=-32602)
    if not _LOCK.acquire(blocking=False):
        raise Conflict("đang tạo một bản sao lưu khác — chờ xong rồi thử lại")
    try:
        return _create(paths, kind, label, cfg, by)
    finally:
        _LOCK.release()


def _create(paths: HubPaths, kind: str, label: str | None, cfg: dict[str, Any] | None, by: str) -> dict[str, Any]:
    d = ensure_dir(paths)
    t0 = time.monotonic()
    bid = _new_id(kind, label, {b["id"] for b in list_backups(paths)})
    final = d / f"{bid}.tar.gz"
    part = d / f".{bid}.tar.gz.part"
    tmpdir = Path(tempfile.mkdtemp(prefix=".snap-", dir=d))
    files: list[dict[str, Any]] = []
    skipped: list[str] = []
    try:
        db_copy = tmpdir / DB_MEMBER
        uv = snapshot_db(paths.db, db_copy)
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as raw:
            level = int(_cfg(cfg, "compresslevel", 5))
            with tarfile.open(fileobj=raw, mode="w:gz", compresslevel=max(1, min(level, 9))) as tf:
                db_info = None
                if uv is not None:
                    size = db_copy.stat().st_size
                    with open(db_copy, "rb") as f:
                        hr = _HashReader(f)
                        tf.addfile(_tarinfo(DB_MEMBER, size), hr)
                    db_info = {"path": DB_MEMBER, "size": size, "sha256": hr.h.hexdigest(), "user_version": uv}
                for rel, full in iter_included(paths.home):
                    try:
                        with open(full, "rb") as f:
                            size = os.fstat(f.fileno()).st_size
                            hr = _HashReader(f)
                            tf.addfile(_tarinfo(f"home/{rel}", size), hr)
                    except (OSError, tarfile.TarError) as e:  # file đổi cỡ/biến mất giữa chừng → bỏ, ghi lại
                        skipped.append(f"{rel}: {e}")
                        continue
                    files.append({"path": str(rel), "size": size, "sha256": hr.h.hexdigest()})
                manifest = {"format": FORMAT, "schema": SCHEMA, "id": bid, "kind": kind, "label": label or None, "by": by,
                            "created_at": _iso(), "cfharness_version": __version__, "user_version": uv, "db": db_info,
                            "files": files, "skipped": skipped, "excluded": list(EXCLUDED), "agent_skip": list(AGENT_SKIP),
                            "hub_home": str(paths.home)}
                mb = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
                tf.addfile(_tarinfo(MANIFEST, len(mb)), io.BytesIO(mb))
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(part, final)
        side = {"manifest": manifest, "archive": {"name": final.name, "size": final.stat().st_size, "sha256": _sha_file(final)},
                "duration_s": round(time.monotonic() - t0, 2)}
        _write_private(sidecar_path(paths, bid), json.dumps(side, ensure_ascii=False, indent=1).encode("utf-8"))
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    removed = prune(paths, cfg, keep_ids={bid})
    log.info("đã tạo bản sao lưu %s (%d file, %d byte, %.1f s)", bid, len(files), side["archive"]["size"], side["duration_s"])
    return {**_summary(side), "path": str(final), "pruned": removed}


def _write_private(p: Path, data: bytes) -> None:
    tmp = p.with_name(f".{p.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, p)


# ============================================================================ liệt kê / giữ
def _summary(side: dict[str, Any]) -> dict[str, Any]:
    m = side.get("manifest") or {}
    a = side.get("archive") or {}
    return {"id": m.get("id"), "kind": m.get("kind"), "label": m.get("label"), "created_at": m.get("created_at"),
            "cfharness_version": m.get("cfharness_version"), "user_version": m.get("user_version"), "files": len(m.get("files") or []),
            "skipped": len(m.get("skipped") or []), "size": a.get("size"), "sha256": a.get("sha256"), "has_db": bool(m.get("db")),
            "path": None}


def _read_manifest_from_tar(archive: Path) -> dict[str, Any]:
    with tarfile.open(archive, "r:gz") as tf:
        m = tf.getmember(MANIFEST)
        f = tf.extractfile(m)
        if f is None or m.size > 20_000_000:
            raise HubError("manifest hỏng")
        return json.loads(f.read().decode("utf-8"))


def read_sidecar(paths: HubPaths, bid: str) -> dict[str, Any] | None:
    try:
        side = json.loads(sidecar_path(paths, bid).read_text(encoding="utf-8"))
        if isinstance(side, dict) and (side.get("manifest") or {}).get("id") == bid:
            return side
    except (OSError, ValueError):
        pass
    return None


def list_backups(paths: HubPaths) -> list[dict[str, Any]]:
    """Bản sao lưu của cfharness trong `backups/` (mới nhất trước). File lạ trong thư mục bị bỏ qua."""
    d = backup_dir(paths)
    out: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(d), reverse=True)
    except OSError:
        return out
    for n in names:
        if not n.endswith(".tar.gz") or not ID_RE.match(n[: -len(".tar.gz")]):
            continue
        bid = n[: -len(".tar.gz")]
        p = d / n
        side = read_sidecar(paths, bid)
        if side is None:
            try:
                side = {"manifest": _read_manifest_from_tar(p), "archive": {"name": n, "size": p.stat().st_size, "sha256": None}}
            except Exception:  # archive hỏng / thiếu manifest → vẫn liệt kê để người thấy và xoá
                m = ID_RE.match(bid)
                side = {"manifest": {"id": bid, "kind": m[2] if m else None, "created_at": None}, "archive": {"name": n, "size": p.stat().st_size},
                        "broken": True}
        s = _summary(side)
        s["path"] = str(p)
        s["broken"] = bool(side.get("broken"))
        out.append(s)
    return out


def get(paths: HubPaths, bid: str) -> dict[str, Any]:
    p = archive_path(paths, bid)
    if not p.is_file():
        raise NotFound(f"không có bản sao lưu {bid}")
    return next(b for b in list_backups(paths) if b["id"] == bid)


def prune(paths: HubPaths, cfg: dict[str, Any] | None, *, keep_ids: set[str] | None = None) -> list[str]:
    """Giữ `backup.keep` bản manual+auto mới nhất và `backup.keep_safety` bản pre-update+pre-restore mới nhất."""
    keep = max(1, int(_cfg(cfg, "keep", DEFAULT_KEEP)))
    keep_safety = max(1, int(_cfg(cfg, "keep_safety", DEFAULT_KEEP_SAFETY)))
    keep_ids = set(keep_ids or ()) | _pinned_by_update(paths)
    removed: list[str] = []
    rows = list_backups(paths)
    for group, n in (((("manual", "auto")), keep), (SAFETY_KINDS, keep_safety)):
        for b in [b for b in rows if b["kind"] in group][n:]:
            if b["id"] in keep_ids:
                continue
            try:
                archive_path(paths, b["id"]).unlink(missing_ok=True)
                sidecar_path(paths, b["id"]).unlink(missing_ok=True)
                removed.append(b["id"])
            except OSError as e:  # pragma: no cover
                log.warning("không xoá được bản sao lưu cũ %s: %s", b["id"], e)
    return removed


def _pinned_by_update(paths: HubPaths) -> set[str]:
    """Bản pre-update mà `update --rollback` còn cần (ghi trong update/state.json) — không bao giờ bị xoá vì retention."""
    try:
        st = json.loads((paths.home / "update" / "state.json").read_text(encoding="utf-8"))
        bid = ((st.get("backup") or {}).get("id")) or ""
        return {bid} if ID_RE.match(bid) else set()
    except (OSError, ValueError, AttributeError):
        return set()


# ============================================================================ kiểm
def _safe_member(name: str) -> bool:
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        return False
    parts = PurePosixPath(name).parts
    if any(p in ("..", "") for p in parts):
        return False
    if name in (MANIFEST, DB_MEMBER):
        return True
    return len(parts) >= 2 and parts[0] == "home" and included(PurePosixPath(*parts[1:]))


def verify(paths: HubPaths, bid: str, *, extract_to: Path | None = None) -> dict[str, Any]:
    """Kiểm sha256 archive (theo file kèm) + từng file (theo manifest) + `PRAGMA integrity_check` trên bản sao DB.
    `extract_to`: giải nén luôn (dùng khi khôi phục) — chỉ file đã kiểm, tên đã lọc."""
    p = archive_path(paths, bid)
    if not p.is_file():
        raise NotFound(f"không có bản sao lưu {bid}")
    errors: list[str] = []
    warnings: list[str] = []
    side = read_sidecar(paths, bid)
    if side is None:
        warnings.append("thiếu file kèm .json — không kiểm được sha256 của cả archive (vẫn kiểm từng file)")
    else:
        want = (side.get("archive") or {}).get("sha256")
        got = _sha_file(p)
        if want != got:
            errors.append(f"sha256 archive không khớp (mong {str(want)[:12]}…, thực tế {got[:12]}…)")
    manifest: dict[str, Any] = {}
    integrity = None
    uv = None
    tmp_root = Path(tempfile.mkdtemp(prefix=".verify-", dir=ensure_dir(paths)))
    try:
        try:
            manifest = _read_manifest_from_tar(p)
        except Exception as e:
            errors.append(f"không đọc được manifest: {e}")
            return {"id": bid, "ok": False, "errors": errors, "warnings": warnings}
        if manifest.get("format") != FORMAT or manifest.get("id") != bid:
            errors.append("manifest không phải của bản sao lưu này")
        expect = {f["path"]: f for f in manifest.get("files") or []}
        db_meta = manifest.get("db") or None
        seen: set[str] = set()
        out_root = extract_to
        db_tmp = tmp_root / DB_MEMBER
        with tarfile.open(p, "r:gz") as tf:
            for m in tf:
                if m.name == MANIFEST:
                    continue
                if not m.isreg() or not _safe_member(m.name):
                    errors.append(f"phần tử không hợp lệ trong archive: {m.name!r}")
                    continue
                src = tf.extractfile(m)
                if src is None:  # pragma: no cover
                    errors.append(f"không đọc được {m.name}")
                    continue
                if m.name == DB_MEMBER:
                    dest = db_tmp
                    meta = db_meta
                    key = DB_MEMBER
                else:
                    key = m.name[len("home/"):]
                    meta = expect.get(key)
                    dest = (out_root / key) if out_root is not None else None
                if meta is None:
                    errors.append(f"file không có trong manifest: {m.name}")
                    continue
                h = hashlib.sha256()
                fh = None
                if dest is not None:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    fh = os.fdopen(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb")
                try:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        h.update(chunk)
                        if fh is not None:
                            fh.write(chunk)
                finally:
                    if fh is not None:
                        fh.close()
                if h.hexdigest() != meta.get("sha256"):
                    errors.append(f"sha256 không khớp: {key}")
                seen.add(key)
        missing = [k for k in expect if k not in seen]
        if db_meta and DB_MEMBER not in seen:
            missing.append(DB_MEMBER)
        if missing:
            errors.append(f"thiếu {len(missing)} file so với manifest (vd {missing[0]})")
        if db_meta and db_tmp.is_file():
            c = sqlite3.connect(str(db_tmp))
            try:
                integrity = str(c.execute("PRAGMA integrity_check").fetchone()[0])
                uv = int(c.execute("PRAGMA user_version").fetchone()[0])
            except sqlite3.DatabaseError as e:
                integrity = f"lỗi: {e}"
            finally:
                c.close()
            if integrity != "ok":
                errors.append(f"PRAGMA integrity_check: {integrity}")
            if uv != manifest.get("user_version"):
                errors.append(f"user_version DB ({uv}) khác manifest ({manifest.get('user_version')})")
            if extract_to is not None and not errors:
                shutil.move(str(db_tmp), str(extract_to / DB_MEMBER))
        elif not db_meta:
            warnings.append("bản sao lưu không có hub.db")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
    return {"id": bid, "ok": not errors, "errors": errors, "warnings": warnings, "files": len(manifest.get("files") or []),
            "user_version": uv if uv is not None else manifest.get("user_version"), "integrity": integrity,
            "cfharness_version": manifest.get("cfharness_version"), "kind": manifest.get("kind"), "created_at": manifest.get("created_at")}


# ============================================================================ khôi phục (CHỈ ngoại tuyến, CLI của người)
def restore_plan(paths: HubPaths, bid: str, *, daemon_running: bool | None = None, check: bool = True) -> dict[str, Any]:
    """Kế hoạch khôi phục (không ghi gì): bản sao lưu, kết quả kiểm, phần sẽ bị thay, phần giữ nguyên, cảnh báo."""
    b = get(paths, bid)
    v = verify(paths, bid) if check else None
    latest = latest_schema_version()
    cur_uv = None
    try:
        cur_uv = db_user_version(paths.db)
    except sqlite3.DatabaseError:
        pass
    warnings: list[str] = []
    blockers: list[str] = []
    if v is not None and not v["ok"]:
        blockers.append("bản sao lưu không qua kiểm tra: " + "; ".join(v["errors"][:3]))
    buv = b.get("user_version")
    if isinstance(buv, int) and buv > latest:
        blockers.append(f"bản sao lưu từ phiên bản mới hơn (schema v{buv} > v{latest} của cfharness {__version__}) — cập nhật cfharness trước")
    if isinstance(buv, int) and isinstance(cur_uv, int) and buv < cur_uv:
        warnings.append(f"DB hiện tại schema v{cur_uv}, bản sao lưu v{buv} — daemon sẽ chạy migration tiến lên khi khởi động")
    if not b.get("has_db"):
        warnings.append("bản sao lưu không có hub.db — chỉ khôi phục file")
    if daemon_running:
        warnings.append("daemon đang chạy — lệnh khôi phục sẽ dừng daemon (--stop) rồi khởi động lại")
    return {"backup": b, "verify": v, "blockers": blockers, "warnings": warnings, "current_user_version": cur_uv,
            "replaces": ["hub.db", *INCLUDE_FILES, *(f"{d}/" for d in INCLUDE_DIRS)],
            "kept": ["secrets/", "auth/", "mcp_server/", "runs/", "checkpoints/", "logs/", *(f"agents/*/{x}/" for x in AGENT_SKIP)],
            "pre_restore": "hiện trạng được chụp thành bản 'pre-restore' trước khi ghi đè",
            "command": f"cfharness backup restore {bid} --stop", "daemon_running": daemon_running}


def restore(paths: HubPaths, bid: str, *, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Khôi phục `bid` vào HUB_HOME. Gọi từ CLI của người khi daemon ĐÃ dừng (giữ `daemon.lock` suốt quá trình)."""
    from .auth import ENV_RUN_ID
    from .daemon import acquire_instance_lock, release_instance_lock

    if os.environ.get(ENV_RUN_ID):
        raise HubError("khôi phục bản sao lưu chỉ người làm được — không chạy trong run của agent", code=-32003)
    lock = acquire_instance_lock(paths)
    if lock is None:
        raise Conflict("daemon đang chạy — dừng daemon trước (cfharness backup restore … --stop)")
    try:
        plan = restore_plan(paths, bid, daemon_running=False, check=False)
        if plan["blockers"]:
            raise HubError("; ".join(plan["blockers"]))
        stage = Path(tempfile.mkdtemp(prefix=f".restore-{bid}-", dir=ensure_dir(paths)))
        try:
            v = verify(paths, bid, extract_to=stage)
            if not v["ok"]:
                raise HubError("bản sao lưu không qua kiểm tra: " + "; ".join(v["errors"][:3]), data={"verify": v})
            pre = None
            if paths.db.is_file() or any(True for _ in iter_included(paths.home)):
                pre = _create(paths, "pre-restore", f"before-{bid[:15].lower()}", cfg, "user")
            removed, written = _apply_stage(paths, stage)
            db_restored = False
            if (stage / DB_MEMBER).is_file():
                for suffix in ("-wal", "-shm", "-journal"):
                    Path(f"{paths.db}{suffix}").unlink(missing_ok=True)
                os.replace(stage / DB_MEMBER, paths.db)
                os.chmod(paths.db, 0o600)
                db_restored = True
            baseline = rebaseline_offline(paths)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    finally:
        release_instance_lock(lock)
    log.info("đã khôi phục bản sao lưu %s (%d file ghi, %d file gỡ)", bid, written, removed)
    return {"restored": bid, "files_written": written, "files_removed": removed, "db_restored": db_restored,
            "pre_restore": (pre or {}).get("id"), "integrity_baseline_rows": baseline, "user_version": v.get("user_version")}


def _apply_stage(paths: HubPaths, stage: Path) -> tuple[int, int]:
    """Đồng bộ tập INCLUDE của HUB_HOME về đúng `stage`: gỡ file thuộc tập mà bản sao lưu không có, ghi file của bản sao lưu.
    Phần ngoài tập (secrets, auth, agents/*/workspaces…) giữ nguyên. Bản 'pre-restore' đã giữ mọi thứ bị gỡ/ghi đè."""
    home = paths.home
    wanted = {rel for rel, _ in iter_included(stage)}
    removed = 0
    for rel, full in list(iter_included(home)):
        if rel not in wanted:
            full.unlink(missing_ok=True)
            removed += 1
    written = 0
    for rel, src in list(iter_included(stage)):
        dst = home.joinpath(*rel.parts)
        # cha bị thay bằng symlink/file → dọn trước (không ghi xuyên symlink)
        for i in range(1, len(rel.parts)):
            anc = home.joinpath(*rel.parts[:i])
            if anc.is_symlink() or (anc.exists() and not anc.is_dir()):
                anc.unlink()
            anc.mkdir(exist_ok=True)
        if dst.is_symlink() or dst.is_dir():
            shutil.rmtree(dst) if dst.is_dir() and not dst.is_symlink() else dst.unlink()
        os.replace(src, dst)
        written += 1
    return removed, written


def rebaseline_offline(paths: HubPaths) -> int:
    """Ghi bản chuẩn toàn vẹn (guard/integrity) khớp với policy/ + config.yaml trên đĩa vào hub.db (daemon đang dừng).
    Cùng cách quét với `IntegrityMonitor.rebaseline`. DB cũ chưa có bảng (schema < v11) → bỏ qua (daemon tự chụp khi khởi động)."""
    from types import SimpleNamespace

    from .guard.integrity import CONFIG, IntegrityMonitor, _now, _sha

    if not paths.db.is_file():
        return 0
    mon = IntegrityMonitor(SimpleNamespace(paths=paths))  # type: ignore[arg-type] — chỉ dùng phần quét đĩa
    rows: list[tuple[Any, ...]] = []
    for rel, e in mon.scan_policy().items():
        if e["kind"] in ("file", "dir"):
            rows.append((rel, e["kind"], e.get("sha"), e.get("content"), _now()))
    raw = mon._read_config()
    if raw is not None and raw != b"__symlink__":
        rows.append((CONFIG, "config", _sha(raw), raw, _now()))
    c = sqlite3.connect(str(paths.db))
    try:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='integrity_baseline'").fetchone():
            return 0
        with c:
            c.execute("DELETE FROM integrity_baseline")
            c.executemany("INSERT INTO integrity_baseline(path, kind, sha256, content, updated_at) VALUES (?,?,?,?,?)", rows)
    finally:
        c.close()
    return len(rows)


# ============================================================================ dịch vụ trong daemon (`hub.backups`)
class BackupService:
    """Bọc các hàm trên cho daemon: cấu hình `backup.*`, vòng nền theo `backup.schedule`, event `backup.created|failed`."""

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.paths: HubPaths = hub.paths
        self._task: asyncio.Task | None = None
        self.started_at = time.time()
        self.last_error: str | None = None
        self.running = False
        self.clock: Callable[[], float] = time.time
        hub.bus.add_listener(self._on_event)

    def cfg(self) -> dict[str, Any]:
        try:
            return dict(self.hub.config.get("backup") or {})
        except KeyError:  # pragma: no cover
            return {}

    # ------------------------------------------------------------------ thao tác
    def create(self, *, kind: str = "manual", label: str | None = None, by: str = "user") -> dict[str, Any]:
        self.running = True
        try:
            return create(self.paths, kind=kind, label=label, cfg=self.cfg(), by=by)
        finally:
            self.running = False

    def list(self) -> dict[str, Any]:
        rows = list_backups(self.paths)
        return {"backups": rows, "dir": str(backup_dir(self.paths)), "schedule": self.schedule_info(rows), "running": self.running,
                "excluded": list(EXCLUDED), "included": [*INCLUDE_FILES, *INCLUDE_DIRS], "last_error": self.last_error,
                "total_size": sum(int(b.get("size") or 0) for b in rows)}

    # ------------------------------------------------------------------ lịch
    def _spec(self) -> Any:
        expr = str(self.cfg().get("schedule") or "").strip()
        if not expr or expr.lower() in ("off", "none", "false", "0"):
            return None
        from .schedules.cron import parse

        return parse(expr)

    def schedule_info(self, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        try:
            spec = self._spec()
        except Exception as e:
            return {"enabled": False, "expr": self.cfg().get("schedule"), "error": str(e)}
        if spec is None:
            return {"enabled": False, "expr": self.cfg().get("schedule")}
        nxt = self.next_due(rows)
        return {"enabled": True, "expr": self.cfg().get("schedule"), "next_at": _iso(nxt.timestamp()) if nxt else None,
                "last_auto": self._last_auto(rows)}

    def _last_auto(self, rows: list[dict[str, Any]] | None = None) -> str | None:
        rows = rows if rows is not None else list_backups(self.paths)
        return next((b["created_at"] for b in rows if b["kind"] == "auto" and b.get("created_at")), None)

    def next_due(self, rows: list[dict[str, Any]] | None = None) -> datetime | None:
        spec = self._spec()
        if spec is None:
            return None
        from .schedules.cron import next_after

        last = self._last_auto(rows)
        base = datetime.fromisoformat(last) if last else datetime.fromtimestamp(self.started_at, timezone.utc)
        return next_after(spec, base)

    def due(self) -> bool:
        try:
            nxt = self.next_due()
        except Exception as e:
            self.last_error = f"backup.schedule không hợp lệ: {e}"
            return False
        return nxt is not None and self.clock() >= nxt.timestamp()

    async def tick(self) -> dict[str, Any] | None:
        """Một nhịp vòng nền: đến hạn → tạo bản `auto` (luồng phụ), phát event. Bỏ khi đang cập nhật / đang tạo."""
        if self.running or not self.due() or _update_busy(self.paths):
            return None
        try:
            res = await asyncio.to_thread(self.create, kind="auto", by="schedule")
        except Exception as e:
            self.last_error = str(e)
            log.exception("sao lưu tự động lỗi")
            self.hub.publish(_event("backup.failed", {"kind": "auto", "error": str(e)[:300]}))
            return None
        self.last_error = None
        self.hub.publish(_event("backup.created", {"id": res["id"], "kind": "auto", "size": res.get("size"), "pruned": len(res.get("pruned") or [])}))
        return res

    def _on_event(self, ev: "HubEvent") -> None:
        if ev.type == "hub.started":
            self.start()
        elif ev.type == "hub.stopping":
            self.stop()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self._loop())
        except RuntimeError:
            self._task = None

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(max(5.0, float(self.cfg().get("tick_s") or 60)))
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("vòng sao lưu lỗi")
                await asyncio.sleep(60)


def _event(kind: str, payload: dict[str, Any]) -> Any:
    from .events import make_event

    return make_event(kind, None, payload, source="hub")


def _update_busy(paths: HubPaths) -> bool:
    try:
        st = json.loads((paths.home / "update" / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return st.get("state") in ("starting", "running") and time.time() - float(st.get("updated_at") or 0) < 1200


def attach(hub: "Hub") -> None:
    hub.backups = BackupService(hub)  # type: ignore[attr-defined]


def validate_setting(key: str, value: Any) -> None:
    """`config.set backup.*`: lịch phải đọc được, số bản giữ ≥ 1."""
    vals = value if key == "backup" and isinstance(value, dict) else {key.split(".", 1)[1]: value} if "." in key else {}
    if "schedule" in vals and vals["schedule"] not in (None, "", "off", "none"):
        from .schedules.cron import CronError, parse

        try:
            parse(str(vals["schedule"]))
        except CronError as e:
            raise ValueError(f"backup.schedule không hợp lệ: {e}") from e
    for k in ("keep", "keep_safety"):
        if k in vals and (not isinstance(vals[k], int) or isinstance(vals[k], bool) or vals[k] < 1):
            raise ValueError(f"backup.{k} phải là số nguyên ≥ 1")


# ============================================================================ RPC
def register_rpc(daemon: "Daemon") -> None:
    """`backup.list` (đọc), `backup.create {label?}` (HUMAN_ONLY, tự audit), `backup.verify {id}` / `backup.restore_plan {id}` (HUMAN_ONLY,
    chỉ đọc). Khôi phục KHÔNG có RPC: daemon phải dừng → CLI của người làm ngoại tuyến (`cfharness backup restore`)."""
    from .mcp_server.rpc import wrap_with_audit

    hub = daemon.hub
    svc: BackupService = hub.backups  # type: ignore[attr-defined]

    def _id(p: dict[str, Any]) -> str:
        bid = str(p.get("id") or "").strip()
        if not ID_RE.match(bid):
            raise HubError("thiếu/không hợp lệ: id bản sao lưu", code=-32602)
        return bid

    async def _list(p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(svc.list)

    async def _create(p: dict[str, Any]) -> dict[str, Any]:
        label = p.get("label")
        res = await asyncio.to_thread(svc.create, kind="manual", label=str(label) if label else None, by="user")
        hub.publish(_event("backup.created", {"id": res["id"], "kind": "manual", "size": res.get("size"), "pruned": len(res.get("pruned") or [])}))
        return res

    async def _verify(p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(verify, svc.paths, _id(p))

    async def _plan(p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(restore_plan, svc.paths, _id(p), daemon_running=True)

    daemon.methods.update({"backup.list": _list, "backup.create": wrap_with_audit(hub, "backup.create", _create),
                           "backup.verify": _verify, "backup.restore_plan": _plan})


__all__ = ["BackupService", "attach", "create", "list_backups", "verify", "restore", "restore_plan", "prune",
           "rebaseline_offline", "snapshot_db", "register_rpc", "validate_setting", "INCLUDE_DIRS", "INCLUDE_FILES", "EXCLUDED",
           "AGENT_SKIP", "MUTATING_BACKUP", "ID_RE", "backup_dir"]
