"""Ngôn ngữ hình ảnh của cfharness trên terminal — đặc tả đầy đủ: `docs/dev-notes/cli-design.md`.

Ba chế độ in (`detect()`):
- **JSON** (`--json`): không qua module này — byte giữ nguyên.
- **thuần** (`Mode.styled=False`): stdout không phải TTY (pipe, test, script), `TERM=dumb` hoặc `CFHARNESS_PLAIN=1`
  → CLI in đúng chuỗi văn bản cũ (ổn định, parse được). Không ANSI.
- **có kiểu** (`Mode.styled=True`): TTY hoặc `FORCE_COLOR` → bố cục `rich` (bảng, khối khoá/giá trị, huy hiệu trạng thái).
  `NO_COLOR` (giá trị khác rỗng) bỏ MÀU nhưng giữ bố cục + đậm/mờ. `Mode.unicode=False` → ký hiệu ASCII + khung bảng ASCII.

Màu: CHỈ 16 màu ANSI có tên + thuộc tính (`bold`/`dim`/`reverse`) — terminal tự ánh xạ theo theme sáng/tối, nên cùng một
bảng màu đọc được trên nền đen lẫn nền trắng, 16/256/truecolor như nhau. Không dùng hex, không `grey62`, không `white on …`.

Chữ do agent sinh (tin nhắn, prompt, preview tool, lỗi của run…) là DỮ LIỆU: luôn qua `data()`/`cell()` = `termsafe.clean`
+ `rich.text.Text` (không markup, không emoji code). Console tạo bởi `console()` tắt markup/emoji/highlight.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TextIO

from rich import box
from rich.console import Console, Group, RenderableType
from rich.padding import Padding as _RichPadding
from rich.table import Table
from rich.text import Text

from ..termsafe import clean, one_line

# ============================================================================ bảng màu (token ngữ nghĩa)
# Giá trị là chuỗi style rich — chat (manager/chatview.py) và TUI (tui/model.py) dùng thẳng được, không cần Theme.
OK = "green"
WARN = "yellow"
ERR = "red"
ERR_STRONG = "bold red"
INFO = "cyan"
ACCENT = "bold bright_blue"  # thương hiệu: tiêu đề màn, lệnh gợi ý chính (bright_blue đọc được trên nền đen của conhost)
BRAND = "bold reverse bright_blue"  # huy hiệu " cfharness " đầu màn
ALARM = "bold reverse red"  # ESTOP BẬT, cảnh báo đỏ — thay cho "bold white on red" (theo theme, không ép nền)
MUTED = "dim"  # chữ phụ: thời gian, đường dẫn, chú thích — thay cho grey42/50/62/70
HEAD = "bold"  # tiêu đề mục, tiêu đề cột
KEY = "dim"  # cột khoá của khối khoá/giá trị
ID = "bold"  # id agent/run/approval
CMD = "cyan"  # lệnh có thể gõ lại (`cfharness …`)
COST = "magenta"  # tiền, token
USER = "bold cyan"  # người (chat) — = chatview.C_USER
MGR = "bold magenta"  # điều phối viên (chat) — = chatview.C_MGR
DATA = ""  # chữ của agent: màu mặc định của terminal

PALETTE: dict[str, str] = {
    "ok": OK, "warn": WARN, "err": ERR, "err_strong": ERR_STRONG, "info": INFO, "accent": ACCENT, "brand": BRAND,
    "alarm": ALARM, "muted": MUTED, "head": HEAD, "key": KEY, "id": ID, "cmd": CMD, "cost": COST, "user": USER,
    "mgr": MGR, "data": DATA,
}

# ============================================================================ ký hiệu (unicode, ASCII)
GLYPHS: dict[str, tuple[str, str]] = {
    "ok": ("✓", "+"),
    "warn": ("!", "!"),
    "err": ("✗", "x"),
    "info": ("•", "*"),
    "hint": ("→", "->"),
    "bullet": ("•", "-"),
    "sep": ("·", "·"),  # U+00B7 có trong cp437/cp1252 và mọi font console
    "mark": ("◆", "*"),
    "queued": ("◌", "."),
    "start": ("▶", ">"),
    "tool": ("┊", "|"),
    "end": ("■", "#"),
    "hold": ("◐", "~"),
    "human": ("⏸", "!"),
    "blocked": ("⊘", "!"),
    "isolation": ("⚠", "!"),
    "more": ("…", "..."),
}


def glyph(name: str, unicode: bool = True) -> str:
    u, a = GLYPHS.get(name, ("?", "?"))
    return u if unicode else a


# ============================================================================ từ vựng trạng thái
@dataclass(frozen=True)
class Status:
    icon: str
    ascii: str
    style: str
    label: str  # tiếng Việt, ngắn (≤ 16 ký tự) để vừa cột bảng


def _s(icon: str, asc: str, style: str, label: str) -> Status:
    return Status(icon, asc, style, label)


# Cùng ký hiệu cho cùng ý nghĩa ở mọi miền: ● đang chạy · ◐ đang giữ/chờ máy · ⏸ chờ NGƯỜI · ○ rảnh · ✓ xong · ✖ lỗi · ⊘ huỷ/chặn
# Ký hiệu agent khớp `tui/model.py` STATUS_STYLE (BRD FR-UI-01) để TUI, chat và CLI là một sản phẩm.
STATUS: dict[str, dict[str, Status]] = {
    "agent": {
        "working": _s("●", "*", "bold green", "đang làm"),
        "waiting": _s("◐", "~", "bold yellow", "chờ slot"),
        "waiting_human": _s("⏸", "!", "bold red", "chờ người duyệt"),
        "idle": _s("○", "o", "dim", "rảnh"),
        "error": _s("✖", "x", "bold red", "lỗi"),
        "paused": _s("‖", "=", "dim", "tạm dừng"),
        "disabled": _s("⊘", "-", "dim", "đã tắt"),
        "archived": _s("⊘", "-", "dim", "đã xoá mềm"),
    },
    "run": {
        "queued": _s("◌", ".", "cyan", "trong hàng đợi"),
        "running": _s("●", "*", "bold green", "đang chạy"),
        "waiting_human": _s("⏸", "!", "bold red", "chờ người duyệt"),
        "completed": _s("✓", "+", "green", "xong"),
        "failed": _s("✖", "x", "bold red", "lỗi"),
        "cancelled": _s("⊘", "-", "dim", "đã huỷ"),
        "interrupted": _s("↺", "~", "yellow", "bị ngắt"),
        "resumed": _s("↪", ">", "dim", "đã tiếp nối"),
        "budget_exceeded": _s("$", "$", "bold yellow", "vượt ngân sách"),
        "timed_out": _s("✖", "x", "bold red", "quá giờ"),
        "crashed": _s("✖", "x", "bold red", "sập"),
        "rate_limited": _s("◐", "~", "yellow", "rate limit"),
        "blocked": _s("⊘", "!", "bold red", "bị chặn"),
        "protocol_violation": _s("✖", "x", "bold red", "sai giao thức"),
    },
    "approval": {
        "open": _s("◐", "~", "bold yellow", "đang giữ"),
        "pending": _s("◐", "~", "bold yellow", "đang giữ"),
        "waiting_human": _s("⏸", "!", "bold red", "chờ người"),
        "allowed": _s("✓", "+", "green", "đã cho phép"),
        "denied": _s("✖", "x", "red", "đã từ chối"),
        "cancelled": _s("⊘", "-", "dim", "đã huỷ"),
    },
    "task": {
        "triage": _s("◇", "?", "magenta", "phân loại"),
        "todo": _s("○", "o", "dim", "cần làm"),
        "ready": _s("◌", ".", "cyan", "sẵn sàng"),
        "running": _s("●", "*", "bold green", "đang làm"),
        "review": _s("◐", "~", "yellow", "chờ review"),
        "done": _s("✓", "+", "green", "xong"),
        "blocked": _s("⊘", "!", "bold red", "bị chặn"),
        "archived": _s("⊘", "-", "dim", "lưu trữ"),
    },
    "check": {
        "ok": _s("✓", "+", "green", "ổn"),
        "warning": _s("!", "!", "bold yellow", "cảnh báo"),
        "error": _s("✗", "x", "bold red", "lỗi"),
    },
}


def status(domain: str, name: str | None) -> Status:
    s = STATUS.get(domain, {}).get(str(name or ""))
    return s or Status("?", "?", MUTED, str(name or "-"))


# ============================================================================ chế độ in
_ORIGINAL_ENCODING: dict[str, str] = {}
_LEGACY_TERM_MARKERS = ("WT_SESSION", "TERM_PROGRAM", "ConEmuANSI", "TERM", "ALACRITTY_LOG", "WEZTERM_EXECUTABLE", "ANSICON")


def remember_encodings() -> None:
    """Gọi TRƯỚC khi `cli.main` ép stdout/stderr sang UTF-8: code page gốc quyết định có in được ký hiệu unicode không."""
    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        _ORIGINAL_ENCODING.setdefault(name, str(getattr(s, "encoding", "") or ""))


def _truthy(v: str | None) -> bool:
    return v is not None and v.strip().lower() not in ("", "0", "false", "no", "off")


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


@dataclass(frozen=True)
class Mode:
    styled: bool  # bố cục rich; False = văn bản thuần (chuỗi cũ của CLI)
    color: bool
    unicode: bool
    width: int | None = None  # None = rich tự đo (COLUMNS / kích thước terminal)

    @property
    def plain(self) -> bool:
        return not self.styled


def unicode_ok(env: Mapping[str, str], encoding: str | None, platform: str | None = None) -> bool:
    """Có in ký hiệu unicode (●✓⏸, khung ─) an toàn không.

    `CFHARNESS_ASCII=1` ép ASCII, `CFHARNESS_ASCII=0` ép unicode. Không thì: code page gốc không phải UTF (cp437/cp1252…)
    → ASCII; Windows mà không có dấu hiệu terminal hiện đại (Windows Terminal `WT_SESSION`, VS Code/… `TERM_PROGRAM`,
    ConEmu, mintty `TERM`…) = conhost cũ, font raster/Consolas thiếu glyph → ASCII.
    """
    forced = env.get("CFHARNESS_ASCII")
    if forced is not None and forced.strip() != "":
        return not _truthy(forced)
    enc = (encoding or "").lower().replace("-", "").replace("_", "")
    if enc and not (enc.startswith("utf") or enc == "cp65001"):
        return False
    if (platform or sys.platform) == "win32" and not any(env.get(k) for k in _LEGACY_TERM_MARKERS):
        return False
    return True


def detect(stream: TextIO | None = None, env: Mapping[str, str] | None = None, *, platform: str | None = None,
           encoding: str | None = None) -> Mode:
    env = os.environ if env is None else env
    stream = sys.stdout if stream is None else stream
    force = _truthy(env.get("FORCE_COLOR"))
    plain = _truthy(env.get("CFHARNESS_PLAIN")) or env.get("TERM", "").lower() == "dumb"
    styled = (_isatty(stream) or force) and not plain
    color = styled and not env.get("NO_COLOR")  # no-color.org: có mặt + khác rỗng
    if encoding is None:
        name = "stderr" if stream is sys.stderr else "stdout"
        encoding = _ORIGINAL_ENCODING.get(name) or str(getattr(stream, "encoding", "") or "")
    width = None
    try:
        cols = int(env.get("COLUMNS", "") or 0)
        width = cols if cols > 0 else None
    except ValueError:
        width = None
    return Mode(styled=styled, color=color, unicode=unicode_ok(env, encoding, platform), width=width)


def console(stream: TextIO | None = None, mode: Mode | None = None, *, record: bool = False, width: int | None = None) -> Console:
    """Console rich cho chế độ có kiểu. Tắt markup/emoji/highlight: chuỗi nào lọt vào cũng in nguyên văn."""
    stream = sys.stdout if stream is None else stream
    mode = mode or detect(stream)
    return Console(
        file=stream,
        force_terminal=True if mode.styled else None,
        no_color=not mode.color,
        color_system="auto" if mode.color else None,
        markup=False,
        emoji=False,
        highlight=False,
        width=width or mode.width,
        record=record,
        safe_box=True,
        # API console win32 chỉ dùng được với console thật; FORCE_COLOR qua pipe trên Windows phải đi đường VT/ANSI
        legacy_windows=None if _isatty(stream) else False,
    )


# ============================================================================ khối dựng
def Padding(renderable: RenderableType, pad: Any = (0, 0, 0, 2), *, expand: bool = False) -> _RichPadding:  # noqa: N802
    """Padding KHÔNG giãn hết bề rộng (không để khoảng trắng cuối dòng — copy/paste và ảnh SVG sạch)."""
    return _RichPadding(renderable, pad, expand=expand)


ASCII_SIMPLE_HEAD = box.Box("    \n    \n -- \n    \n    \n    \n    \n    \n", ascii=True)
NARROW = 60  # dưới bề rộng này: khối khoá/giá trị xếp dọc, bảng bỏ cột phụ


def data(text: Any, style: str = DATA, *, limit: int | None = None, line: bool = False) -> Text:
    """Chữ của agent/người dùng → Text an toàn (termsafe + không markup)."""
    if line:
        return Text(one_line(text, limit or 10_000), style=style)
    return Text(clean(text, limit), style=style)


def cell(v: Any, style: str = "", *, limit: int | None = None) -> Text:
    """Ô bảng: None/rỗng → `-` mờ; Text giữ nguyên; còn lại = dữ liệu một dòng đã làm sạch."""
    if isinstance(v, Text):
        return v
    if v is None or v == "":
        return Text("-", style=MUTED)
    return Text(one_line(v, limit or 10_000), style=style)


def cmds(s: Any, style: str = "", cmd_style: str = CMD) -> Text:
    """Chuỗi của hub có lệnh trong dấu `…` → Text, lệnh tô `CMD` (bỏ dấu backtick)."""
    t = Text(style=style)
    for i, part in enumerate(re.split(r"`([^`]+)`", clean(s))):
        if part:
            t.append(part, style=cmd_style if i % 2 else "")
    return t


def badge(domain: str, name: str | None, mode: Mode | None = None, *, label: bool = True) -> Text:
    st = status(domain, name)
    icon = st.icon if (mode is None or mode.unicode) else st.ascii
    return Text(f"{icon} {st.label}" if label else icon, style=st.style)


def header(title: str, meta: str | Text | None = None, mode: Mode | None = None) -> Text:
    """Tiêu đề màn: ◆ tiêu đề (đậm, màu thương hiệu) + chú thích mờ."""
    t = Text()
    t.append(f"{glyph('mark', mode is None or mode.unicode)} ", style=ACCENT)
    t.append(title, style=HEAD)
    if meta:
        t.append("  ")
        t.append(meta if isinstance(meta, Text) else Text(str(meta), style=MUTED))
    return t


def brand(version: str, tagline: str = "") -> Text:
    t = Text()
    t.append(" cfharness ", style=BRAND)
    t.append(f" v{version}", style=MUTED)
    if tagline:
        t.append(f"  {tagline}")
    return t


def section(title: str) -> Text:
    return Text(title, style=HEAD)


def kv(pairs: Iterable[tuple[str, Any]], *, width: int | None = None, indent: int = 2) -> RenderableType:
    """Khối khoá/giá trị. Hẹp (< NARROW) → khoá một dòng, giá trị thụt dưới."""
    rows = [(k, v if isinstance(v, Text) or not isinstance(v, (str, int, float, type(None))) else cell(v)) for k, v in pairs]
    if width is not None and width < NARROW:
        parts: list[RenderableType] = []
        for k, v in rows:
            parts.append(Text(k, style=KEY))
            parts.append(Padding(v, (0, 0, 0, 2)))
        return Padding(Group(*parts), (0, 0, 0, indent))
    g = Table.grid(padding=(0, 2))
    g.add_column(style=KEY, no_wrap=True)
    g.add_column(overflow="fold")
    for k, v in rows:
        g.add_row(Text(k, style=KEY), v)
    return Padding(g, (0, 0, 0, indent))


@dataclass(frozen=True)
class Col:
    header: str
    justify: str = "left"
    max_width: int | None = None
    min_width: int | None = None
    drop_below: int = 0  # ẩn cột khi bề rộng terminal < giá trị này (cột phụ)
    ratio: int | None = None


def table(cols: Sequence[Col], rows: Iterable[Sequence[Any]], *, mode: Mode | None = None, width: int | None = None) -> Table:
    """Bảng thống nhất: tiêu đề đậm, gạch dưới tiêu đề, không viền; cột phụ tự ẩn khi hẹp; ô dài cắt bằng `…`."""
    unicode = mode is None or mode.unicode
    w = width or 10_000
    keep = [i for i, c in enumerate(cols) if w >= c.drop_below]
    t = Table(box=box.SIMPLE_HEAD if unicode else ASCII_SIMPLE_HEAD, show_edge=False, pad_edge=False, header_style=HEAD,
              expand=False, safe_box=not unicode, padding=(0, 1))
    for i in keep:
        c = cols[i]
        t.add_column(c.header, justify=c.justify, no_wrap=True, overflow="ellipsis", max_width=c.max_width,  # type: ignore[arg-type]
                     min_width=c.min_width, ratio=c.ratio)
    for r in rows:
        t.add_row(*[cell(r[i]) for i in keep])
    return t


def empty(text: str) -> Text:
    return Text(text, style=MUTED)


def hint(text: Any, mode: Mode | None = None) -> Text:
    """Gợi ý: → chữ mờ, lệnh trong `…` tô CMD."""
    t = Text(f"{glyph('hint', mode is None or mode.unicode)} ", style=MUTED)
    t.append(cmds(text, style=MUTED))
    return t


def success(msg: Any, mode: Mode | None = None) -> Text:
    t = Text(f"{glyph('ok', mode is None or mode.unicode)} ", style=f"bold {OK}")
    t.append(cmds(msg))
    return t


def warning(msg: Any, mode: Mode | None = None, *, label: str = "Cảnh báo") -> Text:
    t = Text(f"{glyph('warn', mode is None or mode.unicode)} {label}: ", style=f"bold {WARN}")
    t.append(cmds(msg))
    return t


def error(msg: Any, mode: Mode | None = None, *, fix: str | None = None, details: Sequence[tuple[str, str]] = (),
          label: str = "Lỗi") -> RenderableType:
    """Lỗi: ✗ Lỗi: <thông điệp> · chi tiết từng trường · `Cách sửa:` lệnh tô màu."""
    u = mode is None or mode.unicode
    head = Text(f"{glyph('err', u)} {label}: ", style=ERR_STRONG)
    head.append(cmds(msg))
    parts: list[RenderableType] = [head]
    for f, e in details:
        d = Text(f"  {glyph('bullet', u)} ", style=MUTED)
        d.append(clean(f), style=HEAD)
        d.append(": ")
        d.append(cmds(e))
        parts.append(d)
    if fix:
        h = Text("  Cách sửa: ", style=f"bold {INFO}")
        h.append(cmds(fix))
        parts.append(h)
    return Group(*parts)


# Mã lỗi JSON-RPC (errors.py) → gợi ý chung khi thông điệp chưa kèm lệnh
def fix_for(code: int | None, message: str, data_: Any = None) -> str | None:
    from ..errors import BLOCKED, CONFLICT, NOT_FOUND, SYSTEM_ERROR

    if isinstance(data_, dict) and isinstance(data_.get("hint"), str) and data_["hint"].strip():
        return data_["hint"]
    if "`" in (message or ""):
        return None  # thông điệp đã kèm lệnh — tô màu tại chỗ là đủ
    m = (message or "").lower()
    if code == NOT_FOUND:
        for word, cmd in (("approval", "cfharness approvals --all"), ("agent", "cfharness agent list"), ("run", "cfharness runs list"),
                          ("team", "cfharness team list"), ("task", "cfharness task list"), ("mcp", "cfharness mcp list")):
            if word in m:
                return f"xem danh sách: `{cmd}`"
        return None
    if code == BLOCKED:
        if "estop" in m:
            return "xem: `cfharness estop status` · tắt khi đã an toàn: `cfharness estop off`"
        if "ngân sách" in m or "budget" in m:
            return "tăng ngân sách: `cfharness agent edit <id> --budget-per-day <usd>` hoặc `cfharness run … --budget <usd>`"
        if "đăng nhập" in m or "auth" in m or "login" in m:
            return "đăng nhập: `claude auth login` rồi `cfharness doctor`"
        return None
    if code == CONFLICT and "đã tồn tại" in m:
        return "dùng id khác, hoặc xem cái đang có: `cfharness agent show <id>`"
    if code == SYSTEM_ERROR:
        return "xem log daemon: `cfharness daemon logs -n 50`"
    return None


DAEMON_FIX = "khởi động: `cfharness daemon start` · kiểm tra: `cfharness doctor`"


def fmt_usd(v: Any) -> str:
    return "-" if v is None else f"${float(v):.4f}"


def fmt_ms(ms: Any) -> str:
    if not ms:
        return "-"
    s = float(ms) / 1000
    if s < 60:
        return f"{s:.1f}s"
    s_i = int(s)
    return f"{s_i // 60}m{s_i % 60:02d}s" if s_i < 3600 else f"{s_i // 3600}h{(s_i % 3600) // 60:02d}m"


def fmt_ts(ts: Any) -> str:
    """ISO UTC → `MM-DD HH:MM` giờ máy (bảng); rỗng → `-`."""
    if not ts:
        return "-"
    from datetime import datetime

    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d.astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return str(ts)


__all__ = [
    "ACCENT", "ALARM", "BRAND", "CMD", "COST", "DATA", "ERR", "ERR_STRONG", "GLYPHS", "HEAD", "ID", "INFO", "KEY", "MGR", "MUTED",
    "NARROW", "OK", "PALETTE", "STATUS", "USER", "WARN", "Col", "Mode", "Status", "badge", "brand", "cell", "cmds", "console",
    "data", "detect", "empty", "error", "fix_for", "fmt_ms", "fmt_ts", "fmt_usd", "glyph", "header", "hint", "kv",
    "remember_encodings", "section", "status", "success", "table", "unicode_ok", "warning",
]
