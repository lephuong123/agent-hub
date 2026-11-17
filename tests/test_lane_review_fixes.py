"""Cổng cfharness — sửa theo review (plan-filehub-lane.md §10 "Sửa theo review"): symlink không bao giờ đưa thao tác của daemon ra ngoài cây,
tệp bị bỏ qua trên server không bị xoá, xoá của agent không đè bản người vừa lưu, base nhất quán (nội dung ↔ phiên bản), tệp lười không bị
ghi đè bằng nội dung cụt, Idempotency-Key dùng lại khi thử lại + timeout ghi > thời gian quét, chặn xoá hàng loạt chờ người, người xoá thư
mục không làm mất tệp chưa đẩy của agent, khử trùng khung không nuốt khung thiếu rev, hai bản xung đột cùng phút không đè nhau, `filehub status`
liệt kê thư mục lane. Server lane GIẢ + ScriptedRuntime — không gọi Không gian số/claude thật.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from cfharness.errors import HubError
from cfharness.filehub.lane import workdir as wdmod
from fake_kgs_lane import FakeLane
from lane_fake import close, configure_lane, events, make, steers, until
from m7_fake import rpc, wait_status


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


async def held(d, s, prompt="LÀM", cwd="filehub:chung/Báo cáo", agent="w1"):
    s.by_prompt[prompt] = {"text": "xong", "hold": True}
    r = d.hub.runs.submit(agent, prompt, cwd=cwd)
    run = await wait_status(d, r["id"])
    return run["id"], Path(run["cwd"])


async def finish(d, rid, status=("completed",)):
    d.hub.runs.runtime().release(rid)
    await wait_status(d, rid, status)
    await until(lambda: rid not in d.hub.runs.active)  # end_run (báo thay đổi, mồ côi/xoá cây) chạy SAU khi trạng thái đã ghi
    return d.hub.store.get_run(rid)


def fsop(d, rid):
    async def fs(op, **args):
        return await rpc(d, "fsself.call", {"run_id": rid, "op": op, "args": args})
    return fs


def copies(lane, stem):
    return {n: lane.file(n) for n in lane.root().items if n.startswith(f"Báo cáo/{stem} (cfharness w1 ")}


# ---------------------------------------------------------------- 1. symlink: không bao giờ theo symlink ở BẤT KỲ cấp nào
async def test_fs_delete_through_symlinked_dir_never_touches_outside(env, tmp_path):
    d, s, lane = env
    d.hub.config.set("filehub.lane.push_quiet_s", 30)
    victim = tmp_path / "victim"
    (victim / "sub").mkdir(parents=True)
    (victim / "sub" / "keep.txt").write_text("đừng xoá\n")
    rid, cwd = await held(d, s)
    fs = fsop(d, rid)
    await fs("fs_mkdir", path="L/sub")
    shutil.rmtree(cwd / "L")
    (cwd / "L").symlink_to(victim)
    with pytest.raises(HubError, match="symlink"):
        await fs("fs_delete", path="L/sub")
    assert (victim / "sub" / "keep.txt").read_text() == "đừng xoá\n"
    assert "Báo cáo/L/sub" in lane.root().items  # từ chối TRƯỚC khi gửi lệnh xoá
    await finish(d, rid)


async def test_fs_move_never_pulls_outside_file_into_tree(env, tmp_path):
    d, s, lane = env
    d.hub.config.set("filehub.lane.push_quiet_s", 30)
    outside = tmp_path / "secrets"
    outside.mkdir()
    (outside / "a.txt").write_text("BÍ-MẬT\n")
    rid, cwd = await held(d, s)
    fs = fsop(d, rid)
    await fs("fs_write", path="M/a.txt", content="mồi\n")
    shutil.rmtree(cwd / "M")
    (cwd / "M").symlink_to(outside)
    with pytest.raises(HubError, match="symlink"):
        await fs("fs_move", **{"from": "M/a.txt", "to": "N.txt"})
    assert (outside / "a.txt").read_text() == "BÍ-MẬT\n" and not (cwd / "N.txt").exists()
    await finish(d, rid)
    assert all(b"B\xc3\x8d-M\xe1\xba\xacT" not in (it.content or b"") for it in lane.root().items.values())


async def test_human_delete_and_move_through_symlinked_dir_stay_inside(env, tmp_path):
    d, s, lane = env
    d.hub.config.set("filehub.lane.push_quiet_s", 30)
    victim = tmp_path / "victim"
    (victim / "sub").mkdir(parents=True)
    (victim / "sub" / "keep.txt").write_text("đừng xoá\n")
    (victim / "a.txt").write_text("mồi\n")  # cùng nội dung với tệp trên server → nhánh "không sửa cục bộ" (os.replace)
    rid, cwd = await held(d, s)
    fs = fsop(d, rid)
    await fs("fs_mkdir", path="L/sub")
    await fs("fs_write", path="L/a.txt", content="mồi\n")
    shutil.rmtree(cwd / "L")
    (cwd / "L").symlink_to(victim)
    lane.human_delete("Báo cáo/L/sub")
    lane.human_move("Báo cáo/L/a.txt", "Báo cáo/b.txt")
    await until(lambda: any("moved L/a.txt" in x for x in steers(d, rid)))
    await asyncio.sleep(0.2)
    assert (victim / "sub" / "keep.txt").read_text() == "đừng xoá\n"
    assert (victim / "a.txt").read_text() == "mồi\n"
    await finish(d, rid)


async def test_push_never_reads_through_symlinked_parent(env, tmp_path):
    d, s, lane = env
    d.hub.config.set("filehub.lane.push_quiet_s", 0.6)
    outside = tmp_path / "out"
    outside.mkdir()
    (outside / "f.txt").write_text("NGOÀI-CÂY\n")
    rid, cwd = await held(d, s)
    (cwd / "D").mkdir()
    (cwd / "D" / "f.txt").write_text("trong cây\n")
    wd = d.hub.filehub.lane.workdir_of(rid)
    await until(lambda: "D/f.txt" in wd.pending)
    shutil.rmtree(cwd / "D")
    (cwd / "D").symlink_to(outside)
    await asyncio.sleep(1.2)
    assert lane.file("Báo cáo/D/f.txt") != "NGOÀI-CÂY\n".encode()
    d.hub.config.set("filehub.lane.final_flush_s", 1)
    await finish(d, rid)
    assert all("NGOÀI-CÂY".encode() not in (it.content or b"") for it in lane.root().items.values())
    assert (outside / "f.txt").read_text() == "NGOÀI-CÂY\n"


# ---------------------------------------------------------------- 2. tệp bị bỏ qua trên server không bao giờ bị xoá
async def test_ignored_server_files_are_never_trashed(env):
    d, s, lane = env
    for p in ("Báo cáo/~$draft.docx", "Báo cáo/x.tmp", "Báo cáo/.git/config"):
        lane.human_write(p, "của người\n")
    rid, cwd = await held(d, s)
    await asyncio.sleep(0.6)
    (cwd / "yeu-cau.md").write_text("agent sửa\n")
    await until(lambda: lane.file("Báo cáo/yeu-cau.md") == "agent sửa\n".encode())
    await finish(d, rid)
    for p in ("Báo cáo/~$draft.docx", "Báo cáo/x.tmp", "Báo cáo/.git/config"):
        assert lane.file(p) == "của người\n".encode(), p
    assert not [r for r in lane.requests if r["method"] == "DELETE" and "/item" in r["path"]]


# ---------------------------------------------------------------- 3. xoá của agent đang chờ không đè bản người vừa lưu
async def test_pending_agent_delete_cancelled_by_newer_human_save(env):
    d, s, lane = env
    d.hub.config.set("filehub.lane.push_quiet_s", 1.0)
    rid, cwd = await held(d, s)
    (cwd / "cu.md").unlink()
    wd = d.hub.filehub.lane.workdir_of(rid)
    await until(lambda: (wd.pending.get("cu.md") or {}).get("op") == "delete")
    lane.human_write("Báo cáo/cu.md", "người lưu bản mới\n")
    await until(lambda: any("cu.md" in x for x in steers(d, rid)))
    await asyncio.sleep(1.5)
    assert lane.file("Báo cáo/cu.md") == "người lưu bản mới\n".encode()
    assert (cwd / "cu.md").read_text() == "người lưu bản mới\n"
    assert any("NOT applied" in x and "cu.md" in x for x in steers(d, rid))
    await finish(d, rid)
    assert lane.file("Báo cáo/cu.md") == "người lưu bản mới\n".encode()


# ---------------------------------------------------------------- 4. base: version_id lấy từ ETag của lượt tải, không stat sau
async def test_download_base_matches_downloaded_content(env):
    d, s, lane = env
    rid, cwd = await held(d, s)
    fired = []

    def race(path):
        if path == "Báo cáo/yeu-cau.md" and not fired:
            fired.append(1)
            lane.human_write("Báo cáo/yeu-cau.md", "v3 của người\n")  # lưu tiếp NGAY sau khi bản v2 đã được chọn để trả

    lane.on_content = race
    lane.human_write("Báo cáo/yeu-cau.md", "v2 của người\n")
    await until(lambda: (cwd / "yeu-cau.md").read_text() == "v3 của người\n", timeout=4)
    wd = d.hub.filehub.lane.workdir_of(rid)
    assert wd.base["yeu-cau.md"]["version_id"] == lane.root().items["Báo cáo/yeu-cau.md"].versions[0]["version_id"]
    await finish(d, rid)


# ---------------------------------------------------------------- 5. tệp lười (chưa tải) không bị ghi đè bằng nội dung cụt
async def test_lazy_placeholder_never_overwrites_large_file(env):
    d, s, lane = env
    big = b"X" * 200
    lane.human_write("Báo cáo/lon.bin", big)
    d.hub.config.set("filehub.lane.fetch_lazy_bytes", 50)
    rid, cwd = await held(d, s)
    assert (cwd / "lon.bin").read_bytes() == b""
    (cwd / "lon.bin").write_bytes(b"cut")
    await until(lambda: any("fs_pull" in x and "lon.bin" in x for x in steers(d, rid)))
    await until(lambda: copies(lane, "lon"))
    assert lane.file("Báo cáo/lon.bin") == big
    assert list(copies(lane, "lon").values()) == [b"cut"]
    await finish(d, rid)
    assert lane.file("Báo cáo/lon.bin") == big


# ---------------------------------------------------------------- 6. thử lại sau timeout dùng lại Idempotency-Key; timeout ghi > quét
async def test_retry_after_timeout_reuses_idempotency_key(env):
    d, s, lane = env
    d.hub.config.set("filehub.lane.request_timeout_s", 0.4)
    d.hub.config.set("filehub.lane.write_timeout_s", 0.4)
    rid, cwd = await held(d, s)
    lane.put_delay["Báo cáo/yeu-cau.md"] = 1.0
    (cwd / "yeu-cau.md").write_text("agent sửa\n")
    await until(lambda: len([r for r in lane.requests if r["method"] == "PUT" and r["query"].get("path") == "Báo cáo/yeu-cau.md"]) >= 2, timeout=8)
    await asyncio.sleep(0.5)
    ps = [r for r in lane.requests if r["method"] == "PUT" and r["query"].get("path") == "Báo cáo/yeu-cau.md"]
    assert len({p["headers"]["Idempotency-Key"] for p in ps}) == 1
    assert not copies(lane, "yeu-cau") and not any("Conflict" in x for x in steers(d, rid))
    assert lane.file("Báo cáo/yeu-cau.md") == "agent sửa\n".encode()
    await finish(d, rid)


async def test_write_timeout_longer_than_server_scan(env):
    d, s, lane = env
    d.hub.config.set("filehub.lane.request_timeout_s", 0.4)  # đọc/stat ngắn — ghi dùng write_timeout_s (mặc định > chờ quét 300 s)
    assert float(d.hub.config.get("filehub.lane.write_timeout_s")) > 300
    rid, cwd = await held(d, s)
    lane.put_delay["Báo cáo/yeu-cau.md"] = 0.9
    (cwd / "yeu-cau.md").write_text("agent sửa\n")
    await until(lambda: lane.file("Báo cáo/yeu-cau.md") == "agent sửa\n".encode())
    await asyncio.sleep(1.2)
    assert len([r for r in lane.requests if r["method"] == "PUT" and r["query"].get("path") == "Báo cáo/yeu-cau.md"]) == 1
    assert not copies(lane, "yeu-cau")
    await finish(d, rid)


async def test_409_with_same_content_is_success(env):
    d, s, lane = env
    rid, cwd = await held(d, s)
    it = lane.root().items["Báo cáo/yeu-cau.md"]
    it.set_content("giống hệt\n".encode(), "cfharness")  # vd lần gửi trước đã chốt mà hub không nhận được phản hồi (khoá đã hết hạn)
    it.rev += 1
    (cwd / "yeu-cau.md").write_text("giống hệt\n")
    await until(lambda: any(r["method"] == "PUT" and r["query"].get("path") == "Báo cáo/yeu-cau.md" for r in lane.requests))
    await asyncio.sleep(0.5)
    assert not copies(lane, "yeu-cau") and not any("Conflict" in x for x in steers(d, rid))
    wd = d.hub.filehub.lane.workdir_of(rid)
    assert wd.base["yeu-cau.md"]["version_id"] == it.versions[0]["version_id"] and "yeu-cau.md" not in wd.pending
    await finish(d, rid)


# ---------------------------------------------------------------- 7. xoá hàng loạt: giữ lại, báo người, người quyết
async def _mass(d, s, lane):
    d.hub.config.set("filehub.lane.max_delete_batch", 3)
    for i in range(6):
        lane.human_write(f"Báo cáo/nhieu/f{i}.md", f"tệp {i}\n")
    rid, cwd = await held(d, s)
    for p in (cwd / "nhieu").iterdir():
        p.unlink()
    await until(lambda: events(d, "filehub.blocked"))
    await asyncio.sleep(0.5)
    assert all(lane.file(f"Báo cáo/nhieu/f{i}.md") for i in range(6))
    b = events(d, "filehub.blocked")[-1].payload
    assert b["reason"] == "mass_delete" and b["root_id"] == "r_chung" and b["count"] == 6 and b["lane"] is True and b["run_id"] == rid
    return rid, cwd


async def test_mass_delete_held_until_human_approves(env):
    d, s, lane = env
    rid, cwd = await _mass(d, s, lane)
    await rpc(d, "filehub.resume", {"root_id": "r_chung"})
    await until(lambda: not any(lane.file(f"Báo cáo/nhieu/f{i}.md") for i in range(6)))
    await finish(d, rid)


async def test_mass_delete_human_refuses_restores_files(env):
    d, s, lane = env
    rid, cwd = await _mass(d, s, lane)
    await rpc(d, "filehub.resume", {"root_id": "r_chung", "discard": True})
    await until(lambda: all((cwd / "nhieu" / f"f{i}.md").exists() for i in range(6)))
    assert any("NOT" in x and "delete" in x for x in steers(d, rid))
    await finish(d, rid)
    assert all(lane.file(f"Báo cáo/nhieu/f{i}.md") == f"tệp {i}\n".encode() for i in range(6))


async def test_mass_delete_unanswered_is_held_at_run_end(env):
    d, s, lane = env
    rid, _cwd = await _mass(d, s, lane)
    d.hub.config.set("filehub.lane.final_flush_s", 0.5)
    await finish(d, rid)
    assert all(lane.file(f"Báo cáo/nhieu/f{i}.md") for i in range(6))  # không bao giờ tự quyết
    assert events(d, "filehub.push_failed")[-1].payload["code"] == "mass_delete"


# ---------------------------------------------------------------- 8. người xoá thư mục: tệp chưa đẩy của agent bên trong không mất
async def test_human_folder_delete_keeps_agent_unpushed_files(env):
    d, s, lane = env
    lane.human_write("Báo cáo/td/a.md", "a của người\n")
    d.hub.config.set("filehub.lane.push_quiet_s", 30)
    rid, cwd = await held(d, s)
    (cwd / "td" / "moi.md").write_text("agent mới\n")
    (cwd / "td" / "a.md").write_text("agent sửa a\n")
    wd = d.hub.filehub.lane.workdir_of(rid)
    await until(lambda: "td/moi.md" in wd.pending and "td/a.md" in wd.pending)
    lane.human_delete("Báo cáo/td")
    await until(lambda: any("deleted td" in x for x in steers(d, rid)))
    await finish(d, rid)
    got = {n: lane.file(n) for n in lane.root().items if n.startswith("Báo cáo/td/")}
    assert "agent mới\n".encode() in got.values() and "agent sửa a\n".encode() in got.values()
    assert all("(cfharness w1 " in n for n in got if lane.root().items[n].type == "file")


# ---------------------------------------------------------------- 9. khử trùng không nuốt khung thiếu rev
async def test_dedupe_does_not_swallow_frames_without_rev(env):
    d, s, lane = env
    rid, cwd = await held(d, s)
    wd = d.hub.filehub.lane.workdir_of(rid)
    await wd.on_change({"root": "r_chung", "op": "create", "type": "folder", "id": "F1", "path": "Báo cáo/A", "rev": None, "self": False})
    assert (cwd / "A").is_dir()
    await wd.on_change({"root": "r_chung", "op": "move", "type": "folder", "id": "F1", "path": "Báo cáo/B", "from_path": "Báo cáo/A",
                        "rev": None, "self": False})
    assert (cwd / "B").is_dir() and not (cwd / "A").exists()
    await finish(d, rid)


# ---------------------------------------------------------------- 10. hai bản xung đột cùng phút không đè nhau
async def test_two_conflicts_same_minute_keep_both(env, monkeypatch):
    d, s, lane = env
    monkeypatch.setattr(wdmod, "_hhmm", lambda now=None: "1200")
    d.hub.config.set("filehub.lane.push_quiet_s", 5)
    rid, cwd = await held(d, s)
    (cwd / "yeu-cau.md").write_text("agent 1\n")
    await asyncio.sleep(0.2)
    lane.human_write("Báo cáo/yeu-cau.md", "người 1\n")
    await until(lambda: (cwd / "yeu-cau.md").read_text() == "người 1\n")
    (cwd / "yeu-cau.md").write_text("agent 2\n")
    await asyncio.sleep(0.2)
    lane.human_write("Báo cáo/yeu-cau.md", "người 2\n")
    await until(lambda: (cwd / "yeu-cau.md").read_text() == "người 2\n")
    local = {p.read_text() for p in cwd.iterdir() if p.name.startswith("yeu-cau (cfharness")}
    assert local == {"agent 1\n", "agent 2\n"}
    await finish(d, rid)
    assert set(copies(lane, "yeu-cau").values()) == {b"agent 1\n", b"agent 2\n"}


# ---------------------------------------------------------------- 11. `filehub status` liệt kê thư mục lane
async def test_filehub_status_lists_lane_folders(env, capsys):
    from cfharness.filehub.cli import _print_status

    d, _s, _lane = env
    st = await rpc(d, "filehub.status", {})
    assert [f["name"] for f in st["lane_folders"]] == ["chung"] and st["lane_folders"][0]["mode"] == "lane"
    _print_status(st)
    out = capsys.readouterr().out
    assert "chưa có thư mục chung" not in out and "chung" in out and "lane" in out


def test_conflict_name_unique(tmp_path):
    taken = {"a (cfharness w1 1200).md"}
    n = wdmod.conflict_name("a.md", "w1", taken=lambda x: x in taken, hhmm="1200")
    assert n == "a (cfharness w1 1200 2).md"
