"""Định dạng thuần cho giao diện web (không phụ thuộc Starlette) — dùng lại logic hiển thị của TUI (`tui/model.py`).

- Nhãn trạng thái agent/run/task bằng tiếng Việt dễ hiểu cho người không quen terminal.
- `transcript_item(ev)`: event (dict wire) → mục transcript (dict) để template `_items.html` vẽ; dùng chung cho phần
  phát lại từ DB lúc mở trang và cho luồng SSE live.
- `activity_item(ev)`: dòng "hoạt động gần đây" (firehose mức summary, FR-UI-09) — câu tiếng Việt do hub sinh, chỉ sự kiện có nghĩa (UX v2).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re as _re
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any

from ..tui.model import fmt_cost, fmt_dur, fmt_tokens, parse_ts, short_tool

__all__ = [
    "AGENT_STATUS", "RUN_STATUS", "TASK_STATUS", "fmt_cost", "fmt_dur", "fmt_tokens", "short_tool", "fmt_ts", "ago",
    "md", "transcript_item", "parse_ts", "wait_reason", "activity_item", "agent_status", "run_status", "is_live_run", "first_line", "tokens_of",
    "run_title", "question_view", "QUESTION_STATUS", "avatar_tone", "iso_ts",
    "tool_view", "merge_tool", "Transcript", "mark_final", "same_text", "url_host", "code_html", "highlight",
    "group_items", "group_view", "group_dom", "todo_view", "todo_item", "cut_note", "vi_num", "block_seq", "page_blocks", "day_label",
]

# (icon trong /static/icons.svg, nhãn, tông màu CSS)
AGENT_STATUS: dict[str, tuple[str, str, str]] = {
    "working": ("dot", "Đang làm", "ok"),
    "waiting": ("shield", "Chờ duyệt", "warn"),
    "waiting_human": ("pause", "Chờ bạn duyệt", "danger"),
    "asking": ("question", "Chờ bạn trả lời", "ask"),
    "idle": ("circle", "Rảnh", "muted"),
    "error": ("x", "Lỗi", "danger"),
    "paused": ("pause", "Tạm dừng", "muted"),
}
RUN_STATUS: dict[str, tuple[str, str]] = {
    "queued": ("Đang xếp hàng", "info"),
    "running": ("Đang chạy", "ok"),
    "waiting_human": ("Chờ bạn duyệt", "danger"),
    "completed": ("Xong", "ok"),
    "failed": ("Lỗi", "danger"),
    "cancelled": ("Đã dừng", "muted"),
    "interrupted": ("Bị gián đoạn", "warn"),
    "budget_exceeded": ("Vượt ngân sách", "warn"),
    "timed_out": ("Quá giờ", "warn"),
    "crashed": ("Tiến trình lỗi", "danger"),
    "rate_limited": ("Bị giới hạn tốc độ", "warn"),
    "blocked": ("Bị chặn", "danger"),
    "protocol_violation": ("Sai giao thức", "danger"),
    "resumed": ("Đã tiếp tục ở run khác", "muted"),
}
TASK_STATUS: dict[str, tuple[str, str]] = {
    "triage": ("Chờ phân loại", "warn"),
    "todo": ("Cần làm", "info"),
    "ready": ("Sẵn sàng", "info"),
    "running": ("Đang làm", "ok"),
    "review": ("Đang review", "info"),
    "blocked": ("Bị chặn", "danger"),
    "done": ("Xong", "muted"),
    "archived": ("Đã lưu trữ", "muted"),
}
TASK_COLUMNS = ("triage", "todo", "ready", "running", "review", "blocked", "done")
LIVE_RUN = ("queued", "running", "waiting_human")
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


WAIT_REASON = {
    "workspace_locked": "thư mục đang bị run khác giữ", "budget": "hết ngân sách", "estop": "ESTOP đang bật", "team_estop": "ESTOP team",
    "concurrency": "hết slot chạy", "agent_busy": "agent đang bận", "agent_paused": "agent tạm dừng", "rate_limit": "giới hạn tốc độ",
    "backoff": "chờ thử lại", "rate_limit_reset": "chờ hạn mức reset", "no_assignee": "chưa có người nhận", "member_limit": "thành viên đủ việc",
}


def wait_reason(r: str | None) -> str:
    return WAIT_REASON.get(r or "", r or "")


def agent_status(s: str | None) -> tuple[str, str, str]:
    return AGENT_STATUS.get(s or "", ("circle", s or "—", "muted"))


def run_status(s: str | None) -> tuple[str, str]:
    return RUN_STATUS.get(s or "", (s or "—", "muted"))


def is_live_run(run: dict[str, Any] | None) -> bool:
    return bool(run) and run.get("status") in LIVE_RUN  # type: ignore[union-attr]


def tokens_of(usage: Any) -> int:
    if isinstance(usage, str):
        try:
            usage = json.loads(usage)
        except json.JSONDecodeError:
            return 0
    if not isinstance(usage, dict):
        return 0
    return sum(int(usage.get(k) or 0) for k in USAGE_KEYS)


def fmt_ts(s: str | None, *, date: bool | None = None) -> str:
    t = parse_ts(s)
    if t is None:
        return "—"
    d = datetime.fromtimestamp(t)
    today = d.date() == datetime.now().date()
    if date is False or (date is None and today):
        return d.strftime("%H:%M:%S")
    return d.strftime("%d/%m %H:%M")


def ago(s: str | None, now: float | None = None) -> str:
    t = parse_ts(s)
    if t is None:
        return "—"
    return fmt_dur((now or time.time()) - t) + " trước"


def first_line(s: str | None, n: int = 120) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    line = s.splitlines()[0]
    return line[:n] + ("…" if len(line) > n or len(s.splitlines()) > 1 else "")


# ---------------------------------------------------------------------------- markdown an toàn (P0-2, docs/dev-notes/plan-chat-ui-claude.md)
# Chữ agent là DỮ LIỆU: html=False (HTML thô bị escape), link/ảnh chỉ http/https/mailto, ảnh KHÔNG BAO GIỜ thành <img> (chống
# beacon/theo dõi), link ngoài có rel + target + tên miền thật bên cạnh (chống giả mạo nhãn), code tô màu bằng CLASS pygments
# (CSP chặn `style=`), tên ngôn ngữ là alias lexer pygments (không bao giờ là chuỗi agent đưa). Chỉ đầu ra của md() được `|safe`.
MD_MAX = 100_000  # ký tự đầu vào tối đa (phần sau bị cắt + báo)
HL_MAX = 20_000  # khối code dài hơn → không tô màu (chỉ escape)
MD_CACHE = 256
_MD: Any = None
_MD_CACHE: OrderedDict[bytes, str] = OrderedDict()
_SAFE_SCHEMES = ("http://", "https://", "mailto:")
_LANG_OK = _re.compile(r"[A-Za-z0-9_+#.\-]{1,40}")


def _safe_url(url: str) -> bool:
    return url.strip().lower().startswith(_SAFE_SCHEMES)


def url_host(url: str) -> str:
    """Tên miền THẬT của URL để hiện cạnh link (IDN → punycode, chống giả mạo bằng ký tự giống nhau). mailto → `email`."""
    from urllib.parse import urlsplit

    u = (url or "").strip()
    if u.lower().startswith("mailto:"):
        return "email"
    try:
        host = urlsplit(u).hostname or ""
    except ValueError:
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def code_lang(info: str | None) -> tuple[str, Any]:
    """Chuỗi info của fence (do agent viết) → (tên hiển thị = alias lexer pygments, lexer). Không nhận ra → ("text", None)."""
    s = (info or "").strip()
    name = s.split(maxsplit=1)[0] if s else ""
    if not name or len(name) > 40:
        return "text", None
    try:
        from pygments.lexers import get_lexer_by_name
        from pygments.util import ClassNotFound
    except Exception:  # pragma: no cover — pygments là phụ thuộc khai báo
        return "text", None
    try:
        lx = get_lexer_by_name(name.lower())
    except (ClassNotFound, Exception):
        return "text", None
    label = (lx.aliases[0] if lx.aliases else lx.name) or "text"
    return (label if _LANG_OK.fullmatch(label) else "text"), lx


def code_html(code: str, info: str | None) -> str:
    """Khối code → `<div class="tx-code">` có đầu (tên ngôn ngữ + nút Sao chép) và `<pre><code>` (pygments chỉ dùng class)."""
    from markupsafe import escape

    label, lx = code_lang(info)
    body = ""
    if lx is not None and len(code) <= HL_MAX:
        try:
            from pygments import highlight as _hl
            from pygments.formatters import HtmlFormatter

            body = _hl(code, lx, HtmlFormatter(nowrap=True, classprefix="tk-"))
        except Exception:  # pragma: no cover
            body = ""
    if not body:
        body = str(escape(code))
    return (f'<div class="tx-code"><div class="tx-code-head"><span class="tx-lang">{escape(label)}</span>'
            f'<button type="button" class="tx-copy">Sao chép</button></div><pre><code>{body}</code></pre></div>\n')


try:
    import linkify_it  # noqa: F401  — khai báo trong pyproject (linkify-it-py)

    _HAS_LINKIFY = True
except ImportError:  # pragma: no cover
    _HAS_LINKIFY = False

# Đường dẫn TUYỆT ĐỐI trong chữ của agent (cho chip Xem/Mở ở chat Điều phối — file-open.md §6). Chỉ là gợi ý hiển thị: daemon tự kiểm
# đường dẫn nằm trong workspace của agent khi người bấm (file.read/file.open). URL bị gỡ trước để `/browse/LH-1` không thành đường dẫn.
_URL_RE = _re.compile(r"[a-z][a-z0-9+.-]*://\S+", _re.I)
_WIN_PATH_RE = _re.compile(r"(?<![\w/\\])[A-Za-z]:\\(?:[^\s`'\"<>|*?\\]+\\)*[^\s`'\"<>|*?\\]+")
_POSIX_PATH_RE = _re.compile(r"(?<![\w/.:~-])/(?:home|Users|tmp|var|opt|srv|mnt|root|private|Volumes|workspace|data)(?:/[\w.@+-]+)+")
_FILE_NAME_RE = _re.compile(r"[^\\/]*[^.\\/]\.[A-Za-z0-9]{1,8}$")  # tên có đuôi → file (thư mục: Xem/Mở luôn lỗi → không gắn chip)
PATHS_MAX = 5


def mentioned_paths(text: str | None) -> list[str]:
    """Đường dẫn FILE tuyệt đối (Windows `C:\\…`, POSIX `/home/…`, `/Users/…`…) nhắc tới trong chữ — tối đa `PATHS_MAX`, không trùng,
    bỏ dấu câu cuối. Bỏ: `..` (daemon chặn), thư mục (tên không có đuôi), `~/…` (daemon coi là tương đối)."""
    s = _URL_RE.sub(" ", str(text or "")[:MD_MAX])
    out: list[str] = []
    for m in sorted([*_WIN_PATH_RE.finditer(s), *_POSIX_PATH_RE.finditer(s)], key=lambda m: m.start()):
        p = m.group(0).rstrip(".,;:)]}»")
        if (len(p) < 4 or len(p) > 1024 or ".." in p.replace("\\", "/").split("/") or p in out
                or not _FILE_NAME_RE.search(p)):
            continue
        out.append(p)
        if len(out) >= PATHS_MAX:
            break
    return out


def path_name(path: str) -> str:
    return _re.split(r"[\\/]", path.rstrip("\\/"))[-1] or path


def _md_parser() -> Any:
    from markdown_it import MarkdownIt
    from markupsafe import escape
    from mdit_py_plugins.tasklists import tasklists_plugin

    m = MarkdownIt("commonmark", {"html": False, "linkify": _HAS_LINKIFY, "typographer": False}).enable(["table", "strikethrough"])
    if _HAS_LINKIFY:
        # URL trần (https://…) thành link — CHỈ có scheme: tắt nhận diện "mờ" (`STATE.md` sẽ thành tên miền .md, `a.py` thành .py)
        m.enable("linkify")
        m.linkify.set({"fuzzy_link": False, "fuzzy_email": False, "fuzzy_ip": False})
    m.use(tasklists_plugin)  # enabled=False → ô tích bị disabled (chỉ hiển thị)
    m.validateLink = _safe_url  # type: ignore[method-assign]

    def host_span(host: str) -> str:
        return f'<span class="ext-host"> ({escape(host)})</span>' if host else ""

    def link_open(self: Any, tokens: list[Any], idx: int, options: Any, env: dict[str, Any]) -> str:
        tok = tokens[idx]
        href = str(tok.attrGet("href") or "")
        tok.attrs = {"href": href, "class": "ext", "rel": "nofollow noopener noreferrer", "target": "_blank", "title": href}
        # autolink: chữ hiển thị đã LÀ URL → không lặp tên miền
        env.setdefault("_links", []).append("" if tok.markup in ("autolink", "linkify") else url_host(href))
        return str(self.renderToken(tokens, idx, options, env))

    def link_close(self: Any, tokens: list[Any], idx: int, options: Any, env: dict[str, Any]) -> str:
        stack = env.get("_links") or []
        return "</a>" + host_span(stack.pop() if stack else "")

    def image(self: Any, tokens: list[Any], idx: int, options: Any, env: dict[str, Any]) -> str:
        tok = tokens[idx]
        alt = self.renderInlineAsText(tok.children or [], options, env) or "không tên"
        src = str(tok.attrGet("src") or "")
        if not _safe_url(src):
            return str(escape(f"ảnh: {alt}"))
        return (f'<a class="ext img-link" href="{escape(src)}" rel="nofollow noopener noreferrer" target="_blank" title="{escape(src)}">'
                f"ảnh: {escape(alt)}</a>" + host_span(url_host(src)))

    def fence(self: Any, tokens: list[Any], idx: int, options: Any, env: dict[str, Any]) -> str:
        return code_html(tokens[idx].content, tokens[idx].info)

    def code_block(self: Any, tokens: list[Any], idx: int, options: Any, env: dict[str, Any]) -> str:
        return code_html(tokens[idx].content, None)

    def table_open(self: Any, tokens: list[Any], idx: int, options: Any, env: dict[str, Any]) -> str:
        return '<div class="tx-table"><table>\n'

    def table_close(self: Any, tokens: list[Any], idx: int, options: Any, env: dict[str, Any]) -> str:
        return "</table></div>\n"

    def cell(self: Any, tokens: list[Any], idx: int, options: Any, env: dict[str, Any]) -> str:
        tok = tokens[idx]  # căn lề cột: markdown-it ghi `style="text-align:…"` — CSP chặn style= → đổi thành class
        al = _re.search(r"text-align:\s*(left|right|center)", str(tok.attrGet("style") or ""))
        tok.attrs = {"class": f"al-{al.group(1)}"} if al else {}
        return str(self.renderToken(tokens, idx, options, env))

    for name, fn in (("link_open", link_open), ("link_close", link_close), ("image", image), ("fence", fence), ("code_block", code_block),
                     ("table_open", table_open), ("table_close", table_close), ("th_open", cell), ("td_open", cell)):
        m.add_render_rule(name, fn)
    return m


def md(text: str | None) -> str:
    """Markdown → HTML AN TOÀN (xem khối chú thích trên). LRU theo băm nội dung. Lỗi thư viện → `<pre>` escape."""
    global _MD
    from markupsafe import escape

    text = text or ""
    cut = len(text) > MD_MAX
    if cut:
        text = text[:MD_MAX]
    key = hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=16).digest()
    hit = _MD_CACHE.get(key)
    if hit is not None:
        _MD_CACHE.move_to_end(key)
        return hit
    try:
        if _MD is None:
            _MD = _md_parser()
        out = str(_MD.render(text, {}))
    except Exception:  # pragma: no cover — markdown-it-py là phụ thuộc khai báo
        out = f"<pre>{escape(text)}</pre>"
    if cut:
        out += '<p class="tx-cut">… (nội dung quá dài — đã cắt)</p>'
    _MD_CACHE[key] = out
    if len(_MD_CACHE) > MD_CACHE:
        _MD_CACHE.popitem(last=False)
    return out


# ============================================================================ transcript
FILE_TOOLS = ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit")


def file_ref(name: str, inp: Any, run_id: Any) -> dict[str, str] | None:
    """Tool đọc/ghi file → `{run, path, name}` cho chip file (xem/mở — web/file_pages.py). Đường dẫn chỉ là DỮ LIỆU hiển thị: daemon tự
    kiểm nó nằm trong workspace của run khi người bấm (file.read/file.open)."""
    if name not in FILE_TOOLS or not isinstance(inp, dict) or not run_id:
        return None
    path = inp.get("file_path") or inp.get("notebook_path")
    if not isinstance(path, str) or not path.strip() or len(path) > 4096:
        return None
    return {"run": str(run_id), "path": path, "name": path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or path}


DIFF_CTX = 3  # dòng ngữ cảnh quanh mỗi thay đổi (P1-7)
DIFF_MAX = 200  # dòng diff tối đa hiển thị cho một lời gọi tool; phần sau → "… còn N dòng"
DIFF_IN_MAX = 4000  # dòng tối đa mỗi phía đưa vào difflib (chặn chi phí với chuỗi rất dài)
WRITE_HEAD = 20  # Write: hiện N dòng đầu, phần còn lại gập trong <details>


def _udiff(old: str, new: str) -> tuple[list[tuple[str, str]], int, int]:
    """difflib.unified_diff của hai chuỗi → [(lớp, dòng)] (lớp = add/del/hunk/ctx) + số dòng thêm/bớt. Bỏ 2 dòng tiêu đề ---/+++."""
    out: list[tuple[str, str]] = []
    adds = dels = 0
    lines = difflib.unified_diff(old.splitlines()[:DIFF_IN_MAX], new.splitlines()[:DIFF_IN_MAX], lineterm="", n=DIFF_CTX)
    for i, ln in enumerate(lines):
        if i < 2 and ln.startswith(("---", "+++")):
            continue
        if ln.startswith("@@"):
            out.append(("hunk", ln))
        elif ln.startswith("+"):
            adds += 1
            out.append(("add", ln))
        elif ln.startswith("-"):
            dels += 1
            out.append(("del", ln))
        else:
            out.append(("ctx", ln))
    return out, adds, dels


def _diff_lines(name: str, inp: Any) -> dict[str, Any]:
    """Diff hiển thị của Edit/MultiEdit (unified, `DIFF_CTX` dòng ngữ cảnh) và Write (`WRITE_HEAD` dòng đầu + phần gập). `{}` = không có.
    Kết quả: `lines` [(lớp, segs)] (segs = `highlight(…, danger=False)` → ký tự ẩn/bidi lộ ra), `rest` (Write: phần gập), `more`
    (số dòng bị bỏ do trần `DIFF_MAX`), `adds`/`dels` (đếm trên TOÀN BỘ diff — nuôi "Sửa a.py +3 −1")."""
    if not isinstance(inp, dict):
        return {}
    rows: list[tuple[str, str]] = []
    rest: list[tuple[str, str]] = []
    adds = dels = more = 0
    if name in ("Edit", "MultiEdit"):
        edits = inp.get("edits") if isinstance(inp.get("edits"), list) else [inp]
        for i, e in enumerate(x for x in edits if isinstance(x, dict) and ("old_string" in x or "new_string" in x)):
            d, a, r = _udiff(str(e.get("old_string") or ""), str(e.get("new_string") or ""))
            if i and d:
                rows.append(("meta", f"— chỗ sửa thứ {i + 1} —"))
            rows += d
            adds += a
            dels += r
        more = max(0, len(rows) - DIFF_MAX)
        rows = rows[:DIFF_MAX]
    elif name == "Write" and isinstance(inp.get("content"), str):
        lines = inp["content"].splitlines()
        adds = len(lines)
        rows = [("add", "+" + ln) for ln in lines[:WRITE_HEAD]]
        rest = [("add", "+" + ln) for ln in lines[WRITE_HEAD:DIFF_MAX]]
        more = max(0, len(lines) - DIFF_MAX)
    if not rows:
        return {}
    return {"lines": [(k, highlight(t, False)) for k, t in rows], "rest": [(k, highlight(t, False)) for k, t in rest],
            "more": more, "adds": adds, "dels": dels}


def transcript_item(ev: dict[str, Any]) -> dict[str, Any] | None:
    """Một event → một mục hiển thị (None = bỏ qua). `dom` (nếu có) = id phần tử để thay tại chỗ (tool đang chạy → xong)."""
    t = ev.get("type", "")
    p = ev.get("payload") or {}
    base = {"seq": ev.get("seq"), "ts": fmt_ts(ev.get("ts"), date=False), "iso": iso_ts(ev.get("ts")) if ev.get("ts") else "",
            "sub": bool(ev.get("subagent_id")), "sid": ev.get("subagent_id") or None, "run": ev.get("run_id") or ""}
    if ev.get("subagent_id") and t in ("message.delta", "reasoning.delta", "message.complete", "reasoning.progress", "reasoning.complete"):
        return None
    if t == "message.complete":
        text = p.get("text") or ""
        if not text.strip():
            return None
        return {**base, "kind": "msg", "html": md(text), "text": text, "cut": cut_note("Tin", len(text), ev.get("run_id")) if p.get("truncated") else ""}
    if t == "reasoning.complete":
        text = p.get("text") or ""
        return {**base, "kind": "reasoning", "text": text or ("(suy nghĩ bị ẩn)" if p.get("redacted") else ""),
                "cut": cut_note("Suy nghĩ", len(text), ev.get("run_id")) if p.get("truncated") else ""}
    if t == "run.queued":
        budget = p.get("max_budget_usd")
        return {**base, "kind": "prompt", "dom": f"ev-prompt-{ev.get('seq')}", "text": p.get("prompt_preview") or "",
                "meta": f"{p.get('kind') or 'adhoc'} · ngân sách {'$' + str(budget) if budget else '—'}" + (" · tiếp tục phiên cũ" if p.get("resume") else "")}
    if t == "run.started":  # P0-8: thành dòng meta nhỏ dưới yêu cầu (Transcript.feed); không có yêu cầu → dòng riêng
        mcp = [str(m.get("name")) for m in p.get("mcp_servers") or [] if isinstance(m, dict)]
        return {**base, "kind": "start", "text": f"Bắt đầu · model {p.get('model') or '?'} · quyền {p.get('permission_mode') or '?'}", "mcp": mcp}
    if t == "tool.start":
        name = p.get("name") or "?"
        inp = p.get("input") if isinstance(p.get("input"), dict) else None
        it = {**base, "kind": "tool", "dom": f"tool-{p.get('tool_id') or ev.get('seq')}", "tool": name, "name": short_tool(name), "summary": p.get("args_preview") or "",
              "running": True, "dur": None, "error": False, "result": "", "input": inp, "file": file_ref(name, inp, ev.get("run_id"))}
        it["view"] = tool_view(name, inp, "", False, None, summary=it["summary"])
        return it
    if t == "tool.complete":
        name = p.get("name") or "?"
        prev = (p.get("result_preview") or "").strip()
        it = {**base, "kind": "tool", "dom": f"tool-{p.get('tool_id') or ev.get('seq')}", "tool": name, "name": short_tool(name), "summary": p.get("args_preview") or "",
              "running": False, "dur": p.get("duration_s"), "error": bool(p.get("is_error")), "result": prev[:TOOL_OUT_MAX], "input": None, "file": None}
        it["view"] = tool_view(name, None, it["result"], it["error"], it["dur"], summary=it["summary"])
        return it
    if t == "subagent.spawn":  # P0-8: bỏ dòng riêng — mục tool `Task`/`Agent` đã nói việc giao cho subagent
        return None
    if t in ("approval.open", "approval.waiting_human"):
        # một yêu cầu = MỘT mục (dom theo id): waiting_human thay tại chỗ mục approval.open (không lặp 2–3 lần)
        aid = p.get("request_id") or p.get("approval_id")
        return {**base, "kind": "approval", "dom": f"ev-ap-{aid}" if aid else None, "human": t == "approval.waiting_human", "tool": p.get("tool") or "",
                "preview": p.get("preview") or "", "short": highlight(first_line(p.get("preview"), 100), False), "rule": p.get("matched_rule") or "", "reason": p.get("reason") or "", "id": aid}
    if t == "approval.resolved":
        dec = p.get("decision")
        label = {"allowed": "Đã cho phép", "denied": "Đã từ chối", "cancelled": "Đã huỷ yêu cầu duyệt"}.get(dec, str(dec))
        aid = p.get("request_id")
        return {**base, "kind": "approval", "resolved": True, "decision": dec, "dom": f"ev-ap-{aid}" if aid else None, "tool": p.get("tool") or "",
                "preview": p.get("preview") or "", "short": highlight(first_line(p.get("preview"), 100), False), "id": aid,
                "label": f"{label} bởi {p.get('by') or '?'} qua {p.get('channel') or '?'}" + (f" — {p['note']}" if p.get("note") else "")}
    if t in ("question.open", "question.escalated", "question.reminder"):
        qid = p.get("question_id")
        who = "điều phối viên" if p.get("routed_to") == "manager" else "bạn"
        return {**base, "kind": "question", "dom": f"ev-q-{qid}" if qid else None, "id": qid, "text": p.get("text") or "", "short": highlight(first_line(p.get("text"), 140), False), "answered": False,
                "label": f"Agent hỏi {who}" + (" (điều phối viên chuyển lên bạn)" if t == "question.escalated" else "")}
    if t == "question.answered":
        qid = p.get("question_id")
        pick = "; ".join(f"{k}. {lbl}" for k, lbl in zip(p.get("keys") or [], p.get("labels") or []))
        who = "điều phối viên" if str(p.get("by") or "").startswith("manager:") else "bạn"
        text = " — ".join(x for x in (pick, p.get("text") or "") if x)
        return {**base, "kind": "question", "dom": f"ev-q-{qid}" if qid else None, "id": qid, "answered": True,
                "label": f"Đã được {who} trả lời qua {p.get('channel') or '?'}", "text": text, "short": highlight(first_line(text, 140), False)}
    if t == "question.cancelled":
        qid = p.get("question_id")
        return {**base, "kind": "note", "tone": "muted", "dom": f"ev-q-{qid}" if qid else None, "text": f"Câu hỏi đã huỷ ({p.get('reason') or ''})"}
    if t == "user.message":
        return {**base, "kind": "steer", "dom": f"ev-steer-{ev.get('seq')}", "text": p.get("text") or "", "meta": f"{p.get('by') or 'user'} qua {p.get('channel') or '?'}"}
    if t == "steer.ack":  # P0-8: dấu ✓ trên mục steer gần nhất (Transcript.feed), không thành dòng riêng
        return {**base, "kind": "ack"}
    if t in ("run.completed", "run.failed", "run.cancelled", "run.interrupted", "run.waiting_human") and ev.get("source", "hub") == "hub":
        oc = p.get("outcome") or t.split(".")[1]
        label, tone = run_status(oc)
        dur = (p.get("duration_ms") or 0) / 1000
        return {**base, "kind": "end", "tone": tone, "text": f"{label} · {fmt_dur(dur) if dur else '—'} · {p.get('num_turns') or '?'} lượt · ${float(p.get('cost_usd') or 0):.4f}",
                "error": str(p.get("error") or "")[:400] if oc != "completed" else ""}
    if t in ("error", "isolation.violation", "permission.denied", "guard.blocked", "auth.blocked", "budget.exceeded", "auth.denied"):
        detail = p.get("message") or p.get("detail") or p.get("name") or p.get("tool_name") or p
        return {**base, "kind": "note", "tone": "danger", "text": f"{EVENT_VI.get(t, t)}: {str(detail)[:300]}"}
    if t == "run.takeover":
        return {**base, "kind": "note", "tone": "info", "text": "Người tiếp quản bằng Claude Code"}
    return None


def vi_num(n: int) -> str:
    """8000 → "8.000" (dấu chấm ngăn nghìn kiểu Việt)."""
    return f"{int(n):,}".replace(",", ".")


def cut_note(what: str, n: int, run_id: Any = None) -> str:
    """Báo rõ khi hub chỉ giữ phần đầu (P2): "Tin bị cắt ở 8.000 ký tự — xem đủ bằng `cfharness runs logs <id>`" (log thô của run giữ
    nguyên văn stdout của CLI). Chữ thuần — template autoescape; lệnh nằm trong `<code>` (macro `tx_cut`)."""
    return f"{what} bị cắt ở {vi_num(n)} ký tự" + (" — xem đủ bằng" if run_id else "")


# ---------------------------------------------------------------------------- dòng tool kiểu Claude (P0-7)
TOOL_OUT_MAX = 2000  # = RESULT_PREVIEW_MAX của adapter: output dài bằng ngưỡng này là đã bị cắt
TOOL_IN_MAX = 20_000
TARGET_MAX = 300


def _base(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or path


def _nlines(s: Any) -> int:
    s = str(s or "")
    return len(s.splitlines()) if s else 0


def tool_view(name: str | None, inp: Any, result: str | None = "", is_error: bool = False, dur: float | None = None, *,
              summary: str = "") -> dict[str, Any]:
    """Một lời gọi tool → dòng tóm tắt kiểu Claude (`verb` + `target` + `meta`) và phần mở rộng Input/Output đầy đủ.
    Mọi chữ là DỮ LIỆU (template autoescape); `*_segs` = đoạn đã lộ ký tự ẩn/bidi (`highlight(…, danger=False)`)."""
    name = name or "?"
    d = inp if isinstance(inp, dict) else {}

    def s(k: str) -> str:
        v = d.get(k)
        return v if isinstance(v, str) else ("" if v is None else str(v))

    verb, target, meta = name, "", ""
    diff = _diff_lines(name, d) if d else {}
    if name == "Read":
        verb, target = "Đọc", _base(s("file_path"))
        if isinstance(d.get("offset"), int) or isinstance(d.get("limit"), int):
            meta = " ".join(x for x in (f"từ dòng {d['offset']}" if isinstance(d.get("offset"), int) else "",
                                        f"{d['limit']} dòng" if isinstance(d.get("limit"), int) else "") if x)
    elif name == "Bash":
        verb = "Chạy"
        target = s("description").strip() or (s("command").strip().splitlines() or [""])[0]
    elif name in ("Edit", "MultiEdit"):
        verb, target = "Sửa", _base(s("file_path"))
        if diff:
            meta = f"+{diff['adds']} −{diff['dels']}"
        elif "old_string" in d or "new_string" in d:
            meta = "+0 −0"
    elif name == "NotebookEdit":
        verb, target = "Sửa", _base(s("notebook_path"))
    elif name == "Write":
        verb, target = "Ghi", _base(s("file_path"))
        if "content" in d:
            meta = f"{_nlines(d.get('content'))} dòng"
    elif name == "Grep":
        verb, target = "Tìm", f'"{s("pattern")}"' if s("pattern") else ""
        where = s("path") or s("glob")
        meta = f"trong {_base(where)}" if where else ""
    elif name == "Glob":
        verb, target = "Liệt kê file", f'"{s("pattern")}"' if s("pattern") else ""
    elif name == "WebFetch":
        verb, target = "Tải", url_host(s("url")) or s("url")
    elif name == "WebSearch":
        verb, target = "Tìm web", f'"{s("query")}"' if s("query") else ""
    elif name in ("Task", "Agent"):
        verb, target, meta = "Giao subagent", s("description"), s("subagent_type")
    elif name == "TodoWrite":
        verb = "Cập nhật danh sách việc"
        meta = f"{len(d['todos'])} mục" if isinstance(d.get("todos"), list) else ""
    elif name.startswith("mcp__"):
        parts = name.split("__")
        verb = f"{parts[-1]} · {parts[1]}" if len(parts) >= 3 else short_tool(name)
    target = " ".join((target or summary or "").split())[:TARGET_MAX]
    # Input đầy đủ: Bash = lệnh nguyên văn (+ tham số khác); tool khác = JSON thụt lề (cắt ở TOOL_IN_MAX)
    if name == "Bash" and isinstance(d.get("command"), str):
        text = d["command"]
        extra = {k: v for k, v in d.items() if k not in ("command", "description")}
        if extra:
            text += "\n\n# tham số khác: " + json.dumps(extra, ensure_ascii=False, default=str)
    elif d:
        text = json.dumps(d, ensure_ascii=False, indent=2, default=str)
    else:
        text = summary or ""
    in_cut = len(text) > TOOL_IN_MAX
    text = text[:TOOL_IN_MAX]
    out = (result or "")[:TOOL_OUT_MAX]
    tsegs = highlight(target, False)
    return {"verb": verb, "target": target, "target_segs": tsegs, "target_title": "".join(t for _, t in tsegs), "meta": meta,
            "input_text": text, "input_segs": highlight(text, False), "input_cut": in_cut,
            "output_text": out, "output_segs": highlight(out, False), "truncated": len(result or "") >= TOOL_OUT_MAX,
            "error": bool(is_error), "dur": dur, "diff": diff}


def merge_tool(prev: dict[str, Any], it: dict[str, Any]) -> dict[str, Any]:
    """Gộp mục `tool.start` (có input/diff/chip file) với mục mới cùng `dom` (thường là `tool.complete`, không mang input).
    Dùng CHUNG cho phát lại và live → live không còn mất args/diff/tóm tắt khi tool xong."""
    out = dict(it)
    for k in ("input", "file", "summary"):
        if not out.get(k):
            out[k] = prev.get(k)
    for k in ("ts", "iso"):  # giờ của dòng tool = lúc BẮT ĐẦU
        if prev.get(k):
            out[k] = prev[k]
    if out.get("tool") in (None, "?") and prev.get("tool"):
        out["tool"], out["name"] = prev["tool"], prev.get("name")
    out["view"] = tool_view(out.get("tool"), out.get("input"), out.get("result") or "", bool(out.get("error")), out.get("dur"),
                            summary=out.get("summary") or "")
    return out


# ---------------------------------------------------------------------------- TodoWrite → checklist (P2)
TODO_STATUS = {"completed": "xong", "in_progress": "đang làm", "pending": "chưa làm"}
TODO_MAX = 50  # mục tối đa vẽ ra (phần sau → "… còn N mục")


def todo_view(todos: Any) -> dict[str, Any]:
    """`input.todos` của TodoWrite ([{content, status, activeForm}]) → checklist: mục đang làm hiện `activeForm` ("Đang chạy test"),
    mục khác hiện `content`. Mọi chữ là DỮ LIỆU (template autoescape). `current` = việc đang làm (tiêu đề khi gập)."""
    rows: list[dict[str, Any]] = []
    for t in todos if isinstance(todos, list) else []:
        if not isinstance(t, dict):
            continue
        st = str(t.get("status") or "pending")
        st = st if st in TODO_STATUS else "pending"
        text = str((t.get("activeForm") if st == "in_progress" else None) or t.get("content") or t.get("activeForm") or "")
        rows.append({"text": " ".join(text.split())[:TARGET_MAX], "status": st, "label": TODO_STATUS[st]})
    done = sum(1 for r in rows if r["status"] == "completed")
    cur = next((r["text"] for r in rows if r["status"] == "in_progress"), "")
    return {"rows": rows[:TODO_MAX], "more": max(0, len(rows) - TODO_MAX), "n": len(rows), "done": done, "current": cur}


def todo_item(ev: dict[str, Any]) -> dict[str, Any] | None:
    """`tool.start` của TodoWrite (cấp ngoài) → MỘT mục "Việc cần làm" mỗi run (`dom = todo-<run>`): lần cập nhật sau thay tại chỗ."""
    p = ev.get("payload") or {}
    if ev.get("type") != "tool.start" or p.get("name") != "TodoWrite" or ev.get("subagent_id"):
        return None
    inp = p.get("input") if isinstance(p.get("input"), dict) else {}
    if not isinstance(inp.get("todos"), list):
        return None
    return {"kind": "todo", "dom": f"todo-{ev.get('run_id') or 'run'}", "seq": ev.get("seq"), "ts": fmt_ts(ev.get("ts"), date=False),
            "iso": iso_ts(ev.get("ts")) if ev.get("ts") else "", "sub": False, "sid": None, "run": ev.get("run_id") or "",
            "todo": todo_view(inp["todos"])}


# ---------------------------------------------------------------------------- gom tool liên tiếp (P1-2) — hàm thuần, dùng lại được
GROUP_CLOSE_AT = 3  # nhóm đã xong, không lỗi, từ N tool trở lên → gập sẵn
NEST_KINDS = ("tool", "note")  # mục của subagent được lồng vào mục tool Task/Agent cha; duyệt/câu hỏi luôn ở cấp ngoài


def group_dom(first: dict[str, Any]) -> str:
    """id DOM của nhóm = theo tool ĐẦU TIÊN của nhóm (ổn định giữa phát lại và live)."""
    return f"grp-{first.get('dom') or first.get('seq')}"


def group_view(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Tóm tắt một nhóm tool: "Đã dùng 5 công cụ" + "Đọc ×3, Chạy ×2" + dòng đang chạy mới nhất. `open` = trạng thái mở MẶC ĐỊNH
    (đang chạy hoặc có lỗi → mở; xong và ≥ GROUP_CLOSE_AT tool → gập; nhóm 1–2 tool → mở). `solo` = nhóm 1 tool (không hiện tiêu đề)."""
    counts: dict[str, int] = {}
    for t in tools:
        v = (t.get("view") or {}).get("verb") or t.get("name") or "?"
        counts[v] = counts.get(v, 0) + 1
    verbs = ", ".join(f"{v} ×{c}" for v, c in list(counts.items())[:4]) + (", …" if len(counts) > 4 else "")
    running = [t for t in tools if t.get("running")]
    error = any(t.get("error") for t in tools)
    latest = ""
    if running:
        v = running[-1].get("view") or {}
        latest = " ".join(x for x in (v.get("verb") or running[-1].get("name") or "", v.get("target") or "") if x)
    n = len(tools)
    return {"n": n, "label": f"Đã dùng {n} công cụ", "verbs": verbs, "running": bool(running), "error": error, "latest": latest,
            "latest_segs": highlight(latest[:TARGET_MAX], False) if latest else [],
            "open": bool(running) or error or n < GROUP_CLOSE_AT, "solo": n == 1}


def group_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Danh sách mục phẳng → khối: các mục `tool` LIỀN NHAU (không có tin/duyệt/… xen giữa) gộp thành
    `{"kind": "group", "dom", "tools": [...], **group_view(tools)}`; mục khác giữ nguyên. Vẽ bằng `_transcript.html` `tx_group(g)`.
    Thuần — trang Điều phối dùng lại được (truyền mục có `kind`/`view`/`running`/`error`/`dom`)."""
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for it in items:
        if it.get("kind") == "tool":
            if cur is None:
                cur = {"kind": "group", "dom": group_dom(it), "tools": []}
                out.append(cur)
            cur["tools"].append(it)
        else:
            cur = None
            out.append(it)
    for b in out:
        if b.get("kind") == "group":
            b.update(group_view(b["tools"]))
    return out


class Transcript:
    """Gộp event → mục transcript, DÙNG CHUNG cho phát lại từ DB (`replay_items`) và luồng SSE (`_run_converter`) → cùng HTML.
    `feed(ev)` trả các mục mới/được thay (cùng `dom` → client thay tại chỗ):
    - tool.start + tool.complete cùng id → một mục (`merge_tool`); approval/question cùng id → một mục;
    - `run.started` → dòng meta nhỏ dưới yêu cầu; `steer.ack` → ✓ trên mục chỉ đạo gần nhất (P0-8);
    - P1-2: tool cấp ngoài mang `grp` (= `group_dom` của tool đầu dãy liền nhau — cùng quy tắc với `group_items`);
    - P1-3: mục của subagent (`subagent_id` = id tool Task/Agent cha, xem claude_adapter) mang `parent` = `tool-<id>` và nằm TRONG
      mục cha (`tree(it)["children"]`); cha chưa có trong transcript → ở cấp ngoài như cũ.
    `blocks()` = cây để vẽ (phát lại); `heads(it)` = các khung chứa mục (nhóm, tool cha — ngoài vào trong) để live cập nhật tiêu đề."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self._at: dict[str, int] = {}
        self._kids: dict[str, list[int]] = {}
        self._groups: dict[str, list[int]] = {}
        self._last: tuple[str, str | None] = ("", None)  # (kind, grp) của mục cấp ngoài vừa NỐI THÊM
        self.nodes = 0  # số khối cấp ngoài đã nối (mục không phải tool + nhóm mới) — live biết khi nào client mở khối mới
        self._prompt: dict[str, Any] | None = None
        self._steer: dict[str, Any] | None = None
        self._todo_ids: set[Any] = set()  # tool_id của các lần gọi TodoWrite đã thành checklist
        self.think_tokens: int | None = None  # ước lượng token suy nghĩ gần nhất (`reasoning.progress`) — gắn vào khối suy nghĩ kế

    def feed(self, ev: dict[str, Any]) -> list[dict[str, Any]]:
        t = ev.get("type")
        p = ev.get("payload") or {}
        if t == "reasoning.progress":  # P2: "Đã suy nghĩ · ~1.2k token" trên khối suy nghĩ
            if not ev.get("subagent_id") and isinstance(p.get("estimated_tokens"), (int, float)):
                self.think_tokens = int(p["estimated_tokens"])
            return []
        it = todo_item(ev)
        if it is not None:  # P2: TodoWrite → một checklist mỗi run, cập nhật tại chỗ (không thành dòng tool)
            self._todo_ids.add(p.get("tool_id"))
        elif t == "tool.complete" and p.get("tool_id") in self._todo_ids and not ev.get("subagent_id"):
            if not p.get("is_error"):
                return []
            it = transcript_item(ev)  # lỗi: vẫn hiện dòng tool lỗi
        else:
            it = transcript_item(ev)
        if it is None:
            return []
        k = it["kind"]
        if k == "reasoning" and not it.get("sub"):
            it["tokens"] = fmt_tokens(self.think_tokens) if self.think_tokens else ""
            self.think_tokens = None
        if k == "start" and self._prompt is not None and not self._prompt.get("start"):
            self._prompt["start"] = it
            return [self._prompt]
        if k == "ack":
            if self._steer is not None:
                self._steer["acked"] = True
                return [self._steer]
            it = {**it, "kind": "note", "tone": "info", "text": "Agent đã nhận chỉ đạo"}
        dom = it.get("dom")
        if dom and dom in self._at:
            i = self._at[dom]
            prev = self.items[i]
            if k == "tool" and prev.get("kind") == "tool":
                it = merge_tool(prev, it)
            for key in ("parent", "grp"):  # vị trí trong cây không đổi khi thay tại chỗ
                if prev.get(key):
                    it[key] = prev[key]
                else:
                    it.pop(key, None)
            it["seq0"] = prev.get("seq0")
            self.items[i] = it
        else:
            it["seq0"] = it.get("seq")  # seq của event TẠO mục (giữ khi thay tại chỗ) — mốc phân trang run dài (`?before=`)
            i = len(self.items)
            if dom:
                self._at[dom] = i
            pdom = f"tool-{it['sid']}" if it.get("sid") and k in NEST_KINDS else None
            if pdom and pdom in self._at and pdom != dom:
                it["parent"] = pdom
                self._kids.setdefault(pdom, []).append(i)
            else:
                kind, grp = self._last
                if k == "tool":
                    it["grp"] = grp if kind == "tool" and grp else group_dom(it)
                    self._groups.setdefault(it["grp"], []).append(i)
                if k != "tool" or it["grp"] != grp:
                    self.nodes += 1
                self._last = (k, it.get("grp"))
            self.items.append(it)
        if k == "prompt":
            self._prompt = it
        elif k == "steer":
            self._steer = it
        return [it]

    def tree(self, it: dict[str, Any]) -> dict[str, Any]:
        """Mục tool kèm `children` (mục subagent lồng, đệ quy), `kids_n` (số tool con). Mục khác trả nguyên (cùng đối tượng)."""
        if it.get("kind") != "tool":
            return it
        kids = [self.tree(self.items[j]) for j in self._kids.get(str(it.get("dom")), [])]
        return {**it, "children": kids, "kids_n": sum(1 for c in kids if c.get("kind") == "tool")}

    def group(self, gid: str) -> dict[str, Any]:
        tools = [self.tree(self.items[j]) for j in self._groups.get(gid, [])]
        return {"kind": "group", "dom": gid, "tools": tools, **group_view(tools)}

    def blocks(self) -> list[dict[str, Any]]:
        """Cây để vẽ: mục cấp ngoài, tool liền nhau gộp nhóm (`group_items`), mục subagent nằm trong tool cha."""
        return group_items([self.tree(it) for it in self.items if not it.get("parent")])

    def heads(self, it: dict[str, Any]) -> list[dict[str, Any]]:
        """Các khung chứa `it`, NGOÀI → TRONG: nhóm của tool cấp ngoài cùng, rồi từng tool cha (dạng `tree`). Mục cấp ngoài không
        phải tool → []. Live dùng để tạo nhóm còn thiếu và cập nhật tiêu đề ("Đã dùng N công cụ", "Đang chạy… (n công cụ)")."""
        chain: list[dict[str, Any]] = []
        cur = it
        seen = 0
        while cur.get("parent") and seen < 64:
            p = self.items[self._at[cur["parent"]]]
            chain.insert(0, self.tree(p))
            cur, seen = p, seen + 1
        if cur.get("grp"):
            chain.insert(0, self.group(cur["grp"]))
        return chain


# ---------------------------------------------------------------------------- run dài (P2): chỉ vẽ phần cuối, tải dần phần trước
LONG_RUN = 300  # trên N khối cấp ngoài → chỉ vẽ LONG_PAGE khối cuối + nút "Hiện các mục trước"
LONG_PAGE = 150


def block_seq(b: dict[str, Any]) -> Any:
    """Mốc của một khối cấp ngoài = seq của event TẠO nó (nhóm → tool đầu nhóm). Nhóm không bao giờ bị cắt đôi."""
    first = (b.get("tools") or [b])[0] if b.get("kind") == "group" else b
    return first.get("seq0") if first.get("seq0") is not None else first.get("seq")


def page_blocks(blocks: list[dict[str, Any]], before: int | None = None) -> tuple[list[dict[str, Any]], Any, int]:
    """Phân trang KHỐI (nhóm tool giữ nguyên): trả (khối cần vẽ, mốc `before` cho nút tải phần trước hoặc None, số khối còn ẩn).
    `before=None` (mở trang): ≤ LONG_RUN khối → vẽ hết; nhiều hơn → LONG_PAGE khối cuối. `before=<seq>` (partial): các khối TRƯỚC khối
    có mốc đó, tối đa LONG_PAGE khối cuối của phần ấy (mốc không có → rỗng)."""
    if before is not None:
        idx = next((i for i, b in enumerate(blocks) if block_seq(b) == before), None)
        if idx is None:
            return [], None, 0
        blocks = blocks[:idx]
        if len(blocks) <= LONG_PAGE:
            return blocks, None, 0
    elif len(blocks) <= LONG_RUN:
        return blocks, None, 0
    shown = blocks[-LONG_PAGE:]
    return shown, block_seq(shown[0]), len(blocks) - len(shown)


def day_label(ts: Any, today: Any = None) -> tuple[str, str]:
    """(khoá ngày "YYYY-MM-DD", nhãn): "Hôm nay" / "Hôm qua" / "Thứ Hai, 10/11/2026" — vạch ngăn theo ngày (giờ máy chủ). ("", "") nếu
    không đọc được thời gian."""
    t = parse_ts(ts)
    if t is None:
        return "", ""
    d = datetime.fromtimestamp(t).date()
    now = today or datetime.now().date()
    if d == now:
        lab = "Hôm nay"
    elif (now - d).days == 1:
        lab = "Hôm qua"
    else:
        lab = ("Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ nhật")[d.weekday()] + d.strftime(", %d/%m/%Y")
    return d.isoformat(), lab


def mark_final(items: list[dict[str, Any]]) -> str:
    """Đánh dấu tin agent CUỐI (`final`) và trả chữ gốc của nó ("" nếu không có)."""
    for it in reversed(items):
        if it.get("kind") == "msg" and not it.get("sub"):
            it["final"] = True
            return str(it.get("text") or "")
    return ""


def same_text(a: str | None, b: str | None) -> bool:
    """So hai đoạn chữ sau khi gộp khoảng trắng (thẻ Kết quả trùng tin cuối → không lặp lại)."""
    na, nb = " ".join((a or "").split()), " ".join((b or "").split())
    return bool(na) and na == nb


def stats_of(ev: dict[str, Any]) -> dict[str, Any] | None:
    p = ev.get("payload") or {}
    if ev.get("type") == "usage.tick":  # `out` = token đầu ra (dòng trạng thái "✳ Đang nghĩ… 12s · 3.4k token")
        return {"tokens": fmt_tokens(p.get("total_tokens")), "cost": fmt_cost(p.get("cost_usd")), "out": fmt_tokens(p.get("output_tokens"))}
    return None


# ============================================================================ firehose ("Hoạt động gần đây")
# Chỉ sự kiện CÓ NGHĨA với người (bắt đầu/xong/lỗi/duyệt/hỏi/task/học/điều phối viên) — câu chữ tiếng Việt do HUB sinh; chữ của
# agent (prompt, câu hỏi) chỉ là phần trích ngắn, template autoescape. Tool/lời agent từng bước không vào đây (xem trang run).
EVENT_VI = {
    "error": "Lỗi", "isolation.violation": "Vi phạm cô lập", "permission.denied": "Bị từ chối quyền", "auth.blocked": "claude chưa đăng nhập",
    "budget.exceeded": "Vượt ngân sách", "auth.denied": "Bị chặn (chỉ người)", "guard.blocked": "Guard chặn", "notify.failed": "Gửi thông báo lỗi",
}
_LEARN_VI = {"learning.applied": "tự học: áp dụng thay đổi", "learning.rolled_back": "tự học: đã rollback", "learning.eval_failed": "tự học: eval không đạt",
             "learning.canary": "tự học: đang thử (canary)", "learning.promoted": "tự học: đã chốt thay đổi"}


def _clip(s: Any, n: int = 90) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def activity_text(ev: dict[str, Any]) -> tuple[str, str] | None:
    """(câu tiếng Việt, tông) cho một event, hoặc None = không hiện (nhiễu)."""
    t = ev.get("type", "")
    p = ev.get("payload") or {}
    if ev.get("subagent_id"):
        return None
    if t in ("run.completed", "run.failed", "run.cancelled", "run.interrupted") and ev.get("source", "hub") == "hub":
        oc = p.get("outcome") or t.split(".")[1]
        label, tone = run_status(oc)
        dur = (p.get("duration_ms") or 0) / 1000
        extra = f" · {fmt_dur(dur)}" if dur else ""
        err = f" — {_clip(p.get('error'), 70)}" if p.get("error") and oc != "completed" else ""
        return f"{label.lower()} · ${float(p.get('cost_usd') or 0):.4f}{extra}{err}", {"ok": "ok", "muted": ""}.get(tone, tone)
    if t in ("approval.open", "approval.waiting_human"):
        what = _clip(p.get("preview") or p.get("tool"), 70)
        return (f"chờ bạn duyệt: {what}", "danger") if t == "approval.waiting_human" else (f"xin duyệt: {what}", "warn")
    if t == "approval.resolved":
        dec = {"allowed": "được cho phép", "denied": "bị từ chối", "cancelled": "đã huỷ"}.get(p.get("decision"), str(p.get("decision")))
        return f"thao tác {dec} (bởi {p.get('by') or '?'} qua {p.get('channel') or '?'})", "ok" if p.get("decision") == "allowed" else ""
    if t == "question.open":
        who = "điều phối viên" if p.get("routed_to") == "manager" else "bạn"
        return f"hỏi {who}: “{_clip(p.get('text'), 80)}”", "ask"
    if t == "question.escalated":
        return f"câu hỏi được chuyển lên bạn: “{_clip(p.get('text'), 70)}”", "ask"
    if t == "question.answered":
        who = "điều phối viên" if str(p.get("by") or "").startswith("manager:") else "bạn"
        return f"câu hỏi đã được {who} trả lời", "ok"
    if t == "task.status":
        to = p.get("to")
        if to not in ("running", "done", "blocked", "review"):
            return None
        label, tone = TASK_STATUS.get(to or "", (to, ""))
        return f"task {p.get('task_id') or ''} → {label.lower()}" + (f": {_clip(p.get('reason'), 60)}" if to == "blocked" and p.get("reason") else ""), \
            {"muted": "ok"}.get(tone, tone)
    if t == "manager.action":
        return _clip(p.get("summary") or p.get("tool"), 100), "danger" if p.get("ok") is False else "info"
    if t in _LEARN_VI:
        return _LEARN_VI[t], "warn" if t in ("learning.rolled_back", "learning.eval_failed") else ""
    if t == "user.message":
        return f"bạn chỉ đạo qua {p.get('channel') or '?'}: “{_clip(p.get('text'), 70)}”", "info"
    if t == "hub.estop":
        return (f"ESTOP BẬT {p.get('reason') or ''}".strip(), "danger") if p.get("on") else ("ESTOP đã tắt", "ok")
    if t in EVENT_VI and t != "notify.failed":
        return f"{EVENT_VI[t]}: {_clip(p.get('message') or p.get('detail') or p.get('rule') or p.get('name') or '', 70)}", "danger"
    return None


def activity_item(ev: dict[str, Any]) -> dict[str, Any] | None:
    r = activity_text(ev)
    if r is None:
        return None
    text, tone = r
    return {"ts": fmt_ts(ev.get("ts"), date=False), "iso": ev.get("ts"), "agent": ev.get("agent_id") or "hub", "text": text, "tone": tone,
            "run_id": ev.get("run_id"), "task_id": ev.get("task_id") or (ev.get("payload") or {}).get("task_id")}


# ============================================================================ run, câu hỏi, avatar
_HUB_HEAD = __import__("re").compile(r"^\s*\[cfharness[^\]]*\]\s*")


def run_title(run: dict[str, Any], titles: dict[str, str] | None = None, n: int = 110) -> str:
    """Tiêu đề người đọc được: tiêu đề task nếu run thuộc task; không thì dòng đầu prompt (bỏ tiêu đề `[cfharness …]`)."""
    tid = run.get("task_id")
    if tid and titles and titles.get(tid):
        return first_line(titles[tid], n)
    if run.get("kind") == "manager":
        pr = str(run.get("prompt") or "")
        return "Điều phối viên · nhịp kiểm tra" if pr.lstrip().startswith("[cfharness heartbeat") else "Điều phối viên · trả lời tin nhắn"
    s = _HUB_HEAD.sub("", str(run.get("prompt") or ""))
    return first_line(s, n) or "—"


QUESTION_STATUS = {"with_manager": ("Đang chờ điều phối viên", "info"), "open": ("Chờ bạn trả lời", "ask"), "answered": ("Đã trả lời", "ok"),
                   "cancelled": ("Đã huỷ", "muted")}


def question_view(q: dict[str, Any]) -> dict[str, Any]:
    """Câu hỏi → dữ liệu cho thẻ (chữ agent giữ nguyên — template autoescape + pre-wrap)."""
    a = q.get("answer") or {}
    pick = [f"{k}. {lbl}" for k, lbl in zip(a.get("keys") or [], a.get("labels") or [])]
    by = str(q.get("answered_by") or "")
    who = "Điều phối viên" if by.startswith("manager:") else ("Hub" if by == "hub" else "Bạn")
    label, tone = QUESTION_STATUS.get(q.get("status") or "", (q.get("status") or "", "muted"))
    return {"status_label": label, "status_tone": tone, "pending": q.get("status") in ("with_manager", "open"), "pick": pick,
            "answer_text": a.get("text") or "", "answered_by": who, "options": q.get("options") or [], "single_quick": bool(q.get("options")) and not q.get("multi")}


def avatar_tone(name: str | None) -> str:
    return f"c{sum(ord(c) for c in (name or '?')) % 5}"


def iso_ts(s: Any) -> str:
    """ISO cho thuộc tính `datetime` của <time> (JS hiện thời gian tương đối)."""
    t = parse_ts(s)
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds") if t else ""


# ============================================================================ xem đầy đủ thao tác chờ duyệt (WT-2)

_DANGER = _re.compile(
    r"(?<![\w-])(rm|rmdir|unlink|shred|truncate|dd|mkfs[\w.]*|mv|cp|rsync|chmod|chown|kill|pkill|killall|sudo|crontab|launchctl|systemctl|"
    r"schtasks|sc|reg|wmic|Register-ScheduledTask|Set-ScheduledTask|Start-ScheduledTask|New-Service|Invoke-WmiMethod|Invoke-CimMethod|"
    r"curl|wget|tee|find|xargs|sqlite3|psql|mysql)(?![\w-])"
    r"|git\s+(push|reset|clean|checkout|restore|branch\s+-D|filter-branch|rebase)|--force(?:-with-lease)?\b|--delete\b|--hard\b|-(?:rf|fr|r|f)\b"
    r"|\b(DROP|DELETE|TRUNCATE|ALTER|UPDATE|INSERT)\b|>>?|\|\s*(?:sh|bash|zsh)\b",
    _re.IGNORECASE,
)
_INVISIBLE = _re.compile("[\u0000-\u0008\u000b\u000c\u000d\u000e-\u001f\u007f​-‏‪-‮⁠-⁤⁦-⁩﻿]")
LONG_ACTION = 400


def _visible(ch: str) -> str:
    return {"\r": "␍"}.get(ch, f"⟦U+{ord(ch):04X}⟧")


def highlight(text: str, danger: bool = True) -> list[tuple[str, str]]:
    """Văn bản → các đoạn (loại, chữ): `''` thường, `'hit'` phần nguy hiểm (mark), `'ctl'` ký tự ẩn/điều khiển/bidi hiện thành ký hiệu.
    `danger=False`: chỉ lộ ký tự ẩn/bidi (dòng tool trong transcript — không tô "nguy hiểm")."""
    out: list[tuple[str, str]] = []
    pos = 0
    hits = [(m.start(), m.end(), "hit") for m in _DANGER.finditer(text)] if danger else []
    marks = sorted(hits + [(m.start(), m.end(), "ctl") for m in _INVISIBLE.finditer(text)])
    for s, e, k in marks:
        if s < pos:
            continue
        if s > pos:
            out.append(("", text[pos:s]))
        seg = text[s:e]
        out.append((k, "".join(_visible(c) for c in seg) if k == "ctl" else seg))
        pos = e
    if pos < len(text):
        out.append(("", text[pos:]))
    return out


def action_view(ap: dict[str, Any]) -> dict[str, Any]:
    """Toàn bộ thao tác (KHÔNG cắt): Bash → lệnh đầy đủ; tool khác (kể cả `mcp__*`) → mọi tham số dạng JSON."""
    inp = ap.get("tool_input")
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except json.JSONDecodeError:
            inp = {"input": inp}
    tool = ap.get("tool_name") or ""
    if tool == "Bash" and isinstance(inp, dict) and isinstance(inp.get("command"), str):
        text = inp["command"]
        extra = {k: v for k, v in inp.items() if k not in ("command", "description")}
        if extra:
            text += "\n\n# tham số khác: " + json.dumps(extra, ensure_ascii=False)
    elif isinstance(inp, dict) and inp:
        text = json.dumps(inp, ensure_ascii=False, indent=2, default=str)
    else:
        text = ap.get("preview") or ""
    lines = text.count("\n") + 1
    return {"tool": short_tool(tool) if tool.startswith("mcp__") else tool, "tool_full": tool, "text": text, "segments": highlight(text),
            "length": len(text), "lines": lines, "long": len(text) > LONG_ACTION or lines > 8,
            "hidden_chars": bool(_INVISIBLE.search(text))}


# ============================================================================ Điều phối viên (M7 — MGR-UI)
# Mọi chữ do điều phối viên viết (trả lời, tóm tắt hành động, đích) là DỮ LIỆU: template vẽ bằng autoescape, CSS pre-wrap —
# không markdown, không HTML thô, không link do agent tự đặt (link duy nhất là link nội bộ dựng từ loại + id đích).
MGR_TARGET_KINDS: dict[str, tuple[str, str]] = {
    # loại đích → (nhãn, tiền tố URL nội bộ)
    "agent": ("agent", "/agents/"),
    "task": ("task", "/tasks/"),
    "run": ("run", "/runs/"),
    "team": ("team", "/teams#team-"),
    "approval": ("yêu cầu duyệt", "/approvals#"),
    "learning": ("thay đổi học", "/learnings/"),
    "mcp": ("MCP", "/mcp/"),
}
_TOOL_KIND = (("agent_", "agent"), ("team_", "team"), ("task_", "task"), ("run_", "run"), ("learn_", "learning"))
_ID_SAFE = __import__("re").compile(r"^[A-Za-z0-9_.:\-]{1,128}$")


def mgr_target(target: Any, tool: str | None = None) -> dict[str, Any]:
    """Đích của `manager.action` → {kind, id, label, href|None}. Nhận "agent:x", {"kind": "agent", "id": "x"} hoặc id trần
    (đoán loại theo tên tool, vd `agent_create`). Chỉ dựng link NỘI BỘ cho loại đã biết + id an toàn — không bao giờ dùng URL do agent đưa."""
    from urllib.parse import quote

    kind: str | None = None
    ident: str | None = None
    if isinstance(target, dict):
        kind = str(target.get("kind") or target.get("type") or "") or None
        ident = str(target.get("id") or target.get("name") or "") or None
    elif target is not None:
        s = str(target)
        k, sep, rest = s.partition(":")
        if sep and k in MGR_TARGET_KINDS:
            kind, ident = k, rest
        else:
            ident = s
    if kind is None and tool:
        t = str(tool).rsplit("__", 1)[-1].removeprefix("hub_").removeprefix("manager_")
        kind = next((v for p, v in _TOOL_KIND if t.startswith(p)), None)
    label_kind, prefix = MGR_TARGET_KINDS.get(kind or "", (kind or "", ""))
    href = (prefix + quote(ident, safe="")) if prefix and ident and _ID_SAFE.match(ident) else None
    label = (f"{label_kind} {ident}" if label_kind else str(ident or "")).strip()
    return {"kind": kind, "id": ident, "label": label, "href": href}


def mgr_action_item(ev: dict[str, Any]) -> dict[str, Any]:
    """Event `manager.action` (dict wire) → mục thẻ hành động."""
    p = ev.get("payload") or {}
    tool = str(p.get("tool") or "")
    tgt = mgr_target(p.get("target"), tool)
    ok = p.get("ok", True) is not False and not p.get("error")
    return {"kind": "action", "tool": short_tool(tool) if tool else "", "summary": str(p.get("summary") or tool or "hành động"),
            "target": tgt, "run_id": ev.get("run_id"), "ts": ev.get("ts"), "audit_id": p.get("audit_id"), "ok": ok,
            "error": str(p.get("error") or "") or None}


def mgr_message(m: dict[str, Any]) -> dict[str, Any] | None:
    """Mục `manager.history` → {kind: user|manager, text, ts, run_id}. Chấp nhận vài tên khoá (role/author, text/content)."""
    role = str(m.get("role") or m.get("author") or m.get("kind") or "").lower()
    text = m.get("text") if m.get("text") is not None else m.get("content")
    if role in ("user", "human", "owner"):
        kind = "user"
    elif role in ("manager", "assistant", "agent", "notify", "reply", "tick"):
        kind = "manager"
    else:
        return None
    text = str(text or "")
    cut = kind == "manager" and (bool(m.get("truncated")) or len(text) >= MGR_TEXT_MAX)
    return {"kind": kind, "text": text, "ts": m.get("ts") or m.get("created_at"), "run_id": m.get("run_id"),
            "notify": role == "notify" or bool(m.get("notify")), "tick": role == "tick",
            "cut": cut_note("Tin", len(text), m.get("run_id")) if cut else ""}


MGR_TEXT_MAX = 20_000  # = manager.service.TEXT_MAX (trần chữ trả lời lưu DB) — chạm trần ⇒ đã bị cắt


def mgr_conv(c: dict[str, Any]) -> dict[str, Any]:
    cid = c.get("id") or c.get("conversation_id")
    n = c.get("messages")
    return {"id": cid, "channel": c.get("channel"), "updated_at": c.get("updated_at") or c.get("last_at") or c.get("created_at"),
            "created_at": c.get("created_at"), "count": n if isinstance(n, int) else (len(n) if isinstance(n, list) else c.get("message_count")),
            "title": first_line(str(c.get("title") or c.get("preview") or ""), 60)}
