"""CLI `cfharness manager set|get|chat|tick|conversations|history` + `cfharness chat` (client thuần của daemon, RPC `manager.*`).

`cfharness chat` / `manager chat` KHÔNG kèm nội dung → phiên chat qua lại: terminal → giao diện fullscreen (`tui/chat.py`),
`--classic` hoặc không phải TTY → REPL (`manager/repl.py`); kèm nội dung → gửi một tin và chờ trả lời qua luồng sự kiện (`repl.oneshot`)."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    mg = sub.add_parser("manager", help="điều phối viên: agent quản lý + giao việc cho các agent khác (M7)", parents=[common])
    ms = mg.add_subparsers(dest="mgr_action", required=True)
    s = ms.add_parser("set", help="chỉ định agent làm điều phối viên (chỉ người); --none để huỷ", parents=[common])
    s.add_argument("agent", nargs="?")
    s.add_argument("--none", action="store_true", help="huỷ chỉ định")
    ms.add_parser("get", help="điều phối viên hiện tại, hội thoại, nhịp", parents=[common])
    c = ms.add_parser("chat", help='nhắn điều phối viên: cfharness manager chat "<nội dung>" (không nội dung = chat qua lại)', parents=[common])
    _chat_args(c)
    t = ms.add_parser("tick", help="chạy một nhịp ngay (gom sự kiện đang chờ)", parents=[common])
    t.add_argument("--if-due", action="store_true", help="chỉ chạy nếu tới hạn/có sự kiện (như vòng nền)")
    cv = ms.add_parser("conversations", help="liệt kê hội thoại", parents=[common])
    cv.add_argument("--channel")
    h = ms.add_parser("history", help="lịch sử một hội thoại", parents=[common])
    h.add_argument("conversation")
    h.add_argument("--limit", type=int, default=30)
    cap = ms.add_parser("capabilities", help="khung quyền của điều phối viên (strict | hermes) — xem/đặt (đặt: chỉ người, audit)", parents=[common])
    cap.add_argument("cap_action", nargs="?", default="show", choices=["show", "set", "reset"])
    cap.add_argument("--profile", choices=["strict", "hermes"])
    cap.add_argument("--tools", help="danh sách tool phân cách bằng dấu phẩy (ghi đè profile); 'profile' = theo profile")
    cap.add_argument("--mcp", choices=["none", "read_only_trusted", "trusted", "profile"])
    cap.add_argument("--web", choices=["on", "off", "profile"])
    cap.add_argument("--connectors", help="connector claude.ai cho phép, phân cách dấu phẩy (vd \"Claude Docs,Atlassian\"); cần --mcp trusted; "
                     "'none' = tắt. Bật = run nạp cả cấu hình user ~/.claude (docs/dev-notes/manager-connectors.md)")
    cap.add_argument("--sync-agent", action="store_true", help="đặt luôn tool_policy.tools của agent điều phối viên = tool của khung mới")
    mg.set_defaults(fn=cmd_manager)
    ch = sub.add_parser("chat", help="chat qua lại với điều phối viên trên terminal (kênh cli, dùng chung với TUI `top` → m)", parents=[common])
    _chat_args(ch)
    ch.set_defaults(fn=cmd_chat)


def _chat_args(c: argparse.ArgumentParser) -> None:
    c.add_argument("text", nargs="*", help="nội dung; bỏ trống = vào phiên chat qua lại")
    c.add_argument("--conversation", "-c", help="id hội thoại (mặc định: hội thoại gần nhất của kênh cli)")
    c.add_argument("--new", action="store_true", help="mở hội thoại mới")
    c.add_argument("--channel", default="cli", help=argparse.SUPPRESS)
    c.add_argument("--no-wait", action="store_true", help="không chờ trả lời (chỉ khi có nội dung)")
    c.add_argument("--timeout", type=float, default=900, help="giây chờ mỗi câu trả lời")
    c.add_argument("--classic", action="store_true",
                   help="chat qua lại bằng REPL dòng lệnh cũ (prompt_toolkit) thay cho giao diện fullscreen (chuột, cuộn, bảng agent)")


def _print_get(g: dict[str, Any]) -> None:
    print(f"Điều phối viên: {g.get('agent_id') or '(chưa chỉ định — `cfharness manager set <agent>`)'}")
    tk = g.get("tick") or {}
    print(f"  nhịp: {'bật' if tk.get('heartbeat') else 'tắt'} · mỗi {int(tk.get('interval_s') or 0)} s · sự kiện chờ: {tk.get('pending_events', 0)}"
          f" · tick gần nhất: {g.get('last_tick') or '-'} · kế tiếp: {g.get('next_tick') or '-'}" + (" · ĐANG CHẠY" if tk.get("running") else ""))
    for c in (g.get("conversations") or [])[:10]:
        print(f"  {c['id']}  {c['channel']:<14} {c.get('messages', 0):>3} tin · {c['updated_at']}  {c.get('title') or ''}")


def cmd_manager(ctx: Any) -> int:
    from ..cli import eprint

    a = ctx.args
    act = a.mgr_action
    if act == "set":
        if not a.none and not a.agent:
            eprint("lỗi: cần <agent> hoặc --none")
            return 1
        g = ctx.call("manager.set", {"agent_id": None if a.none else a.agent})
        if ctx.json:
            ctx.out(g)
        else:
            _print_get(g)
        return 0
    if act == "get":
        g = ctx.call("manager.get")
        if ctx.json:
            ctx.out(g)
        else:
            _print_get(g)
        return 0
    if act == "conversations":
        rows = ctx.call("manager.conversations", {"channel": a.channel})
        if ctx.json:
            ctx.out(rows)
        else:
            for c in rows:
                print(f"{c['id']}  {c['channel']:<14} {c.get('messages', 0):>3} tin · {c['updated_at']}  {c.get('title') or ''}")
        return 0
    if act == "history":
        rows = ctx.call("manager.history", {"conversation_id": a.conversation, "limit": a.limit})
        if ctx.json:
            ctx.out(rows)
        else:
            for m in rows:
                print(f"[{m['ts'][:19]}] {m['role']}: {m['text']}")
        return 0
    if act == "tick":
        res = ctx.call("manager.tick", {"force": not a.if_due})
        if ctx.json:
            ctx.out(res)
        else:
            print(f"tick: run {res['run_id']} ({res.get('reason')}, {res.get('events', 0)} sự kiện)" if res.get("run_id") else f"tick bỏ qua: {res.get('skipped')}")
        return 0
    if act == "capabilities":
        return _cmd_capabilities(ctx)
    return cmd_chat(ctx)


def _print_caps(info: dict[str, Any]) -> None:
    e = info.get("effective") or {}
    print(f"Khung điều phối viên: {e.get('profile')}  (cấu hình: {info.get('config')})")
    print(f"  tool: {', '.join(e.get('tools') or []) or '-'}")
    print(f"  MCP: {e.get('mcp')} · web: {'bật' if e.get('web') else 'tắt'}")
    if e.get("connectors"):
        print(f"  connector claude.ai: {', '.join(e['connectors'])}  (run nạp cấu hình user ~/.claude; MCP ngoài agent/connector bị guard chặn)")
    for w in e.get("warnings") or []:
        print(f"  ! {w}")
    if info.get("manager"):
        print(f"  agent điều phối viên: {info['manager']} · tools: {info.get('manager_tools')}")
        print(f"  {'VƯỢT KHUNG: ' + info['problem'] if info.get('problem') else 'cấu hình agent nằm trong khung'}")
    print("  luôn giữ: hook guard + approval, sandbox OS, permissions.deny; không bypass/extra_args/subagents; không duyệt/policy/purge/ESTOP off")


def _cmd_capabilities(ctx: Any) -> int:
    from ..cli import eprint

    a = ctx.args
    if a.cap_action == "show":
        info = ctx.call("manager.capabilities")
        ctx.out(info) if ctx.json else _print_caps(info)
        return 0
    if a.cap_action == "reset":
        new: dict[str, Any] = {"profile": "strict", "tools": None, "mcp": None, "web": None, "connectors": None}
    else:
        cur = (ctx.call("manager.capabilities") or {}).get("config") or {}
        new = {k: cur.get(k) for k in ("profile", "tools", "mcp", "web", "connectors")}
        if not any(x is not None for x in (a.profile, a.tools, a.mcp, a.web, a.connectors)):
            eprint("lỗi: cần ít nhất một trong --profile/--tools/--mcp/--web/--connectors")
            return 1
        if a.profile:
            new["profile"] = a.profile
        if a.tools is not None:
            new["tools"] = None if a.tools == "profile" else [t.strip() for t in a.tools.split(",") if t.strip()]
        if a.mcp:
            new["mcp"] = None if a.mcp == "profile" else a.mcp
        if a.web:
            new["web"] = None if a.web == "profile" else a.web == "on"
        if a.connectors is not None:
            names = [c.strip() for c in a.connectors.split(",") if c.strip()]
            new["connectors"] = None if names in ([], ["none"]) else names
    ctx.call("config.set", {"key": "manager.capabilities", "value": new})
    info = ctx.call("manager.capabilities")
    if a.sync_agent and info.get("manager"):
        ctx.call("agent.update", {"id": info["manager"], "changes": {"tool_policy": {"tools": (info.get("effective") or {}).get("tools") or []}}})
        info = ctx.call("manager.capabilities")
    ctx.out(info) if ctx.json else _print_caps(info)
    return 0


def cmd_chat(ctx: Any) -> int:
    from ..cli import eprint
    from . import repl

    a = ctx.args
    ctx.client()  # tự khởi động daemon nếu chưa chạy
    if not a.text:
        if ctx.json:
            eprint("lỗi: chat qua lại không hỗ trợ --json — dùng `cfharness manager chat \"<nội dung>\" --json`")
            return 1
        if a.channel != "cli":
            eprint("lỗi: chat qua lại chỉ dùng kênh cli")
            return 1
        if not a.classic and sys.stdin.isatty() and sys.stdout.isatty():
            from ..tui.chat import run_chat  # fullscreen kiểu Claude Code (tui/chat.py)

            return run_chat(ctx.paths.socket, ctx.paths.home, conversation_id=a.conversation, new=a.new)
        return asyncio.run(repl.repl(ctx.paths.socket, ctx.paths.home, conversation_id=a.conversation, new=a.new, timeout=a.timeout))
    from . import attachments as attach

    words, images = [], []
    for w in a.text:  # shell đã bỏ escape: mỗi đối số là một token; file ảnh có thật → gửi kèm
        p = Path(w).expanduser()
        if p.suffix.lower() in attach.IMAGE_SUFFIXES and p.is_file():
            att, err = attach.read_image(p)
            if err:
                eprint(f"lỗi: {err}")
                return 1
            images.append(att)
            words.append(f"[ảnh: {p.name}]")
        else:
            words.append(w)
    text = " ".join(words)
    params: dict[str, Any] = {"channel": a.channel, "text": text}
    if images:
        params["images"] = [x.wire() for x in images]
    if a.conversation:
        params["conversation_id"] = a.conversation
    elif not a.new:
        params["reuse_latest"] = True
    notes: list[str] = []
    if not a.no_wait and not ctx.json:
        eprint("(đang chờ điều phối viên trả lời… Ctrl-C để thôi chờ — tin vẫn được xử lý)")
    res, reply = asyncio.run(repl.oneshot(ctx.paths.socket, params, wait=not a.no_wait, timeout=a.timeout, on_notify=notes.append))
    if a.no_wait or res.get("steered"):
        ctx.out(res, f"đã gửi (hội thoại {res['conversation_id']}, run {res['run_id']}{', steer vào run đang chạy' if res.get('steered') else ''})")
        return 0
    if reply is None:
        eprint("hết thời gian chờ — xem sau bằng `cfharness manager history " + str(res.get("conversation_id")) + "`")
        return 2
    if ctx.json:
        ctx.out({**res, "reply": reply.get("text"), "ok": reply.get("ok", True)})
    else:
        from ..termsafe import clean

        for n in notes:
            print(f"[thông báo] {clean(n)}")
        print(clean(reply.get("text")))
    return 0
