"""Web — thẻ "Không gian số" ở Cài đặt + link "Mở trên Không gian số" trên chip tệp (filehub/, plan-cloud-files §3.6) + cài đặt trên web
cả qua link public (plan-filehub-workspace-root W7 + #121): người dùng hub tự thiết lập/đăng nhập/nối/gỡ thư mục/tạm dừng; thiết lập/đăng
nhập/chuyển chế độ/gỡ có bước 2, đăng nhập từ xa = relay OAuth qua route công khai `/filehub/oauth/cb` (state một lần, giới hạn tốc độ, không
phản chiếu tham số). Web không bao giờ nhận đường dẫn binary sync; đường dẫn bản sao trên máy chỉ đặt được khi web dùng trên chính máy chạy
hub. Chữ từ binary sync là dữ liệu → autoescape. Thẻ: status/index ghi tay; đăng nhập: binary GIẢ `fake_tangthu_sync.py` (không mạng, không claude).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_web import BASE, ISOLATED, _start, _stop, audit_rows, until  # noqa: F401 — khung test web


PROXY = {"x-forwarded-for": "203.0.113.9"}  # như qua tunnel public → không phải web cục bộ


@pytest.fixture
async def env(hub_home, tmp_path, monkeypatch):
    import cfharness.filehub.service as fhs

    monkeypatch.setattr(fhs, "BINARY_CANDIDATES", ())  # không dò binary thật cài trên máy chạy test
    from cfharness.runtime import FakeRuntime

    e = await _start(hub_home, FakeRuntime(ISOLATED))
    yield e, tmp_path
    await _stop(e)


def _setup(hub, tmp: Path) -> Path:
    local = (tmp / "KhongGianSo" / "chung").resolve()
    (local / "Báo cáo").mkdir(parents=True)
    (local / "Báo cáo" / "q3.docx").write_text("x")
    hub.config.set("filehub.server", "https://kgs.example.test")
    hub.config.set("filehub.folders", [{"name": "chung", "root_id": "r_chung", "ws_id": "ws_1", "local_path": str(local)}])
    hub.config.set("filehub.enabled", True)
    fh = hub.filehub
    fh.prepare_files()
    fh.status_path.write_text(json.dumps({
        "version": "0.3.0", "pid": 1, "state": "idle", "updated_at": "2026-11-15T03:00:00+00:00",
        "roots": [{"id": "r_chung", "name": "chung", "ws_id": "ws_1", "local_path": str(local), "state": "idle", "pending_up": 2, "pending_down": 0,
                   "blocked": None, "conflicts": [{"path": "<script>alert(1)</script>.docx", "created_at": "2026-11-15T03:00:00+00:00"}], "last_error": None}],
        "recent": [{"ts": "t", "root_id": "r_chung", "dir": "down", "op": "update", "path": "Báo cáo/q3.docx", "file_id": "f1", "folder_id": "d1"}]}))
    (fh.profile_dir / "index").mkdir(parents=True, exist_ok=True)
    (fh.profile_dir / "index" / "r_chung.json").write_text(json.dumps({
        "root_folder_id": "fr_chung", "items": {"Báo cáo": {"folder_id": "d1", "parent_folder_id": "fr_chung"},
                                                "Báo cáo/q3.docx": {"file_id": "f1", "parent_folder_id": "d1"}}}))
    return local


async def test_card_not_configured_then_status(env):
    e, tmp = env
    await e.login()
    assert 'hx-get="/partials/filehub"' in (await e.client.get("/settings")).text
    card = (await e.client.get("/partials/filehub")).text
    assert "Không gian số" in card and "thiếu binary sync" in card and "/do/filehub_setup" not in card
    local = _setup(e.hub, tmp)
    card = (await e.client.get("/partials/filehub")).text
    assert "có bản xung đột" in card and "filehub:chung" in card and str(local) in card and "⬆2" in card
    assert "<script>alert(1)</script>" not in card and "&lt;script&gt;" in card
    assert 'href="https://kgs.example.test/files/fr_chung"' in card and 'rel="noopener noreferrer"' in card
    assert 'action="/do/filehub_pause"' in card  # web cục bộ: có nút


async def test_pause_remove_over_public_link(env):
    """W7/#121: qua link public (proxy) vẫn tạm dừng/chạy lại/gỡ được — vẫn đăng nhập web + CSRF + khoá phiên + xác nhận (gỡ: bước 2), audit web."""
    e, tmp = env
    await e.login()
    local = _setup(e.hub, tmp)
    card = (await e.client.get("/partials/filehub", headers=PROXY)).text
    assert 'action="/do/filehub_pause"' in card and 'action="/do/filehub_login"' in card and 'action="/do/filehub_remove"' in card
    r = await e.post("/do/filehub_pause", {"next": "/settings#filehub"}, headers=PROXY)
    assert r.status_code == 200 and "Tạm dừng đồng bộ với Không gian số?" in r.text and not e.hub.filehub.paused()
    r = await e.client.post("/do/filehub_pause", data={"confirm": "yes", "csrf": e.csrf}, headers=PROXY)  # thiếu khoá phiên → 403
    assert r.status_code == 403 and not e.hub.filehub.paused()
    r = await e.post("/do/filehub_pause", {"confirm": "yes", "next": "/settings#filehub"}, headers=PROXY)
    assert r.status_code == 303 and e.hub.filehub.paused()
    assert audit_rows(e, "filehub.pause")[-1]["channel"] == "web"
    assert "tạm dừng" in (await e.client.get("/partials/filehub")).text
    r = await e.post("/do/filehub_resume", {"confirm": "yes", "next": "/settings#filehub"}, headers=PROXY)
    assert r.status_code == 303 and not e.hub.filehub.paused()
    r = await e.post("/do/filehub_remove", {"name": "chung", "next": "/settings#filehub"}, headers=PROXY)
    assert r.status_code == 200 and "Gỡ thư mục chung «chung»" in r.text and e.hub.filehub.folders()
    r = await e.post("/do/filehub_remove", {"name": "chung", "confirm": "yes", "next": "/settings#filehub"}, headers=PROXY)
    assert r.status_code == 200 and "Xác nhận lần 2" in r.text and e.hub.filehub.folders()  # gỡ = bước 2 bắt buộc
    r = await e.post("/do/filehub_remove", {"name": "chung", "confirm": "yes", "confirm2": "yes", "next": "/settings#filehub"}, headers=PROXY)
    assert r.status_code == 303 and not e.hub.filehub.folders() and local.is_dir()  # tệp trên máy giữ nguyên
    assert audit_rows(e, "filehub.remove")[-1]["channel"] == "web"


async def test_rename_from_web(env):
    """Đổi tên cfharness từ thẻ Cài đặt: xác nhận → `filehub.rename` (audit web); tên mới sai → lỗi ở web; tên cũ thành bí danh."""
    e, tmp = env
    await e.login()
    local = _setup(e.hub, tmp)
    card = (await e.client.get("/partials/filehub", headers=PROXY)).text
    assert 'action="/do/filehub_rename"' in card
    r = await e.post("/do/filehub_rename", {"name": "chung", "new_name": "Dự án", "next": "/settings#filehub"}, headers=PROXY)
    assert r.status_code == 200 and "Đổi tên thư mục chung «chung» thành «Dự án»" in r.text and e.hub.filehub.folders()[0]["name"] == "chung"
    r = await e.post("/do/filehub_rename", {"name": "chung", "new_name": "../x", "confirm": "yes", "next": "/settings#filehub"}, headers=PROXY)
    assert e.hub.filehub.folders()[0]["name"] == "chung"
    r = await e.post("/do/filehub_rename", {"name": "chung", "new_name": "Dự án", "confirm": "yes", "next": "/settings#filehub"}, headers=PROXY)
    assert r.status_code == 303
    f = e.hub.filehub.folders()[0]
    assert f["name"] == "Dự án" and f["aliases"] == ["chung"] and f["local_path"] == str(local)
    assert audit_rows(e, "filehub.rename")[-1]["channel"] == "web"
    card = (await e.client.get("/partials/filehub")).text
    assert "filehub:Dự án" in card and "tên cũ vẫn khớp" in card


async def test_setup_login_and_add_folder_over_public_link(env):
    """#121 qua link public (đã gộp với W7): form thiết lập không có ô binary/đường dẫn máy; đường dẫn máy từ cloud bị từ chối; binary gửi từ
    client bị bỏ qua; đăng nhập = relay (bước 2) → thẻ tự poll 3 s + nút "Mở trang đồng ý" → đồng ý xong tự nối thư mục; thư mục khác đã cấp →
    thêm từ web (local_path từ cloud bị từ chối)."""
    import httpx2 as httpx

    from fh_fake import fake_bin

    e, tmp = env
    await e.login()
    b = fake_bin(tmp / "bin", {"FAKE_TT_LOGIN_HOLD_S": "0.3"})
    e.hub.config.set("filehub.sync_binary", str(b))  # admin đã cài binary (web không bao giờ đặt được)
    e.hub.config.set("web.public_origin", "https://an.example.com")
    parent = (tmp / "KGS").resolve()
    e.hub.config.set("filehub.local_parent", str(parent))
    e.hub.config.set("filehub.tick_s", 0.05)
    card = (await e.client.get("/partials/filehub", headers=PROXY)).text
    assert 'action="/do/filehub_setup"' in card and 'name="server"' in card and 'name="local_parent"' not in card  # qua cloud: không sửa chỗ đặt
    assert 'name="binary"' not in card and 'name="scope"' in card
    yes = {"confirm": "yes", "confirm2": "yes", "next": "/settings#filehub"}
    # đường dẫn trên máy từ cloud → từ chối, không đổi cấu hình
    await e.post("/do/filehub_setup", {"server": "https://kgs.example.test", "scope": "folder", "name": "Báo cáo", "local_parent": str(tmp / "evil"),
                                       **yes}, headers=PROXY)
    assert not e.hub.config.get("filehub.server") and e.hub.filehub.login_state.get("state") == "none"
    # URL không phải https → lỗi kiểm ở web
    await e.post("/do/filehub_setup", {"server": "http://kgs.example.test", "scope": "folder", "name": "x", **yes}, headers=PROXY)
    assert not e.hub.config.get("filehub.server")
    # binary gửi từ client bị bỏ qua
    r = await _step2(e, "filehub_setup", {"server": "https://kgs.example.test", "scope": "folder", "name": "Báo cáo", "binary": "/bin/sh"}, PROXY)
    assert r.status_code == 303 and r.headers["location"] == "/filehub/login" and e.hub.config.get("filehub.sync_binary") == str(b)
    assert e.hub.config.get("filehub.server") == "https://kgs.example.test"
    card = (await e.client.get("/partials/filehub", headers=PROXY)).text
    assert "Mở trang đồng ý" in card and 'href="https://kgs.example.test/api/oauth/authorize?' in card
    assert 'hx-trigger="every 3s"' in card
    state = e.hub.filehub._relay["state"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=e.app), base_url=BASE) as anon:
        r = await anon.get("/filehub/oauth/cb", params={"code": "good_code", "state": state})
        assert r.status_code == 200
    assert await until(lambda: e.hub.filehub.folders(), timeout=15)
    f = e.hub.filehub.folders()[0]
    assert f["name"] == "Báo cáo" and f["local_path"] == str(parent / "Báo cáo") and (parent / "Báo cáo").is_dir()
    card = (await e.client.get("/partials/filehub", headers=PROXY)).text
    assert "filehub:Báo cáo" in card and 'hx-trigger="every 3s"' not in card
    assert "thêm thư mục khác" in card  # chế độ một thư mục: cùng một nút đăng nhập lại để cấp thêm thư mục
    assert audit_rows(e, "filehub.setup")[-1]["channel"] == "web"
    # root khác được cấp lúc đăng nhập (vd thêm thư mục) → thêm từ web; local_path từ cloud bị từ chối
    e.hub.filehub.login_state["roots"].append({"id": "r_hop", "ws_id": "ws_1", "name": "Họp"})
    card = (await e.client.get("/partials/filehub", headers=PROXY)).text
    assert 'action="/do/filehub_add"' in card and 'value="r_hop"' in card and 'name="local_path"' not in card
    await e.post("/do/filehub_add", {"root_id": "r_hop", "name": "Họp", "local_path": str(tmp / "evil"), "confirm": "yes"}, headers=PROXY)
    assert len(e.hub.filehub.folders()) == 1
    r = await e.post("/do/filehub_add", {"root_id": "r_hop", "name": "Họp", "confirm": "yes", "next": "/settings#filehub"}, headers=PROXY)
    assert r.status_code == 303 and [x["name"] for x in e.hub.filehub.folders()] == ["Báo cáo", "Họp"]
    await e.hub.filehub.stop()


async def test_card_workspace_account_root_files_revoked_rename(env):
    e, tmp = env
    await e.login()
    local = _setup(e.hub, tmp)
    e.hub.config.set("filehub.folders", [{"name": "admin harness", "root_id": "ws_9", "ws_id": "ws_9", "local_path": str(local), "kind": "workspace",
                                          "account": "chu.domain"}])
    e.hub.filehub.status_path.write_text(json.dumps({
        "version": "0.4.0", "state": "idle",
        "roots": [{"id": "ws_9", "name": "admin harness", "server_name": "Admin <b>Harness</b> 2", "kind": "workspace", "state": "disconnected",
                   "last_error": "root_revoked", "unpushable": [{"path": "<img src=x>.md", "kind": "root_files"}, {"path": "to.iso", "kind": "too_large"}],
                   "conflicts": []}]}))
    card = (await e.client.get("/partials/filehub")).text
    assert "Toàn bộ workspace «Admin &lt;b&gt;Harness&lt;/b&gt; 2»" in card and "chu.domain" in card
    assert "mất quyền" in card and "đăng nhập lại bằng tài khoản admin" in card and "mất quyền truy cập" in card
    assert "&lt;img src=x&gt;.md" in card and "<img src=x>" not in card and "Tệp ở gốc workspace không đồng bộ" in card
    assert "to.iso" in card and "too_large" in card
    assert "filehub:Admin &lt;b&gt;Harness&lt;/b&gt; 2" in card and "<b>Harness</b>" not in card
    assert "/do/filehub_switch" not in card  # đã là workspace
    # binary S5 thật: gỡ admin = tạm thời (root không ngắt) → cấp lại quyền là tự chạy tiếp, không bảo đăng nhập lại
    e.hub.filehub.status_path.write_text(json.dumps({
        "version": "0.4.0", "state": "idle",
        "roots": [{"id": "ws_9", "name": "admin harness", "kind": "workspace", "state": "error", "last_error": "root_revoked", "conflicts": []}]}))
    card = (await e.client.get("/partials/filehub")).text
    assert "mất quyền" in card and "không cần" in card and "Cấp lại quyền admin" in card
    e.hub.filehub.status_path.write_text(json.dumps({
        "version": "0.4.0", "state": "idle",
        "roots": [{"id": "ws_9", "name": "admin harness", "kind": "workspace", "state": "disconnected", "last_error": "workspace_deleted",
                   "local_path": str(local) + " (đã ngắt đồng bộ)", "conflicts": []}]}))
    card = (await e.client.get("/partials/filehub")).text
    assert "workspace đã bị xoá" in card and "(đã ngắt đồng bộ)" in card and "Cấp lại quyền admin" not in card


def _fh_bin(e, tmp, **env):
    from fh_fake import fake_bin

    b = fake_bin(tmp / "bin", {"FAKE_TT_WS_NAME": "admin harness", **env})
    e.hub.config.set("filehub.server", "https://kgs.example.test")
    e.hub.config.set("filehub.sync_binary", str(b))
    e.hub.config.set("filehub.local_parent", str(tmp / "KGS"))
    e.hub.config.set("filehub.tick_s", 0.05)
    e.hub.config.set("filehub.poll_s", 0.1)
    return b


async def _step2(e, action: str, data: dict, headers: dict | None = None):
    h = headers or {}
    r = await e.post(f"/do/{action}", {**data, "next": "/settings#filehub"}, headers=h)
    assert r.status_code == 200 and 'name="confirm"' in r.text, r.text[:500]
    r = await e.post(f"/do/{action}", {**data, "confirm": "yes", "next": "/settings#filehub"}, headers=h)
    assert r.status_code == 200 and "Xác nhận lần 2" in r.text and 'value="confirm2"' not in r.text
    return await e.post(f"/do/{action}", {**data, "confirm": "yes", "confirm2": "yes", "next": "/settings#filehub"}, headers=h)


async def test_setup_via_public_link_relay_oauth_callback(env):
    """Thiết lập từ web mở qua link public: bước 2 bắt buộc → daemon chạy binary với --redirect-uri <public_origin>/filehub/oauth/cb →
    /filehub/login đưa link trang đồng ý (tab mới) → Không gian số chuyển về route công khai (KHÔNG có cookie phiên) → đăng nhập xong."""
    import httpx2 as httpx

    from fh_fake import login_argv

    e, tmp = env
    await e.login()
    b = _fh_bin(e, tmp)
    e.hub.config.set("filehub.sync_binary", None)
    e.hub.config.set("filehub.server", None)
    proxy = {"x-forwarded-for": "203.0.113.9"}
    card = (await e.client.get("/partials/filehub", headers=proxy)).text
    assert "thiếu binary sync" in card and 'action="/do/filehub_setup"' not in card
    # qua link public: binary gửi từ web bị BỎ (web không bao giờ nhận đường dẫn binary) → chưa có binary cài sẵn → từ chối
    r = await e.post("/do/filehub_setup", {"server": "https://kgs.example.test", "binary": str(b), "scope": "workspace", "confirm": "yes",
                                           "confirm2": "yes", "next": "/settings#filehub"}, headers=proxy)
    assert r.status_code == 303 and e.hub.config.get("filehub.sync_binary") is None and not e.hub.filehub.login_state.get("url")
    e.hub.config.set("filehub.sync_binary", str(b))  # quản trị đặt sẵn ở terminal
    card = (await e.client.get("/partials/filehub", headers=proxy)).text
    assert 'action="/do/filehub_setup"' in card and "web.public_origin" in card and 'name="binary"' not in card
    e.hub.config.set("filehub.server", "https://kgs.example.test")
    r = await _step2(e, "filehub_setup", {"server": "https://kgs.example.test", "binary": str(b), "scope": "workspace"}, proxy)
    assert r.status_code == 303 and not e.hub.filehub.login_state.get("url")  # thiếu web.public_origin → lỗi, không chạy binary
    e.hub.config.set("web.public_origin", "https://an.example.com")
    r = await _step2(e, "filehub_setup", {"server": "https://kgs.example.test", "binary": str(b), "scope": "workspace"}, proxy)
    assert r.status_code == 303 and r.headers["location"] == "/filehub/login"
    argv = login_argv(e.hub)[-1]
    assert argv[argv.index("--redirect-uri") + 1] == "https://an.example.com/filehub/oauth/cb" and "--root-mode" in argv
    page = (await e.client.get("/filehub/login", headers=proxy)).text
    fh = e.hub.filehub
    state = fh._relay["state"]
    assert 'target="_blank" rel="noopener noreferrer"' in page and f"state={state}" in page and "Toàn bộ workspace" in page
    assert audit_rows(e, "filehub.setup")[-1]["channel"] == "web"
    # callback: trình duyệt quay về từ Không gian số — KHÔNG có cookie phiên (SameSite=Strict)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=e.app), base_url=BASE) as anon:
        r = await anon.get("/filehub/oauth/cb", params={"code": "c-<script>alert(1)</script>", "state": "sai" * 11})
        assert r.status_code == 400 and "<script>" not in r.text and "không hợp lệ hoặc đã hết hạn" in r.text
        assert fh._relay is not None  # lượt chờ thật còn nguyên
        r = await anon.get("/filehub/oauth/cb", params={"code": "good_code", "state": state})
        assert r.status_code == 200 and "Đã đăng nhập Không gian số" in r.text and "good_code" not in r.text and state not in r.text
        r = await anon.get("/filehub/oauth/cb", params={"code": "good_code", "state": state})  # dùng lại → từ chối
        assert r.status_code == 400
    assert fh.folders()[0]["kind"] == "workspace" and (fh.tokens_path.parent / "relay_code.txt").read_text() == "good_code"
    rows = audit_rows(e, "filehub.login_callback")
    assert rows and rows[-1]["channel"] == "web" and "good_code" not in json.dumps(rows) and state not in json.dumps(rows)
    assert "đã đăng nhập" in (await e.client.get("/partials/filehub/login")).text


async def test_callback_no_pending_xss_and_rate_limit(env):
    import httpx2 as httpx

    e, _ = env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=e.app), base_url=BASE) as anon:
        r = await anon.get("/filehub/oauth/cb", params={"code": "x", "state": "<svg onload=alert(1)>" * 3, "error": "<b>boom</b>"})
        assert r.status_code == 400 and "<svg" not in r.text and "<b>boom" not in r.text and "&lt;svg" not in r.text
        r = await anon.get("/filehub/oauth/cb")  # không có lượt chờ / không có state
        assert r.status_code == 400
        codes = [(await anon.get("/filehub/oauth/cb", params={"code": "x", "state": f"s{i:031d}"})).status_code for i in range(15)]
        assert codes[-1] == 429 and "Thử sai quá nhiều" in (await anon.get("/filehub/oauth/cb", params={"state": "y" * 32})).text
    assert not audit_rows(e, "filehub.login_callback")


async def test_login_on_hub_machine_uses_loopback_listener_and_switch_remove_step2(env):
    """Web trên chính máy hub: đăng nhập lại KHÔNG relay (listener loopback của binary); chuyển chế độ + gỡ cần bước 2."""
    from fh_fake import login_argv

    e, tmp = env
    await e.login()
    local = _setup(e.hub, tmp)
    _fh_bin(e, tmp)
    e.hub.config.set("filehub.folders", [{"name": "chung", "root_id": "r_chung", "ws_id": "ws_1", "local_path": str(local)}])
    card = (await e.client.get("/partials/filehub")).text
    assert 'action="/do/filehub_switch"' in card
    r = await _step2(e, "filehub_login", {})
    assert r.status_code == 303 and r.headers["location"] == "/filehub/login"
    argv = login_argv(e.hub)[-1]
    assert "--redirect-uri" not in argv and "--no-browser" in argv and "--root-mode" not in argv  # giữ chế độ một thư mục
    await until(lambda: e.hub.filehub.login_state.get("state") == "done")
    r = await _step2(e, "filehub_switch", {})
    assert r.status_code == 303 and r.headers["location"] == "/filehub/login"
    assert login_argv(e.hub)[-1][login_argv(e.hub)[-1].index("--root-mode") + 1] == "workspace"
    await until(lambda: e.hub.filehub.login_state.get("state") in ("done", "error"))
    assert [f["kind"] for f in e.hub.filehub.folders()] == ["workspace"] and local.exists()
    name = e.hub.filehub.folders()[0]["name"]
    r = await _step2(e, "filehub_remove", {"name": name})
    assert r.status_code == 303 and not e.hub.filehub.folders() and audit_rows(e, "filehub.remove")[-1]["channel"] == "web"


async def test_open_on_khong_gian_so_link_and_diff_chip(env):
    e, tmp = env
    await e.login()
    local = _setup(e.hub, tmp)
    r = await e.client.get("/filehub/open", params={"path": str(local / "Báo cáo" / "q3.docx")})
    assert r.status_code == 303 and r.headers["location"] == "https://kgs.example.test/files/d1?file=f1"
    r = await e.client.get("/filehub/open", params={"path": str(tmp / "khac.txt")})
    assert r.status_code == 404
    # run trên thư mục chung: chip tệp có "Không gian số", hoàn tác chỉ trên Không gian số
    e.hub.agents.create({"id": "w1", "name": "w1"})
    run = e.hub.runs.submit("w1", "viết", cwd="filehub:chung/Báo cáo")
    assert await until(lambda: (e.hub.store.get_run(run["id"]) or {}).get("status") in ("completed", "failed"))
    e.hub.store.update_run(run["id"], {"diff": {"kind": "dir", "root": str(local / "Báo cáo"), "filehub": "chung", "revertible": False,
                                                 "files": [{"path": "q3.docx", "status": "modified", "revertible": False}], "stat": {"files": 1}}})
    part = (await e.client.get(f"/partials/run/{run['id']}/diff")).text
    assert f'/filehub/open?run={run["id"]}&amp;path=q3.docx' in part and "Không gian số" in part
    assert "/do/run_revert" not in part and "thùng rác trên Không gian số" in part
    r = await e.client.get("/filehub/open", params={"run": run["id"], "path": "q3.docx"})
    assert r.status_code == 303 and r.headers["location"] == "https://kgs.example.test/files/d1?file=f1"


async def test_requires_login(env):
    e, _ = env
    r = await e.client.get("/filehub/open", params={"path": "/x"})
    assert r.status_code in (303, 401, 403)
    r = await e.client.get("/partials/filehub")
    assert r.status_code in (303, 401, 403)


async def test_lane_held_mass_delete_buttons(env, monkeypatch):
    """Cổng cfharness: lô xoá hàng loạt đang GIỮ của run lane → thẻ "Cổng cfharness" có nút "Cho xoá"/"Không xoá" (xác nhận, RPC
    `filehub.resume {root_id, discard}` HUMAN_ONLY sẵn có, audit web) — trước đây chỉ CLI `filehub resume --root … [--discard]`."""
    e, _tmp = env
    await e.login()
    e.hub.config.set("filehub.folders", [{"name": "chung", "root_id": "r_chung", "ws_id": "w", "kind": "folder", "mode": "lane", "server": "qltm"}])
    lane = e.hub.filehub.lane

    class W:
        root, tree, latencies, server = "r_chung", "t1", [], "qltm"

        def info(self):
            return {"tree": "t1", "run_id": "run_x", "agent_id": "w1", "folder": "chung", "sub": "<b>Báo cáo</b>", "mode": "tree", "pending": 30,
                    "blocked": 0, "failed": 0, "lazy": 0, "parked": False, "revoked": False, "held": 30, "root_id": "r_chung"}

        async def stop(self):
            pass

    calls = []

    async def release(root_id, *, discard=False):
        calls.append((root_id, discard))
        return {"root_id": root_id, "lane": True, "discarded" if discard else "released": 30, "trees": 1}

    monkeypatch.setattr(lane, "release_deletes", release)
    lane.workdirs["t1"] = W()
    try:
        card = (await e.client.get("/partials/filehub/lane")).text
        assert "30 mục xoá đang chờ duyệt" in card and "<b>Báo cáo</b>" not in card
        assert card.count('action="/do/filehub_lane_deletes"') == 2
        r = await e.post("/do/filehub_lane_deletes", {"root_id": "r_chung", "next": "/settings#filehub-lane"})
        assert r.status_code == 200 and "Cho agent xoá" in r.text and not calls  # xác nhận trước
        r = await e.post("/do/filehub_lane_deletes", {"root_id": "r_chung", "confirm": "yes", "next": "/settings#filehub-lane"})
        assert r.status_code == 303 and calls == [("r_chung", False)]
        r = await e.post("/do/filehub_lane_deletes", {"root_id": "r_chung", "discard": "1", "confirm": "yes", "next": "/settings#filehub-lane"})
        assert r.status_code == 303 and calls[-1] == ("r_chung", True)
        rows = audit_rows(e, "filehub.resume")
        assert rows[-1]["channel"] == "web"
    finally:
        lane.workdirs.pop("t1", None)
