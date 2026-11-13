"""Đăng nhập tài khoản Claude qua `claude setup-token` chạy trong PTY (hợp đồng: docs/dev-notes/plan-claude-accounts.md).

Luồng: `start_login()` spawn `claude setup-token` trong một pseudo-terminal (POSIX: `pty` stdlib, đọc master fd bằng
`loop.add_reader`; Windows: `pywinpty`/ConPTY, đọc bằng một thread) → đọc màn hình tới khi thấy link
`…/oauth/authorize?…` → `LoginHandle.url`. Người dùng mở link, đăng nhập, dán code → `submit(code)` gõ code + Enter →
đọc token `sk-ant-oat01-…` → đóng tiến trình. Mỗi handle = MỘT lần thử: lỗi (code sai, hết giờ…) thì tiến trình bị đóng,
muốn thử lại thì gọi `start_login()` lần nữa (link cũ mất hiệu lực vì `state`/PKCE đổi).

Không mở trình duyệt trên máy daemon: `claude` mở link bằng lệnh trong biến `BROWSER` nếu có (không có thì `rundll32`/
`open`/`xdg-open`) — ta đặt `BROWSER` = một lệnh vô hại (POSIX `true`; Windows `python <url>` → lỗi "không mở được file",
thoát ngay). Khi đó CLI in "Browser didn't open? Use the url below to sign in".

Hình dạng màn hình thật (spike Windows, claude 2.1.323, PTY 400 cột): chữ cách nhau bằng `ESC[<n>G` (đặt cột) thay vì dấu
cách; link nằm cả trong hyperlink OSC 8 (`ESC]8;id=…;<url>BEL`) lẫn text thường; host là `https://claude.com/cai/oauth/authorize?…`
(không phải `claude.ai`) — nên regex chỉ bám đường dẫn `/oauth/authorize?`. Sau link có dòng trống rồi
"Paste code here if prompted >". Token in một dòng riêng sau "Your OAuth token (valid for 1 year):".

KHÔNG BAO GIỜ log/đưa vào thông báo lỗi: token, code.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import logging
import os
import re
import shutil
import sys
import time
from collections.abc import Callable
from typing import Any

from cfharness.errors import HubError, SystemFailure
from cfharness.oscompat import IS_WINDOWS, exec_argv, kill_tree

log = logging.getLogger(__name__)

# Rộng để link (~330 ký tự) và token không bị bẻ dòng; vẫn chịu được bẻ dòng nếu CLI tự giới hạn cột.
PTY_COLS = 512
PTY_ROWS = 60
_RAW_CAP = 2_000_000  # ký tự; spinner vẽ lại liên tục — giữ phần cuối
_QUIET_ACCEPT_S = 1.0  # ứng viên chưa có dấu kết thúc mà màn hình im lặng chừng này → nhận

# --- làm sạch màn hình -------------------------------------------------------------------------------------------

_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_OSC8_URL_RE = re.compile(r"\x1b\]8;[^;\x07\x1b]*;(https://[^\x07\x1b\s]+)(?:\x07|\x1b\\)")
_DCS_RE = re.compile(r"\x1b[P^_X][^\x1b]*\x1b\\")
_CSI_RE = re.compile(r"\x1b\[([0-?]*)[ -/]*([@-~])")
_ESC2_RE = re.compile(r"\x1b[()*+][0-9A-Za-z]|\x1b[@-Z\\-_=>78]")
_CRLF_RE = re.compile(r"\r+\n")  # CRLF, và CR CR LF khi stdout text-mode của Windows dịch thêm LF thành CRLF
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# CSI di chuyển con trỏ theo cột (G đặt cột, C tiến, X xoá n ký tự) ~ khoảng trắng; đổi hàng (H/f/d/A/B/E/F) ~ xuống dòng.
_CSI_SPACE = set("GCX")
_CSI_NEWLINE = set("HfdABEF")


def _csi_sub(m: re.Match[str]) -> str:
    final = m.group(2)
    if final in _CSI_SPACE:
        return " "
    if final in _CSI_NEWLINE:
        return "\n"
    return ""


def clean_screen(raw: str) -> str:
    """Bỏ mã ANSI/OSC/DCS, đổi lệnh đặt cột thành dấu cách, đổi hàng thành xuống dòng, chuẩn hoá CR."""
    s = _OSC_RE.sub("", raw)
    s = _DCS_RE.sub("", s)
    s = _CSI_RE.sub(_csi_sub, s)
    s = _ESC2_RE.sub("", s)
    s = s.replace("\x1b", "")
    s = _CRLF_RE.sub("\n", s).replace("\r", "\n")
    return _CTRL_RE.sub("", s)


# --- trích link / token (chịu bẻ dòng) ------------------------------------------------------------------------------

_URL_CHARS = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_TOKEN_CHARS = re.compile(r"[A-Za-z0-9_\-]+")
_URL_START_RE = re.compile(r"https://[A-Za-z0-9.\-]+(?::\d+)?(?:/[A-Za-z0-9\-._~%]*)*?/oauth/authorize\?")
_TOKEN_START_RE = re.compile(r"sk-ant-oat01-")
_TOKEN_MIN_LEN = 40


def _take_wrapped(text: str, start: int, chars: re.Pattern[str]) -> tuple[str, bool]:
    """Đọc chuỗi bắt đầu tại `start`, nối các dòng bị bẻ tiếp theo (dòng chỉ gồm ký tự hợp lệ, không dấu cách).

    Trả (giá trị, đã_kết_thúc): kết thúc = gặp ký tự lạ trên cùng dòng, dòng trống, hoặc dòng có chữ khác.
    Chưa kết thúc = hết buffer khi chuỗi còn có thể chạy tiếp (chờ thêm dữ liệu)."""
    lines = text[start:].split("\n")
    m = chars.match(lines[0])
    value = m.group(0) if m else ""
    if m is None or m.end() < len(lines[0].rstrip()):
        return value, True
    if len(lines) == 1:
        return value, False
    for line in lines[1:-1]:
        s = line.strip()
        if not s or not chars.fullmatch(s):
            return value, True
        value += s
    last = lines[-1].strip()  # dòng cuối buffer (chưa có \n): có thể còn đang in
    if not last:
        return value, False
    if not chars.fullmatch(last):
        return value, True
    return value + last, False


def find_login_url(raw: str, *, final: bool = False) -> str | None:
    """Link `…/oauth/authorize?…` trong màn hình thô. Ưu tiên hyperlink OSC 8 (không bao giờ bị bẻ dòng).
    `final=True` (tiến trình đã thoát / màn hình im lặng): nhận cả ứng viên chưa thấy dấu kết thúc."""
    for m in _OSC8_URL_RE.finditer(raw):
        if "/oauth/authorize?" in m.group(1):
            return m.group(1)
    text = clean_screen(raw)
    m2 = _URL_START_RE.search(text)
    if not m2:
        return None
    url, done = _take_wrapped(text, m2.start(), _URL_CHARS)
    if not (done or final):
        return None
    return url if len(url) > m2.end() - m2.start() else None


def find_token(raw: str, *, final: bool = False) -> str | None:
    text = clean_screen(raw)
    m = _TOKEN_START_RE.search(text)
    if not m:
        return None
    tok, done = _take_wrapped(text, m.start(), _TOKEN_CHARS)
    if not (done or final) or len(tok) < _TOKEN_MIN_LEN:
        return None
    return tok


_ERROR_LINE_RE = re.compile(r"(OAuth error|Invalid code|\bError\b|\berror:|failed|Failed|expired|denied)")


def find_error_line(raw: str) -> str | None:
    for line in clean_screen(raw).split("\n"):
        s = " ".join(line.split())
        if s and _ERROR_LINE_RE.search(s):
            return s
    return None


def _tail_lines(raw: str, n: int = 4) -> str:
    lines = [" ".join(x.split()) for x in clean_screen(raw).split("\n")]
    lines = [x for x in lines if x and not set(x) <= set("·✢*✶✻✽.░▓█▄ ")]
    return " | ".join(lines[-n:])[:400]


def _redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "…")
    return text


# --- PTY theo nền tảng -----------------------------------------------------------------------------------------------


def _noop_browser() -> str:
    if IS_WINDOWS:
        return sys.executable  # `python <url>` → "can't open file", thoát ngay, không mở gì
    return shutil.which("true") or "/bin/true"


def _resolve_binary(binary: str, env: dict[str, str]) -> str | None:
    if os.path.sep in binary or (os.altsep and os.altsep in binary):
        return binary if os.path.isfile(binary) else None
    return shutil.which(binary, path=env.get("PATH") or env.get("Path") or os.environ.get("PATH"))


class _Pty:
    """Một tiến trình trong PTY; `on_data(str)` / `on_eof()` được gọi TRÊN event loop."""

    pid: int | None = None

    def write(self, data: str) -> None:  # pragma: no cover — interface
        raise NotImplementedError

    def alive(self) -> bool:  # pragma: no cover
        raise NotImplementedError

    async def kill(self) -> None:  # pragma: no cover
        raise NotImplementedError


class _PosixPty(_Pty):  # pragma: no cover — POSIX
    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.master: int | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._closed = False

    async def start(self, argv: list[str], env: dict[str, str], on_data: Callable[[str], None], on_eof: Callable[[], None]) -> None:
        import fcntl
        import pty
        import struct
        import termios

        master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0))
            self.proc = await asyncio.create_subprocess_exec(
                *argv, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True, close_fds=True
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.pid = self.proc.pid
        self.master = master
        os.set_blocking(master, False)
        self.loop = asyncio.get_running_loop()
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")

        def _readable() -> None:
            try:
                data = os.read(master, 65536)
            except BlockingIOError:
                return
            except OSError:  # EIO: phía slave đã đóng (tiến trình thoát)
                data = b""
            if not data:
                self._remove_reader()
                on_eof()
                return
            on_data(dec.decode(data))

        self.loop.add_reader(master, _readable)

    def _remove_reader(self) -> None:
        if self.loop is not None and self.master is not None:
            with contextlib.suppress(Exception):
                self.loop.remove_reader(self.master)

    def write(self, data: str) -> None:
        if self.master is None or self._closed:
            raise OSError("pty đã đóng")
        buf = data.encode("utf-8")
        while buf:
            try:
                n = os.write(self.master, buf)
            except BlockingIOError:
                time.sleep(0.01)
                continue
            buf = buf[n:]

    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def kill(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._remove_reader()
        if self.proc is not None and self.proc.returncode is None:
            kill_tree(self.proc.pid)
            with contextlib.suppress(ProcessLookupError):
                self.proc.kill()
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                await asyncio.wait_for(self.proc.wait(), 5)
        if self.master is not None:
            with contextlib.suppress(OSError):
                os.close(self.master)
            self.master = None


class _WinPty(_Pty):  # pragma: no cover — Windows
    def __init__(self) -> None:
        self.proc: Any = None
        self._closed = False

    async def start(self, argv: list[str], env: dict[str, str], on_data: Callable[[str], None], on_eof: Callable[[], None]) -> None:
        import threading

        from winpty import PtyProcess  # pywinpty (dependency chỉ win32)

        loop = asyncio.get_running_loop()
        self.proc = await asyncio.to_thread(PtyProcess.spawn, argv, env=env, dimensions=(PTY_ROWS, PTY_COLS))
        self.pid = self.proc.pid
        proc = self.proc

        def _reader() -> None:
            while True:
                try:
                    data = proc.read(65536)
                except (EOFError, OSError, ValueError):
                    break
                except Exception:  # noqa: BLE001 — thread nền không được chết ồn ào
                    break
                if data:
                    with contextlib.suppress(RuntimeError):  # loop đã đóng
                        loop.call_soon_threadsafe(on_data, data)
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(on_eof)

        threading.Thread(target=_reader, name="claude-login-pty", daemon=True).start()

    def write(self, data: str) -> None:
        if self.proc is None or self._closed:
            raise OSError("pty đã đóng")
        self.proc.write(data)

    def alive(self) -> bool:
        if self.proc is None or self._closed:
            return False
        try:
            return bool(self.proc.isalive())
        except Exception:  # noqa: BLE001
            return False

    async def kill(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self.proc
        if proc is None:
            return

        def _kill() -> None:
            with contextlib.suppress(Exception):
                if proc.isalive():
                    kill_tree(proc.pid)
            with contextlib.suppress(Exception):
                proc.close(force=True)

        await asyncio.to_thread(_kill)


# --- handle ----------------------------------------------------------------------------------------------------------


class LoginHandle:
    """Một phiên `claude setup-token` đang chờ code. `url` = link đăng nhập (đưa cho người dùng mở)."""

    url: str

    def __init__(self, pty: _Pty) -> None:
        self.url = ""
        self._pty = pty
        self._raw = ""
        self._mark = 0  # vị trí trong _raw lúc submit (chỉ tìm token/lỗi sau đó)
        self._eof = False
        self._changed = asyncio.Event()
        self._last_data = time.monotonic()
        self._lock = asyncio.Lock()
        self._closed = False
        self._used = False

    # gọi trên event loop từ _Pty
    def _on_data(self, data: str) -> None:
        self._raw += data
        if len(self._raw) > _RAW_CAP:
            cut = len(self._raw) - _RAW_CAP // 2
            self._raw = self._raw[cut:]
            self._mark = max(0, self._mark - cut)
        self._last_data = time.monotonic()
        self._changed.set()

    def _on_eof(self) -> None:
        self._eof = True
        self._changed.set()

    async def _wait_change(self, deadline: float) -> bool:
        """Chờ dữ liệu mới tới deadline hoặc tối đa _QUIET_ACCEPT_S; True nếu có thay đổi."""
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        self._changed.clear()
        try:
            await asyncio.wait_for(self._changed.wait(), min(left, _QUIET_ACCEPT_S))
            return True
        except TimeoutError:
            return False

    async def _wait_url(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            quiet = time.monotonic() - self._last_data >= _QUIET_ACCEPT_S
            url = find_login_url(self._raw, final=self._eof or quiet)
            if url:
                return url
            if self._eof:
                tail = _tail_lines(self._raw)
                raise HubError(
                    "`claude setup-token` đã thoát mà không đưa link đăng nhập"
                    + (f" (màn hình: {tail})" if tail else "")
                    + ". Kiểm tra `claude --version` và kết nối mạng rồi thử lại."
                )
            if time.monotonic() >= deadline:
                raise SystemFailure(
                    f"Hết {timeout:g} giây mà `claude setup-token` chưa đưa link đăng nhập. Thử lại sau, hoặc dùng "
                    "\"Thêm bằng token\" (chạy `claude setup-token` trên máy khác rồi dán token)."
                )
            await self._wait_change(deadline)

    async def submit(self, code: str, timeout: float = 60) -> str:
        """Gõ code người dùng dán vào CLI, chờ token. Trả token `sk-ant-oat01-…`. Xong (thành công hay lỗi) đều đóng tiến trình."""
        code = (code or "").strip()
        if not code:
            raise HubError("Chưa nhập code. Dán code trang đăng nhập Claude hiển thị sau khi bạn đồng ý.")
        if any(c.isspace() for c in code) or len(code) > 4096:
            raise HubError("Code không hợp lệ (có khoảng trắng hoặc quá dài). Chép lại nguyên code từ trang đăng nhập.")
        async with self._lock:
            if self._closed or self._used:
                raise HubError("Phiên đăng nhập này đã kết thúc. Bấm đăng nhập lại để lấy link mới.")
            if self._eof or not self._pty.alive():
                await self.close()
                raise HubError("`claude setup-token` đã thoát trước khi nhận code. Bấm đăng nhập lại để lấy link mới.")
            self._used = True
            self._mark = len(self._raw)
            try:
                return await self._submit(code, timeout)
            finally:
                await self.close()

    async def _submit(self, code: str, timeout: float) -> str:
        try:
            # Gõ code thành MỘT lần ghi (Ink nhận cả chuỗi như dán, không hiểu thành phím tắt), rồi Enter riêng.
            self._pty.write(code)
            await asyncio.sleep(0.3)
            self._pty.write("\r")
        except OSError as e:
            raise HubError("Không gửi được code vào `claude setup-token` (tiến trình đã đóng). Bấm đăng nhập lại.") from e
        deadline = time.monotonic() + timeout
        err_seen_at: float | None = None
        while True:
            after = self._raw[self._mark :]
            quiet = time.monotonic() - self._last_data >= _QUIET_ACCEPT_S
            tok = find_token(after, final=self._eof or quiet)
            if tok:
                log.info("claude setup-token: đã nhận token (%d ký tự)", len(tok))
                return tok
            err = find_error_line(_redact(after, code))
            if err:
                now = time.monotonic()
                if err_seen_at is None:
                    err_seen_at = now
                elif now - err_seen_at >= 0.5 or self._eof:
                    err = _redact(find_error_line(_redact(self._raw[self._mark :], code)) or err, code)
                    raise HubError(
                        f"Claude không nhận code: {err[:300]}. Kiểm tra đã chép ĐỦ code (gồm cả phần sau dấu #), "
                        "code chỉ dùng được một lần và trong vài phút — bấm đăng nhập lại để lấy link mới."
                    )
            if self._eof:
                tail = _redact(_tail_lines(after), code)
                raise HubError(
                    "`claude setup-token` đã thoát mà không đưa token"
                    + (f" (màn hình: {tail})" if tail else "")
                    + ". Bấm đăng nhập lại để lấy link mới."
                )
            if time.monotonic() >= deadline:
                raise SystemFailure(
                    f"Hết {timeout:g} giây chờ token sau khi gửi code. Code có thể đã hết hạn hoặc mạng chậm — bấm đăng nhập lại."
                )
            await self._wait_change(deadline)

    async def close(self) -> None:
        """Giết cả cây tiến trình `claude setup-token`; gọi nhiều lần không sao."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._pty.kill()
        self._raw = ""  # có thể chứa code/token đã echo — không giữ lại


async def start_login(binary: str, env: dict[str, str], timeout: float = 45) -> LoginHandle:
    """Chạy `<binary> setup-token` trong PTY, chờ link đăng nhập (≤ `timeout` giây) rồi trả handle.

    `env` = env ĐẦY ĐỦ của tiến trình con (vd `runtime.claude_cli.child_env(...)`); rỗng → env của daemon. Luôn đặt
    `BROWSER` thành lệnh vô hại để CLI không mở trình duyệt trên máy chạy daemon."""
    env = dict(env or os.environ)
    resolved = _resolve_binary(binary, env)
    if resolved is None:
        raise HubError(
            f"Không tìm thấy lệnh Claude Code `{binary}` trên máy chạy hub. Cài Claude Code "
            "(https://docs.claude.com/claude-code) hoặc sửa `runtime.claude.binary` trong Cài đặt."
        )
    env["BROWSER"] = _noop_browser()
    env.pop("CLAUDECODE", None)
    argv = exec_argv([resolved, "setup-token"])
    pty: _Pty
    if IS_WINDOWS:  # pragma: no cover — Windows
        try:
            import winpty  # noqa: F401
        except ImportError as e:
            raise SystemFailure("Thiếu gói `pywinpty` nên không mở được terminal ảo cho `claude setup-token`. Chạy `uv sync`.") from e
        pty = _WinPty()
    else:
        pty = _PosixPty()
    handle = LoginHandle(pty)
    try:
        await pty.start(argv, env, handle._on_data, handle._on_eof)  # type: ignore[attr-defined]
    except (FileNotFoundError, PermissionError) as e:
        raise HubError(f"Không chạy được `{binary} setup-token`: {type(e).__name__}. Kiểm tra cài đặt Claude Code.") from e
    except OSError as e:
        raise SystemFailure(f"Không mở được terminal ảo cho `claude setup-token`: {e}") from e
    try:
        handle.url = await handle._wait_url(timeout)
    except BaseException:
        await handle.close()
        raise
    log.info("claude setup-token: đã có link đăng nhập (pid %s)", pty.pid)
    return handle


__all__ = ["PTY_COLS", "LoginHandle", "clean_screen", "find_login_url", "find_token", "start_login"]
