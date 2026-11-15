"""Xuất / nhập TOÀN BỘ hub sang máy khác (`cfharness hub export|import`). Plan: `docs/dev-notes/plan-hub-clone.md` (PR1: lõi gói + clone).

Gói `.cfhx` = MỘT file tar KHÔNG nén ngoài (0600); từng phần tử nén gzip riêng (trừ file đã nén) để mở/kiểm được phần lẻ:
- `hub.db` — chụp bằng SQLite online backup API (`backup.snapshot_db`), không bao giờ chép file thô.
- `home/<rel>` — đúng tập sao lưu của `backup.py` (`backup.iter_included`: cấu hình, chính sách, tri thức, ledger, prompt đóng băng…).
- `extra/runs/<rel>` — profile `full`: stdout/stderr thô của run.
- `claude/<sid>.jsonl`, `claude/<sid>/…`, `claude/index.json` — (PR3, phần optional) session Claude Code của hub để `--resume` chạy tiếp
  ở máy mới: `core` = hội thoại điều phối viên + task chưa xong + run dở/`pending_resume`; `full` = mọi session hub biết. Đã che secret
  theo giá trị; nhập = đổi thư mục `projects/<encode_cwd(cwd mới)>/` + viết lại đường dẫn trong từng dòng (`transfer_sessions.py`).
- `secrets.enc` — (PR2, mặc định có) `secrets/*` (+ tuỳ chọn `auth/totp.json`, `mcp_server/tokens.json`) mã hoá scrypt → AES-256-GCM, AAD
  ràng buộc id gói + sha256 phần còn lại của manifest (`transfer_secrets.py`). Gắn bởi `seal_package` trong tiến trình CLI của người
  SAU khi daemon đóng gói phần không mật — passphrase không bao giờ qua RPC. `--no-secrets` → chỉ ghi TÊN vào `missing_secrets`.
- `MANIFEST.json` — phần tử CUỐI: `format=cfharness-export`, `schema`, `id`, `profile`, `mode_hint`, `source{home, home_real, user_home,
  os, sep, arch, host, cfharness_version, user_version, hub_id}`, `path_roots{hub_home, hub_home_real, user_home, workspaces[]}`,
  `sections{tên: {prefix, count, bytes, optional}}`, `files[{path, size, sha256, gz, packed}]` (sha256 của nội dung GỐC), `missing_secrets`
  (TÊN secret KHÔNG mang theo), `secrets{cipher, kdf, nonce, names, totp, mcp_tokens}` khi có `secrets.enc`.
Luôn bỏ: `checkpoints/` (chủ repo chốt), `secrets/`, `auth/`, `mcp_server/`, `logs/`, `workspaces/`, `worktrees/`, `evals/`, `models/`,
`update/`, `backups/`, `exports/`, `imports/`, socket/pid/lock (`secrets/` chỉ đi qua `secrets.enc`; KHÔNG BAO GIỜ `auth/web.json`, thiết bị tin
cậy, `<socket>.auth`). Phần mở rộng (bản sau, không đổi format): `repos.json` (PR5) — mỗi phần là một mục `sections` có `prefix`; bản cũ gặp mục lạ `optional`
thì bỏ qua kèm cảnh báo, mục lạ không `optional` → từ chối.

XUẤT chạy trong daemon (RPC `hub.export`, HUMAN_ONLY, tự audit) hoặc ngoại tuyến (CLI giữ `daemon.lock` khi daemon dừng). Một RPC duy
nhất làm cả "làm lặng" (`RunManager.quiesced`: không khởi chạy run trong hàng đợi) → dừng run đang chạy nếu `stop` → chụp → ghi nguyên tử
(`.part` → rename) → kiểm lại → thả "làm lặng" trong `finally` — không có cặp prepare/release để một client chết giữa chừng làm hub kẹt
lặng mãi. Daemon chỉ ghi vào `HUB_HOME/exports/` (0700); muốn đặt chỗ khác thì CLI của người tự chép sau khi RPC trả về.
Làm lặng (PR2): ngoài hàng đợi run, mọi vòng nền GHI tri thức/DB (learning, learning_ops = curator/regression/graph quality/digest,
curation, schedules, manager tick, backup, dispatcher, vector graph) kiểm `runs.export_quiesced(hub)` ở ĐẦU mỗi lượt và bỏ lượt khi đang
xuất — một cờ có sẵn, mỗi vòng một dòng. GIỚI HẠN: lượt đã bắt đầu trước khi làm lặng và đang `await` (vd gate eval chờ run, trích xuất
graph cuối run) không bị cắt và có thể ghi tiếp trong lúc chụp. Thứ tự chụp giữ như PR1: `hub.db` TRƯỚC (online backup — nhất quán tự
thân) rồi file tri thức SAU: `Ledger.apply` ghi blob snapshot → file → hàng `ledger_entries`, nên mọi entry có trong DB đã chụp đều có
blob/file trên đĩa lúc quét file (rollback ở máy đích còn đủ blob); chiều ngược (chụp file trước) có thể để lại entry trỏ blob không có
trong gói. Một thay đổi lọt giữa hai lần chụp chỉ làm file MỚI hơn DB một bước — ledger coi là sửa ngoài ledger (không tự rollback đè).

NHẬP CHỈ ngoại tuyến từ CLI của người (không có RPC): daemon đích dừng (giữ `daemon.lock` suốt quá trình), không trong run của agent →
kiểm gói (tên phần tử an toàn, sha256 từng phần, `user_version` ≤ mã đang chạy, cùng kiểu đường dẫn hệ điều hành) → home đích phải trống
(hoặc `replace` → chụp bản `pre-restore` bằng backup trước) → giải nén vào `HUB_HOME/.import-<id>/` (stage) → viết lại đường dẫn TRÊN STAGE
bằng `pathmap` (HUB_HOME nguồn → đích, `$HOME` nguồn → đích, `--map-path`; bảng append-only + cột lịch sử giữ nguyên; bảng tri thức graph
`mem_*` không sửa tay) → dọn trạng thái của máy cũ (`MACHINE_STATE`), run `running|queued` → `interrupted`, áp chế độ (`clone` mặc định:
tắt Telegram/Slack, pause mọi lịch, task đang chạy/chờ chạy → `blocked` + bình luận "[hub] …") → `PRAGMA integrity_check` → chuyển vào chỗ
→ `backup.rebaseline_offline` → `hub_state.imported_from`. File tri thức thuộc ledger còn đường dẫn cũ được ghi vào
`hub_state.import_followups` (bản này chỉ liệt kê, KHÔNG ghi file trực tiếp).
Chế độ `clone` còn huỷ approval/câu hỏi đang chờ của hub nguồn (`cancelled` + "[hub] nhân bản — chưa chạy lại" — không phải từ chối, không
`expired`; run tương ứng → `interrupted`, không tự resume). Secret (PR2) giải mã ngay sau khi kiểm gói (sai passphrase → từ chối, chưa ghi
gì) và ghi SAU CÙNG. Lỗi giữa chừng sau khi đã ghi vào home → tự hoàn tác (bản `pre-restore` khi `replace`; home trống → gỡ phần đã ghi).
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import logging
import os
import platform
import re
import shutil
import socket
import sqlite3
import stat
import tarfile
import tempfile
import threading
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, BinaryIO, Iterable, Iterator

from . import __version__
from . import backup as B
from . import transfer_sessions as TS
from .errors import Conflict, HubError, NotFound
from .pathmap import SCAN_SKIP_DIRS, PathMap, db_count_matches, db_rewrite, rewrite_files, scan_files
from .paths import HubPaths
from .secrets import NAME_RE as SECRET_RE  # tên secret thật (không tính `.meta.json`, file tạm)

if TYPE_CHECKING:
    from .daemon import Daemon
    from .hub import Hub

log = logging.getLogger("cfharness.hub_transfer")

FORMAT = "cfharness-export"
SCHEMA = 1
EXT = ".cfhx"
MANIFEST = "MANIFEST.json"
DB_MEMBER = "hub.db"
HOME_PREFIX = "home/"
RUNS_PREFIX = "extra/runs/"
EXPORTS_DIRNAME = "exports"
IMPORTS_DIRNAME = "imports"
PROFILES = ("core", "full")
MODES = ("clone", "move")
DEFAULT_PROFILE = "full"
DEFAULT_MODE = "clone"
# Phần (section) bản này đọc được. Thêm phần mới (secrets/claude/repos) = thêm vào đây + luật tên phần tử ở `_member_dest`.
KNOWN_SECTIONS = ("db", "home", "runs", "secrets", "claude")
SECRETS_MEMBER = "secrets.enc"  # = transfer_secrets.MEMBER (PR2: secret mã hoá bằng passphrase)
MAX_SECRETS_BLOB = 48 * 1024 * 1024
# Bảng tri thức thuộc ledger: không viết lại đường dẫn tay (chỉ đếm, báo cáo) — rollback/ghim canary phải còn khớp blob ledger
KNOWLEDGE_TABLES = ("mem_*",)
# Trạng thái gắn với MÁY cũ trong hub_state — luôn xoá khi nhập (khoá, rate limit, phiên bản CLI, đăng nhập claude, job launchd, offset bot…)
MACHINE_STATE = ("repo_locks", "browser_locks", "rate_limit", "rate_limit_gate", "rate_limit_gates", "rate_limit_by_account",
                 "pyenv_baseline", "cli_compat", "cli_unknown_rate", "auth_block", "claude_account_checks", "admin_watch.active",
                 "systems.registry", "systems.paused", "telegram.offset", "slack.seen")
# Thêm cho `clone` (2 hub cùng chạy): trạng thái kênh chat của bot máy nguồn (nút/tin/map hội thoại) + run hẹn tự resume
CLONE_STATE = ("pending_resume", "auto_resume_pending")
CLONE_STATE_PREFIXES = ("telegram.", "slack.")
# Task sẽ được dispatcher tự chạy tiếp → ở bản nhân bản: chặn lại (người gỡ chặn ở máy muốn chạy)
CLONE_BLOCK_TASK_STATUSES = ("triage", "ready", "running", "review")
CLONE_NOTE = "[hub] nhân bản từ máy khác — task chưa chạy lại ở máy này; gỡ chặn (`cfharness task unblock <id>`) nếu muốn chạy tiếp ở đây"
CLONE_SCHEDULE_NOTE = "[hub] nhân bản từ máy khác — bật lại lịch cần chạy ở máy này"
CLONE_CONFIG = (("notify.telegram.enabled", False), ("notify.slack.enabled", False), ("notify.slack.interactive", False))
IMPORT_RUN_NOTE = "[hub] run đang dở lúc xuất gói — nhập sang máy khác"
# clone: run dở (kể cả đang chờ người duyệt) → interrupted; approval/câu hỏi đang chờ → cancelled (KHÔNG `expired`, không từ chối)
CLONE_RUN_STATUSES = ("running", "queued", "waiting_human")
APPROVAL_PENDING = ("open", "waiting_human")  # = approvals.PENDING
QUESTION_PENDING = ("with_manager", "open")  # = questions.service.PENDING
CLONE_PENDING_NOTE = "[hub] nhân bản — chưa chạy lại"
NO_GZ_SUFFIX = (".enc", ".gz", ".zst", ".zip", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".xz", ".bz2")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}-\d{8}-\d{6}$")
MUTATING_TRANSFER = ("hub.export",)
MAX_MANIFEST = 64 * 1024 * 1024
_LOCK = threading.Lock()


# ============================================================================ tiện ích
def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _private_dir(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:  # pragma: no cover
        pass
    return d


def exports_dir(paths: HubPaths) -> Path:
    return paths.home / EXPORTS_DIRNAME


def imports_dir(paths: HubPaths) -> Path:
    return paths.home / IMPORTS_DIRNAME


def _host_label() -> str:
    h = re.sub(r"[^a-z0-9-]+", "-", (socket.gethostname() or "hub").split(".")[0].lower()).strip("-")
    return (h or "hub")[:40].strip("-") or "hub"


def _new_id(existing: Iterable[str]) -> str:
    taken = set(existing)
    while True:
        eid = f"{_host_label()}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        if eid not in taken:
            return eid
        time.sleep(1.0)  # cùng giây (hiếm)


def _require_human_process(what: str) -> None:
    from .auth import ENV_RUN_ID

    if os.environ.get(ENV_RUN_ID):
        raise HubError(f"{what} chỉ người làm được — không chạy trong run của agent", code=-32003)


def _tarinfo(name: str, size: int) -> tarfile.TarInfo:
    ti = tarfile.TarInfo(name)
    ti.size, ti.mode, ti.mtime, ti.type = size, 0o600, int(time.time()), tarfile.REGTYPE
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def _gz_ok(name: str) -> bool:
    return not name.lower().endswith(NO_GZ_SUFFIX)


def _add_member(tf: tarfile.TarFile, name: str, src: BinaryIO, tmpdir: Path, *, level: int) -> dict[str, Any]:
    """Thêm một phần tử: nội dung gốc → (gzip) → file tạm → tar. sha256/cỡ tính trên nội dung GỐC."""
    gz = _gz_ok(name)
    h = hashlib.sha256()
    n = 0
    with tempfile.SpooledTemporaryFile(max_size=16 << 20, dir=tmpdir) as spool:
        out: Any = gzip.GzipFile(filename="", fileobj=spool, mode="wb", compresslevel=level, mtime=0) if gz else spool
        try:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                h.update(chunk)
                n += len(chunk)
                out.write(chunk)
        finally:
            if gz:
                out.close()
        packed = spool.tell()
        spool.seek(0)
        tf.addfile(_tarinfo(name, packed), spool)
    return {"path": name, "size": n, "sha256": h.hexdigest(), "gz": gz, "packed": packed}


def _iter_runs(home: Path) -> Iterator[tuple[PurePosixPath, Path]]:
    root = home / "runs"
    try:
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            return
    except OSError:
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not (dp / d).is_symlink())
        for fn in sorted(filenames):
            full = dp / fn
            try:
                if not stat.S_ISREG(os.lstat(full).st_mode):
                    continue
            except OSError:
                continue
            yield PurePosixPath(full.relative_to(root).as_posix()), full


def _policy_dirs(home: Path) -> list[str]:
    """Thư mục trong `policy/` (kể cả rỗng, vd `policy/history`) — bản chuẩn toàn vẹn tính cả thư mục, gói chỉ mang file."""
    root = home / "policy"
    out: list[str] = []
    if root.is_symlink() or not root.is_dir():
        return out
    for dirpath, dirnames, _f in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not (Path(dirpath) / d).is_symlink())
        out += [(Path(dirpath) / d).relative_to(home).as_posix() for d in dirnames]
    return sorted(out)


def _restore_dirs(home: Path, dirs: Any) -> None:
    for d in dirs if isinstance(dirs, list) else []:
        parts = _safe_parts(str(d)) if isinstance(d, str) else None
        if parts and parts[0] == "policy" and len(parts) >= 2:
            home.joinpath(*parts).mkdir(parents=True, exist_ok=True)


def _secret_names(home: Path) -> list[str]:
    try:
        return sorted(n for n in os.listdir(home / "secrets") if SECRET_RE.match(n) and (home / "secrets" / n).is_file())
    except OSError:
        return []


def _workspace_roots(db: Path) -> list[str]:
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover
        return []
    try:
        rows = c.execute("SELECT DISTINCT workspace_default FROM agents WHERE workspace_default IS NOT NULL").fetchall()
    except sqlite3.Error:
        return []
    finally:
        c.close()
    out = set()
    for (w,) in rows:
        s = str(w or "")
        s = s[4:] if s.startswith("dir:") else s
        if os.path.isabs(s):
            out.add(s)
    return sorted(out)


# ============================================================================ xuất
def export_package(paths: HubPaths, *, profile: str = DEFAULT_PROFILE, by: str = "user", compresslevel: int = 6,
                   claude_home: Path | None = None) -> dict[str, Any]:
    """Ghi gói `.cfhx` vào `HUB_HOME/exports/` rồi kiểm lại. Daemon: gọi trong luồng phụ khi hub đã lặng; ngoại tuyến: người gọi giữ
    `daemon.lock`. Trả tóm tắt (id, path, size, sha256, profile, files, user_version, verify)."""
    if profile not in PROFILES:
        raise HubError(f"profile '{profile}' không hợp lệ (core|full)", code=-32602)
    if not paths.db.is_file():
        raise NotFound(f"không có hub.db ở {paths.home} — không có gì để xuất")
    if not _LOCK.acquire(blocking=False):
        raise Conflict("đang xuất một gói khác — chờ xong rồi thử lại")
    try:
        return _export(paths, profile, by, max(1, min(int(compresslevel), 9)), claude_home)
    finally:
        _LOCK.release()


def _export(paths: HubPaths, profile: str, by: str, level: int, chome: Path | None = None) -> dict[str, Any]:
    from .admin_overview import hub_id

    chome = Path(chome) if chome is not None else TS.claude_home()
    d = _private_dir(exports_dir(paths))
    t0 = time.monotonic()
    eid = _new_id(p.name[: -len(EXT)] for p in d.glob(f"*{EXT}"))
    final = d / f"{eid}{EXT}"
    part = d / f".{eid}{EXT}.part"
    tmpdir = Path(tempfile.mkdtemp(prefix=".export-", dir=d))
    files: list[dict[str, Any]] = []
    skipped: list[str] = []
    sections: dict[str, dict[str, Any]] = {}

    def _sec(name: str, prefix: str, meta: dict[str, Any]) -> None:
        s = sections.setdefault(name, {"prefix": prefix, "count": 0, "bytes": 0, "optional": False})
        s["count"] += 1
        s["bytes"] += int(meta["size"])

    try:
        db_copy = tmpdir / DB_MEMBER
        uv = B.snapshot_db(paths.db, db_copy)
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as raw:
            with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
                with open(db_copy, "rb") as f:
                    meta = _add_member(tf, DB_MEMBER, f, tmpdir, level=level)
                files.append(meta)
                _sec("db", DB_MEMBER, meta)
                sources: list[tuple[str, str, Path]] = [("home", f"{HOME_PREFIX}{rel}", full) for rel, full in B.iter_included(paths.home)]
                if profile == "full":
                    sources += [("runs", f"{RUNS_PREFIX}{rel}", full) for rel, full in _iter_runs(paths.home)]
                for sec, name, full in sources:
                    try:
                        with open(full, "rb") as f:
                            meta = _add_member(tf, name, f, tmpdir, level=level)
                    except (OSError, tarfile.TarError) as e:  # file đổi/biến mất giữa chừng → bỏ, ghi lại
                        skipped.append(f"{name}: {e}")
                        continue
                    files.append(meta)
                    _sec(sec, HOME_PREFIX if sec == "home" else RUNS_PREFIX, meta)
                # PR3: session Claude Code của hub (`claude/`, phần optional) — chọn theo profile từ DB ĐÃ chụp, che secret theo giá trị
                cfiles, csec, cskip = TS.export_into(lambda n, f: _add_member(tf, n, f, tmpdir, level=level), db_copy, paths.home, profile,
                                                     tmpdir, chome=chome)
                files += cfiles
                skipped += cskip
                if csec is not None:
                    sections[TS.SECTION] = csec
                home = str(paths.home)
                user_home = str(Path.home())
                manifest = {
                    "format": FORMAT, "schema": SCHEMA, "id": eid, "created_at": _iso(), "by": by, "profile": profile, "mode_hint": DEFAULT_MODE,
                    "source": {"home": home, "home_real": os.path.realpath(home), "user_home": user_home, "os": platform.system().lower(),
                               "sep": os.sep, "arch": platform.machine(), "host": _host_label(), "cfharness_version": __version__,
                               "user_version": uv, "hub_id": hub_id(paths.home)},
                    "path_roots": {"hub_home": home, "hub_home_real": os.path.realpath(home), "user_home": user_home,
                                   "workspaces": _workspace_roots(db_copy), "claude_home": str(chome)},
                    "sections": sections, "files": files, "skipped": skipped,
                    "missing_secrets": _secret_names(paths.home), "dirs": _policy_dirs(paths.home),
                    "excluded": sorted({*B.EXCLUDED, EXPORTS_DIRNAME, IMPORTS_DIRNAME, *(("runs",) if profile == "core" else ())}),
                }
                mb = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
                tf.addfile(_tarinfo(MANIFEST, len(mb)), io.BytesIO(mb))
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(part, final)
        os.chmod(final, 0o600)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    v = verify_package(final, tmp_parent=d)
    if not v["ok"]:
        final.unlink(missing_ok=True)
        raise HubError("gói vừa ghi không qua kiểm tra: " + "; ".join(v["errors"][:3]), data={"verify": v})
    size = final.stat().st_size
    sha = B._sha_file(final)
    log.info("đã xuất hub thành %s (%d phần tử, %d byte, %.1f s)", final.name, len(files), size, time.monotonic() - t0)
    return {"id": eid, "path": str(final), "size": size, "sha256": sha, "profile": profile, "files": len(files), "skipped": len(skipped),
            "user_version": uv, "sections": {k: {"count": s["count"], "bytes": s["bytes"]} for k, s in sections.items()},
            "missing_secrets": manifest["missing_secrets"], "verify": {"ok": True, "warnings": v["warnings"]},
            "duration_s": round(time.monotonic() - t0, 2)}


def seal_package(pkg: Path, home: Path, passphrase: str, *, with_totp: bool = False, with_mcp_tokens: bool = False) -> dict[str, Any]:
    """Gắn phần `secrets.enc` (transfer_secrets) vào gói VỪA xuất: đọc secret của `home`, mã hoá với AAD ràng buộc manifest, cắt gói tại
    phần tử MANIFEST (luôn cuối) rồi ghi `secrets.enc` + manifest mới, kiểm lại gói + thử giải mã. Gọi trong tiến trình CLI của người
    (passphrase không qua RPC). Lỗi bất kỳ sau khi đã cắt → xoá gói (bản xuất hỏng không được để lại). Không có secret nào → không đổi gói."""
    from . import transfer_secrets as S

    _require_human_process("xuất hub")
    pkg = Path(pkg)
    m = read_manifest(pkg)
    errs, _w, _s = check_manifest(m)
    if errs:
        raise HubError("gói không hợp lệ: " + "; ".join(errs[:3]))
    if any(isinstance(f, dict) and f.get("path") == SECRETS_MEMBER for f in m.get("files") or []):
        raise Conflict("gói đã có phần secrets.enc")
    items = S.collect(home, with_totp=with_totp, with_mcp_tokens=with_mcp_tokens)
    if not items:
        return {"sealed": False, "names": [], "totp": False, "mcp_tokens": False}
    m2, ct = S.encrypt(m, items, passphrase)
    m2.setdefault("sections", {})[S.SECTION] = {"prefix": SECRETS_MEMBER, "count": len(S.names_of(items)), "bytes": len(ct), "optional": True}
    entry = {"path": SECRETS_MEMBER, "size": len(ct), "sha256": hashlib.sha256(ct).hexdigest(), "gz": False, "packed": len(ct)}
    m2["files"] = [*m2.get("files", []), entry]
    mb = json.dumps(m2, ensure_ascii=False, indent=1).encode("utf-8")
    with tarfile.open(pkg, "r:") as tf:
        mems = tf.getmembers()
    if not mems or mems[-1].name != MANIFEST:
        raise HubError("gói hỏng: MANIFEST không ở cuối")
    cut = mems[-1].offset
    try:
        with open(pkg, "r+b") as raw:
            raw.truncate(cut)
            raw.seek(cut)
            with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
                tf.addfile(_tarinfo(SECRETS_MEMBER, len(ct)), io.BytesIO(ct))
                tf.addfile(_tarinfo(MANIFEST, len(mb)), io.BytesIO(mb))
            raw.flush()
            os.fsync(raw.fileno())
        os.chmod(pkg, 0o600)
        v = verify_package(pkg, tmp_parent=pkg.parent)
        if not v["ok"] or v.get("secrets_blob") is None:
            raise HubError("gói sau khi gắn secret không qua kiểm tra: " + "; ".join(v["errors"][:3]))
        S.decrypt(v["manifest"], v["secrets_blob"], passphrase)  # chứng minh mở lại được bằng đúng passphrase
    except BaseException:
        pkg.unlink(missing_ok=True)
        raise
    s = m2[S.SECTION]
    return {"sealed": True, "names": s["names"], "totp": s["totp"], "mcp_tokens": s["mcp_tokens"], "size": pkg.stat().st_size,
            "sha256": B._sha_file(pkg), "missing_secrets": m2.get("missing_secrets") or []}


# ============================================================================ đọc / kiểm
def read_manifest(pkg: Path) -> dict[str, Any]:
    try:
        with tarfile.open(pkg, "r:") as tf:
            m = tf.getmember(MANIFEST)
            f = tf.extractfile(m)
            if f is None or not m.isreg() or m.size > MAX_MANIFEST:
                raise HubError("manifest hỏng")
            data = json.loads(f.read().decode("utf-8"))
    except (tarfile.TarError, KeyError, OSError, ValueError) as e:
        raise HubError(f"không đọc được gói {pkg.name}: {e}") from e
    if not isinstance(data, dict):
        raise HubError("manifest hỏng")
    return data


def _safe_parts(rel: str) -> tuple[str, ...] | None:
    if not rel or rel.startswith("/") or "\\" in rel or "\x00" in rel:
        return None
    parts = PurePosixPath(rel).parts
    if any(p in ("..", ".", "") for p in parts) or rel != "/".join(parts):
        return None
    return parts


def _member_dest(name: str) -> PurePosixPath | None:
    """Tên phần tử → đường dẫn tương đối trong stage (None = không hợp lệ). `home/<rel>` phải thuộc tập sao lưu."""
    parts = _safe_parts(name)
    if parts is None:
        return None
    if name in (DB_MEMBER, SECRETS_MEMBER):
        return PurePosixPath(name)
    if name.startswith(HOME_PREFIX) and len(parts) >= 2:
        rel = PurePosixPath(*parts[1:])
        return rel if B.included(rel) else None
    if name.startswith(RUNS_PREFIX) and len(parts) >= 3:
        return PurePosixPath("runs", *parts[2:])
    if name.startswith(TS.PREFIX):
        return TS.member_dest(parts)
    return None


def check_manifest(m: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """(lỗi chặn, cảnh báo, tiền tố phần tử bỏ qua) của manifest — không đọc dữ liệu."""
    errors: list[str] = []
    warnings: list[str] = []
    skip_prefixes: list[str] = []
    if m.get("format") != FORMAT:
        errors.append("không phải gói xuất hub cfharness (format sai)")
        return errors, warnings, skip_prefixes
    sch = m.get("schema")
    if not isinstance(sch, int) or sch < 1:
        errors.append("manifest: schema không hợp lệ")
    elif sch > SCHEMA:
        errors.append(f"gói định dạng v{sch} mới hơn bản này hiểu (v{SCHEMA}) — cập nhật cfharness trên máy này trước")
    if not isinstance(m.get("files"), list):
        errors.append("manifest: thiếu danh sách files")
    secs = m.get("sections") if isinstance(m.get("sections"), dict) else {}
    for name, s in secs.items():
        if name in KNOWN_SECTIONS:
            continue
        s = s if isinstance(s, dict) else {}
        prefix = str(s.get("prefix") or "")
        if s.get("optional") and prefix and _safe_parts(prefix.rstrip("/")) is not None and not prefix.startswith((HOME_PREFIX, RUNS_PREFIX, DB_MEMBER)):
            warnings.append(f"gói có phần '{name}' mà bản cfharness này chưa đọc được — bỏ qua (cập nhật cfharness để nhập phần này)")
            skip_prefixes.append(prefix)
        else:
            errors.append(f"gói có phần bắt buộc '{name}' mà bản cfharness này chưa hỗ trợ — cập nhật cfharness trước")
    if "secrets" in secs:
        from . import transfer_secrets as S

        if (secs.get("secrets") or {}).get("prefix") != SECRETS_MEMBER:
            errors.append("manifest: phần secrets khai báo sai")
        errors += S.check_meta(m)
    if TS.SECTION in secs and (secs.get(TS.SECTION) or {}).get("prefix") != TS.PREFIX:
        errors.append("manifest: phần claude khai báo sai")
    src = m.get("source") if isinstance(m.get("source"), dict) else {}
    uv = src.get("user_version")
    latest = B.latest_schema_version()
    if isinstance(uv, int) and uv > latest:
        errors.append(f"gói từ cfharness mới hơn (schema v{uv} > v{latest} của cfharness {__version__}) — cập nhật cfharness trên máy này "
                      f"lên ≥ {src.get('cfharness_version') or 'bản của máy nguồn'} trước")
    elif not isinstance(uv, int):
        errors.append("manifest: thiếu user_version của hub.db")
    if src.get("sep") not in (None, os.sep):
        errors.append(f"gói xuất từ hệ điều hành khác ({src.get('os')}) — nhập khác hệ điều hành chưa hỗ trợ ở bản này")
    return errors, warnings, skip_prefixes


def verify_package(pkg: Path, *, extract_to: Path | None = None, tmp_parent: Path | None = None) -> dict[str, Any]:
    """Kiểm gói: manifest hợp lệ, MỌI phần tử là file thường có tên an toàn + có trong manifest, sha256 nội dung gốc khớp, không
    thiếu/thừa, `hub.db` qua `PRAGMA integrity_check` và `user_version` khớp manifest. `extract_to`: giải nén luôn (stage)."""
    pkg = Path(pkg)
    if not pkg.is_file():
        raise NotFound(f"không có file gói {pkg}")
    errors: list[str] = []
    try:
        m = read_manifest(pkg)
    except HubError as e:
        return {"ok": False, "errors": [str(e)], "warnings": [], "manifest": {}}
    errors, warnings, skip_prefixes = check_manifest(m)
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings, "manifest": m}
    expect: dict[str, dict[str, Any]] = {}
    for f in m.get("files") or []:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str):
            errors.append("manifest: mục files hỏng")
            continue
        expect[f["path"]] = f
    seen: set[str] = set()
    secrets_blob: bytes | None = None
    integrity = None
    uv = None
    work = Path(tempfile.mkdtemp(prefix=".verify-", dir=tmp_parent)) if extract_to is None else None
    db_tmp = (extract_to / DB_MEMBER) if extract_to is not None else work / DB_MEMBER  # type: ignore[operator]
    try:
        with tarfile.open(pkg, "r:") as tf:
            for mem in tf:
                if mem.name == MANIFEST:
                    continue
                if any(mem.name.startswith(p) for p in skip_prefixes):
                    continue
                dest_rel = _member_dest(mem.name)
                if not mem.isreg() or dest_rel is None:
                    errors.append(f"phần tử không hợp lệ trong gói: {mem.name!r}")
                    continue
                meta = expect.get(mem.name)
                if meta is None:
                    errors.append(f"phần tử không có trong manifest: {mem.name}")
                    continue
                if mem.name in seen:
                    errors.append(f"phần tử lặp: {mem.name}")
                    continue
                src = tf.extractfile(mem)
                if src is None:  # pragma: no cover
                    errors.append(f"không đọc được {mem.name}")
                    continue
                if mem.name == SECRETS_MEMBER:  # bản mã nhỏ → giữ trong RAM (không ghi ra stage), kiểm sha như mọi phần tử
                    if meta.get("gz") or mem.size > MAX_SECRETS_BLOB or "secrets" not in (m.get("sections") or {}):
                        errors.append("phần secrets.enc không hợp lệ")
                    else:
                        blob = src.read(MAX_SECRETS_BLOB + 1)
                        if hashlib.sha256(blob).hexdigest() != meta.get("sha256") or len(blob) != int(meta.get("size") or -1):
                            errors.append(f"sha256 không khớp: {mem.name}")
                        else:
                            secrets_blob = blob
                    seen.add(mem.name)
                    continue
                if mem.name == DB_MEMBER:
                    dest: Path | None = db_tmp
                else:
                    dest = (extract_to.joinpath(*dest_rel.parts)) if extract_to is not None else None
                ok = _copy_member(src, bool(meta.get("gz")), int(meta.get("size") or 0), dest)
                if ok is None:
                    errors.append(f"nội dung giải nén lớn hơn manifest khai báo: {mem.name}")
                elif ok != meta.get("sha256"):
                    errors.append(f"sha256 không khớp: {mem.name}")
                seen.add(mem.name)
    except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
        errors.append(f"gói hỏng: {e}")
    missing = [k for k in expect if k not in seen and not any(k.startswith(p) for p in skip_prefixes)]
    if missing:
        errors.append(f"thiếu {len(missing)} phần tử so với manifest (vd {missing[0]})")
    if "secrets" in (m.get("sections") or {}) and SECRETS_MEMBER not in expect:
        errors.append("manifest khai báo phần secrets nhưng không có secrets.enc")
    if DB_MEMBER not in expect:
        errors.append("gói không có hub.db")
    elif db_tmp.is_file() and not errors:
        c = sqlite3.connect(str(db_tmp))
        try:
            integrity = str(c.execute("PRAGMA integrity_check").fetchone()[0])
            uv = int(c.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.DatabaseError as e:
            integrity = f"lỗi: {e}"
        finally:
            c.close()
        if integrity != "ok":
            errors.append(f"hub.db PRAGMA integrity_check: {integrity}")
        if uv != (m.get("source") or {}).get("user_version"):
            errors.append(f"user_version hub.db ({uv}) khác manifest ({(m.get('source') or {}).get('user_version')})")
    if work is not None:
        shutil.rmtree(work, ignore_errors=True)
    return {"ok": not errors, "errors": errors, "warnings": warnings, "manifest": m, "files": len(expect), "integrity": integrity,
            "user_version": uv if uv is not None else (m.get("source") or {}).get("user_version"),
            "secrets_blob": secrets_blob if not errors else None}


def _copy_member(src: BinaryIO, gz: bool, size: int, dest: Path | None) -> str | None:
    """Đọc (giải gzip) một phần tử, tính sha256, ghi `dest` (0600) nếu có. Vượt `size` khai báo → None (chống bom giải nén)."""
    reader: Any = gzip.GzipFile(fileobj=src, mode="rb") if gz else src
    h = hashlib.sha256()
    n = 0
    fh = None
    if dest is not None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fh = os.fdopen(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb")
    try:
        for chunk in iter(lambda: reader.read(1 << 20), b""):
            n += len(chunk)
            if n > size:
                return None
            h.update(chunk)
            if fh is not None:
                fh.write(chunk)
    finally:
        if fh is not None:
            fh.close()
    return h.hexdigest()


# ============================================================================ nhập
def parse_map_paths(items: Iterable[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for it in items or ():
        if "=" not in str(it):
            raise ValueError(f"--map-path phải dạng CŨ=MỚI: {it!r}")
        old, new = str(it).split("=", 1)
        old, new = old.strip(), new.strip()
        if not os.path.isabs(old) or not os.path.isabs(new):
            raise ValueError(f"--map-path cần đường dẫn tuyệt đối ở cả hai phía: {it!r}")
        out.append((old, new))
    return out


def build_pathmap(manifest: dict[str, Any], home: Path, user_home: Path, maps: Iterable[tuple[str, str]] = ()) -> PathMap:
    """`$HOME` nguồn → `$HOME` đích, HUB_HOME nguồn (cả realpath) → HUB_HOME đích, rồi `--map-path` (ghi đè cặp trùng)."""
    roots = manifest.get("path_roots") if isinstance(manifest.get("path_roots"), dict) else {}
    pairs: list[tuple[str, str]] = []
    uh = str(roots.get("user_home") or "")
    if uh and os.path.isabs(uh):
        pairs.append((uh, str(user_home)))
    for k in ("hub_home", "hub_home_real"):
        v = str(roots.get(k) or "")
        if v and os.path.isabs(v):
            pairs.append((v, str(home)))
    pairs += list(maps)
    return PathMap(pairs)


def home_blank(paths: HubPaths) -> bool:
    """Home đích "trống": chưa có hub.db, chưa có file nào thuộc tập sao lưu, chưa có secret (chỉ có thứ cfharness vừa tạo)."""
    if paths.db.exists():
        return False
    if any(True for _ in B.iter_included(paths.home)):
        return False
    return not _secret_names(paths.home)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _state_keys(conn: sqlite3.Connection, mode: str) -> list[str]:
    keys = [r[0] for r in conn.execute("SELECT key FROM hub_state")]
    drop = set(MACHINE_STATE) | (set(CLONE_STATE) if mode == "clone" else set())
    return sorted(k for k in keys if k in drop or (mode == "clone" and k.startswith(CLONE_STATE_PREFIXES)))


def _adjust_db(db: Path, *, mode: str, apply: bool, manifest: dict[str, Any], followups: dict[str, Any] | None = None,
               extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Dọn trạng thái máy cũ + áp chế độ trên DB của STAGE (daemon chưa chạy). `apply=False` chỉ đếm (dry-run).

    `clone`: run `running|queued|waiting_human` → `interrupted` (không tự resume — `pending_resume` bị xoá); approval đang chờ
    (`open|waiting_human`) và câu hỏi đang chờ (`with_manager|open`) → `cancelled` kèm ghi chú "[hub] nhân bản — chưa chạy lại" (HUỶ do
    nhân bản, không phải từ chối/hết giờ: không có `expired`, không signal reject; máy nguồn vẫn hỏi người như cũ). `move`: giữ nguyên."""
    from .store import _dumps, now_iso

    out: dict[str, Any] = {"state_cleared": [], "runs_interrupted": 0, "runs_pending_resume": 0, "tasks_blocked": 0, "schedules_paused": 0,
                           "approvals_cancelled": 0, "questions_cancelled": 0}
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        tabs = _tables(conn)
        if apply:
            conn.execute("BEGIN IMMEDIATE")
        if "hub_state" in tabs:
            keys = _state_keys(conn, mode)
            out["state_cleared"] = keys
            if apply and keys:
                conn.executemany("DELETE FROM hub_state WHERE key=?", [(k,) for k in keys])
        now = now_iso()
        if "runs" in tabs:
            sts = CLONE_RUN_STATUSES if mode == "clone" else ("running",)
            rows = conn.execute(f"SELECT id FROM runs WHERE status IN ({','.join('?' * len(sts))})", sts).fetchall()
            out["runs_interrupted"] = len(rows)
            if apply and rows:
                conn.executemany("UPDATE runs SET status='interrupted', outcome='interrupted', ended_at=COALESCE(ended_at, ?), error=? WHERE id=?",
                                 [(now, IMPORT_RUN_NOTE, r[0]) for r in rows])
            if mode == "move" and rows:  # chuyển nhà: run đang dở tự resume ở máy mới (recovery đọc pending_resume)
                out["runs_pending_resume"] = len(rows)
                if apply:
                    cur = conn.execute("SELECT value FROM hub_state WHERE key='pending_resume'").fetchone()
                    lst = sorted(set((json.loads(cur[0]) if cur else []) + [r[0] for r in rows]))
                    conn.execute("INSERT INTO hub_state(key, value, updated_at) VALUES ('pending_resume',?,?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (_dumps(lst), now))
        if mode == "clone" and "tasks" in tabs:
            sts = CLONE_BLOCK_TASK_STATUSES
            rows = conn.execute(f"SELECT id FROM tasks WHERE status IN ({','.join('?' * len(sts))})", sts).fetchall()
            out["tasks_blocked"] = len(rows)
            if apply and rows:
                cols = {c[1] for c in conn.execute("PRAGMA table_info(tasks)")}
                sets = "status='blocked', block_reason=?, updated_at=?" + (", wait_reason=NULL" if "wait_reason" in cols else "")
                conn.executemany(f"UPDATE tasks SET {sets} WHERE id=?", [(CLONE_NOTE, now, r[0]) for r in rows])
                if "task_comments" in tabs:
                    import uuid

                    conn.executemany("INSERT INTO task_comments(id, task_id, author, kind, text, run_id, created_at) VALUES (?,?,?,?,?,?,?)",
                                     [("c_" + uuid.uuid4().hex[:10], r[0], "hub", "comment", CLONE_NOTE, None, now) for r in rows])
        if mode == "clone" and "approval_requests" in tabs:
            rows = conn.execute(f"SELECT id FROM approval_requests WHERE status IN ({','.join('?' * len(APPROVAL_PENDING))})",
                                APPROVAL_PENDING).fetchall()
            out["approvals_cancelled"] = len(rows)
            if apply and rows:
                conn.executemany("UPDATE approval_requests SET status='cancelled', resolved_at=?, resolved_by='hub', resolved_channel='import', "
                                 "note=? WHERE id=?", [(now, CLONE_PENDING_NOTE, r[0]) for r in rows])
        if mode == "clone" and "questions" in tabs:
            rows = conn.execute(f"SELECT id FROM questions WHERE status IN ({','.join('?' * len(QUESTION_PENDING))})", QUESTION_PENDING).fetchall()
            out["questions_cancelled"] = len(rows)
            if apply and rows:
                conn.executemany("UPDATE questions SET status='cancelled', answered_by='hub', answered_channel='import', answered_at=?, "
                                 "escalation_note=? WHERE id=?", [(now, CLONE_PENDING_NOTE, r[0]) for r in rows])
        if mode == "clone" and "schedules" in tabs:
            rows = conn.execute("SELECT id FROM schedules WHERE status='active' AND deleted_at IS NULL").fetchall()
            out["schedules_paused"] = len(rows)
            if apply and rows:
                conn.executemany("UPDATE schedules SET status='paused', paused_reason=?, paused_by='import', updated_at=? WHERE id=?",
                                 [(CLONE_SCHEDULE_NOTE, now, r[0]) for r in rows])
        if apply and "hub_state" in tabs:
            src = manifest.get("source") or {}
            info = {"export_id": manifest.get("id"), "source_hub_id": src.get("hub_id"), "source_host": src.get("host"),
                    "source_version": src.get("cfharness_version"), "profile": manifest.get("profile"), "mode": mode, "at": now, **(extra or {})}
            rows_state = [("imported_from", info)]
            if mode == "clone":
                rows_state.append(("cloned_from", {"hub_id": src.get("hub_id"), "export_id": manifest.get("id"), "at": now}))
            if followups:
                rows_state.append(("import_followups", followups))
            for k, v in rows_state:
                conn.execute("INSERT INTO hub_state(key, value, updated_at) VALUES (?,?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at", (k, _dumps(v), now))
            if "audit_log" in tabs:
                data = {"export_id": manifest.get("id"), "source_hub_id": src.get("hub_id"), "mode": mode, "profile": manifest.get("profile"),
                        "runs_interrupted": out["runs_interrupted"], "tasks_blocked": out["tasks_blocked"], "schedules_paused": out["schedules_paused"],
                        "approvals_cancelled": out["approvals_cancelled"], "questions_cancelled": out["questions_cancelled"],
                        **{k: (len(v) if isinstance(v, list) else v) for k, v in (extra or {}).items()}}
                conn.execute("INSERT INTO audit_log (ts, kind, actor, channel, data) VALUES (?,?,?,?,?)",
                             (now, "hub.imported", "user", "cli", json.dumps(data, ensure_ascii=False)))
        if apply:
            conn.execute("COMMIT")
    except Exception:
        if apply and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return out


def _adjust_config(stage: Path, mode: str, *, apply: bool) -> list[str]:
    """`clone`: tắt kênh Telegram/Slack trong config.yaml của stage (giữ cấu hình khác — người tự `notify … setup` bot riêng)."""
    if mode != "clone":
        return []
    from .config import HubConfig

    p = stage / "config.yaml"
    cfg = HubConfig(p)
    changed = []
    for key, val in CLONE_CONFIG:
        try:
            cur = cfg.get(key)
        except KeyError:
            cur = None
        if cur != val:
            changed.append(key)
            if apply:
                cfg.set(key, val)
    if apply and p.exists():
        os.chmod(p, 0o600)
    return changed


def _mcp_missing_commands(db: Path, pm: PathMap) -> list[dict[str, str]]:
    """MCP stdio có `command` không tìm thấy ở máy này (sau khi đổi đường dẫn) — chỉ báo, không sửa."""
    out: list[dict[str, str]] = []
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:  # pragma: no cover
        return out
    try:
        if "mcp_servers" not in _tables(c):
            return out
        cols = {x[1] for x in c.execute("PRAGMA table_info(mcp_servers)")}
        if not {"name", "config"} <= cols:
            return out
        where = " WHERE deleted_at IS NULL" if "deleted_at" in cols else ""
        rows = c.execute(f"SELECT name, config FROM mcp_servers{where}").fetchall()
    except sqlite3.Error:
        return out
    finally:
        c.close()
    for name, conf in rows:
        try:
            cmd = (json.loads(conf) or {}).get("command")
        except (TypeError, ValueError):
            continue
        if not cmd or not isinstance(cmd, str):
            continue
        cmd2 = pm.rewrite(cmd)
        found = os.path.exists(cmd2) if os.path.isabs(cmd2) else shutil.which(cmd2) is not None
        if not found:
            out.append({"server": str(name), "command": cmd2})
    return out


def _stage_work(stage: Path, manifest: dict[str, Any], pm: PathMap, mode: str, *, apply: bool, extra: dict[str, Any] | None = None,
                claude_target: Path | None = None) -> dict[str, Any]:
    """Phần dùng chung giữa dry-run và nhập thật — trên STAGE (đã giải nén, đã kiểm sha). Session Claude (PR3) chỉ LẬP KẾ HOẠCH ở đây
    (đếm, sid trùng); ghi thật ở `import_package` sau khi hub.db đã vào chỗ."""
    db = stage / DB_MEMBER
    rep: dict[str, Any] = {}
    rep["claude_sessions"] = TS.plan(stage / TS.STAGE_DIR, claude_target if claude_target is not None else TS.claude_home(), pm)
    rep["db_rewrite"] = db_rewrite(db, pm, apply=apply, skip_tables=KNOWLEDGE_TABLES)
    rep["knowledge_rows_with_old_paths"] = db_count_matches(db, pm, KNOWLEDGE_TABLES)
    files_rw, review = scan_files(stage, pm, skip_dirs=(*SCAN_SKIP_DIRS, TS.STAGE_DIR))
    rep["files_rewrite"] = files_rw
    rep["review"] = review
    if apply and files_rw:
        rewrite_files(stage, files_rw, pm, db=db)
    rep["config_changed"] = _adjust_config(stage, mode, apply=apply)
    rep["mcp_missing"] = _mcp_missing_commands(db, pm)
    followups = None
    if review or rep["knowledge_rows_with_old_paths"]:
        followups = {"files": review, "graph_rows": rep["knowledge_rows_with_old_paths"], "map": pm.as_list(), "status": "pending",
                     "note": "tri thức thuộc ledger còn đường dẫn máy cũ — chưa tự viết lại (người sửa qua ledger)"}
    rep.update(_adjust_db(db, mode=mode, apply=apply, manifest=manifest, followups=followups, extra=extra))
    return rep


def has_secrets(manifest: dict[str, Any]) -> bool:
    return "secrets" in (manifest.get("sections") or {})


def check_passphrase(pkg: Path, passphrase: str) -> list[str]:
    """Thử passphrase NHANH (chỉ đọc manifest + `secrets.enc`, không giải nén cả gói): đúng → danh sách tên secret; sai / gói bị tráo
    (sha256 phần tử, AAD) → HubError. Dùng ở CLI trước bước xác nhận; `import_package` vẫn kiểm đầy đủ."""
    from . import transfer_secrets as S

    m = read_manifest(Path(pkg))
    errs, _w, _s = check_manifest(m)
    if errs:
        raise HubError("gói không hợp lệ: " + "; ".join(errs[:3]))
    if not has_secrets(m):
        return []
    meta = next((f for f in m.get("files") or [] if isinstance(f, dict) and f.get("path") == SECRETS_MEMBER), None)
    if meta is None:
        raise HubError("manifest khai báo phần secrets nhưng không có secrets.enc")
    with tarfile.open(pkg, "r:") as tf:
        try:
            mem = tf.getmember(SECRETS_MEMBER)
        except KeyError:
            raise HubError("gói thiếu secrets.enc") from None
        f = tf.extractfile(mem)
        if f is None or not mem.isreg() or mem.size > MAX_SECRETS_BLOB:
            raise HubError("phần secrets.enc không hợp lệ")
        blob = f.read(MAX_SECRETS_BLOB + 1)
    if hashlib.sha256(blob).hexdigest() != meta.get("sha256"):
        raise HubError("sha256 không khớp: secrets.enc")
    S.decrypt(m, blob, passphrase)
    return list((m.get(S.SECTION) or {}).get("names") or [])


def _secrets_plan(m: dict[str, Any], blob: bytes | None, mode: str, passphrase: str | None, skip: bool) -> dict[str, Any]:
    """Phần secret của kế hoạch nhập — chỉ TÊN. Có passphrase → thử giải mã luôn (sai → `error`, người gọi chặn trước khi ghi gì)."""
    from . import transfer_secrets as S

    missing = list(m.get("missing_secrets") or [])
    if not has_secrets(m):
        return {"present": False, "names": [], "import": [], "skip_mode": [], "unlocked": None, "error": None, "missing": missing}
    meta = m.get(S.SECTION) or {}
    names = list(meta.get("names") or [])
    split = S.plan_names(names, mode)
    out: dict[str, Any] = {"present": True, "names": names, "totp": bool(meta.get("totp")), "mcp_tokens": bool(meta.get("mcp_tokens")),
                           "skipped": bool(skip), "unlocked": None, "error": None, **split}
    if skip:
        out.update({"import": [], "missing": sorted(set(missing) | set(split["import"]))})
        return out
    out["missing"] = missing
    if passphrase is not None and blob is not None:
        try:
            S.decrypt(m, blob, passphrase)
            out["unlocked"] = True
        except HubError as e:
            out["unlocked"], out["error"] = False, str(e)
    return out


def import_plan(paths: HubPaths, pkg: Path, *, maps: Iterable[tuple[str, str]] = (), mode: str = DEFAULT_MODE,
                user_home: Path | None = None, passphrase: str | None = None, skip_secrets: bool = False,
                claude_home: Path | None = None) -> dict[str, Any]:
    """Kế hoạch nhập (KHÔNG ghi gì vào home đích): kiểm gói, bản đồ đường dẫn, số giá trị/file sẽ viết lại, trạng thái sẽ dọn,
    run/task/lịch bị ảnh hưởng, MCP thiếu lệnh, secret phải đặt lại, home đích có trống không. Giải nén vào thư mục tạm riêng.
    `passphrase` → thử giải mã `secrets.enc` (sai → blocker). Không bao giờ trả giá trị secret."""
    if mode not in MODES:
        raise HubError(f"--mode '{mode}' không hợp lệ (clone|move)", code=-32602)
    pkg = Path(pkg).expanduser()
    maps = list(maps)
    tmp = Path(tempfile.mkdtemp(prefix="cfharness-import-plan-"))
    os.chmod(tmp, 0o700)
    try:
        v = verify_package(pkg, extract_to=tmp)
        m = v.get("manifest") or {}
        blockers = list(v["errors"])
        warnings = list(v["warnings"])
        pm = build_pathmap(m, paths.home, user_home or Path.home(), maps)
        work: dict[str, Any] = {}
        if v["ok"]:
            work = _stage_work(tmp, m, pm, mode, apply=False, claude_target=claude_home)
            cs = work.get("claude_sessions") or {}
            if cs.get("conflicts"):
                warnings.append(f"{len(cs['conflicts'])} session Claude đã có ở máy này (trùng id) — giữ bản của máy này, không nhập bản trong gói")
            if cs.get("present") and cs.get("redacted"):
                warnings.append("transcript session Claude trong gói đã che secret theo giá trị (***) — resume vẫn chạy, model thấy *** chỗ token cũ")
        blank = home_blank(paths)
        if m.get("profile") == "core":
            warnings.append("gói profile core — không có stdout thô của run cũ (runs/)")
        sec = _secrets_plan(m, v.get("secrets_blob"), mode, passphrase, skip_secrets) if v["ok"] else \
            {"present": has_secrets(m), "names": [], "import": [], "skip_mode": [], "missing": list(m.get("missing_secrets") or [])}
        if sec.get("error"):
            blockers.append(sec["error"])
        if sec["missing"]:
            warnings.append(f"{len(sec['missing'])} secret KHÔNG được nhập — đặt lại bằng `cfharness secret set <TÊN>` sau khi nhập")
        if sec.get("skip_mode"):
            warnings.append(f"chế độ clone: bỏ {len(sec['skip_mode'])} secret bot Telegram/Slack + OAuth MCP (máy này cần bot riêng, "
                            "đăng nhập lại MCP OAuth)")
        if mode == "clone":
            warnings.append("chế độ clone: Telegram/Slack tắt, lịch tạm dừng, task đang chạy bị chặn — máy nguồn vẫn chạy bình thường")
        size = pkg.stat().st_size if pkg.is_file() else 0
        try:
            free = shutil.disk_usage(paths.home if paths.home.exists() else paths.home.parent).free
        except OSError:
            free = None
        need = sum(int(f.get("size") or 0) for f in m.get("files") or [] if isinstance(f, dict)) * 2
        if free is not None and need > free:
            blockers.append(f"không đủ dung lượng đĩa (cần ~{need} byte, còn {free})")
        return {"package": str(pkg), "size": size, "id": m.get("id"), "profile": m.get("profile"), "source": m.get("source") or {},
                "created_at": m.get("created_at"), "mode": mode, "target_home": str(paths.home), "home_blank": blank,
                "verify": {k: v[k] for k in ("ok", "errors", "warnings", "files", "integrity", "user_version") if k in v},
                "path_map": pm.as_list(), "blockers": blockers, "warnings": warnings, "missing_secrets": sec["missing"], "secrets": sec,
                "machine_state": list(MACHINE_STATE), "space_needed": need, **work}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def import_package(paths: HubPaths, pkg: Path, *, maps: Iterable[tuple[str, str]] = (), mode: str = DEFAULT_MODE, replace: bool = False,
                   user_home: Path | None = None, cfg: dict[str, Any] | None = None, passphrase: str | None = None,
                   skip_secrets: bool = False, claude_home: Path | None = None) -> dict[str, Any]:
    """Nhập gói vào `paths.home`. Gọi từ CLI của người khi daemon đích ĐÃ dừng (giữ `daemon.lock` suốt quá trình).

    Gói có `secrets.enc`: cần `passphrase` (hoặc `skip_secrets`) — giải mã NGAY sau khi kiểm gói, TRƯỚC mọi ghi vào home (sai → từ chối,
    home không đổi). Secret được ghi SAU CÙNG (theo chế độ — `transfer_secrets.classify`), có nhật ký hoàn tác.
    Session Claude Code (PR3, phần `claude/`) ghi vào `<claude home>/projects/<encode_cwd(cwd mới)>/` SAU khi hub.db vào chỗ, TRƯỚC secret —
    cả ở chế độ `clone` (hội thoại điều phối viên tiếp tục ở máy mới); sid đã có → giữ của máy này.
    Lỗi bất kỳ sau khi bắt đầu ghi vào home → tự hoàn tác (gồm cả file session vừa ghi): `replace` → khôi phục bản `pre-restore` vừa chụp; home trống → gỡ những gì đã
    ghi. Báo lỗi kèm `data.rolled_back` (và lệnh khôi phục tay nếu chính bước hoàn tác hỏng)."""
    from . import transfer_secrets as S
    from .daemon import acquire_instance_lock, release_instance_lock

    _require_human_process("nhập hub")
    if mode not in MODES:
        raise HubError(f"--mode '{mode}' không hợp lệ (clone|move)", code=-32602)
    pkg = Path(pkg).expanduser().resolve()
    maps = list(maps)
    if not pkg.is_file():
        raise NotFound(f"không có file gói {pkg}")
    _private_dir(paths.home)
    lock = acquire_instance_lock(paths)
    if lock is None:
        raise Conflict("daemon của home đích đang chạy — dừng daemon trước (cfharness hub import … --replace --stop)")
    try:
        blank = home_blank(paths)
        if not blank and not replace:
            raise Conflict(f"home đích {paths.home} không trống (đã có hub.db/tri thức/secret) — dùng --replace (hiện trạng được chụp "
                           "thành bản sao lưu pre-restore trước khi ghi đè) hoặc chọn --home khác")
        stage = Path(tempfile.mkdtemp(prefix=".import-", dir=paths.home))
        try:
            v = verify_package(pkg, extract_to=stage)
            if not v["ok"]:
                raise HubError("gói không qua kiểm tra: " + "; ".join(v["errors"][:3]), data={"verify": {k: v[k] for k in ("errors", "warnings")}})
            m = v["manifest"]
            payload: dict[str, bytes] | None = None
            sec = _secrets_plan(m, v.get("secrets_blob"), mode, None, skip_secrets)
            if sec["present"] and not skip_secrets:
                if passphrase is None:
                    raise HubError("gói có secret mã hoá — cần passphrase (hoặc --skip-secrets để nhập phần còn lại)", code=-32602)
                payload = S.decrypt(m, v["secrets_blob"], passphrase)  # sai → HubError, chưa ghi gì vào home
            pm = build_pathmap(m, paths.home, user_home or Path.home(), maps)
            info = {"secrets_imported": len(sec["import"]), "secrets_skipped_mode": len(sec["skip_mode"]), "secrets_missing": sec["missing"]}
            chome = Path(claude_home) if claude_home is not None else TS.claude_home()
            rep = _stage_work(stage, m, pm, mode, apply=True, extra=info, claude_target=chome)
            c = sqlite3.connect(str(stage / DB_MEMBER))
            try:
                integ = str(c.execute("PRAGMA integrity_check").fetchone()[0])
            finally:
                c.close()
            if integ != "ok":
                raise HubError(f"hub.db sau khi viết lại đường dẫn không qua integrity_check: {integ}")
            pre = None
            if not blank:
                pre = B._create(paths, "pre-restore", f"before-import-{str(m.get('id') or '')[:12].lower()}", cfg, "user")
            runs_moved: list[Path] = []
            undo: list[tuple[Path, bytes | None]] = []
            sess_created: list[Path] = []
            sess_rep: dict[str, Any] = rep.get("claude_sessions") or {}
            sec_rep: dict[str, Any] = {"imported": [], "skipped_mode": [], "kept_target": []}
            try:
                removed, written = B._apply_stage(paths, stage)
                _move_runs(stage, paths, runs_moved)
                _restore_dirs(paths.home, m.get("dirs"))
                for suffix in ("-wal", "-shm", "-journal"):
                    Path(f"{paths.db}{suffix}").unlink(missing_ok=True)
                os.replace(stage / DB_MEMBER, paths.db)
                os.chmod(paths.db, 0o600)
                baseline = B.rebaseline_offline(paths)
                sess_rep, sess_created = TS.install(stage / TS.STAGE_DIR, chome, pm)
                if payload is not None:
                    sec_rep, undo = S.apply(paths.home, payload, mode=mode)
            except BaseException as e:
                S.rollback(undo)
                TS.rollback(sess_created)
                _rollback_import(paths, pre, blank, runs_moved, e)
                raise  # pragma: no cover — _rollback_import luôn ném
        finally:
            shutil.rmtree(stage, ignore_errors=True)
    finally:
        release_instance_lock(lock)
    log.info("đã nhập gói %s vào %s (chế độ %s, %d file, %d secret)", m.get("id"), paths.home, mode, written, len(sec_rep["imported"]))
    return {"imported": m.get("id"), "mode": mode, "home": str(paths.home), "files_written": written, "files_removed": removed,
            "runs_files": len(runs_moved), "pre_restore": (pre or {}).get("id"), "integrity_baseline_rows": baseline, "path_map": pm.as_list(),
            "user_version": v.get("user_version"), "missing_secrets": sec["missing"],
            "secrets": {"imported": sec_rep["imported"], "skipped_mode": sec_rep["skipped_mode"], "kept_target": sec_rep["kept_target"],
                        "missing": sec["missing"], "skipped": bool(skip_secrets and sec["present"]),
                        "totp": bool(sec_rep.get("totp")), "mcp_tokens": bool(sec_rep.get("mcp_tokens"))}, **rep,
            "claude_sessions": sess_rep}


def _rollback_import(paths: HubPaths, pre: dict[str, Any] | None, blank: bool, runs_moved: list[Path], err: BaseException) -> None:
    """Hoàn tác một lần nhập hỏng giữa chừng (vẫn giữ `daemon.lock`) rồi NÉM lỗi gốc đã bọc. `pre` = bản pre-restore vừa chụp."""
    for p in runs_moved:  # file stdout run do lần nhập này thêm (chưa từng có ở đích)
        p.unlink(missing_ok=True)
    bid = (pre or {}).get("id")
    try:
        if bid:
            stage = Path(tempfile.mkdtemp(prefix=f".rollback-{bid}-", dir=B.ensure_dir(paths)))
            try:
                v = B.verify(paths, bid, extract_to=stage)
                if not v["ok"]:
                    raise HubError("bản pre-restore không qua kiểm tra: " + "; ".join(v["errors"][:2]))
                B._apply_stage(paths, stage)
                if (stage / DB_MEMBER).is_file():
                    for suffix in ("-wal", "-shm", "-journal"):
                        Path(f"{paths.db}{suffix}").unlink(missing_ok=True)
                    os.replace(stage / DB_MEMBER, paths.db)
                    os.chmod(paths.db, 0o600)
                B.rebaseline_offline(paths)
            finally:
                shutil.rmtree(stage, ignore_errors=True)
        elif blank:  # home trống trước khi nhập → gỡ phần đã ghi, trả về trống
            empty = Path(tempfile.mkdtemp(prefix=".rollback-empty-", dir=paths.home))
            try:
                B._apply_stage(paths, empty)
            finally:
                shutil.rmtree(empty, ignore_errors=True)
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(f"{paths.db}{suffix}").unlink(missing_ok=True)
    except Exception as e2:
        log.exception("hoàn tác lần nhập hỏng thất bại")
        hint = f" — khôi phục tay: cfharness backup restore {bid} --stop" if bid else ""
        raise HubError(f"nhập lỗi giữa chừng ({err}) VÀ hoàn tác cũng lỗi ({e2}){hint}",
                       data={"rolled_back": False, "pre_restore": bid}) from err
    where = f"đã khôi phục hiện trạng trước khi nhập (bản {bid})" if bid else "đã gỡ phần đã ghi, home đích trở lại trống"
    log.warning("nhập lỗi giữa chừng, %s: %s", where, err)
    raise HubError(f"nhập lỗi giữa chừng: {err} — {where}", data={"rolled_back": True, "pre_restore": bid}) from err


def _move_runs(stage: Path, paths: HubPaths, out: list[Path]) -> list[Path]:
    """`extra/runs/*` → `HUB_HOME/runs/` — không ghi đè file đã có (run của hub đích). Thêm vào `out` từng file đã chuyển (để hoàn tác)."""
    src_root = stage / "runs"
    if not src_root.is_dir():
        return out
    dst_root = _private_dir(paths.home / "runs")
    for dirpath, _dirnames, filenames in os.walk(src_root):
        for fn in filenames:
            src = Path(dirpath) / fn
            rel = src.relative_to(src_root)
            dst = dst_root / rel
            if dst.exists() or dst.is_symlink():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
            out.append(dst)
    return out


# ============================================================================ trong daemon
async def export_in_daemon(hub: "Hub", *, profile: str = DEFAULT_PROFILE, stop: bool = False, stop_wait_s: float = 60.0,
                           by: str = "user") -> dict[str, Any]:
    """Làm lặng hub (không khởi chạy run trong hàng đợi) → run đang chạy: `stop` thì dừng và chờ, không thì từ chối → chụp + ghi gói
    trong luồng phụ → thả lặng (luôn, kể cả lỗi)."""
    _require_human_process("xuất hub")
    if profile not in PROFILES:
        raise HubError(f"profile '{profile}' không hợp lệ (core|full)", code=-32602)
    runs = hub.runs
    if runs.quiesced:
        raise Conflict("hub đang xuất một gói khác — chờ xong rồi thử lại")
    runs.quiesced = "export"
    stopped: list[str] = []
    try:
        active = sorted(runs.active)
        if active and not stop:
            raise Conflict(f"có {len(active)} run đang chạy ({', '.join(active[:5])}) — chờ xong, hoặc thêm --stop để dừng chúng trước khi xuất",
                           data={"running": active})
        for rid in active:
            try:
                await runs.stop(rid)
                stopped.append(rid)
            except HubError:
                pass
        deadline = time.monotonic() + stop_wait_s
        while runs.active and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if runs.active:
            raise Conflict(f"run vẫn chưa dừng sau {stop_wait_s:.0f}s: {', '.join(sorted(runs.active)[:5])} — thử lại sau")
        res = await asyncio.to_thread(export_package, hub.paths, profile=profile, by=by)
    finally:
        runs.quiesced = None
        try:
            runs.tick()
        except Exception:  # pragma: no cover
            log.exception("tick sau khi xuất lỗi")
    return {**res, "stopped_runs": stopped}


def export_offline(paths: HubPaths, *, profile: str = DEFAULT_PROFILE) -> dict[str, Any]:
    """Xuất khi daemon KHÔNG chạy: giữ `daemon.lock` (daemon không thể khởi động chen) trong lúc chụp."""
    from .daemon import acquire_instance_lock, release_instance_lock

    _require_human_process("xuất hub")
    lock = acquire_instance_lock(paths)
    if lock is None:
        raise Conflict("daemon đang chạy — xuất qua daemon (bỏ chế độ ngoại tuyến)")
    try:
        return export_package(paths, profile=profile, by="user")
    finally:
        release_instance_lock(lock)


def register_rpc(daemon: "Daemon") -> None:
    """`hub.export {profile?, stop?}` (HUMAN_ONLY, tự audit). Nhập KHÔNG có RPC: daemon đích phải dừng → CLI của người."""
    from .mcp_server.rpc import wrap_with_audit

    hub = daemon.hub

    async def _export(p: dict[str, Any]) -> dict[str, Any]:
        profile = str(p.get("profile") or DEFAULT_PROFILE)
        return await export_in_daemon(hub, profile=profile, stop=bool(p.get("stop")), by="user")

    daemon.methods.update({"hub.export": wrap_with_audit(hub, "hub.export", _export)})


__all__ = ["seal_package", "check_passphrase", "has_secrets", "export_package", "export_in_daemon", "export_offline", "verify_package", "read_manifest", "check_manifest", "import_plan",
           "import_package", "build_pathmap", "parse_map_paths", "home_blank", "register_rpc", "MACHINE_STATE", "CLONE_STATE",
           "MUTATING_TRANSFER", "PROFILES", "MODES", "EXT", "exports_dir", "imports_dir"]
