"""Tài liệu PDF/Office đính kèm cho điều phối viên: nhận diện theo nội dung + trích bản đọc được (`manager/doc_extract.py`),
`decode_files` nhận tài liệu, ghi chú prompt. PDF mẫu sinh ngay trong test bằng pypdf (`make_pdf`). Không gọi claude."""

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
    assert dx.extract(".pdf", b"%PDF-1.7") == []  # PDF hỏng: chỉ lưu bản gốc


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


# ============================================================================ PDF (pypdf)
_IDENTITY_CMAP = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CMapName /Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
1 beginbfrange
<0000> <FFFF> <0000>
endbfrange
endcmap
CMapName currentdict /CMap defineresource pop
end
end"""


def _pdf_literal(text: str) -> str:
    return "(" + text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"


def make_pdf(pages: list[str | None], *, unicode: bool = False, password: str | None = None) -> bytes:
    """PDF tối thiểu, mỗi phần tử một trang (None = trang không có chữ, như bản scan). `unicode` = font Type0 Identity-H + ToUnicode
    (chữ tiếng Việt có dấu); `password` = mã hoá AES-128 với mật khẩu người dùng đó ("" = mở được không cần mật khẩu)."""
    from pypdf import PdfWriter
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject, TextStringObject

    n = NameObject
    w = PdfWriter()
    if unicode:
        cmap = DecodedStreamObject()
        cmap.set_data(_IDENTITY_CMAP)
        desc = DictionaryObject({n("/Type"): n("/Font"), n("/Subtype"): n("/CIDFontType2"), n("/BaseFont"): n("/Arial"),
                                 n("/CIDSystemInfo"): DictionaryObject({n("/Registry"): TextStringObject("Adobe"),
                                                                        n("/Ordering"): TextStringObject("Identity"),
                                                                        n("/Supplement"): NumberObject(0)})})
        font = DictionaryObject({n("/Type"): n("/Font"), n("/Subtype"): n("/Type0"), n("/BaseFont"): n("/Arial"),
                                 n("/Encoding"): n("/Identity-H"), n("/DescendantFonts"): ArrayObject([w._add_object(desc)]),
                                 n("/ToUnicode"): w._add_object(cmap)})
    else:
        font = DictionaryObject({n("/Type"): n("/Font"), n("/Subtype"): n("/Type1"), n("/BaseFont"): n("/Helvetica")})
    fref = w._add_object(font)
    for text in pages:
        page = w.add_blank_page(612, 792)
        if text is None:
            continue
        shown = ("<" + "".join(f"{ord(c):04X}" for c in text) + ">") if unicode else _pdf_literal(text)
        cs = DecodedStreamObject()
        cs.set_data(f"BT /F1 12 Tf 72 720 Td {shown} Tj ET".encode("latin-1"))
        page[n("/Contents")] = w._add_object(cs)
        page[n("/Resources")] = DictionaryObject({n("/Font"): DictionaryObject({n("/F1"): fref})})
    if password is not None:
        w.encrypt(user_password=password, owner_password="owner-pw", algorithm="AES-128")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _pdf_pages(data: bytes) -> list[str]:
    from pypdf import PdfReader

    return [(p.extract_text() or "").strip() for p in PdfReader(io.BytesIO(data)).pages]


def test_pdf_text_copy_has_page_headers():
    data = make_pdf(["Hello ALPHA-1", "Second (ALPHA-2)", None])
    assert dx.doc_ext("bao-cao.pdf", data) == ".pdf"
    out = dx.extract(".pdf", data)
    assert [s for s, _ in out] == [".txt"]  # 3 trang ≤ split_pages → chỉ bản chữ
    text = out[0][1].decode("utf-8")
    assert text.startswith("--- page 1 ---\nHello ALPHA-1")
    assert "--- page 2 ---\nSecond (ALPHA-2)" in text and "--- page 3 ---" in text  # trang trống vẫn có tiêu đề
    assert "cắt bớt" not in text


def test_pdf_vietnamese_text_round_trips():
    out = dict(dx.extract(".pdf", make_pdf(["Tiếng Việt có dấu: Đào móng, khối lượng 12,5 m³"], unicode=True)))
    assert "Tiếng Việt có dấu: Đào móng, khối lượng 12,5 m³" in out[".txt"].decode("utf-8")


def test_pdf_corrupt_truncated_and_garbage_never_raise():
    good = make_pdf([f"A{i}" for i in range(5)])
    for cut in (len(good) // 3, len(good) // 2, len(good) - 40):
        res = dx.extract(".pdf", good[:cut])  # cụt: pypdf có thể khôi phục một phần hoặc bỏ — miễn không ném lỗi
        assert isinstance(res, list) and all(dx.PDF_COPY_SUFFIX.match(s) for s, _ in res)
    assert dx.extract(".pdf", b"%PDF-1.7\n" + b"\x00\xff" * 500) == []
    assert dx.extract(".pdf", b"") == []
    assert dx.pdf_extract(b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF") == []


def test_pdf_encrypted_with_password_skipped_but_empty_password_opened():
    assert dx.extract(".pdf", make_pdf(["secret page"], password="pw-123")) == []
    out = dict(dx.extract(".pdf", make_pdf(["open page BETA-7"], password="")))
    assert "open page BETA-7" in out[".txt"].decode("utf-8")


def test_pdf_scanned_has_no_text_copy_but_still_split():
    assert dx.extract(".pdf", make_pdf([None, None])) == []
    out = dx.extract(".pdf", make_pdf([None] * 5), pdf={"split_pages": 2})
    assert [s for s, _ in out] == [".p001-002.pdf", ".p003-004.pdf", ".p005-005.pdf"]


def test_pdf_page_cap_adds_truncation_note():
    out = dict(dx.extract(".pdf", make_pdf([f"P{i}" for i in range(1, 6)]), pdf={"text_max_pages": 2}))
    text = out[".txt"].decode("utf-8")
    assert "--- page 2 ---" in text and "--- page 3 ---" not in text
    assert "chỉ trích 2/5 trang" in text


def test_pdf_output_cap_adds_truncation_note(monkeypatch):
    monkeypatch.setattr(dx, "OUT_MAX_BYTES", 60)
    text = dict(dx.extract(".pdf", make_pdf(["x" * 30, "y" * 30, "z" * 30])))[".txt"].decode("utf-8")
    assert "--- page 1 ---" in text and "--- page 3 ---" not in text and "cắt bớt: quá" in text


def test_pdf_time_budget_stops_between_pages(monkeypatch):
    data = make_pdf([f"T{i}" for i in range(10)])
    clock = iter(range(0, 100_000, 7))
    monkeypatch.setattr(dx.time, "monotonic", lambda: float(next(clock)))
    text = dict(dx.extract(".pdf", data, pdf={"time_budget_s": 10}))[".txt"].decode("utf-8")
    assert "hết thời gian trích" in text and "--- page 10 ---" not in text


def test_pdf_split_parts_keep_pages_and_respect_caps():
    from pypdf import PdfReader

    data = make_pdf([f"GAMMA-{i}" for i in range(1, 8)])
    out = dx.extract(".pdf", data, pdf={"split_pages": 3})
    assert [s for s, _ in out] == [".txt", ".p001-003.pdf", ".p004-006.pdf", ".p007-007.pdf"]
    parts = dict(out)
    assert _pdf_pages(parts[".p001-003.pdf"]) == ["GAMMA-1", "GAMMA-2", "GAMMA-3"]
    assert _pdf_pages(parts[".p007-007.pdf"]) == ["GAMMA-7"]
    assert all(dx.PDF_COPY_SUFFIX.match(s) for s, _ in out)
    # trần số phần / tổng dung lượng / tắt tách / đúng N trang
    assert [s for s, _ in dx.extract(".pdf", data, pdf={"split_pages": 2, "max_parts": 2})] == [".txt", ".p001-002.pdf", ".p003-004.pdf"]
    assert dx.pdf_split(PdfReader(io.BytesIO(data)), split_pages=3, max_bytes=10) == []
    assert [s for s, _ in dx.extract(".pdf", data, pdf={"split_pages": 0})] == [".txt"]
    assert [s for s, _ in dx.extract(".pdf", data, pdf={"split_pages": 7})] == [".txt"]


def test_pdf_part_names_widen_for_long_documents():
    out = dx.extract(".pdf", make_pdf([None] * 1001), pdf={"split_pages": 500})
    assert [s for s, _ in out] == [".p0001-0500.pdf", ".p0501-1000.pdf", ".p1001-1001.pdf"]


def test_pdf_options_defaults_and_bad_values():
    assert dx.pdf_options({}) == {"split_pages": 50, "max_parts": 20, "timeout_s": 90.0}
    o = dx.pdf_options({"attachments": {"pdf": {"split": False, "split_pages": 10, "max_parts": 3, "timeout_s": 30}}})
    assert o == {"split_pages": 0, "max_parts": 3, "timeout_s": 30.0}
    assert dx.pdf_options({"attachments": {"pdf": {"split_pages": "x", "max_parts": True, "timeout_s": 10_000}}}) == dx.pdf_options({})


def test_extract_isolated_runs_in_subprocess_and_matches_inline():
    data = make_pdf([f"DELTA-{i}" for i in range(1, 6)])
    out = dx.extract_isolated(data, {"split_pages": 2, "max_parts": 20, "timeout_s": 60})
    assert [s for s, _ in out] == [".txt", ".p001-002.pdf", ".p003-004.pdf", ".p005-005.pdf"]
    assert out == dx.extract(".pdf", data, pdf={"split_pages": 2})
    assert dx.extract_isolated(b"%PDF-1.7 garbage", {"timeout_s": 60}) == []


def test_extract_isolated_timeout_and_bad_output_return_empty(monkeypatch):
    import subprocess as sp

    def slow(*a, **k):
        raise sp.TimeoutExpired(cmd="x", timeout=k.get("timeout"))

    monkeypatch.setattr(dx.subprocess, "run", slow)
    assert dx.extract_isolated(make_pdf(["x"]), {"timeout_s": 5}) == []
    # tiến trình con trả hậu tố lạ (vd "/../x") → bỏ hết, không ghi gì
    monkeypatch.setattr(dx.subprocess, "run", lambda *a, **k: sp.CompletedProcess(a, 0, b'[["/../../evil.txt", "eA=="]]', b""))
    assert dx.extract_isolated(make_pdf(["x"]), {"timeout_s": 5}) == []
    monkeypatch.setattr(dx.subprocess, "run", lambda *a, **k: sp.CompletedProcess(a, 1, b"", b"Traceback boom"))
    assert dx.extract_isolated(make_pdf(["x"]), {"timeout_s": 5}) == []


def test_extract_isolated_real_timeout_kills_worker():
    # hạn giờ thật (không giả lập): tiến trình con không kịp khởi động + import pypdf trong 0.01 giây → bị giết, trả []
    assert dx.extract_isolated(make_pdf(["x"]), {"timeout_s": 0.01}) == []


def test_write_attachment_files_pdf_copies(tmp_path):
    from cfharness.manager.service import write_attachment_files

    pdf = make_pdf([f"EPS-{i}" for i in range(1, 5)])
    saved, extracted = write_attachment_files(tmp_path / "attachments", [("bao_cao.pdf", pdf), ("scan.pdf", make_pdf([None])),
                                                                         ("ghi.md", b"# a")],
                                              pdf_opts={"split_pages": 2, "max_parts": 20, "timeout_s": 60})
    assert len(saved) == 3 and saved[0].endswith("-bao_cao.pdf") and saved[2].endswith("-ghi.md")
    assert [x[len(saved[0]):] for x in extracted[saved[0]]] == [".txt", ".p001-002.pdf", ".p003-004.pdf"]
    assert extracted[saved[1]] == []  # PDF scan 1 trang: không bản chữ, không tách — vẫn có khoá (files_note báo)
    assert saved[2] not in extracted
    assert "--- page 4 ---\nEPS-4" in (tmp_path / extracted[saved[0]][0]).read_text(encoding="utf-8")
    assert _pdf_pages((tmp_path / extracted[saved[0]][2]).read_bytes()) == ["EPS-3", "EPS-4"]


def test_files_note_pdf_variants():
    files = ["attachments/a.pdf", "attachments/scan.pdf", "attachments/b.docx"]
    ex = {"attachments/a.pdf": ["attachments/a.pdf.txt", "attachments/a.pdf.p001-050.pdf", "attachments/a.pdf.p051-060.pdf"],
          "attachments/scan.pdf": [], "attachments/b.docx": ["attachments/b.docx.txt"]}
    note = prompts.files_note(files, ex)
    assert "attachments/a.pdf (text copy made by the hub" in note and "attachments/a.pdf.txt" in note
    assert "page-range parts" in note and "attachments/a.pdf.p051-060.pdf" in note
    assert "attachments/scan.pdf (no text copy" in note and "only `file_path`" in note
    assert "attachments/b.docx (readable copies made by the hub: attachments/b.docx.txt)" in note
    assert "never pass `pages`" not in note and "Excel row number" not in note  # có pdftoppm; không có sheet
    assert "never pass `pages`" in prompts.files_note(files, ex, pdf_render=False)
    assert "never pass `pages`" not in prompts.files_note(["attachments/b.docx"], {"attachments/b.docx": []}, pdf_render=False)
    assert "PDFs too" not in note


async def test_save_files_async_does_not_block_event_loop(tmp_path, monkeypatch):
    import asyncio
    import time as _time
    from types import SimpleNamespace

    from cfharness.manager.service import ManagerService

    def slow_extract(data, opts=None):
        _time.sleep(0.5)  # PDF lâu: phải chạy trong thread, loop vẫn chạy
        return [(".txt", b"--- page 1 ---\nOK")]

    monkeypatch.setattr(dx, "extract_isolated", slow_extract)
    fake = SimpleNamespace(_conv_cwd=lambda aid, cid: str(tmp_path), hub=SimpleNamespace(config={}))
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    t = asyncio.create_task(ticker())
    saved, extracted = await ManagerService._save_files_async(fake, "boss", "mc_1", [("a.pdf", make_pdf(["x"]))])
    t.cancel()
    assert ticks >= 10
    assert extracted[saved[0]] == [saved[0] + ".txt"] and (tmp_path / saved[0]).is_file()
