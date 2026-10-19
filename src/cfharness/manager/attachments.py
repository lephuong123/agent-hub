"""Đính kèm khi chat với điều phối viên trên terminal (REPL `cfharness chat`, TUI `top` → `m`). Phía CLIENT, thuần + clipboard.

- Kéo–thả file/thư mục vào terminal: terminal dán đường dẫn đã escape (`/Users/a/My\\ Folder`) hoặc trong nháy. `parse_input` nhận ra
  token là đường dẫn CÓ THẬT → thư mục/file thường giữ trong chữ (đã bỏ escape); file ảnh → tách thành ảnh đính kèm.
- `is_command`: dòng bắt đầu bằng `/` CHỈ là lệnh khi token đầu là tên lệnh đã biết (đường dẫn kéo vào không bị hiểu nhầm là lệnh).
- `clipboard_image()`: ảnh trong clipboard (macOS `osascript`, Linux `wl-paste`/`xclip`) → bytes PNG, hoặc None.
Ảnh gửi qua RPC `manager.chat{images:[{name, data_b64}]}`; daemon kiểm định dạng/kích thước và lưu vào workspace hội thoại.
"""

from __future__ import annotations

import base64
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 4
_TOKEN = re.compile(r"'[^']*'|\"[^\"]*\"|(?:\\.|[^\s\\'\"])+")
MARKER = "[Ảnh #{n}]"
_MARKER_RE = re.compile(r"\[Ảnh #(\d+)\]")


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
    """Token (có thể escape/nháy) → Path nếu là đường dẫn tuyệt đối/~ đang tồn tại."""
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
def _run(argv: list[str], timeout: float = 5.0) -> bytes | None:
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 and r.stdout else None


def clipboard_image() -> bytes | None:
    """Ảnh trong clipboard → bytes (PNG), None nếu không có ảnh/không hỗ trợ."""
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
    if sys.platform == "darwin":
        out = _run(["pbpaste"])
    elif shutil.which("wl-paste"):
        out = _run(["wl-paste", "--no-newline"])
    elif shutil.which("xclip"):
        out = _run(["xclip", "-selection", "clipboard", "-o"])
    else:
        out = None
    return out.decode("utf-8", "replace") if out else ""
