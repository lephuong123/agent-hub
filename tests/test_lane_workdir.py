"""Cổng cfharness H3 — cây làm việc theo run: đẩy khi lưu (If-Match/If-None-Match, xoá → thùng rác, di chuyển, thư mục), khử tiếng vọng,
xung đột 409 → bản riêng, 423 → hàng đợi + thử lại, secret CHẶN TRƯỚC khi đẩy (không byte nào rời máy), infected, flush cuối run, mồ côi +
đẩy lại/bỏ, đỗ cây khi chờ người + dùng lại khi chạy tiếp, daemon chết giữa run → đẩy nốt. Server lane GIẢ + ScriptedRuntime.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from cfharness.redact import register_secret_values
from fake_kgs_lane import KEY, FakeLane
from lane_fake import close, configure_lane, events, make, steers, until
from m7_fake import rpc, wait_status

SECRET = "sk-test-SECRET-value-9f8e7d6c5b4a"


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


def puts(lane, path):
    return [r for r in lane.requests if r["method"] == "PUT" and r["path"].endswith("/content") and r["query"].get("path") == path]


async def finish(d, rid, status=("completed",)):
    d.hub.runs.runtime().release(rid)
    return await wait_status(d, rid, status)


async def test_push_on_save_create_delete_move_mkdir_and_echo(env):
    d, s, lane = env
    rid, cwd = await held(d, s)
    (cwd / "yeu-cau.md").write_text("agent sửa\n")
    await until(lambda: lane.file("Báo cáo/yeu-cau.md") == "agent sửa\n".encode())
    p = puts(lane, "Báo cáo/yeu-cau.md")[-1]
    assert p["headers"]["If-Match"].strip('"') and p["headers"]["X-Lane-Agent"] == "w1" and p["headers"]["X-Lane-Run"] == rid
    (cwd / "moi").mkdir()
    (cwd / "moi" / "bao-cao.md").write_text("# Báo cáo\n")
    await until(lambda: lane.file("Báo cáo/moi/bao-cao.md") == "# Báo cáo\n".encode())
    assert puts(lane, "Báo cáo/moi/bao-cao.md")[-1]["headers"]["If-None-Match"] == "*"
    os.replace(cwd / "cu.md", cwd / "luu-tru.md")  # đổi tên → POST move (không tạo bản mới + xoá)
    await until(lambda: lane.file("Báo cáo/luu-tru.md") == "bản cũ\n".encode() and lane.file("Báo cáo/cu.md") is None)
    assert any(r["path"].endswith("/move") for r in lane.requests)
    (cwd / "luu-tru.md").unlink()
    await until(lambda: lane.file("Báo cáo/luu-tru.md") is None)
    assert any(r["method"] == "DELETE" and r["query"].get("path") == "Báo cáo/luu-tru.md" for r in lane.requests)
    (cwd / "rong").mkdir()
    await until(lambda: "Báo cáo/rong" in lane.root().items)
    # tệp bị bỏ qua (claude CLI, rác) + symlink trỏ ra ngoài + liên kết cứng: không bao giờ đẩy
    (cwd / ".claude").mkdir()
    (cwd / ".claude" / "settings.local.json").write_text("{}")
    (cwd / ".DS_Store").write_text("x")
    (cwd / "hosts").symlink_to("/etc/hosts")
    os.link(cwd / "yeu-cau.md", cwd / "cung.md")
    await asyncio.sleep(0.5)
    names = {r["query"].get("path") for r in lane.requests if r["method"] == "PUT"}
    assert not {"Báo cáo/.claude/settings.local.json", "Báo cáo/.DS_Store", "Báo cáo/hosts", "Báo cáo/cung.md"} & names
    # khung tiếng vọng (self:true) của chính các lệnh ghi → không steer
    assert steers(d, rid) == []
    run = await finish(d, rid)
    diff = run["diff"]
    assert diff["kind"] == "lane" and diff["revertible"] is False and diff["filehub"] == "chung"
    st = {(f["status"], f["path"]) for f in diff["files"]}
    assert {("modified", "yeu-cau.md"), ("added", "moi/bao-cao.md"), ("renamed", "luu-tru.md"), ("deleted", "luu-tru.md")} <= st
    ch = events(d, "filehub.changed")[-1].payload
    assert ch["run_id"] == rid and "yeu-cau.md" in ch["paths"] and ch["lane"] is True
    assert events(d, "filehub.deleted")[-1].payload["paths"] == ["luu-tru.md"]
    with pytest.raises(Exception, match="Không gian số|filehub"):
        await rpc(d, "run.revert", {"id": rid})  # hoàn tác ở phiên bản/thùng rác Không gian số
    # cây có hardlink/symlink không đẩy → mồ côi (giữ lại cho người xem), không mất
    orph = d.hub.filehub.lane.orphans()
    assert not orph or all(o["tree"] == rid for o in orph)


async def test_conflict_409_keeps_both_versions(env):
    d, s, lane = env
    rid, cwd = await held(d, s)
    # người lưu bản mới mà KHÔNG có khung sự kiện (vd khung đến muộn) → lần đẩy của agent nhận 409
    it = lane.root().items["Báo cáo/yeu-cau.md"]
    it.set_content("bản của người\n".encode(), "human")
    it.rev += 1
    (cwd / "yeu-cau.md").write_text("bản của agent\n")
    await until(lambda: any("Conflict on yeu-cau.md" in x for x in steers(d, rid)))
    copy = next(n for n in lane.root().items if n.startswith("Báo cáo/yeu-cau (cfharness w1 "))
    assert lane.file(copy) == "bản của agent\n".encode() and lane.file("Báo cáo/yeu-cau.md") == "bản của người\n".encode()
    assert (cwd / "yeu-cau.md").read_text() == "bản của người\n" and (cwd / copy.split("/", 1)[1]).read_text() == "bản của agent\n"
    assert puts(lane, copy)[-1]["headers"]["If-None-Match"] == "*"
    assert events(d, "filehub.conflict")[-1].payload["path"] == "yeu-cau.md"
    # người sửa qua web trong lúc agent cũng đang sửa (khung đến TRƯỚC lần đẩy) → cũng giữ cả hai
    d.hub.config.set("filehub.lane.push_quiet_s", 5)
    (cwd / "yeu-cau.md").write_text("agent sửa tiếp\n")
    await asyncio.sleep(0.2)
    lane.human_write("Báo cáo/yeu-cau.md", "người sửa tiếp\n")
    await until(lambda: (cwd / "yeu-cau.md").read_text() == "người sửa tiếp\n")
    assert any((cwd / n).read_text() == "agent sửa tiếp\n" for n in os.listdir(cwd) if n.startswith("yeu-cau (cfharness"))
    await finish(d, rid)
    assert any(lane.file(n) == "agent sửa tiếp\n".encode() for n in lane.root().items if n.startswith("Báo cáo/yeu-cau (cfharness"))


async def test_locked_file_queued_then_saved(env):
    d, s, lane = env
    rid, cwd = await held(d, s)
    lane.lock("Báo cáo/yeu-cau.md", seconds=0.6)
    (cwd / "yeu-cau.md").write_text("đợi khoá\n")
    await until(lambda: any("is locked on Không gian số" in x for x in steers(d, rid)))
    assert lane.file("Báo cáo/yeu-cau.md") == "viết báo cáo\n".encode()
    await until(lambda: lane.file("Báo cáo/yeu-cau.md") == "đợi khoá\n".encode(), timeout=5)
    assert len([x for x in steers(d, rid) if "locked" in x]) == 1  # steer MỘT lần
    await finish(d, rid)


async def test_secret_blocks_push_before_any_byte_leaves(env):
    d, s, lane = env
    register_secret_values(SECRET)
    rid, cwd = await held(d, s)
    (cwd / "cau-hinh.md").write_text(f"token = {SECRET}\n")
    (cwd / "yeu-cau.md").write_text(f"lộ: {SECRET}\n")
    await until(lambda: len(events(d, "filehub.secret_blocked")) >= 2)
    await asyncio.sleep(0.3)
    assert not puts(lane, "Báo cáo/cau-hinh.md") and not puts(lane, "Báo cáo/yeu-cau.md")  # không một request nào mang tệp
    assert all(SECRET.encode() not in (it.content or b"") for it in lane.root().items.values())
    assert any("NOT saved to Không gian số" in x and "cau-hinh.md" in x for x in steers(d, rid))
    st = await rpc(d, "fsself.call", {"run_id": rid, "op": "fs_status", "args": {}})
    assert set(st["blocked_paths"]) == {"cau-hinh.md", "yeu-cau.md"}
    # gỡ secret → đẩy bình thường
    (cwd / "yeu-cau.md").write_text("đã gỡ\n")
    await until(lambda: lane.file("Báo cáo/yeu-cau.md") == "đã gỡ\n".encode())
    # tool fs_write cũng bị chặn
    r = await rpc(d, "fsself.call", {"run_id": rid, "op": "fs_write", "args": {"path": "x.md", "content": f"k={SECRET}"}})
    assert r == {"path": "x.md", "saved": False, "reason": "secret_blocked"} and not puts(lane, "Báo cáo/x.md")
    run = await finish(d, rid)
    # còn tệp bị chặn → cây mồ côi (không mất, không lên cloud) + event push_failed
    pf = events(d, "filehub.push_failed")[-1].payload
    assert pf["run_id"] == rid and pf["code"] == "secret_blocked" and set(pf["paths"]) == {"cau-hinh.md", "x.md"}
    o = d.hub.filehub.lane.orphans()
    assert o[0]["tree"] == rid and (d.hub.paths.home / "filehub" / "lane-orphans" / rid / "tree" / "cau-hinh.md").exists()
    assert not Path(run["cwd"]).exists()
    assert oct((d.hub.paths.home / "filehub" / "lane-orphans").stat().st_mode & 0o777) == "0o700"
    # đẩy lại vẫn bị chặn (secret còn) → giữ; bỏ → xoá
    r = await rpc(d, "filehub.lane_orphan", {"tree": rid, "action": "push"})
    assert r["pushed"] is False and not puts(lane, "Báo cáo/cau-hinh.md")
    assert (await rpc(d, "filehub.lane_orphan", {"tree": rid, "action": "drop"}))["dropped"]
    assert not d.hub.filehub.lane.orphans()


async def test_infected_and_auth_failed_orphan_then_push_again(env):
    d, s, lane = env
    rid, cwd = await held(d, s)
    (cwd / "virus.txt").write_text("X5O EICAR")
    await until(lambda: events(d, "filehub.infected"))
    assert events(d, "filehub.infected")[-1].payload["path"] == "virus.txt" and lane.file("Báo cáo/virus.txt") is None
    d.hub.config.set("filehub.lane.push_quiet_s", 30)  # gộp dài → chỉ flush cuối run mới đẩy
    (cwd / "cuoi.md").write_text("lưu phút chót\n")
    await asyncio.sleep(0.2)
    assert lane.file("Báo cáo/cuoi.md") is None
    await finish(d, rid)
    assert lane.file("Báo cáo/cuoi.md") == "lưu phút chót\n".encode()  # flush cuối run bỏ qua gộp
    # khoá bị thu hồi giữa run → không đẩy được → mồ côi; người cấp lại khoá → đẩy lại được
    rid2, cwd2 = await held(d, s, prompt="LẦN-2")
    d.hub.config.set("filehub.lane.push_quiet_s", 0.1)
    lane.revoke()
    await until(lambda: d.hub.filehub.lane.states["qltm"]["state"] == "auth_failed")
    (cwd2 / "sau-thu-hoi.md").write_text("kẹt\n")
    await asyncio.sleep(0.3)
    await finish(d, rid2)
    pf = events(d, "filehub.push_failed")[-1].payload
    assert pf["run_id"] == rid2 and "sau-thu-hoi.md" in pf["paths"] and pf["code"] == "unauthorized"
    lane.revoked = False
    d.hub.filehub.lane._clients.clear()
    r = await rpc(d, "filehub.lane_orphan", {"tree": rid2, "action": "push"})
    assert r["pushed"] is True and lane.file("Báo cáo/sau-thu-hoi.md") == "kẹt\n".encode()
    assert all(o["tree"] != rid2 for o in d.hub.filehub.lane.orphans())


async def test_park_on_waiting_human_and_reuse_tree_on_continue(env, monkeypatch):
    d, s, lane = env
    monkeypatch.setattr(d.hub.runs, "on_approval_resolved", lambda run_id: None)  # approval giả "ap_test" — giữ run ở waiting_human
    rid, cwd = await held(d, s)
    (cwd / "nhap.md").write_text("nháp\n")
    await until(lambda: lane.file("Báo cáo/nhap.md"))
    d.hub.config.set("approval.pause_delay_s", 0)
    d.hub.runs.pause_for_human(rid, "ap_test")
    d.hub.runs.runtime().release(rid)
    await wait_status(d, rid, ("waiting_human",))
    await until(lambda: d.hub.filehub.lane.workdirs.get(rid) is not None and d.hub.filehub.lane.workdirs[rid].parked)
    assert cwd.exists() and (cwd / "nhap.md").read_text() == "nháp\n"  # cây giữ nguyên khi chờ người
    lane.human_write("Báo cáo/yeu-cau.md", "người sửa lúc agent chờ\n")
    await asyncio.sleep(0.3)
    d.hub.store.update_run(rid, {"status": "interrupted"})  # (như sau khi người quyết) — cho phép chạy tiếp session
    s.by_prompt["TIẾP"] = {"text": "xong", "hold": True}
    nr = d.hub.runs.continue_run(rid, "TIẾP")
    run2 = await wait_status(d, nr["id"])
    assert Path(run2["cwd"]) == cwd and run2["run_opts"]["filehub_lane"]["tree"] == rid  # CÙNG cây → claude --resume theo cwd
    assert (cwd / "yeu-cau.md").read_text() == "người sửa lúc agent chờ\n"  # đối soát base với server trước khi chạy tiếp
    spec = d.hub.runs.runtime().specs[nr["id"]]
    assert "<shared-folder-worktree>" not in spec.prompt  # resume: không chèn lại ghi chú
    assert "The human changed yeu-cau.md" in spec.prompt  # thay đổi của người lúc chờ → tin đầu của lượt chạy tiếp
    await finish(d, nr["id"])
    assert not cwd.exists()


async def test_daemon_died_mid_run_recovery_pushes_leftovers(env):
    d, s, lane = env
    svc = d.hub.filehub.lane
    d.hub.config.set("filehub.lane.push_quiet_s", 30)
    rid, cwd = await held(d, s)
    (cwd / "chua-day.md").write_text("chưa đẩy\n")
    await asyncio.sleep(0.2)
    wd = svc.workdir_of(rid)
    async with wd.lock:
        wd.scan()
        wd.persist()
    await wd.stop()  # "daemon chết": vòng quét mất, trạng thái còn trên đĩa
    svc.workdirs.clear()
    svc.by_run.clear()
    d.hub.store.update_run(rid, {"status": "failed"})
    await svc.recover()
    assert lane.file("Báo cáo/chua-day.md") == "chưa đẩy\n".encode() and not cwd.exists()
    assert not (d.hub.paths.home / "filehub" / "lane" / f"{rid}.json").exists()
    d.hub.runs.runtime().release(rid)


async def test_fs_write_tools(env):
    d, s, lane = env
    rid, cwd = await held(d, s)

    async def fs(op, **args):
        return await rpc(d, "fsself.call", {"run_id": rid, "op": op, "args": args})

    r = await fs("fs_write", path="ghi-chu.md", content="ghi ngay\n")
    assert r["saved"] is True and lane.file("Báo cáo/ghi-chu.md") == "ghi ngay\n".encode() and (cwd / "ghi-chu.md").read_text() == "ghi ngay\n"
    with pytest.raises(Exception, match="create_only"):
        await fs("fs_write", path="ghi-chu.md", content="x", create_only=True)
    assert (await fs("fs_mkdir", path="Phụ lục"))["created"] and "Báo cáo/Phụ lục" in lane.root().items
    assert (await fs("fs_move", **{"from": "ghi-chu.md", "to": "Phụ lục/ghi-chu.md"}))["moved"]
    assert (cwd / "Phụ lục" / "ghi-chu.md").exists() and lane.file("Báo cáo/Phụ lục/ghi-chu.md")
    vs = (await fs("fs_versions", path="yeu-cau.md"))["versions"]
    (cwd / "yeu-cau.md").write_text("v2\n")
    await until(lambda: lane.file("Báo cáo/yeu-cau.md") == b"v2\n")
    r = await fs("fs_restore", path="yeu-cau.md", version_id=vs[0]["version_id"])
    assert r["restored"] and lane.file("Báo cáo/yeu-cau.md") == "viết báo cáo\n".encode() and (cwd / "yeu-cau.md").read_text() == "viết báo cáo\n"
    assert (await fs("fs_delete", path="Phụ lục"))["trashed"] and "Báo cáo/Phụ lục" not in lane.root().items and not (cwd / "Phụ lục").exists()
    with pytest.raises(Exception):
        await fs("fs_write", path="../ngoai.md", content="x")
    link = await fs("fs_link", path="yeu-cau.md")
    assert link["url"] is None or "/files/" in link["url"]
    await asyncio.sleep(0.3)
    assert steers(d, rid) == []  # mọi khung do chính hub ghi → không steer
    await finish(d, rid)
    assert KEY not in "\n".join(str(tuple(r)) for t in ("events", "audit_log", "runs") for r in d.hub.store.conn.execute(f"SELECT * FROM {t}"))
