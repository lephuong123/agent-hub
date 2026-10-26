"""`tui/cc_render.py` — kiểu vẽ Claude Code (thuần). Markdown của agent: hiển thị được nhưng KHÔNG link, KHÔNG mã điều khiển."""

from __future__ import annotations

import io

from rich.console import Console
from rich.text import Text

from cfharness.tui import cc_render as R
from test_manager_repl import EVIL


def ansi(t: Text) -> str:
    buf = io.StringIO()
    Console(file=buf, force_terminal=True, color_system="truecolor", width=120, markup=False, highlight=False).print(t)
    return buf.getvalue()


def test_shapes_like_claude_code():
    assert R.user_text("xin chào\ndòng 2").plain == "> xin chào\n  dòng 2"
    assert R.assistant_text("Mình xem nhé").plain == "● Mình xem nhé"
    tool = R.tool_text("Bash", "ls -la", result="a\nb\nc\nd\ne", duration_s=0.4).plain
    assert tool.splitlines() == ["● Bash(ls -la)  0.4s", "  ⎿  a", "     b", "     c", "     … +2 dòng (click để mở)"]
    assert "Đang chạy…" in R.tool_text("Read", "x.py", status="running", started=0, now=3).plain
    assert R.spinner_text("Đang nghĩ", elapsed_s=12, tokens=1234, frame=4).plain == "✻ Đang nghĩ… (12s · ↓ 1.2k token · esc để dừng)"
    assert R.stats_text(tool_uses=3, tokens=3400, elapsed_s=45, cost_usd=0.0123) == "3 tool · 3.4k token · 45s · $0.012"


def test_markdown_renders_like_claude_without_links_or_escapes():
    md = "# Kế hoạch\n\nMình sẽ **giao việc** cho `w1`:\n\n1. Đọc README\n2. Tóm tắt\n\n```python\nprint('hi')\n```\n\nXem [tài liệu](http://evil.example) nhé."
    t = R.markdown_text(md, 80)
    assert isinstance(t, Text)
    lines = t.plain.splitlines()
    assert lines[0] == "Kế hoạch" and "**" not in t.plain and "```" not in t.plain and "# " not in t.plain
    assert "giao việc" in t.plain and "Đọc README" in t.plain and "print('hi')" in t.plain
    assert all(ln == ln.rstrip() for ln in lines), "không đệm khoảng trắng cuối dòng"
    assert not any(getattr(s.style, "link", None) for s in t.spans if not isinstance(s.style, str))
    out = ansi(t)
    assert "\x1b]8" not in out  # không OSC 8 (link click được)


def test_markdown_strips_injected_controls_and_never_reads_rich_markup():
    t = R.markdown_text("**đậm** " + EVIL, 120)
    out = ansi(t)
    for bad in ("\x1b]8", "\x1b]0", "\x9b", "\x07", "‮", "pwned"):
        assert bad not in out, (bad, out)
    assert "[bold]B[/bold]" in t.plain  # markup Rich hiện nguyên văn
    assert "\x1b[31mĐỎ" not in out
