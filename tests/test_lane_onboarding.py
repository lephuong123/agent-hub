"""Cổng cfharness — "dán khoá xong là chạy được luôn" (nghiệm thu UI + e2e sau PR #147). Mỗi ca đỏ trước khi sửa.

- B0: `filehub.lane_setup` tự thêm MỌI gốc khoá được cấp thành thư mục chung chế độ lane (tên an toàn + duy nhất, gốc workspace → kind
  workspace); thư mục đồng bộ kiểu cũ cùng gốc KHÔNG bị đổi (chỉ báo). Mỗi `hello` mới sau đó đối soát: gốc mới → thêm, gốc mất quyền → đánh
  dấu (không xoá gì), cấp lại → hết đánh dấu; event `filehub.lane.roots`.
- B0b: bỏ trống địa chỉ → thử `filehub.lane.default_urls` (8223 rồi 8233), tên cổng lấy từ `hello.server.instance`.
- B1: `filehub.list` (tool `filehub_list` của điều phối viên) + `filehub.status` thấy thư mục chế độ lane; lỗi tên sai liệt kê tên có thật.
- B5: lô xoá đang giữ liệt kê đường dẫn (thẻ web, trang xác nhận, thông báo).
- B9/B10/B12: trạng thái cổng bằng chữ tiếng Việt + việc cần làm, kiểm tra sức khoẻ nền tự hồi phục `offline`, nút "Thử kết nối",
  ngày hết hạn theo giờ máy + số ngày nguyên, quay về `#filehub-lane`.
Server lane GIẢ (`fake_kgs_lane.py`), không gọi claude / Không gian số thật.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cfharness.errors import HubError
from fake_kgs_lane import KEY, FakeLane, Root
from lane_fake import close, events, make
from m7_fake import rpc, wait_status


@pytest.fixture
def lane():
    f = FakeLane(ping_s=0.3)
    f.human_write("Báo cáo/yeu-cau.md", "viết\n")
    yield f
    f.close()


@pytest.fixture
async def env(hub_home, lane):
    d, s = await make(hub_home)
    yield d, s, lane
    await close(d)


def _sync(d, tmp: Path, name: str, root: str) -> Path:
    local = (tmp / "KhongGianSo" / name).resolve()
    local.mkdir(parents=True, exist_ok=True)
    rows = [f for f in d.hub.filehub.all_folders()] + [{"name": name, "root_id": root, "ws_id": "w", "local_path": str(local)}]
    d.hub.config.set("filehub.folders", rows)
    d.hub.config.set("filehub.enabled", True)
    return local


def _folder(d, name):
    return next((f for f in d.hub.filehub.all_folders() if f["name"] == name), None)


# ============================================================================ B0
async def test_setup_auto_adds_every_granted_root_and_run_works(env, tmp_path):
    d, s, lane = env
    lane.roots["r_ws"] = Root("r_ws", kind="workspace", ws="r_ws", name="Phòng CCHC/2026")
    lane.roots["r_cu"] = Root("r_cu", name="cu")
    _sync(d, tmp_path, "chung", "r_khac")  # trùng TÊN với gốc r_chung (gốc khác) → tên mới duy nhất
    old = _sync(d, tmp_path, "cu", "r_cu")  # gốc r_cu còn cấu hình đồng bộ cũ (đã bỏ — H5) → TỰ chuyển sang cổng, bản sao để nguyên
    (old / "giu.txt").write_text("bản sao cũ\n")
    r = await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
    added = {x["name"]: x for x in r["added"]}
    assert set(added) == {"chung 2", "Phòng CCHC-2026"}
    assert added["chung 2"]["workspace"] == "filehub:chung 2"
    assert _folder(d, "chung 2")["mode"] == "lane" and _folder(d, "chung 2")["root_id"] == "r_chung"
    ws = _folder(d, "Phòng CCHC-2026")
    assert ws["kind"] == "workspace" and ws["mode"] == "lane" and ws["server"] == "qltm"
    assert _folder(d, "cu")["local_path"] == str(old) and _folder(d, "cu")["mode"] == "lane" and _folder(d, "cu")["server"] == "qltm"
    assert (old / "giu.txt").read_text() == "bản sao cũ\n"  # hub không đụng bản sao cũ
    assert [x["name"] for x in r["converted"]] == ["cu"] and r["converted"][0]["old_copy"] == str(old)
    assert _folder(d, "chung")["root_id"] == "r_khac" and d.hub.filehub.lane_target("filehub:chung") is None  # gốc không được cấp: vẫn cần chuyển
    ev = events(d, "filehub.lane.roots")[-1].payload
    assert ev["server"] == "qltm" and sorted(ev["added"]) == ["Phòng CCHC-2026", "chung 2"] and ev["converted"] == ["cu"]
    # dùng được NGAY: run với workspace filehub:<tên> dựng cây đúng gốc
    s.by_prompt["LÀM"] = {"text": "xong", "hold": True}
    rid = d.hub.runs.submit("w1", "LÀM", cwd="filehub:chung 2/Báo cáo")["id"]
    run = await wait_status(d, rid)
    assert (Path(run["cwd"]) / "yeu-cau.md").read_text() == "viết\n"
    d.hub.runs.runtime().release(rid)
    await wait_status(d, rid, ("completed",))
    # thiết lập lại (thay khoá) không thêm trùng
    r2 = await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
    assert r2["added"] == [] and r2["converted"] == [] and len(d.hub.filehub.lane_folders()) == 3


async def test_later_hello_reconciles_new_and_revoked_roots(env):
    d, s, lane = env
    await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
    svc = d.hub.filehub.lane
    assert _folder(d, "chung")["mode"] == "lane"
    lane.roots["r_moi"] = Root("r_moi", name="moi")
    await svc.hello("qltm", force=True)
    assert _folder(d, "moi")["root_id"] == "r_moi"
    lane.revoke_root("r_moi")
    await svc.hello("qltm", force=True)
    assert _folder(d, "moi") is not None  # không xoá gì
    st = {f["name"]: f for f in (await rpc(d, "filehub.lane_status", {}))["folders"]}
    assert st["moi"]["revoked"] is True and st["chung"]["revoked"] is False
    lst = {f["name"]: f for f in d.hub.filehub.list()}
    assert lst["moi"]["status"] == "revoked" and lst["chung"]["status"] == "ok"
    ev = events(d, "filehub.lane.roots")[-1].payload
    assert ev["revoked"] == ["moi"]
    n = len(events(d, "filehub.lane.roots"))
    await svc.hello("qltm", force=True)  # không đổi gì → không event mới
    assert len(events(d, "filehub.lane.roots")) == n
    lane.roots["r_moi"] = Root("r_moi", name="moi")
    await svc.hello("qltm", force=True)
    st = {f["name"]: f for f in (await rpc(d, "filehub.lane_status", {}))["folders"]}
    assert st["moi"]["revoked"] is False and events(d, "filehub.lane.roots")[-1].payload["regranted"] == ["moi"]
    # `lane add` vẫn dùng được cho trường hợp tay (gốc đã có → báo đã có)
    with pytest.raises(HubError, match="đã có"):
        await rpc(d, "filehub.lane_add", {"root_id": "r_moi", "name": "khac"})
    # xoá khoá: thư mục chỉ có qua cổng này rời khỏi cấu hình (không đụng tệp nào); dán lại khoá → trở lại
    r = await rpc(d, "filehub.lane_disable", {"server": "qltm"})
    assert r["removed"] and sorted(r["folders"]) == ["chung", "moi"] and not d.hub.filehub.lane_folders()
    assert lane.file("Báo cáo/yeu-cau.md") == "viết\n".encode()
    await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
    assert sorted(f["name"] for f in d.hub.filehub.lane_folders()) == ["chung", "moi"]


# ============================================================================ B0b
async def test_empty_url_uses_default_ports_and_server_name_from_hello(env):
    from cfharness.config import DEFAULTS

    d, s, lane = env
    assert DEFAULTS["filehub"]["lane"]["default_urls"] == ["http://127.0.0.1:8223", "http://127.0.0.1:8233"]
    d.hub.config.set("filehub.lane.default_urls", ["http://127.0.0.1:9", lane.url])  # cổng đầu không ai nghe → thử cổng sau
    r = await rpc(d, "filehub.lane_setup", {"key": KEY})
    assert r["server"] == "qltm" and r["url"] == lane.url and _folder(d, "chung")["mode"] == "lane"
    d.hub.config.set("filehub.lane.default_urls", ["http://127.0.0.1:9"])
    with pytest.raises(HubError, match="127.0.0.1:9"):
        await rpc(d, "filehub.lane_setup", {"server": "cchc", "key": KEY})


def test_cli_setup_url_optional():
    from cfharness.cli import build_parser

    a = build_parser().parse_args(["filehub", "lane", "setup"])
    assert a.url is None and a.server is None


# ============================================================================ B1
async def test_list_status_and_manager_see_lane_folders(env):
    from cfharness.manager.ops import ManagerOps

    d, s, lane = env
    await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
    rows = d.hub.filehub.list()
    assert rows == [{"name": "chung", "workspace": "filehub:chung", "mode": "lane", "kind": "folder", "root_id": "r_chung", "server": "qltm",
                     "status": "ok", "server_name": "chung", "local_path": None}]
    assert ManagerOps(d.hub.manager, {"id": "r_x", "agent_id": "boss"}).op_filehub_list({}) == rows
    st = await rpc(d, "filehub.status", {})
    assert st["lane_configured"] is True and st["lane_folders"][0]["name"] == "chung"
    with pytest.raises(HubError) as ei:
        d.hub.filehub.resolve_ref("filehub:khong-co")
    assert "chung" in ei.value.message and "filehub status" not in ei.value.message


# ============================================================================ B10
async def test_health_check_recovers_offline_with_backoff(env):
    d, s, lane = env
    await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
    svc = d.hub.filehub.lane
    if svc._health is not None:  # vòng nền (đồng hồ thật) — ca này gọi từng lượt với đồng hồ giả
        svc._health.cancel()
    svc._health_last.clear()

    def hellos():
        return sum(1 for r in lane.requests if r["path"].endswith("/hello"))

    svc._set_state("qltm", "offline", code="offline")
    n = hellos()
    await svc.health_check(now=1000.0)
    assert svc.states["qltm"]["state"] == "ok" and hellos() == n + 1
    await svc.health_check(now=1001.0)  # đang ok: nhịp thưa (health_ok_s)
    assert hellos() == n + 1
    await svc.health_check(now=1000.0 + float(d.hub.config.get("filehub.lane.health_ok_s")) + 1)
    assert hellos() == n + 2
    lane.fail_next("GET hello", 401, "unauthorized")
    await svc.health_check(now=5000.0)
    assert svc.states["qltm"]["state"] == "auth_failed"
    await svc.health_check(now=5001.0)  # lùi: chưa tới lượt thử lại
    assert hellos() == n + 3
    await svc.health_check(now=5000.0 + 6)
    assert svc.states["qltm"]["state"] == "ok"
    svc.start()
    assert svc._health is not None and not svc._health.done()


# ============================================================================ B5
def test_blocked_notification_lists_paths():
    from cfharness.notify.slack import _filehub_text

    t = _filehub_text("filehub.blocked", {"folder": "chung", "root_id": "r_chung", "reason": "mass_delete", "lane": True, "count": 30,
                                          "paths": [f"a/{i}.txt" for i in range(30)]}, "w1")
    assert "a/0.txt" in t and "30" in t and "Cổng cfharness" in t


def test_workdir_info_lists_held_paths(tmp_path):
    from cfharness.filehub.lane.workdir import Workdir

    wd = Workdir.__new__(Workdir)
    wd.pending = {f"x/{i:03d}.txt": {"op": "delete", "held": True} for i in range(60)}
    held = Workdir.held_view(wd)
    assert held["count"] == 60 and len(held["paths"]) == 50 and held["paths"][0] == "x/000.txt"


# ============================================================================ web: B5, B9, B10, B12
@pytest.fixture
async def web(hub_home):
    from cfharness.runtime import FakeRuntime
    from test_web import ISOLATED, _start, _stop

    e = await _start(hub_home, FakeRuntime(ISOLATED))
    yield e
    await _stop(e)


async def test_web_lane_card_texts_test_button_and_redirect(web, lane):
    e = web
    await e.login()
    card = (await e.client.get("/partials/filehub/lane")).text
    assert 'placeholder="http://127.0.0.1:8223"' in card and 'value="http://127.0.0.1:8223"' not in card  # chỉ cần dán khoá; trống = tự thử 8223 rồi 8233
    r = await e.post("/do/filehub_lane_setup", {"url": lane.url, "next": "/settings#filehub-lane"})
    assert r.status_code == 200 and "Nhập lại giá trị ở trang Cài đặt" not in r.text
    r = await e.post("/do/filehub_lane_setup", {"url": lane.url, "confirm": "yes", "confirm2": "yes", "key": KEY, "next": "/settings#filehub-lane"})
    assert r.status_code == 303 and r.headers["location"].endswith("#filehub-lane")
    assert _folder(e, "chung")["mode"] == "lane"
    card = (await e.client.get("/partials/filehub/lane")).text
    assert "filehub:chung" in card and "còn 180 ngày" in card and "180.0" not in card
    assert re.search(r"hết hạn \d{2}/\d{2}/\d{4}", card) and "Z (" not in card
    assert 'action="/do/filehub_lane_test"' in card
    r = await e.post("/do/filehub_lane_test", {"server": "qltm", "next": "/settings#filehub-lane"})
    assert r.status_code == 303 and r.headers["location"].endswith("#filehub-lane")
    # khoá bị thu hồi → chữ tiếng Việt + việc cần làm, không "còn … ngày", không mã thô
    lane.revoke()
    r = await e.post("/do/filehub_lane_test", {"server": "qltm", "next": "/settings#filehub-lane"})
    card = (await e.client.get("/partials/filehub/lane")).text
    assert "tạo khoá mới" in card and "còn 1" not in card and ">auth_failed<" not in card
    # thẻ "Không gian số" (đồng bộ cũ) không còn nói "chưa thiết lập" khi đã dùng cổng
    fh = (await e.client.get("/partials/filehub")).text
    assert "chưa thiết lập" not in fh and "cổng cfharness" in fh


async def test_web_held_deletes_list_paths_on_card_and_confirm(web, monkeypatch):
    e = web
    await e.login()
    e.hub.config.set("filehub.folders", [{"name": "chung", "root_id": "r_chung", "ws_id": "w", "kind": "folder", "mode": "lane", "server": "qltm"}])
    lane = e.hub.filehub.lane
    paths = [f"Báo cáo/{i:03d}.txt" for i in range(60)]

    class W:
        root, tree, latencies, server = "r_chung", "t1", [], "qltm"

        def info(self):
            return {"tree": "t1", "run_id": "run_x", "agent_id": "w1", "folder": "chung", "sub": "", "mode": "tree", "pending": 60,
                    "blocked": 0, "failed": 0, "lazy": 0, "parked": False, "revoked": False, "held": 60, "held_paths": paths[:50],
                    "root_id": "r_chung"}

        async def stop(self):
            pass

    lane.workdirs["t1"] = W()
    try:
        card = (await e.client.get("/partials/filehub/lane")).text
        assert "Báo cáo/000.txt" in card and "Báo cáo/049.txt" in card and "và 10 mục nữa" in card
        r = await e.post("/do/filehub_lane_deletes", {"root_id": "r_chung", "next": "/settings#filehub-lane"})
        assert r.status_code == 200 and "Báo cáo/000.txt" in r.text and "và 10 mục nữa" in r.text
    finally:
        lane.workdirs.pop("t1", None)
