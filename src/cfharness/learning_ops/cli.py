"""Lệnh CLI của M4-B: `golden …`, `eval …`, `digest …`, `curator …`, `regression …`.

`learn digest` (tên trong BRD FR-LN-13) được viết lại thành `digest run` trước khi parse (`rewrite_argv`).
Golden task chỉ NGƯỜI tạo/sửa (Q-07): agent gọi các lệnh ghi này sẽ bị guard chặn (`policy_change`, forbidden).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from ..config import parse_value


def rewrite_argv(argv: list[str]) -> list[str]:
    pos = [i for i, a in enumerate(argv) if not a.startswith("-")]
    if len(pos) >= 2 and pos[1] == pos[0] + 1 and argv[pos[0]] == "learn" and argv[pos[1]] == "digest":
        return argv[: pos[0]] + ["digest", "run"] + argv[pos[1] + 1 :]
    return argv


def _usd(v: Any) -> str:
    return "-" if v is None else f"${float(v):.4f}"


def _load_obj(s: str) -> Any:
    p = Path(s)
    if p.exists() and p.is_file():
        s = p.read_text(encoding="utf-8")
    return yaml.safe_load(s)


def checker_from_args(a: argparse.Namespace, *, required: bool = True) -> dict[str, Any] | None:
    checks: list[dict[str, Any]] = []
    if getattr(a, "checker", None):
        checks.append(_load_obj(a.checker))
    for pat in getattr(a, "regex", None) or []:
        checks.append({"kind": "regex", "pattern": pat, "flags": a.flags or "", "must_match": True})
    for pat in getattr(a, "not_regex", None) or []:
        checks.append({"kind": "regex", "pattern": pat, "flags": a.flags or "", "must_match": False})
    if getattr(a, "contains", None):
        checks.append({"kind": "contains", "all": list(a.contains), "ignore_case": True})
    if getattr(a, "json_schema", None):
        checks.append({"kind": "json_schema", "schema": _load_obj(a.json_schema)})
    if getattr(a, "cmd", None):
        checks.append({"kind": "command", "cmd": a.cmd, "timeout_s": a.cmd_timeout})
    rubric = None
    if getattr(a, "judge_file", None):
        rubric = Path(a.judge_file).read_text(encoding="utf-8")
    elif getattr(a, "judge", None):
        rubric = a.judge
    if rubric:
        j: dict[str, Any] = {"kind": "llm_judge", "rubric": rubric, "pass_threshold": a.pass_threshold}
        if a.judge_model:
            j["model"] = a.judge_model
        checks.append(j)
    if not checks:
        if required:
            raise ValueError("cần ít nhất một cách chấm: --regex/--not-regex/--contains/--json-schema/--cmd/--judge/--checker")
        return None
    return checks[0] if len(checks) == 1 else {"kind": "all", "checks": checks}


def _add_checker_args(x: argparse.ArgumentParser) -> None:
    g = x.add_argument_group("cách chấm (người chọn — nhiều cờ = tất cả phải đạt)")
    g.add_argument("--regex", action="append", help="output phải khớp regex")
    g.add_argument("--not-regex", action="append", help="output KHÔNG được khớp regex")
    g.add_argument("--flags", default="", help="cờ regex: i m s x")
    g.add_argument("--contains", action="append", help="output phải chứa chuỗi (không phân biệt hoa thường)")
    g.add_argument("--json-schema", help="JSON Schema (file hoặc chuỗi JSON/YAML) — output/structured_output phải hợp lệ")
    g.add_argument("--cmd", help="lệnh test chạy trong workspace của run eval (exit 0 = đạt; stdin = output)")
    g.add_argument("--cmd-timeout", type=float, default=120)
    g.add_argument("--judge", help="rubric cho LLM judge (claude -p --json-schema)")
    g.add_argument("--judge-file", help="file rubric cho LLM judge")
    g.add_argument("--judge-model", help="model của judge (mặc định eval.judge.model)")
    g.add_argument("--pass-threshold", type=float, default=0.7, help="ngưỡng đạt của judge (0..1)")
    g.add_argument("--checker", help="checker đầy đủ (file hoặc chuỗi JSON/YAML)")


def _golden_line(g: dict[str, Any]) -> list[Any]:
    ck = g.get("checker") or {}
    kind = ck.get("kind") if ck.get("kind") != "all" else "all(" + ",".join(c.get("kind", "?") for c in ck.get("checks") or []) + ")"
    return [g["id"], g["agent_id"], kind, "on" if g.get("active") else "off", f"v{g.get('version')}", (g.get("title") or "")[:50], g.get("source_run_id") or "-"]


def _table(rows: list[list[Any]], headers: list[str]) -> str:
    cols = [headers] + [[("" if c is None else str(c)) for c in r] for r in rows]
    w = [max(len(r[i]) for r in cols) for i in range(len(headers))]
    return "\n".join("  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip() for r in cols)


def _golden_text(g: dict[str, Any]) -> str:
    lines = [f"{g['id']}  agent={g['agent_id']}  {'active' if g.get('active') else 'ĐÃ XOÁ MỀM'}  v{g.get('version')}  bởi {g.get('created_by')}  {g.get('created_at')}",
             f"tiêu đề: {g.get('title')}"]
    if g.get("source_run_id"):
        lines.append(f"từ run: {g['source_run_id']}")
    if g.get("context"):
        lines.append("ngữ cảnh:\n  " + g["context"].strip().replace("\n", "\n  "))
    lines.append("prompt:\n  " + g["prompt"].strip().replace("\n", "\n  "))
    if g.get("files"):
        lines.append("file seed: " + ", ".join(g["files"]))
    lines.append("cách chấm: " + json.dumps(g.get("checker"), ensure_ascii=False))
    if g.get("recent_results"):
        lines.append("kết quả gần đây:")
        for r in g["recent_results"][:10]:
            lines.append(f"  {r['created_at']}  {r['eval_id']}  {r['variant']:<9} #{r['sample']}  điểm {r['score']}  {r['outcome']}  {_usd(r.get('cost_usd'))}")
    return "\n".join(lines)


def _verdict_text(v: dict[str, Any]) -> str:
    s = v.get("scores") or {}
    lines = [f"eval {v.get('eval_id')}: {v['status']}  — {v.get('reason')}", f"điểm trước {s.get('before')} → sau {s.get('after')} (tụt {s.get('drop')})   chi phí {_usd(v.get('cost_usd'))}"]
    for k, t in (s.get("per_task") or {}).items():
        lines.append(f"  {k}: {t.get('before')} → {t.get('after')}  (n={t.get('n_before')}/{t.get('n_after')})")
    if v.get("rolled_back"):
        lines.append("đã rollback: " + ", ".join(v["rolled_back"]))
    if v.get("flags"):
        lines.append("cờ: " + ", ".join(v["flags"]))
    return "\n".join(lines)


# ============================================================================ commands
def cmd_golden(ctx: Any) -> int:
    a = ctx.args
    act = a.action
    if act == "add":
        if not a.prompt and not a.prompt_file:
            raise ValueError("cần --prompt hoặc --prompt-file")
        files = {}
        for item in a.file or []:
            rel, _, src = item.partition("=")
            files[rel] = Path(src).read_text(encoding="utf-8")
        p = {"agent_id": a.agent, "prompt": Path(a.prompt_file).read_text(encoding="utf-8") if a.prompt_file else a.prompt,
             "context": a.context, "title": a.title, "checker": checker_from_args(a), "files": files or None}
        if a.output_schema:
            p["output_schema"] = _load_obj(a.output_schema)
        g = ctx.call("golden.add", p)
        ctx.out(g, f"đã tạo golden task {g['id']} cho {g['agent_id']}\n" + _golden_text(g))
    elif act == "from-run":
        g = ctx.call("golden.from_run", {"run_id": a.run, "checker": checker_from_args(a), "title": a.title, "prompt": a.prompt, "context": a.context})
        ctx.out(g, f"đã đánh dấu run {a.run} làm golden task {g['id']}\n" + _golden_text(g))
    elif act == "list":
        r = ctx.call("golden.list", {"agent_id": a.agent, "all": a.all})
        ctx.out(r, _table([_golden_line(g) for g in r["tasks"]], ["ID", "AGENT", "CHẤM", "TT", "VER", "TIÊU ĐỀ", "TỪ RUN"]) if r["tasks"] else "(chưa có golden task)")
    elif act == "show":
        g = ctx.call("golden.show", {"id": a.id})
        ctx.out(g, _golden_text(g))
    elif act == "edit":
        ch: dict[str, Any] = {}
        if a.prompt or a.prompt_file:
            ch["prompt"] = Path(a.prompt_file).read_text(encoding="utf-8") if a.prompt_file else a.prompt
        for k in ("title", "context"):
            if getattr(a, k) is not None:
                ch[k] = getattr(a, k)
        ck = checker_from_args(a, required=False)
        if ck:
            ch["checker"] = ck
        g = ctx.call("golden.update", {"id": a.id, "changes": ch})
        ctx.out(g, f"đã sửa {g['id']} (v{g['version']})")
    elif act == "rm":
        g = ctx.call("golden.rm", {"id": a.id})
        ctx.out(g, f"đã xoá mềm golden task {g['id']} (khôi phục: cfharness golden restore {g['id']})")
    elif act == "restore":
        g = ctx.call("golden.restore", {"id": a.id})
        ctx.out(g, f"đã khôi phục golden task {g['id']}")
    elif act == "candidates":
        r = ctx.call("golden.candidates", {"agent_id": a.agent, "limit": a.limit})
        ctx.out(r, "\n".join(f"{c['run_id']}  ({c['reason']})  {c['prompt_preview']}" for c in r) or "(không có run ứng viên)")
    elif act == "suggestions":
        r = ctx.call("golden.suggestions", {"agent_id": a.agent, "status": "all" if a.all else "open"})
        ctx.out(r, _table([[s["id"], s["agent_id"], s["status"], s.get("task_id") or "-", s["run_id"], (s.get("title") or s.get("reason") or "")[:60]] for s in r],
                          ["ID", "AGENT", "TT", "TASK", "RUN", "TIÊU ĐỀ"]) if r else "(không có đề xuất golden đang chờ)")
    elif act in ("approve", "reject"):
        s = ctx.call(f"golden.suggestion_{act}", {"id": a.id})
        ctx.out(s, f"đã tạo golden task {s['golden_task_id']} từ đề xuất {s['id']} ({s['agent_id']})" if act == "approve"
                else f"đã bỏ đề xuất {s['id']} — không gợi ý lại")
    elif act == "scan":
        r = ctx.call("golden.suggest_scan", {})
        ctx.out(r, (f"đề xuất mới: {', '.join(r['suggested'])}" if r["suggested"] else "không có đề xuất mới")
                + ("\nbỏ qua: " + ", ".join(f"{k}={v}" for k, v in sorted(r["skipped"].items())) if r["skipped"] else ""))
    return 0


def cmd_eval(ctx: Any) -> int:
    a = ctx.args
    act = a.action
    if act == "run":
        r = ctx.call("eval.run", {"agent_id": a.agent, "n": a.n, "tasks": a.task}, timeout=3600)
        lines = [f"eval {r['eval_id']}: điểm {r['score']}   chi phí {_usd(r['cost_usd'])}"]
        lines += [f"  {k}: {t['after']} (n={t['n_after']})" for k, t in (r.get("per_task") or {}).items()]
        ctx.out(r, "\n".join(lines))
    elif act == "check":
        if a.proposal:
            prop = _load_obj(a.proposal)
        else:
            changes = {}
            for item in a.set or []:
                k, _, v = item.partition("=")
                changes[k.strip()] = parse_value(v)
            if a.soul_file:
                changes["soul"] = Path(a.soul_file).read_text(encoding="utf-8")
            if not changes:
                raise ValueError("cần --proposal FILE hoặc --set khoá=giá_trị (vd --set model=haiku)")
            prop = {"id": "manual", "scope": "agent", "agent_id": a.agent, "target": "config", "op": "patch", "candidate": changes, "created_by": "user"}
        v = ctx.call("eval.check", {"proposal": prop}, timeout=3600)
        ctx.out(v, _verdict_text(v) + "\n(chỉ đánh giá — không áp dụng thay đổi nào)")
    elif act == "post-apply":
        v = ctx.call("eval.post_apply", {"entry_id": a.entry, "no_rollback": a.no_rollback}, timeout=3600)
        ctx.out(v, _verdict_text(v))
    elif act == "batch":
        r = ctx.call("eval.batch", {"since": a.since}, timeout=3600)
        ctx.out(r, "\n".join(_verdict_text(v) for v in r) or "(không có entry L1 nào cần chấm)")
    elif act == "list":
        r = ctx.call("eval.list", {"agent_id": a.agent, "limit": a.limit})
        ctx.out(r, _table([[e["id"], e["kind"], e["agent_id"], e["status"], e.get("before_score"), e.get("after_score"), _usd(e.get("cost_usd")), e["created_at"][:19], (e.get("reason") or "")[:50]] for e in r],
                          ["ID", "LOẠI", "AGENT", "KẾT QUẢ", "TRƯỚC", "SAU", "CHI PHÍ", "LÚC", "LÝ DO"]) if r else "(chưa có eval)")
    elif act == "show":
        d = ctx.call("eval.show", {"id": a.id})
        lines = [f"{d['id']}  {d['kind']}  agent={d['agent_id']}  {d['status']}  {d.get('before_score')} → {d.get('after_score')}  {_usd(d.get('cost_usd'))}", f"lý do: {d.get('reason')}"]
        for r in d["results"]:
            lines.append(f"  {r['variant']:<9} {r['golden_task_id']} #{r['sample']}  điểm {r['score']}  {r['outcome']}  run {r['run_id']}  {_usd(r.get('cost_usd'))}  {json.dumps(r['detail'], ensure_ascii=False)[:120]}")
        ctx.out(d, "\n".join(lines))
    return 0


def cmd_digest(ctx: Any) -> int:
    a = ctx.args
    if a.action == "run":
        r = ctx.call("digest.run", {"since": a.since, "agent_id": a.agent, "no_send": a.no_send})
        ctx.out(r, r["text"] + f"\n\n(digest {r['id']} — Slack: {r['slack']})")
    elif a.action == "list":
        r = ctx.call("digest.list", {"limit": a.limit})
        ctx.out(r, _table([[d["id"], d["created_at"][:19], d.get("agent_id") or "-", d["counts"].get("applied"), d["counts"].get("rolled_back"), d.get("slack_status"), d.get("tui_seen_at") or "-"] for d in r],
                          ["ID", "LÚC", "AGENT", "ÁP DỤNG", "ROLLBACK", "SLACK", "TUI ĐÃ XEM"]) if r else "(chưa có digest)")
    elif a.action == "show":
        d = ctx.call("digest.show", {"id": a.id})
        ctx.out(d, d.get("text") or "(không có digest)")
    return 0


def cmd_curator(ctx: Any) -> int:
    a = ctx.args
    if a.action == "run":
        r = ctx.call("curator.run", {"dry_run": a.dry_run, "agent_id": a.agent}, timeout=3600)
        c = r["counts"]
        ctx.out(r, f"curator{' (dry-run)' if r['dry_run'] else ''}: kiểm {c['checked']}, stale {c['marked_stale']}, archive {c['archived']}, "
                   f"kích hoạt lại {c['reactivated']}, lỗi {c['errors']}" + (f"\n{r['note']}" if r.get("note") else "") + f"\nbáo cáo: {r['report_path']}")
    else:
        r = ctx.call("curator.status", {})
        ctx.out(r, json.dumps(r, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_regression(ctx: Any) -> int:
    a = ctx.args
    if a.action == "check":
        r = ctx.call("regression.check", {"agent_id": a.agent}, timeout=3600)
        ctx.out(r, "\n".join(f"{x['entry_id']}: {x['action']}" + (f" — {x['reason']}" if x.get("reason") else "") for x in r) or "(không phát hiện hồi quy)")
    else:
        r = ctx.call("regression.list", {"agent_id": a.agent, "status": a.status})
        ctx.out(r, _table([[w["entry_id"], w["agent_id"], w["status"], f"{w.get('target')}:{w.get('target_ref')}", w["applied_at"][:19],
                            f"{(w.get('observed') or {}).get('runs', 0)} run", (w.get("reason") or "")[:60]] for w in r],
                          ["ENTRY", "AGENT", "TT", "ĐÍCH", "ÁP DỤNG", "CỬA SỔ", "LÝ DO"]) if r else "(chưa theo dõi thay đổi nào)")
    return 0


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    gp = sub.add_parser("golden", help="golden task do người quản lý (FR-LN-10, Q-07)", parents=[common])
    gs = gp.add_subparsers(dest="action", required=True)
    x = gs.add_parser("add", parents=[common], help="viết tay một golden task")
    x.add_argument("agent")
    x.add_argument("--prompt")
    x.add_argument("--prompt-file")
    x.add_argument("--context", help="ngữ cảnh chèn trước prompt")
    x.add_argument("--file", action="append", metavar="ĐƯỜNG_DẪN_TƯƠNG_ĐỐI=FILE_NGUỒN", help="seed file vào workspace của run eval")
    x.add_argument("--title")
    x.add_argument("--output-schema", help="JSON Schema cho --json-schema của run eval (structured output)")
    _add_checker_args(x)
    x = gs.add_parser("from-run", parents=[common], help="đánh dấu một run thật làm golden task")
    x.add_argument("run")
    x.add_argument("--title")
    x.add_argument("--prompt", help="sửa prompt (mặc định = prompt gốc của run)")
    x.add_argument("--context")
    _add_checker_args(x)
    x = gs.add_parser("list", parents=[common])
    x.add_argument("agent", nargs="?")
    x.add_argument("--all", action="store_true", help="kể cả đã xoá mềm")
    for name in ("show", "rm", "restore"):
        gs.add_parser(name, parents=[common]).add_argument("id")
    x = gs.add_parser("edit", parents=[common])
    x.add_argument("id")
    x.add_argument("--prompt")
    x.add_argument("--prompt-file")
    x.add_argument("--title")
    x.add_argument("--context")
    _add_checker_args(x)
    x = gs.add_parser("candidates", parents=[common], help="run ứng viên nên làm golden")
    x.add_argument("agent")
    x.add_argument("--limit", type=int, default=5)
    x = gs.add_parser("suggestions", parents=[common], help="đề xuất golden từ task done (hub tự đề xuất, bạn duyệt)")
    x.add_argument("agent", nargs="?")
    x.add_argument("--all", action="store_true", help="kể cả đã duyệt/bỏ")
    gs.add_parser("approve", parents=[common], help="duyệt đề xuất → tạo golden task").add_argument("id")
    gs.add_parser("reject", parents=[common], help="bỏ đề xuất (không gợi ý lại)").add_argument("id")
    gs.add_parser("scan", parents=[common], help="quét task done ngay (vòng nền tự quét mỗi giờ)")
    gp.set_defaults(fn=cmd_golden)

    ep = sub.add_parser("eval", help="eval gate (FR-LN-10)", parents=[common])
    es = ep.add_subparsers(dest="action", required=True)
    x = es.add_parser("run", parents=[common], help="chấm cấu hình hiện tại của agent trên golden task")
    x.add_argument("agent")
    x.add_argument("-n", type=int, help="số lần mỗi task (mặc định eval.samples)")
    x.add_argument("--task", action="append")
    x = es.add_parser("check", parents=[common], help="thử gate trước áp dụng cho một thay đổi (không áp dụng)")
    x.add_argument("agent")
    x.add_argument("--set", action="append", metavar="KHOÁ=GIÁ_TRỊ", help="vd model=haiku, effort=low")
    x.add_argument("--soul-file")
    x.add_argument("--proposal", help="proposal đầy đủ (file/chuỗi JSON/YAML)")
    x = es.add_parser("post-apply", parents=[common], help="chấm sau áp dụng một ledger entry (tụt → rollback)")
    x.add_argument("entry")
    x.add_argument("--no-rollback", action="store_true")
    x = es.add_parser("batch", parents=[common], help="eval theo lô cho entry L1 chưa chấm")
    x.add_argument("--since")
    x = es.add_parser("list", parents=[common])
    x.add_argument("agent", nargs="?")
    x.add_argument("--limit", type=int, default=30)
    es.add_parser("show", parents=[common]).add_argument("id")
    ep.set_defaults(fn=cmd_eval)

    dp = sub.add_parser("digest", help="digest học (FR-LN-13) — cũng gọi được bằng `learn digest`", parents=[common])
    ds = dp.add_subparsers(dest="action", required=True)
    x = ds.add_parser("run", parents=[common], help="dựng digest ngay (và gửi Slack)")
    x.add_argument("--since")
    x.add_argument("--agent")
    x.add_argument("--no-send", action="store_true")
    x = ds.add_parser("list", parents=[common])
    x.add_argument("--limit", type=int, default=20)
    x = ds.add_parser("show", parents=[common])
    x.add_argument("id", nargs="?")
    dp.set_defaults(fn=cmd_digest)

    cp = sub.add_parser("curator", help="curator pha 1 (FR-LN-08)", parents=[common])
    cs = cp.add_subparsers(dest="action", required=True)
    x = cs.add_parser("run", parents=[common])
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--agent")
    cs.add_parser("status", parents=[common])
    cp.set_defaults(fn=cmd_curator)

    rp = sub.add_parser("regression", help="theo dõi hồi quy sau thay đổi học (FR-LN-17)", parents=[common])
    rs = rp.add_subparsers(dest="action", required=True)
    x = rs.add_parser("list", parents=[common])
    x.add_argument("agent", nargs="?")
    x.add_argument("--status", choices=["watching", "passed", "rolled_back", "gone"])
    x = rs.add_parser("check", parents=[common])
    x.add_argument("agent", nargs="?")
    rp.set_defaults(fn=cmd_regression)
