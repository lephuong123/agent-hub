"""Hỗ trợ phía hub cho app desktop (web/desktop.py, docs/dev-notes/desktop-app.md — hợp đồng C1, C2, C3, C4, C7).

Không gọi `claude`: web chạy in-process (ASGITransport) trên daemon in-process + FakeRuntime; test `--ensure` spawn server web THẬT
tách tiến trình trong HUB_HOME tạm (runtime giả) rồi dọn sạch. Cầu nối giọng nói kiểm bằng node + DOM giả (bỏ qua nếu thiếu node).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from cfharness.web import desktop as D
from cfharness.web.security import check_login_token, read_state, state_path, write_state

from test_web import env, held, open_approval, running_run  # noqa: F401  (fixture daemon + web in-process)

ROOT = Path(__file__).resolve().parents[1]
VOICE_JS = ROOT / "src" / "cfharness" / "web" / "static" / "voice.js"
HARNESS = Path(__file__).parent / "js" / "voice_native_harness.js"
WIN = sys.platform == "win32"


# =========================================================================== C2: /.well-known/cfharness.json
async def test_well_known_public_minimal(env):
    r = await env.client.get("/.well-known/cfharness.json")  # KHÔNG đăng nhập
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {"kind": "cfharness.hub", "schema": 1, "app_api": 1}  # đúng 3 khoá — không phiên bản, không tên
    # Host vẫn bị kiểm (chống DNS rebinding)
    bad = await env.client.get("/.well-known/cfharness.json", headers={"Host": "evil.example.com"})
    assert bad.status_code == 421
    # chỉ đúng đường này công khai — tiền tố khác vẫn cần phiên
    assert (await env.client.get("/.well-known/other.json")).status_code in (303, 401, 404)
    assert (await env.client.get("/.well-known/other.json")).status_code != 200


# =========================================================================== C3: /app/badge
async def test_badge_requires_session_json_401(env):
    r = await env.client.get("/app/badge")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"error": "unauthenticated"}


async def test_badge_shape_and_counts(held):
    e = held
    await e.login()
    r = await e.client.get("/app/badge")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    b = r.json()
    assert set(b) == {"approvals", "questions", "running", "estop", "version"}
    assert b["approvals"] == 0 and b["questions"] == 0 and b["running"] == 0 and b["estop"] is False
    from cfharness import __version__

    assert b["version"] == __version__
    run = await running_run(e)
    open_approval(e.hub, run["id"])
    b = (await e.client.get("/app/badge")).json()
    assert b["approvals"] == 1 and b["running"] == 1
    for k in ("approvals", "questions", "running"):
        assert type(b[k]) is int
    assert type(b["estop"]) is bool
    assert "rm -rf" not in json.dumps(b)  # không chữ tự do
    r = await e.post("/do/estop", {"on": "1", "confirm": "yes"})
    assert r.status_code in (200, 303), r.text
    assert (await e.client.get("/app/badge")).json()["estop"] is True


async def test_badge_does_not_extend_session(env):
    await env.login()
    sid = env.client.cookies.get(env.cookie)
    sess = env.auth.sessions[sid]
    sess.last -= 100
    before = sess.last
    assert (await env.client.get("/app/badge")).status_code == 200
    assert env.auth.sessions[sid].last == before  # polling của app không gia hạn phiên
    await env.client.get("/activity")  # đối chứng: trang của người thì gia hạn
    assert env.auth.sessions[sid].last > before


def test_badge_from_whitelist_only():
    pend = [{"id": "ap1", "summary": "rm -rf /", "routed_to": None}, {"id": "ap2", "routed_to": "manager", "summary": "x"}, "rác"]
    qs = [{"id": "q1", "text": "Xoá không?"}, {"id": "q2", "text": "<script>"}]
    b = D.badge_from(pend, qs, {"running": 3, "home": "/Users/x"}, {"on": False, "admin": True, "reason": "bí mật"})
    assert b == {"approvals": 1, "questions": 2, "running": 3, "estop": True, "version": b["version"]}
    assert D.badge_from(None, None, {"running": "5"}, None)["running"] == 0
    assert D.badge_from([], [], {"running": True}, {})["running"] == 0


# =========================================================================== C7: User-Agent của app chỉ để log
async def test_app_user_agent_logged_not_rendered(env, caplog):
    D._SEEN_APP_VERSIONS.clear()
    ua = "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 CfharnessDesktop/0.1.0"
    with caplog.at_level(logging.INFO, logger="cfharness.web"):
        a = await env.client.get("/.well-known/cfharness.json", headers={"User-Agent": ua})
        b = await env.client.get("/.well-known/cfharness.json")
    assert a.content == b.content and a.status_code == b.status_code
    assert any("CfharnessDesktop/0.1.0" in r.getMessage() for r in caplog.records)
    await env.login()
    p1 = await env.client.get("/login", headers={"User-Agent": ua})
    p2 = await env.client.get("/login")
    assert p1.status_code == p2.status_code
    assert D.note_app_ua("Mozilla/5.0 Chrome/120") is None
    assert D.note_app_ua("x CfharnessDesktop/1.2.3-beta") == "1.2.3-beta"


# =========================================================================== Tải app desktop từ web của hub
async def test_settings_has_desktop_download_links(env):
    await env.login()
    r = await env.client.get("/settings/desktop")
    assert r.status_code == 200
    html = r.text
    assert 'id="desktop-app"' in html
    for url in D.DOWNLOADS.values():
        assert url in html
    # Link tải là hằng số trỏ repo app cố định, tên tệp cố định của mỗi release (không lấy từ cấu hình).
    assert D.DOWNLOADS["mac"].startswith("https://github.com/lephuong123/cfharness-desktop/releases/latest/download/")
    assert D.DOWNLOADS["mac"].endswith("/Cfharness-Desktop-macOS.dmg")
    assert D.DOWNLOADS["windows"].endswith("/Cfharness-Desktop-Windows-setup.exe")
    # Mọi trang đã đăng nhập có link nhanh trong menu người dùng (chân thanh bên); biểu tượng có trong sprite.
    home = await env.client.get("/activity")
    assert 'href="/settings/desktop"' in home.text
    sprite = await env.client.get("/static/icons.svg")
    assert 'id="i-download"' in sprite.text


# =========================================================================== C1: cfharness web --json [--ensure]
def _json_out(capsys) -> dict:
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1, out  # đúng MỘT dòng JSON
    return json.loads(out[0])


def test_json_web_not_running(hub_home, capsys):
    from cfharness.cli import main

    assert main(["web", "--json"]) == 1
    j = _json_out(capsys)
    assert j["ok"] is False and j["error"] == "web_not_running" and isinstance(j["message"], str) and j["message"]


def test_json_running_server_shape_no_secret(hub_home, capsys, monkeypatch):
    from cfharness.auth import load_token
    from cfharness.admin_overview import hub_id
    from cfharness.cli import main

    key = os.urandom(32)
    write_state(hub_home, host="127.0.0.1", port=8711, key=key)
    monkeypatch.setattr(D, "probe", lambda *a, **k: True)
    hub_home.config.write_text(yaml.safe_dump({"web": {"display_name": "Hub dự án A"}}))
    assert main(["web", "--json"]) == 0
    j = _json_out(capsys)
    raw = json.dumps(j)
    assert set(j) == {"ok", "origin", "login_url", "hub_id", "version", "name", "started"}
    assert j["ok"] is True and j["started"] is False
    assert j["origin"] == "http://cfharness.localhost:8711"
    assert j["login_url"].startswith("http://cfharness.localhost:8711/login?t=")
    assert check_login_token(key, j["login_url"].split("?t=", 1)[1])
    assert j["hub_id"] == hub_id(hub_home.home)
    assert j["name"] == "Hub dự án A"
    assert key.hex() not in raw  # khoá ký không bao giờ lộ
    try:
        tok = load_token(hub_home.socket)
    except Exception:  # noqa: BLE001
        tok = None
    assert not tok or tok not in raw
    state_path(hub_home).unlink()


def test_json_name_defaults_to_home_basename(hub_home):
    assert D.display_name(hub_home, {}) == hub_home.home.name
    assert D.display_name(hub_home, {"display_name": "  A‮B\nC  "}) == "AB C"
    assert len(D.display_name(hub_home, {"display_name": "x" * 200})) <= 60


def test_ensure_explicit_busy_port(hub_home, capsys):
    from cfharness.cli import main

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        assert main(["web", "--json", "--ensure", "--port", str(port)]) == 1
    j = _json_out(capsys)
    assert j == {"ok": False, "error": "port_busy", "message": j["message"]}


def test_preferred_port_per_hub(tmp_path, monkeypatch):
    from cfharness.paths import HubPaths

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    default = HubPaths.resolve(tmp_path / ".cfharness")
    assert D.preferred_port(default) == 8765
    a, b = HubPaths.resolve(tmp_path / "hub-a"), HubPaths.resolve(tmp_path / "hub-b")
    pa, pb = D.preferred_port(a), D.preferred_port(b)
    assert pa == D.preferred_port(HubPaths.resolve(tmp_path / "hub-a"))  # tất định
    assert 8770 <= pa < 9770 and 8770 <= pb < 9770 and pa != pb
    assert D.pick_port(a) not in (None, 8766)


def test_config_web_port_and_display_name_validation():
    from cfharness.config import DEFAULTS, validate_setting

    assert DEFAULTS["web"]["port"] is None and DEFAULTS["web"]["display_name"] is None
    validate_setting("web.port", 8711)
    validate_setting("web.port", None)
    validate_setting("web.display_name", "Hub của An")
    validate_setting("web.display_name", None)
    for bad in (80, 70000, "8711", True):
        with pytest.raises(ValueError):
            validate_setting("web.port", bad)
    for bad in ("", "a\nb", "x" * 61, "a‮b", 5):
        with pytest.raises(ValueError):
            validate_setting("web.display_name", bad)
    from cfharness.guard.integrity import SECURITY_KEYS

    assert "web" in SECURITY_KEYS  # web.port / web.display_name nằm trong nhánh web (kiểm toàn vẹn)


def test_agent_cannot_run_web_json(hub_home):
    from cfharness.guard.classifier import GuardContext, classify
    from cfharness.guard.policy import PolicyStore

    ps = PolicyStore(hub_home.home)
    ps.ensure()
    ctx = GuardContext(hub_home.home / "agents", ps.load(), run_since=time.time(), hub_socket=str(hub_home.socket), git_probe=False)
    for cmd in ("cfharness web --json", "cfharness web --json --ensure", "cfharness web --ensure --port 9000"):
        assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.05)


class _FakeProc:
    """Tiến trình giả của `spawn_web`: pid KHÁC pid ghi trong web.json (giống launcher venv trên Windows)."""

    pid = 999_999_999

    def __init__(self) -> None:
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code


def _fake_spawn(hub_home, calls, delay=0.3):
    import threading

    def spawn(paths, *, port, host, name, nonce=None):
        calls.append(nonce)
        proc = _FakeProc()

        def later():
            time.sleep(delay)  # server "đang khởi động" — lần --ensure thứ hai phải chờ khoá, không bật bản thứ hai
            write_state(paths, host=host, port=port, key=os.urandom(32), name=name, nonce=nonce)

        threading.Thread(target=later, daemon=True).start()
        return proc

    return spawn


def test_ensure_concurrent_calls_spawn_once(hub_home, capsys, monkeypatch):
    """Hai `--ensure` cùng lúc: khoá `auth/web.ensure.lock` → chỉ MỘT server được bật; lần sau dùng server đó (started=false, cùng khoá ký)."""
    import threading

    from cfharness.cli import main

    calls: list = []
    monkeypatch.setattr(D, "spawn_web", _fake_spawn(hub_home, calls))
    monkeypatch.setattr(D, "probe", lambda *a, **k: True)
    port = _free_port()
    codes: list = []
    ts = [threading.Thread(target=lambda: codes.append(main(["web", "--json", "--ensure", "--port", str(port), "--wait", "10"]))) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    out = [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]
    assert codes == [0, 0] and len(calls) == 1, (codes, calls, out)
    assert sorted(j["started"] for j in out) == [False, True]
    key = bytes.fromhex(read_state(hub_home)["key"])
    assert all(check_login_token(key, j["login_url"].split("?t=", 1)[1]) for j in out)  # cả hai link ký bằng khoá của server đang phục vụ
    assert D.ensure_lock_path(hub_home).exists() and (D.ensure_lock_path(hub_home).stat().st_mode & 0o777) == 0o600
    state_path(hub_home).unlink()


def test_ensure_started_by_nonce_not_pid(hub_home, capsys, monkeypatch):
    """`started` theo nonce trong web.json, không theo pid (Windows: python thật là con của launcher, pid khác proc.pid)."""
    from cfharness.cli import main

    calls: list = []
    monkeypatch.setattr(D, "spawn_web", _fake_spawn(hub_home, calls, delay=0.05))
    monkeypatch.setattr(D, "probe", lambda *a, **k: True)
    assert main(["web", "--json", "--ensure", "--port", str(_free_port())]) == 0
    j = _json_out(capsys)
    st = read_state(hub_home)
    assert j["started"] is True and st["pid"] != _FakeProc.pid and st["nonce"] == calls[0]
    assert st["nonce"] not in json.dumps(j)
    state_path(hub_home).unlink()


def test_wait_ready_ignores_foreign_state_while_own_proc_alive(hub_home, monkeypatch):
    """State của server KHÁC (khoá ký khác) không được tin khi tiến trình của mình còn đang khởi động; chỉ nhận sau khi nó thoát."""
    monkeypatch.setattr(D, "probe", lambda *a, **k: True)
    write_state(hub_home, host="127.0.0.1", port=8711, key=os.urandom(32), nonce="ab" * 16)
    proc = _FakeProc()
    assert D.wait_ready(hub_home, proc, 0.4, nonce="cd" * 16) is None
    proc.code = 1  # thua cuộc bind cổng → server kia đang phục vụ hub này
    st = D.wait_ready(hub_home, proc, 2.0, nonce="cd" * 16)
    assert st is not None and st["nonce"] == "ab" * 16
    state_path(hub_home).unlink()


def test_take_nonce_env(monkeypatch):
    monkeypatch.setenv(D.NONCE_ENV, "ab" * 16)
    assert D.take_nonce_env() == "ab" * 16 and D.NONCE_ENV not in os.environ
    monkeypatch.setenv(D.NONCE_ENV, "không-hợp-lệ")
    assert D.take_nonce_env() is None


@pytest.mark.skipif(WIN, reason="kiểm session POSIX; nhánh Windows dùng DETACHED_PROCESS (không fork)")
def test_ensure_starts_detached_server(hub_home):
    """`--ensure` khởi động server web TÁCH tiến trình, sống sau khi CLI thoát; lần sau chỉ trả link (started=false)."""
    hub_home.config.write_text(yaml.safe_dump({"runtime": {"kind": "fake"}}))
    port = _free_port()
    envv = {**os.environ, "CFHARNESS_HOME": str(hub_home.home)}
    cmd = [sys.executable, "-m", "cfharness", "web", "--json", "--ensure", "--port", str(port)]
    web_pid = None
    try:
        p = subprocess.run(cmd, env=envv, capture_output=True, text=True, timeout=90)
        lines = p.stdout.strip().splitlines()
        assert p.returncode == 0 and len(lines) == 1, (p.stdout, p.stderr, (hub_home.home / "logs" / "web.log").read_text() if (hub_home.home / "logs" / "web.log").exists() else "")
        j = json.loads(lines[0])
        assert j["ok"] is True and j["started"] is True and j["origin"] == f"http://cfharness.localhost:{port}"
        st = read_state(hub_home)
        assert st is not None and int(st["port"]) == port
        web_pid = int(st["pid"])
        assert os.getsid(web_pid) == web_pid  # session riêng → không chết theo terminal/CLI
        assert check_login_token(bytes.fromhex(st["key"]), j["login_url"].split("?t=", 1)[1])
        assert st["key"] not in p.stdout and st["key"] not in p.stderr
        assert D.probe("127.0.0.1", port, "cfharness.localhost")  # CLI đã thoát, server vẫn trả lời
        log = hub_home.home / "logs" / "web.log"
        assert log.exists() and "login?t=" not in log.read_text()  # --no-link: link không vào log
        assert (log.stat().st_mode & 0o777) == 0o600
        p2 = subprocess.run(cmd, env=envv, capture_output=True, text=True, timeout=60)
        j2 = json.loads(p2.stdout.strip())
        assert p2.returncode == 0 and j2["ok"] is True and j2["started"] is False and j2["origin"] == j["origin"]
        assert j2["login_url"] != j["login_url"]  # link một lần mới mỗi lần
        p3 = subprocess.run([sys.executable, "-m", "cfharness", "web", "--json"], env=envv, capture_output=True, text=True, timeout=60)
        assert json.loads(p3.stdout.strip())["ok"] is True
    finally:
        if web_pid:
            _kill(web_pid)
        subprocess.run([sys.executable, "-m", "cfharness", "daemon", "stop"], env=envv, capture_output=True, timeout=60)
    assert read_state(hub_home) is None


# =========================================================================== C4: cầu nối giọng nói native trong voice.js
NODE = shutil.which("node")


def _harness(scenario: str) -> dict:
    p = subprocess.run([NODE, str(HARNESS), str(VOICE_JS), scenario], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stdout + p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="cần node để chạy voice.js với DOM giả")
def test_voice_native_bridge_dictation():
    r = _harness("native")
    assert r["engine"] == "native" and r["micHidden"] is False and r["waveHidden"] is False
    assert r["startArg"] == {"lang": "vi-VN", "interim": True}
    assert r["registered"] == ["end", "error", "result"]
    assert r["micOn"] is True
    assert r["interimValue"] == "xin chào"
    assert r["mixedValue"] == "xin chào bạn ơi"
    assert r["voiceField"] == "dictation"  # bất biến: tin đọc chép mang voice=dictation (daemon không duyệt bằng giọng nói)
    assert r["stopCalled"] is True and r["micOnAfterStop"] is False
    assert r["afterLate"] == r["finalValue"]  # sự kiện muộn sau khi dừng bị bỏ


@pytest.mark.skipif(NODE is None, reason="cần node để chạy voice.js với DOM giả")
def test_voice_native_bridge_error_and_fallbacks():
    r = _harness("error")
    assert r["errShown"] is True and "quyền micro" in r["errText"] and r["micOnAfterError"] is False
    assert _harness("unavailable") == {"engine": "none", "micHidden": True, "waveHidden": True, "srStarted": 0}  # available()=false, không SR
    assert _harness("none") == {"engine": "none", "micHidden": True, "waveHidden": True, "srStarted": 0}  # như cũ
    assert _harness("websr")["engine"] == "web"  # không có cầu nối → Web Speech API như cũ


def test_voice_js_static_invariants():
    js = VOICE_JS.read_text(encoding="utf-8")
    assert "window.cfhNative" in js and "innerHTML" not in js and "eval(" not in js
    assert js.index("cfhNative") < js.index("function boot(")  # dò cầu nối trước khi gắn nút
    assert 'inp.value = "conversation"' in js and 'inp.value = "dictation"' in js
