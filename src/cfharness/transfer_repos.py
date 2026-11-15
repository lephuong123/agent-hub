"""Repo workspace của hub khi chuyển sang máy khác — phần `repos.json` (optional) của gói `.cfhx` (PR5 của docs/dev-notes/plan-hub-clone.md
§4.4, §6.7).

Repo là dữ liệu CỦA NGƯỜI (có thể rất lớn, có lịch sử riêng) — KHÔNG BAO GIỜ được đóng gói. Gói chỉ ghi đủ để máy mới biết phải clone gì:
- XUẤT (`collect`): đường dẫn tuyệt đối NGOÀI HUB_HOME mà hub đang dùng làm thư mục làm việc — `workspace_default` của agent chưa lưu trữ,
  `policies.workspace` (`dir`/`worktree`) của team chưa lưu trữ, `workspace` của task còn mở, `cwd` của run chưa xong. Mỗi đường dẫn là repo
  git → gốc repo, `remote.origin.url` (CHE thông tin đăng nhập: bỏ `user:pass@`/token trong URL + che theo giá trị secret), nhánh, HEAD,
  `dirty` + số file chưa commit. git chạy với `workspace.GIT_SAFE_ARGS` + `GIT_ENV` (không hook/fsmonitor/sshCommand/pager, không hỏi mật khẩu).
- NHẬP (`plan`): đường dẫn → bản đồ đường dẫn của lần nhập; repo không có ở máy đích → báo + lệnh gợi ý `git clone <remote> <đích> && git -C
  <đích> checkout <nhánh>`; repo dirty ở nguồn → cảnh báo "thay đổi chưa commit KHÔNG đi theo gói". `pause_missing` (trên DB của stage): agent có
  `workspace_default` không tồn tại ở máy này → `paused` + `hub_state.import_paused[agent] = {reason, workspace}` (lý do hiện ở `hub doctor`, lỗi
  submit); `agent resume` (người) gỡ khi đã có repo. `clone` (`--clone-repos`, chạy SAU khi hub.db đã vào chỗ): `git clone` từng repo thiếu có
  remote — không bao giờ vào thư mục đã có (kiểm trước + git từ chối thư mục không rỗng); lỗi một repo → báo, không huỷ lần nhập.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Callable

from .oscompat import no_window_kwargs
from .pathmap import PathMap, is_abs

SECTION = "repos"
MEMBER = "repos.json"
SCHEMA = 1
PAUSE_REASON = "[hub] workspace chưa có trên máy này"
PAUSED_KEY = "import_paused"
REPOS_KEY = "import_repos"
OPEN_TASKS = ("triage", "todo", "ready", "running", "review", "blocked")
OPEN_RUNS = ("running", "queued", "waiting_human")
CLONE_TIMEOUT_S = 1800
MAX_USED_BY = 50
_URL_CRED = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*://)([^/?#@]*)@")


# ============================================================================ tiện ích
def mask_remote(url: str | None) -> tuple[str | None, bool]:
    """(URL đã che, có che thông tin đăng nhập không). `https://user:tok@host/x` → `https://host/x`; dạng scp `git@host:x` giữ (user `git`
    không phải bí mật); còn lại che theo GIÁ TRỊ secret (`redact_str`)."""
    if not url:
        return None, False
    from .redact import redact_str

    u = str(url).strip()
    hit = False
    m = _URL_CRED.match(u)
    if m:
        u = m.group(1) + u[m.end():]
        hit = True
    u2 = re.sub(r"([?&](?:access_token|token|private_token|password|key)=)[^&#]*", r"\1***", u, flags=re.I)
    hit = hit or u2 != u
    out = redact_str(u2)
    return out, hit or out != u2


def _git(cwd: str | Path, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    from .workspace import GIT_ENV, GIT_SAFE_ARGS

    return subprocess.run(["git", *GIT_SAFE_ARGS, "-C", str(cwd), *args], capture_output=True, timeout=timeout, encoding="utf-8",
                          errors="replace", env={**os.environ, **GIT_ENV}, **no_window_kwargs())


def git_info(path: str | Path) -> dict[str, Any]:
    """Trạng thái git của một thư mục (đọc, không ghi). Không phải repo / git lỗi → `{"git": False}`."""
    from .workspace import git_root

    p = Path(path).expanduser()
    out: dict[str, Any] = {"exists": p.is_dir(), "git": False}
    if not p.is_dir():
        return out
    try:
        root = git_root(p)
    except Exception:  # pragma: no cover
        root = None
    if root is None:
        return out
    out.update(git=True, root=root)
    try:
        r = _git(root, "config", "--get", "remote.origin.url")
        remote, masked = mask_remote(r.stdout.strip() if r.returncode == 0 else None)
        out.update(remote=remote, remote_masked=masked)
        b = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        out["branch"] = b.stdout.strip() if b.returncode == 0 else None
        h = _git(root, "rev-parse", "HEAD")
        out["head"] = h.stdout.strip() if h.returncode == 0 else None
        s = _git(root, "status", "--porcelain", "--untracked-files=normal", timeout=120)
        lines = [x for x in s.stdout.splitlines() if x.strip()] if s.returncode == 0 else []
        out.update(dirty=bool(lines), uncommitted=len(lines))
    except (OSError, subprocess.TimeoutExpired) as e:
        out["error"] = str(e)[:200]
    return out


def _ws_path(v: Any) -> str | None:
    """Giá trị workspace (chuỗi đường dẫn | JSON `{kind, path}` | dict) → đường dẫn tuyệt đối kiểu thư mục, hoặc None (scratch…)."""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("{"):
            try:
                v = json.loads(s)
            except ValueError:
                return None
        else:
            s = s[4:] if s.startswith("dir:") else s
            s = os.path.expanduser(s)
            return s if is_abs(s) else None
    if isinstance(v, dict):
        if v.get("kind") not in (None, "dir", "worktree") or not v.get("path"):
            return None
        s = os.path.expanduser(str(v["path"]))
        return s if is_abs(s) else None
    return None


def _inside(p: str, roots: list[str]) -> bool:
    for r in roots:
        r = r.rstrip("/\\")
        if r and (p == r or p.startswith(r + os.sep) or p.startswith(r + "/")):
            return True
    return False


# ============================================================================ xuất
def used_paths(db: Path, hub_roots: list[str]) -> dict[str, list[dict[str, str]]]:
    """{đường dẫn: [ai dùng]} từ DB (đã chụp) — chỉ đường dẫn tuyệt đối NGOÀI HUB_HOME."""
    out: dict[str, list[dict[str, str]]] = {}

    def add(path: str | None, kind: str, ident: str) -> None:
        if not path or _inside(path, hub_roots):
            return
        lst = out.setdefault(path, [])
        if len(lst) < MAX_USED_BY:
            lst.append({"kind": kind, "id": str(ident)})

    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tabs = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "agents" in tabs:
            for aid, ws in c.execute("SELECT id, workspace_default FROM agents WHERE archived_at IS NULL AND workspace_default IS NOT NULL"):
                add(_ws_path(ws), "agent", aid)
        if "teams" in tabs:
            for tid, pol in c.execute("SELECT id, policies FROM teams WHERE archived_at IS NULL"):
                try:
                    add(_ws_path((json.loads(pol or "{}") or {}).get("workspace")), "team", tid)
                except (ValueError, AttributeError):
                    continue
        if "tasks" in tabs:
            q = f"SELECT id, workspace FROM tasks WHERE workspace IS NOT NULL AND status IN ({','.join('?' * len(OPEN_TASKS))})"
            for tid, ws in c.execute(q, OPEN_TASKS):
                add(_ws_path(ws), "task", tid)
        if "runs" in tabs:
            cols = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
            cont = " OR (status='interrupted' AND continued_in_run_id IS NULL)" if "continued_in_run_id" in cols else ""
            q = f"SELECT id, cwd FROM runs WHERE cwd IS NOT NULL AND (status IN ({','.join('?' * len(OPEN_RUNS))}){cont})"
            for rid, cwd in c.execute(q, OPEN_RUNS):
                add(_ws_path(cwd), "run", rid)
    finally:
        c.close()
    return out


def collect(db: Path, hub_roots: list[str]) -> list[dict[str, Any]]:
    """Danh sách repo cho `repos.json`, gộp theo gốc repo git (thư mục không phải git: theo chính đường dẫn). Không đọc nội dung file."""
    by_root: dict[str, dict[str, Any]] = {}
    for path, users in sorted(used_paths(db, hub_roots).items()):
        info = git_info(path)
        key = str(info.get("root") or path)
        ent = by_root.get(key)
        if ent is None:
            ent = {"path": key, **{k: v for k, v in info.items() if k != "root"}, "used_by": []}
            by_root[key] = ent
        for u in users:
            if len(ent["used_by"]) < MAX_USED_BY:
                ent["used_by"].append({**u, "path": path})
    return [by_root[k] for k in sorted(by_root)]


def payload(repos: list[dict[str, Any]]) -> bytes:
    return json.dumps({"schema": SCHEMA, "repos": repos}, ensure_ascii=False, indent=1).encode("utf-8")


# ============================================================================ nhập
def read_stage(stage: Path) -> list[dict[str, Any]]:
    p = stage / MEMBER
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    repos = data.get("repos") if isinstance(data, dict) else None
    return [r for r in repos if isinstance(r, dict) and isinstance(r.get("path"), str)] if isinstance(repos, list) else []


def clone_hint(r: dict[str, Any]) -> str | None:
    if not r.get("remote"):
        return None
    to = r["to"]
    cmd = f"git clone {r['remote']} {_q(to)}"
    br = r.get("branch")
    if br and br != "HEAD":
        cmd += f" && git -C {_q(to)} checkout {br}"
    elif r.get("head"):
        cmd += f" && git -C {_q(to)} checkout {r['head']}"
    return cmd


def _q(s: str) -> str:
    return f'"{s}"' if any(ch in s for ch in " '\"$`") else s


def plan(stage: Path, pm: PathMap) -> dict[str, Any]:
    """Kế hoạch repo của lần nhập: mỗi repo `from → to` (theo bản đồ đường dẫn), có ở máy này chưa, HEAD có khớp, lệnh gợi ý."""
    repos: list[dict[str, Any]] = []
    for r in read_stage(stage):
        to = pm.rewrite(r["path"], syntax="text")
        ent = {"from": r["path"], "to": to, "git": bool(r.get("git")), "remote": r.get("remote"), "remote_masked": bool(r.get("remote_masked")),
               "branch": r.get("branch"), "head": r.get("head"), "dirty": bool(r.get("dirty")), "uncommitted": int(r.get("uncommitted") or 0),
               "used_by": [{**u, "path": pm.rewrite(str(u.get("path") or ""), syntax="text")} for u in (r.get("used_by") or []) if isinstance(u, dict)],
               "exists": Path(to).expanduser().exists() if is_abs(to) else False}
        if ent["exists"] and ent["git"]:
            cur = git_info(to)
            ent["head_now"] = cur.get("head")
            ent["head_match"] = bool(cur.get("head")) and cur.get("head") == ent["head"]
        ent["hint"] = None if ent["exists"] else clone_hint(ent)
        repos.append(ent)
    warnings: list[str] = []
    missing = [r for r in repos if not r["exists"]]
    for r in missing:
        warnings.append(f"repo {r['to']} chưa có ở máy này" + (f" — clone: {r['hint']}" if r["hint"] else " (không có remote — chép tay)")
                        + " (hoặc nhập với --clone-repos)")
    for r in repos:
        if r["dirty"]:
            warnings.append(f"repo {r['from']} có {r['uncommitted']} file chưa commit ở máy nguồn — thay đổi chưa commit KHÔNG đi theo gói "
                            "(commit + push ở máy nguồn rồi pull ở máy này)")
        if r["exists"] and r["git"] and r.get("head") and r.get("head_match") is False:
            warnings.append(f"repo {r['to']} đã có nhưng HEAD khác máy nguồn ({str(r.get('head_now') or '?')[:12]} ≠ {str(r['head'])[:12]})")
    return {"repos": repos, "missing": len(missing), "dirty": sum(1 for r in repos if r["dirty"]), "warnings": warnings}


def pause_missing(conn: sqlite3.Connection, *, apply: bool, now: str, exists: Callable[[str], bool] | None = None,
                  pm: PathMap | None = None) -> dict[str, Any]:
    """Agent (chưa lưu trữ, chưa paused) có `workspace_default` không tồn tại ở máy này → `paused` + lý do. Trên DB của STAGE (đã viết lại
    đường dẫn; dry-run truyền `pm`). Trả `{agent_id: {reason, workspace}}` (mọi agent thiếu, kể cả đã paused sẵn — để doctor báo)."""
    ex = exists or (lambda p: Path(p).expanduser().is_dir())
    out: dict[str, Any] = {}
    rows = conn.execute("SELECT id, workspace_default, status FROM agents WHERE archived_at IS NULL AND workspace_default IS NOT NULL").fetchall()
    for aid, ws, st in rows:
        p = _ws_path(ws)
        if p and pm is not None:  # dry-run: DB của stage CHƯA viết lại đường dẫn
            p = pm.rewrite(p, syntax="text")
        if not p or ex(p):
            continue
        out[aid] = {"reason": PAUSE_REASON, "workspace": p, "was": st}
        if apply and st != "paused":
            conn.execute("UPDATE agents SET status='paused', current_run_id=NULL, updated_at=? WHERE id=?", (now, aid))
    return out


def clone(repos: list[dict[str, Any]], *, timeout: float = CLONE_TIMEOUT_S) -> list[dict[str, Any]]:
    """`git clone` từng repo thiếu (có remote) vào đường dẫn đích rồi checkout nhánh/HEAD của nguồn. Không bao giờ vào thư mục đã có.
    Trả kết quả từng repo `{to, ok, error?}`; lỗi một repo không chặn repo khác."""
    from .workspace import GIT_ENV

    out: list[dict[str, Any]] = []
    env = {**os.environ, **GIT_ENV}
    safe = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
    for r in repos:
        to = str(r.get("to") or "")
        if r.get("exists"):
            continue
        res: dict[str, Any] = {"to": to, "remote": r.get("remote"), "ok": False}
        out.append(res)
        if not r.get("remote"):
            res["error"] = "không có remote — chép repo tay"
            continue
        dst = Path(to).expanduser()
        if not is_abs(to) or not dst.is_absolute():
            res["error"] = "đường dẫn đích không phải tuyệt đối của máy này"
            continue
        if dst.exists() or dst.is_symlink():
            res["error"] = "thư mục đích đã có — không clone đè"
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            cp = subprocess.run(["git", *safe, "clone", "--quiet", "--", str(r["remote"]), str(dst)], capture_output=True, encoding="utf-8",
                                errors="replace", timeout=timeout, env=env, **no_window_kwargs())
            if cp.returncode != 0:
                res["error"] = _err(cp)
                continue
            ref = r.get("branch") if r.get("branch") and r.get("branch") != "HEAD" else r.get("head")
            if ref:
                co = subprocess.run(["git", *safe, "-C", str(dst), "checkout", "--quiet", str(ref)], capture_output=True, encoding="utf-8",
                                    errors="replace", timeout=300, env=env, **no_window_kwargs())
                if co.returncode != 0:
                    res["warning"] = f"đã clone nhưng không checkout được {ref}: {_err(co)}"
            res["ok"] = True
        except (OSError, subprocess.TimeoutExpired) as e:
            res["error"] = str(e)[:300]
    return out


def _err(cp: subprocess.CompletedProcess) -> str:
    from .redact import redact_str

    msg = (cp.stderr or cp.stdout or f"exit {cp.returncode}").strip().splitlines()
    return redact_str(" ".join(msg[-3:]))[:300]


def unpause_present(db: Path, now: str) -> list[str]:
    """Sau `--clone-repos`: agent bị tạm dừng vì thiếu workspace mà nay thư mục đã có → bỏ paused + gỡ lý do (DB ngoại tuyến, giữ lock)."""
    c = sqlite3.connect(str(db), isolation_level=None)
    done: list[str] = []
    try:
        r = c.execute("SELECT value FROM hub_state WHERE key=?", (PAUSED_KEY,)).fetchone()
        paused = json.loads(r[0]) if r else {}
        if not isinstance(paused, dict) or not paused:
            return done
        c.execute("BEGIN IMMEDIATE")
        for aid, info in list(paused.items()):
            if Path(str((info or {}).get("workspace") or "")).expanduser().is_dir():
                if (info or {}).get("was") != "paused":
                    c.execute("UPDATE agents SET status='idle', updated_at=? WHERE id=? AND status='paused'", (now, aid))
                paused.pop(aid)
                done.append(aid)
        c.execute("INSERT INTO hub_state(key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                  "updated_at=excluded.updated_at", (PAUSED_KEY, json.dumps(paused, ensure_ascii=False), now))
        c.execute("COMMIT")
    except Exception:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise
    finally:
        c.close()
    return done


__all__ = ["SECTION", "MEMBER", "PAUSE_REASON", "PAUSED_KEY", "REPOS_KEY", "mask_remote", "git_info", "collect", "payload", "read_stage", "plan",
           "pause_missing", "clone", "clone_hint", "unpause_present"]
