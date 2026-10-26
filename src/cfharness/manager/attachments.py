"""Đính kèm khi chat với điều phối viên trên terminal (REPL `cfharness chat`, TUI `top` → `m`). Phía CLIENT, thuần + clipboard.

- Kéo–thả file/thư mục vào terminal: terminal dán đường dẫn đã escape (`/Users/a/My\\ Folder`) hoặc trong nháy. `parse_input` nhận ra
  token là đường dẫn CÓ THẬT → thư mục/file thường giữ trong chữ (đã bỏ escape); file ảnh → tách thành ảnh đính kèm.
- `is_command`: dòng bắt đầu bằng `/` CHỈ là lệnh khi token đầu là tên lệnh đã biết (đường dẫn kéo vào không bị hiểu nhầm là lệnh).
- `clipboard_image()`/`clipboard_text()`: clipboard (macOS `osascript`/`pbpaste`, Linux `wl-paste`/`xclip`, Windows PowerShell
  `-NoProfile -NonInteractive -STA` → PNG qua tệp tạm) → bytes PNG / chữ, hoặc None/"" khi không có, lỗi hay quá giờ.
Ảnh gửi qua RPC `manager.chat{images:[{name, data_b64}]}`; daemon kiểm định dạng/kích thước và lưu vào workspace hội thoại.
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..oscompat import IS_WINDOWS, no_window_kwargs

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 4
_TOKEN = re.compile(r"'[^']*'|\"[^\"]*\"|(?:\\.|[^\s\\'\"])+")
MARKER = "[Ảnh #{n}]"
_MARKER_RE = re.compile(r"\[Ảnh #(\d+)\]")
_WIN_ABS = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/])")  # C:\…, C:/…, \\máy\share\…


@dataclass
class Attachment:
    name: str
    data: bytes

    def wire(self) -> dict[str, str]:
        return {"name": self.name, "data_b64": base64.b64encode(self.data).decode("ascii")}


@dataclass
class Parsed:
    text: str
    images: list[Attachment] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)  # thư mục/file thường nhận ra trong chữ
    errors: list[str] = field(default_factory=list)


def _as_path(tok: str) -> Path | None:
    """Token (có thể escape/nháy) → Path nếu là đường dẫn tuyệt đối/~ đang tồn tại.
    Windows: `\\` là dấu phân cách, không phải escape — terminal kéo vào `C:\\a\\b` hoặc `"C:\\a b\\c"` (nháy khi có dấu cách)."""
    if IS_WINDOWS:
        raw = tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "'\"" else tok
        if not (_WIN_ABS.match(raw) or raw.startswith("~")):
            return None
        try:
            p = Path(raw).expanduser()
            return p if p.exists() else None
        except (OSError, ValueError):
            return None
    try:
        parts = shlex.split(tok)
    except ValueError:
        return None
    if len(parts) != 1:
        return None
    raw = parts[0]
    if raw.startswith("file://"):
        raw = raw[len("file://"):]
    if not (raw.startswith("/") or raw.startswith("~")):
        return None
    try:
        p = Path(raw).expanduser()
        return p if p.exists() else None
    except (OSError, ValueError):
        return None


def first_token_is_path(s: str) -> bool:
    m = _TOKEN.match(s.strip())
    return bool(m and _as_path(m.group(0)))


def is_command(s: str, commands: set[str]) -> bool:
    """`/new`, `/open x`… là lệnh; `/Users/a/b`, `/tmp/x.png` (kéo vào) hay `/a/b` là chữ."""
    s = s.strip()
    if not s.startswith("/") or s.startswith("//"):
        return False
    head = s.split(None, 1)[0].lower()
    if head in commands:
        return True
    return not ("/" in head[1:] or first_token_is_path(s))


def read_image(p: Path) -> tuple[Attachment | None, str | None]:
    try:
        size = p.stat().st_size
        if size > MAX_IMAGE_BYTES:
            return None, f"{p.name}: ảnh quá lớn ({size // 1024} KB > {MAX_IMAGE_BYTES // 1024 // 1024} MB)"
        return Attachment(p.name, p.read_bytes()), None
    except OSError as e:
        return None, f"{p.name}: không đọc được ({e.strerror or e})"


def parse_input(text: str, pasted: dict[int, Attachment] | None = None) -> Parsed:
    """Tách ảnh (file kéo vào + ảnh dán `[Ảnh #n]`) khỏi chữ; đường dẫn thường được bỏ escape cho dễ đọc."""
    out = Parsed(text="")
    pieces: list[str] = []
    last = 0
    for m in _TOKEN.finditer(text):
        tok = m.group(0)
        p = _as_path(tok)
        repl = tok
        if p is not None:
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES:
                att, err = read_image(p)
                if att:
                    out.images.append(att)
                    repl = f"[ảnh: {p.name}]"
                else:
                    out.errors.append(err or p.name)
            else:
                s = str(p)
                out.paths.append(s)
                repl = f'"{s}"' if any(c.isspace() for c in s) else s
        pieces.append(text[last:m.start()] + repl)
        last = m.end()
    pieces.append(text[last:])
    body = "".join(pieces)
    if pasted:
        def _sub(m: re.Match[str]) -> str:
            att = pasted.get(int(m.group(1)))
            if att is None:
                return m.group(0)
            out.images.append(att)
            return f"[ảnh: {att.name}]"

        body = _MARKER_RE.sub(_sub, body)
    if len(out.images) > MAX_IMAGES:
        out.errors.append(f"tối đa {MAX_IMAGES} ảnh mỗi tin — bỏ {len(out.images) - MAX_IMAGES} ảnh cuối")
        out.images = out.images[:MAX_IMAGES]
    out.text = body.strip()
    return out


# ---------------------------------------------------------------------- clipboard
CLIP_TIMEOUT_S = 5.0
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
CLIP_READ_CAP = 64 * 1024 * 1024  # ảnh clipboard > trần này → bỏ (không đọc vào RAM); > MAX_IMAGE_BYTES vẫn trả để REPL/TUI báo "quá lớn"

# Windows: PowerShell (STA — clipboard WinForms cần STA). `Get-Clipboard -Format Image` chỉ có ở Windows PowerShell 5.1;
# pwsh 7 → rơi về System.Windows.Forms.Clipboard. Đường dẫn tệp tạm truyền qua biến môi trường (không ghép vào mã lệnh).
_PS_IMAGE = r"""
$ErrorActionPreference = 'Stop'
$img = $null
try { $img = Get-Clipboard -Format Image } catch { $img = $null }
if (-not $img) {
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing
  if ([System.Windows.Forms.Clipboard]::ContainsImage()) { $img = [System.Windows.Forms.Clipboard]::GetImage() }
}
if (-not $img) { exit 3 }
$img.Save($env:CFHARNESS_CLIP_OUT, [System.Drawing.Imaging.ImageFormat]::Png)
$img.Dispose()
exit 0
"""
_PS_TEXT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$t = Get-Clipboard -Raw
if ($null -eq $t) { exit 3 }
[Console]::Out.Write([string]$t)
exit 0
"""


def _run(argv: list[str], timeout: float = CLIP_TIMEOUT_S, env: dict[str, str] | None = None) -> bytes | None:
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout, check=False, env=env, stdin=subprocess.DEVNULL, **no_window_kwargs())
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return r.stdout if r.returncode == 0 and r.stdout else None


def _powershell() -> str | None:
    """Windows PowerShell 5.1 (luôn có, mặc định STA) trước; pwsh 7 nếu không có."""
    return shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh.exe") or shutil.which("pwsh")


def _ps_argv(exe: str, script: str) -> list[str]:
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")  # -EncodedCommand: không lo nháy/escape
    return [exe, "-NoProfile", "-NonInteractive", "-NoLogo", "-STA", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc]


def _win_clipboard_image() -> bytes | None:
    exe = _powershell()
    if not exe:
        return None
    fd, tmp = tempfile.mkstemp(prefix="cfharness-clip-", suffix=".png")
    os.close(fd)
    try:
        env = dict(os.environ, CFHARNESS_CLIP_OUT=tmp)
        try:
            r = subprocess.run(_ps_argv(exe, _PS_IMAGE), capture_output=True, timeout=CLIP_TIMEOUT_S, check=False, env=env,
                               stdin=subprocess.DEVNULL, **no_window_kwargs())
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        if r.returncode != 0:
            return None
        try:
            size = os.path.getsize(tmp)
            if size <= 0 or size > CLIP_READ_CAP:
                return None
            with open(tmp, "rb") as f:
                data = f.read(CLIP_READ_CAP + 1)
        except OSError:
            return None
        return data if data.startswith(_PNG_MAGIC) else None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _win_clipboard_text() -> bytes | None:
    exe = _powershell()
    return _run(_ps_argv(exe, _PS_TEXT)) if exe else None


def clipboard_image() -> bytes | None:
    """Ảnh trong clipboard → bytes (PNG), None nếu không có ảnh/không hỗ trợ/lỗi/quá giờ."""
    if IS_WINDOWS:
        return _win_clipboard_image()
    if sys.platform == "darwin":
        out = _run(["osascript", "-e", "the clipboard as «class PNGf»"])
        if not out:
            return None
        m = re.search(r"data PNGf([0-9A-Fa-f]+)", out.decode("utf-8", "replace"))
        if not m:
            return None
        try:
            return bytes.fromhex(m.group(1))
        except ValueError:
            return None
    if shutil.which("wl-paste"):
        return _run(["wl-paste", "--no-newline", "--type", "image/png"])
    if shutil.which("xclip"):
        return _run(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"])
    return None


def clipboard_text() -> str:
    if IS_WINDOWS:
        out = _win_clipboard_text()
        return out.decode("utf-8", "replace").lstrip("﻿").replace("\r\n", "\n") if out else ""
    if sys.platform == "darwin":
        out = _run(["pbpaste"])
    elif shutil.which("wl-paste"):
        out = _run(["wl-paste", "--no-newline"])
    elif shutil.which("xclip"):
        out = _run(["xclip", "-selection", "clipboard", "-o"])
    else:
        out = None
    return out.decode("utf-8", "replace") if out else ""
