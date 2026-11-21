"""Ghi vào thư mục chung `/Users/Shared/cfharness` (kênh admind → hub). Đường dẫn lấy từ `admin_control` (HẰNG SỐ, test đổi được).

- `control/visibility.json` — mức xem dữ liệu + công tắc trang minh bạch (SYS-03, VIS-07); hub đọc để quyết định xuất trường nào.
- `control/estop[.<u>]` — dùng lại `admin_control.set_flag`.
Mọi file ghi nguyên tử (mkstemp cùng thư mục → chmod 0644 → fsync → rename); nội dung chỉ enum/bool/số (không chữ tự do tới agent).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import admin_control as AC

if TYPE_CHECKING:
    from .dispatch import State

VISIBILITY_FILE = "visibility.json"


def write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        try:
            os.fchmod(fd, mode)  # type: ignore[attr-defined]
        except (AttributeError, OSError):  # pragma: no cover — Windows
            pass
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def write_json(path: Path, obj: Any, mode: int = 0o644) -> None:
    write_atomic(path, (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8"), mode)


def visibility(state: "State") -> dict[str, Any]:
    from .ops_system import setting

    return {"schema": 1, "activity": bool(setting(state.db, "visibility.activity")),
            "transparency": bool(setting(state.db, "visibility.transparency"))}


def write_visibility(state: "State") -> dict[str, Any]:
    v = visibility(state)
    write_json(AC.control_dir() / VISIBILITY_FILE, v)
    return v
