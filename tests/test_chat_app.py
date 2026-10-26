"""`cfharness chat` fullscreen (tui/chat.py) — Textual Pilot + daemon IN-PROCESS (socket thật, ScriptedRuntime). Không gọi `claude`.

Kiểm: chữ đang chạy + dòng tool của điều phối viên (click mở rộng), thẻ hành động click → mở đích, thẻ duyệt click → ĐIỀN SẴN lệnh
(chỉ duyệt sau Enter + y), bảng agent đang chạy → click mở AttachScreen, kéo chọn chữ → copy, lịch sử ô nhập dùng chung tệp với REPL,
chữ agent là DỮ LIỆU (không markup/ANSI). Ảnh SVG: `CFHARNESS_SCREENSHOTS=docs/screenshots/m2`.
"""

from __future__ import annotations

from textual.widgets import Input, Static

from cfharness.events import make_event
from cfharness.manager import attachments as A
from cfharness.tui import chat as C
from cfharness.tui.app import AttachScreen, ConfirmScreen, TasksScreen
from m7_fake import wait_status
from test_manager_repl import EVIL, designate, env  # noqa: F401  (fixture daemon + điều phối viên)
from test_tui import SIZE, shot, until


def items(scr: C.ChatScreen) -> list[C.Item]:
    return [w for w in scr.chat_log.children if isinstance(w, C.Item)]


def log_text(scr: C.ChatScreen) -> str:
    return "\n".join(str(getattr(w.render(), "plain", w.render())) for w in items(scr))


def item_with(scr: C.ChatScreen, needle: str) -> C.Item | None:
    return next((w for w in items(scr) if needle in str(getattr(w.render(), "plain", ""))), None)


async def open_chat(app, pilot) -> C.ChatScreen:
    assert await until(lambda: isinstance(app.screen, C.ChatScreen) and app.screen.session is not None, pilot=pilot)
    scr = app.screen
    assert await until(lambda: scr.session.state is not None or "Chưa có điều phối viên" in log_text(scr), pilot=pilot)
    return scr


async def send(pilot, scr: C.ChatScreen, text: str) -> None:
    inp = scr.query_one("#chat-input", Input)
    inp.focus()
    inp.value = text
    await pilot.press("enter")


def no_native_clipboard(monkeypatch) -> list[str]:
    """Test không được ghi clipboard thật của máy."""
    got: list[str] = []
    monkeypatch.setattr(A, "clipboard_set", lambda text: got.append(text) or True)
    return got


# =========================================================================== thuần
def test_flatten_makes_selectable_text_and_tool_text():
    from rich.console import Group
    from rich.padding import Padding
    from rich.text import Text

    t = C.flatten(Group(Text(""), Text("hdr"), Padding(Text("a\nb"), (0, 0, 0, 2))))
    assert isinstance(t, Text) and t.plain == "\nhdr\n  a\n  b"
    it = {"name": "Bash", "args": "ls -la", "result": "\n".join(f"line {i}" for i in range(10)), "done": True, "duration_s": 1.25,
          "input": {"command": "ls -la"}}
    short = C.tool_text(it, expanded=False).plain
    assert "● Bash(ls -la)  1.2s" in short and "line 2" in short and "line 3" not in short and "+7 dòng (click để mở)" in short
    full = C.tool_text(it, expanded=True).plain
    assert "line 9" in full and '"command": "ls -la"' in full and "click để mở" not in full
    run = C.tool_text({"name": "Read", "args": "a.py", "started": 0.0}, expanded=False, now=3.0).plain
    assert "đang chạy… 3s" in run
    evil = C.tool_text({"name": "x", "args": EVIL, "result": EVIL, "done": True}, expanded=True).plain + C.text_item(EVIL).plain
    assert "\x1b" not in evil and "http://evil" not in evil and "\x07" not in evil


def test_history_file_shared_with_repl_format(tmp_path):
    from prompt_toolkit.history import FileHistory

    p = tmp_path / "cli_history"
    FileHistory(str(p)).store_string("từ REPL\ndòng 2")
    C.append_history(p, "từ fullscreen")
    assert C.load_history(p) == ["từ REPL\ndòng 2", "từ fullscreen"]
    assert list(FileHistory(str(p)).load_history_strings()) == ["từ fullscreen", "từ REPL\ndòng 2"]


def test_live_state_emits_tools_and_interim_text():
    st = C.LiveChatState("boss", "c1")
    st.sent({"conversation_id": "c1", "run_id": "r1"}, "hi")
    st.apply({"type": "message.delta", "run_id": "r1", "payload": {"text": "Để mình xem"}})
    out = st.apply({"type": "tool.start", "run_id": "r1", "payload": {"tool_id": "t1", "name": "Read", "args_preview": "a.py", "input": {"file_path": "a.py"}}})
    assert [o["kind"] for o in out] == ["interim", "tool"] and out[0]["text"] == "Để mình xem" and st.draft == ""
    assert st.activity == "Read"
    done = st.apply({"type": "tool.complete", "run_id": "r1", "payload": {"tool_id": "t1", "duration_s": 0.4, "result_preview": "x"}})
    assert done[0]["kind"] == "tool_done" and done[0]["result"] == "x"
    assert st.apply({"type": "tool.start", "run_id": "rX", "payload": {"tool_id": "t9", "name": "Bash"}}) == []  # run khác


# =========================================================================== app + daemon thật
async def test_no_manager_shows_guidance(env, hub_home):
    app = C.ChatApp(hub_home.socket, home=hub_home.home)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_chat(app, pilot)
        assert "Chưa có điều phối viên" in log_text(scr) and "cfharness manager set" in log_text(scr)
        assert scr.query_one("#chat-input", Input).disabled


async def test_stream_tools_click_expand_actions_and_reply(env, hub_home, monkeypatch):
    d = env
    no_native_clipboard(monkeypatch)
    await designate(d)
    d.hub.agents.create({"id": "w1", "name": "w1", "role": "worker", "model": "haiku"})
    task = d.hub.tasks.create({"assignee": "w1", "title": "Tóm tắt README"})
    app = C.ChatApp(hub_home.socket, home=hub_home.home)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_chat(app, pilot)
        await send(pilot, scr, "giao việc tóm tắt README cho w1")
        assert await until(lambda: scr.session.state.active_run is not None, pilot=pilot)
        rid = scr.session.state.active_run
        await wait_status(d, rid)
        assert "giao việc tóm tắt README" in log_text(scr)
        # chữ đang chạy hiện trong transcript
        d.hub.publish(make_event("message.delta", "boss", {"text": "Để mình xem README trước. " + EVIL}, run_id=rid))
        draft = scr.query_one("#chat-draft", Static)
        assert await until(lambda: draft.display and "Để mình xem README" in draft.render().plain, pilot=pilot)
        assert "\x1b" not in draft.render().plain and "http://evil" not in draft.render().plain
        # tool → đoạn chữ trước đó được giữ, dòng tool đang chạy rồi xong
        out = "\n".join(f"dòng {i}" for i in range(12))
        d.hub.publish(make_event("tool.start", "boss", {"tool_id": "t1", "name": "Read", "args_preview": "README.md", "input": {"file_path": "README.md"}}, run_id=rid))
        assert await until(lambda: item_with(scr, "Read(README.md)") is not None, pilot=pilot)
        assert item_with(scr, "Để mình xem README trước.") is not None and not draft.display
        d.hub.publish(make_event("tool.complete", "boss", {"tool_id": "t1", "name": "Read", "duration_s": 0.3, "result_preview": out}, run_id=rid))
        tool = item_with(scr, "Read(README.md)")
        assert await until(lambda: "+9 dòng (click để mở)" in tool.render().plain, pilot=pilot)
        await pilot.click(tool)
        assert await until(lambda: "dòng 11" in tool.render().plain and '"file_path": "README.md"' in tool.render().plain, pilot=pilot)
        await pilot.click(tool)
        assert await until(lambda: "dòng 11" not in tool.render().plain, pilot=pilot)
        # Ctrl-O: mở rộng mọi tool
        await pilot.press("ctrl+o")
        assert await until(lambda: "dòng 11" in tool.render().plain, pilot=pilot)
        await pilot.press("ctrl+o")
        # thẻ hành động: click → mở đích (task → TasksScreen); đích lạ → chỉ thông báo
        d.hub.publish(make_event("manager.action", "boss", {"tool": "task_create", "target": f"task:{task['id']}", "summary": "Giao task cho w1", "ok": True}, run_id=rid))
        d.hub.publish(make_event("manager.action", "boss", {"tool": "x", "target": "javascript:alert(1)", "summary": "Đích lạ", "ok": True}, run_id=rid))
        assert await until(lambda: item_with(scr, "Đích lạ") is not None, pilot=pilot)
        shot(app, "20-chat-dang-lam.svg")
        await pilot.click(item_with(scr, "Đích lạ"))
        await pilot.pause(0.2)
        assert app.screen is scr
        await pilot.click(item_with(scr, "Giao task cho w1"))
        assert await until(lambda: isinstance(app.screen, TasksScreen), pilot=pilot)
        await pilot.press("escape")
        assert await until(lambda: app.screen is scr, pilot=pilot)
        # trả lời
        d.hub.runs.runtime().release(rid)  # type: ignore[attr-defined]
        assert await until(lambda: not scr.session.state.busy and item_with(scr, "Đã giao việc.") is not None, pilot=pilot)
        assert not draft.display
        shot(app, "21-chat-xong.svg")


async def test_agent_panel_opens_attach_and_approval_card_prefills(env, hub_home, monkeypatch):
    d = env
    no_native_clipboard(monkeypatch)
    await designate(d)
    d.hub.agents.create({"id": "w1", "name": "Worker một", "role": "worker", "model": "haiku"})
    rt = d.hub.runs.runtime()
    orig = rt.script
    rt.script = lambda spec: {**(orig(spec) or {}), "hold": True} if spec.agent_id == "w1" else orig(spec)  # type: ignore[attr-defined]
    app = C.ChatApp(hub_home.socket, home=hub_home.home)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_chat(app, pilot)
        run = d.hub.runs.submit("w1", "dọn thư mục build")
        await wait_status(d, run["id"])
        box = scr.query_one("#chat-agents-box")
        agents = scr.query_one("#chat-agents", C.AgentList)
        assert await until(lambda: box.display and agents.option_count >= 1, pilot=pilot)
        assert "1 agent đang chạy" in scr.query_one("#chat-agents-head", Static).render().plain
        # ↓ ở ô nhập trống → vào bảng; ↑ → về ô nhập
        await pilot.press("down")
        assert await until(lambda: agents.has_focus, pilot=pilot)
        await pilot.press("up")
        assert await until(lambda: scr.query_one("#chat-input", Input).has_focus, pilot=pilot)
        # click dòng agent → xem agent chạy live; Esc → về chat
        await pilot.click(agents, offset=(4, 0))
        assert await until(lambda: isinstance(app.screen, AttachScreen) and app.screen.run_id == run["id"], pilot=pilot)
        shot(app, "22-chat-attach.svg")
        await pilot.press("escape")
        assert await until(lambda: app.screen is scr, pilot=pilot)
        # thẻ duyệt: click chỉ ĐIỀN SẴN lệnh; duyệt thật sau Enter + y
        ap = d.hub.approvals.open(d.hub.store.get_run(run["id"]), tool_name="Bash", tool_input={"command": "rm -rf build/"}, tool_use_id="toolu_1",
                                  fingerprint="fp-1", verdict={"klass": "fs_delete", "rule": "rm_recursive", "verdict": "destructive", "reason": "xoá đệ quy"},
                                  channel="hook")
        assert await until(lambda: item_with(scr, "Cần bạn duyệt") is not None, pilot=pilot)
        card = item_with(scr, "Cần bạn duyệt")
        assert "rm -rf build/" in card.render().plain
        await pilot.click(card)
        inp = scr.query_one("#chat-input", Input)
        assert await until(lambda: inp.value == f"/approve {ap['id']} ", pilot=pilot)
        assert d.hub.approvals.get(ap["id"])["status"] == "open"  # chưa làm gì
        await pilot.press("enter")
        assert await until(lambda: isinstance(app.screen, ConfirmScreen), pilot=pilot)
        await pilot.press("y")
        assert await until(lambda: d.hub.approvals.get(ap["id"])["status"] != "open", pilot=pilot)
        got = d.hub.approvals.get(ap["id"])
        assert got["status"] == "allowed" and got["resolved_channel"] == "cli"
        rt.release(run["id"])  # type: ignore[attr-defined]


async def test_drag_select_copies_and_ctrl_c(env, hub_home, monkeypatch):
    d = env
    native = no_native_clipboard(monkeypatch)
    await designate(d)
    app = C.ChatApp(hub_home.socket, home=hub_home.home)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_chat(app, pilot)
        scr.add_item({"kind": "note", "text": "chép dòng này nhé"})
        await pilot.pause(0.2)
        w = item_with(scr, "chép dòng này nhé")
        col = w.render().plain.index("chép")
        await pilot.mouse_down(w, offset=(col, 0))
        await pilot.hover(w, offset=(col + 12, 0))
        await pilot.mouse_up(w, offset=(col + 12, 0))
        assert await until(lambda: native and native[-1].startswith("chép dòng"), pilot=pilot)
        assert app.clipboard == native[-1]  # OSC 52 qua copy_to_clipboard của Textual
        assert "Đã copy" in " ".join(str(n.message) for n in app._notifications)
        # kéo chọn không bị coi là click (không đổi màn, không điền ô nhập)
        assert app.screen is scr and scr.query_one("#chat-input", Input).value == ""
        # Ctrl-C khi rảnh, không có vùng chọn: lần 1 nhắc, lần 2 thoát
        scr.clear_selection()
        await pilot.press("ctrl+c")
        await pilot.pause(0.1)
        assert app.is_running
        await pilot.press("ctrl+c")
        assert await until(lambda: not app.is_running, pilot=pilot)


async def test_history_up_down_and_slash_hint(env, hub_home, monkeypatch):
    d = env
    no_native_clipboard(monkeypatch)
    await designate(d)
    C.append_history(hub_home.home / "cli_history", "tin cũ từ REPL")
    app = C.ChatApp(hub_home.socket, home=hub_home.home)
    async with app.run_test(size=SIZE) as pilot:
        scr = await open_chat(app, pilot)
        inp = scr.query_one("#chat-input", Input)
        inp.value = "đang gõ"
        await pilot.press("up")
        assert inp.value == "tin cũ từ REPL"
        await pilot.press("down")
        assert inp.value == "đang gõ"
        inp.value = ""
        await pilot.press("/", "a", "p")
        hint = scr.query_one("#chat-hint", Static)
        assert await until(lambda: hint.display and "/approve" in hint.render().plain, pilot=pilot)
        await pilot.press("escape")
        assert not hint.display
