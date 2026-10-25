"""CLI `cfharness system scan|list|show|add|rm|lock|unlock|pause|resume|restart` (client thuần của daemon, RPC `system.*`)."""

from __future__ import annotations

import argparse
from typing import Any

ICON = {"running": "●", "idle": "○", "paused": "⏸", "not_loaded": "·", "error": "✗"}


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    s = sub.add_parser("system", help="hệ thống ngoài hub (app trong ~/Workspace + job launchd/Task Scheduler): đăng ký, xem, tạm dừng/bật lại job", parents=[common])
    ss = s.add_subparsers(dest="sys_action")
    sc = ss.add_parser("scan", help="quét thư mục gốc tìm hệ thống + job launchd/Task Scheduler (--apply để đăng ký hệ thống mới; chỉ người)", parents=[common])
    sc.add_argument("--apply", action="store_true")
    ss.add_parser("list", help="hệ thống đã đăng ký + trạng thái job", parents=[common])
    sh = ss.add_parser("show", help="chi tiết một hệ thống (--log: đuôi log mỗi job)", parents=[common])
    sh.add_argument("id")
    sh.add_argument("--log", type=int, nargs="?", const=3000, default=0, metavar="BYTES")
    ad = ss.add_parser("add", help="đăng ký một thư mục làm hệ thống (chỉ người)", parents=[common])
    ad.add_argument("path")
    ad.add_argument("--id")
    rm = ss.add_parser("rm", help="gỡ hệ thống khỏi hub (không đụng file/job; chỉ người)", parents=[common])
    rm.add_argument("id")
    for name, hlp in (("lock", "khoá job: điều phối viên không thao tác được (chỉ người)"), ("unlock", "bỏ khoá job (chỉ người)")):
        p = ss.add_parser(name, help=hlp, parents=[common])
        p.add_argument("label")
    for name, hlp in (("pause", "tạm dừng job (launchd: bootout + disable; Windows: /End + /DISABLE — giữ qua khởi động lại máy)"), ("resume", "bật lại job đã tạm dừng"),
                      ("restart", "khởi động lại job (launchd: kickstart -k; Windows: /End + /Run)")):
        p = ss.add_parser(name, help=hlp, parents=[common])
        p.add_argument("label")
        p.add_argument("--reason", default="")
    s.set_defaults(fn=cmd_system)


def _print_system(s: dict[str, Any], *, logs: bool = False) -> None:
    print(f"{s['id']}  —  {s.get('title') or ''}\n  {s['path']}")
    if not s["jobs"]:
        print("  (không có job launchd/Task Scheduler)")
    for j in s["jobs"]:
        extra = f" pid {j['pid']}" if j.get("pid") else (f" exit {j['last_exit']}" if j.get("last_exit") not in (None, 0) else "")
        pb = j.get("paused_by")
        who = f" · dừng bởi {pb['by']} {str(pb.get('at') or '')[:16]}" + (f" ({pb['reason']})" if pb.get("reason") else "") if pb else ""
        print(f"  {ICON.get(j['status'], '?')} {j['label']:<36} {j['status_vi']:<18}{extra} · {j['schedule']}" + (" · 🔒" if j.get("locked") else "") + who)
        for k, v in (j.get("log") or {}).items() if logs else ():
            if v:
                print(f"    ── {k} ──")
                print("    " + v.rstrip().replace("\n", "\n    "))
        if logs and j.get("log_note"):
            print(f"    (log: {j['log_note']})")


def cmd_system(ctx: Any) -> int:
    a = ctx.args
    act = a.sys_action or "list"
    if act == "scan":
        r = ctx.call("system.scan", {"apply": a.apply})
        if ctx.json:
            ctx.out(r)
            return 0
        print(f"Gốc: {', '.join(r['roots'])} — tìm thấy {len(r['found'])} hệ thống, {sum(len(s['jobs']) for s in r['found'])} job")
        new = set(r["new"])
        for s in r["found"]:
            print(f"  {'+' if s['id'] in new else ' '} {s['id']:<34} {len(s['jobs']):>2} job  {s['title'][:60]}")
        if r["gone"]:
            print(f"Đã đăng ký nhưng không còn thấy: {', '.join(r['gone'])}")
        print(f"Đã đăng ký {len(new)} hệ thống mới (tổng {r['registered']})." if r["applied"]
              else (f"{len(new)} hệ thống chưa đăng ký — chạy `cfharness system scan --apply` để đăng ký." if new else "Không có hệ thống mới."))
        return 0
    if act == "list":
        rows = ctx.call("system.list", {})
        if ctx.json:
            ctx.out(rows)
            return 0
        if not rows:
            print("Chưa đăng ký hệ thống nào — `cfharness system scan` rồi `cfharness system scan --apply`.")
        for s in rows:
            _print_system(s)
        return 0
    if act == "show":
        s = ctx.call("system.get", {"id": a.id, "log_bytes": a.log or None})
        ctx.out(s) if ctx.json else _print_system(s, logs=True)
        return 0
    if act == "add":
        s = ctx.call("system.add", {"path": a.path, "id": a.id})
        ctx.out(s) if ctx.json else _print_system(s)
        return 0
    if act == "rm":
        r = ctx.call("system.rm", {"id": a.id})
        ctx.out(r) if ctx.json else print(f"Đã gỡ {r['id']} ({r['path']}) khỏi hub — file và job giữ nguyên.")
        return 0
    if act in ("lock", "unlock"):
        r = ctx.call("system.lock", {"label": a.label, "on": act == "lock"})
        ctx.out(r) if ctx.json else print(f"{'Đã khoá' if r['locked'] else 'Đã bỏ khoá'} {r['label']} ({r['system']}).")
        return 0
    r = ctx.call("system.job", {"label": a.label, "action": act, "reason": a.reason})
    if ctx.json:
        ctx.out(r)
    else:
        print(f"{r['label']} ({r['system']}): {r['detail']} → {r['status_vi']}")
    return 0
