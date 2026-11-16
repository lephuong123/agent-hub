"""Web tự chạy lại khi bản cài đổi phiên bản (web/reload.py) — sự cố trang Cài đặt 500 sau cập nhật lên v0.2.12 trên macOS."""

from __future__ import annotations

import socket
import sys
import threading
import time

from cfharness.web import reload as R


def _watcher(versions: list[str | None], **kw):
    seq = iter(versions)
    hits: list[str] = []
    w = R.InstallWatcher(hits.append, probe=lambda: next(seq), executable=sys.executable, **kw)
    return w, hits


def test_no_change_never_fires():
    w, hits = _watcher(["0.2.11"] * 6)
    assert [w.check() for _ in range(5)] == [False] * 5
    assert hits == [] and w.changed_to is None


def test_change_fires_once_after_stable_polls():
    w, hits = _watcher(["0.2.11", "0.2.12", "0.2.12", "0.2.12"], stable_polls=2)
    assert w.check() is False  # thấy lần 1 — uv có thể đang ghi dở
    assert w.check() is True
    assert w.check() is False  # chỉ một lần
    assert hits == ["0.2.12"] and w.changed_to == "0.2.12"


def test_flapping_or_unreadable_resets_counter():
    w, hits = _watcher(["0.2.11", "0.2.12", None, "0.2.12", "0.2.11", "0.2.12", "0.2.12"], stable_polls=2)
    assert [w.check() for _ in range(6)] == [False, False, False, False, False, True]
    assert hits == ["0.2.12"]


def test_missing_interpreter_waits(tmp_path):
    hits: list[str] = []
    seq = iter(["0.2.11", "0.2.12", "0.2.12", "0.2.12"])
    w = R.InstallWatcher(hits.append, probe=lambda: next(seq), executable=str(tmp_path / "python"), stable_polls=1)
    assert w.check() is False and w.check() is False  # venv đang tạo lại: chưa có python → chưa chạy lại
    (tmp_path / "python").write_text("")
    assert w.check() is True and hits == ["0.2.12"]


def test_unknown_baseline_disables():
    w, hits = _watcher([None, "0.2.12", "0.2.12"], stable_polls=1)
    assert w.check() is False and w.check() is False and hits == []
    w.start()  # không tạo luồng
    assert w._thread is None


def test_baseline_is_installed_not_loaded_version():
    """So metadata↔metadata: bản editable có __version__ khác metadata không gây vòng lặp chạy lại."""
    w, hits = _watcher(["0.0.0-meta"] * 4, stable_polls=1)
    assert w.check() is False and hits == []


def test_installed_version_reads_metadata():
    from importlib.metadata import version

    assert R.installed_version() == version("cfharness")


def test_restart_argv_keeps_command_drops_open():
    orig = ["/old/python", "/Users/u/.local/bin/cfharness", "web", "--port", "8721", "--open", "--no-link", "--home", "/h"]
    assert R.restart_argv(orig, executable="/venv/python") == ["/venv/python", "/Users/u/.local/bin/cfharness", "web", "--port", "8721",
                                                               "--no-link", "--home", "/h"]
    assert R.restart_argv(["/p", "-m", "cfharness", "web"], executable="/p") == ["/p", "-m", "cfharness", "web"]


def test_watcher_stops_real_uvicorn_server():
    """Luồng nền phát hiện bản mới → uvicorn dừng êm (cmd_web sau đó exec lại)."""
    import uvicorn
    from starlette.applications import Starlette

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(Starlette(), host="127.0.0.1", port=port, log_level="error", timeout_graceful_shutdown=5))
    state = {"v": "0.2.11"}

    def on_change(_v: str) -> None:
        server.should_exit = True

    w = R.InstallWatcher(on_change, probe=lambda: state["v"], poll_s=0.05, stable_polls=2, executable=sys.executable)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    deadline = time.time() + 5
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started
    w.start()
    state["v"] = "0.2.12"
    t.join(10)
    w.stop()
    assert not t.is_alive() and w.changed_to == "0.2.12"


# ============================================================================ phía daemon: web còn chạy code cũ (khởi động trước bản sửa)
import json
import os
import subprocess
import textwrap

import pytest

from cfharness.paths import HubPaths
from cfharness.web import security as S

FAKE_WEB = textwrap.dedent('''
    import json, os, signal, sys, time
    from pathlib import Path
    home = Path(os.environ["CFHARNESS_HOME"])
    (home / "auth").mkdir(parents=True, exist_ok=True)
    st = {"v": 1, "pid": os.getpid(), "host": "127.0.0.1", "name": "cfharness.localhost", "port": 1, "key": "00" * 32,
          "argv": sys.argv[1:]}
    if os.environ.get("FAKE_WEB_VERSION"):
        st["version"] = os.environ["FAKE_WEB_VERSION"]
    p = home / "auth" / "web.json"
    p.write_text(json.dumps(st)); os.chmod(p, 0o600)
    def bye(*_):
        try:
            if json.loads(p.read_text())["pid"] == os.getpid():
                p.unlink()
        except Exception:
            pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, bye)
    while True:
        time.sleep(0.1)
''')


def test_web_argv_from_cmdline():
    f = R.web_argv_from_cmdline
    assert f("/v/bin/python /Users/u/.local/bin/cfharness web --port 8721 --open --no-link") == [
        "/v/bin/python", "/Users/u/.local/bin/cfharness", "web", "--port", "8721", "--no-link"]
    assert f("/v/bin/python -m cfharness web --no-link --port 8765") == ["/v/bin/python", "-m", "cfharness", "web", "--no-link", "--port", "8765"]
    assert f("/v/bin/python -m cfharness daemon run") is None
    assert f("/usr/bin/python3 -m pytest tests/test_web.py") is None
    assert f("vim web") is None


def test_web_state_records_version(tmp_path):
    from cfharness import __version__

    paths = HubPaths(tmp_path)
    S.write_state(paths, host="127.0.0.1", port=1, key=b"\0" * 32)
    assert json.loads(S.state_path(paths).read_text())["version"] == __version__


def test_stale_web_never_touches_non_web_pid(tmp_path):
    """State cũ trỏ tới pid của chính tiến trình này (pytest) / pid không phải web → không bao giờ dừng."""
    paths = HubPaths(tmp_path)
    S.write_state(paths, host="127.0.0.1", port=1, key=b"\0" * 32)
    data = json.loads(S.state_path(paths).read_text())
    data.pop("version")
    S.state_path(paths).write_text(json.dumps(data))
    assert R.stale_web(paths, "9.9.9") is None  # pid = chính mình
    assert R.restart_stale_web(paths, "9.9.9", respawn_wait_s=0.1) == "none"
    assert R.stale_web(paths, "9.9.9", cmdline=lambda _p: "/usr/bin/python3 -m pytest") is None


@pytest.mark.skipif(os.name == "nt", reason="Windows: helper cập nhật đã dừng/chạy lại web")
@pytest.mark.parametrize("old_version", [None, "0.2.11"])
def test_daemon_restarts_stale_web_process(tmp_path, old_version):
    """Web cũ (không có/khác version) → SIGTERM; không ai bật lại → daemon chạy lại đúng dòng lệnh (bỏ --open)."""
    home = tmp_path / "home"
    paths = HubPaths(home)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    launcher = bindir / "cfharness"
    launcher.write_text(FAKE_WEB)
    env = {**os.environ, "CFHARNESS_HOME": str(home)}
    if old_version:
        env["FAKE_WEB_VERSION"] = old_version
    old = subprocess.Popen([sys.executable, str(launcher), "web", "--port", "1", "--open"], env=env)
    procs = [old.pid]
    try:
        deadline = time.time() + 10
        while S.read_state(paths) is None and time.time() < deadline:
            time.sleep(0.05)
        assert S.read_state(paths)["pid"] == old.pid
        assert R.stale_web(paths, old_version or "") is None if old_version else True  # cùng bản → không đụng
        assert R.restart_stale_web(paths, "9.9.9", respawn_wait_s=0.5) == "respawned"
        assert old.wait(10) == 0
        deadline = time.time() + 10
        st = None
        while time.time() < deadline:
            st = S.read_state(paths)
            if st and st["pid"] != old.pid:
                break
            time.sleep(0.05)
        assert st and st["pid"] != old.pid
        procs.append(st["pid"])
        assert st["argv"] == ["web", "--port", "1"]  # --open bị bỏ
        assert (home / "logs" / "web.log").exists()
    finally:
        for pid in procs:
            try:
                os.kill(pid, 15)
            except OSError:
                pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX")
def test_daemon_leaves_respawn_to_launchd(tmp_path, monkeypatch):
    """Ai đó (launchd KeepAlive) bật lại web trong lúc chờ → daemon không chạy thêm bản thứ hai."""
    home = tmp_path / "home"
    paths = HubPaths(home)
    launcher = tmp_path / "cfharness"
    launcher.write_text(FAKE_WEB)
    env = {**os.environ, "CFHARNESS_HOME": str(home)}
    old = subprocess.Popen([sys.executable, str(launcher), "web"], env=env)
    new = None
    try:
        deadline = time.time() + 10
        while S.read_state(paths) is None and time.time() < deadline:
            time.sleep(0.05)
        spawned: list = []
        monkeypatch.setattr(R, "spawn_detached", lambda *a: spawned.append(a))
        real_wait = R._wait_gone

        def gone_then_launchd(pid, t):
            nonlocal new
            ok = real_wait(pid, t)
            new = subprocess.Popen([sys.executable, str(launcher), "web"], env={**env, "FAKE_WEB_VERSION": "9.9.9"})
            return ok

        monkeypatch.setattr(R, "_wait_gone", gone_then_launchd)
        assert R.restart_stale_web(paths, "9.9.9", respawn_wait_s=10) == "killed"
        assert spawned == []
        assert R.stale_web(paths, "9.9.9") is None  # web mới cùng bản → để yên
    finally:
        for p in (old, new):
            if p:
                p.kill()
