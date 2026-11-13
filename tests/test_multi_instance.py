"""Đa instance P1 (`docs/dev-notes/multi-instance.md`): quyền file, socket dự phòng + kiểm chủ socket, cổng MCP, cookie web
theo hub, cổng callback OAuth, biến môi trường chỉ-cho-test, `cfharness doctor multi`. Không gọi `claude`, không cần daemon."""

from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path
from types import SimpleNamespace

import httpx2 as httpx
import pytest

from cfharness import oscompat
from cfharness import paths as P
from cfharness.oscompat import IS_WINDOWS

posix = pytest.mark.skipif(IS_WINDOWS, reason="quyền POSIX / Unix socket")


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


# ============================================================================ 1. quyền HUB_HOME + umask
@posix
def test_ensure_makes_home_private(tmp_path):
    hp = P.HubPaths(tmp_path / "hub")
    hp.ensure()
    assert _mode(hp.home) == 0o700 and _mode(hp.home / "secrets") == 0o700


@posix
def test_tighten_existing_install(tmp_path):
    home = tmp_path / "hub"
    (home / "logs").mkdir(parents=True)
    (home / "runs").mkdir()
    (home / "hub.db").write_text("x")
    os.chmod(home, 0o755)
    os.chmod(home / "logs", 0o755)
    os.chmod(home / "hub.db", 0o644)
    hp = P.HubPaths(home)
    fixed = hp.tighten_permissions()
    assert _mode(home) == 0o700 and _mode(home / "logs") == 0o700 and _mode(home / "hub.db") == 0o600
    assert str(home / "hub.db") in fixed
    assert hp.tighten_permissions() == []  # idempotent


@posix
def test_tighten_does_not_raise_when_chmod_fails(tmp_path, monkeypatch, caplog):
    home = tmp_path / "hub"
    home.mkdir()
    os.chmod(home, 0o755)

    def boom(*a, **k):
        raise PermissionError("không cho")

    monkeypatch.setattr(os, "chmod", boom)
    assert P.HubPaths(home).tighten_permissions() == []
    assert "không siết được quyền" in caplog.text


@posix
def test_private_umask_new_files_and_child_gets_original(tmp_path, monkeypatch):
    monkeypatch.setattr(oscompat, "_CHILD_UMASK", None)
    old = os.umask(0o022)
    try:
        oscompat.set_private_umask()
        f = tmp_path / "hub.db"
        f.write_text("x")
        d = tmp_path / "logs"
        d.mkdir()
        assert _mode(f) == 0o600 and _mode(d) == 0o700
        kw = oscompat.new_group_kwargs()
        assert kw["umask"] == 0o022 and kw["start_new_session"] is True  # claude con: file trong workspace như người tạo
    finally:
        os.umask(old)


# ============================================================================ 2. socket dự phòng + kiểm chủ
@posix
def test_long_home_socket_goes_to_private_user_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("CFHARNESS_SOCKET", raising=False)
    long_home = tmp_path / ("x" * 120)
    sock = P.HubPaths(long_home).socket
    assert sock.parent == Path("/tmp") / f"cfharness-{os.getuid()}"
    assert sock.name.startswith("cfharness-") and sock.name.endswith(".sock")
    assert P.HubPaths(long_home).socket == sock  # xác định (client = daemon)
    assert P.HubPaths(tmp_path / ("y" * 120)).socket != sock


@posix
def test_ensure_socket_dir_private_and_owner_checked(tmp_path, monkeypatch):
    d = tmp_path / "sockdir"
    monkeypatch.setattr(P, "fallback_socket_dir", lambda: d)
    P.ensure_socket_dir(d / "cfharness-abc.sock")
    assert _mode(d) == 0o700
    os.chmod(d, 0o755)
    P.ensure_socket_dir(d / "cfharness-abc.sock")
    assert _mode(d) == 0o700  # siết lại
    # symlink chiếm chỗ → từ chối
    d.rmdir()
    (tmp_path / "evil").mkdir()
    d.symlink_to(tmp_path / "evil")
    with pytest.raises(P.UnsafeSocketDir):
        P.ensure_socket_dir(d / "cfharness-abc.sock")
    # socket không ở thư mục dự phòng → không đụng
    P.ensure_socket_dir(tmp_path / "other" / "hub.sock")
    assert not (tmp_path / "other").exists()


@posix
def test_socket_dir_protected_by_sandbox_and_classifier(tmp_path, monkeypatch):
    from cfharness.guard.classifier import HUB_SOCKET_RE

    sock = P.fallback_socket_dir() / "cfharness-abc.sock"
    assert P.socket_private_dirs(sock) == [str(sock.parent)]
    assert P.socket_private_dirs(tmp_path / "hub.sock") == []
    assert HUB_SOCKET_RE.search(f"rm -rf /tmp/cfharness-{os.getuid()}")
    assert HUB_SOCKET_RE.search(f"ls /private/tmp/cfharness-{os.getuid()}/")
    assert not HUB_SOCKET_RE.search("rm -rf /tmp/cfharness-build-cache")


def _tok(sock: Path, value: str = "tok-bi-mat") -> None:
    from cfharness.auth import token_path

    token_path(sock).write_text(value)
    os.chmod(token_path(sock), 0o600)


@posix
def test_with_auth_sends_token_only_to_own_real_socket(tmp_path):
    import tempfile

    from cfharness.rpc import UntrustedSocket, with_auth

    base = Path(tempfile.mkdtemp(prefix="cfm-", dir="/tmp"))
    sock = base / "h.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock))
        _tok(sock)
        assert with_auth(sock, {})["_auth"] == "tok-bi-mat"
        # file thường giả làm socket (kẻ chiếm chỗ) → từ chối, không gửi token
        fake = base / "f.sock"
        fake.write_text("")
        _tok(fake)
        with pytest.raises(UntrustedSocket, match="không phải Unix socket"):
            with_auth(fake, {})
        # symlink tới socket thật → cũng từ chối (lstat)
        link = base / "l.sock"
        link.symlink_to(sock)
        _tok(link)
        with pytest.raises(UntrustedSocket):
            with_auth(link, {})
    finally:
        srv.close()


@posix
def test_with_auth_refuses_socket_of_other_user(tmp_path, monkeypatch):
    import tempfile

    from cfharness import rpc
    from cfharness.rpc import UntrustedSocket, with_auth

    base = Path(tempfile.mkdtemp(prefix="cfm-", dir="/tmp"))
    sock = base / "h.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(sock))
        _tok(sock)
        real_lstat = os.lstat

        def lstat(p, *a, **k):
            st = real_lstat(p, *a, **k)
            if str(p) == str(sock):
                return SimpleNamespace(st_mode=st.st_mode, st_uid=os.getuid() + 1)
            return st

        monkeypatch.setattr(os, "lstat", lstat)
        with pytest.raises(UntrustedSocket, match="thuộc user khác") as ei:
            with_auth(sock, {"a": 1})
        assert "tok-bi-mat" not in str(ei.value)
        assert isinstance(ei.value, rpc.RpcError)  # CLI báo lỗi như mọi RpcError (mã thoát hệ thống)
    finally:
        srv.close()


@posix
def test_rpc_client_does_not_leak_token_to_squatted_socket(tmp_path, monkeypatch):
    """Client thật: socket không phải của mình → lỗi TRƯỚC khi ghi gì lên kết nối."""
    import tempfile
    import threading

    from cfharness.rpc import RpcClient, UntrustedSocket

    base = Path(tempfile.mkdtemp(prefix="cfm-", dir="/tmp"))
    sock = base / "h.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock))
    srv.listen(1)
    _tok(sock)
    got: list[bytes] = []

    def serve():
        c, _ = srv.accept()
        c.settimeout(2)
        try:
            got.append(c.recv(65536))
        except OSError:
            pass
        c.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    real_lstat = os.lstat
    monkeypatch.setattr(os, "lstat", lambda p, *a, **k: SimpleNamespace(st_mode=real_lstat(p).st_mode, st_uid=os.getuid() + 1)
                        if str(p) == str(sock) else real_lstat(p, *a, **k))
    try:
        with pytest.raises(UntrustedSocket):
            RpcClient(sock, timeout=2).call("approval.approve", {"id": "x"})
        t.join(3)
        assert not any(b"tok-bi-mat" in g for g in got)
    finally:
        srv.close()


# ============================================================================ 3. cổng MCP HTTP
def test_mcp_http_default_port_differs_from_web():
    from cfharness.config import DEFAULTS, MCP_HTTP_DEFAULT_PORT
    from cfharness.guard.service import is_hub_mcp_config
    from cfharness.web.cli import DEFAULT_PORT as WEB_PORT

    assert DEFAULTS["mcp_server"]["http_port"] == MCP_HTTP_DEFAULT_PORT == 8766 != WEB_PORT
    assert is_hub_mcp_config({"url": "http://127.0.0.1:8766/mcp"})
    assert is_hub_mcp_config({"url": "http://127.0.0.1:8765/mcp"})  # cài đặt cũ vẫn nhận ra
    assert is_hub_mcp_config({"url": "http://127.0.0.1:9100/mcp"}, port=9100)
    assert not is_hub_mcp_config({"url": "http://127.0.0.1:9100/mcp"})


def test_config_validates_ports():
    from cfharness.config import validate_setting

    for bad in (0, 70000, "8766", True):
        with pytest.raises(ValueError):
            validate_setting("mcp_server.http_port", bad)
    validate_setting("mcp_client.oauth_callback_port", 0)
    with pytest.raises(ValueError):
        validate_setting("mcp_client.oauth_callback_port", -1)


def test_mcp_serve_busy_port_clear_error(tmp_path, capsys):
    from cfharness.cli import main

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        rc = main(["--home", str(tmp_path / "hub"), "mcp", "serve", "--http", "--port", str(port)])
    finally:
        s.close()
    err = capsys.readouterr().err
    assert rc == 1 and f"cổng {port} đang bận" in err and "--port" in err


# ============================================================================ 4. cookie web theo hub
def test_cookie_name_per_hub(tmp_path):
    from cfharness.web.security import cookie_name

    a, b = cookie_name(tmp_path / "a"), cookie_name(tmp_path / "b")
    assert a != b and a.startswith("ahub_web_") and len(a) == len("ahub_web_") + 6
    assert cookie_name(P.HubPaths(tmp_path / "a")) == a == cookie_name(str(tmp_path / "a"))


async def test_two_hubs_same_host_do_not_share_session_cookie(tmp_path):
    from cfharness.web.app import create_app
    from cfharness.web.security import WebAuth, cookie_name

    names = []
    for i, port in ((1, 8711), (2, 8721)):
        hp = P.HubPaths(tmp_path / f"hub{i}")
        auth = WebAuth()
        app = create_app(hp, auth=auth, port=port)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"http://cfharness.localhost:{port}") as c:
            r = await c.get("/login?t=" + auth.mint())
            assert r.status_code == 303 and r.headers["location"].startswith("/#k=")  # khoá phiên vẫn ngoài cookie
            sc = r.headers["set-cookie"]
            assert sc.startswith(cookie_name(hp) + "=") and "httponly" in sc.lower()
            names.append(cookie_name(hp))
    assert names[0] != names[1]


# ============================================================================ 5. cổng callback OAuth
def test_oauth_callback_port_configurable(tmp_path):
    from cfharness.config import HubConfig
    from cfharness.mcp_client.oauth_cli import DEFAULT_PORT, callback_port

    hp = P.HubPaths(tmp_path / "hub")
    hp.ensure()
    ctx = SimpleNamespace(paths=hp, args=SimpleNamespace(port=None))
    assert callback_port(ctx) == DEFAULT_PORT == 33418
    HubConfig(hp.config).set("mcp_client.oauth_callback_port", 0)
    assert callback_port(ctx) == 0
    HubConfig(hp.config).set("mcp_client.oauth_callback_port", 34000)
    assert callback_port(ctx) == 34000
    ctx.args.port = 35000
    assert callback_port(ctx) == 35000


# ============================================================================ 6. biến môi trường chỉ-cho-test
def test_extra_workspace_roots_env_ignored_without_testing_flag(tmp_path, monkeypatch):
    from cfharness import workspace as W

    monkeypatch.setenv("CFHARNESS_EXTRA_WORKSPACE_ROOTS", str(tmp_path / "ngoai"))
    monkeypatch.setenv("CFHARNESS_TESTING", "1")
    assert W.env_extra_roots() == [str(tmp_path / "ngoai")]
    monkeypatch.delenv("CFHARNESS_TESTING")
    assert W.env_extra_roots() == []
    monkeypatch.setenv("CFHARNESS_TESTING", "yes")
    assert W.env_extra_roots() == []


def test_extra_roots_env_ignored_in_validate_workspace_and_profiles(tmp_path, monkeypatch):
    from cfharness.config import HubConfig
    from cfharness.errors import HubError
    from cfharness.guard import profiles
    from cfharness import workspace as W

    outside = tmp_path / "ngoai"
    outside.mkdir()
    hp = P.HubPaths(tmp_path / "hub")
    hp.ensure()
    hub = SimpleNamespace(paths=hp, config=HubConfig(hp.config))
    HubConfig(hp.config).set("workspace.allowed_roots", [str(tmp_path / "chi-day")])
    monkeypatch.setattr(W, "_EXTRA_ALLOWED_ROOTS", [])
    monkeypatch.setenv("CFHARNESS_EXTRA_WORKSPACE_ROOTS", str(outside))
    monkeypatch.delenv("CFHARNESS_TESTING")
    with pytest.raises(HubError, match="không nằm trong gốc workspace"):
        W.validate_workspace(hub, str(outside))
    assert not any(str(outside) in r or os.path.realpath(outside) in r for r in profiles.allowed_roots(hub))


def test_conftest_sets_testing_flag_for_subprocesses():
    assert os.environ.get("CFHARNESS_TESTING") == "1"


# ============================================================================ 7. doctor multi
def test_doctor_multi_reports_and_never_prints_tokens(tmp_path, capsys):
    from cfharness.cli import main

    home = tmp_path / "hub"
    hp = P.HubPaths(home)
    hp.ensure()
    (home / "secrets" / "TELEGRAM_BOT_TOKEN").write_text("123456:AAAA-bi-mat-tuyet-doi")
    rc = main(["--home", str(home), "doctor", "multi", "--json"])
    out = capsys.readouterr().out
    assert "bi-mat-tuyet-doi" not in out and "123456" not in out
    data = json.loads(out)
    checks = {c["check"]: c for c in data["checks"]}
    assert "vân tay sha256:" in checks["token Telegram bot"]["detail"]
    assert checks["token Slack bot"]["detail"] == "chưa cấu hình"
    assert "8766" in checks["cổng MCP HTTP khác cổng web"]["detail"]
    if not IS_WINDOWS:
        assert checks["HUB_HOME riêng tư (0700)"]["ok"]
    assert rc == (0 if data["ok"] else 1)
    # HUB_HOME mở cho người khác → lỗi, mã thoát 1
    if not IS_WINDOWS:
        os.chmod(home, 0o755)
        rc = main(["--home", str(home), "doctor", "multi", "--json"])
        data = json.loads(capsys.readouterr().out)
        assert rc == 1 and not data["ok"]
        assert any(c["check"] == "HUB_HOME riêng tư (0700)" and not c["ok"] for c in data["checks"])


def test_doctor_multi_text_is_vietnamese(tmp_path, capsys):
    from cfharness.cli import main

    rc = main(["--home", str(tmp_path / "hub"), "doctor", "multi"])
    out = capsys.readouterr().out
    assert "cổng MCP HTTP" in out and "workspace.allowed_roots" in out
    assert rc in (0, 1)


def test_multi_is_reserved_agent_id():
    from cfharness.agents import RESERVED_IDS

    assert "multi" in RESERVED_IDS


# ============================================================================ 1b. một daemon cho mỗi HUB_HOME (daemon.lock)
def _daemon_env(hp: P.HubPaths) -> dict[str, str]:
    import sys as _sys

    env = dict(os.environ)
    env["CFHARNESS_HOME"] = str(hp.home)
    return env | {"PYTHONPATH": os.pathsep.join(p for p in _sys.path if p)}


@posix
def test_daemon_lock_exclusive_and_released(tmp_path):
    from cfharness import daemon as D

    hp = P.HubPaths(tmp_path / "hub")
    hp.ensure()
    h = D.acquire_instance_lock(hp)
    assert h is not None and _mode(D.instance_lock_path(hp)) == 0o600
    assert D.acquire_instance_lock(hp) is None  # bản thứ hai không lấy được
    assert D.instance_lock_held(hp)
    D.release_instance_lock(h)
    assert not D.instance_lock_held(hp)
    D.release_instance_lock(D.acquire_instance_lock(hp))


@posix
def test_daemon_lock_second_daemon_exits(tmp_path):
    """Sự cố lamlinh: bản daemon thứ hai trên CÙNG HUB_HOME phải thoát (mã ≠ 0), không mở hub.db/socket."""
    import subprocess
    import sys as _sys

    from cfharness import daemon as D

    hp = P.HubPaths(tmp_path / "hub")
    hp.ensure()
    h = D.acquire_instance_lock(hp)  # giả một daemon đang khởi động (chưa có socket)
    try:
        r = subprocess.run([_sys.executable, "-m", "cfharness", "daemon", "run"], env=_daemon_env(hp),
                           capture_output=True, text=True, timeout=60)
    finally:
        D.release_instance_lock(h)
    assert r.returncode == 1 and "daemon đã chạy" in r.stderr
    assert not hp.socket.exists() and not hp.db.exists()


@posix
def test_daemon_lock_spawn_waits_instead_of_second_daemon(tmp_path, monkeypatch):
    """Tự khởi động (CLI/doctor) khi daemon khác đang giữ khoá: chờ, KHÔNG bật thêm tiến trình."""
    from cfharness import daemon as D

    hp = P.HubPaths(tmp_path / "hub")
    hp.ensure()

    def boom(*a, **k):
        raise AssertionError("không được bật daemon thứ hai")

    monkeypatch.setattr(D.subprocess, "Popen", boom)
    h = D.acquire_instance_lock(hp)
    try:
        with pytest.raises(RuntimeError, match="không sẵn sàng"):
            D.spawn_background(hp, wait_s=0.5)
    finally:
        D.release_instance_lock(h)


@posix
def test_daemon_lock_two_concurrent_daemons_one_survives(tmp_path):
    import subprocess
    import sys as _sys
    import time as _time

    from cfharness.rpc import DaemonUnavailable, RpcClient

    hp = P.HubPaths(tmp_path / "hub")
    hp.ensure()
    cmd = [_sys.executable, "-m", "cfharness", "daemon", "run"]
    procs = [subprocess.Popen(cmd, env=_daemon_env(hp), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for _ in range(2)]
    try:
        c = RpcClient(hp.socket, timeout=2)
        deadline = _time.monotonic() + 30
        pid = None
        while _time.monotonic() < deadline:
            try:
                pid = c.call("hub.ping")["pid"]
                break
            except DaemonUnavailable:
                _time.sleep(0.1)
        assert pid in {p.pid for p in procs}
        loser = next(p for p in procs if p.pid != pid)
        assert loser.wait(timeout=30) == 1
        assert procs[0 if procs[0].pid == pid else 1].poll() is None  # bản thắng vẫn chạy
        c.call("hub.shutdown")
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
            p.wait(timeout=30)
            if p.stderr:
                p.stderr.close()


def test_totp_enroll_hides_secret_uri_by_default(tmp_path, capsys):
    from cfharness.web.cli import cmd_totp

    hp = P.HubPaths(tmp_path / "hub")
    hp.ensure()

    def ctx(show: bool) -> SimpleNamespace:
        def call(*a, **k):
            raise RuntimeError("không có daemon")

        return SimpleNamespace(paths=hp, call=call, args=SimpleNamespace(ttl=None, activate=False, show_uri=show))

    assert cmd_totp(ctx(False), ["totp", "enroll"]) == 0
    out = capsys.readouterr().out
    assert "otpauth://" not in out and "secret=" not in out and "/enroll?t=" in out
    assert cmd_totp(ctx(True), ["totp", "enroll"]) == 0
    assert "otpauth://" in capsys.readouterr().out
