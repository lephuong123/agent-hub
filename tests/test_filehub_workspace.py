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
