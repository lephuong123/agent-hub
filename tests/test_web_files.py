"""Xem / mở file agent đã thao tác (fileview.py + web/file_pages.py, docs/dev-notes/file-open.md).

Hàm thuần (phạm vi đường dẫn, đuôi bị chặn, loại nội dung) + web thật: daemon IN-PROCESS + FakeRuntime, app Starlette qua ASGITransport
(client = 127.0.0.1). KHÔNG BAO GIỜ mở ứng dụng thật: `fileview.OPENER` luôn bị thay bằng hàm ghi lại lời gọi.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import httpx2 as httpx
import pytest

from cfharness import auth, fileview
from cfharness.errors import Blocked, HubError, NotFound
from cfharness.fileview import can_open, check_raw_path, kind_of, open_command, resolve_in_roots, secret_name
from cfharness.runtime import FakeRuntime
from cfharness.web import present as P
from cfharness.web.app import _run_converter, create_app, make_templates, replay_items
from test_web import BASE, ISOLATED, PORT, _start, _stop, audit_rows, until  # noqa: F401 — khung test web

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


@pytest.fixture(autouse=True)
def no_real_open(monkeypatch):
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(fileview, "OPENER", lambda real, reveal: calls.append((real, reveal)))
    return calls


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "ws"
    (w / "src").mkdir(parents=True)
    (w / "src" / "a.py").write_text("print('<script>alert(1)</script>')\nx = 1\n", encoding="utf-8")
    (w / "notes.md").write_text("# Ghi chú\nnội dung tiếng Việt\n", encoding="utf-8")
    (w / "img.png").write_bytes(PNG)
    (w / "pic.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', encoding="utf-8")
    (w / "page.html").write_text("<html><script>alert(1)</script></html>", encoding="utf-8")
    (w / "blob.bin").write_bytes(b"\x00\x01\x02binary\xff")
    (w / "tool.exe").write_bytes(b"MZ\x90\x00")
    (w / "run.bat").write_text("echo hi", encoding="utf-8")
    (w / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (w / ".env.example").write_text("TOKEN=", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("BÍ MẬT NGOÀI WORKSPACE", encoding="utf-8")
    return w


# =========================================================================== hàm thuần
def test_raw_path_rejects_unc_device_ads_and_control_chars():
    for bad in ("\\\\server\\share\\x.txt", "//server/share/x", "\\\\?\\C:\\x", "\\\\.\\PhysicalDrive0", "a\x00b", "a\nb", "C:\\x.txt:hidden",
                "NUL", "dir/CON.txt", "com1", "", "   ", "x" * 5000):
        with pytest.raises(HubError):
            check_raw_path(bad)
    with pytest.raises(HubError):
        check_raw_path(None)
    assert check_raw_path("  src/a.py ") == "src/a.py"


def test_containment_traversal_absolute_and_relative(ws, tmp_path, hub_home):
    roots = [str(ws)]
    real, root = resolve_in_roots("src/a.py", roots, hub_home=hub_home.home)
    assert Path(real) == (ws / "src" / "a.py").resolve() and Path(root) == ws.resolve()
    real, _ = resolve_in_roots(str(ws / "notes.md"), roots, hub_home=hub_home.home)
    assert Path(real) == (ws / "notes.md").resolve()
    # đi ra ngoài bằng .. (tương đối và tuyệt đối) / đường dẫn tuyệt đối ngoài gốc
    for bad in ("../outside.txt", "src/../../outside.txt", str(ws / ".." / "outside.txt"), str(tmp_path / "outside.txt")):
        with pytest.raises(Blocked):
            resolve_in_roots(bad, roots, hub_home=hub_home.home)
    with pytest.raises(NotFound):
        resolve_in_roots("khong-co.txt", roots, hub_home=hub_home.home)
    with pytest.raises(NotFound):  # không có gốc nào tồn tại
        resolve_in_roots("a.txt", [str(tmp_path / "khong-ton-tai")], hub_home=hub_home.home)
    with pytest.raises(NotFound):  # gốc ổ đĩa / "/" quá rộng — bị bỏ
        resolve_in_roots(str(tmp_path / "outside.txt"), [Path(tmp_path).anchor], hub_home=hub_home.home)


def test_symlink_escape_is_blocked(ws, tmp_path, hub_home):
    link = ws / "link.txt"
    dlink = ws / "linkdir"
    try:
        os.symlink(tmp_path / "outside.txt", link)
        os.symlink(tmp_path, dlink, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("không tạo được symlink trên máy này (Windows cần Developer Mode)")
    for bad in ("link.txt", "linkdir/outside.txt", str(link)):
        with pytest.raises(Blocked):
            resolve_in_roots(bad, [str(ws)], hub_home=hub_home.home)
    inside = ws / "inside-link.md"
    os.symlink(ws / "notes.md", inside)  # symlink trỏ vào TRONG gốc thì được
    assert Path(resolve_in_roots("inside-link.md", [str(ws)], hub_home=hub_home.home)[0]) == (ws / "notes.md").resolve()


@pytest.mark.skipif(os.name != "nt", reason="junction NTFS chỉ có trên Windows")
def test_junction_escape_is_blocked_on_windows(ws, tmp_path, hub_home):
    import _winapi

    out_dir = tmp_path / "ngoai"
    out_dir.mkdir()
    (out_dir / "s.txt").write_text("BÍ MẬT", encoding="utf-8")
    _winapi.CreateJunction(str(out_dir), str(ws / "jn"))  # không cần quyền admin, khác symlink
    for bad in ("jn/s.txt", str(ws / "jn" / "s.txt")):
        with pytest.raises(Blocked):
            resolve_in_roots(bad, [str(ws)], hub_home=hub_home.home)


def test_secrets_and_hub_home_internals_blocked(ws, hub_home):
    with pytest.raises(Blocked, match="bí mật"):
        resolve_in_roots(".env", [str(ws)], hub_home=hub_home.home)
    assert Path(resolve_in_roots(".env.example", [str(ws)], hub_home=hub_home.home)[0]).name == ".env.example"
    for n in (".env", ".env.local", "id_rsa", "id_ed25519", "server.pem", "x.key", ".netrc", ".npmrc", "credentials.json", "app.secrets.yaml"):
        assert secret_name(n), n
    for n in ("id_rsa.pub", ".env.example", "notes.md", "keyboard.py", "secretary.txt"):
        assert not secret_name(n), n
    # gốc (lỡ) trùng HUB_HOME: nội bộ hub bị chặn, workspace agent + worktree thì được
    hh = hub_home.home
    (hh / "secrets").mkdir(exist_ok=True)
    (hh / "secrets" / "x.txt").write_text("s", encoding="utf-8")
    (hh / "auth").mkdir(exist_ok=True)
    (hh / "auth" / "web.json").write_text("{}", encoding="utf-8")
    wsd = hh / "agents" / "ag0" / "workspaces" / "r1"
    wsd.mkdir(parents=True)
    (wsd / "ok.md").write_text("ok", encoding="utf-8")
    (hh / "worktrees" / "w").mkdir(parents=True)
    (hh / "worktrees" / "w" / "ok.md").write_text("ok", encoding="utf-8")
    for bad in ("secrets/x.txt", "auth/web.json", "config.yaml"):
        if not (hh / bad).exists():
            (hh / bad).write_text("x", encoding="utf-8")
        with pytest.raises(Blocked, match="HUB_HOME"):
            resolve_in_roots(bad, [str(hh)], hub_home=hh)
    assert resolve_in_roots("agents/ag0/workspaces/r1/ok.md", [str(hh)], hub_home=hh)
    assert resolve_in_roots("worktrees/w/ok.md", [str(hh)], hub_home=hh)
    # token người cạnh socket
    sock = ws / "hub.sock"
    tok = auth.token_path(sock)
    tok.write_text("t", encoding="utf-8")
    with pytest.raises(Blocked, match="token"):
        resolve_in_roots(tok.name, [str(ws)], hub_home=hh, socket=sock)


def test_open_allowlist_blocks_executables_and_scripts():
    for bad in ("x.exe", "x.bat", "x.cmd", "x.com", "x.ps1", "x.vbs", "x.js", "x.jse", "x.wsf", "x.msi", "x.lnk", "x.scr", "x.hta", "x.reg",
                "x.sh", "x.app", "x.jar", "x.py", "x.rb", "x.html", "x.htm", "x.svg", "x.docm", "x.xlsm", "x.doc", "Makefile", "x.PS1", "x.EXE"):
        assert not can_open(bad), bad
    for ok in ("a.md", "a.txt", "a.pdf", "a.png", "a.JPG", "a.docx", "a.xlsx", "a.csv", "a.json", "a.mp4"):
        assert can_open(ok), ok
    # lệnh mở theo OS (không chạy): reveal luôn là trình quản lý file, không bao giờ chạy file
    cmd = open_command("/w/a.md", reveal=True)
    assert cmd is None or cmd[0] in ("explorer.exe", "open", "xdg-open")


def test_kind_of():
    assert kind_of("a.png", PNG) == ("image", "image/png")
    assert kind_of("a.svg", b"<svg/>")[0] == "image"
    assert kind_of("x", b"%PDF-1.7")[0] == "pdf"
    assert kind_of("a.py", "xin chào".encode())[0] == "text"
    assert kind_of("a.txt", "xin chào".encode()[:-1])[0] == "text"  # ký tự nhiều byte bị cắt ở cuối mẫu
    assert kind_of("a.dat", b"\x00\x01")[0] == "binary"
    assert kind_of("a.dat", b"\xff\xfe\xfa abc")[0] == "binary"


def test_file_methods_human_only_and_classified():
    assert {"file.read", "file.open"} <= auth.HUMAN_ONLY
    assert "file.open" in auth.all_mutating()
    assert not ({"file.read", "file.open"} & set(auth.MANAGER_ALLOWED)) and not ({"file.read", "file.open"} & set(auth.AGENT_ALLOWED_MUTATING))


# =========================================================================== chip file trong diễn biến run
def _tool_ev(t: str, seq: int, **p) -> dict:
    return {"type": t, "seq": seq, "run_id": "r-1", "ts": "2026-11-12T00:00:00Z", "payload": {"tool_id": "tu1", "name": "Read", **p}}


def test_transcript_file_chip_replay_and_live():
    start = _tool_ev("tool.start", 1, input={"file_path": "/w/src/a&b.py"}, args_preview="/w/src/a&b.py")
    done = _tool_ev("tool.complete", 2, result_preview="ok", duration_s=0.1)
    it = P.transcript_item(start)
    assert it["file"] == {"run": "r-1", "path": "/w/src/a&b.py", "name": "a&b.py"}
    assert P.transcript_item({**start, "payload": {"name": "Bash", "input": {"command": "ls"}}}).get("file") is None
    assert P.file_ref("NotebookEdit", {"notebook_path": "n.ipynb"}, "r")["path"] == "n.ipynb"
    assert P.file_ref("Read", {"file_path": "x"}, None) is None
    items = replay_items([start, done])
    assert len(items) == 1 and items[0]["file"]["path"] == "/w/src/a&b.py" and not items[0]["running"]
    t = make_templates()
    html = str(t.env.get_template("_items.html").module.item(items[0]))
    assert 'href="/files/view?run=r-1&amp;path=/w/src/a%26b.py"' in html and 'data-fopen="open"' in html and 'data-fopen="reveal"' in html
    assert 'data-path="/w/src/a&amp;b.py"' in html
    conv = _run_converter(t)
    live = [m for ev in (start, done) for m in conv(ev) if m["t"] == "item"]
    assert len(live) == 2 and all("/files/view?run=r-1" in m["html"] for m in live)  # tool.complete thay tại chỗ vẫn giữ chip
    assert "Đọc" in live[-1]["html"] and "a&amp;b.py" in live[-1]["html"] and "file_path" in live[-1]["html"]  # P0-3: live giữ input khi tool xong


# =========================================================================== web thật
@pytest.fixture
async def env(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    yield e
    await _stop(e)


async def run_in(env, ws: Path) -> str:
    env.hub.agents.create({"id": "ag0", "name": "ag0", "model": "haiku"})
    run = env.hub.runs.submit("ag0", "việc mẫu")
    assert await until(lambda: (env.hub.store.get_run(run["id"]) or {}).get("status") in ("completed", "failed"))
    env.hub.store.update_run(run["id"], {"cwd": str(ws)})
    return run["id"]


def q(rid: str, path: str, **kw) -> str:
    return httpx.QueryParams({"run": rid, "path": path, **kw}).__str__()


async def test_requires_login_and_csrf(env, ws, no_real_open):
    c = env.client
    rid = await run_in(env, ws)
    assert (await c.get(f"/files/view?{q(rid, 'notes.md')}")).status_code == 303
    assert (await c.get(f"/files/raw?{q(rid, 'img.png')}")).status_code == 303
    assert (await c.post("/files/open", data={"run": rid, "path": "notes.md"})).status_code == 401
    await env.login()
    assert (await c.post("/files/open", data={"run": rid, "path": "notes.md", "k": env.key})).status_code == 403  # thiếu CSRF
    assert (await c.post("/files/open", data={"run": rid, "path": "notes.md", "csrf": env.csrf})).status_code == 403  # thiếu khoá WT-1
    r = await c.post("/files/open", data={"run": rid, "path": "notes.md", "csrf": env.csrf, "k": env.key}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert no_real_open == []
    assert (await c.get("/files/view?path=notes.md")).status_code == 400  # thiếu run/agent


async def test_viewer_text_image_binary_and_raw_headers(env, ws):
    await env.login()
    rid = await run_in(env, ws)
    c = env.client
    r = await c.get(f"/files/view?{q(rid, 'src/a.py')}")
    assert r.status_code == 200 and 'class="fview-code"' in r.text and r.text.count('class="ln"') == 2
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text and "<script>alert(1)" not in r.text
    assert "không mở trực tiếp" in r.text  # .py: chỉ xem / mở thư mục (đang cục bộ)
    assert 'data-fopen="reveal"' in r.text and 'data-fopen="open"' not in r.text
    r = await c.get(f"/files/view?{q(rid, str(ws / 'notes.md'))}")
    assert r.status_code == 200 and "nội dung tiếng Việt" in r.text and 'data-fopen="open"' in r.text
    r = await c.get(f"/files/view?{q(rid, 'img.png')}")
    assert r.status_code == 200 and '<img class="fview-img" src="/files/raw?' in r.text
    r = await c.get(f"/files/view?{q(rid, 'blob.bin')}")
    assert r.status_code == 200 and "File nhị phân" in r.text and "fview-code" not in r.text
    # raw: ảnh inline đúng content-type + nosniff; SVG inline nhưng CSP sandbox; HTML/nhị phân → attachment octet-stream
    r = await c.get(f"/files/raw?{q(rid, 'img.png')}")
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["content-disposition"].startswith("inline")
    r = await c.get(f"/files/raw?{q(rid, 'pic.svg')}")
    assert r.headers["content-type"].startswith("image/svg+xml") and "sandbox" in r.headers["content-security-policy"]
    for name in ("page.html", "blob.bin", "src/a.py"):
        r = await c.get(f"/files/raw?{q(rid, name)}")
        assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream", name
        assert r.headers["content-disposition"].startswith("attachment") and "sandbox" in r.headers["content-security-policy"]
    r = await c.get(f"/files/raw?{q(rid, 'img.png', dl='1')}")
    assert r.headers["content-disposition"].startswith("attachment") and 'filename="img.png"' in r.headers["content-disposition"]
    rows = audit_rows(env, "file.read")
    assert rows and all(x["channel"] == "web" for x in rows)
    assert all("alert(1)" not in (x["data"] or "") for x in rows)  # nội dung file không bao giờ vào audit


async def test_viewer_blocks_escape_secret_and_unknown_run(env, ws, tmp_path):
    await env.login()
    rid = await run_in(env, ws)
    c = env.client
    for bad in ("../outside.txt", str(tmp_path / "outside.txt"), ".env", "\\\\server\\share\\x.txt"):
        for route in ("view", "raw"):
            r = await c.get(f"/files/{route}?{q(rid, bad)}")
            assert r.status_code == 400 and "BÍ MẬT" not in r.text and "TOKEN=secret" not in r.text, (route, bad)
    assert (await c.get(f"/files/view?{q('khong-co-run', 'notes.md')}")).status_code == 404
    denied = [json.loads(x["data"]) for x in audit_rows(env, "file.read")]
    assert any(d.get("ok") is False for d in denied)


async def test_viewer_truncates_big_text(env, ws):
    await env.login()
    rid = await run_in(env, ws)
    (ws / "big.log").write_text("dòng\n" * 300_000, encoding="utf-8")
    r = await env.client.get(f"/files/view?{q(rid, 'big.log')}")
    assert r.status_code == 200 and "chỉ hiển thị" in r.text


async def test_open_local_allowlist_reveal_and_audit(env, ws, no_real_open):
    await env.login()
    rid = await run_in(env, ws)
    js = {"accept": "application/json"}
    r = await env.post("/files/open", {"run": rid, "path": "notes.md"}, headers=js)
    assert r.status_code == 200 and r.json()["ok"] and no_real_open == [(str((ws / "notes.md").resolve()), False)]
    for blocked in ("tool.exe", "run.bat", "src/a.py", "page.html"):
        r = await env.post("/files/open", {"run": rid, "path": blocked}, headers=js)
        assert r.status_code == 400 and not r.json()["ok"] and "không mở trực tiếp" in r.json()["message"], blocked
    assert len(no_real_open) == 1
    r = await env.post("/files/open", {"run": rid, "path": "tool.exe", "reveal": "1"}, headers=js)  # mở THƯ MỤC chứa file thì được
    assert r.status_code == 200 and no_real_open[-1] == (str((ws / "tool.exe").resolve()), True)
    r = await env.post("/files/open", {"run": rid, "path": "../outside.txt", "reveal": "1"}, headers=js)
    assert r.status_code == 400 and len(no_real_open) == 2
    # không JS: chuyển hướng + flash
    r = await env.post("/files/open", {"run": rid, "path": "notes.md", "next": f"/runs/{rid}"})
    assert r.status_code == 303 and r.headers["location"] == f"/runs/{rid}"
    rows = audit_rows(env, "file.open")
    assert rows and all(x["channel"] == "web" and x["actor"] == "user" for x in rows)
    assert any(json.loads(x["data"]).get("ok") is False for x in rows)


async def test_open_refused_when_not_local(env, ws, no_real_open):
    """Qua tunnel/proxy (header proxy) hoặc host public → không mở app trên máy chủ (403), vẫn xem được."""
    await env.login()
    rid = await run_in(env, ws)
    js = {"accept": "application/json"}
    for h in ({"x-forwarded-for": "203.0.113.9"}, {"cf-connecting-ip": "203.0.113.9"}, {"forwarded": "for=203.0.113.9"}):
        r = await env.post("/files/open", {"run": rid, "path": "notes.md"}, headers={**js, **h})
        assert r.status_code == 403 and "CHÍNH máy" in r.json()["message"]
    # host public (web.public_origin) dùng chung phiên — vẫn từ chối mở; trình xem chạy và không hiện nút mở
    origin = "https://an.example.com"
    pub = create_app(env.paths, auth=env.auth, port=PORT, public_origin=origin)
    sid = env.client.cookies.get(env.cookie)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=pub), base_url=origin, cookies={env.cookie: sid}) as c:
        r = await c.post("/files/open", data={"run": rid, "path": "notes.md", "csrf": env.csrf, "k": env.key}, headers={**js, "origin": origin})
        assert r.status_code == 403
        r = await c.get(f"/files/view?{q(rid, 'notes.md')}")
        assert r.status_code == 200 and "nội dung tiếng Việt" in r.text and "link public" in r.text and "data-fopen" not in r.text
    assert no_real_open == []


async def test_agent_scope_and_diff_chip(env, ws):
    await env.login()
    rid = await run_in(env, ws)
    # agent: workspace_default + <home>/workspaces
    home = Path(env.hub.store.get_agent("ag0")["home_path"])
    (home / "workspaces" / "c1").mkdir(parents=True, exist_ok=True)
    (home / "workspaces" / "c1" / "att.md").write_text("đính kèm", encoding="utf-8")
    r = await env.client.get("/files/view?" + str(httpx.QueryParams({"agent": "ag0", "path": str(home / "workspaces" / "c1" / "att.md")})))
    assert r.status_code == 200 and "đính kèm" in r.text
    r = await env.client.get("/files/view?" + str(httpx.QueryParams({"agent": "ag0", "path": str(home / "SOUL.md")})))
    assert r.status_code in (400, 404)
    # khối "Thay đổi file": chip cho file còn tồn tại, không cho file đã xoá
    env.hub.store.update_run(rid, {"diff": {"kind": "dir", "root": str(ws), "files": [{"path": "notes.md", "status": "modified"},
                                                                                       {"path": "gone.txt", "status": "deleted"}]}})
    r = await env.client.get(f"/partials/run/{rid}/diff")
    assert r.status_code == 200 and f"/files/view?run={rid}&amp;path=notes.md" in r.text and "path=gone.txt" not in r.text


async def test_files_js_and_css_served(env):
    await env.login()
    page = await env.client.get("/")
    assert "/static/files.js" in page.text and "/static/files.css" in page.text
    js = await env.client.get("/static/files.js")
    assert js.status_code == 200 and "/files/open" in js.text and "innerHTML" not in js.text


# =========================================================================== cấu hình `files.*` (trang Cài đặt)
def test_files_settings_validate_and_loosens():
    from cfharness.config import validate_setting

    validate_setting("files.open_extensions", [".txt", ".PDF"])
    for bad in ([".exe"], [".html"], ["pdf"], [".docm"], "x", [1]):
        with pytest.raises(ValueError):
            validate_setting("files.open_extensions", bad)
    for bad in (["a/b"], ["a\b"], [""], "x"):
        with pytest.raises(ValueError):
            validate_setting("files.secret_patterns", bad)
    for bad in (0, 51, True, "10"):
        with pytest.raises(ValueError):
            validate_setting("files.max_download_mb", bad)
    with pytest.raises(ValueError):
        validate_setting("files.la", 1)
    assert fileview.loosens("files.open_extensions", [".txt"], [".txt", ".pdf"])
    assert not fileview.loosens("files.open_extensions", [".txt", ".pdf"], [".txt"])
    assert fileview.loosens("files.secret_patterns", [".env", "*.pem"], [".env"])
    assert not fileview.loosens("files.secret_patterns", [".env"], [".env", "*.pem"])
    assert fileview.loosens("files.secret_allow", ["*.pub"], ["*.pub", ".env"])
    assert fileview.loosens("files.cloud_download", False, True) and not fileview.loosens("files.cloud_download", True, False)
    assert fileview.loosens("files.max_download_mb", 10, 20) and not fileview.loosens("files.max_download_mb", 10, 5)
    assert fileview.loosens("files.max_download_mb", None, 20)  # chưa đặt = mặc định 10


def test_files_settings_drive_secret_and_open():
    cfg = {"secret_patterns": ["*.secret"], "secret_allow": [], "open_extensions": [".txt", ".exe"]}
    assert secret_name("a.SECRET", cfg) and not secret_name(".env", cfg)
    assert can_open("x.txt", cfg) and not can_open("x.pdf", cfg)
    assert not can_open("x.exe", cfg)  # NEVER_OPEN thắng cả khi cấu hình lỡ có


async def test_cloud_download_toggle_and_size_cap(env, ws):
    await env.login()
    rid = await run_in(env, ws)
    cloud = {"x-forwarded-for": "203.0.113.9"}
    assert (await env.client.get(f"/files/raw?{q(rid, 'blob.bin', dl='1')}", headers=cloud)).status_code == 200  # mặc định: cho tải
    env.hub.config.set("files.cloud_download", False)
    r = await env.client.get(f"/files/view?{q(rid, 'notes.md')}", headers=cloud)
    assert r.status_code == 200 and "nội dung tiếng Việt" in r.text and "dl=1" not in r.text  # vẫn xem, không có nút tải
    assert (await env.client.get(f"/files/raw?{q(rid, 'blob.bin', dl='1')}", headers=cloud)).status_code in (400, 403)
    assert (await env.client.get(f"/files/raw?{q(rid, 'blob.bin')}", headers=cloud)).status_code == 403  # nhị phân = tải về
    assert (await env.client.get(f"/files/raw?{q(rid, 'img.png')}", headers=cloud)).status_code == 200  # ảnh xem inline vẫn được
    assert (await env.client.get(f"/files/raw?{q(rid, 'blob.bin', dl='1')}")).status_code == 200  # dùng trên máy: luôn tải được
    (ws / "big.bin").write_bytes(b"\x01" * 1_500_000)
    env.hub.config.set("files.max_download_mb", 1)
    assert (await env.client.get(f"/files/raw?{q(rid, 'big.bin', dl='1')}")).status_code != 200
    env.hub.config.set("files.max_download_mb", 2)
    assert (await env.client.get(f"/files/raw?{q(rid, 'big.bin', dl='1')}")).status_code == 200


async def test_settings_page_files_group_and_public_can_only_tighten(env):
    await env.login()
    r = await env.client.get("/settings")
    assert r.status_code == 200 and "files.open_extensions" in r.text and "Xem / mở file" in r.text and "<textarea" in r.text
    cloud = {"x-forwarded-for": "203.0.113.9"}

    async def put(key, value, headers=None):
        data = {"key": key, "value": value, "confirm": "yes", "next": "/settings"}
        return await env.post("/do/config_set", data, headers=headers)

    # siết qua link public: được
    await put("files.max_download_mb", "5", cloud)
    assert env.hub.config.get("files.max_download_mb") == 5
    cur = env.hub.config.get("files.secret_patterns")
    await put("files.secret_patterns", "\n".join(cur + ["custom.txt"]), cloud)  # thêm mẫu bí mật = siết
    assert "custom.txt" in env.hub.config.get("files.secret_patterns")
    await put("files.secret_patterns", ".env", cloud)  # bỏ bớt mẫu = nới → từ chối
    assert "*.pem" in env.hub.config.get("files.secret_patterns")
    # nới qua link public: bị từ chối, giá trị không đổi
    await put("files.max_download_mb", "20", cloud)
    assert env.hub.config.get("files.max_download_mb") == 5
    await put("files.open_extensions", ".txt, .pdf, .stl", cloud)
    assert ".stl" not in (env.hub.config.get("files.open_extensions") or [])
    # trên chính máy: nới được; đuôi chương trình vẫn bị từ chối
    await put("files.open_extensions", ".txt, .pdf, .stl")
    assert env.hub.config.get("files.open_extensions") == [".txt", ".pdf", ".stl"]
    await put("files.open_extensions", ".txt\n.exe")
    assert ".exe" not in env.hub.config.get("files.open_extensions")
