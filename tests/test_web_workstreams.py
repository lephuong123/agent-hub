"""Trang Điều phối theo luồng việc (docs/dev-notes/workstreams.md) — daemon in-process + FakeRuntime, app Starlette qua ASGITransport."""

from __future__ import annotations

import re

import pytest

from cfharness.runtime import FakeRuntime

from test_web import ISOLATED, _start, _stop  # noqa: F401
from test_web_manager import designate, mgr_rpc, send


@pytest.fixture
async def wenv(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    e.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    e.hub.agents.create({"id": "w1", "name": "w1"})
    yield e
    await _stop(e)


def _side(html: str) -> str:
    return re.search(r'<section class="side-recents".*?</section>', html, re.S).group(0)


def _bar(html: str) -> str:
    return re.search(r'<nav class="ws-bar".*?</nav>', html, re.S).group(0)


async def test_sidebar_groups_lobby_and_workstreams_and_create(wenv):
    await wenv.login()
    await designate(wenv)
    r = await wenv.client.get("/manager")
    bar = _bar(r.text)
    assert "Sảnh" in bar and 'action="/do/ws_create"' in bar and "Hội thoại ở Sảnh" in _side(r.text)
    # mở luồng chỉ cần tên; tên bắt đầu bằng mã Jira → tách mã
    r = await wenv.post("/do/ws_create", {"title": "LH-55166 Monitor <b>x</b>", "next": "/manager"})
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/manager?w=ws_")
    wid = loc.split("w=", 1)[1]
    r = await wenv.client.get(loc)
    assert "Monitor &lt;b&gt;x&lt;/b&gt;" in _bar(r.text) and "<b>x</b>" not in r.text  # tên luồng là dữ liệu: escape
    assert 'class="ws-tab on"' in r.text and "LH-55166" in r.text
    assert f'name="w" value="{wid}"' in r.text  # tin đầu tiên mở hội thoại trong luồng
    assert "Ghi chú luồng" in r.text and 'action="/do/ws_state"' in r.text
    # tên trống → hub tự đặt tên
    r = await wenv.post("/do/ws_create", {"title": "", "next": "/manager"})
    assert r.status_code == 303
    w2 = await mgr_rpc(wenv, "workstream.get", {"id": r.headers["location"].split("w=", 1)[1]})
    assert w2["title"].startswith("Luồng ") and w2["key"] is None


async def test_send_in_workstream_files_conversation_and_lobby_stays_separate(wenv):
    await wenv.login()
    await designate(wenv)
    w = await mgr_rpc(wenv, "workstream.create", {"title": "Dreem shot", "key": "LH-55815", "state": "PR #712 chờ QA"})
    r = await wenv.post("/manager/send", {"text": "sao rồi?", "w": w["id"]}, headers={"X-Ahub-Fetch": "1"})
    assert r.status_code == 200, r.text
    cid = r.json()["conversation_id"]
    convs = await mgr_rpc(wenv, "manager.conversations", {"workstream": w["id"]})
    assert [c["id"] for c in convs] == [cid]
    lob = await send(wenv, "việc khác ở sảnh")
    assert lob["conversation_id"] != cid
    # mở hội thoại của luồng: chip + STATE + panel task; Sảnh chỉ có hội thoại không thuộc luồng
    r = await wenv.client.get(f"/manager?c={cid}")
    assert "PR #712 chờ QA" in r.text and "LH-55815" in r.text
    side = _side(r.text)  # trong luồng: thanh bên chỉ có hội thoại của luồng
    assert f"?c={cid}" in side and f"?c={lob['conversation_id']}" not in side
    side = _side((await wenv.client.get("/manager")).text)  # Sảnh
    assert f"?c={lob['conversation_id']}" in side and f"?c={cid}" not in side
    # ?w=<mã> → hội thoại gần nhất của luồng
    r = await wenv.client.get("/manager?w=LH-55815")
    assert f'name="c" value="{cid}"' in r.text


async def test_state_edit_rename_move_archive_close(wenv):
    await wenv.login()
    await designate(wenv)
    w = await mgr_rpc(wenv, "workstream.create", {"title": "Vulns", "key": "LH-54698"})
    r = await wenv.post("/do/ws_state", {"id": w["id"], "state": "Goal: fix CVEs", "next": "/manager"})
    assert r.status_code == 303
    assert (await mgr_rpc(wenv, "workstream.get", {"id": w["id"]}))["state"] == "Goal: fix CVEs"
    res = await send(wenv, "hội thoại ở sảnh")
    cid = res["conversation_id"]
    r = await wenv.post("/do/conv_rename", {"conversation_id": cid, "title": "Đổi tên rồi", "next": "/manager"})
    assert r.status_code == 303
    r = await wenv.post("/do/conv_move", {"conversation_id": cid, "workstream": w["id"], "next": "/manager"})
    assert r.status_code == 303
    c = (await mgr_rpc(wenv, "manager.conversations", {"workstream": w["id"]}))[0]
    assert c["id"] == cid and c["title"] == "Đổi tên rồi"
    # lưu trữ cần xác nhận
    r = await wenv.post("/do/conv_archive", {"conversation_id": cid, "next": "/manager"})
    assert r.status_code == 200 and "Lưu trữ hội thoại" in r.text
    r = await wenv.post("/do/conv_archive", {"conversation_id": cid, "confirm": "yes", "next": "/manager"})
    assert r.status_code == 303
    assert await mgr_rpc(wenv, "manager.conversations", {"workstream": w["id"]}) == []
    # đóng luồng (xác nhận) → xuống mục Đã đóng
    r = await wenv.post("/do/ws_close", {"id": w["id"], "confirm": "yes", "next": "/manager"})
    assert r.status_code == 303
    r = await wenv.client.get("/manager")
    assert "Đã đóng 1" in _bar(r.text)
    actions = [row["kind"] for row in wenv.hub.store.conn.execute("SELECT kind FROM audit_log")]
    assert "workstream.update" in actions and "manager.conversation_update" in actions


def test_tab_state_priority():
    from cfharness.web.manager import tab_state

    assert tab_state({"blocked": 1, "running": 2, "open": 3}, 0, 1, True)[0] == "danger"  # cần người thắng đang chạy
    assert tab_state({}, 2, 0, True)[0] == "danger"
    dot, label = tab_state({"running": 0, "open": 1}, 0, 1, True)
    assert dot == "run" and "điều phối viên đang xử lý" in label
    assert tab_state({"running": 2, "open": 2}, 0, 0, True)[0] == "run"
    assert tab_state({"open": 2, "total": 3}, 0, 0, True)[0] == "idle"
    assert tab_state({"open": 0, "total": 3, "done": 3}, 0, 0, False)[0] == "done"
    assert tab_state({}, 0, 0, True) == ("done", "Đã xong — không có gì đang chạy")
    assert tab_state({}, 0, 0, False)[0] == "none"


async def test_tabs_partial_shows_running_then_done(wenv):
    import asyncio

    await wenv.login()
    await designate(wenv)
    w = await mgr_rpc(wenv, "workstream.create", {"title": "Dreem shot", "key": "LH-55815"})
    r = await wenv.post("/manager/send", {"text": "làm đi", "w": w["id"]}, headers={"X-Ahub-Fetch": "1"})
    res = r.json()
    run_id, cid = res["run_id"], res["conversation_id"]
    for _ in range(200):  # FakeRuntime xong rất nhanh
        if (wenv.hub.store.get_run(run_id) or {}).get("status") not in ("queued", "running"):
            break
        await asyncio.sleep(0.05)

    def tab(html: str) -> str:
        return re.search(rf'<a class="ws-tab[^"]*" href="/manager\?w={w["id"]}".*?</a>', html, re.S).group(0)

    # giả lập lượt điều phối viên còn đang chạy trong luồng
    wenv.hub.store.conn.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
    convs = await mgr_rpc(wenv, "manager.conversations", {"workstream": w["id"]})
    assert convs[0]["running"] == {"id": run_id, "status": "running"}
    r = await wenv.client.get(f"/partials/manager/tabs?w={w['id']}")
    assert r.status_code == 200 and r.text.lstrip().startswith('<div class="ws-tabs" id="ws-tabs"')
    t = tab(r.text)
    assert 'ws-dot run' in t and "Đang chạy" in t and "ws-tab on" in t
    page = (await wenv.client.get(f"/manager?c={cid}")).text
    assert 'ws-dot run' in _side(page)  # hội thoại đang chạy có chấm ở thanh bên
    # xong → dấu "đã xong"
    wenv.hub.store.conn.execute("UPDATE runs SET status='completed' WHERE id=?", (run_id,))
    t = tab((await wenv.client.get("/partials/manager/tabs")).text)
    assert 'ws-dot done' in t and "Đã xong" in t and "ws-tab on" not in t
    # trả lời tới trong lúc đang xem hội thoại: làm mới hàng tab kèm c=<hội thoại> → không hiện "chưa đọc"
    wenv.hub.store.conn.execute("UPDATE manager_conversations SET read_upto=0 WHERE id=?", (cid,))
    assert '<span class="ws-unread">' in tab((await wenv.client.get(f"/partials/manager/tabs?w={w['id']}")).text)
    assert '<span class="ws-unread">' not in tab((await wenv.client.get(f"/partials/manager/tabs?w={w['id']}&c={cid}")).text)


async def test_manage_menu_info_rename_and_delete(wenv):
    """Menu "⚙ Luồng này": thông tin luồng + đổi tên + xoá (xác nhận trước, rồi về Sảnh)."""
    await wenv.login()
    await designate(wenv)
    w = await mgr_rpc(wenv, "workstream.create", {"title": "Monitor", "key": "LH-55166", "sprint": "S216"})
    html = (await wenv.client.get(f"/manager?w={w['id']}")).text
    menu = re.search(r'<details class="ws-pop-wrap" id="ws-manage">.*?</details>', html, re.S).group(0)
    for s in ("Đang mở", "LH-55166", "S216", "0/0 xong", w["id"], 'action="/do/ws_edit"', 'action="/do/ws_close"', 'action="/do/ws_delete"'):
        assert s in menu, s
    assert 'id="ws-manage"' not in (await wenv.client.get("/manager")).text  # Sảnh: không có menu luồng
    r = await wenv.post("/do/ws_edit", {"id": w["id"], "title": "LH-55167 Monitor mới", "next": "/manager"})
    assert r.status_code == 303
    got = await mgr_rpc(wenv, "workstream.get", {"id": w["id"]})
    assert (got["key"], got["title"]) == ("LH-55167", "Monitor mới")
    # xoá: lần đầu chỉ hỏi xác nhận, chưa xoá
    r = await wenv.post("/do/ws_delete", {"id": w["id"], "label": "LH-55167 · Monitor mới", "next": "/manager"})
    assert r.status_code == 200 and "XOÁ HẲN luồng" in r.text
    assert (await mgr_rpc(wenv, "workstream.get", {"id": w["id"]}))["id"] == w["id"]
    r = await wenv.post("/do/ws_delete", {"id": w["id"], "confirm": "yes", "next": "/manager"})
    assert r.status_code == 303 and r.headers["location"].startswith("/manager")
    assert await mgr_rpc(wenv, "workstream.list", {"status": "all"}) == []
    assert "Monitor mới" not in _bar((await wenv.client.get("/manager")).text)
    row = wenv.hub.store.conn.execute("SELECT channel FROM audit_log WHERE kind='workstream.delete'").fetchone()
    assert row and row[0] == "web"
