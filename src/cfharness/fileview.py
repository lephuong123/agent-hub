"""Xem / mở file mà agent đã thao tác (web: chip file trong diễn biến run và "Thay đổi file") — docs/dev-notes/file-open.md.

Daemon là nơi DUY NHẤT đọc file và mở ứng dụng; tiến trình web chỉ gọi RPC (web là client thuần — web.md §1):
- `file.read {run_id|agent_id, path, max_bytes?, whole?}` — CHỈ ĐỌC, ∈ HUMAN_ONLY (agent gọi qua socket sẽ đọc được workspace của
  agent KHÁC / ngoài sandbox của mình → không cho). Trả nội dung base64 (≤ `max_bytes`, trần `RAW_MAX`) + loại (text/image/pdf/binary).
- `file.open {run_id|agent_id, path, reveal?}` — mở bằng ứng dụng mặc định của OS (hoặc mở thư mục chứa file). Không đổi dữ liệu
  hub nhưng là TÁC DỤNG PHỤ trên máy (chạy một tiến trình ngoài sandbox) → ∈ HUMAN_ONLY + `MUTATING_FILES` (phân loại quyền như method
  ghi) + tự bọc audit. Chỉ đuôi trong `OPEN_EXTS` (tài liệu/ảnh/văn bản thuần) — script/chương trình/mã nguồn KHÔNG mở bằng app mặc
  định (Windows mở `.js/.vbs/.py` = CHẠY), chỉ được "mở thư mục".

Gốc được phép do DAEMON tự suy ra từ run/agent (không bao giờ nhận gốc từ client):
- run: `runs.cwd` (thư mục làm việc thật lúc spawn) + `diff.root` (gốc repo git của checkpoint);
- agent: `workspace_default` + `<home agent>/workspaces/` (hội thoại điều phối viên, scratch).
Đường dẫn (tuyệt đối hoặc tương đối theo gốc) → `realpath` (đi hết symlink) phải nằm TRONG một gốc; từ chối UNC/thiết bị/ADS,
nội bộ HUB_HOME (trừ `agents/*/workspaces/**`, `worktrees/**`), token người, và tên file bí mật (`SECRET_NAMES`).
"""

from __future__ import annotations

import base64
import mimetypes
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .errors import Blocked, HubError, NotFound
from .oscompat import IS_WINDOWS, no_window_kwargs

if TYPE_CHECKING:
    from .daemon import Daemon
    from .hub import Hub

MUTATING_FILES = ("file.open",)
VIEW_MAX = 1_000_000  # trình xem văn bản: đọc tối đa 1 MB (phần sau báo "đã cắt")
RAW_MAX = 10_000_000  # tải về / ảnh / PDF: toàn bộ file, tối đa 10 MB (base64 qua RPC)
PATH_MAX = 4096

IMAGE_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
              ".bmp": "image/bmp", ".ico": "image/x-icon", ".svg": "image/svg+xml", ".avif": "image/avif"}
PDF_EXTS = {".pdf"}
# Mở bằng app mặc định: DANH SÁCH CHO PHÉP (an toàn hơn danh sách cấm — đuôi lạ có thể gắn với trình thông dịch). Không có: mã nguồn
# chạy được (.py .js .sh .ps1 .rb .pl .php …), .html/.htm/.svg (trình duyệt chạy JS từ file://), Office có macro (.docm .xlsm …,
# .doc/.xls/.ppt cũ), mọi chương trình/lối tắt (.exe .bat .cmd .com .msi .lnk .scr .hta .reg .app .jar …).
OPEN_EXTS = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".diff", ".patch",
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".avif",
    ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".rtf", ".pages", ".numbers", ".key",
    ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".mp4", ".mov", ".webm", ".mkv", ".avi",
    ".zip",
})
_SECRET_RE = re.compile(
    r"^(\.env(\..+)?|\.netrc|_netrc|\.npmrc|\.pypirc|\.git-credentials|\.pgpass|id_(rsa|dsa|ecdsa|ed25519)(_sk)?|"
    r".+\.(pem|key|p12|pfx|jks|keystore|kdbx|ppk)|credentials(\.json)?|.*secrets?\.(json|ya?ml|toml|env))$", re.I)
_SECRET_OK = re.compile(r"^\.env\.(example|sample|template|dist|defaults?)$", re.I)
_WIN_DEVICES = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$)(\..*)?$", re.I)


def secret_name(name: str) -> bool:
    """Tên file thường chứa bí mật (.env, khoá riêng, credentials…) → không cho xem/mở qua web."""
    n = name.strip()
    if _SECRET_OK.match(n):
        return False
    if n.endswith(".pub"):  # khoá CÔNG KHAI ssh
        return False
    return bool(_SECRET_RE.match(n))


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p))


def _within(child: str, root: str) -> bool:
    c, r = _norm(child), _norm(root)
    if c == r:
        return True
    return c.startswith(r.rstrip("\\/") + os.sep)


def check_raw_path(path: Any) -> str:
    """Kiểm dạng chuỗi đường dẫn TRƯỚC khi chạm hệ thống file: kiểu, độ dài, NUL, UNC/thiết bị/ADS. Trả chuỗi đã strip."""
    if not isinstance(path, str) or not path.strip():
        raise HubError("thiếu tham số 'path'", code=-32602)
    s = path.strip()
    if len(s) > PATH_MAX or "\x00" in s or any(ord(ch) < 0x20 for ch in s):
        raise Blocked("đường dẫn không hợp lệ")
    if s.startswith(("\\\\", "//")) or s.lower().startswith(("\\\\?\\", "\\\\.\\", "//?/", "//./")):
        raise Blocked("không mở đường dẫn mạng/thiết bị (UNC)")
    if IS_WINDOWS or re.match(r"^[A-Za-z]:", s):
        rest = s[2:] if re.match(r"^[A-Za-z]:", s) else s
        if ":" in rest:  # luồng dữ liệu phụ NTFS (file.txt:secret) / tiền tố lạ
            raise Blocked("đường dẫn không hợp lệ (có ':' — luồng dữ liệu phụ)")
    for part in re.split(r"[\\/]+", s):
        if part and _WIN_DEVICES.match(part):
            raise Blocked(f"đường dẫn trỏ tới tên thiết bị ({part})")
    return s


def _hub_forbidden(real: str, hub_home: Path, socket: Path | None) -> str | None:
    """Lý do từ chối nếu `real` nằm trong nội bộ HUB_HOME (trừ workspace agent + worktree) hoặc là token/socket."""
    from .auth import token_path

    for hh in dict.fromkeys([str(hub_home), os.path.realpath(hub_home)]):
        if _within(real, hh):
            rel = [x for x in re.split(r"[\\/]+", os.path.relpath(real, hh)) if x]
            low = [x.lower() for x in rel] if IS_WINDOWS else rel
            if len(low) >= 3 and low[0] == "agents" and low[2] == "workspaces":
                continue
            if low and low[0] == "worktrees":
                continue
            return "dữ liệu nội bộ của hub (HUB_HOME)"
    if socket is not None:
        for t in (socket, token_path(socket)):
            for cand in dict.fromkeys([str(t), os.path.realpath(t)]):
                if _norm(real) == _norm(cand):
                    return "token/socket của hub"
    return None


def resolve_in_roots(path: str, roots: list[str], *, hub_home: Path, socket: Path | None = None) -> tuple[str, str]:
    """`path` (tuyệt đối hoặc tương đối theo một gốc) → (realpath, gốc chứa nó). Từ chối nếu ngoài mọi gốc / nội bộ hub / bí mật.

    Thuần về logic (chỉ đọc hệ thống file) — test trực tiếp. Gốc nào không tồn tại thì bỏ qua."""
    s = check_raw_path(path)
    real_roots = []
    for r in roots:
        if not r:
            continue
        try:
            rr = os.path.realpath(os.path.expanduser(str(r)))
        except (OSError, ValueError):
            continue
        if not os.path.isdir(rr):
            continue
        if Path(rr).anchor and _norm(rr) == _norm(Path(rr).anchor):  # gốc ổ đĩa / "/" — quá rộng, không bao giờ là workspace
            continue
        real_roots.append(rr)
    real_roots = list(dict.fromkeys(real_roots))
    if not real_roots:
        raise NotFound("không xác định được thư mục làm việc của run/agent này")
    p = Path(os.path.expanduser(s))
    cands = [str(p)] if p.is_absolute() else [os.path.join(r, s) for r in real_roots]
    last_err: HubError | None = None
    for c in cands:
        try:
            real = os.path.realpath(c)
        except (OSError, ValueError):
            continue
        root = next((r for r in real_roots if _within(real, r)), None)
        if root is None:
            last_err = Blocked("file nằm ngoài thư mục làm việc của run/agent — không xem/mở được")
            continue
        why = _hub_forbidden(real, hub_home, socket)
        if why:
            raise Blocked(f"không xem/mở được {why}")
        if secret_name(os.path.basename(real)):
            raise Blocked(f"file có thể chứa bí mật ({os.path.basename(real)}) — không xem/mở qua web; mở trực tiếp trên máy nếu cần")
        if not os.path.exists(real):
            last_err = NotFound(f"file không còn tồn tại: {s}")
            continue
        return real, root
    raise last_err or NotFound(f"không tìm thấy file: {s}")


def kind_of(name: str, head: bytes) -> tuple[str, str]:
    """(loại, mime) — `text` | `image` | `pdf` | `binary`. Văn bản = UTF-8 hợp lệ (cho phép cắt giữa ký tự ở cuối), không có NUL."""
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXTS:
        return "image", IMAGE_EXTS[ext]
    if ext in PDF_EXTS or head.startswith(b"%PDF-"):
        return "pdf", "application/pdf"
    sample = head[:65536]
    if b"\x00" not in sample:
        try:
            sample.decode("utf-8")
            return "text", "text/plain; charset=utf-8"
        except UnicodeDecodeError as e:
            if e.start >= len(sample) - 4:  # ký tự nhiều byte bị cắt ở cuối mẫu
                return "text", "text/plain; charset=utf-8"
    return "binary", mimetypes.guess_type(name)[0] or "application/octet-stream"


def read_file(real: str, *, max_bytes: int, whole: bool) -> dict[str, Any]:
    """Đọc ≤ max_bytes. `whole` (tải về/ảnh/PDF): file lớn hơn → lỗi thay vì cắt (không trả file hỏng)."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(real, flags)
    except OSError as e:
        raise HubError(f"không đọc được file: {e.strerror or e}") from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        import stat as _stat

        if not _stat.S_ISREG(st.st_mode):
            raise Blocked("chỉ xem được file thường (không phải thư mục/thiết bị/FIFO)")
        if whole and st.st_size > max_bytes:
            raise HubError(f"file quá lớn để tải qua web ({st.st_size:,} byte > {max_bytes:,})", code=-32001)
        data = f.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    data = data[:max_bytes]
    kind, mime = kind_of(real, data)
    return {"size": st.st_size, "mtime": st.st_mtime, "kind": kind, "mime": mime, "truncated": truncated,
            "data_b64": base64.b64encode(data).decode("ascii")}


def open_command(real: str, *, reveal: bool) -> list[str] | None:
    """Lệnh mở theo OS (None = Windows mở bằng `os.startfile`). Tách riêng để test không chạy app thật."""
    if IS_WINDOWS:
        return ["explorer.exe", f"/select,{real}"] if reveal else None
    if sys.platform == "darwin":
        return ["open", "-R", real] if reveal else ["open", real]
    return ["xdg-open", os.path.dirname(real) if reveal else real]


def _launch(real: str, reveal: bool) -> None:  # pragma: no cover — chạy app thật; test thay bằng hàm giả (`OPENER`)
    cmd = open_command(real, reveal=reveal)
    if cmd is None:
        os.startfile(real, "open")  # type: ignore[attr-defined]  # ShellExecute "open" — chỉ đuôi trong OPEN_EXTS
        return
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                     start_new_session=not IS_WINDOWS, **no_window_kwargs())


OPENER: Callable[[str, bool], None] = _launch  # test gán lại để không bao giờ mở ứng dụng thật


def can_open(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in OPEN_EXTS


class FileAccess:
    """Suy ra gốc được phép theo run/agent (từ DB của hub) rồi đọc/mở trong đó."""

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub

    def roots(self, p: dict[str, Any]) -> list[str]:
        store = self.hub.store
        rid, aid = p.get("run_id"), p.get("agent_id")
        out: list[str] = []
        if rid:
            run = store.find_run(str(rid))
            if run is None:
                raise NotFound(f"không tìm thấy run '{rid}'")
            if run.get("cwd"):
                out.append(str(run["cwd"]))
            diff = run.get("diff")
            if isinstance(diff, dict) and diff.get("root"):
                out.append(str(diff["root"]))
            if not out:  # run chưa spawn (chưa có cwd): theo agent của run
                aid = run.get("agent_id")
        if aid and not out:
            ag = store.get_agent(str(aid))
            if ag is None:
                raise NotFound(f"không tìm thấy agent '{aid}'")
            if ag.get("workspace_default"):
                out.append(str(ag["workspace_default"]))
            if ag.get("home_path"):
                out.append(str(Path(ag["home_path"]) / "workspaces"))
        if not rid and not aid:
            raise HubError("cần run_id hoặc agent_id", code=-32602)
        return out

    def resolve(self, p: dict[str, Any]) -> tuple[str, str]:
        return resolve_in_roots(p.get("path"), self.roots(p), hub_home=self.hub.paths.home, socket=self.hub.paths.socket)

    def read(self, p: dict[str, Any]) -> dict[str, Any]:
        real, root = self.resolve(p)
        whole = bool(p.get("whole"))
        try:
            mb = int(p.get("max_bytes") or (RAW_MAX if whole else VIEW_MAX))
        except (TypeError, ValueError):
            mb = VIEW_MAX
        mb = max(1, min(mb, RAW_MAX))
        out = read_file(real, max_bytes=mb, whole=whole)
        return {"path": real, "root": root, "rel": os.path.relpath(real, root).replace("\\", "/"), "name": os.path.basename(real),
                "can_open": can_open(real), **out}

    def open(self, p: dict[str, Any]) -> dict[str, Any]:
        real, root = self.resolve(p)
        reveal = bool(p.get("reveal"))
        if not os.path.isfile(real):
            raise Blocked("chỉ mở được file thường")
        if not reveal and not can_open(real):
            raise Blocked(f"không mở trực tiếp loại file '{os.path.splitext(real)[1] or '(không đuôi)'}' (có thể là chương trình/script) — "
                          "dùng \"Mở thư mục\" hoặc \"Xem\"")
        try:
            OPENER(real, reveal)
        except OSError as e:
            raise HubError(f"không mở được trên máy chạy hub: {e.strerror or e}") from None
        return {"opened": True, "reveal": reveal, "path": real, "run_id": p.get("run_id"), "agent_id": p.get("agent_id")}


def register_rpc(daemon: "Daemon") -> None:
    """`file.read` (HUMAN_ONLY, chỉ đọc) + `file.open` (HUMAN_ONLY, tác dụng phụ). Chạy I/O ngoài event loop.

    Audit MỌI lời gọi (cả bị từ chối — dò đường dẫn là tín hiệu đáng xem): kind = method, data = {path, run/agent, reveal, kết quả
    hoặc lỗi}, kênh/actor theo `_audit` của người gọi (web: `channel=web`). KHÔNG bao giờ ghi nội dung file vào audit."""
    import asyncio

    hub = daemon.hub
    fa = FileAccess(hub)

    def wrap(method: str, fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], Any]:
        async def run(p: dict[str, Any]) -> dict[str, Any]:
            meta = p.pop("_audit", None) if isinstance(p, dict) else None
            meta = meta if isinstance(meta, dict) else {}
            data: dict[str, Any] = {"path": str(p.get("path") or "")[:PATH_MAX], "reveal": bool(p.get("reveal")) if method == "file.open" else None}
            try:
                res = await asyncio.to_thread(fn, p)
                data.update({"ok": True, "resolved": res.get("path"), "size": res.get("size"), "kind": res.get("kind")})
                return res
            except HubError as e:
                data.update({"ok": False, "error": str(e)[:300]})
                raise
            finally:
                try:
                    hub.audit.record(method, p.get("agent_id"), p.get("run_id"), {k: v for k, v in data.items() if v is not None},
                                     actor=str(meta.get("actor") or "user"), channel=str(meta.get("channel") or "cli"),
                                     target=str(p.get("path") or "")[:300], summary=method if data.get("ok") else f"{method} (từ chối)")
                except Exception:  # pragma: no cover — audit lỗi không che kết quả
                    pass

        return run

    daemon.methods.update({"file.read": wrap("file.read", fa.read), "file.open": wrap("file.open", fa.open)})


__all__ = ["FileAccess", "MUTATING_FILES", "OPEN_EXTS", "RAW_MAX", "VIEW_MAX", "can_open", "check_raw_path", "kind_of", "open_command",
           "read_file", "register_rpc", "resolve_in_roots", "secret_name"]
