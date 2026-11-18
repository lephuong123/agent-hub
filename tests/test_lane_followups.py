"""Cổng cfharness — việc còn lại của plan §10 (phía hub). Server lane GIẢ + ScriptedRuntime — không gọi Không gian số/claude thật.

1. `run revert` cho run lane = khôi phục TRÊN Không gian số bằng phiên bản/thùng rác (CHỈ người), chỉ khi mục chưa bị sửa tiếp sau run.
2. Presence vẫn được làm mới khi cây đỗ vì run chờ người (`waiting_human`) — plan §4.5 "waiting_human giữ cây + presence".
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from cfharness import auth
from cfharness.errors import HubError
from fake_kgs_lane import FakeLane
from lane_fake import close, configure_lane, events, make, until
from m7_fake import rpc, wait_status


@pytest.fixture
def lane():
    f = FakeLane(ping_s=0.3)
    f.human_write("Báo cáo/yeu-cau.md", "bản gốc\n")
    f.human_write("Báo cáo/cu.md", "tệp cũ\n")
    f.human_write("Báo cáo/a.md", "đổi tên tôi\n")
    yield f
    f.close()


@pytest.fixture
async def env(hub_home, lane):
    d, s = await make(hub_home)
    await configure_lane(d, lane)
    yield d, s, lane
    await close(d)


async def _run_with_changes(d, s, lane) -> str:
    """Run sửa yeu-cau.md, tạo moi.md + thư mục mới, xoá cu.md, đổi tên a.md → b.md; chờ run xong và cây được dọn."""
    s.by_prompt["SỬA"] = {"text": "xong", "hold": True}
    rid = d.hub.runs.submit("w1", "SỬA", cwd="filehub:chung/Báo cáo")["id"]
    run = await wait_status(d, rid)
    cwd = Path(run["cwd"])
    (cwd / "yeu-cau.md").write_text("agent sửa\n")
    (cwd / "moi.md").write_text("agent tạo\n")
    (cwd / "thu-muc-moi").mkdir()
    (cwd / "thu-muc-moi" / "x.md").write_text("x\n")
    (cwd / "cu.md").unlink()
    (cwd / "a.md").rename(cwd / "b.md")
    await until(lambda: lane.file("Báo cáo/moi.md") and lane.file("Báo cáo/b.md") and not lane.file("Báo cáo/cu.md")
                and lane.file("Báo cáo/yeu-cau.md") == "agent sửa\n".encode() and lane.file("Báo cáo/thu-muc-moi/x.md"))
    d.hub.runs.runtime().release(rid)
    await wait_status(d, rid, ("completed",))
    await until(lambda: rid not in d.hub.runs.active and not d.hub.filehub.lane.workdirs)
    return rid


# ============================================================================ 1. run revert
async def test_lane_run_revert_restores_versions_and_trash(env):
    d, s, lane = env
    rid = await _run_with_changes(d, s, lane)
    diff = d.hub.store.get_run(rid)["diff"]
    assert diff["kind"] == "lane" and diff["revertible"] is True
    assert all("content" not in o and "data" not in o for o in diff["revert_ops"])  # chỉ id phiên bản/lô thùng rác, không nội dung
    n_writes = len(lane.writes())
    dry = await rpc(d, "run.revert", {"id": rid, "dry_run": True})
    assert dry["dry_run"] and {"yeu-cau.md", "moi.md", "cu.md", "a.md", "b.md"} <= set(dry["reverted"])
    assert len(lane.writes()) == n_writes  # kiểm thử không ghi gì
    res = await rpc(d, "run.revert", {"id": rid})
    assert res["lane"] and not res["dry_run"]
    assert lane.file("Báo cáo/yeu-cau.md") == "bản gốc\n".encode()  # phiên bản trước run
    assert lane.file("Báo cáo/cu.md") == "tệp cũ\n".encode()  # lấy lại từ thùng rác
    assert lane.file("Báo cáo/a.md") == "đổi tên tôi\n".encode() and lane.file("Báo cáo/b.md") is None  # đổi tên lại
    assert lane.file("Báo cáo/moi.md") is None and "Báo cáo/thu-muc-moi" not in lane.roots["r_chung"].items  # tệp/thư mục run tạo → thùng rác
    assert any(b[1][0].path == "Báo cáo/moi.md" for b in lane.trash.values())  # không xoá hẳn
    assert d.hub.store.get_run(rid)["reverted_at"]
    ev = events(d, "run.reverted")
    assert ev and ev[-1].run_id == rid and ev[-1].payload.get("lane") is True
    with pytest.raises(HubError, match="đã được revert"):
        await rpc(d, "run.revert", {"id": rid})


async def test_lane_run_revert_refuses_when_modified_after_run(env):
    d, s, lane = env
    rid = await _run_with_changes(d, s, lane)
    lane.human_write("Báo cáo/yeu-cau.md", "người sửa tiếp\n")  # phiên bản mới hơn bản run để lại
    lane.human_write("Báo cáo/cu.md", "người tạo lại\n")  # chỗ run xoá đã có mục mới
    n_writes = len(lane.writes())
    with pytest.raises(HubError, match="đã bị sửa tiếp") as ei:
        await rpc(d, "run.revert", {"id": rid})
    assert set(ei.value.data["conflicts"]) == {"yeu-cau.md", "cu.md"}
    assert len(lane.writes()) == n_writes  # không ghi gì (kiểm TRƯỚC toàn bộ)
    assert lane.file("Báo cáo/moi.md") == "agent tạo\n".encode()
    assert not d.hub.store.get_run(rid).get("reverted_at")


async def test_lane_run_revert_without_revert_data_is_refused(env):
    d, s, lane = env
    rid = await _run_with_changes(d, s, lane)
    diff = dict(d.hub.store.get_run(rid)["diff"])
    diff.pop("revert_ops")
    diff["revertible"] = False  # run cũ (trước bản này): không có id phiên bản/lô thùng rác
    d.hub.store.update_run(rid, {"diff": diff})
    with pytest.raises(HubError, match="không có dữ liệu hoàn tác"):
        await rpc(d, "run.revert", {"id": rid})


async def test_lane_run_revert_refused_while_tree_parked(env):
    d, s, lane = env
    s.by_prompt["CHỜ"] = {"text": "xong", "hold": True}
    rid = d.hub.runs.submit("w1", "CHỜ", cwd="filehub:chung/Báo cáo")["id"]
    run = await wait_status(d, rid)
    (Path(run["cwd"]) / "moi.md").write_text("agent tạo\n")
    await until(lambda: lane.file("Báo cáo/moi.md"))
    await d.hub.filehub.lane.flush_run(rid)
    d.hub.store.update_run(rid, {"status": "interrupted"})
    await d.hub.filehub.lane.end_run(rid)  # cây đỗ (resume dùng lại)
    with pytest.raises(HubError, match="còn đang dùng"):
        await rpc(d, "run.revert", {"id": rid})
    assert lane.file("Báo cáo/moi.md")


async def test_lane_run_revert_refused_while_orphan_has_unpushed_changes(env):
    d, s, lane = env
    rid = await _run_with_changes(d, s, lane)
    svc = d.hub.filehub.lane
    (svc.orphan_dir / rid).mkdir(parents=True)  # cây mồ côi của chính run (thay đổi chưa đẩy)
    with pytest.raises(HubError, match="chưa đẩy"):
        await rpc(d, "run.revert", {"id": rid, "dry_run": True})


def test_run_revert_is_human_only():
    assert "run.revert" in auth.HUMAN_ONLY
    assert "run.revert" not in auth.MANAGER_ALLOWED


def test_web_diff_view_lane_revertible():
    from cfharness.web.extra_pages import diff_view, revert_confirm

    res = {"diff": {"kind": "lane", "revertible": True, "files": [{"status": "modified", "path": "a.md"}], "revert_ops": [{}]}}
    v = diff_view(res, {"status": "completed"})
    assert v["can_revert"] and v["lane"]
    old = {"diff": {"kind": "lane", "revertible": False, "filehub": "chung", "files": [{"status": "modified", "path": "a.md"}]}}
    v = diff_view(old, {"status": "completed"})
    assert not v["can_revert"] and "Không gian số" in v["why_not"]
    assert "thùng rác" in revert_confirm({"id": "r1", "lane": "1", "files": 2})


# ============================================================================ 2. presence khi chờ người
async def test_presence_refreshed_while_parked_waiting_human(env):
    d, s, lane = env
    d.hub.config.set("filehub.lane.presence_ttl_s", 30)
    s.by_prompt["CHỜ"] = {"text": "xong", "hold": True}
    rid = d.hub.runs.submit("w1", "CHỜ", cwd="filehub:chung/Báo cáo")["id"]
    run = await wait_status(d, rid)
    (Path(run["cwd"]) / "moi.md").write_text("agent tạo\n")
    await until(lambda: lane.presence.get(("r_chung", rid)))
    d.hub.store.update_run(rid, {"status": "waiting_human"})
    await d.hub.filehub.lane.end_run(rid)
    svc = d.hub.filehub.lane
    wd = svc.workdirs[d.hub.store.get_run(rid)["run_opts"]["filehub_lane"]["tree"]]
    assert wd.parked
    first = lane.presence[("r_chung", rid)]["until"]
    await svc.parked_presence(now=time.time() + 20)  # quá nửa ttl → làm mới (run vẫn chờ người)
    assert lane.presence[("r_chung", rid)]["until"] > first
    # hết chờ người theo kiểu kết thúc (huỷ) → không làm mới nữa, presence bị xoá khi cây đóng
    d.hub.store.update_run(rid, {"status": "cancelled"})
    before = lane.presence[("r_chung", rid)]["until"]
    await svc.parked_presence(now=time.time() + 60)
    assert lane.presence.get(("r_chung", rid), {}).get("until") in (None, before)


async def test_presence_not_refreshed_for_interrupted_park(env):
    d, s, lane = env
    d.hub.config.set("filehub.lane.presence_ttl_s", 30)
    s.by_prompt["CHỜ"] = {"text": "xong", "hold": True}
    rid = d.hub.runs.submit("w1", "CHỜ", cwd="filehub:chung/Báo cáo")["id"]
    run = await wait_status(d, rid)
    (Path(run["cwd"]) / "moi.md").write_text("agent tạo\n")
    await until(lambda: lane.presence.get(("r_chung", rid)))
    d.hub.store.update_run(rid, {"status": "interrupted"})
    await d.hub.filehub.lane.end_run(rid)
    n = sum(1 for r in lane.requests if r["path"].endswith("/presence") and r["method"] == "PUT")
    await d.hub.filehub.lane.parked_presence(now=time.time() + 20)
    assert sum(1 for r in lane.requests if r["path"].endswith("/presence") and r["method"] == "PUT") == n
