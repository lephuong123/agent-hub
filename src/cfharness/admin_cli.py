"""CLI `cfharness admin …` — quản trị máy nhiều người dùng (admin_overview.py, admin_rpc.py; docs/dev-notes/admin-overview.md, admin-users.md).

- `overview` · `users list|show|add|revoke` · `estop on|off|list` · `alerts` · `audit` · `policy show|check|template` · `setup check`
  (phía admin) · `me` (phía user: trang minh bạch — đúng những gì hub này xuất cho admin, chính sách, ESTOP admin, admin-log).
- Đọc/ESTOP: client thuần qua RPC `admin.*` (∈ HUMAN_ONLY; phía admin daemon còn kiểm nhóm cfharness-admin).
- `users add|revoke`, `setup check`, `policy template`: KHÔNG gọi daemon — mặc định chỉ IN lệnh sudo; `--write` (root) mới chạy.
(+ `install-daemon|uninstall-daemon` từ systems/cli.py.)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

STATE_VI = {"ok": "đang chạy", "stale": "QUÁ HẠN", "stopped": "đã dừng", "malformed": "file hỏng", "owner_mismatch": "SAI CHỦ FILE"}
LEVEL = {"ok": "ok  ", "warn": "CẢNH", "fail": "LỖI "}


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    from .admin_control import REASON_CODES
    from .systems.cli import add_admin_daemon_parsers

    a = sub.add_parser("admin", help="quản trị máy nhiều người dùng (chỉ người): overview | users | estop | alerts | audit | policy | setup | me | install-daemon",
                       parents=[common])
    asub = a.add_subparsers(dest="admin_cmd", required=True)
    add_admin_daemon_parsers(asub, common)
    o = asub.add_parser("overview", help="tổng quan mọi hub trên máy: chi phí, run, duyệt/câu hỏi, ESTOP (chỉ người)", parents=[common])
    o.set_defaults(fn=cmd_overview)

    u = asub.add_parser("users", help="người dùng của máy: list | show <user> | add <user> | revoke <user>", parents=[common])
    us = u.add_subparsers(dest="users_cmd", required=True)
    us.add_parser("list", help="mọi user: nhóm, LaunchDaemon, thư mục, trạng thái", parents=[common]).set_defaults(fn=cmd_users_list)
    sh = us.add_parser("show", help="chi tiết một user: hub, cảnh báo, lịch sử, chính sách, admin-log", parents=[common])
    sh.add_argument("user")
    sh.add_argument("--days", type=int, default=30)
    sh.set_defaults(fn=cmd_users_show)
    for name, hlp in (("add", "cấp quyền: nhóm cfharness-users + thư mục riêng + LaunchDaemon (mặc định chỉ in lệnh sudo)"),
                      ("revoke", "thu quyền: ESTOP riêng user + gỡ LaunchDaemon + gỡ khỏi nhóm (không xoá HUB_HOME; mặc định chỉ in lệnh)")):
        x = us.add_parser(name, help=hlp, parents=[common])
        x.add_argument("user")
        x.add_argument("--write", action="store_true", help="chạy thật (cần root)")
        if name == "add":
            x.add_argument("--no-daemon", action="store_true", help="không cài LaunchDaemon")
            x.add_argument("--web-port", type=int, default=None)
            x.add_argument("--system-install", action="store_true", help="LaunchDaemon chạy bản cài root-owned /opt/cfharness")
        x.set_defaults(fn=cmd_users_grant, grant=name)

    e = asub.add_parser("estop", help="ESTOP quản trị máy: on|off [USER|--all] | list (nhóm cfharness-admin, không cần sudo)", parents=[common])
    es = e.add_subparsers(dest="estop_cmd", required=True)
    for name in ("on", "off"):
        x = es.add_parser(name, help=f"{'bật' if name == 'on' else 'gỡ'} cờ ESTOP cho một user hoặc mọi user", parents=[common])
        x.add_argument("user", nargs="?", default=None)
        x.add_argument("--all", action="store_true", help="mọi user trên máy")
        if name == "on":
            x.add_argument("--reason", choices=REASON_CODES, default="other", help="mã lý do (không có chữ tự do tới agent)")
        x.set_defaults(fn=cmd_estop, on=name == "on")
    es.add_parser("list", help="cờ ESTOP admin đang bật", parents=[common]).set_defaults(fn=cmd_estop_list)

    asub.add_parser("alerts", help="cảnh báo đang mở + gần đây (quá hạn, ngân sách, tamper, chính sách…)", parents=[common]).set_defaults(fn=cmd_alerts)
    au = asub.add_parser("audit", help="audit METADATA của các user (loại thao tác, kênh, kết quả — không nội dung)", parents=[common])
    au.add_argument("--user", default=None)
    au.add_argument("--days", type=int, default=7)
    au.add_argument("--limit", type=int, default=200)
    au.set_defaults(fn=cmd_audit)

    po = asub.add_parser("policy", help="chính sách root-owned: show [--user U] | check | template", parents=[common])
    ps = po.add_subparsers(dest="policy_cmd", required=True)
    for name in ("show", "check"):
        x = ps.add_parser(name, help="chính sách có hiệu lực" + (" + lỗi file (mã thoát 1 khi có lỗi)" if name == "check" else ""), parents=[common])
        x.add_argument("--user", default=None)
        x.set_defaults(fn=cmd_policy)
    ps.add_parser("template", help="in mẫu JSON + lệnh sudo cài file chính sách (web/CLI không tự sửa chính sách)",
                  parents=[common]).set_defaults(fn=cmd_policy_template)

    se = asub.add_parser("setup", help="setup check [--runbook]: kiểm nhóm + thư mục chung", parents=[common])
    ss = se.add_subparsers(dest="setup_cmd", required=True)
    sc = ss.add_parser("check", help="kiểm nhóm cfharness-admin/cfharness-users + chủ/quyền thư mục chung, in lệnh sửa", parents=[common])
    sc.add_argument("--runbook", action="store_true", help="in toàn bộ lệnh thiết lập một lần")
    sc.set_defaults(fn=cmd_setup_check)
    asub.add_parser("me", help="phía user: hub này xuất gì cho admin, chính sách, ESTOP admin, admin-log",
                    parents=[common]).set_defaults(fn=cmd_me)


def _age(s: Any) -> str:
    if s is None:
        return "?"
    s = float(s)
    return f"{int(s)} s" if s < 120 else (f"{int(s // 60)} ph" if s < 7200 else f"{s / 3600:.1f} h")


def render_text(r: dict[str, Any]) -> str:
    if not r.get("exists"):
        return (f"Thư mục trạng thái {r.get('dir')} chưa có — xem docs/dev-notes/admin-overview.md "
                "(tạo nhóm cfharness-admin + thư mục, rồi mỗi hub: config set admin_export.enabled true).")
    lines = [f"Tổng quan máy · {r['dir']} · {len(r['hubs'])} hub"]
    hdr = f"{'user':<16} {'trạng thái':<13} {'phiên bản':<10} {'$ hôm nay':>10} {'$ 7 ngày':>10} {'chạy':>5} {'chờ':>4} {'lỗi':>4} {'duyệt':>6} {'hỏi':>4} {'agent':>5}  estop  cập nhật"
    lines.append(hdr)
    for h in r["hubs"]:
        d = h.get("data") or {}
        c, ru = d.get("cost_usd") or {}, d.get("runs") or {}
        st = STATE_VI.get(h["state"], h["state"])
        if not d:
            lines.append(f"{h['user']:<16} {st:<13} ({h.get('error') or '-'})")
            continue
        lines.append(f"{h['user']:<16} {st:<13} {d.get('version', '?'):<10} {c.get('today', 0):>10.4f} {c.get('week', 0):>10.4f} "
                     f"{ru.get('running', 0):>5} {ru.get('queued', 0):>4} {ru.get('today_failed', 0):>4} {(d.get('approvals') or {}).get('open', 0):>6} "
                     f"{(d.get('questions') or {}).get('open', 0):>4} {(d.get('agents') or {}).get('total', 0):>5}  "
                     f"{'BẬT' if (d.get('estop') or {}).get('on') else '-':<5}  {_age(h.get('age_s'))} trước")
    t = r.get("totals") or {}
    lines.append(f"Tổng: ${t.get('cost_today_usd', 0):.4f} hôm nay · ${t.get('cost_week_usd', 0):.4f} 7 ngày · {t.get('running', 0)} đang chạy · "
                 f"{t.get('queued', 0)} chờ · {t.get('failed_today', 0)} lỗi hôm nay · {t.get('approvals_open', 0)} chờ duyệt · "
                 f"{t.get('questions_open', 0)} câu hỏi · {t.get('stale', 0)} quá hạn · {t.get('bad', 0)} file lỗi")
    me = r.get("self") or {}
    if not me.get("enabled"):
        lines.append("(hub này chưa xuất trạng thái — `cfharness config set admin_export.enabled true`)")
    elif me.get("last_error"):
        lines.append(f"(hub này xuất lỗi: {me['last_error']})")
    return "\n".join(lines)


def cmd_overview(ctx: Any) -> int:
    r = ctx.call("admin.overview", {})
    ctx.out(r, render_text(r))
    return 0


def render_users(r: dict[str, Any]) -> str:
    from .admin_watch import ALERT_VI

    if not r.get("exists"):
        return f"Thư mục trạng thái {r.get('dir')} chưa có — `cfharness admin setup check`."
    lines = [f"{'user':<16} {'trạng thái':<24} {'nhóm':<5} {'plist':<5} {'thư mục':<7} {'hub':>3} {'$ hôm nay':>10} {'$ tháng':>10} {'chạy':>4}"]
    for u in r.get("users") or []:
        lines.append(f"{u['user']:<16} {u['state_vi']:<24} {'có' if u['member'] else '-':<5} {'có' if u['plist'] else '-':<5} "
                     f"{'có' if u['granted'] else '-':<7} {u['hubs']:>3} {u['cost_today_usd']:>10.4f} {u['cost_month_usd']:>10.4f} {u['running']:>4}")
    al = r.get("alerts") or []
    if al:
        lines.append(f"Cảnh báo đang mở ({len(al)}):")
        lines += [f"  - {a.get('user')}{' / ' + a['hub_id'] if a.get('hub_id') else ''}: {ALERT_VI.get(a.get('code'), a.get('code'))}" for a in al]
    fl = r.get("flags") or []
    if fl:
        lines.append("ESTOP quản trị máy: " + ", ".join(f"{f.get('user') or 'MỌI USER'} ({f.get('reason_code')})" for f in fl))
    return "\n".join(lines)


def cmd_users_list(ctx: Any) -> int:
    r = ctx.call("admin.users", {})
    ctx.out(r, render_users(r))
    return 0


def render_user(r: dict[str, Any]) -> str:
    from .admin_watch import ALERT_VI

    row = r.get("row") or {}
    lines = [f"User {r['user']} — {row.get('state_vi')} · nhóm {'có' if row.get('member') else 'KHÔNG'} · LaunchDaemon {'có' if row.get('plist') else 'không'}"]
    for h in r.get("hubs") or []:
        d = h.get("data") or {}
        c = d.get("cost_usd") or {}
        lines.append(f"  hub {h.get('hub_id') or '-'}: {STATE_VI.get(h['state'], h['state'])} · v{d.get('version', '?')} · "
                     f"${float(c.get('today') or 0):.4f} hôm nay · ${float(c.get('month') or 0):.4f} tháng")
    for a in r.get("alerts") or []:
        lines.append(f"  ! {ALERT_VI.get(a.get('code'), a.get('code'))}")
    es = r.get("estop") or {}
    lines.append(f"  ESTOP admin: {'BẬT (' + str(es.get('scope')) + ', ' + str(es.get('reason_code')) + ')' if es.get('on') else 'tắt'}")
    pol = r.get("policy") or {}
    lines.append(f"  chính sách: rev {pol.get('rev')} · {json.dumps(pol.get('policy') or {}, ensure_ascii=False)}" + (f" · LỖI {pol['errors']}" if pol.get("errors") else ""))
    hist = (r.get("history") or {}).get("cost_by_day") or []
    if hist:
        lines.append("  chi phí theo ngày: " + " ".join(f"{x['label'][5:]}=${x['value']:.2f}" for x in hist[-7:]))
    for x in (r.get("admin_log") or [])[:10]:
        lines.append(f"  admin-log {x.get('ts')} {x.get('by')} {x.get('action')} {x.get('reason_code') or ''}")
    return "\n".join(lines)


def cmd_users_show(ctx: Any) -> int:
    r = ctx.call("admin.user", {"user": ctx.args.user, "days": ctx.args.days})
    ctx.out(r, render_user(r))
    return 0


def cmd_users_grant(ctx: Any) -> int:
    from .admin_overview import DEFAULT_DIR
    from .admin_users import AddPlan, RevokePlan

    a = ctx.args
    try:
        if a.grant == "add":
            plan: Any = AddPlan(a.user, Path(DEFAULT_DIR)).build(install_daemon=not a.no_daemon, web_port=a.web_port, system_install=a.system_install)
        else:
            plan = RevokePlan(a.user, Path(DEFAULT_DIR)).build()
    except ValueError as e:
        print(f"lỗi: {e}")
        return 1
    if a.write:
        try:
            done = plan.apply()
            if a.grant == "add" and not a.no_daemon:
                done += _install_daemon(a.user, a.web_port, a.system_install)
            elif a.grant == "revoke":
                done += _remove_daemon(a.user)
        except (PermissionError, RuntimeError, OSError, ValueError) as e:
            print(f"lỗi: {e}")
            return 1
        ctx.out({"done": done}, "\n".join(done))
        if a.grant == "add":
            print("Bước tiếp:\n  " + "\n  ".join(plan.next_steps()))
        return 0
    data = {"user": a.user, "commands": plan.commands(), **({"next_steps": plan.next_steps()} if a.grant == "add" else {})}
    text = "\n".join(plan.commands()) + (("\n# bước tiếp:\n" + "\n".join(plan.next_steps())) if a.grant == "add" else "")
    ctx.out(data, f"# lệnh (hoặc: sudo cfharness admin users {a.grant} {a.user} --write)\n" + text)
    return 0


def _install_daemon(user: str, web_port: int | None, system_install: bool) -> list[str]:
    from .systems.autostart import LaunchDaemonPlan, user_home_of

    plan = LaunchDaemonPlan(user, user_home=user_home_of(user), web_port=web_port, system_install=system_install)
    return plan.apply()


def _remove_daemon(user: str) -> list[str]:
    from .systems.autostart import LaunchDaemonPlan, user_home_of

    try:
        return LaunchDaemonPlan(user, user_home=user_home_of(user)).remove()
    except ValueError:
        return []


def cmd_estop(ctx: Any) -> int:
    a = ctx.args
    if not a.all and not a.user:
        print("lỗi: nêu USER hoặc --all")
        return 2
    p: dict[str, Any] = {"on": bool(a.on), "all": bool(a.all), "user": None if a.all else a.user}
    if a.on:
        p["reason_code"] = a.reason
    r = ctx.call("admin.estop_set", p)
    who = "MỌI user" if a.all else a.user
    ctx.out(r, f"ESTOP quản trị máy {'BẬT' if a.on else 'đã gỡ'} cho {who}" + (f" (mã {a.reason})" if a.on else "") + " — đã ghi admin-log")
    return 0


def cmd_estop_list(ctx: Any) -> int:
    r = ctx.call("admin.estop_list", {})
    fl = r.get("flags") or []
    ctx.out(r, "\n".join(f"{f.get('user') or 'MỌI USER'}: {f.get('reason_code')} từ {f.get('since')}" for f in fl) or "không có cờ ESTOP admin nào")
    return 0


def cmd_alerts(ctx: Any) -> int:
    from .admin_watch import ALERT_VI

    r = ctx.call("admin.alerts", {})
    act = r.get("active") or []
    lines = [f"Đang mở ({len(act)}):"] + [f"  {a.get('user')}{' / ' + a['hub_id'] if a.get('hub_id') else ''}: "
                                          f"{ALERT_VI.get(a.get('code'), a.get('code'))} (từ {a.get('since')})" for a in act]
    lines.append("Gần đây:")
    lines += [f"  {x.get('ts')} {x.get('user')} {x.get('code')} {'hồi phục' if x.get('state') == 'resolved' else 'BẬT'}" for x in (r.get("recent") or [])[:30]]
    ctx.out(r, "\n".join(lines))
    return 0


def cmd_audit(ctx: Any) -> int:
    a = ctx.args
    r = ctx.call("admin.audit", {"user": a.user, "days": a.days, "limit": a.limit})
    if not r.get("exists"):
        ctx.out(r, f"Chưa có thư mục audit {r.get('dir')} (root tạo audit/<user>/ khi cấp quyền).")
        return 0
    lines = [f"{x['ts']}  {x['user']:<14} {x['kind']:<28} {x.get('actor', ''):<9} {x.get('channel', ''):<9} {x.get('outcome', '')}"
             + (f" key={x['key']}" if x.get("key") else "") for x in r.get("rows") or []]
    ctx.out(r, "\n".join(lines) or "không có dòng nào")
    return 0


def cmd_policy(ctx: Any) -> int:
    r = ctx.call("admin.policy", {"user": ctx.args.user} if ctx.args.user else {})
    txt = [f"Thư mục: {r.get('dir')} ({'có' if r.get('exists') else 'chưa có'}) · nguồn: {', '.join(r.get('sources') or []) or '-'} · rev {r.get('rev')} "
           f"(máy {r.get('machine_rev')})", json.dumps(r.get("policy") or {}, ensure_ascii=False, indent=2)]
    if r.get("errors"):
        txt.append(f"Lỗi file (bỏ qua — fail-open): {', '.join(r['errors'])}")
    if r.get("blocked"):
        txt.append("CHẶN mọi run: _machine.json bắt buộc chính sách (policy_required) nhưng phần của user hỏng/thiếu")
    ctx.out(r, "\n".join(txt))
    return 0 if (ctx.args.policy_cmd != "check" or not r.get("errors")) else 1


def cmd_policy_template(ctx: Any) -> int:
    from .admin_control import policy_dir

    tpl = {"rev": 1, "budget": {"usd_per_day": 20, "usd_per_run_max": 5}, "runs": {"max_concurrent": 2},
           "require": {"sandbox": True, "confine": False, "export": True}, "models": {"allow": []}, "min_version": "0.2.8"}
    d = str(policy_dir())
    text = (json.dumps(tpl, indent=2) + "\n# lưu thành ./policy.json rồi (mặc định cho mọi user / riêng user U / toàn máy):\n"
            f"sudo install -m 0644 -o root -g wheel ./policy.json {d}/_default.json\n"
            f"sudo install -m 0644 -o root -g wheel ./policy.json {d}/U.json\n"
            f"sudo install -m 0644 -o root -g wheel ./policy.json {d}/_machine.json   # thêm \"policy_required\": true → fail-closed\n"
            "# tăng \"rev\" mỗi lần sửa — admin thấy user nào chưa áp (policy_mismatch)")
    ctx.out({"template": tpl, "dir": d}, text)
    return 0


def cmd_setup_check(ctx: Any) -> int:
    from .admin_overview import os_user
    from .admin_users import runbook, setup_check

    if ctx.args.runbook:
        cmds = runbook(os_user())
        ctx.out({"commands": cmds}, "\n".join(cmds))
        return 0
    r = setup_check()
    lines = [f"Thư mục chung: {r['root']}"]
    for c in r["checks"]:
        lines.append(f"[{LEVEL[c['level']]}] {c['detail']}")
        lines += [f"         {f}" for f in c["fix"]]
    ctx.out(r, "\n".join(lines))
    return 0 if r["level"] != "fail" else 1


def cmd_me(ctx: Any) -> int:
    r = ctx.call("admin.self", {})
    exp = r.get("export") or {}
    es = r.get("admin_estop") or {}
    pol = r.get("policy") or {}
    lines = [f"Hub của {r.get('user')} — máy {'có' if r.get('managed') else 'KHÔNG có'} quản trị chung",
             f"Xuất trạng thái cho admin: {'BẬT' if exp.get('enabled') else 'tắt'} → {exp.get('file')}" + (f" (lỗi {exp['last_error']})" if exp.get("last_error") else ""),
             "Nội dung xuất (chỉ số liệu):", json.dumps(exp.get("preview") or {}, ensure_ascii=False, indent=2),
             f"Audit metadata cho admin: {'BẬT' if (r.get('audit') or {}).get('enabled') else 'tắt'}",
             f"Chính sách quản trị: {json.dumps(pol.get('policy') or {}, ensure_ascii=False)} (rev {pol.get('rev')})" + (f" · lỗi {pol['errors']}" if pol.get("errors") else ""),
             f"ESTOP quản trị máy: {'BẬT (' + str(es.get('reason_code')) + ')' if es.get('on') else 'tắt'}", "Thao tác của admin trên hub này:"]
    lines += [f"  {x.get('ts')} {x.get('by')} {x.get('action')} {x.get('reason_code') or ''}" for x in r.get("admin_log") or []] or ["  (chưa có)"]
    ctx.out(r, "\n".join(lines))
    return 0


__all__ = ["add_parsers", "render_text", "render_user", "render_users"]
