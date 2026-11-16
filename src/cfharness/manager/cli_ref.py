"""Khối "CLI reference" cho system prompt của điều phối viên (tiếng Anh — quy ước AGENTS.md).

Vì sao: điều phối viên không chạy được CLI `cfharness` và trước đây không biết lệnh nào có thật → bịa lệnh (vd `cfharness run list`
thay vì `cfharness runs list`). Khối này sinh TỰ ĐỘNG từ parser argparse thật (`cli.build_parser`) nên không lệch với CLI; chỉ tên
lệnh + lệnh con + tham số vị trí (không mô tả tiếng Việt) → ngắn, ổn định giữa các run (giữ prompt cache, hash chỉ đổi khi CLI đổi).
Mỗi nhóm lệnh MỘT dòng (`cfharness runs: list | show <id> | …`); cờ chỉ ghi cho lệnh cấp một không có lệnh con — lệnh con thì
người/agent xem `--help`. Bản cũ (mỗi lệnh con một dòng kèm cờ) dài ~14–18k ký tự, đẩy system prompt điều phối viên vượt giới hạn
dòng lệnh Windows (WinError 206).
"""

from __future__ import annotations

import argparse
import functools

HEAD = """# cfharness CLI reference
The owner can run these `cfharness` commands in a terminal. You cannot run them yourself.
- When the owner asks for hub data your hub tools can read (agents, runs, tasks, approvals, costs, schedules), fetch it with
  those tools and answer with the data. Do not hand over a command instead.
- Give a command only when the owner must run it (something your tools cannot do or are not allowed to do), or when the owner
  asks for one. Use ONLY commands listed below, exactly as written; never guess a command or flag. Subcommand flags are
  not listed: for any flag you are not sure of, point to `cfharness <command> <subcommand> --help` instead of guessing.
- Format: `cfharness <group>: sub1 <arg> | sub2 | …` means `cfharness <group> sub1 <arg>`, `cfharness <group> sub2`, and so on.
  `name {a|b}` means the subcommand has its own subcommands a and b. Placeholders in <angle brackets> are positional
  arguments; X is a flag value; … means more flags exist. Shared flags on every command: --json, --home HOME.
"""

_SKIP_FLAGS = {"-h", "--help", "--json", "--home"}
_SKIP_CMDS = {"hook"}  # lệnh nội bộ (claude CLI gọi), người không gõ


def _subparsers(p: argparse.ArgumentParser) -> argparse._SubParsersAction | None:  # type: ignore[name-defined]
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a
    return None


MAX_FLAGS = 4  # cờ mỗi lệnh; nhiều hơn → "…" (người/agent xem `--help`) — giữ khối ngắn


def _args(p: argparse.ArgumentParser, with_flags: bool = True) -> str:
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
    if not with_flags:
        flags = []
    elif len(flags) > MAX_FLAGS:
        flags = flags[:MAX_FLAGS] + ["…"]
    return " ".join(out + flags)


def _positional(p: argparse.ArgumentParser) -> str:
    return _args(p, with_flags=False)


def _sub_item(name: str, p: argparse.ArgumentParser) -> str:
    sp = _subparsers(p)
    if sp is None:
        return f"{name} {_positional(p)}".rstrip()
    return f"{name} {{{'|'.join(sp.choices)}}}"


def _lines(root: argparse.ArgumentParser) -> list[str]:
    sp = _subparsers(root)
    out: list[str] = []
    seen: set[int] = set()
    for name, sub in sp.choices.items() if sp else []:
        if id(sub) in seen or name in _SKIP_CMDS:  # alias của cùng lệnh / lệnh nội bộ
            continue
        seen.add(id(sub))
        ssp = _subparsers(sub)
        if ssp is None:
            out.append(f"cfharness {name} {_args(sub)}".rstrip())
            continue
        items: list[str] = []
        own = _positional(sub)
        sseen: set[int] = set()
        for sname, ssub in ssp.choices.items():
            if id(ssub) in sseen:
                continue
            sseen.add(id(ssub))
            items.append(_sub_item(sname, ssub))
        out.append(f"cfharness {name}{' ' + own if own else ''}: " + " | ".join(items))
    return out


@functools.lru_cache(maxsize=1)
def cli_reference() -> str:
    from ..cli import build_parser

    lines = ["cfharness --version"] + _lines(build_parser())
    return HEAD + "```text\n" + "\n".join(lines) + "\n```"
