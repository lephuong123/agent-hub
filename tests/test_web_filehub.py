"""Web — thẻ "Không gian số" ở Cài đặt + link "Mở trên Không gian số" trên chip tệp (filehub/, plan-cloud-files §3.6).

Người dùng hub tự đăng nhập Không gian số + nối/gỡ thư mục + tạm dừng — cả qua link public (cloud). Web không bao giờ nhận đường dẫn binary
sync; đường dẫn bản sao trên máy chỉ đặt được khi web dùng trên chính máy chạy hub. Chữ từ binary sync (đường dẫn, bản xung đột) là dữ liệu →
autoescape. Luồng đăng nhập dùng binary sync GIẢ (`tests/fake_tangthu_sync.py`), còn lại status/index ghi tay.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_web import ISOLATED, _start, _stop, audit_rows, until  # noqa: F401 — khung test web


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
    e, tmp = env
    await e.login()
    local = _setup(e.hub, tmp)
    card = (await e.client.get("/partials/filehub", headers=PROXY)).text
    assert 'action="/do/filehub_pause"' in card and 'action="/do/filehub_remove"' in card
    r = await e.post("/do/filehub_pause", {"next": "/settings#filehub"}, headers=PROXY)
    assert r.status_code == 200 and "Tạm dừng đồng bộ với Không gian số?" in r.text and not e.hub.filehub.paused()
    r = await e.post("/do/filehub_pause", {"confirm": "yes", "next": "/settings#filehub"}, headers=PROXY)
    assert r.status_code == 303 and e.hub.filehub.paused()
    assert audit_rows(e, "filehub.pause")[-1]["channel"] == "web"
    assert "tạm dừng" in (await e.client.get("/partials/filehub")).text
    r = await e.post("/do/filehub_resume", {"confirm": "yes", "next": "/settings#filehub"}, headers=PROXY)
    assert r.status_code == 303 and not e.hub.filehub.paused()
    r = await e.post("/do/filehub_remove", {"name": "chung", "next": "/settings#filehub"}, headers=PROXY)
    assert r.status_code == 200 and "Gỡ thư mục chung «chung»" in r.text and e.hub.filehub.folders()
    r = await e.post("/do/filehub_remove", {"name": "chung", "confirm": "yes", "next": "/settings#filehub"}, headers=PROXY)
    assert r.status_code == 303 and not e.hub.filehub.folders() and local.is_dir()  # tệp trên máy giữ nguyên
    assert audit_rows(e, "filehub.remove")[-1]["channel"] == "web"


async def test_setup_login_and_add_folder_over_public_link(env):
    from fh_fake import fake_bin

    e, tmp = env
    await e.login()
    b = fake_bin(tmp / "bin", {"FAKE_TT_LOGIN_HOLD_S": "1.5"})
    e.hub.config.set("filehub.sync_binary", str(b))  # admin đã cài binary (web không bao giờ đặt được)
    parent = (tmp / "KGS").resolve()
    e.hub.config.set("filehub.local_parent", str(parent))
    e.hub.config.set("filehub.tick_s", 0.05)
    card = (await e.client.get("/partials/filehub", headers=PROXY)).text
    assert 'action="/do/filehub_setup"' in card and 'name="server"' in card and 'name="local_parent"' not in card  # qua cloud: không sửa chỗ đặt
    assert 'name="binary"' not in card
    # đường dẫn trên máy từ cloud → từ chối, không đổi cấu hình
    await e.post("/do/filehub_setup", {"server": "https://kgs.example.test", "name": "Báo cáo", "local_parent": str(tmp / "evil"),
                                       "confirm": "yes", "next": "/settings#filehub"}, headers=PROXY)
    assert not e.hub.config.get("filehub.server") and e.hub.filehub.login_state.get("state") == "none"
    # URL không phải https → lỗi kiểm ở web
    await e.post("/do/filehub_setup", {"server": "http://kgs.example.test", "name": "x", "confirm": "yes"}, headers=PROXY)
    assert not e.hub.config.get("filehub.server")
    # binary gửi từ client bị bỏ qua
    r = await e.post("/do/filehub_setup", {"server": "https://kgs.example.test", "name": "Báo cáo", "binary": "/bin/sh",
                                           "confirm": "yes", "next": "/settings#filehub"}, headers=PROXY)
    assert r.status_code == 303 and e.hub.config.get("filehub.sync_binary") == str(b)
    assert e.hub.config.get("filehub.server") == "https://kgs.example.test"
    card = (await e.client.get("/partials/filehub", headers=PROXY)).text
    assert "Mở trang đồng ý" in card and 'href="https://kgs.example.test/api/sync/oauth/authorize?' in card
    assert 'hx-trigger="every 3s"' in card
    assert await until(lambda: e.hub.filehub.folders(), timeout=15)
    f = e.hub.filehub.folders()[0]
    assert f["name"] == "Báo cáo" and f["local_path"] == str(parent / "Báo cáo") and (parent / "Báo cáo").is_dir()
    card = (await e.client.get("/partials/filehub", headers=PROXY)).text
    assert "filehub:Báo cáo" in card and 'hx-trigger="every 3s"' not in card
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
