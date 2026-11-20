"""Tiến trình con trích PDF cho `doc_extract.extract_isolated` — `python -I -m cfharness.manager.pdf_worker`.

stdin: một dòng JSON tuỳ chọn (`split_pages`, `max_parts`, `text_max_pages`, `time_budget_s`) rồi byte PDF.
stdout: mỗi sự kiện của `doc_extract.pdf_events` một dòng JSON, ghi + flush NGAY (phần tách: `data` → `b64`). Daemon giết tiến trình
khi quá hạn giờ (một trang bệnh hoạn treo `extract_text`) vẫn đọc được các dòng đã ghi → giữ các trang/phần đã xong.
Chạy tách khỏi daemon để PDF bệnh hoạn (vòng lặp, nổ bộ nhớ) chỉ làm chết tiến trình này.
POSIX: tự đặt trần bộ nhớ ảo `MEMORY_LIMIT_BYTES` (Windows: chỉ hạn giờ của daemon).
"""

from __future__ import annotations

import base64
import json
import sys

MEMORY_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
OPTION_KEYS = ("split_pages", "max_parts", "text_max_pages", "time_budget_s")


def _limit_memory() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    except (ImportError, ValueError, OSError):  # best-effort: Windows không có `resource`; macOS có thể từ chối RLIMIT_AS
        pass


def main() -> int:
    _limit_memory()
    raw = sys.stdin.buffer.read()
    head, _, data = raw.partition(b"\n")
    try:
        opts = json.loads(head or b"{}")
    except ValueError:
        opts = {}
    if not isinstance(opts, dict):
        opts = {}
    kw = {k: opts[k] for k in OPTION_KEYS if isinstance(opts.get(k), (int, float)) and not isinstance(opts.get(k), bool)}
    from .doc_extract import pdf_events

    out = sys.stdout
    for ev in pdf_events(data, **kw):
        if "data" in ev:
            body = ev.pop("data")
            ev["b64"] = base64.b64encode(body).decode("ascii")
        out.write(json.dumps(ev, ensure_ascii=True) + "\n")
        out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
