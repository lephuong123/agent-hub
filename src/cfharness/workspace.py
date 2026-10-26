"""Workspace ghi thẳng vào repo thật (FR-RT-12, Q-06): khoá theo repo, checkpoint trước run, diff sau run, `run revert`.

- **Khoá** (`WorkspaceLocks`, thay bản tối thiểu `guard/locks.py` của M1-B, giữ nguyên giao diện): khoá = GỐC REPO git
  (`git rev-parse --show-toplevel`) chứa cwd; thư mục không phải git → realpath của cwd. Gắn với SESSION nên run
  `waiting_human` giữ khoá qua lần dừng tiến trình, run resume kế thừa (Q-21). Run chỉ đọc không lấy khoá
  (`RunManager._lock_key`).
- **Checkpoint** (`take_checkpoint`): repo git → `HEAD`, nhánh, danh sách file chưa commit (kể cả untracked) + bản sao
  nội dung của chúng (để phân biệt thay đổi của người với của run). KHÔNG commit, KHÔNG stash, KHÔNG đổi gì trong repo.
  Thư mục thường → danh sách file (size, mtime).
- **Diff** (`compute_diff`): file có nội dung khác trạng thái trước run = file run đã sửa (thêm/sửa/xoá); lưu bản trước/sau
  (blob theo sha256 trong `HUB_HOME/checkpoints/<run_id>/blobs/`) + patch unified (`diff.patch`).
- **Revert** (`revert`): chỉ khi MỌI file của run vẫn đúng như lúc run kết thúc; nếu có file bị sửa tiếp → `Conflict`
  liệt kê file, không ghi đè gì. Run đã tạo commit → chỉ khôi phục nội dung file, không đổi HEAD (cảnh báo).
  Thư mục không phải git → không hoàn tác được (chỉ có danh sách file).
"""

from __future__ import annotations

import difflib
import hashlib
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .errors import Conflict, HubError
from .guard.locks import RepoLocks
from .oscompat import IS_WINDOWS, no_window_kwargs

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".tox", "dist", "build"}
GIT_ENV = {"LC_ALL": "C", "LANG": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
# R2-4 (SEC): daemon chạy git NGOÀI sandbox trên repo agent ghi được → vô hiệu khoá chạy lệnh trong .git/config
GIT_SAFE_ARGS = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "core.sshCommand=", "-c", "core.pager=cat",
                 "-c", "diff.external=", "-c", "core.untrackedCache=false")


_EXTRA_ALLOWED_ROOTS: list[str] = []  # CHỈ test (conftest) đặt; sản phẩm luôn rỗng


def validate_workspace(hub: Any, path: Any, what: str = "workspace") -> None:
    """MGR-1(a): workspace `dir` phải nằm TRONG một gốc người cho phép (`workspace.allowed_roots`, mặc định ~/Workspace +
    vùng scratch của hub) VÀ không trùng/chứa/nằm trong bất kỳ vùng bảo vệ cố định hoặc protected path nào. Cho CẢ người lẫn
    manager (người muốn ngoại lệ thì thêm root qua config). Chỉ áp cho workspace kiểu thư mục thật."""
    if path in (None, "", "scratch", "worktree"):
        return
    if isinstance(path, dict):
        if path.get("kind") in ("scratch", "worktree") and not path.get("path"):
            return
        path = path.get("path")
    if not path:
        return
    from .guard.runtime import fixed_protected_dirs

    try:
        p = Path(str(path)).expanduser().resolve()
    except (OSError, RuntimeError):
        raise HubError(f"{what}: đường dẫn không hợp lệ: {path!r}")
    hh = hub.paths.home.resolve()
    uh = Path(os.path.expanduser("~")).resolve()
    # gốc cho phép: cấu hình + vùng scratch/workspace của hub + worktree của hub
    roots: list[Path] = []
    try:
        for r in hub.config.get("workspace.allowed_roots") or []:
            roots.append(Path(str(r)).expanduser().resolve())
    except KeyError:
        pass
    if not roots:
        roots = [uh / "Workspace"]
    roots += [hh / "agents", hh / "worktrees", hh / "evals", hh / "checkpoints"]
    roots += [Path(r) for r in _EXTRA_ALLOWED_ROOTS]  # test-only (conftest, cùng tiến trình); rỗng trong sản phẩm
    env_extra = os.environ.get("CFHARNESS_EXTRA_WORKSPACE_ROOTS")  # test-only (daemon tiến trình con kế thừa env)
    if env_extra:
        roots += [Path(r).expanduser().resolve() for r in env_extra.split(os.pathsep) if r]
    if not any(p == r or p.is_relative_to(r) for r in roots):
        raise HubError(f"{what} '{p}' không nằm trong gốc workspace cho phép ({', '.join(str(r) for r in roots)}). "
                       f"Người có thể thêm gốc: `cfharness config set workspace.allowed_roots '[...]'`", code=-32003)
    # không trùng/chứa/nằm trong vùng bảo vệ cố định
    socket = hub.paths.socket
    sb_cfg = {}
    try:
        sb_cfg = (hub.config.get("guard") or {}).get("sandbox") or {}
    except KeyError:
        pass
    for region in fixed_protected_dirs(hh, socket, sb_cfg):
        if "*" in region:
            import fnmatch

            if fnmatch.fnmatchcase(str(p), region) or fnmatch.fnmatchcase(str(p), region + "/*"):
                raise HubError(f"{what} '{p}' là/nằm trong vùng bảo vệ ({region})", code=-32003)
            continue
        rp = Path(region)
        if p == rp or p.is_relative_to(rp) or rp.is_relative_to(p):
            raise HubError(f"{what} '{p}' trùng/chứa/nằm trong vùng bảo vệ ({region})", code=-32003)
    # protected path do người khai báo (dotenv, ssh, repo nhánh…)
    try:
        from .guard.classifier import protected_path

        ctx = hub.guard.context_for(None, cwd=str(p) if p.is_dir() else None)
        under = ctx.policy.protected_under(str(p))
        # tài nguyên NGƯỜI khai báo nằm BÊN TRONG workspace (vd `<repo>/.next` prod) là hợp lệ — đã được chặn riêng
        r = protected_path(ctx, str(p), "write") or (under if under is not None and under.builtin else None)
        if r is not None:
            raise HubError(f"{what} '{p}' là/chứa tài nguyên được bảo vệ ({r.selector})", code=-32003)
    except HubError:
        raise
    except Exception:  # pragma: no cover
        pass


# ============================================================================ git helpers
def _git(cwd: str | Path, *args: str, check: bool = False, binary: bool = False) -> subprocess.CompletedProcess:
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", *GIT_SAFE_ARGS, "-C", str(cwd), *args], capture_output=True, timeout=60, env=env, check=check,
                          encoding=None if binary else "utf-8", errors=None if binary else "replace", **no_window_kwargs())  # git xuất UTF-8; Windows mặc định cp1252


def git_root(path: str | Path) -> str | None:
    p = Path(path).expanduser()
    if not p.is_dir():
        return None
    try:
        r = _git(p, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return os.path.realpath(r.stdout.strip())


def _head(root: str) -> tuple[str | None, str | None]:
    h = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    head = h.stdout.strip() if h.returncode == 0 and h.stdout.strip() else None
    b = _git(root, "symbolic-ref", "-q", "--short", "HEAD")
    branch = b.stdout.strip() if b.returncode == 0 and b.stdout.strip() else None
    return head, branch


def _status_paths(root: str) -> list[str]:
    """Đường dẫn (tương đối gốc repo) có thay đổi chưa commit, kể cả untracked; rename → cả tên cũ và mới."""
    r = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
    if r.returncode != 0:
        raise HubError(f"git status lỗi: {r.stderr.strip()[:300]}")
    out: list[str] = []
    for ent in r.stdout.split("\0"):
        if len(ent) > 3:
            out.append(ent[3:])
    return out


def _names_z(root: str, *args: str) -> list[str]:
    r = _git(root, *args)
    if r.returncode != 0:
        return []
    return [x for x in r.stdout.split("\0") if x]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: Path) -> bytes | None:
    try:
        if path.is_symlink():
            return ("symlink:" + os.readlink(path)).encode()
        if not path.is_file():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _git_blob(root: str, head: str | None, rel: str) -> bytes | None:
    if not head:
        return None
    r = _git(root, "cat-file", "-e", f"{head}:{rel}")
    if r.returncode != 0:
        return None
    if IS_WINDOWS:
        # Windows thường bật core.autocrlf: bản trong HEAD là LF nhưng file trong worktree là CRLF — lấy đúng byte mà
        # checkout ghi ra (`--filters` áp eol/smudge), không thì revert ghi LF và git báo file "M" dù nội dung y hệt
        b = _git(root, "cat-file", "--filters", f"{head}:{rel}", binary=True)
    else:
        b = _git(root, "show", f"{head}:{rel}", binary=True)
    return b.stdout if b.returncode == 0 else None


class BlobStore:
    def __init__(self, base: Path, max_bytes: int) -> None:
        self.base = base
        self.max_bytes = max_bytes

    def put(self, data: bytes | None) -> str | None:
        if data is None or len(data) > self.max_bytes:
            return None
        sha = _sha(data)
        p = self.base / "blobs" / sha
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return sha

    def get(self, sha: str) -> bytes | None:
        p = self.base / "blobs" / sha
        return p.read_bytes() if p.exists() else None


# ============================================================================ khoá
class WorkspaceLocks(RepoLocks):
    """Khoá ghi theo repo (FR-RT-12). Giao diện của `RepoLocks` (M1-B) giữ nguyên; chỉ đổi cách tính khoá."""

    @staticmethod
    def key_for(cwd: str | None, workspace_kind: str) -> str | None:
        if not cwd or workspace_kind != "dir":
            return None
        return git_root(cwd) or os.path.realpath(os.path.expanduser(cwd))


# ============================================================================ checkpoint / diff
def take_checkpoint(cwd: str | Path, store_dir: Path, *, max_file_bytes: int = 5_000_000, max_files: int = 20_000) -> dict[str, Any]:
    root = git_root(cwd)
    t = time.time()
    if root is None:
        files, truncated = _walk(Path(cwd).resolve(), max_files)
        return {"kind": "dir", "root": str(Path(cwd).resolve()), "files": files, "truncated": truncated, "taken_at": t}
    head, branch = _head(root)
    blobs = BlobStore(store_dir, max_file_bytes)
    dirty: dict[str, dict[str, Any]] = {}
    paths = _status_paths(root)
    for rel in paths[:max_files]:
        data = _read(Path(root) / rel)
        dirty[rel] = {"sha": _sha(data) if data is not None else None, "blob": blobs.put(data), "size": len(data) if data is not None else None}
    return {
        "kind": "git", "root": root, "head": head, "branch": branch, "dirty": dirty,
        "truncated": len(paths) > max_files, "taken_at": t,
    }


def _walk(root: Path, max_files: int) -> tuple[dict[str, list[int]], bool]:
    out: dict[str, list[int]] = {}
    truncated = False
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            p = Path(dp) / fn
            try:
                st = p.lstat()
            except OSError:
                continue
            out[str(p.relative_to(root))] = [st.st_size, st.st_mtime_ns]
            if len(out) >= max_files:
                return out, True
    return out, truncated


def compute_diff(cp: dict[str, Any], store_dir: Path, *, max_file_bytes: int = 5_000_000) -> dict[str, Any]:
    if cp.get("kind") == "dir":
        root = Path(cp["root"])
        now, truncated = _walk(root, 20_000)
        before = cp.get("files") or {}
        files = []
        for rel in sorted(set(before) | set(now)):
            b, a = before.get(rel), now.get(rel)
            if b == a:
                continue
            st = "added" if b is None else ("deleted" if a is None else "modified")
            files.append({"path": rel, "status": st, "revertible": False})
        return {"kind": "dir", "root": str(root), "files": files, "revertible": False, "truncated": truncated or cp.get("truncated", False),
                "stat": {"files": len(files)}}
    root = cp["root"]
    head_before = cp.get("head")
    head_after, branch_after = _head(root)
    blobs = BlobStore(store_dir, max_file_bytes)
    dirty = cp.get("dirty") or {}
    cand = set(dirty) | set(_status_paths(root))
    if head_before:
        cand |= set(_names_z(root, "diff", "--name-only", "-z", "--no-renames", head_before))
    else:
        cand |= set(_names_z(root, "ls-files", "-z"))
    files: list[dict[str, Any]] = []
    patch_parts: list[str] = []
    ins = dels = 0
    for rel in sorted(cand):
        if rel in dirty:
            b_sha, b_blob = dirty[rel]["sha"], dirty[rel]["blob"]
            b_data = blobs.get(b_blob) if b_blob else None
            b_known = b_sha is None or b_blob is not None
        else:
            b_data = _git_blob(root, head_before, rel)
            b_sha = _sha(b_data) if b_data is not None else None
            b_blob = blobs.put(b_data)
            b_known = b_data is None or b_blob is not None
        a_data = _read(Path(root) / rel)
        a_sha = _sha(a_data) if a_data is not None else None
        if a_sha == b_sha:
            continue
        a_blob = blobs.put(a_data)
        st = "added" if b_sha is None else ("deleted" if a_sha is None else "modified")
        files.append({"path": rel, "status": st, "before_sha": b_sha, "after_sha": a_sha, "before_blob": b_blob, "after_blob": a_blob,
                      "revertible": bool(b_known)})
        p, i, d = _unified(rel, b_data, a_data)
        patch_parts.append(p)
        ins += i
        dels += d
    patch_path = store_dir / "diff.patch"
    store_dir.mkdir(parents=True, exist_ok=True)
    patch_path.write_text("".join(patch_parts), encoding="utf-8")
    return {
        "kind": "git", "root": root, "head_before": head_before, "head_after": head_after, "branch_before": cp.get("branch"),
        "branch_after": branch_after, "commits_made": head_before != head_after, "files": files,
        "revertible": all(f["revertible"] for f in files), "patch_path": str(patch_path),
        "stat": {"files": len(files), "insertions": ins, "deletions": dels}, "truncated": cp.get("truncated", False),
    }


def _unified(rel: str, before: bytes | None, after: bytes | None) -> tuple[str, int, int]:
    def lines(b: bytes | None) -> list[str] | None:
        if b is None:
            return []
        try:
            return b.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError:
            return None

    bl, al = lines(before), lines(after)
    if bl is None or al is None:
        return f"Binary file {rel} differs\n", 0, 0
    out = list(difflib.unified_diff(bl, al, fromfile=f"a/{rel}" if before is not None else "/dev/null", tofile=f"b/{rel}" if after is not None else "/dev/null"))
    text = "".join(x if x.endswith("\n") else x + "\n\\ No newline at end of file\n" for x in out)
    i = sum(1 for x in out if x.startswith("+") and not x.startswith("+++"))
    d = sum(1 for x in out if x.startswith("-") and not x.startswith("---"))
    return text, i, d


def modified_since(diff: dict[str, Any]) -> list[str]:
    """File của run đã bị sửa tiếp sau khi run kết thúc (nội dung hiện tại ≠ lúc run xong)."""
    if diff.get("kind") != "git":
        return []
    root = Path(diff["root"])
    out = []
    for f in diff.get("files") or []:
        cur = _read(root / f["path"])
        if (_sha(cur) if cur is not None else None) != f.get("after_sha"):
            out.append(f["path"])
    return out


def resume_conflicts(run: dict[str, Any]) -> list[str]:
    """FR-RT-19 + FR-RT-12: trước khi resume run bị gián đoạn, file của run đã bị NGƯỜI sửa tiếp → không resume."""
    diff = run.get("diff")
    if not isinstance(diff, dict) or diff.get("kind") != "git":
        return []
    try:
        return modified_since(diff)
    except OSError:
        return []


def revert(diff: dict[str, Any], store_dir: Path, *, dry_run: bool = False) -> dict[str, Any]:
    if diff.get("kind") != "git":
        raise HubError("workspace không phải repo git — chỉ có danh sách file bị sửa, không hoàn tác được (FR-RT-12)")
    files = diff.get("files") or []
    if not files:
        return {"reverted": [], "warnings": ["run không sửa file nào"]}
    not_rev = [f["path"] for f in files if not f.get("revertible")]
    if not_rev:
        raise HubError(f"không đủ dữ liệu để hoàn tác (file quá lớn khi checkpoint): {', '.join(not_rev[:20])}")
    conflicts = modified_since(diff)
    if conflicts:
        raise Conflict(
            f"xung đột: {len(conflicts)} file đã bị sửa tiếp sau run — không ghi đè: {', '.join(conflicts[:20])}",
            data={"conflicts": conflicts},
        )
    blobs = BlobStore(store_dir, 1 << 62)
    root = Path(diff["root"])
    done = []
    for f in files:
        p = root / f["path"]
        if dry_run:
            done.append(f["path"])
            continue
        if f.get("before_sha") is None:
            if p.is_symlink() or p.exists():
                p.unlink()
        else:
            data = blobs.get(f["before_blob"])
            if data is None:
                raise HubError(f"thiếu bản lưu trước run của {f['path']}")
            p.parent.mkdir(parents=True, exist_ok=True)
            if data.startswith(b"symlink:") and len(data) < 4096 and f.get("status") != "added":
                # bản trước là symlink — dựng lại symlink
                try:
                    if p.is_symlink() or p.exists():
                        p.unlink()
                    os.symlink(data[len(b"symlink:"):].decode(), p)
                    done.append(f["path"])
                    continue
                except OSError:
                    pass
            p.write_bytes(data)
        done.append(f["path"])
    warnings = []
    if diff.get("commits_made"):
        warnings.append(f"run đã đổi HEAD ({(diff.get('head_before') or '')[:10]} → {(diff.get('head_after') or '')[:10]}); revert chỉ khôi phục nội dung file, KHÔNG đổi HEAD/nhánh")
    return {"reverted": done, "warnings": warnings, "dry_run": dry_run}
