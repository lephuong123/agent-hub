"""CLI `agent-hub manager set|get|chat|tick|conversations|history` (client thuần của daemon, RPC `manager.*`)."""

from __future__ import annotations

import argparse
import time
from typing import Any


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    mg = sub.add_parser("manager", help="điều phối viên: agent quản lý + giao việc cho các agent khác (M7)", parents=[common])
    ms = mg.add_subparsers(dest="mgr_action", required=True)
    s = ms.add_parser("set", help="chỉ định agent làm điều phối viên (chỉ người); --none để huỷ", parents=[common])
    s.add_argument("agent", nargs="?")
    s.add_argument("--none", action="store_true", help="huỷ chỉ định")
    ms.add_parser("get", help="điều phối viên hiện tại, hội thoại, nhịp", parents=[common])
    c = ms.add_parser("chat", help='nhắn điều phối viên: agent-hub manager chat "<nội dung>"', parents=[common])
    c.add_argument("text", nargs="+")
    c.add_argument("--conversation", "-c", help="id hội thoại (mặc định: hội thoại gần nhất của kênh cli)")
    c.add_argument("--new", action="store_true", help="mở hội thoại mới")
    c.add_argument("--channel", default="cli")
    c.add_argument("--no-wait", action="store_true", help="không chờ trả lời")
    c.add_argument("--timeout", type=float, default=900)
    t = ms.add_parser("tick", help="chạy một nhịp ngay (gom sự kiện đang chờ)", parents=[common])
    t.add_argument("--if-due", action="store_true", help="chỉ chạy nếu tới hạn/có sự kiện (như vòng nền)")
    cv = ms.add_parser("conversations", help="liệt kê hội thoại", parents=[common])
    cv.add_argument("--channel")
    h = ms.add_parser("history", help="lịch sử một hội thoại", parents=[common])
    h.add_argument("conversation")
    h.add_argument("--limit", type=int, default=30)
    mg.set_defaults(fn=cmd_manager)


def _print_get(g: dict[str, Any]) -> None:
    print(f"Điều phối viên: {g.get('agent_id') or '(chưa chỉ định — `agent-hub manager set <agent>`)'}")
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
    # chat
    text = " ".join(a.text)
    params: dict[str, Any] = {"channel": a.channel, "text": text}
    if a.conversation:
        params["conversation_id"] = a.conversation
    elif not a.new:
        params["reuse_latest"] = True
    res = ctx.call("manager.chat", params)
    if a.no_wait or res.get("steered"):
        ctx.out(res, f"đã gửi (hội thoại {res['conversation_id']}, run {res['run_id']}{', steer vào run đang chạy' if res.get('steered') else ''})")
        return 0
    eprint(f"(hội thoại {res['conversation_id']} · run {res['run_id']} — đang chờ điều phối viên trả lời…)")
    deadline = time.monotonic() + a.timeout
    run_id = res["run_id"]
    while time.monotonic() < deadline:
        hist = ctx.call("manager.history", {"conversation_id": res["conversation_id"], "limit": 20})
        got = [m for m in hist if m["role"] == "manager" and m.get("run_id") and m["id"] > 0 and _after_user(hist, run_id, m)]
        if got:
            if ctx.json:
                ctx.out({**res, "reply": got[-1]["text"]})
            else:
                for m in [x for x in hist if x["role"] == "notify" and _after_user(hist, run_id, x)]:
                    print(f"[thông báo] {m['text']}")
                print(got[-1]["text"])
            return 0
        time.sleep(1.0)
    eprint("hết thời gian chờ — xem sau bằng `agent-hub manager history " + res["conversation_id"] + "`")
    return 2


def _after_user(hist: list[dict[str, Any]], run_id: str, m: dict[str, Any]) -> bool:
    """Tin `m` đến SAU tin người của run `run_id` (run có thể được tiếp nối bởi run khác sau approval)."""
    uid = next((x["id"] for x in hist if x["role"] == "user" and x.get("run_id") == run_id), None)
    return uid is not None and m["id"] > uid
