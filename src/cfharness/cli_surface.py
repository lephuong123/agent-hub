"""Bề mặt CLI bổ sung khi hardening (H-08, H-11; FR-UI-08).

`rewrite_argv(argv, parser)` chạy TRƯỚC mọi bộ viết lại khác trong `cli.main`:
- `run list|show|logs|stop|follow …` → `runs …` — KHÔNG BAO GIỜ bị hiểu thành `run <agent> <prompt>` (trước đây
  `run logs <x>` với agent tên `logs` sẽ khởi chạy run thật). Id agent trùng các từ khoá này bị cấm khi tạo (`agents.RESERVED_IDS`).
- `requests` → `approvals`; `approve <id> --deny` → `deny <id>`; `feedback <run> --good|--wrong|--partial` → `feedback <run> good|…`;
- `learn pin|unpin …` → `knowledge pin|unpin …`; `learn enable|disable [agent]` → `learn policy set (agents.<id>.)enabled true|false`
  (chỉ khi parser `learn` của luồng học CHƯA có lệnh con cùng tên);
- `eval golden …` → `golden …`; `eval results` → `eval list`.
Lệnh mới: `stop <run|agent>`, `trash list|restore|purge`, `hook <Event>` (hook quan sát, fail-open).
"""

from __future__ import annotations

import argparse
import json
from typing import Any

RUN_SUBCOMMANDS = ("list", "show", "logs", "stop", "follow")
_GLOBAL_WITH_VALUE = ("--home",)


def _subcommands(parser: argparse.ArgumentParser | None, *path: str) -> set[str]:
    """Tên lệnh con có thật của `parser` theo đường `path` (rỗng nếu không tìm thấy)."""
    if parser is None:
        return set()
    cur: argparse.ArgumentParser = parser
    for name in path:
        nxt = None
        for act in cur._actions:  # noqa: SLF001 - argparse không có API công khai
            if isinstance(act, argparse._SubParsersAction) and name in act.choices:  # noqa: SLF001
                nxt = act.choices[name]
                break
        if nxt is None:
            return set()
        cur = nxt
    out: set[str] = set()
    for act in cur._actions:  # noqa: SLF001
        if isinstance(act, argparse._SubParsersAction):  # noqa: SLF001
            out |= set(act.choices)
    return out


def _positions(argv: list[str]) -> list[int]:
    pos: list[int] = []
    skip = False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a in _GLOBAL_WITH_VALUE:
            skip = True
            continue
        if a.startswith("-"):
            continue
        pos.append(i)
    return pos


def rewrite_argv(argv: list[str], parser: argparse.ArgumentParser | None = None) -> list[str]:
    argv = list(argv)
    pos = _positions(argv)
    if not pos:
        return argv
    i = pos[0]
    cmd = argv[i]
    nxt_i = pos[1] if len(pos) > 1 else None
    nxt = argv[nxt_i] if nxt_i is not None else None
    adjacent = nxt_i == i + 1
    if cmd == "run" and adjacent and nxt in RUN_SUBCOMMANDS:
        return argv[:i] + ["runs"] + argv[i + 1 :]
    if cmd == "feedback" and any(f in argv[i + 1 :] for f in ("--good", "--wrong", "--partial")):
        # BRD FR-UI-08: `feedback <run> --good|--wrong [--note]` → dạng positional `feedback <run> good|wrong`
        rest = argv[i + 1 :]
        verdict = next(f[2:] for f in rest if f in ("--good", "--wrong", "--partial"))
        rest = [a for a in rest if a not in ("--good", "--wrong", "--partial")]
        rpos = [k for k, a in enumerate(rest) if not a.startswith("-") and (k == 0 or not rest[k - 1].startswith("--"))]
        at = rpos[0] + 1 if rpos else len(rest)
        return argv[:i] + ["feedback"] + rest[:at] + [verdict] + rest[at:]
    if cmd == "requests":
        return argv[:i] + ["approvals"] + argv[i + 1 :]
    if cmd == "approve" and "--deny" in argv[i + 1 :]:
        rest = [a for a in argv[i + 1 :] if a != "--deny"]
        return argv[:i] + ["deny"] + rest
    if cmd == "learn" and adjacent and nxt in ("pin", "unpin", "enable", "disable"):
        if nxt in _subcommands(parser, "learn"):
            return argv  # luồng học đã có lệnh thật — không che
        if nxt in ("pin", "unpin"):
            return argv[:i] + ["knowledge", nxt] + argv[nxt_i + 1 :]
        rest = argv[nxt_i + 1 :]
        rpos = [k for k, a in enumerate(rest) if not a.startswith("-")]
        flags = [a for k, a in enumerate(rest) if k not in rpos]
        key = f"agents.{rest[rpos[0]]}.enabled" if rpos else "enabled"
        return argv[:i] + ["learn", "policy", "set", key, "true" if nxt == "enable" else "false"] + flags
    if cmd == "eval" and adjacent and nxt == "golden" and "golden" not in _subcommands(parser, "eval"):
        return argv[:i] + ["golden"] + argv[nxt_i + 1 :]
    if cmd == "eval" and adjacent and nxt == "results" and "results" not in _subcommands(parser, "eval"):
        return argv[:i] + ["eval", "list"] + argv[nxt_i + 1 :]
    return argv


# ============================================================================ stop
def cmd_stop(ctx: Any) -> int:
    """`stop <run|agent>` (FR-UI-08): id run (hoặc tiền tố) → dừng run đó; id agent → dừng mọi run đang chạy/chờ của agent."""
    from .rpc import RpcError

    ref = ctx.args.ref
    agent = None
    try:
        agent = ctx.call("agent.get", {"id": ref})
    except RpcError:
        agent = None
    run = None
    try:
        run = ctx.call("run.get", {"id": ref})
    except RpcError:
        run = None
    if agent and run and run.get("agent_id") != ref:
        raise ValueError(f"'{ref}' vừa là agent vừa khớp run {run['id']} — dùng `cfharness run stop <run>` cho rõ")
    if agent:
        active = [r for r in ctx.call("run.list", {"agent": ref, "limit": 500}) if r["status"] in ("queued", "running")]
        stopped = [ctx.call("run.stop", {"id": r["id"]})["id"] for r in active]
        ctx.out({"agent_id": ref, "stopped": stopped}, f"đã gửi lệnh dừng {len(stopped)} run của agent {ref}" + (f": {', '.join(stopped)}" if stopped else ""))
        return 0
    if run is None:
        raise ValueError(f"không tìm thấy run hoặc agent '{ref}'")
    r = ctx.call("run.stop", {"id": run["id"]})
    ctx.out(r, f"đã gửi lệnh dừng run {r['id']}")
    return 0


# ============================================================================ trash
def cmd_trash(ctx: Any) -> int:
    import sys

    from .cli import _table

    a = ctx.args
    if a.action == "list":
        rows = ctx.call("trash.list")
        ctx.out(rows, _table([[r["kind"], r["id"], r.get("deleted_at") or "-", r.get("purge_after") or "-", r.get("path") or ""] for r in rows],
                             ["LOẠI", "ID", "XOÁ LÚC", "PURGE SAU", "ĐƯỜNG DẪN"]) if rows else "(thùng rác trống)")
        return 0
    if a.action == "restore":
        r = ctx.call("trash.restore", {"id": a.id, "kind": a.kind})
        ctx.out(r, f"đã khôi phục {r['kind']} {r['id']}")
        return 0
    confirm = a.confirm
    if confirm is None and sys.stdin.isatty():
        confirm = input(f"purge XOÁ VĨNH VIỄN '{a.id}' khỏi thùng rác (không hoàn tác). Gõ lại id để xác nhận: ").strip()
    r = ctx.call("trash.purge", {"id": a.id, "kind": a.kind, "confirm": confirm, "channel": "cli", "by": "user"})
    ctx.out(r, f"đã purge {r['kind']} {r['id']}")
    return 0


# ============================================================================ hook
def cmd_hook(ctx: Any) -> int:
    """`cfharness hook <Event>` — cùng thân với `python -m cfharness.observe_hook` (fail-OPEN, luôn exit 0)."""
    from .observe_hook import observe

    a = ctx.args
    argv = [a.event, "--timeout", str(a.timeout)]
    if a.run:
        argv += ["--run", a.run]
    argv += ["--socket", a.socket or str(ctx.paths.socket), "--home", str(ctx.paths.home)]
    try:
        res = observe(argv)
    except BaseException:  # pragma: no cover
        res = {"ok": False}
    if ctx.json:
        print(json.dumps(res, ensure_ascii=False, default=str))
    return 0


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    st = sub.add_parser("stop", help="dừng run (id run) hoặc mọi run đang chạy/chờ của agent (id agent)", parents=[common])
    st.add_argument("ref", help="id run (hoặc tiền tố) | id agent")
    st.set_defaults(fn=cmd_stop)

    tr = sub.add_parser("trash", help="thùng rác hợp nhất: agent, team, MCP server đã xoá mềm (FR-SG-06)", parents=[common])
    ts = tr.add_subparsers(dest="action", required=True)
    ts.add_parser("list", help="liệt kê mục trong thùng rác", parents=[common])
    for name, hlp in (("restore", "khôi phục một mục"), ("purge", "XOÁ VĨNH VIỄN (phá huỷ; hiện chỉ agent)")):
        x = ts.add_parser(name, help=hlp, parents=[common])
        x.add_argument("id")
        x.add_argument("--kind", choices=["agent", "team", "mcp"], help="khi trùng tên giữa các loại")
        if name == "purge":
            x.add_argument("--confirm", help="gõ lại id để xác nhận")
    tr.set_defaults(fn=cmd_trash)

    hk = sub.add_parser("hook", help="hook quan sát của hub (PostToolUse|UserPromptSubmit|Stop), fail-open — CLI tự gọi", parents=[common])
    hk.add_argument("event", choices=["PostToolUse", "UserPromptSubmit", "Stop"])
    hk.add_argument("--run", help="id run (mặc định $CFHARNESS_RUN_ID)")
    hk.add_argument("--socket", help="socket daemon (mặc định của HUB_HOME)")
    hk.add_argument("--timeout", type=float, default=2.0, help="giây chờ daemon (quá giờ → hook.timeout, vẫn exit 0)")
    hk.set_defaults(fn=cmd_hook)
