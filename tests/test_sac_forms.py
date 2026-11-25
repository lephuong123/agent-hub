"""Rà tĩnh template SAC: mọi form gửi /op/<mã> phải dùng đúng tên tham số của model thao tác (admind `extra=forbid`)
và có đủ trường bắt buộc. Lệch tên chỉ lộ ra khi người bấm nút ("tham số không hợp lệ") — vd nút Khởi động lại hub từng gửi
`user` thay cho `users`."""

from __future__ import annotations

import re
from pathlib import Path

from cfharness.admind.ops import load_all

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "cfharness" / "superadmin" / "templates"


def _forms(text: str) -> list[tuple[str, set[str], int]]:
    out: list[tuple[str, set[str], int]] = []
    for m in re.finditer(r'op_form\(\s*"([\w.]+)"', text):
        seg = text[m.end(): text.find("}}", m.end())]  # tới hết biểu thức Jinja của lời gọi macro
        keys = re.findall(r'"([\w.:]+)"\s*:', seg)
        out.append((m.group(1), {k.split(":")[0].split(".")[0] for k in keys}, text.count("\n", 0, m.start()) + 1))
    for m in re.finditer(r'(?:form_open\(\s*"([\w.]+)"|action="/op/([\w.]+)")', text):
        end = text.find("</form>", m.end())
        body = text[m.end(): end if end > 0 else None]
        keys = re.findall(r'name="p\.([\w.:]+)"', body)
        out.append((m.group(1) or m.group(2), {k.split(":")[0].split(".")[0] for k in keys}, text.count("\n", 0, m.start()) + 1))
    return out


def test_template_forms_match_op_params():
    ops = load_all()
    problems, seen = [], 0
    for t in sorted(TEMPLATES.glob("*.html")):
        for name, keys, line in _forms(t.read_text(encoding="utf-8")):
            seen += 1
            o = ops.get(name)
            if o is None:
                problems.append(f"{t.name}:{line} {name}: thao tác không tồn tại")
                continue
            fields = o.params.model_fields
            extra = keys - set(fields)
            missing = {k for k, f in fields.items() if f.is_required()} - keys
            if extra or missing:
                problems.append(f"{t.name}:{line} {name}: thừa {sorted(extra)}, thiếu bắt buộc {sorted(missing)}")
    assert seen > 50  # bộ dò còn nhận ra form (template đổi cách viết → sửa _forms)
    assert not problems, "\n".join(problems)


def test_no_nested_forms():
    """HTML không cho lồng form: trình duyệt bỏ thẻ form bên trong → nút thao tác gửi theo form NGOÀI (vd GET lọc ngày, kèm
    csrf lên URL). `_m.html` là nơi định nghĩa macro — bỏ qua."""
    problems = []
    for t in sorted(TEMPLATES.glob("*.html")):
        if t.name == "_m.html":
            continue
        text, depth = t.read_text(encoding="utf-8"), 0
        for m in re.finditer(r"<form\b|</form>|op_form\(|form_open\(", text):
            line = text.count("\n", 0, m.start()) + 1
            if m.group(0) == "</form>":
                depth = max(0, depth - 1)
                continue
            if depth:
                problems.append(f"{t.name}:{line} {m.group(0)} nằm trong một form khác")
            if m.group(0) != "op_form(":  # op_form tự đóng form của nó
                depth += 1
    assert not problems, "\n".join(problems)
