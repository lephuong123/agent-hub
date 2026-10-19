"""`agent-hub chat` — chat qua lại với điều phối viên trên terminal (kênh `cli`, dùng chung hội thoại với TUI `top` → `m`).

Client THUẦN của daemon: `AsyncHubClient(channel="cli")` (mọi ghi gửi `_audit.channel=cli`) + `events.subscribe` (agent = điều phối
viên, level detail) để chữ chạy / thẻ hành động / trả lời tới ngay, không hỏi lại theo chu kỳ. Không RPC mới.

- TTY: `prompt_toolkit` — dòng nhập luôn ở đáy, tin mới in phía trên (`patch_stdout`), thanh trạng thái (spinner, thời gian, tool
  đang dùng, đuôi chữ đang viết), gợi ý lệnh `/`, lịch sử ↑↓ ở `HUB_HOME/cli_history` (0600), Alt-Enter xuống dòng. Gõ khi điều
  phối viên đang làm → tin được steer vào lượt đó. Ctrl-C: đang làm → dừng lượt (`run.stop`); rảnh → lần 2 trong 2 s mới thoát.
- Không phải TTY: đọc từng dòng stdin, gửi rồi CHỜ trả lời, in chữ thường không màu (dùng trong script/test).
- `oneshot()`: `manager chat "<tin>"` — đăng ký event TRƯỚC khi gửi rồi chờ `manager.reply` của hội thoại (thay vòng hỏi 1 s cũ).
Chữ của điều phối viên là DỮ LIỆU: luôn qua `termsafe.clean` + `rich.text.Text` (xem `chatview`).
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.text import Text

from ..rpc import DaemonUnavailable, RpcError
from ..termsafe import one_line
from ..tui.client import AsyncHubClient, subscribe
from . import chatview as V

NO_MANAGER = (
    "Chưa có điều phối viên.\n"
    "  Tạo và chỉ định một agent làm điều phối viên (chỉ người làm được):\n"
    "    agent-hub agent create dieu-phoi --template manager --name \"Điều phối viên\"\n"
    "    agent-hub manager set dieu-phoi\n"
)
REPLAY = 6  # số tin in lại khi mở hội thoại cũ


def _err(e: Exception) -> str:
    return e.message if isinstance(e, RpcError) else str(e)


class Out:
    """Đầu ra: `rich.Console` theo `sys.stdout` HIỆN TẠI (để `patch_stdout` của prompt_toolkit bắt được)."""

    def __init__(self, color: bool, file: Any = None, width: int | None = None) -> None:
        self.color = color
        self.console = Console(file=file, force_terminal=color, no_color=not color, color_system="auto" if color else None,
                               highlight=False, markup=False, emoji=False, width=width)

    def write(self, renderable: Any) -> None:
        self.console.print(renderable)

    def clear(self) -> None:
        if self.color:
            self.console.clear()


def to_ansi(text: Text, width: int) -> str:
    buf = io.StringIO()
    Console(file=buf, force_terminal=True, color_system="256", width=max(20, width), highlight=False, markup=False,
            emoji=False).print(text, end="", no_wrap=True, overflow="ellipsis", crop=True)
    return buf.getvalue()


class ChatSession:
    """Lõi phiên chat (bất đồng bộ, không phụ thuộc giao diện nhập): mở hội thoại, gửi, nhận event, lệnh `/`."""

    def __init__(self, client: AsyncHubClient, out: Out, *, conversation_id: str | None = None, new: bool = False,
                 replay: int = REPLAY) -> None:
        self.client = client
        self.out = out
        self.want_conv = conversation_id
        self.new = new
        self.replay = replay
        self.state: V.ChatState | None = None
        self.changed = asyncio.Event()
        self._pump_task: asyncio.Task | None = None
        self._sub: Any = None
        self._inflight = 0
        self._held: list[dict[str, Any]] = []
        self.closing = False
        self.lost = False

    # ------------------------------------------------------------------ mở / đóng
    async def open(self) -> bool:
        g = await self.client.call("manager.get") or {}
        mid = g.get("agent_id")
        if not mid:
            self.out.write(Text("◆ ", style="bold magenta") + Text(NO_MANAGER))
            return False
        st = await self.client.call("hub.status") or {}
        name = mid
        try:
            ag = await self.client.call("agent.get", {"id": mid}) or {}
            name = str(ag.get("name") or mid)
        except RpcError:
            pass
        cid, title, rows = None, None, []
        if self.want_conv:
            conv = await self._find_conv(self.want_conv)
            if conv is None:
                raise RpcError(-32004, f"không có hội thoại '{self.want_conv}' trên kênh cli của điều phối viên hiện tại (/list để xem)")
            cid, title = conv["id"], conv.get("title")
        elif not self.new:
            convs = await self.client.call("manager.conversations", {"channel": V.CHANNEL}) or []
            if convs:
                cid, title = convs[0]["id"], convs[0].get("title")
        if cid and self.replay:
            rows = await self.client.call("manager.history", {"conversation_id": cid, "limit": self.replay}) or []
        self.state = V.ChatState(mid, cid, name=one_line(name, 40))
        self.state.last_seq = int(st.get("last_seq") or 0)
        self.state.epoch = st.get("epoch")
        items = V.history_items(rows)
        self.out.write(V.banner(mid, cid, title=title, resumed=len(items)))
        for it in items:
            self.out.write(V.render(it, manager_name=self.state.name))
        await self._resume(rows)
        self._pump_task = asyncio.ensure_future(self._pump())
        return True

    async def close(self) -> None:
        self.closing = True
        if self._sub is not None:
            self._sub.close()
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _resume(self, rows: list[dict[str, Any]]) -> None:
        st = self.state
        assert st is not None
        rid = V.pending_run(rows)
        run = None
        if rid:
            try:
                run = await self.client.call("run.get", {"id": rid})
            except RpcError:
                run = None
        st.resume(rows, run)
        if st.busy:
            self.out.write(V.render({"kind": "note", "text": f"điều phối viên vẫn đang xử lý tin trước (run {st.active_run})"}))

    async def _find_conv(self, cid: str) -> dict[str, Any] | None:
        convs = await self.client.call("manager.conversations", {"channel": V.CHANNEL, "limit": 500}) or []
        return next((c for c in convs if c.get("id") == cid), None)

    # ------------------------------------------------------------------ luồng event
    async def _pump(self) -> None:
        st = self.state
        assert st is not None
        delay = 0.2
        while not self.closing:
            params = {"agent_id": st.manager_id, "level": "detail", "since_seq": st.last_seq, "epoch": st.epoch}
            self._sub = subscribe(self.client, params)
            try:
                async for msg in self._sub:
                    if "subscribed" in msg:
                        if self.lost:
                            self.lost = False
                            self.out.write(V.render({"kind": "note", "text": "đã nối lại với daemon"}))
                        delay = 0.2
                        continue
                    if "end" in msg:
                        break
                    self._apply(msg)
            except (DaemonUnavailable, RpcError, OSError, ValueError):
                pass
            if self.closing:
                return
            if not self.lost:
                self.lost = True
                self.out.write(V.render({"kind": "note", "text": "mất kết nối luồng sự kiện — đang nối lại…", "tone": "warn"}))
            await asyncio.sleep(delay)
            delay = min(delay * 2, 3.0)

    def _apply(self, ev: dict[str, Any]) -> None:
        st = self.state
        assert st is not None
        if self._inflight:
            # tin đang gửi chưa có run_id/conversation_id → giữ event lại, áp sau khi `manager.chat` trả về
            self._held.append(ev)
            st.last_seq = max(st.last_seq, int(ev.get("seq") or 0))
            return
        for it in st.apply(ev):
            self.out.write(V.render(it, manager_name=st.name))
        self.changed.set()

    # ------------------------------------------------------------------ gửi / dừng
    async def send(self, text: str) -> dict[str, Any] | None:
        st = self.state
        assert st is not None
        text = text.strip()
        if not text:
            return None
        self.out.write(V.render({"kind": "user", "text": text}))
        params: dict[str, Any] = {"channel": V.CHANNEL, "text": text}
        if st.conversation_id:
            params["conversation_id"] = st.conversation_id
        self._inflight += 1
        try:
            res = await self.client.call("manager.chat", params, audit=True) or {}
        except (RpcError, DaemonUnavailable) as e:
            self.out.write(V.render({"kind": "note", "text": f"chưa gửi được: {_err(e)}", "tone": "err"}))
            return None
        finally:
            self._inflight -= 1
            held, self._held = self._held, []
        for it in st.sent(res, text):
            self.out.write(V.render(it))
        for ev in held:
            for it in st.apply(ev):
                self.out.write(V.render(it, manager_name=st.name))
        self.changed.set()
        return res

    async def stop_turn(self) -> bool:
        st = self.state
        if st is None or not st.busy or not st.active_run:
            self.out.write(V.render({"kind": "note", "text": "không có lượt nào đang chạy"}))
            return False
        try:
            await self.client.call("run.stop", {"id": st.active_run}, audit=True)
            self.out.write(V.render({"kind": "note", "text": f"đã yêu cầu dừng lượt {st.active_run}", "tone": "warn"}))
            return True
        except (RpcError, DaemonUnavailable) as e:
            self.out.write(V.render({"kind": "note", "text": f"không dừng được: {_err(e)}", "tone": "err"}))
            return False

    async def wait_idle(self, timeout: float) -> bool:
        st = self.state
        assert st is not None
        deadline = time.monotonic() + timeout
        while st.busy:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), min(left, 1.0))
            except asyncio.TimeoutError:
                pass
        return True

    # ------------------------------------------------------------------ lệnh
    async def handle(self, line: str) -> bool:
        """Một dòng người gõ. Trả False khi người muốn thoát."""
        s = line.strip()
        if not s:
            return True
        if not s.startswith("/") or s.startswith("//"):
            await self.send(s[1:] if s.startswith("//") else s)
            return True
        cmd, _, arg = s.partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        st = self.state
        assert st is not None
        try:
            if cmd in ("/quit", "/exit", "/q"):
                return False
            if cmd in ("/help", "/h", "/?"):
                self.out.write(V.help_text())
            elif cmd == "/new":
                st.switch(None)
                self.out.write(V.render({"kind": "note", "text": "hội thoại mới — tin tiếp theo sẽ mở hội thoại mới"}))
            elif cmd in ("/list", "/ls"):
                rows = await self.client.call("manager.conversations", {"channel": V.CHANNEL}) or []
                self.out.write(V.conv_list_text(rows[:20], st.conversation_id))
            elif cmd == "/open":
                if not arg:
                    self.out.write(V.render({"kind": "note", "text": "cần id hội thoại: /open <id> (xem /list)", "tone": "warn"}))
                    return True
                conv = await self._find_conv(arg)
                if conv is None:
                    self.out.write(V.render({"kind": "note", "text": f"không có hội thoại '{one_line(arg, 40)}' (xem /list)", "tone": "err"}))
                    return True
                st.switch(conv["id"])
                rows = await self.client.call("manager.history", {"conversation_id": conv["id"], "limit": max(self.replay, 10)}) or []
                items = V.history_items(rows)
                self.out.write(V.banner(st.manager_id, conv["id"], title=conv.get("title"), resumed=len(items)))
                for it in items:
                    self.out.write(V.render(it, manager_name=st.name))
                await self._resume(rows)
            elif cmd == "/history":
                if not st.conversation_id:
                    self.out.write(V.render({"kind": "note", "text": "hội thoại mới — chưa có lịch sử"}))
                    return True
                n = int(arg) if arg.isdigit() else 20
                rows = await self.client.call("manager.history", {"conversation_id": st.conversation_id, "limit": max(1, min(n, 500))}) or []
                for it in V.history_items(rows):
                    self.out.write(V.render(it, manager_name=st.name))
            elif cmd == "/stop":
                await self.stop_turn()
            elif cmd == "/clear":
                self.out.clear()
            else:
                self.out.write(V.render({"kind": "note", "text": f"lệnh không rõ {one_line(cmd, 20)} — /help (muốn gửi chữ bắt đầu bằng / thì gõ //…)", "tone": "warn"}))
        except (RpcError, DaemonUnavailable) as e:
            self.out.write(V.render({"kind": "note", "text": _err(e), "tone": "err"}))
        return True


# ---------------------------------------------------------------------- giao diện nhập
def _history_file(home: Path) -> str:
    p = Path(home) / "cli_history"
    try:
        fd = os.open(p, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(p, 0o600)
    except OSError:
        pass
    return str(p)


async def run_tty(session: ChatSession, home: Path) -> int:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.styles import Style

    class SlashCompleter(Completer):
        def get_completions(self, document: Any, complete_event: Any) -> Any:
            t = document.text_before_cursor
            if not t.startswith("/") or " " in t:
                return
            for c, desc in V.HELP:
                name = c.split()[0]
                if name.startswith(t):
                    yield Completion(name, start_position=-len(t), display_meta=desc)

    kb = KeyBindings()

    @kb.add("escape", "enter")
    def _newline(event: Any) -> None:
        event.current_buffer.insert_text("\n")

    def toolbar() -> Any:
        st = session.state
        if st is None:
            return ""
        try:
            width = os.get_terminal_size().columns
        except OSError:
            width = 100
        return ANSI(to_ansi(st.status_text(width), width))

    ps: PromptSession = PromptSession(
        history=FileHistory(_history_file(home)), completer=SlashCompleter(), complete_while_typing=True, key_bindings=kb,
        bottom_toolbar=toolbar, refresh_interval=0.12, erase_when_done=True,
        style=Style.from_dict({"bottom-toolbar": "noreverse", "bottom-toolbar.text": ""}),
    )
    last_ctrl_c = 0.0
    with patch_stdout(raw=True):
        while True:
            try:
                line = await ps.prompt_async(ANSI("\x1b[1;36m❯\x1b[0m "))
            except KeyboardInterrupt:
                st = session.state
                if st is not None and st.busy:
                    await session.stop_turn()
                    continue
                now = time.monotonic()
                if now - last_ctrl_c < 2.0:
                    break
                last_ctrl_c = now
                session.out.write(V.render({"kind": "note", "text": "Ctrl-C lần nữa (hoặc Ctrl-D) để thoát"}))
                continue
            except EOFError:
                break
            if not await session.handle(line):
                break
    return 0


async def run_plain(session: ChatSession, *, timeout: float, readline: Callable[[], str] | None = None) -> int:
    """Không phải TTY: mỗi dòng stdin → lệnh/tin; tin thì chờ trả lời trước khi đọc dòng kế. Hết giờ chờ → mã 2."""
    loop = asyncio.get_running_loop()
    rl = readline or sys.stdin.readline
    code = 0
    while True:
        line = await loop.run_in_executor(None, rl)
        if not line:
            break
        if not await session.handle(line):
            break
        st = session.state
        if st is not None and st.busy and not await session.wait_idle(timeout):
            session.out.write(V.render({"kind": "note", "text": "hết thời gian chờ trả lời", "tone": "warn"}))
            code = 2
    st = session.state
    if st is not None and st.busy and not await session.wait_idle(timeout):
        code = 2
    return code


async def repl(sock: Path, home: Path, *, conversation_id: str | None, new: bool, timeout: float, tty: bool | None = None) -> int:
    tty = sys.stdin.isatty() and sys.stdout.isatty() if tty is None else tty
    client = AsyncHubClient(sock, channel="cli")
    session = ChatSession(client, Out(color=tty and not os.environ.get("NO_COLOR")), conversation_id=conversation_id, new=new)
    if not await session.open():
        return 1
    try:
        if tty:
            return await run_tty(session, home)
        return await run_plain(session, timeout=timeout)
    finally:
        await session.close()


# ---------------------------------------------------------------------- một lần (`manager chat "<tin>"`)
async def oneshot(sock: Path, params: dict[str, Any], *, wait: bool, timeout: float,
                  on_notify: Callable[[str], None] | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Gửi một tin; `wait` → chờ `manager.reply` (kind=reply) của hội thoại. Trả `(kết quả manager.chat, payload trả lời | None khi hết giờ)`.
    Đăng ký event TRƯỚC khi gửi nên không lỡ trả lời tới nhanh."""
    client = AsyncHubClient(sock, channel="cli")
    if not wait:
        return await client.call("manager.chat", params, audit=True) or {}, None
    g = await client.call("manager.get") or {}
    mid = g.get("agent_id")
    if not mid:
        res = await client.call("manager.chat", params, audit=True)  # để daemon trả Conflict chuẩn
        return res or {}, None
    st = await client.call("hub.status") or {}
    sub = subscribe(client, {"agent_id": mid, "level": "summary", "since_seq": int(st.get("last_seq") or 0), "epoch": st.get("epoch")})
    q: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        try:
            async for msg in sub:
                await q.put(msg)
        except Exception as e:  # pragma: no cover
            await q.put({"end": {"reason": str(e)}})

    task = asyncio.ensure_future(pump())
    try:
        first = await asyncio.wait_for(q.get(), 10)
        if "subscribed" not in first:
            raise RpcError(-32603, "không đăng ký được luồng sự kiện")
        res = await client.call("manager.chat", params, audit=True) or {}
        if res.get("steered"):
            return res, None
        cid = res.get("conversation_id")
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return res, None
            try:
                ev = await asyncio.wait_for(q.get(), left)
            except asyncio.TimeoutError:
                return res, None
            if "end" in ev:
                return res, None
            if ev.get("type") != "manager.reply":
                continue
            p = ev.get("payload") or {}
            if p.get("kind") == "notify" and p.get("conversation_id") in (cid, None):
                if on_notify:
                    on_notify(str(p.get("text") or ""))
                continue
            if p.get("conversation_id") == cid and p.get("kind", "reply") == "reply":
                return res, p
    finally:
        sub.close()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


__all__ = ["ChatSession", "Out", "repl", "oneshot", "run_plain", "run_tty", "to_ansi", "NO_MANAGER"]
