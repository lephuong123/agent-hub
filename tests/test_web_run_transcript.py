"""Transcript trang run kiểu Claude chat (docs/dev-notes/plan-chat-ui-claude.md P1-1, P1-2, P1-3, P1-6, P1-7, P1-11).

- P1-1: `_run_converter` gửi `{t:"part", html}` = `md()` của khối đang chạy chữ, có điều tiết (thời gian / số ký tự / trần 8000).
- P1-2: tool liền nhau gom nhóm (`present.group_items`, `Transcript.grp`); nhóm lỗi/đang chạy mở, nhóm xong ≥3 tool gập.
- P1-3: mục subagent (`subagent_id`) nằm TRONG tool Task/Agent cha (fixture `claude-subagent.jsonl`, qua adapter thật).
- P1-7: diff Edit/Write = difflib unified có ngữ cảnh, trần dòng, đếm +/−, lộ ký tự bidi.
- P1-11: duyệt/câu hỏi trong transcript chỉ là MỘT dòng trỏ tới thẻ (#ap-/#q-), không có nút thao tác.
- Phát lại == live: mô phỏng `txAppend` (app.js) trên cây DOM nhỏ (html.parser) → HTML cuối giống hệt bản phát lại.
Không gọi `claude` thật.
"""

from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser
from pathlib import Path

from cfharness.claude_adapter import adapt_lines
from cfharness.web import present as P
from cfharness.web.app import PART_MAX, _run_converter, make_templates, replay_tx
from test_web_markdown import _safe

FIX = Path(__file__).parent / "fixtures"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


# =========================================================================== cây DOM nhỏ để mô phỏng client
class Node:
    def __init__(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tag, self.attrs, self.kids, self.parent = tag, [list(a) for a in attrs], [], None

    def get(self, k: str) -> str | None:
        return next((v for a, v in self.attrs if a == k), None)

    def has(self, k: str) -> bool:
        return any(a == k for a, _ in self.attrs)

    def set(self, k: str, v: str | None) -> None:
        for a in self.attrs:
            if a[0] == k:
                a[1] = v
                return
        self.attrs.append([k, v])

    def drop(self, k: str) -> None:
        self.attrs = [a for a in self.attrs if a[0] != k]

    def append(self, n: "Node | str") -> None:
        if isinstance(n, Node):
            if n.parent is not None:
                n.parent.kids.remove(n)
            n.parent = self
        self.kids.append(n)

    def replace_with(self, n: "Node") -> None:
        p = self.parent
        p.kids[p.kids.index(self)] = n
        n.parent, self.parent = p, None

    def walk(self):
        yield self
        for k in self.kids:
            if isinstance(k, Node):
                yield from k.walk()

    def find(self, id_: str) -> "Node | None":
        return next((n for n in self.walk() if n.get("id") == id_), None) if id_ else None

    def html(self) -> str:
        if self.tag == "#root":
            return "".join(k if isinstance(k, str) else k.html() for k in self.kids)
        a = "".join(f" {k}" if v is None else f' {k}="{_html.escape(v, quote=True)}"' for k, v in sorted(self.attrs))
        if self.tag in VOID:
            return f"<{self.tag}{a}>"
        inner = "".join(_html.escape(k, quote=False) if isinstance(k, str) else k.html() for k in self.kids)
        return f"<{self.tag}{a}>{inner}</{self.tag}>"


class _Build(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", [])
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs)
        self.cur.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag, attrs):
        self.cur.append(Node(tag, attrs))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        self.cur.append(data)


def parse(html: str) -> Node:
    b = _Build()
    b.feed(html)
    b.close()
    return b.root


def first(html: str) -> Node:
    return next(k for k in parse(html.strip()).kids if isinstance(k, Node))


def norm(html: str) -> str:
    return parse(html).html()


class Client:
    """Mô phỏng `txAppend` của app.js (không người bấm: mọi <details> theo trạng thái server)."""

    def __init__(self) -> None:
        self.root = Node("#root", [])

    def apply(self, m: dict) -> None:
        if m["t"] != "item":
            return
        node = first(m["html"])
        for i, h in enumerate(m.get("heads") or []):
            sh = first(h["html"])
            el = self.root.find(h["dom"])
            if el is not None:
                was, auto = el.get("data-auto"), sh.get("data-auto")
                if was is not None and auto is not None and was != auto and el.has("open") == (was == "1"):
                    el.set("open", None) if auto == "1" else el.drop("open")
                el.set("data-auto", auto)
                el.set("class", sh.get("class"))
                old_sum = next(k for k in el.kids if isinstance(k, Node) and k.tag == "summary")
                old_sum.replace_with(next(k for k in sh.kids if isinstance(k, Node) and k.tag == "summary"))
            elif i == 0 and "tx-group" in (sh.get("class") or "").split():
                self.root.append(sh)
        old = self.root.find(m.get("dom"))
        if old is not None:
            if old.tag == "details" and node.tag == "details" and old.get("data-auto") is None and old.has("open"):
                node.set("open", None)
            old.replace_with(node)
            return
        slot = self.root.find(f"{m['parent']}-k") if m.get("parent") else None
        (slot or self.root).append(node)


def live_html(events: list[dict], conv=None) -> tuple[str, list[dict]]:
    conv = conv or _run_converter(make_templates())
    cl = Client()
    msgs = [m for ev in events for m in conv(ev)]
    for m in msgs:
        cl.apply(m)
    return cl.root.html(), msgs


def replay_html(events: list[dict]) -> str:
    mod = make_templates().env.get_template("_items.html").module
    tx = replay_tx(events)
    return "".join(str(mod.item(b)) for b in tx.blocks())


# =========================================================================== sự kiện mẫu
def tool(seq, tid, name, inp=None, *, done=None, err=False, sub=None, out="ok"):
    evs = [{"type": "tool.start", "seq": seq, "run_id": "r1", "subagent_id": sub, "ts": f"2026-11-13T10:00:{seq:02d}Z",
            "payload": {"tool_id": tid, "name": name, "input": inp or {}, "args_preview": ""}}]
    if done is not None:
        evs.append({"type": "tool.complete", "seq": done, "run_id": "r1", "subagent_id": sub, "ts": f"2026-11-13T10:00:{done:02d}Z",
                    "payload": {"tool_id": tid, "name": name, "duration_s": 0.2, "is_error": err, "result_preview": out}})
    return evs


def msg(seq, text, sub=None):
    return {"type": "message.complete", "seq": seq, "run_id": "r1", "subagent_id": sub, "payload": {"text": text}}


EVENTS = [
    {"type": "run.queued", "seq": 1, "run_id": "r1", "payload": {"prompt_preview": "sửa <b>bug</b>", "kind": "adhoc"}},
    {"type": "run.started", "seq": 2, "run_id": "r1", "payload": {"model": "haiku", "permission_mode": "default"}},
    *tool(3, "a1", "Read", {"file_path": "/w/a.py"}, done=4),
    *tool(5, "a2", "Bash", {"command": "pytest"}, done=6, err=True, out="1 failed"),
    msg(7, "Thấy lỗi, sửa **a.py**:"),
    *tool(8, "b1", "Edit", {"file_path": "/w/a.py", "old_string": "x = 1\ny = 2\nz = 3", "new_string": "x = 1\ny = 20\nz = 3"}, done=9),
    *tool(10, "t1", "Task", {"description": "Kiểm <i>tra</i>", "prompt": "chạy lại test", "subagent_type": "general"}),
    *tool(11, "s1", "Grep", {"pattern": "def "}, done=12, sub="t1"),
    msg(13, "chữ subagent — bỏ", sub="t1"),
    {"type": "error", "seq": 14, "run_id": "r1", "subagent_id": "t1", "payload": {"message": "subagent cảnh báo <x>"}},
    *tool(15, "s2", "Bash", {"command": "pytest -q"}, done=17, sub="t1"),
    *tool(16, "s3", "Read", {"file_path": "/w/b.py"}, sub="t1"),  # chưa xong khi Task xong (vẫn hiện đang chạy, lồng đúng chỗ)
    {"type": "tool.complete", "seq": 18, "run_id": "r1", "payload": {"tool_id": "t1", "name": "Task", "duration_s": 3.2, "result_preview": "đã kiểm"}},
    {"type": "approval.open", "seq": 19, "run_id": "r1", "payload": {"request_id": "ap1", "tool": "Bash", "preview": "rm -rf build\nnext line"}},
    {"type": "approval.waiting_human", "seq": 20, "run_id": "r1", "payload": {"request_id": "ap1", "tool": "Bash", "preview": "rm -rf build\nnext line"}},
    *tool(21, "c1", "Bash", {"command": "make"}),  # sau duyệt → nhóm mới, đang chạy
    {"type": "question.open", "seq": 22, "run_id": "r1", "payload": {"question_id": "q1", "text": "Chọn cách nào?\nchi tiết dài"}},
    *tool(23, "d1", "Read", {"file_path": "/w/c.py"}, done=24),
    *tool(25, "d2", "Read", {"file_path": "/w/d.py"}, done=26),
    *tool(27, "d3", "Glob", {"pattern": "*.md"}, done=28),
    {"type": "approval.resolved", "seq": 29, "run_id": "r1", "payload": {"request_id": "ap1", "decision": "allowed", "by": "user", "channel": "web",
                                                                         "tool": "Bash", "preview": "rm -rf build"}},
    msg(30, "Xong <script>x</script>"),
    {"type": "run.completed", "seq": 31, "run_id": "r1", "source": "hub", "payload": {"outcome": "completed", "cost_usd": 0.01, "duration_ms": 2000, "num_turns": 2}},
]
for _e in EVENTS:  # mọi event thật đều có ts (phát lại sắp theo ts, seq)
    _e.setdefault("ts", f"2026-11-13T10:00:{_e['seq']:02d}Z")


# =========================================================================== phát lại == live (cây DOM)
def test_replay_and_live_build_the_same_dom_with_groups_and_subagents():
    live, msgs = live_html(EVENTS)
    replay = replay_html(EVENTS)
    assert live == norm(replay)
    _safe(replay, icons=True)
    # mọi lần thay/chèn: message mang đủ ngữ cảnh — tool cấp ngoài → parent = nhóm; tool subagent → parent = tool Task
    items = [m for m in msgs if m["t"] == "item"]
    assert next(m for m in items if m["dom"] == "tool-a2")["parent"] == "grp-tool-a1"
    sub = next(m for m in items if m["dom"] == "tool-s1")
    assert sub["parent"] == "tool-t1" and [h["dom"] for h in sub["heads"]] == ["grp-tool-b1", "tool-t1"]
    assert all("tx-tool-body" not in h["html"] for m in items for h in m.get("heads") or [])  # tiêu đề tool cha: chỉ <summary>
    # thay theo dom vẫn chạy trong nhóm/tool cha: không phần tử nào trùng id
    ids = [n.get("id") for n in parse(replay).walk() if n.get("id")]
    assert len(ids) == len(set(ids))


def test_grouping_rules_tool_tool_msg_tool_and_open_states():
    tx = replay_tx(EVENTS)
    blocks = tx.blocks()
    groups = [b for b in blocks if b["kind"] == "group"]
    assert [g["dom"] for g in groups] == ["grp-tool-a1", "grp-tool-b1", "grp-tool-c1", "grp-tool-d1"]
    g1, g2, g3, g4 = groups
    assert (g1["n"], g1["error"], g1["open"], g1["verbs"]) == (2, True, True, "Đọc ×1, Chạy ×1")  # lỗi → mở
    assert g2["n"] == 2 and [t["dom"] for t in g2["tools"]] == ["tool-b1", "tool-t1"]  # tool subagent KHÔNG làm vỡ nhóm
    assert g3["solo"] and g3["running"] and g3["open"] and g3["latest"] == "Chạy make"  # đang chạy → mở, dòng đang chạy mới nhất
    assert (g4["n"], g4["running"], g4["error"], g4["open"], g4["verbs"]) == (3, False, False, False, "Đọc ×2, Liệt kê file ×1")  # xong ≥3 → gập
    mod = make_templates().env.get_template("_items.html").module
    h1, h3, h4 = (str(mod.item(g)) for g in (g1, g3, g4))
    assert re.search(r'<details class="tx-group err" id="grp-tool-a1" data-auto="1" open>', h1)
    assert "Đã dùng 2 công cụ" in h1 and "Đọc ×1, Chạy ×1" in h1
    assert 'class="tx-group solo running"' in h3 and " open>" in h3.split("\n", 1)[0]
    assert re.search(r'<details class="tx-group" id="grp-tool-d1" data-auto="0">', h4) and "Đã dùng 3 công cụ" in h4
    # tool–tool–msg–tool → 2 nhóm (hàm thuần dùng lại được cho trang Điều phối)
    flat = [{"kind": "tool", "dom": "x1"}, {"kind": "tool", "dom": "x2", "running": True}, {"kind": "msg"}, {"kind": "tool", "dom": "x3"}]
    out = P.group_items(flat)
    assert [b["kind"] for b in out] == ["group", "msg", "group"] and [len(b["tools"]) for b in out if b["kind"] == "group"] == [2, 1]
    assert out[0]["open"] and out[0]["running"] and out[2]["solo"]


def test_nested_subagent_items_inside_parent_tool():
    tx = replay_tx(EVENTS)
    task = next(t for g in tx.blocks() if g["kind"] == "group" for t in g["tools"] if t["dom"] == "tool-t1")
    assert [c.get("dom") for c in task["children"]] == ["tool-s1", None, "tool-s2", "tool-s3"] and task["kids_n"] == 3
    assert task["children"][1]["kind"] == "note"  # lỗi của subagent cũng lồng
    assert not any(i.get("sub") and i["kind"] == "msg" for i in tx.items)  # chữ subagent bỏ (như trước)
    html = str(make_templates().env.get_template("_items.html").module.item(task))
    assert "Xong (3 công cụ · 3.2s)" in html and 'id="tool-t1-k"' in html and html.index('id="tool-s1"') > html.index('id="tool-t1-k"')
    assert "Kiểm &lt;i&gt;tra&lt;/i&gt;" in html and "<i>tra" not in html
    # đang chạy: "Đang chạy… (n công cụ)" + mở sẵn
    running = replay_tx(EVENTS[:EVENTS.index(next(e for e in EVENTS if e["seq"] == 18))])
    t = running.tree(running.items[running._at["tool-t1"]])
    h = str(make_templates().env.get_template("_items.html").module.item(t))
    assert "Đang chạy… (3 công cụ)" in h and re.search(r'id="tool-t1"[^>]*data-auto="1" open>', h)


def test_nested_subagent_from_real_adapter_fixture():
    lines = (FIX / "claude-subagent.jsonl").read_text(encoding="utf-8").splitlines()
    evs = [e.to_wire() for e in adapt_lines("a", lines, run_id="r1")]
    for i, e in enumerate(evs):
        e.setdefault("seq", i + 1)
    tx = replay_tx(evs)
    task = next(t for b in tx.blocks() if b["kind"] == "group" for t in b["tools"] if t["tool"] == "Task")
    assert [c["tool"] for c in task["children"]] == ["Glob", "Read", "Bash"] and task["children"][2]["error"]
    assert not task["running"] and task["kids_n"] == 3
    live, _ = live_html(evs)
    assert live == norm(replay_html(evs))
    assert "&lt;b&gt;hi&lt;/b&gt;" in live and "<b>hi" not in live


# =========================================================================== P1-1 part
def test_part_messages_throttled_and_sanitized():
    now = [100.0]
    conv = _run_converter(make_templates(), clock=lambda: now[0])

    def delta(text, sub=None):
        return conv({"type": "message.delta", "seq": 1, "subagent_id": sub, "payload": {"text": text}})

    out = delta("**a** <script>alert(1)</script>")
    assert [m["t"] for m in out] == ["delta", "part"]  # khối mới → render ngay
    part = out[1]["html"]
    assert "<strong>a</strong>" in part and "<script>" not in part and "&lt;script&gt;" in part
    _safe(part)
    assert [m["t"] for m in delta(" b")] == ["delta"]  # < 250 ms, < 400 ký tự → chỉ delta
    now[0] += 0.3
    out = delta(" c")
    assert [m["t"] for m in out] == ["delta", "part"] and "a</strong> &lt;script&gt;alert(1)&lt;/script&gt; b c" in out[1]["html"]
    assert [m["t"] for m in delta("x" * 450)] == ["delta", "part"]  # đủ 400 ký tự mới → không đợi thời gian
    assert delta("sub", sub="t1") == []  # chữ subagent không lên live
    now[0] += 1
    assert [m["t"] for m in delta("y" * PART_MAX)] == ["delta"]  # vượt trần → thôi render
    # message.complete → khối mới: part lại từ đầu, không lẫn chữ cũ
    conv({"type": "message.complete", "seq": 2, "payload": {"text": "xong"}})
    now[0] += 1
    out = delta("mới")
    assert out[1]["t"] == "part" and "x" not in out[1]["html"] and "mới" in out[1]["html"]
    # suy nghĩ sau chữ → khối chữ kế là khối mới
    conv({"type": "reasoning.delta", "seq": 3, "payload": {"text": "nghĩ"}})
    now[0] += 1
    out = delta("khác")
    assert "mới" not in out[1]["html"] and "khác" in out[1]["html"]


# =========================================================================== P1-7 diff
def test_edit_diff_is_unified_with_context_cap_and_counts():
    old = "\n".join(f"line {i}" for i in range(1, 21))
    new = old.replace("line 10", "line 10 changed").replace("line 11\n", "")
    v = P.tool_view("Edit", {"file_path": "/w/a.py", "old_string": old, "new_string": new})
    d = v["diff"]
    rows = [(k, "".join(t for _, t in s)) for k, s in d["lines"]]
    assert rows[0][0] == "hunk" and rows[0][1].startswith("@@")
    assert ("ctx", " line 7") in rows and ("ctx", " line 13") in rows and ("ctx", " line 6") not in rows  # 3 dòng ngữ cảnh
    assert ("del", "-line 10") in rows and ("add", "+line 10 changed") in rows and ("del", "-line 11") in rows
    assert (d["adds"], d["dels"]) == (1, 2) and v["meta"] == "+1 −2" and v["verb"] == "Sửa"
    # trần dòng + "… còn N dòng"
    big = P.tool_view("Edit", {"file_path": "x", "old_string": "", "new_string": "\n".join(str(i) for i in range(500))})["diff"]
    assert len(big["lines"]) == P.DIFF_MAX and big["more"] == 1 + 500 - P.DIFF_MAX and big["adds"] == 500
    html = str(make_templates().env.get_template("_transcript.html").module.tx_diff(big))
    assert f"… còn {big['more']} dòng" in html and 'class="udiff tx-diff"' in html and 'class="dl add"' in html
    # MultiEdit: nhiều chỗ sửa, đếm cộng dồn
    me = P.tool_view("MultiEdit", {"file_path": "x", "edits": [{"old_string": "a", "new_string": "b"}, {"old_string": "c\nd", "new_string": "c"}]})
    assert me["meta"] == "+1 −2" and any(k == "meta" for k, _ in me["diff"]["lines"])
    # ký tự bidi/ẩn bị lộ trong diff, chữ agent escape
    ev = P.tool_view("Edit", {"file_path": "x", "old_string": "a", "new_string": "<b>‮exe"})
    html = str(make_templates().env.get_template("_transcript.html").module.tx_diff(ev["diff"]))
    assert '<mark class="ctl">⟦U+202E⟧</mark>' in html and "‮" not in html and "&lt;b&gt;" in html


def test_write_shows_first_20_lines_rest_collapsed():
    content = "\n".join(f"r{i}" for i in range(260))
    v = P.tool_view("Write", {"file_path": "/w/n.txt", "content": content})
    d = v["diff"]
    assert len(d["lines"]) == P.WRITE_HEAD and len(d["rest"]) == P.DIFF_MAX - P.WRITE_HEAD and d["more"] == 60 and v["meta"] == "260 dòng"
    html = str(make_templates().env.get_template("_transcript.html").module.tx_diff(d))
    assert '<details class="tx-more"><summary>Xem thêm 180 dòng</summary>' in html and "… còn 60 dòng" in html
    assert html.index("+r19") < html.index("Xem thêm") < html.index("+r20")


# =========================================================================== P1-11 duyệt / câu hỏi một dòng
def test_approval_and_question_are_one_line_links_to_cards():
    tx = replay_tx(EVENTS[:EVENTS.index(next(e for e in EVENTS if e["seq"] == 23))])
    mod = make_templates().env.get_template("_items.html").module
    ap = str(mod.item(tx.items[tx._at["ev-ap-ap1"]]))
    assert 'href="#ap-ap1"' in ap and 'data-alt="/inbox#ap-ap1"' in ap and "Chờ bạn duyệt" in ap and "rm -rf build…" in ap
    assert "next line" not in ap and "<form" not in ap and "/do/" not in ap and "luật" not in ap
    q = str(mod.item(tx.items[tx._at["ev-q-q1"]]))
    assert 'href="#q-q1"' in q and 'data-alt="/inbox#q-q1"' in q and "Chọn cách nào?…" in q and "chi tiết dài" not in q
    assert "<textarea" not in q and "<form" not in q
    done = replay_tx(EVENTS)
    ap2 = str(mod.item(done.items[done._at["ev-ap-ap1"]]))
    assert 'class="tx-line-a" href' not in ap2 and "Đã cho phép bởi user qua web" in ap2
    _safe(ap + q + ap2, icons=True)


def test_items_have_hover_timestamps():
    tx = replay_tx(EVENTS)
    html = replay_html(EVENTS)
    t = next(i for i in tx.items if i.get("dom") == "tool-a1")
    assert t["iso"] and f'<time class="tx-time" datetime="{t["iso"]}" data-rel title="{t["ts"]}">' in html


def test_live_converter_primed_with_rendered_events_continues_groups():
    """SSE nối vào giữa run: event ≤ since đã có trên trang → nạp vào bộ gộp (sse_run `prime`), không phát lại."""
    cut = next(i for i, e in enumerate(EVENTS) if e["seq"] == 16)  # trang vẽ tới seq 15: tool s3 chưa bắt đầu, Task t1 đang chạy
    conv = _run_converter(make_templates(), prime=EVENTS[:cut])
    rest = [m for ev in EVENTS[cut:] for m in conv(ev) if m["t"] == "item"]
    s3 = next(m for m in rest if m["dom"] == "tool-s3")
    assert s3["parent"] == "tool-t1"  # subagent tìm được tool cha đã vẽ sẵn
    t1 = [m for m in rest if m["dom"] == "tool-t1"][-1]
    assert t1["parent"] == "grp-tool-b1" and "Kiểm &lt;i&gt;tra&lt;/i&gt;" in t1["html"] and "chạy lại test" in t1["html"]  # input giữ khi xong
    # trang (phát lại tới cut) + live phần còn lại == phát lại toàn bộ
    cl = Client()
    cl.root = parse(replay_html(EVENTS[:cut]))
    conv2 = _run_converter(make_templates(), prime=EVENTS[:cut])
    for m in [m for ev in EVENTS[cut:] for m in conv2(ev)]:
        cl.apply(m)
    assert cl.root.html() == norm(replay_html(EVENTS))
