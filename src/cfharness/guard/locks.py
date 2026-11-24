"""Khoá ghi theo thư mục làm việc (bản M1 tối thiểu; M3 đổi cách tính khoá ở `workspace.WorkspaceLocks`).

Giao diện (giữ nguyên từ M1; luồng A `workspace-parallel` thêm đa khoá + dọn người giữ chết):
- `key_for(cwd, workspace_kind) -> str | None` : khoá cho run; None = không cần khoá (scratch riêng của run).
- `holder(key) -> dict | None`                 : ai đang giữ ĐỘC QUYỀN ({session_id, run_id, since}).
- `blocked_for(key, session_id) -> dict | None`: người giữ (session khác) chặn `key` — kể cả khoá cha/con xung đột (B5).
- `try_acquire(key, session_id, run_id) -> bool`: cùng session (chuỗi resume) được kế thừa khoá.
- `try_acquire_many(keys, session_id, run_id)` : lấy NHIỀU khoá nguyên tử (tất cả hoặc không) — run giữ khoá cwd + khoá
  mỗi `extra_write_roots` (B4).
- `release(key, session_id)` / `release_many(keys, session_id)` / `release_session(session_id)`: nhả nếu đúng người giữ.
- `force_release(key, reason)`                  : gỡ khoá bất kể người giữ (RPC `workspace.unlock`, chỉ người).
Khoá gắn với SESSION (không phải run) để run `waiting_human` giữ khoá qua lần dừng tiến trình và run resume kế thừa
(Q-21). Lưu trong `hub_state` (sống qua restart daemon).

Dọn người giữ chết (B3): `is_active(session_id) -> bool` (tiêm vào, như `BrowserLock`) — session không còn run sống thì khoá
của nó bị gỡ ở `try_acquire*`/`blocked_for`/`sweep` (event `workspace.lock_released`, reason `no_live_run`).
Xung đột (B5): hai khoá xung đột khi trùng nhau, hoặc khi một khoá là thư mục KHÔNG phải git (khoá cả cây con) và chứa khoá
kia. So trên đường dẫn đã chuẩn hoá (`normcase` — không phân biệt hoa thường trên Windows). Khoá gốc repo git KHÔNG khoá
worktree/repo lồng bên trong nó (worktree có khoá riêng).
Event (`on_event(type, payload)`): `workspace.lock_acquired|lock_released|lock_blocked`, payload {key, session_id, run_id, reason}.

Chế độ khoá (luồng E, `guard.repo_lock` ∈ `repo|files|off` — đọc qua `repo_lock_mode`): `repo` (mặc định) = khoá ĐỘC QUYỀN như trên
(map `repo_locks`). `files` = run giữ khoá repo kiểu CHIA SẺ (map `repo_locks_shared`: {khoá: {session: người giữ}}) — nhiều session
cùng giữ; người giữ độc quyền chặn người xin chia sẻ và ngược lại. Ghi từng file được điều phối bằng lease (`guard/leases.py`).
`mode=None` ở `blockers`/`try_acquire*` = `default_mode()` (lớp con đọc cấu hình) → dispatcher/RunManager không phải biết chế độ.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Iterable

STATE_KEY = "repo_locks"
SHARED_STATE_KEY = "repo_locks_shared"
REPO_LOCK_MODES = ("repo", "files", "off")


def normalize_repo_lock(value: Any) -> str:
    """Giá trị `guard.repo_lock` → `repo|files|off`. `true`/None (thiếu) = `repo` (hành vi cũ), `false` = `off`; chuỗi lạ → `repo`
    (an toàn: khoá độc quyền)."""
    if value is None or value is True:
        return "repo"
    if value is False:
        return "off"
    v = str(value).strip().lower()
    if v in ("false", "0", "no"):
        return "off"
    if v in ("true", "1", "yes"):
        return "repo"
    return v if v in REPO_LOCK_MODES else "repo"


def repo_lock_mode(config: Any) -> str:
    """HÀM DUY NHẤT đọc `guard.repo_lock` (mọi nơi đọc khoá này đi qua đây): `repo` | `files` | `off`."""
    try:
        return normalize_repo_lock(config.get("guard.repo_lock"))
    except KeyError:
        return "repo"


def validate_repo_lock(value: Any) -> None:
    if isinstance(value, bool) or value in REPO_LOCK_MODES:
        return
    raise ValueError("guard.repo_lock phải là repo | files | off (true = repo, false = off)")


def norm_key(key: str) -> str:
    """Dạng so sánh của khoá: đường dẫn → normpath + normcase (Windows: chữ thường, dấu `\\`); khoá khác (filehub) giữ nguyên."""
    if os.path.isabs(key):
        return os.path.normcase(os.path.normpath(key))
    return key


def _contains(parent: str, child: str) -> bool:
    """`child` nằm hẳn trong `parent` (cả hai đã `norm_key`, là đường dẫn tuyệt đối)."""
    if parent == child or not (os.path.isabs(parent) and os.path.isabs(child)):
        return False
    base = parent if parent.endswith(os.sep) else parent + os.sep
    return child.startswith(base)


def is_tree_key(key: str) -> bool:
    """Khoá thư mục KHÔNG phải gốc repo git → khoá cả cây con (B5). Gốc repo/worktree có `.git` (thư mục hoặc file).
    Khoá không phải đường dẫn (filehub) → False."""
    if not os.path.isabs(key):
        return False
    return not os.path.exists(os.path.join(key, ".git"))


class RepoLocks:
    def __init__(self, store: Any, is_active: Callable[[str], bool] | None = None,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.store = store
        self.is_active = is_active
        self.on_event = on_event
        self._blocked_seen: set[tuple[str, str, str]] = set()  # (session, khoá, session giữ) đã báo lock_blocked — không spam mỗi tick

    def _all(self) -> dict[str, dict[str, Any]]:
        d = self.store.state_get(STATE_KEY, {}) or {}
        return dict(d) if isinstance(d, dict) else {}

    def _save(self, d: dict[str, dict[str, Any]]) -> None:
        self.store.state_set(STATE_KEY, d)

    def _shared(self) -> dict[str, dict[str, dict[str, Any]]]:
        d = self.store.state_get(SHARED_STATE_KEY, {}) or {}
        if not isinstance(d, dict):
            return {}
        return {k: dict(v) for k, v in d.items() if isinstance(v, dict) and v}

    def _save_shared(self, d: dict[str, dict[str, dict[str, Any]]]) -> None:
        self.store.state_set(SHARED_STATE_KEY, {k: v for k, v in d.items() if v})

    def default_mode(self) -> str:
        """`exclusive` | `shared` — chế độ xin khoá khi không nói rõ. Lớp gốc: luôn độc quyền (hành vi cũ)."""
        return "exclusive"

    def _on_session_released(self, session_id: str | None, keys: list[str] | None, reason: str) -> None:
        """Móc cho lớp con: khoá repo của session vừa được nhả (`keys` None = mọi khoá) → nhả lease file của nó."""

    def _emit(self, kind: str, key: str, session_id: str | None, run_id: str | None, reason: str, **extra: Any) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(f"workspace.{kind}", {"key": key, "session_id": session_id, "run_id": run_id, "reason": reason, **extra})
        except Exception:  # pragma: no cover - phát event lỗi không làm hỏng khoá  # best-effort: event chỉ để quan sát
            pass

    @staticmethod
    def key_for(cwd: str | None, workspace_kind: str) -> str | None:
        if not cwd or workspace_kind != "dir":
            return None
        return os.path.realpath(os.path.expanduser(cwd))

    # ------------------------------------------------------------------ xung đột / người giữ chết
    @staticmethod
    def _tree(key: str, entry: dict[str, Any] | None = None) -> bool:
        if entry is not None and "tree" in entry:
            return bool(entry["tree"])
        return is_tree_key(key)

    def conflicts(self, a: str, b: str, *, a_tree: bool | None = None, b_tree: bool | None = None) -> bool:
        """Hai khoá xung đột: trùng (đã chuẩn hoá), hoặc khoá thư mục không phải git chứa khoá kia (B5)."""
        na, nb = norm_key(a), norm_key(b)
        if na == nb:
            return True
        if _contains(na, nb):
            return self._tree(a) if a_tree is None else a_tree
        if _contains(nb, na):
            return self._tree(b) if b_tree is None else b_tree
        return False

    def _release_reason(self, session_id: str | None, entry: dict[str, Any]) -> str | None:
        """None = người giữ còn sống; khác None = lý do gỡ khoá của người giữ đã chết."""
        if self.is_active is None or not session_id:
            return None
        return None if self.is_active(session_id) else "no_live_run"

    def session_dead(self, session_id: str | None) -> str | None:
        """Lý do session không còn được coi là người giữ (None = còn sống / không biết). Lỗi tra → còn sống (không gỡ nhầm)."""
        try:
            return self._release_reason(session_id, {})
        except Exception:  # fail-closed: không xác định được → coi như còn giữ
            return None

    def sweep(self) -> list[str]:
        """Gỡ khoá (độc quyền lẫn chia sẻ) của session không còn run sống (B3). Trả danh sách khoá đã gỡ."""
        if self.is_active is None:
            return []
        d = self._all()
        sh = self._shared()
        cache: dict[Any, str | None] = {}

        def reason(sid: Any, h: dict[str, Any]) -> str | None:
            if sid not in cache:
                try:
                    cache[sid] = self._release_reason(sid, h)
                except Exception:  # pragma: no cover  # fail-closed: lỗi tra DB → coi như còn sống (không gỡ nhầm)
                    cache[sid] = None
            return cache[sid]

        gone: list[tuple[str, dict[str, Any], str]] = []
        for k, h in d.items():
            r = reason(h.get("session_id"), h)
            if r:
                gone.append((k, h, str(r)))
        gone_sh: list[tuple[str, dict[str, Any], str]] = []
        for k, holders in sh.items():
            for sid, h in holders.items():
                r = reason(sid, h)
                if r:
                    gone_sh.append((k, {**h, "session_id": sid}, str(r)))
        if not gone and not gone_sh:
            return []
        if gone:
            for k, _h, _r in gone:
                d.pop(k, None)
            self._save(d)
        if gone_sh:
            for k, h, _r in gone_sh:
                sh.get(k, {}).pop(h["session_id"], None)
            self._save_shared(sh)
        for k, h, r in gone + gone_sh:
            self._emit("lock_released", k, h.get("session_id"), h.get("run_id"), r)
        for sid in dict.fromkeys(h.get("session_id") for _k, h, _r in gone + gone_sh):
            self._on_session_released(sid, None, str(cache.get(sid) or "no_live_run"))
        return [k for k, _h, _r in gone + gone_sh]

    def holder(self, key: str) -> dict[str, Any] | None:
        """Người giữ ĐỘC QUYỀN của `key` (None nếu không ai giữ / chỉ có người giữ chia sẻ — xem `shared_holders`)."""
        d = self._all()
        if key in d:
            return d[key]
        nk = norm_key(key)
        return next((h for k, h in d.items() if norm_key(k) == nk), None)

    def shared_holders(self, key: str) -> dict[str, dict[str, Any]]:
        """{session_id: người giữ} đang giữ `key` kiểu chia sẻ (chế độ `files`)."""
        nk = norm_key(key)
        out: dict[str, dict[str, Any]] = {}
        for k, holders in self._shared().items():
            if norm_key(k) == nk:
                out.update(holders)
        return out

    def list_shared(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Khoá CHIA SẺ đang giữ {khoá: {session_id: người giữ}}."""
        return self._shared()

    def blockers(self, keys: Iterable[str | None], session_id: str | None, *, sweep: bool = True,
                 mode: str | None = None) -> dict[str, dict[str, Any]]:
        """Khoá đang giữ (của session KHÁC) xung đột với một trong `keys` → {khoá đang giữ: người giữ + `wanted` + `mode`}.
        Người giữ độc quyền luôn chặn; người giữ chia sẻ chỉ chặn khi xin `exclusive` (`mode` None = `default_mode()`)."""
        want = [k for k in dict.fromkeys(keys) if k]
        if not want:
            return {}
        mode = mode or self.default_mode()
        if sweep:
            self.sweep()
        out: dict[str, dict[str, Any]] = {}
        for hk, h in self._all().items():
            if h.get("session_id") == session_id:
                continue
            for k in want:
                if self.conflicts(k, hk, b_tree=self._tree(hk, h)):
                    out[hk] = {**h, "wanted": k, "mode": "exclusive"}
                    break
        if mode == "exclusive":
            for hk, holders in self._shared().items():
                if hk in out:
                    continue
                for sid, h in holders.items():
                    if sid == session_id:
                        continue
                    hit = next((k for k in want if self.conflicts(k, hk, b_tree=self._tree(hk, h))), None)
                    if hit is not None:
                        out[hk] = {**h, "session_id": sid, "wanted": hit, "mode": "shared"}
                        break
        return out

    def blocked_for(self, key: str | None, session_id: str | None, mode: str | None = None) -> dict[str, Any] | None:
        if key is None:
            return None
        b = self.blockers([key], session_id, mode=mode)
        if not b:
            return None
        hk, h = next(iter(b.items()))
        return {**h, "key": hk}

    def try_acquire(self, key: str | None, session_id: str, run_id: str, mode: str | None = None) -> bool:
        return self.try_acquire_many([key], session_id, run_id, mode=mode)

    def try_acquire_many(self, keys: Iterable[str | None], session_id: str, run_id: str, mode: str | None = None) -> bool:
        """Lấy MỌI khoá trong `keys` cho session, nguyên tử: một khoá bị session khác giữ (hoặc xung đột cha/con) → không lấy
        khoá nào. Cùng session → kế thừa (giữ `since`). `mode` `shared` (chế độ `files`): nhiều session cùng giữ, chỉ người giữ
        độc quyền chặn; `exclusive`: người giữ chia sẻ cũng chặn. None = `default_mode()`."""
        want = [k for k in dict.fromkeys(keys) if k]
        if not want:
            return True
        mode = mode or self.default_mode()
        b = self.blockers(want, session_id, mode=mode)
        if b:
            for hk, h in b.items():
                wanted = h.get("wanted") or hk
                sig = (session_id, wanted, str(h.get("session_id")))
                if sig not in self._blocked_seen:
                    self._blocked_seen.add(sig)
                    self._emit("lock_blocked", wanted, session_id, run_id, "held", holder_key=hk,
                               holder_session_id=h.get("session_id"), holder_run_id=h.get("run_id"))
            return False
        if mode == "shared":
            return self._acquire_shared(want, session_id, run_id)
        d = self._all()
        idx = {norm_key(k): k for k in d}
        fresh: list[str] = []
        now = time.time()
        for k in want:
            cur = idx.get(norm_key(k), k)
            h = d.pop(cur, None)
            if h is None:
                fresh.append(k)
            d[k] = {"session_id": session_id, "run_id": run_id, "since": (h or {}).get("since") or now, "tree": is_tree_key(k)}
        self._save(d)
        if self._blocked_seen:
            self._blocked_seen = {s for s in self._blocked_seen if s[0] != session_id}
        for k in fresh:
            self._emit("lock_acquired", k, session_id, run_id, "acquire")
        return True

    def _acquire_shared(self, want: list[str], session_id: str, run_id: str) -> bool:
        sh = self._shared()
        idx = {norm_key(k): k for k in sh}
        fresh: list[str] = []
        now = time.time()
        for k in want:
            cur = idx.get(norm_key(k), k)
            holders = sh.setdefault(cur, {})
            h = holders.get(session_id)
            if h is None:
                fresh.append(cur)
            holders[session_id] = {"run_id": run_id, "since": (h or {}).get("since") or now, "tree": is_tree_key(k)}
        self._save_shared(sh)
        if self._blocked_seen:
            self._blocked_seen = {s for s in self._blocked_seen if s[0] != session_id}
        for k in fresh:
            self._emit("lock_acquired", k, session_id, run_id, "acquire", mode="shared")
        return True

    def release(self, key: str | None, session_id: str | None, reason: str = "run_ended") -> None:
        self.release_many([key], session_id, reason)

    def release_many(self, keys: Iterable[str | None], session_id: str | None, reason: str = "run_ended") -> None:
        keys = [k for k in keys if k]
        want = {norm_key(k) for k in keys}
        if not want:
            return
        d = self._all()
        gone = [(k, h) for k, h in d.items() if norm_key(k) in want and h.get("session_id") == session_id]
        if gone:
            for k, _h in gone:
                d.pop(k)
            self._save(d)
        sh = self._shared()
        gone_sh = [(k, holders[session_id]) for k, holders in sh.items()
                   if session_id is not None and norm_key(k) in want and session_id in holders]
        if gone_sh:
            for k, _h in gone_sh:
                sh[k].pop(session_id, None)
            self._save_shared(sh)
        for k, h in gone + gone_sh:
            self._emit("lock_released", k, session_id, h.get("run_id"), reason)
        self._on_session_released(session_id, keys, reason)

    def release_session(self, session_id: str | None, reason: str = "session_ended") -> None:
        d = self._all()
        gone = [(k, v) for k, v in d.items() if v.get("session_id") == session_id]
        if gone:
            self._save({k: v for k, v in d.items() if v.get("session_id") != session_id})
        sh = self._shared()
        gone_sh = [(k, holders[session_id]) for k, holders in sh.items() if session_id is not None and session_id in holders]
        if gone_sh:
            for k, _h in gone_sh:
                sh[k].pop(session_id, None)
            self._save_shared(sh)
        for k, h in gone + gone_sh:
            self._emit("lock_released", k, session_id, h.get("run_id"), reason)
        self._on_session_released(session_id, None, reason)

    def force_release(self, key: str, reason: str = "manual") -> dict[str, Any] | None:
        """Gỡ khoá `key` bất kể ai giữ (người gỡ tay) — người giữ độc quyền lẫn MỌI người giữ chia sẻ. Trả người giữ cũ + `key`
        (+ `shared` = {session: người giữ} khi có người giữ chia sẻ); None nếu không ai giữ."""
        d = self._all()
        nk = norm_key(key)
        hit = next((k for k in d if norm_key(k) == nk), None)
        sh = self._shared()
        hit_sh = next((k for k in sh if norm_key(k) == nk), None)
        if hit is None and hit_sh is None:
            return None
        out: dict[str, Any] = {}
        if hit is not None:
            h = d.pop(hit)
            self._save(d)
            self._emit("lock_released", hit, h.get("session_id"), h.get("run_id"), reason)
            self._on_session_released(h.get("session_id"), [hit], reason)
            out = {**h, "key": hit}
        if hit_sh is not None:
            holders = sh.pop(hit_sh)
            self._save_shared(sh)
            for sid, h in holders.items():
                self._emit("lock_released", hit_sh, sid, h.get("run_id"), reason, mode="shared")
                self._on_session_released(sid, [hit_sh], reason)
            if not out:
                first_sid, first = next(iter(holders.items()))
                out = {**first, "session_id": first_sid, "key": hit_sh}
            out["shared"] = holders
        return out

    def list(self) -> dict[str, dict[str, Any]]:
        """Khoá ĐỘC QUYỀN đang giữ {khoá: người giữ} (giao diện cũ). Khoá chia sẻ: `list_shared`."""
        return self._all()


BROWSER_STATE_KEY = "browser_locks"


class BrowserLock:
    """Khoá trình duyệt Claude in Chrome: Chrome THẬT của người dùng chung cho mọi agent → tối đa
    `runtime.claude.chrome.max_concurrent_runs` run dùng tool chrome cùng lúc. Gắn theo RUN (không theo session như RepoLocks):
    lấy ở lần gọi tool chrome đầu tiên (GuardService.check), nhả khi run kết thúc (RunManager._run finally) — run `waiting_human`
    đã dừng tiến trình nên cũng nhả. Lưu `hub_state` (sống qua restart); người giữ không còn chạy bị dọn khi có run khác xin."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def _all(self) -> dict[str, dict[str, Any]]:
        d = self.store.state_get(BROWSER_STATE_KEY, {}) or {}
        return dict(d) if isinstance(d, dict) else {}

    def _save(self, d: dict[str, dict[str, Any]]) -> None:
        self.store.state_set(BROWSER_STATE_KEY, d)

    def acquire(self, run_id: str, agent_id: str | None, limit: int | None, is_active: Any = None) -> dict[str, dict[str, Any]] | None:
        """None = run giữ khoá (mới lấy hoặc đã có); dict = các run đang giữ (đã đủ `limit`)."""
        d = self._all()
        if run_id in d:
            return None
        n = len(d)
        if is_active is not None:
            d = {rid: h for rid, h in d.items() if is_active(rid)}
        if limit is not None and len(d) >= max(1, int(limit)):
            if len(d) != n:
                self._save(d)
            return d
        d[run_id] = {"agent_id": agent_id, "since": time.time()}
        self._save(d)
        return None

    def release(self, run_id: str | None) -> None:
        if not run_id:
            return
        d = self._all()
        if run_id in d:
            d.pop(run_id)
            self._save(d)

    def list(self) -> dict[str, dict[str, Any]]:
        return self._all()
