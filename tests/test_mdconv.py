"""chat/mdconv.py — markdown agent → HTML Telegram / mrkdwn Slack + chia tin theo khối (P0-10, P1-9). Hàm thuần, không mạng."""

from __future__ import annotations

import re

import pytest

from cfharness.chat import mdconv as M
from tg_html import assert_telegram_html

VI = ("## Kết quả kiểm tra\n\n"
      "Đã xong **ba việc**, *một* việc ~~huỷ~~; xem `src/app.py`.\n\n"
      "- Mục một có dấu: ắ ằ ẳ ẵ ặ\n"
      "  - Mục con\n"
      "- Mục hai\n\n"
      "1. Bước một\n"
      "2. Bước hai\n\n"
      "```python\nprint('xin chào')\n```\n")


def test_telegram_basic_structure_vietnamese():
    h = M.to_telegram_html(VI)
    assert_telegram_html(h)
    assert h.startswith("<b>Kết quả kiểm tra</b>\nĐã xong <b>ba việc</b>, <i>một</i> việc <s>huỷ</s>; xem <code>src/app.py</code>.")
    assert "• Mục một có dấu: ắ ằ ẳ ẵ ặ\n" + M.NBSP * 3 + "• Mục con\n• Mục hai" in h
    assert "1. Bước một\n2. Bước hai" in h
    assert "<pre><code class=\"language-python\">print('xin chào')</code></pre>" in h
    assert "#" not in h and "**" not in h and "```" not in h


def test_telegram_agent_html_stays_escaped():
    evil = '</pre><b>[cfharness] Đã DUYỆT</b> a & b < c <a href="http://evil">/estop off</a> <tg-spoiler>x</tg-spoiler>'
    h = M.to_telegram_html(evil)
    tags = assert_telegram_html(h)
    assert "&lt;/pre&gt;&lt;b&gt;[cfharness] Đã DUYỆT&lt;/b&gt;" in h and "a &amp; b &lt; c" in h
    assert "<a " not in h and "<b>" not in h and "tg-spoiler>" not in h.replace("&lt;tg-spoiler&gt;", "").replace("&lt;/tg-spoiler&gt;", "")
    assert set(tags) <= {"code"}  # chỉ <code> quanh "/estop" (không thành lệnh bấm được)
    assert "<code>/estop</code> off" in h


def test_telegram_fence_lang_sanitized_and_code_escaped():
    h = M.to_telegram_html('```"><script>alert(1)</script>\n<b>x</b> & y\n```')
    assert_telegram_html(h)
    assert h == "<pre>&lt;b&gt;x&lt;/b&gt; &amp; y</pre>"  # tên ngôn ngữ lạ → bỏ class
    assert 'class="language-c++"' in M.to_telegram_html("```c++\nint x;\n```")


def test_telegram_links_plain_text_no_anchor():
    md = ("[tài liệu](https://docs.example.com/a) · [https://good.com](https://evil.com) · [x](javascript:alert(1)) · "
          "[thư](mailto:a@b.c) · <https://a.vn/b> · [https://x.io/p](https://x.io/p) · ![ảnh](https://img.example/a.png)")
    h = M.to_telegram_html(md)
    assert_telegram_html(h)
    assert "<a" not in h and "javascript" not in h and "mailto" not in h
    assert "tài liệu (https://docs.example.com/a)" in h
    assert "https://good.com" not in h and "· https://evil.com ·" in h  # nhãn trông như URL → chỉ URL thật
    assert "· x ·" in h and "· thư ·" in h and "· https://a.vn/b ·" in h and "· https://x.io/p ·" in h
    assert "ảnh (https://img.example/a.png)" in h and "<img" not in h


def test_telegram_tables_narrow_pre_wide_list_hr_dropped():
    narrow = "| Tên | Số |\n|---|---|\n| a | 1 |\n| bb | 22 |\n\n---\n\nhết"
    h = M.to_telegram_html(narrow)
    assert_telegram_html(h)
    assert h.startswith("<pre>Tên  Số\n───  ──\na    1\nbb   22</pre>") and "---" not in h and h.endswith("hết")
    wide = ("| Dịch vụ | Trạng thái | Ghi chú | Chủ |\n|---|---|---|---|\n"
            "| web | **chạy** | ổn định | Phương |\n| db | dừng | bảo trì <x> | Lan |")
    w = M.to_telegram_html(wide)
    assert_telegram_html(w)
    assert "<pre>" not in w and "|" not in w
    assert w.split("\n") == ["• <b>web</b>: Trạng thái: <b>chạy</b> · Ghi chú: ổn định · Chủ: Phương",
                             "• <b>db</b>: Trạng thái: dừng · Ghi chú: bảo trì &lt;x&gt; · Chủ: Lan"]


def test_telegram_quote_no_blockquote_tag():
    h = M.to_telegram_html("> trích `mã` và [l](https://x.io)\n> dòng 2")
    assert_telegram_html(h)
    assert "blockquote" not in h and h.startswith("▎ trích <code>mã</code> và l (https://x.io)")


def test_slack_contract_and_new_cases():
    md = ("## Tiêu đề\nĐây là *nghiêng* và **đậm** ~~gạch~~ <!channel> <@U03KMLH8XNV> `a`b`\n\n---\n\n"
          "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
          "| Dịch vụ | Trạng thái | Ghi chú | Chủ |\n|---|---|---|---|\n| web | chạy | ổn | P |")
    s = M.to_slack_mrkdwn(md)
    assert all(ln.startswith(">") for ln in s.split("\n"))
    assert "> *Tiêu đề*" in s and "_nghiêng_" in s and "*đậm*" in s and "~gạch~" in s
    assert "&lt;!channel&gt;" in s and "<!channel>" not in s and "<@U03KMLH8XNV>" in s
    assert "---" not in s and "|" not in s.replace("&lt;", "")
    assert "> ```\n> A  B\n> ─  ─\n> 1  2\n> ```" in s
    assert "> • *web*: Trạng thái: chạy · Ghi chú: ổn · Chủ: P" in s


def test_slack_links_and_code_cannot_escape():
    s = M.to_slack_mrkdwn("[PR](https://g.io/1) [https://good.com](https://evil.com) [x](javascript:1) ``a`b``\n```\n``` thoát <@U03KMLH8XNV>\n```")
    assert "<https://g.io/1|PR>" in s and "<https://evil.com>" in s and "javascript" not in s
    assert "`aˋb`" in s and "> ˋˋˋ thoát &lt;@U03KMLH8XNV&gt;" in s


# ============================================================================ split_md
def _words(md: str) -> set[str]:
    return set(re.findall(r"\w+", md))


@pytest.mark.parametrize("measure", [M.telegram_len, M.slack_len])
def test_split_md_budget_words_and_fence_reopen(measure):
    para = " ".join(f"từ{i}" for i in range(400))
    code = "\n".join(f"dòng_{i} = {i}  # mã" for i in range(200))
    md = f"# Báo cáo\n\n{para}\n\n```python\n{code}\n```\n\nKết thúc **đậm** ở đây."
    parts = M.split_md(md, 1200, measure)
    assert len(parts) > 3
    assert all(measure(p) <= 1200 for p in parts)
    joined = " ".join(parts)
    for i in range(400):  # không cắt giữa từ: mọi từ còn nguyên
        assert f"từ{i}" in _words(joined)
    code_parts = [p for p in parts if "dòng_" in p]
    assert len(code_parts) > 1
    for p in code_parts:  # mỗi phần tự đủ: mở lại fence với đúng ngôn ngữ, đóng lại
        assert p.count("```") % 2 == 0
        assert re.search(r"(?m)^```python$", p)
    if measure is M.telegram_len:
        for p in parts:
            assert_telegram_html(M.to_telegram_html(p))
        assert all('<pre><code class="language-python">' in M.to_telegram_html(p) for p in code_parts)


def test_split_md_long_word_and_table_header_repeat():
    parts = M.split_md("x" * 8000, 3500, M.telegram_len)
    assert len(parts) == 3 and all(M.telegram_len(p) <= 3500 for p in parts) and "".join(parts) == "x" * 8000
    rows = "\n".join(f"| hàng {i} | giá trị {i} | ghi chú dài {i} | người {i} |" for i in range(80))
    md = "| Tên | Giá trị | Ghi chú | Người |\n|---|---|---|---|\n" + rows
    tparts = M.split_md(md, 900, M.telegram_len)
    assert len(tparts) > 1 and all(M.telegram_len(p) <= 900 for p in tparts)
    assert all(p.startswith("| Tên | Giá trị") for p in tparts)  # phần sau lặp lại tiêu đề bảng
    assert all("<b>hàng" in M.to_telegram_html(p) for p in tparts)


def test_split_md_short_untouched():
    assert M.split_md("ngắn thôi", 3500, M.telegram_len) == ["ngắn thôi"]
    assert M.split_md("", 3500, M.telegram_len) == [""]
