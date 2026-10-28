"""CLI quản lý skill của NGƯỜI: `cfharness skill ls|show|add|edit|import|rm|restore|pin|unpin|promote`.

Client mỏng của daemon — CHỈ gọi RPC `knowledge.skill_*` (learning/skill_rpc.py, hợp đồng ở `docs/dev-notes/skill-manager.md`)
và `knowledge.pin`. Mọi ghi đi qua pipeline học (guard + ledger + rollback), không ghi file trực tiếp.

Chọn tầng giống RPC: `<agent>` (tầng agent; lệnh đọc = bản hiệu lực của agent), `--scope team [--team X]`, `--scope global`.
Agent không chạy được nhóm lệnh ghi này: `guard/classifier.HUB_HUMAN_ONLY_CLI["skill"]` + RPC ∈ `auth.HUMAN_ONLY`.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ..errors import NOT_FOUND
from ..rpc import RpcError
from .cli import _short

EXIT_BLOCKED = 3  # cùng mã thoát với Blocked (errors.EXIT_BY_CODE) và `learn propose` bị chặn
AUDIT = {"channel": "cli", "actor": "user"}
SKILL_TEMPLATE = """---
name: {name}
description: {description}
---

# {title}

## When to use
- …

## Steps
1. …
"""


# ============================================================================ tiện ích
def _tier_label(scope: str | None, owner: str | None) -> str:
    return "global" if scope == "global" else f"{scope}:{owner}"


def _ref_args(scope: str | None, owner: str | None, name: str) -> str:
    """Đối số CLI chỉ đúng một skill ở một tầng — dùng cho gợi ý lệnh."""
    if scope == "team":
        return f"--scope team --team {owner} {name}"
    if scope == "global":
        return f"--scope global {name}"
    return f"{owner} {name}"


def _positionals(a: Any, second: str = "name") -> tuple[str | None, str]:
    """`<agent> <x>` hoặc (tầng team/global) chỉ `<x>`: argparse điền từ trái → dời sang phải khi thiếu."""
    agent, val = a.agent, getattr(a, second)
    if val is None:
        if agent is None:
            raise ValueError(f"thiếu {second}")
        if not (a.scope in ("team", "global") or a.team):
            raise ValueError(f"cần `<agent> <{second}>`, hoặc `--scope team|global <{second}>`")
        agent, val = None, agent
    return agent, val


def _tier(a: Any, agent: str | None, *, explicit: bool = False) -> dict[str, Any]:
    """Tham số tầng cho RPC. `explicit` → luôn có scope (ghi/kiểm tồn tại); không thì agent không kèm scope = bản hiệu lực."""
    scope = a.scope or ("team" if a.team else None)
    if scope is None and explicit and agent:
        scope = "agent"
    if scope is None and not agent:
        raise ValueError("cần <agent>, hoặc --scope team [--team X], hoặc --scope global")
    p: dict[str, Any] = {"agent": agent}
    if scope:
        p["scope"] = scope
    if a.team:
        p["team"] = a.team
    return {k: v for k, v in p.items() if v is not None}


def _write_tier(scope: str, owner: str | None) -> dict[str, Any]:
    """Tham số ghi vào ĐÚNG tầng skill đang nằm (từ kết quả skill_get)."""
    if scope == "agent":
        return {"agent": owner}
    if scope == "team":
        return {"scope": "team", "team": owner}
    return {"scope": "global"}


def _call(ctx: Any, method: str, params: dict[str, Any], *, write: bool = False) -> Any:
    if write:
        params = {**params, "_audit": AUDIT}
    return ctx.call(method, params)


def _exists(ctx: Any, tier: dict[str, Any], name: str) -> bool:
    try:
        _call(ctx, "knowledge.skill_get", {**tier, "name": name, "ledger_limit": 1})
        return True
    except RpcError as e:
        if e.code == NOT_FOUND:
            return False
        raise


def _read_text(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"không thấy file '{p}'")
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def editor_argv() -> list[str]:
    """$VISUAL / $EDITOR (có thể kèm đối số, vd `code --wait`), mặc định notepad (Windows) / vi."""
    raw = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if os.name == "nt" else "vi")
    if os.name == "nt":  # posix=False giữ dấu `\` của đường dẫn Windows, nhưng giữ cả dấu nháy → gỡ
        return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in shlex.split(raw, posix=False)]
    return shlex.split(raw)


def edit_text(initial: str, *, suffix: str = ".md") -> str:
    """Mở trình soạn thảo trên file tạm, trả nội dung sau khi đóng (CRLF → LF). Trình soạn thảo thoát mã ≠ 0 → không lưu."""
    fd, tmp = tempfile.mkstemp(prefix="cfharness-skill-", suffix=suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(initial)
        argv = editor_argv()
        try:
            rc = subprocess.call([*argv, tmp])
        except FileNotFoundError as e:
            raise ValueError(f"không chạy được trình soạn thảo '{argv[0]}' — đặt biến VISUAL/EDITOR, hoặc dùng --from FILE") from e
        if rc != 0:
            raise ValueError(f"trình soạn thảo thoát mã {rc} — không lưu")
        return Path(tmp).read_text(encoding="utf-8").replace("\r\n", "\n")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def report_write(ctx: Any, r: dict[str, Any], verb: str) -> int:
    """In kết quả ghi (pipeline). `blocked` là KẾT QUẢ (RPC thành công) nhưng thao tác không xảy ra → mã thoát 3."""
    blocked = r.get("status") == "blocked"
    if ctx.json:
        ctx.out(r)
        return EXIT_BLOCKED if blocked else 0
    name, tier = r.get("name") or r.get("target_ref") or "?", _tier_label(r.get("scope"), r.get("owner"))
    st = r.get("status")
    if blocked:
        print(f"BỊ CHẶN — skill '{name}' ({tier}) không được {verb}: [{r.get('rule') or 'guard'}] {r.get('reason') or ''}".rstrip(), file=sys.stderr)
        if r.get("proposal_id"):
            print(f"  proposal {r['proposal_id']} (xem: cfharness learn proposal {r['proposal_id']})", file=sys.stderr)
        return EXIT_BLOCKED
    if st == "unchanged":
        print(f"không đổi — skill '{name}' ({tier}) giữ nguyên{' (' + r['file'] + ')' if r.get('file') and r['file'] != 'SKILL.md' else ''}")
        return 0
    lines: list[str] = []
    if st in ("applied", "canary", "promoted", "restored"):
        lines.append(f"đã {verb} skill '{name}' ({tier}) — {st}" + (f" · {r['file']}" if r.get("file") and r["file"] != "SKILL.md" else ""))
    else:  # deferred_* … : pipeline nhận nhưng chưa áp dụng
        lines.append(f"skill '{name}' ({tier}): {st} — CHƯA áp dụng" + (f": {r['reason']}" if r.get("reason") else ""))
    ids = [f"proposal {r['proposal_id']}"] if r.get("proposal_id") else []
    if r.get("restored_entry_id"):
        ids.append(f"khôi phục entry {r['restored_entry_id']}")
    if r.get("ledger_entry_id"):
        ids.append(f"ledger {r['ledger_entry_id']}")
    if ids:
        lines.append("  " + " · ".join(ids))
    if r.get("canary_id"):
        lines.append(f"  canary {r['canary_id']} (mode {r.get('mode') or '?'}) — thay đổi của người áp dụng ngay cho mọi agent của tầng, canary chỉ theo dõi"
                     " (xem: cfharness learn canary show " + r["canary_id"] + ")")
    if r.get("imported_files"):
        lines.append(f"  đã nhập: {', '.join(r['imported_files'])}")
    for s in r.get("skipped") or []:
        lines.append(f"  bỏ qua {s.get('path')}: {s.get('reason')}")
    src = r.get("source_archive")
    if isinstance(src, dict):
        lines.append(f"  bản tầng agent ({r.get('from_agent')}): archive {src.get('status')}" + (f" ledger {src['ledger_entry_id']}" if src.get("ledger_entry_id") else "")
                     + (f" — {src.get('reason')}" if src.get("status") == "blocked" else ""))
    elif r.get("shadowed_by_agent_copy") and r.get("from_agent"):
        lines.append(f"  lưu ý: bản tầng agent của {r['from_agent']} vẫn che bản mới với chính {r['from_agent']} (archive: --archive-source)")
    if r.get("ledger_entry_id"):
        lines.append(f"  hoàn tác: cfharness learn rollback {r['ledger_entry_id']}")
    print("\n".join(lines))
    return 0


# ============================================================================ lệnh
def _ls(ctx: Any, a: Any) -> int:
    p: dict[str, Any] = {"include_archived": bool(a.archived)}
    if a.agent or a.scope or a.team:
        p.update(_tier(a, a.agent))
    r = _call(ctx, "knowledge.skill_list", p)
    if ctx.json:
        ctx.out(r)
        return 0
    rows = r.get("skills") or []
    live = [s for s in rows if s.get("state") not in ("archived", "deleted")]
    gone = [s for s in rows if s.get("state") in ("archived", "deleted")]
    head = {"effective": f"skill hiệu lực của {r.get('agent')} (agent che team che global)", "tier": f"skill tầng {_tier_label(r.get('scope'), r.get('owner'))}",
            "all": "mọi skill (mọi agent, team, global)"}.get(r.get("mode"), "skill")
    print(f"{head}: {len(live)}")
    if live:
        print(f"  {'TÊN':<28} {'TẦNG':<16} {'MÔ TẢ':<44} {'BỞI':<10} {'TRẠNG THÁI':<10} {'DÙNG':>4}  {'LẦN CUỐI':<10}")
    for s in live:
        flags = (" [pin]" if s.get("pinned") else "") + (f" che {','.join(s['shadows'])}" if s.get("shadows") else "")
        print(f"  {s['name']:<28} {_tier_label(s.get('scope'), s.get('owner')):<16} {_short(s.get('description'), 44):<44} {(s.get('created_by') or '-'):<10} "
              f"{(s.get('state') or '-'):<10} {int(s.get('use_count') or 0):>4}  {(s.get('last_used_at') or '-')[:10]:<10}{flags}")
    if gone:
        print("đã archive (xoá mềm):")
        for s in gone:
            hint = f"cfharness skill restore {_ref_args(s.get('scope'), s.get('owner'), s['name'])}" if s.get("restore_entry_id") else "không còn entry khôi phục được"
            print(f"  {s['name']:<28} {_tier_label(s.get('scope'), s.get('owner')):<16} {(s.get('deleted_at') or '')[:19]:<19}"
                  + (f" xoá hẳn sau {s['purge_after'][:10]}" if s.get("purge_after") else "") + f"  → {hint}")
    elif not a.archived:
        print("(skill đã archive: thêm --archived)")
    return 0


def _show(ctx: Any, a: Any) -> int:
    agent, name = _positionals(a)
    r = _call(ctx, "knowledge.skill_get", {**_tier(a, agent), "name": name, "file": a.file, "ledger_limit": a.ledger})
    if ctx.json:
        ctx.out(r)
        return 0
    pv = r.get("provenance") or {}
    print(f"skill {r['name']} — tầng {_tier_label(r.get('scope'), r.get('owner'))}" + (f" (che {', '.join(r['shadows'])})" if r.get("shadows") else ""))
    print(f"  mô tả: {(r.get('frontmatter') or {}).get('description') or '-'}")
    print(f"  bởi {pv.get('created_by') or '-'} · trạng thái {pv.get('state') or '-'} · dùng {int(pv.get('use_count') or 0)} lần"
          + (f" (cuối {str(pv['last_used_at'])[:19]})" if pv.get("last_used_at") else "") + f" · pin {'có' if pv.get('pinned') else 'không'}")
    print(f"  đường dẫn: {r.get('path')}")
    sf = r.get("support_files") or []
    print("  file hỗ trợ: " + (", ".join(sf) if sf else "(không có)"))
    led = r.get("ledger") or []
    if led:
        print("  ledger (mới → cũ):")
        for e in led:
            rb = f" [đã rollback bởi {e['rolled_back_by']}]" if e.get("rolled_back_by") else ""
            rof = f" (rollback của {e['rollback_of']})" if e.get("rollback_of") else ""
            print(f"    {e.get('id')}  {(e.get('applied_at') or '')[:19]}  {e.get('op') or '-':<8} {e.get('actor') or '-':<10} {_short(e.get('summary'), 60)}{rof}{rb}")
    print("─" * 40)
    print((r["file"]["content"] if r.get("file") else r.get("content") or "").rstrip("\n"))
    return 0


def _add(ctx: Any, a: Any) -> int:
    agent, name = _positionals(a)
    tier = _tier(a, agent, explicit=True)
    rationale = {"rationale": a.rationale} if a.rationale else {}
    src = Path(a.source).expanduser() if a.source else None
    if src is not None and src.is_dir():  # thư mục skill (SKILL.md + references/ templates/ scripts/) → import
        r = _call(ctx, "knowledge.skill_import", {**tier, "path": str(src.resolve()), "name": name, "description": a.description, **rationale}, write=True)
        return report_write(ctx, r, "thêm")
    if _exists(ctx, tier, name):
        raise ValueError(f"skill '{name}' đã có ở tầng {tier.get('scope')}{':' + (tier.get('team') or tier.get('agent') or '') if tier.get('scope') != 'global' else ''}"
                         f" — sửa bằng `cfharness skill edit`")
    params: dict[str, Any] = {**tier, "name": name, **rationale}
    if src is not None:
        params["content"] = _read_text(str(src))
        if a.description:
            params["description"] = a.description
    elif a.body_file:
        if not a.description:
            raise ValueError("--body-file cần --description (một dòng tiếng Anh, ≤ 60 ký tự)")
        params.update(description=a.description, body=_read_text(a.body_file))
    else:
        initial = SKILL_TEMPLATE.format(name=name, description=a.description or "One line in English (max 60 chars)",
                                        title=name.replace("-", " ").capitalize())
        text = edit_text(initial)
        if not text.strip() or text == initial:
            print("không đổi — chưa tạo skill (bỏ qua)")
            return 0
        params["content"] = text
    return report_write(ctx, _call(ctx, "knowledge.skill_save", params, write=True), "thêm")


def _edit(ctx: Any, a: Any) -> int:
    agent, name = _positionals(a)
    tier = _tier(a, agent)
    cur = _call(ctx, "knowledge.skill_get", {**tier, "name": name, "ledger_limit": 1})
    scope, owner = cur["scope"], cur.get("owner")
    if a.file and a.file in (cur.get("support_files") or []):
        old = _call(ctx, "knowledge.skill_get", {**tier, "name": name, "file": a.file, "ledger_limit": 1})["file"]["content"]
    elif a.file:
        old = ""  # file hỗ trợ mới (references/ templates/ scripts/ assets/) — daemon kiểm đường dẫn
    else:
        old = cur["content"]
    old = old.replace("\r\n", "\n")
    if a.source:
        text = _read_text(a.source)
    else:
        if not ctx.json:
            print(f"sửa {a.file or 'SKILL.md'} của skill '{name}' ở tầng {_tier_label(scope, owner)} …", file=sys.stderr)
        text = edit_text(old, suffix=Path(a.file).suffix if a.file else ".md")
    if text == old or (not a.file and not text.strip()):
        if ctx.json:
            ctx.out({"status": "unchanged", "name": name, "scope": scope, "owner": owner, "file": a.file or "SKILL.md"})
        else:
            print(f"không đổi — skill '{name}' ({_tier_label(scope, owner)}) giữ nguyên")
        return 0
    params: dict[str, Any] = {**_write_tier(scope, owner), "name": name, "content": text}
    if a.file:
        params["file"] = a.file
    if a.rationale:
        params["rationale"] = a.rationale
    return report_write(ctx, _call(ctx, "knowledge.skill_save", params, write=True), "sửa")


def _import(ctx: Any, a: Any) -> int:
    agent, path = _positionals(a, "path")
    p = Path(path).expanduser()
    params = {**_tier(a, agent, explicit=True), "path": str(p.resolve() if p.exists() else p), "name": a.name, "description": a.description,
              "overwrite": bool(a.overwrite), "rationale": a.rationale}
    r = _call(ctx, "knowledge.skill_import", {k: v for k, v in params.items() if v is not None}, write=True)
    return report_write(ctx, r, "nhập")


def _rm(ctx: Any, a: Any) -> int:
    agent, name = _positionals(a)
    tier = _tier(a, agent, explicit=True)
    if not a.yes and not ctx.json and sys.stdin.isatty():
        where = "global" if tier["scope"] == "global" else f"{tier['scope']}:{tier.get('team') or agent or '?'}"
        ans = input(f"Archive skill '{name}' ({where})? Xoá mềm — khôi phục được bằng `cfharness skill restore`. [y/N] ").strip().lower()
        if ans not in ("y", "yes", "c", "co", "có"):
            print("đã huỷ")
            return 1
    r = _call(ctx, "knowledge.skill_archive", {**tier, "name": name, **({"rationale": a.rationale} if a.rationale else {})}, write=True)
    code = report_write(ctx, r, "archive")
    if code == 0 and not ctx.json and r.get("status") in ("applied", "canary"):
        print(f"  khôi phục: cfharness skill restore {_ref_args(r.get('scope'), r.get('owner'), name)}")
    return code


def _restore(ctx: Any, a: Any) -> int:
    agent, name = _positionals(a)
    r = _call(ctx, "knowledge.skill_restore", {**_tier(a, agent, explicit=True), "name": name, **({"rationale": a.rationale} if a.rationale else {})}, write=True)
    return report_write(ctx, r, "khôi phục")


def _pin(ctx: Any, a: Any, on: bool) -> int:
    agent, name = _positionals(a)
    cur = _call(ctx, "knowledge.skill_get", {**_tier(a, agent), "name": name, "ledger_limit": 1})  # tìm đúng tầng của skill
    scope, owner = cur["scope"], cur.get("owner")
    r = _call(ctx, "knowledge.pin", {"scope": scope, "agent": owner if scope == "agent" else None, "team": owner if scope == "team" else None,
                                     "target": "skill", "ref": name, "on": on}, write=True)
    ctx.out(r, f"{'đã pin' if on else 'đã bỏ pin'} skill '{name}' ({_tier_label(scope, owner)}) — vòng học/curator {'không' if on else 'được'} đụng vào")
    return 0


def _promote(ctx: Any, a: Any) -> int:
    params = {"agent": a.agent, "name": a.name, "to": a.to, "team": a.team, "archive_source": bool(a.archive_source), "rationale": a.rationale}
    r = _call(ctx, "knowledge.skill_promote", {k: v for k, v in params.items() if v is not None}, write=True)
    return report_write(ctx, r, "đưa lên")


def cmd_skill(ctx: Any) -> int:
    a = ctx.args
    act = a.action
    if act in ("ls", "list"):
        return _ls(ctx, a)
    if act == "show":
        return _show(ctx, a)
    if act == "add":
        return _add(ctx, a)
    if act == "edit":
        return _edit(ctx, a)
    if act == "import":
        return _import(ctx, a)
    if act == "rm":
        return _rm(ctx, a)
    if act == "restore":
        return _restore(ctx, a)
    if act in ("pin", "unpin"):
        return _pin(ctx, a, act == "pin")
    if act == "promote":
        return _promote(ctx, a)
    raise ValueError(f"lệnh skill không hợp lệ: {act}")


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    sp = sub.add_parser("skill", help="quản lý skill (agent/team/global): xem, thêm, sửa, nhập, archive, khôi phục, pin, đưa lên tầng", parents=[common])
    ss = sp.add_subparsers(dest="action", required=True)

    def tier(x: argparse.ArgumentParser) -> None:
        x.add_argument("--scope", choices=["agent", "team", "global"], help="tầng (mặc định: agent khi có <agent>)")
        x.add_argument("--team", help="id team (scope team; bỏ trống = team duy nhất của <agent>)")

    def target(x: argparse.ArgumentParser, second: str = "name", help2: str = "tên skill") -> None:
        x.add_argument("agent", nargs="?", help="id agent (bỏ khi --scope team --team X / --scope global)")
        x.add_argument(second, nargs="?", help=help2)
        tier(x)

    x = ss.add_parser("ls", aliases=["list"], parents=[common], help="liệt kê skill (không đối số = mọi agent + team + global)")
    x.add_argument("agent", nargs="?", help="id agent → bản hiệu lực (agent che team che global)")
    tier(x)
    x.add_argument("--archived", action="store_true", help="kèm skill đã archive (+ gợi ý khôi phục)")

    x = ss.add_parser("show", parents=[common], help="chi tiết skill: nguồn gốc, file hỗ trợ, ledger, nội dung")
    target(x)
    x.add_argument("--file", help="xem file hỗ trợ (vd references/x.md) thay vì SKILL.md")
    x.add_argument("--ledger", type=int, default=10, help="số entry ledger (mặc định 10)")

    x = ss.add_parser("add", parents=[common], help="tạo skill mới: --from FILE|DIR, --description + --body-file, hoặc mở $EDITOR")
    target(x)
    x.add_argument("--from", dest="source", help="file SKILL.md/.md (lưu nội dung) hoặc thư mục skill (nhập cả cây)")
    x.add_argument("--description", help="mô tả một dòng tiếng Anh (≤ 60 ký tự)")
    x.add_argument("--body-file", help="file thân SKILL.md (cần --description)")
    x.add_argument("--rationale")

    x = ss.add_parser("edit", parents=[common], help="sửa SKILL.md (hoặc --file) trong $VISUAL/$EDITOR, hoặc --from FILE")
    target(x)
    x.add_argument("--file", help="file hỗ trợ (references/ templates/ scripts/ assets/) thay vì SKILL.md")
    x.add_argument("--from", dest="source", help="lấy nội dung mới từ FILE (không mở trình soạn thảo)")
    x.add_argument("--rationale")

    x = ss.add_parser("import", parents=[common], help="nhập skill từ thư mục / SKILL.md / file .md")
    target(x, "path", "thư mục chứa SKILL.md, file SKILL.md hoặc file .md")
    x.add_argument("--name", help="đặt tên khác (mặc định: frontmatter name / tên thư mục)")
    x.add_argument("--description")
    x.add_argument("--overwrite", action="store_true", help="đã có → thay CẢ cây (rollback được)")
    x.add_argument("--rationale")

    x = ss.add_parser("rm", parents=[common], help="archive (xoá mềm) skill — khôi phục bằng `skill restore`")
    target(x)
    x.add_argument("--rationale")
    x.add_argument("-y", "--yes", action="store_true", help="không hỏi xác nhận")

    x = ss.add_parser("restore", parents=[common], help="khôi phục skill đã archive (rollback entry archive mới nhất)")
    target(x)
    x.add_argument("--rationale")

    for nm in ("pin", "unpin"):
        x = ss.add_parser(nm, parents=[common], help="pin: vòng học/curator không đụng vào skill" if nm == "pin" else "bỏ pin skill")
        target(x)

    x = ss.add_parser("promote", parents=[common], help="đưa skill tầng agent lên team/global (áp dụng ngay, canary theo dõi)")
    x.add_argument("agent")
    x.add_argument("name")
    x.add_argument("--to", required=True, choices=["team", "global"])
    x.add_argument("--team", help="team đích (mặc định: team duy nhất của agent)")
    x.add_argument("--archive-source", action="store_true", help="archive bản tầng agent sau khi đưa lên")
    x.add_argument("--rationale")
    sp.set_defaults(fn=cmd_skill)
