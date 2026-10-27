"""UX v2 web — câu hỏi có lựa chọn (trang Cần bạn, Tổng quan, run, task, Điều phối) + vệ sinh template + định dạng thuần.

Daemon IN-PROCESS + FakeRuntime (hạ tầng của tests/test_web.py). Không gọi `claude`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cfharness.web import present as P
from cfharness.web.app import replay_items
from cfharness.web.manager import _timeline, question_from_event

from test_web import ISOLATED, _start, _stop, audit_rows, until  # noqa: F401
from cfharness.runtime import FakeRuntime

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "cfharness" / "web" / "templates"
XSS = '<script>alert("x")</script><img src=x onerror=alert(1)> <a href="javascript:alert(2)">bấm</a>'


@pytest.fixture
async def qenv(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    e.hub.agents.create({"id": "ag0", "name": "Agent 0", "model": "haiku"})
    run = e.hub.runs.submit("ag0", "việc thử")
    assert await until(lambda: (e.hub.store.get_run(run["id"]) or {}).get("status") == "completed")
    e.run = e.hub.store.get_run(run["id"])
    yield e
    await _stop(e)


def _ask(e, text="Xoá kiểu nào?", options=None, **kw):
    return e.hub.questions.open(e.run, text, options if options is not None else ["Chỉ file cũ", {"label": "Cả thư mục", "description": "3 GB"}], **kw)


# =========================================================================== vệ sinh template (CSP)
def test_templates_have_no_inline_style_script_or_handlers():
    bad = []
    for f in sorted(TEMPLATES.glob("*.html")):
        s = f.read_text(encoding="utf-8")
        for pat, why in ((r"\sstyle\s*=", "style="), (r"<script(?![^>]*\ssrc=)[^>]*>", "<script> inline"), (r"\son[a-z]+\s*=", "on*= handler"),
                         (r"javascript:", "javascript: URL"), (r"https?://(?!127\.0\.0\.1|cfharness\.localhost)[a-z]", "URL ngoài (CDN)")):
            for m in re.finditer(pat, s, re.I):
                bad.append(f"{f.name}: {why}: {s[max(0, m.start() - 30):m.end() + 30]!r}")
    assert not bad, "\n".join(bad)


def test_icon_sprite_has_every_icon_used():
    sprite = (TEMPLATES.parent / "static" / "icons.svg").read_text(encoding="utf-8")
    have = set(re.findall(r'<symbol id="i-([a-z0-9-]+)"', sprite))
    used: set[str] = set()
    for f in TEMPLATES.glob("*.html"):
        s = f.read_text(encoding="utf-8")
        used |= set(re.findall(r"icon\('([a-z0-9-]+)'", s)) | set(re.findall(r'ic="([a-z0-9-]+)"', s))
    used |= {v[0] for v in P.AGENT_STATUS.values()}
    assert used - have == set(), used - have


# =========================================================================== trang Cần bạn + trả lời
async def test_inbox_renders_question_escaped_and_answer_is_audited_web(qenv):
    e = qenv
    await e.login()
    q = _ask(e, text="Chọn đi " + XSS, options=[XSS, "An toàn"])
    r = await e.client.get("/inbox")
    assert r.status_code == 200
    html = r.text
    assert f'id="q-{q["id"]}"' in html and "Câu hỏi chờ bạn trả lời" in html
    assert "<script>alert" not in html and "<img src=x" not in html and 'href="javascript' not in html
    assert "&lt;script&gt;" in html  # chữ agent hiện ra như chữ
    assert 'type="radio" name="keys" value="1"' in html and 'name="text"' in html and e.key not in html
    # badge menu = approval + câu hỏi chờ người
    assert re.search(r'href="/inbox"[^>]*>.*?<span class="badge[^"]*">1</span>', html, re.S)
    # không có khoá phiên ngoài cookie → 403, chưa trả lời
    r = await e.client.post("/do/question_answer", data={"csrf": e.csrf, "id": q["id"], "keys": "2"})
    assert r.status_code == 403 and e.hub.questions.get(q["id"])["status"] == "open"
    # thiếu cả lựa chọn lẫn chữ → báo lỗi, chưa trả lời
    r = await e.post("/do/question_answer", {"id": q["id"], "next": "/inbox"})
    assert r.status_code == 303 and e.hub.questions.get(q["id"])["status"] == "open"
    r = await e.post("/do/question_answer", {"id": q["id"], "keys": "2", "text": "ghi chú <b>x</b>", "next": "/inbox"})
    assert r.status_code == 303 and r.headers["location"] == "/inbox"
    row = e.hub.questions.get(q["id"])
    assert row["status"] == "answered" and row["answered_by"] == "user" and row["answered_channel"] == "web"
    assert row["answer"]["keys"] == ["2"] and row["answer"]["text"] == "ghi chú <b>x</b>"
    aud = audit_rows(e, "question.answer")
    assert aud and aud[-1]["channel"] == "web" and aud[-1]["actor"] == "user"
    html = (await e.client.get("/inbox")).text
    assert "Không có gì cần bạn lúc này" in html and "ghi chú &lt;b&gt;x&lt;/b&gt;" in html  # mục "đã trả lời gần đây"


async def test_question_cancel_needs_confirmation(qenv):
    e = qenv
    await e.login()
    q = _ask(e)
    r = await e.post("/do/question_cancel", {"id": q["id"], "next": "/inbox"})
    assert r.status_code == 200 and "Huỷ câu hỏi này?" in r.text and e.hub.questions.get(q["id"])["status"] == "open"
    r = await e.post("/do/question_cancel", {"id": q["id"], "confirm": "yes", "next": "/inbox"})
    assert r.status_code == 303 and e.hub.questions.get(q["id"])["status"] == "cancelled"


async def test_with_manager_question_take_moves_it_to_human(qenv):
    e = qenv
    await e.login()
    e.hub.agents.create({"id": "boss", "name": "Boss", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    resp = await e.d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "manager.set", "params": {"agent_id": "boss"}})
    assert "error" not in resp, resp
    resp = await e.d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "agent.pause", "params": {"id": "ag0"}})  # task chờ, không chạy xong ngay
    assert "error" not in resp, resp
    t = e.hub.tasks.create({"assignee": "ag0", "title": "Dọn link", "body": "x"})
    q = _ask(e, task_id=t["id"])
    assert q["status"] == "with_manager"
    html = (await e.client.get("/inbox")).text
    assert "Đang chờ điều phối viên" in html and 'action="/do/question_take"' in html
    assert 'name="keys"' not in html.split(f'id="q-{q["id"]}"', 1)[1].split("</article>", 1)[0]  # chưa có form trả lời khi đang ở manager
    html = (await e.client.get(f"/tasks/{t['id']}")).text
    assert f'id="q-{q["id"]}"' in html
    r = await e.post("/do/question_take", {"id": q["id"], "next": f"/inbox#q-{q['id']}"})
    assert r.status_code == 303 and r.headers["location"] == f"/inbox#q-{q['id']}"
    row = e.hub.questions.get(q["id"])
    assert row["status"] == "open" and row["routed_to"] == "human"
    html = (await e.client.get("/inbox")).text
    assert 'type="radio" name="keys"' in html


async def test_overview_strip_one_click_answers_and_agent_marked_asking(qenv):
    e = qenv
    await e.login()
    q = _ask(e)
    for path in ("/", "/partials/overview"):
        html = (await e.client.get(path)).text
        card = html.split(f'id="q-{q["id"]}"', 1)[1].split("</article>", 1)[0]
        assert 'action="/do/question_answer"' in card and '<input type="hidden" name="keys" value="1">' in card
        assert "<textarea" not in card  # khung tự làm mới mỗi vài giây: không có ô nhập (không mất chữ đang gõ)
        assert "Chờ bạn trả lời" in html  # agent có câu hỏi chờ → nhãn "Chờ bạn trả lời" thay vì "Rảnh"
    r = await e.post("/do/question_answer", {"id": q["id"], "keys": "1", "next": "/"})
    assert r.status_code == 303 and e.hub.questions.get(q["id"])["status"] == "answered"


async def test_run_page_shows_question_once_and_runs_list_uses_titles(qenv):
    e = qenv
    await e.login()
    q = _ask(e)
    html = (await e.client.get(f"/runs/{e.run['id']}")).text
    assert html.count(f'id="q-{q["id"]}"') == 1  # khung trả lời + mục diễn biến không lặp id
    t = e.hub.tasks.create({"assignee": "ag0", "title": "Tiêu đề task dễ đọc", "body": "x"})
    assert await until(lambda: any(r.get("task_id") == t["id"] for r in e.hub.store.list_runs()))
    html = (await e.client.get("/runs")).text
    assert "Tiêu đề task dễ đọc" in html and "You have been assigned" not in html


async def test_new_layout_sidebar_groups_and_inbox_nav(qenv):
    e = qenv
    await e.login()
    html = (await e.client.get("/approvals")).text
    nav = re.search(r'<nav class="nav".*?</nav>', html, re.S).group(0)
    for g in ("Làm việc", "Đội ngũ", "Học &amp; chất lượng", "Hệ thống"):
        assert g in nav
    assert re.search(r'href="/inbox" class="on"', nav)  # trang Duyệt thuộc mục "Cần bạn"
    assert 'class="tabbar"' in html and re.search(r"/static/icons\.svg\?v=[0-9a-f]+#i-", html)
    r = await e.client.get("/static/icons.svg")
    assert r.status_code == 200 and "image/svg+xml" in r.headers["content-type"]


# =========================================================================== định dạng thuần
def test_replay_merges_approval_open_waiting_and_resolved_into_one_item():
    evs = [
        {"type": "approval.open", "seq": 1, "payload": {"request_id": "ap_1", "tool": "Bash", "preview": "rm -rf x"}},
        {"type": "approval.waiting_human", "seq": 2, "payload": {"request_id": "ap_1", "tool": "Bash", "preview": "rm -rf x"}},
    ]
    items = replay_items(evs)
    assert len(items) == 1 and items[0]["human"] is True and items[0]["dom"] == "ev-ap-ap_1"
    items = replay_items(evs + [{"type": "approval.resolved", "seq": 3, "payload": {"request_id": "ap_1", "decision": "allowed", "by": "user", "channel": "web"}}])
    assert len(items) == 1 and items[0]["resolved"] and "Đã cho phép" in items[0]["label"]
    qs = replay_items([{"type": "question.open", "seq": 4, "payload": {"question_id": "q_1", "text": "Hỏi?", "routed_to": "human"}},
                       {"type": "question.answered", "seq": 5, "payload": {"question_id": "q_1", "keys": ["2"], "labels": ["B"], "by": "user", "channel": "web"}}])
    assert len(qs) == 1 and qs[0]["answered"] and qs[0]["text"] == "2. B"


def test_run_title_question_view_and_activity():
    assert P.run_title({"prompt": "[cfharness task t_1] You have been assigned a task"}) == "You have been assigned a task"
    assert P.run_title({"task_id": "t_1", "prompt": "[cfharness task t_1] x"}, {"t_1": "Viết docs"}) == "Viết docs"
    assert P.run_title({"kind": "manager", "prompt": "[cfharness heartbeat · key x]\nEvents"}) == "Điều phối viên · nhịp kiểm tra"
    v = P.question_view({"status": "answered", "answer": {"keys": ["1"], "labels": ["A"], "text": "ok"}, "answered_by": "manager:boss", "options": []})
    assert v["pick"] == ["1. A"] and v["answered_by"] == "Điều phối viên" and not v["pending"]
    assert P.question_view({"status": "open", "options": [{"key": "1"}], "multi": False})["single_quick"]
    assert P.question_view({"status": "open", "options": [{"key": "1"}], "multi": True})["single_quick"] is False
    assert P.activity_item({"type": "run.started", "agent_id": "a", "payload": {}}) is None
    assert P.activity_item({"type": "task.status", "agent_id": "a", "payload": {"task_id": "t1", "to": "triage"}}) is None
    it = P.activity_item({"type": "task.status", "agent_id": "a", "payload": {"task_id": "t1", "to": "blocked", "reason": "thiếu quyền"}})
    assert it and "bị chặn" in it["text"] and it["tone"] == "danger" and it["task_id"] == "t1"


def test_manager_timeline_groups_consecutive_actions_and_places_questions():
    msgs = [{"kind": "user", "text": "u", "ts": "2026-10-19T01:00:00Z", "run_id": "r1"},
            {"kind": "manager", "text": "m", "ts": "2026-10-19T01:00:30Z", "run_id": "r1"}]
    acts = [{"kind": "action", "summary": f"a{i}", "ts": f"2026-10-19T01:00:1{i}Z", "run_id": "r1", "ok": i != 1, "target": {}} for i in range(3)]
    acts.append({"kind": "action", "summary": "khác", "ts": "2026-10-19T01:00:12Z", "run_id": "r9", "ok": True, "target": {}})  # run khác hội thoại → bỏ
    qs = [{"id": "q1", "created_at": "2026-10-19T01:00:40Z", "status": "open", "run_id": "r1"}]
    tl = _timeline(msgs, acts, qs)
    assert [x["kind"] for x in tl] == ["user", "actions", "manager", "question"]
    assert len(tl[1]["items"]) == 3 and tl[1]["ok"] is False
    q = question_from_event({"type": "question.answered", "agent_id": "boss", "ts": "t",
                             "payload": {"question_id": "q1", "keys": ["2"], "labels": ["B"], "text": "note", "by": "user", "conversation_id": "mc1"}})
    assert q["status"] == "answered" and q["answer"] == {"keys": ["2"], "labels": ["B"], "text": "note"} and q["conversation_id"] == "mc1"
