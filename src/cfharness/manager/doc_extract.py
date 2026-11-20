"""Tệp tài liệu (PDF/Office) người gửi kèm tin cho điều phối viên: nhận diện theo NỘI DUNG + trích bản đọc được.

- `doc_ext(name, data)`: PDF (`%PDF-`), Office OOXML/ODF (zip hợp lệ + đuôi .xlsx/.xlsm/.docx/.pptx/.ods/.odt/.odp),
  Office cũ (OLE2 + đuôi .xls/.doc/.ppt) → đuôi chuẩn; còn lại → None (không tin đuôi một mình, không tin MIME).
- `extract(ext, data)`: bản đọc được cho MỌI khung quyền (strict chỉ có Read): xlsx → mỗi sheet một CSV (cột đầu `row` = số dòng
  Excel, tiêu đề = chữ cái cột A, B, C… để "cột E dòng 7" khớp với file gốc); docx → chữ theo đoạn; pptx → chữ theo slide (Office: stdlib).
  PDF (thư viện `pypdf`, thuần Python): một `.txt` có tiêu đề `--- page N ---` (≤ `PDF_TEXT_MAX_PAGES` trang, ≤ `OUT_MAX_BYTES`, cắt
  thì ghi chú trong chữ); PDF nhiều hơn `split_pages` trang → thêm các phần `.p001-050.pdf`… (≤ `PDF_SPLIT_MAX_PARTS` phần, tổng
  ≤ `PDF_PARTS_MAX_BYTES`) để Read đọc từng phần chỉ với `file_path` (giữ bảng/hình). PDF mã hoá (thử mật khẩu rỗng, không được → bỏ)
  hoặc PDF scan (không có chữ) → không có `.txt`. Daemon gọi PDF qua `extract_isolated` (tiến trình con `pdf_worker`, có hạn giờ —
  PDF bệnh hoạn không treo/ăn CPU của daemon). .xls/.doc/.ppt/ODF không trích (chỉ lưu bản gốc).
- Chống zip bomb: chỉ đọc các phần cần, mỗi phần ≤ `PART_MAX_BYTES` (theo kích thước giải nén khai báo VÀ đếm thật), đầu ra
  ≤ `OUT_MAX_BYTES`/tệp, ≤ `SHEET_MAX_ROWS` dòng/sheet; lỗi → bỏ trích (bản gốc vẫn được lưu). Không bao giờ ném lỗi ra ngoài.
  PDF: giới hạn giải nén có sẵn của pypdf + ngân sách thời gian theo trang (`PDF_TIME_BUDGET_S`) + hạn giờ cứng của tiến trình con.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import logging
import posixpath
import re
import subprocess
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from typing import Any, Iterator

PART_MAX_BYTES = 64 * 1024 * 1024
OUT_MAX_BYTES = 8 * 1024 * 1024
SHEET_MAX_ROWS = 50_000
SHEET_MAX_COLS = 702  # A..ZZ
PDF_TEXT_MAX_PAGES = 500  # trang tối đa đưa vào bản chữ
PDF_TIME_BUDGET_S = 30.0  # ngân sách thời gian trích MỘT tệp PDF (kiểm giữa các trang; hết → dừng, ghi chú trong chữ)
PDF_SPLIT_PAGES = 50  # mặc định `attachments.pdf.split_pages`: PDF nhiều hơn N trang → thêm các phần N trang
PDF_SPLIT_MAX_PARTS = 20  # mặc định `attachments.pdf.max_parts`
PDF_PARTS_MAX_BYTES = 48 * 1024 * 1024  # tổng dung lượng các phần của một PDF
PDF_WORKER_TIMEOUT_S = 90.0  # mặc định `attachments.pdf.timeout_s`: hạn giờ cứng của tiến trình con trích PDF
PDF_COPY_SUFFIX = re.compile(r"^\.(txt|p\d{3,5}-\d{3,5}\.pdf)$")  # hậu tố hợp lệ trả về từ tiến trình con

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


def pdf_to_text(reader: Any, *, max_pages: int = PDF_TEXT_MAX_PAGES, deadline: float | None = None) -> str:
    """Chữ theo trang (`--- page N ---`); trang không có chữ vẫn có tiêu đề. Rỗng khi KHÔNG trang nào có chữ (PDF scan).
    Cắt (quá `max_pages`, quá `OUT_MAX_BYTES`, hết ngân sách thời gian) → dòng ghi chú cuối chữ."""
    total = len(reader.pages)
    parts: list[str] = []
    size = 0
    any_text = False
    note = ""
    for i in range(min(total, max_pages)):
        if deadline is not None and time.monotonic() > deadline:
            note = f"--- (cắt bớt: hết thời gian trích, mới được {i}/{total} trang — đọc phần còn lại trong bản gốc) ---"
            break
        try:
            t = (reader.pages[i].extract_text() or "").strip()
        except Exception:  # noqa: BLE001 — một trang hỏng không bỏ cả tệp
            t = "(không đọc được chữ của trang này)"
        else:
            any_text = any_text or bool(t)
        block = f"--- page {i + 1} ---\n{t}"
        size += len(block.encode("utf-8")) + 2
        if size > OUT_MAX_BYTES:
            note = f"--- (cắt bớt: quá {OUT_MAX_BYTES // 1024 // 1024} MB, mới được {i}/{total} trang — đọc phần còn lại trong bản gốc) ---"
            break
        parts.append(block)
    else:
        if total > max_pages:
            note = f"--- (cắt bớt: chỉ trích {max_pages}/{total} trang đầu — đọc phần còn lại trong bản gốc) ---"
    if not any_text:
        return ""
    if note:
        parts.append(note)
    return "\n\n".join(parts) + "\n"


def pdf_split(reader: Any, *, split_pages: int, max_parts: int = PDF_SPLIT_MAX_PARTS, max_bytes: int | None = None,
              deadline: float | None = None) -> list[tuple[str, bytes]]:
    """PDF > `split_pages` trang → [(".p001-050.pdf", bytes), …] theo thứ tự trang; dừng ở `max_parts` phần / `max_bytes` tổng
    (mặc định `PDF_PARTS_MAX_BYTES`) / hết thời gian (các phần đã làm vẫn trả về). ≤ `split_pages` trang hoặc `split_pages` ≤ 0 → []."""
    from pypdf import PdfWriter

    cap = PDF_PARTS_MAX_BYTES if max_bytes is None else max_bytes
    total = len(reader.pages)
    if split_pages <= 0 or total <= split_pages:
        return []
    width = max(3, len(str(total)))
    out: list[tuple[str, bytes]] = []
    used = 0
    for start in range(0, total, split_pages):
        if len(out) >= max_parts or (deadline is not None and time.monotonic() > deadline):
            break
        end = min(start + split_pages, total)
        w = PdfWriter()
        for i in range(start, end):
            w.add_page(reader.pages[i])
        buf = io.BytesIO()
        w.write(buf)
        body = buf.getvalue()
        if used + len(body) > cap:
            break
        used += len(body)
        out.append((f".p{start + 1:0{width}d}-{end:0{width}d}.pdf", body))
    return out


def pdf_extract(data: bytes, *, split_pages: int = PDF_SPLIT_PAGES, max_parts: int = PDF_SPLIT_MAX_PARTS,
                text_max_pages: int = PDF_TEXT_MAX_PAGES, time_budget_s: float = PDF_TIME_BUDGET_S) -> list[tuple[str, bytes]]:
    """PDF → [(".txt", chữ UTF-8)?, (".p001-050.pdf", bytes)…]. Không mở được (hỏng/mã hoá có mật khẩu) → []. Không bao giờ ném lỗi.
    Ngân sách `time_budget_s` cho phần chữ, thêm một lần nữa cho phần tách."""
    try:
        budget = max(0.1, float(time_budget_s))
        deadline = time.monotonic() + budget
        reader = _pdf_reader(data)
        if reader is None:
            return []
        out: list[tuple[str, bytes]] = []
        try:
            text = pdf_to_text(reader, max_pages=int(text_max_pages), deadline=deadline)
        except Exception:  # noqa: BLE001 — không lấy được chữ vẫn thử tách phần
            log.debug("trích chữ PDF lỗi", exc_info=True)
            text = ""
        if text.strip():
            out.append((".txt", text.encode("utf-8")))
        try:
            out.extend(pdf_split(reader, split_pages=int(split_pages), max_parts=int(max_parts), deadline=time.monotonic() + budget))
        except Exception:  # noqa: BLE001 — tách hỏng: giữ bản chữ
            log.debug("tách PDF lỗi", exc_info=True)
        return out
    except Exception:  # noqa: BLE001 — PDF hỏng/bệnh hoạn: chỉ lưu bản gốc
        log.debug("trích PDF lỗi", exc_info=True)
        return []


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

    def num(key: str, default: float, lo: float, hi: float) -> float:
        v = raw.get(key, default)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi else default

    split = raw.get("split", True) is not False
    return {"split_pages": int(num("split_pages", PDF_SPLIT_PAGES, 1, 10_000)) if split else 0,
            "max_parts": int(num("max_parts", PDF_SPLIT_MAX_PARTS, 1, 200)),
            "timeout_s": num("timeout_s", PDF_WORKER_TIMEOUT_S, 5, 600)}


def extract_isolated(data: bytes, opts: dict[str, Any] | None = None) -> list[tuple[str, bytes]]:
    """`pdf_extract` trong TIẾN TRÌNH CON (`python -I -m cfharness.manager.pdf_worker`) có hạn giờ cứng `timeout_s`: PDF bệnh hoạn
    (vòng lặp, nổ bộ nhớ) không treo daemon; hết giờ → giết, trả []. Gọi từ thread (không chặn event loop). Không bao giờ ném lỗi."""
    from ..oscompat import no_window_kwargs

    o = dict(opts or {})
    timeout = float(o.pop("timeout_s", PDF_WORKER_TIMEOUT_S))
    o.setdefault("time_budget_s", max(1.0, min(PDF_TIME_BUDGET_S, timeout / 3)))
    head = json.dumps({k: o[k] for k in ("split_pages", "max_parts", "time_budget_s") if k in o}).encode()
    try:
        r = subprocess.run([sys.executable, "-I", "-m", "cfharness.manager.pdf_worker"], input=head + b"\n" + data,
                           capture_output=True, timeout=timeout, **no_window_kwargs())
    except subprocess.TimeoutExpired:
        log.warning("trích PDF quá %.0f giây — bỏ (chỉ lưu bản gốc)", timeout)
        return []
    except OSError as e:
        log.warning("không chạy được tiến trình trích PDF: %s", e)
        return []
    if r.returncode != 0:
        log.warning("tiến trình trích PDF lỗi (mã %s): %s", r.returncode, r.stderr.decode("utf-8", "replace")[-300:])
        return []
    try:
        items = json.loads(r.stdout or b"[]")
        out = []
        for suffix, b64 in items:
            if not isinstance(suffix, str) or not PDF_COPY_SUFFIX.match(suffix):
                raise ValueError(f"hậu tố lạ: {suffix!r}")
            out.append((suffix, base64.b64decode(b64, validate=True)))
        return out
    except (ValueError, TypeError) as e:
        log.warning("kết quả trích PDF không hợp lệ: %s", e)
        return []


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
