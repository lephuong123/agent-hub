"""task_report.py — khung báo cáo task "Đã làm / Còn mở / Cần bạn": tách phần, chọn tóm tắt, gộp task gốc, chữ gửi người."""

from __future__ import annotations

from cfharness import task_report as r

REPORT = """Đã sửa lỗi đăng nhập.

**Đã làm**
- Sửa `auth.py`
- Thêm test

```bash
pytest
```

**Còn mở:**
- Chưa test trên Windows

## Cần bạn
- Duyệt PR #12
"""


def test_parse_sections_vi_and_en():
    p = r.parse(REPORT)
    assert p["structured"] and p["outcome"] == "Đã sửa lỗi đăng nhập."
    assert "`auth.py`" in p["done"] and "```bash" in p["done"]
    assert p["open"] == "- Chưa test trên Windows" and p["need"] == "- Duyệt PR #12"
    en = r.parse("Fixed it.\n### Done\n- a\nStill open:\n- None\n**Needs you**\n- Nothing")
    assert en["done"] == "- a" and r.is_empty(en["open"]) and r.is_empty(en["need"])


def test_plain_text_is_not_structured():
    for s in ("done", "Xong rồi.\nĐã sửa file a.py", ""):
        assert not r.parse(s)["structured"]
    assert r.parse("Xong rồi.\nĐã sửa file a.py")["done"] == "Đã sửa file a.py"


def test_pick_summary_prefers_fuller_report():
    assert r.pick_summary("explicit summary", "done") == "explicit summary"
    assert r.pick_summary("short", REPORT) == REPORT.strip()
    assert r.pick_summary(REPORT, "a much longer plain text final message without any headings at all, really long") == REPORT.strip()
    assert r.pick_summary(None, "x") == "x" and r.pick_summary("y", None) == "y" and r.pick_summary(None, None) == ""


def test_compose_root_gathers_open_and_need_without_ids():
    out = r.compose_root([{"id": "t_1", "title": "A", "assignee_agent_id": "w1", "result_summary": REPORT},
                          {"id": "t_2", "title": "B", "result_summary": "xong phần 0"}])
    assert "t_1" not in out and "t_2" not in out
    p = r.parse(out)
    assert p["outcome"] == "Đã xong cả 2 phần việc."
    assert "**A** — w1: Đã sửa lỗi đăng nhập." in p["done"] and "**B**: xong phần 0" in p["done"]
    assert p["open"] == "- A: Chưa test trên Windows" and p["need"] == "- A: Duyệt PR #12"
    empty = r.parse(r.compose_root([{"title": "B", "result_summary": "xong"}]))
    assert r.is_empty(empty["open"]) and r.is_empty(empty["need"])


def test_owner_report_and_needs_owner():
    t = {"title": "Sửa login", "result_summary": REPORT}
    txt = r.owner_report(t)
    assert txt.startswith("✅ Xong việc **Sửa login**") and "Duyệt PR #12" in txt and r.needs_owner(t)
    plain = r.owner_report({"title": "X", "result_summary": "xong"})
    assert "không ghi rõ phần còn mở" in plain  # không bịa "không cần gì"
    assert "không để lại báo cáo" in r.owner_report({"title": "X"})
    assert not r.needs_owner({"result_summary": "Xong.\n**Cần bạn**\n- Không cần gì"})


def test_digest_puts_open_and_need_first():
    d = r.digest(REPORT, 200)
    assert d.startswith("Đã sửa lỗi đăng nhập. | Still open: - Chưa test trên Windows | Needs owner: - Duyệt PR #12")
    assert len(r.digest("x" * 2000, 100)) == 100
