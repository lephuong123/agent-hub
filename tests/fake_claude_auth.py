#!/usr/bin/env python3
"""`claude auth status|login|logout` giả (KHÔNG mạng) cho tests/test_claudeai_login.py, tests/test_account_connectors.py.

Trạng thái đăng nhập claude.ai = file (có = đã đăng nhập): cấu hình mặc định → FAKE_AUTH_STATE; có CLAUDE_CONFIG_DIR →
`<CLAUDE_CONFIG_DIR>/.fake_login` (mỗi thư mục cấu hình một phiên riêng, như Keychain theo thư mục của claude thật). Email của
phiên = FAKE_AUTH_EMAIL (mặc định user@example.com), phiên theo thư mục: `<tên thư mục>@example.com`.
`auth login` chạy luồng PTY của fake_claude_setup_token.py ở chế độ signin (ghi file khi code đúng).
FAKE_AUTH_TOKEN_SEEN=<file>: ghi lại nếu nhận CLAUDE_CODE_OAUTH_TOKEN trong env (đăng nhập/kiểm phiên claude.ai không bao giờ
được mang token hub). FAKE_AUTH_LOG=<file>: thêm một dòng `<lệnh> <CLAUDE_CONFIG_DIR|->` mỗi lần gọi.
"""
import json
import os
import runpy
import sys
from pathlib import Path

cdir = os.environ.get("CLAUDE_CONFIG_DIR")
state = os.path.join(cdir, ".fake_login") if cdir else os.environ["FAKE_AUTH_STATE"]
email = f"{os.path.basename(cdir.rstrip('/'))}@example.com" if cdir else os.environ.get("FAKE_AUTH_EMAIL", "user@example.com")
seen = os.environ.get("FAKE_AUTH_TOKEN_SEEN")
if seen and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
    Path(seen).write_text("token", encoding="utf-8")
args = sys.argv[1:]
if os.environ.get("FAKE_AUTH_LOG"):
    with open(os.environ["FAKE_AUTH_LOG"], "a", encoding="utf-8") as f:
        f.write(f"{' '.join(args[:2])} {cdir or '-'}\n")
if args[:2] == ["auth", "status"]:
    ok = os.path.exists(state)
    print(json.dumps({"loggedIn": ok, **({"authMethod": "claude.ai", "email": email, "orgName": "Org",
                                          "subscriptionType": "team"} if ok else {})}))
elif args[:2] == ["auth", "logout"]:
    if os.path.exists(state):
        os.remove(state)
    print("Successfully logged out")
elif args[:2] == ["auth", "login"]:
    os.environ["FAKE_SETUP_SIGNIN"] = state
    sys.argv = [str(Path(__file__).with_name("fake_claude_setup_token.py"))]
    runpy.run_path(sys.argv[0], run_name="__main__")
else:
    print(f"fake: unsupported {args}", file=sys.stderr)
    sys.exit(2)
