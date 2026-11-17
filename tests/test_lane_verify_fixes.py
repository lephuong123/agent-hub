"""Cổng cfharness — sửa theo vòng kiểm chứng (plan-filehub-lane.md §10 "Sửa theo kiểm chứng"): tool `fs_delete` thư mục đi CÙNG đường
xoá với quét (chặn xoá hàng loạt + đếm, bỏ qua tệp bị bỏ qua/vô hình, không xoá thư mục cha còn chứa chúng), dừng giữa chừng không để lại
base/tệp cũ, run chỉ đọc không bao giờ đẩy sau khi daemon khởi động lại, người di chuyển tệp lên tên agent vừa tạo (chưa đẩy) không ghi đè
bản của người, ghi đã chốt sau timeout không thành bản xung đột giả, tệp lười không bị tải khi người sửa, xung đột không làm tệp gốc biến mất.
Server lane GIẢ + ScriptedRuntime — không gọi Không gian số/claude thật.
"""

from __future__ import annotations

import asyncio

import pytest

from cfharness.errors import HubError
from cfharness.filehub.lane import workdir as wdmod
from fake_kgs_lane import FakeLane
from lane_fake import close, configure_lane, events, make, steers, until
from m7_fake import rpc
from test_lane_review_fixes import copies, finish, fsop, held


@pytest.fixture
def lane():
    f = FakeLane(ping_s=0.3)
    f.human_write("Báo cáo/yeu-cau.md", "viết báo cáo\n")
    f.human_write("Báo cáo/cu.md", "bản cũ\n")
    yield f
    f.close()


@pytest.fixture
async def env(hub_home, lane):
    d, s = await make(hub_home)
    await configure_lane(d, lane)
    yield d, s, lane
    await close(d)


def deletes(lane):
    return [r["query"]["path"] for r in lane.requests if r["method"] == "DELETE" and "/item" in r["path"]]


# ---------------------------------------------------------------- 1. fs_delete thư mục: chặn xoá hàng loạt + bỏ qua tệp bị bỏ qua
async def test_fs_delete_folder_goes_through_mass_delete_gate_and_skips_ignored(env):
    d, s, lane = env
    for i in range(30):
        lane.human_write(f"Báo cáo/lon/f{i:02}.md", f"tệp {i}\n")
    lane.human_write("Báo cáo/lon/~$lock.docx", "khoá Office của người\n")
    lane.human_write("Báo cáo/lon/.git/config", "git của người\n")
    rid, cwd = await held(d, s)
    r = await fsop(d, rid)("fs_delete", path="lon")
    assert r["trashed"] is False and r.get("held") is True
    assert not deletes(lane)  # GIỮ — chưa xoá gì trên server
    b = events(d, "filehub.blocked")[-1].payload
    assert b["reason"] == "mass_delete" and b["count"] == 30 and b["lane"] is True
    assert any("ON HOLD" in x for x in steers(d, rid))
    await rpc(d, "filehub.resume", {"root_id": "r_chung"})
    await until(lambda: not any(lane.file(f"Báo cáo/lon/f{i:02}.md") for i in range(30)))
    await asyncio.sleep(0.3)
    assert lane.file("Báo cáo/lon/~$lock.docx") == "khoá Office của người\n".encode()
    assert lane.file("Báo cáo/lon/.git/config") == "git của người\n".encode()
    assert len(deletes(lane)) == 30 and "Báo cáo/lon" not in deletes(lane) and "Báo cáo/lon/.git" not in deletes(lane)
    await finish(d, rid)


async def test_fs_delete_folder_with_ignored_items_keeps_parent_and_tells_agent(env):
    d, s, lane = env
    lane.human_write("Báo cáo/nho/a.md", "a\n")
    lane.human_write("Báo cáo/nho/con/b.md", "b\n")
    lane.human_write("Báo cáo/nho/~$a.docx", "khoá\n")
    rid, cwd = await held(d, s)
    r = await fsop(d, rid)("fs_delete", path="nho")
    assert r["trashed"] is False and r["deleted"] == 3 and r.get("kept_parent") and "nho" in r.get("note", "")
    assert sorted(deletes(lane)) == ["Báo cáo/nho/a.md", "Báo cáo/nho/con", "Báo cáo/nho/con/b.md"]
    assert lane.file("Báo cáo/nho/~$a.docx") == "khoá\n".encode() and "Báo cáo/nho" in lane.root().items
    wd = d.hub.filehub.lane.workdir_of(rid)
    assert wd.del_count == 3  # đếm chung với quét
    await finish(d, rid)


# ---------------------------------------------------------------- 2. fs_delete thư mục dừng giữa chừng: base + đĩa cập nhật theo từng mục
async def test_fs_delete_folder_stopped_midway_updates_base_per_child(env):
    d, s, lane = env
    for n in ("a", "b", "c"):
        lane.human_write(f"Báo cáo/td/{n}.md", f"{n}\n")
    lane.lock("Báo cáo/td/c.md")
    rid, cwd = await held(d, s)
    wd = d.hub.filehub.lane.workdir_of(rid)
    try:
        r = await fsop(d, rid)("fs_delete", path="td")
    except HubError:
        r = None
    assert lane.file("Báo cáo/td/a.md") is None and lane.file("Báo cáo/td/b.md") is None
    assert "td/a.md" not in wd.base and "td/b.md" not in wd.base
    assert not (cwd / "td" / "a.md").exists() and not (cwd / "td" / "b.md").exists()
    assert r is not None and r["trashed"] is False and "td/c.md" in r.get("queued", [])
    lane.unlock("Báo cáo/td/c.md")
    d.hub.config.set("filehub.lane.final_flush_s", 3)
    for e in wd.pending.values():
        e["retry_at"] = 0
    await until(lambda: "Báo cáo/td" not in lane.root().items, timeout=8)
    await finish(d, rid)


# ---------------------------------------------------------------- 3. run chỉ đọc không bao giờ đẩy sau khi daemon khởi động lại
async def test_read_only_tree_never_pushed_on_recovery(env):
    d, s, lane = env
    svc = d.hub.filehub.lane
    s.by_prompt["ĐỌC"] = {"text": "xong", "hold": True}
    from m7_fake import wait_status

    rid = d.hub.runs.submit("w1", "ĐỌC", cwd="filehub:chung/Báo cáo", run_opts={"read_only": True})["id"]
    run = await wait_status(d, rid)
    wd = svc.workdir_of(rid)
    assert wd.read_only
    from pathlib import Path

    cwd = Path(run["cwd"])
    (cwd / "yeu-cau.md").write_text("không được đẩy\n")
    wd.base["yeu-cau.md"] = {"type": "file", "rev": 1, "version_id": lane.root().items["Báo cáo/yeu-cau.md"].versions[0]["version_id"], "sha256": "x"}
    wd.pending["yeu-cau.md"] = {"since": 0.0, "op": "write"}
    wd.persist()
    await wd.stop()
    svc.workdirs.clear()
    svc.by_run.clear()
    d.hub.store.update_run(rid, {"status": "failed"})
    n0 = len(lane.writes())
    await svc.recover()
    assert len(lane.writes()) == n0 and lane.file("Báo cáo/yeu-cau.md") == "viết báo cáo\n".encode()
    assert not cwd.exists() and not (svc.state_dir / f"{rid}.json").exists()
    d.hub.runs.runtime().release(rid)


# ---------------------------------------------------------------- 4. người di chuyển tệp lên tên agent vừa tạo (chưa đẩy)
async def test_human_move_onto_unpushed_agent_file_keeps_both(env):
    d, s, lane = env
    d.hub.config.set("filehub.lane.push_quiet_s", 30)
    rid, cwd = await held(d, s)
    (cwd / "moi.md").write_text("agent tạo\n")
    wd = d.hub.filehub.lane.workdir_of(rid)
    await until(lambda: "moi.md" in wd.pending)
    lane.human_move("Báo cáo/cu.md", "Báo cáo/moi.md")
    await until(lambda: any("moi.md" in x for x in steers(d, rid)))
    await asyncio.sleep(0.2)
    assert (cwd / "moi.md").read_text() == "bản cũ\n" and not (cwd / "cu.md").exists()
    d.hub.config.set("filehub.lane.final_flush_s", 3)
    await finish(d, rid)
    assert lane.file("Báo cáo/moi.md") == "bản cũ\n".encode()
    assert "Báo cáo/cu.md" not in lane.root().items
    assert list(copies(lane, "moi").values()) == ["agent tạo\n".encode()]


# ---------------------------------------------------------------- 5. ghi đã chốt sau timeout → lần sửa sau không thành xung đột giả
async def test_committed_after_timeout_then_edit_is_not_a_conflict(env):
    d, s, lane = env
    d.hub.config.set("filehub.lane.write_timeout_s", 0.4)
    rid, cwd = await held(d, s)
    lane.put_delay["Báo cáo/yeu-cau.md"] = 0.8
    (cwd / "yeu-cau.md").write_text("bản A\n")
    await until(lambda: lane.file("Báo cáo/yeu-cau.md") == "bản A\n".encode(), timeout=5)
    (cwd / "yeu-cau.md").write_text("bản B\n")
    await until(lambda: lane.file("Báo cáo/yeu-cau.md") == "bản B\n".encode(), timeout=8)
    await asyncio.sleep(0.3)
    assert not copies(lane, "yeu-cau") and not any("Conflict" in x for x in steers(d, rid))
    await finish(d, rid)


# ---------------------------------------------------------------- 6. tệp lười: người sửa → chỉ cập nhật metadata, không tải
async def test_human_edit_on_lazy_file_stays_lazy(env):
    d, s, lane = env
    lane.human_write("Báo cáo/lon.bin", b"X" * 200)
    d.hub.config.set("filehub.lane.fetch_lazy_bytes", 50)
    rid, cwd = await held(d, s)
    wd = d.hub.filehub.lane.workdir_of(rid)
    lane.human_write("Báo cáo/lon.bin", b"Y" * 300)
    vid = lane.root().items["Báo cáo/lon.bin"].versions[0]["version_id"]
    await until(lambda: wd.base["lon.bin"].get("version_id") == vid)
    assert wd.base["lon.bin"].get("lazy") and (cwd / "lon.bin").read_bytes() == b"" and "lon.bin" in wd.lazy
    assert not [r for r in lane.requests if r["method"] == "GET" and r["path"].endswith("/content") and r["query"].get("path") == "Báo cáo/lon.bin"]
    await finish(d, rid)


# ---------------------------------------------------------------- 7. xung đột: tệp gốc không biến mất trong lúc tải bản của người
async def test_conflict_original_never_missing_during_download(env, monkeypatch):
    d, s, lane = env
    d.hub.config.set("filehub.lane.push_quiet_s", 30)
    rid, cwd = await held(d, s)
    wd = d.hub.filehub.lane.workdir_of(rid)
    seen = []
    orig = wdmod.Workdir._download

    async def spy(self, rel, it=None, **kw):
        seen.append((rel, self.fs.is_file(rel)))
        return await orig(self, rel, it, **kw)

    monkeypatch.setattr(wdmod.Workdir, "_download", spy)
    (cwd / "yeu-cau.md").write_text("agent sửa\n")
    await until(lambda: "yeu-cau.md" in wd.pending)
    lane.human_write("Báo cáo/yeu-cau.md", "người sửa\n")
    await until(lambda: any("Conflict" in x for x in steers(d, rid)))
    assert ("yeu-cau.md", True) in seen and ("yeu-cau.md", False) not in seen
    assert (cwd / "yeu-cau.md").read_text() == "người sửa\n"
    await finish(d, rid)
