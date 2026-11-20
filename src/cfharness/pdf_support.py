"""Poppler (`pdftoppm`) cho tool Read của Claude Code — dò, kiểm cấu hình `tools.poppler_path`, thêm vào PATH của run.

Sự thật đã kiểm (claude 2.1.329, `docs/dev-notes/pdf-attachments.md`): Read đọc PDF chỉ với `file_path` → khối document gốc, KHÔNG cần
poppler. Model truyền `pages` (Haiku tự làm kể cả với PDF 2 trang) → Read gọi `pdftoppm` để vẽ trang; máy không có poppler → lỗi
"pdftoppm is not installed". Khung quyền strict chỉ có Read nên agent không tự xoay được → hub:
- `pdftoppm_status(cfg)`: dò `pdftoppm` (PATH của daemon + `tools.poppler_path`), CACHE theo (giá trị cấu hình, PATH) + TTL — không
  gọi tiến trình con mỗi tin; đổi `tools.poppler_path` → khoá cache khác → dò lại ngay.
- Không dùng được → `PDF_SYSTEM_NOTE` vào system prompt mọi run (`prompt.build_system_prompt(pdf_note=…)`) + ghi chú trong `files_note`.
- `tools.poppler_path` hợp lệ → `run_path_extra(cfg)` trả thư mục đó; `ClaudeCliRuntime._run_env` thêm vào CUỐI PATH của tiến trình
  `claude` (thêm cuối, không chèn đầu: thư mục poppler không che được `git`/`python`… sẵn có trên PATH — chỉ thêm lệnh còn thiếu).
- Kiểm giá trị (`validate_poppler_path`): đường dẫn TUYỆT ĐỐI tới thư mục có sẵn, chứa `pdftoppm`/`pdftoppm.exe` là file thường; không
  NUL, không ký tự ngăn cách PATH (`;`/`:`) — một giá trị không thể chèn nhiều mục PATH. Kiểm lại lúc dùng (thư mục bị xoá → bỏ qua).
Không dựa vào binary của Git Bash (có `pdftotext` nhưng không có `pdftoppm`).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("cfharness.pdf_support")

CONFIG_KEY = "tools.poppler_path"
PDFTOPPM = "pdftoppm"
CACHE_TTL_S = 300.0
FIX_HINT = ("cài poppler: `scoop install poppler` hoặc `winget install oschwartz10612.Poppler` (Windows), `brew install poppler` (macOS), "
            "`apt install poppler-utils` (Linux); hoặc `cfharness config set tools.poppler_path <thư mục chứa pdftoppm>`")

# Tiếng Anh (prompt gửi agent). Chỉ thêm khi pdftoppm KHÔNG dùng được trên máy hub.
PDF_SYSTEM_NOTE = """# Reading PDF files
PDF page rendering (pdftoppm) is not installed on this machine. To read a PDF, call Read with ONLY `file_path` and never pass `pages`:
with `pages` the Read tool fails. If cfharness made a text copy (`<file>.pdf.txt`) or page-range parts (`<file>.pdf.p001-050.pdf`)
next to an attached PDF, read those (each with only `file_path`) instead of the whole file."""


def _exe_names() -> tuple[str, ...]:
    return (PDFTOPPM + ".exe", PDFTOPPM) if sys.platform == "win32" else (PDFTOPPM,)


def _find_in_dir(d: Path) -> Path | None:
    for n in _exe_names():
        p = d / n
        try:
            if p.is_file():  # theo symlink (Homebrew/scoop shim) — đích phải là file thường
                return p
        except OSError:
            continue
    return None


def validate_poppler_path(value: Any) -> None:
    """`tools.poppler_path`: null hoặc đường dẫn tuyệt đối tới thư mục có `pdftoppm(.exe)`. Sai → ValueError (tiếng Việt)."""
    if value is None:
        return
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{CONFIG_KEY} phải là đường dẫn thư mục (chuỗi) hoặc null")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{CONFIG_KEY} chứa ký tự không hợp lệ")
    if os.pathsep in value or (sys.platform != "win32" and ";" in value):
        raise ValueError(f"{CONFIG_KEY} chỉ nhận MỘT thư mục (không chứa '{os.pathsep}')")
    p = Path(os.path.expanduser(value))
    if not p.is_absolute():
        raise ValueError(f"{CONFIG_KEY} phải là đường dẫn tuyệt đối")
    if not p.is_dir():
        raise ValueError(f"{CONFIG_KEY}: không có thư mục {value}")
    if _find_in_dir(p) is None:
        raise ValueError(f"{CONFIG_KEY}: thư mục {value} không có {PDFTOPPM}{'.exe' if sys.platform == 'win32' else ''}")


def validate_setting(key: str, value: Any) -> None:
    """Gọi từ `config.validate_setting` cho khoá `tools` / `tools.poppler_path`."""
    if key == "tools":
        if value is None:
            return
        if not isinstance(value, dict):
            raise ValueError("tools phải là object")
        for k, v in value.items():
            if k != "poppler_path":
                raise ValueError(f"tools.{k}: khoá không có")
            validate_poppler_path(v)
    elif key == CONFIG_KEY:
        validate_poppler_path(value)
    elif key.startswith("tools."):
        raise ValueError(f"{key}: khoá không có")


def _configured(cfg: Any) -> Any:
    """Giá trị `tools.poppler_path` từ HubConfig hoặc dict cấu hình đã gộp (thiếu → None)."""
    if cfg is None:
        return None
    try:
        if hasattr(cfg, "get") and not isinstance(cfg, dict):
            return cfg.get(CONFIG_KEY)
        return ((cfg or {}).get("tools") or {}).get("poppler_path")
    except (KeyError, AttributeError, TypeError):
        return None


def poppler_dir(cfg: Any) -> str | None:
    """Thư mục `tools.poppler_path` nếu đặt VÀ còn hợp lệ lúc này; không thì None (giá trị hỏng chỉ log, không chặn run)."""
    v = _configured(cfg)
    if v is None:
        return None
    try:
        validate_poppler_path(v)
    except ValueError as e:
        log.warning("bỏ qua %s: %s", CONFIG_KEY, e)
        return None
    return str(Path(os.path.expanduser(v)))


_cache: dict[tuple[Any, str], tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def reset_cache() -> None:
    with _lock:
        _cache.clear()


def pdftoppm_status(cfg: Any = None, *, ttl_s: float = CACHE_TTL_S) -> dict[str, Any]:
    """`{usable, path, source: "path"|"config"|None, configured}` — có cache theo (giá trị cấu hình, PATH daemon), sống `ttl_s` giây."""
    configured = _configured(cfg)
    env_path = os.environ.get("PATH", "")
    key = (configured if isinstance(configured, (str, type(None))) else repr(configured), env_path)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl_s:
            return dict(hit[1])
    found = shutil.which(PDFTOPPM)
    out: dict[str, Any] = {"usable": bool(found), "path": found, "source": "path" if found else None, "configured": configured}
    if not found:
        d = poppler_dir(cfg)
        exe = _find_in_dir(Path(d)) if d else None
        if exe is not None:
            out.update(usable=True, path=str(exe), source="config")
    with _lock:
        _cache[key] = (now, out)
    return dict(out)


def pdf_render_available(cfg: Any = None) -> bool:
    try:
        return bool(pdftoppm_status(cfg)["usable"])
    except Exception:  # best-effort: dò lỗi → coi như không có (chỉ thêm lời nhắc an toàn)
        log.debug("dò pdftoppm lỗi", exc_info=True)
        return False


def system_note(cfg: Any = None) -> str | None:
    """Khối system prompt cho MỌI run khi máy không có pdftoppm; có thì None."""
    return None if pdf_render_available(cfg) else PDF_SYSTEM_NOTE


def run_path_extra(cfg: Any = None) -> list[str]:
    """Thư mục thêm vào CUỐI PATH của run `claude`: `[tools.poppler_path]` khi hợp lệ, không thì []."""
    d = poppler_dir(cfg)
    return [d] if d else []


def append_path(env: dict[str, str], dirs: list[str]) -> dict[str, str]:
    """Thêm `dirs` vào CUỐI PATH của `env` (giữ đúng tên biến sẵn có — Windows không phân biệt hoa thường), bỏ mục đã có."""
    if not dirs:
        return env
    name = "PATH"
    if sys.platform == "win32":
        name = next((k for k in env if k.upper() == "PATH"), "PATH")
    cur = [x for x in (env.get(name) or "").split(os.pathsep) if x]
    norm = {os.path.normcase(os.path.normpath(x)) for x in cur}
    for d in dirs:
        if d and os.pathsep not in d and "\x00" not in d and os.path.normcase(os.path.normpath(d)) not in norm:
            cur.append(d)
            norm.add(os.path.normcase(os.path.normpath(d)))
    env[name] = os.pathsep.join(cur)
    return env
