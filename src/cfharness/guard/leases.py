"""Lease theo file (workspace-parallel luồng E, `guard.repo_lock: files` — docs/dev-notes/workspace-parallel.md §E).

Chế độ `files`: run giữ khoá repo kiểu CHIA SẺ (`RepoLocks`, map `repo_locks_shared`) nên nhiều session làm cùng một checkout; việc
ghi được điều phối ở đây, trong `GuardService.check` (TRƯỚC lối tắt trust — lease là điều phối tài nguyên chung, không phải quyền):

- Edit/Write/MultiEdit/NotebookEdit lên file nằm trong repo đang được giữ chia sẻ → lấy LEASE của file (bảng `file_leases`, khoá chính
  (repo, đường dẫn tương đối chuẩn hoá — không phân biệt hoa thường trên Windows) → một người giữ, nguyên tử): session khác còn sống
  giữ → deny NGAY kèm agent/run giữ; nội dung file khác sha256 đã ghi nhận lần cuối mà thay đổi không phải của session này → deny
  "đọc lại file" (ghi nhận lại hash để lần thử sau qua). `pending` = session vừa được cho ghi, PostToolUse (observe_hook) chưa về →
  hash khác thì coi là chính nó ghi (hook quan sát fail-open: mất PostToolUse chỉ làm yếu phát hiện, không chặn nhầm).
- Bash đổi trạng thái CHUNG của checkout (`repo_ops`: git checkout/switch/reset/rebase/merge/pull/stash/cherry-pick/revert/clean/am,
  `git add -A|.|-u`, `git commit -a|--all|--amend`, npm/yarn/pnpm/bun/pip/uv/poetry install|ci|add|remove…, ghi lockfile) cần quyền
  ĐỘC QUYỀN: session khác còn giữ repo (chia sẻ) hoặc giữ lease → deny + gợi ý; không ai → đặt dấu độc quyền (hàng `path=''`, hết hạn
  sau `guard.lease_exclusive_ttl_s`, nhả ở PostToolUse của Bash / hết run) — trong lúc đó Edit của session khác bị deny "repo bận".
  `git add/commit/restore/rm <đường dẫn>` → chỉ deny khi đường dẫn đang bị session KHÁC lease; `git commit` không đường dẫn (commit cả
  index dùng chung) → deny khi index đang có file session khác lease.
- Lease nhả cùng khoá repo của session (`WorkspaceLocks._on_session_released` — mọi đường A nhả khoá: hết run, stop, recovery,
  gỡ tay, quét người giữ chết). Người giữ không còn run sống bị dọn khi có người khác xin.
Đây là điều phối giữa các agent hợp tác, KHÔNG phải ranh giới an ninh (script/ngôn ngữ khác vẫn ghi được — sandbox/guard lo phần đó).
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from ..oscompat import IS_WINDOWS, no_window_kwargs
from . import shell
from .locks import norm_key, repo_lock_mode

log = logging.getLogger(__name__)

FILE_WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
EXCLUSIVE = ""  # `path` của hàng dấu độc quyền cả repo
DEFAULT_EXCLUSIVE_TTL_S = 600
HASH_MAX_BYTES = 20_000_000  # file lớn hơn → chữ ký size+mtime thay sha256

LOCKFILES = frozenset({"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock", "poetry.lock",
                       "uv.lock", "pipfile.lock", "cargo.lock", "gemfile.lock", "composer.lock", "go.sum"})

ADVICE = "work on another file, or ask the lead to isolate this task in a git worktree"


# ============================================================================ Bash: thao tác đổi trạng thái chung của repo
@dataclass
class RepoOp:
    kind: str  # exclusive | paths | index
    what: str  # mô tả ngắn (vd `git checkout`)
    cwd: str  # thư mục lệnh chạy (sau `cd`/`git -C`), tuyệt đối
    paths: tuple[str, ...] = field(default_factory=tuple)  # tuyệt đối (kind `paths`)


_WRAPPERS = frozenset({"nohup", "time", "builtin", "stdbuf", "chronic", "caffeinate", "unbuffer", "setsid", "noglob", "nocorrect", "doas", "sudo",
                       "nice", "ionice", "timeout", "gtimeout", "command", "exec", "env", "then", "else", "do", "!", "if", "elif", "while", "until"})
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_NPM_MUT = frozenset({"install", "i", "in", "ins", "inst", "insta", "instal", "isnt", "isnta", "isntal", "isntall", "add", "ci", "clean-install",
                      "ic", "install-clean", "isntall-clean", "uninstall", "un", "unlink", "remove", "rm", "r", "update", "up", "upgrade", "udpate",
                      "dedupe", "ddp", "prune", "install-test", "it", "install-ci-test", "cit"})
_YARN_MUT = frozenset({"install", "add", "remove", "upgrade", "up", "dedupe", "import", "upgrade-interactive"})
_PNPM_MUT = frozenset({"install", "i", "add", "remove", "rm", "uninstall", "un", "update", "up", "upgrade", "dedupe", "import", "prune",
                       "install-test", "it"})
_BUN_MUT = frozenset({"install", "i", "add", "a", "remove", "rm", "update", "upgrade"})
_PIP_MUT = frozenset({"install", "uninstall"})
_UV_MUT = frozenset({"add", "remove", "sync", "lock"})
_POETRY_MUT = frozenset({"add", "remove", "install", "lock", "update"})
_GIT_EXCLUSIVE = frozenset({"switch", "rebase", "merge", "pull", "cherry-pick", "revert", "am", "clean"})
_GIT_COMMIT_VALUED = frozenset("mFcCt")
_GIT_COMMIT_VALUED_LONG = frozenset({"--message", "--file", "--reuse-message", "--reedit-message", "--template", "--author", "--date",
                                     "--fixup", "--squash", "--trailer", "--cleanup", "--pathspec-from-file"})
_WRITE_CMDS = frozenset({"tee", "cp", "mv", "rm", "sed", "perl", "truncate", "install", "ln", "touch"})


def _abs(p: str, cwd: str) -> str:
    from .classifier import _abs as cabs, _real

    return os.path.normpath(_real(cabs(p, cwd)))


def _base(word: str) -> str:
    b = word.replace("\\", "/").rsplit("/", 1)[-1]
    if IS_WINDOWS:
        b = b.lower()
        for ext in (".exe", ".cmd", ".bat"):
            if b.endswith(ext):
                b = b[: -len(ext)]
    return b


def _strip_wrappers(words: list[shell.Word]) -> list[shell.Word]:
    i = 0
    while i < len(words):
        t = words[i].text
        if t in _WRAPPERS:
            i += 1
            while i < len(words) and (words[i].text.startswith("-") or "=" in words[i].text and not words[i].text.startswith("=")
                                      or (t in ("timeout", "gtimeout") and words[i].text[:1].isdigit())):
                if t in ("sudo", "doas") and words[i].text in ("-u", "-g", "-C", "-h", "-p", "-r", "-t", "-U", "-D"):
                    i += 1  # cờ có giá trị
                i += 1
            continue
        break
    return words[i:]


def repo_ops(command: str, cwd: str, *, _depth: int = 0) -> list[RepoOp]:
    """Các thao tác của lệnh Bash cần điều phối trong chế độ `files` (THUẦN, không đụng đĩa trừ realpath). Lỗi phân tích → []
    (điều phối, không phải an ninh)."""
    if not command or _depth > 3:
        return []
    res = shell.parse(shell.normalize(command))
    if res.error:
        return []
    out: list[RepoOp] = []
    cur = cwd
    for c in res.commands:
        for op, w in c.redirects:
            if op in (">", ">>", ">|", "&>", "&>>", "<>") and _base(w.literal).lower() in LOCKFILES:
                out.append(RepoOp("exclusive", f"write {_base(w.literal)}", cur))
        words = _strip_wrappers(c.words)
        if not words:
            continue
        base = _base(words[0].literal)
        args = words[1:]
        texts = [a.literal for a in args]
        if base in ("cd", "pushd"):
            if texts and not texts[0].startswith("-") and not args[0].dynamic:
                cur = _abs(texts[0], cur)
            continue
        if base in _SHELLS and "-c" in texts:
            i = texts.index("-c")
            if i + 1 < len(texts):
                out += repo_ops(texts[i + 1], cur, _depth=_depth + 1)
            continue
        if base == "git":
            out += _git_ops(args, cur)
        elif base == "npm" and _sub(texts) in _NPM_MUT and not _global(texts):
            out.append(RepoOp("exclusive", f"npm {_sub(texts)}", cur))
        elif base == "npm" and _sub(texts) == "audit" and "fix" in texts:
            out.append(RepoOp("exclusive", "npm audit fix", cur))
        elif base == "yarn" and (_sub(texts) is None or _sub(texts) in _YARN_MUT) and "global" not in texts:
            out.append(RepoOp("exclusive", f"yarn {_sub(texts) or 'install'}", cur))
        elif base == "pnpm" and _sub(texts) in _PNPM_MUT and not _global(texts):
            out.append(RepoOp("exclusive", f"pnpm {_sub(texts)}", cur))
        elif base == "bun" and _sub(texts) in _BUN_MUT and not _global(texts):
            out.append(RepoOp("exclusive", f"bun {_sub(texts)}", cur))
        elif base in ("pip", "pip3") and _sub(texts) in _PIP_MUT:
            out.append(RepoOp("exclusive", f"pip {_sub(texts)}", cur))
        elif base.startswith("python") and texts[:2] == ["-m", "pip"] and _sub(texts[2:]) in _PIP_MUT:
            out.append(RepoOp("exclusive", f"pip {_sub(texts[2:])}", cur))
        elif base == "uv" and (_sub(texts) in _UV_MUT or (_sub(texts) == "pip" and _sub(texts[1:]) in _PIP_MUT)):
            out.append(RepoOp("exclusive", f"uv {' '.join(t for t in texts[:2] if not t.startswith('-'))}", cur))
        elif base == "poetry" and _sub(texts) in _POETRY_MUT:
            out.append(RepoOp("exclusive", f"poetry {_sub(texts)}", cur))
        elif base in _WRITE_CMDS and any(_base(t).lower() in LOCKFILES for t in texts if not t.startswith("-")):
            out.append(RepoOp("exclusive", f"{base} lockfile", cur))
    for sub in res.substitutions:
        out += repo_ops(sub, cur, _depth=_depth + 1)
    return out


def _sub(texts: list[str]) -> str | None:
    return next((t for t in texts if not t.startswith("-")), None)


def _global(texts: list[str]) -> bool:
    return any(t in ("-g", "--global", "--location=global") for t in texts)


def _git_ops(args: list[shell.Word], cwd: str) -> list[RepoOp]:
    texts = [a.literal for a in args]
    i = 0
    while i < len(texts) and texts[i].startswith("-"):
        if texts[i] == "-C" and i + 1 < len(texts):
            cwd = _abs(texts[i + 1], cwd)
            i += 2
        elif texts[i] in ("-c", "--config-env", "--git-dir", "--work-tree", "--namespace", "--exec-path") and i + 1 < len(texts):
            i += 2
        else:
            i += 1
    if i >= len(texts):
        return []
    sub, rest, rw = texts[i], texts[i + 1:], args[i + 1:]
    X = lambda what=None: [RepoOp("exclusive", what or f"git {sub}", cwd)]  # noqa: E731

    def after_dd() -> list[str] | None:
        return rest[rest.index("--") + 1:] if "--" in rest else None

    def paths_op(ps: list[str], what: str) -> list[RepoOp]:
        if not ps:
            return []
        if any(p in (".", "./", ":/", ":", "*") or p.startswith(":(") for p in ps):
            return X(f"{what} {' '.join(ps[:3])}")
        return [RepoOp("paths", what, cwd, tuple(_abs(p, cwd) for p in ps))]

    if any(w.glob for w in rw if not w.literal.startswith("-")) and sub in ("add", "commit", "checkout", "restore", "reset", "rm"):
        return X(f"git {sub} <glob>")  # glob mở rộng ra file nào thì không biết trước → coi như cả repo
    if sub in _GIT_EXCLUSIVE:
        return X()
    if sub == "stash":
        return [] if rest[:1] in (["list"], ["show"]) else X()
    if sub == "checkout":
        dd = after_dd()
        if dd is not None and not [t for t in rest[: rest.index("--")] if not t.startswith("-")]:
            return paths_op(dd, "git checkout --")
        return X()
    if sub == "reset":
        dd = after_dd()
        if dd is not None and not any(t in ("--hard", "--soft", "--merge", "--keep") for t in rest):
            return paths_op(dd, "git reset --")
        return X()
    if sub in ("restore", "rm", "mv"):
        ps, skip = [], False
        for t in rest:
            if skip:
                skip = False
                continue
            if t in ("-s", "--source", "--pathspec-from-file"):
                skip = True
                continue
            if t.startswith("-") and t != "--":
                if t.startswith("--pathspec-from-file"):
                    return X(f"git {sub} --pathspec-from-file")
                continue
            if t != "--":
                ps.append(t)
        return paths_op(ps, f"git {sub}")
    if sub == "add":
        if any(t in ("-A", "--all", "-u", "--update", "--no-ignore-removal") or (t.startswith("-") and not t.startswith("--") and ("A" in t or "u" in t))
               for t in rest):
            return X(f"git add {' '.join(t for t in rest if t.startswith('-'))}")
        if any(t.startswith("--pathspec-from-file") for t in rest):
            return X("git add --pathspec-from-file")
        return paths_op([t for t in rest if not t.startswith("-")], "git add")
    if sub == "commit":
        ps: list[str] = []
        all_ = amend = False
        j = 0
        only_paths = False
        while j < len(rest):
            t = rest[j]
            if only_paths:
                ps.append(t)
            elif t == "--":
                only_paths = True
            elif t in ("--all",):
                all_ = True
            elif t == "--amend":
                amend = True
            elif t.startswith("--"):
                if t in _GIT_COMMIT_VALUED_LONG:
                    j += 1
                if t.startswith("--pathspec-from-file"):
                    return X("git commit --pathspec-from-file")
            elif t.startswith("-") and len(t) > 1:
                for k, ch in enumerate(t[1:]):
                    if ch == "a":
                        all_ = True
                    if ch in _GIT_COMMIT_VALUED:
                        if k == len(t) - 2:
                            j += 1  # giá trị là từ kế tiếp
                        break  # phần còn lại là giá trị dính liền
            else:
                ps.append(t)
            j += 1
        if all_ or amend:
            return X("git commit --amend" if amend and not all_ else "git commit -a")
        if ps:
            return paths_op(ps, "git commit")
        return [RepoOp("index", "git commit", cwd)]
    return []


# ============================================================================ lease
def norm_rel(rel: str) -> str:
    r = rel.replace("\\", "/").strip("/")
    while r.startswith("./"):
        r = r[2:]
    return r.lower() if IS_WINDOWS else r


def file_sig(path: str) -> str | None:
    """sha256 nội dung (None = file không tồn tại); file > HASH_MAX_BYTES → `size:<n>:mtime:<ns>`."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not os.path.isfile(path):
        return None
    if st.st_size > HASH_MAX_BYTES:
        return f"size:{st.st_size}:mtime:{st.st_mtime_ns}"
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _now() -> str:
    from ..store import now_iso

    return now_iso()


class FileLeases:
    """Bảng `file_leases`. `locks` = `WorkspaceLocks` (biết khoá chia sẻ đang giữ + session còn sống + phát event)."""

    def __init__(self, store: Any, locks: Any) -> None:
        self.store = store
        self.locks = locks

    @property
    def conn(self) -> Any:
        return self.store.conn

    # ------------------------------------------------------------------ tra cứu
    def locate(self, abs_path: str) -> tuple[str, str] | None:
        """(khoá repo đang được giữ CHIA SẺ chứa `abs_path`, đường dẫn tương đối) — khoá sâu nhất; None = không thuộc repo chế độ files."""
        np_ = norm_key(abs_path)
        best: str | None = None
        for k in self.locks.list_shared():
            if not os.path.isabs(k):
                continue
            nk = norm_key(k)
            base = nk if nk.endswith(os.sep) else nk + os.sep
            if (np_ == nk or np_.startswith(base)) and (best is None or len(nk) > len(norm_key(best))):
                best = k
        if best is None:
            return None
        rel = os.path.relpath(os.path.normpath(abs_path), os.path.normpath(best)).replace(os.sep, "/")
        if rel == "." or rel.startswith("../"):
            return None
        return best, rel

    def _row(self, r: Any) -> dict[str, Any]:
        return {"repo_key": r["repo_key"], "rel": r["rel"], "path": r["path"], "session_id": r["session_id"], "run_id": r["run_id"],
                "agent_id": r["agent_id"], "sha256": r["sha256"], "pending": bool(r["pending"]), "acquired_at": r["acquired_at"],
                "updated_at": r["updated_at"], "expires_at": r["expires_at"]}

    def _get(self, repo_key: str, path: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM file_leases WHERE repo=? AND path=?", (norm_key(repo_key), path)).fetchone()
        return self._row(r) if r is not None else None

    def rows(self, repo_key: str | None = None, session_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM file_leases WHERE 1=1", []
        if repo_key is not None:
            sql += " AND repo=?"
            args.append(norm_key(repo_key))
        if session_id is not None:
            sql += " AND session_id=?"
            args.append(session_id)
        return [self._row(r) for r in self.conn.execute(sql + " ORDER BY repo, path", args)]

    def list(self) -> list[dict[str, Any]]:
        return self.rows()

    def paths_for(self, session_id: str | None, repo_key: str) -> list[str]:
        """Đường dẫn tương đối (dạng gốc) các file session đã lease trong repo — phạm vi diff/revert của run (B7)."""
        if not session_id:
            return []
        return [r["rel"] for r in self.rows(repo_key, session_id) if r["path"] != EXCLUSIVE]

    def _alive(self, row: dict[str, Any]) -> bool:
        if row["path"] == EXCLUSIVE and row.get("expires_at") and float(row["expires_at"]) < time.time():
            return False
        return self.locks.session_dead(row["session_id"]) is None

    def _drop(self, row: dict[str, Any], reason: str) -> None:
        self.conn.execute("DELETE FROM file_leases WHERE repo=? AND path=? AND session_id=?", (norm_key(row["repo_key"]), row["path"], row["session_id"]))
        self._emit("lease_released", row["repo_key"], row["rel"], row["session_id"], row["run_id"], reason)

    def others(self, repo_key: str, session_id: str | None) -> list[dict[str, Any]]:
        """Lease còn hiệu lực của session KHÁC trong repo (dọn lease của người giữ đã chết / dấu độc quyền hết hạn)."""
        out = []
        for r in self.rows(repo_key):
            if r["session_id"] == session_id:
                continue
            if not self._alive(r):
                self._drop(r, "expired" if r["path"] == EXCLUSIVE else "no_live_run")
                continue
            out.append(r)
        return out

    def _emit(self, kind: str, repo_key: str, rel: str, session_id: str | None, run_id: str | None, reason: str, **extra: Any) -> None:
        if self.locks.on_event is None:
            return
        try:
            self.locks.on_event(f"workspace.{kind}", {"key": repo_key, "path": rel, "session_id": session_id, "run_id": run_id, "reason": reason,
                                                      **extra})
        except Exception:  # best-effort: event chỉ để quan sát, không làm hỏng lease
            log.debug("phát event %s lỗi", kind, exc_info=True)

    # ------------------------------------------------------------------ lấy / làm mới
    def acquire(self, repo_key: str, rel: str, abs_path: str, session_id: str, run_id: str | None, agent_id: str | None) -> dict[str, Any]:
        """{status: ok | held | changed | busy, …}. ok = session giữ lease (mới hoặc đã có) và được ghi."""
        path = norm_rel(rel)
        for o in self.others(repo_key, session_id):
            if o["path"] == EXCLUSIVE:
                return {"status": "busy", "holder": o}
        cur = file_sig(abs_path)
        now = _now()
        rk = norm_key(repo_key)
        fresh = self.conn.execute(
            "INSERT OR IGNORE INTO file_leases (repo, path, rel, repo_key, session_id, run_id, agent_id, sha256, pending, acquired_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,1,?,?)", (rk, path, rel, repo_key, session_id, run_id, agent_id, cur, now, now)).rowcount == 1
        row = self._get(repo_key, path)
        if row is None:  # pragma: no cover - vừa INSERT
            raise RuntimeError("file_leases: không đọc lại được lease vừa ghi")
        if row["session_id"] != session_id:
            if self._alive(row):
                return {"status": "held", "holder": row}
            self._drop(row, "no_live_run")
            return self.acquire(repo_key, rel, abs_path, session_id, run_id, agent_id)
        if fresh:
            self._emit("lease_acquired", repo_key, rel, session_id, run_id, "edit")
            return {"status": "ok", "lease": row}
        if row["sha256"] != cur and not row["pending"]:
            self.conn.execute("UPDATE file_leases SET sha256=?, updated_at=?, run_id=? WHERE repo=? AND path=?", (cur, now, run_id, rk, path))
            return {"status": "changed", "lease": row, "sha256": cur}
        self.conn.execute("UPDATE file_leases SET pending=1, run_id=?, agent_id=COALESCE(?, agent_id), updated_at=? WHERE repo=? AND path=?",
                          (run_id, agent_id, now, rk, path))
        return {"status": "ok", "lease": row}

    def after_write(self, session_id: str | None, abs_path: str) -> bool:
        """PostToolUse của Edit/Write…: ghi nhận hash mới của file session giữ, bỏ `pending`. True nếu có lease được cập nhật."""
        loc = self.locate(abs_path)
        if loc is None or not session_id:
            return False
        row = self._get(loc[0], norm_rel(loc[1]))
        if row is None or row["session_id"] != session_id:
            return False
        self.conn.execute("UPDATE file_leases SET sha256=?, pending=0, updated_at=? WHERE repo=? AND path=?",
                          (file_sig(abs_path), _now(), norm_key(loc[0]), row["path"]))
        return True

    # ------------------------------------------------------------------ độc quyền (Bash)
    def exclusive_blockers(self, repo_key: str, session_id: str | None) -> list[dict[str, Any]]:
        """Ai (session khác còn sống) đang làm trong repo: người giữ chia sẻ/độc quyền khoá repo + lease file + dấu độc quyền."""
        out: dict[str, dict[str, Any]] = {}
        for sid, h in self.locks.shared_holders(repo_key).items():
            if sid != session_id and self.locks.session_dead(sid) is None:
                out.setdefault(sid, {"session_id": sid, "run_id": h.get("run_id"), "files": []})
        h = self.locks.holder(repo_key)
        if h and h.get("session_id") != session_id and self.locks.session_dead(h.get("session_id")) is None:
            out.setdefault(str(h.get("session_id")), {"session_id": h.get("session_id"), "run_id": h.get("run_id"), "files": []})
        for r in self.others(repo_key, session_id):
            e = out.setdefault(r["session_id"], {"session_id": r["session_id"], "run_id": r["run_id"], "files": []})
            e["agent_id"] = e.get("agent_id") or r["agent_id"]
            if r["path"] != EXCLUSIVE:
                e["files"].append(r["rel"])
            else:
                e["exclusive"] = True
        return list(out.values())

    def grant_exclusive(self, repo_key: str, session_id: str, run_id: str | None, agent_id: str | None, ttl_s: float, what: str) -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO file_leases (repo, path, rel, repo_key, session_id, run_id, agent_id, sha256, pending, acquired_at, updated_at, expires_at) "
            "VALUES (?,?,?,?,?,?,?,?,0,?,?,?) ON CONFLICT(repo, path) DO UPDATE SET session_id=excluded.session_id, run_id=excluded.run_id, "
            "agent_id=excluded.agent_id, rel=excluded.rel, updated_at=excluded.updated_at, expires_at=excluded.expires_at",
            (norm_key(repo_key), EXCLUSIVE, what[:200], repo_key, session_id, run_id, agent_id, None, now, now, time.time() + float(ttl_s)))
        self._emit("lease_acquired", repo_key, "", session_id, run_id, "exclusive", what=what[:200])

    def release_exclusive(self, session_id: str | None) -> int:
        if not session_id:
            return 0
        rows = [r for r in self.rows(session_id=session_id) if r["path"] == EXCLUSIVE]
        for r in rows:
            self._drop(r, "tool_done")
        return len(rows)

    # ------------------------------------------------------------------ nhả
    def release_session(self, session_id: str | None, repo_keys: list[str] | None = None, reason: str = "run_ended") -> int:
        if not session_id:
            return 0
        rows = self.rows(session_id=session_id)
        if repo_keys is not None:
            want = {norm_key(k) for k in repo_keys if k}
            rows = [r for r in rows if norm_key(r["repo_key"]) in want]
        for r in rows:
            self._drop(r, reason)
        return len(rows)

    def sweep(self) -> int:
        n = 0
        for r in self.rows():
            if not self._alive(r):
                self._drop(r, "expired" if r["path"] == EXCLUSIVE else "no_live_run")
                n += 1
        return n


def staged_paths(root: str) -> list[str]:
    """File đang có trong index (so với HEAD) của checkout `root` — tương đối, dạng git. Lỗi git → [] (điều phối, không chặn)."""
    from ..workspace import GIT_ENV, GIT_SAFE_ARGS

    try:
        r = subprocess.run(["git", *GIT_SAFE_ARGS, "-C", root, "diff", "--cached", "--name-only", "-z", "--no-renames"], capture_output=True,
                           timeout=10, env={**os.environ, **GIT_ENV}, encoding="utf-8", errors="replace", **no_window_kwargs())
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [x for x in r.stdout.split("\0") if x] if r.returncode == 0 else []


# ============================================================================ kiểm trong GuardService.check
def _who(rows: list[dict[str, Any]], store: Any) -> str:
    parts = []
    for e in rows[:4]:
        agent = e.get("agent_id")
        if not agent and e.get("run_id"):
            run = store.get_run(e["run_id"])
            agent = (run or {}).get("agent_id")
        files = e.get("files") or []
        parts.append(f"agent {agent or '?'} (run {e.get('run_id') or '?'})" + (f" editing {', '.join(files[:3])}" if files else ""))
    return "; ".join(parts) + (f" and {len(rows) - 4} more" if len(rows) > 4 else "")


def check(hub: Any, run: dict[str, Any], tool_name: str, tool_input: dict[str, Any], cwd: str) -> dict[str, Any] | None:
    """None = không liên quan / được phép (lease đã lấy); dict {rule, reason, message, key, path, holder} = deny. CHỈ khi
    `guard.repo_lock: files`. Lỗi ném ra → GuardService/hook từ chối (fail-closed)."""
    if repo_lock_mode(hub.config) != "files":
        return None
    locks = hub.runs.locks
    leases: FileLeases | None = getattr(locks, "leases", None)
    if leases is None:
        return None
    sid, rid, agent = run.get("session_id"), run.get("id"), run.get("agent_id")
    if not sid:
        return None
    if tool_name in FILE_WRITE_TOOLS:
        raw = (tool_input or {}).get(FILE_WRITE_TOOLS[tool_name])
        if not raw or not isinstance(raw, str):
            return None
        ap = _abs(raw, cwd)
        loc = leases.locate(ap)
        if loc is None:
            return None
        repo_key, rel = loc
        res = leases.acquire(repo_key, rel, ap, sid, rid, agent)
        if res["status"] == "ok":
            return None
        h = res.get("holder") or {}
        who = _who([h], hub.store) if h else ""
        if res["status"] == "held":
            return {"rule": "file_lease_held", "key": repo_key, "path": rel, "holder": h,
                    "reason": f"file {rel} đang được session khác giữ lease ({who})",
                    "message": f"[cfharness] File lease: {rel} is being edited by another agent ({who}). Do not edit it now and do not work "
                               f"around this with Bash: {ADVICE}."}
        if res["status"] == "busy":
            return {"rule": "repo_exclusive_busy", "key": repo_key, "path": rel, "holder": h,
                    "reason": f"repo đang chạy thao tác độc quyền của session khác ({h.get('rel')}; {who})",
                    "message": f"[cfharness] The repository is busy: another agent ({who}) is running `{h.get('rel')}`, which changes the "
                               "whole checkout. Retry this edit in a minute."}
        return {"rule": "file_changed_externally", "key": repo_key, "path": rel, "holder": None,
                "reason": f"file {rel} bị sửa ngoài lease của session (nội dung khác lần đọc/ghi cuối)",
                "message": f"[cfharness] File lease: {rel} was changed by someone else since you last read or wrote it. Read the file again, "
                           "then redo your edit on the current content."}
    if tool_name != "Bash":
        return None
    cmd = (tool_input or {}).get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    for op in repo_ops(cmd, cwd):
        loc = leases.locate(op.cwd)
        repo_key = loc[0] if loc else next((k for k in leases.locks.list_shared() if norm_key(k) == norm_key(op.cwd)), None)
        if repo_key is None:
            continue
        if op.kind == "exclusive":
            bl = leases.exclusive_blockers(repo_key, sid)
            if bl:
                who = _who(bl, hub.store)
                return {"rule": "repo_exclusive_needed", "key": repo_key, "path": None, "holder": bl[0],
                        "reason": f"`{op.what}` đổi trạng thái chung của repo trong khi session khác đang làm ({who})",
                        "message": f"[cfharness] `{op.what}` changes the shared checkout, but other agents are working in it ({who}). Do not "
                                   "run it now. Stage and commit only your own files by path (`git add <your files>`, "
                                   f"`git commit -m <msg> -- <your files>`), or {ADVICE}."}
            ttl = hub.config.get("guard.lease_exclusive_ttl_s") if _has(hub.config, "guard.lease_exclusive_ttl_s") else None
            leases.grant_exclusive(repo_key, sid, rid, agent, float(ttl or DEFAULT_EXCLUSIVE_TTL_S), op.what)
            continue
        if op.kind == "paths":
            others = [r for r in leases.others(repo_key, sid) if r["path"] != EXCLUSIVE]
            hits = []
            for p in op.paths:
                pl = leases.locate(p)
                if pl is None or norm_key(pl[0]) != norm_key(repo_key):
                    if norm_key(p) == norm_key(repo_key):
                        hits += others  # đường dẫn = gốc repo
                    continue
                pr = norm_rel(pl[1])
                hits += [r for r in others if r["path"] == pr or r["path"].startswith(pr + "/")]
        else:  # index: commit cả index dùng chung
            others = [r for r in leases.others(repo_key, sid) if r["path"] != EXCLUSIVE]
            staged = {norm_rel(x) for x in staged_paths(repo_key)} if others else set()
            hits = [r for r in others if r["path"] in staged]
        if hits:
            files = sorted({r["rel"] for r in hits})
            who = _who([{"agent_id": r["agent_id"], "run_id": r["run_id"], "files": [r["rel"]]} for r in hits], hub.store)
            extra = (" The index already contains files staged by another agent: commit only your files with "
                     "`git commit -m <msg> -- <your files>`." if op.kind == "index" else " Only stage, commit or restore your own files.")
            return {"rule": "file_lease_held", "key": repo_key, "path": files[0], "holder": hits[0],
                    "reason": f"`{op.what}` chạm file session khác đang giữ lease: {', '.join(files[:5])}",
                    "message": f"[cfharness] `{op.what}` touches files other agents are editing ({who}).{extra}"}
    return None


def _has(config: Any, key: str) -> bool:
    try:
        config.get(key)
        return True
    except KeyError:
        return False


def observe(hub: Any, run: dict[str, Any], event: str, data: dict[str, Any]) -> None:
    """PostToolUse (observe_hook, fail-open): làm mới hash file vừa ghi / nhả dấu độc quyền sau Bash. Chỉ chế độ `files`."""
    if event != "PostToolUse" or repo_lock_mode(hub.config) != "files":
        return
    leases: FileLeases | None = getattr(hub.runs.locks, "leases", None)
    if leases is None:
        return
    sid = run.get("session_id")
    tool = data.get("tool_name")
    if tool == "Bash":
        leases.release_exclusive(sid)
    elif tool in FILE_WRITE_TOOLS and isinstance(data.get("file_path"), str):
        cwd = data.get("cwd") if isinstance(data.get("cwd"), str) and data.get("cwd") else (run.get("cwd") or os.getcwd())
        leases.after_write(sid, _abs(data["file_path"], cwd))
