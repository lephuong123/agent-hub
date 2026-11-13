"""Đợt 3 — P2 hoàn thiện giao diện chat (docs/dev-notes/plan-chat-ui-claude.md mục 3, Đợt 3).

- TodoWrite → MỘT checklist "Việc cần làm" mỗi run (`todo-<run>`), cập nhật tại chỗ; phát lại == live.
- Báo rõ khi bị cắt (tin 8.000 ký tự, output tool ở trần, suy nghĩ) kèm lệnh `cfharness runs logs <id>`.
- Run dài: > 300 khối → chỉ vẽ 150 khối cuối + partial `/partials/run/<id>/items?before=<seq>` (cần đăng nhập, nhóm tool nguyên vẹn).
- Sao chép cả tin / output tool: văn bản gốc trong <template class="tx-src"> (không trong data-*).
- Dòng trạng thái live (`{t:"status"}`), "Đã suy nghĩ · ~1.2k token".
- Điều phối: vạch ngăn theo ngày; tải lại khi lượt còn chạy → khung đang trả lời có tool đang dùng + checklist.
- `.udiff` của trang diff: không có "\n" giữa các dòng (dòng trống).
Không gọi `claude` thật.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from cfharness.events import make_event
from cfharness.runtime import FakeRuntime
from cfharness.web import present as P
from cfharness.web.app import _run_converter, make_templates, replay_tx, run_status_line
from cfharness.web.manager import pending_info, with_days
from test_web import ISOLATED, _start, _stop, until  # noqa: F401
from test_web_markdown import _safe
from test_web_run_transcript import live_html, norm, parse, replay_html

TPL = make_templates()
ITEMS = TPL.env.get_template("_items.html").module
TXM = TPL.env.get_template("_transcript.html").module


def ev(seq, type_, payload, **kw):
    return {"type": type_, "seq": seq, "run_id": "r1", "ts": f"2026-11-13T10:{seq // 60:02d}:{seq % 60:02d}Z", "payload": payload, **kw}


def todo_start(seq, tid, todos):
    return ev(seq, "tool.start", {"tool_id": tid, "name": "TodoWrite", "input": {"todos": todos}, "args_preview": ""})


def done(seq, tid, name="TodoWrite", err=False, out="ok"):
    return ev(seq, "tool.complete", {"tool_id": tid, "name": name, "duration_s": 0.1, "is_error": err, "result_preview": out})


T1 = [{"content": "Đọc <b>code</b>", "status": "completed", "activeForm": "Đang đọc code"},
      {"content": "Chạy test", "status": "in_progress", "activeForm": "Đang chạy test"},
      {"content": "Sửa lỗi", "status": "pending", "activeForm": "Đang sửa lỗi"}]
T2 = [dict(t, status="completed") for t in T1]

TODO_EVENTS = [
    ev(1, "run.queued", {"prompt_preview": "làm", "kind": "adhoc"}),
    todo_start(2, "td1", T1), done(3, "td1"),
    ev(4, "tool.start", {"tool_id": "a1", "name": "Read", "input": {"file_path": "/w/a.py"}, "args_preview": ""}), done(5, "a1", "Read"),
    ev(6, "message.complete", {"text": "đang làm"}),
    todo_start(7, "td2", T2), done(8, "td2"),
    ev(9, "message.complete", {"text": "xong"}),
]


# =========================================================================== TodoWrite
def test_todowrite_is_one_checklist_per_run_replaced_in_place():
    tx = replay_tx(TODO_EVENTS)
    todos = [i for i in tx.items if i["kind"] == "todo"]
    assert len(todos) == 1 and todos[0]["dom"] == "todo-r1"
    assert not any(i["kind"] == "tool" and i["tool"] == "TodoWrite" for i in tx.items)  # không thành dòng tool
    d = todos[0]["todo"]
    assert (d["n"], d["done"], d["current"]) == (3, 3, "")  # bản cuối (T2) thay bản đầu
    blocks = tx.blocks()
    assert [b["kind"] for b in blocks][:3] == ["prompt", "todo", "group"]  # giữ chỗ lần đầu xuất hiện
    html = str(ITEMS.item(todos[0]))
    assert 'id="todo-r1"' in html and "Việc cần làm" in html and "3/3 xong" in html
    assert html.count('type="checkbox" disabled checked') == 3 and "Đọc &lt;b&gt;code&lt;/b&gt;" in html and "<b>code" not in html
    _safe(html, icons=True)
    # bản đầu: mục đang làm hiện activeForm, 1/3 xong, ô chưa tích
    first = replay_tx(TODO_EVENTS[:3])
    h1 = str(ITEMS.item(next(i for i in first.items if i["kind"] == "todo")))
    assert "1/3 xong" in h1 and "Đang chạy test" in h1 and h1.count("checked") == 1 and "đang làm" in h1


def test_todowrite_live_equals_replay_and_updates_by_dom():
    live, msgs = live_html(TODO_EVENTS)
    assert live == norm(replay_html(TODO_EVENTS))
    todo_msgs = [m for m in msgs if m["t"] == "item" and m["kind"] == "todo"]
    assert [m["dom"] for m in todo_msgs] == ["todo-r1", "todo-r1"] and "3/3 xong" in todo_msgs[-1]["html"]
    assert len([n for n in parse(live).walk() if n.get("id") == "todo-r1"]) == 1


def test_todowrite_error_still_shows_tool_line_and_subagent_todo_stays_tool():
    evs = [todo_start(1, "td1", T1), done(2, "td1", err=True, out="lỗi ghi")]
    tx = replay_tx(evs)
    assert [i["kind"] for i in tx.items] == ["todo", "tool"] and tx.items[1]["error"]
    sub = replay_tx([dict(todo_start(1, "td9", T1), subagent_id="x")])
    assert sub.items[0]["kind"] == "tool"  # TodoWrite của subagent: dòng tool như cũ


# =========================================================================== báo cắt
def test_truncation_notices_for_message_tool_output_and_thinking():
    long = "x" * 8000
    evs = [ev(1, "message.complete", {"text": long, "truncated": True}),
           ev(2, "reasoning.complete", {"text": "y" * 8000, "truncated": True, "redacted": False}),
           ev(3, "tool.start", {"tool_id": "b1", "name": "Bash", "input": {"command": "cat big"}, "args_preview": ""}),
           done(4, "b1", "Bash", out="z" * 2500)]
    html = replay_html(evs)
    assert "Tin bị cắt ở 8.000 ký tự — xem đủ bằng <code>cfharness runs logs r1</code>" in html
    assert "Suy nghĩ bị cắt ở 8.000 ký tự — xem đủ bằng <code>cfharness runs logs r1</code>" in html
    assert "Output bị cắt ở 2.000 ký tự — xem đủ bằng <code>cfharness runs logs r1</code>" in html
    plain = replay_html([ev(1, "message.complete", {"text": "ngắn"})])
    assert "bị cắt" not in plain
    assert P.cut_note("Tin", 20000, None) == "Tin bị cắt ở 20.000 ký tự"


# =========================================================================== sao chép
def test_copy_whole_message_and_tool_output_raw_source_in_template():
    evs = [ev(1, "message.complete", {"text": "**đậm** <script>x</script>"}),
           ev(2, "tool.start", {"tool_id": "b1", "name": "Bash", "input": {"command": "ls"}, "args_preview": ""}),
           done(3, "b1", "Bash", out="a.txt\n<b>‮</b>")]
    html = replay_html(evs)
    srcs = re.findall(r'<button type="button" class="tx-copy tx-copy-src"[^>]*>.*?</button><template class="tx-src">(.*?)</template>', html, re.S)
    assert srcs == ["**đậm** &lt;script&gt;x&lt;/script&gt;", "a.txt\n&lt;b&gt;‮&lt;/b&gt;"]  # văn bản GỐC, escape
    assert not re.search(r"data-[a-z-]+=\"[^\"]*(đậm|a\.txt)", html)  # không bao giờ trong data-*
    _safe(html, icons=True)
    # tin của Điều phối (tx_msg không có `text`) không có nút này — trang Điều phối có hàng thao tác riêng
    assert "tx-copy-src" not in str(TXM.tx_msg({"html": "<p>x</p>", "seq": None}, "mm-md"))


# =========================================================================== dòng trạng thái + token suy nghĩ
def test_status_line_messages_shape_and_no_html():
    assert run_status_line({"type": "turn.start", "payload": {}}) == {"t": "status", "state": "think", "label": "Đang nghĩ…"}
    s = run_status_line({"type": "tool.start", "payload": {"name": "Bash", "input": {"command": "npm test"}}})
    assert s == {"t": "status", "state": "tool", "label": "Chạy npm test…"}
    assert run_status_line({"type": "tool.generating", "payload": {"name": "mcp__x__do"}})["label"].startswith("Đang soạn lệnh")
    assert run_status_line({"type": "reasoning.progress", "payload": {"estimated_tokens": 1234}}) == {"t": "status", "think_tokens": "~1.2k"}
    assert run_status_line({"type": "approval.waiting_human", "payload": {}})["state"] == "wait"
    assert run_status_line({"type": "message.complete", "payload": {}}) is None
    assert run_status_line({"type": "turn.start", "subagent_id": "s", "payload": {}}) is None
    conv = _run_converter(TPL)
    out = conv({"type": "usage.tick", "seq": 5, "payload": {"total_tokens": 9000, "output_tokens": 3400, "cost_usd": 0.01}})
    assert out == [{"t": "stats", "tokens": "9.0k", "cost": "$0.0100", "out": "3.4k"}] or (out[0]["t"] == "stats" and out[0]["out"] == "3.4k")
    msgs = [m for e in [ev(1, "turn.start", {}), ev(2, "tool.start", {"tool_id": "q", "name": "Read", "input": {"file_path": "/a/<b>.py"}}),
                        ev(3, "reasoning.progress", {"estimated_tokens": 50})] for m in conv(e)]
    st = [m for m in msgs if m["t"] == "status"]
    assert [m.get("label") or m.get("think_tokens") for m in st] == ["Đang nghĩ…", "Đọc <b>.py…", "~50"]
    assert all("html" not in m for m in st)  # client gán textContent


def test_thinking_summary_shows_estimated_tokens():
    evs = [ev(1, "reasoning.progress", {"estimated_tokens": 1200}), ev(2, "reasoning.progress", {"estimated_tokens": 1234}),
           ev(3, "reasoning.complete", {"text": "nghĩ", "truncated": False, "redacted": False}),
           ev(4, "reasoning.complete", {"text": "nghĩ tiếp", "truncated": False, "redacted": False})]
    html = replay_html(evs)
    assert html.count("Đã suy nghĩ · ~1.2k token") == 1 and "<summary>Suy nghĩ<time" in html  # ước lượng chỉ gắn khối kế tiếp
    live, _ = live_html(evs)
    assert live == norm(html)


async def test_run_page_has_status_line_only_while_live(hub_home):
    rt = FakeRuntime(ISOLATED, line_delay_s=0.01, hold=True)
    e = await _start(hub_home, rt)
    try:
        await e.login()
        e.hub.agents.create({"id": "ag0", "name": "ag0", "model": "haiku"})
        run = e.hub.runs.submit("ag0", "việc dài")
        assert await until(lambda: (e.hub.store.get_run(run["id"]) or {}).get("status") == "running")
        page = (await e.client.get(f"/runs/{run['id']}")).text
        assert 'id="tx-status"' in page and 'class="tx-status-t">Đang nghĩ…' in page and 'data-start="20' in page
    finally:
        rt.release()
        await _stop(e)


# =========================================================================== run dài
def _long_events(n_iter: int) -> list[dict]:
    evs, seq = [ev(1, "run.queued", {"prompt_preview": "dài", "kind": "adhoc"})], 2
    for i in range(n_iter):
        evs.append(ev(seq, "message.complete", {"text": f"bước {i}"}))
        evs.append(ev(seq + 1, "tool.start", {"tool_id": f"a{i}", "name": "Read", "input": {"file_path": f"/w/{i}.py"}}))
        evs.append(done(seq + 2, f"a{i}", "Read"))
        evs.append(ev(seq + 3, "tool.start", {"tool_id": f"b{i}", "name": "Bash", "input": {"command": f"echo {i}"}}))
        evs.append(done(seq + 4, f"b{i}", "Bash"))
        seq += 5
    return evs


def test_page_blocks_keeps_groups_whole_and_pages_backwards():
    blocks = replay_tx(_long_events(200)).blocks()  # 1 + 400 khối
    shown, before, hidden = P.page_blocks(blocks)
    assert len(shown) == P.LONG_PAGE and hidden == len(blocks) - P.LONG_PAGE and before == P.block_seq(shown[0])
    assert all(len(b["tools"]) == 2 for b in shown if b["kind"] == "group")  # nhóm không bị cắt đôi
    prev, before2, hidden2 = P.page_blocks(blocks, before)
    assert prev == blocks[-2 * P.LONG_PAGE:-P.LONG_PAGE] and hidden2 == len(blocks) - 2 * P.LONG_PAGE
    rest, before3, hidden3 = P.page_blocks(blocks, before2)
    assert rest == blocks[:-2 * P.LONG_PAGE] and before3 is None and hidden3 == 0
    assert P.page_blocks(blocks, 999_999) == ([], None, 0)  # mốc lạ → rỗng
    small = replay_tx(_long_events(10)).blocks()
    assert P.page_blocks(small) == (small, None, 0)


async def test_long_run_page_and_items_partial_need_auth_and_keep_groups(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    try:
        c = e.client
        assert (await c.get("/partials/run/x/items?before=1")).status_code == 401  # cần đăng nhập
        await e.login()
        e.hub.agents.create({"id": "ag0", "name": "ag0", "model": "haiku"})
        run = e.hub.runs.submit("ag0", "làm việc")
        assert (await e.hub.runs.wait(run["id"], 20))["status"] == "completed"
        for x in _long_events(170)[1:]:  # ~340 khối thêm vào run đã xong
            e.hub.publish(make_event(x["type"], "ag0", x["payload"], run_id=run["id"]))
        page = (await c.get(f"/runs/{run['id']}")).text
        m = re.search(r'hx-get="/partials/run/' + re.escape(run["id"]) + r'/items\?before=(\d+)"', page)
        assert m and "Hiện các mục trước" in page
        assert "bước 169" in page and "bước 0<" not in page
        r = await c.get(f"/partials/run/{run['id']}/items?before={m.group(1)}")
        assert r.status_code == 200 and "Hiện các mục trước" in r.text and "bước 0<" not in r.text  # còn nữa → nút ở đầu
        m2 = re.search(r'/items\?before=(\d+)"', r.text)
        r2 = await c.get(f"/partials/run/{run['id']}/items?before={m2.group(1)}")
        assert r2.status_code == 200 and "bước 0<" in r2.text and "Hiện các mục trước" not in r2.text  # trang đầu tiên
        groups = re.findall(r'<details class="tx-group[^"]*" id="(grp-[^"]+)"', page + r.text)
        assert len(groups) == len(set(groups))  # không nhóm nào bị vẽ hai lần (trang + partial)
        grp_nodes = [n for n in parse(r.text).walk() if "tx-group" in (n.get("class") or "").split()]
        assert grp_nodes and all(sum(1 for k in g.walk() if "tx-tool" in (k.get("class") or "").split()) == 2 for g in grp_nodes)  # nhóm nguyên vẹn
        assert (await c.get(f"/partials/run/{run['id']}/items?before=abc")).status_code == 400
        _safe(r.text, icons=True)
    finally:
        await _stop(e)


# =========================================================================== Điều phối: vạch ngày, tải lại khi còn chạy
def test_date_dividers_before_first_item_of_each_day():
    items = [{"kind": "user", "ts": "2026-11-10T03:00:00Z"}, {"kind": "manager", "ts": "2026-11-10T03:01:00Z"},
             {"kind": "user", "ts": "2026-11-12T03:00:00Z"}, {"kind": "user", "ts": None}, {"kind": "manager", "ts": "2026-11-13T03:00:00Z"}]
    out = with_days(items, today=date(2026, 11, 13))
    assert [x["kind"] for x in out] == ["day", "user", "manager", "day", "user", "user", "day", "manager"]
    days = [x["text"] for x in out if x["kind"] == "day"]
    assert days[1:] == ["Hôm qua", "Hôm nay"] and days[0].endswith("10/11/2026") and days[0].startswith("Thứ ")
    mod = TPL.env.get_template("_manager.html").make_module({"csrf": "", "path": "/manager"})
    html = str(mod.item(out[0]))
    assert 'class="mm-day" role="separator"' in html and "10/11/2026" in html


def test_pending_info_last_tool_and_todo():
    evs = [ev(1, "tool.start", {"tool_id": "a", "name": "Read", "input": {"file_path": "/w/a.py"}}), done(2, "a", "Read"),
           todo_start(3, "td", T1),
           ev(4, "tool.start", {"tool_id": "b", "name": "Bash", "input": {"command": "pytest"}}),
           dict(ev(5, "tool.start", {"tool_id": "s", "name": "Grep", "input": {"pattern": "x"}}), subagent_id="b")]
    info = pending_info(evs)
    assert info["tool"] == "Đang dùng Chạy pytest…" and info["todo"]["dom"] == "todo-r1"
    assert pending_info(evs[:2])["tool"] == "Vừa dùng Đọc a.py" and pending_info([]) == {"tool": "", "todo": None}


@pytest.fixture
async def mheld(hub_home):
    from cfharness.web import manager_stub

    rt = FakeRuntime(ISOLATED, line_delay_s=0.01, hold=True)
    e = await _start(hub_home, rt)
    e.stub = manager_stub.install(e.d)
    e.hub.agents.create({"id": "boss", "name": "Điều phối", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    yield e
    rt.release()
    await _stop(e)


async def test_reload_while_coordinator_live_shows_last_tool_and_todo(mheld):
    from test_web_manager import designate, send

    e = mheld
    await e.login()
    await designate(e)
    j = await send(e, "làm việc dài")
    conv, run = j["conversation_id"], j["run_id"]
    assert await until(lambda: (e.hub.store.get_run(run) or {}).get("status") == "running")
    e.hub.publish(make_event("tool.start", "boss", {"tool_id": "x1", "name": "Read", "input": {"file_path": "/w/<b>a.py"}}, run_id=run))
    e.hub.publish(make_event("tool.start", "boss", {"tool_id": "x2", "name": "TodoWrite", "input": {"todos": T1}}, run_id=run))
    page = (await e.client.get(f"/manager?c={conv}")).text
    pend = page[page.index(f'data-pending="{run}"'):]
    pend = pend[:pend.index('<div class="composer-wrap">') if '<div class="composer-wrap">' in pend else None]
    assert '<div class="mm-tool"><span class="tx-spin"' in pend and "Đang dùng Đọc &lt;b&gt;a.py" in pend
    assert 'class="ti tx-todo" id="todo-' in pend and "1/3 xong" in pend
    assert 'class="mm-day"' in page  # vạch "Hôm nay"


# =========================================================================== diff trang run: không dòng trống
def test_run_diff_udiff_has_no_newline_between_rows():
    src = (P.__file__.rsplit("present.py", 1)[0] + "templates/_run_diff.html")
    s = open(src, encoding="utf-8").read()
    assert "</span>\n{% endfor %}" not in s
    mod = TPL.env.get_template("_run_diff.html")
    dv = {"total_files": 1, "stat": {}, "root": "/w", "kind": "git", "head_before": "a", "files": [
        {"path": "a.py", "status": "modified", "status_view": ("Sửa", "info"), "has_patch": True, "binary": False, "adds": 1, "dels": 1,
         "lines": [("hunk", "@@ -1 +1 @@"), ("del", "-a"), ("add", "+b")], "more": 0}]}
    html = mod.render({"dv": dv, "run": {"id": "r1"}, "csrf": ""})
    pre = re.search(r'<pre class="udiff"[^>]*>(.*?)</pre>', html, re.S).group(1)
    assert "\n" not in pre and pre.count('<span class="dl') == 3
