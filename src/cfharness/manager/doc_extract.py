"""Tệp tài liệu (PDF/Office) người gửi kèm tin cho điều phối viên: nhận diện theo NỘI DUNG + trích chữ bằng stdlib.

- `doc_ext(name, data)`: PDF (`%PDF-`), Office OOXML/ODF (zip hợp lệ + đuôi .xlsx/.xlsm/.docx/.pptx/.ods/.odt/.odp),
  Office cũ (OLE2 + đuôi .xls/.doc/.ppt) → đuôi chuẩn; còn lại → None (không tin đuôi một mình, không tin MIME).
- `extract(ext, data)`: bản đọc được cho MỌI khung quyền (strict chỉ có Read): xlsx → mỗi sheet một CSV (cột đầu `row` = số dòng
  Excel, tiêu đề = chữ cái cột A, B, C… để "cột E dòng 7" khớp với file gốc); docx → chữ theo đoạn; pptx → chữ theo slide.
  PDF đọc thẳng bằng Read; .xls/.doc/.ppt/ODF không trích (chỉ lưu bản gốc).
- Chống zip bomb: chỉ đọc các phần cần, mỗi phần ≤ `PART_MAX_BYTES` (theo kích thước giải nén khai báo VÀ đếm thật), đầu ra
  ≤ `OUT_MAX_BYTES`/tệp, ≤ `SHEET_MAX_ROWS` dòng/sheet; lỗi → bỏ trích (bản gốc vẫn được lưu). Không bao giờ ném lỗi ra ngoài.
"""

from __future__ import annotations

import csv
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Iterator

PART_MAX_BYTES = 64 * 1024 * 1024
OUT_MAX_BYTES = 8 * 1024 * 1024
SHEET_MAX_ROWS = 50_000
SHEET_MAX_COLS = 702  # A..ZZ

ZIP_DOCS = {".xlsx", ".xlsm", ".docx", ".pptx", ".ods", ".odt", ".odp"}
OLE_DOCS = {".xls", ".doc", ".ppt"}
DOC_SUFFIXES = ZIP_DOCS | OLE_DOCS | {".pdf"}
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_NS_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_NS_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_NS_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_CELL_REF = re.compile(r"^([A-Z]{1,3})(\d+)$")


def _suffix(name: str) -> str:
    n = str(name or "").lower()
    i = n.rfind(".")
    return n[i:] if i >= 0 else ""


def doc_ext(name: str, data: bytes) -> str | None:
    """Đuôi chuẩn của tài liệu được hỗ trợ (theo nội dung, đuôi chỉ để phân biệt các loại cùng khung zip/OLE), hoặc None."""
    if data.startswith(b"%PDF-"):
        return ".pdf"
    suf = _suffix(name)
    if data.startswith(b"PK\x03\x04") and suf in ZIP_DOCS:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
        except (zipfile.BadZipFile, ValueError, OSError):
            return None
        if "[Content_Types].xml" in names or "mimetype" in names:
            return suf
        return None
    if data.startswith(_OLE_MAGIC) and suf in OLE_DOCS:
        return suf
    return None


def _read(z: zipfile.ZipFile, part: str) -> bytes | None:
    try:
        info = z.getinfo(part)
    except KeyError:
        return None
    if info.file_size > PART_MAX_BYTES:
        raise ValueError(f"{part}: phần giải nén quá lớn")
    with z.open(info) as f:
        data = f.read(PART_MAX_BYTES + 1)
    if len(data) > PART_MAX_BYTES:
        raise ValueError(f"{part}: phần giải nén quá lớn")
    return data


def _col_index(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _col_name(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _text_of(el: ET.Element, tag: str) -> str:
    return "".join(t.text or "" for t in el.iter(tag))


def _xlsx_sheets(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    """[(tên sheet, đường dẫn phần trong zip)] theo thứ tự workbook."""
    wb = _read(z, "xl/workbook.xml")
    if wb is None:
        return []
    rels: dict[str, str] = {}
    raw = _read(z, "xl/_rels/workbook.xml.rels")
    if raw is not None:
        for r in ET.fromstring(raw).iter(_NS_PKG_REL + "Relationship"):
            t = r.get("Target") or ""
            rels[r.get("Id") or ""] = t.lstrip("/") if t.startswith("/") else posixpath.normpath(posixpath.join("xl", t))
    out = []
    for s in ET.fromstring(wb).iter(_NS_MAIN + "sheet"):
        part = rels.get(s.get(_NS_REL + "id") or "")
        if part:
            out.append((s.get("name") or part, part))
    return out


def _xlsx_rows(raw: bytes, shared: list[str]) -> Iterator[tuple[int, dict[int, str]]]:
    for _, el in ET.iterparse(io.BytesIO(raw)):
        if el.tag != _NS_MAIN + "row":
            continue
        cells: dict[int, str] = {}
        rnum = int(el.get("r") or 0)
        for c in el.iter(_NS_MAIN + "c"):
            m = _CELL_REF.match(c.get("r") or "")
            if not m:
                continue
            col = _col_index(m.group(1))
            rnum = rnum or int(m.group(2))
            t = c.get("t")
            if t == "inlineStr":
                val = _text_of(c, _NS_MAIN + "t")
            else:
                v = c.find(_NS_MAIN + "v")
                val = v.text or "" if v is not None else ""
                if t == "s" and val.isdigit() and int(val) < len(shared):
                    val = shared[int(val)]
                elif t == "b":
                    val = "TRUE" if val == "1" else "FALSE"
            if val != "" and col < SHEET_MAX_COLS:
                cells[col] = val
        el.clear()
        if cells:
            yield rnum, cells


def xlsx_to_csv(data: bytes) -> list[tuple[str, str]]:
    """xlsx → [(tên sheet, nội dung CSV)]. Giá trị = giá trị đã lưu (công thức → kết quả lần tính gần nhất; ngày = số serial Excel)."""
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared: list[str] = []
        raw = _read(z, "xl/sharedStrings.xml")
        if raw is not None:
            shared = [_text_of(si, _NS_MAIN + "t") for si in ET.fromstring(raw).iter(_NS_MAIN + "si")]
        for name, part in _xlsx_sheets(z):
            raw = _read(z, part)
            if raw is None:
                continue
            rows = []
            width = 0
            for rnum, cells in _xlsx_rows(raw, shared):
                rows.append((rnum, cells))
                width = max(width, max(cells) + 1)
                if len(rows) >= SHEET_MAX_ROWS:
                    break
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            w.writerow(["row"] + [_col_name(i) for i in range(width)])
            for rnum, cells in rows:
                w.writerow([rnum] + [cells.get(i, "") for i in range(width)])
                if buf.tell() > OUT_MAX_BYTES:
                    w.writerow(["…", f"(cắt bớt: quá {OUT_MAX_BYTES // 1024 // 1024} MB)"])
                    break
            out.append((name, buf.getvalue()))
    return out


def docx_to_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        raw = _read(z, "word/document.xml")
    if raw is None:
        return ""
    paras = []
    for p in ET.fromstring(raw).iter(_NS_W + "p"):
        paras.append("".join((t.text or "") if t.tag == _NS_W + "t" else "\t" for t in p.iter() if t.tag in (_NS_W + "t", _NS_W + "tab")))
    return "\n".join(paras)[:OUT_MAX_BYTES]


def pptx_to_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.sub(r"\D", "", n)))
        parts = []
        for i, n in enumerate(slides, 1):
            raw = _read(z, n)
            if raw is None:
                continue
            lines = ["".join(t.text or "" for t in p.iter(_NS_A + "t")) for p in ET.fromstring(raw).iter(_NS_A + "p")]
            parts.append(f"--- Slide {i} ---\n" + "\n".join(x for x in lines if x))
    return "\n\n".join(parts)[:OUT_MAX_BYTES]


def extract(ext: str, data: bytes) -> list[tuple[str, bytes]]:
    """Bản đọc được: [(hậu tố tên tệp, bytes UTF-8)]; rỗng khi loại này không trích hoặc trích lỗi."""
    try:
        if ext in (".xlsx", ".xlsm"):
            from ..manager.service import safe_file_name

            return [(f".{i}-{safe_file_name(n, 'sheet')[:40]}.csv", body.encode("utf-8")) for i, (n, body) in enumerate(xlsx_to_csv(data), 1)]
        if ext == ".docx":
            t = docx_to_text(data)
            return [(".txt", t.encode("utf-8"))] if t.strip() else []
        if ext == ".pptx":
            t = pptx_to_text(data)
            return [(".txt", t.encode("utf-8"))] if t.strip() else []
    except (zipfile.BadZipFile, ET.ParseError, ValueError, KeyError, OSError, RuntimeError, EOFError):
        return []
    return []
