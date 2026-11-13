#!/usr/bin/env python3
"""`claude setup-token` giả cho tests/test_accounts_login.py — chạy trong PTY thật, KHÔNG gọi mạng/Anthropic.

Mô phỏng hình dạng màn hình thật (spike claude 2.1.323): chữ cách nhau bằng `ESC[<n>G`, màu 24-bit, OSC tiêu đề, spinner;
link bị bẻ dòng (CRLF) và đặt sau dòng trống; nhắc "Paste code here if prompted >"; đọc code từ terminal; token bị bẻ dòng.

Điều khiển bằng env:
- FAKE_SETUP_MODE: ok (mặc định) | silent (không bao giờ in link) | exit_early (in lỗi rồi thoát trước link) | hang (nhận code nhưng không in token)
- FAKE_SETUP_OSC8=1: in link kèm hyperlink OSC 8 (như CLI thật)
- FAKE_SETUP_EXPECT: code hợp lệ (mặc định `goodcode#state123`); code khác → "OAuth error: …"
- FAKE_SETUP_TOKEN: token in ra khi code đúng
- FAKE_SETUP_ENV_OUT: đường dẫn file — ghi giá trị BROWSER nhận được (test kiểm không mở trình duyệt)
"""
import os
import sys
import time

ESC = "\x1b"
mode = os.environ.get("FAKE_SETUP_MODE", "ok")
expect = os.environ.get("FAKE_SETUP_EXPECT", "goodcode#state123")
token = os.environ.get("FAKE_SETUP_TOKEN", "sk-ant-oat01-" + "Ab3_-x" * 16 + "QQ")
env_out = os.environ.get("FAKE_SETUP_ENV_OUT")
if env_out:
    with open(env_out, "w", encoding="utf-8") as f:
        f.write(os.environ.get("BROWSER", ""))


def w(s: str) -> None:
    sys.stdout.write(s)
    sys.stdout.flush()


def words(text: str, col: int = 2, color: str = "") -> str:
    """Chữ đặt bằng ESC[<n>G thay cho dấu cách (như ConPTY/Ink)."""
    out = []
    for word in text.split(" "):
        out.append(f"{ESC}[{col}G{color}{word}")
        col += len(word) + 1
    return "".join(out) + f"{ESC}[39m\r\n"


def wrapped(s: str, width: int) -> str:
    return "\r\n".join(s[i : i + width] for i in range(0, len(s), width)) + "\r\n"


w(f"{ESC}[?25l{ESC}]0;claude{ESC}\\{ESC}[?2004h")
w(words("Welcome to Claude Code v9.9.9", color=f"{ESC}[38;2;215;119;87m") + "\r\n")
w(words("This will guide you through long-lived (1-year) auth token setup for your Claude account.") + "\r\n")
for g in "·✢*✶":
    w(f"\r\n{ESC}[1C{ESC}[1A{ESC}[38;2;255;255;255m{g}{ESC}[39m\r\n")
    time.sleep(0.05)

if mode == "exit_early":
    w(words("error: unknown option '--fake'") + "\r\n")
    sys.exit(1)
if mode == "silent":
    while True:
        time.sleep(0.2)
        w(f"{ESC}[1A{ESC}[38;2;255;255;255m*{ESC}[39m\r\n")

url = (
    "https://claude.example/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    "&response_type=code&redirect_uri=https%3A%2F%2Fplatform.example%2Foauth%2Fcode%2Fcallback&scope=user%3Ainference"
    "&code_challenge=FAKEchallenge_abcdefghijklmnopqrstuvwxyz012&code_challenge_method=S256&state=FAKEstate_0123456789"
)
w(words("Browser didn't open? Use the url below to sign in (c to copy)", color=f"{ESC}[38;2;153;153;153m") + "\r\n")
# in link thành 2 lần ghi để test việc ghép các lần đọc PTY
body = wrapped(url, 70)
link = f"{ESC}]8;id=x1;{url}\x07" if os.environ.get("FAKE_SETUP_OSC8") == "1" else ""
w(link + f"{ESC}[38;2;153;153;153m" + body[:100])
time.sleep(0.15)
w(body[100:] + f"{ESC}[39m" + (f"{ESC}]8;;\x07" if link else "") + "\r\n")
w(words("Hold Shift while selecting to use your terminal's native copy") + "\r\n")
w(words("Paste code here if prompted >"))

code = sys.stdin.readline().strip().replace(f"{ESC}[200~", "").replace(f"{ESC}[201~", "")
w("\r\n")
if mode == "hang":
    while True:
        time.sleep(0.2)
if code != expect:
    w(words("OAuth error: Invalid code. Please make sure the full code was copied.", color=f"{ESC}[38;2;255;0;0m"))
    w(words("Press Enter to retry.") + "\r\n")
    sys.stdin.readline()
    sys.exit(1)
w(words("✓ Long-lived authentication token created successfully!", color=f"{ESC}[38;2;0;255;0m") + "\r\n")
w(words("Your OAuth token (valid for 1 year):") + "\r\n")
w(f"{ESC}[38;2;255;193;7m" + wrapped(token, 45) + f"{ESC}[39m\r\n")
w(words("Store this token securely. You won't be able to see it again.") + "\r\n")
w(words("Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>") + "\r\n")
time.sleep(0.3)
sys.exit(0)
