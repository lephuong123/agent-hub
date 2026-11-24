"""Cổng cfharness H2 — luồng sự kiện (cursor, nối lại sau khi đứt), steer run khi người sửa tệp trong phạm vi, tool hub-self `fs_*`
(chỉ đọc + ràng run), chế độ `tools` cho run chỉ đọc, run song song trên cùng gốc (khoá theo gốc chỉ khi `serialize_runs`), `filehub:` ở cả hai chế độ. Server lane GIẢ + ScriptedRuntime.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cfharness import auth
from cfharness.errors import HubError
from fake_kgs_lane import KEY, FakeLane
from lane_fake import close, configure_lane, events, make, steers, until
from m7_fake import rpc, wait_status


@pytest.fixture
def lane():
    f = FakeLane(ping_s=0.3)
    f.human_write("Báo cáo/yeu-cau.md", "viết báo cáo\n")
    f.human_write("Báo cáo/so-lieu.csv", "a,b\n1,2\n")
    f.human_write("Khác/x.txt", "khác\n")
    yield f
    f.close()


@pytest.fixture
async def env(hub_home, lane):
    d, s = await make(hub_home)
    await configure_lane(d, lane)
    yield d, s, lane
    await close(d)


async def _held_run(d, s, prompt="LÀM-VIỆC", cwd="filehub:chung/Báo cáo", read_only=False, agent="w1"):
    s.by_prompt[prompt] = {"text": "xong", "hold": True}
    r = d.hub.runs.submit(agent, prompt, cwd=cwd, run_opts={"read_only": True} if read_only else None)
    return await wait_status(d, r["id"])


async def test_tree_run_materializes_scope_steers_on_human_change_and_cleans_up(env):
    d, s, lane = env
    run = await _held_run(d, s)
    rid = run["id"]
    cwd = Path(run["cwd"])
    assert cwd.name == f"lane-{rid}" and cwd.parent.name == "workspaces"  # cây trong vùng làm việc của agent
    assert sorted(p.name for p in cwd.iterdir()) == ["so-lieu.csv", "yeu-cau.md"]  # CHỈ phạm vi `Báo cáo/`
    assert (cwd / "yeu-cau.md").read_text() == "viết báo cáo\n"
    assert run["run_opts"]["workspace_kind"] == "filehub_lane" and run["run_opts"]["filehub_lane"]["tree"] == rid
    spec = d.hub.runs.runtime().specs[rid]
    assert "<shared-folder-worktree>" in spec.prompt and spec.workspace_kind == "dir"  # cây của NGƯỜI — không bao giờ scratch
    mcp = json.loads(Path(spec.mcp_config_path).read_text())
    hs = mcp["mcpServers"]["hub-self"]
    assert hs["args"][-2:] == ["--fs", "rw"]
    # khoá không lộ ra run: env/argv/mcp.json/settings/prompt
    blob = json.dumps(mcp) + Path(spec.settings_path).read_text() + json.dumps(spec.env_extra) + spec.prompt + json.dumps(hs)
    assert KEY not in blob and "FILEHUB_LANE" not in blob
    # người sửa tệp trong phạm vi → kéo về + steer ≤ 1 s; tệp ngoài phạm vi → không steer
    t0 = asyncio.get_running_loop().time()
    lane.human_write("Báo cáo/yeu-cau.md", "người sửa\n")
    lane.human_write("Khác/x.txt", "ngoài phạm vi\n")
    await until(lambda: steers(d, rid))
    assert asyncio.get_running_loop().time() - t0 < 1.5
    assert steers(d, rid) == ["[cfharness] The human changed yeu-cau.md on Không gian số just now. Re-Read it before editing."]
    assert (cwd / "yeu-cau.md").read_text() == "người sửa\n"
    hc = events(d, "filehub.human_changed")
    assert hc[-1].payload["path"] == "yeu-cau.md" and hc[-1].payload["latency_ms"] is not None and hc[-1].run_id == rid
    # cùng tệp sửa lần 2 → không steer lặp (một lần/(run, tệp)) nhưng vẫn kéo về
    lane.human_write("Báo cáo/yeu-cau.md", "người sửa lần 2\n")
    await until(lambda: (cwd / "yeu-cau.md").read_text() == "người sửa lần 2\n")
    assert len(steers(d, rid)) == 1
    # người xoá / di chuyển trong phạm vi
    lane.human_move("Báo cáo/so-lieu.csv", "Báo cáo/so-lieu-2025.csv")
    await until(lambda: (cwd / "so-lieu-2025.csv").exists() and not (cwd / "so-lieu.csv").exists())
    lane.human_delete("Báo cáo/so-lieu-2025.csv")
    await until(lambda: not (cwd / "so-lieu-2025.csv").exists())
    assert any("moved so-lieu.csv to so-lieu-2025.csv" in x for x in steers(d, rid)) and any("deleted so-lieu-2025.csv" in x for x in steers(d, rid))
    # không khoá theo gốc thư mục chung: run ghi thứ hai cùng thư mục chạy SONG SONG (xong khi run đầu còn đang chạy)
    s.by_prompt["THỨ-HAI"] = {"text": "xong"}
    r2 = {"run_id": d.hub.runs.submit("w2", "THỨ-HAI", cwd="filehub:chung/Khác")["id"]}
    await wait_status(d, r2["run_id"], ("completed",))
    assert d.hub.store.get_run(rid)["status"] == "running"
    d.hub.runs.runtime().release(rid)
    await wait_status(d, rid, ("completed",))
    assert not cwd.exists() and rid not in d.hub.filehub.lane.by_run  # cây bị xoá khi run kết thúc
    assert not (d.hub.paths.home / "filehub" / "lane" / f"{rid}.json").exists()


async def test_stream_reconnects_and_resumes_from_cursor(env):
    d, s, lane = env
    run = await _held_run(d, s)
    rid, cwd = run["id"], Path(run["cwd"])
    await until(lambda: lane.stream_opens >= 1)
    opens = lane.stream_opens
    lane.drop_streams()  # mất kết nối SSE
    lane.human_write("Báo cáo/yeu-cau.md", "trong lúc mất kết nối\n")  # khung phát khi hub chưa nối lại
    await until(lambda: lane.stream_opens > opens)
    await until(lambda: (cwd / "yeu-cau.md").read_text() == "trong lúc mất kết nối\n")
    reqs = [r for r in lane.requests if r["path"].endswith("/events")]
    assert reqs[-1]["query"].get("cursor") and reqs[-1]["headers"].get("Last-Event-ID") == reqs[-1]["query"]["cursor"]
    assert d.hub.store.state_get("filehub.lane")["cursors"]["qltm"].startswith("c1.")
    assert d.hub.filehub.lane.stream_reconnects.get("qltm", 0) >= 1
    # khung `reset` → list lại phạm vi rồi nối tiếp
    lane.root().items["Báo cáo/yeu-cau.md"].set_content(b"sua thang (journal nen)\n", "human")
    lane.root().items["Báo cáo/yeu-cau.md"].rev += 1
    lane.reset_root()
    await until(lambda: (cwd / "yeu-cau.md").read_text() == "sua thang (journal nen)\n")
    d.hub.runs.runtime().release(rid)
    await wait_status(d, rid, ("completed",))


async def test_read_only_run_uses_tools_mode_and_fs_tools_bound_to_run(env):
    d, s, lane = env
    run = await _held_run(d, s, prompt="ĐỌC-THÔI", read_only=True)
    rid, cwd = run["id"], Path(run["cwd"])
    assert cwd.is_dir() and not any(cwd.iterdir())  # chế độ tools: cây rỗng
    spec = d.hub.runs.runtime().specs[rid]
    assert "EMPTY work tree" in spec.prompt
    hs = json.loads(Path(spec.mcp_config_path).read_text())["mcpServers"]["hub-self"]
    assert hs["args"][-2:] == ["--fs", "ro"]
    assert d.hub.store.get_run(rid).get("workspace_key") is None  # run chỉ đọc không lấy khoá

    async def fs(op, args=None, run_id=rid, caller=None):
        return await rpc(d, "fsself.call", {"run_id": run_id, "op": op, "args": args or {}}, as_caller=caller)

    ls = await fs("fs_list")
    assert {i["path"] for i in ls["items"]} == {"yeu-cau.md", "so-lieu.csv"}
    assert (await fs("fs_read", {"path": "yeu-cau.md"}))["text"] == "viết báo cáo\n"
    st = await fs("fs_stat", {"path": "yeu-cau.md"})
    assert st["type"] == "file" and st["path"] == "yeu-cau.md"
    assert (await fs("fs_status"))["mode"] == "tools"
    assert len((await fs("fs_versions", {"path": "yeu-cau.md"}))["versions"]) == 1
    # đọc tệp rồi người sửa → vẫn steer (dù chế độ tools)
    lane.human_write("Báo cáo/yeu-cau.md", "mới\n")
    await until(lambda: steers(d, rid))
    # ra ngoài phạm vi / đường dẫn bẩn → từ chối phía hub (không gửi)
    n_req = len(lane.requests)
    for bad in ("../Khác/x.txt", ".tangthu/db", "/etc/passwd", "a\x00b"):
        with pytest.raises(HubError):
            await fs("fs_read", {"path": bad})
    assert len(lane.requests) == n_req
    # run chỉ đọc: tool ghi bị từ chối
    for op in ("fs_write", "fs_mkdir", "fs_delete", "fs_move", "fs_restore"):
        with pytest.raises(HubError, match="chỉ đọc"):
            await fs(op, {"path": "x.md", "content": "x", "from": "a", "to": "b", "version_id": "v"})
    # fs_pull vào cây (chỉ đọc vẫn được kéo về để đọc bằng Read)
    r = await fs("fs_pull", {"path": "so-lieu.csv"})
    assert (cwd / "so-lieu.csv").read_text() == "a,b\n1,2\n" and r["local"] == str(cwd / "so-lieu.csv")
    # tiến trình của run KHÁC gọi fsself.call với run_id này → từ chối
    other = auth.Caller(via="socket", pid=12345, token_ok=False, run_id="r_khac", agent_id="w2", evidence=["env_run_id"])
    with pytest.raises(HubError, match="không khớp"):
        await fs("fs_list", caller=other)
    # run không có workspace lane → chỉ đọc server (`--fs browse`, test_lane_followups); tắt read_all_runs → không có fs_*
    d.hub.config.set("filehub.lane.read_all_runs", False)
    s.by_prompt["THƯỜNG"] = {"text": "x", "hold": True}
    r2 = {"run_id": d.hub.runs.submit("w2", "THƯỜNG")["id"]}
    await wait_status(d, r2["run_id"])
    with pytest.raises(HubError, match="lane"):
        await fs("fs_list", {"path": "chung"}, run_id=r2["run_id"])
    d.hub.runs.runtime().release()
    await wait_status(d, rid, ("completed",))


async def test_root_not_granted_fails_run_with_filehub_unavailable(env):
    d, s, lane = env
    lane.revoke_root("r_chung")
    d.hub.filehub.lane._hello.clear()
    r = {"run_id": d.hub.runs.submit("w1", "X", cwd="filehub:chung")["id"]}
    run = await wait_status(d, r["run_id"], ("failed",))
    assert "filehub_unavailable" in (run.get("error") or "")


async def test_filehub_refs_lane_only_legacy_needs_migration(env, tmp_path):
    """`filehub:<tên>` của thư mục qua cổng → cây theo run; thư mục còn cấu hình đồng bộ cũ (đã bỏ — H5) → mọi đường (run, task, ref) từ
    chối với lời giải thích, không bao giờ chạy trong bản sao cũ; task giải lại lúc chạy."""
    d, s, lane = env
    local = tmp_path / "KhongGianSo" / "cu"
    (local / "A").mkdir(parents=True)
    d.hub.config.set("filehub.folders", [*d.hub.config.get("filehub.folders"),
                                         {"name": "cu", "root_id": "r_cu", "ws_id": "w", "local_path": str(local.resolve())}])
    assert d.hub.filehub.lane_target("filehub:cu/A") is None
    with pytest.raises(HubError, match="đồng bộ cũ") as ei:
        d.hub.filehub.resolve_ref("filehub:cu/A")
    assert ei.value.data["needs_migration"] is True
    with pytest.raises(HubError, match="đồng bộ cũ"):
        d.hub.runs.submit("w1", "X", cwd="filehub:cu")
    with pytest.raises(HubError, match="đồng bộ cũ"):
        await rpc(d, "task.create", {"assignee": "w1", "title": "T", "body": "T", "workspace": "filehub:cu/A"})
    assert d.hub.filehub.lane_target("filehub:chung/Báo cáo")["sub"] == "Báo cáo"
    with pytest.raises(HubError, match="qua cổng"):
        d.hub.filehub.resolve_ref("filehub:chung")
    s.by_prompt["TASK-LANE"] = {"text": "xong"}
    t = await rpc(d, "task.create", {"assignee": "w1", "title": "TASK-LANE", "body": "TASK-LANE", "workspace": "filehub:chung/Báo cáo"})
    assert t["workspace"]["lane"] is True and t["workspace"]["path"] is None
    run = await until(lambda: next((r for r in d.hub.store.list_runs(agent_id="w1") if r.get("status") == "completed"), None))
    assert run["run_opts"]["filehub_lane"]["sub"] == "Báo cáo"
    assert {f["name"] for f in d.hub.filehub.lane_folders()} == {"chung"}
    assert {f["name"] for f in d.hub.filehub.legacy_folders()} == {"cu"}


async def test_parallel_runs_same_root_see_each_others_writes(env):
    """Hai run ghi cùng thư mục chung chạy song song: khung `self: true` của run A (cùng khoá cổng) KHÔNG bị cây của run B coi là tiếng
    vọng — B kéo bản mới + steer nói rõ là run khác; A không kéo lại bản của chính mình."""
    d, s, lane = env
    a = await _held_run(d, s, prompt="A")
    b = await _held_run(d, s, prompt="B", agent="w2")
    assert a["status"] == b["status"] == "running"
    ca, cb = Path(a["cwd"]), Path(b["cwd"])
    (ca / "yeu-cau.md").write_text("run A sửa\n")
    await until(lambda: lane.file("Báo cáo/yeu-cau.md") == "run A sửa\n".encode())
    await until(lambda: (cb / "yeu-cau.md").read_text() == "run A sửa\n")
    assert steers(d, b["id"]) == ["[cfharness] Another cfharness agent run changed yeu-cau.md on Không gian số just now. Re-Read it before editing."]
    assert steers(d, a["id"]) == []  # tiếng vọng của chính A
    hc = [e for e in events(d, "filehub.human_changed") if e.run_id == b["id"]]
    assert hc and hc[-1].payload.get("by") == "cfharness"
    # B sửa tệp khác → A kéo về; người sửa → câu "The human" như cũ
    (cb / "so-lieu.csv").write_text("a,b\n3,4\n")
    await until(lambda: (ca / "so-lieu.csv").read_text() == "a,b\n3,4\n")
    lane.human_write("Báo cáo/yeu-cau.md", "người sửa\n")
    await until(lambda: any(x.startswith("[cfharness] The human changed yeu-cau.md") for x in steers(d, a["id"])))
    # thư mục chung đang có run làm việc → không gỡ được
    with pytest.raises(Exception, match="đang có run"):
        d.hub.filehub.remove("chung")


async def test_serialize_runs_restores_root_lock(env):
    d, s, lane = env
    d.hub.config.set("filehub.lane.serialize_runs", True)
    run = await _held_run(d, s)
    s.by_prompt["THỨ-HAI"] = {"text": "xong"}
    r2 = d.hub.runs.submit("w2", "THỨ-HAI", cwd="filehub:chung/Khác")["id"]
    await asyncio.sleep(0.3)
    assert d.hub.store.get_run(r2)["status"] == "queued"
    d.hub.runs.runtime().release(run["id"])
    await wait_status(d, run["id"], ("completed",))
    await wait_status(d, r2, ("completed",))
