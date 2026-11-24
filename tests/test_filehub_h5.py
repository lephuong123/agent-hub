"""H5 (plan-filehub-lane §7) — gỡ đồng bộ 2 chiều cũ; cổng cfharness là chế độ duy nhất. Đường nâng cấp cho hub còn cấu hình đồng bộ:

- daemon khởi động: KHÔNG xoá gì (bản sao trên máy, `HUB_HOME/filehub/`, token thiết bị cũ), thư mục đồng bộ = "cần chuyển" với chữ cố định
  ở status / CLI / thẻ web / `filehub.list` của điều phối viên, event `filehub.state{needs_migration}`;
- run/task nhắm thư mục cần chuyển bị từ chối với chữ đó; run tạo trước H5 trên bản sao cũ (đang xếp hàng) → `failed`, không chạy trong bản sao;
- khoá cổng cấp đúng gốc → tự chuyển sang cổng (đối soát), bản sao cũ để nguyên;
- tiến trình `tang-thu-sync-headless` còn sót (ghi ở `filehub/supervisor.json`) → dừng; pid không khớp thời điểm khởi động → không đụng;
- khoá cấu hình cũ của đồng bộ không làm hỏng gì; lệnh CLI cũ in hướng dẫn; route relay OAuth/đăng nhập đã gỡ.
Không gọi claude, không gọi Không gian số thật (server lane giả `fake_kgs_lane.py`).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from cfharness.errors import HubError
from cfharness.filehub.service import LEGACY_TEXT, legacy_text, stop_orphan_sync
from fake_kgs_lane import KEY, FakeLane, Root
from lane_fake import close, make, until
from m7_fake import rpc, wait_status

MSG = ("Thư mục «chung» đang dùng đồng bộ cũ (đã bỏ) — dán khoá cổng cfharness ở Cài đặt để dùng tiếp; bản sao trên máy để nguyên")


@pytest.fixture
def lane():
    f = FakeLane(ping_s=0.3)
    f.human_write("Báo cáo/yeu-cau.md", "viết\n")
    yield f
    f.close()


def _old_hub(hub_home, tmp: Path) -> Path:
    """config.yaml + tệp của một hub đang dùng đồng bộ 2 chiều cũ (trước H5): khoá cấu hình cũ, bản sao trên máy có metadata `.tangthu*`,
    trạng thái/hồ sơ của binary, token thiết bị."""
    local = (tmp / "KhongGianSo" / "chung").resolve()
    (local / "Báo cáo").mkdir(parents=True)
    (local / "Báo cáo" / "yeu-cau.md").write_text("bản trên máy\n")
    (local / ".tangthu.db").write_text("meta")
    cfg = {"filehub": {"enabled": True, "server": "https://kgs.example.test", "sync_binary": "/usr/local/bin/tang-thu-sync-headless",
                       "sync_min_version": "0.1.0", "device_name": "cfharness · Máy", "local_parent": str(local.parent), "scope": "workspace",
                       "mode": "sync", "max_delete_batch": 50, "tick_s": 1.0, "presence_ttl_s": 600, "steer_on_cloud_change": True,
                       "folders": [{"name": "chung", "root_id": "r_chung", "ws_id": "w", "local_path": str(local), "kind": "folder"}]}}
    hub_home.config.write_text(yaml.safe_dump(cfg, allow_unicode=True))
    fd = hub_home.home / "filehub" / "profile"
    fd.mkdir(parents=True)
    (fd / "base.sqlite").write_text("profile")
    (hub_home.home / "filehub" / "status.json").write_text(json.dumps({"state": "idle", "roots": [{"id": "r_chung"}]}))
    td = hub_home.home / "secrets" / "filehub"
    td.mkdir(parents=True, mode=0o700)
    (td / "tokens.json").write_text("{}")
    return local


def _intact(hub_home, local: Path) -> None:
    assert (local / "Báo cáo" / "yeu-cau.md").read_text() == "bản trên máy\n" and (local / ".tangthu.db").read_text() == "meta"
    assert (hub_home.home / "filehub" / "profile" / "base.sqlite").exists() and (hub_home.home / "filehub" / "status.json").exists()
    assert (hub_home.home / "secrets" / "filehub" / "tokens.json").exists()


def test_message_text():
    assert legacy_text("chung") == MSG and "{name}" in LEGACY_TEXT


# ============================================================================ khởi động với cấu hình cũ
async def test_upgrade_marks_needs_migration_refuses_runs_deletes_nothing(hub_home, tmp_path, capsys):
    local = _old_hub(hub_home, tmp_path)
    d, s = await make(hub_home)
    try:
        await asyncio.sleep(0.1)
        fh = d.hub.filehub
        st = await rpc(d, "filehub.status", {})
        assert st["state"] == "needs_migration" and st["folders"] == []
        assert [(x["name"], x["status"], x["message"], x["old_copy"]) for x in st["legacy"]] == [("chung", "needs_migration", MSG, str(local))]
        ev = [e.payload for e in d.hub.store.query_events(types=["filehub.state"])]
        assert ev and ev[-1] == {"state": "needs_migration", "folders": ["chung"]}
        # điều phối viên thấy nhưng được dặn không giao
        row = next(x for x in await rpc(d, "filehub.list", {}) if x["name"] == "chung")
        assert row["status"] == "needs_migration" and row["mode"] == "sync" and row["local_path"] is None and "do NOT assign" in row["note"]
        # CLI
        from cfharness.filehub.cli import _print_status

        _print_status(st)
        assert MSG in capsys.readouterr().out
        # run / task / workspace mặc định của agent / ref → từ chối với đúng chữ đó
        with pytest.raises(HubError, match="đồng bộ cũ"):
            d.hub.runs.submit("w1", "X", cwd="filehub:chung/Báo cáo")
        with pytest.raises(HubError) as ei:
            await rpc(d, "task.create", {"assignee": "w1", "title": "T", "body": "T", "workspace": "filehub:chung"})
        assert ei.value.message.endswith(MSG)
        d.hub.store.conn.execute("UPDATE agents SET workspace_default='filehub:chung' WHERE id='w2'")
        d.hub.store.conn.commit()
        with pytest.raises(HubError, match="đồng bộ cũ"):
            d.hub.runs.submit("w2", "Y")
        # run tạo TRƯỚC H5 trên bản sao cũ (đang xếp hàng): không chạy trong bản sao → failed với chữ đó
        s.by_prompt["CU"] = {"text": "không được chạy"}
        rid = d.hub.runs.submit("w1", "CU", cwd=str(local), run_opts={"workspace_kind": "filehub", "filehub": "chung"})["id"]
        run = await wait_status(d, rid, ("failed",))
        assert MSG in (run.get("error") or "") and not any("CU" in sp.prompt for sp in s.specs)
        # khoá cấu hình cũ không làm hỏng gì; không có tiến trình/thư mục nào bị xoá
        assert d.hub.config.get("filehub.sync_binary") == "/usr/local/bin/tang-thu-sync-headless"
        _intact(hub_home, local)
        # resume / rename / remove trên thư mục cần chuyển
        with pytest.raises(HubError, match="đồng bộ cũ"):
            await rpc(d, "filehub.resume", {"root_id": "r_chung"})
        await rpc(d, "filehub.remove", {"name": "chung"})
        assert not fh.legacy_folders() and (await rpc(d, "filehub.status", {}))["state"] == "not_configured"
        _intact(hub_home, local)
    finally:
        await close(d)


async def test_lane_key_granting_same_root_auto_converts(hub_home, tmp_path, lane):
    local = _old_hub(hub_home, tmp_path)
    lane.roots.pop("r_chung")  # khoá chưa cấp gốc của thư mục đồng bộ cũ
    d, s = await make(hub_home)
    try:
        r = await rpc(d, "filehub.lane_setup", {"server": "qltm", "url": lane.url, "key": KEY})
        assert r["converted"] == [] and d.hub.filehub.legacy_folders()
        assert (await rpc(d, "filehub.status", {}))["state"] == "needs_migration"
        # người cấp thêm gốc ở Không gian số → lần kiểm tra sau (vòng sức khoẻ / "Thử kết nối") tự chuyển
        lane.roots["r_chung"] = Root("r_chung", name="chung")
        lane.human_write("Báo cáo/yeu-cau.md", "viết\n")
        t = await rpc(d, "filehub.lane_test", {"server": "qltm"})
        assert t["ok"]
        f = next(x for x in d.hub.filehub.all_folders() if x["name"] == "chung")
        assert f["mode"] == "lane" and f["server"] == "qltm" and f["local_path"] == str(local)
        st = await rpc(d, "filehub.status", {})
        assert st["state"] == "ok" and st["legacy"] == [] and st["folders"][0]["old_copy"] == str(local)
        assert events_conv(d) == ["chung"]
        # dùng được ngay qua cổng: cây theo run lấy nội dung TRÊN Không gian số, không phải bản sao cũ
        s.by_prompt["LÀM"] = {"text": "xong", "hold": True}
        rid = d.hub.runs.submit("w1", "LÀM", cwd="filehub:chung/Báo cáo")["id"]
        run = await wait_status(d, rid)
        assert Path(run["cwd"]) != local / "Báo cáo" and (Path(run["cwd"]) / "yeu-cau.md").read_text() == "viết\n"
        d.hub.runs.runtime().release(rid)
        await wait_status(d, rid, ("completed",))
        _intact(hub_home, local)
        # gỡ cổng: chỉ cấu hình — bản sao cũ vẫn nguyên
        await rpc(d, "filehub.lane_disable", {"server": "qltm"})
        assert not d.hub.filehub.all_folders()
        _intact(hub_home, local)
    finally:
        await close(d)


def events_conv(d) -> list[str]:
    return [n for e in d.hub.store.query_events(types=["filehub.lane.roots"]) for n in e.payload.get("converted") or []]


# ============================================================================ tiến trình đồng bộ còn sót
def _sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)


def _alive(p: subprocess.Popen) -> bool:
    return p.poll() is None


@pytest.mark.skipif(sys.platform == "win32", reason="process group POSIX")
async def test_orphan_sync_process_stopped_on_start(hub_home, tmp_path):
    from cfharness.recovery import pid_start_time

    _old_hub(hub_home, tmp_path)
    p = _sleeper()
    try:
        st0 = None
        for _ in range(50):
            st0 = pid_start_time(p.pid)
            if st0:
                break
            time.sleep(0.05)
        (hub_home.home / "filehub" / "supervisor.json").write_text(json.dumps({"pid": p.pid, "start_time": st0}))
        d, _s = await make(hub_home)
        try:
            await until(lambda: not _alive(p), timeout=20)
            await until(lambda: d.hub.filehub.orphan_sync is not None)
            assert d.hub.filehub.orphan_sync["found"] and d.hub.filehub.orphan_sync["pid"] == p.pid
            assert not (hub_home.home / "filehub" / "supervisor.json").exists()
            assert (hub_home.home / "filehub" / "profile" / "base.sqlite").exists()  # chỉ dừng tiến trình — không xoá dữ liệu
        finally:
            await close(d)
    finally:
        if _alive(p):
            p.kill()
        p.wait()


@pytest.mark.skipif(sys.platform == "win32", reason="process group POSIX")
def test_orphan_sync_pid_reused_is_not_touched(tmp_path):
    p = _sleeper()
    try:
        (tmp_path / "filehub").mkdir()
        (tmp_path / "filehub" / "supervisor.json").write_text(json.dumps({"pid": p.pid, "start_time": time.time() - 86400}))
        r = stop_orphan_sync(tmp_path, grace_s=1)
        assert r == {"found": False, "stale": True} and _alive(p)
        assert not (tmp_path / "filehub" / "supervisor.json").exists()
        assert stop_orphan_sync(tmp_path) == {"found": False}  # không có tệp → không làm gì
    finally:
        p.kill()
        p.wait()


# ============================================================================ thao tác còn lại trên thư mục qua cổng
async def test_rename_remove_link_on_lane_folder(hub_home, lane):
    from lane_fake import configure_lane

    d, s = await make(hub_home)
    try:
        await configure_lane(d, lane)
        d.hub.config.set("filehub.server", "https://kgs.example.test")
        r = await rpc(d, "filehub.link", {"path": "chung/Báo cáo/yeu-cau.md"})
        items = lane.root().items
        assert r["url"] == f"https://kgs.example.test/files/{items['Báo cáo'].id}?file={items['Báo cáo/yeu-cau.md'].id}"
        r = await rpc(d, "filehub.rename", {"name": "chung", "new_name": "Dự án"})
        assert r["aliases"] == ["chung"] and d.hub.filehub.lane_target("filehub:chung/Báo cáo")["folder"] == "Dự án"
        s.by_prompt["GIỮ"] = {"text": "x", "hold": True}
        rid = d.hub.runs.submit("w1", "GIỮ", cwd="filehub:Dự án")["id"]
        await wait_status(d, rid)
        with pytest.raises(HubError, match="đang có run"):
            await rpc(d, "filehub.remove", {"name": "Dự án"})
        # link theo run (chip "Không gian số" ở diff của run)
        r = await rpc(d, "filehub.link", {"run_id": rid, "path": "Báo cáo"})
        assert r["url"] == f"https://kgs.example.test/files/{items['Báo cáo'].id}"
        d.hub.runs.runtime().release(rid)
        await wait_status(d, rid, ("completed",))
        # status=completed được ghi TRƯỚC khi run đóng cây lane (await) và nhả khoá thư mục → chờ run rời `active`
        # (pop và nhả khoá cùng một đoạn đồng bộ), nếu không máy chậm (CI macOS) gặp "đang có run".
        for _ in range(500):
            if rid not in d.hub.runs.active:
                break
            await asyncio.sleep(0.02)
        await rpc(d, "filehub.remove", {"name": "Dự án"})
        assert not d.hub.filehub.all_folders()
    finally:
        await close(d)


def test_cli_removed_commands_print_guidance(capsys):
    import argparse

    from cfharness.filehub import cli as C

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    C.add_parsers(sub, argparse.ArgumentParser(add_help=False))
    for argv in (["filehub", "setup", "--server", "https://x"], ["filehub", "login"], ["filehub", "pause"],
                 ["filehub", "switch", "chung", "--to", "lane"], ["filehub", "switch-to-workspace"], ["filehub", "add", "r1"]):
        a = ap.parse_args(argv)

        class Ctx:
            args, json = a, False

            def call(self, *x, **k):  # pragma: no cover — không được gọi daemon
                raise AssertionError("lệnh cũ không được gọi RPC")

        assert C.cmd_filehub(Ctx()) == 2
        assert "cổng cfharness" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        ap.parse_args(["filehub", "resume"])  # resume chỉ còn cho lô xoá đang giữ: bắt buộc --root


def test_removed_rpc_and_config_keys():
    from cfharness.auth import HUMAN_ONLY
    from cfharness.config import DEFAULTS
    from cfharness.filehub.rpc import MUTATING_FILEHUB

    gone = ("filehub.setup", "filehub.login", "filehub.login_callback", "filehub.switch_to_workspace", "filehub.add", "filehub.pause",
            "filehub.switch")
    assert not set(gone) & set(MUTATING_FILEHUB) and not set(gone) & set(HUMAN_ONLY)
    assert not {"sync_binary", "local_parent", "device_name", "relay_timeout_s", "max_delete_batch", "mode"} & set(DEFAULTS["filehub"])
    assert DEFAULTS["filehub"]["lane"]["max_delete_batch"] == 20  # chặn xoá hàng loạt của CỔNG giữ nguyên
    src = Path(__file__).resolve().parents[1] / "src" / "cfharness" / "filehub"
    assert not (src / "supervisor.py").exists()


# ============================================================================ web
@pytest.fixture
async def web(hub_home, tmp_path):
    from cfharness.runtime import FakeRuntime
    from test_web import ISOLATED, _start, _stop

    _old_hub(hub_home, tmp_path)
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    yield e
    await _stop(e)


async def test_web_card_shows_needs_migration_and_old_routes_gone(web):
    e = web
    await e.login()
    page = (await e.client.get("/settings/filehub")).text
    assert 'hx-get="/partials/filehub/lane"' in page and 'id="filehub"' in page
    for path in ("/partials/filehub", "/partials/filehub/lane"):
        card = (await e.client.get(path)).text
        assert MSG.replace("«", "&laquo;").replace("»", "&raquo;") in card or MSG in card
        assert "filehub_setup" not in card and "filehub_switch" not in card and "filehub_pause" not in card
    assert (await e.client.get("/filehub/login")).status_code == 404
    r = await e.client.get("/filehub/oauth/cb?code=x&state=" + "s" * 32, follow_redirects=False)
    assert r.status_code == 404
    for act in ("filehub_setup", "filehub_login", "filehub_pause", "filehub_switch", "filehub_switch_mode", "filehub_add"):
        r = await e.post(f"/do/{act}", {"confirm": "yes", "next": "/settings/filehub"})
        assert r.status_code in (303, 400, 404), (act, r.status_code)
