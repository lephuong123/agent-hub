"""CLI `cfharness admin overview [--json]` — tổng quan mọi hub trên máy (admin_overview.py, docs/dev-notes/admin-overview.md).

Client thuần: RPC `admin.overview` (∈ HUMAN_ONLY) — daemon đọc thư mục trạng thái chung `admin_export.dir`.
"""

from __future__ import annotations

import argparse
from typing import Any

STATE_VI = {"ok": "đang chạy", "stale": "QUÁ HẠN", "stopped": "đã dừng", "malformed": "file hỏng", "owner_mismatch": "SAI CHỦ FILE"}


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    a = sub.add_parser("admin", help="quản trị máy nhiều người dùng (tổng quan mọi hub)", parents=[common])
    asub = a.add_subparsers(dest="admin_cmd", required=True)
    o = asub.add_parser("overview", help="tổng quan mọi hub trên máy: chi phí, run, duyệt/câu hỏi, ESTOP (chỉ người)", parents=[common])
    o.set_defaults(fn=cmd_overview)


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


__all__ = ["add_parsers", "render_text"]
