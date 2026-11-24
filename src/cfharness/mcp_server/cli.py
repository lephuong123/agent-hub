"""Lệnh CLI của luồng A: `cfharness mcp …`, `cfharness secret …`, `cfharness audit …`, `cfharness feedback …`.

Đăng ký từ `cli.build_parser` qua `add_parsers(sub, common)`. Mọi lệnh là client của daemon (RPC), trừ
`mcp serve` (tiến trình MCP mỏng), `mcp snippet`/`mcp token` (chỉ in cấu hình/đọc file token cục bộ).
KHÔNG sửa cấu hình Claude Code của người dùng — `mcp snippet` chỉ IN lệnh/JSON để người tự thêm.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

from ..config import MCP_HTTP_DEFAULT_PORT

EXIT_OK, EXIT_USER, EXIT_SYS = 0, 1, 2


def _eprint(*a: Any) -> None:
    print(*a, file=sys.stderr)


def _kv_list(values: list[str] | None, what: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for v in values or []:
        if "=" in v:
            k, val = v.split("=", 1)
        elif ":" in v and what == "header":
            k, val = v.split(":", 1)
        else:
            raise ValueError(f"--{what} cần dạng KHOÁ=GIÁ_TRỊ: {v}")
        out[k.strip()] = val.strip() if val.strip() != "" else None
    return out


def _split(values: list[str] | None) -> list[str] | None:
    if values is None:
        return None
    out: list[str] = []
    for v in values:
        out.extend(x.strip() for x in v.split(",") if x.strip())
    return out


# ============================================================================ caller cho MCP server
class DaemonCaller:
    """Gọi RPC daemon; tự khởi động daemon nếu chưa chạy (FR-MS-01a). An toàn đa luồng (mỗi lần một socket)."""

    def __init__(self, paths: Any) -> None:
        from ..rpc import RpcClient

        self.paths = paths
        self.client = RpcClient(paths.socket, timeout=120, kind="mcp")
        self._lock = threading.Lock()
        self._started = False

    def __call__(self, method: str, params: dict[str, Any]) -> Any:
        from ..rpc import DaemonUnavailable, RpcClient
        from ..trace import outgoing

        params = outgoing(params)  # L2: một req_id cho lời gọi tool — lần thử lại sau khi tự khởi động daemon dùng CÙNG mã
        try:
            return RpcClient(self.paths.socket, timeout=120, kind="mcp").call(method, params)  # L3: token riêng của MCP server
        except DaemonUnavailable:
            with self._lock:
                if not self._started:
                    from ..daemon import spawn_background

                    spawn_background(self.paths)
                    self._started = True
            return RpcClient(self.paths.socket, timeout=120, kind="mcp").call(method, params)


# ============================================================================ mcp serve / snippet / token
def _cfharness_command() -> list[str]:
    exe = shutil.which("cfharness")
    if exe:
        return [str(Path(exe).resolve())]
    return [sys.executable, "-m", "cfharness"]


def _logs_settings(ctx: Any) -> dict[str, Any]:
    from ..logs import logs_settings

    return logs_settings(_config_get(ctx, "logs", {}))


def cmd_mcp_serve(ctx: Any) -> int:
    import logging

    a = ctx.args
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    for h in logging.getLogger().handlers:
        h.setLevel(logging.WARNING)  # stderr (claude hiển thị) giữ như cũ: chỉ WARNING trở lên
    from ..logs import setup_process_logging

    # L1: nhiều `mcp serve` chạy cùng lúc → một file chung `mcp.jsonl` (append, xoay lúc mở); không ghi được (sandbox…) → bỏ qua
    setup_process_logging(ctx.paths.logs_dir, "mcp", "mcp.jsonl", _logs_settings(ctx), shared=True)
    caller = DaemonCaller(ctx.paths)
    if a.http:
        from .http import serve_http

        port = int(a.port or _config_get(ctx, "mcp_server.http_port", MCP_HTTP_DEFAULT_PORT))
        from ..web.cli import _port_busy
        from ..web.security import is_loopback

        if is_loopback(a.host) and _port_busy(a.host, port):
            _eprint(f"lỗi: cổng {port} đang bận (web cfharness mặc định 8765, MCP mặc định {MCP_HTTP_DEFAULT_PORT}; hub khác trên máy?) — "
                    f"chọn cổng khác: cfharness mcp serve --http --port {port + 1} "
                    f"(hoặc cfharness config set mcp_server.http_port {port + 1})")
            return 1
        _eprint(f"cfharness MCP (Streamable HTTP) tại http://{a.host}:{port}/mcp — token: `cfharness mcp token`")
        serve_http(caller, ctx.paths, host=a.host, port=int(port))
        return EXIT_OK
    from .server import build_server

    build_server(caller, default_level="control", transport="stdio").run("stdio")
    return EXIT_OK


def _config_get(ctx: Any, key: str, default: Any) -> Any:
    from ..config import HubConfig

    try:
        return HubConfig(ctx.paths.config).get(key)
    except KeyError:
        return default


def cmd_mcp_snippet(ctx: Any) -> int:
    a = ctx.args
    name = a.name
    env = {}
    default_home = (Path.home() / ".cfharness").resolve()
    if ctx.paths.home != default_home:
        env["CFHARNESS_HOME"] = str(ctx.paths.home)
    if a.http:
        from .http import ensure_tokens

        port = a.port or _config_get(ctx, "mcp_server.http_port", MCP_HTTP_DEFAULT_PORT)
        tok = ensure_tokens(ctx.paths)["read" if a.read_only else "control"]
        url = f"http://127.0.0.1:{port}/mcp"
        entry: dict[str, Any] = {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {tok}"}}
        cli = f"claude mcp add --transport http --scope {a.scope} {name} {url} --header \"Authorization: Bearer {tok}\""
        note = f"Chạy server trước: cfharness mcp serve --http --port {port}"
    else:
        cmd = _cfharness_command()
        entry = {"type": "stdio", "command": cmd[0], "args": cmd[1:] + ["mcp", "serve"]}
        if env:
            entry["env"] = env
        env_flags = "".join(f" -e {k}={v}" for k, v in env.items())
        cli = f"claude mcp add --scope {a.scope}{env_flags} {name} -- {' '.join(cmd + ['mcp', 'serve'])}"
        note = "Claude Code tự spawn `cfharness mcp serve` (stdio); daemon tự khởi động nếu chưa chạy."
    js = {"mcpServers": {name: entry}}
    if ctx.json:
        print(json.dumps({"command": cli, "json": js, "note": note}, ensure_ascii=False, indent=2))
    else:
        print("# Thêm vào Claude Code bằng lệnh (cfharness KHÔNG tự sửa cấu hình của bạn):")
        print(cli)
        print("\n# Hoặc JSON (vd file cho --mcp-config, hoặc .mcp.json của project):")
        print(json.dumps(js, ensure_ascii=False, indent=2))
        print(f"\n# {note}")
    return EXIT_OK


def cmd_mcp_token(ctx: Any) -> int:
    from .http import ensure_tokens, tokens_path

    toks = ensure_tokens(ctx.paths, rotate=ctx.args.rotate)
    ctx.out({**toks, "path": str(tokens_path(ctx.paths))}, f"read:    {toks['read']}\ncontrol: {toks['control']}\n(file {tokens_path(ctx.paths)}, 0600)")
    return EXIT_OK


# ============================================================================ registry
def _policy_fields(sp: argparse.ArgumentParser) -> None:
    """Chính sách hub giữ cho một server (cấu hình kết nối do claude giữ theo tài khoản — `cfharness mcp claude`)."""
    sp.add_argument("--include", action="append", help="glob tool được dùng")
    sp.add_argument("--exclude", action="append", help="glob tool bị chặn")
    sp.add_argument("--require-approval", action="append", help="glob tool luôn qua phê duyệt")
    sp.add_argument("--trust", help="trusted | untrusted (giá trị lạ = untrusted)")
    sp.add_argument("--timeout", type=float, help="giây")
    sp.add_argument("--description")


def _policy_payload(a: argparse.Namespace) -> dict[str, Any]:
    d: dict[str, Any] = {}
    tools: dict[str, Any] = {}
    for src in ("include", "exclude", "require_approval"):
        v = _split(getattr(a, src))
        if v is not None:
            tools[src] = v
    if tools:
        d["tools"] = tools
    for src in ("trust", "description"):
        if getattr(a, src) is not None:
            d[src] = getattr(a, src)
    if a.timeout is not None:
        d["timeout_s"] = a.timeout
    return d


def _tools_text(tools: dict[str, Any]) -> str:
    lines = []
    for t, r in sorted(tools.items()):
        flags = []
        if r.get("readOnlyHint") is True:
            flags.append("readOnly")
        if r.get("destructiveHint") is True:
            flags.append("DESTRUCTIVE")
        if r.get("suspicious"):
            flags.append("ĐÁNG NGỜ")
        lines.append(f"  - {t}  [{', '.join(flags) or '-'}]")
    return "\n".join(lines)


def _server_text(s: dict[str, Any]) -> str:
    cfg = s["config"]
    tools = cfg.get("tools") or {}
    lines = [
        f"{s['name']}  [{s['transport']}, trust={s['trust']}, nguồn=claude]" + ("  (ĐÃ XOÁ MỀM)" if s.get("deleted_at") else ""),
        *([f"  host: {cfg['url']}"] if cfg.get("url") else []),
        f"  tools: include={tools.get('include') or '*'} exclude={tools.get('exclude') or '-'} require_approval={tools.get('require_approval') or '-'}",
        *([f"  ghi đè annotations (người đặt): {json.dumps(tools['annotations'], ensure_ascii=False)}"] if tools.get("annotations") else []),
        "  cấu hình kết nối + đăng nhập: claude theo tài khoản (cfharness mcp claude status)",
    ]
    if s.get("agents"):
        lines.append(f"  agent dùng: {', '.join(s['agents'])}")
    for w in s.get("warnings") or []:
        lines.append(f"  ! {w}")
    return "\n".join(lines)


def _cmd_annotate(ctx: Any) -> int:
    """`cfharness mcp annotate <server> <tool-glob> --not-destructive|--destructive|--read-only|--not-read-only|--clear` — ghi đè
    annotations server tự khai (chỉ người: mcp.update ∈ HUMAN_ONLY). Không gỡ được luật tên-xoá, tài nguyên bảo vệ, require_approval."""
    a = ctx.args
    from ..browser import CHROME_SERVER

    if a.name == CHROME_SERVER:
        return _annotate_chrome(ctx)
    s = ctx.call("mcp.show", {"name": a.name})
    tools = dict((s.get("config") or {}).get("tools") or {})
    ann = {k: dict(v) for k, v in (tools.get("annotations") or {}).items()}
    cur = dict(ann.get(a.tool) or {})
    if a.clear:
        cur = {}
    if a.not_destructive:
        cur["destructiveHint"] = False
    if a.destructive:
        cur["destructiveHint"] = True
    if a.read_only:
        cur["readOnlyHint"] = True
    if a.not_read_only:
        cur["readOnlyHint"] = False
    if not a.clear and not cur:
        _eprint("chọn ít nhất một: --not-destructive | --destructive | --read-only | --not-read-only | --clear")
        return EXIT_USER
    if cur:
        ann[a.tool] = cur
    else:
        ann.pop(a.tool, None)
    cache = s.get("annotations_cache") or {}
    import fnmatch

    hits = sorted(t for t in cache if fnmatch.fnmatchcase(t, a.tool))
    res = ctx.call("mcp.update", {"name": a.name, "patch": {"tools": {"annotations": ann}}})
    msg = (f"MCP {a.name}: tool khớp '{a.tool}' → " + (json.dumps(cur, ensure_ascii=False) if cur else "bỏ ghi đè") +
           (f" ({len(hits)} tool đã biết: {', '.join(hits[:10])}{'…' if len(hits) > 10 else ''})" if hits else ""))
    ctx.out(res, msg)
    return EXIT_OK


def _annotate_chrome(ctx: Any) -> int:
    """Claude in Chrome không nằm trong registry (server dynamic của `claude --chrome`) → ghi đè vào
    `runtime.claude.chrome.annotations` qua `config.set` (chỉ người). Luật URL/SSRF của navigate không gỡ được."""
    from ..guard.chrome import ACTION_TOOLS, READ_TOOLS, SENSITIVE_TOOLS, URL_TOOLS

    a = ctx.args
    cur_all = ctx.call("config.get", {"key": "runtime.claude.chrome.annotations"})
    ann = {k: dict(v) for k, v in (cur_all if isinstance(cur_all, dict) else {}).items() if isinstance(v, dict)}
    cur = {} if a.clear else dict(ann.get(a.tool) or {})
    if a.not_destructive:
        cur["destructiveHint"] = False
    if a.destructive:
        cur["destructiveHint"] = True
    if a.read_only:
        cur["readOnlyHint"] = True
    if a.not_read_only:
        cur["readOnlyHint"] = False
    if not a.clear and not cur:
        _eprint("chọn ít nhất một: --not-destructive | --destructive | --read-only | --not-read-only | --clear")
        return EXIT_USER
    if cur:
        ann[a.tool] = cur
    else:
        ann.pop(a.tool, None)
    import fnmatch

    known = sorted(READ_TOOLS | URL_TOOLS | ACTION_TOOLS | SENSITIVE_TOOLS)
    hits = [t for t in known if fnmatch.fnmatchcase(t, a.tool)]
    res = ctx.call("config.set", {"key": "runtime.claude.chrome.annotations", "value": ann})
    ctx.out(res, f"Claude in Chrome: tool khớp '{a.tool}' → " + (json.dumps(cur, ensure_ascii=False) if cur else "bỏ ghi đè")
            + (f" ({', '.join(hits)})" if hits else " (chưa khớp tool nào trong bảng phân loại)")
            + " — lưu ở runtime.claude.chrome.annotations")
    return EXIT_OK


_STATUS_VI = {"connected": "đã kết nối", "needs_auth": "CẦN ĐĂNG NHẬP", "failed": "lỗi kết nối", "unknown": "chưa rõ"}


def _ask_secret(prompt: str) -> str:
    """Giá trị bí mật (header/env/URL chuyển hướng): getpass khi có TTY, không thì một dòng stdin — không bao giờ qua argv."""
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.readline().strip()


def cmd_mcp_claude(ctx: Any) -> int:
    """`cfharness mcp claude add|rm|login|logout|status` — MCP do claude quản lý theo tài khoản (plan-mcp-claude-native PR3)."""
    a = ctx.args
    op = a.claude_op
    base = {"account_id": a.account}
    if op == "status":
        res = ctx.call("mcp.claude_status", {**base, "refresh": bool(a.refresh)}, timeout=180)
        lines = [f"MCP do claude quản lý — tài khoản {res.get('account_label')} ({res.get('config_path')})"]
        if res.get("error"):
            lines.append(f"  ! {res['error']}")
        if not res.get("servers"):
            lines.append("  (không có server nào — thêm: cfharness mcp claude add <tên> --url URL)")
        for s in res.get("servers") or []:
            reg = "đã đăng ký" if s.get("registered") else "chưa đăng ký"
            lines.append(f"  {s['name']:<28} {s['transport']:<6} {s.get('host') or '-':<30} {_STATUS_VI.get(s['status'], s['status']):<14} {reg}"
                         + (f"  agent: {', '.join(s['agents'])}" if s.get("agents") else ""))
            if s["status"] == "needs_auth":
                lines.append(f"      → cfharness mcp claude login {s['name']}" + (f" --account {a.account}" if a.account else ""))
        ctx.out(res, "\n".join(lines))
        return EXIT_OK
    if op == "add":
        cmd = [x for x in (a.cmd or []) if x != "--"]
        if bool(a.url) == bool(cmd):
            _eprint("chọn MỘT: --url URL (http/sse) hoặc -- <lệnh> <args…> (stdio)")
            return EXIT_USER
        p: dict[str, Any] = {**base, "name": a.name, "transport": a.transport or ("stdio" if cmd else "http"), "register": not a.no_register}
        if a.trust:
            p["trust"] = a.trust
        if a.url:
            p["url"] = a.url
        else:
            p["command"], p["args"] = cmd[0], cmd[1:]
        if a.header:
            p["headers"] = {h: _ask_secret(f"Giá trị header {h} (không hiện khi gõ): ") for h in a.header}
        if a.env:
            p["env"] = {k: _ask_secret(f"Giá trị env {k} (không hiện khi gõ): ") for k in a.env}
        res = ctx.call("mcp.claude_add", p, timeout=180)
        text = (f"đã thêm MCP '{res['name']}' ({res['transport']}{' · ' + res['host'] if res.get('host') else ''}) vào cấu hình claude "
                f"của tài khoản {res.get('account') or 'mặc định'}" + ("; đã đăng ký registry (nguồn claude)" if res.get("registered") else ""))
        for w in res.get("warnings") or []:
            text += f"\n  ! {w}"
        if res["transport"] != "stdio":
            text += f"\n  đăng nhập (nếu server dùng OAuth): cfharness mcp claude login {res['name']}" + (f" --account {a.account}" if a.account else "")
        ctx.out(res, text)
        return EXIT_OK
    if op == "rm":
        res = ctx.call("mcp.claude_remove", {**base, "name": a.name, "force": bool(a.force)}, timeout=120)
        reg = {"removed": "đã xoá mềm dòng registry", "kept": "registry giữ nguyên (tài khoản khác còn server này: "
               + ", ".join(res.get("other_accounts") or []) + ")", "none": "không có dòng registry nguồn claude"}[res["registry"]]
        ctx.out(res, f"đã gỡ MCP '{res['name']}' khỏi cấu hình claude của tài khoản {res.get('account') or 'mặc định'}; {reg}")
        return EXIT_OK
    if op == "logout":
        res = ctx.call("mcp.claude_logout", {**base, "name": a.name}, timeout=120)
        ctx.out(res, f"đã đăng xuất MCP '{res['name']}' (tài khoản {res.get('account') or 'mặc định'})")
        return EXIT_OK
    lg = ctx.call("mcp.claude_login_start", {**base, "name": a.name}, timeout=120)
    print(f"Đăng nhập MCP '{lg['name']}' (tài khoản {lg.get('account') or 'mặc định'}): mở link sau, đăng nhập + đồng ý cấp quyền.")
    print("Trình duyệt sẽ chuyển tới http://localhost:… (có thể báo không mở được trang) — chép NGUYÊN địa chỉ trên thanh trình duyệt rồi dán:")
    print(f"  {lg['url']}")
    try:
        url = _ask_secret("URL chuyển hướng (không hiện khi gõ): ").strip()
    except (KeyboardInterrupt, EOFError):
        ctx.call("mcp.claude_login_cancel", {"login_id": lg["login_id"]})
        print("\nĐã huỷ.")
        return EXIT_USER
    if not url:
        ctx.call("mcp.claude_login_cancel", {"login_id": lg["login_id"]})
        print("Không có URL — đã huỷ.")
        return EXIT_USER
    res = ctx.call("mcp.claude_login_finish", {"login_id": lg["login_id"], "redirect_url": url}, timeout=240)
    ctx.out(res, f"MCP '{res['name']}': {_STATUS_VI.get(res['status'], res['status'])}")
    return EXIT_OK


def cmd_mcp(ctx: Any) -> int:
    a = ctx.args
    act = a.mcp_action
    if act == "claude":
        return cmd_mcp_claude(ctx)
    if act == "serve":
        return cmd_mcp_serve(ctx)
    if act == "snippet":
        return cmd_mcp_snippet(ctx)
    if act == "token":
        return cmd_mcp_token(ctx)
    if act == "add":
        if a.source == "hub" or a.rest or any(getattr(a, "legacy_" + f) for f in ("url", "transport", "env", "header")):
            _eprint("hub không còn tự giữ cấu hình MCP (lệnh/url/header/env/OAuth) — thêm server vào claude theo tài khoản: "
                    f"cfharness mcp claude add {a.name} --url <URL> | -- <lệnh> <args…> [--account <id>]")
            return EXIT_USER
        res = ctx.call("mcp.add", {"config": {"name": a.name, **_policy_payload(a)}, "account_id": a.account})
        ctx.out(res, f"đã đăng ký MCP cho hub (tài khoản {res.get('account') or 'cấu hình claude mặc định'})\n" + _server_text(res)
                + "\n  (hub không probe — gắn nhãn tool: cfharness mcp annotate " + a.name + " <tool> --read-only|--destructive)")
        return EXIT_OK
    if act == "claude-servers":
        res = ctx.call("mcp.claude_servers", {"account_id": a.account})
        lines = [f"MCP do claude quản lý — tài khoản {res.get('account') or 'cấu hình claude mặc định'} ({res.get('config_path')})"]
        if not res.get("servers"):
            lines.append("  (không có server nào)" if res.get("exists") else "  (chưa có .claude.json)")
        for s in res.get("servers") or []:
            reg = "đã đăng ký cho hub" if s.get("registered") else "chưa đăng ký (cfharness mcp add " + s["name"] + ")"
            lines.append(f"  {s['name']:<28} {s['transport']:<6} {s.get('host') or '-':<30} {reg}"
                         + (f"  agent: {', '.join(s['agents'])}" if s.get("agents") else ""))
        ctx.out(res, "\n".join(lines))
        return EXIT_OK
    if act == "edit":
        patch = _policy_payload(a)
        if not patch:
            _eprint("không có thay đổi nào (sửa cách kết nối: cfharness mcp claude rm + cfharness mcp claude add)")
            return EXIT_USER
        res = ctx.call("mcp.update", {"name": a.name, "patch": patch})
        ctx.out(res, "đã sửa\n" + _server_text(res))
        return EXIT_OK
    if act == "list":
        rows = ctx.call("mcp.list", {"deleted": a.deleted})
        if not rows:
            ctx.out(rows, "(registry trống — `cfharness mcp claude add` rồi gán cho agent)")
            return EXIT_OK
        lines = []
        for s in rows:
            host = (s.get("config") or {}).get("url") or "-"
            lines.append(f"{s['name']:<28} {s['transport']:<6} {s['trust']:<10} {host}" + ("  (xoá)" if s.get("deleted_at") else ""))
        ctx.out(rows, "\n".join(lines))
        return EXIT_OK
    if act == "show":
        s = ctx.call("mcp.show", {"name": a.name})
        text = _server_text(s)
        cache = s.get("annotations_cache") or {}
        if cache:
            text += f"\n  annotations (lần probe cũ {s.get('last_probe_at') or '-'}):\n" + _tools_text(cache)
        ctx.out(s, text)
        return EXIT_OK
    if act == "rm":
        s = ctx.call("mcp.remove", {"name": a.name, "force": a.force})
        ctx.out(s, f"đã xoá mềm MCP server {a.name} → {s.get('trash_path')} (khôi phục: cfharness mcp restore {a.name})")
        return EXIT_OK
    if act == "restore":
        s = ctx.call("mcp.restore", {"name": a.name})
        ctx.out(s, f"đã khôi phục MCP server {a.name}")
        return EXIT_OK
    if act in ("assign", "unassign"):
        ag = ctx.call("mcp.assign", {"server": a.server, "agent": a.agent, "unassign": act == "unassign"})
        ctx.out(ag, f"agent {ag['id']}: mcp = {', '.join(ag.get('mcp_servers') or []) or '-'} (revision {ag['config_revision']})")
        return EXIT_OK
    if act == "annotate":
        return _cmd_annotate(ctx)
    if act == "agent-config":
        res = ctx.call("mcp.agent_config", {"agent": a.agent})
        ctx.out(res, json.dumps(res, ensure_ascii=False, indent=2))
        return EXIT_OK
    return EXIT_USER


# ============================================================================ secret / audit / feedback
def cmd_secret(ctx: Any) -> int:
    a = ctx.args
    if a.secret_action == "set":
        if a.value_stdin:
            value = sys.stdin.read().rstrip("\n")
        elif a.from_env:
            value = os.environ.get(a.from_env, "")
        else:
            value = getpass.getpass(f"giá trị cho {a.name}: ")
        r = ctx.call("secret.set", {"name": a.name, "value": value})
        ctx.out(r, f"đã lưu secret {r['name']} (file 0600; giá trị không bao giờ hiển thị)")
    elif a.secret_action == "list":
        rows = ctx.call("secret.list")
        ctx.out(rows, "\n".join(f"{r['name']:<40} {'THIẾU' if r.get('missing') else 'có'}  dùng bởi: {', '.join(r.get('used_by') or []) or '-'}" for r in rows) or "(chưa có secret)")
    elif a.secret_action == "rm":
        r = ctx.call("secret.rm", {"name": a.name})
        ctx.out(r, f"đã xoá secret {a.name}")
    return EXIT_OK


def _audit_time(v: str | None, flag: str) -> str | None:
    """`24h`/`30m`/`7d`/ISO → ISO UTC (đổi ở CLI để thông báo lỗi rõ trước khi gọi daemon)."""
    if not v:
        return None
    from ..log_query import parse_time

    try:
        return parse_time(v)
    except ValueError as e:
        raise SystemExit(f"{flag}: {e}") from None


def _audit_filter_params(a: Any) -> dict[str, Any]:
    p = {"kind": a.kind, "run_id": a.run, "agent_id": a.agent, "channel": a.channel, "principal": a.principal, "actor": a.actor,
         "outcome": a.outcome, "req_id": a.req, "q": a.q, "since": _audit_time(a.since, "--since"), "until": _audit_time(a.until, "--until")}
    return {k: v for k, v in p.items() if v not in (None, "")}


def _one_line(v: Any, n: int) -> str:
    from ..termsafe import one_line

    return one_line(str(v if v is not None else "-"))[:n]


def cmd_audit(ctx: Any) -> int:
    a = ctx.args
    if a.audit_action == "verify":
        return cmd_audit_verify(ctx)
    if a.audit_action == "export":
        return cmd_audit_export(ctx)
    params = _audit_filter_params(a)
    params["limit"] = a.limit
    if a.before_id is not None:
        params["before_id"] = a.before_id
    rows = ctx.call("audit.list", params)
    text = "\n".join(f"#{r['id']:<5} {r['ts']}  {_one_line(r['kind'], 30):<18} {_one_line(r['channel'], 20):<20} {_one_line(r['actor'], 16):<16} "
                     f"{r.get('outcome') or '-':<6} {_one_line(r.get('principal'), 24):<20} {_one_line(r.get('target'), 60)}  {r.get('run_id') or ''}"
                     f"{'  ' + r['req_id'] if r.get('req_id') else ''}" for r in rows) or "(audit trống)"
    if rows and len(rows) >= a.limit:
        text += f"\n… còn nữa: thêm --before-id {min(r['id'] for r in rows)}"
    ctx.out(rows, text)
    return EXIT_OK


def cmd_audit_export(ctx: Any) -> int:
    """L5: `cfharness audit export [--since 7d] [--until …] [--format jsonl|csv] [lọc như list]` → HUB_HOME/exports/audit-<ts>.<ext> (0600)."""
    a = ctx.args
    params = _audit_filter_params(a)
    params["format"] = a.format
    r = ctx.call("audit.export", params)
    ctx.out(r, f"đã xuất {r.get('rows', 0)} dòng audit (#{r.get('first_id') or '-'}…#{r.get('last_id') or '-'}, kèm cột băm) → {r.get('path')}\n"
               f"  {r.get('bytes', 0)} byte · sha256 {r.get('sha256')} · file 0600")
    return EXIT_OK


def cmd_audit_verify(ctx: Any) -> int:
    """L4: `cfharness audit verify [--since-id N] [--json]` — mã thoát 1 khi chuỗi gãy / bản neo lệch."""
    a = ctx.args
    params: dict[str, Any] = {"since_id": a.since_id} if a.since_id is not None else {}
    if getattr(a, "archive", False):
        params["archive"] = True
    r = ctx.call("audit.verify", params)
    lines = []
    if r.get("no_chain"):
        lines.append("DB chưa có chuỗi băm audit (schema < v30)")
    b = r.get("break")
    lines.append(f"đã kiểm {r.get('checked', 0)} dòng (#{r.get('first_id') or '-'}…#{r.get('last_id') or '-'}; chuỗi bắt đầu từ #{r.get('chain_start_id') or '-'}"
                 f"{'; ' + str(r['unchained']) + ' dòng trước v30 không kiểm' if r.get('unchained') else ''}) trong {r.get('duration_ms', 0)} ms")
    if b:
        lines.append(f"GÃY ở dòng #{b['id']}: {b['reason']}\n  mong đợi: {b.get('expected')}\n  thực tế:  {b.get('actual')}")
    anc = r.get("anchors") or {}
    if not anc.get("file"):
        lines.append("bản neo: chưa có file audit-anchors.jsonl")
    if r.get("archive_cutoff"):
        lines.append(f"lưu trữ: dòng ≤ #{r['archive_cutoff']} đã chuyển ra audit-archive/ ({r.get('archive_files', 0)} file)"
                     + (f"; dòng đầu DB nối với file lưu trữ #{r['archive_link_id']}" if r.get("archive_link_id") else ""))
    else:
        lt = anc.get("latest") or {}
        lines.append(f"bản neo: kiểm {anc.get('checked', 0)}/{anc.get('total', 0)}" + (f", mới nhất #{lt.get('last_id')} lúc {lt.get('ts')}" if lt else "")
                     + (f", {anc['bad_lines']} dòng hỏng" if anc.get("bad_lines") else ""))
        for x in anc.get("problems") or []:
            what = "không còn trong DB (bị xoá)" if x["reason"] == "anchor_missing" else "băm khác (bị sửa rồi tính lại chuỗi)"
            lines.append(f"  BẢN NEO #{x['id']}: {what}")
    arc = r.get("archive")
    if arc is not None:
        lines.append(f"file lưu trữ: kiểm {arc.get('files', 0)} file, {arc.get('rows', 0)} dòng" + ("" if arc.get("ok") else " — CÓ LỖI"))
        for x in arc.get("problems") or []:
            lines.append(f"  {x.get('file')}: {x.get('reason')}" + (f" (dòng #{x['id']})" if x.get("id") else ""))
    lines.append("KẾT QUẢ: " + ("toàn vẹn" if r.get("ok") else "PHÁT HIỆN SỬA/XOÁ — đã phát cảnh báo audit.tamper_detected"))
    ctx.out(r, "\n".join(lines))
    return EXIT_OK if r.get("ok") else 1


def cmd_feedback(ctx: Any) -> int:
    a = ctx.args
    r = ctx.call("feedback.record", {"run_id": a.run_id, "kind": a.kind, "note": a.note, "corrected_output": a.corrected_output, "source": "cli"})
    ctx.out(r, f"đã ghi signal {r['signal_id']} ({r['kind']}) cho agent {r['agent_id']}")
    return EXIT_OK


# ============================================================================ parser
def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    m = sub.add_parser("mcp", help="MCP: server của hub (serve/snippet/token) và registry MCP cho agent", parents=[common])
    ms = m.add_subparsers(dest="mcp_action", required=True)

    sv = ms.add_parser("serve", help="chạy MCP server của hub (stdio; --http = Streamable HTTP chỉ 127.0.0.1)", parents=[common])
    sv.add_argument("--http", action="store_true")
    sv.add_argument("--host", default="127.0.0.1", help="chỉ chấp nhận loopback")
    sv.add_argument("--port", type=int, help=f"mặc định mcp_server.http_port ({MCP_HTTP_DEFAULT_PORT}, khác cổng web 8765)")

    sn = ms.add_parser("snippet", help="IN lệnh/JSON để thêm cfharness vào Claude Code (không tự sửa cấu hình)", parents=[common])
    sn.add_argument("--http", action="store_true")
    sn.add_argument("--port", type=int)
    sn.add_argument("--name", default="cfharness")
    sn.add_argument("--scope", default="user", choices=["user", "project", "local"])
    sn.add_argument("--read-only", action="store_true", help="(HTTP) dùng token mức read")

    tk = ms.add_parser("token", help="xem/đổi token cục bộ cho MCP HTTP", parents=[common])
    tk.add_argument("--rotate", action="store_true")

    ad = ms.add_parser("add", help="đăng ký cho hub một server CÓ SẴN trong cấu hình claude của tài khoản (thêm server mới: mcp claude add)",
                       parents=[common])
    ad.add_argument("name")
    ad.add_argument("--account", help="id tài khoản Claude để kiểm (mặc định: tài khoản đang dùng)")
    ad.add_argument("--source", choices=["claude", "hub"], help=argparse.SUPPRESS)  # cờ cũ: `claude` = mặc định, `hub` → lỗi hướng dẫn
    _policy_fields(ad)
    # cờ kiểu cũ (hub tự giữ cấu hình) → lỗi hướng dẫn `mcp claude add`
    for flag in ("--url", "--transport", "--env", "--header"):
        ad.add_argument(flag, action="append", dest="legacy_" + flag.strip("-"), help=argparse.SUPPRESS)
    ad.add_argument("rest", nargs="*", help=argparse.SUPPRESS)  # `-- <lệnh> <args…>` kiểu cũ
    cs = ms.add_parser("claude-servers", help="MCP do claude quản lý trong cấu hình claude của tài khoản (chỉ tên/transport/host)",
                       parents=[common])
    cs.add_argument("--account", help="id tài khoản Claude (mặc định: đang dùng)")
    mc = ms.add_parser("claude", help="MCP do claude quản lý theo tài khoản: add|rm|login|logout|status (claude giữ cấu hình + token)",
                       parents=[common])
    mcs = mc.add_subparsers(dest="claude_op", required=True)
    acct = argparse.ArgumentParser(add_help=False)
    acct.add_argument("--account", help="id tài khoản Claude (mặc định: đang dùng; 'machine' = cấu hình claude mặc định)")
    c_st = mcs.add_parser("status", help="server + trạng thái (claude mcp list, cache 30 s)", parents=[common, acct])
    c_st.add_argument("--refresh", action="store_true", help="bỏ cache")
    c_ad = mcs.add_parser("add", help="thêm: add <tên> --url URL [--transport sse] [--header TÊN…] | add <tên> -- <lệnh> <args…> [--env KHOÁ…]",
                          parents=[common, acct])
    c_ad.add_argument("name")
    c_ad.add_argument("--url", help="http(s)://… (transport http/sse)")
    c_ad.add_argument("--transport", choices=["http", "sse", "stdio"])
    c_ad.add_argument("--header", action="append", metavar="TÊN", help="header (giá trị hỏi khi chạy, không qua argv); chỉ tài khoản token")
    c_ad.add_argument("--env", action="append", metavar="KHOÁ", help="env stdio (giá trị hỏi khi chạy); chỉ tài khoản token")
    c_ad.add_argument("--trust", choices=["trusted", "untrusted"], help="trust trong registry (mặc định untrusted)")
    c_ad.add_argument("--no-register", action="store_true", help="chỉ thêm vào claude, không tạo dòng registry")
    c_ad.add_argument("cmd", nargs="*", help="-- <lệnh> <args…> (stdio)")
    for op, hp in (("rm", "gỡ khỏi cấu hình claude của tài khoản (registry xoá mềm khi không tài khoản nào còn)"),
                   ("login", "đăng nhập OAuth (claude mcp login --no-browser): in link, dán URL chuyển hướng"),
                   ("logout", "đăng xuất (claude mcp logout)")):
        c = mcs.add_parser(op, help=hp, parents=[common, acct])
        c.add_argument("name")
        if op == "rm":
            c.add_argument("--force", action="store_true", help="xoá cả khi agent còn gán (bản cuối)")
    ed = ms.add_parser("edit", help="sửa chính sách của server: trust, include/exclude/require-approval, mô tả", parents=[common])
    ed.add_argument("name")
    _policy_fields(ed)
    an = ms.add_parser("annotate", help="ghi đè annotations tool (người đặt), vd: mcp annotate playwright 'browser_navigate' --not-destructive",
                       parents=[common])
    an.add_argument("name", help="tên MCP server")
    an.add_argument("tool", help="tên tool hoặc glob (vd browser_*)")
    an.add_argument("--not-destructive", action="store_true", help="destructiveHint=false (không hỏi duyệt vì cờ của server)")
    an.add_argument("--destructive", action="store_true", help="destructiveHint=true (luôn hỏi duyệt)")
    an.add_argument("--read-only", action="store_true", help="readOnlyHint=true (coi là chỉ đọc)")
    an.add_argument("--not-read-only", action="store_true", help="readOnlyHint=false")
    an.add_argument("--clear", action="store_true", help="bỏ ghi đè của glob này")
    ls = ms.add_parser("list", help="liệt kê registry", parents=[common])
    ls.add_argument("--deleted", action="store_true")
    for name, hlp in (("show", "xem server + tool/annotations"), ("restore", "khôi phục server đã xoá mềm")):
        x = ms.add_parser(name, help=hlp, parents=[common])
        x.add_argument("name")
    rm = ms.add_parser("rm", help="xoá mềm server", parents=[common])
    rm.add_argument("name")
    rm.add_argument("--force", action="store_true", help="xoá dù agent đang dùng")
    for name in ("assign", "unassign"):
        x = ms.add_parser(name, help=f"{'gán' if name == 'assign' else 'gỡ'} MCP server cho agent", parents=[common])
        x.add_argument("server")
        x.add_argument("agent")
    ac = ms.add_parser("agent-config", help="xem MCP + allow/deny sẽ dùng khi chạy agent", parents=[common])
    ac.add_argument("agent")
    m.set_defaults(fn=cmd_mcp)

    se = sub.add_parser("secret", help="secret store (file 0600 trong HUB_HOME/secrets)", parents=[common])
    ss = se.add_subparsers(dest="secret_action", required=True)
    st = ss.add_parser("set", parents=[common])
    st.add_argument("name")
    st.add_argument("--value-stdin", action="store_true", help="đọc giá trị từ stdin")
    st.add_argument("--from-env", metavar="VAR", help="lấy giá trị từ biến môi trường")
    ss.add_parser("list", parents=[common])
    sr = ss.add_parser("rm", parents=[common])
    sr.add_argument("name")
    se.set_defaults(fn=cmd_secret)

    au = sub.add_parser("audit", help="xem nhật ký kiểm toán", parents=[common])
    aus = au.add_subparsers(dest="audit_action", required=True)

    def _filters(sp: Any) -> None:
        sp.add_argument("--kind", help="vd tool.write, agent.* (dấu * cuối = tiền tố)")
        sp.add_argument("--run")
        sp.add_argument("--agent")
        sp.add_argument("--channel")
        sp.add_argument("--principal", help="vd os:phuong, web:dev_ab12cd34, telegram:123, agent:coder")
        sp.add_argument("--actor", help="vd user, hub, agent:coder")
        sp.add_argument("--outcome", choices=["ok", "error", "denied"])
        sp.add_argument("--req", help="mã yêu cầu r_…")
        sp.add_argument("--q", help="tìm chuỗi con trong tóm tắt/đích (không tìm trong data)")
        sp.add_argument("--since", help="vd 24h, 30m, 7d hoặc 2026-11-18T10:00")
        sp.add_argument("--until", help="như --since")

    al = aus.add_parser("list", parents=[common])
    _filters(al)
    al.add_argument("--before-id", type=int, default=None, help="phân trang: chỉ dòng có id nhỏ hơn số này")
    al.add_argument("--limit", type=int, default=50)
    ae = aus.add_parser("export", help="xuất audit ra HUB_HOME/exports/ (0600, kèm cột băm)", parents=[common])
    _filters(ae)
    ae.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    av = aus.add_parser("verify", help="kiểm chuỗi băm nhật ký kiểm toán + bản neo (L4)", parents=[common])
    av.add_argument("--since-id", type=int, default=None, help="chỉ kiểm từ dòng id này (dòng trước được tin)")
    av.add_argument("--archive", action="store_true", help="kiểm cả các file trong audit-archive/ (sha256 + chuỗi băm)")
    au.set_defaults(fn=cmd_audit)

    fb = sub.add_parser("feedback", help="ghi phản hồi về một run (Signal)", parents=[common])
    fb.add_argument("run_id")
    fb.add_argument("kind", choices=["good", "wrong", "partial"])
    fb.add_argument("--note")
    fb.add_argument("--corrected-output")
    fb.set_defaults(fn=cmd_feedback)
