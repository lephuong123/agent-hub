"""`cfharness chat` — chat qua lại với điều phối viên trên terminal (manager/repl.py + chatview.py + termsafe.py).

Daemon IN-PROCESS trên Unix socket thật + ScriptedRuntime (giữ run điều phối viên ở `running` để kiểm chữ chạy/steer/stop).
Không gọi `claude`.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from cfharness import cli
from cfharness.daemon import Daemon
from cfharness.events import make_event
from cfharness.manager import chatview as V
from cfharness.manager import repl
from cfharness.termsafe import clean, one_line
from cfharness.tui.client import AsyncHubClient
from m3_fake import ScriptedRuntime
from m7_fake import MgrScript, wait_status

EVIL = ("A\x1b]8;;http://evil.example\x07bấm\x1b]8;;\x07 \x1b[31mĐỎ\x1b[0m \x1b]0;pwned\x07 ‮gnp.exe ​ẩn "
        "[link=http://x]y[/link] [bold]B[/bold] \x9b2J\rghi đè\x07\x08")


async def until(pred, timeout: float = 10.0) -> bool:
    for _ in range(int(timeout / 0.02)):
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.fixture
async def env(hub_home):
    s = MgrScript()
    d = Daemon(hub_home, runtime=ScriptedRuntime(s))
    d.hub.config.set("tasks.tick_s", 0.05)
    d.hub.config.set("manager.check_s", 3600)
    task = asyncio.create_task(d.serve())
    assert await until(lambda: hub_home.socket.exists())
    d.hub.agents.create({"id": "boss", "name": "Điều phối", "role": "hub boss", "model": "haiku", "tool_policy": {"tools": ["Read", "Grep", "Glob"]}})
    d.script = s  # type: ignore[attr-defined]
    yield d
    d.hub.runs.runtime().release()  # type: ignore[attr-defined]
    d.stopping.set()
    try:
        await asyncio.wait_for(task, 20)
    except Exception:
        pass


async def designate(d) -> None:
    resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "manager.set", "params": {"agent_id": "boss"}})
    assert "error" not in resp, resp


def session(hub_home, *, color: bool = False, **kw) -> tuple[repl.ChatSession, io.StringIO]:
    buf = io.StringIO()
    s = repl.ChatSession(AsyncHubClient(hub_home.socket, channel="cli"), repl.Out(color=color, file=buf, width=120), **kw)
    return s, buf


# =========================================================================== termsafe (thuần)
def test_termsafe_strips_escapes_controls_bidi():
    out = clean(EVIL)
    for bad in ("\x1b", "\x07", "\x08", "\x9b", "\r", "‮", "​", "http://evil.example", "pwned"):
        assert bad not in out, (bad, out)
    assert "bấm" in out and "ĐỎ" in out and "[link=http://x]y[/link]" in out and "ghi đè" in out
    assert clean("a\r\nb\tc") == "a\nb\tc"
    assert one_line("a\n\nb   c", 4) == "a b…"


def test_render_never_interprets_markup_or_ansi():
    from rich.console import Console

    for item in ({"kind": "manager", "text": EVIL}, {"kind": "user", "text": EVIL}, {"kind": "notify", "text": EVIL},
                 {"kind": "action", "summary": EVIL, "target": {"label": EVIL}, "ok": False, "error": EVIL},
                 {"kind": "approval", "request_id": "ap_1", "preview": EVIL, "reason": EVIL}, {"kind": "note", "text": EVIL}):
        buf = io.StringIO()
        Console(file=buf, force_terminal=True, color_system="truecolor", width=200, markup=False, highlight=False).print(V.render(item))
        s = buf.getvalue()
        assert "\x1b]" not in s and "http://evil" not in s and "‮" not in s and "\x9b" not in s and "\x07" not in s, (item["kind"], s)
        assert "\x1b[31mĐỎ" not in s  # mã màu của agent bị bỏ (rich chỉ phát mã của chính nó)
        if item["kind"] in ("manager", "user", "notify", "note"):
            assert "[link=http://x]y[/link]" in s and "[bold]B[/bold]" in s  # markup hiện nguyên văn


def test_chatstate_tracks_continuation_and_filters_other_conversations():
    st = V.ChatState("boss", "c1")
    st.sent({"conversation_id": "c1", "run_id": "r1"}, "hi")
    assert st.busy and st.active_run == "r1"
    assert st.apply({"type": "message.delta", "run_id": "rX", "payload": {"text": "khác"}}) == []
    st.apply({"type": "message.delta", "run_id": "r1", "payload": {"text": "Đang "}})
    st.apply({"type": "message.delta", "run_id": "r1", "payload": {"text": "xem"}})
    assert st.draft == "Đang xem" and "Đang xem" in st.status_text(200).plain
    # approval → run tiếp nối (parent_run_id) vẫn thuộc hội thoại
    items = st.apply({"type": "approval.open", "run_id": "r1", "payload": {"request_id": "ap_9", "preview": "rm -rf x"}})
    assert items[0]["kind"] == "approval" and "chờ bạn duyệt ap_9" in st.status_text(200).plain
    st.apply({"type": "run.queued", "run_id": "r2", "payload": {"parent_run_id": "r1"}})
    assert "r2" in st.runs and st.active_run == "r2"
    act = st.apply({"type": "manager.action", "run_id": "r2", "payload": {"tool": "agent_create", "target": "agent:x", "summary": "tạo x", "ok": True}})
    assert act[0]["kind"] == "action" and act[0]["target"]["href"]
    assert st.apply({"type": "manager.reply", "payload": {"conversation_id": "c2", "kind": "reply", "text": "khác"}}) == []
    assert st.apply({"type": "manager.reply", "payload": {"conversation_id": "c1", "kind": "tick", "text": "nhịp"}}) == []
    out = st.apply({"type": "manager.reply", "run_id": "r2", "payload": {"conversation_id": "c1", "kind": "reply", "text": "xong", "ok": True}})
    assert out[0]["kind"] == "manager" and not st.busy and st.draft == ""


# =========================================================================== phiên chat với daemon thật
async def test_no_manager_friendly_guidance(env, hub_home):
    s, buf = session(hub_home)
    assert await s.open() is False
    assert "Chưa có điều phối viên" in buf.getvalue() and "cfharness manager set" in buf.getvalue()


async def test_stream_actions_reply_and_audit_channel_cli(env, hub_home):
    d = env
    await designate(d)
    s, buf = session(hub_home, new=True)
    assert await s.open()
    try:
        res = await s.send("xin chào điều phối viên")
        rid = res["run_id"]
        await wait_status(d, rid)
        assert s.state.busy and s.state.active_run == rid
        d.hub.publish(make_event("message.delta", "boss", {"text": "Mình đang tạo "}, run_id=rid))
        d.hub.publish(make_event("message.delta", "boss", {"text": "agent researcher"}, run_id=rid))
        d.hub.publish(make_event("manager.action", "boss", {"tool": "agent_create", "target": "agent:researcher", "summary": "Tạo agent researcher", "ok": True}, run_id=rid))
        d.hub.publish(make_event("manager.action", "boss", {"tool": "agent_update", "target": "agent:boss", "summary": "Sửa chính mình", "ok": False, "error": "không được"}, run_id=rid))
        assert await until(lambda: "agent researcher" in s.state.draft and "Sửa chính mình" in buf.getvalue())
        assert "agent researcher" in s.state.status_text(200).plain and "đang nghĩ" in s.state.status_text(200).plain
        d.hub.runs.runtime().release(rid)  # type: ignore[attr-defined]
        assert await s.wait_idle(10)
        out = buf.getvalue()
        assert "❯ Bạn" in out and "xin chào điều phối viên" in out
        assert "✓ Tạo agent researcher" in out
        assert "✗ Sửa chính mình" in out and "không được" in out
        assert "◆ Điều phối" in out and "Đã giao việc." in out
        assert out.index("Tạo agent researcher") < out.index("Đã giao việc.")
        row = d.hub.store.conn.execute("SELECT actor, channel FROM audit_log WHERE kind='manager.chat' ORDER BY id DESC LIMIT 1").fetchone()
        assert row and row["channel"] == "cli" and row["actor"] == "user"
    finally:
        await s.close()


async def test_steer_while_running_and_stop_turn(env, hub_home):
    d = env
    await designate(d)
    s, buf = session(hub_home, new=True)
    assert await s.open()
    try:
        res = await s.send("việc 1")
        rid = res["run_id"]
        await wait_status(d, rid)
        await asyncio.sleep(0.1)  # để run qua phần init (steer cần tiến trình nhận stdin)
        r2 = await s.send("thêm ý: làm nhanh nhé")
        assert r2 and r2.get("steered") and r2["run_id"] == rid
        assert "đã chèn tin vào lượt" in buf.getvalue()
        assert s.state.busy  # steer không kết thúc lượt
        assert await s.handle("/stop")
        assert await s.wait_idle(10)
        out = buf.getvalue()
        assert "đã yêu cầu dừng lượt" in out and "không trả lời được" in out
        assert d.hub.store.get_run(rid)["status"] == "cancelled"
        # Ctrl-C/stop khi rảnh → chỉ báo, không lỗi
        assert await s.stop_turn() is False
    finally:
        await s.close()


async def test_commands_new_list_open_history_and_shared_conversation(env, hub_home):
    d = env
    await designate(d)
    d.script.hold_manager = False
    s, buf = session(hub_home, new=True)
    assert await s.open()
    try:
        await s.send("tin đầu tiên")
        assert await s.wait_idle(10)
        c1 = s.state.conversation_id
        assert await s.handle("/new") and s.state.conversation_id is None
        await s.send("hội thoại thứ hai")
        assert await s.wait_idle(10)
        c2 = s.state.conversation_id
        assert c2 and c2 != c1
        buf.truncate(0)
        buf.seek(0)
        assert await s.handle("/list")
        assert c1 in buf.getvalue() and f"▸ {c2}" in buf.getvalue()
        assert await s.handle(f"/open {c1}")
        assert s.state.conversation_id == c1 and "tin đầu tiên" in buf.getvalue()
        assert await s.handle("/open c_khong_co")
        assert "không có hội thoại" in buf.getvalue()
        buf.truncate(0)
        buf.seek(0)
        assert await s.handle("/history 1")
        assert "Đã giao việc." in buf.getvalue() and "tin đầu tiên" not in buf.getvalue()
        assert await s.handle("/help") and "/history" in buf.getvalue()
        assert await s.handle("/xyz") and "lệnh không rõ" in buf.getvalue()
        assert await s.handle("/quit") is False
    finally:
        await s.close()
    # phiên mới (vd mở lại terminal, hoặc TUI) mặc định nối vào hội thoại cli gần nhất + in lại tin trước
    s2, buf2 = session(hub_home)
    assert await s2.open()
    await s2.close()
    assert s2.state.conversation_id == c2 and "2 tin trước" in buf2.getvalue() and "hội thoại thứ hai" in buf2.getvalue()


async def test_manager_injection_rendered_as_data(env, hub_home):
    d = env
    await designate(d)
    d.script.hold_manager = False
    d.script.reply = EVIL
    s, buf = session(hub_home, color=True, new=True)
    assert await s.open()
    try:
        await s.send("thử")
        assert await s.wait_idle(10)
        out = buf.getvalue()
        assert "ghi đè" in out and "[link=http://x]y[/link]" in out
        for bad in ("\x1b]", "http://evil", "pwned", "‮", "​", "\x9b", "\x07", "\x1b[31mĐỎ", "\r"):
            assert bad not in out, bad
    finally:
        await s.close()


async def test_plain_mode_reads_stdin_lines(env, hub_home):
    d = env
    await designate(d)
    d.script.hold_manager = False
    s, buf = session(hub_home, new=True)
    assert await s.open()
    lines = iter(["chào bạn\n", "/history\n", ""])
    try:
        code = await repl.run_plain(s, timeout=10, readline=lambda: next(lines))
    finally:
        await s.close()
    assert code == 0
    out = buf.getvalue()
    assert out.count("Đã giao việc.") >= 2 and "\x1b[" not in out  # trả lời + lịch sử; không màu


async def test_tty_prompt_loop_with_pipe_input(env, hub_home):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    d = env
    await designate(d)
    d.script.hold_manager = False
    s, buf = session(hub_home, new=True)
    assert await s.open()
    try:
        with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
            inp.send_text("chào từ tty\r")

            async def later():
                assert await until(lambda: s.state.conversation_id is not None and not s.state.busy and "Đã giao việc." in buf.getvalue())
                inp.send_text("/quit\r")

            t = asyncio.ensure_future(later())
            code = await asyncio.wait_for(repl.run_tty(s, hub_home.home), 15)
            await t
    finally:
        await s.close()
    assert code == 0
    assert "chào từ tty" in buf.getvalue()
    hist = hub_home.home / "cli_history"
    assert hist.exists() and (hist.stat().st_mode & 0o777) == 0o600 and "chào từ tty" in hist.read_text()


async def test_oneshot_cli_waits_via_events(env, hub_home, capsys):
    d = env
    await designate(d)
    d.script.hold_manager = False
    code = await asyncio.to_thread(cli.main, ["manager", "chat", "báo", "cáo", "--json"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["reply"] == "Đã giao việc." and out["conversation_id"] and out["run_id"]
    # `cfharness chat "<tin>"` = cùng hành vi, nối vào hội thoại cli gần nhất
    code = await asyncio.to_thread(cli.main, ["chat", "tiếp", "nhé"])
    assert code == 0
    cap = capsys.readouterr()
    assert "Đã giao việc." in cap.out and "đang chờ điều phối viên" in cap.err
    convs = await AsyncHubClient(hub_home.socket).call("manager.conversations", {"channel": "cli"})
    assert len(convs) == 1 and convs[0]["messages"] == 4
    # REPL không hỗ trợ --json
    assert await asyncio.to_thread(cli.main, ["chat", "--json"]) == 1
