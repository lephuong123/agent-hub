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


async def test_sidebar_groups_lobby_and_workstreams_and_create(wenv):
    await wenv.login()
    await designate(wenv)
    r = await wenv.client.get("/manager")
    side = _side(r.text)
    assert "Sảnh" in side and "Luồng việc" in side and 'action="/do/ws_create"' in side
    # tạo luồng từ thanh bên → chuyển vào luồng
    r = await wenv.post("/do/ws_create", {"key": "LH-55166", "title": "Monitor <b>x</b>", "sprint": "S216", "next": "/manager"})
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/manager?w=ws_")
    wid = loc.split("w=", 1)[1]
    r = await wenv.client.get(loc)
    assert "LH-55166" in _side(r.text) and "Monitor &lt;b&gt;x&lt;/b&gt;" in r.text  # tên luồng là dữ liệu: escape
    assert "<b>x</b>" not in r.text
    assert 'class="ws-chip' in r.text and "Hội thoại mới trong luồng" in r.text
    assert f'name="w" value="{wid}"' in r.text  # tin đầu tiên mở hội thoại trong luồng
    assert "STATE.md" in r.text and 'action="/do/ws_state"' in r.text


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
    side = _side(r.text)
    lobby_part = side.split("Luồng việc", 1)[0]
    assert f"?c={lob['conversation_id']}" in lobby_part
    assert f"?c={cid}" not in lobby_part
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
    assert "Đã đóng (1)" in _side(r.text)
    actions = [row["kind"] for row in wenv.hub.store.conn.execute("SELECT kind FROM audit_log")]
    assert "workstream.update" in actions and "manager.conversation_update" in actions
