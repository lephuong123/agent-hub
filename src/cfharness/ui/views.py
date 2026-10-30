"""Màn hình CLI ở chế độ có kiểu (TTY) — mỗi hàm nhận dữ liệu RPC đã có và trả renderable `rich`.

Thuần (không I/O, không RPC) để test được và để ghi ảnh SVG (`scripts/cli_screenshots.py`). Chế độ thuần (pipe/test) KHÔNG
đi qua đây: `cli.py` vẫn in chuỗi văn bản cũ. Mọi chữ của agent qua `theme.data`/`theme.cell` (termsafe + Text).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from rich.console import Group, RenderableType
from rich.text import Text

from . import theme as th
from .theme import Col, Mode, Padding

BLANK = Text("")


def _u(mode: Mode | None) -> bool:
    return mode is None or mode.unicode


def _sep(mode: Mode | None) -> str:
    return f" {th.glyph('sep', _u(mode))} "


# ============================================================================ tổng quan (cfharness / cfharness --help)
OVERVIEW: list[tuple[str, list[tuple[str, str]]]] = [
    ("Bắt đầu", [
        ("cfharness doctor", "kiểm sức khoẻ: claude, đăng nhập, daemon"),
        ("cfharness daemon start", "chạy daemon nền (CLI cũng tự khởi động khi cần)"),
        ("cfharness agent create <id> --template coder", "tạo agent từ template (researcher|coder|reviewer|ops)"),
    ]),
    ("Làm việc hằng ngày", [
        ('cfharness run <agent> "<prompt>" -f', "chạy một việc và theo dõi tới khi xong"),
        ("cfharness runs list", "các run gần đây · chi tiết: runs show <run>"),
        ("cfharness agent list", "đội agent · chi tiết: agent show <id>"),
        ("cfharness top", "màn htop trực tiếp (TUI)"),
        ("cfharness chat", "chat với điều phối viên"),
    ]),
    ("An toàn", [
        ("cfharness approvals", "thao tác phá huỷ đang giữ chờ bạn duyệt"),
        ("cfharness approve <id>", "cho phép một lần · từ chối: deny <id>"),
        ("cfharness estop on|off|status", "dừng khẩn cấp toàn hub"),
    ]),
    ("Cấu hình", [
        ("cfharness config get [khoá]", "đọc cấu hình · ghi: config set <khoá> <giá trị>"),
        ("cfharness daemon status", "trạng thái hub, chi phí hôm nay, ESTOP"),
        ("cfharness web", "giao diện trình duyệt (chỉ localhost)"),
    ]),
]

# Nhóm cho danh sách MỌI lệnh (`--help`); lệnh không có ở đây rơi vào "Khác"
COMMAND_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("Hub", ("daemon", "doctor", "config", "home", "estop", "web", "top", "attach")),
    ("Agent & run", ("agent", "run", "runs", "stop", "steer", "takeover", "template", "search", "trash")),
    ("Đội & việc", ("manager", "chat", "team", "task", "schedule", "questions", "answer", "system")),
    ("An toàn", ("approvals", "approve", "deny", "guard", "secret", "audit")),
    ("Tích hợp", ("mcp", "notify", "telemetry", "hook")),
    ("Học & đánh giá", ("learn", "knowledge", "golden", "eval", "digest", "curator", "curation", "regression", "feedback", "report", "cost")),
]


def overview(version: str, mode: Mode | None = None, *, commands: Sequence[tuple[str, str]] | None = None,
             width: int | None = None) -> RenderableType:
    parts: list[RenderableType] = [th.brand(version, "điều phối đội agent chạy trên claude CLI"), BLANK]
    narrow = width is not None and width < th.NARROW
    cw = max(len(c) for _, rows in OVERVIEW for c, _ in rows)  # một cột lệnh thẳng hàng qua mọi nhóm
    for title, rows in OVERVIEW:
        parts.append(th.section(title))
        parts.append(_cmd_grid(rows, narrow, cmd_width=cw))
        parts.append(BLANK)
    if commands:
        parts.append(th.section("Mọi lệnh"))
        known = {c for _, cs in COMMAND_GROUPS for c in cs}
        helps = dict(commands)
        groups = [(t, [c for c in cs if c in helps]) for t, cs in COMMAND_GROUPS]
        rest = [c for c, _ in commands if c not in known]
        if rest:
            groups.append(("Khác", rest))
        for t, cs in groups:
            if not cs:
                continue
            parts.append(Text(f"  {t}", style=th.MUTED))
            parts.append(_cmd_grid([(c, helps[c]) for c in cs], narrow, indent=4, cmd_width=12))
        parts.append(BLANK)
    tip = Text()
    tip.append("Chi tiết một lệnh: ", style=th.MUTED)
    tip.append("cfharness <lệnh> --help", style=th.CMD)
    tip.append(_sep(mode), style=th.MUTED)
    if not commands:
        tip.append("mọi lệnh: ", style=th.MUTED)
        tip.append("cfharness --help", style=th.CMD)
        tip.append(_sep(mode), style=th.MUTED)
    tip.append("thêm ", style=th.MUTED)
    tip.append("--json", style=th.CMD)
    tip.append(" để máy đọc", style=th.MUTED)
    parts.append(tip)
    return Group(*parts)


def _cmd_grid(rows: Sequence[tuple[str, str]], narrow: bool, *, indent: int = 2, cmd_width: int | None = None) -> RenderableType:
    from rich.table import Table

    if narrow:
        out: list[RenderableType] = []
        for c, d in rows:
            out.append(Text(c, style=th.CMD))
            out.append(Padding(Text(d, style=th.MUTED), (0, 0, 0, 2)))
        return Padding(Group(*out), (0, 0, 0, indent))
    g = Table.grid(padding=(0, 3))
    g.add_column(style=th.CMD, no_wrap=True, min_width=cmd_width)
    g.add_column(style=th.MUTED, overflow="fold")
    for c, d in rows:
        g.add_row(c, d)
    return Padding(g, (0, 0, 0, indent))


def plain_overview(version: str) -> str:
    """Bản văn bản thuần của `overview` (cfharness không đối số, khi pipe)."""
    lines = [f"cfharness v{version} — điều phối đội agent chạy trên claude CLI", ""]
    for title, rows in OVERVIEW:
        lines.append(title)
        w = max(len(c) for c, _ in rows)
        lines += [f"  {c.ljust(w)}   {d}" for c, d in rows]
        lines.append("")
    lines.append("Chi tiết một lệnh: cfharness <lệnh> --help · mọi lệnh: cfharness --help · thêm --json để máy đọc")
    return "\n".join(lines)


# ============================================================================ doctor
def doctor(checks: Sequence[dict[str, Any]], mode: Mode | None = None, *, home: str | None = None, width: int | None = None) -> RenderableType:
    from rich.table import Table

    g = Table.grid(padding=(0, 2))
    g.add_column(no_wrap=True)
    g.add_column(no_wrap=True, style=th.HEAD)
    g.add_column(overflow="fold")
    narrow = width is not None and width < th.NARROW
    body: list[RenderableType] = []
    for c in checks:
        b = th.badge("check", c.get("level"), mode, label=False)
        detail = th.cmds(c.get("detail") or "")
        if c.get("ok"):
            detail.stylize(th.MUTED)
        if narrow:
            line = Text.assemble(b, " ", Text(str(c.get("check")), style=th.HEAD))
            body += [line, Padding(detail, (0, 0, 0, 2))]
        else:
            g.add_row(b, Text(str(c.get("check"))), detail)
    n_err = sum(1 for c in checks if c.get("level") == "error")
    n_warn = sum(1 for c in checks if c.get("level") == "warning")
    n_ok = sum(1 for c in checks if c.get("ok"))
    summary = Text("  ")
    if n_err:
        summary.append(f"{n_err} lỗi", style=th.ERR_STRONG)
        summary.append(_sep(mode), style=th.MUTED)
    if n_warn:
        summary.append(f"{n_warn} cảnh báo", style=f"bold {th.WARN}")
        summary.append(_sep(mode), style=th.MUTED)
    summary.append(f"{n_ok}/{len(checks)} ổn", style=th.OK if not n_err else th.MUTED)
    parts: list[RenderableType] = [th.header("Kiểm sức khoẻ", None, mode)]
    if home:
        parts.append(Text(f"  HUB_HOME {home}", style=th.MUTED))
    parts.append(BLANK)
    parts.append(Padding(Group(*body), (0, 0, 0, 2)) if narrow else Padding(g, (0, 0, 0, 2)))
    parts += [BLANK, summary]
    if n_err:
        parts.append(Padding(th.hint("sửa các mục ✗ rồi chạy lại `cfharness doctor`" if _u(mode) else "sửa các mục x rồi chạy lại `cfharness doctor`", mode), (0, 0, 0, 2)))
    return Group(*parts)


# ============================================================================ daemon
def daemon_status(st: dict[str, Any], mode: Mode | None = None, *, width: int | None = None) -> RenderableType:
    u = _u(mode)
    es = st.get("estop") or {}
    run_line = Text()
    run_line.append(f"{st.get('running', 0)} đang chạy", style=th.OK if st.get("running") else "")
    run_line.append(_sep(mode), style=th.MUTED)
    run_line.append(f"{st.get('queued', 0)} chờ", style=th.WARN if st.get("queued") else "")
    run_line.append(f"  (tối đa {st.get('max_concurrent_runs')})", style=th.MUTED)
    estop = Text(" ESTOP BẬT ", style=th.ALARM) if es.get("on") else Text("tắt", style=th.MUTED)
    if es.get("on") and es.get("reason"):
        estop.append(f"  {th.clean(es.get('reason'))}", style=th.ERR)
    pairs: list[tuple[str, Any]] = [
        ("HUB_HOME", Text(str(st.get("home")), style=th.MUTED)),
        ("runtime", Text(str(st.get("runtime")))),
        ("run", run_line),
        ("agent", Text(str(st.get("agents", 0)))),
        ("chi phí hôm nay", Text(th.fmt_usd(st.get("cost_today_usd")), style=th.COST)),
    ]
    rl = st.get("rate_limit") or {}
    if rl:
        r = Text(str(rl.get("status") or "-"), style=th.OK if rl.get("status") in ("allowed", "ok") else th.ERR_STRONG)
        if rl.get("utilization") is not None:
            r.append(f"  {float(rl['utilization']) * 100:.0f}% {rl.get('type') or ''}".rstrip(), style=th.MUTED)
        pairs.append(("rate limit", r))
    pairs.append(("ESTOP", estop))
    teams = [t for t in (st.get("teams_estop") or []) if t]
    if teams:
        pairs.append(("ESTOP team", Text(", ".join(str(t.get("id") if isinstance(t, dict) else t) for t in teams), style=th.ERR_STRONG)))
    cli = (st.get("compat") or {}).get("cli") or {}
    if cli:
        c = Text(str(cli.get("version") or "?"))
        stc = cli.get("status")
        if stc == "untested":
            c.append("  chưa có trong ma trận đã kiểm", style=f"bold {th.WARN}")
        elif stc == "missing":
            c.append("  không chạy được `claude --version`", style=th.ERR_STRONG)
        elif stc:
            c.append(f"  ({'đã kiểm' if stc in ('ok', 'tested') else stc})", style=th.MUTED)
        pairs.append(("claude CLI", c))
    pairs.append(("epoch", Text(f"{st.get('epoch')}{_sep(mode)}seq {st.get('last_seq')}", style=th.MUTED)))
    auto = st.get("autostart") or {}
    if auto.get("text"):
        pairs.append(("tự khởi động", th.cmds(str(auto["text"]).removeprefix("tự khởi động: "), style=th.MUTED if not auto.get("installed") else "")))
    head = Text()
    head.append(f"{th.status('run', 'running').icon if u else '*'} ", style="bold green")
    head.append("daemon đang chạy", style=th.HEAD)
    head.append(f"  v{st.get('version')}", style=th.MUTED)
    parts: list[RenderableType] = [head, BLANK, th.kv(pairs, width=width)]
    warns: list[RenderableType] = []
    if st.get("auth_block"):
        warns.append(th.warning("claude chưa đăng nhập — spawn bị chặn. Chạy `claude auth login` rồi `cfharness doctor`", mode))
    ur = (st.get("compat") or {}).get("unknown_rate") or {}
    if ur.get("alert"):
        warns.append(th.error(f"{ur.get('unknown')}/{ur.get('total')} event không nhận dạng được trong 1 giờ ({float(ur.get('rate') or 0) * 100:.1f}%)",
                              mode, fix="cân nhắc `cfharness estop on` rồi kiểm phiên bản claude", label="Cảnh báo đỏ"))
    if warns:
        parts += [BLANK, *warns]
    return Group(*parts)


def daemon_started(pid: Any, socket: str, mode: Mode | None = None) -> RenderableType:
    return Group(th.success(f"daemon đã khởi động (pid {pid})", mode), Text(f"  socket {socket}", style=th.MUTED),
                 Padding(th.hint("xem trạng thái: `cfharness daemon status`", mode), (0, 0, 0, 2)))


def daemon_down(home: str, auto: dict[str, Any] | None, mode: Mode | None = None) -> RenderableType:
    u = _u(mode)
    head = Text()
    head.append(f"{'○' if u else 'o'} ", style=th.MUTED)
    head.append("daemon không chạy", style=th.HEAD)
    head.append(f"  HUB_HOME {home}", style=th.MUTED)
    parts: list[RenderableType] = [head, Padding(th.hint("khởi động: `cfharness daemon start` (lệnh khác cũng tự khởi động khi cần)", mode), (0, 0, 0, 2))]
    if auto and auto.get("text"):
        parts.append(Padding(th.cmds(auto["text"], style=th.MUTED), (0, 0, 0, 2)))
    return Group(*parts)


# ============================================================================ agent
AGENT_COLS = [
    Col("ID"), Col("TÊN", max_width=20, drop_below=110), Col("VAI TRÒ", max_width=24, drop_below=96), Col("TRẠNG THÁI"),
    Col("MODEL", max_width=18, drop_below=72), Col("RUN", drop_below=88), Col("TOOL CUỐI", max_width=18, drop_below=120),
    Col("$ HÔM NAY", justify="right"), Col("REV", justify="right", drop_below=80),
]


def agent_status_name(r: dict[str, Any]) -> str:
    return "archived" if r.get("deleted_at") or r.get("archived_at") else str(r.get("status") or "")


def agent_list(rows: Sequence[dict[str, Any]], mode: Mode | None = None, *, width: int | None = None) -> RenderableType:
    if not rows:
        return Group(th.empty("(chưa có agent)"), th.hint("tạo: `cfharness agent create <id> --template coder`", mode))
    from ..tui.model import short_tool

    body = [[Text(str(r["id"]), style=th.ID), r.get("name"), r.get("role"), th.badge("agent", agent_status_name(r), mode),
             r.get("model"), Text(str(r.get("current_run_id")), style=th.INFO) if r.get("current_run_id") else None,
             short_tool(r.get("last_tool")) if r.get("last_tool") else None,
             Text(th.fmt_usd(r.get("cost_today_usd")), style=th.COST), str(r.get("config_revision", ""))] for r in rows]
    working = sum(1 for r in rows if r.get("status") == "working")
    meta = f"{len(rows)} agent" + (f"{_sep(mode)}{working} đang làm" if working else "")
    return Group(th.header("Agent", meta, mode), BLANK, th.table(AGENT_COLS, body, mode=mode, width=width))


def agent_created(ag: dict[str, Any], mode: Mode | None = None, *, template: str | None = None) -> RenderableType:
    msg = f"đã tạo agent {ag['id']}" + (f" từ template {template}" if template else "")
    return Group(th.success(msg, mode), Text(f"  home {ag.get('home_path')}", style=th.MUTED),
                 Padding(th.hint(f"chạy thử: `cfharness run {ag['id']} \"<prompt>\" -f` · kiểm: `cfharness doctor {ag['id']}`", mode), (0, 0, 0, 2)))


def agent_show(ag: dict[str, Any], mode: Mode | None = None, *, width: int | None = None) -> RenderableType:
    tp = ag.get("tool_policy") or {}
    lim = {k: v for k, v in (ag.get("limits") or {}).items() if v is not None}
    head = Text()
    head.append(f"{th.glyph('mark', _u(mode))} ", style=th.ACCENT)
    head.append(str(ag["id"]), style=th.HEAD)
    if ag.get("name") and ag.get("name") != ag.get("id"):
        head.append("  ")
        head.append(th.data(ag["name"], line=True))
    head.append("  ")
    head.append(th.badge("agent", agent_status_name(ag), mode))
    parts: list[RenderableType] = [head]
    if ag.get("deleted_at"):
        parts.append(th.warning(f"đã xoá mềm — thùng rác: {ag.get('trash_path')} · khôi phục: `cfharness agent restore {ag['id']}`", mode, label="Đã xoá"))
    pairs: list[tuple[str, Any]] = [
        ("vai trò", th.data(ag.get("role") or "-", line=True)),
        ("mô tả", th.data(ag.get("description") or "-")),
        ("model", Text(f"{ag.get('model')}" + (f"{_sep(mode)}effort {ag['effort']}" if ag.get("effort") else "")
                       + (f"{_sep(mode)}dự phòng {ag['fallback_model']}" if ag.get("fallback_model") else ""))),
        ("run hiện tại", Text(str(ag.get("current_run_id")), style=th.INFO) if ag.get("current_run_id") else th.cell(None)),
        ("revision", Text(str(ag.get("config_revision")))),
        ("quyền", Text(str(tp.get("permission_mode") or "(mặc định)"))),
        ("tool cho phép", th.cell(",".join(tp.get("allowed_tools") or []))),
        ("tool cấm", th.cell(",".join(tp.get("disallowed_tools") or []))),
        ("trình duyệt", Text(str(tp.get("browser") or "(mặc định hub)"))),
        ("domain WebFetch", Text("không giới hạn" if tp.get("web_allow_domains") is None else ", ".join(tp["web_allow_domains"]) or "chặn mọi WebFetch")),
        ("MCP", th.cell(",".join(ag.get("mcp_servers") or []))),
        ("giới hạn", th.cell(", ".join(f"{k}={v}" for k, v in lim.items()))),
        ("workspace", Text(str(ag.get("workspace_default") or "(scratch theo run)"), style=th.MUTED)),
        ("home", Text(str(ag.get("home_path")), style=th.MUTED)),
        ("skills", th.cell(", ".join(ag.get("skills") or []))),
        ("memory", Text(f"{ag.get('memory_chars', 0)} ký tự", style=th.MUTED)),
        ("chi phí hôm nay", Text(th.fmt_usd(ag.get("cost_today_usd")), style=th.COST)),
    ]
    parts += [BLANK, th.kv(pairs, width=width)]
    runs = ag.get("recent_runs") or []
    if runs:
        parts += [BLANK, th.section("Run gần nhất"),
                  th.table([Col("RUN"), Col("TRẠNG THÁI"), Col("CHI PHÍ", justify="right"), Col("VÀO HÀNG", drop_below=64),
                            Col("REV", justify="right", drop_below=72)],
                           [[Text(str(r["id"]), style=th.ID), th.badge("run", r.get("status"), mode),
                             Text(th.fmt_usd(r.get("cost_usd")), style=th.COST), th.fmt_ts(r.get("queued_at")),
                             str(r.get("agent_config_revision") or "")] for r in runs], mode=mode, width=width)]
    parts += [BLANK, th.hint(f"chạy: `cfharness run {ag['id']} \"<prompt>\" -f` · sửa: `cfharness agent edit {ag['id']} …`", mode)]
    return Group(*parts)


# ============================================================================ run
RUN_COLS = [
    Col("RUN"), Col("AGENT", max_width=18), Col("TRẠNG THÁI"), Col("MODEL", max_width=18, drop_below=104),
    Col("CHI PHÍ", justify="right"), Col("THỜI GIAN", justify="right", drop_below=70), Col("VÀO HÀNG", drop_below=90),
    Col("PROMPT", max_width=48, drop_below=80, ratio=1),
]


def runs_list(rows: Sequence[dict[str, Any]], mode: Mode | None = None, *, width: int | None = None) -> RenderableType:
    if not rows:
        return Group(th.empty("(chưa có run)"), th.hint('chạy: `cfharness run <agent> "<prompt>" -f`', mode))
    body = [[Text(str(r["id"]), style=th.ID), r.get("agent_id"), th.badge("run", r.get("status"), mode), r.get("model"),
             Text(th.fmt_usd(r.get("cost_usd")), style=th.COST), th.fmt_ms(r.get("duration_ms")), th.fmt_ts(r.get("queued_at")),
             th.data(r.get("prompt") or "", line=True, limit=200)] for r in rows]
    live = sum(1 for r in rows if r.get("status") in ("queued", "running", "waiting_human"))
    total = sum(float(r.get("cost_usd") or 0) for r in rows)
    meta = f"{len(rows)} run{_sep(mode)}{th.fmt_usd(total)}" + (f"{_sep(mode)}{live} đang sống" if live else "")
    return Group(th.header("Run", meta, mode), BLANK, th.table(RUN_COLS, body, mode=mode, width=width),
                 BLANK, th.hint("chi tiết: `cfharness runs show <run>` · theo dõi: `cfharness runs follow <run>`", mode))


def run_show(r: dict[str, Any], mode: Mode | None = None, *, width: int | None = None) -> RenderableType:
    head = Text()
    head.append(f"{th.glyph('mark', _u(mode))} ", style=th.ACCENT)
    head.append(f"run {r.get('id')}", style=th.HEAD)
    head.append("  ")
    head.append(th.badge("run", r.get("status"), mode))
    if r.get("outcome") and r.get("outcome") != r.get("status"):
        head.append(f"  kết quả: {r.get('outcome')}", style=th.MUTED)
    usage = Text(th.fmt_usd(r.get("cost_usd")), style=th.COST)
    if r.get("max_budget_usd") is not None:
        usage.append(f" / ngân sách {th.fmt_usd(r.get('max_budget_usd'))}", style=th.MUTED)
    if r.get("cost_basis"):
        usage.append(f"  ({r['cost_basis']})", style=th.MUTED)
    timing = Text(th.fmt_ms(r.get("duration_ms")))
    timing.append(f"{_sep(mode)}{r.get('num_turns') or '-'} lượt", style=th.MUTED)
    pairs: list[tuple[str, Any]] = [
        ("agent", Text(f"{r.get('agent_id')}" + (f"  rev {r['agent_config_revision']}" if r.get("agent_config_revision") else ""))),
        ("model", Text(f"{r.get('model') or '-'}" + (f"{_sep(mode)}CLI {r['cli_version']}" if r.get("cli_version") else ""))),
        ("chi phí", usage),
        ("thời gian", timing),
        ("vào hàng", Text(f"{r.get('queued_at') or '-'}", style=th.MUTED)),
        ("bắt đầu → xong", Text(f"{r.get('started_at') or '-'} → {r.get('ended_at') or '-'}", style=th.MUTED)),
        ("session", Text(str(r.get("session_id") or "-"))),
        ("cwd", Text(str(r.get("cwd") or "-"), style=th.MUTED)),
        ("pid", Text(str(r.get("pid") or "-"), style=th.MUTED)),
        ("prompt hash", Text(str(r.get("system_prompt_hash") or "-"), style=th.MUTED)),
        ("stdout thô", Text(str(r.get("raw_log_path") or "-"), style=th.MUTED)),
        ("stderr", Text(str(r.get("stderr_log_path") or "-"), style=th.MUTED)),
    ]
    iv = r.get("isolation_violations")
    if iv:
        items = iv if isinstance(iv, list) else [iv]
        txt = Text(style=th.WARN)
        for i, x in enumerate(items):
            if i:
                txt.append("\n")
            txt.append(th.one_line(f"{x.get('kind')} {x.get('name')}" if isinstance(x, dict) else x, 100))
        pairs.append(("vi phạm cô lập", txt))
    parts: list[RenderableType] = [head, BLANK, th.kv(pairs, width=width)]
    if r.get("prompt"):
        parts += [BLANK, th.section("Prompt"), Padding(th.data(r.get("prompt"), limit=2000), (0, 0, 0, 2))]
    if r.get("summary"):
        parts += [BLANK, th.section("Tóm tắt"), Padding(th.data(r.get("summary"), limit=4000), (0, 0, 0, 2))]
    if r.get("error"):
        parts += [BLANK, th.error(th.clean(r.get("error"), 1200), mode, label="Lỗi của run")]
    argv = " ".join(r.get("argv_redacted") or [])
    if argv:
        parts += [BLANK, Text("argv (đã che)", style=th.KEY), Padding(th.data(argv, th.MUTED), (0, 0, 0, 2))]
    evs = r.get("events") or []
    if evs:
        parts += [BLANK, th.section(f"Event ({len(evs)})"),
                  th.table([Col("#", justify="right"), Col("LÚC", drop_below=70), Col("LOẠI"), Col("PAYLOAD", ratio=1, max_width=120)],
                           [[str(e.get("seq")), th.fmt_ts(e.get("ts")), Text(str(e.get("type")), style=th.INFO),
                             th.data(json.dumps(e.get("payload"), ensure_ascii=False, default=str), th.MUTED, line=True, limit=160)] for e in evs],
                           mode=mode, width=width)]
    return Group(*parts)


# ============================================================================ run --follow (một dòng mỗi event)
TERMINAL_EVENTS = ("run.completed", "run.failed", "run.cancelled", "run.interrupted")


def event_line(ev: dict[str, Any], state: dict[str, Any], mode: Mode | None = None) -> Text | None:
    """Bản có kiểu của `cli.render_event` (cùng tập event, cùng thứ tự thông tin)."""
    u = _u(mode)
    g = lambda n: th.glyph(n, u)  # noqa: E731
    t = ev.get("type")
    p = ev.get("payload") or {}
    ind = "    " if ev.get("subagent_id") else ""
    sep = _sep(mode)
    if t == "run.queued":
        x = Text(f"{g('queued')} ", style=th.INFO)
        x.append(f"run {ev.get('run_id')}", style=th.ID)
        x.append(f" vào hàng đợi{sep}agent {ev.get('agent_id')}{sep}ngân sách {th.fmt_usd(p.get('max_budget_usd'))}", style=th.MUTED)
        return x
    if t == "run.started":
        mcp = ",".join(str(s.get("name")) for s in p.get("mcp_servers") or []) or "-"
        x = Text(f"{g('start')} ", style=th.ACCENT)
        x.append(str(ev.get("agent_id")), style=th.HEAD)
        x.append(f"{sep}{p.get('model')}{sep}session {str(p.get('session_id'))[:8]}{sep}mcp {mcp}{sep}CLI {p.get('claude_code_version')}", style=th.MUTED)
        x.append(f"\n  cwd {p.get('cwd')}", style=th.MUTED)
        return x
    if t == "message.complete":
        body = th.data((p.get("text") or "").rstrip())
        if ind:
            body = Text(ind) + body
        return body
    if t == "tool.start":
        state[p.get("tool_id")] = p.get("name")
        x = Text(f"{ind}{g('tool')} ", style=th.MUTED)
        x.append(th.data(p.get("name"), th.INFO, line=True, limit=80))
        x.append(th.data(f"({p.get('args_preview') or ''})", th.MUTED, line=True, limit=200))
        return x
    if t == "tool.complete":
        err = bool(p.get("is_error"))
        prev = th.clean(p.get("result_preview") or "").splitlines()
        first = prev[0][:100] if prev else ""
        x = Text(f"{ind}  {g('err') if err else g('ok')} ", style=th.ERR_STRONG if err else th.OK)
        x.append(th.data(p.get("name"), line=True, limit=80))
        x.append(f" {p.get('duration_s')}s  ", style=th.MUTED)
        x.append(th.data(first, th.ERR if err else th.MUTED, line=True))
        if len(prev) > 1:
            x.append(f" (+{len(prev) - 1} dòng)", style=th.MUTED)
        return x
    if t == "approval.open":
        rid = th.one_line(p.get("request_id"), 60)
        x = Text(f"{g('hold')} GIỮ CHỜ DUYỆT ", style=f"bold {th.WARN}")
        x.append(rid, style=th.ID)
        x.append(": ")
        x.append(th.data(p.get("preview"), line=True, limit=200))
        if p.get("reason"):
            x.append(f" — {th.one_line(p.get('reason'), 200)}", style=th.MUTED)
        x.append("\n  ")
        x.append(th.hint(f"duyệt: `cfharness approve {rid}` · từ chối: `cfharness deny {rid}`", mode))
        return x
    if t == "approval.resolved":
        dec = str(p.get("decision"))
        x = Text(f"{g('hold')} ", style=th.MUTED)
        x.append(th.one_line(p.get("request_id"), 60), style=th.ID)
        x.append(": ")
        x.append(th.badge("approval", dec, mode))
        x.append(f" bởi {p.get('by')} ({p.get('channel')}, chờ {p.get('wait_s')}s)", style=th.MUTED)
        return x
    if t == "run.waiting_human":
        x = Text(f"{g('human')} chờ người duyệt ", style=th.ERR_STRONG)
        x.append(th.one_line(p.get("approval_id"), 60), style=th.ID)
        x.append(" — tiến trình đã dừng, giữ khoá thư mục. Khi duyệt, hub tự resume ở run mới.", style=th.MUTED)
        return x
    if t == "guard.blocked":
        x = Text(f"{g('blocked')} guard chặn: ", style=th.ERR_STRONG)
        x.append(th.data(p.get("detail"), line=True, limit=300))
        return x
    if t == "isolation.violation":
        x = Text(f"{g('isolation')} vi phạm cô lập: ", style=f"bold {th.WARN}")
        x.append(th.data(f"{p.get('kind')} {p.get('name')}", th.MUTED, line=True, limit=200))
        return x
    if t in TERMINAL_EVENTS and ev.get("source") == "hub":
        u_ = p.get("usage") or {}
        toks = sum(int(u_.get(k, 0) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
        oc = p.get("outcome") or str(t).split(".")[1]
        st_ = th.status("run", oc)
        x = Text(f"{g('end')} {st_.label}", style=st_.style)
        x.append(f"{sep}")
        x.append(th.fmt_usd(p.get("cost_usd")), style=th.COST)
        x.append(f"{sep}{th.fmt_ms(p.get('duration_ms'))}{sep}{toks} token{sep}{p.get('num_turns') or '-'} lượt", style=th.MUTED)
        if p.get("error") and oc != "completed":
            x.append("\n  ")
            x.append(Text("lỗi: ", style=th.ERR_STRONG))
            x.append(th.data(str(p["error"])[:400]))
        return x
    if t == "error":
        x = Text(f"{g('err')} ", style=th.ERR_STRONG)
        x.append(th.data(p.get("message"), line=True, limit=400))
        return x
    if t == "cli.version_warning":
        return th.warning(f"claude CLI {p.get('version')} chưa có trong ma trận đã kiểm ({', '.join(p.get('matrix') or [])})", mode)
    return None


def run_queued(run: dict[str, Any], mode: Mode | None = None) -> RenderableType:
    x = th.success(f"đã gửi run {run.get('id')}", mode)
    x.append(f"  agent {run.get('agent_id')}{_sep(mode)}{run.get('status') or 'queued'}", style=th.MUTED)
    return Group(x, Padding(th.hint(f"theo dõi: `cfharness runs follow {run.get('id')}` · xem: `cfharness runs show {run.get('id')}`", mode), (0, 0, 0, 2)))


def run_started(run: dict[str, Any], mode: Mode | None = None) -> Text:
    x = Text(f"{th.glyph('mark', _u(mode))} ", style=th.ACCENT)
    x.append(f"run {run.get('id')}", style=th.ID)
    x.append(f"  session {run.get('session_id')}", style=th.MUTED)
    return x


# ============================================================================ approvals
def approvals(rows: Sequence[dict[str, Any]], mode: Mode | None = None, *, width: int | None = None, ago: Any = None,
              show_all: bool = False) -> RenderableType:
    if not rows:
        msg = "(không có yêu cầu phê duyệt nào đang chờ)" if not show_all else "(chưa có approval)"
        return Group(th.success(msg.strip("()"), mode) if not show_all else th.empty(msg))
    cols = [Col("ID"), Col("TRẠNG THÁI"), Col("AGENT", max_width=16), Col("RUN", drop_below=100), Col("CHỜ", justify="right"),
            Col("LUẬT", max_width=22, drop_below=90), Col("THAO TÁC", ratio=1, max_width=80)]
    body = [[Text(str(r["id"]), style=th.ID), th.badge("approval", r.get("status"), mode), r.get("agent_id"), r.get("run_id"),
             ago(r) if ago else "-", r.get("matched_rule"), th.data(r.get("preview") or "", line=True, limit=300)] for r in rows]
    pending = sum(1 for r in rows if r.get("status") in ("open", "pending", "waiting_human"))
    meta = f"{pending} đang chờ" if not show_all else f"{len(rows)} yêu cầu{_sep(mode)}{pending} đang chờ"
    return Group(th.header("Phê duyệt", meta, mode), BLANK, th.table(cols, body, mode=mode, width=width), BLANK,
                 th.hint("duyệt: `cfharness approve <id>` · từ chối: `cfharness deny <id> --note '…'`", mode))


# ============================================================================ estop
def estop(st: dict[str, Any], mode: Mode | None = None, *, team: str | None = None) -> RenderableType:
    scope = f" team {team}" if team else ""
    if st.get("on"):
        x = Text(f" ESTOP{scope} BẬT ", style=th.ALARM)
        if st.get("reason"):
            x.append("  ")
            x.append(th.data(st.get("reason"), th.ERR, line=True, limit=200))
        parts: list[RenderableType] = [x, Text("  hub không khởi chạy run mới" + (" của team này" if team else ""), style=th.MUTED)]
        if st.get("stop_running"):
            parts.append(Text("  đã gửi lệnh dừng mọi run đang chạy", style=th.MUTED))
        parts.append(Padding(th.hint(f"tắt khi đã an toàn: `cfharness estop off{' --team ' + team if team else ''}`", mode), (0, 0, 0, 2)))
        return Group(*parts)
    x = th.success(f"ESTOP{scope} tắt — hub nhận run mới bình thường", mode)
    return Group(x, Padding(th.hint(f"bật khi cần dừng gấp: `cfharness estop on{' --team ' + team if team else ''} --reason '…' --stop-running`", mode), (0, 0, 0, 2)))


# ============================================================================ config
def _flatten(v: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(v, dict) and v:
        out: list[tuple[str, Any]] = []
        for k in v:
            out += _flatten(v[k], f"{prefix}.{k}" if prefix else str(k))
        return out
    return [(prefix, v)]


def _value(v: Any) -> Text:
    if v is None:
        return Text("null", style=th.MUTED)
    if isinstance(v, bool):
        return Text("true" if v else "false", style=th.OK if v else th.WARN)
    if isinstance(v, (int, float)):
        return Text(str(v), style=th.INFO)
    if isinstance(v, (list, dict)):
        return Text(th.clean(json.dumps(v, ensure_ascii=False, default=str)), style="")
    return Text(th.clean(v))


def config_value(key: str | None, v: Any, mode: Mode | None = None, *, width: int | None = None) -> RenderableType:
    if not isinstance(v, (dict, list)):
        x = Text(f"{key} = " if key else "", style=th.KEY)
        x.append(_value(v))
        return x
    if isinstance(v, list):
        return Group(th.header(key or "config", f"{len(v)} phần tử", mode), Padding(_value(v), (0, 0, 0, 2)))
    from rich.table import Table

    flat = _flatten(v, key or "")
    g = Table.grid(padding=(0, 2))
    g.add_column(no_wrap=True)
    g.add_column(overflow="fold")
    for k, val in flat:
        kt = Text()
        if key and k.startswith(key + "."):
            kt.append(key + ".", style=th.MUTED)
            kt.append(k[len(key) + 1:])
        else:
            dot = k.rfind(".")
            kt.append(k[: dot + 1], style=th.MUTED)
            kt.append(k[dot + 1:])
        g.add_row(kt, _value(val))
    return Group(th.header(key or "Cấu hình", f"{len(flat)} khoá", mode), BLANK, Padding(g, (0, 0, 0, 2)), BLANK,
                 th.hint("đổi: `cfharness config set <khoá> <giá trị>` (giá trị đọc như YAML)", mode))


def config_set(key: str, v: Any, mode: Mode | None = None) -> RenderableType:
    x = th.success("đã ghi ", mode)
    x.append(f"{key} = ", style=th.KEY)
    x.append(_value(v))
    return Group(x, Text("  có hiệu lực ngay (daemon đọc lại theo mtime)", style=th.MUTED))


# ============================================================================ thông báo một dòng
def done(msg: str, mode: Mode | None = None) -> Text:
    return th.success(msg, mode)
