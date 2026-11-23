"""CLI `cfharness worktree list|clean [--dry-run] [--yes]` — worktree của task (housekeeping.py, RPC `worktree.list|clean`).
`worktree` ∈ HUB_HUMAN_ONLY_CLI (agent gõ, kể cả chỉ xem, đều bị chặn).

`clean`: daemon tính danh sách dọn được (thử, `dry_run`) → in → y/N (bỏ qua bằng `--yes`) → dọn ĐÚNG các worktree đã in (daemon kiểm
lại từng cái lúc dọn). `--dry-run` chỉ in kế hoạch.
"""

from __future__ import annotations

import argparse
from typing import Any


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    w = sub.add_parser("worktree", help="worktree git của task do hub tạo: liệt kê / dọn (sạch + đã merge) — chỉ người", parents=[common])
    ws = w.add_subparsers(dest="worktree_cmd", required=True)
    ls = ws.add_parser("list", help="liệt kê worktree của task, trạng thái và có dọn được không", parents=[common])
    ls.set_defaults(fn=cmd_list)
    cl = ws.add_parser("clean", help="gỡ worktree dọn được (task xong/huỷ, sạch, nhánh đã merge) + nhánh hub tạo", parents=[common])
    cl.add_argument("--dry-run", action="store_true", help="chỉ in các bước sẽ làm, không gỡ gì")
    cl.add_argument("--yes", "-y", action="store_true", help="không hỏi xác nhận")
    cl.add_argument("paths", nargs="*", help="chỉ dọn các worktree này (mặc định: mọi worktree dọn được)")
    cl.set_defaults(fn=cmd_clean)


def _row(it: dict[str, Any]) -> str:
    tasks = ", ".join(f"{t['id']}:{t['status']}" for t in it.get("tasks") or [])
    deps = it.get("deps") or {}
    dep = " · node_modules liên kết" if deps.get("linked") else (" · cần npm ci" if deps.get("needs_install") else "")
    head = f"{'✓ dọn được' if it.get('cleanable') else '· giữ'}  {it['path']}"
    info = f"    nhánh {it.get('branch') or '?'} ← base {it.get('base') or '?'}{' (đường B cũ)' if it.get('legacy') else ''}{dep} · task {tasks}"
    return head + "\n" + info + ("" if it.get("cleanable") else f"\n    lý do: {it.get('reason')}")


def list_text(r: dict[str, Any]) -> str:
    items = r.get("items") or []
    if not items:
        return "(không có worktree nào của task)"
    return "\n".join(_row(x) for x in items) + f"\n{len(items)} worktree, {r.get('cleanable', 0)} dọn được — `cfharness worktree clean`"


def plan_text(r: dict[str, Any]) -> str:
    lines = []
    for x in r.get("removed") or []:
        lines.append(f"- {x['path']} (nhánh {x.get('branch') or '?'}, task {', '.join(x.get('tasks') or [])})")
        lines += [f"    {a}" for a in x.get("actions") or []]
    for x in r.get("skipped") or []:
        lines.append(f"  bỏ qua {x['path']}: {x['reason']}")
    return "\n".join(lines) or "(không có worktree nào dọn được)"


def cmd_list(ctx: Any) -> int:
    r = ctx.call("worktree.list", {})
    ctx.out(r, list_text(r))
    return 0


def cmd_clean(ctx: Any) -> int:
    a = ctx.args
    params: dict[str, Any] = {"dry_run": True}
    if a.paths:
        params["paths"] = list(a.paths)
    plan = ctx.call("worktree.clean", params)
    targets = [x["path"] for x in plan.get("removed") or []]
    if a.dry_run or not targets:
        ctx.out(plan, ("Thử (không gỡ gì):\n" if targets else "") + plan_text(plan))
        return 0
    if not ctx.json:
        print("Sẽ gỡ:\n" + plan_text(plan))
    if not a.yes:
        try:
            ok = input(f"Gỡ {len(targets)} worktree? [y/N] ").strip().lower() in ("y", "yes", "c", "co", "có")
        except EOFError:
            ok = False
        if not ok:
            print("huỷ — không gỡ gì")
            return 1
    r = ctx.call("worktree.clean", {"paths": targets, "dry_run": False})
    done = r.get("removed") or []
    ctx.out(r, "\n".join([f"đã gỡ {x['path']}" + (f" + nhánh {x['branch']}" if x.get("branch_deleted") else "") for x in done]
                         + [f"bỏ qua {x['path']}: {x['reason']}" for x in r.get("skipped") or []]) or "(không gỡ gì)")
    return 0 if not r.get("skipped") else 1
