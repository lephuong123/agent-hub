#!/usr/bin/env python3
"""`claude auth status|login|logout` giả (KHÔNG mạng) cho tests/test_claudeai_login.py.

Trạng thái đăng nhập claude.ai = file FAKE_AUTH_STATE (có = đã đăng nhập). `auth login` chạy luồng PTY của
fake_claude_setup_token.py ở chế độ signin (ghi file khi code đúng). FAKE_AUTH_TOKEN_SEEN=<file>: ghi lại nếu nhận
CLAUDE_CODE_OAUTH_TOKEN trong env (đăng nhập/kiểm phiên claude.ai không bao giờ được mang token hub).
"""
import json
import os
import runpy
import sys
from pathlib import Path

state = os.environ["FAKE_AUTH_STATE"]
seen = os.environ.get("FAKE_AUTH_TOKEN_SEEN")
if seen and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
    Path(seen).write_text("token", encoding="utf-8")
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    ok = os.path.exists(state)
    print(json.dumps({"loggedIn": ok, **({"authMethod": "claude.ai", "email": "user@example.com", "orgName": "Org",
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
