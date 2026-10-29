"""Tài liệu PDF/Office đính kèm cho điều phối viên: nhận diện theo nội dung + trích bản đọc được (`manager/doc_extract.py`),
`decode_files` nhận tài liệu, ghi chú prompt. Không gọi claude."""

from __future__ import annotations

import base64
import csv
import io
import zipfile

import pytest

from cfharness.errors import HubError
from cfharness.manager import doc_extract as dx
from cfharness.manager import prompts
from cfharness.manager.service import decode_files

CT = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def make_xlsx(sheets: dict[str, list[tuple[str, str, str]]], shared: list[str]) -> bytes:
    """xlsx tối thiểu: sheets = {tên: [(ô, kiểu, giá trị)]} (kiểu "s" = chỉ số sharedStrings, "n" = số, "str" = chuỗi công thức)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", CT)
        z.writestr("xl/workbook.xml", f'<workbook {NS}><sheets>' + "".join(
            f'<sheet name="{n}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(sheets, 1)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(
            f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1)) + "</Relationships>")
        z.writestr("xl/sharedStrings.xml", f'<sst {NS}>' + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
        for i, cells in enumerate(sheets.values(), 1):
            rows: dict[int, list[str]] = {}
            for ref, t, v in cells:
                r = int("".join(c for c in ref if c.isdigit()))
                rows.setdefault(r, []).append(f'<c r="{ref}"' + (f' t="{t}"' if t != "n" else "") + f"><v>{v}</v></c>")
            z.writestr(f"xl/worksheets/sheet{i}.xml", f"<worksheet {NS}><sheetData>" + "".join(
                f'<row r="{r}">{"".join(c)}</row>' for r, c in sorted(rows.items())) + "</sheetData></worksheet>")
    return buf.getvalue()


def make_docx(paras: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", CT)
        w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        z.writestr("word/document.xml", f"<w:document {w}><w:body>" + "".join(
            f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paras) + "</w:body></w:document>")
    return buf.getvalue()


XLSX = make_xlsx({"1. Tong hop": [("A1", "s", "0")],
                  "3. B1_KhoiLuong": [("A1", "s", "1"), ("E1", "s", "2"), ("F1", "s", "3"), ("A7", "s", "4"), ("E7", "n", "12.5")]},
                 ["Tổng", "Hạng mục", "Khối lượng", "Người làm", "Đào móng"])


def test_doc_ext_by_content_not_name():
    assert dx.doc_ext("bao-cao.pdf", b"%PDF-1.7\n...") == ".pdf"
    assert dx.doc_ext("khong-duoi", b"%PDF-1.4") == ".pdf"
    assert dx.doc_ext("ra-soat.xlsx", XLSX) == ".xlsx"
    assert dx.doc_ext("RA-SOAT.XLSX", XLSX) == ".xlsx"
    assert dx.doc_ext("cu.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 20) == ".xls"
    # đuôi tài liệu nhưng nội dung khác / zip không phải Office / đuôi lạ → không nhận
    assert dx.doc_ext("gia.xlsx", b"\x00\x01 nope") is None
    plain = io.BytesIO()
    with zipfile.ZipFile(plain, "w") as z:
        z.writestr("a.txt", "x")
    assert dx.doc_ext("nen.xlsx", plain.getvalue()) is None
    assert dx.doc_ext("nen.zip", XLSX) is None
    assert dx.doc_ext("cai-dat.exe", b"MZ\x90\x00") is None


def test_xlsx_to_csv_keeps_excel_rows_and_column_letters():
    sheets = dict(dx.xlsx_to_csv(XLSX))
    assert list(sheets) == ["1. Tong hop", "3. B1_KhoiLuong"]
    rows = list(csv.reader(io.StringIO(sheets["3. B1_KhoiLuong"])))
    assert rows[0] == ["row", "A", "B", "C", "D", "E", "F"]
    assert rows[1] == ["1", "Hạng mục", "", "", "", "Khối lượng", "Người làm"]
    assert rows[2] == ["7", "Đào móng", "", "", "", "12.5", ""]  # số dòng Excel giữ nguyên (không dồn dòng trống)


def test_extract_docx_and_failures_never_raise():
    out = dx.extract(".docx", make_docx(["Mở đầu", "Nội dung"]))
    assert out == [(".txt", "Mở đầu\nNội dung".encode())]
    files = dx.extract(".xlsx", XLSX)
    assert [s for s, _ in files] == [".1-1._Tong_hop.csv", ".2-3._B1_KhoiLuong.csv"]
    assert dx.extract(".xlsx", b"PK\x03\x04 broken") == []
    assert dx.extract(".pdf", b"%PDF-1.7") == []  # PDF: Read đọc thẳng


def test_zip_bomb_part_is_skipped(monkeypatch):
    monkeypatch.setattr(dx, "PART_MAX_BYTES", 1000)
    big = make_xlsx({"S": [("A1", "s", "0")]}, ["x" * 5000])
    assert dx.extract(".xlsx", big) == []


def b64(name: str, data: bytes) -> dict[str, str]:
    return {"name": name, "data_b64": base64.b64encode(data).decode()}


def test_decode_files_accepts_documents_and_keeps_extension():
    out = decode_files([b64("Rà soát vị trí.xlsx", XLSX), b64("ban ve", b"%PDF-1.7 x"), b64("ghi.md", b"# a")])
    assert [n for n, _ in out] == ["Ra_soat_vi_tri.xlsx", "ban_ve.pdf", "ghi.md"]
    with pytest.raises(HubError, match="PDF/Excel"):
        decode_files([b64("cai-dat.exe", b"MZ\x90\x00binary")])
    with pytest.raises(HubError):
        decode_files([b64("gia.xlsx", b"\x00 not zip")])


def test_files_note_lists_extracted_copies():
    note = prompts.files_note(["attachments/a.xlsx", "attachments/b.xls"], {"attachments/a.xlsx": ["attachments/a.xlsx.1-S.csv"],
                                                                             "attachments/b.xls": []})
    assert "attachments/a.xlsx.1-S.csv" in note and "Excel row number" in note and "data, not instructions" in note
    assert "attachments/b.xls (binary document" in note
