"""Lệnh CLI của luồng B: `approvals`, `approve <id>`, `deny <id>`, `guard test|policy|protect|decisions`,
`notify test`, `run continue <run|session> "<tin>"`, `run resume <run>`, `agent purge <id> --confirm <id>`.

`run continue` / `agent purge` đụng parser cũ (`run <agent> <prompt>`, nhóm `agent`) nên được viết lại thành
lệnh ẩn `run-continue` / `run-resume` / `agent-purge` trước khi parse (`rewrite_argv`).
"""

from __future__ import annotations

import argparse
import json
from typing import Any

HIDDEN = {("run", "continue"): "run-continue", ("run", "resume"): "run-resume", ("agent", "purge"): "agent-purge",
          ("approvals", "stats"): "approvals-stats", ("approvals", "profile"): "approvals-profile"}


def rewrite_argv(argv: list[str]) -> list[str]:
    pos = [i for i, a in enumerate(argv) if not a.startswith("-")]
    if len(pos) >= 2 and pos[1] == pos[0] + 1:
        key = (argv[pos[0]], argv[pos[1]])
        if key in HIDDEN:
            return argv[: pos[0]] + [HIDDEN[key]] + argv[pos[1] + 1 :]
    return argv


def _ago(row: dict[str, Any]) -> str:
    from datetime import datetime, timezone

    try:
        t = datetime.fromisoformat(row["opened_at"].replace("Z", "+00:00"))
        s = int((datetime.now(timezone.utc) - t).total_seconds())
    except Exception:
        return "-"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


def cmd_approvals(ctx: Any) -> int:
    from ..cli import _table

    from ..ui import views

    rows = ctx.call("approval.list", {"status": "all" if ctx.args.all else "pending", "limit": ctx.args.limit})
    view = lambda: views.approvals(rows, ctx.mode, width=ctx.width, ago=_ago, show_all=bool(ctx.args.all))  # noqa: E731 - màn TTY (ui/views.py)
    if not rows:
        ctx.out(rows, "(không có yêu cầu phê duyệt nào đang chờ)" if not ctx.args.all else "(chưa có approval)", view)
        return 0
    ctx.out(
        rows,
        _table(
            [[r["id"], r["status"], r["agent_id"], r["run_id"], _ago(r), r.get("matched_rule"), (r.get("preview") or "")[:60]] for r in rows],
            ["ID", "STATUS", "AGENT", "RUN", "CHỜ", "LUẬT", "THAO TÁC"],
        )
        + "\n\nduyệt: cfharness approve <id>   từ chối: cfharness deny <id> --note '…'",
        view,
    )
    return 0


def cmd_approvals_stats(ctx: Any) -> int:
    """`cfharness approvals stats [--days 30] [--json]` — vì sao agent phải dừng hỏi: theo luật / agent / tool / kết quả / người quyết."""
    from ..cli import _table

    st = ctx.call("approval.stats", {"days": ctx.args.days})

    def sec(title: str, rows: list[dict[str, Any]], top: int = 15) -> str:
        if not rows:
            return f"{title}: (không có)"
        body = _table([[r["key"], r["count"], r.get("allowed", 0), r.get("denied", 0), r.get("cancelled", 0), r.get("pending", 0),
                        _fmt_s(r.get("wait_median_s")), _fmt_s(r.get("wait_max_s"))] for r in rows[:top]],
                      ["", "SỐ", "CHO", "TỪ CHỐI", "HUỶ", "ĐANG CHỜ", "CHỜ (TRUNG VỊ)", "CHỜ (MAX)"])
        return f"{title}\n{body}"

    w = st.get("wait") or {}
    head = (f"approval {st['days']:g} ngày qua (từ {st['since'][:10]}): {st['total']} yêu cầu, {st['pending']} đang chờ; "
            f"thời gian chờ trung vị {_fmt_s(w.get('median_s'))}, max {_fmt_s(w.get('max_s'))}\n"
            "phân bố chờ: " + ", ".join(f"{k} {v}" for k, v in (w.get("buckets") or {}).items()))
    auto = st.get("auto_allowed_without_approval") or []
    parts = [head, sec("THEO LUẬT", st["by_rule"]), sec("THEO AGENT", st["by_agent"]), sec("THEO TOOL", st["by_tool"]),
             sec("THEO KẾT QUẢ", st["by_outcome"]), sec("AI QUYẾT", st["by_decider"]), sec("ĐỊNH TUYẾN (routed_to)", st["by_routed_to"])]
    if auto:
        parts.append("hub tự cho phép không mở approval (destructive/uncertain): " + ", ".join(f"{a['classifier']} {a['n']}" for a in auto))
    ctx.out(st, "\n\n".join(parts))
    return 0


def _fmt_s(v: Any) -> str:
    if v is None:
        return "-"
    v = float(v)
    return f"{v:.0f}s" if v < 120 else f"{v / 60:.0f}m" if v < 7200 else f"{v / 3600:.1f}h"


def parse_duration(s: str | None) -> float | None:
    """`90s`, `30m`, `4h`, `2d` hoặc số giây → giây. None/rỗng → None. Sai định dạng → ValueError."""
    if not s:
        return None
    import re

    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(s).lower())
    if not m:
        raise ValueError(f"thời hạn không hợp lệ: {s!r} (vd 30m, 4h, 2d)")
    v = float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    if v <= 0:
        raise ValueError("thời hạn phải > 0")
    return v


def trust_text(rows: dict[str, Any]) -> str:
    if not rows:
        return "(không agent nào đang trust full)"
    return "\n".join(f"⚠ {aid}: trust FULL — " + (f"hết hạn {t['until']}" if t.get("until") else "không hết hạn")
                     + f" (bởi {t.get('by') or '?'} qua {t.get('channel') or '?'}, lúc {t.get('at') or '?'})" for aid, t in sorted(rows.items()))


def cmd_approvals_profile(ctx: Any) -> int:
    r = ctx.call("approval.profile")
    lines = [f"approval.profile = {r['profile']}  (đổi — chỉ người: cfharness config set approval.profile strict|balanced|autonomous)"]
    for k, v in (r.get("knobs") or {}).items():
        lines.append(f"  {k} = {str(v['value']).lower()}  ({'đặt rõ trong config.yaml' if v['source'] == 'explicit' else 'theo profile'})")
    d = r.get("delegate_to_manager") or {}
    lines.append(f"  giao điều phối viên: {'bật' if d.get('enabled') else 'tắt'} · lớp {', '.join(d.get('classes') or []) or '-'} · "
                 f"quá {int(d.get('timeout_s') or 0)}s → người")
    lines.append(f"  hết giờ giữ (mặc định): {r.get('on_timeout')}")
    ctx.out(r, "\n".join(lines))
    return 0


def cmd_decide(ctx: Any) -> int:
    a = ctx.args
    method = "approval.approve" if a.cmd == "approve" else "approval.deny"
    params = {"id": a.id, "note": a.note, "channel": "cli", "by": "user"}
    if a.cmd == "approve" and getattr(a, "always", False):
        params["always"] = True
    r = ctx.call(method, params)
    extra = ""
    if r.get("always_rule"):
        ar = r["always_rule"]
        extra = (f"\n  luật luôn cho phép {ar['id']} ({ar['kind']}: " + (ar.get("domain") or (ar.get("preview") or "")[:60]) + f", agent {ar.get('agent_id')})"
                 f" — gỡ: cfharness guard always rm {ar['id']}")
    ctx.out(r, f"{r['id']}: {r['status']} (chờ {r.get('wait_s')}s) — {r.get('preview')}{extra}")
    return 0


def cmd_guard(ctx: Any) -> int:
    from ..cli import _table

    a = ctx.args
    if a.action == "test":
        p: dict[str, Any] = {"cwd": a.cwd}
        if a.tool and a.tool != "Bash":
            p["tool"], p["input"] = a.tool, json.loads(a.input or "{}")
        else:
            p["command"] = a.command
        if a.run:
            p["run_id"] = a.run
        v = ctx.call("guard.test", p)
        mark = {"safe": "✓ an toàn", "write": "✎ ghi (chạy, có audit)", "destructive": "✗ PHÁ HUỶ (giữ chờ người)", "uncertain": "? KHÔNG CHẮC (giữ chờ người)"}[v["verdict"]]
        if v.get("forbidden"):
            mark = "⛔ CẤM (từ chối thẳng)"
        ctx.out(v, f"{mark}\n  luật: {v['rule']}  nhóm: {v.get('klass') or '-'}\n  lý do: {v.get('reason') or '-'}" + (f"\n  tài nguyên bảo vệ: {v['protected_id']}" if v.get("protected_id") else ""))
        return 0
    if a.action == "policy":
        pol = ctx.call("guard.policy")
        ctx.out(pol, json.dumps(pol, ensure_ascii=False, indent=2))
        return 0
    if a.action == "decisions":
        rows = ctx.call("guard.decisions", {"run_id": a.run, "limit": a.limit})
        ctx.out(rows, _table([[r["ts"], r["run_id"], r["tool_name"], r["verdict"], r["action"], r["matched_rule"], r.get("approval_request_id") or ""] for r in rows], ["TS", "RUN", "TOOL", "VERDICT", "ACTION", "RULE", "APPROVAL"]) if rows else "(chưa có)")
        return 0
    if a.action == "sandbox":
        if a.op == "list":
            rows = ctx.call("guard.sandbox.list")
            ctx.out(rows, _table([[k, "localhost/loopback" if v.get("allow_localhost") else "-", ",".join(v.get("allow_domains") or []) or "-",
                                   ",".join(v.get("dev_hosts") or []) or "-", v.get("by") or "", v.get("at") or ""] for k, v in rows.items()],
                                 ["AGENT", "LOOPBACK", "DOMAIN MỞ THÊM", "HOST DEV", "BỞI", "LÚC"]) if rows else "(không agent nào có ngoại lệ sandbox)")
        elif a.op == "dev-host":
            r = ctx.call("guard.sandbox.dev_host", {"id": a.agent, "host": a.host, "remove": bool(a.remove)})
            ctx.out(r, (f"agent {a.agent}: đã gỡ host dev {a.host}" if a.remove else
                        f"⚠ agent {a.agent}: tin host dev {a.host} — WebFetch/Chrome tới host này không hỏi dù trỏ về IP nội bộ; Bash được ra mạng tới nó"))
        elif a.op == "allow-domain":
            r = ctx.call("guard.sandbox.allow_domain", {"id": a.agent, "domain": a.domain, "remove": bool(a.remove)})
            ctx.out(r, (f"agent {a.agent}: đã gỡ domain {a.domain}" if a.remove else
                        f"⚠ agent {a.agent}: Bash được ra mạng tới {a.domain} (domain hiện mở: {', '.join(r.get('allow_domains') or [])})"))
        else:
            on = a.state == "on"
            r = ctx.call("guard.sandbox.set", {"id": a.agent, "allow_localhost": on})
            ctx.out(r, (f"⚠ agent {a.agent}: CHO PHÉP bind cổng local + kết nối loopback trong sandbox (filesystem/Unix socket vẫn chặn)"
                        if on else f"agent {a.agent}: đã gỡ ngoại lệ loopback"))
        return 0
    if a.action == "trust":
        if a.op == "list":
            rows = ctx.call("guard.trust.list")
            ctx.out(rows, trust_text(rows))
            return 0
        try:
            for_s = parse_duration(a.for_)
        except ValueError as e:
            from ..errors import HubError

            raise HubError(str(e)) from None
        if a.op == "all":
            r = ctx.call("guard.trust.set_all", {"mode": a.mode, "for_s": for_s})
            ctx.out(r, (f"⚠ {r.get('count')} agent: TRUST FULL — hub cho phép mọi thao tác, không sandbox, bypassPermissions"
                        + (f" tới {r.get('until')}" if r.get("until") else "") + " (áp cho run mới; ESTOP vẫn chặn; audit vẫn ghi)"
                        + "\nagent tạo sau lệnh này: `cfharness config set guard.trust_new_agents full`")
                   if a.mode == "full" else f"{r.get('count')} agent: đã tắt trust — guard/sandbox như thường từ run kế tiếp")
            return 0
        r = ctx.call("guard.trust.set", {"id": a.op, "mode": a.mode, "for_s": for_s})
        ctx.out(r, (f"⚠ agent {a.op}: TRUST FULL — hub cho phép mọi thao tác, không sandbox, bypassPermissions"
                    + (f" tới {r.get('until')}" if r.get("until") else "") + " (áp cho run mới; ESTOP vẫn chặn; audit vẫn ghi)")
               if a.mode == "full" else f"agent {a.op}: đã tắt trust — guard/sandbox như thường từ run kế tiếp")
        return 0
    if a.action == "repo-deny":
        if a.op == "list":
            d = ctx.call("guard.repo_deny.list")
            ctx.out(d, _table([[repo, p] for repo, pats in d.items() for p in pats], ["REPO", "PATTERN"]) if d else "(chưa có mẫu chặn theo repo)")
        elif a.op == "add":
            r = ctx.call("guard.repo_deny.add", {"repo": a.repo, "pattern": a.pattern})
            ctx.out(r, f"đã thêm mẫu chặn '{a.pattern}' cho repo {a.repo} (áp cho MỌI agent chạy trong repo)")
        else:
            r = ctx.call("guard.repo_deny.rm", {"repo": a.repo, "pattern": a.pattern})
            ctx.out(r, f"đã gỡ mẫu chặn repo {a.repo}")
        return 0
    if a.action == "always":
        if a.op == "list":
            rows = ctx.call("guard.policy").get("always_allow") or []
            ctx.out(rows, _table([[r["id"], "đã gỡ" if r.get("disabled") else "bật", r.get("agent_id") or "*", r["kind"], r.get("tool") or "",
                                   (r.get("domain") or r.get("preview") or "")[:60], r.get("approval_id") or "", r.get("created_at") or ""] for r in rows],
                                 ["ID", "TRẠNG THÁI", "AGENT", "KIỂU", "TOOL", "THAO TÁC/DOMAIN", "TỪ APPROVAL", "LÚC"]) if rows else "(chưa có luật luôn cho phép)")
        else:
            r = ctx.call("guard.protect.rm", {"id": a.id})
            ctx.out(r, f"đã gỡ luật luôn cho phép {r['id']} (lịch sử giữ trong policy/history/)")
        return 0
    if a.action == "protect":
        if a.op == "list":
            rows = ctx.call("guard.protect.list")
            ctx.out(rows, _table([[r["id"], r["kind"], r["selector"], ",".join(r["ops_protected"]), "mặc định" if r.get("builtin") else r.get("note") or ""] for r in rows], ["ID", "KIND", "SELECTOR", "OPS", "GHI CHÚ"]))
        elif a.op == "add":
            r = ctx.call("guard.protect.add", {"kind": a.kind, "selector": a.selector, "ops": a.ops, "note": a.note or ""})
            ctx.out(r, f"đã bảo vệ {r['id']}: {r['kind']} {r['selector']} ({','.join(r['ops_protected'])})")
        else:
            r = ctx.call("guard.protect.rm", {"id": a.id})
            ctx.out(r, f"đã gỡ {r['id']} ({r['selector']})")
        return 0
    return 1


def cmd_notify(ctx: Any) -> int:
    if getattr(ctx.args, "action", None) == "telegram":  # chat/cli.py
        from ..chat.cli import cmd_telegram

        return cmd_telegram(ctx)
    if getattr(ctx.args, "action", None) == "slack":  # chat/slack/cli.py
        from ..chat.slack.cli import cmd_slack

        return cmd_slack(ctx)
    r = ctx.call("notify.test", {"text": ctx.args.text})
    ctx.out(r, "đã gửi Slack" if r.get("sent") else f"không gửi: {r.get('reason')}")
    return 0 if r.get("sent") else 1


def cmd_run_continue(ctx: Any) -> int:
    from ..cli import EXIT_OK, EXIT_USER, eprint, follow_run

    a = ctx.args
    if a.cmd == "run-continue":
        run = ctx.call("run.continue", {"id": a.ref, "prompt": a.prompt, "budget_usd": a.budget})
    else:
        run = ctx.call("run.resume", {"id": a.ref, "prompt": getattr(a, "prompt", None)})
    if not a.follow:
        ctx.out(run, run["id"])
        return EXIT_OK
    if not ctx.json:
        eprint(f"run {run['id']} (resume session {run['session_id']})")
    follow_run(ctx, run["id"], run.get("seq_before"), run.get("epoch"))
    final = ctx.call("run.get", {"id": run["id"]})
    return EXIT_OK if final.get("outcome") == "completed" else EXIT_USER


def cmd_agent_purge(ctx: Any) -> int:
    import sys

    a = ctx.args
    confirm = a.confirm
    if confirm is None and sys.stdin.isatty():
        confirm = input(f"purge XOÁ VĨNH VIỄN thùng rác của agent '{a.id}' (không hoàn tác). Gõ lại id để xác nhận: ").strip()
    r = ctx.call("agent.purge", {"id": a.id, "confirm": confirm, "channel": "cli", "by": "user"})
    ctx.out(r, f"đã purge agent {a.id}")
    return 0


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    ap = sub.add_parser("approvals", help="yêu cầu phê duyệt thao tác phá huỷ đang chờ", parents=[common])
    ap.add_argument("--all", action="store_true", help="kể cả đã quyết")
    ap.add_argument("--limit", type=int, default=50)
    ap.set_defaults(fn=cmd_approvals)
    st = sub.add_parser("approvals-stats", help="(= approvals stats) thống kê approval: luật/agent/tool/kết quả/thời gian chờ", parents=[common])
    st.add_argument("--days", type=float, default=30, help="số ngày gần nhất (mặc định 30)")
    st.set_defaults(fn=cmd_approvals_stats)
    pf = sub.add_parser("approvals-profile", help="(= approvals profile) hồ sơ phê duyệt đang hiệu lực", parents=[common])
    pf.set_defaults(fn=cmd_approvals_profile)
    for name, hlp in (("approve", "cho phép MỘT lần thao tác đang chờ"), ("deny", "từ chối thao tác đang chờ")):
        x = sub.add_parser(name, help=hlp, parents=[common])
        x.add_argument("id")
        x.add_argument("--note", help="ghi chú (agent đọc được khi bị từ chối)")
        if name == "approve":
            x.add_argument("--always", action="store_true",
                           help="luôn cho phép đúng thao tác này cho agent này (thao tác 'không chắc', hoặc tool MCP chỉ bị server tự đánh dấu "
                                "destructive → luật cho đúng tool đó; KHÔNG áp cho thao tác phá huỷ khác)")
        x.set_defaults(fn=cmd_decide)

    g = sub.add_parser("guard", help="bộ phân loại thao tác phá huỷ và tài nguyên được bảo vệ", parents=[common])
    gs = g.add_subparsers(dest="action", required=True)
    t = gs.add_parser("test", help='thử phân loại: cfharness guard test "<lệnh>"', parents=[common])
    t.add_argument("command", nargs="?", default="")
    t.add_argument("--tool", default="Bash", help="tên tool (Write, Edit, mcp__srv__tool…)")
    t.add_argument("--input", help="JSON input cho tool không phải Bash")
    t.add_argument("--cwd", help="thư mục làm việc giả định")
    t.add_argument("--run", help="dùng ngữ cảnh của run này (mốc file run tự tạo)")
    gs.add_parser("policy", help="xem chính sách an toàn hiện hành", parents=[common])
    dd = gs.add_parser("decisions", help="nhật ký quyết định guard", parents=[common])
    dd.add_argument("--run")
    dd.add_argument("--limit", type=int, default=50)
    sbx = gs.add_parser("sandbox", help="ngoại lệ sandbox theo agent (chỉ người): localhost/loopback", parents=[common])
    sbs = sbx.add_subparsers(dest="op", required=True)
    sbs.add_parser("list", parents=[common])
    sal = sbs.add_parser("allow-localhost", help="cho agent bind cổng local + gọi http://127.0.0.1 (npm run dev, Playwright/CDP)", parents=[common])
    sal.add_argument("agent")
    sal.add_argument("state", choices=["on", "off"])
    sad = sbs.add_parser("allow-domain", help="mở domain mạng cho Bash của agent (chỉ người, R3-1); --remove để gỡ", parents=[common])
    sad.add_argument("agent")
    sad.add_argument("domain", help="vd github.com, *.cchchn.com, api.example.com:8443")
    sad.add_argument("--remove", action="store_true")
    sdh = sbs.add_parser("dev-host", help="tin host dev trỏ về IP nội bộ cho agent (WebFetch/Chrome không hỏi SSRF; Bash ra mạng tới host)",
                         parents=[common])
    sdh.add_argument("agent")
    sdh.add_argument("host", help="đúng một tên host, vd gamma-dev.creativeforce.io")
    sdh.add_argument("--remove", action="store_true")
    tr = gs.add_parser("trust", help="trust full theo agent (chỉ người): guard cho phép mọi thao tác, không sandbox, bypassPermissions",
                       parents=[common])
    tr.add_argument("op", metavar="agent|all|list", help="id agent, `all` = mọi agent đang có, hoặc `list` để xem")
    tr.add_argument("mode", nargs="?", choices=["full", "off"], default="full")
    tr.add_argument("--for", dest="for_", help="tự tắt sau (vd 30m, 4h, 2d)")
    rd = gs.add_parser("repo-deny", help="mẫu chặn theo repo (chỉ người): áp cho MỌI agent chạy trong repo (MGR-2)", parents=[common])
    rds = rd.add_subparsers(dest="op", required=True)
    rds.add_parser("list", parents=[common])
    rda = rds.add_parser("add", help='vd: guard repo-deny add /path/repo "Bash(npm run deploy:*)"', parents=[common])
    rda.add_argument("repo")
    rda.add_argument("pattern")
    rdr = rds.add_parser("rm", parents=[common])
    rdr.add_argument("repo")
    rdr.add_argument("pattern", nargs="?")
    al = gs.add_parser("always", help="luật luôn cho phép tạo khi duyệt --always (chỉ người gỡ)", parents=[common])
    als = al.add_subparsers(dest="op", required=True)
    als.add_parser("list", parents=[common])
    alr = als.add_parser("rm", parents=[common])
    alr.add_argument("id")
    pr = gs.add_parser("protect", help="tài nguyên được bảo vệ (chỉ người sửa)", parents=[common])
    ps = pr.add_subparsers(dest="op", required=True)
    ps.add_parser("list", parents=[common])
    pa = ps.add_parser("add", parents=[common])
    pa.add_argument("--kind", required=True, choices=["path", "repo", "db", "mcp_tool", "hub"])
    pa.add_argument("--selector", required=True, help="glob đường dẫn | nhánh (main|release/*) | dsn:bảng* | server/tool* | ledger|policy…")
    pa.add_argument("--ops", default="delete,overwrite,write", help="delete,overwrite,write")
    pa.add_argument("--note")
    prm = ps.add_parser("rm", parents=[common])
    prm.add_argument("id")
    g.set_defaults(fn=cmd_guard)

    n = sub.add_parser("notify", help="thông báo Slack / Telegram", parents=[common])
    ns = n.add_subparsers(dest="action", required=True)
    nt = ns.add_parser("test", parents=[common])
    nt.add_argument("--text")
    from ..chat.cli import add_notify_parsers as _add_telegram

    _add_telegram(ns, common)  # notify telegram setup|status|test|disable|allow|revoke
    from ..chat.slack.cli import add_parser as _add_slack

    _add_slack(ns, common)  # notify slack setup|status|test|disable|allow|revoke (Slack 2 chiều)
    n.set_defaults(fn=cmd_notify)

    rc = sub.add_parser("run-continue", help='(= run continue) tiếp tục hội thoại: cfharness run continue <run|session> "<tin>"', parents=[common])
    rc.add_argument("ref")
    rc.add_argument("prompt")
    rc.add_argument("--follow", "-f", action="store_true")
    rc.add_argument("--budget", type=float)
    rc.set_defaults(fn=cmd_run_continue)
    rr = sub.add_parser("run-resume", help="(= run resume) tiếp tục run bị gián đoạn", parents=[common])
    rr.add_argument("ref")
    rr.add_argument("prompt", nargs="?")
    rr.add_argument("--follow", "-f", action="store_true")
    rr.set_defaults(fn=cmd_run_continue)
    pg = sub.add_parser("agent-purge", help="(= agent purge) xoá vĩnh viễn agent đã xoá mềm — phá huỷ", parents=[common])
    pg.add_argument("id")
    pg.add_argument("--confirm", help="gõ lại id để xác nhận")
    pg.set_defaults(fn=cmd_agent_purge)
