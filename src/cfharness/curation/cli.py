"""CLI M5-B: `cfharness curation …`, `cfharness report kpi|overhead`, `cfharness telemetry status|flush`."""

from __future__ import annotations

import argparse
from typing import Any

HIDDEN = {("report", "kpi"): "report-kpi", ("report", "overhead"): "report-overhead"}


def rewrite_argv(argv: list[str]) -> list[str]:
    pos = [i for i, a in enumerate(argv) if not a.startswith("-")]
    if len(pos) >= 2 and pos[1] == pos[0] + 1 and (argv[pos[0]], argv[pos[1]]) in HIDDEN:
        return argv[: pos[0]] + [HIDDEN[(argv[pos[0]], argv[pos[1]])]] + argv[pos[1] + 1 :]
    return argv


def _usd(v: Any) -> str:
    return "-" if v is None else f"${float(v):.4f}"


def _rec_text(r: dict[str, Any]) -> str:
    try:
        with open(r["report_path"], encoding="utf-8") as f:
            return f.read().rstrip()
    except (KeyError, OSError):
        return f"{r.get('id')} {r.get('kind')} {r.get('status')} — {r.get('summary') or ''}"


def cmd_curation(ctx: Any) -> int:
    a = ctx.args
    act = a.action
    if act == "run":
        r = ctx.call("curation.run", {"dry_run": a.dry_run, "team": a.team, "llm": True if a.llm else None})
        ctx.out(r, _rec_text(r))
    elif act == "detect":
        r = ctx.call("curation.detect", {"team": a.team})
        lines = [f"team {c['team_id']} · điểm {c['score']} · " + ", ".join(f"{m['agent_id']}/{m['name']}" for m in c["members"]) for c in r]
        ctx.out(r, "\n".join(lines) or "(không có cụm skill gần trùng giữa agent)")
    elif act == "phase2":
        r = ctx.call("curation.phase2", {"agent": a.agent, "dry_run": a.dry_run, "focus": a.focus, "force": a.force})
        ctx.out(r, _rec_text(r) if a.dry_run else f"đã khởi chạy curator pha 2 {r['id']} (run {r.get('run_id')}, snapshot {(r.get('snapshot') or {}).get('snapshot_id')}) — "
                f"xem: cfharness curation show {r['id']}")
    elif act == "list":
        r = ctx.call("curation.list", {"kind": a.kind, "limit": a.limit})
        ctx.out(r, "\n".join(f"{x['id']}  {x['kind']:<11} {x['status']:<12} {x.get('agent_id') or x.get('team_id') or '-':<12} {_usd(x.get('cost_usd'))}  {x.get('summary') or ''}" for x in r)
                or "(chưa có lượt curation)")
    elif act == "show":
        r = ctx.call("curation.show", {"id": a.id})
        ctx.out(r, _rec_text(r))
    elif act == "rollback":
        r = ctx.call("curation.rollback", {"id": a.id})
        rb = r.get("rolled_back") or {}
        ctx.out(r, f"đã rollback {len(rb.get('rollback_entries') or [])} entry của {r['id']}" + (f"; lỗi: {rb['errors']}" if rb.get("errors") else ""))
        return 1 if rb.get("errors") else 0
    elif act == "status":
        r = ctx.call("curation.status", {})
        ctx.out(r, f"đã chi hôm nay {_usd(r['spent_today_usd'])} / còn {_usd(r['budget_left_usd'])} · rảnh {r['idle_min']} phút · "
                   f"pha 2 {'BẬT' if r['config']['phase2'].get('enabled') else 'tắt'} · promote chờ: {len(r['pending_promotions'])}")
    elif act == "tick":
        r = ctx.call("curation.tick", {})
        ctx.out(r, str(r or "(không có việc đến hạn)"))
    return 0


def cmd_report_kpi(ctx: Any) -> int:
    from .kpi import render_kpi

    r = ctx.call("report.kpi", {"since": ctx.args.since, "weeks": ctx.args.weeks, "agent": ctx.args.agent})
    ctx.out(r, render_kpi(r))
    return 0


def cmd_report_overhead(ctx: Any) -> int:
    from .kpi import render_overhead

    r = ctx.call("report.overhead", {"since": ctx.args.since, "agent": ctx.args.agent})
    ctx.out(r, render_overhead(r))
    return 0


def cmd_telemetry(ctx: Any) -> int:
    r = ctx.call(f"telemetry.{ctx.args.action}", {})
    ctx.out(r, " · ".join(f"{k}={v}" for k, v in r.items()))
    return 0


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    cp = sub.add_parser("curation", help="curator liên agent (FR-LN-12) + curator pha 2 LLM (FR-LN-09)", parents=[common])
    cs = cp.add_subparsers(dest="action", required=True)
    x = cs.add_parser("run", parents=[common], help="phát hiện skill gần trùng giữa agent cùng team → proposal promote lên team")
    x.add_argument("--team")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--llm", action="store_true", help="gộp nội dung bằng LLM (claude -p --json-schema)")
    x = cs.add_parser("detect", parents=[common], help="chỉ liệt kê cụm gần trùng (không ghi gì)")
    x.add_argument("--team")
    x = cs.add_parser("phase2", parents=[common], help="curator pha 2: LLM gộp skill trong một agent (prompt Hermes)")
    x.add_argument("agent")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--focus")
    x.add_argument("--force", action="store_true", help="chạy dù curation.phase2.enabled=false")
    x = cs.add_parser("list", parents=[common])
    x.add_argument("--kind", choices=["cross_agent", "phase2"])
    x.add_argument("--limit", type=int, default=30)
    cs.add_parser("show", parents=[common]).add_argument("id")
    cs.add_parser("rollback", parents=[common], help="hoàn tác mọi thay đổi của một lượt curation").add_argument("id")
    cs.add_parser("status", parents=[common])
    cs.add_parser("tick", parents=[common], help="chạy một lượt lịch nền ngay (vẫn kiểm rảnh/ngân sách)")
    cp.set_defaults(fn=cmd_curation)

    k = sub.add_parser("report-kpi", help=argparse.SUPPRESS, parents=[common])
    k.add_argument("--since", help="mốc bắt đầu (mặc định = lúc bật học)")
    k.add_argument("--weeks", type=int)
    k.add_argument("--agent")
    k.set_defaults(fn=cmd_report_kpi)
    o = sub.add_parser("report-overhead", help=argparse.SUPPRESS, parents=[common])
    o.add_argument("--since")
    o.add_argument("--agent")
    o.set_defaults(fn=cmd_report_overhead)

    t = sub.add_parser("telemetry", help="OpenTelemetry (FR-OB-07, tắt mặc định: config set telemetry.otel.enabled true)", parents=[common])
    t.add_argument("action", choices=["status", "flush"])
    t.set_defaults(fn=cmd_telemetry)
