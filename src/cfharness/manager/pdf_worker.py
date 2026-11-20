"""Tiến trình con trích PDF cho `doc_extract.extract_isolated` — `python -I -m cfharness.manager.pdf_worker`.

stdin: một dòng JSON (`out_dir` bắt buộc; tuỳ chọn `split_pages`, `max_parts`, `text_max_pages`, `time_budget_s`, `parts_max_bytes`)
rồi byte PDF. Ghi THẲNG vào `out_dir` (thư mục tạm riêng daemon vừa tạo): mỗi sự kiện của `doc_extract.pdf_events` một dòng JSON trong
`events.jsonl`, ghi + flush NGAY; mỗi phần tách một tệp `part-NNNN.pdf` (sự kiện chỉ mang tên tệp, không mang byte) → daemon không phải
giữ byte các phần trong bộ nhớ. stdout không dùng. Daemon giết tiến trình khi quá hạn giờ (một trang bệnh hoạn treo `extract_text`)
vẫn đọc được các dòng/tệp đã ghi → giữ các trang/phần đã xong.
Chạy tách khỏi daemon để PDF bệnh hoạn (vòng lặp, nổ bộ nhớ) chỉ làm chết tiến trình này.
POSIX: tự đặt trần bộ nhớ ảo `MEMORY_LIMIT_BYTES` (Windows: chỉ hạn giờ của daemon).
"""

from __future__ import annotations

import json
import os
import sys

MEMORY_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
OPTION_KEYS = ("split_pages", "max_parts", "text_max_pages", "time_budget_s", "parts_max_bytes")


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
    del raw
    try:
        opts = json.loads(head or b"{}")
    except ValueError:
        opts = {}
    if not isinstance(opts, dict):
        opts = {}
    out_dir = opts.get("out_dir")
    if not isinstance(out_dir, str) or not os.path.isdir(out_dir):
        sys.stderr.write("pdf_worker: thiếu out_dir\n")
        return 2
    kw = {k: opts[k] for k in OPTION_KEYS if isinstance(opts.get(k), (int, float)) and not isinstance(opts.get(k), bool)}
    from .doc_extract import WORKER_EVENTS, pdf_events

    k = 0
    with open(os.path.join(out_dir, WORKER_EVENTS), "xb") as out:
        for ev in pdf_events(data, **kw):
            if "data" in ev:
                body = ev.pop("data")
                k += 1
                name = f"part-{k:04d}.pdf"
                with open(os.path.join(out_dir, name), "xb") as f:
                    f.write(body)
                ev["file"] = name
            # chữ trích từ PDF có thể chứa surrogate lẻ → thay bằng "?" (daemon đọc UTF-8)
            out.write(json.dumps(ev, ensure_ascii=False).encode("utf-8", "replace") + b"\n")
            out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
