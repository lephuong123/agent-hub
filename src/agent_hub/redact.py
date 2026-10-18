"""Che secret trong dữ liệu trước khi lưu/hiển thị (NFR-10, FR-RT-02 argv_redacted, H-12).

Hai lớp, dùng CHUNG cho DB (events/runs/audit/guard_decisions), raw stdout (`runs/*.jsonl`), log, OTel, Slack:
1. Theo MẪU: chuỗi trông như token (`sk-…`, `ghp_…`, `Bearer …`) + khoá dict nhạy cảm (`token`, `password`…).
2. Theo GIÁ TRỊ (H-12): mọi giá trị secret hub biết — nạp từ SecretStore khi khởi động, thêm khi `secret set`,
   và cả giá trị **env-fallback** đã resolve (`${VAR}` lấy từ biến môi trường) — bị thay bằng `***` ở bất cứ đâu,
   kể cả dạng đã escape JSON. Sổ đăng ký là của TIẾN TRÌNH (daemon), chỉ giữ trong bộ nhớ.
Giá trị ngắn hơn `MIN_VALUE_LEN` không đăng ký (tránh che nhầm từ thông dụng).
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any

_SECRET_KEY = re.compile(r"(secret|token|password|passwd|api[_-]?key|authorization|cookie|credential)", re.I)
_SECRET_VALUE = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9_\-]{16,}|(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_\-]{16,}|xox[abprs]-[A-Za-z0-9\-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_\-]{30,}|"
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}|Bearer\s+[A-Za-z0-9._\-]{12,})"
)
MASK = "***"
MIN_VALUE_LEN = 8

# ---------------------------------------------------------------------------- sổ giá trị secret (H-12)
_lock = threading.Lock()
_values: set[str] = set()
_rx: re.Pattern[str] | None = None
_rx_bytes: re.Pattern[bytes] | None = None


def _variants(v: str) -> set[str]:
    out = {v}
    j = json.dumps(v, ensure_ascii=False)[1:-1]  # dạng đã escape trong JSON (", \, xuống dòng)
    out.add(j)
    out.add(json.dumps(v)[1:-1])  # ensure_ascii=True
    return {x for x in out if len(x) >= MIN_VALUE_LEN}


def register_secret_values(*values: str | None) -> None:
    """Đăng ký giá trị secret để che theo giá trị. Gọi khi nạp/đặt/resolve secret."""
    global _rx, _rx_bytes
    new: set[str] = set()
    for v in values:
        if isinstance(v, str) and len(v.strip()) >= MIN_VALUE_LEN:
            new |= _variants(v)
            if v.strip() != v:
                new |= _variants(v.strip())
    with _lock:
        if new <= _values:
            return
        _values.update(new)
        alts = sorted(_values, key=len, reverse=True)
        _rx = re.compile("|".join(re.escape(a) for a in alts))
        _rx_bytes = re.compile(b"|".join(re.escape(a.encode("utf-8")) for a in alts))


def known_secret_count() -> int:
    return len(_values)


def mask_values(s: str) -> str:
    rx = _rx
    return rx.sub(MASK, s) if rx is not None and s else s


def mask_bytes(b: bytes) -> bytes:
    rx = _rx_bytes
    return rx.sub(MASK.encode(), b) if rx is not None and b else b


def mask_file_inplace(path: Any, limit: int = 50_000_000) -> bool:
    """Che theo giá trị trong một file log (ghi lại nguyên tử). Trả True nếu có thay đổi."""
    import os
    from pathlib import Path

    rx = _rx_bytes
    p = Path(path)
    try:
        if rx is None or not p.is_file() or p.is_symlink() or p.stat().st_size > limit:
            return False
        data = p.read_bytes()
        new = rx.sub(MASK.encode(), data)
        if new == data:
            return False
        tmp = p.with_name(f".{p.name}.mask.tmp")
        tmp.write_bytes(new)
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def mask_obj(obj: Any) -> Any:
    """Che theo GIÁ TRỊ trong cấu trúc lồng (không đổi khoá/kiểu). Không có secret nào → trả nguyên obj."""
    if _rx is None:
        return obj
    if isinstance(obj, str):
        return mask_values(obj)
    if isinstance(obj, dict):
        return {k: mask_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_obj(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(mask_obj(x) for x in obj)
    return obj


# ---------------------------------------------------------------------------- API cũ (đã gồm che theo giá trị)
def redact_str(s: str) -> str:
    return _SECRET_VALUE.sub(MASK, mask_values(s))


def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: (MASK if isinstance(k, str) and _SECRET_KEY.search(k) and v else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    if isinstance(obj, str):
        return redact_str(obj)
    return obj


def redact_argv(argv: list[str]) -> list[str]:
    """Che giá trị đi sau cờ nhạy cảm và chuỗi trông như secret; rút gọn prompt dài."""
    out: list[str] = []
    hide_next = False
    for a in argv:
        if hide_next:
            out.append(MASK)
            hide_next = False
            continue
        if a.startswith("--") and _SECRET_KEY.search(a):
            out.append(a)
            hide_next = True
            continue
        out.append(redact_str(a))
    return out


class SecretLogFilter:
    """logging.Filter: che theo giá trị + mẫu trong MỌI bản ghi log (gắn vào handler của daemon — H-12)."""

    def filter(self, record: Any) -> bool:
        try:
            msg = record.getMessage()
            new = redact_str(msg)
            if record.exc_info and not record.exc_text:
                import logging as _logging

                record.exc_text = _logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact_str(record.exc_text)
            if new != msg or record.args:
                record.msg, record.args = new, None
        except Exception:  # pragma: no cover — không để log hỏng vì bộ che
            pass
        return True


def install_log_filter(logger: Any = None) -> None:
    """Gắn `SecretLogFilter` vào mọi handler của logger (mặc định root). Idempotent."""
    import logging as _logging

    lg = logger or _logging.getLogger()
    for h in list(lg.handlers):
        if not any(isinstance(f, SecretLogFilter) for f in h.filters):
            h.addFilter(SecretLogFilter())


def install_stderr_masking() -> None:
    """Thư viện bên thứ ba (vd SDK `mcp`) log ra stderr qua `logging.lastResort` → `logs/daemon.out` KHÔNG che.
    Gắn handler stderr có `SecretLogFilter` cho root logger (daemon gọi lúc khởi động)."""
    import logging as _logging
    import sys as _sys

    root = _logging.getLogger()
    if not any(getattr(h, "_agent_hub_masked", False) for h in root.handlers):
        h = _logging.StreamHandler(_sys.stderr)
        h.setLevel(_logging.WARNING)
        h.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        h._agent_hub_masked = True  # type: ignore[attr-defined]
        root.addHandler(h)
    install_log_filter(root)
