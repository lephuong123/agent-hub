"""Workspace `filehub:<tên>[/<con>]` (thư mục chung Không gian số): giải đường dẫn, khoá theo GỐC thư mục chung, run chỉ đọc song
song, checkpoint `_walk` + diff, `run revert` tắt, giam trong W, không trong HUB_HOME, `extra_write_roots` cho scratch, quyền ĐỌC thư mục
chung cho mọi run (sandbox + prompt), báo sau run (xoá/đổi/bí mật). ScriptedRuntime — không gọi claude, không chạy binary sync.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from cfharness.daemon import Daemon
from cfharness.errors import HubError
from cfharness.redact import register_secret_values
from fh_fake import configure, events, until
from m3_fake import ScriptedRuntime
from m7_fake import rpc, wait_status


class Script:
    def __init__(self) -> None:
        self.by_prompt: dict[str, dict] = {}
        self.specs: list = []

    def __call__(self, spec):
        self.specs.append(spec)
        for k, v in self.by_prompt.items():
            if k in spec.prompt:
                return dict(v)
        return {"text": "ok", "cost": 0.001}


@pytest.fixture
async def env(hub_home, tmp_path):
    s = Script()
    d = Daemon(hub_home, runtime=ScriptedRuntime(s))
    d.hub.config.set("tasks.tick_s", 0.05)
    local = configure(d.hub, tmp_path)  # cấu hình thư mục chung (không chạy binary — test này không cần đồng bộ)
    d.hub.config.set("filehub.sync_binary", None)
    (local / "Báo cáo").mkdir()
    (local / "Báo cáo" / "yeu-cau.md").write_text("viết báo cáo\n")
    (local / "Khác").mkdir()
    d.hub.start()
    for a in ("w1", "w2"):
        d.hub.agents.create({"id": a, "name": a, "limits": {"max_concurrent_runs": 5}})
    yield d, s, local, tmp_path
    d.hub.runs.runtime().release()  # type: ignore[attr-defined]
    await asyncio.sleep(0.05)
    d.hub.runs.shutting_down = True
    await d.hub.dispatcher.stop()
    await d.hub.filehub.stop()
    d.hub.store.close()


def test_resolve_ref(env):
    d, _, local, _ = env
    fh = d.hub.filehub
    assert fh.resolve_ref("filehub:chung") == (str(local), fh.folder("chung"), "")
    assert fh.resolve_ref("filehub:chung/Báo cáo/")[0] == str(local / "Báo cáo")
    for bad, msg in (("filehub:chung/../x", "không hợp lệ"), ("filehub:chung/.tangthu", "không hợp lệ"), ("filehub:khong-co", "không có thư mục chung"),
                     ("filehub:chung/chua-co", "chưa có trên máy"), ("chung", "filehub:")):
        with pytest.raises(HubError, match=msg):
            fh.resolve_ref(bad)
    (local / "ra-ngoai").symlink_to(local.parent)
    with pytest.raises(HubError, match="thoát ra ngoài"):
        fh.resolve_ref("filehub:chung/ra-ngoai")


async def test_task_on_subfolder_checkpoint_diff_no_revert(env):
    d, s, local, _ = env
    s.by_prompt["VIẾT-BÁO-CÁO"] = {"text": "xong", "files": {"bao-cao.md": "# Báo cáo\n", "yeu-cau.md": None}}
    t = await rpc(d, "task.create", {"assignee": "w1", "title": "VIẾT-BÁO-CÁO", "body": "VIẾT-BÁO-CÁO", "workspace": "filehub:chung/Báo cáo"})
    assert t["workspace"]["kind"] == "filehub" and t["workspace"]["path"] == str(local / "Báo cáo") and t["workspace"]["folder"] == "chung"
    run = await until(lambda: next((r for r in d.hub.store.list_runs(agent_id="w1") if r.get("diff")), None))
    assert run["cwd"] == str(local / "Báo cáo")
    assert run["run_opts"]["filehub"] == "chung" and run["run_opts"]["workspace_kind"] == "filehub"
    assert run["checkpoint"]["kind"] == "dir" and run["workspace_key"] == str(local)  # khoá = GỐC thư mục chung
    assert run["diff"]["filehub"] == "chung" and {f["path"]: f["status"] for f in run["diff"]["files"]} == {"bao-cao.md": "added", "yeu-cau.md": "deleted"}
    with pytest.raises(HubError, match="Không gian số"):
        await rpc(d, "run.revert", {"id": run["id"]})
    # báo sau run: xoá (không hỏi duyệt — chỉ báo) + đổi
    await until(lambda: events(d.hub, "filehub.deleted") and events(d.hub, "filehub.changed"))
    de = events(d.hub, "filehub.deleted")[0].payload
    assert de["count"] == 1 and de["paths"] == ["Báo cáo/yeu-cau.md"] and de["run_id"] == run["id"] and de["folder"] == "chung"
    assert events(d.hub, "filehub.changed")[0].payload["paths"] == ["Báo cáo/bao-cao.md"]
    assert not events(d.hub, "filehub.secret_warning")


async def test_secret_scan_after_run(env):
    d, s, local, _ = env
    register_secret_values("SECRETVALUE-filehub-1234567")
    s.by_prompt["GHI-BI-MAT"] = {"text": "xong", "files": {"ghi-chu.txt": "token=SECRETVALUE-filehub-1234567\n", "sach.txt": "ok"}}
    await rpc(d, "run.start", {"agent": "w1", "prompt": "GHI-BI-MAT", "cwd": "filehub:chung"})
    await until(lambda: events(d.hub, "filehub.secret_warning"))
    p = events(d.hub, "filehub.secret_warning")[0].payload
    assert p["paths"] == ["ghi-chu.txt"] and "SECRETVALUE" not in json.dumps(p)


async def test_lock_by_root_serializes_writers_read_only_parallel(env):
    d, s, local, _ = env
    s.by_prompt["GIU"] = {"text": "x", "hold": True}
    r1 = await rpc(d, "run.start", {"agent": "w1", "prompt": "GIU 1", "cwd": "filehub:chung/Báo cáo"})
    await wait_status(d, r1["id"])
    assert d.hub.runs.locks.holder(str(local))["run_id"] == r1["id"]
    r2 = await rpc(d, "run.start", {"agent": "w2", "prompt": "GIU 2", "cwd": "filehub:chung/Khác"})  # thư mục con KHÁC, cùng gốc
    await asyncio.sleep(0.3)
    assert d.hub.store.get_run(r2["id"])["status"] == "queued"
    ro = d.hub.runs.submit("w2", "GIU ro", cwd="filehub:chung", run_opts={"read_only": True})
    await wait_status(d, ro["id"])  # chỉ đọc: không lấy khoá → chạy song song
    d.hub.runs.runtime().release(r1["id"])  # type: ignore[attr-defined]
    await wait_status(d, r1["id"], ("completed",))
    await wait_status(d, r2["id"])
    assert d.hub.runs.locks.holder(str(local))["run_id"] == r2["id"]


async def test_git_repo_shared_folder_still_walks(env):
    d, s, local, _ = env
    subprocess.run(["git", "init", "-q", str(local)], check=True)
    s.by_prompt["SUA"] = {"text": "x", "files": {"moi.txt": "1"}}
    r = await rpc(d, "run.start", {"agent": "w1", "prompt": "SUA", "cwd": "filehub:chung"})
    run = await until(lambda: (d.hub.store.get_run(r["id"]) or {}).get("diff") and d.hub.store.get_run(r["id"]))
    assert run["checkpoint"]["kind"] == "dir" and run["diff"]["kind"] == "dir" and not run["diff"]["revertible"]


async def test_confine_outside_w_rejected(env):
    d, _, local, tmp = env
    w = tmp / "W"
    w.mkdir()
    d.hub.config.set("workspace.confine", {"root": str(w)})
    with pytest.raises(HubError, match="giam"):
        await rpc(d, "run.start", {"agent": "w1", "prompt": "x", "cwd": "filehub:chung"})
    with pytest.raises(HubError, match="giam"):
        await rpc(d, "task.create", {"assignee": "w1", "title": "x", "workspace": "filehub:chung"})


async def test_extra_write_roots_for_scratch_and_read_for_every_run(env):
    d, s, local, tmp = env
    s.by_prompt["SCRATCH-GHI"] = {"text": "x", "hold": True}
    t = await rpc(d, "task.create", {"assignee": "w1", "title": "SCRATCH-GHI", "body": "SCRATCH-GHI", "workspace": "scratch",
                                     "extra_write_roots": [str(local / "Báo cáo")]})
    assert t["workspace"]["extra_write_roots"] == [str(local / "Báo cáo")]
    spec = await until(lambda: next((x for x in s.specs if "SCRATCH-GHI" in x.prompt), None))
    st = json.loads(Path(spec.settings_path).read_text())
    assert str(local / "Báo cáo") in st["sandbox"]["filesystem"]["allowWrite"]
    hh = str(d.hub.paths.home)
    assert f"{hh}/filehub" in st["sandbox"]["filesystem"]["denyRead"] and f"{hh}/filehub" in st["sandbox"]["filesystem"]["denyWrite"]
    assert any(r.startswith("Read(") and "/filehub/**" in r for r in st["permissions"]["deny"])
    # mọi run biết thư mục chung (DỮ LIỆU của người) qua system prompt
    assert "# Shared folder (Không gian số)" in spec.system_prompt and f"chung: {local}" in spec.system_prompt
    d.hub.runs.runtime().release()  # type: ignore[attr-defined]


async def test_confine_allows_reading_shared_folder(env):
    d, s, local, tmp = env
    # bản sao cục bộ nằm trong W → classifier/sandbox cho đọc
    d.hub.config.set("workspace.confine", {"root": str(local.parent)})
    ctx = d.hub.guard.context_for({"id": "r", "cwd": str(tmp / "ws"), "agent_id": "w1", "run_opts": {}}, cwd=str(tmp / "ws"))
    assert str(local) in ctx.confine_read and ctx.filehub_roots == (str(local),)


def test_workspace_kind_listed():
    from cfharness.tasks import WORKSPACE_KINDS

    assert "filehub" in WORKSPACE_KINDS


# ------------------------------------------------------------------ sửa sau nghiệm thu thật (2026-11-15)
async def test_agent_workspace_default_filehub_ref(env):
    """#1: `agent create --workspace "filehub:<tên>/<con>"` — trước đây coi là đường dẫn tương đối (lỗi vùng bảo vệ)."""
    d, s, local, _ = env
    await rpc(d, "agent.create", {"config": {"id": "w3", "name": "w3", "workspace_default": "filehub:chung/Báo cáo"}})
    assert d.hub.store.get_agent("w3")["workspace_default"] == "filehub:chung/Báo cáo"
    r = await rpc(d, "run.start", {"agent": "w3", "prompt": "x"})
    run = await until(lambda: (d.hub.store.get_run(r["id"]) or {}).get("diff") and d.hub.store.get_run(r["id"]))
    assert run["cwd"] == str(local / "Báo cáo") and run["run_opts"]["filehub"] == "chung" and run["workspace_key"] == str(local)
    await rpc(d, "agent.update", {"id": "w3", "changes": {"workspace_default": "filehub:chung/Khác"}})
    assert d.hub.runs.locks.key_for("filehub:chung/Khác", "dir") == str(local)
    with pytest.raises(HubError, match="không hợp lệ"):
        await rpc(d, "agent.update", {"id": "w3", "changes": {"workspace_default": "filehub:chung/../x"}})


def test_ignore_has_claude_internal_files(env):
    """#2: Claude Code ghi `.claude/.cc-writes` trong cwd của run — không được đẩy lên Không gian số."""
    d, *_ = env
    lines = d.hub.filehub.ignore_lines()
    assert ".claude/" in lines and ".cc-writes" in lines


def test_state_event_not_emitted_for_syncing_idle_flapping(env):
    """#3: syncing↔idle mỗi vòng không phát/ghi `filehub.state`; vào/ra trạng thái có ý nghĩa thì có."""
    d, *_ = env
    fh = d.hub.filehub
    fh.prepare_files()

    def st(state):
        fh.status_path.write_text(json.dumps({"version": "0.3.0", "state": state, "roots": [], "recent": []}))
        fh.tick()

    st("idle")
    n0 = len(events(d.hub, "filehub.state"))
    for _ in range(5):
        st("syncing")
        st("idle")
    assert len(events(d.hub, "filehub.state")) == n0
    st("offline")
    st("syncing")
    assert [e.payload["state"] for e in events(d.hub, "filehub.state")][n0:] == ["offline", "idle"]


def test_device_name_keeps_ip_hostname(monkeypatch, env):
    """#4: hostname "192.168.2.51" → không cắt thành "192"; tên thân thiện có giới hạn độ dài."""
    from cfharness.filehub import service as S

    assert S.friendly_host("192.168.2.51") == "192.168.2.51" and S.friendly_host("an-mac.local") == "an-mac"
    assert len(S.friendly_host("x" * 200)) == S.DEVICE_NAME_MAX
    monkeypatch.setattr(S, "_MACHINE", None)
    monkeypatch.setattr(S.sys, "platform", "linux")
    monkeypatch.setattr(S.socket, "gethostname", lambda: "192.168.2.51")
    d, *_ = env
    assert d.hub.filehub.device_name() == "cfharness · 192.168.2.51"
    monkeypatch.setattr(S, "_MACHINE", None)


# ------------------------------------------------------------------ gốc workspace (plan-filehub-workspace-root.md, PR H7)
async def test_workspace_root_top_level_folders_inside_confine(env):
    """Chế độ workspace: `filehub:<ws>/<thư mục cấp 1>/…` = đúng đường dẫn người thấy trên web; bản sao trong W (giam) → task/run chạy,
    khoá = gốc workspace; tên sống trên web (đổi tên) là bí danh; prompt nói rõ gốc = workspace + tệp mới phải trong thư mục cấp 1."""
    d, s, _old, tmp = env
    w = tmp / "W"
    ws_local = (w / "KhongGianSo" / "admin harness")
    (ws_local / "v1_2026-11-15" / "00_Tong_quan").mkdir(parents=True)
    (ws_local / "v1_2026-11-15" / "00_Tong_quan" / "README.md").write_text("đọc tôi\n")
    d.hub.config.set("filehub.folders", [{"name": "admin harness", "root_id": "ws_9", "ws_id": "ws_9", "local_path": str(ws_local.resolve()),
                                          "kind": "workspace", "account": "chu.domain"}])
    d.hub.config.set("workspace.confine", {"root": str(w)})
    ws_local = ws_local.resolve()
    fh = d.hub.filehub
    fh.prepare_files()
    fh.status_path.write_text(json.dumps({"state": "idle", "roots": [{"id": "ws_9", "kind": "workspace", "server_name": "Admin Harness (mới)"}]}))
    sub = "filehub:admin harness/v1_2026-11-15/00_Tong_quan"
    assert fh.resolve_ref(sub)[0] == str(ws_local / "v1_2026-11-15" / "00_Tong_quan")
    assert fh.resolve_ref("filehub:Admin Harness (mới)/v1_2026-11-15")[0] == str(ws_local / "v1_2026-11-15")  # bí danh tên mới
    with pytest.raises(HubError, match="không có thư mục chung"):
        fh.resolve_ref("filehub:Admin Harness/v1_2026-11-15")
    # server_name null (chưa đồng bộ lần nào) / chậm cập nhật (đổi tên ws không có sự kiện feed — S5 làm mới theo token ~15 phút):
    # tên cfharness luôn khớp, không lỗi
    fh.status_path.write_text(json.dumps({"state": "idle", "roots": [{"id": "ws_9", "kind": "workspace", "server_name": None}]}))
    assert fh.resolve_ref(sub)[0] == str(ws_local / "v1_2026-11-15" / "00_Tong_quan") and fh.status()["folders"][0]["server_name"] == ""
    fh.status_path.write_text(json.dumps({"state": "idle", "roots": [{"id": "ws_9", "kind": "workspace", "server_name": "Admin Harness (mới)"}]}))
    s.by_prompt["DOC-TONG-QUAN"] = {"text": "xong", "files": {"tom-tat.md": "# tóm tắt\n"}}
    t = await rpc(d, "task.create", {"assignee": "w1", "title": "DOC-TONG-QUAN", "body": "DOC-TONG-QUAN", "workspace": sub})
    assert t["workspace"]["kind"] == "filehub" and t["workspace"]["folder"] == "admin harness"
    run = await until(lambda: next((r for r in d.hub.store.list_runs(agent_id="w1") if r.get("diff")), None))
    assert run["cwd"] == str(ws_local / "v1_2026-11-15" / "00_Tong_quan") and run["workspace_key"] == str(ws_local)
    spec = next(x for x in s.specs if "DOC-TONG-QUAN" in x.prompt)
    assert "the WHOLE Không gian số workspace" in spec.system_prompt and "INSIDE a top-level folder" in spec.system_prompt
    assert '"Admin Harness (mới)"' in spec.system_prompt
    rows = await rpc(d, "filehub.list")
    assert rows[0]["kind"] == "workspace" and rows[0]["server_name"] == "Admin Harness (mới)"
    st = await rpc(d, "filehub.status")
    assert st["folders"][0]["display"] == "Toàn bộ workspace «Admin Harness (mới)»" and st["folders"][0]["account"] == "chu.domain"


def test_revoked_state_names_account(env):
    d, *_ = env
    fh = d.hub.filehub
    fh.prepare_files()
    d.hub.config.set("filehub.folders", [{**fh.folders()[0], "account": "chu.domain"}])
    fh.status_path.write_text(json.dumps({"state": "idle", "roots": [{"id": "r_chung", "state": "disconnected", "last_error": "root_revoked",
                                                                      "server_name": None}]}))
    st = fh.status()
    assert st["state"] == "revoked" and st["folders"][0]["revoked"] and st["folders"][0]["account"] == "chu.domain"
    fh.tick()
    assert events(d.hub, "filehub.state")[-1].payload["state"] == "revoked"
    assert not st["folders"][0]["revoked_temporary"]  # root đã ngắt (binary cũ) → phải đăng nhập lại


def test_revoked_temporary_workspace_root(env):
    """Binary S5 thật khi tài khoản bị gỡ admin: root KHÔNG ngắt — `state=error`, `last_error=root_revoked` (e2e 2026-11-16). Cấp lại quyền
    là tự chạy tiếp (T6 xét quyền sống) → thẻ/CLI không bảo đăng nhập lại."""
    d, *_ = env
    fh = d.hub.filehub
    fh.prepare_files()
    d.hub.config.set("filehub.folders", [{**fh.folders()[0], "account": "chu.domain"}])
    fh.status_path.write_text(json.dumps({"state": "idle", "roots": [{"id": "r_chung", "state": "error", "last_error": "root_revoked"}]}))
    st = fh.status()
    f = st["folders"][0]
    assert st["state"] == "revoked" and f["revoked"] and f["revoked_temporary"] and f["account"] == "chu.domain"
    fh.status_path.write_text(json.dumps({"state": "idle", "roots": [{"id": "r_chung", "state": "idle", "last_error": None}]}))
    st = fh.status()
    assert st["state"] == "idle" and not st["folders"][0]["revoked"] and not st["folders"][0]["revoked_temporary"]


def test_workspace_deleted_disconnects_with_reason(env):
    """Binary S5 khi workspace bị xoá trên server (WebSocket `root_revoked workspace_deleted`): root NGẮT, `last_error=workspace_deleted`,
    bản trên máy đổi tên `<tên> (đã ngắt đồng bộ)` → status nêu rõ (không phải "mất quyền tạm thời") + chỗ tệp nằm bây giờ (e2e 2026-11-16)."""
    d, *_ = env
    fh = d.hub.filehub
    fh.prepare_files()
    f0 = fh.folders()[0]
    moved = f0["local_path"] + " (đã ngắt đồng bộ)"
    fh.status_path.write_text(json.dumps({"state": "idle", "roots": [{"id": f0["root_id"], "state": "disconnected", "last_error": "workspace_deleted",
                                                                      "local_path": moved}]}))
    st = fh.status()
    f = st["folders"][0]
    assert st["state"] == "revoked" and f["revoked"] and f["workspace_deleted"] and not f["revoked_temporary"]
    assert f["disconnected_path"] == moved


# ------------------------------------------------------------------ báo sau run cho extra_write_roots (việc dở filehub.md)
async def test_extra_write_roots_in_shared_folder_reported_after_run(env):
    """Run workspace scratch + `extra_write_roots` trong thư mục chung → cũng có filehub.changed|deleted|secret_warning (payload
    `extra_root`); gốc ngoài thư mục chung → không báo."""
    d, s, local, tmp = env
    register_secret_values("SECRETVALUE-extra-98765432")
    other = tmp / "ngoai"
    other.mkdir()
    (other / "a.txt").write_text("1")
    bc = local / "Báo cáo"
    s.by_prompt["EXTRA-GHI"] = {"text": "xong", "files": {str(bc / "moi.md"): "# mới\n", str(bc / "yeu-cau.md"): None,
                                                         str(bc / "bi-mat.txt"): "k=SECRETVALUE-extra-98765432\n", str(other / "a.txt"): None}}
    await rpc(d, "task.create", {"assignee": "w1", "title": "EXTRA-GHI", "body": "EXTRA-GHI", "workspace": "scratch",
                                 "extra_write_roots": [str(bc), str(other)]})
    await until(lambda: events(d.hub, "filehub.deleted") and events(d.hub, "filehub.changed") and events(d.hub, "filehub.secret_warning"))
    de = events(d.hub, "filehub.deleted")[0].payload
    assert de["paths"] == ["Báo cáo/yeu-cau.md"] and de["extra_root"] == "Báo cáo" and de["folder"] == "chung"
    assert sorted(events(d.hub, "filehub.changed")[0].payload["paths"]) == ["Báo cáo/bi-mat.txt", "Báo cáo/moi.md"]
    sw = events(d.hub, "filehub.secret_warning")[0].payload
    assert sw["paths"] == ["Báo cáo/bi-mat.txt"] and "SECRETVALUE" not in json.dumps(sw)
    await asyncio.sleep(0.2)
    assert len(events(d.hub, "filehub.deleted")) == 1  # a.txt ngoài thư mục chung: không báo


async def test_extra_write_root_covered_by_main_workspace_not_duplicated(env):
    d, s, local, _ = env
    s.by_prompt["CHINH-GHI"] = {"text": "x", "files": {"Báo cáo/z.md": "z"}}
    run = d.hub.runs.submit("w1", "CHINH-GHI", cwd="filehub:chung", run_opts={"extra_write_roots": [str(local / "Báo cáo")]})
    await until(lambda: events(d.hub, "filehub.changed"))
    await asyncio.sleep(0.2)
    ch = events(d.hub, "filehub.changed")
    assert len(ch) == 1 and ch[0].payload["paths"] == ["Báo cáo/z.md"] and "extra_root" not in ch[0].payload
    assert ch[0].payload["run_id"] == run["id"]


# ------------------------------------------------------------------ đổi tên cfharness (filehub.rename)
def _synced(d, local):
    fh = d.hub.filehub
    fh.prepare_files()
    fh.status_path.write_text(json.dumps({"state": "idle", "roots": [{"id": "r_chung", "local_path": str(local), "state": "idle"}]}))


async def test_rename_keeps_local_path_and_old_name_as_alias(env):
    from cfharness.errors import Conflict

    d, s, local, _ = env
    fh = d.hub.filehub
    with pytest.raises(HubError, match="chưa đồng bộ lần đầu"):  # binary còn nhận --root-name của lần thêm
        await rpc(d, "filehub.rename", {"name": "chung", "new_name": "Dự án"})
    _synced(d, local)
    for bad, msg in (("", "thiếu"), ("../x", "không hợp lệ"), ("chung", "trùng tên hiện tại")):
        with pytest.raises(HubError, match=msg):
            await rpc(d, "filehub.rename", {"name": "chung", "new_name": bad})
    r = await rpc(d, "filehub.rename", {"name": "chung", "new_name": "Dự án"})
    assert r["name"] == "Dự án" and r["previous"] == "chung" and r["local_path"] == str(local) and r["workspace"] == "filehub:Dự án"
    f = fh.folders()[0]
    assert f["name"] == "Dự án" and f["aliases"] == ["chung"] and f["local_path"] == str(local) and f["root_id"] == "r_chung"
    assert fh.resolve_ref("filehub:Dự án/Báo cáo")[0] == str(local / "Báo cáo")
    assert fh.resolve_ref("filehub:chung/Báo cáo")[0] == str(local / "Báo cáo")  # tên cũ vẫn khớp (task/agent đã ghi)
    assert fh.list()[0]["aliases"] == ["chung"] and fh.status()["folders"][0]["aliases"] == ["chung"]
    assert fh._run_folder({"run_opts": {"filehub": "chung"}})["name"] == "Dự án"  # run/task đã ghi tên cũ vẫn có báo sau run
    s.by_prompt["SAU-DOI-TEN"] = {"text": "x", "files": {"sau.md": "1"}}
    run = d.hub.runs.submit("w1", "SAU-DOI-TEN", cwd="filehub:chung", run_opts={})
    assert d.hub.store.get_run(run["id"])["run_opts"]["filehub"] == "Dự án"
    await until(lambda: events(d.hub, "filehub.changed"))
    # đổi lại tên cũ: bỏ khỏi bí danh; add trùng bí danh → Conflict; tên không có → NotFound
    await rpc(d, "filehub.rename", {"name": "Dự án", "new_name": "chung"})
    assert fh.folders()[0]["aliases"] == ["Dự án"]
    fh.login_state = {"roots": [{"id": "r_2", "name": "x"}]}
    with pytest.raises(Conflict, match="tên cũ"):
        fh.add("r_2", "Dự án", local_path=str(local.parent / "hai"))
    with pytest.raises(HubError, match="không có thư mục chung"):
        await rpc(d, "filehub.rename", {"name": "khong-co", "new_name": "y"})


async def test_rename_rejected_while_run_holds_lock(env):
    d, s, local, _ = env
    _synced(d, local)
    s.by_prompt["GIU-DOI-TEN"] = {"text": "x", "hold": True}
    r1 = await rpc(d, "run.start", {"agent": "w1", "prompt": "GIU-DOI-TEN", "cwd": "filehub:chung"})
    await wait_status(d, r1["id"])
    with pytest.raises(HubError, match="đang có run"):
        await rpc(d, "filehub.rename", {"name": "chung", "new_name": "moi"})
    assert d.hub.filehub.folders()[0]["name"] == "chung"
    d.hub.runs.runtime().release(r1["id"])  # type: ignore[attr-defined]
    await wait_status(d, r1["id"], ("completed",))


async def test_local_parent_outside_confine_falls_back_inside_w(env):
    """Hub giam trong W mà `filehub.local_parent` (mặc định `~/KhongGianSo`) ở ngoài W → bản sao mới đặt ở `<W>/KhongGianSo/<tên>`
    thay vì báo "nằm ngoài workspace được giam" (lỗi thật trên hub user: nối thư mục qua web không sửa được đường dẫn)."""
    d, _, _local, tmp = env
    fh = d.hub.filehub
    out = tmp / "Ngoai"
    d.hub.config.set("filehub.local_parent", str(out))
    assert fh.local_parent() == str(out)  # chưa giam → giữ cấu hình
    w = tmp / "W"
    w.mkdir()
    d.hub.config.set("workspace.confine", {"root": str(w)})
    assert fh.local_parent() == str(w.resolve() / "KhongGianSo")
    assert fh.status()["local_parent"] == str(w.resolve() / "KhongGianSo")
    res = fh.add("r_moi", "LLinh - CFharness")
    assert res["local_path"] == str(w.resolve() / "KhongGianSo" / "LLinh - CFharness")
    assert (w / "KhongGianSo" / "LLinh - CFharness").is_dir() and not out.exists()
    # đã đặt trong W → giữ đúng chỗ người chọn
    inside = w / "Chung"
    d.hub.config.set("filehub.local_parent", str(inside))
    assert fh.local_parent() == str(inside)


async def test_setup_rejects_explicit_local_parent_outside_confine(env):
    d, _, _local, tmp = env
    w = tmp / "W"
    w.mkdir()
    d.hub.config.set("workspace.confine", {"root": str(w)})
    before = d.hub.config.get("filehub.server")
    with pytest.raises(HubError, match="ngoài workspace được giam"):
        await rpc(d, "filehub.setup", {"server": "https://kgs2.example.test", "binary": str(tmp / "bin" / "tang-thu-sync-headless"),
                                       "local_parent": str(tmp / "Ngoai"), "no_browser": True})
    assert d.hub.config.get("filehub.server") == before  # không ghi gì khi bị từ chối
