#!/usr/bin/env python3
"""`claude mcp add-json|remove|list|login|logout` giả (KHÔNG mạng) cho tests/test_mcp_claude_login.py.

Trạng thái theo thư mục cấu hình (`CLAUDE_CONFIG_DIR` — bắt buộc; hub đặt cả cho cấu hình mặc định khi test): `<dir>/.claude.json`
(mcpServers user-scope, như claude thật) + `<dir>/.fake_mcp_auth.json` ({tên: true} = đã đăng nhập, như mcpOAuth trong Keychain theo thư mục).

`mcp list`: server có url chứa "oauth" cần đăng nhập (`! Needs authentication` tới khi login), url chứa "dead" → `✘ Failed to connect`,
còn lại `✓ Connected` — dòng in cả url/lệnh (như claude thật) để test kiểm hub không làm lộ.
`mcp login <tên> --no-browser`: chạy trong PTY — in link authorize (OSC 8 + chữ), nhắc "Or paste the redirect URL here:", đọc dòng; URL có
`state=FAKESTATE` và `code=` → lưu đăng nhập, in "Authentication successful", thoát 0; URL khác → "That doesn't look like a redirect URL…"
rồi hỏi lại (như claude 2.1.329). FAKE_MCP_LOGIN_MODE=nosave: báo thành công nhưng KHÔNG lưu; exit_early: in lỗi rồi thoát trước link.

FAKE_MCP_LOG=<file>: mỗi lần gọi thêm một dòng JSON {argv, config_dir, oauth_token, api_key, claudecode, cwd, stdin}.
"""
import json
import os
import sys
from pathlib import Path

ESC = "\x1b"
args = sys.argv[1:]
cdir = os.environ.get("CLAUDE_CONFIG_DIR")
stdin_data = None
if args[:2] == ["mcp", "add-json"] and args[-1:] == ["-"]:
    if os.environ.get("CLAUDECODE"):
        print("Reading the JSON from stdin is refused inside a Claude Code session", file=sys.stderr)
        sys.exit(1)
    stdin_data = sys.stdin.read()
if os.environ.get("FAKE_MCP_LOG"):
    with open(os.environ["FAKE_MCP_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": args, "config_dir": cdir, "oauth_token": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")),
                            "api_key": bool(os.environ.get("ANTHROPIC_API_KEY")), "claudecode": bool(os.environ.get("CLAUDECODE")),
                            "cwd": os.getcwd(), "stdin": stdin_data}) + "\n")
if not cdir:
    print("fake: CLAUDE_CONFIG_DIR bắt buộc", file=sys.stderr)
    sys.exit(3)
cfg_path = Path(cdir) / ".claude.json"
auth_path = Path(cdir) / ".fake_mcp_auth.json"


def load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(p: Path, d: dict) -> None:
    p.write_text(json.dumps(d, indent=2), encoding="utf-8")


cfg = load(cfg_path)
servers = cfg.setdefault("mcpServers", {})
auth = load(auth_path)

if args[:2] == ["mcp", "add-json"]:
    name = args[-2]
    if not all(c.isalnum() or c in "-_" for c in name):
        print(f"Invalid name {name}. Names can only contain letters, numbers, hyphens, and underscores.", file=sys.stderr)
        sys.exit(1)
    try:
        entry = json.loads(stdin_data or "")
    except ValueError:
        print("Invalid configuration: not a JSON object. Check its quotes, commas and braces.", file=sys.stderr)
        sys.exit(1)
    if name in servers:
        print(f"MCP server {name} already exists in user config", file=sys.stderr)
        sys.exit(1)
    if "fail" in name:
        print(f"Invalid configuration: url {entry.get('url')} header {json.dumps(entry.get('headers'))}", file=sys.stderr)
        sys.exit(1)
    servers[name] = entry
    save(cfg_path, cfg)
    print(f"Added {entry.get('type')} MCP server {name} to user config")
elif args[:2] == ["mcp", "remove"]:
    name = args[-1]
    if name not in servers:
        print(f'No MCP server named "{name}" in user scope', file=sys.stderr)
        sys.exit(1)
    del servers[name]
    save(cfg_path, cfg)
    print(f"Removed MCP server {name} from user config\nFile modified: {cfg_path}")
elif args[:2] == ["mcp", "list"]:
    print("Checking MCP server health…\n")
    for name, e in servers.items():
        where = e.get("url") or " ".join([e.get("command", "")] + list(e.get("args") or []))
        kind = (e.get("type") or "stdio").upper()
        if "dead" in str(e.get("url") or ""):
            st = "✘ Failed to connect — ECONNREFUSED"
        elif "oauth" in str(e.get("url") or "") and not auth.get(name):
            st = "! Needs authentication"
        else:
            st = "✓ Connected"
        print(f"{name}: {where} ({kind}) - {st}")
elif args[:2] == ["mcp", "logout"]:
    name = args[2]
    if name not in servers:
        print(f'No MCP server named "{name}". Configured servers: {", ".join(servers)}', file=sys.stderr)
        sys.exit(1)
    auth.pop(name, None)
    save(auth_path, auth)
    print(f'Signed out of "{name}". Run `claude mcp login {name}` to authenticate again.')
elif args[:2] == ["mcp", "login"]:
    name = args[2]
    mode = os.environ.get("FAKE_MCP_LOGIN_MODE", "ok")

    def w(s: str) -> None:
        sys.stdout.write(s)
        sys.stdout.flush()

    w(f'Starting authentication for "{name}"…\r\n')
    if mode == "exit_early" or name not in servers:
        w(f"Error: cannot authenticate {name} at https://leak.example/?k=SECRETQUERY\r\n")
        sys.exit(1)
    url = f"https://auth.example/v1/authorize?response_type=code&client_id=abc&redirect_uri=http%3A%2F%2Flocalhost%3A51814%2Fcallback&state=FAKESTATE"
    w("Visit this URL to authorize:\r\n  " + f"{ESC}]8;;{url}\x07{ESC}[94m{url}{ESC}[39m{ESC}]8;;\x07\r\n\r\n")
    w("Waiting for authorization… (^C to cancel)\r\n")
    while True:
        w(f"{ESC}[1G{ESC}[0JOr paste the redirect URL here: {ESC}[33G")
        line = sys.stdin.readline()
        if not line:
            sys.exit(1)
        got = line.strip().replace(f"{ESC}[200~", "").replace(f"{ESC}[201~", "")
        if "state=FAKESTATE" in got and "code=" in got:
            if mode != "nosave":
                auth[name] = True
                save(auth_path, auth)
            w(f'\r\nAuthentication successful. Connected to "{name}".\r\n')
            sys.exit(0)
        w("\r\nThat doesn't look like a redirect URL for this sign-in — paste the full address from the browser page this sign-in opened.\r\n")
else:
    print(f"fake: unsupported {args}", file=sys.stderr)
    sys.exit(2)
