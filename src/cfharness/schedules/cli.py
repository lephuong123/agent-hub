"""CLI `cfharness schedule add|list|show|pause|resume|rm|run-now|edit|preview` (client thuần của daemon, RPC `schedule.*`)."""

from __future__ import annotations

import argparse
from datetime import datetime
from typing import Any

ICON = {"active": "●", "paused": "⏸", "done": "✓"}
EXPR_HELP = ("biểu thức: cron 5 trường ('*/15 * * * *', '0 8 * * mon-fri'), 'every 30m', 'daily 08:00', 'weekdays 08:30', "
             "'weekly mon,thu 09:00', 'monthly 1 09:00', 'at 2026-10-20 09:00', 'in 30m' (giờ máy)")


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    s = sub.add_parser("schedule", help="lịch chạy tự động: điều phối viên / agent / task theo cron (kết quả về kênh chat)", parents=[common])
    ss = s.add_subparsers(dest="sched_action")

    def _common_fields(p: argparse.ArgumentParser, *, add: bool) -> None:
        p.add_argument("--kind", choices=("manager", "agent", "task"), default="manager" if add else None,
                       help="manager = gửi vào hội thoại điều phối viên (mặc định); agent = chạy một agent; task = tạo task")
        p.add_argument("--agent", dest="target", help="kind=agent: id agent chạy")
        p.add_argument("--team", help="kind=task: team nhận task")
        p.add_argument("--assignee", help="kind=task: agent nhận task")
        p.add_argument("--name")
        p.add_argument("--to", dest="deliver_to", help="nơi nhận kết quả: id hội thoại (mc_…) hoặc kênh web|cli|telegram:<chat_id>|slack:<chat_id> "
                                                       "(bỏ trống = hội thoại gần nhất của bạn)")
        p.add_argument("--budget", type=float, help="trần chi phí mỗi lượt ($)")

    ad = ss.add_parser("add", help="tạo lịch", parents=[common], description=EXPR_HELP)
    ad.add_argument("expr", help=EXPR_HELP)
    ad.add_argument("prompt", help="việc cần làm mỗi lượt")
    _common_fields(ad, add=True)
    ls = ss.add_parser("list", help="danh sách lịch", parents=[common])
    ls.add_argument("--status", choices=("active", "paused", "done"))
    sh = ss.add_parser("show", help="chi tiết + các lượt gần nhất", parents=[common])
    sh.add_argument("id")
    sh.add_argument("--fires", type=int, default=10)
    ed = ss.add_parser("edit", help="sửa lịch (chỉ người)", parents=[common])
    ed.add_argument("id")
    ed.add_argument("--expr")
    ed.add_argument("--prompt")
    _common_fields(ed, add=False)
    pa = ss.add_parser("pause", help="tạm dừng lịch", parents=[common])
    pa.add_argument("id")
    pa.add_argument("--reason", default="")
    for name, hlp in (("resume", "bật lại lịch"), ("rm", "xoá lịch (xoá mềm, giữ lịch sử lượt)"), ("run-now", "chạy một lượt ngay")):
        p = ss.add_parser(name, help=hlp, parents=[common])
        p.add_argument("id")
    pv = ss.add_parser("preview", help="xem trước các mốc chạy của một biểu thức", parents=[common])
    pv.add_argument("expr")
    pv.add_argument("--count", type=int, default=5)
    s.set_defaults(fn=cmd_schedule)


def _local(ts: Any) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(ts)


def _dest(r: dict[str, Any]) -> str:
    dt = r.get("deliver_to") or {}
    return dt.get("conversation_id") or dt.get("channel") or "hội thoại gần nhất"


def _target(r: dict[str, Any]) -> str:
    if r["kind"] == "manager":
        return "điều phối viên"
    if r["kind"] == "agent":
        return f"agent {r.get('target')}"
    o = r.get("options") or {}
    return "task → " + " / ".join(x for x in (f"team {o['team']}" if o.get("team") else "", f"agent {o['assignee']}" if o.get("assignee") else "") if x)


def _print(r: dict[str, Any], *, full: bool = False) -> None:
    from ..termsafe import clean, one_line

    print(f"{ICON.get(r['status'], '?')} {r['id']}  {one_line(r['name'])[:60]}  [{r['status']}]")
    print(f"    {r.get('describe') or r['expr']} · {_target(r)} · kết quả → {_dest(r)} · ${r.get('budget_usd') or '—'}/lượt · tạo bởi {r['created_by']}")
    print(f"    lượt tới: {_local(r.get('next_run_at'))} · lượt trước: {_local(r.get('last_run_at'))} ({r.get('last_status') or '—'})"
          + (f" · lỗi liên tiếp {r['fail_count']}" if r.get("fail_count") else "")
          + (f" · dừng: {one_line(str(r.get('paused_reason')))[:80]}" if r.get("paused_reason") else ""))
    if full:
        print("    việc mỗi lượt:\n      " + clean(r["prompt"]).replace("\n", "\n      "))
        for f in r.get("fires") or []:
            ref = f.get("run_id") or f.get("task_id") or ""
            print(f"    · {_local(f.get('fired_at'))} {f['status']:<9} {ref} {('(' + str(f['reason']) + ')') if f.get('reason') else ''}"
                  + (" [chạy ngay]" if f.get("manual") else ""))


def cmd_schedule(ctx: Any) -> int:
    a = ctx.args
    act = a.sched_action or "list"
    if act == "list":
        rows = ctx.call("schedule.list", {"status": a.status})
        if ctx.json:
            ctx.out(rows)
            return 0
        if not rows:
            print("Chưa có lịch nào — `cfharness schedule add \"daily 08:00\" \"tóm tắt góp ý mới\"`.")
        for r in rows:
            _print(r)
        return 0
    if act == "show":
        r = ctx.call("schedule.get", {"id": a.id, "fires": a.fires})
        ctx.out(r) if ctx.json else _print(r, full=True)
        return 0
    if act == "preview":
        r = ctx.call("schedule.preview", {"expr": a.expr, "count": a.count})
        if ctx.json:
            ctx.out(r)
            return 0
        print(f"{r['expr']} — {r['describe']} (giờ máy {r.get('tz') or '?'}, ~{r['runs_per_day']:g} lượt/ngày)")
        for t in r["next"]:
            print("  " + t[:16].replace("T", " "))
        return 0
    if act in ("add", "edit"):
        p: dict[str, Any] = {}
        if act == "add":
            p.update({"expr": a.expr, "prompt": a.prompt})
        else:
            p["id"] = a.id
            for k in ("expr", "prompt"):
                if getattr(a, k):
                    p[k] = getattr(a, k)
        for k in ("kind", "target", "team", "assignee", "name", "deliver_to"):
            v = getattr(a, k, None)
            if v:
                p[k] = v
        if a.budget is not None:
            p["budget_usd"] = a.budget
        r = ctx.call("schedule.create" if act == "add" else "schedule.update", p)
        if ctx.json:
            ctx.out(r)
        else:
            print(("Đã tạo lịch:" if act == "add" else "Đã sửa lịch:"))
            _print(r)
            print(f"Tắt: cfharness schedule pause {r['id']} · xoá: cfharness schedule rm {r['id']} · thử ngay: cfharness schedule run-now {r['id']}")
        return 0
    if act == "pause":
        r = ctx.call("schedule.pause", {"id": a.id, "reason": a.reason})
        ctx.out(r) if ctx.json else print(f"Đã tạm dừng {r['id']} ({r['name']}). Bật lại: cfharness schedule resume {r['id']}")
        return 0
    if act == "resume":
        r = ctx.call("schedule.resume", {"id": a.id})
        ctx.out(r) if ctx.json else print(f"Đã bật lại {r['id']} — lượt tới {_local(r.get('next_run_at'))}.")
        return 0
    if act == "rm":
        r = ctx.call("schedule.rm", {"id": a.id})
        ctx.out(r) if ctx.json else print(f"Đã xoá lịch {r['id']} ({r['name']}).")
        return 0
    r = ctx.call("schedule.run_now", {"id": a.id})
    if ctx.json:
        ctx.out(r)
    else:
        ref = f"run {r['run_id']}" if r.get("run_id") else f"task {r.get('task_id')}"
        print(f"Đã chạy lịch {r['id']} ngay: {ref}" + (f" (hội thoại {r['conversation_id']})" if r.get("conversation_id") else "")
              + " — kết quả về kênh nhận của lịch.")
    return 0
