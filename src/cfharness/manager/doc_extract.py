"""Tệp tài liệu (PDF/Office) người gửi kèm tin cho điều phối viên: nhận diện theo NỘI DUNG + trích bản đọc được.

- `doc_ext(name, data)`: PDF (`%PDF-`), Office OOXML/ODF (zip hợp lệ + đuôi .xlsx/.xlsm/.docx/.pptx/.ods/.odt/.odp),
  Office cũ (OLE2 + đuôi .xls/.doc/.ppt) → đuôi chuẩn; còn lại → None (không tin đuôi một mình, không tin MIME).
- `extract(ext, data)`: bản đọc được cho MỌI khung quyền (strict chỉ có Read): xlsx → mỗi sheet một CSV (cột đầu `row` = số dòng
  Excel, tiêu đề = chữ cái cột A, B, C… để "cột E dòng 7" khớp với file gốc); docx → chữ theo đoạn; pptx → chữ theo slide (Office: stdlib).
  PDF (thư viện `pypdf`, thuần Python): một `.txt` mà DÒNG ĐẦU nêu phạm vi trang (`pages 1-13 of 400`, cắt vì đâu), mỗi trang có tiêu
  đề `--- page N ---` (≤ `PDF_TEXT_MAX_PAGES` trang, ≤ `OUT_MAX_BYTES`); PDF nhiều hơn `split_pages` trang → thêm các phần
  `.p001-050.pdf`… (≤ `max_parts` phần — tự nới số trang/phần tới `PDF_PART_MAX_PAGES` để phủ hết tài liệu — tổng ≤ `PDF_PARTS_MAX_BYTES`)
  để Read đọc từng phần chỉ với `file_path` (giữ bảng/hình). PDF mã hoá (thử mật khẩu rỗng, không được → bỏ) hoặc PDF scan (không có
  chữ) → không có `.txt`. Daemon gọi PDF qua `extract_isolated` (tiến trình con `pdf_worker`, có hạn giờ, ghi từng trang/phần ngay
  vào thư mục tạm riêng — phần tách là tệp, daemon chỉ chuyển chỗ, không nạp vào bộ nhớ; bị giết vẫn giữ phần đã xong) và nhận kèm
  `info` (tổng trang, phạm vi đã phủ) để `files_note` nói rõ cho agent. Dòng trong PDF giả tiêu đề của hub → thêm tiền tố `(in PDF) `.
  .xls/.doc/.ppt/ODF không trích (chỉ lưu bản gốc).
- Chống zip bomb: chỉ đọc các phần cần, mỗi phần ≤ `PART_MAX_BYTES` (theo kích thước giải nén khai báo VÀ đếm thật), đầu ra
  ≤ `OUT_MAX_BYTES`/tệp, ≤ `SHEET_MAX_ROWS` dòng/sheet; lỗi → bỏ trích (bản gốc vẫn được lưu). Không bao giờ ném lỗi ra ngoài.
  PDF: giới hạn giải nén có sẵn của pypdf + ngân sách thời gian giữa các trang (`PDF_TIME_BUDGET_S`) + hạn giờ cứng của tiến trình con.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any, Iterable, Iterator

PART_MAX_BYTES = 64 * 1024 * 1024
OUT_MAX_BYTES = 8 * 1024 * 1024
SHEET_MAX_ROWS = 50_000
SHEET_MAX_COLS = 702  # A..ZZ
PDF_TEXT_MAX_PAGES = 500  # trang tối đa đưa vào bản chữ
PDF_TIME_BUDGET_S = 30.0  # ngân sách mềm MỘT tệp PDF (kiểm giữa các trang; tách phần ≤ 40%, chữ phần còn lại)
PDF_SPLIT_PAGES = 50  # mặc định `attachments.pdf.split_pages`: PDF nhiều hơn N trang → thêm các phần N trang
PDF_PART_MAX_PAGES = 100  # trần trang mỗi phần (kể cả khi nới để phủ hết tài liệu); Read của agent đọc cả phần một lần
PDF_SPLIT_MAX_PARTS = 20  # mặc định `attachments.pdf.max_parts`
# tổng dung lượng các phần của MỘT TIN (chia đều cho các PDF của tin — `write_attachment_files`): PDF mà mọi trang dùng chung một tài
# nguyên lớn (font/ảnh) → mỗi phần chép lại tài nguyên đó, 9.5 MB vào có thể thành 48 MB phần
PDF_PARTS_MAX_BYTES = 48 * 1024 * 1024
PDF_READ_MAX_PAGES = 600  # API Claude từ chối PDF > 600 trang khi Read cả tệp chỉ với `file_path`
# mặc định `attachments.pdf.timeout_s`: hạn giờ cứng CHUNG cho mọi PDF của MỘT tin (các PDF trích song song) — phải dưới hạn 60 s
# của RPC web → `manager.chat`, nếu không người thấy lỗi gửi trong khi hub vẫn nhận tin (gửi lại = lượt trùng)
PDF_MESSAGE_TIMEOUT_S = 40.0
PDF_LIMITS = {"split_pages": (1, PDF_PART_MAX_PAGES), "max_parts": (1, 200), "timeout_s": (5, 50)}  # = kiểm ở config.validate_setting
PDF_COPY_SUFFIX = re.compile(r"^\.(txt|p\d{3,8}-\d{3,8}\.pdf)$")  # hậu tố hợp lệ trả về từ tiến trình con
_PART_SUFFIX = re.compile(r"^\.p(\d{3,8})-(\d{3,8})\.pdf$")
PAGE_ERROR_TEXT = "(the text of this page could not be extracted)"
# tiến trình con ghi vào thư mục tạm riêng do daemon tạo: sự kiện (JSON mỗi dòng) + mỗi phần một tệp — stdout không mang dữ liệu
WORKER_EVENTS = "events.jsonl"
WORKER_STDERR = "stderr.txt"
_WORKER_PART_FILE = re.compile(r"^part-\d{1,4}\.pdf$")
EVENTS_MAX_BYTES = 4 * OUT_MAX_BYTES  # đọc tối đa ngần này byte sự kiện (chữ ≤ OUT_MAX_BYTES + phần đầu/cuối)
EVENT_LINE_MAX_BYTES = 2 * OUT_MAX_BYTES + 65536  # một dòng sự kiện dài hơn → bỏ dòng đó
# dòng chữ TRONG PDF giống hệt tiêu đề của hub (`--- page 7 ---`, `[PDF text copy made by cfharness…`) → thêm tiền tố để agent không
# nhầm ranh giới trang / phạm vi bản chữ
_FAKE_HEADER = re.compile(r"^(\s*)(-{3}\s*(page\s+\d+|\(end of text copy)|\[PDF text copy made by cfharness)", re.IGNORECASE | re.MULTILINE)

log = logging.getLogger("cfharness.manager.doc_extract")

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


def _pdf_reader(data: bytes) -> Any | None:
    """PdfReader đã mở khoá (mã hoá → thử mật khẩu rỗng); không mở được → None."""
    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # PDF lỗi nhẹ sinh rất nhiều cảnh báo — không làm ồn hub.log
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):  # PasswordType.NOT_DECRYPTED == 0
                return None
        except Exception:  # noqa: BLE001 — thuật toán lạ/thiếu thư viện: coi như không mở được
            return None
    return reader


def pdf_part_pages(total: int, split_pages: int, max_parts: int) -> int:
    """Số trang mỗi phần; 0 = không tách (`split_pages` ≤ 0 hoặc PDF ≤ `split_pages` trang). Nới lên (tới `PDF_PART_MAX_PAGES`) để
    `max_parts` phần phủ HẾT tài liệu — PDF 3000 trang, 20 phần → 100 trang/phần thay vì dừng ở trang 1000."""
    if split_pages <= 0 or total <= split_pages:
        return 0
    need = -(-total // max(1, max_parts))
    return min(max(split_pages, need), max(split_pages, PDF_PART_MAX_PAGES))


def pdf_events(data: bytes, *, split_pages: int = PDF_SPLIT_PAGES, max_parts: int = PDF_SPLIT_MAX_PARTS,
               text_max_pages: int = PDF_TEXT_MAX_PAGES, time_budget_s: float = PDF_TIME_BUDGET_S,
               parts_max_bytes: int = PDF_PARTS_MAX_BYTES) -> Iterator[dict[str, Any]]:
    """Trích PDF thành dòng sự kiện (tiến trình con ghi từng sự kiện ngay → bị giết giữa chừng vẫn giữ phần đã xong):
    `{"status": "encrypted"|"unreadable"}` (rồi hết) | `{"pages": N}` → các `{"part", "first", "last", "data"}` → `{"parts_end": lý do}`
    → các `{"page": n, "text"}` → `{"text_end": lý do}` → `{"done": True}`. Lý do cắt: None (đủ) | "pages" | "bytes" | "time" | "parts" | "error".
    Tách phần TRƯỚC (chỉ chép đối tượng trang, không phân tích nội dung → nhanh), ≤ 40% ngân sách; chữ dùng phần còn lại. Ngân sách
    chỉ kiểm giữa các trang — một trang bệnh hoạn treo thì hạn giờ cứng của tiến trình con giết, các trang/phần trước đó vẫn còn.
    `parts_max_bytes`: trần tổng dung lượng các phần của PDF này (daemon chia `PDF_PARTS_MAX_BYTES` của cả tin cho các PDF)."""
    start = time.monotonic()
    budget = max(0.1, float(time_budget_s))
    try:
        reader = _pdf_reader(data)
        total = len(reader.pages) if reader is not None else 0
    except Exception:  # noqa: BLE001 — hỏng/cụt/rác
        log.debug("mở PDF lỗi", exc_info=True)
        yield {"status": "unreadable"}
        return
    if reader is None:
        yield {"status": "encrypted"}
        return
    yield {"pages": total}
    # 1) phần tách
    per = pdf_part_pages(total, int(split_pages), int(max_parts))
    reason: str | None = None
    if per:
        from pypdf import PdfWriter

        width = max(3, len(str(total)))
        used = 0
        deadline = start + budget * 0.4
        try:
            for k, first in enumerate(range(0, total, per)):
                if k >= max_parts:
                    reason = "parts"
                    break
                if time.monotonic() > deadline:
                    reason = "time"
                    break
                last = min(first + per, total)
                w = PdfWriter()
                for i in range(first, last):
                    w.add_page(reader.pages[i])
                buf = io.BytesIO()
                w.write(buf)
                body = buf.getvalue()
                if used + len(body) > parts_max_bytes:
                    reason = "bytes"
                    break
                used += len(body)
                yield {"part": f".p{first + 1:0{width}d}-{last:0{width}d}.pdf", "first": first + 1, "last": last, "data": body}
        except Exception:  # noqa: BLE001 — tách hỏng: giữ các phần đã có, vẫn trích chữ
            log.debug("tách PDF lỗi", exc_info=True)
            reason = "error"
    yield {"parts_end": reason}
    # 2) chữ theo trang
    reason = None
    size = 0
    deadline = start + budget
    for i in range(total):
        if i >= text_max_pages:
            reason = "pages"
            break
        if time.monotonic() > deadline:
            reason = "time"
            break
        try:
            t = (reader.pages[i].extract_text() or "").strip()
        except Exception:  # noqa: BLE001 — một trang hỏng không bỏ cả tệp
            t = PAGE_ERROR_TEXT
        size += len(t.encode("utf-8")) + 32
        if size > OUT_MAX_BYTES:
            reason = "bytes"
            break
        yield {"page": i + 1, "text": t}
    yield {"text_end": reason}
    yield {"done": True}


_REASON_EN = {"pages": "page limit of the text copy", "bytes": "size limit of the text copy", "time": "extraction time limit",
              "error": "extraction stopped early", "parts": "maximum number of parts", None: "extraction stopped early"}


def _text_header(total: int | None, n: int, reason: str | None, cut: bool) -> str:
    of = f" of {total}" if total else ""
    if not cut:
        return f"[PDF text copy made by cfharness: all {n} page(s). Each page starts with `--- page N ---`.]"
    return (f"[PDF text copy made by cfharness: ONLY pages 1-{n}{of} (stopped: {_REASON_EN.get(reason, reason)}). "
            f"Each page starts with `--- page N ---`; pages after {n} are NOT in this file.]")


def _neutralize(text: str) -> str:
    """Dòng trong PDF trông như tiêu đề của hub (`--- page 7 ---`, `[PDF text copy made by cfharness…`) → tiền tố `(in PDF) `."""
    return _FAKE_HEADER.sub(lambda m: m.group(1) + "(in PDF) " + m.group(2), text)


def pdf_assemble(events: Iterable[dict[str, Any]], *, interrupted: str | None = None, parts_max_bytes: int | None = None
                 ) -> tuple[list[tuple[str, bytes | Path]], dict[str, Any]]:
    """Sự kiện của `pdf_events` (trong tiến trình hoặc đọc lại từ tiến trình con, có thể cụt) → ([(".txt", chữ)?, (".pNNN-MMM.pdf",
    bytes | Path)…], info) — phần từ tiến trình con là `Path` tệp trong thư mục tạm (`path` + `size`, đã kiểm ở `_read_worker_events`),
    không nạp vào bộ nhớ daemon. `parts_max_bytes`: trần tổng các phần (kiểm lại ở daemon, không tin tiến trình con).
    Sự kiện lạ/sai thứ tự bị bỏ TỪNG CÁI (không bỏ cả kết quả). Bản chữ có dòng đầu nêu PHẠM VI trang (`pages 1-13 of 400`).
    info = {status: ok|encrypted|unreadable|timeout|error, pages, text_pages, text_reason, parts_last, parts_reason} — `files_note` nêu
    cho agent bản chữ / các phần phủ tới trang nào, trang nào không đọc được."""
    info: dict[str, Any] = {"status": "ok", "pages": None, "text_pages": 0, "text_reason": None, "parts_last": 0, "parts_reason": None}
    pages: list[str] = []
    parts: list[tuple[str, bytes | Path]] = []
    size = 0
    parts_size = 0
    text_end = parts_end = False
    any_text = False
    page_errors = 0
    try:
        for ev in events:
            if not isinstance(ev, dict):
                continue
            if "status" in ev and ev["status"] in ("encrypted", "unreadable"):
                info["status"] = ev["status"]
            elif "pages" in ev and isinstance(ev["pages"], int) and not isinstance(ev["pages"], bool) and ev["pages"] >= 0:
                info["pages"] = ev["pages"]
            elif "part" in ev:
                m = _PART_SUFFIX.match(str(ev.get("part")))
                first, last = ev.get("first"), ev.get("last")
                body: bytes | Path | None = ev.get("data") if isinstance(ev.get("data"), bytes) else None
                n = len(body) if body is not None else 0
                if body is None and isinstance(ev.get("path"), Path) and isinstance(ev.get("size"), int):
                    body, n = ev["path"], ev["size"]
                if (m and body is not None and isinstance(first, int) and isinstance(last, int) and not parts_end
                        and (int(m.group(1)), int(m.group(2))) == (first, last) and first == info["parts_last"] + 1 and last >= first):
                    if parts_max_bytes is not None and parts_size + n > parts_max_bytes:
                        parts_end = True  # phần sau không còn liền mạch → dừng nhận phần
                        info["parts_reason"] = "bytes"
                        continue
                    parts_size += n
                    parts.append((ev["part"], body))
                    info["parts_last"] = last
                else:
                    log.warning("bỏ phần PDF không hợp lệ: %r", str(ev.get("part"))[:40])
            elif "parts_end" in ev:
                if parts_end:
                    continue
                parts_end = True
                info["parts_reason"] = ev["parts_end"] if ev["parts_end"] in _REASON_EN else None
            elif "page" in ev:
                if text_end or ev.get("page") != len(pages) + 1 or not isinstance(ev.get("text"), str):
                    continue
                t = ev["text"]
                size += len(t.encode("utf-8")) + 32
                if size > OUT_MAX_BYTES:
                    info["text_reason"] = "bytes"
                    text_end = True
                    continue
                pages.append(t)
                page_errors += t == PAGE_ERROR_TEXT
                any_text = any_text or (bool(t) and t != PAGE_ERROR_TEXT)
            elif "text_end" in ev and not text_end:
                text_end = True
                info["text_reason"] = ev["text_end"] if ev["text_end"] in _REASON_EN else None
    except Exception:  # noqa: BLE001 — luồng sự kiện hỏng giữa chừng: giữ phần đã có
        log.debug("đọc kết quả trích PDF lỗi", exc_info=True)
        interrupted = interrupted or "error"
    total = info["pages"]
    if total is not None and not parts_end and interrupted:
        info["parts_reason"] = interrupted
    if total is not None and not text_end:
        info["text_reason"] = interrupted or "error"
    if total is None and info["status"] == "ok":
        info["status"] = "timeout" if interrupted == "time" else "error" if interrupted else "unreadable"
    out: list[tuple[str, bytes]] = []
    if any_text:
        n = len(pages)
        cut = n < (total or n) or info["text_reason"] is not None
        if not cut:
            info["text_reason"] = None
        head = _text_header(total, n, info["text_reason"], cut)
        body = "\n\n".join(f"--- page {i} ---\n{_neutralize(t)}" for i, t in enumerate(pages, 1))
        tail = f"\n\n--- (end of text copy: pages 1-{n}" + (f" of {total}" if total else "") + ") ---" if cut else ""
        out.append((".txt", (head + "\n\n" + body + tail + "\n").encode("utf-8")))
        info["text_pages"] = n
    elif total is not None:
        # không trang nào có chữ: đọc hết (hoặc tới trần) mà không thấy chữ = PDF scan; dừng giữa chừng = chưa biết; có trang trích LỖI
        # (giới hạn giải nén, content stream hỏng) → "page_errors", không nói là PDF scan
        if text_end and info["text_reason"] in (None, "pages", "bytes"):
            info["no_text"] = "page_errors" if page_errors else "scanned"
        else:
            info["no_text"] = info["text_reason"] or "error"
        info["text_reason"] = None
    out.extend(parts)
    return out, info


def pdf_extract_info(data: bytes, **kw: Any) -> tuple[list[tuple[str, bytes]], dict[str, Any]]:
    """`pdf_events` + `pdf_assemble` NGAY trong tiến trình gọi (test, CLI). Không bao giờ ném lỗi."""
    try:
        return pdf_assemble(pdf_events(data, **kw))
    except Exception:  # noqa: BLE001 — PDF hỏng/bệnh hoạn: chỉ lưu bản gốc
        log.debug("trích PDF lỗi", exc_info=True)
        return [], {"status": "error", "pages": None, "text_pages": 0, "text_reason": None, "parts_last": 0, "parts_reason": None}


def pdf_extract(data: bytes, **kw: Any) -> list[tuple[str, bytes]]:
    """PDF → [(".txt", chữ UTF-8)?, (".p001-050.pdf", bytes)…]. Không mở được (hỏng/mã hoá có mật khẩu) → []. Không bao giờ ném lỗi."""
    return pdf_extract_info(data, **kw)[0]


def pdf_options(cfg: Any) -> dict[str, Any]:
    """`attachments.pdf.*` (HubConfig hoặc dict đã gộp) → tuỳ chọn cho `extract_isolated`; giá trị lạ → mặc định."""
    raw: Any = None
    try:
        if hasattr(cfg, "get") and not isinstance(cfg, dict):
            raw = cfg.get("attachments.pdf")
        else:
            raw = ((cfg or {}).get("attachments") or {}).get("pdf")
    except (KeyError, AttributeError, TypeError):
        raw = None
    raw = raw if isinstance(raw, dict) else {}

    def num(key: str, default: float) -> float:
        lo, hi = PDF_LIMITS[key]
        v = raw.get(key, default)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi else default

    split = raw.get("split", True) is not False
    return {"split_pages": int(num("split_pages", PDF_SPLIT_PAGES)) if split else 0,
            "max_parts": int(num("max_parts", PDF_SPLIT_MAX_PARTS)),
            "timeout_s": num("timeout_s", PDF_MESSAGE_TIMEOUT_S)}


def _read_worker_events(work: Path) -> Iterator[dict[str, Any]]:
    """`events.jsonl` của `pdf_worker` (JSON mỗi dòng, có thể cụt khi bị giết) → sự kiện. Đọc từng dòng (≤ `EVENT_LINE_MAX_BYTES`,
    tổng ≤ `EVENTS_MAX_BYTES`) — không nạp cả tệp; dòng hỏng/cụt/quá dài bị bỏ. Phần tách: `file` phải đúng mẫu `part-NNNN.pdf`, là
    tệp thường (không symlink) ngay trong `work` → `path` + `size`; sai → bỏ mục đó."""
    try:
        f = open(work / WORKER_EVENTS, "rb")  # noqa: SIM115 — đóng ở finally
    except OSError:
        return
    total = 0
    try:
        while total < EVENTS_MAX_BYTES:
            line = f.readline(EVENT_LINE_MAX_BYTES)
            if not line:
                break
            total += len(line)
            if not line.endswith(b"\n") and len(line) >= EVENT_LINE_MAX_BYTES:  # dòng quá dài: bỏ phần còn lại của dòng
                while True:
                    rest = f.readline(EVENT_LINE_MAX_BYTES)
                    total += len(rest)
                    if not rest or rest.endswith(b"\n") or total >= EVENTS_MAX_BYTES:
                        break
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if "part" in ev:
                ev.pop("data", None)
                name = ev.pop("file", None)
                if not isinstance(name, str) or not _WORKER_PART_FILE.match(name):
                    ev["path"] = None
                else:
                    pth = work / name
                    try:
                        st = os.lstat(pth)
                        ok, size = stat.S_ISREG(st.st_mode), int(st.st_size)
                    except OSError:
                        ok, size = False, 0
                    ev["path"], ev["size"] = (pth, size) if ok else (None, None)
            yield ev
    finally:
        f.close()


def _worker_argv() -> list[str]:
    return [sys.executable, "-I", "-m", "cfharness.manager.pdf_worker"]


def extract_isolated(data: bytes, opts: dict[str, Any] | None = None, *, work_dir: Path | None = None
                     ) -> tuple[list[tuple[str, bytes | Path]], dict[str, Any]]:
    """`pdf_events` trong TIẾN TRÌNH CON (`python -I -m cfharness.manager.pdf_worker`) có hạn giờ cứng `timeout_s`: PDF bệnh hoạn
    (vòng lặp, nổ bộ nhớ) không treo daemon; hết giờ → giết, GIỮ các trang/phần đã ghi (tiến trình con ghi từng sự kiện/phần ngay).
    Tiến trình con ghi THẲNG vào thư mục tạm riêng (`work_dir`: sự kiện `events.jsonl` + mỗi phần một tệp) — daemon không giữ byte các
    phần trong bộ nhớ. `work_dir` do người gọi tạo (rỗng) → phần trả về là `Path` trong đó, người gọi chuyển chỗ rồi xoá thư mục; None →
    tự tạo thư mục tạm, đọc phần thành bytes rồi xoá (test, CLI). `opts.parts_max_bytes`: trần tổng các phần của PDF này.
    Trả (bản trích, info) như `pdf_assemble`. Gọi từ thread (không chặn event loop). Không bao giờ ném lỗi."""
    from ..oscompat import no_window_kwargs

    o = dict(opts or {})
    timeout = float(o.pop("timeout_s", PDF_MESSAGE_TIMEOUT_S))
    pmax = o.get("parts_max_bytes")
    parts_max = int(pmax) if isinstance(pmax, (int, float)) and not isinstance(pmax, bool) and pmax >= 0 else PDF_PARTS_MAX_BYTES
    o["parts_max_bytes"] = parts_max
    # ngân sách mềm < hạn giờ cứng: chừa ~1.5 s khởi động tiến trình + import pypdf, để thường kết thúc sạch trước khi bị giết
    o.setdefault("time_budget_s", max(0.5, min(PDF_TIME_BUDGET_S, timeout * 0.8 - 1.5)))
    own = work_dir is None
    try:
        work = Path(tempfile.mkdtemp(prefix="cfh-pdf-")) if own else Path(work_dir)  # type: ignore[arg-type]
    except OSError as e:
        log.warning("không tạo được thư mục tạm trích PDF: %s", e)
        return pdf_assemble([], interrupted="error")
    head = {k: o[k] for k in ("split_pages", "max_parts", "text_max_pages", "time_budget_s", "parts_max_bytes") if k in o}
    head["out_dir"] = str(work)
    interrupted: str | None = None
    try:
        try:
            with open(work / WORKER_STDERR, "wb") as err:
                r = subprocess.run(_worker_argv(), input=json.dumps(head).encode() + b"\n" + data, stdout=subprocess.DEVNULL,
                                   stderr=err, timeout=timeout, **no_window_kwargs())
            if r.returncode != 0:
                interrupted = "error"
                log.warning("tiến trình trích PDF lỗi (mã %s): %s", r.returncode, _tail(work / WORKER_STDERR))
        except subprocess.TimeoutExpired:
            log.warning("trích PDF quá %.0f giây — giữ phần đã trích được", timeout)
            interrupted = "time"
        except OSError as e:
            log.warning("không chạy được tiến trình trích PDF: %s", e)
            interrupted = "error"
        out, info = pdf_assemble(_read_worker_events(work), interrupted=interrupted, parts_max_bytes=parts_max)
        if own:
            loaded: list[tuple[str, bytes | Path]] = []
            for suffix, body in out:
                if isinstance(body, Path):
                    try:
                        body = body.read_bytes()
                    except OSError:
                        continue
                loaded.append((suffix, body))
            out = loaded
        return out, info
    except Exception:  # noqa: BLE001 — không bao giờ ném lỗi ra daemon
        log.warning("trích PDF lỗi", exc_info=True)
        return pdf_assemble([], interrupted="error")
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)


def _tail(path: Path, n: int = 300) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - n))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def extract(ext: str, data: bytes, *, pdf: dict[str, Any] | None = None) -> list[tuple[str, bytes]]:
    """Bản đọc được: [(hậu tố tên tệp, bytes)]; rỗng khi loại này không trích hoặc trích lỗi. PDF chạy NGAY trong tiến trình gọi
    (`pdf` = tuỳ chọn của `pdf_extract`); daemon dùng `extract_isolated` cho PDF."""
    if ext == ".pdf":
        keep = ("split_pages", "max_parts", "text_max_pages", "time_budget_s")
        return pdf_extract(data, **{k: v for k, v in (pdf or {}).items() if k in keep})
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
