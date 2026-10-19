"""Chuyển HUB_HOME tên cũ (`~/.agent-hub`) sang home mới (`~/.cfharness`) — `home migrate|status`.  rename:keep

Các bước (chạy lại được: nếu thư mục đã chuyển mà còn đường dẫn cũ trong DB thì chỉ viết lại):
1. Daemon của home cũ phải dừng (`--stop` để tự dừng qua RPC `hub.shutdown`).
2. `os.rename(cũ, mới)` (cùng ổ đĩa dưới ~), xoá socket/pid/token cũ còn sót.
3. Viết lại đường dẫn tuyệt đối trong `hub.db` ở mọi cột chữ của bảng KHÔNG append-only (agents.home_path/trash_path,
   runs.cwd/raw_log_path/stderr_log_path, skills.path, teams.trash_path, hub_state, tasks…). KHÔNG sửa bảng append-only
   (`APPEND_ONLY`) và cột lịch sử (`HISTORY_COLS`): đó là bản ghi quá khứ, giữ nguyên chữ cũ.
4. Viết lại file cấu hình cấp hub có chứa đường dẫn cũ (`REWRITE_FILES`: config.yaml, policy/*.yaml ngoài history/,
   mcp/registry.yaml, checkpoints/*/checkpoint.json) và cập nhật bản chuẩn toàn vẹn (`integrity_baseline`) cho file
   policy/config đã viết lại — CHỈ khi file trên đĩa đang khớp bản chuẩn (file bị sửa ngoài vẫn bị phát hiện như cũ).
   File khác có chứa đường dẫn cũ (memory/skill/config agent — thuộc ledger) chỉ được LIỆT KÊ để người xem, không sửa.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import HubPaths, legacy_default_home, legacy_home_in_use, resolve_home

APPEND_ONLY = frozenset({"events", "audit_log", "guard_decisions", "snapshots", "ledger_entries", "integrity_baseline", "sqlite_sequence"})
HISTORY_COLS = frozenset({("runs", "argv_redacted"), ("runs", "diff")})
REWRITE_FILES = ("config.yaml", "policy/*.yaml", "policy/golden/*", "mcp/registry.yaml", "checkpoints/*/checkpoint.json")
SCAN_SKIP_DIRS = ("checkpoints", "runs", "logs", "snapshots", "ledger", "prompts", "policy/history", "trash/tamper", "worktrees", "evals", ".git")
STALE = ("hub.sock", "hub.sock.auth", "hub.pid")
_TEXT_SUFFIX = (".yaml", ".yml", ".json", ".md", ".txt", ".toml")
_MAX_SCAN = 2 * 1024 * 1024


@dataclass
class Plan:
    src: Path
    dst: Path
    moved: bool = False  # thư mục đã ở chỗ mới (chạy lại sau lần dở)
    db: dict[str, int] = field(default_factory=dict)  # "bảng.cột" → số hàng
    files: list[str] = field(default_factory=list)  # file cấp hub sẽ viết lại (tương đối)
    review: list[str] = field(default_factory=list)  # file khác có đường dẫn cũ — người xem
    baseline: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"from": str(self.src), "to": str(self.dst), "moved": self.moved, "db": self.db, "files": self.files,
                "review": self.review, "baseline": self.baseline}


def _pattern(old: str) -> re.Pattern[str]:
    # đúng đường dẫn cũ (không khớp `<cũ>2`): theo sau là hết chuỗi, `/`, dấu nháy, khoảng trắng, phân cách JSON
    return re.compile(re.escape(old) + r"(?=$|[/\"'\s,}\]:;)])")


def rewrite_text(s: str, olds: list[str], new: str) -> str:
    for o in olds:
        s = _pattern(o).sub(new, s)
    return s


def _olds(src: Path) -> list[str]:
    out = [str(src).rstrip("/")]
    rp = os.path.realpath(src).rstrip("/")
    if rp not in out:
        out.append(rp)
    return sorted(out, key=len, reverse=True)


def _db_targets(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        if t in APPEND_ONLY:
            continue
        for col in conn.execute(f'PRAGMA table_info("{t}")'):
            name, decl = col[1], (col[2] or "").upper()
            if (t, name) in HISTORY_COLS or any(x in decl for x in ("INT", "REAL", "BLOB")):
                continue
            out.append((t, name))
    return out


def _db_rewrite(db: Path, olds: list[str], new: str, *, apply: bool) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not db.exists():
        return counts
    conn = sqlite3.connect(str(db), isolation_level=None)
    try:
        if apply:
            conn.execute("BEGIN IMMEDIATE")
        for t, c in _db_targets(conn):
            like = " OR ".join(f'"{c}" LIKE ?' for _ in olds)
            rows = conn.execute(f'SELECT rowid, "{c}" FROM "{t}" WHERE typeof("{c}")=\'text\' AND ({like})', [f"%{o}%" for o in olds]).fetchall()
            n = 0
            for rowid, val in rows:
                nv = rewrite_text(val, olds, new)
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


def _rel_matches(rel: str, pats: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(rel, p) for p in pats)


def _scan_files(home: Path, olds: list[str]) -> tuple[list[str], list[str]]:
    """(file cấp hub sẽ viết lại, file khác chứa đường dẫn cũ) — tương đối với `home`."""
    rewrite: list[str] = []
    review: list[str] = []
    needles = [o.encode() for o in olds]
    for dirpath, dirnames, filenames in os.walk(home, followlinks=False):
        rel_dir = os.path.relpath(dirpath, home)
        rel_dir = "" if rel_dir == "." else rel_dir
        for fn in filenames:
            rel = f"{rel_dir}/{fn}" if rel_dir else fn
            full = Path(dirpath) / fn
            is_target = _rel_matches(rel, REWRITE_FILES) and not rel.startswith("policy/history/")
            if not is_target:
                if not fn.endswith(_TEXT_SUFFIX) or any(rel == d or rel.startswith(d + "/") for d in SCAN_SKIP_DIRS):
                    continue
            try:
                if full.is_symlink() or full.stat().st_size > _MAX_SCAN:
                    continue
                b = full.read_bytes()
            except OSError:
                continue
            if any(n in b for n in needles):
                (rewrite if is_target else review).append(rel)
    return sorted(rewrite), sorted(review)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _rewrite_files(home: Path, rels: list[str], olds: list[str], new: str) -> list[str]:
    """Viết lại file; cập nhật integrity_baseline cho file đang khớp bản chuẩn. Trả danh sách bản chuẩn đã cập nhật."""
    db = home / "hub.db"
    conn = sqlite3.connect(str(db), isolation_level=None) if db.exists() else None
    updated: list[str] = []
    try:
        for rel in rels:
            p = home / rel
            raw = p.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            nb = rewrite_text(text, olds, new).encode("utf-8")
            if nb == raw:
                continue
            mode = p.stat().st_mode & 0o777
            tmp = p.with_name(p.name + ".migrate-tmp")
            tmp.write_bytes(nb)
            os.chmod(tmp, mode)
            os.replace(tmp, p)
            if conn is not None:
                row = conn.execute("SELECT sha256 FROM integrity_baseline WHERE path=?", (rel,)).fetchone()
                if row is not None and row[0] == _sha(raw):
                    conn.execute("UPDATE integrity_baseline SET sha256=?, content=?, updated_at=? WHERE path=?",
                                 (_sha(nb), nb, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), rel))
                    updated.append(rel)
    finally:
        if conn is not None:
            conn.close()
    return updated


def daemon_running(home: Path) -> int | None:
    """pid daemon của `home` nếu còn trả lời ping, ngược lại None."""
    from .rpc import DaemonUnavailable, RpcClient

    try:
        st = RpcClient(HubPaths(home).socket, timeout=3).call("hub.ping")
        return int(st.get("pid") or 0) or -1
    except DaemonUnavailable:
        return None
    except Exception:
        return -1


def stop_daemon(home: Path, wait_s: float = 30.0) -> None:
    from .rpc import DaemonUnavailable, RpcClient

    sock = HubPaths(home).socket
    try:
        RpcClient(sock, timeout=10).call("hub.shutdown")
    except DaemonUnavailable:
        return
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline and daemon_running(home) is not None:
        time.sleep(0.2)
    if daemon_running(home) is not None:
        raise RuntimeError("daemon của home cũ không dừng trong thời gian chờ — dừng tay rồi chạy lại")


def plan(src: Path, dst: Path) -> Plan:
    src, dst = Path(src).expanduser(), Path(dst).expanduser()
    pl = Plan(src=src, dst=dst)
    if not src.exists() and dst.is_dir():
        pl.moved = True  # lần trước đã chuyển — chỉ viết lại phần còn sót
    elif not (src / "hub.db").exists():
        raise ValueError(f"không thấy HUB_HOME cũ có hub.db ở {src}")
    elif dst.exists() and any(dst.iterdir()):
        raise ValueError(f"đích {dst} đã tồn tại và không rỗng — chuyển tay hoặc xoá trước")
    s_abs, d_abs = src.resolve() if src.exists() else src, dst.resolve() if dst.exists() else dst
    if str(d_abs).startswith(str(s_abs) + "/") or str(s_abs).startswith(str(d_abs) + "/") or s_abs == d_abs:
        raise ValueError("thư mục cũ và mới không được trùng/lồng nhau")
    home = dst if pl.moved else src
    olds = _olds(src) if src.exists() else [str(src).rstrip("/")]
    pl.db = _db_rewrite(home / "hub.db", olds, str(dst), apply=False)
    pl.files, pl.review = _scan_files(home, olds)
    return pl


def migrate(src: Path, dst: Path, *, stop: bool = False) -> Plan:
    pl = plan(src, dst)
    src, dst = pl.src, pl.dst
    olds = _olds(src) if src.exists() else [str(src).rstrip("/")]
    if not pl.moved:
        if daemon_running(src) is not None:
            if not stop:
                raise ValueError("daemon của home cũ đang chạy — dừng trước (`cfharness daemon stop`) hoặc thêm --stop")
            stop_daemon(src)
        conn = sqlite3.connect(str(src / "hub.db"))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        if dst.exists():
            dst.rmdir()  # rỗng (đã kiểm ở plan)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.rename(src, dst)
        pl.moved = True
    for n in STALE:
        try:
            (dst / n).unlink()
        except OSError:
            pass
    pl.db = _db_rewrite(dst / "hub.db", olds, str(dst), apply=True)
    pl.baseline = _rewrite_files(dst, pl.files, olds, str(dst))
    return pl


# ============================================================================ CLI
def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    hp = sub.add_parser("home", help="HUB_HOME: xem đang dùng thư mục nào, chuyển home tên cũ sang tên mới", parents=[common])
    hs = hp.add_subparsers(dest="home_action", required=True)
    hs.add_parser("status", help="HUB_HOME đang dùng, có còn home tên cũ không", parents=[common])
    m = hs.add_parser("migrate", help="chuyển home tên cũ → home mặc định mới và viết lại đường dẫn trong DB", parents=[common])
    m.add_argument("--from", dest="src", help="home cũ (mặc định home tên cũ dưới ~)")
    m.add_argument("--to", dest="dst", help="home mới (mặc định home mặc định hiện tại)")
    m.add_argument("--dry-run", action="store_true", help="chỉ in kế hoạch, không ghi gì")
    m.add_argument("--stop", action="store_true", help="tự dừng daemon của home cũ nếu đang chạy")
    hp.set_defaults(fn=cmd_home)


def _default_pair(a: argparse.Namespace) -> tuple[Path, Path]:
    from .paths import DEFAULT_HOME_DIRNAME

    legacy = legacy_default_home()
    src = Path(a.src).expanduser() if a.src else legacy
    dst = Path(a.dst).expanduser() if a.dst else Path.home() / DEFAULT_HOME_DIRNAME
    if src is None:
        raise ValueError("cần --from (home cũ) — tên home mặc định chưa đổi")
    return src, dst


def _print_plan(pl: Plan, applied: bool) -> None:
    head = "ĐÃ CHUYỂN" if applied else "KẾ HOẠCH (chưa ghi gì)"
    print(f"{head}: {pl.src} → {pl.dst}" + (" (thư mục đã ở chỗ mới — chỉ viết lại phần còn sót)" if pl.moved and not applied else ""))
    total = sum(pl.db.values())
    print(f"  DB: {total} giá trị đường dẫn" + ("" if total else " (không có)"))
    for k, v in sorted(pl.db.items()):
        print(f"    {k:<32} {v}")
    print("  không sửa (lịch sử): bảng " + ", ".join(sorted(APPEND_ONLY - {"sqlite_sequence"})) + "; cột "
          + ", ".join(f"{t}.{c}" for t, c in sorted(HISTORY_COLS)))
    if pl.files:
        print("  file viết lại: " + ", ".join(pl.files))
    if pl.baseline:
        print("  bản chuẩn toàn vẹn cập nhật: " + ", ".join(pl.baseline))
    if pl.review:
        print("  CẦN NGƯỜI XEM (có đường dẫn cũ, không tự sửa): " + ", ".join(pl.review))


def cmd_home(ctx: Any) -> int:
    a = ctx.args
    if a.home_action == "status":
        home = ctx.paths.home
        legacy = legacy_default_home()
        info = {"home": str(home), "legacy_in_use": legacy_home_in_use(home), "legacy_home": str(legacy) if legacy else None,
                "legacy_exists": bool(legacy and legacy.exists())}
        text = f"HUB_HOME = {home}"
        if info["legacy_in_use"]:
            text += "\n  đang dùng home TÊN CŨ — chạy `cfharness home migrate` để chuyển sang tên mới"
        elif info["legacy_exists"]:
            text += f"\n  vẫn còn home tên cũ {legacy} (không dùng)"
        ctx.out(info, text)
        return 0
    src, dst = _default_pair(a)
    if a.dry_run:
        pl = plan(src, dst)
        if ctx.json:
            ctx.out(pl.as_dict())
        else:
            _print_plan(pl, False)
        return 0
    pl = migrate(src, dst, stop=a.stop)
    if ctx.json:
        ctx.out(pl.as_dict())
    else:
        _print_plan(pl, True)
        print("Tiếp theo: `cfharness daemon start`, rồi `cfharness mcp snippet` để cập nhật ~/.claude.json nếu cần.")
    return 0


def legacy_notice(home: Path) -> str | None:
    """Cảnh báo (tối đa 1 lần/ngày, mốc `HUB_HOME/.legacy_notice`) khi CLI chạy trên home tên cũ."""
    if not legacy_home_in_use(home) or resolve_home() != home:
        return None
    mark = home / ".legacy_notice"
    try:
        if mark.exists() and time.time() - mark.stat().st_mtime < 86400:
            return None
        mark.touch()
    except OSError:
        pass
    return f"lưu ý: đang dùng HUB_HOME tên cũ {home} — chạy `cfharness home migrate --dry-run` rồi `cfharness home migrate --stop` để chuyển."
