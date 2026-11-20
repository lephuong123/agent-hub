"""Poppler (`pdftoppm`) cho tool Read của Claude Code — dò, kiểm cấu hình `tools.poppler_path`, thêm vào PATH của run.

Sự thật đã kiểm (claude 2.1.329, `docs/dev-notes/pdf-attachments.md`): Read đọc PDF chỉ với `file_path` → khối document gốc, KHÔNG cần
poppler. Model truyền `pages` (Haiku tự làm kể cả với PDF 2 trang) → Read gọi `pdftoppm` để vẽ trang; máy không có poppler → lỗi
"pdftoppm is not installed". Khung quyền strict chỉ có Read nên agent không tự xoay được → hub:
- `pdftoppm_status(cfg)`: dò `pdftoppm` (PATH của daemon + `tools.poppler_path`) và CHẠY thử `pdftoppm -v` (`probe`, cùng quy tắc với
  Claude Code), CACHE theo (giá trị cấu hình, PATH) + TTL — không gọi tiến trình con mỗi tin; đổi `tools.poppler_path` → dò lại ngay.
  PATH là của daemon lúc khởi động: cài poppler khi daemon đang chạy → cần khởi động lại daemon (hoặc đặt `tools.poppler_path`).
- Không dùng được → `PDF_SYSTEM_NOTE` vào system prompt mọi run (`prompt.build_system_prompt(pdf_note=…)`) + ghi chú trong `files_note`.
- `tools.poppler_path` hợp lệ → `run_path_extra(cfg)` trả thư mục đó (+ thư mục pdftoppm tìm thấy trên PATH daemon, phòng env_extra của
  run thay PATH); `ClaudeCliRuntime._run_env` thêm vào CUỐI PATH của tiến trình
  `claude` (thêm cuối, không chèn đầu: thư mục poppler không che được `git`/`python`… sẵn có trên PATH — chỉ thêm lệnh còn thiếu).
- Kiểm giá trị (`validate_poppler_path`): đường dẫn TUYỆT ĐỐI tới thư mục có sẵn, chứa `pdftoppm`/`pdftoppm.exe` là file thường; không
  NUL, không ký tự ngăn cách PATH (`;`/`:`) — một giá trị không thể chèn nhiều mục PATH. Kiểm lại lúc dùng (thư mục bị xoá → bỏ qua).
Không dựa vào binary của Git Bash (có `pdftotext` nhưng không có `pdftoppm`).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
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
            "`apt install poppler-utils` (Linux) rồi khởi động lại daemon (daemon giữ PATH lúc khởi động); hoặc "
            "`cfharness config set tools.poppler_path <thư mục chứa pdftoppm>` (có hiệu lực ngay)")
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


PROBE_TIMEOUT_S = 5.0


def probe(exe: str) -> bool:
    """`pdftoppm -v` chạy được? Cùng quy tắc với Claude Code (claude 2.1.329): mã 0, hoặc mã khác 127 VÀ có in ra stderr. File tồn tại
    mà hỏng/sai kiến trúc/không phải pdftoppm → False (không thì hub bỏ lời nhắc "không truyền pages" trong khi Read vẫn lỗi)."""
    from .oscompat import no_window_kwargs

    try:
        r = subprocess.run([exe, "-v"], capture_output=True, timeout=PROBE_TIMEOUT_S, stdin=subprocess.DEVNULL, **no_window_kwargs())
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return r.returncode == 0 or (r.returncode != 127 and bool((r.stderr or b"").strip()))


def _in_event_loop() -> bool:
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _compute(configured: Any) -> dict[str, Any]:
    """Phần CHẬM của `pdftoppm_status` (which + chạy `pdftoppm -v`, tới `PROBE_TIMEOUT_S`); chỉ nhận giá trị cấu hình (an toàn trong thread)."""
    out: dict[str, Any] = {"usable": False, "path": None, "source": None, "configured": configured, "broken": None}
    found = shutil.which(PDFTOPPM)
    if found:
        if probe(found):
            out.update(usable=True, path=found, source="path")
        else:
            out["broken"] = found
    if not out["usable"]:
        d = poppler_dir({"tools": {"poppler_path": configured}})
        exe = _find_in_dir(Path(d)) if d else None
        if exe is not None:
            if probe(str(exe)):
                out.update(usable=True, path=str(exe), source="config")
            else:
                out["broken"] = str(exe)
    return out


_refreshing: set[tuple[Any, str]] = set()


def _refresh_bg(key: tuple[Any, str], configured: Any) -> None:
    """Dò lại trong thread nền (một lần mỗi khoá cùng lúc) — gọi từ event loop không bao giờ chờ `pdftoppm -v`."""
    with _lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def run() -> None:
        try:
            st = _compute(configured)
            with _lock:
                _cache[key] = (time.monotonic(), st)
        except Exception:  # pragma: no cover - best-effort
            log.debug("dò pdftoppm (nền) lỗi", exc_info=True)
        finally:
            with _lock:
                _refreshing.discard(key)

    threading.Thread(target=run, name="pdftoppm-probe", daemon=True).start()


def pdftoppm_status(cfg: Any = None, *, ttl_s: float = CACHE_TTL_S, block: bool | None = None) -> dict[str, Any]:
    """`{usable, path, source: "path"|"config"|None, configured, broken}` — có cache theo (giá trị cấu hình, PATH daemon), sống `ttl_s`
    giây (cache = không chạy `pdftoppm -v` mỗi tin/mỗi run). `broken` = đường dẫn pdftoppm tìm thấy nhưng `probe` hỏng.
    `block` (mặc định: True khi KHÔNG ở trong event loop): False → không bao giờ chạy tiến trình con tại chỗ — cache hết hạn trả giá trị
    cũ và dò lại trong thread nền; chưa có cache trả `usable=False, pending=True` (an toàn: chỉ thêm lời nhắc "không truyền pages")
    rồi dò nền. Gọi từ code async muốn kết quả chắc chắn → `await asyncio.to_thread(pdftoppm_status, cfg)`.
    PATH là PATH của DAEMON lúc khởi động: cài poppler khi daemon đang chạy → daemon chỉ thấy sau khi khởi động lại (hoặc đặt
    `tools.poppler_path`, có hiệu lực ngay)."""
    configured = _configured(cfg)
    env_path = os.environ.get("PATH", "")
    key = (configured if isinstance(configured, (str, type(None))) else repr(configured), env_path)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < ttl_s:
        return dict(hit[1])
    if block is None:
        block = not _in_event_loop()
    if not block:
        _refresh_bg(key, configured)
        if hit:
            return dict(hit[1])
        return {"usable": False, "path": None, "source": None, "configured": configured, "broken": None, "pending": True}
    out = _compute(configured)
    with _lock:
        _cache[key] = (time.monotonic(), out)
    return dict(out)


def warm(cfg: Any = None) -> None:
    """Dò sẵn trong thread nền lúc daemon khởi động (lượt chat/run đầu tiên đã có kết quả thật, không phải `pending`)."""
    configured = _configured(cfg)
    _refresh_bg((configured if isinstance(configured, (str, type(None))) else repr(configured), os.environ.get("PATH", "")), configured)


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
    """Thư mục thêm vào CUỐI PATH của run `claude`: `tools.poppler_path` khi hợp lệ; pdftoppm dùng được thấy trên PATH của daemon →
    thêm cả thư mục đó (env_extra của run có thể thay PATH và làm mất nó trong khi hub đã bỏ lời nhắc "không truyền pages")."""
    out: list[str] = []
    d = poppler_dir(cfg)
    if d:
        out.append(d)
    try:
        st = pdftoppm_status(cfg)
    except Exception:  # pragma: no cover - best-effort
        st = {}
    if st.get("usable") and st.get("source") == "path" and st.get("path"):
        p = str(Path(st["path"]).parent)
        if p not in out:
            out.append(p)
    return out


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
