"""PR2 (plan-api-caching): không chặn event loop daemon.

- `system.list/get`: đọc backend (PowerShell/launchctl) trong thread + singleflight; một RPC chậm không làm RPC khác trễ.
- `TaskScheduler._query`: N thread cùng hỏi khi cache hết hạn → MỘT tiến trình PowerShell; ghi giữa chừng không để lại cache cũ.
- `claude_version`: stale-while-revalidate (trong loop trả giá trị cũ, đo lại nền), khoá theo chữ ký binary (nâng cấp CLI → đo lại).
- `login_available` / `auth_status`: singleflight (N run song song → một `claude auth status`), cache None ngắn, gộp auth_status 5 s.
- guard: `rev-parse --git-dir/--git-common-dir/--show-toplevel` cache theo cwd; `status --porcelain` không cache.
KHÔNG gọi `claude`/PowerShell/launchctl thật: binary giả là script Python trong tmp.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

from cfharness.config import DEFAULTS
from cfharness.daemon import Daemon
from cfharness.guard import classifier
from cfharness.runtime import claude_cli
from cfharness.runtime.claude_cli import ClaudeCliRuntime, claude_version
from cfharness.runtime.fake import FakeRuntime
from cfharness.systems.service import REGISTRY as SYS_REGISTRY
from cfharness.systems.taskscheduler import TaskScheduler


async def _rpc(d: Daemon, method: str, params: dict | None = None):
    r = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": dict(params or {})})
    assert "error" not in r, r
    return r["result"]


# ============================================================================ systems
class _SlowBackend:
    """Backend giả: `state()` chậm (như PowerShell 1–3 s) và đếm số lượt."""

    def __init__(self, delay: float = 0.4) -> None:
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def state(self):
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        return {"com.app.job": {"loaded": True, "pid": None, "last_exit": 0, "running": False}}

    def disabled(self):
        return set()

    def jobs(self, *_a):
        return [{"label": "com.app.job", "kind": "agent", "schedule": "hằng ngày", "program": ["x"]}]

    @staticmethod
    def label_ok(label):
        return True


def _systems_daemon(hub_home, backend) -> Daemon:
    d = Daemon(hub_home, runtime=FakeRuntime())
    d.hub.systems._lc = backend
    d.hub.store.state_set(SYS_REGISTRY, {"app": {"path": str(hub_home.home / "app"), "title": "App", "added_at": "2026-01-01T00:00:00Z"}})
    return d


async def test_system_list_does_not_block_other_rpc(hub_home):
    d = _systems_daemon(hub_home, _SlowBackend(0.6))
    try:
        slow = asyncio.create_task(_rpc(d, "system.list"))
        await asyncio.sleep(0.05)  # system.list đang chờ backend
        t0 = time.monotonic()
        await _rpc(d, "hub.ping")
        assert time.monotonic() - t0 < 0.3  # RPC khác không chờ PowerShell
        assert not slow.done()
        res = await slow
        assert [r["id"] for r in res] == ["app"]
    finally:
        d.hub.store.close()


async def test_system_list_get_singleflight(hub_home):
    be = _SlowBackend(0.3)
    d = _systems_daemon(hub_home, be)
    try:
        res = await asyncio.gather(*[_rpc(d, "system.list") for _ in range(5)], *[_rpc(d, "system.get", {"id": "app"}) for _ in range(3)])
        assert be.calls == 1  # 8 lời gọi đồng thời → một lượt đọc backend
        assert all(r[0]["id"] == "app" for r in res[:5]) and all(r["id"] == "app" for r in res[5:])
        await _rpc(d, "system.list")
        assert be.calls == 2  # lượt đã xong → không giữ (cache là việc của backend)
    finally:
        d.hub.store.close()


def test_taskscheduler_query_singleflight_and_write_invalidation():
    calls: list[list[str]] = []
    lock = threading.Lock()
    task = {"TaskName": "com.x.job", "TaskPath": "\\", "State": "Ready", "Actions": [{"Execute": "C:\\x\\run.exe"}], "Triggers": []}

    def runner(argv, timeout):
        with lock:
            calls.append(argv)
        if argv[0] == "powershell":
            time.sleep(0.3)
            return 0, json.dumps([task]), ""
        return 0, "", ""

    ts = TaskScheduler(runner=runner)
    assert ts.cache_s >= 15  # PR2: cache 2 s → 20 s
    threads = [threading.Thread(target=ts.jobs) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for a in calls if a[0] == "powershell") == 1
    ts.jobs()
    assert sum(1 for a in calls if a[0] == "powershell") == 1  # còn trong cache
    ts.pause("com.x.job")  # ghi → xoá cache
    ts.jobs()
    assert sum(1 for a in calls if a[0] == "powershell") == 2


def test_taskscheduler_inflight_query_not_cached_after_write():
    """Lượt truy vấn bắt đầu TRƯỚC thao tác ghi không được ghi kết quả (cũ) vào cache."""
    started, release = threading.Event(), threading.Event()

    def runner(argv, timeout):
        if argv[0] == "powershell":
            started.set()
            release.wait(5)
            return 0, "[]", ""
        return 0, "", ""

    ts = TaskScheduler(runner=runner)
    t = threading.Thread(target=ts.jobs)
    t.start()
    assert started.wait(5)
    ts._run("/Change", "/TN", "\\x", "/DISABLE")
    release.set()
    t.join()
    assert ts._cache is None


# ============================================================================ claude CLI giả
def _fake_cli(tmp_path: Path, *, version: str = "2.1.302", auth: dict | None = None, delay: float = 0.0) -> tuple[str, Path]:
    """Script Python đóng vai `claude`: `--version` / `auth status`; mỗi lần chạy ghi một dòng vào file đếm."""
    count = tmp_path / "calls.txt"
    p = tmp_path / "claude_fake.py"
    p.write_text(
        f"#!{sys.executable}\nimport json, sys, time\n"
        f"open({str(count)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"time.sleep({delay!r})\n"
        f"if sys.argv[1:] == ['--version']:\n    print({version!r} + ' (Claude Code)')\n"
        f"else:\n    print(json.dumps({auth or {'loggedIn': True, 'authMethod': 'claude.ai'}!r}))\n", encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p), count


def _calls(count: Path, prefix: str) -> int:
    return sum(1 for ln in count.read_text().splitlines() if ln.startswith(prefix)) if count.exists() else 0


def _wait(pred, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


async def test_claude_version_stale_while_revalidate(tmp_path):
    binary, count = _fake_cli(tmp_path, delay=0.5)
    claude_cli._version_cache.pop(binary, None)
    assert await asyncio.to_thread(claude_version, binary, 600) == "2.1.302"  # lần đầu: đo (trong thread)
    assert _calls(count, "--version") == 1
    sig = claude_cli._binary_sig(binary)
    claude_cli._version_cache[binary] = (time.monotonic() - 10_000, "2.1.100", sig)  # hết hạn
    t0 = time.monotonic()
    assert claude_version(binary, 600) == "2.1.100"  # trong event loop: trả giá trị CŨ ngay, không chờ 0,5 s
    assert time.monotonic() - t0 < 0.2
    assert await asyncio.to_thread(_wait, lambda: claude_cli._version_cache[binary][1] == "2.1.302")
    assert _calls(count, "--version") == 2
    claude_cli._version_cache.pop(binary, None)


def test_claude_version_keyed_on_binary_mtime(tmp_path):
    binary, count = _fake_cli(tmp_path)
    claude_cli._version_cache.pop(binary, None)
    assert claude_version(binary, 600) == "2.1.302"
    assert claude_version(binary, 600) == "2.1.302" and _calls(count, "--version") == 1  # cache
    _fake_cli(tmp_path, version="2.2.0")  # "nâng cấp" CLI: ghi lại binary
    st = os.stat(binary)
    os.utime(binary, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert claude_version(binary, 600) == "2.2.0"  # chữ ký đổi → đo lại dù chưa hết TTL
    claude_cli._version_cache.pop(binary, None)


async def test_aversion_first_call_off_loop(tmp_path):
    binary, _ = _fake_cli(tmp_path, delay=0.4)
    claude_cli._version_cache.pop(binary, None)
    rt = ClaudeCliRuntime({**DEFAULTS["runtime"]["claude"], "binary": binary})
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    tk = asyncio.create_task(ticker())
    try:
        assert await rt.aversion() == "2.1.302"
    finally:
        tk.cancel()
    assert ticks >= 5  # loop vẫn chạy trong lúc đo phiên bản lần đầu
    claude_cli._version_cache.pop(binary, None)


async def test_login_available_singleflight(tmp_path):
    binary, count = _fake_cli(tmp_path, delay=0.4)
    rt = ClaudeCliRuntime({**DEFAULTS["runtime"]["claude"], "binary": binary})
    res = await asyncio.gather(*[rt.login_available() for _ in range(8)])
    assert res == [True] * 8
    assert _calls(count, "auth status") == 1  # 8 run song song → một `claude auth status`
    assert await rt.login_available() is True and _calls(count, "auth status") == 1  # cache
    claude_cli.clear_auth_caches()  # đăng nhập/đăng xuất
    assert await rt.login_available() is True and _calls(count, "auth status") == 2


async def test_login_available_caches_unknown_briefly(tmp_path, monkeypatch):
    binary, count = _fake_cli(tmp_path, auth={"weird": 1})
    rt = ClaudeCliRuntime({**DEFAULTS["runtime"]["claude"], "binary": binary})
    assert await rt.login_available() is None
    assert await rt.login_available() is None
    assert _calls(count, "auth status") == 1  # None cũng được cache (ngắn)
    key = claude_cli._login_key(binary, None)
    ts, v = claude_cli._login_cache[key]
    claude_cli._login_cache[key] = (ts - claude_cli.AUTH_NONE_CACHE_S - 1, v)
    assert await rt.login_available() is None and _calls(count, "auth status") == 2  # hết 45 s → hỏi lại


async def test_auth_status_coalesced(tmp_path):
    binary, count = _fake_cli(tmp_path, delay=0.3, auth={"loggedIn": True})
    rt = ClaudeCliRuntime({**DEFAULTS["runtime"]["claude"], "binary": binary})
    assert await asyncio.gather(*[rt.auth_status() for _ in range(5)]) == [True] * 5
    assert await rt.auth_status() is True
    assert _calls(count, "auth status") == 1
    key = next(iter(claude_cli._auth_status_cache))
    ts, v = claude_cli._auth_status_cache[key]
    claude_cli._auth_status_cache[key] = (ts - claude_cli.AUTH_STATUS_COALESCE_S - 1, v)
    assert await rt.auth_status() is True and _calls(count, "auth status") == 2


# ============================================================================ guard rev-parse
def test_guard_revparse_cached_status_not(monkeypatch, tmp_path):
    seen: list[tuple[str, ...]] = []

    def fake(cwd, *args):
        seen.append(args)
        return "/repo/.git" if args[0] == "rev-parse" else ""

    monkeypatch.setattr(classifier, "_git_out_uncached", fake)
    cwd = str(tmp_path)
    for _ in range(3):
        classifier._git_out(cwd, "rev-parse", "--path-format=absolute", "--git-dir")
        classifier._git_out(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
        classifier._git_out(cwd, "rev-parse", "--show-toplevel")
        classifier._git_out(cwd, "rev-parse", "--verify", "-q", "HEAD")
        classifier._git_out(cwd, "status", "--porcelain")
    assert seen.count(("rev-parse", "--path-format=absolute", "--git-dir")) == 1
    assert seen.count(("rev-parse", "--path-format=absolute", "--git-common-dir")) == 1
    assert seen.count(("rev-parse", "--show-toplevel")) == 1
    assert seen.count(("rev-parse", "--verify", "-q", "HEAD")) == 3  # phụ thuộc trạng thái → không cache
    assert seen.count(("status", "--porcelain")) == 3
    # hết TTL → hỏi lại; LRU có trần
    k = (cwd, ("rev-parse", "--show-toplevel"))
    classifier._revparse_cache[k] = (time.monotonic() - classifier._REVPARSE_TTL_S - 1, "/old")
    classifier._git_out(cwd, "rev-parse", "--show-toplevel")
    assert seen.count(("rev-parse", "--show-toplevel")) == 2
    for i in range(classifier._REVPARSE_MAX + 10):
        classifier._git_out(f"{cwd}/{i}", "rev-parse", "--show-toplevel")
    assert len(classifier._revparse_cache) == classifier._REVPARSE_MAX


def test_guard_revparse_failure_not_cached(monkeypatch, tmp_path):
    n = {"c": 0}

    def fake(cwd, *args):
        n["c"] += 1

    monkeypatch.setattr(classifier, "_git_out_uncached", fake)
    classifier._git_out(str(tmp_path), "rev-parse", "--show-toplevel")
    classifier._git_out(str(tmp_path), "rev-parse", "--show-toplevel")
    assert n["c"] == 2  # chưa phải repo / lỗi → hỏi lại lần sau (vd vừa `git init`)
