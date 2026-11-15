"""Thư mục dữ liệu của app desktop cfharness (hợp đồng C6 — docs/dev-notes/desktop-app.md, PLAN app §5.6 / H5).

App (Tauri, identifier `io.cfharness.desktop`) chạy cùng user OS với agent của hub local. Thư mục data của app giữ cookie phiên WebView
của MỌI hub đã thêm, token directory, danh sách hub → agent KHÔNG được đọc/ghi. Chặn ở 3 lớp (guard/runtime.py + guard/classifier.py):
sandbox `denyRead`/`denyWrite`, `permissions.deny` cho Read/Grep/Glob/Edit/Write…, và classifier (rule `desktop_app_data`, forbidden).
Lớp `permissions.deny` áp cho MỌI hub kể cả agent trust_full (dữ liệu đăng nhập của app không phải quyền của hub để trao).

Thư mục (hàm THUẦN theo `home`/env — test truyền giá trị giả):
- macOS: `~/Library/Application Support/io.cfharness.desktop` (Tauri app_data_dir) + nơi WKWebView giữ cookie/bộ nhớ của app:
  `~/Library/WebKit/io.cfharness.desktop`, `~/Library/HTTPStorages/io.cfharness.desktop`, `~/Library/Caches/io.cfharness.desktop`.
- Windows: `%APPDATA%\\io.cfharness.desktop` (app_data_dir) + `%LOCALAPPDATA%\\io.cfharness.desktop` (dữ liệu WebView2 `EBWebView`).
- Linux: `~/.local/share/io.cfharness.desktop` (+ `$XDG_DATA_HOME`), `~/.cache/io.cfharness.desktop`, `~/.config/io.cfharness.desktop`.
Không phụ thuộc nền tảng đang chạy: mọi biến thể đều được liệt kê (thừa thì vô hại), riêng Windows chỉ thêm khi biết `%APPDATA%`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

APP_ID = "io.cfharness.desktop"
REASON = "agent không được đọc/ghi thư mục dữ liệu của app desktop cfharness (cookie phiên, token directory của mọi hub)"
RULE = "desktop_app_data"

# Lệnh shell nhắc tới thư mục app qua dạng đường dẫn phổ biến (kể cả biến môi trường chưa mở rộng). KHÔNG chặn chuỗi `io.cfharness.desktop`
# đứng một mình (vd agent làm việc trên repo app grep identifier trong tauri.conf.json).
SHELL_RE = re.compile(
    r"(?i)(application[\s\\\"']*support|appdata[/\\]+(?:roaming|local)|%(?:local)?appdata%|\$\{?(?:env:)?(?:local)?appdata\}?"
    r"|\.local[/\\]+share|\$\{?xdg_(?:data|config|cache)_home\}?|\.cache|\.config"
    r"|library[/\\]+(?:webkit|httpstorages|caches|cookies|preferences|saved\s*application\s*state))[/\\]+"
    + re.escape(APP_ID) + r"(?![A-Za-z0-9_\-])"
)


def app_data_dirs(home: str | Path | None = None, env: dict[str, str] | None = None) -> list[str]:
    """Mọi thư mục dữ liệu của app (đường dẫn tuyệt đối, không trùng)."""
    h = str(home if home is not None else os.path.expanduser("~")).rstrip("/\\")
    e = os.environ if env is None else env
    out = [f"{h}/Library/Application Support/{APP_ID}", f"{h}/Library/WebKit/{APP_ID}", f"{h}/Library/HTTPStorages/{APP_ID}",
           f"{h}/Library/Caches/{APP_ID}", f"{h}/.local/share/{APP_ID}", f"{h}/.cache/{APP_ID}", f"{h}/.config/{APP_ID}"]
    xdg = e.get("XDG_DATA_HOME")
    if xdg and os.path.isabs(xdg):
        out.append(f"{xdg.rstrip('/')}/{APP_ID}")
    for var, default in (("APPDATA", "AppData/Roaming"), ("LOCALAPPDATA", "AppData/Local")):
        v = e.get(var)
        base = v.replace("\\", "/").rstrip("/") if v else f"{h}/{default}"
        out.append(f"{base}/{APP_ID}")
    return list(dict.fromkeys(out))


def _canon(p: str) -> str:
    return p.replace("\\", "/").rstrip("/").casefold()


def is_app_data(path: str, home: str | Path | None = None, env: dict[str, str] | None = None) -> bool:
    """`path` (tuyệt đối) nằm trong (hoặc là) một thư mục dữ liệu của app. So sánh không phân biệt hoa/thường (APFS/NTFS) và theo cả realpath."""
    cands = {_canon(path)}
    try:
        cands.add(_canon(os.path.realpath(path)))
    except (OSError, ValueError):
        pass
    roots = set()
    for r in app_data_dirs(home, env):
        roots.add(_canon(r))
        try:
            roots.add(_canon(os.path.realpath(r)))
        except (OSError, ValueError):
            pass
    return any(c == r or c.startswith(r + "/") for c in cands for r in roots)


_BRACE_RE = re.compile(r"\{[^{}]*\}")


def glob_may_touch(pattern: str, home: str | Path | None = None, env: dict[str, str] | None = None) -> bool:
    """Mẫu Glob TUYỆT ĐỐI có thể khớp tệp BÊN TRONG một thư mục dữ liệu của app (kể cả khi tên thư mục app bị viết bằng ký tự đại diện:
    `…/io.cfharness.deskto?/**`, `…/[i]o.cfharness.desktop/*`, `…/{io,x}.cfharness.desktop/**`). So từng thành phần đường dẫn bằng fnmatch,
    không phân biệt hoa/thường; `**` hoặc `{…}` (coi là `*`) trước khi hết đường dẫn thư mục app → coi là khớp (thà chặn nhầm)."""
    import fnmatch

    psegs = _canon(pattern).split("/")
    for root in app_data_dirs(home, env):
        rsegs = _canon(root).split("/")
        for i, rs in enumerate(rsegs):
            if i >= len(psegs):
                break
            ps = _BRACE_RE.sub("*", psegs[i])
            if "**" in ps:
                return True
            if not fnmatch.fnmatchcase(rs, ps):
                break
        else:
            if len(psegs) > len(rsegs):
                return True
    return False


# lệnh tách đôi (vd `cd ~/Library/Application\ Support && cat io.cfharness.desktop/…`): có identifier VÀ có gốc thư mục app ở đâu đó
_ID_RE = re.compile(r"(?<![A-Za-z0-9_\-])" + re.escape(APP_ID) + r"(?![A-Za-z0-9_\-])", re.I)
_BASE_RE = re.compile(r"(?i)application[\s\\\"']*support|appdata|library[/\\]+(?:webkit|httpstorages|caches|cookies)|\.local[/\\]+share"
                      r"|xdg_(?:data|config|cache)_home|(?:~|home\}?)[/\\]+\.(?:cache|config)\b")


def shell_mentions(script: str) -> bool:
    s = script or ""
    return bool(SHELL_RE.search(s)) or (bool(_ID_RE.search(s)) and bool(_BASE_RE.search(s)))


__all__ = ["APP_ID", "REASON", "RULE", "SHELL_RE", "app_data_dirs", "glob_may_touch", "is_app_data", "shell_mentions"]
