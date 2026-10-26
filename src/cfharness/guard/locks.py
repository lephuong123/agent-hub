"""Khoá ghi theo thư mục làm việc — BẢN TỐI THIỂU M1 (khoá theo repo đầy đủ + checkpoint là FR-RT-12, M3).

Giao diện (M3 giữ nguyên, chỉ đổi cách tính khoá/điều kiện):
- `key_for(cwd, workspace_kind) -> str | None` : khoá cho run; None = không cần khoá (scratch riêng của run).
- `holder(key) -> dict | None`                 : ai đang giữ ({session_id, run_id, since}).
- `try_acquire(key, session_id, run_id) -> bool`: cùng session (chuỗi resume) được kế thừa khoá.
- `release(key, session_id)`                    : nhả nếu đúng người giữ.
Khoá gắn với SESSION (không phải run) để run `waiting_human` giữ khoá qua lần dừng tiến trình và run resume kế thừa
(Q-21). Lưu trong `hub_state` (sống qua restart daemon). M1: khoá = đường dẫn tuyệt đối của cwd (`dir:`); M3 đổi thành
gốc repo git và bỏ qua run chỉ đọc.
"""

from __future__ import annotations

import os
import time
from typing import Any

STATE_KEY = "repo_locks"


class RepoLocks:
    def __init__(self, store: Any) -> None:
        self.store = store

    def _all(self) -> dict[str, dict[str, Any]]:
        return dict(self.store.state_get(STATE_KEY, {}) or {})

    def _save(self, d: dict[str, dict[str, Any]]) -> None:
        self.store.state_set(STATE_KEY, d)

    @staticmethod
    def key_for(cwd: str | None, workspace_kind: str) -> str | None:
        if not cwd or workspace_kind != "dir":
            return None
        return os.path.realpath(os.path.expanduser(cwd))

    def holder(self, key: str) -> dict[str, Any] | None:
        return self._all().get(key)

    def blocked_for(self, key: str | None, session_id: str | None) -> dict[str, Any] | None:
        if key is None:
            return None
        h = self.holder(key)
        if h and h.get("session_id") != session_id:
            return h
        return None

    def try_acquire(self, key: str | None, session_id: str, run_id: str) -> bool:
        if key is None:
            return True
        d = self._all()
        h = d.get(key)
        if h and h.get("session_id") != session_id:
            return False
        d[key] = {"session_id": session_id, "run_id": run_id, "since": (h or {}).get("since") or time.time()}
        self._save(d)
        return True

    def release(self, key: str | None, session_id: str | None) -> None:
        if key is None:
            return
        d = self._all()
        h = d.get(key)
        if h and h.get("session_id") == session_id:
            d.pop(key)
            self._save(d)

    def release_session(self, session_id: str | None) -> None:
        d = self._all()
        keep = {k: v for k, v in d.items() if v.get("session_id") != session_id}
        if len(keep) != len(d):
            self._save(keep)

    def list(self) -> dict[str, dict[str, Any]]:
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

    def acquire(self, run_id: str, agent_id: str | None, limit: int, is_active: Any = None) -> dict[str, dict[str, Any]] | None:
        """None = run giữ khoá (mới lấy hoặc đã có); dict = các run đang giữ (đã đủ `limit`)."""
        d = self._all()
        if run_id in d:
            return None
        n = len(d)
        if is_active is not None:
            d = {rid: h for rid, h in d.items() if is_active(rid)}
        if len(d) >= max(1, int(limit)):
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
