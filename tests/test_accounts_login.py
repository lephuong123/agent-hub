"""Đăng nhập Claude qua PTY (`accounts/login.py`): bộ trích link/token thuần + chạy `start_login` qua PTY THẬT của nền tảng
hiện tại với `tests/fake_claude_setup_token.py` (không gọi `claude` thật, không mạng)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from cfharness.accounts import login as L
from cfharness.errors import HubError, SystemFailure

FAKE = str(Path(__file__).with_name("fake_claude_setup_token.py"))
URL = (
    "https://claude.example/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    "&response_type=code&redirect_uri=https%3A%2F%2Fplatform.example%2Foauth%2Fcode%2Fcallback&scope=user%3Ainference"
    "&code_challenge=FAKEchallenge_abcdefghijklmnopqrstuvwxyz012&code_challenge_method=S256&state=FAKEstate_0123456789"
)
TOKEN = "sk-ant-oat01-" + "Ab3_-x" * 16 + "QQ"

# Hình dạng màn hình THẬT (spike Windows/ConPTY, claude 2.1.323) — rút gọn, state/challenge thay bằng giá trị giả.
REAL_URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&response_type=code"
    "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=user%3Ainference"
    "&code_challenge=CH_xyz&code_challenge_method=S256&state=ST_abc"
)
REAL_SCREEN = (
    "\x1b[1t\x1b[c\x1b[?1004h\x1b[?9001h\x1b]0;claude\x1b\\\x1b7\x1b[r\x1b8\x1b[?25h\x1b[?25l\x1b[?2004h"
    "\x1b[38;2;215;119;87mWelcome\x1b[9Gto\x1b[12GClaude\x1b[19GCode\x1b[24G\x1b[38;2;153;153;153mv2.1.323\x1b[39m\r\n"
    "\x1b[2G\x1b[38;2;255;255;255m·\x1b[4G\x1b[39mOpening\x1b[12Gbrowser\x1b[20Gto\x1b[23Gsign\x1b[28Gin…\r\n"
    "\x1b[?2026l\x1b[>0q\x1b[?u\x1b]7501;?\x07\x1b[c\x1b[?2026h\r\n\x1b[1C\x1b[1A\x1b[38;2;255;255;255m✢\x1b[39m\r\n"
    "\x1b[1C\x1b[1A\x1b[38;2;153;153;153mBrowser didn't open? Use the url below to sign in (c to copy)\x1b[39m\r\n\r\n"
    f"\x1b]8;id=m5f04r;{REAL_URL}\x07\x1b[38;2;153;153;153m{REAL_URL}\x1b[39m\x1b]8;;\x07\r\n\r\n"
    "\x1b[2G\x1b[38;2;153;153;153mHold\x1b[7GShift\x1b[13Gwhile\x1b[19Gselecting\x1b[39m\r\n\r\n"
    "\x1b[2GPaste\x1b[8Gcode\x1b[13Ghere\x1b[18Gif\x1b[21Gprompted\x1b[30G>\r\n\x1b[?2026l"
)


# --- thuần -----------------------------------------------------------------------------------------------------------


def test_clean_screen_cursor_columns_become_spaces():
    s = L.clean_screen("\x1b[2GPaste\x1b[8Gcode\x1b[13Ghere\x1b]0;t\x07\r\n\x1b[1mX\x1b[22m")
    assert s.split() == ["Paste", "code", "here", "X"]
    assert "\x1b" not in s


def test_real_screen_url_from_osc8_and_from_text():
    assert L.find_login_url(REAL_SCREEN) == REAL_URL
    no_osc8 = L._OSC8_URL_RE.sub("", REAL_SCREEN)
    assert "\x1b]8;" not in no_osc8.replace("\x1b]8;;", "")
    assert L.find_login_url(no_osc8) == REAL_URL


def test_wrapped_url_waits_for_terminator_then_joins():
    parts = [URL[i : i + 50] for i in range(0, len(URL), 50)]
    partial = "\x1b[39mUse the url below\r\n\r\n" + "\r\n".join(parts[:2])
    assert L.find_login_url(partial) is None  # còn có thể đang in
    assert L.find_login_url(partial, final=True) == "".join(parts[:2])
    full = "\x1b[39mUse the url below\r\n\r\n" + "\r\n".join(parts) + "\r\n\r\n\x1b[2GPaste\x1b[8Gcode"
    assert L.find_login_url(full) == URL


def test_wrapped_url_stops_at_text_line_and_other_host():
    raw = "  https://other.host/x/oauth/authorize?a=1&b=2\r\n  more&c=3\r\nHold Shift while\r\n"
    assert L.find_login_url(raw) == "https://other.host/x/oauth/authorize?a=1&b=2more&c=3"
    assert L.find_login_url("https://claude.ai/login?x=1\r\n\r\n") is None


def test_token_wrapped_and_min_length():
    raw = "Your OAuth token (valid for 1 year):\r\n\r\n\x1b[33m" + TOKEN[:30] + "\r\n" + TOKEN[30:] + "\x1b[39m\r\n\r\nStore this token"
    assert L.find_token(raw) == TOKEN
    assert L.find_token("sk-ant-oat01-short\r\n\r\n") is None
    assert L.find_token("sk-ant-oat01-" + "a" * 50) is None  # chưa kết thúc dòng → chờ
    assert L.find_token("sk-ant-oat01-" + "a" * 50, final=True) == "sk-ant-oat01-" + "a" * 50


# --- PTY thật ----------------------------------------------------------------------------------------------------------


def _pty_available() -> bool:
    try:
        if sys.platform == "win32":
            import winpty  # noqa: F401
        else:
            import pty  # noqa: F401
            import termios  # noqa: F401
    except ImportError:
        return False
    return True


needs_pty = pytest.mark.skipif(not _pty_available(), reason="không có PTY trên nền tảng này")


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    for k in [k for k in env if k.startswith("FAKE_SETUP_")]:
        env.pop(k)
    env["FAKE_SETUP_ENV_OUT"] = str(tmp_path / "browser.txt")
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra)
    return env


@needs_pty
@pytest.mark.parametrize("osc8", ["0", "1"])
async def test_login_ok_through_real_pty(tmp_path, caplog, osc8):
    caplog.set_level("DEBUG")
    h = await L.start_login(FAKE, _env(tmp_path, FAKE_SETUP_OSC8=osc8), timeout=30)
    try:
        assert h.url == URL
        browser = (tmp_path / "browser.txt").read_text(encoding="utf-8")
        assert browser and "chrome" not in browser.lower() and "rundll32" not in browser.lower()
        assert browser == L._noop_browser()
        tok = await h.submit("goodcode#state123", timeout=30)
        assert tok == TOKEN
    finally:
        await h.close()
    await h.close()  # idempotent
    assert not h._pty.alive()
    assert TOKEN not in caplog.text and "goodcode" not in caplog.text
    with pytest.raises(HubError, match="đã kết thúc"):
        await h.submit("goodcode#state123")


@needs_pty
async def test_login_bad_code_error_without_echoing_code(tmp_path):
    h = await L.start_login(FAKE, _env(tmp_path), timeout=30)
    with pytest.raises(HubError) as ei:
        await h.submit("wrongcode#zzz", timeout=30)
    msg = ei.value.message
    assert "không nhận code" in msg and "Invalid code" in msg
    assert "wrongcode" not in msg
    assert not h._pty.alive()
    await h.close()


@needs_pty
async def test_login_timeout_waiting_for_url(tmp_path):
    with pytest.raises(SystemFailure, match="chưa đưa link"):
        await L.start_login(FAKE, _env(tmp_path, FAKE_SETUP_MODE="silent"), timeout=2)


@needs_pty
async def test_login_cli_exits_before_url(tmp_path):
    with pytest.raises(HubError, match="thoát mà không đưa link") as ei:
        await L.start_login(FAKE, _env(tmp_path, FAKE_SETUP_MODE="exit_early"), timeout=30)
    assert "unknown option" in ei.value.message


@needs_pty
async def test_login_timeout_waiting_for_token(tmp_path):
    h = await L.start_login(FAKE, _env(tmp_path, FAKE_SETUP_MODE="hang"), timeout=30)
    with pytest.raises(SystemFailure, match="chờ token"):
        await h.submit("goodcode#state123", timeout=2)
    assert not h._pty.alive()


@needs_pty
async def test_close_without_submit_kills_process(tmp_path):
    h = await L.start_login(FAKE, _env(tmp_path), timeout=30)
    assert h._pty.alive()
    await h.close()
    await h.close()
    assert not h._pty.alive()
    with pytest.raises(HubError):
        await h.submit("goodcode#state123")


async def test_empty_code_rejected_before_cli():
    h = L.LoginHandle(L._Pty())
    h._closed = True
    with pytest.raises(HubError, match="Chưa nhập code"):
        await h.submit("   ")


async def test_cli_missing(tmp_path):
    with pytest.raises(HubError, match="Không tìm thấy lệnh Claude Code"):
        await L.start_login(str(tmp_path / "no-such-claude"), dict(os.environ))
    with pytest.raises(HubError, match="Không tìm thấy"):
        await L.start_login("claude-does-not-exist-xyz", dict(os.environ))
