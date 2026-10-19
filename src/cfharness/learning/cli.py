"""CLI M4-A: `cfharness learn …` (vòng học) và `cfharness knowledge …` (kho tri thức tầng agent).

`learn digest` thuộc M4-B (được viết lại thành `digest run` trước khi parse — learning_ops/cli.py).
Không có lệnh duyệt/từ chối proposal: proposal tự áp dụng (FR-LN-05); người chỉ `rollback`, `pin`, tắt học (`learn policy`).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml


def _usd(v: Any) -> str:
    return "-" if v is None else f"${float(v):.4f}"


def _short(s: Any, n: int = 70) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _load(s: str) -> Any:
    p = Path(s)
    if p.exists() and p.is_file():
        s = p.read_text(encoding="utf-8")
    return yaml.safe_load(s)


# ============================================================================ learn
def cmd_learn(ctx: Any) -> int:
    a = ctx.args
    act = a.action
    if act == "status":
        r = ctx.call("learn.status", {"agent": a.agent})
        if ctx.json:
            ctx.out(r)
            return 0
        t = r["today"]
        lines = [f"học: enabled={r['policy']['enabled']} auto_apply={r['policy']['auto_apply']}" + (f"   agent {a.agent}: {r['switches']}" if a.agent else ""),
                 f"hôm nay: proposal {t['proposals'] or {}}  áp dụng {t['applied']}  rollback {t['rollbacks']}  review {t['reviews']} ({_usd(t['review_cost_usd'])})",
                 "hàng chờ duyệt: 0 (không có — tự áp dụng, báo chứ không hỏi)"]
        for j in r["running_reviews"]:
            lines.append(f"  review đang chạy {j['id']} ({j['agent_id']}) run {j['run_id']}")
        for j in r["queued_reviews"]:
            lines.append(f"  review chờ {j['id']} ({j['agent_id']}, {j['trigger']}) từ {j['not_before']}")
        for p in r["deferred"]:
            lines.append(f"  hoãn {p['id']} {p['status']} {p['target']}:{p['target_ref']} → {p.get('not_before') or ''} {_short(p.get('block_detail'), 60)}")
        if r.get("counters"):
            lines.append(f"bộ đếm từ lần review trước: {r['counters']}")
        print("\n".join(lines))
        return 0
    if act == "feed":
        rows = ctx.call("learn.feed", {"agent": a.agent, "days": a.days, "limit": a.limit})
        if ctx.json:
            ctx.out(rows)
            return 0
        for e in reversed(rows):
            p = e["payload"]
            what = p.get("summary") or p.get("detail") or p.get("reason") or p.get("kind") or p.get("target_ref") or ""
            extra = f"   ↩ {p['rollback_cmd']}" if p.get("rollback_cmd") else ""
            print(f"{e['ts'][:19]}  {e['type']:<20} {e.get('agent_id') or '-':<12} {_short(what, 80)}{extra}")
        return 0
    if act == "journey":
        r = ctx.call("learn.journey", {"agent": a.agent})
        if ctx.json:
            ctx.out(r)
            return 0
        for it in r["items"]:
            desc = {k: v for k, v in it.items() if k not in ("ts", "what", "id") and v not in (None, "", [])}
            print(f"{(it['ts'] or '')[:19]}  {it['what']:<8} {it['id']:<16} {_short(json.dumps(desc, ensure_ascii=False), 110)}")
        return 0
    if act == "signals":
        rows = ctx.call("learn.signals", {"agent": a.agent, "kind": a.kind, "limit": a.limit})
        if ctx.json:
            ctx.out(rows)
            return 0
        for s in rows:
            ev = s.get("evidence") or {}
            print(f"{s['created_at'][:19]}  {s['id']}  {s['agent_id']:<12} {s['kind']:<15} {s['source']:<9} {s.get('run_id') or '-':<16} "
                  f"{'(đã review ' + s['processed_by_review_id'] + ')' if s.get('processed_by_review_id') else ''} {_short(ev.get('note') or ev.get('text') or ev.get('message') or ev.get('pattern') or '', 50)}")
        return 0
    if act == "signal":
        ctx.out(ctx.call("learn.signal_add", {"agent": a.agent, "kind": a.kind, "run": a.run, "note": a.note}), "đã ghi Signal")
        return 0
    if act == "refine":
        r = ctx.call("learn.refine", {"agent": a.agent, "focus": a.focus, "run": a.run})
        ctx.out(r, f"đã xếp review {r['id']} cho {r['agent_id']} (nguồn run {r.get('source_run_id')}) — chạy khi agent rảnh; theo dõi: cfharness learn feed --agent {r['agent_id']}")
        return 0
    if act == "reviews":
        rows = ctx.call("learn.reviews", {"agent": a.agent, "status": a.status})
        if ctx.json:
            ctx.out(rows)
            return 0
        for j in rows:
            n = len((j.get("result") or {}).get("proposals") or [])
            print(f"{j['created_at'][:19]}  {j['id']}  {j['agent_id']:<12} {j['trigger']:<8} {j['status']:<9} run={j.get('run_id') or '-':<16} proposals={n} {_usd(j.get('cost_usd'))} {_short(j.get('reason'), 50)}")
        return 0
    if act == "review":
        r = ctx.call("learn.review_get", {"id": a.id})
        ctx.out(r, json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return 0
    if act == "review-cancel":
        ctx.out(ctx.call("learn.review_cancel", {"id": a.id}), "đã huỷ")
        return 0
    if act == "proposals":
        rows = ctx.call("learn.proposals", {"agent": a.agent, "status": a.status, "limit": a.limit})
        if ctx.json:
            ctx.out(rows)
            return 0
        for p in rows:
            print(f"{p['created_at'][:19]}  {p['id']}  {p.get('agent_id') or p['scope']:<12} {p.get('risk_class') or '--'} {p['status']:<22} {p['op']:<7} {p['target']}:{p.get('target_ref') or ''}"
                  f"  {('→ ' + p['ledger_entry_id']) if p.get('ledger_entry_id') else _short(p.get('block_detail'), 60)}")
        return 0
    if act == "proposal":
        r = ctx.call("learn.proposal_get", {"id": a.id})
        if ctx.json:
            ctx.out(r)
        else:
            print(json.dumps({k: v for k, v in r.items() if k != "diff"}, ensure_ascii=False, indent=2, default=str))
            print(r.get("diff") or "")
        return 0
    if act == "propose":
        payload = _load(a.payload) if a.payload else {}
        r = ctx.call("learn.propose", {"agent": a.agent, "target": a.target, "op": a.op, "payload": payload, "scope": a.scope, "rationale": a.rationale, "team": a.team})
        ctx.out(r, f"{r['proposal_id']}: {r['status']}" + (f" — {r.get('reason')}" if r.get("reason") else "") + (f" (ledger {r['ledger_entry_id']})" if r.get("ledger_entry_id") else "")
                + (f" canary {r['canary_id']} trên {r.get('canary_agent') or 'mọi agent (shadow)'}" if r.get("canary_id") else ""))
        return 0 if r["status"] != "blocked" else 3
    if act == "retry":
        r = ctx.call("learn.retry", {"id": a.id})
        ctx.out(r, f"{r['proposal_id']}: {r['status']} {r.get('reason') or ''}")
        return 0
    if act == "ledger":
        rows = ctx.call("learn.ledger", {"agent": a.agent, "target": a.target, "limit": a.limit})
        if ctx.json:
            ctx.out(rows)
            return 0
        for e in rows:
            rb = f"  [rolled back by {e['rolled_back_by']}]" if e.get("rolled_back_by") else ""
            print(f"{e['applied_at'][:19]}  {e['id']}  {e.get('agent_id') or e['scope']:<12} {e.get('risk_class') or '--'} {e['actor']:<10} {e['op']:<8} {e['target']}:{e.get('target_ref') or ''}"
                  f"  {_short(e.get('summary'), 60)}{rb}")
        return 0
    if act == "show":
        r = ctx.call("learn.show", {"id": a.id})
        if ctx.json:
            ctx.out(r)
        else:
            print(json.dumps({k: v for k, v in r.items() if k != "diff"}, ensure_ascii=False, indent=2, default=str))
            print(r.get("diff") or "(không có diff văn bản)")
        return 0
    if act == "rollback":
        r = ctx.call("learn.rollback", {"id": a.id, "chain": a.chain, "force": a.force, "reason": a.reason})
        ctx.out(r, f"đã rollback {', '.join(r.get('chain') or [a.id])} → entry mới {r['id']} (rollback được tiếp: cfharness learn rollback {r['id']})")
        return 0
    if act == "policy":
        if a.op == "get":
            r = ctx.call("learn.policy_get", {"key": a.key})
            ctx.out(r, yaml.safe_dump(r, allow_unicode=True, sort_keys=False) if not isinstance(r, (str, int, float, bool)) or r is None else str(r))
        else:
            if a.key is None or a.value is None:
                raise ValueError("cần: learn policy set <khoá> <giá_trị>")
            r = ctx.call("learn.policy_set", {"key": a.key, "value": a.value})
            ctx.out(r, f"đã đặt {a.key} (policy/learning.yaml, có audit)")
        return 0
    if act == "cache":
        r = ctx.call("learn.cache", {"agent": a.agent, "days": a.days})
        if ctx.json:
            ctx.out(r)
            return 0
        for aid, s in sorted(r["agents"].items()):
            hit = "-" if s["cache_hit"] is None else f"{100 * s['cache_hit']:.1f}%"
            warn = "  ⚠ hash system prompt đổi nhiều (mất cache)" if s["warning"] else ""
            print(f"{aid:<14} runs={s['runs']:<4} cache_hit={hit:<7} input={s['input']} cache_read={s['cache_read']} cache_creation={s['cache_creation']} "
                  f"hash_changes_today={s['prompt_hash_changes_today']}{warn}")
        return 0
    if act == "tick":
        ctx.out(ctx.call("learn.tick", {}), "ok")
        return 0
    if act == "canary":
        return _cmd_canary(ctx)
    raise ValueError(f"lệnh learn không hợp lệ: {act}")


def _cmd_canary(ctx: Any) -> int:
    """M5-A: `learn canary list|show|check` — canary/shadow của thay đổi tầng team/global/USER (FR-LN-16)."""
    a = ctx.args
    op = a.canary_action
    if op == "list":
        rows = ctx.call("learn.canaries", {"status": None if a.all else (a.status or "canary"), "agent": a.agent, "limit": a.limit})
        if ctx.json:
            ctx.out(rows)
            return 0
        if not rows:
            print("không có canary nào" + ("" if a.all or a.status else " đang chạy (--all để xem cả đã kết thúc)"))
        for c in rows:
            who = f"canary={c.get('canary_agent_id')}" if c["mode"] == "canary" else f"shadow theo dõi {','.join(c.get('watch_agents') or [])}"
            print(f"{c['started_at'][:19]}  {c['id']}  {c['status']:<11} {c['scope']}:{c.get('owner_id') or 'global'} {c['target']}:{c.get('target_ref') or ''}  {who}"
                  f"  {c.get('progress') or ''}  hạn {str(c.get('deadline') or '')[:16]}  {_short(c.get('reason'), 70)}")
        return 0
    if op == "show":
        r = ctx.call("learn.canary_get", {"id": a.id})
        ctx.out(r, json.dumps(r, ensure_ascii=False, indent=2, default=str))
        return 0
    if op == "check":
        r = ctx.call("learn.canary_check", {"id": a.id})
        ctx.out(r, "\n".join(f"{x.get('canary_id')}: {x.get('action')} {x.get('reason') or ''}" for x in r) or "không có canary đang chạy")
        return 0
    raise ValueError(f"lệnh canary không hợp lệ: {op}")


# ============================================================================ knowledge
def cmd_knowledge(ctx: Any) -> int:
    a = ctx.args
    act = a.action
    if act == "show":
        r = ctx.call("knowledge.show", {"agent": a.agent})
        if ctx.json:
            ctx.out(r)
            return 0
        print(f"{r['agent_id']}: MEMORY.md {r['memory']['chars']}/{r['memory']['limit']} ký tự ({r['memory']['entries']} mục) · USER.md {r['user']['chars']}/{r['user']['limit']} ({r['user']['entries']} mục)")
        print("skill:")
        for s in r["skills"]:
            flags = (" [pin]" if s.get("pinned") else "") + (f" [{s['scope']}]" if s.get("scope") != "agent" else "") + (f" che {','.join(s['shadows'])}" if s.get("shadows") else "")
            print(f"  - {s['name']:<28} {_short(s.get('description'), 60):<60} by={s.get('created_by') or '-'} state={s.get('state')} dùng={s.get('use_count') or 0}{flags}")
        if r["archived"]:
            print("đã archive (xoá mềm — khôi phục: learn rollback <entry>):")
            for s in r["archived"]:
                print(f"  - {s['name']}  {s.get('deleted_at') or ''} → {s.get('archived_path') or ''}")
        return 0
    if act == "skills":
        ctx.out(ctx.call("knowledge.skills", {"agent": a.agent}))
        return 0
    if act == "view":
        r = ctx.call("knowledge.skill_view", {"agent": a.agent, "name": a.name, "file": a.file})
        ctx.out(r, r["content"])
        return 0
    if act == "memory":
        r = ctx.call("knowledge.memory", {"agent": a.agent, "target": "user" if a.user else "memory"})
        ctx.out(r, f"{'USER.md' if a.user else 'MEMORY.md'} {r['chars']}/{r['limit']} ký tự\n" + "\n§\n".join(r["entries"]))
        return 0
    if act in ("pin", "unpin"):
        r = ctx.call("knowledge.pin", {"agent": a.agent if a.scope == "agent" else None, "target": a.target, "ref": a.ref, "on": act == "pin",
                                       "scope": a.scope, "team": a.team or (a.agent if a.scope == "team" else None), "file": a.file})
        ctx.out(r, f"{'đã pin' if act == 'pin' else 'đã bỏ pin'} {a.target} {a.ref} của {a.agent} — vòng học {'không' if act == 'pin' else 'được'} đụng vào")
        return 0
    if act == "pins":
        ctx.out(ctx.call("knowledge.pins", {"agent": a.agent, "scope": "agent" if a.agent else None}))
        return 0
    if act == "tier":  # M5-A
        r = ctx.call("knowledge.tier", {"scope": a.scope, "team": a.team})
        if ctx.json:
            ctx.out(r)
            return 0
        print(f"tầng {r['scope']}:{r['owner']}" + (f" — thành viên: {', '.join(r['members'])}" if r.get("members") is not None else ""))
        for f, m in r["memory"].items():
            print(f"{f} {m['chars']}/{m['limit']} ký tự")
            for e in m["entries"]:
                print(f"  [{e['key']}]{' [pin]' if e['pinned'] else ''} {_short(e['text'], 100)}")
        print("skill:")
        for x in r["skills"]:
            print(f"  - {x['name']:<28} {_short(x.get('description'), 60)} by={x.get('created_by') or '-'}{' [pin]' if x.get('pinned') else ''}")
        for c in r["canaries"]:
            print(f"canary {c['id']} {c['mode']} {c['target']}:{c.get('target_ref')} trên {c.get('canary_agent_id') or ','.join(c.get('watch_agents') or [])} {c.get('progress') or ''}")
        return 0
    if act == "versions":
        r = ctx.call("knowledge.versions", {"agent": a.agent})
        if ctx.json:
            ctx.out(r)
            return 0
        for v in r["versions"]:
            print(f"{v['scope']}:{v['owner']:<12} {v['target']:<12} {v['ref']:<28} {v['version']}  {v['source']}" + (f" ({v['canary_id']})" if v.get("canary_id") else ""))
        return 0
    if act == "user":
        r = ctx.call("knowledge.user", {"agent": a.agent})
        if ctx.json:
            ctx.out(r)
            return 0
        print(f"hồ sơ dùng chung (global/USER.md, giới hạn {r['shared_limit']} ký tự):")
        for e in r["shared"]:
            print(f"  [{e['key']}]{' [pin]' if e['pinned'] else ''} {e['text']}")
        for ag, obs in r["peers"].items():
            print(f"quan sát riêng của {ag} (agents/{ag}/USER.md — chỉ {ag} đọc):")
            for e in obs:
                print(f"  - {e}")
        return 0
    raise ValueError(f"lệnh knowledge không hợp lệ: {act}")


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    lp = sub.add_parser("learn", help="vòng học tự chủ (FR-LN): signal, review, proposal, ledger, rollback", parents=[common])
    ls = lp.add_subparsers(dest="action", required=True)
    x = ls.add_parser("status", parents=[common], help="tổng quan học hôm nay (không có hàng chờ duyệt)")
    x.add_argument("agent", nargs="?")
    x = ls.add_parser("feed", parents=[common], help="dòng sự kiện học (đã áp dụng/rollback/chặn/hoãn) kèm lệnh rollback")
    x.add_argument("--agent")
    x.add_argument("--days", type=int, default=1)
    x.add_argument("--limit", type=int, default=100)
    ls.add_parser("journey", parents=[common], help="dòng thời gian học của một agent").add_argument("agent")
    x = ls.add_parser("signals", parents=[common], help="Signal đã thu (FR-LN-01)")
    x.add_argument("--agent")
    x.add_argument("--kind")
    x.add_argument("--limit", type=int, default=100)
    x = ls.add_parser("signal", parents=[common], help="ghi Signal thủ công")
    x.add_argument("agent")
    x.add_argument("kind", choices=["correction", "edit_output", "undo", "reject", "cancel", "good", "failure", "repeat_pattern"])
    x.add_argument("--run")
    x.add_argument("--note")
    x = ls.add_parser("refine", parents=[common], help="review thủ công (fork session run gần nhất)")
    x.add_argument("agent")
    x.add_argument("--focus", help="trọng tâm review (tiếng Anh)")
    x.add_argument("--run", help="run nguồn để fork (mặc định run gần nhất)")
    x = ls.add_parser("reviews", parents=[common])
    x.add_argument("--agent")
    x.add_argument("--status")
    ls.add_parser("review", parents=[common]).add_argument("id")
    ls.add_parser("review-cancel", parents=[common]).add_argument("id")
    x = ls.add_parser("proposals", parents=[common], help="proposal và trạng thái (applied/deferred/blocked…)")
    x.add_argument("--agent")
    x.add_argument("--status")
    x.add_argument("--limit", type=int, default=100)
    ls.add_parser("proposal", parents=[common]).add_argument("id")
    x = ls.add_parser("propose", parents=[common], help="người tạo thay đổi tri thức/cấu hình qua CÙNG pipeline (guard + ledger)")
    x.add_argument("agent")
    x.add_argument("--target", required=True, choices=["memory", "skill", "config", "user_profile"])
    x.add_argument("--op", required=True, choices=["add", "patch", "merge", "archive", "delete"])
    x.add_argument("--payload", help="YAML/JSON hoặc đường dẫn file (vd {name: x, old_string: a, new_string: b})")
    x.add_argument("--scope", default="agent", choices=["agent", "team", "global"])
    x.add_argument("--team", help="team của tầng team (mặc định: team duy nhất của agent)")
    x.add_argument("--rationale")
    ls.add_parser("retry", parents=[common], help="thử áp dụng lại proposal hoãn").add_argument("id")
    x = ls.add_parser("ledger", parents=[common], help="ledger thay đổi (append-only)")
    x.add_argument("--agent")
    x.add_argument("--target")
    x.add_argument("--limit", type=int, default=50)
    ls.add_parser("show", parents=[common], help="xem một ledger entry + diff").add_argument("id")
    x = ls.add_parser("rollback", parents=[common], help="rollback một entry (tạo entry mới; rollback được tiếp)")
    x.add_argument("id")
    x.add_argument("--chain", action="store_true", help="rollback luôn các thay đổi sau cùng đích (mới → cũ)")
    x.add_argument("--force", action="store_true", help="khôi phục dù đích bị sửa ngoài ledger")
    x.add_argument("--reason")
    x = ls.add_parser("policy", parents=[common], help="công tắc học / config_bounds / rate (chính sách an toàn — chỉ người)")
    x.add_argument("op", choices=["get", "set"])
    x.add_argument("key", nargs="?", help="vd enabled, auto_apply, agents.<id>.enabled, config_bounds.max_effort, rate.cooldown_per_target_h")
    x.add_argument("value", nargs="?")
    x = ls.add_parser("cache", parents=[common], help="hiệu quả prompt cache (FR-OB-04)")
    x.add_argument("--agent")
    x.add_argument("--days", type=int, default=1)
    ls.add_parser("tick", parents=[common], help="(gỡ lỗi) chạy một vòng nền: review đến hạn, proposal hoãn")
    x = ls.add_parser("canary", parents=[common], help="canary/shadow của thay đổi tầng team/global/USER (FR-LN-16) — không cần duyệt")
    cs = x.add_subparsers(dest="canary_action", required=True)
    y = cs.add_parser("list", parents=[common], help="canary đang chạy (--all: cả promoted/rolled_back)")
    y.add_argument("--status", choices=["canary", "promoted", "rolled_back", "failed"])
    y.add_argument("--all", action="store_true")
    y.add_argument("--agent")
    y.add_argument("--limit", type=int, default=50)
    cs.add_parser("show", parents=[common]).add_argument("id")
    y = cs.add_parser("check", parents=[common], help="(gỡ lỗi) kiểm ngay cửa sổ canary: promote / rollback nếu đến hạn")
    y.add_argument("id", nargs="?")
    lp.set_defaults(fn=cmd_learn)

    kp = sub.add_parser("knowledge", help="kho tri thức tầng agent: memory, skill, pin (FR-LN-11)", parents=[common])
    ks = kp.add_subparsers(dest="action", required=True)
    ks.add_parser("show", parents=[common]).add_argument("agent")
    ks.add_parser("skills", parents=[common]).add_argument("agent")
    x = ks.add_parser("view", parents=[common])
    x.add_argument("agent")
    x.add_argument("name")
    x.add_argument("--file")
    x = ks.add_parser("memory", parents=[common])
    x.add_argument("agent")
    x.add_argument("--user", action="store_true", help="USER.md thay vì MEMORY.md")
    for name in ("pin", "unpin"):
        x = ks.add_parser(name, parents=[common], help="vòng học không đụng vào đích đã pin")
        x.add_argument("agent", help="id agent (scope agent) | id team (scope team) | '-' (scope global)")
        x.add_argument("target", choices=["skill", "memory", "config", "memory_entry"])
        x.add_argument("ref", help="tên skill | MEMORY.md/USER.md | trường cấu hình | memory_entry: đoạn trích duy nhất của dòng")
        x.add_argument("--scope", default="agent", choices=["agent", "team", "global"])
        x.add_argument("--team")
        x.add_argument("--file", help="memory_entry: USER.md | MEMORY.md | memory.md (mặc định USER.md ở global)")
    x = ks.add_parser("pins", parents=[common])
    x.add_argument("agent", nargs="?")
    x = ks.add_parser("tier", parents=[common], help="kho tầng team/global (bản sống) + canary đang chạy (M5)")
    x.add_argument("scope", choices=["team", "global"])
    x.add_argument("team", nargs="?")
    ks.add_parser("versions", parents=[common], help="phiên bản tri thức team/global agent đang được gán (live/pinned/canary)").add_argument("agent")
    ks.add_parser("user", parents=[common], help="hồ sơ người dùng dùng chung + quan sát riêng từng agent (FR-LN-14)").add_argument("agent", nargs="?")
    kp.set_defaults(fn=cmd_knowledge)
