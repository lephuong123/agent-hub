"""Cổng cfharness — sửa theo nghiệm thu PR #148 (ảnh 21–25). Mỗi ca đỏ trước khi sửa.

1. Lô xoá hàng loạt đang giữ KHÔNG đổi nghĩa khi run kết thúc: cây mồ côi còn lô xoá vẫn hiện "N mục xoá đang chờ duyệt" + Cho xoá / Không xoá
   (cùng RPC `filehub.resume {root_id, discard?}`); "Đẩy lại" KHÔNG ngầm duyệt lô xoá; thẻ + trang xác nhận nêu TỪNG mục (xoá / ghi / di chuyển)
   và đường dẫn (có trần); "Cần bạn" có lô xoá + link từ task bị chặn.
2. Ô địa chỉ "Nâng cao" để trống (placeholder) → hub tự thử 8223 rồi 8233.
3. Sức khoẻ cổng: kiểm lại mỗi 60 s khi ổn + thẻ ghi "kiểm tra lúc …".
4. Tool đọc của hub-self (`filehub_list`, `task_show`…) không bị audit là `tool.write`.
5. Điều phối viên thấy lý do chờ của task bằng chữ rõ (`wait`), không đoán "rate limit" từ mã thô.
Server lane GIẢ + ScriptedRuntime — không gọi Không gian số/claude thật.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from fake_kgs_lane import FakeLane
from lane_fake import close, configure_lane, events, make, until
from m7_fake import rpc, wait_status


@pytest.fixture
def lane():
    f = FakeLane(ping_s=0.3)
    f.human_write("Báo cáo/yeu-cau.md", "viết báo cáo\n")
    yield f
    f.close()


@pytest.fixture
async def env(hub_home, lane):
    d, s = await make(hub_home)
    await configure_lane(d, lane)
    yield d, s, lane
    await close(d)


async def _orphan_with_held_deletes(d, s, lane, *, extra_write: bool = False):
    d.hub.config.set("filehub.lane.max_delete_batch", 3)
    d.hub.config.set("filehub.lane.final_flush_s", 0.5)
    for i in range(6):
        lane.human_write(f"Báo cáo/nhieu/f{i}.md", f"tệp {i}\n")
    s.by_prompt["XOÁ"] = {"text": "xong", "hold": True}
    rid = d.hub.runs.submit("w1", "XOÁ", cwd="filehub:chung/Báo cáo")["id"]
    run = await wait_status(d, rid)
    cwd = Path(run["cwd"])
    for p in (cwd / "nhieu").iterdir():
        p.unlink()
    await until(lambda: events(d, "filehub.blocked"))
    if extra_write:
        d.hub.filehub.lane.workdir_of(rid).pending["moi.md"] = {"since": 0.0, "op": "write", "retry_at": 9e12}
        (cwd / "moi.md").write_text("mới\n")
    d.hub.runs.runtime().release(rid)
    await wait_status(d, rid, ("completed", "blocked"))
    await until(lambda: rid not in d.hub.runs.active and d.hub.filehub.lane.orphans())
    return rid


# ============================================================================ 1. lô xoá sau khi run kết thúc
async def test_orphan_keeps_held_batch_with_ops_and_refuse_keeps_files(env):
    d, s, lane = env
    rid = await _orphan_with_held_deletes(d, s, lane)
    o = d.hub.filehub.lane.orphans()[0]
    assert o["run_id"] == rid and o["root_id"] == "r_chung" and o["held"] == 6
    assert sorted(o["held_paths"]) == [f"nhieu/f{i}.md" for i in range(6)]
    assert {(x["op"], x["label"]) for x in o["items"]} == {("delete", "xoá")}
    # "Không xoá" (cùng RPC như khi run còn chạy) → tệp ở lại Không gian số, cây mồ côi biến mất
    r = await rpc(d, "filehub.resume", {"root_id": "r_chung", "discard": True})
    assert r["discarded"] == 6
    assert not d.hub.filehub.lane.orphans()
    assert all(lane.file(f"Báo cáo/nhieu/f{i}.md") == f"tệp {i}\n".encode() for i in range(6))


async def test_orphan_allow_deletes_after_run_end(env):
    d, s, lane = env
    await _orphan_with_held_deletes(d, s, lane)
    r = await rpc(d, "filehub.resume", {"root_id": "r_chung"})
    assert r["released"] == 6
    assert not any(lane.file(f"Báo cáo/nhieu/f{i}.md") for i in range(6))
    assert not d.hub.filehub.lane.orphans()


async def test_orphan_push_never_approves_held_deletes(env):
    d, s, lane = env
    rid = await _orphan_with_held_deletes(d, s, lane, extra_write=True)
    o = d.hub.filehub.lane.orphans()[0]
    assert {"path": "moi.md", "op": "write", "label": "ghi", "held": False} in o["items"]
    r = await rpc(d, "filehub.lane_orphan", {"tree": rid, "action": "push"})
    assert lane.file("Báo cáo/moi.md") == "mới\n".encode()
    assert all(lane.file(f"Báo cáo/nhieu/f{i}.md") for i in range(6))  # không bao giờ xoá ngầm
    assert r["pushed"] is False and r["held"] == 6
    o = d.hub.filehub.lane.orphans()[0]
    assert o["held"] == 6 and o["count"] == 6


# ============================================================================ 1. web
@pytest.fixture
async def web(hub_home):
    from cfharness.runtime import FakeRuntime
    from test_web import ISOLATED, _start, _stop

    e = await _start(hub_home, FakeRuntime(ISOLATED))
    yield e
    await _stop(e)


ORPHAN = {"tree": "r_t1", "folder": "chung", "root_id": "r_chung", "run_id": "r_t1", "agent_id": "w1", "task_id": "t_abc", "count": 4,
          "held": 3, "held_paths": ["Ghi chép/Nháp/nhap-04.txt", "Ghi chép/Nháp/nhap-05.txt", "Ghi chép/Nháp/nhap-06.txt"],
          "items": [{"path": "Ghi chép/Nháp/nhap-04.txt", "op": "delete", "label": "xoá", "held": True},
                    {"path": "Ghi chép/Nháp/nhap-05.txt", "op": "delete", "label": "xoá", "held": True},
                    {"path": "Ghi chép/Nháp/nhap-06.txt", "op": "delete", "label": "xoá", "held": True},
                    {"path": "Ghi chép/b.md", "op": "move", "label": "di chuyển", "from": "Ghi chép/a.md", "held": False}],
          "since": 0}


async def _with_orphan(e, monkeypatch):
    e.hub.config.set("filehub.folders", [{"name": "chung", "root_id": "r_chung", "ws_id": "w", "kind": "folder", "mode": "lane", "server": "qltm"}])
    monkeypatch.setattr(e.hub.filehub.lane, "orphans", lambda: [dict(ORPHAN)])


async def test_web_card_held_batch_after_run_end_and_explicit_ops(web, monkeypatch):
    e = web
    await e.login()
    await _with_orphan(e, monkeypatch)
    card = (await e.client.get("/partials/filehub/lane")).text
    assert "3 mục xoá đang chờ duyệt" in card and "Ghi chép/Nháp/nhap-04.txt" in card
    assert 'action="/do/filehub_lane_deletes"' in card and "Cho xoá" in card and "Không xoá" in card
    assert "di chuyển" in card and "Ghi chép/a.md" in card  # thay đổi khác: nêu thao tác
    # xác nhận "Cho xoá": nêu XOÁ + số + đường dẫn
    r = await e.post("/do/filehub_lane_deletes", {"root_id": "r_chung", "n": "3", "next": "/settings/filehub"})
    assert r.status_code == 200 and "Xoá 3 tệp trên Không gian số" in r.text and "nhap-06.txt" in r.text
    r = await e.post("/do/filehub_lane_deletes", {"root_id": "r_chung", "n": "3", "discard": "1", "next": "/settings/filehub"})
    assert "Không xoá — giữ 3 tệp" in r.text and "bỏ thay đổi của agent" in r.text
    # xác nhận "Đẩy lại": liệt kê thao tác từng mục, nói rõ lô xoá KHÔNG đi kèm
    r = await e.post("/do/filehub_lane_orphan", {"tree": "r_t1", "action": "push", "next": "/settings/filehub"})
    assert r.status_code == 200 and "Di chuyển Ghi chép/a.md → Ghi chép/b.md" in r.text
    assert "3 mục xoá" in r.text and "Đẩy lại các thay đổi còn kẹt lên Không gian số?" not in r.text
    r = await e.post("/do/filehub_lane_orphan", {"tree": "r_t1", "action": "drop", "next": "/settings/filehub"})
    assert "tệp trên Không gian số giữ nguyên" in r.text and "Ghi chép/b.md" in r.text


async def test_web_inbox_shows_held_batch_and_links_blocked_task(web, monkeypatch):
    e = web
    await e.login()
    await _with_orphan(e, monkeypatch)
    e.hub.agents.create({"id": "w1", "name": "w1"})
    t = e.hub.tasks.create({"title": "Xoá nháp", "assignee_agent_id": "w1"})
    e.hub.tasks.set_status(t["id"], "blocked", reason="x", block_reason="Đã xoá 3/6 tệp; ba tệp còn lại bị giữ")
    monkeypatch.setitem(ORPHAN, "task_id", t["id"])
    page = (await e.client.get("/inbox")).text
    assert "Xoá hàng loạt chờ bạn duyệt" in page and "nhap-05.txt" in page
    assert 'action="/do/filehub_lane_deletes"' in page
    assert re.search(r'href="#held-r_chung"', page)  # task bị chặn → link tới lô xoá


# ============================================================================ 2
async def test_web_advanced_url_is_empty_with_placeholder(web):
    e = web
    await e.login()
    card = (await e.client.get("/partials/filehub/lane")).text
    assert 'name="url"' in card and 'value="http://127.0.0.1:8223"' not in card and 'placeholder="http://127.0.0.1:8223"' in card


# ============================================================================ 3
async def test_health_default_60s_and_checked_time_on_card(env):
    from cfharness.config import DEFAULTS

    d, s, lane = env
    assert DEFAULTS["filehub"]["lane"]["health_ok_s"] == 60
    st = (await rpc(d, "filehub.lane_status", {}))["servers"][0]
    assert re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", st["checked"] or "")


# ============================================================================ 4
async def test_hub_self_read_tools_not_audited_as_write(env):
    from cfharness.events import make_event

    d, s, lane = env
    before = d.hub.store.conn.execute("SELECT COUNT(*) FROM audit_log WHERE kind='tool.write'").fetchone()[0]
    for tool in ("filehub_list", "task_show", "hub_overview", "fs_read"):
        d.hub.publish(make_event("tool.start", "boss", {"name": f"mcp__hub-self__{tool}", "tool_id": f"tu_{tool}", "input": {}}, run_id="r_x"))
    d.hub.publish(make_event("tool.start", "boss", {"name": "mcp__hub-self__task_create", "tool_id": "tu_w", "input": {}}, run_id="r_x"))
    await asyncio.sleep(0.1)
    rows = d.hub.store.conn.execute("SELECT target FROM audit_log WHERE kind='tool.write'").fetchall()[before:]
    assert [r[0] for r in rows] == ["hub-self/task_create"]


# ============================================================================ 5
async def test_manager_sees_explicit_wait_reason(env):
    from cfharness.manager.ops import ManagerOps

    d, s, lane = env
    t = d.hub.tasks.create({"title": "chờ", "assignee_agent_id": "w1"})
    ops = ManagerOps(d.hub.manager, {"id": "r_x", "agent_id": "boss"})
    d.hub.tasks.update(t["id"], {"wait_reason": "concurrency"})
    row = next(x for x in ops.op_task_list({}) if x["id"] == t["id"])
    assert "free run slot" in row["wait"] and "rate limit" not in row["wait"].lower()
    d.hub.tasks.update(t["id"], {"wait_reason": "workspace_locked"})
    row = next(x for x in ops.op_hub_overview({})["open_tasks"] if x["id"] == t["id"])
    assert "workspace" in row["wait"] and "rate limit" not in row["wait"].lower()
    d.hub.tasks.update(t["id"], {"wait_reason": "rate_limit"})
    row = next(x for x in ops.op_task_list({}) if x["id"] == t["id"])
    assert "no rate-limit error" in row["wait"]
