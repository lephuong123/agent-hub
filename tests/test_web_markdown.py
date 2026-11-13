"""Bộ render transcript dùng chung (docs/dev-notes/plan-chat-ui-claude.md P0-2/P0-3/P0-7/P0-8).

- `present.md()` đã cứng hoá: XSS qua link/ảnh/fence/bảng, không `<img`, không `style=`, tô màu bằng class, cắt ngưỡng.
- `replay_items` (phát lại từ DB) và `_run_converter` (SSE live) dùng chung `present.Transcript` → cùng HTML cuối.
- `present.tool_view`: bảng động từ tiếng Việt; chữ agent (lệnh, đường dẫn) chỉ là dữ liệu, ký tự bidi bị lộ.
- Partial `/partials/run/<id>/result` thay `location.reload()`; thẻ Kết quả không lặp tin cuối.
Không gọi `claude` thật: web chạy trên daemon in-process + FakeRuntime.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from cfharness.runtime import FakeRuntime
from cfharness.web import present as P
from cfharness.web.app import _run_converter, make_templates, replay_items
from test_web import ISOLATED, _start, _stop, running_run, until  # noqa: F401 — khung test web

BAD_TAGS = {"script", "img", "iframe", "style", "object", "embed", "link", "meta", "form", "base", "svg", "math"}


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag


def _safe(html: str, icons: bool = False) -> None:
    """Kiểm trên CÂY THẺ thật (chữ đã escape như `&lt;script&gt;` là dữ liệu, không tính): không thẻ nguy hiểm, không `style=`/`on*=`,
    href/src chỉ http/https/mailto hoặc đường dẫn nội bộ. `icons=True`: cho phép <svg><use> của sprite icon nội bộ."""
    p = _Tags()
    p.feed(html)
    for tag, attrs in p.tags:
        if icons and tag in ("svg", "use"):
            assert all(not k.startswith("on") and k != "style" for k in attrs) and (attrs.get("href") or "/static/").startswith("/static/")
            continue
        assert tag not in BAD_TAGS, (tag, attrs)
        for k, v in attrs.items():
            assert k != "style" and not k.startswith("on"), (tag, k, v)
            if k in ("href", "src", "action", "formaction", "xlink:href"):
                assert re.match(r"^(https?://|mailto:|/(?!/))", v or ""), (tag, k, v)
    assert not re.search(r"<[a-z][^>]*\sstyle\s*=", html, re.I)  # chuỗi thô: không thẻ nào mang style=


# =========================================================================== md(): XSS
@pytest.mark.parametrize("src", [
    '```"><script>alert(1)</script>\nx\n```',
    '```"><svg onload=alert(1)>\nx\n```',
    "[a](&#106;avascript:alert(1)) [b](&#x6A;avascript&colon;alert(1)) [c](JaVaScRiPt:alert(1))",
    "<javascript:alert(1)> <data:text/html,<script>alert(1)</script>>",
    "[r][1] [s][2]\n\n[1]: javascript:alert(1)\n[2]: data:text/html;base64,PHNjcmlwdD4=",
    "[d](data:text/html,<script>alert(1)</script>) ![e](data:image/png;base64,AAAA)",
    "| a | b |\n|:-:|--:|\n| <script>alert(1)</script> | <img src=x onerror=alert(1)> |",
    "```py\nprint('</code></pre><script>alert(1)</script>')\n```",
    "<div style=\"color:red\" onclick=\"x()\">raw</div>\n\n<span style=x>inline</span>",
    "![x](https://evil.example/track.png \"t\") ![y](javascript:alert(1))",
])
def test_md_xss_cases_are_neutralised(src):
    _safe(P.md(src))


def test_md_fence_language_is_lexer_name_never_raw_info():
    html = P.md('```"><script>alert(1)</script>\nx\n```')
    assert '<span class="tx-lang">text</span>' in html
    html = P.md('```"><svg onload=alert(1)>\nx\n```')
    assert '<span class="tx-lang">text</span>' in html and "onload" not in html
    html = P.md("```py\nprint('</code></pre><script>')\n```")
    assert '<span class="tx-lang">python</span>' in html and '<span class="tk-' in html
    assert "&lt;/code&gt;&lt;/pre&gt;&lt;script&gt;" in html and html.count("</code></pre>") == 1
    assert '<button type="button" class="tx-copy">Sao chép</button>' in html


def test_md_links_images_tables_tasks():
    html = P.md("[Bấm đây](https://evil.example/x?a=1&b=2)")
    assert 'rel="nofollow noopener noreferrer"' in html and 'target="_blank"' in html and 'class="ext"' in html
    assert 'title="https://evil.example/x?a=1&amp;b=2"' in html and '<span class="ext-host"> (evil.example)</span>' in html
    # tên miền IDN giả (а Cyrillic) hiện punycode — người thấy ngay đó không phải apple.com
    assert "xn--" in P.md("[apple](https://аpple.com/login)")
    # autolink: chữ hiển thị đã là URL → không lặp tên miền
    assert "ext-host" not in P.md("<https://a.example/b>")
    img = P.md("![sơ đồ](https://x.example/a.png)")
    assert "<img" not in img and ">ảnh: sơ đồ</a>" in img and 'class="ext img-link"' in img
    assert "<img" not in P.md("![a](data:image/png;base64,AAAA)")
    tbl = P.md("| a | b |\n|:-|-:|\n| <b>x</b> | 2 |")
    assert '<div class="tx-table"><table>' in tbl and "<th class=\"al-left\">" in tbl and "&lt;b&gt;x&lt;/b&gt;" in tbl
    assert "<del>" in P.md("~~bỏ~~") or "<s>bỏ</s>" in P.md("~~bỏ~~")
    tasks = P.md("- [x] xong\n- [ ] chưa")
    assert tasks.count('disabled="disabled"') == 2 and 'type="checkbox"' in tasks
    _safe(tbl + tasks + img)


def test_md_long_code_skips_highlight_and_long_input_is_capped():
    code = "x = 1\n" * (P.HL_MAX // 6 + 50)
    html = P.md(f"```python\n{code}```")
    assert '<span class="tx-lang">python</span>' in html and '<span class="tk-' not in html
    big = "a" * (P.MD_MAX + 500)
    out = P.md(big)
    assert "đã cắt" in out and out.count("a") <= P.MD_MAX + 50
    assert P.md("**x**") is P.md("**x**")  # LRU: cùng nội dung → cùng chuỗi đã render


def test_md_never_emits_style_attribute_for_many_languages():
    for lang in ("python", "js", "bash", "json", "html", "sql", "diff", "yaml", "go", "rust", "unknown-lang"):
        html = P.md(f"```{lang}\n<a href='x' style='y'>{{\"k\": [1, 2]}}</a> -- c\n+ add\n```")
        _safe(html)


# =========================================================================== tool_view
TOOL_CASES = [
    ("Read", {"file_path": "/w/src/a.py"}, "Đọc", "a.py", ""),
    ("Bash", {"command": "npm test -- --watch", "description": "Chạy test"}, "Chạy", "Chạy test", ""),
    ("Bash", {"command": "ls -la\npwd"}, "Chạy", "ls -la", ""),
    ("Edit", {"file_path": "C:\\w\\x.py", "old_string": "a", "new_string": "b\nc\nd"}, "Sửa", "x.py", "+3 −1"),
    ("MultiEdit", {"file_path": "/w/y.py", "edits": [{"old_string": "a", "new_string": "b"}, {"old_string": "c\nd", "new_string": ""}]}, "Sửa", "y.py", "+1 −3"),
    ("Write", {"file_path": "/w/n.md", "content": "1\n2\n3\n4"}, "Ghi", "n.md", "4 dòng"),
    ("Grep", {"pattern": "TODO", "path": "/w/src"}, "Tìm", '"TODO"', "trong src"),
    ("Glob", {"pattern": "**/*.py"}, "Liệt kê file", '"**/*.py"', ""),
    ("WebFetch", {"url": "https://docs.example.com/a/b", "prompt": "x"}, "Tải", "docs.example.com", ""),
    ("WebSearch", {"query": "markdown it"}, "Tìm web", '"markdown it"', ""),
    ("mcp__github__create_issue", {"title": "x"}, "create_issue · github", "title=x", ""),
]


@pytest.mark.parametrize("name,inp,verb,target,meta", TOOL_CASES)
def test_tool_view_verbs(name, inp, verb, target, meta):
    v = P.tool_view(name, inp, "ok", False, 0.5, summary=target if name.startswith("mcp__") else "")
    assert (v["verb"], v["target"], v["meta"]) == (verb, target, meta)


def test_tool_view_input_output_and_truncation():
    v = P.tool_view("Bash", {"command": "echo 'a'\nrm -rf x", "description": "d", "timeout": 5}, "x" * 2000, True, 1.0)
    assert v["input_text"].startswith("echo 'a'\nrm -rf x") and '"timeout": 5' in v["input_text"]
    assert v["truncated"] and len(v["output_text"]) == 2000 and v["error"]
    assert all(k != "hit" for k, _ in v["input_segs"])  # dòng tool không tô "nguy hiểm" như hộp duyệt
    v = P.tool_view("Read", {"file_path": "a"}, "ngắn", False, None)
    assert not v["truncated"] and '"file_path": "a"' in v["input_text"]
    big = {"content": "y" * (P.TOOL_IN_MAX + 10)}
    v = P.tool_view("Write", big, "", False, None)
    assert v["input_cut"] and len(v["input_text"]) == P.TOOL_IN_MAX


def _tool_html(name: str, inp: dict, result: str = "ok", err: bool = False) -> str:
    start = {"type": "tool.start", "seq": 1, "run_id": "r1", "payload": {"tool_id": "t1", "name": name, "input": inp, "args_preview": "prev"}}
    done = {"type": "tool.complete", "seq": 2, "run_id": "r1", "payload": {"tool_id": "t1", "name": name, "duration_s": 0.2, "is_error": err,
                                                                          "result_preview": result}}
    items = replay_items([start, done])
    return str(make_templates().env.get_template("_items.html").module.item(items[0]))


def test_tool_row_escapes_agent_data_and_reveals_bidi():
    html = _tool_html("Bash", {"command": '"><script>alert(1)</script>', "description": "<img src=x onerror=alert(1)>"}, result="<svg onload=x>")
    _safe(html, icons=True)
    assert "&lt;script&gt;" in html and "&lt;img src=x onerror=alert(1)&gt;" in html and "&lt;svg onload=x&gt;" in html
    assert not re.search(r'data-[a-z-]+="[^"]*(script|onerror)', html)  # input agent không vào data-*
    html = _tool_html("Read", {"file_path": "/w/\"><script>x</script>.py"})
    _safe(html, icons=True)
    html = _tool_html("Bash", {"command": "echo safe\u202etxt.exe"})
    assert '<mark class="ctl">⟦U+202E⟧</mark>' in html and "\u202e" not in html
    err = _tool_html("Bash", {"command": "false"}, result="boom", err=True)
    assert re.search(r'<details class="ti tool tx-tool err"[^>]* open>', err) and "boom" in err
    ok = _tool_html("Read", {"file_path": "/w/a.py"})
    assert " open>" not in ok.split("\n", 1)[0] and "Đọc" in ok and "a.py" in ok


# =========================================================================== replay == live
EVENTS = [
    {"type": "run.queued", "seq": 1, "run_id": "r1", "payload": {"prompt_preview": "làm <b>X</b>", "kind": "adhoc"}},
    {"type": "run.started", "seq": 2, "run_id": "r1", "payload": {"model": "haiku", "permission_mode": "default", "mcp_servers": [{"name": "gh"}]}},
    {"type": "reasoning.complete", "seq": 3, "run_id": "r1", "payload": {"text": "nghĩ <i>kỹ</i>"}},
    {"type": "message.complete", "seq": 4, "run_id": "r1", "payload": {"text": "Đọc file:\n```py\nx = '<a>'\n```"}},
    {"type": "tool.start", "seq": 5, "run_id": "r1", "payload": {"tool_id": "t1", "name": "Edit", "args_preview": "/w/a.py",
                                                                "input": {"file_path": "/w/a.py", "old_string": "a", "new_string": "b\nc"}}},
    {"type": "subagent.spawn", "seq": 6, "run_id": "r1", "payload": {"type": "general", "description": "phụ"}},
    {"type": "user.message", "seq": 7, "run_id": "r1", "payload": {"text": "chỉ src/", "by": "user", "channel": "web"}},
    {"type": "tool.complete", "seq": 8, "run_id": "r1", "payload": {"tool_id": "t1", "name": "Edit", "duration_s": 0.3, "result_preview": "đã sửa"}},
    {"type": "steer.ack", "seq": 9, "run_id": "r1", "payload": {}},
    {"type": "tool.start", "seq": 10, "run_id": "r1", "payload": {"tool_id": "t2", "name": "Bash", "args_preview": "false", "input": {"command": "false"}}},
    {"type": "tool.complete", "seq": 11, "run_id": "r1", "payload": {"tool_id": "t2", "name": "Bash", "duration_s": 0.1, "is_error": True, "result_preview": "exit 1"}},
    {"type": "approval.open", "seq": 12, "run_id": "r1", "payload": {"request_id": "ap1", "tool": "Bash", "preview": "rm x"}},
    {"type": "approval.resolved", "seq": 13, "run_id": "r1", "payload": {"request_id": "ap1", "decision": "allowed", "by": "user", "channel": "web"}},
    {"type": "message.complete", "seq": 14, "run_id": "r1", "payload": {"text": "Xong **rồi**"}},
    {"type": "run.completed", "seq": 15, "run_id": "r1", "source": "hub",
     "payload": {"outcome": "completed", "cost_usd": 0.01, "duration_ms": 2000, "num_turns": 2}},
]


def test_replay_and_live_render_the_same_final_html():
    t = make_templates()
    mod = t.env.get_template("_items.html").module
    replay_html = [str(mod.item(it)) for it in replay_items(EVENTS)]
    # mô phỏng client: thay theo `dom` nếu đã có, không thì nối cuối (txAppend trong app.js)
    conv = _run_converter(t)
    order: list[str] = []
    by_key: dict[str, str] = {}
    for i, ev in enumerate(EVENTS):
        for m in conv(ev):
            if m["t"] != "item":
                continue
            key = m.get("dom") or f"#{i}"
            if key not in by_key:
                order.append(key)
            by_key[key] = m["html"]
    live_html = [by_key[k] for k in order]
    assert live_html == replay_html
    joined = "".join(replay_html)
    _safe(joined, icons=True)
    # P0-3: live giữ input/diff của tool.start khi tool xong
    assert "+2 −1" in joined and "new_string" in joined and "/w/a.py" in joined
    # P0-8: start thành dòng meta dưới yêu cầu, ✓ trên chỉ đạo, không còn dòng subagent riêng
    items = replay_items(EVENTS)
    kinds = [i["kind"] for i in items]
    assert "start" not in kinds and "ack" not in kinds and not any(i["kind"] == "note" for i in items)
    assert items[0]["start"]["text"].startswith("Bắt đầu") and "tx-start" in replay_html[0]
    steer = next(i for i in items if i["kind"] == "steer")
    assert steer["acked"] and "agent đã nhận" in by_key[steer["dom"]]
    tool = next(i for i in items if i["kind"] == "tool" and i["tool"] == "Edit")
    assert tool["view"]["meta"] == "+2 −1" and tool["diff"] and tool["input"]["file_path"] == "/w/a.py" and not tool["running"]
    assert "&lt;b&gt;X&lt;/b&gt;" in joined and "<b>X</b>" not in joined and "&lt;i&gt;kỹ&lt;/i&gt;" in joined


def test_steer_ack_without_steer_and_start_without_prompt_fallback():
    items = replay_items([{"type": "run.started", "seq": 1, "payload": {"model": "m"}}, {"type": "steer.ack", "seq": 2, "payload": {}}])
    assert [i["kind"] for i in items] == ["start", "note"]


def test_mark_final_and_same_text():
    items = replay_items(EVENTS)
    assert P.mark_final(items) == "Xong **rồi**"
    finals = [i for i in items if i.get("final")]
    assert len(finals) == 1 and finals[0]["seq"] == 14
    html = str(make_templates().env.get_template("_items.html").module.item(finals[0]))
    assert 'class="ti msg tx-msg final"' in html
    assert P.same_text("Xong  **rồi**\n", "Xong **rồi**") and not P.same_text("", "") and not P.same_text("a", "b")


# =========================================================================== web thật: partial kết quả, thẻ Kết quả không lặp
@pytest.fixture
async def env(hub_home):
    e = await _start(hub_home, FakeRuntime(ISOLATED))
    yield e
    await _stop(e)


async def test_run_result_partial_and_no_duplicate_summary(env):
    c = env.client
    assert (await c.get("/partials/run/x/result")).status_code == 401  # cần đăng nhập
    await env.login()
    env.hub.agents.create({"id": "ag0", "name": "ag0", "model": "haiku"})
    run = env.hub.runs.submit("ag0", "làm việc")
    final = await env.hub.runs.wait(run["id"], 20)
    assert final["status"] == "completed"
    r = await c.get(f"/partials/run/{run['id']}/result")
    assert r.status_code == 200 and 'id="run-result"' in r.text and "Kết quả" in r.text and 'hx-get="/partials/run/' in r.text
    page = (await c.get(f"/runs/{run['id']}")).text
    assert 'id="run-result"' in page and 'id="steer-card"' not in page
    evs = env.hub.store.conn.execute("SELECT payload FROM events WHERE run_id=? AND type='message.complete' ORDER BY seq", (run["id"],)).fetchall()
    summary = (env.hub.store.get_run(run["id"]) or {}).get("summary") or ""
    if evs and summary and P.same_text(summary, __import__("json").loads(evs[-1][0]).get("text")):
        assert 'class="md result"' not in page and "tx-msg final" in page  # trùng tin cuối → không lặp
    # htmx poll thanh run sau khi run xong → 286 (htmx ngừng poll); gọi thường vẫn 200
    assert (await c.get(f"/partials/run/{run['id']}", headers={"HX-Request": "true"})).status_code == 286
    assert (await c.get(f"/partials/run/{run['id']}")).status_code == 200


async def test_run_result_partial_204_while_live(hub_home):
    rt = FakeRuntime(ISOLATED, line_delay_s=0.01, hold=True)
    e = await _start(hub_home, rt)
    try:
        await e.login()
        run = await running_run(e)
        r = await e.client.get(f"/partials/run/{run['id']}/result")
        assert r.status_code == 204
        page = (await e.client.get(f"/runs/{run['id']}")).text
        assert 'id="steer-card"' in page and f'data-result="/partials/run/{run["id"]}/result"' in page and 'id="steer-form"' in page
    finally:
        rt.release()
        await _stop(e)
