"""Lệnh CLI của M3: `team …`, `task …`, `run revert|diff <run>`, `report`, `cost`, `agent clone|export|import`.

`run revert` / `agent clone` … đụng parser cũ (`run <agent> <prompt>`, nhóm `agent`) nên được viết lại thành lệnh ẩn
(`run-revert`, `agent-clone`, …) trước khi parse (`rewrite_argv`, gọi trong `cli.main`).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from ..config import parse_value

HIDDEN = {
    ("run", "revert"): "run-revert", ("run", "diff"): "run-diff",
    ("agent", "clone"): "agent-clone", ("agent", "export"): "agent-export", ("agent", "import"): "agent-import",
}


def rewrite_argv(argv: list[str]) -> list[str]:
    pos = [i for i, a in enumerate(argv) if not a.startswith("-")]
    if len(pos) >= 2 and pos[1] == pos[0] + 1:
        key = (argv[pos[0]], argv[pos[1]])
        if key in HIDDEN:
            return argv[: pos[0]] + [HIDDEN[key]] + argv[pos[1] + 1 :]
    return argv


def _kv(items: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for it in items or []:
        if "=" not in it:
            raise ValueError(f"cần dạng khoá=giá_trị: {it}")
        k, v = it.split("=", 1)
        out[k.strip()] = parse_value(v)
    return out


def _usd(v: Any) -> str:
    return "-" if v is None else f"${float(v):.4f}"


# ============================================================================ team
def _team_text(t: dict[str, Any]) -> str:
    lines = [f"{t['id']}  {t['name']}" + ("   [ESTOP: " + (t.get("estop_reason") or "bật") + "]" if t.get("estop") else "") + ("   (archived)" if t.get("archived_at") else "")]
    if t.get("charter"):
        lines.append("charter: " + t["charter"].strip().replace("\n", "\n         "))
    lines.append("chính sách: " + json.dumps(t.get("policies") or {}, ensure_ascii=False))
    lines.append("thành viên:")
    for m in t.get("members") or []:
        extra = f"  {m.get('agent_status') or ''}  đang làm {m.get('in_progress', '-')}/{m['max_in_progress']}" if "agent_status" in m else f"  max {m['max_in_progress']}"
        lines.append(f"  - {m['agent_id']:<16} {m['role']:<9}{extra}")
    lines.append("task: " + (", ".join(f"{k}={v}" for k, v in sorted((t.get("counts") or {}).items())) or "(chưa có)"))
    lines.append(f"chi phí hôm nay: {_usd(t.get('cost_today_usd'))}" + (f"   tuần: {_usd(t.get('cost_week_usd'))}" if "cost_week_usd" in t else "")
                 + (f"   ngân sách ngày: {_usd(t.get('budget_usd_per_day'))} (còn {_usd(t.get('budget_remaining_usd'))})" if t.get("budget_usd_per_day") else ""))
    return "\n".join(lines)


def cmd_team(ctx: Any) -> int:
    from ..cli import _table

    a = ctx.args
    act = a.action
    if act == "create":
        members = [{"agent_id": x, "role": "lead"} for x in ([a.lead] if a.lead else [])]
        members += [{"agent_id": x, "role": "worker"} for x in a.worker or []]
        members += [{"agent_id": x, "role": "reviewer"} for x in a.reviewer or []]
        charter = Path(a.charter_file).read_text(encoding="utf-8") if a.charter_file else (a.charter or "")
        t = ctx.call("team.create", {"config": {"id": a.id, "name": a.name or a.id, "charter": charter, "policies": _kv(a.policy), "members": members}})
        ctx.out(t, _team_text(t))
    elif act == "list":
        rows = ctx.call("team.list", {"include_archived": a.archived})
        ctx.out(rows, _table([[t["id"], t["name"], len(t["members"]), next((m["agent_id"] for m in t["members"] if m["role"] == "lead"), "-"),
                               "BẬT" if t["estop"] else "", ", ".join(f"{k}={v}" for k, v in sorted(t["counts"].items())), _usd(t["cost_today_usd"])] for t in rows],
                              ["TEAM", "TÊN", "TV", "LEAD", "ESTOP", "TASK", "HÔM NAY"]) if rows else "(chưa có team)")
    elif act == "show":
        t = ctx.call("team.get", {"id": a.id})
        ctx.out(t, _team_text(t))
    elif act == "edit":
        ch: dict[str, Any] = {}
        if a.name:
            ch["name"] = a.name
        if a.charter_file or a.charter is not None:
            ch["charter"] = Path(a.charter_file).read_text(encoding="utf-8") if a.charter_file else a.charter
        if a.policy:
            ch["policies"] = _kv(a.policy)
        t = ctx.call("team.update", {"id": a.id, "changes": ch})
        ctx.out(t, _team_text(t))
    elif act == "member":
        if a.op == "add":
            t = ctx.call("team.member_set", {"team": a.team, "agent": a.agent, "role": a.role, "max_in_progress": a.max_in_progress})
        else:
            t = ctx.call("team.member_rm", {"team": a.team, "agent": a.agent})
        ctx.out(t, _team_text(t))
    elif act == "archive":
        t = ctx.call("team.archive", {"id": a.id, "cancel_open_tasks": a.cancel_open_tasks})
        ctx.out(t, f"đã archive team {a.id} (xoá mềm, khôi phục: cfharness team restore {a.id})")
    elif act == "restore":
        t = ctx.call("team.restore", {"id": a.id})
        ctx.out(t, f"đã khôi phục team {a.id}")
    elif act == "estop":
        st = ctx.call("team.estop", {"id": a.id, "on": a.state == "on", "reason": a.reason, "stop_running": a.stop_running})
        ctx.out(st, f"ESTOP team {a.id}: {'BẬT' if st['on'] else 'tắt'}" + (" (đã gửi dừng run đang chạy)" if st.get("stop_running") else ""))
    return 0


# ============================================================================ task
def _task_line(t: dict[str, Any]) -> list[Any]:
    return [t["id"], t["status"], t.get("team_id") or "-", t.get("assignee_agent_id") or (t.get("role") or "-"), (t["title"] or "")[:48],
            ",".join(t.get("parents") or []) or "-", t.get("wait_reason") or (t.get("block_reason") or "")[:40] or "", _usd(t.get("cost_usd"))]


def _task_text(t: dict[str, Any]) -> str:
    lines = [f"{t['id']}  [{t['status']}]  {t['title']}",
             f"team: {t.get('team_id') or '-'}   assignee: {t.get('assignee_agent_id') or '-'}   kind: {t.get('kind')}   ưu tiên: {t.get('priority')}   "
             f"lần thử: {t.get('attempts')}   lỗi liên tiếp: {t.get('consecutive_failures')}   chi phí: {_usd(t.get('cost_usd'))}"]
    if t.get("block_reason"):
        lines.append(f"blocked: {t['block_reason']}")
    if t.get("wait_reason"):
        lines.append(f"đang chờ: {t['wait_reason']}")
    if t.get("workspace"):
        lines.append(f"workspace: {json.dumps(t['workspace'], ensure_ascii=False)}")
    if t.get("body"):
        lines.append("\n" + t["body"].strip())
    if t.get("parents"):
        lines.append("\ncha: " + ", ".join(f"{p['id']} [{p['status']}] {p['title']}" for p in t["parents"]))
    if t.get("children"):
        lines.append("con: " + ", ".join(f"{c['id']} [{c['status']}] {c['title']}" for c in t["children"]))
    if t.get("subtasks"):
        lines.append("\ntask con (lead tách):")
        lines += [f"  - {s['id']} [{s['status']}] {s.get('assignee_agent_id') or s.get('role') or '-'}: {s['title']}" for s in t["subtasks"]]
    if t.get("runs"):
        lines.append("\nrun:")
        lines += [f"  - {r['id']} {r['kind']:<6} {r['agent_id']:<14} {r['status']:<12} {_usd(r.get('cost_usd'))}" + (f"  ← {r['parent_run_id']}" if r.get("parent_run_id") else "") for r in t["runs"]]
    if t.get("comments"):
        lines.append("\ncomment:")
        lines += [f"  [{c['created_at']}] {c['author']} ({c['kind']}): {c['text'][:300]}" for c in t["comments"]]
    if t.get("result_summary"):
        lines.append("\nkết quả:\n" + t["result_summary"])
    return "\n".join(lines)


def _watch(ctx: Any, task_id: str, timeout: float | None) -> dict[str, Any]:
    after, epoch = 0, None
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        r = ctx.call("task.events", {"task_id": task_id, "after_seq": after, "epoch": epoch, "wait_s": 20}, timeout=40)
        for e in r["events"]:
            if not ctx.json and e["type"] in ("task.status", "task.created", "task.decomposed", "task.circuit_open", "run.completed", "run.failed", "workspace.diff"):
                p = e.get("payload") or {}
                desc = f"{p.get('from', '')}→{p.get('to', '')} {p.get('reason') or ''}" if e["type"] == "task.status" else json.dumps({k: p[k] for k in list(p)[:4]}, ensure_ascii=False)[:160]
                print(f"#{e['seq']:<5} {e['type']:<18} {p.get('task_id') or e.get('run_id') or ''}  {desc}", flush=True)
        after, epoch = r["latest_seq"], r["epoch"]
        if r["done"]:
            return r["result"]
        if deadline and time.monotonic() > deadline:
            return {"status": r["status"], "timeout": True}


def cmd_task(ctx: Any) -> int:
    from ..cli import _table

    a = ctx.args
    act = a.action
    if act == "create":
        body = Path(a.body_file).read_text(encoding="utf-8") if a.body_file else (a.body or "")
        p: dict[str, Any] = {"title": a.title, "body": body, "team": a.team, "assignee": a.assignee, "parents": a.parent or [], "priority": a.priority,
                             "budget_usd": a.budget, "model": a.model, "max_runtime_s": a.max_runtime, "max_retries": a.max_retries,
                             "idempotency_key": a.key, "created_by": "human"}
        if a.workspace:
            p["workspace"] = {"kind": "dir", "path": str(Path(a.workspace).expanduser().resolve())}
            if getattr(a, "extra_write_root", None):
                p["workspace"]["extra_write_roots"] = [str(Path(x).expanduser().resolve()) for x in a.extra_write_root]
        if getattr(a, "worktree_branch", None):
            p["worktree"] = {k: v for k, v in {"branch": a.worktree_branch, "base": a.worktree_base,
                                               "path": str(Path(a.worktree_path).expanduser().resolve()) if a.worktree_path else None}.items() if v}
        if a.decompose is not None:
            p["decompose"] = a.decompose
        if a.schema_file:
            p["result_schema"] = json.loads(Path(a.schema_file).read_text(encoding="utf-8"))
        t = ctx.call("task.create", {k: v for k, v in p.items() if v is not None})
        for w in t.get("warnings") or []:
            print(f"cảnh báo: {w}")
        if not a.wait:
            ctx.out(t, t["id"])
            return 0
        res = _watch(ctx, t["id"], None)
        ctx.out(res, "\n" + (res.get("result_summary") or res.get("block_reason") or res.get("status") or ""))
        return 0 if res.get("status") == "done" else 1
    if act == "list":
        rows = ctx.call("task.list", {"team": a.team, "status": a.status, "assignee": a.assignee, "origin": a.origin, "include_archived": a.all, "limit": a.limit})
        ctx.out(rows, _table([_task_line(t) for t in rows], ["TASK", "STATUS", "TEAM", "AGENT", "TITLE", "CHA", "CHỜ/LÝ DO", "COST"]) if rows else "(chưa có task)")
    elif act == "show":
        t = ctx.call("task.get", {"id": a.id})
        ctx.out(t, _task_text(t))
    elif act == "watch":
        res = _watch(ctx, a.id, a.timeout)
        ctx.out(res, "\n" + (res.get("result_summary") or res.get("block_reason") or res.get("status") or ""))
    elif act == "comment":
        r = ctx.call("task.comment", {"id": a.id, "text": a.text, "steer": a.steer, "author": "human"})
        ctx.out(r, r["comment_id"] + (f" (steer → {r['steered_run_id']})" if r.get("steered_run_id") else (f" (steer lỗi: {r['steer_error']})" if r.get("steer_error") else "")))
    elif act in ("block", "unblock", "retry", "archive"):
        p = {"id": a.id}
        if act == "block":
            p["reason"] = a.reason
        t = ctx.call(f"task.{act}", p)
        ctx.out(t, f"{t['id']}: {t['status']}")
    elif act == "link":
        t = ctx.call("task.link", {"parent": a.parent, "child": a.child})
        ctx.out(t, f"{a.child} phụ thuộc {a.parent}")
    return 0


# ============================================================================ run revert / diff, report, cost, agent clone/export/import
def cmd_run_revert(ctx: Any) -> int:
    a = ctx.args
    r = ctx.call("run.revert", {"id": a.id, "dry_run": a.dry_run})
    txt = ("(thử) sẽ khôi phục" if a.dry_run else "đã khôi phục") + f" {len(r['reverted'])} file: " + ", ".join(r["reverted"])
    for w in r.get("warnings") or []:
        txt += f"\ncảnh báo: {w}"
    ctx.out(r, txt)
    return 0


def cmd_run_diff(ctx: Any) -> int:
    a = ctx.args
    r = ctx.call("run.diff", {"id": a.id, "patch": a.patch})
    d = r.get("diff") or {}
    if ctx.json:
        ctx.out(r)
        return 0
    if not d:
        print("(run không có diff — không ghi vào workspace dir)")
        return 0
    cp = r.get("checkpoint") or {}
    print(f"workspace: {d.get('root')} ({d.get('kind')})  HEAD trước: {(cp.get('head') or '-')[:10]}  nhánh: {cp.get('branch') or '-'}")
    print(f"thay đổi: {d.get('stat')}" + ("   (đã revert " + r["reverted_at"] + ")" if r.get("reverted_at") else ""))
    for f in d.get("files") or []:
        print(f"  {f['status']:<9} {f['path']}" + ("   ⚠ đã bị sửa tiếp" if f["path"] in (r.get("modified_since") or []) else ""))
    if r.get("patch"):
        print("\n" + r["patch"])
    return 0


def cmd_report(ctx: Any) -> int:
    from ..reports import render_text

    r = ctx.call("report.get", {"since": ctx.args.since})
    ctx.out(r, render_text(r))
    return 0


def cmd_cost(ctx: Any) -> int:
    from ..cli import _table

    a = ctx.args
    r = ctx.call("report.cost", {"since": a.since, "by": a.by})
    ctx.out(r, _table([[x[a.by], x["runs"], _usd(x["cost_usd"])] for x in r["rows"]], [a.by.upper(), "RUN", "COST"]) + f"\ntổng: {_usd(r['total_usd'])}" if r["rows"] else "(chưa có chi phí)")
    return 0


def cmd_agent_x(ctx: Any) -> int:
    a = ctx.args
    if a.cmd == "agent-clone":
        r = ctx.call("agent.clone", {"src": a.src, "dst": a.dst, "with_memory": a.with_memory, "name": a.name})
        ctx.out(r, f"đã clone {a.src} → {a.dst} ({len(r.get('skills') or [])} skill)")
    elif a.cmd == "agent-export":
        r = ctx.call("agent.export", {"id": a.id, "path": str(Path(a.path).expanduser().resolve()), "with_memory": a.with_memory})
        ctx.out(r, f"đã export {a.id} → {r['path']} ({len(r['files'])} file)")
    else:
        r = ctx.call("agent.import", {"path": str(Path(a.path).expanduser().resolve()), "id": a.id, "name": a.name})
        ctx.out(r, f"đã import agent {r['id']}")
    return 0


# ============================================================================ parser
def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    tm = sub.add_parser("team", help="quản lý team (FR-TM)", parents=[common])
    ts = tm.add_subparsers(dest="action", required=True)
    c = ts.add_parser("create", parents=[common], help="tạo team")
    c.add_argument("id")
    c.add_argument("--name")
    c.add_argument("--charter")
    c.add_argument("--charter-file")
    c.add_argument("--lead")
    c.add_argument("--worker", action="append")
    c.add_argument("--reviewer", action="append")
    c.add_argument("--policy", action="append", metavar="KHOÁ=GIÁ_TRỊ", help="vd budget_usd_per_day=2, review_required=true")
    x = ts.add_parser("list", parents=[common])
    x.add_argument("--archived", action="store_true")
    for name in ("show", "restore"):
        ts.add_parser(name, parents=[common]).add_argument("id")
    e = ts.add_parser("edit", parents=[common])
    e.add_argument("id")
    e.add_argument("--name")
    e.add_argument("--charter")
    e.add_argument("--charter-file")
    e.add_argument("--policy", action="append", metavar="KHOÁ=GIÁ_TRỊ")
    m = ts.add_parser("member", parents=[common], help="thêm/gỡ thành viên")
    m.add_argument("op", choices=["add", "rm"])
    m.add_argument("team")
    m.add_argument("agent")
    m.add_argument("--role", default="worker", choices=["lead", "worker", "reviewer", "verifier"])
    m.add_argument("--max-in-progress", type=int)
    ar = ts.add_parser("archive", parents=[common], help="xoá mềm team")
    ar.add_argument("id")
    ar.add_argument("--cancel-open-tasks", action="store_true")
    es = ts.add_parser("estop", parents=[common], help="ESTOP theo team")
    es.add_argument("id")
    es.add_argument("state", choices=["on", "off"])
    es.add_argument("--reason")
    es.add_argument("--stop-running", action="store_true")
    tm.set_defaults(fn=cmd_team)

    tk = sub.add_parser("task", help="task & DAG (FR-TK)", parents=[common])
    tks = tk.add_subparsers(dest="action", required=True)
    c = tks.add_parser("create", parents=[common], help='tạo task: cfharness task create "<tiêu đề>" --team X | --assignee A')
    c.add_argument("title")
    c.add_argument("--body")
    c.add_argument("--body-file")
    c.add_argument("--team")
    c.add_argument("--assignee")
    c.add_argument("--parent", action="append", help="id task cha (lặp lại)")
    c.add_argument("--priority", type=int, default=0)
    c.add_argument("--workspace", help="repo/thư mục làm việc (ghi thẳng, có khoá + checkpoint + diff)")
    c.add_argument("--extra-write-root", action="append", help="thư mục NGOÀI workspace được ghi không cần duyệt (vd worktree của ticket; "
                                                                 "phải nằm trong workspace.allowed_roots; lặp lại)")
    c.add_argument("--worktree-branch", help="chạy trong git worktree riêng của --workspace (hub tạo ngay) trên nhánh này — "
                                             "task cùng repo chạy song song")
    c.add_argument("--worktree-base", help="nhánh/commit gốc khi tạo nhánh mới (mặc định HEAD của repo)")
    c.add_argument("--worktree-path", help="thư mục worktree (mặc định <cha của repo>/_wt/<repo>-<nhánh>)")
    c.add_argument("--budget", type=float, help="trần USD mỗi run")
    c.add_argument("--model")
    c.add_argument("--max-runtime", type=int, help="giây")
    c.add_argument("--max-retries", type=int, help="ngưỡng circuit breaker (lỗi liên tiếp)")
    c.add_argument("--key", help="idempotency_key")
    c.add_argument("--schema-file", help="JSON Schema output có cấu trúc")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--decompose", dest="decompose", action="store_true", default=None, help="ép lead tách việc")
    g.add_argument("--no-decompose", dest="decompose", action="store_false", help="không tách, định tuyến thẳng cho worker")
    c.add_argument("--wait", action="store_true", help="theo dõi tới khi task xong")
    ls = tks.add_parser("list", parents=[common])
    ls.add_argument("--team")
    ls.add_argument("--status")
    ls.add_argument("--assignee")
    ls.add_argument("--origin")
    ls.add_argument("--all", action="store_true", help="kể cả archived")
    ls.add_argument("--limit", type=int, default=200)
    for name in ("show", "unblock", "retry", "archive"):
        tks.add_parser(name, parents=[common]).add_argument("id")
    w = tks.add_parser("watch", parents=[common], help="theo dõi task (và task con) tới khi xong")
    w.add_argument("id")
    w.add_argument("--timeout", type=float)
    cm = tks.add_parser("comment", parents=[common])
    cm.add_argument("id")
    cm.add_argument("text")
    cm.add_argument("--steer", action="store_true", help="gửi luôn vào run đang chạy")
    b = tks.add_parser("block", parents=[common])
    b.add_argument("id")
    b.add_argument("--reason", required=True)
    lk = tks.add_parser("link", parents=[common], help="child phụ thuộc parent")
    lk.add_argument("parent")
    lk.add_argument("child")
    tk.set_defaults(fn=cmd_task)

    rv = sub.add_parser("run-revert", help=argparse.SUPPRESS, parents=[common])
    rv.add_argument("id")
    rv.add_argument("--dry-run", action="store_true")
    rv.set_defaults(fn=cmd_run_revert)
    rd = sub.add_parser("run-diff", help=argparse.SUPPRESS, parents=[common])
    rd.add_argument("id")
    rd.add_argument("--patch", action="store_true")
    rd.set_defaults(fn=cmd_run_diff)

    rp = sub.add_parser("report", help="báo cáo insights (FR-OB-06)", parents=[common])
    rp.add_argument("--since", help="7d (mặc định), 24h, 2026-10-01")
    rp.set_defaults(fn=cmd_report)
    co = sub.add_parser("cost", help="chi phí theo agent|team|day|kind|model", parents=[common])
    co.add_argument("--by", default="agent", choices=["agent", "team", "day", "kind", "model"])
    co.add_argument("--since", help="1d (mặc định), 7d, 2026-10-01")
    co.set_defaults(fn=cmd_cost)

    cl = sub.add_parser("agent-clone", help=argparse.SUPPRESS, parents=[common])
    cl.add_argument("src")
    cl.add_argument("dst")
    cl.add_argument("--with-memory", action="store_true")
    cl.add_argument("--name")
    cl.set_defaults(fn=cmd_agent_x)
    ex = sub.add_parser("agent-export", help=argparse.SUPPRESS, parents=[common])
    ex.add_argument("id")
    ex.add_argument("path")
    ex.add_argument("--with-memory", action="store_true")
    ex.set_defaults(fn=cmd_agent_x)
    im = sub.add_parser("agent-import", help=argparse.SUPPRESS, parents=[common])
    im.add_argument("path")
    im.add_argument("--id")
    im.add_argument("--name")
    im.set_defaults(fn=cmd_agent_x)
