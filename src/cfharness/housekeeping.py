"""Dung lượng HUB_HOME + thư mục làm việc dọn được (đề xuất #8 — `docs/dev-notes/housekeeping.md`).

- `disk_usage(home)`: dung lượng theo mục cấp 1 của HUB_HOME (logs, workspaces của agent, checkpoints, backups, exports…) + chỗ trống ổ đĩa.
  Không theo symlink; đi qua thư mục bằng `os.scandir` (không mở file).
- `scan(home, conn)`: thư mục làm việc trong HUB_HOME KHÔNG còn được tham chiếu:
  * `conv_missing` / `conv_archived` — `agents/<a>/workspaces/conv-<id>` của hội thoại điều phối viên không còn / đã lưu trữ;
  * `scratch_orphan` — `agents/<a>/workspaces/<tên>` không run/task/agent/team nào tham chiếu (không có run id `<tên>`, không cwd nào nằm trong);
  * `scratch_stale` — workspace scratch chỉ còn run đã KẾT THÚC tham chiếu, lần cuối cũ hơn `older_than_days` (chỉ khi người hỏi; mặc định
    doctor chỉ ĐẾM ở 30 ngày, không đưa vào danh sách dọn);
  * `worktree_orphan` — `HUB_HOME/worktrees/<tên>` không run/task nào tham chiếu;
  * `task_worktree_done` — git worktree NGOÀI HUB_HOME của task đã xong/lưu trữ còn trên đĩa: CHỈ BÁO (repo của người — hub không xoá,
    gợi ý `git worktree remove`).
  Thư mục có run CHƯA kết thúc (queued/running/waiting_human/interrupted) nằm trong → `busy`, không bao giờ dọn.
- `cleanup(hub, paths, older_than_days)`: CHỈ trong daemon (RPC `hub.cleanup`, HUMAN_ONLY, tự audit). Tính LẠI danh sách ngay lúc dọn,
  chỉ xoá giao của danh sách người đã xác nhận với ứng viên hiện tại (chống TOCTOU), kiểm lại từng thư mục (thật, không symlink, đúng vùng
  `agents/*/workspaces/*` hoặc `worktrees/*`, không run sống). Không bao giờ đụng `runs`/ledger/audit/DB — chỉ xoá thư mục trên đĩa.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .daemon import Daemon
    from .hub import Hub

log = logging.getLogger("cfharness.housekeeping")

# trạng thái run coi là CÒN SỐNG (interrupted: có thể tự resume sau restart — giữ cwd)
LIVE_RUN = ("queued", "running", "waiting_human", "interrupted")
OPEN_TASK = ("triage", "todo", "ready", "running", "review", "blocked")
CLEANABLE = ("conv_missing", "conv_archived", "scratch_orphan", "worktree_orphan")  # + scratch_stale khi người chọn older_than_days
STALE_DAYS_DEFAULT = 30
MAX_ITEMS = 300
CACHE_S = 600.0
LABELS = {
    "conv_missing": "hội thoại điều phối viên không còn",
    "conv_archived": "hội thoại điều phối viên đã lưu trữ",
    "scratch_orphan": "workspace scratch mồ côi",
    "scratch_stale": "workspace scratch của run đã xong (cũ)",
    "worktree_orphan": "worktree mồ côi",
    "task_worktree_done": "git worktree của task đã xong (ngoài HUB_HOME)",
}


# ============================================================================ dung lượng
def _du(p: Path) -> tuple[int, int]:
    """(byte, số file) — không theo symlink, bỏ qua lỗi quyền."""
    total = files = 0
    stack = [str(p)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(e.path)
                    else:
                        total += st.st_size
                        files += 1
        except OSError:
            continue
    return total, files


def disk_usage(home: Path) -> dict[str, Any]:
    """Dung lượng theo mục cấp 1 (giảm dần) + `agents/*/workspaces` gộp riêng (thường là phần lớn nhất trong `agents/`)."""
    entries: list[dict[str, Any]] = []
    total = 0
    try:
        top = sorted(os.scandir(home), key=lambda e: e.name)
    except OSError:
        top = []
    for e in top:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            b, n = _du(Path(e.path))
        else:
            b, n = st.st_size, 1
        total += b
        entries.append({"name": e.name, "bytes": b, "files": n, "dir": stat.S_ISDIR(st.st_mode)})
    ws_bytes = ws_files = 0
    agents = home / "agents"
    if agents.is_dir() and not agents.is_symlink():
        for a in _subdirs(agents):
            w = a / "workspaces"
            if w.is_dir() and not w.is_symlink():
                b, n = _du(w)
                ws_bytes += b
                ws_files += n
    entries.sort(key=lambda x: -x["bytes"])
    free = None
    try:
        du = shutil.disk_usage(home)
        free = {"free": du.free, "total": du.total}
    except OSError:
        pass
    return {"total": total, "entries": entries, "agent_workspaces": {"bytes": ws_bytes, "files": ws_files}, "disk": free}


def human(n: Any) -> str:
    try:
        f = float(n or 0)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"


# ============================================================================ quét thư mục làm việc
def _subdirs(p: Path) -> list[Path]:
    out = []
    try:
        for e in os.scandir(p):
            try:
                if e.is_dir(follow_symlinks=False):
                    out.append(Path(e.path))
            except OSError:
                continue
    except OSError:
        pass
    return sorted(out)


def _tables(c: sqlite3.Connection) -> set[str]:
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _parse_ts(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _norm(p: Any) -> str | None:
    if not p:
        return None
    s = os.path.expanduser(str(p))
    return os.path.normpath(s) if os.path.isabs(s) else None


def _ws_path(raw: Any) -> str | None:
    """Đường dẫn trong cột JSON `workspace`/`policies.workspace` (dict {kind, path} hoặc chuỗi)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return _norm(raw)
    if isinstance(raw, dict):
        if isinstance(raw.get("workspace"), (dict, str)):
            return _ws_path(raw["workspace"])
        return _norm(raw.get("path"))
    return None


def scan(home: Path, conn: sqlite3.Connection | None, *, older_than_days: float | None = None,
         now: datetime | None = None) -> dict[str, Any]:
    """Liệt kê thư mục dọn được. `older_than_days` = None → `scratch_stale` chỉ đếm (ngưỡng 30 ngày), không thành ứng viên dọn."""
    now = now or datetime.now(timezone.utc)
    home = Path(os.path.normpath(str(home)))
    stale_days = float(older_than_days) if older_than_days is not None else float(STALE_DAYS_DEFAULT)
    cutoff = now - timedelta(days=stale_days)
    tables = _tables(conn) if conn is not None else set()

    # ứng viên: đường dẫn → mục
    cand: dict[str, dict[str, Any]] = {}
    agents_dir = home / "agents"
    if agents_dir.is_dir() and not agents_dir.is_symlink():
        for a in _subdirs(agents_dir):
            w = a / "workspaces"
            if not w.is_dir() or w.is_symlink():
                continue
            for d in _subdirs(w):
                cand[str(d)] = {"path": str(d), "rel": d.relative_to(home).as_posix(), "agent": a.name, "name": d.name,
                                "zone": "conv" if d.name.startswith("conv-") else "scratch", "refs": 0, "live": [], "last": None}
    wt = home / "worktrees"
    if wt.is_dir() and not wt.is_symlink():
        for d in _subdirs(wt):
            cand[str(d)] = {"path": str(d), "rel": d.relative_to(home).as_posix(), "agent": None, "name": d.name, "zone": "worktree",
                            "refs": 0, "live": [], "last": None}

    def owner(p: str | None) -> dict[str, Any] | None:
        """Ứng viên chứa đường dẫn `p` (hoặc chính nó)."""
        if not p:
            return None
        cur = p
        for _ in range(64):
            if cur in cand:
                return cand[cur]
            parent = os.path.dirname(cur)
            if parent == cur or len(parent) < len(str(home)):
                return None
            cur = parent
        return None

    def touch(c: dict[str, Any] | None, *, live: str | None = None, ts: Any = None, permanent: bool = False) -> None:
        if c is None:
            return
        c["refs"] += 1
        if live:
            c["live"].append(live)
        if permanent:
            c["permanent"] = True
        t = _parse_ts(ts)
        if t and (c["last"] is None or t > c["last"]):
            c["last"] = t

    conv_rows: dict[str, Any] = {}
    if conn is not None:
        if "runs" in tables:
            by_name = {(c["agent"], c["name"]): c for c in cand.values() if c["zone"] == "scratch"}
            for r in conn.execute("SELECT id, agent_id, cwd, status, queued_at, started_at, ended_at FROM runs"):
                live = r[0] if r[3] in LIVE_RUN else None
                ts = r[6] or r[5] or r[4]
                hit = owner(_norm(r[2]))
                touch(hit, live=live, ts=ts)
                byid = by_name.get((r[1], r[0]))
                if byid is not None and byid is not hit:
                    touch(byid, live=live, ts=ts)
        if "tasks" in tables:
            for r in conn.execute(f"SELECT id, status, workspace FROM tasks WHERE workspace IS NOT NULL AND status IN ({','.join('?' * len(OPEN_TASK))})",
                                  OPEN_TASK):
                touch(owner(_ws_path(r[2])), live=f"task {r[0]}")
        if "agents" in tables:
            for r in conn.execute("SELECT workspace_default FROM agents WHERE workspace_default IS NOT NULL"):
                touch(owner(_norm(r[0])), permanent=True)
        if "teams" in tables:
            for r in conn.execute("SELECT policies FROM teams"):
                touch(owner(_ws_path(r[0])), permanent=True)
        if "manager_conversations" in tables:
            conv_rows = {r[0]: r[1] for r in conn.execute("SELECT id, archived_at FROM manager_conversations")}

    items: list[dict[str, Any]] = []
    stale_count = stale_bytes = 0
    for c in cand.values():
        kind = None
        if c["zone"] == "conv":
            cid = c["name"][len("conv-"):]
            if conn is None or "manager_conversations" not in tables:
                continue
            if cid not in conv_rows:
                kind = "conv_missing"
            elif conv_rows[cid]:
                kind = "conv_archived"
        elif c.get("permanent"):
            continue
        elif c["refs"] == 0:
            if conn is None:
                continue  # không đọc được DB → không đoán "mồ côi"
            kind = "scratch_orphan" if c["zone"] == "scratch" else "worktree_orphan"
        elif c["zone"] == "scratch" and not c["live"] and c["last"] is not None and c["last"] < cutoff and _mtime_before(c["path"], cutoff):
            kind = "scratch_stale"
        if kind is None:
            continue
        b, n = _du(Path(c["path"]))
        it = {"kind": kind, "path": c["path"], "rel": c["rel"], "agent": c["agent"], "bytes": b, "files": n,
              "busy": sorted(set(c["live"]))[:5], "last": c["last"].isoformat() if c["last"] else None}
        if kind == "scratch_stale":
            stale_count += 1
            stale_bytes += b
        it["cleanable"] = not it["busy"] and (kind in CLEANABLE or (kind == "scratch_stale" and older_than_days is not None))
        items.append(it)

    # git worktree của task đã xong, NGOÀI HUB_HOME: chỉ báo
    task_wt: list[dict[str, Any]] = []
    if conn is not None and "tasks" in tables:
        open_paths = set()
        done: list[tuple[str, str]] = []
        for r in conn.execute("SELECT id, status, workspace FROM tasks WHERE workspace IS NOT NULL"):
            try:
                ws = json.loads(r[2]) if isinstance(r[2], str) else None
            except ValueError:
                ws = None
            if not isinstance(ws, dict) or ws.get("kind") != "worktree":
                continue
            p = _norm(ws.get("path"))
            if not p:
                continue
            if r[1] in OPEN_TASK:
                open_paths.add(p)
            elif r[1] in ("done", "archived"):
                done.append((r[0], p))
        seen = set()
        for tid, p in done:
            if p in open_paths or p in seen or p.startswith(str(home) + os.sep):
                continue
            seen.add(p)
            if os.path.isdir(p) and not os.path.islink(p):
                task_wt.append({"kind": "task_worktree_done", "task": tid, "path": p, "cleanable": False,
                                "fix": f"git -C {p} status && git worktree remove {p}"})
    items.sort(key=lambda x: (-x["bytes"], x["rel"]))
    cleanable = [x for x in items if x["cleanable"]]
    by_kind: dict[str, dict[str, int]] = {}
    for x in items:
        k = by_kind.setdefault(x["kind"], {"count": 0, "bytes": 0})
        k["count"] += 1
        k["bytes"] += x["bytes"]
    return {"items": items[:MAX_ITEMS], "truncated": len(items) > MAX_ITEMS, "by_kind": by_kind,
            "cleanable": {"count": len(cleanable), "bytes": sum(x["bytes"] for x in cleanable)},
            "stale": {"days": stale_days, "count": stale_count, "bytes": stale_bytes},
            "busy": sum(1 for x in items if x["busy"]), "task_worktrees": task_wt[:50],
            "older_than_days": older_than_days}


def _mtime_before(path: str, cutoff: datetime) -> bool:
    """Thư mục (chính nó) không bị sửa sau mốc — phòng hờ run cũ nhưng thư mục vừa được ai đó ghi tiếp."""
    try:
        return datetime.fromtimestamp(os.lstat(path).st_mtime, timezone.utc) < cutoff
    except OSError:
        return False


def report(home: Path, conn: sqlite3.Connection | None, *, older_than_days: float | None = None) -> dict[str, Any]:
    return {"disk": disk_usage(home), "cleanup": scan(home, conn, older_than_days=older_than_days), "at": datetime.now(timezone.utc).isoformat()}


# ============================================================================ dọn (CHỈ trong daemon)
def _safe_target(home: Path, path: str) -> bool:
    """Chỉ `agents/<a>/workspaces/<x>` hoặc `worktrees/<x>`, thư mục thật (không symlink ở bất kỳ cấp nào trong HUB_HOME)."""
    try:
        rel = Path(path).relative_to(home).parts
    except ValueError:
        return False
    if not ((len(rel) == 4 and rel[0] == "agents" and rel[2] == "workspaces") or (len(rel) == 2 and rel[0] == "worktrees")):
        return False
    if any(x in ("", ".", "..") for x in rel):
        return False
    cur = home
    for part in rel:
        cur = cur / part
        try:
            if stat.S_ISLNK(os.lstat(cur).st_mode):
                return False
        except OSError:
            return False
    return stat.S_ISDIR(os.lstat(cur).st_mode)


def _live_cwds(hub: "Hub") -> list[str]:
    rows = hub.store.conn.execute(f"SELECT id, agent_id, cwd FROM runs WHERE status IN ({','.join('?' * len(LIVE_RUN))})", LIVE_RUN).fetchall()
    out = []
    for r in rows:
        if r[2]:
            out.append(os.path.normpath(str(r[2])))
        try:
            ag = hub.store.get_agent(r[1]) or {}
            if ag.get("home_path"):
                out.append(os.path.normpath(str(Path(ag["home_path"]) / "workspaces" / r[0])))
        except Exception:  # noqa: BLE001
            log.debug("housekeeping: không đọc được agent của workspace", exc_info=True)
    return out


def cleanup(hub: "Hub", paths: list[str], *, older_than_days: float | None = None) -> dict[str, Any]:
    """Xoá các thư mục `paths` (tương đối HUB_HOME, từ danh sách người đã xem) NẾU chúng vẫn là ứng viên dọn được ngay lúc này."""
    home = Path(os.path.normpath(str(hub.paths.home)))
    want = {str(p).strip().strip("/") for p in paths or [] if str(p).strip()}
    if not want:
        return {"removed": [], "skipped": [], "freed_bytes": 0}
    cur = scan(home, hub.store.conn, older_than_days=older_than_days)
    by_rel = {x["rel"]: x for x in cur["items"]}
    live = _live_cwds(hub)
    removed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    freed = 0
    for rel in sorted(want):
        it = by_rel.get(rel)
        if it is None or not it["cleanable"]:
            skipped.append({"rel": rel, "reason": "không còn là ứng viên dọn" if it is None else ("đang có run/task dùng" if it["busy"] else "không dọn tự động")})
            continue
        p = it["path"]
        if any(c == p or c.startswith(p + os.sep) for c in live):
            skipped.append({"rel": rel, "reason": "đang có run dùng"})
            continue
        if not _safe_target(home, p):
            skipped.append({"rel": rel, "reason": "không phải thư mục thật trong vùng làm việc của hub"})
            continue
        try:
            shutil.rmtree(p)
        except OSError as e:
            skipped.append({"rel": rel, "reason": f"lỗi xoá: {e}"[:200]})
            continue
        freed += int(it["bytes"])
        removed.append({"rel": rel, "kind": it["kind"], "bytes": it["bytes"]})
    log.info("dọn thư mục làm việc: xoá %d, bỏ qua %d, giải phóng %s", len(removed), len(skipped), human(freed))
    return {"removed": removed, "skipped": skipped, "freed_bytes": freed, "status": "done", "count": len(removed)}


def register_rpc(daemon: "Daemon") -> None:
    """`hub.housekeeping {older_than_days?, refresh?}` (HUMAN_ONLY, chỉ đọc; cache 10 phút — web thẻ Sức khoẻ) và
    `hub.cleanup {paths: [rel…], older_than_days?}` (HUMAN_ONLY, tự audit; chỉ xoá thư mục còn là ứng viên lúc gọi)."""
    import asyncio

    from .mcp_server.rpc import wrap_with_audit

    hub = daemon.hub
    cache: dict[str, Any] = {}

    async def _report(p: dict[str, Any]) -> dict[str, Any]:
        days = p.get("older_than_days")
        days = float(days) if days not in (None, "") else None
        key = str(days)
        hit = cache.get(key)
        if hit and not p.get("refresh") and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        # quét đĩa trong luồng phụ (có thể chậm khi checkpoints lớn); đọc DB qua kết nối chỉ đọc riêng (an toàn ngoài luồng chính)
        def work() -> dict[str, Any]:
            c = None
            try:
                c = sqlite3.connect(f"file:{hub.paths.db}?mode=ro", uri=True, timeout=5)
                return report(hub.paths.home, c, older_than_days=days)
            finally:
                if c is not None:
                    c.close()

        res = await asyncio.to_thread(work)
        cache[key] = (time.monotonic(), res)
        return res

    def _cleanup(p: dict[str, Any]) -> dict[str, Any]:
        days = p.get("older_than_days")
        res = cleanup(hub, list(p.get("paths") or []), older_than_days=float(days) if days not in (None, "") else None)
        cache.clear()
        return res

    daemon.methods["hub.housekeeping"] = _report
    daemon.methods["hub.cleanup"] = wrap_with_audit(hub, "hub.cleanup", _cleanup)


__all__ = ["disk_usage", "scan", "report", "cleanup", "human", "register_rpc", "LABELS"]
