"""Web — thẻ "Không gian số — cổng cfharness" ở Cài đặt + link "Mở trên Không gian số" trên chip tệp (filehub/). Đồng bộ 2 chiều cũ đã bỏ
(H5): không còn thiết lập/đăng nhập/relay OAuth/tạm dừng; thao tác còn lại (đổi tên, gỡ thư mục, lô xoá đang giữ) đi qua `ACTIONS` → RPC
`filehub.*` HUMAN_ONLY, audit `web`, làm được cả qua link public (gỡ có bước 2). Thư mục đồng bộ cũ: tests/test_filehub_h5.py.
"""

from __future__ import annotations

import pytest

from fake_kgs_lane import KEY, FakeLane
from test_web import BASE, ISOLATED, _start, _stop, audit_rows, until  # noqa: F401 — khung test web


PROXY = {"x-forwarded-for": "203.0.113.9"}  # như qua tunnel public → không phải web cục bộ


@pytest.fixture
async def env(hub_home, tmp_path):
    from cfharness.runtime import FakeRuntime

    e = await _start(hub_home, FakeRuntime(ISOLATED))
    yield e, tmp_path
    await _stop(e)


@pytest.fixture
def lane():
    f = FakeLane(ping_s=0.3)
    f.human_write("Báo cáo/q3.docx", "x")
    yield f
    f.close()


async def _lane_folder(e, lane) -> None:
    from m7_fake import rpc

    d = e.daemon if hasattr(e, "daemon") else None
    if d is not None:
        await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
    else:  # khung web không lộ daemon: đi đường service (cùng hàm RPC gọi)
        await e.hub.filehub.lane.setup({"server": "qltm", "url": lane.url, "key": KEY})
    e.hub.config.set("filehub.server", "https://kgs.example.test")


async def test_rename_remove_lane_folder_over_public_link(env, lane):
    e, _tmp = env
    await e.login()
    await _lane_folder(e, lane)
    assert [f["name"] for f in e.hub.filehub.lane_folders()] == ["chung"]
    card = (await e.client.get("/partials/filehub/lane", headers=PROXY)).text
    assert 'action="/do/filehub_rename"' in card and 'action="/do/filehub_remove"' in card and "filehub:chung" in card
    r = await e.post("/do/filehub_rename", {"name": "chung", "new_name": "../x", "confirm": "yes", "next": "/settings#filehub-lane"}, headers=PROXY)
    assert [f["name"] for f in e.hub.filehub.lane_folders()] == ["chung"]
    r = await e.post("/do/filehub_rename", {"name": "chung", "new_name": "Dự án", "confirm": "yes", "next": "/settings#filehub-lane"},
                     headers=PROXY)
    assert r.status_code == 303 and [f["name"] for f in e.hub.filehub.lane_folders()] == ["Dự án"]
    assert e.hub.filehub.lane_target("filehub:chung/Báo cáo")["folder"] == "Dự án"  # tên cũ = bí danh
    assert audit_rows(e, "filehub.rename")[-1]["channel"] == "web"
    r = await e.post("/do/filehub_remove", {"name": "Dự án", "confirm": "yes", "next": "/settings#filehub-lane"}, headers=PROXY)
    assert r.status_code == 200 and e.hub.filehub.lane_folders()  # bước 2 bắt buộc
    r = await e.post("/do/filehub_remove", {"name": "Dự án", "confirm": "yes", "confirm2": "yes", "next": "/settings#filehub-lane"}, headers=PROXY)
    assert r.status_code == 303 and not e.hub.filehub.lane_folders() and audit_rows(e, "filehub.remove")[-1]["channel"] == "web"


async def test_open_on_khong_gian_so_link_and_diff_chip(env, lane):
    e, _tmp = env
    await e.login()
    await _lane_folder(e, lane)
    items = lane.root().items
    r = await e.client.get("/filehub/open", params={"path": "chung/Báo cáo/q3.docx"})
    assert r.status_code == 303 and r.headers["location"] == f"https://kgs.example.test/files/{items['Báo cáo'].id}?file={items['Báo cáo/q3.docx'].id}"
    r = await e.client.get("/filehub/open", params={"path": "/tmp/khac.txt"})
    assert r.status_code == 404
    # run lane đã xong: chip tệp có "Không gian số" → link theo phạm vi của run
    e.hub.agents.create({"id": "w1", "name": "w1"})
    run = e.hub.runs.submit("w1", "viết", cwd="filehub:chung/Báo cáo")
    assert await until(lambda: (e.hub.store.get_run(run["id"]) or {}).get("status") in ("completed", "failed"))
    e.hub.store.update_run(run["id"], {"diff": {"kind": "lane", "root": "/x", "filehub": "chung", "revertible": False,
                                                 "lane": {"server": "qltm", "root": "r_chung", "sub": "Báo cáo"},
                                                 "files": [{"path": "q3.docx", "status": "modified", "revertible": False}], "stat": {"files": 1}}})
    part = (await e.client.get(f"/partials/run/{run['id']}/diff")).text
    assert f'/filehub/open?run={run["id"]}&amp;path=q3.docx' in part and "Không gian số" in part
    r = await e.client.get("/filehub/open", params={"run": run["id"], "path": "q3.docx"})
    assert r.status_code == 303 and r.headers["location"].endswith(f"?file={items['Báo cáo/q3.docx'].id}")


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
        assert r.status_code == 200 and "tệp trên Không gian số (vào thùng rác" in r.text and not calls  # xác nhận trước
        r = await e.post("/do/filehub_lane_deletes", {"root_id": "r_chung", "confirm": "yes", "next": "/settings#filehub-lane"})
        assert r.status_code == 303 and calls == [("r_chung", False)]
        r = await e.post("/do/filehub_lane_deletes", {"root_id": "r_chung", "discard": "1", "confirm": "yes", "next": "/settings#filehub-lane"})
        assert r.status_code == 303 and calls[-1] == ("r_chung", True)
        rows = audit_rows(e, "filehub.resume")
        assert rows[-1]["channel"] == "web"
    finally:
        lane.workdirs.pop("t1", None)
