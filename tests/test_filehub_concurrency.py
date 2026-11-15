"""Người và agent cùng đụng thư mục chung (plan-cloud-files §3.4, quyết định 3 — chỉ cảnh báo, không chặn):

- run ghi vào `filehub:` đang chạy → `control.json` có presence (binary đẩy dải "cfharness đang làm việc" lên Không gian số), hết run → null;
- binary sync báo thay đổi chiều `down` (người sửa trên web) trong lúc run giữ khoá gốc → steer `by="hub"` câu CỐ ĐỊNH, một lần mỗi tệp;
- `signals` không coi steer của hub là correction của người;
- bản xung đột → event `filehub.conflict` → Telegram/Slack dòng chữ cố định (không nút).
Binary sync GIẢ (`fake_tangthu_sync.py`) + ScriptedRuntime giữ run ở `running`. Không gọi mạng/claude.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from cfharness.daemon import Daemon
from cfharness.events import make_event
from cfharness.filehub.service import STEER_TEXT
from fh_fake import configure, events, inject, until
from m3_fake import ScriptedRuntime
from m7_fake import rpc, wait_status

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="binary sync chỉ macOS/Linux (shim #!/bin/sh)")


@pytest.fixture
async def env(hub_home, tmp_path):
    rt = ScriptedRuntime(lambda spec: {"text": "xong", "hold": "GIU" in spec.prompt})
    d = Daemon(hub_home, runtime=rt)
    d.hub.config.set("tasks.tick_s", 0.05)
    # regex correction bắt cả câu của hub — nếu signals KHÔNG bỏ qua by=hub thì sẽ ghi correction
    d.hub.config.set("learning.steer_correction_regex", "(?i)human changed|re-read")
    local = configure(d.hub, tmp_path)
    (local / "Báo cáo").mkdir()
    d.hub.start()
    d.hub.agents.create({"id": "w1", "name": "w1"})
    await until(lambda: (d.hub.filehub.read_status() or {}).get("roots"))
    yield d, rt, local
    rt.release()
    await asyncio.sleep(0.05)
    d.hub.runs.shutting_down = True
    await d.hub.dispatcher.stop()
    await d.hub.filehub.stop()
    d.hub.store.close()


def _control(d) -> dict:
    return json.loads(d.hub.filehub.control_path.read_text())


async def test_presence_while_writing_run_runs(env):
    d, rt, local = env
    r = await rpc(d, "run.start", {"agent": "w1", "prompt": "GIU viết báo cáo", "cwd": "filehub:chung/Báo cáo"})
    await wait_status(d, r["id"])
    await until(lambda: (_control(d)["presence"].get("r_chung") or {}).get("label") == "cfharness · w1")
    assert _control(d)["presence"]["r_chung"]["ttl_s"] == 600
    # binary đã đọc lại control.json (mtime đổi) — đẩy presence lên Không gian số
    seen = d.hub.filehub.profile_dir / "control_seen.jsonl"
    await until(lambda: seen.exists() and any((json.loads(x)["presence"].get("r_chung") or {}).get("label") for x in seen.read_text().splitlines()))
    rt.release(r["id"])
    await wait_status(d, r["id"], ("completed",))
    await until(lambda: _control(d)["presence"].get("r_chung") is None)
    # run chỉ đọc không đặt presence
    ro = d.hub.runs.submit("w1", "GIU đọc", cwd="filehub:chung", run_opts={"read_only": True})
    await wait_status(d, ro["id"])
    await asyncio.sleep(0.2)
    assert _control(d)["presence"].get("r_chung") is None
    rt.release(ro["id"])


async def test_cloud_change_during_run_steers_once_and_is_not_correction(env):
    d, rt, local = env
    r = await rpc(d, "run.start", {"agent": "w1", "prompt": "GIU sửa báo cáo", "cwd": "filehub:chung/Báo cáo"})
    await wait_status(d, r["id"])
    inject(d.hub, op="update", root_id="r_chung", path="Báo cáo/q3.docx", content="người sửa")
    await until(lambda: [e for e in events(d.hub, "user.message") if e.run_id == r["id"]])
    ev = [e for e in events(d.hub, "user.message") if e.run_id == r["id"]][0]
    assert ev.payload["by"] == "hub" and ev.payload["steer"] and ev.payload["text"] == STEER_TEXT.format(rel="Báo cáo/q3.docx")
    assert (local / "Báo cáo" / "q3.docx").read_text() == "người sửa"
    # cùng tệp đổi lần nữa → không steer lặp; tệp khác → steer
    inject(d.hub, op="update", root_id="r_chung", path="Báo cáo/q3.docx", content="lần 2")
    inject(d.hub, op="create", root_id="r_chung", path="ghi-chu.txt", content="x")
    await until(lambda: len([e for e in events(d.hub, "user.message") if e.run_id == r["id"]]) >= 2)
    await asyncio.sleep(0.3)
    texts = [e.payload["text"] for e in events(d.hub, "user.message") if e.run_id == r["id"]]
    assert texts == [STEER_TEXT.format(rel="Báo cáo/q3.docx"), STEER_TEXT.format(rel="ghi-chu.txt")]
    assert d.hub.store.get_run(r["id"])["steer_count"] == 2
    corr = d.hub.store.conn.execute("SELECT COUNT(*) FROM signals WHERE kind='correction'").fetchone()[0]
    assert corr == 0
    # đối chứng: steer của NGƯỜI cùng nội dung vẫn là correction
    await rpc(d, "run.steer", {"id": r["id"], "text": "the human changed it, re-read"})
    await until(lambda: d.hub.store.conn.execute("SELECT COUNT(*) FROM signals WHERE kind='correction'").fetchone()[0] == 1)
    rt.release(r["id"])


async def test_no_steer_without_lock_holder(env):
    d, rt, local = env
    inject(d.hub, op="create", root_id="r_chung", path="moi.txt", content="x")
    await until(lambda: (local / "moi.txt").exists())
    await asyncio.sleep(0.3)
    assert not [e for e in events(d.hub, "user.message") if (e.payload or {}).get("by") == "hub"]


async def test_conflict_event_and_chat_text(env):
    from cfharness.notify.slack import format_message
    from cfharness.notify.telegram import format_event

    d, rt, local = env
    inject(d.hub, op="conflict", root_id="r_chung", path="Báo cáo/q3 (bản xung đột).docx")
    await until(lambda: events(d.hub, "filehub.conflict"))
    ev = events(d.hub, "filehub.conflict")[0]
    tg = format_event(ev)
    assert "Bản xung đột trong «chung»" in tg and "q3 (bản xung đột).docx" in tg and "không mất bản nào" in tg
    # dòng kết quả run / xoá: chữ cố định, link là CHỮ (không nút)
    url = "https://kgs.example.test/files/fr_chung"
    ch = make_event("filehub.changed", "w1", {"folder": "chung", "count": 3, "paths": ["a", "b", "c"], "url": url, "run_id": "r1"})
    assert format_message(ch) == f"📁 3 tệp trong «chung» đã đổi (do *w1*)\nMở: {url}"
    de = make_event("filehub.deleted", "w1", {"folder": "chung", "count": 7, "paths": ["x/1.txt", "x/2.txt"], "url": url, "run_id": "r1"})
    t = format_event(de)
    assert "đã xoá 7 tệp trong «chung»" in t and "x/1.txt, x/2.txt (+5)" in t and "thùng rác Không gian số" in t and f"Mở: {url}" in t
    bad = make_event("filehub.changed", "w1", {"folder": "chung", "count": 1, "url": "javascript:alert(1)"})
    assert "javascript" not in format_message(bad)
    sw = make_event("filehub.secret_warning", "w1", {"folder": "chung", "count": 1, "paths": [".env"]})
    assert "có thể chứa bí mật" in format_message(sw)
    for et in ("filehub.changed", "filehub.deleted", "filehub.conflict", "filehub.secret_warning", "filehub.blocked"):
        assert et in d.hub.config.get("notify.telegram.events") and et in d.hub.config.get("notify.slack.events")
