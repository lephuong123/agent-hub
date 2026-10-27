"""Lệnh cài đặt quyền trong chat (`cfharness chat`, REPL điều phối viên) — `/perm`, `/profile`, `/trust`, `/trust-host`, `/sandbox`,
`/mcp-safe`, `/delegate`, `/reminders`.

Lệnh `/…` do CLIENT xử lý, KHÔNG gửi tới model: điều phối viên không gõ được. Mỗi lệnh ghi gọi đúng RPC mà CLI dùng (`config.set`,
`guard.sandbox.*`, `guard.trust.set`, `mcp.update`) — cổng `HUMAN_ONLY` + audit ở daemon như CLI. Lệnh ghi luôn in giá trị cũ → mới và hỏi
y/N; `/trust … full` và `/profile autonomous` xác nhận 2 bước.
"""

from __future__ import annotations

import fnmatch
import shlex
from typing import TYPE_CHECKING, Any

from rich.text import Text

from ..guard.cli import parse_duration, trust_text
from ..termsafe import one_line
from ..ui import theme as th

if TYPE_CHECKING:
    from .repl import ChatSession

HELP = [
    ("/perm", "tóm tắt quyền: profile, trust, ngoại lệ sandbox, thống kê duyệt 7 ngày + gợi ý bớt hỏi"),
    ("/profile [strict|balanced|autonomous]", "xem/đổi hồ sơ phê duyệt"),
    ("/trust [<agent> full|off [4h]]", "trust full theo agent: không hỏi gì, không sandbox, bypassPermissions (2 bước)"),
    ("/trust-host <agent> <host> [--remove]", "tin host dev trỏ về IP nội bộ (WebFetch/Chrome không hỏi SSRF)"),
    ("/sandbox <agent> localhost on|off", "cho agent bind cổng local + gọi 127.0.0.1"),
    ("/sandbox <agent> domain <d> [--remove]", "mở domain mạng cho Bash của agent"),
    ("/mcp-safe <server> <glob> [--clear]", "đánh dấu tool MCP không phá huỷ (vd /mcp-safe playwright browser_*)"),
    ("/delegate on|off [file,git,mcp]", "giao điều phối viên duyệt các lớp này"),
    ("/reminders on|off|once|<n>", "số lần nhắc lại mỗi yêu cầu duyệt"),
]
COMMANDS = [c.split()[0] for c, _ in HELP]

PROFILE_DESC = {
    "strict": "mọi thao tác phá huỷ/không chắc đều hỏi người",
    "balanced": "không chắc → chạy + audit; phá huỷ revert được bằng checkpoint → tự cho phép; giao điều phối viên một số lớp",
    "autonomous": "balanced + tự cho phép phá huỷ trong scratch/worktree của agent có allow_bypass",
}
DELEGABLE = ("file", "git", "mcp", "db", "system")

# luật → gợi ý lệnh bớt hỏi (chữ của hub; {agent} điền từ thống kê)
HINTS = {
    "ssrf_private_resolved": "host dev trỏ về IP nội bộ → /trust-host {agent} <host>",
    "mcp_destructive_hint": "tool MCP bị server tự gắn destructive → /mcp-safe <server> <tool-glob>",
    "mcp_unknown_tool": "tool MCP chưa probe → cfharness mcp probe <server>, hoặc /mcp-safe",
    "delete_not_run_created": "xoá thư mục tạm/do task tạo → đã tự cho phép khi guard.task_created_deletes bật (balanced)",
    "code_delete_api": "script của repo bị coi là xoá file → đã tự cho phép khi script đã commit (guard.trusted_repo_scripts)",
    "write_outside_workspace": "ghi ngoài workspace → task đặt extra_write_roots; worktree do task tạo đã tự cho phép (balanced)",
    "web_domain_not_allowed": "domain ngoài allowlist → /always <id> khi duyệt (luật web_domain)",
}


def _usage(s: "ChatSession", text: str) -> None:
    s._note(text, "warn")


async def _cfg(s: "ChatSession", key: str) -> Any:
    try:
        return await s.client.call("config.get", {"key": key})
    except Exception:  # noqa: BLE001 — khoá chưa có
        return None


async def _set(s: "ChatSession", key: str, value: Any) -> Any:
    return await s.client.call("config.set", {"key": key, "value": value}, audit=True)


def _known_agent(s: "ChatSession", aid: str) -> bool:
    rows = getattr(getattr(s.mon, "model", None), "rows", None)
    if not rows:
        return True  # chưa có snapshot — để daemon kiểm (NotFound)
    return aid in rows


async def run(s: "ChatSession", cmd: str, arg: str) -> None:
    fn = {"/perm": perm, "/profile": profile, "/trust": trust, "/trust-host": trust_host, "/sandbox": sandbox, "/mcp-safe": mcp_safe,
          "/delegate": delegate, "/reminders": reminders}[cmd]
    try:
        args = shlex.split(arg) if arg else []
    except ValueError:
        args = arg.split()
    await fn(s, args)


# ---------------------------------------------------------------------------- /perm
async def perm(s: "ChatSession", args: list[str]) -> None:
    prof = await s.client.call("approval.profile", {}) or {}
    trusts = await s.client.call("guard.trust.list", {}) or {}
    sbx = await s.client.call("guard.sandbox.list", {}) or {}
    stats = await s.client.call("approval.stats", {"days": 7}) or {}
    rem_max = await _cfg(s, "notify.reminder_max_count")
    rem_iv = await _cfg(s, "notify.reminder_interval_s")
    s.out.write(perm_text(prof, trusts, sbx, stats, rem_max, rem_iv))


def perm_text(prof: dict[str, Any], trusts: dict[str, Any], sbx: dict[str, Any], stats: dict[str, Any], rem_max: Any, rem_iv: Any) -> Text:
    t = Text()
    t.append("  Quyền & phê duyệt\n", style=f"bold {th.HEAD}")
    p = str(prof.get("profile") or "?")
    t.append("  profile  ", style=th.MUTED)
    t.append(p, style=th.HEAD)
    t.append(f"  {PROFILE_DESC.get(p, '')}\n", style=th.MUTED)
    for k, v in (prof.get("knobs") or {}).items():
        t.append(f"    {k} = {str(v.get('value')).lower()}", style=th.MUTED)
        t.append("  (đặt rõ)\n" if v.get("source") == "explicit" else "\n", style=th.MUTED)
    d = prof.get("delegate_to_manager") or {}
    t.append(f"  giao điều phối viên  {'bật' if d.get('enabled') else 'tắt'} · lớp {', '.join(d.get('classes') or []) or '-'}"
             f" · quá {int(d.get('timeout_s') or 0)}s → người\n", style=th.MUTED)
    t.append(f"  nhắc lại  mỗi {int(rem_iv or 0)}s (giãn dần) · tối đa {'∞' if rem_max is None else rem_max} lần\n", style=th.MUTED)
    if trusts:
        for line in trust_text(trusts).splitlines():
            t.append(f"  {line}\n", style=th.WARN)
    else:
        t.append("  trust full  (không agent nào)\n", style=th.MUTED)
    for aid, v in sorted(sbx.items()):
        bits = (["loopback"] if v.get("allow_localhost") else []) + [f"domain {x}" for x in v.get("allow_domains") or []
                                                                     if x not in (v.get("dev_hosts") or [])]
        bits += [f"host dev {x}" for x in v.get("dev_hosts") or []]
        if bits:
            t.append(f"  sandbox {aid}: {', '.join(bits)}\n", style=th.MUTED)
    total = int(stats.get("total") or 0)
    rules = stats.get("by_rule") or []
    deciders = {e.get("key"): int(e.get("count") or 0) for e in stats.get("by_decider") or []}
    t.append(f"\n  7 ngày qua: {total} yêu cầu duyệt, người quyết {deciders.get('human', 0)}, điều phối viên {deciders.get('manager', 0)}\n",
             style=th.HEAD)
    by_agent = stats.get("by_agent") or []
    top_agent = str(by_agent[0].get("key")) if by_agent else "<agent>"
    for e in rules[:8]:
        rule = str(e.get("key") or "-")
        t.append(f"    {int(e.get('count') or 0):>3}  {rule}", style="")
        hint = HINTS.get(rule)
        if hint:
            t.append(f"  → {hint.format(agent=top_agent)}", style=th.MUTED)
        t.append("\n")
    if not total:
        t.append("    (không có yêu cầu nào)\n", style=th.MUTED)
    return t


# ---------------------------------------------------------------------------- /profile
async def profile(s: "ChatSession", args: list[str]) -> None:
    cur = await s.client.call("approval.profile", {}) or {}
    if not args:
        p = str(cur.get("profile") or "?")
        s._note(f"profile hiện tại: {p} — {PROFILE_DESC.get(p, '')}. Đổi: /profile strict|balanced|autonomous")
        return
    new = args[0].lower()
    if new not in PROFILE_DESC:
        return _usage(s, "dùng: /profile strict|balanced|autonomous")
    old = str(cur.get("profile") or "?")
    if new == old:
        s._note(f"profile đã là {new}")
        return
    s._note(f"đổi profile {old} → {new}: {PROFILE_DESC[new]}")
    if not await s.confirm(f"Đổi approval.profile {old} → {new}? [y/N] "):
        return
    if new == "autonomous" and not await s.confirm("Bước 2/2 — autonomous tự cho phép thao tác phá huỷ trong scratch/worktree. Xác nhận? [y/N] "):
        return
    await _set(s, "approval.profile", new)
    s._note(f"approval.profile = {new}")


# ---------------------------------------------------------------------------- /trust
async def trust(s: "ChatSession", args: list[str]) -> None:
    if not args or args[0] == "list":
        rows = await s.client.call("guard.trust.list", {}) or {}
        s._note(trust_text(rows), "warn" if rows else None)
        return
    aid = args[0]
    mode = (args[1].lower() if len(args) > 1 else "full")
    if mode not in ("full", "off"):
        return _usage(s, "dùng: /trust <agent> full [4h|30m|2d] · /trust <agent> off · /trust")
    try:
        for_s = parse_duration(args[2]) if len(args) > 2 and mode == "full" else None
    except ValueError as e:
        return _usage(s, str(e))
    if not _known_agent(s, aid):
        return _usage(s, f"không có agent '{one_line(aid, 40)}' (xem /agents)")
    if mode == "off":
        if not await s.confirm(f"Tắt trust của agent {aid} (guard/sandbox như thường từ run kế tiếp)? [y/N] "):
            return
        await s.client.call("guard.trust.set", {"id": aid, "mode": "off", "channel": "cli", "by": "user"}, audit=True)
        s._note(f"agent {aid}: đã tắt trust")
        return
    s._note(f"TRUST FULL cho agent {aid}" + (f" trong {args[2]}" if for_s else " (không hết hạn)") + ": hub cho phép MỌI thao tác — "
            "xoá, push, ghi ngoài workspace, dữ liệu/chính sách của hub, mạng nội bộ — không hỏi ai. Run mới chạy bypassPermissions, "
            "không sandbox OS. Vẫn còn: audit từng thao tác, ESTOP.", "warn")
    if not await s.confirm(f"Bước 1/2 — bật trust full cho {aid}? [y/N] "):
        return
    if not await s.confirm(f"Bước 2/2 — xác nhận agent {aid} chạy KHÔNG rào chắn [y/N] "):
        return
    r = await s.client.call("guard.trust.set", {"id": aid, "mode": "full", "for_s": for_s, "channel": "cli", "by": "user"}, audit=True) or {}
    s._note(f"⚠ agent {aid}: trust FULL" + (f" tới {r.get('until')}" if r.get("until") else "") + " — áp cho run mới; tắt: /trust "
            f"{aid} off", "warn")


# ---------------------------------------------------------------------------- /trust-host
async def trust_host(s: "ChatSession", args: list[str]) -> None:
    remove = "--remove" in args
    rest = [a for a in args if a != "--remove"]
    if len(rest) != 2:
        return _usage(s, "dùng: /trust-host <agent> <host> [--remove]  (vd /trust-host web-verifier gamma-dev.creativeforce.io)")
    aid, host = rest
    if not _known_agent(s, aid):
        return _usage(s, f"không có agent '{one_line(aid, 40)}' (xem /agents)")
    what = (f"gỡ host dev {host} của agent {aid}" if remove else
            f"tin host dev {host} cho agent {aid}: WebFetch/Claude in Chrome tới đúng host này không hỏi dù trỏ về IP nội bộ; Bash ra mạng tới nó")
    if not await s.confirm(f"{what[0].upper()}{what[1:]}? [y/N] "):
        return
    await s.client.call("guard.sandbox.dev_host", {"id": aid, "host": host, "remove": remove, "channel": "cli", "by": "user"}, audit=True)
    s._note(f"đã {what}")


# ---------------------------------------------------------------------------- /sandbox
async def sandbox(s: "ChatSession", args: list[str]) -> None:
    use = "dùng: /sandbox <agent> localhost on|off · /sandbox <agent> domain <d> [--remove]"
    if len(args) < 3:
        return _usage(s, use)
    aid, what = args[0], args[1].lower()
    if not _known_agent(s, aid):
        return _usage(s, f"không có agent '{one_line(aid, 40)}' (xem /agents)")
    if what == "localhost" and args[2].lower() in ("on", "off"):
        on = args[2].lower() == "on"
        if not await s.confirm(f"{'Cho' if on else 'Gỡ quyền'} agent {aid} bind cổng local + gọi 127.0.0.1 trong sandbox? [y/N] "):
            return
        await s.client.call("guard.sandbox.set", {"id": aid, "allow_localhost": on, "channel": "cli", "by": "user"}, audit=True)
        s._note(f"agent {aid}: loopback {'BẬT' if on else 'tắt'}")
        return
    if what == "domain":
        remove = "--remove" in args[3:]
        dom = args[2]
        if not await s.confirm(f"{'Gỡ' if remove else 'Mở'} domain {dom} cho Bash của agent {aid}? [y/N] "):
            return
        r = await s.client.call("guard.sandbox.allow_domain", {"id": aid, "domain": dom, "remove": remove, "channel": "cli", "by": "user"},
                                audit=True) or {}
        s._note(f"agent {aid}: domain mở = {', '.join(r.get('allow_domains') or []) or '-'}")
        return
    _usage(s, use)


# ---------------------------------------------------------------------------- /mcp-safe
async def mcp_safe(s: "ChatSession", args: list[str]) -> None:
    clear = "--clear" in args
    rest = [a for a in args if a != "--clear"]
    if len(rest) != 2:
        return _usage(s, "dùng: /mcp-safe <server> <tool-glob> [--clear]  (vd /mcp-safe playwright browser_*)")
    name, glob = rest
    from ..browser import CHROME_SERVER

    if name == CHROME_SERVER:
        key = "runtime.claude.chrome.annotations"
        ann = {k: dict(v) for k, v in ((await _cfg(s, key)) or {}).items() if isinstance(v, dict)}
        hits: list[str] = []
    else:
        srv = await s.client.call("mcp.show", {"name": name}) or {}
        ann = {k: dict(v) for k, v in (((srv.get("config") or {}).get("tools") or {}).get("annotations") or {}).items()}
        hits = sorted(t for t in (srv.get("annotations_cache") or {}) if fnmatch.fnmatchcase(t, glob))
    cur = {} if clear else {**(ann.get(glob) or {}), "destructiveHint": False}
    what = (f"bỏ ghi đè annotations '{glob}' của MCP {name}" if clear else
            f"đánh dấu tool MCP {name} khớp '{glob}' là KHÔNG phá huỷ" + (f" ({', '.join(hits[:8])}{'…' if len(hits) > 8 else ''})" if hits else ""))
    s._note(what + " — tool tên xoá, tài nguyên bảo vệ, require_approval vẫn hỏi")
    if not await s.confirm("Áp dụng? [y/N] "):
        return
    if cur:
        ann[glob] = cur
    else:
        ann.pop(glob, None)
    if name == CHROME_SERVER:
        await _set(s, "runtime.claude.chrome.annotations", ann)
    else:
        await s.client.call("mcp.update", {"name": name, "patch": {"tools": {"annotations": ann}}}, audit=True)
    s._note(f"đã {what}")


# ---------------------------------------------------------------------------- /delegate
async def delegate(s: "ChatSession", args: list[str]) -> None:
    prof = await s.client.call("approval.profile", {}) or {}
    d = prof.get("delegate_to_manager") or {}
    if not args:
        s._note(f"giao điều phối viên: {'bật' if d.get('enabled') else 'tắt'} · lớp {', '.join(d.get('classes') or []) or '-'}"
                " — đổi: /delegate on|off [file,git,mcp]")
        return
    st = args[0].lower()
    if st not in ("on", "off"):
        return _usage(s, "dùng: /delegate on|off [file,git,mcp,db,system]")
    classes = list(d.get("classes") or [])
    if len(args) > 1:
        classes = [c.strip() for c in args[1].split(",") if c.strip()]
        bad = [c for c in classes if c not in DELEGABLE]
        if bad:
            return _usage(s, f"lớp không giao được: {', '.join(bad)} (chỉ {', '.join(DELEGABLE)})")
    new = {"enabled": st == "on", "classes": classes, "timeout_s": d.get("timeout_s") or 600}
    if not await s.confirm(f"Giao điều phối viên duyệt: {'bật' if new['enabled'] else 'tắt'} · lớp {', '.join(classes) or '-'}? [y/N] "):
        return
    await _set(s, "approval.delegate_to_manager", new)
    s._note(f"approval.delegate_to_manager = {'bật' if new['enabled'] else 'tắt'} · {', '.join(classes) or '-'}")


# ---------------------------------------------------------------------------- /reminders
async def reminders(s: "ChatSession", args: list[str]) -> None:
    cur = await _cfg(s, "notify.reminder_max_count")
    if not args:
        s._note(f"nhắc lại tối đa: {'∞' if cur is None else cur} lần mỗi yêu cầu — đổi: /reminders on|off|once|<n>")
        return
    a = args[0].lower()
    new: int | None
    if a == "on":
        new = None
    elif a == "off":
        new = 0
    elif a == "once":
        new = 1
    elif a.isdigit():
        new = int(a)
    else:
        return _usage(s, "dùng: /reminders on|off|once|<n>")
    show = lambda v: "∞" if v is None else str(v)  # noqa: E731
    if not await s.confirm(f"Nhắc lại tối đa {show(cur)} → {show(new)} lần mỗi yêu cầu duyệt (yêu cầu vẫn chờ, chỉ thôi nhắc)? [y/N] "):
        return
    await _set(s, "notify.reminder_max_count", new)
    s._note(f"notify.reminder_max_count = {show(new)}")
