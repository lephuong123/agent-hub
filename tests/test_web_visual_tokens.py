"""Token màu web đạt WCAG AA (light + dark) cho các cặp chữ/nền chính; không còn `#fff` cứng trên nền danger/ask."""
import re
from pathlib import Path

CSS = (Path(__file__).parents[1] / "src" / "cfharness" / "web" / "static" / "app.css").read_text(encoding="utf-8")
DARK_HEAD = "@media (prefers-color-scheme: dark)"


def _tokens(src: str) -> dict[str, str]:
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})\b", src))


def _lum(h: str) -> float:
    ch = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    ch = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in ch]
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


LIGHT = _tokens(CSS.split(DARK_HEAD)[0])
DARK = {**LIGHT, **_tokens(CSS.split(DARK_HEAD)[1].split("\n}\n")[0])}
TEXT_PAIRS = [("on-accent", "accent"), ("on-accent", "accent-hover"), ("on-danger", "danger"), ("on-ask", "ask"),
              ("muted", "bg"), ("muted", "surface"), ("muted", "surface-3"), ("muted", "side-bg"),
              ("ok", "ok-soft"), ("warn", "warn-soft"), ("danger", "danger-soft"), ("accent-text", "accent-soft"),
              ("accent-text", "side-bg")]


def test_text_token_pairs_meet_wcag_aa():
    bad = [(mode, fg, bg, round(_ratio(t[fg], t[bg]), 2)) for mode, t in (("light", LIGHT), ("dark", DARK))
           for fg, bg in TEXT_PAIRS if _ratio(t[fg], t[bg]) < 4.5]
    assert not bad


def test_focus_and_faint_meet_non_text_contrast():
    for t in (LIGHT, DARK):
        assert _ratio(t["accent"], t["bg"]) >= 3 and _ratio(t["faint"], t["bg"]) >= 3


def test_no_hardcoded_white_on_danger_or_ask():
    assert not re.search(r"background:\s*var\(--(danger|ask)\);\s*color:\s*#fff", CSS)
