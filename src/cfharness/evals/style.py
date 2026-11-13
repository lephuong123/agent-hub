"""Chấm PHONG CÁCH câu trả lời theo `prompt.RESPONSE_STYLE` — luật tất định, hàm thuần (plan-chat-ui-claude.md P2 "golden eval").

Dùng ở hai chỗ:
- checker golden `{"kind": "response_style", ...}` (`evals/checkers.py`) — yêu cầu = prompt của golden task;
- chấm offline câu trả lời THẬT của điều phối viên/agent: RPC chỉ đọc `eval.style` → `cfharness eval style [run] [--since]`.

Luật (mỗi luật một mã; `skip` để bỏ luật):
- `internal_id`  : không lộ id nội bộ (task `t_…`, run `r_…`, hội thoại `mc_…`, luồng việc `ws_…`, câu hỏi `q_…`, duyệt `ap_…`, …)
                   — trừ id có sẵn trong yêu cầu, hoặc người hỏi chính id ("id", "mã run"…).
- `hub_header`   : không có dòng bắt đầu `[cfharness` (dòng hệ thống của hub).
- `no_action`    : không có `NO_ACTION` trong câu trả lời người đọc.
- `fence_lang`   : khối mã rào (```) phải có tên ngôn ngữ.
- `length`       : câu hỏi ngắn (≤ `short_request_chars`) → câu trả lời ≤ `short_budget` ký tự (không tính khối mã).
- `language`     : yêu cầu tiếng Việt (hoặc không có yêu cầu — mặc định chủ hub viết tiếng Việt) → phần chữ (bỏ mã, đường dẫn, URL)
                   phải "chủ yếu tiếng Việt": tỉ lệ từ có dấu tiếng Việt ≥ `min_vi_ratio`. Yêu cầu bằng ngôn ngữ khác → bỏ qua.
- `hr`           : không có đường kẻ ngang (`---`, `***`, `___`).

Điểm = số luật đạt / số luật áp dụng; đạt khi không vi phạm luật nào.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any

RULES = ("internal_id", "hub_header", "no_action", "fence_lang", "length", "language", "hr")

# Tiền tố id do hub sinh (`"<p>_" + uuid4().hex[:n]` / `secrets.token_hex(n)` — runs.py, tasks.py, manager/service.py, …):
# r_ (12 hex) run, t_ (10) task, c_ (10) bình luận task, mc_ (10) hội thoại điều phối, conv_ (8) hội thoại web stub,
# ws_ (8) luồng việc, q_ (10) câu hỏi, ap_ (10) duyệt, gs_/g_ golden, sg_ gợi ý, le_ ledger, pr_ proposal, rv_ review,
# sn_ snapshot, sc_ lịch, aa_ hành động duyệt sẵn, cn_/dg_/er_/ev_ nội bộ khác.
ID_PREFIXES = ("r", "t", "c", "mc", "conv", "ws", "q", "ap", "gs", "g", "sg", "le", "pr", "rv", "sn", "sc", "aa", "cn", "dg", "er", "ev")
_ID = re.compile(r"(?<![\w-])(?:" + "|".join(sorted(ID_PREFIXES, key=len, reverse=True)) + r")_[0-9a-f]{8,12}(?![\w-])")
_ASKS_ID = re.compile(r"(?i)\b(?:ids?|mã\s+(?:run|task|việc|hội\s+thoại))\b")
_HUB_HEADER = re.compile(r"(?m)^\s*\[(?:cfharness|agent-hub)\b")  # rename:keep
_NO_ACTION = re.compile(r"\bNO_ACTION\b")
_URL = re.compile(r"\bhttps?://\S+|\bwww\.\S+")
_PATHISH = re.compile(r"(?:[A-Za-z]:)?[\w.~-]*[/\\][\w./\\~-]+")
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?")
# chữ cái riêng của tiếng Việt (không kể dấu thanh — dấu thanh nhận qua NFD)
_VI_LETTERS = set("đĐăĂâÂêÊôÔơƠưƯ")
_VI_TONES = {"̀", "́", "̃", "̉", "̣"}  # huyền, sắc, ngã, hỏi, nặng


@dataclass
class StyleOptions:
    short_request_chars: int = 160  # yêu cầu ≤ ngần này ký tự = "câu hỏi ngắn"
    short_budget: int = 700  # câu trả lời cho câu hỏi ngắn: tối đa ngần này ký tự (không tính khối mã)
    min_vi_ratio: float = 0.2  # tỉ lệ từ có dấu tiếng Việt tối thiểu để coi là "chủ yếu tiếng Việt"
    min_words: int = 6  # ít từ hơn → không đủ để kết luận ngôn ngữ (bỏ luật language)
    default_lang: str = "vi"  # ngôn ngữ chủ hub khi không có yêu cầu (tin chủ động, nhịp)
    skip: tuple[str, ...] = ()

    @classmethod
    def from_spec(cls, spec: dict[str, Any] | None) -> "StyleOptions":
        s = spec or {}
        o = cls()
        for k in ("short_request_chars", "short_budget", "min_words"):
            if s.get(k) is not None:
                setattr(o, k, int(s[k]))
        if s.get("min_vi_ratio") is not None:
            o.min_vi_ratio = float(s["min_vi_ratio"])
        if s.get("default_lang"):
            o.default_lang = str(s["default_lang"])
        if s.get("skip"):
            o.skip = tuple(str(x) for x in s["skip"])
        return o


@dataclass
class Violation:
    rule: str
    detail: str
    samples: list[str] = field(default_factory=list)


@dataclass
class StyleReport:
    score: float
    passed: bool
    violations: list[Violation]
    checked: list[str]
    stats: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Kiểm tham số checker `response_style`. Sai → ValueError (checkers.py đổi thành HubError)."""
    s = dict(spec)
    for k in ("short_request_chars", "short_budget", "min_words"):
        if s.get(k) is not None and (not isinstance(s[k], int) or isinstance(s[k], bool) or s[k] < 0):
            raise ValueError(f"{k} phải là số nguyên ≥ 0")
    if s.get("min_vi_ratio") is not None:
        v = s["min_vi_ratio"]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= float(v) <= 1:
            raise ValueError("min_vi_ratio phải trong [0, 1]")
    skip = s.get("skip") or []
    if not isinstance(skip, list) or any(x not in RULES for x in skip):
        raise ValueError(f"skip phải là danh sách luật trong {', '.join(RULES)}")
    return s


# ============================================================================ markdown
@lru_cache(maxsize=1)
def _md():
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark", {"html": False})


def _tokens(text: str) -> list[Any]:
    return list(_md().parse(text or ""))


def _prose(text: str) -> str:
    """Chữ người đọc: bỏ khối mã, `mã` nội dòng, URL, đường dẫn."""
    parts: list[str] = []
    for t in _tokens(text):
        if t.type == "inline" and t.children:
            for c in t.children:
                if c.type in ("text", "text_special"):
                    parts.append(c.content)
                elif c.type in ("softbreak", "hardbreak"):
                    parts.append(" ")
            parts.append("\n")
    s = " ".join(parts)
    s = _URL.sub(" ", s)
    return _PATHISH.sub(" ", s)


def _without_code(text: str) -> str:
    lines, out, fence = (text or "").split("\n"), [], None
    for ln in lines:
        m = re.match(r"^\s{0,3}(`{3,}|~{3,})", ln)
        if fence is None and m:
            fence = m.group(1)[0] * len(m.group(1))
            continue
        if fence is not None:
            if ln.strip().startswith(fence) and not ln.strip().strip(fence[0]):
                fence = None
            continue
        out.append(ln)
    return "\n".join(out).strip()


# ============================================================================ ngôn ngữ
def _is_vi_word(w: str) -> bool:
    if any(ch in _VI_LETTERS for ch in w):
        return True
    return any(ch in _VI_TONES for ch in unicodedata.normalize("NFD", w))


def vi_ratio(text: str) -> tuple[float, int]:
    """(tỉ lệ từ có dấu tiếng Việt, số từ) trên phần chữ."""
    words = _WORD.findall(text or "")
    if not words:
        return 0.0, 0
    return sum(1 for w in words if _is_vi_word(w)) / len(words), len(words)


def detect_lang(text: str, *, min_words: int = 2, min_ratio: float = 0.15) -> str:
    """"vi" | "other" | "unknown" (quá ít chữ). Tiếng Việt gõ không dấu → "other" (luật language bỏ qua, không phạt nhầm)."""
    r, n = vi_ratio(_URL.sub(" ", _PATHISH.sub(" ", re.sub(r"`[^`]*`", " ", text or ""))))
    if n < min_words:
        return "unknown"
    return "vi" if r >= min_ratio else "other"


# ============================================================================ chấm
def check_style(reply: str, request: str = "", options: StyleOptions | None = None) -> StyleReport:
    """Chấm `reply` (markdown người đọc) theo RESPONSE_STYLE. `request` = tin người (rỗng: tin chủ động/nhịp)."""
    o = options or StyleOptions()
    reply = str(reply or "")
    request = str(request or "")
    v: list[Violation] = []
    checked: list[str] = []
    stats: dict[str, Any] = {"chars": len(reply)}
    toks = _tokens(reply)

    def on(rule: str) -> bool:
        if rule in o.skip:
            return False
        checked.append(rule)
        return True

    if on("internal_id"):
        allowed = set(_ID.findall(request))
        asks = bool(_ASKS_ID.search(request))
        ids = [m for m in dict.fromkeys(_ID.findall(reply)) if m not in allowed]
        if ids and not asks:
            v.append(Violation("internal_id", f"lộ {len(ids)} id nội bộ", ids[:5]))
    if on("hub_header"):
        hs = [m.group(0).strip() + "…" for m in _HUB_HEADER.finditer(reply)]
        if hs:
            v.append(Violation("hub_header", "có dòng hệ thống của hub", hs[:3]))
    if on("no_action"):
        if _NO_ACTION.search(reply):
            v.append(Violation("no_action", "NO_ACTION lọt vào câu trả lời"))
    if on("fence_lang"):
        fences = [t for t in toks if t.type == "fence"]
        bare = [t.content.strip().split("\n")[0][:60] for t in fences if not (t.info or "").strip()]
        stats["fences"] = len(fences)
        if bare:
            v.append(Violation("fence_lang", f"{len(bare)}/{len(fences)} khối mã không có tên ngôn ngữ", bare[:3]))
    req = request.strip()
    if req and len(req) <= o.short_request_chars and on("length"):
        n = len(_without_code(reply))
        stats["prose_chars"] = n
        if n > o.short_budget:
            v.append(Violation("length", f"câu hỏi ngắn ({len(req)} ký tự) nhưng trả lời {n} ký tự > {o.short_budget}"))
    want = detect_lang(req) if req else o.default_lang
    if want == "unknown":
        want = o.default_lang
    stats["request_lang"] = want
    if want == "vi" and "language" not in o.skip:
        r, n = vi_ratio(_prose(reply))
        stats["vi_ratio"] = round(r, 3)
        if n >= o.min_words:
            checked.append("language")
            if r < o.min_vi_ratio:
                v.append(Violation("language", f"yêu cầu tiếng Việt nhưng chỉ {r:.0%} từ có dấu tiếng Việt (< {o.min_vi_ratio:.0%})"))
    if on("hr"):
        if any(t.type == "hr" for t in toks):
            v.append(Violation("hr", "có đường kẻ ngang"))
    failed = {x.rule for x in v}
    score = 1.0 if not checked else (len(checked) - len(failed)) / len(checked)
    return StyleReport(round(score, 4), not v, v, checked, stats)
