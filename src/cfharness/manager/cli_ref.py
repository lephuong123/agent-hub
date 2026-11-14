"""Khối "CLI reference" cho system prompt của điều phối viên (tiếng Anh — quy ước AGENTS.md).

Vì sao: điều phối viên không chạy được CLI `cfharness` và trước đây không biết lệnh nào có thật → bịa lệnh (vd `cfharness run list`
thay vì `cfharness runs list`). Khối này sinh TỰ ĐỘNG từ parser argparse thật (`cli.build_parser`) nên không lệch với CLI; chỉ tên
lệnh + lệnh con + tham số vị trí + cờ (không mô tả tiếng Việt) → ngắn, ổn định giữa các run (giữ prompt cache, hash chỉ đổi khi CLI đổi).
"""

from __future__ import annotations

import argparse
import functools

HEAD = """# cfharness CLI reference
The owner can run these `cfharness` commands in a terminal. You cannot run them yourself.
- When the owner asks for hub data your hub tools can read (agents, runs, tasks, approvals, costs, schedules), fetch it with
  those tools and answer with the data. Do not hand over a command instead.
- Give a command only when the owner must run it (something your tools cannot do or are not allowed to do), or when the owner
  asks for one. Use ONLY commands and flags listed below, exactly as written; never guess a command or flag. If what is needed
  is not listed, say so and point to `cfharness <command> --help`.
- Shared flags on every command: --json, --home HOME. Placeholders in <angle brackets> are positional arguments; X is a
  flag value; … means more flags exist (see `cfharness <command> --help`).
"""

_SKIP_FLAGS = {"-h", "--help", "--json", "--home"}
_SKIP_CMDS = {"hook"}  # lệnh nội bộ (claude CLI gọi), người không gõ


def _subparsers(p: argparse.ArgumentParser) -> argparse._SubParsersAction | None:  # type: ignore[name-defined]
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a
    return None


MAX_FLAGS = 4  # cờ mỗi lệnh; nhiều hơn → "…" (người/agent xem `--help`) — giữ khối ngắn


def _args(p: argparse.ArgumentParser) -> str:
    out: list[str] = []
    flags: list[str] = []
    for a in p._actions:
        if isinstance(a, (argparse._SubParsersAction, argparse._HelpAction)):
            continue
        if not a.option_strings:
            name = f"<{a.dest}>"
            out.append(f"[{name}...]" if a.nargs in ("*", "?", argparse.REMAINDER) else (f"{name}..." if a.nargs == "+" else name))
            continue
        flag = max(a.option_strings, key=len)
        if flag in _SKIP_FLAGS:
            continue
        if a.nargs == 0:
            flags.append(flag)
        elif a.choices and len(a.choices) <= 4:
            flags.append(f"{flag} {{{','.join(map(str, a.choices))}}}")
        else:
            flags.append(f"{flag} X")
    if len(flags) > MAX_FLAGS:
        flags = flags[:MAX_FLAGS] + ["…"]
    return " ".join(out + flags)


def _lines(p: argparse.ArgumentParser, path: str, depth: int) -> list[str]:
    sp = _subparsers(p)
    if sp is None or depth > 2:
        args = _args(p)
        return [f"{path} {args}".rstrip()]
    out: list[str] = []
    own = _args(p)
    if own:
        out.append(f"{path} {own}")
    seen: set[int] = set()
    for name, sub in sp.choices.items():
        if id(sub) in seen or (depth == 0 and name in _SKIP_CMDS):  # alias của cùng lệnh con / lệnh nội bộ
            continue
        seen.add(id(sub))
        out.extend(_lines(sub, f"{path} {name}", depth + 1))
    return out


@functools.lru_cache(maxsize=1)
def cli_reference() -> str:
    from ..cli import build_parser

    lines = _lines(build_parser(), "cfharness", 0)
    return HEAD + "```text\n" + "\n".join(lines) + "\n```"
