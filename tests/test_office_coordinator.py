"""Văn phòng 3D — điều phối viên là nhân vật chính, ngồi bàn ra lệnh, nói chuyện với agent, agent ít đi lại (docs/dev-notes/office-coordinator.md).

- Hành vi (Node, không trình duyệt): `tests/office_coordinator_check.mjs` dựng World/Walker/Actor thật + mô phỏng thời gian.
- Tĩnh: talk.js / chat.js / walk.js không dựng HTML từ dữ liệu, không đọc khung `item.html` của /sse/run; talk.js chỉ gửi 3 action run
  qua `/office/do/` (cùng danh sách trắng của server); office.js ẩn actor điều phối viên AI khi đi bộ (không có 2 điều phối viên).
Endpoint: tests/test_office_talk.py.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from cfharness.web import office as O

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "cfharness" / "web" / "static"
NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="cần Node để dựng văn phòng headless")
def test_coordinator_behaviour_headless():
    r = subprocess.run([NODE, str(ROOT / "tests" / "office_coordinator_check.mjs"), str(ROOT)], capture_output=True, text=True,
                       encoding="utf-8", timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL" not in r.stdout and r.stdout.count("ok ") >= 24, r.stdout
    for must in ("không có 2 điều phối viên", "ngồi bàn ≥ 80 %", "spawn n=19 teams=2", "spawn n=80 teams=0", "spawn n=80 teams=3", "doorSpawn n=19 teams=2", "đứng ở doorSpawn", "đang nói chuyện",
                 "vẫn giữ câu trả lời cuối", "ghế là mgrSpot của World mới", "confirmKey: steer gắn run_id", "resetsRetry: chỉ delta/item", "streamUrl: lần đầu", "epoch đổi → bỏ đoạn dở"):
        assert must in r.stdout, must


def _src(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_talk_chat_walk_never_build_html_from_data():
    for f in ("office/talk.js", "office/chat.js", "office/walk.js"):
        s = _src(f)
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "DOMParser", "createContextualFragment", "document.write", "eval("):
            assert bad not in s, (f, bad)
    talk = _src("office/talk.js")
    # khung SSE run `item` mang HTML (transcript) → talk.js không bao giờ đọc .html
    assert not re.search(r"\.html\b", talk) and "f.k === 'text'" in talk
    assert "fetch(" not in talk  # gửi qua api.post (CSRF + khoá phiên), đọc qua EventSource /sse/run


def test_talk_only_uses_whitelisted_run_actions():
    talk = _src("office/talk.js")
    names = set(re.findall(r"name: '(\w+)'", talk))
    assert names == {"steer", "run_continue", "run_start"} and names <= set(O.OFFICE_ACTIONS) and names == O.RUN_ACTIONS
    assert sorted(set(re.findall(r"'(/[a-z/]+)'", talk))) == ["/office/do/", "/sse/run/"]
    # confirm=yes chỉ sau khi người bấm Đồng ý (o.ask), hoặc đã đồng ý steer cho ĐÚNG run này; run mới (tốn tiền) lần nào cũng hỏi
    send = talk[talk.index("async function send()"):talk.index("// ---------------------------------------------------------------- mở / đóng")]
    assert send.count("confirm: 'yes'") == 2 and "await o.ask(res.data)" in send and "ck && s.confirmed.has(ck)" in send
    assert send.index("await o.ask(res.data)") < send.index("if (ck) s.confirmed.add(ck)") and "s.confirmed.has(plan.name)" not in send
    ck = talk[talk.index("export function confirmKey"):talk.index("export function replyFrame")]
    assert "plan.name === 'steer'" in ck and "'steer:' + plan.fields.id" in ck and ": null;" in ck


def test_office_hides_ai_coordinator_while_walking():
    js = _src("office.js")
    assert "a.puppet = a === coord;" in js and "const coord = walk.active && walk.isCoordinator ? coordinator() : null;" in js
    assert ".filter((a) => !a.puppet).map((a) => a.chibi.group)" in js  # actor ẩn không bấm được
    assert "a.puppet = false; a.talking = null;" in js  # tắt đi bộ → về như cũ
    assert "c.group.visible = !this.puppet;" in _src("office/actors.js")
    assert "if (walk.sit(world.mgrSpot)) chat.open({ desk: true });" in js and "talk.open(t.id)" in js
