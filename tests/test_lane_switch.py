"""Cổng cfharness H4 — presence của run ghi, chuyển chế độ sync ↔ lane (`filehub switch`: từ chối khi đồng bộ chưa yên / gốc chưa cấp,
bản sao cũ để NGUYÊN và được báo), `filehub lane add`, mặc định vẫn `sync`, thẻ web "Cổng cfharness" (khoá chỉ ở bước 2, không vào HTML/audit).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cfharness.errors import HubError
from cfharness.filehub.service import local_roots
from fake_kgs_lane import KEY, FakeLane
from lane_fake import close, configure_lane, make, until
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


def _sync_folder(d, tmp: Path, *, state="idle", pending_up=0) -> Path:
    local = (tmp / "KhongGianSo" / "chung").resolve()
    (local / "Báo cáo").mkdir(parents=True, exist_ok=True)
    (local / "Báo cáo" / "yeu-cau.md").write_text("viết\n")
    d.hub.config.set("filehub.server", "https://kgs.example.test")
    d.hub.config.set("filehub.folders", [{"name": "chung", "root_id": "r_chung", "ws_id": "w", "local_path": str(local)}])
    d.hub.config.set("filehub.enabled", True)
    fh = d.hub.filehub
    fh.prepare_files()
    fh.status_path.write_text(json.dumps({"version": "0.3.0", "state": state, "roots": [
        {"id": "r_chung", "name": "chung", "local_path": str(local), "state": state, "pending_up": pending_up, "pending_down": 0,
         "blocked": None, "conflicts": []}]}))
    return local


async def test_default_stays_sync_and_switch_keeps_old_copy(env, tmp_path):
    d, s, lane = env
    assert d.hub.config.get("filehub.mode") == "sync"
    local = _sync_folder(d, tmp_path, pending_up=2)
    await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
    assert d.hub.filehub.lane_target("filehub:chung") is None  # thiết lập cổng KHÔNG đổi chế độ thư mục đang đồng bộ
    assert d.hub.filehub.resolve_ref("filehub:chung")[0] == str(local)
    with pytest.raises(HubError, match="chưa yên"):  # còn thay đổi chờ đẩy → từ chối
        await rpc(d, "filehub.switch", {"name": "chung", "to": "lane"})
    _sync_folder(d, tmp_path)
    lane.roots.pop("r_chung")
    with pytest.raises(HubError, match="chưa được cấp"):
        await rpc(d, "filehub.switch", {"name": "chung", "to": "lane"})
    lane.roots["r_chung"] = __import__("fake_kgs_lane").Root("r_chung")
    r = await rpc(d, "filehub.switch", {"name": "chung", "to": "lane"})
    assert r["mode"] == "lane" and r["old_copy"] == str(local) and "GIỮ NGUYÊN" in r["note"]
    assert (local / "Báo cáo" / "yeu-cau.md").read_text() == "viết\n"  # hub KHÔNG xoá/di chuyển bản sao cũ
    assert str(local) not in local_roots(d.hub)  # bản sao cũ không còn là thư mục chung (guard/sandbox/khoá)
    assert d.hub.filehub.lane_target("filehub:chung/Báo cáo")["root"] == "r_chung"
    assert d.hub.filehub.stale_roots() == ["r_chung"]  # binary sync bỏ gốc (`--remove-root`)
    st = await rpc(d, "filehub.lane_status", {})
    assert st["folders"][0]["old_copy"] == str(local)
    audit = [tuple(x) for x in d.hub.store.conn.execute("SELECT kind FROM audit_log WHERE kind='filehub.switch'")]
    assert audit
    # về lại sync (bản sao còn) — run lane đang chạy thì từ chối
    await configure_lane(d, lane)  # (cấu hình lại thư mục lane cho run thử)
    d.hub.config.set("filehub.folders", [{"name": "chung", "root_id": "r_chung", "ws_id": "w", "local_path": str(local), "mode": "lane", "server": "qltm"}])
    s.by_prompt["GIỮ"] = {"text": "x", "hold": True}
    rid = d.hub.runs.submit("w1", "GIỮ", cwd="filehub:chung")["id"]
    await wait_status(d, rid)
    with pytest.raises(HubError, match="đang có run"):
        await rpc(d, "filehub.switch", {"name": "chung", "to": "sync"})
    d.hub.runs.runtime().release(rid)
    await wait_status(d, rid, ("completed",))
    r = await rpc(d, "filehub.switch", {"name": "chung", "to": "sync"})
    assert r["mode"] == "sync" and d.hub.filehub.resolve_ref("filehub:chung")[0] == str(local)


async def test_lane_add_and_global_mode(env):
    d, s, lane = env
    # thiết lập đã TỰ thêm gốc được cấp (test_lane_onboarding.py); `lane add` vẫn dùng được cho gốc chưa có (ca tay)
    await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
    d.hub.config.set("filehub.folders", [])
    r = await rpc(d, "filehub.lane_add", {"root_id": "r_chung", "name": "chung"})
    assert r["mode"] == "lane" and r["workspace"] == "filehub:chung" and d.hub.filehub.lane_target("filehub:chung")
    with pytest.raises(HubError, match="đã có"):
        await rpc(d, "filehub.lane_add", {"root_id": "r_chung", "name": "khac"})
    with pytest.raises(HubError, match="không được cấp"):
        await rpc(d, "filehub.lane_add", {"root_id": "r_la", "name": "la"})
    # `filehub.mode = lane` toàn hub: thư mục không đặt mode riêng cũng thành lane
    d.hub.config.set("filehub.folders", [{"name": "chung", "root_id": "r_chung", "local_path": "/tmp/x"}])
    assert d.hub.filehub.lane_target("filehub:chung") is None
    d.hub.config.set("filehub.mode", "lane")
    assert d.hub.filehub.lane_target("filehub:chung")["server"] == "qltm"


async def test_presence_while_writing_and_cleared_after(env):
    d, s, lane = env
    await configure_lane(d, lane)
    s.by_prompt["VIẾT"] = {"text": "x", "hold": True}
    rid = d.hub.runs.submit("w1", "VIẾT", cwd="filehub:chung/Báo cáo")["id"]
    run = await wait_status(d, rid)
    await until(lambda: ("r_chung", rid) in lane.presence)
    assert lane.presence[("r_chung", rid)]["label"] == "cfharness · w1"
    (Path(run["cwd"]) / "yeu-cau.md").write_text("sửa\n")
    await until(lambda: "đang sửa yeu-cau.md" in lane.presence.get(("r_chung", rid), {}).get("label", ""))
    put = [r for r in lane.requests if r["path"].endswith("/presence") and r["method"] == "PUT"][-1]
    assert put["headers"]["X-Lane-Agent"] == "w1" and put["headers"]["X-Lane-Run"] == rid
    d.hub.runs.runtime().release(rid)
    await wait_status(d, rid, ("completed",))
    await until(lambda: ("r_chung", rid) not in lane.presence)
    # run chỉ đọc → không presence
    s.by_prompt["ĐỌC"] = {"text": "x", "hold": True}
    rid2 = d.hub.runs.submit("w1", "ĐỌC", cwd="filehub:chung", run_opts={"read_only": True})["id"]
    await wait_status(d, rid2)
    import asyncio

    await asyncio.sleep(0.3)
    assert ("r_chung", rid2) not in lane.presence
    d.hub.runs.runtime().release(rid2)


# ============================================================================ web
@pytest.fixture
async def web(hub_home):
    from cfharness.runtime import FakeRuntime
    from test_web import ISOLATED, _start, _stop

    e = await _start(hub_home, FakeRuntime(ISOLATED))
    yield e
    await _stop(e)


async def test_web_lane_card_key_only_at_step2(web, lane):
    from test_web import audit_rows

    e = web
    await e.login()
    assert 'hx-get="/partials/filehub/lane"' in (await e.client.get("/settings")).text
    card = (await e.client.get("/partials/filehub/lane")).text
    assert "Cổng cfharness" in card and 'action="/do/filehub_lane_setup"' in card and 'name="key"' not in card
    # bước 1: xác nhận (không có ô khoá) → bước 2: ô password trống
    r = await e.post("/do/filehub_lane_setup", {"server": "qltm", "url": lane.url, "next": "/settings#filehub-lane"})
    assert r.status_code == 200 and "Nối hub với cổng cfharness" in r.text
    r = await e.post("/do/filehub_lane_setup", {"server": "qltm", "url": lane.url, "confirm": "yes", "next": "/settings#filehub-lane"})
    assert r.status_code == 200 and 'type="password"' in r.text and 'name="key"' in r.text and KEY not in r.text
    # URL ngoài máy → từ chối ngay ở web
    r = await e.post("/do/filehub_lane_setup", {"server": "qltm", "url": "http://10.0.0.1:8223", "confirm": "yes", "confirm2": "yes", "key": KEY,
                                                "next": "/settings#filehub-lane"})
    assert not e.hub.secrets.exists("FILEHUB_LANE_QLTM")
    r = await e.post("/do/filehub_lane_setup", {"server": "qltm", "url": lane.url, "confirm": "yes", "confirm2": "yes", "key": KEY,
                                                "next": "/settings#filehub-lane"})
    assert r.status_code == 303 and e.hub.secrets.get("FILEHUB_LANE_QLTM") == KEY
    row = audit_rows(e, "filehub.lane_setup")[-1]
    assert row["channel"] == "web" and KEY not in json.dumps(row)
    card = (await e.client.get("/partials/filehub/lane")).text
    assert KEY not in card and KEY[:12] in card and "lamlinh" in card
