"""Ngôn ngữ hình ảnh CLI (ui/theme.py, ui/views.py — docs/dev-notes/cli-design.md).

- Chế độ in: NO_COLOR / FORCE_COLOR / không TTY / TERM=dumb / CFHARNESS_PLAIN, ký hiệu ASCII, bề rộng hẹp.
- `--json` giữ nguyên byte dù bật chế độ có kiểu; chế độ thuần giữ chuỗi cũ.
- Chữ của agent là DỮ LIỆU: không chèn được markup rich, ANSI/OSC, bidi.
Không gọi `claude` (daemon thật + FakeRuntime như test_e2e).
"""

from __future__ import annotations

import io
import json

import pytest
import yaml
from rich.console import Console

from cfharness.cli import main
from cfharness.daemon import spawn_background
from cfharness.ui import theme as th
from cfharness.ui import views as V

MODERN = {"WT_SESSION": "1"}


class _Tty(io.StringIO):
    def isatty(self) -> bool:  # noqa: D401
        return True


def render(renderable, *, width: int = 120, color: bool = True, unicode: bool = True) -> tuple[str, str]:
    """(văn bản, chuỗi có mã ANSI) của một renderable ở chế độ có kiểu."""
    buf = _Tty()
    mode = th.Mode(styled=True, color=color, unicode=unicode, width=width)
    con = th.console(buf, mode, record=True)
    con.print(renderable)
    return con.export_text(), buf.getvalue()


# ============================================================================ chế độ in
def test_detect_tty_and_plain():
    assert th.detect(_Tty(), MODERN, platform="linux", encoding="utf-8") == th.Mode(True, True, True)
    assert th.detect(io.StringIO(), MODERN, platform="linux", encoding="utf-8").styled is False  # pipe → thuần
    assert th.detect(_Tty(), {**MODERN, "TERM": "dumb"}, platform="linux").styled is False
    assert th.detect(_Tty(), {**MODERN, "CFHARNESS_PLAIN": "1"}, platform="linux").styled is False
    assert th.detect(_Tty(), {**MODERN, "CFHARNESS_PLAIN": "0"}, platform="linux").styled is True


def test_detect_no_color_and_force_color():
    m = th.detect(_Tty(), {**MODERN, "NO_COLOR": "1"}, platform="linux", encoding="utf-8")
    assert m.styled and not m.color  # bố cục giữ, bỏ màu
    assert th.detect(_Tty(), {**MODERN, "NO_COLOR": ""}, platform="linux").color is True  # rỗng = không đặt
    f = th.detect(io.StringIO(), {**MODERN, "FORCE_COLOR": "1"}, platform="linux", encoding="utf-8")
    assert f.styled and f.color
    assert th.detect(io.StringIO(), {**MODERN, "FORCE_COLOR": "0"}, platform="linux").styled is False
    both = th.detect(io.StringIO(), {**MODERN, "FORCE_COLOR": "1", "NO_COLOR": "1"}, platform="linux")
    assert both.styled and not both.color  # NO_COLOR thắng phần màu


def test_no_color_output_has_no_colour_codes():
    _, raw = render(th.badge("run", "failed"), color=False)
    assert "\x1b[31" not in raw and "\x1b[91" not in raw  # không mã màu đỏ
    assert "lỗi" in raw
    _, raw_c = render(th.badge("run", "failed"), color=True)
    assert "\x1b[" in raw_c and "31" in raw_c


def test_unicode_fallback_rules():
    assert th.unicode_ok({"CFHARNESS_ASCII": "1", **MODERN}, "utf-8", "linux") is False
    assert th.unicode_ok({"CFHARNESS_ASCII": "0"}, "cp437", "win32") is True  # ép unicode
    assert th.unicode_ok(MODERN, "cp437", "linux") is False  # code page cũ
    assert th.unicode_ok(MODERN, "cp1252", "win32") is False
    assert th.unicode_ok({}, "utf-8", "win32") is False  # conhost cũ (không dấu hiệu terminal hiện đại)
    assert th.unicode_ok({"WT_SESSION": "x"}, "utf-8", "win32") is True  # Windows Terminal
    assert th.unicode_ok({"TERM_PROGRAM": "vscode"}, "cp65001", "win32") is True
    assert th.unicode_ok({}, "utf-8", "darwin") is True


def test_ascii_mode_glyphs_and_box():
    rows = [{"id": "r_1", "agent_id": "a", "status": "completed", "cost_usd": 0.01, "duration_ms": 1200, "queued_at": None, "prompt": "x"}]
    text, _ = render(V.runs_list(rows, th.Mode(True, True, False), width=120), unicode=False)
    assert "+ xong" in text and "---" in text
    assert not any(ch in text for ch in "✓●─◆→"), text
    assert all(ord(c) < 128 or c.isalpha() or c in "·…" for c in text)  # chỉ ASCII + chữ tiếng Việt


def test_narrow_width_drops_secondary_columns_and_stacks_kv():
    rows = [{"id": "coder", "name": "Coder", "role": "Viết code", "status": "working", "model": "sonnet", "current_run_id": "r_1",
             "last_tool": "Bash", "cost_today_usd": 0.5, "config_revision": 3}]
    wide, _ = render(V.agent_list(rows, width=140), width=140)
    narrow, _ = render(V.agent_list(rows, width=50), width=50)
    assert "VAI TRÒ" in wide and "TOOL CUỐI" in wide
    assert "VAI TRÒ" not in narrow and "TOOL CUỐI" not in narrow and "MODEL" not in narrow
    assert "coder" in narrow and "đang làm" in narrow and "$0.5000" in narrow
    assert max(len(ln) for ln in narrow.splitlines()) <= 50
    kv_narrow, _ = render(th.kv([("chi phí hôm nay", "$1")], width=40), width=40)
    assert kv_narrow.splitlines()[0].strip() == "chi phí hôm nay" and kv_narrow.splitlines()[1].strip() == "$1"


def test_status_vocabulary_matches_tui():
    from cfharness.tui.model import STATUS_STYLE

    for name, (icon, _label, _style) in STATUS_STYLE.items():
        assert th.STATUS["agent"][name].icon == icon, name
    for st in ("completed", "failed", "cancelled", "budget_exceeded", "timed_out", "interrupted", "resumed", "crashed",
               "rate_limited", "blocked", "protocol_violation", "queued", "running", "waiting_human"):
        assert st in th.STATUS["run"], st
    from cfharness.tasks import STATUSES

    assert set(STATUSES) <= set(th.STATUS["task"])
    # chỉ 16 màu ANSI có tên + thuộc tính → theo theme sáng/tối của terminal
    for style in list(th.PALETTE.values()) + [s.style for d in th.STATUS.values() for s in d.values()]:
        assert "#" not in style and "grey" not in style and "rgb" not in style and " on " not in style, style


def test_fix_hints():
    from cfharness.errors import BLOCKED, NOT_FOUND, SYSTEM_ERROR

    assert "agent list" in th.fix_for(NOT_FOUND, "không tìm thấy agent 'x'")
    assert "estop off" in th.fix_for(BLOCKED, "ESTOP đang bật — không khởi chạy run mới")
    assert "daemon logs" in th.fix_for(SYSTEM_ERROR, "hỏng")
    assert th.fix_for(BLOCKED, "agent 'x' đang paused — `cfharness agent resume x` trước") is None  # đã có lệnh
    assert th.fix_for(NOT_FOUND, "x", {"hint": "làm Y"}) == "làm Y"
    text, _ = render(th.error("agent 'x' đang paused — `cfharness agent resume x` trước", fix=None))
    assert "cfharness agent resume x" in text and "`" not in text


# ============================================================================ chữ agent là DỮ LIỆU
EVIL = "[bold red]GIẢ[/bold red] [link=https://evil.example]bấm[/link] \x1b]8;;https://evil.example\x07x\x1b]8;;\x07 \x1b[2J‮gnp.exe :emoji_x:"


def _assert_inert(text: str, raw: str, *, full: bool = True) -> None:
    assert "[bold red]GIẢ[/bold red]" in text  # markup in nguyên văn
    assert "evil.example\x07" not in raw and "\x1b]8" not in raw and "\x1b[2J" not in raw and "‮" not in raw
    if full:  # ô bảng cắt bằng `…` — phần sau có thể không hiện
        assert "[link=https://evil.example]" in text and ":emoji_x:" in text


def test_markup_injection_inert_in_views():
    ev = {"type": "message.complete", "payload": {"text": EVIL}, "run_id": "r_1"}
    _assert_inert(*render(V.event_line(ev, {})))
    tool = {"type": "tool.start", "payload": {"name": "Bash", "args_preview": EVIL, "tool_id": "t"}}
    _assert_inert(*render(V.event_line(tool, {}), width=400))
    rows = [{"id": "r_1", "agent_id": "a", "status": "running", "prompt": EVIL, "cost_usd": 0, "queued_at": None}]
    _assert_inert(*render(V.runs_list(rows), width=400), full=False)
    ap = [{"id": "ap_1", "status": "waiting_human", "agent_id": "a", "run_id": "r_1", "preview": EVIL, "matched_rule": "x"}]
    _assert_inert(*render(V.approvals(ap), width=400), full=False)
    run = {"id": "r_1", "status": "failed", "agent_id": "a", "prompt": EVIL, "summary": EVIL, "error": EVIL}
    text, raw = render(V.run_show(run), width=400)
    _assert_inert(text, raw)
    ag = {"id": "a", "name": EVIL, "role": EVIL, "description": EVIL, "status": "idle", "config_revision": 1, "home_path": "/h"}
    _assert_inert(*render(V.agent_show(ag), width=400))


def test_error_message_markup_inert(capsys, monkeypatch):
    from cfharness.cli import report_error

    monkeypatch.setenv("FORCE_COLOR", "1")
    report_error(EVIL)
    err = capsys.readouterr().err
    assert "[bold red]GIẢ[/bold red]" in err and "\x1b]8" not in err and "‮" not in err


# ============================================================================ CLI thật (daemon + FakeRuntime)
@pytest.fixture
def daemon(hub_home):
    hub_home.config.write_text(yaml.safe_dump({"runtime": {"kind": "fake", "fake": {"line_delay_s": 0.0}}}))
    spawn_background(hub_home)
    yield hub_home
    main(["daemon", "stop"])


def cli(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_json_identical_when_styled(daemon, capsys, monkeypatch):
    assert cli(capsys, "agent", "create", "coder", "--model", "haiku")[0] == 0
    assert cli(capsys, "run", "coder", "liệt kê", "--follow")[0] == 0
    run_id = json.loads(cli(capsys, "runs", "list", "--json")[1])[0]["id"]
    cmds = [("agent", "list"), ("agent", "show", "coder"), ("runs", "list"), ("runs", "show", run_id), ("approvals",),
            ("approvals", "--all"), ("estop", "status"), ("config", "get", "runs"), ("config", "get", "runs.stop_grace_s")]
    # (doctor gọi `claude --version`/`auth status` thật — không đưa vào test; nhánh --json của nó đi cùng ctx.out như các lệnh trên)
    plain = {c: cli(capsys, *c, "--json") for c in cmds}
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("WT_SESSION", "1")
    for c in cmds:
        got = cli(capsys, *c, "--json")
        assert got == plain[c], c
        assert "\x1b[" not in got[1]


def test_plain_output_stable_and_styled_output(daemon, capsys, monkeypatch):
    assert cli(capsys, "agent", "create", "coder", "--model", "haiku")[0] == 0
    code, out, _ = cli(capsys, "agent", "list")
    assert code == 0 and out.splitlines()[0].split() == ["ID", "NAME", "ROLE", "STATUS", "MODEL", "RUN", "LAST", "TOOL", "$", "HÔM", "NAY", "REV"]
    assert "\x1b[" not in out
    code, out, _ = cli(capsys, "run", "coder", "liệt kê", "--follow")
    assert code == 0 and "┊ Bash(ls)" in out and "■ completed" in out and "\x1b[" not in out

    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("WT_SESSION", "1")
    monkeypatch.setenv("COLUMNS", "120")
    code, out, _ = cli(capsys, "agent", "list")
    assert code == 0 and "\x1b[" in out and "TRẠNG THÁI" in out and "rảnh" in out
    code, out, _ = cli(capsys, "run", "coder", "liệt kê", "--follow")
    assert code == 0 and "Bash" in out and "xong" in out and "runs show" in out
    code, out, err = cli(capsys, "agent", "show", "khong-co")
    assert code == 1 and "Lỗi" in err and "Cách sửa" in err and "agent list" in err
    monkeypatch.setenv("NO_COLOR", "1")
    code, out, _ = cli(capsys, "runs", "list")
    assert code == 0 and "xong" in out and "\x1b[3" not in out  # bố cục còn, không màu


def test_overview_no_args(capsys, monkeypatch, hub_home):
    code, out, _ = cli(capsys)
    assert code == 0 and "Bắt đầu" in out and "cfharness doctor" in out and "\x1b[" not in out
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("WT_SESSION", "1")
    code, out, _ = cli(capsys, "--help")
    assert code == 0 and "Mọi lệnh" in out and "approvals" in out and "==SUPPRESS==" not in out
    monkeypatch.delenv("FORCE_COLOR")
    code, out, _ = cli(capsys, "--help")  # pipe: --help vẫn là trợ giúp argparse đầy đủ
    assert code == 0 and "usage: cfharness" in out


def test_screens_render_without_error():
    """Mọi màn dựng được ở cả unicode/ASCII, rộng/hẹp (bắt lỗi kiểu dữ liệu thiếu khoá)."""
    st = {"version": "0.1.0", "home": "/h", "epoch": "e", "last_seq": 1, "runtime": "fake", "max_concurrent_runs": 4, "running": 1,
          "queued": 2, "estop": {"on": True, "reason": "bảo trì"}, "agents": 3, "cost_today_usd": 1.2, "auth_block": True,
          "compat": {"cli": {"version": "9", "status": "untested"}, "unknown_rate": {"alert": True, "unknown": 5, "total": 10, "rate": 0.5}}}
    checks = [{"check": "a", "ok": True, "detail": "x", "level": "ok"}, {"check": "b", "ok": False, "detail": "`cfharness daemon start`", "level": "warning"},
              {"check": "c", "ok": False, "detail": "y", "level": "error"}]
    for u in (True, False):
        m = th.Mode(True, True, u)
        for w in (40, 120):
            for r in (V.overview("0.1.0", m, width=w), V.overview("0.1.0", m, commands=[("agent", "x"), ("zzz", "y")], width=w),
                      V.daemon_status(st, m, width=w), V.daemon_down("/h", None, m), V.doctor(checks, m, home="/h", width=w),
                      V.estop({"on": False}, m), V.estop({"on": True, "reason": "r"}, m, team="t"),
                      V.config_value("a", {"b": {"c": 1, "d": None, "e": [1]}, "f": True}, m), V.config_value("k", 3, m),
                      V.agent_list([], m), V.runs_list([], m), V.approvals([], m)):
                render(r, width=w, unicode=u)
    text, _ = render(V.daemon_status(st, width=120))
    assert "ESTOP BẬT" in text and "Cách sửa" in text and "claude auth login" in text
