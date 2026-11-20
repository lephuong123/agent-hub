"""Tiến trình con trích PDF cho `doc_extract.extract_isolated` — `python -I -m cfharness.manager.pdf_worker`.

stdin: một dòng JSON tuỳ chọn (`split_pages`, `max_parts`, `time_budget_s`) rồi byte PDF. stdout: JSON `[[hậu tố, base64], …]`.
Chạy tách khỏi daemon để PDF bệnh hoạn (vòng lặp, nổ bộ nhớ) chỉ làm chết tiến trình này (daemon giết khi quá hạn giờ).
POSIX: tự đặt trần bộ nhớ ảo `MEMORY_LIMIT_BYTES` (Windows: chỉ hạn giờ của daemon).
"""

from __future__ import annotations

import base64
import json
import sys

MEMORY_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
OPTION_KEYS = ("split_pages", "max_parts", "time_budget_s")


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
    from .doc_extract import pdf_extract

    out = [[suffix, base64.b64encode(body).decode("ascii")] for suffix, body in pdf_extract(data, **kw)]
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
