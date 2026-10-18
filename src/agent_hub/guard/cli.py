"""Lệnh CLI của luồng B: `approvals`, `approve <id>`, `deny <id>`, `guard test|policy|protect|decisions`,
`notify test`, `run continue <run|session> "<tin>"`, `run resume <run>`, `agent purge <id> --confirm <id>`.

`run continue` / `agent purge` đụng parser cũ (`run <agent> <prompt>`, nhóm `agent`) nên được viết lại thành
lệnh ẩn `run-continue` / `run-resume` / `agent-purge` trước khi parse (`rewrite_argv`).
"""

from __future__ import annotations

import argparse
import json
from typing import Any

HIDDEN = {("run", "continue"): "run-continue", ("run", "resume"): "run-resume", ("agent", "purge"): "agent-purge"}


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

    rows = ctx.call("approval.list", {"status": "all" if ctx.args.all else "pending", "limit": ctx.args.limit})
    if not rows:
        ctx.out(rows, "(không có yêu cầu phê duyệt nào đang chờ)" if not ctx.args.all else "(chưa có approval)")
        return 0
    ctx.out(
        rows,
        _table(
            [[r["id"], r["status"], r["agent_id"], r["run_id"], _ago(r), r.get("matched_rule"), (r.get("preview") or "")[:60]] for r in rows],
            ["ID", "STATUS", "AGENT", "RUN", "CHỜ", "LUẬT", "THAO TÁC"],
        )
        + "\n\nduyệt: agent-hub approve <id>   từ chối: agent-hub deny <id> --note '…'",
    )
    return 0


def cmd_decide(ctx: Any) -> int:
    a = ctx.args
    method = "approval.approve" if a.cmd == "approve" else "approval.deny"
    r = ctx.call(method, {"id": a.id, "note": a.note, "channel": "cli", "by": "user"})
    ctx.out(r, f"{r['id']}: {r['status']} (chờ {r.get('wait_s')}s) — {r.get('preview')}")
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
            ctx.out(rows, _table([[k, "localhost/loopback" if v.get("allow_localhost") else "-", v.get("by") or "", v.get("at") or ""] for k, v in rows.items()],
                                 ["AGENT", "NGOẠI LỆ", "BỞI", "LÚC"]) if rows else "(không agent nào có ngoại lệ sandbox)")
        else:
            on = a.state == "on"
            r = ctx.call("guard.sandbox.set", {"id": a.agent, "allow_localhost": on})
            ctx.out(r, (f"⚠ agent {a.agent}: CHO PHÉP bind cổng local + kết nối loopback trong sandbox (filesystem/Unix socket vẫn chặn)"
                        if on else f"agent {a.agent}: đã gỡ ngoại lệ loopback"))
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
    for name, hlp in (("approve", "cho phép MỘT lần thao tác đang chờ"), ("deny", "từ chối thao tác đang chờ")):
        x = sub.add_parser(name, help=hlp, parents=[common])
        x.add_argument("id")
        x.add_argument("--note", help="ghi chú (agent đọc được khi bị từ chối)")
        x.set_defaults(fn=cmd_decide)

    g = sub.add_parser("guard", help="bộ phân loại thao tác phá huỷ và tài nguyên được bảo vệ", parents=[common])
    gs = g.add_subparsers(dest="action", required=True)
    t = gs.add_parser("test", help='thử phân loại: agent-hub guard test "<lệnh>"', parents=[common])
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
    n.set_defaults(fn=cmd_notify)

    rc = sub.add_parser("run-continue", help='(= run continue) tiếp tục hội thoại: agent-hub run continue <run|session> "<tin>"', parents=[common])
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
