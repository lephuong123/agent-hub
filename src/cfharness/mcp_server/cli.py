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
        self.client = RpcClient(paths.socket, timeout=120)
        self._lock = threading.Lock()
        self._started = False

    def __call__(self, method: str, params: dict[str, Any]) -> Any:
        from ..rpc import DaemonUnavailable, RpcClient

        try:
            return RpcClient(self.paths.socket, timeout=120).call(method, params)
        except DaemonUnavailable:
            with self._lock:
                if not self._started:
                    from ..daemon import spawn_background

                    spawn_background(self.paths)
                    self._started = True
            return RpcClient(self.paths.socket, timeout=120).call(method, params)


# ============================================================================ mcp serve / snippet / token
def _cfharness_command() -> list[str]:
    exe = shutil.which("cfharness")
    if exe:
        return [str(Path(exe).resolve())]
    return [sys.executable, "-m", "cfharness"]


def cmd_mcp_serve(ctx: Any) -> int:
    import logging

    a = ctx.args
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    caller = DaemonCaller(ctx.paths)
    if a.http:
        from .http import serve_http

        port = a.port or _config_get(ctx, "mcp_server.http_port", 8765)
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

        port = a.port or _config_get(ctx, "mcp_server.http_port", 8765)
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
def _server_payload(a: argparse.Namespace, *, creating: bool) -> dict[str, Any]:
    d: dict[str, Any] = {}
    command = list(a.command or [])
    if command and command[0] == "--":
        command = command[1:]
    if command:
        d["command"], d["args"] = command[0], command[1:]
        d.setdefault("transport", "stdio")
    if a.url:
        d["url"] = a.url
        d["transport"] = a.transport or "http"
    if a.transport:
        d["transport"] = a.transport
    if a.env:
        env = _kv_list(a.env, "env")
        d["env"] = env if not creating else {k: v for k, v in env.items() if v is not None}
    if a.header:
        hd = _kv_list(a.header, "header")
        d["headers"] = hd if not creating else {k: v for k, v in hd.items() if v is not None}
    tools: dict[str, Any] = {}
    for src, dst in (("include", "include"), ("exclude", "exclude"), ("require_approval", "require_approval")):
        v = _split(getattr(a, src))
        if v is not None:
            tools[dst] = v
    if tools:
        d["tools"] = tools
    for src in ("trust", "description"):
        if getattr(a, src) is not None:
            d[src] = getattr(a, src)
    if a.timeout is not None:
        d["timeout_s"] = a.timeout
    return d


def _probe_text(pr: dict[str, Any]) -> str:
    if not pr.get("ok"):
        return f"✗ probe lỗi ({pr.get('elapsed_ms')} ms): {pr.get('error')}\n  gợi ý: {pr.get('hint')}"
    lines = [f"✓ probe {pr.get('name')} ({pr.get('elapsed_ms')} ms, trust={pr.get('trust')}): {len(pr['tools'])} tool"]
    for t, r in sorted(pr["tools"].items()):
        flags = []
        if r.get("readOnlyHint") is True:
            flags.append("readOnly")
        if r.get("destructiveHint") is True:
            flags.append("DESTRUCTIVE")
        if r.get("readOnlyHint") is None and r.get("destructiveHint") is None:
            flags.append("không annotations → coi là ghi")
        if r.get("suspicious"):
            flags.append("ĐÁNG NGỜ")
        lines.append(f"  - {t}  [{', '.join(flags)}]  {(r.get('description') or '').splitlines()[0][:80] if r.get('description') else ''}")
    for w in pr.get("warnings") or []:
        lines.append(f"  ! {w}")
    if pr.get("new_tools"):
        lines.append(f"  tool mới: {', '.join(pr['new_tools'])}")
    return "\n".join(lines)


def _server_text(s: dict[str, Any]) -> str:
    cfg = s["config"]
    target = (cfg.get("command", "") + " " + " ".join(cfg.get("args") or [])).strip() if s["transport"] == "stdio" else cfg.get("url")
    tools = cfg.get("tools") or {}
    lines = [
        f"{s['name']}  [{s['transport']}, trust={s['trust']}, nguồn={s.get('source')}]" + ("  (ĐÃ XOÁ MỀM)" if s.get("deleted_at") else ""),
        f"  {target}",
        f"  env: {json.dumps(cfg.get('env') or {}, ensure_ascii=False)}" if s["transport"] == "stdio" else f"  headers: {json.dumps(cfg.get('headers') or {}, ensure_ascii=False)}",
        f"  tools: include={tools.get('include') or '*'} exclude={tools.get('exclude') or '-'} require_approval={tools.get('require_approval') or '-'}",
        *([f"  ghi đè annotations (người đặt): {json.dumps(tools['annotations'], ensure_ascii=False)}"] if tools.get("annotations") else []),
        f"  probe: {s.get('last_probe_at') or 'chưa'}" + (f"  lỗi: {s['probe_error']}" if s.get("probe_error") else "") + f"  · {len(s.get('annotations_cache') or {})} tool",
    ]
    if s.get("agents"):
        lines.append(f"  agent dùng: {', '.join(s['agents'])}")
    if s.get("missing_secrets"):
        lines.append(f"  ! thiếu secret: {', '.join(s['missing_secrets'])}")
    for w in s.get("warnings") or []:
        lines.append(f"  ! {w}")
    if s.get("secrets_moved"):
        lines.append(f"  secret đã chuyển vào secret store: {', '.join(s['secrets_moved'])}")
    return "\n".join(lines)


def _cmd_annotate(ctx: Any) -> int:
    """`cfharness mcp annotate <server> <tool-glob> --not-destructive|--destructive|--read-only|--not-read-only|--clear` — ghi đè
    annotations server tự khai (chỉ người: mcp.update ∈ HUMAN_ONLY). Không gỡ được luật tên-xoá, tài nguyên bảo vệ, require_approval."""
    a = ctx.args
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
           (f" ({len(hits)} tool đã probe: {', '.join(hits[:10])}{'…' if len(hits) > 10 else ''})" if hits else
            " (chưa khớp tool nào đã probe — `cfharness mcp probe " + a.name + "`)"))
    ctx.out(res, msg)
    return EXIT_OK


def cmd_mcp(ctx: Any) -> int:
    a = ctx.args
    act = a.mcp_action
    if act == "serve":
        return cmd_mcp_serve(ctx)
    if act == "snippet":
        return cmd_mcp_snippet(ctx)
    if act == "token":
        return cmd_mcp_token(ctx)
    if act == "add":
        payload = {"name": a.name, **_server_payload(a, creating=True)}
        res = ctx.call("mcp.add", {"config": payload, "probe": not a.no_probe, "timeout_s": a.timeout})
        text = "đã thêm MCP server\n" + _server_text(res)
        if res.get("probe"):
            text += "\n" + _probe_text(res["probe"])
        ctx.out(res, text)
        return EXIT_OK if not res.get("probe") or res["probe"].get("ok") else EXIT_SYS
    if act == "edit":
        patch = _server_payload(a, creating=False)
        if not patch:
            _eprint("không có thay đổi nào")
            return EXIT_USER
        res = ctx.call("mcp.update", {"name": a.name, "patch": patch, "probe": a.probe})
        ctx.out(res, "đã sửa\n" + _server_text(res) + ("\n" + _probe_text(res["probe"]) if res.get("probe") else ""))
        return EXIT_OK
    if act == "list":
        rows = ctx.call("mcp.list", {"deleted": a.deleted})
        if not rows:
            ctx.out(rows, "(registry trống — `cfharness mcp add` hoặc `cfharness mcp import --from claude`)")
            return EXIT_OK
        lines = []
        for s in rows:
            n_tools = len(s.get("annotations_cache") or {})
            lines.append(f"{s['name']:<28} {s['transport']:<6} {s['trust']:<10} {n_tools:>3} tool  probe={s.get('last_probe_at') or '-'}" + ("  (xoá)" if s.get("deleted_at") else ""))
        ctx.out(rows, "\n".join(lines))
        return EXIT_OK
    if act == "show":
        s = ctx.call("mcp.show", {"name": a.name})
        text = _server_text(s)
        cache = s.get("annotations_cache") or {}
        if cache:
            text += "\n" + _probe_text({"ok": True, "name": s["name"], "tools": cache, "elapsed_ms": "-", "trust": s["trust"]}).split("\n", 1)[-1]
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
    if act in ("probe", "test"):
        pr = ctx.call("mcp.probe", {"name": a.name, "timeout_s": a.timeout})
        ctx.out(pr, _probe_text(pr))
        return EXIT_OK if pr.get("ok") else EXIT_SYS
    if act == "import":
        return _cmd_import(ctx)
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


def _cmd_import(ctx: Any) -> int:
    a = ctx.args
    if a.source != "claude":
        _eprint("chỉ hỗ trợ --from claude")
        return EXIT_USER
    path = str(Path(a.file).expanduser().resolve()) if a.file else str(Path.home() / ".claude.json")
    project = str(Path(a.project).expanduser().resolve()) if a.project else None
    select = _split(a.select)
    if not a.all and select is None:
        plan = ctx.call("mcp.import", {"path": path, "project": project, "dry_run": True})
        avail = plan["available"]
        if not avail:
            ctx.out(plan, f"{path}: không có mcpServers nào")
            return EXIT_OK
        if ctx.json or not sys.stdin.isatty():
            ctx.out(plan, "\n".join(f"  {i['name']}  [{i.get('transport')}] {i.get('status')}" for i in plan["items"]) + "\n(dùng --all hoặc --select a,b để import)")
            return EXIT_OK
        print(f"MCP server trong {path}:")
        for i, it in enumerate(plan["items"], 1):
            sec = f"  secret: {', '.join(it.get('secrets') or [])}" if it.get("secrets") else ""
            print(f"  {i}. {it['name']}  [{it.get('transport')}] {it.get('status')}{sec}")
        ans = input("Chọn (vd 1,3 | all | Enter = huỷ): ").strip()
        if not ans:
            return EXIT_OK
        if ans.lower() == "all":
            select = None
        else:
            idx = [int(x) for x in ans.replace(" ", "").split(",") if x]
            select = [plan["items"][i - 1]["name"] for i in idx]
    res = ctx.call(
        "mcp.import",
        {"path": path, "project": project, "select": select, "dry_run": a.dry_run, "overwrite": a.overwrite, "probe": a.probe},
    )
    lines = [f"nguồn: {res['source']} (chỉ đọc, không sửa)" + ("  [DRY-RUN]" if res["dry_run"] else "")]
    for it in res["items"]:
        lines.append(f"  {it['name']:<28} {it.get('status')}" + (f"  lỗi: {it['error']}" if it.get("error") else ""))
        for n in it.get("notes") or []:
            lines.append(f"      {n}")
        if it.get("probe"):
            pr = it["probe"]
            lines.append(f"      probe: {'ok' if pr.get('ok') else 'lỗi ' + str(pr.get('error'))} · {pr.get('tools')} tool")
    ctx.out(res, "\n".join(lines))
    return EXIT_OK


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


def cmd_audit(ctx: Any) -> int:
    a = ctx.args
    rows = ctx.call("audit.list", {"kind": a.kind, "run_id": a.run, "agent_id": a.agent, "channel": a.channel, "limit": a.limit})
    ctx.out(rows, "\n".join(f"#{r['id']:<5} {r['ts']}  {r['kind']:<18} {r['channel']:<20} {r['actor']:<16} {r.get('target') or '-'}  {r.get('run_id') or ''}" for r in rows) or "(audit trống)")
    return EXIT_OK


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
    sv.add_argument("--port", type=int)

    sn = ms.add_parser("snippet", help="IN lệnh/JSON để thêm cfharness vào Claude Code (không tự sửa cấu hình)", parents=[common])
    sn.add_argument("--http", action="store_true")
    sn.add_argument("--port", type=int)
    sn.add_argument("--name", default="cfharness")
    sn.add_argument("--scope", default="user", choices=["user", "project", "local"])
    sn.add_argument("--read-only", action="store_true", help="(HTTP) dùng token mức read")

    tk = ms.add_parser("token", help="xem/đổi token cục bộ cho MCP HTTP", parents=[common])
    tk.add_argument("--rotate", action="store_true")

    def server_fields(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--transport", choices=["stdio", "http", "sse"])
        sp.add_argument("--url")
        sp.add_argument("--env", action="append", metavar="KEY=VALUE", help="giá trị secret dạng rõ tự chuyển vào secret store")
        sp.add_argument("--header", action="append", metavar="KEY=VALUE")
        sp.add_argument("--include", action="append", help="glob tool được dùng")
        sp.add_argument("--exclude", action="append", help="glob tool bị chặn")
        sp.add_argument("--require-approval", action="append", help="glob tool luôn qua phê duyệt")
        sp.add_argument("--trust", help="trusted | untrusted (giá trị lạ = untrusted)")
        sp.add_argument("--timeout", type=float, help="giây")
        sp.add_argument("--description")
        sp.add_argument("command", nargs=argparse.REMAINDER, help="(stdio) -- lệnh và tham số")

    ad = ms.add_parser("add", help="thêm MCP server: cfharness mcp add <tên> [--trust …] -- <lệnh> <args…> | --url URL", parents=[common])
    ad.add_argument("name")
    ad.add_argument("--no-probe", action="store_true")
    server_fields(ad)
    ed = ms.add_parser("edit", help="sửa MCP server (--env KEY= để xoá khoá)", parents=[common])
    ed.add_argument("name")
    ed.add_argument("--probe", action="store_true")
    server_fields(ed)
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
    for name in ("probe", "test"):
        x = ms.add_parser(name, help="kết nối + tools/list, lưu annotations (không gọi tool)", parents=[common])
        x.add_argument("name")
        x.add_argument("--timeout", type=float)
    im = ms.add_parser("import", help="import mcpServers từ ~/.claude.json (chỉ đọc)", parents=[common])
    im.add_argument("--from", dest="source", default="claude", choices=["claude"])
    im.add_argument("--file", help="đường dẫn file (mặc định ~/.claude.json)")
    im.add_argument("--project", help="kèm mcpServers cấp project của thư mục này")
    im.add_argument("--select", action="append", help="tên server, phân tách dấu phẩy")
    im.add_argument("--all", action="store_true")
    im.add_argument("--dry-run", action="store_true")
    im.add_argument("--overwrite", action="store_true")
    im.add_argument("--probe", action="store_true", help="probe ngay sau khi import")
    for name in ("assign", "unassign"):
        x = ms.add_parser(name, help=f"{'gán' if name == 'assign' else 'gỡ'} MCP server cho agent", parents=[common])
        x.add_argument("server")
        x.add_argument("agent")
    ac = ms.add_parser("agent-config", help="xem mcpServers + allow/deny sẽ dùng khi chạy agent (không lộ secret)", parents=[common])
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
    al = aus.add_parser("list", parents=[common])
    al.add_argument("--kind", help="vd tool.write, agent.* ")
    al.add_argument("--run")
    al.add_argument("--agent")
    al.add_argument("--channel")
    al.add_argument("--limit", type=int, default=50)
    au.set_defaults(fn=cmd_audit)

    fb = sub.add_parser("feedback", help="ghi phản hồi về một run (Signal)", parents=[common])
    fb.add_argument("run_id")
    fb.add_argument("kind", choices=["good", "wrong", "partial"])
    fb.add_argument("--note")
    fb.add_argument("--corrected-output")
    fb.set_defaults(fn=cmd_feedback)
