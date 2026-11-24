"""Workspace ghi thẳng vào repo thật (FR-RT-12, Q-06): khoá theo repo, checkpoint trước run, diff sau run, `run revert`.

- **Khoá** (`WorkspaceLocks`, thay bản tối thiểu `guard/locks.py` của M1-B, giữ nguyên giao diện): khoá = GỐC REPO git
  (`git rev-parse --show-toplevel`) chứa cwd; thư mục không phải git → realpath của cwd. Gắn với SESSION nên run
  `waiting_human` giữ khoá qua lần dừng tiến trình, run resume kế thừa (Q-21). Run chỉ đọc không lấy khoá
  (`RunManager.lock_keys`). Luồng A workspace song song: một hàm tính khoá (`WorkspaceLocks.keys_for`) cho run lẫn task
  (`task_lock_target`); kind `worktree` → thư mục worktree của `claude --worktree`; run giữ thêm khoá mỗi `extra_write_roots`;
  khoá của session không còn run sống / `waiting_human` quá `guard.lock_waiting_ttl_s` tự gỡ; thư mục không phải git khoá cả cây con.
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
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .errors import Conflict, HubError
from .guard.locks import RepoLocks
from .oscompat import IS_WINDOWS, no_window_kwargs

log = logging.getLogger(__name__)

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".tox", "dist", "build"}
GIT_ENV = {"LC_ALL": "C", "LANG": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
# R2-4 (SEC): daemon chạy git NGOÀI sandbox trên repo agent ghi được → vô hiệu khoá chạy lệnh trong .git/config
GIT_SAFE_ARGS = ("-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "core.sshCommand=", "-c", "core.pager=cat",
                 "-c", "diff.external=", "-c", "core.untrackedCache=false")


FILEHUB_NO_REVERT = ("run trên thư mục chung (filehub) không hoàn tác ở hub — khôi phục bằng phiên bản/thùng rác trên Không gian số "
                     "(người thấy được mọi thay đổi ở đó)")

_EXTRA_ALLOWED_ROOTS: list[str] = []  # CHỈ test (conftest) đặt; sản phẩm luôn rỗng


def env_extra_roots() -> list[str]:
    """Gốc workspace phụ từ `CFHARNESS_EXTRA_WORKSPACE_ROOTS` — CHỈ khi `CFHARNESS_TESTING=1` (tests/conftest.py đặt; daemon
    tiến trình con của test kế thừa). Ngoài test biến này bị BỎ QUA: không ai nới được gốc workspace qua env (đa instance P1)."""
    if os.environ.get("CFHARNESS_TESTING") != "1":
        return []
    return [r for r in (os.environ.get("CFHARNESS_EXTRA_WORKSPACE_ROOTS") or "").split(os.pathsep) if r]


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
    if str(path).startswith("filehub:"):  # thư mục chung Không gian số → kiểm đường dẫn cục bộ đã giải
        fh = getattr(hub, "filehub", None)
        if fh is None:
            raise HubError(f"{what}: thư mục chung (filehub) chưa bật")
        if fh.lane_target(str(path)) is not None:
            return  # cổng cfharness: không bản sao — cây dựng trong vùng làm việc của agent lúc run chạy (filehub/lane/)
        fh.resolve_ref(str(path))  # luôn ném lỗi có giải thích (thư mục đồng bộ cũ cần chuyển / tên sai)
        return
    from .guard.runtime import fixed_protected_dirs

    try:
        p = Path(str(path)).expanduser().resolve()
    except (OSError, RuntimeError):
        raise HubError(f"{what}: đường dẫn không hợp lệ: {path!r}")
    hh = hub.paths.home.resolve()
    uh = Path(os.path.expanduser("~")).resolve()
    from . import confine as _confine

    conf = _confine.of_hub(hub)
    if conf is not None:  # P2: giam trong W — chỉ W + vùng làm việc của hub (bỏ qua allowed_roots và gốc test)
        croots = _confine.allowed_workspace_roots(hub, conf)
        if not any(p == r or p.is_relative_to(r) for r in croots):
            raise HubError(f"{what} '{p}' nằm ngoài workspace được giam ({conf.root}) — workspace.confine.root (chỉ người đổi)", code=-32003)
    # gốc cho phép: cấu hình + vùng scratch/workspace của hub + worktree của hub
    roots: list[Path] = []
    try:
        for r in hub.config.get("workspace.allowed_roots") or []:
            roots.append(Path(str(r)).expanduser().resolve())
    except KeyError:
        pass
    if not roots:
        roots = [uh / "Workspace"]
    if conf is not None:
        roots.append(Path(conf.root))
    roots += [hh / "agents", hh / "worktrees", hh / "evals", hh / "checkpoints"]
    roots += [Path(r) for r in _EXTRA_ALLOWED_ROOTS]  # test-only (conftest, cùng tiến trình); rỗng trong sản phẩm
    roots += [Path(r).expanduser().resolve() for r in env_extra_roots()]  # test-only (CFHARNESS_TESTING=1)
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
        log.warning("workspace: kiểm tài nguyên bảo vệ lỗi — bỏ qua kiểm này", exc_info=True)


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


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


# ============================================================================ vòng đời worktree (luồng B — docs/dev-notes/workspace-parallel.md)
WORKTREE_SLUG_MAX = 40
DEPS_MODES = ("link", "install", "none")
LOCKFILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")
LONGPATHS_WARNING = ("Windows: git chưa bật core.longpaths — file có đường dẫn dài hơn 260 ký tự trong worktree (checkout, node_modules) "
                     "sẽ lỗi. Người bật: `git config --global core.longpaths true` (hub không tự đổi cấu hình của người)")


def worktree_slug(repo_name: str, branch: str) -> str:
    """Tên thư mục worktree mặc định: `<repo>-<nhánh đã làm sạch, ≤40 ký tự>-<sha1(nhánh)[:6]>`. Hậu tố băm theo TÊN NHÁNH GỐC nên
    hai nhánh làm sạch ra cùng chuỗi (`a/b` và `a-b`, hay hai nhánh dài chung 40 ký tự đầu) vẫn ra hai thư mục khác nhau."""
    clean = "".join(c if (c.isascii() and c.isalnum()) or c in "-_." else "-" for c in branch)
    while "--" in clean:
        clean = clean.replace("--", "-")
    clean = clean.strip("-.")[:WORKTREE_SLUG_MAX].strip("-.") or "wt"
    return f"{repo_name}-{clean}-{hashlib.sha1(branch.encode('utf-8')).hexdigest()[:6]}"


def longpaths_warning(root: str | Path, *, windows: bool | None = None) -> str | None:
    """Cảnh báo (chuỗi) khi chạy trên Windows mà `core.longpaths` của repo chưa bật; None nếu không cần. CHỈ đọc cấu hình — không đổi.
    Đọc cả cấu hình system (Git for Windows hay đặt `core.longpaths` ở đó) nên bỏ `GIT_CONFIG_NOSYSTEM` cho riêng lệnh này."""
    if not (IS_WINDOWS if windows is None else windows):
        return None
    env = {**os.environ, **GIT_ENV}
    env.pop("GIT_CONFIG_NOSYSTEM", None)
    try:
        r = subprocess.run(["git", *GIT_SAFE_ARGS, "-C", str(root), "config", "--bool", "--get", "core.longpaths"], capture_output=True,
                           timeout=30, env=env, encoding="utf-8", errors="replace", **no_window_kwargs())
    except (OSError, subprocess.TimeoutExpired):
        return LONGPATHS_WARNING
    return None if r.stdout.strip() == "true" else LONGPATHS_WARNING


def _head_name(root: str) -> str | None:
    """Nhánh hiện tại của checkout `root` (HEAD tách rời → sha)."""
    head, branch = _head(root)
    return branch or head


def _is_link(p: str | Path) -> bool:
    return os.path.islink(p) or os.path.isjunction(p)


def _make_dir_link(target: Path, link: Path) -> None:
    """Liên kết thư mục `link` → `target`: Windows = junction (không cần quyền admin/Developer Mode), POSIX = symlink."""
    if IS_WINDOWS:
        try:
            import _winapi  # type: ignore[import-not-found]

            _winapi.CreateJunction(str(target), str(link))
            return
        except (ImportError, AttributeError):
            pass
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, timeout=30, encoding="utf-8",
                           errors="replace", **no_window_kwargs())
        if r.returncode != 0 or not _is_link(link):
            raise OSError(f"mklink /J thất bại: {(r.stderr or r.stdout).strip()[:200]}")
        return
    os.symlink(str(target), str(link), target_is_directory=True)


def _unlink_dir_link(p: str | Path) -> None:
    """Gỡ CHÍNH liên kết (junction/symlink), không bao giờ đi theo nó (thư mục đích giữ nguyên)."""
    p = str(p)
    if os.path.isjunction(p):
        os.rmdir(p)  # junction: rmdir gỡ điểm reparse, không đụng nội dung đích
    elif os.path.islink(p):
        try:
            os.unlink(p)
        except (IsADirectoryError, PermissionError):  # Windows: symlink thư mục gỡ bằng rmdir
            os.rmdir(p)
    if _is_link(p):
        raise OSError(f"không gỡ được liên kết {p}")


def _deps_mismatch(wt: Path, repo: Path) -> tuple[str | None, str | None]:
    """(lý do khác, lockfile) — lý do None = `package.json` và lockfile giống hệt từng byte với repo chính."""
    try:
        if (wt / "package.json").read_bytes() != (repo / "package.json").read_bytes():
            return "package.json khác repo chính", None
    except OSError:
        return "repo chính không có package.json", None
    found = None
    for name in LOCKFILES:
        a, b = wt / name, repo / name
        if a.is_file() != b.is_file():
            return f"{name} chỉ có ở một bên", None
        if a.is_file():
            try:
                if a.read_bytes() != b.read_bytes():
                    return f"{name} khác repo chính", None
            except OSError as e:
                return f"không đọc được {name}: {e}"[:200], None
            found = found or name
    if found is None:
        return "không có lockfile (package-lock.json / yarn.lock / pnpm-lock.yaml)", None
    return None, found


def _ensure_ignored(wt: Path, name: str) -> bool:
    """Liên kết `name` phải bị git bỏ qua (không thì agent `git add` nhầm nó và worktree luôn "bẩn"). Mẫu `node_modules/` không khớp
    symlink trên POSIX → thêm `/<name>` vào `info/exclude` của repo (file cục bộ, không commit). Trả True nếu đã phải thêm."""
    if _git(wt, "check-ignore", "-q", name).returncode == 0:
        return False
    common = _git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    if not common:
        return False
    ex = Path(common) / "info" / "exclude"
    line = f"/{name}"
    try:
        cur = ex.read_text(encoding="utf-8") if ex.is_file() else ""
        if line not in cur.splitlines():
            ex.parent.mkdir(parents=True, exist_ok=True)
            with ex.open("a", encoding="utf-8") as f:
                f.write(("" if not cur or cur.endswith("\n") else "\n") + line + "\n")
    except OSError:
        log.warning("worktree: không ghi được %s", ex, exc_info=True)
        return False
    return True


def link_deps(wt_path: str | Path, repo_path: str | Path, mode: str = "link") -> dict[str, Any]:
    """Phụ thuộc node của worktree mới (`workspace.deps`): `link` (mặc định) — `package.json` + lockfile giống hệt repo chính và repo
    chính đã có `node_modules` → liên kết `node_modules` sang đó (junction Windows / symlink POSIX); khác → `needs_install=true` (agent
    chạy `npm ci`). `install` → luôn `needs_install`. `none` → không làm gì. Không phải dự án node (không có package.json) → `node=false`.
    Trả dict `deps` ghi vào metadata worktree."""
    mode = mode if mode in DEPS_MODES else "link"
    wt, repo = Path(wt_path), Path(repo_path)
    out: dict[str, Any] = {"mode": mode, "node": (wt / "package.json").is_file(), "linked": False, "needs_install": False}
    if not out["node"] or mode == "none":
        return out
    if mode == "install":
        out.update(needs_install=True, reason="workspace.deps=install")
        return out
    nm, main_nm = wt / "node_modules", repo / "node_modules"
    if _is_link(nm):
        out.update(linked=True, target=os.path.realpath(nm))
        return out
    if nm.exists():
        out["reason"] = "worktree đã có node_modules riêng"
        return out
    why, lockfile = _deps_mismatch(wt, repo)
    if why is None and not main_nm.is_dir():
        why = "repo chính chưa có node_modules"
    if why is not None:
        out.update(needs_install=True, reason=why)
        return out
    target = Path(os.path.realpath(main_nm))
    try:
        _make_dir_link(target, nm)
    except OSError as e:
        out.update(needs_install=True, reason=f"không tạo được liên kết node_modules: {e}"[:200])
        return out
    out.update(linked=True, target=str(target), lockfile=lockfile)
    if _ensure_ignored(wt, "node_modules"):
        out["excluded"] = True
    return out


def deps_status(wt_path: str | Path, repo_path: str | Path, mode: str = "link") -> dict[str, Any]:
    """`deps` của worktree DÙNG LẠI (hub không tạo): chỉ đọc trạng thái, không tạo/gỡ liên kết."""
    wt = Path(wt_path)
    out: dict[str, Any] = {"mode": mode if mode in DEPS_MODES else "link", "node": (wt / "package.json").is_file(), "linked": False,
                           "needs_install": False}
    if not out["node"]:
        return out
    nm = wt / "node_modules"
    if _is_link(nm):
        out.update(linked=True, target=os.path.realpath(nm))
    elif not nm.exists() and out["mode"] != "none":
        out.update(needs_install=True, reason="worktree chưa có node_modules")
    return out


def _deps_mode(hub: Any) -> str:
    try:
        m = hub.config.get("workspace.deps") if hub is not None else None
    except KeyError:
        m = None
    return m if m in DEPS_MODES else "link"


def ref_collision(root: str, branch: str) -> str | None:
    """Tên nhánh `branch` không tạo được vì vướng ref có sẵn (git lưu ref như đường dẫn: không thể có cùng lúc `cfh/x` và `cfh/x/…`).
    Trả ref đang chắn (`refs/heads/…`) hoặc None. Đường B cũ dùng `cfh/<task id>`, luồng D dùng `cfh/<root id>/…` → kiểm tường minh
    trước khi tạo để báo lỗi rõ ràng (và luồng D chọn tiền tố khác) thay vì lỗi `cannot lock ref` của git."""
    parts = [x for x in str(branch).split("/") if x]
    for i in range(1, len(parts)):
        ref = "refs/heads/" + "/".join(parts[:i])
        if _git(root, "rev-parse", "--verify", "-q", ref).returncode == 0:
            return ref
    r = _git(root, "for-each-ref", "--count=1", "--format=%(refname)", f"refs/heads/{branch}/")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().splitlines()[0]
    return None


# Sổ worktree do HUB tạo (hub_state, chỉ `ensure_worktree_meta` ghi): {normcase(realpath): {repo, branch, base, branch_created, created_at}}.
# `owned`/`branch_created`/`branch`/`base` dùng để DỌN chỉ tin sổ này — không bao giờ tin metadata trong JSON của task (task.create
# nhận workspace từ agent, nên metadata có thể bị giả để `worktree clean` xoá nhánh chưa merge).
WORKTREE_REGISTRY_KEY = "worktrees.registry"


def _wt_key(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(str(path))))


def worktree_registry(src: Any) -> dict[str, dict[str, Any]]:
    """Đọc sổ worktree từ `Store` (state_get) hoặc kết nối sqlite (luồng phụ, chỉ đọc). Lỗi/không có → {}."""
    import json
    import sqlite3

    if src is None:
        return {}
    if isinstance(src, dict):  # sổ đã đọc sẵn
        return src
    try:
        if hasattr(src, "state_get"):
            d = src.state_get(WORKTREE_REGISTRY_KEY, {})
        else:
            r = src.execute("SELECT value FROM hub_state WHERE key=?", (WORKTREE_REGISTRY_KEY,)).fetchone()
            d = json.loads(r[0]) if r and r[0] else {}
    except (sqlite3.Error, ValueError, TypeError):
        return {}
    return {k: v for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}


def registered_worktree(src: Any, path: str | Path | None) -> dict[str, Any] | None:
    if not path:
        return None
    return worktree_registry(src).get(_wt_key(path))


def register_worktree(store: Any, meta: dict[str, Any]) -> None:
    """Ghi worktree hub vừa tạo vào sổ. Dựng lại thư mục trên nhánh có sẵn (cùng đường dẫn, cùng nhánh) giữ `branch_created` cũ."""
    if store is None or not hasattr(store, "state_set") or not meta.get("path"):
        return
    reg = worktree_registry(store)
    k = _wt_key(meta["path"])
    old = reg.get(k) or {}
    bc = bool(meta.get("branch_created")) or bool(old.get("branch") == meta.get("branch") and old.get("branch_created"))
    reg[k] = {"repo": meta.get("repo"), "path": meta.get("path"), "branch": meta.get("branch"), "base": meta.get("base"),
              "branch_created": bc, "created_at": old.get("created_at") or meta.get("created_at")}
    store.state_set(WORKTREE_REGISTRY_KEY, reg)


def forget_worktree(store: Any, path: str | Path | None) -> None:
    """Bỏ worktree đã gỡ khỏi sổ (đường dẫn có thể được dùng lại cho worktree khác)."""
    if store is None or not hasattr(store, "state_set") or not path:
        return
    reg = worktree_registry(store)
    if reg.pop(_wt_key(path), None) is not None:
        store.state_set(WORKTREE_REGISTRY_KEY, reg)


def trusted_worktree_meta(src: Any, meta: dict[str, Any] | None) -> dict[str, Any]:
    """Metadata worktree (từ JSON task) với `owned`/`branch_created`/`branch`/`base` lấy từ SỔ của hub. Không có trong sổ →
    `owned=False`, `branch_created=False` (không dọn, không xoá nhánh)."""
    m = dict(meta or {})
    ent = registered_worktree(src, m.get("path"))
    if ent is None:
        m.update(owned=False, branch_created=False)
        return m
    m.update(owned=True, branch_created=bool(ent.get("branch_created")), branch=ent.get("branch") or m.get("branch"),
             base=ent.get("base"))
    return m


def ensure_worktree_meta(hub: Any, repo: str, branch: str, base: str | None = None, path: str | None = None, *,
                         deps_mode: str | None = None) -> dict[str, Any]:
    """Git worktree riêng cho một task (để nhiều task cùng repo chạy SONG SONG: khoá repo tính theo gốc của worktree, không theo
    repo chính). Trả metadata lưu trong `workspace.worktree` của task:
    `{repo, branch, base, path, owned, created_at, branch_created, deps}` — `owned` = hub vừa tạo thư mục này (dọn được qua
    `cfharness worktree clean`), `branch_created` = hub tạo nhánh (chỉ khi đó mới `git branch -d` lúc dọn), `base` = nhánh/commit gốc
    để xét "đã merge" (mặc định nhánh hiện tại của repo chính).
    Mặc định `<thư mục cha của repo>/_wt/<worktree_slug(repo, nhánh)>`.
    Đã có thư mục: phải là worktree của CÙNG repo và đang ở `branch` thì dùng lại (`owned=false`, không đụng node_modules); khác → lỗi.
    Nhánh đã có → checkout nó (git từ chối nếu nhánh đang mở ở worktree khác); chưa có → tạo từ `base` (mặc định HEAD của repo).
    Tạo mới → `link_deps` theo `workspace.deps` (hoặc `deps_mode`)."""
    from .store import now_iso

    rp = Path(str(repo)).expanduser()
    root = git_root(rp)
    if root is None:
        raise HubError(f"worktree: '{repo}' không phải repo git")
    validate_workspace(hub, {"kind": "dir", "path": root}, "repo của worktree")
    branch = str(branch or "").strip()
    if not branch or _git(root, "check-ref-format", "--branch", branch).returncode != 0:
        raise HubError(f"worktree: tên nhánh không hợp lệ: {branch!r}")
    if path:
        wp = Path(str(path)).expanduser()
        if not wp.is_absolute():
            raise HubError(f"worktree: path phải là đường dẫn tuyệt đối: {path!r}")
    else:
        wp = Path(root).parent / "_wt" / worktree_slug(Path(root).name, branch)
    wp = wp.resolve()
    validate_workspace(hub, {"kind": "dir", "path": str(wp)}, "thư mục worktree")
    mode = deps_mode if deps_mode in DEPS_MODES else _deps_mode(hub)
    base_name = str(base).strip() if base else None
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    meta: dict[str, Any] = {"repo": root, "branch": branch, "base": base_name, "path": str(wp), "owned": False, "created_at": now_iso(),
                            "branch_created": False}
    if wp.exists():
        wroot = git_root(wp)
        wcommon = _git(wp, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip() if wroot else ""
        cur = _git(wp, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() if wroot else ""
        if wroot and _same(wroot, str(wp)) and _same(wcommon, common) and cur == branch:
            meta["base"] = base_name or _head_name(root)
            meta["deps"] = deps_status(wp, root, mode)
            return meta
        raise HubError(f"worktree: '{wp}' đã tồn tại nhưng không phải worktree của {root} trên nhánh {branch} — chọn path khác")
    if _git(root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0:
        r = _git(root, "worktree", "add", str(wp), branch)
        meta["base"] = base_name or _head_name(root)
    else:
        b = base_name or "HEAD"
        if _git(root, "rev-parse", "--verify", "-q", f"{b}^{{commit}}").returncode != 0:
            raise HubError(f"worktree: base không tồn tại: {b!r}")
        clash = ref_collision(root, branch)
        if clash is not None:
            raise HubError(f"worktree: không tạo được nhánh {branch!r} — vướng ref có sẵn {clash} (git không giữ cùng lúc `a` và `a/…`); "
                           "chọn tên nhánh khác")
        meta["base"] = base_name or _head_name(root)
        r = _git(root, "worktree", "add", "-b", branch, str(wp), b)
        meta["branch_created"] = r.returncode == 0
    if r.returncode != 0:
        raise HubError(f"worktree: git worktree add thất bại: {(r.stderr or r.stdout).strip()[:300]}")
    meta["owned"] = True
    register_worktree(getattr(hub, "store", None), meta)
    try:
        meta["deps"] = link_deps(wp, root, mode)
    except OSError as e:  # không để lỗi liên kết làm hỏng việc tạo task — agent tự cài
        log.warning("worktree: link_deps lỗi cho %s", wp, exc_info=True)
        meta["deps"] = {"mode": mode, "node": True, "linked": False, "needs_install": True, "reason": str(e)[:200]}
    return meta


def ensure_worktree(hub: Any, repo: str, branch: str, base: str | None = None, path: str | None = None) -> tuple[str, bool]:
    """Bản cũ (tương thích): `(đường dẫn, có tạo mới không)` — xem `ensure_worktree_meta`."""
    m = ensure_worktree_meta(hub, repo, branch, base, path)
    return m["path"], bool(m["owned"])


def worktree_paths(root: str) -> list[str]:
    """Mọi worktree git biết của repo (kể cả checkout chính), theo `git worktree list --porcelain`."""
    r = _git(root, "worktree", "list", "--porcelain")
    if r.returncode != 0:
        return []
    return [ln[len("worktree "):].strip() for ln in r.stdout.splitlines() if ln.startswith("worktree ")]


def branch_merged(root: str, branch: str, base: str | None) -> str | None:
    """Nhánh đã an toàn để gỡ? `base` = đã merge vào base; `upstream` = có trên nhánh upstream; `remote` = có trên một nhánh remote nào đó.
    None = chưa (hoặc nhánh không còn)."""
    if _git(root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode != 0:
        return None
    if _in_base(root, branch, base):  # base = chính nhánh → không tính (luôn "đã merge")
        return "base"
    up = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{branch}@{{upstream}}")
    if up.returncode == 0 and up.stdout.strip() \
            and _git(root, "merge-base", "--is-ancestor", f"refs/heads/{branch}", up.stdout.strip()).returncode == 0:
        return "upstream"
    rem = _git(root, "branch", "-r", "--contains", f"refs/heads/{branch}")
    if rem.returncode == 0 and rem.stdout.strip():
        return "remote"
    return None


def _base_is_branch(root: str, branch: str, base: str | None) -> bool:
    """`base` trỏ tới chính nhánh `branch` (tên trùng / ref đầy đủ trùng) → "đã merge vào base" luôn đúng, vô nghĩa."""
    if not base:
        return False
    b = str(base).strip()
    if b in (branch, f"refs/heads/{branch}", f"heads/{branch}"):
        return True
    r = _git(root, "rev-parse", "--symbolic-full-name", b)
    return r.returncode == 0 and r.stdout.strip() == f"refs/heads/{branch}"


def _in_base(root: str, branch: str, base: str | None) -> bool:
    """Kiểm lại trước `branch -D`: base hợp lệ, không phải chính nhánh, và nhánh là tổ tiên của base."""
    if not base or _base_is_branch(root, branch, base):
        return False
    if _git(root, "rev-parse", "--verify", "-q", f"{base}^{{commit}}").returncode != 0:
        return False
    return _git(root, "merge-base", "--is-ancestor", f"refs/heads/{branch}", f"{base}^{{commit}}").returncode == 0


def find_dir_links(top: str | Path) -> list[str]:
    """Mọi junction/symlink THƯ MỤC trong cây `top` (không đi vào liên kết, bỏ `.git`). `git worktree remove` xoá đệ quy và trên Windows
    coi junction là thư mục thường → sẽ xoá luôn nội dung ĐÍCH (vd node_modules của repo chính) nếu còn liên kết."""
    out: list[str] = []
    stack = [str(top)]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if e.name == ".git" and d == str(top):
                    continue
                try:
                    if e.is_junction() or e.is_symlink():
                        if os.path.isdir(e.path) or e.is_junction():
                            out.append(e.path)
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                except OSError:
                    continue
    return sorted(out)


def worktree_lock_holders(locks: Any, path: str | Path) -> list[dict[str, Any]]:
    """Người giữ khoá (`RepoLocks`) trùng/xung đột với thư mục worktree `path` — gốc worktree là khoá ghi của run trong đó.
    `locks.blockers` tự dọn khoá của session không còn run sống trước (B3), nên chỉ còn người giữ đang sống."""
    if locks is None:
        return []
    key = os.path.realpath(str(path))
    # mode="exclusive": người giữ CHIA SẺ (chế độ `files`) cũng tính — gỡ worktree cần không ai còn sống dùng nó
    return [{**h, "key": hk} for hk, h in (locks.blockers([key], None, mode="exclusive") or {}).items()]


def remove_worktree(meta: dict[str, Any], *, dry_run: bool = False, force: bool = False, locks: Any = None) -> dict[str, Any]:
    """Gỡ worktree theo metadata, an toàn với junction: (0) `locks` (khoá repo của hub) còn người giữ SỐNG trên worktree → `Conflict`
    kể cả `force` (khoá của session không còn run sống được dọn trước đó); (1) worktree còn thay đổi chưa commit → `Conflict` (trừ `force`); (2) gỡ mọi
    junction/symlink thư mục KHÔNG được git theo dõi (node_modules liên kết…) — gỡ chính liên kết, không đi theo; (3) `git worktree
    remove` (KHÔNG `--force` trừ khi người chọn `force` cho worktree bẩn — liên kết đã gỡ trước đó); (4) hub tạo nhánh (`branch_created`)
    và nhánh đã merge vào base / có trên upstream/remote → `git branch -d` (git từ chối vì base ≠ HEAD của checkout chính → `-D`, an toàn
    vì đã kiểm merge). `dry_run` → chỉ trả các bước sẽ làm."""
    repo = str(meta.get("repo") or "")
    wp = str(meta.get("path") or "")
    branch = str(meta.get("branch") or "")
    root = git_root(repo) if repo else None
    if root is None:
        raise HubError(f"worktree: repo không còn / không phải git: {repo!r}")
    if not wp or _same(wp, root):
        raise HubError("worktree: không gỡ checkout chính của repo")
    out: dict[str, Any] = {"path": wp, "repo": root, "branch": branch, "dry_run": dry_run, "links": [], "removed": False,
                           "branch_deleted": False, "actions": []}
    held = worktree_lock_holders(locks, wp)
    if held:
        h = held[0]
        raise Conflict(f"worktree {wp} đang bị khoá bởi run {h.get('run_id')} (session {h.get('session_id')}) — không gỡ khi run còn sống",
                       data={"path": wp, "holders": held[:5]})
    exists = os.path.isdir(wp)
    cur_branch = _head(wp)[1] if exists else None  # nhánh THẬT của worktree, đọc trước khi gỡ
    if exists:
        if not any(_same(wp, x) for x in worktree_paths(root)):
            raise HubError(f"worktree: '{wp}' không phải worktree của {root} — không đụng vào")
        links = find_dir_links(wp)
        tracked = set()
        if links:
            rels = [os.path.relpath(x, wp).replace(os.sep, "/") for x in links]
            tracked = set(_names_z(wp, "ls-files", "-z", "--", *rels))
            links = [x for x, rel in zip(links, rels) if rel not in tracked]
        link_rels = {os.path.relpath(x, wp).replace(os.sep, "/") for x in links}
        dirty = [p for p in _status_paths(wp) if p.rstrip("/") not in link_rels]
        out["dirty"] = dirty[:50]
        if dirty and not force:
            raise Conflict(f"worktree {wp} còn {len(dirty)} thay đổi chưa commit ({', '.join(dirty[:5])}) — commit/bỏ trước, "
                           "hoặc dọn với --force", data={"dirty": dirty[:50], "path": wp})
        out["links"] = links
        out["actions"] += [f"gỡ liên kết {x}" for x in links] + [f"git worktree remove{' --force' if dirty else ''} {wp}"]
        if not dry_run:
            for x in links:
                _unlink_dir_link(x)
            if find_dir_links(wp) and dirty:  # còn liên kết (được git theo dõi) → không bao giờ --force
                raise Conflict(f"worktree {wp} còn liên kết thư mục được git theo dõi — không gỡ bằng --force")
            r = _git(root, "worktree", "remove", *(["--force"] if dirty else []), wp)
            if r.returncode != 0:
                raise HubError(f"worktree: git worktree remove thất bại: {(r.stderr or r.stdout).strip()[:300]}")
            out["removed"] = True
    else:
        out["actions"].append("git worktree prune (thư mục không còn)")
        if not dry_run:
            _git(root, "worktree", "prune")
    if branch and meta.get("branch_created"):
        why_keep = None
        if cur_branch != branch:
            why_keep = (f"worktree đang ở nhánh {cur_branch}, không phải {branch} — giữ nhánh" if cur_branch else
                        "không xác nhận được nhánh hiện tại của worktree — giữ nhánh")
        elif _base_is_branch(root, branch, meta.get("base")):
            why_keep = f"base {meta.get('base')!r} chính là nhánh {branch} — không xét được đã merge, giữ nhánh"
        how = branch_merged(root, branch, meta.get("base")) if why_keep is None else None
        out["branch_merged"] = how
        if why_keep is not None:
            out["branch_kept"] = why_keep
        elif how is None:
            out["branch_kept"] = "nhánh chưa merge vào base / chưa có trên remote — giữ"
        else:
            out["actions"].append(f"git branch -d {branch} (đã merge: {how})")
            if not dry_run:
                r = _git(root, "branch", "-d", branch)
                # -d chỉ xét HEAD/upstream của checkout chính → -D CHỈ khi kiểm lại: nhánh nằm trong base (base ≠ nhánh)
                if r.returncode != 0 and _in_base(root, branch, meta.get("base")):
                    r = _git(root, "branch", "-D", branch)
                out["branch_deleted"] = r.returncode == 0
                if r.returncode != 0:
                    out["branch_kept"] = (r.stderr or r.stdout).strip()[:200] or "git branch -d từ chối — giữ nhánh"
    elif branch:
        out["branch_kept"] = "nhánh không do hub tạo — giữ"
    return out


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
LIVE_RUN_STATUSES = ("queued", "running", "waiting_human")


def claude_worktree_dir(repo: str, name: str) -> str:
    """Thư mục mà `claude --worktree <name>` tạo cho repo (đã kiểm trong bản CLI 2.1.3xx: `<gốc repo>/.claude/worktrees/<name>`).
    realpath — có thể chưa tồn tại (CLI tạo lúc run bắt đầu)."""
    root = git_root(repo) or os.path.realpath(os.path.expanduser(str(repo)))
    return os.path.realpath(os.path.join(root, ".claude", "worktrees", str(name)))


def is_main_checkout(path: str | Path) -> bool:
    """`path` nằm trong checkout CHÍNH của repo (`.git` ở gốc là thư mục), không phải worktree liên kết (`.git` là file)."""
    root = git_root(path)
    return root is not None and os.path.isdir(os.path.join(root, ".git"))


def task_lock_target(t: dict[str, Any]) -> dict[str, Any]:
    """Task → dạng "run" tối thiểu ({cwd, run_opts}) để tính khoá bằng CÙNG hàm với run (`RunManager.lock_keys`, B1). Ánh xạ
    như `TaskDispatcher._workspace` + `_submit_claimed`: filehub → ref (kind dir), dir/worktree → path, worktree → tên
    (`workspace.name` hoặc id task), `extra_write_roots` (≤ 5) cho dir/worktree/scratch."""
    ws = t.get("workspace") or {}
    kind = ws.get("kind") or "scratch"
    opts: dict[str, Any] = {"workspace_kind": kind}
    if kind == "filehub":
        cwd, opts["workspace_kind"] = (ws.get("ref") or ws.get("path")), "dir"
    else:
        cwd = ws.get("path") if kind in ("dir", "worktree") else None
    if kind == "worktree":
        opts["worktree"] = ws.get("name") or t.get("id")
    extra = ws.get("extra_write_roots") or []
    if extra and kind in ("dir", "worktree", "scratch"):
        opts["extra_write_roots"] = [str(x) for x in extra][:5]
    return {"cwd": cwd, "run_opts": opts, "task_id": t.get("id")}


class WorkspaceLocks(RepoLocks):
    """Khoá ghi theo repo (FR-RT-12). Giao diện của `RepoLocks` (M1-B) giữ nguyên; đổi cách tính khoá + biết run sống (B3):
    session không còn run `queued|running|waiting_human` → khoá bị gỡ; run `waiting_human` quá `guard.lock_waiting_ttl_s`
    (null = tắt) cũng mất khoá (resume sau đó xin lại khoá + kiểm `resume_conflicts`).
    Luồng E: `guard.repo_lock: files` → run xin khoá kiểu CHIA SẺ (`default_mode`), ghi từng file qua lease (`self.leases`,
    `guard/leases.py`); lease của session nhả cùng khoá repo của nó (mọi đường nhả khoá của luồng A)."""

    def __init__(self, store: Any, hub: Any = None) -> None:
        super().__init__(store)
        self.hub = hub
        self.leases: Any = None
        if hub is not None:  # thư mục chung (filehub, cổng): khoá = GỐC thư mục chung trên server, kể cả `filehub:<tên>/<con>`
            from .guard.leases import FileLeases

            self.key_for = self._key_for_hub  # type: ignore[method-assign]
            self.is_active = self._session_live
            self.on_event = self._publish
            self.leases = FileLeases(store, self)

    def mode(self) -> str:
        """`repo` | `files` | `off` theo `guard.repo_lock` (không có hub → `repo`)."""
        from .guard.locks import repo_lock_mode

        return repo_lock_mode(self.hub.config) if self.hub is not None else "repo"

    def default_mode(self) -> str:
        return "shared" if self.mode() == "files" else "exclusive"

    def _on_session_released(self, session_id: str | None, keys: list[str] | None, reason: str) -> None:
        if self.leases is None or not session_id:
            return
        try:
            self.leases.release_session(session_id, keys, reason)
        except Exception:  # best-effort: không để lỗi nhả lease làm hỏng việc nhả khoá repo (lease mồ côi bị quét theo session sống)
            log.warning("nhả lease file của session %s lỗi", session_id, exc_info=True)

    @staticmethod
    def key_for(cwd: str | None, workspace_kind: str) -> str | None:
        if not cwd or workspace_kind != "dir":
            return None
        return git_root(cwd) or os.path.realpath(os.path.expanduser(cwd))

    def keys_for(self, cwd: str | None, kind: str, *, worktree: str | None = None,
                 extra_write_roots: list[str] | tuple[str, ...] | None = None) -> list[str]:
        """HÀM DUY NHẤT tính khoá của một workspace (B1/B4): [khoá cwd, khoá mỗi extra_write_roots] (bỏ trùng, khoá cwd đứng đầu
        nếu có). Kind `worktree` (đường cũ: cwd = repo, `claude --worktree <tên>`) → khoá = thư mục worktree của CLI, KHÔNG khoá
        repo chính; thiếu tên (CLI tự đặt tên ngẫu nhiên) → không khoá cwd. `scratch` → chỉ khoá extra_write_roots."""
        keys: list[str] = []
        if cwd:
            if kind == "worktree":
                if worktree:
                    keys.append(claude_worktree_dir(str(cwd), str(worktree)))
            elif kind in ("dir", "filehub"):
                k = self.key_for(str(cwd), "dir")
                if k:
                    keys.append(k)
        for x in extra_write_roots or ():
            k = self.key_for(str(x), "dir") if x else None
            if k:
                keys.append(k)
        return list(dict.fromkeys(keys))

    def _session_live(self, session_id: str) -> bool:
        return self._session_reason(session_id) is None

    def _release_reason(self, session_id: str | None, entry: dict[str, Any]) -> str | None:
        if self.is_active != self._session_live:  # is_active được tiêm thay (test/tuỳ biến) → theo nó
            return super()._release_reason(session_id, entry)
        return self._session_reason(session_id)

    def _session_reason(self, session_id: str | None) -> str | None:
        if not session_id or self.hub is None:
            return None
        rows = self.store.conn.execute(
            f"SELECT status, ended_at, started_at, queued_at FROM runs WHERE session_id=? AND status IN ({','.join('?' * len(LIVE_RUN_STATUSES))})",
            (session_id, *LIVE_RUN_STATUSES)).fetchall()
        if not rows:
            return "no_live_run"
        if any(r[0] != "waiting_human" for r in rows):
            return None
        try:
            ttl = self.hub.config.get("guard.lock_waiting_ttl_s")
        except KeyError:
            ttl = None
        if ttl in (None, "", 0, False):
            return None
        now = time.time()
        for r in rows:
            ts = r[1] or r[2] or r[3]
            if not ts or now - _ts(ts) < float(ttl):
                return None
        return "waiting_ttl"

    def _publish(self, etype: str, payload: dict[str, Any]) -> None:
        from .events import make_event

        rid = payload.get("run_id")
        run = self.store.get_run(rid) if rid else None
        ev = make_event(etype, (run or {}).get("agent_id"), payload, run_id=rid, session_id=payload.get("session_id"))
        if run:
            ev.task_id, ev.team_id = run.get("task_id"), run.get("team_id")
        self.hub.publish(ev)

    def _key_for_hub(self, cwd: str | None, workspace_kind: str) -> str | None:
        if cwd and str(cwd).startswith("filehub:"):  # workspace_default của agent dạng filehub:<tên>[/<con>]
            try:
                lt = self.hub.filehub.lane_target(str(cwd))
            except HubError:
                return None
            if lt is None or workspace_kind != "dir" or not self.hub.config.get("filehub.lane.serialize_runs"):
                return None  # cổng cfharness: run song song (cây riêng + If-Match); chỉ khoá theo gốc khi người bật serialize_runs
            return str(lt["key"])
        return WorkspaceLocks.key_for(cwd, workspace_kind)


def _ts(iso_ts: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


# ============================================================================ checkpoint / diff
def take_checkpoint(cwd: str | Path, store_dir: Path, *, max_file_bytes: int = 5_000_000, max_files: int = 20_000,
                    force_walk: bool = False) -> dict[str, Any]:
    """`force_walk` (thư mục chung filehub): luôn chỉ danh sách file (size, mtime) — không revert ở hub, hoàn tác là việc của phiên bản/
    thùng rác trên Không gian số."""
    root = None if force_walk else git_root(cwd)
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


def _path_filter(paths: Any) -> Any:
    """`paths` (đường dẫn tương đối của các file run đã lease — luồng E/B7) → hàm lọc; None = không lọc (chế độ repo)."""
    if paths is None:
        return None
    from .guard.leases import norm_rel

    want = {norm_rel(str(p)) for p in paths}
    return lambda rel: norm_rel(str(rel)) in want


def compute_diff(cp: dict[str, Any], store_dir: Path, *, max_file_bytes: int = 5_000_000, paths: Any = None) -> dict[str, Any]:
    """`paths` (chế độ `guard.repo_lock: files`, B7): chỉ tính các file này (file run đã lease) — nhiều session cùng ghi một checkout
    nên "mọi thay đổi trong lúc run chạy" không còn là của run. None = như cũ (mọi thay đổi)."""
    keep = _path_filter(paths)
    scope = {"scope": "leases", "scoped_paths": sorted(str(p) for p in paths)[:500]} if paths is not None else {}
    if cp.get("kind") == "dir":
        root = Path(cp["root"])
        now, truncated = _walk(root, 20_000)
        before = cp.get("files") or {}
        files = []
        for rel in sorted(set(before) | set(now)):
            if keep is not None and not keep(Path(rel).as_posix()):
                continue
            b, a = before.get(rel), now.get(rel)
            if b == a:
                continue
            st = "added" if b is None else ("deleted" if a is None else "modified")
            files.append({"path": rel, "status": st, "revertible": False})
        out = {"kind": "dir", "root": str(root), "files": files, "revertible": False, "truncated": truncated or cp.get("truncated", False),
               "stat": {"files": len(files)}, **scope}
        if cp.get("filehub"):
            out["filehub"] = cp["filehub"]
        return out
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
    if keep is not None:
        cand = {c for c in cand if keep(c)}
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
    patch_path.write_text("".join(patch_parts), encoding="utf-8", newline="")  # newline="": giữ nguyên CRLF của nội dung (Windows không đổi thành CR CR LF)
    return {
        "kind": "git", "root": root, "head_before": head_before, "head_after": head_after, "branch_before": cp.get("branch"),
        "branch_after": branch_after, "commits_made": head_before != head_after, "files": files,
        "revertible": all(f["revertible"] for f in files), "patch_path": str(patch_path),
        "stat": {"files": len(files), "insertions": ins, "deletions": dels}, "truncated": cp.get("truncated", False), **scope,
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


def revert(diff: dict[str, Any], store_dir: Path, *, dry_run: bool = False, paths: Any = None,
           leased_by_others: Any = None) -> dict[str, Any]:
    """`paths` (B7): chỉ hoàn tác các file này (None = mọi file của diff). `leased_by_others` (chế độ `files`): đường dẫn tương đối
    đang bị session KHÁC còn sống giữ lease → `Conflict` liệt kê, không ghi gì."""
    if diff.get("filehub"):
        raise HubError(FILEHUB_NO_REVERT)
    if diff.get("kind") != "git":
        raise HubError("workspace không phải repo git — chỉ có danh sách file bị sửa, không hoàn tác được (FR-RT-12)")
    files = diff.get("files") or []
    keep = _path_filter(paths)
    if keep is not None:
        files = [f for f in files if keep(f["path"])]
        diff = {**diff, "files": files}
    if not files:
        return {"reverted": [], "warnings": ["run không sửa file nào"]}
    busy_keep = _path_filter(leased_by_others or [])
    busy = [f["path"] for f in files if busy_keep is not None and leased_by_others and busy_keep(f["path"])]
    if busy:
        raise Conflict(f"xung đột: {len(busy)} file đang được session khác giữ lease (đang sửa) — không hoàn tác: {', '.join(busy[:20])}",
                       data={"conflicts": busy, "reason": "leased"})
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
