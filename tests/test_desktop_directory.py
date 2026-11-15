"""App desktop — phía hub phần 2 (docs/dev-notes/desktop-app.md): C5 directory hub + C6 chặn thư mục dữ liệu app.

C5: SPEC admin_overview schema 3 (`public_origin`, `display_name`), `admin_directory.py` (lọc mục, token băm), RPC `admin.directory*`,
route công khai có token `/.well-known/cfharness-directory?t=` (sai → 404, sai liên tục → 429), CLI `cfharness admin directory …`.
C6: `desktop_app.py` + sandbox denyRead/denyWrite + `permissions.deny` (mọi hub, kể cả trust_full) + classifier `desktop_app_data`.

Không gọi `claude` (FakeRuntime, daemon + web in-process); nhóm cfharness-admin giả bằng fixture `as_admin`.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from cfharness import admin_directory as ad
from cfharness import admin_overview as ao
from cfharness import auth
from cfharness.desktop_app import APP_ID, app_data_dirs, is_app_data, shell_mentions
from cfharness.guard.classifier import GuardContext, classify
from cfharness.guard.policy import PolicyStore

from test_web import env  # noqa: F401  (fixture daemon + web in-process)

WIN = sys.platform == "win32"
ME = ao.os_user()
SECRET = "TOPSECRET-sk-ant-zzz-du-an-B"


def _status(**over) -> dict:
    d = {"schema": 3, "user": ME, "hub_id": "0123456789abcdef", "version": "0.2.10", "generated_at": "2026-11-15T08:00:00Z",
         "generated_ts": time.time(), "interval_s": 60, "daemon": {"up": True}, "cost_usd": {"today": 1.5},
         "public_origin": "https://an.example.com", "display_name": "Hub của An", "summary": SECRET}
    d.update(over)
    return d


def _put(status_dir: Path, hid: str, data) -> None:
    d = status_dir / ME
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{hid}.json").write_text(data if isinstance(data, str) else json.dumps(data))


def _strings(obj, out=None):
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(k)
            _strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _strings(v, out)
    elif isinstance(obj, str):
        out.append(obj)
    return out


# =========================================================================== C5: SPEC schema 3
def test_spec_schema3_origin_and_name_validated():
    assert ao.SCHEMA == 3 and ao.SCHEMAS_READ == (1, 2, 3)
    good = ao.sanitize(_status())
    assert good["public_origin"] == "https://an.example.com" and good["display_name"] == "Hub của An"
    assert "summary" not in good and SECRET not in json.dumps(good)
    for bad in ("http://an.example.com", "https://10.0.0.5", "https://an.example.com/path", "https://u:p@an.example.com",
                "https://an.example.com?x=1", "https://cfharness.localhost", "javascript:alert(1)", 42, "", "https://" + "a" * 300 + ".com"):
        assert "public_origin" not in ao.sanitize(_status(public_origin=bad)), bad
    # chưa chuẩn hoá (chữ hoa / :443) → bỏ: bộ đọc không "sửa hộ" file lạ
    assert "public_origin" not in ao.sanitize(_status(public_origin="https://AN.example.com"))
    # tên: một dòng, bỏ điều khiển/bidi/zero-width, ≤ 60
    n = ao.sanitize(_status(display_name="An\u202e\u200b\x1b[31m\nhub"))["display_name"]
    assert n == "An hub" and "\u202e" not in n and "\x1b" not in n
    assert len(ao.sanitize(_status(display_name="x" * 100))["display_name"]) == 60
    assert "display_name" not in ao.sanitize(_status(display_name="   "))
    assert "display_name" not in ao.sanitize(_status(display_name=["a"]))
    # bộ đọc vẫn nhận schema 1/2 (hub chưa nâng cấp) — chỉ là không có origin
    for s in (1, 2):
        assert ao.sanitize(_status(schema=s, public_origin=None))["schema"] == s


async def test_collect_exports_public_origin_and_display_name(env):
    hub = env.hub
    data = ao.collect(hub)
    # không có trường schema 3 → ghi schema 2: admin hub chưa nâng cấp (SCHEMAS_READ = 1, 2) vẫn đọc được
    assert data["schema"] == 2 and "public_origin" not in data and "display_name" not in data
    hub.config.set("web.public_origin", "https://an.example.com")
    hub.config.set("web.display_name", "Hub An")
    data = ao.collect(hub)
    assert data["schema"] == 3
    assert data["public_origin"] == "https://an.example.com" and data["display_name"] == "Hub An"
    assert ao.sanitize(data) == data
    hub.config.set("web.display_name", None)
    hub.config.set("web.public_origin", "https://An.Example.com/")  # hợp lệ nhưng chưa chuẩn → xuất dạng chuẩn, không bị loại khỏi danh mục
    data = ao.collect(hub)
    assert data["schema"] == 3 and data["public_origin"] == "https://an.example.com" and "display_name" not in data


# =========================================================================== C5: lọc mục (thuần)
def test_entry_validation_pure():
    ok = {"id": "0123456789abcdef", "name": "Hub An", "user": ME if len(ME) <= 32 else "an", "origin": "https://an.example.com:8443"}
    e = ad.entry(ok)
    assert e is not None and set(e) == {"id", "name", "user", "origin"} and e["origin"] == "https://an.example.com:8443"
    assert ad.entry({**ok, "id": "0123456789abcdef0"}) is None  # > 16 hex
    assert ad.entry({**ok, "id": "XYZ"}) is None
    assert ad.entry({**ok, "user": "a" * 33}) is None
    assert ad.entry({**ok, "user": "an\nb"}) is None
    for o in ("http://a.example.com", "https://1.2.3.4", "https://a.example.com/x", "ftp://a.example.com", None):
        assert ad.entry({**ok, "origin": o}) is None, o
    assert ad.entry({**ok, "name": None})["name"] == ok["user"]  # thiếu tên → dùng user
    assert ad.entry({**ok, "name": "A\u2066B\u2069\tC"})["name"] == "AB C"
    assert ad.entry({**ok, "extra": SECRET}) == ad.entry(ok)  # khoá lạ không lọt ra
    doc = ad.document("Máy\x07 văn phòng", [ok] * 250)
    assert set(doc) == {"kind", "schema", "name", "generated_at", "hubs"}
    assert doc["kind"] == "cfharness.directory" and doc["schema"] == 1 and doc["name"] == "Máy văn phòng"
    assert len(doc["hubs"]) == ad.HUBS_MAX
    assert ad.document(None, [])["name"] == "cfharness"


@pytest.mark.skipif(WIN, reason="quyền POSIX/chủ file")
def test_entries_from_overview_filters_malformed(tmp_path):
    st = tmp_path / "status"
    _put(st, "0000000000000001", _status(hub_id="0000000000000001"))
    _put(st, "0000000000000002", _status(hub_id="0000000000000002", public_origin="https://b.example.com", display_name=None))
    _put(st, "0000000000000003", _status(hub_id="0000000000000003", public_origin="http://c.example.com"))  # origin sai → không có trong danh mục
    _put(st, "0000000000000004", _status(hub_id="0000000000000004", schema=2, public_origin=None))  # hub cũ không có origin
    _put(st, "0000000000000005", "{not json")
    _put(st, "0000000000000006", _status(hub_id="ffffffffffffffff"))  # tên file ≠ hub_id → owner_mismatch
    _put(st, "0000000000000007", _status(hub_id="0000000000000007"))  # trùng origin với 1 → bỏ
    _put(st, "0000000000000008", _status(hub_id="0000000000000008", schema=99, public_origin="https://h.example.com"))
    _put(st, "0123456789abcdef0123", _status(hub_id="0123456789abcdef0123", public_origin="https://long.example.com"))  # id > 16
    ov = ao.read_dir(st)
    hubs, dropped = ad.entries_from_overview(ov)
    assert [h["id"] for h in hubs] == ["0000000000000001", "0000000000000002"]
    assert hubs[1]["name"] == ME[:60]
    assert dropped == 2  # trùng origin + id quá dài
    blob = json.dumps(hubs)
    assert SECRET not in blob and "summary" not in blob and "cost" not in blob
    for h in hubs:
        assert set(h) == {"id", "name", "user", "origin"}


# =========================================================================== C5: token
@pytest.mark.skipif(WIN, reason="quyền POSIX")
def test_token_hashed_0600_rotate(hub_home):
    assert not ad.verify_token(hub_home, "x" * 43) and ad.read_token_state(hub_home) is None
    tok, st = ad.write_token(hub_home)
    p = ad.token_path(hub_home)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    raw = p.read_text()
    assert tok not in raw and st["sha256"] in raw and st["rev"] == 1
    assert ad.verify_token(hub_home, tok)
    for bad in ("", None, tok[:-1] + ("A" if tok[-1] != "A" else "B"), tok + "x", "../../etc", 123):
        assert not ad.verify_token(hub_home, bad), bad
    tok2, st2 = ad.write_token(hub_home)
    assert st2["rev"] == 2 and ad.verify_token(hub_home, tok2) and not ad.verify_token(hub_home, tok)


def test_config_and_rpc_registration():
    from cfharness.config import validate_setting
    from cfharness.guard.integrity import SECURITY_KEYS

    assert "admin_directory" in SECURITY_KEYS
    validate_setting("admin_directory.name", "Văn phòng Hà Nội")
    validate_setting("admin_directory.name", None)
    for bad in ("a\nb", "x" * 61, "", "a\u202eb", 5):
        with pytest.raises(ValueError):
            validate_setting("admin_directory.name", bad)
    with pytest.raises(ValueError):
        validate_setting("admin_directory.other", 1)
    for m in (*ad.READ_DIRECTORY, *ad.MUTATING_DIRECTORY):
        assert m in auth.HUMAN_ONLY, m
        assert m not in auth.MANAGER_ALLOWED, m
    assert set(ad.MUTATING_DIRECTORY) <= auth.all_mutating()
    assert not set(ad.READ_DIRECTORY) & auth.all_mutating()  # đọc (kể cả serve có token) không vào audit
    ctx = GuardContext(cwd=Path("/tmp"), policy=PolicyStore(Path("/tmp/cfh-test-x")).load())
    for cmd in ("cfharness admin directory token", "cfharness admin directory token --rotate", "cfharness admin directory export --out /tmp/d.json",
                "cfharness admin directory status"):
        assert classify("Bash", {"command": cmd}, ctx).forbidden, cmd


# =========================================================================== C5: RPC + route (daemon + web in-process)
async def _setup_dir(env, tmp_path):
    st = tmp_path / "status"
    env.hub.config.set("admin_export.dir", str(st))
    _put(st, "0000000000000001", _status(hub_id="0000000000000001"))
    _put(st, "0000000000000002", _status(hub_id="0000000000000002", public_origin="https://b.example.com", display_name="B\u202e"))
    _put(st, "0000000000000003", "garbage")
    return st


@pytest.mark.skipif(WIN, reason="quyền POSIX/nhóm")
async def test_rpc_directory_admin_only_and_token_once(env, tmp_path, as_admin):
    from cfharness.rpc import RpcError

    await _setup_dir(env, tmp_path)
    from cfharness.tui.client import AsyncHubClient

    cli = AsyncHubClient(env.paths.socket)
    doc = await cli.call("admin.directory", {})
    assert [h["id"] for h in doc["hubs"]] == ["0000000000000001", "0000000000000002"] and doc["hubs"][1]["name"] == "B"
    st = await cli.call("admin.directory_status", {})
    assert st["token"] is False and st["hubs"] == 2 and st["public"] is False
    r = await cli.call("admin.directory_token", {})
    assert r["url"].startswith("http://cfharness.localhost:") and "/.well-known/cfharness-directory?t=" in r["url"]
    tok = r["url"].split("?t=", 1)[1]
    with pytest.raises(RpcError) as ei:  # không --rotate: token chỉ hiện một lần
        await cli.call("admin.directory_token", {})
    assert ei.value.data.get("reason") == "token_exists"
    env.hub.config.set("web.public_origin", "https://admin.example.com")
    r2 = await cli.call("admin.directory_token", {"rotate": True})
    assert r2["url"].startswith("https://admin.example.com/.well-known/cfharness-directory?t=") and r2["rotated"] and r2["public"]
    tok2 = r2["url"].split("?t=", 1)[1]
    assert not ad.verify_token(env.paths, tok) and ad.verify_token(env.paths, tok2)
    # token không vào audit (append-only)
    rows = env.hub.store.conn.execute("SELECT * FROM audit_log WHERE kind='admin.directory_token'").fetchall()
    assert len(rows) == 2 and tok not in json.dumps([list(map(str, x)) for x in rows]) and tok2 not in json.dumps([list(map(str, x)) for x in rows])
    with pytest.raises(RpcError):
        await cli.call("admin.directory_serve", {"token": tok})
    assert (await cli.call("admin.directory_serve", {"token": tok2}))["hubs"]


@pytest.mark.skipif(WIN, reason="quyền POSIX/nhóm")
async def test_rpc_directory_not_admin_blocked(env, tmp_path):
    from cfharness.rpc import RpcError
    from cfharness.tui.client import AsyncHubClient

    await _setup_dir(env, tmp_path)
    cli = AsyncHubClient(env.paths.socket)
    for m in ("admin.directory", "admin.directory_status", "admin.directory_token"):
        with pytest.raises(RpcError):
            await cli.call(m, {})
    tok, _ = ad.write_token(env.paths)
    with pytest.raises(RpcError):  # token đúng nhưng hub không phải admin → không có gì
        await cli.call("admin.directory_serve", {"token": tok})
    r = await env.client.get(f"/.well-known/cfharness-directory?t={tok}")
    assert r.status_code == 404 and r.json() == {"error": "not_found"}


@pytest.mark.skipif(WIN, reason="quyền POSIX/nhóm")
async def test_route_public_token_gated(env, tmp_path, as_admin):
    await _setup_dir(env, tmp_path)
    env.hub.config.set("admin_directory.name", "Văn phòng")
    # chưa có token → 404
    r = await env.client.get("/.well-known/cfharness-directory?t=" + "a" * 43)
    assert r.status_code == 404 and r.json() == {"error": "not_found"}
    tok, _ = ad.write_token(env.paths)
    for q in ("", "?t=", "?t=wrong", "?t=" + tok[:-2], "?x=" + tok):
        r = await env.client.get("/.well-known/cfharness-directory" + q)  # KHÔNG đăng nhập
        assert r.status_code == 404 and r.json() == {"error": "not_found"}, q
        assert r.headers["cache-control"] == "no-store"
    r = await env.client.get(f"/.well-known/cfharness-directory?t={tok}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    d = r.json()
    assert set(d) == {"kind", "schema", "name", "generated_at", "hubs"} and d["kind"] == "cfharness.directory" and d["schema"] == 1
    assert d["name"] == "Văn phòng" and [h["origin"] for h in d["hubs"]] == ["https://an.example.com", "https://b.example.com"]
    for h in d["hubs"]:
        assert set(h) == {"id", "name", "user", "origin"}
    blob = r.text
    assert SECRET not in blob and "cost" not in blob and "summary" not in blob and str(tmp_path) not in blob
    # Host vẫn bị kiểm (chống DNS rebinding)
    assert (await env.client.get(f"/.well-known/cfharness-directory?t={tok}", headers={"Host": "evil.example.com"})).status_code == 421


@pytest.mark.skipif(WIN, reason="quyền POSIX/nhóm")
async def test_route_rate_limited(env, tmp_path, as_admin):
    await _setup_dir(env, tmp_path)
    tok, _ = ad.write_token(env.paths)
    codes = [(await env.client.get(f"/.well-known/cfharness-directory?t=bad{i}")).status_code for i in range(12)]
    assert codes[:10] == [404] * 10 and codes[-1] == 429
    r = await env.client.get(f"/.well-known/cfharness-directory?t={tok}")  # đang khoá: token đúng cũng chờ
    assert r.status_code == 429 and r.json() == {"error": "rate_limited"} and int(r.headers["retry-after"]) > 0
    # trần lần trả danh mục (token đúng)
    from cfharness.web.desktop import SuccessWindow

    t = [0.0]
    w = SuccessWindow(per_ip=3, total=5, window_s=60, clock=lambda: t[0])
    assert [w.allow("a") for _ in range(4)] == [True, True, True, False]
    assert w.allow("b") and w.allow("c") and not w.allow("d")
    t[0] = 61
    assert w.allow("a")


def test_compare_is_constant_time_source():
    import inspect

    src = inspect.getsource(ad.verify_token)
    assert "compare_digest" in src and "==" not in src.replace("compare_digest", "")


# =========================================================================== C6: thư mục dữ liệu app desktop
def test_app_data_dirs_cover_mac_windows():
    dirs = app_data_dirs("/Users/an", {"APPDATA": r"C:\Users\an\AppData\Roaming", "LOCALAPPDATA": r"C:\Users\an\AppData\Local"})
    assert f"/Users/an/Library/Application Support/{APP_ID}" in dirs
    assert f"C:/Users/an/AppData/Roaming/{APP_ID}" in dirs and f"C:/Users/an/AppData/Local/{APP_ID}" in dirs
    assert f"/Users/an/Library/WebKit/{APP_ID}" in dirs  # cookie WKWebView
    env0 = {"APPDATA": r"C:\Users\an\AppData\Roaming"}
    assert is_app_data(rf"C:\Users\an\AppData\Roaming\{APP_ID}\hubs.json", "/Users/an", env0)
    assert is_app_data(f"/Users/an/Library/Application Support/IO.CFHARNESS.DESKTOP/x", "/Users/an", env0)  # APFS không phân biệt hoa/thường
    assert not is_app_data(f"/Users/an/Library/Application Support/{APP_ID}x/a", "/Users/an", env0)
    assert not is_app_data("/Users/an/work/cfharness-desktop/src-tauri/tauri.conf.json", "/Users/an", env0)
    from cfharness.desktop_app import glob_may_touch

    assert glob_may_touch("C:/Users/an/AppData/Roaming/io.cfharness.deskto?/*", "/Users/an", env0)
    assert glob_may_touch("/Users/an/Library/WebKit/IO.CFHARNESS.*/**", "/Users/an", env0)
    assert not glob_may_touch("/Users/an/Library/WebKit/io.cfharness.deskto?", "/Users/an", env0)  # chỉ chính tên thư mục, không vào trong
    assert not glob_may_touch("/Users/an/work/**/*.py", "/Users/an", env0)


H = os.path.expanduser("~")
APPDIR = f"{H}/Library/Application Support/{APP_ID}"
DESTRUCTIVE_APP_DATA = [
    ("Read", {"file_path": f"{APPDIR}/hubs.json"}),
    ("Read", {"file_path": f"{H}/Library/WebKit/{APP_ID}/WebsiteData/Default/cookies.db"}),
    ("Grep", {"pattern": "token", "path": APPDIR}),
    ("Glob", {"pattern": "**/*", "path": f"{H}/Library/HTTPStorages/{APP_ID}"}),
    ("Glob", {"pattern": f"{APPDIR}/**/*.json"}),
    # ký tự đại diện NGAY trong tên thư mục app (không chỉ sau `*` đầu tiên) — Glob không bị sandbox OS bọc
    ("Glob", {"pattern": "~/Library/Application Support/io.cfharness.deskto?/**"}),
    ("Glob", {"pattern": f"{H}/Library/Application Support/[i]o.cfharness.desktop/*"}),
    ("Glob", {"pattern": "~/Library/Application Support/{io,x}.cfharness.desktop/*"}),
    ("Glob", {"pattern": "~/Library/*/io.cfharness.desktop/*.json"}),
    ("Glob", {"pattern": "io.cfharness.deskto?/**", "path": f"{H}/Library/Application Support"}),
    ("Write", {"file_path": f"{APPDIR}/hubs.json", "content": "{}"}),
    ("Edit", {"file_path": f"{APPDIR}/hubs.json", "old_string": "a", "new_string": "b"}),
    ("Bash", {"command": f"cat ~/Library/Application\\ Support/{APP_ID}/hubs.json"}),
    ("Bash", {"command": f"sqlite3 \"$HOME/Library/WebKit/{APP_ID}/WebsiteData/cookies.db\" .dump"}),
    ("Bash", {"command": f"cd ~/Library/Application\\ Support && tar czf /tmp/x.tgz {APP_ID}"}),
    ("Bash", {"command": f"type %APPDATA%\\{APP_ID}\\hubs.json"}),
    ("Bash", {"command": f"Get-Content $env:LOCALAPPDATA\\{APP_ID}\\EBWebView\\Default\\Cookies"}),
    ("Bash", {"command": f"rm -rf ~/.local/share/{APP_ID}"}),
]


def test_desktop_app_data_forbidden(tmp_path):
    ctx = GuardContext(cwd=tmp_path, policy=PolicyStore(tmp_path / "h").load())
    missed = []
    for tool, ti in DESTRUCTIVE_APP_DATA:
        v = classify(tool, ti, ctx)
        if not (v.forbidden and v.rule == "desktop_app_data"):
            missed.append((tool, ti, v.verdict, v.rule))
    assert not missed, missed
    # agent làm việc trên repo app (identifier trong tauri.conf.json) KHÔNG bị chặn nhầm
    for tool, ti in (("Bash", {"command": f"grep -rn {APP_ID} src-tauri/tauri.conf.json"}),
                     ("Read", {"file_path": f"{H}/Library/Application Support/{APP_ID}x/a"}),
                     ("Bash", {"command": "ls ~/Library/Application\\ Support/Code"}),
                     ("Glob", {"pattern": "~/Library/Application Support/*"}),  # chỉ tên thư mục cấp trên, không vào trong thư mục app
                     ("Glob", {"pattern": "src/**/*.py"}),
                     ("Glob", {"pattern": "**/*.rs", "path": f"{H}/Workspace/cfharness-desktop"})):
        assert classify(tool, ti, ctx).rule != "desktop_app_data", ti
    assert not shell_mentions(f"echo {APP_ID}")


def test_permissions_deny_every_hub_including_trust_full(tmp_path):
    from cfharness.guard.classifier import NEVER_DOWNGRADE_RULES
    from cfharness.guard.runtime import build_settings, rule_path

    assert "desktop_app_data" in NEVER_DOWNGRADE_RULES
    home = tmp_path / "hub"
    (home / "policy").mkdir(parents=True)
    for trust in (False, True):
        s = build_settings({}, run_id="r1", socket=tmp_path / "hub.sock", policy_dir=home / "policy", guard_cfg={"enabled": True},
                           approval_timeout_s=60, cwd=tmp_path, trust_full=trust)
        deny = s["permissions"]["deny"]
        for t in ("Read", "Grep", "Glob", "Edit", "Write"):
            assert f"{t}(/{rule_path(APPDIR)}/**)" in deny, (trust, t)


@pytest.mark.skipif(WIN, reason="sandbox macOS/Linux")
def test_sandbox_denies_app_data(tmp_path):
    from cfharness.guard.runtime import fixed_protected_dirs, sandbox_settings

    s = sandbox_settings(tmp_path / "hub", tmp_path / "hub.sock", {})
    for d in app_data_dirs():
        assert d in s["filesystem"]["denyRead"] and d in s["filesystem"]["denyWrite"], d
    assert APPDIR in fixed_protected_dirs(tmp_path / "hub", tmp_path / "hub.sock")
    # cwd nằm trong thư mục app → vẫn không gỡ chặn ghi (vùng cố định)
    s2 = sandbox_settings(tmp_path / "hub", tmp_path / "hub.sock", {}, cwd=Path(APPDIR) / "x")
    assert APPDIR in s2["filesystem"]["denyWrite"]
