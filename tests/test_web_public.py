"""Web public qua tunnel TLS + đăng nhập TOTP + LaunchDaemon + CLAUDE_CODE_OAUTH_TOKEN (docs/dev-notes/web-public.md).

Daemon IN-PROCESS + FakeRuntime, app Starlette qua ASGITransport (client = 127.0.0.1 như tunnel cục bộ). Không gọi `claude`,
không mạng, không launchctl/sudo thật.
"""

from __future__ import annotations

import asyncio
import os
import plistlib
import sys
import time
from pathlib import Path

import httpx2 as httpx
import pytest

from cfharness.daemon import Daemon
from cfharness.runtime import FakeRuntime
from cfharness.web.app import create_app
from cfharness.web.public import LoginLimiter, client_ip, parse_public_origin, public_hosts
from cfharness.web.security import COOKIE, WebAuth, login_url
from cfharness.web.totp import TotpStore, hotp, match_step, otpauth_uri, totp

ISOLATED = Path(__file__).parent / "fixtures" / "claude-hub-isolated-run.jsonl"
PORT = 8765
ORIGIN = "https://an.example.com"
LOCAL = f"http://cfharness.localhost:{PORT}"


class Env:
    def __init__(self, d: Daemon, task: asyncio.Task, paths) -> None:
        self.d, self.task, self.paths, self.hub = d, task, paths, d.hub
        self.clients: list[httpx.AsyncClient] = []

    def app(self, *, public: str | None = ORIGIN, limiter: LoginLimiter | None = None, ip_header: str = "x-forwarded-for", auth: WebAuth | None = None):
        return create_app(self.paths, auth=auth or WebAuth(), port=PORT, public_origin=public, limiter=limiter, ip_header=ip_header)

    def client(self, app, base: str = ORIGIN) -> httpx.AsyncClient:
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base)
        self.clients.append(c)
        return c

    def audit(self, kind: str) -> list[dict]:
        rows = self.hub.store.conn.execute("SELECT kind, actor, channel, data FROM audit_log WHERE kind=? ORDER BY id", (kind,)).fetchall()
        return [dict(r) for r in rows]


@pytest.fixture
async def env(hub_home):
    d = Daemon(hub_home, runtime=FakeRuntime(ISOLATED))
    task = asyncio.create_task(d.serve())
    for _ in range(200):
        if hub_home.socket.exists():
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    e = Env(d, task, hub_home)
    yield e
    for c in e.clients:
        await c.aclose()
    d.stopping.set()
    try:
        await asyncio.wait_for(task, 20)
    except Exception:
        pass


def _code(paths) -> str:
    st = TotpStore(paths)
    return totp(st._read("totp.json")["secret"])


ORIG_HDR = {"origin": ORIGIN}


# ============================================================================ cấu hình
def test_public_origin_validation_and_protection(hub_home):
    assert parse_public_origin("https://An.Example.com/") == "https://an.example.com"
    assert parse_public_origin("https://an.example.com:8443") == "https://an.example.com:8443"
    assert parse_public_origin(None) is None and parse_public_origin("") is None
    for bad in ("http://an.example.com", "https://an.example.com/x", "https://u@an.example.com", "https://127.0.0.1",
                "https://1.2.3.4", "https://x.localhost", "https://an.example.com?q=1", "ftp://a.b", "https://localhost"):
        with pytest.raises(ValueError):
            parse_public_origin(bad)
    assert public_hosts("https://an.example.com") == {"an.example.com", "an.example.com:443"}
    from cfharness import auth
    from cfharness.config import HubConfig
    from cfharness.guard.integrity import SECURITY_KEYS
    from cfharness.web.settings_schema import BY_KEY

    cfg = HubConfig(hub_home.config)
    with pytest.raises(ValueError):
        cfg.set("web.public_origin", "http://an.example.com")
    with pytest.raises(ValueError):
        cfg.set("web.session_max_age_s", 5)
    with pytest.raises(ValueError):
        cfg.set("web.client_ip_header", "x-real-ip")
    assert cfg.set("web.public_origin", "https://an.example.com") == "https://an.example.com"
    # chỉ người: nhánh web kiểm toàn vẹn, config.set HUMAN_ONLY, điều phối viên không có config.set, web Cài đặt không sửa được
    assert "web" in SECURITY_KEYS and "config.set" in auth.HUMAN_ONLY and "config.set" not in auth.MANAGER_ALLOWED
    assert not any(str(k).startswith("web.") for k in BY_KEY)


def test_login_url_and_session_lifetime():
    assert login_url("cfharness.localhost", 8765, "TOK", "https://an.example.com") == "https://an.example.com/login?t=TOK"
    assert login_url("cfharness.localhost", 8765, "TOK") == "http://cfharness.localhost:8765/login?t=TOK"
    a = WebAuth(max_s=600, idle_s=3600)
    s = a.new_session(now=1000.0)
    assert a.get(s.sid, now=1500.0) is not None
    assert a.get(s.sid, now=1700.0) is None  # quá tuổi tối đa dù vẫn thao tác


# ============================================================================ TOTP thuần
def test_totp_rfc6238_vectors_and_replay():
    # RFC 6238 phụ lục B (SHA1, khoá ASCII "12345678901234567890"), 8 chữ số → so 6 chữ số cuối
    import base64

    sec = base64.b32encode(b"12345678901234567890").decode()
    assert hotp(sec, 59 // 30, 8) == "94287082"
    assert hotp(sec, 1111111109 // 30, 8) == "07081804"
    assert totp(sec, 1234567890) == "005924"
    now = 1_000_000.0
    c = totp(sec, now)
    st = match_step(sec, c, now=now)
    assert st == int(now // 30)
    assert match_step(sec, c, now=now, after_step=st) is None  # dùng lại trong cửa sổ → từ chối
    assert match_step(sec, totp(sec, now - 30), now=now) is not None  # lệch 1 bước
    assert match_step(sec, totp(sec, now - 90), now=now) is None
    assert match_step(sec, "abc", now=now) is None
    assert otpauth_uri(sec, "an.example.com").startswith("otpauth://totp/cfharness%3Aan.example.com?secret=")


def test_totp_store_files_private(hub_home):
    st = TotpStore(hub_home)
    secret, token = st.enroll(activate=True)
    p = hub_home.home / "auth" / "totp.json"
    if sys.platform != "win32":
        assert (p.stat().st_mode & 0o777) == 0o600 and (p.parent.stat().st_mode & 0o777) == 0o700
    assert token not in (hub_home.home / "auth" / "totp_enroll.json").read_text()  # link chỉ lưu dạng băm
    assert st.verify(totp(secret)) and not st.verify(totp(secret))  # chống dùng lại
    assert st.disable() and not st.enrolled()


# ============================================================================ Host / Origin / cookie
async def test_public_host_only_when_configured(env):
    local_only = env.client(env.app(public=None))
    r = await local_only.get("/login")
    assert r.status_code == 421  # Host an.example.com chưa cấu hình → chống DNS rebinding giữ nguyên
    pub = env.client(env.app())
    r = await pub.get("/login")
    assert r.status_code == 200 and "cfharness web --open" not in r.text
    assert r.headers.get("strict-transport-security", "").startswith("max-age=")
    r = await pub.get("/login", headers={"host": "evil.example.com"})
    assert r.status_code == 421
    # host cục bộ vẫn chạy như cũ, không HSTS
    loc = env.client(env.app(), base=LOCAL)
    r = await loc.get("/login")
    assert r.status_code == 200 and "strict-transport-security" not in r.headers


async def test_totp_login_secure_cookie_origin_and_replay(env):
    TotpStore(env.paths).enroll(activate=True)
    app = env.app()
    c = env.client(app)
    r = await c.post("/login", data={"code": "000000"}, headers={"origin": "https://evil.example.com"})
    assert r.status_code == 403
    code = _code(env.paths)
    r = await c.post("/login", data={"code": code}, headers=ORIG_HDR)
    assert r.status_code == 303 and r.headers["location"].startswith("/#k=")
    sc = r.headers["set-cookie"]
    assert COOKIE in sc and "secure" in sc.lower() and "httponly" in sc.lower() and "samesite=strict" in sc.lower()
    # dùng lại cùng mã (trong cửa sổ) → từ chối
    c2 = env.client(app)
    r = await c2.post("/login", data={"code": code}, headers=ORIG_HDR)
    assert r.status_code == 401
    assert await _until(lambda: env.audit("web.login_failed")) and env.audit("web.login")
    # host cục bộ: cookie KHÔNG Secure (http://cfharness.localhost)
    loc = env.client(app, base=LOCAL)
    r = await loc.get("/login?t=" + app.state.web.auth.mint())
    assert r.status_code == 303 and "secure" not in r.headers["set-cookie"].lower()


async def test_one_time_link_still_works_on_public_host(env):
    app = env.app()
    c = env.client(app)
    r = await c.get("/login?t=" + app.state.web.auth.mint())
    assert r.status_code == 303 and "secure" in r.headers["set-cookie"].lower()


async def _until(pred, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


# ============================================================================ giới hạn tốc độ
async def test_rate_limit_per_ip_and_lockout(env):
    TotpStore(env.paths).enroll(activate=True)
    lim = LoginLimiter(ip_max_failures=3, global_max_failures=100, window_s=600, lockout_s=600)
    app = env.app(limiter=lim)
    c = env.client(app)
    a_ip = {**ORIG_HDR, "x-forwarded-for": "203.0.113.7"}
    for _ in range(2):
        assert (await c.post("/login", data={"code": "000000"}, headers=a_ip)).status_code == 401
    r = await c.post("/login", data={"code": "000000"}, headers=a_ip)
    assert r.status_code == 429  # lần sai thứ 3 → khoá IP
    r = await c.post("/login", data={"code": _code(env.paths)}, headers=a_ip)
    assert r.status_code == 429  # đang khoá: mã ĐÚNG cũng không vào
    # IP khác (tunnel cục bộ + public_origin → tin XFF phần tử cuối) vẫn đăng nhập được
    r = await c.post("/login", data={"code": _code(env.paths)}, headers={**ORIG_HDR, "x-forwarded-for": "1.1.1.1, 198.51.100.9"})
    assert r.status_code == 303
    assert await _until(lambda: env.audit("web.login_locked"))
    import json

    assert any(json.loads(x["data"]).get("ip") == "203.0.113.7" for x in env.audit("web.login_failed"))


async def test_global_lockout_across_ips(env):
    TotpStore(env.paths).enroll(activate=True)
    lim = LoginLimiter(ip_max_failures=100, global_max_failures=4, window_s=600, lockout_s=600)
    c = env.client(env.app(limiter=lim))
    for i in range(4):
        await c.post("/login", data={"code": "000000"}, headers={**ORIG_HDR, "x-forwarded-for": f"198.51.100.{i}"})
    r = await c.post("/login", data={"code": _code(env.paths)}, headers={**ORIG_HDR, "x-forwarded-for": "192.0.2.200"})
    assert r.status_code == 429  # kẻ đổi IP liên tục vẫn bị khoá toàn cục


async def test_xff_ignored_when_not_public(env):
    TotpStore(env.paths).enroll(activate=True)
    lim = LoginLimiter(ip_max_failures=2, global_max_failures=100)
    c = env.client(env.app(public=None, limiter=lim), base=LOCAL)
    h = {"origin": LOCAL}
    await c.post("/login", data={"code": "000000"}, headers={**h, "x-forwarded-for": "203.0.113.1"})
    await c.post("/login", data={"code": "000000"}, headers={**h, "x-forwarded-for": "203.0.113.2"})
    # không public → XFF bị bỏ qua: cả hai lần tính cho cùng peer 127.0.0.1 → peer bị khoá dù "đổi IP"
    r = await c.post("/login", data={"code": _code(env.paths)}, headers={**h, "x-forwarded-for": "203.0.113.3"})
    assert r.status_code == 429


def test_client_ip_rules():
    lo = {"client": ("127.0.0.1", 5000)}
    far = {"client": ("10.0.0.5", 5000)}
    h = {"x-forwarded-for": "6.6.6.6, 203.0.113.9", "cf-connecting-ip": "198.51.100.1"}
    assert client_ip(lo, h, public=False) == "127.0.0.1"
    assert client_ip(lo, h, public=True) == "203.0.113.9"  # phần tử CUỐI (proxy gần nhất thêm)
    assert client_ip(lo, h, public=True, header="cf-connecting-ip") == "198.51.100.1"
    assert client_ip(lo, h, public=True, header="none") == "127.0.0.1"
    assert client_ip(far, h, public=True) == "10.0.0.5"  # peer không phải loopback → không tin header
    assert client_ip(lo, {"x-forwarded-for": "not-an-ip"}, public=True) == "127.0.0.1"


def test_limiter_day_cap():
    t = [0.0]
    lim = LoginLimiter(ip_max_failures=1000, global_max_failures=1000, window_s=60, lockout_s=60, day_max_failures=5, day_lockout_s=86400,
                       clock=lambda: t[0])
    for i in range(5):
        t[0] += 120  # ngoài cửa sổ ngắn
        lim.fail(f"ip{i}")
    assert lim.locked("x") is not None and lim.locked("x")[0] > 80000


# ============================================================================ đăng ký bằng link một lần
async def test_enroll_flow_one_time(env):
    store = TotpStore(env.paths)
    _, token = store.enroll()
    assert not store.enrolled()
    app = env.app()
    c = env.client(app)
    r = await c.get(f"/enroll?t={token}")
    assert r.status_code == 200 and "otpauth://" not in r.text  # GET (xem trước link) không lộ bí mật, không tiêu link
    r = await c.get("/enroll?t=sai")
    assert r.status_code == 401
    r = await c.post("/enroll", data={"t": token, "action": "show"}, headers=ORIG_HDR)
    assert r.status_code == 200 and "otpauth://" in r.text
    # mã QR để quét bằng Authenticator: SVG nội tuyến do server sinh, không style/script (CSP 'self'), trang không cache
    assert '<div class="totp-qr"><svg role="img"' in r.text and "<path" in r.text
    qr = r.text.split('<div class="totp-qr">', 1)[1].split("</div>", 1)[0]
    assert "style=" not in qr and "<script" not in qr and "href" not in qr
    assert r.headers.get("cache-control") == "no-store"
    r2 = await c.post("/enroll", data={"t": token, "action": "show"}, headers=ORIG_HDR)
    assert r2.status_code == 401 and "otpauth://" not in r2.text  # chỉ hiện MỘT lần
    secret = store._read("totp_enroll.json")["secret"]
    r = await c.post("/enroll", data={"t": token, "action": "confirm", "code": "000000" if totp(secret) != "000000" else "111111"}, headers=ORIG_HDR)
    assert r.status_code == 401 and not store.enrolled()
    r = await c.post("/enroll", data={"t": token, "action": "confirm", "code": totp(secret)}, headers=ORIG_HDR)
    assert r.status_code == 303 and "secure" in r.headers["set-cookie"].lower()
    assert store.enrolled() and store.pending(token) is None  # link đã tiêu
    r = await c.post("/enroll", data={"t": token, "action": "show"}, headers={**ORIG_HDR, "origin": "https://evil.example.com"})
    assert r.status_code == 403


# ============================================================================ classifier: lệnh chỉ người
def test_agent_cannot_run_admin_or_totp(hub_home):
    from cfharness.guard.classifier import GuardContext, classify
    from cfharness.guard.policy import PolicyStore

    ps = PolicyStore(hub_home.home)
    ps.ensure()
    ctx = GuardContext(hub_home.home / "agents", ps.load(), run_since=time.time(), hub_socket=str(hub_home.socket), git_probe=False)
    for cmd in ("cfharness web totp enroll", "cfharness web totp status", "cfharness admin install-daemon --user an",
                "cfharness admin install-daemon --user an --write", "cfharness config set web.public_origin https://x.example.com"):
        assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd


# ============================================================================ LaunchDaemon (Q3)
def test_launchdaemon_plist(tmp_path):
    from cfharness.systems.autostart import LaunchDaemonPlan

    uh = tmp_path / "Users" / "an"
    plan = LaunchDaemonPlan("an", user_home=uh, web_port=8711, directory=tmp_path / "LD")
    assert plan.path() == tmp_path / "LD" / "com.cfharness.daemon.an.plist"
    d = plistlib.loads(plan.plist())
    home = str(uh / ".cfharness")
    assert d["UserName"] == "an" and d["Label"] == "com.cfharness.daemon.an"
    assert d["EnvironmentVariables"]["CFHARNESS_HOME"] == home and d["EnvironmentVariables"]["HOME"] == str(uh)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in d["EnvironmentVariables"]  # token chỉ ở secret store, không trong plist
    assert d["KeepAlive"] == {"SuccessfulExit": False} and d["RunAtLoad"] is True
    assert d["StandardOutPath"].startswith(home + "/logs/") and d["ProgramArguments"][-4:] == ["daemon", "run", "--home", home]
    w = plistlib.loads(plan.plist("web"))
    assert w["Label"] == "com.cfharness.web.an" and "--no-link" in w["ProgramArguments"] and "8711" in w["ProgramArguments"]
    cmds = plan.commands()
    assert any("launchctl bootstrap system" in c and "daemon.an.plist" in c for c in cmds)
    assert any("web.an.plist" in c for c in cmds)
    for bad in ("root", "An", "a b", "../x", ""):
        with pytest.raises(ValueError):
            LaunchDaemonPlan(bad, user_home=uh)


def test_launchdaemon_apply_needs_root_and_uses_runner(tmp_path):
    from cfharness.systems.autostart import LaunchDaemonPlan

    calls: list[list[str]] = []

    def runner(argv, timeout):
        calls.append(argv)
        return 0, "", ""

    ld = tmp_path / "LD"
    ld.mkdir()
    plan = LaunchDaemonPlan("an", user_home=tmp_path / "an", directory=ld, runner=runner)
    with pytest.raises(PermissionError):
        plan.apply(euid=501)
    assert not calls and not list(ld.iterdir())
    done = plan.apply(euid=0)
    p = ld / "com.cfharness.daemon.an.plist"
    assert p.exists() and (p.stat().st_mode & 0o777) == 0o644 and done
    assert ["/bin/launchctl", "bootstrap", "system", str(p)] in calls
    assert calls[0][:3] == ["/usr/bin/sudo", "-u", "an"]
    plan.remove(euid=0)
    assert not p.exists()


def test_admin_cli_prints_by_default(hub_home, capsys, monkeypatch):
    import cfharness.systems.autostart as A
    from cfharness.cli import main

    monkeypatch.setattr(A, "user_home_of", lambda u: Path("/Users") / u)
    rc = main(["admin", "install-daemon", "--user", "an", "--web-port", "8711"])
    out = capsys.readouterr().out
    assert rc == 0 and "<key>UserName</key>" in out and "sudo launchctl bootstrap system" in out


# ============================================================================ CLAUDE_CODE_OAUTH_TOKEN
TOKEN = "sk-ant-oat01-" + "Z" * 40


def test_child_env_injects_token_and_still_strips(hub_home):
    from cfharness.runtime.claude_cli import ClaudeCliRuntime, child_env
    from cfharness.secrets import SecretStore

    cfg = {"env_strip": ["CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_OAUTH_TOKEN"], "env_extra": {"MCP_TOOL_TIMEOUT": "1"}}
    base = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s", "PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "từ-env-daemon"}
    env = child_env(cfg, base)
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_SESSION_ID" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    env = child_env(cfg, base, oauth_token=TOKEN)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN and "CLAUDE_CODE_SESSION_ID" not in env and env["MCP_TOOL_TIMEOUT"] == "1"
    ss = SecretStore(hub_home.home / "secrets", env_fallback=True)
    rt = ClaudeCliRuntime({"env_strip": ["CLAUDE_CODE_SESSION_ID"]}, secrets=ss)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in rt.env() or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    ss.set("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    assert rt.env()["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN

    class Spec:
        env_extra = {"CLAUDE_CODE_OAUTH_TOKEN": "agent-override", "X": "1"}

    e = rt._run_env(Spec())
    assert e["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN and e["X"] == "1"  # env_extra của run không ghi đè token của hub


def test_token_redacted_everywhere(hub_home):
    from cfharness import redact
    from cfharness.secrets import SecretStore

    SecretStore(hub_home.home / "secrets").set("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    assert TOKEN not in redact.mask_values(f"env CLAUDE_CODE_OAUTH_TOKEN={TOKEN} end")
    assert TOKEN.encode() not in redact.mask_bytes(f'{{"x":"{TOKEN}"}}'.encode())
    assert TOKEN not in str(redact.redact({"cmd": f"echo {TOKEN}"}))


async def test_oneshot_and_main_runtime_get_token_not_argv(hub_home, monkeypatch):
    """oneshot (judge/curator/graph): token vào env tiến trình con, không vào argv."""
    from cfharness.hub import Hub
    from cfharness.runtime import ClaudeCliRuntime, OneShotRequest

    hub = Hub(hub_home)
    hub.secrets.set("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    rt = hub.runs.runtime() if hub.config.get("runtime.kind") == "claude" else None
    if rt is not None:
        assert isinstance(rt, ClaudeCliRuntime) and rt.env()["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    seen: dict = {}

    class P:
        returncode = 0

        async def communicate(self, data=None):
            return b'{"type":"result","structured_output":{"ok":1},"total_cost_usd":0}', b""

    async def fake_exec(*argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env")
        return P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    r = ClaudeCliRuntime({**(hub.config.get("runtime.claude") or {}), "binary": "fake-claude"}, secrets=hub.secrets)
    await r.oneshot(OneShotRequest(prompt="hi", schema={"type": "object"}, system="s", model="haiku", budget_usd=0.01, timeout_s=5, label="t"))
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert all(TOKEN not in str(a) for a in seen["argv"])
    hub.store.conn.close()


def test_takeover_env_reads_local_secret(hub_home):
    from cfharness.secrets import SecretStore
    from cfharness.tui.takeover import takeover_env

    info = {"env_strip": ["CLAUDECODE"]}
    assert takeover_env(info, hub_home).get("CLAUDE_CODE_OAUTH_TOKEN") == os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    SecretStore(hub_home.home / "secrets").set("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    assert takeover_env(info, hub_home)["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN


def test_totp_qr_encodes_exact_uri():
    """QR phải mã hoá ĐÚNG URI otpauth (so khớp ma trận segno sinh lại từ URI với SVG đã vẽ)."""
    import re

    import segno

    from cfharness.web.totp import qr_svg

    uri = otpauth_uri("JBSWY3DPEHPK3PXP", "an.example.com")
    svg = qr_svg(uri)
    assert svg and svg.startswith('<svg role="img"') and "viewBox" in svg and "width=" not in svg.split(">", 1)[0]
    q = segno.make(uri, error="m")
    n = len(q.matrix)
    dark = set()  # dựng lại ô tối từ path "M x y h a m b c ..." (segno vẽ theo hàng, nét ngang)
    path = re.search(r'class="qrline"[^>]* d="([^"]+)"', svg).group(1)
    x = y = 0.0
    for cmd, a, b in re.findall(r"([Mmh])(-?[\d.]+)(?:\s(-?[\d.]+))?", path):
        a = float(a)
        if cmd == "M":
            x, y = a, float(b)
        elif cmd == "m":
            x, y = x + a, y + float(b)
        else:
            for i in range(int(a)):
                dark.add((int(y - 0.5) - 4, int(x + i) - 4))
            x += a
    want = {(r, c) for r in range(n) for c in range(n) if q.matrix[r][c] & 1}
    assert dark == want
