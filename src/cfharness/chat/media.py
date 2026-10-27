"""Kênh chat ↔ điều phối viên: tệp/ảnh (gắn vào bot là `bot.media`).

Phần DÙNG CHUNG mọi kênh (`MediaBase`): phân loại ảnh/tệp văn bản, giới hạn (cỡ, số lượng), kiểm UTF-8/NUL, đếm lượt tải,
báo lỗi (chữ hub, tên tệp escape), chuyển cho điều phối viên, và toàn bộ chiều RA (`[[file: …]]` → `bot.send_document`).
Phần riêng nền tảng chỉ là "lấy danh sách tệp trong tin + hàm tải": Telegram = `MediaChat` (dưới đây), Slack = `chat/slack/media.py`.

VÀO (người → điều phối viên) — Telegram:
- Chỉ được gọi SAU khi `bot.handle_update` đã kiểm allowlist chat + người gửi (WT-4); trước khi tải còn kiểm lại `bot.is_allowed`
  → người lạ KHÔNG BAO GIỜ làm bot tải tệp. Kích thước khai báo trong tin được kiểm TRƯỚC `getFile`; tải dạng stream, dừng khi vượt trần.
- Ảnh (`photo` — lấy cỡ LỚN NHẤT; hoặc `document` là ảnh PNG/JPEG/GIF/WebP) ≤ 5 MB, ≤ 4 ảnh/tin (cùng giới hạn TUI,
  `manager/attachments.py`); định dạng kiểm lại theo magic bytes ở daemon (`manager.chat`).
- Tệp VĂN BẢN khác (`document`: text/*, JSON, YAML, CSV, mã nguồn…) ≤ `notify.telegram.document_max_bytes`, UTF-8, không NUL, ≤ 4/tin.
  PDF/Office/nhị phân → từ chối kèm lời giải thích (chưa hỗ trợ).
- Album (`media_group_id`) → gom trong `media_group_wait_s` thành MỘT tin; chú thích (caption) = nội dung tin.
- Tin thoại/âm thanh/video tròn → trả lời lịch sự là chưa hỗ trợ (chưa có chuyển giọng nói thành chữ).
- Gửi qua `bot.manager.handle_text(images=…, files=…)` → `bot.call("manager.chat")` (cổng người + audit kênh telegram).

RA (điều phối viên → người, "deliverable mode"):
- Dòng `[[file: <đường dẫn tương đối>]]` trong trả lời (quy ước ghi trong prompt chat kênh Telegram — `manager/prompts.py`)
  → gỡ khỏi chữ; sau khung chữ (WT-8) gửi từng tệp bằng `sendDocument` (KHÔNG nút; chú thích do hub sinh).
- Chỉ tệp THƯỜNG nằm trong cwd hội thoại (`agents/<id>/workspaces/conv-<id>`): không đường dẫn tuyệt đối, không `..`, không
  thành phần nào là symlink/junction, realpath phải nằm trong gốc, mở bằng O_NOFOLLOW (nếu có) + fstat; ≤ `deliver_max_bytes`,
  ≤ `deliver_max_files`. Tệp chứa giá trị secret hub biết / chuỗi trông như token → KHÔNG gửi.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..manager.attachments import IMAGE_SUFFIXES, MAX_IMAGE_BYTES, MAX_IMAGES
from ..notify.base import clip
from ..notify.telegram import TelegramError, esc, visible

if TYPE_CHECKING:
    from ..notify.telegram import TelegramApi
    from .base import ChatBot

log = logging.getLogger("cfharness.chat.telegram.media")

MAX_DOCS = 4
IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
TEXT_MIMES = {"application/json", "application/xml", "application/x-yaml", "application/yaml", "application/javascript",
              "application/x-sh", "application/x-python", "application/toml", "application/sql", "application/csv", "application/x-ndjson"}
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".yaml", ".yml", ".toml", ".ini",
                 ".cfg", ".conf", ".log", ".xml", ".html", ".htm", ".css", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".py", ".rb", ".go",
                 ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".sh", ".ps1", ".bat", ".sql", ".swift", ".dart",
                 ".vue", ".svelte", ".tex", ".env.example", ".diff", ".patch", ".srt", ".vtt"}
VOICE_KINDS = ("voice", "audio", "video_note")
OTHER_KINDS = ("video", "animation", "sticker")
VOICE_REPLY = ("🎙 Xin lỗi, hub chưa hỗ trợ tin nhắn thoại/âm thanh (chưa có chuyển giọng nói thành chữ). "
               "Bạn nhắn bằng chữ giúp mình nhé — ảnh và tệp văn bản thì gửi được.")
OTHER_REPLY = "Xin lỗi, hub chưa nhận video/ảnh động/sticker. Gửi ảnh (PNG/JPEG/GIF/WebP) hoặc tệp văn bản kèm chú thích nhé."

_MARKER = re.compile(r"^[ \t>*-]*\[\[\s*file\s*:\s*([^\]\n]{1,512}?)\s*\]\][ \t]*$", re.I | re.M)


def has_media(m: dict[str, Any]) -> bool:
    return any(k in m for k in ("photo", "document", *VOICE_KINDS, *OTHER_KINDS))


def extract_file_markers(text: str) -> tuple[str, list[str]]:
    """Trả lời của điều phối viên → (chữ đã gỡ dòng đánh dấu, [đường dẫn tương đối theo thứ tự, không trùng])."""
    found: list[str] = []
    for m in _MARKER.finditer(text or ""):
        p = m.group(1).strip().strip("`'\"").strip()
        if p and p not in found:
            found.append(p)
    if not found:
        return text, []
    out = _MARKER.sub("", text)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out, found


def resolve_deliverable(root: Path, rel: str, max_bytes: int) -> tuple[bytes | None, str]:
    """Đọc tệp `rel` trong `root` an toàn. Trả (bytes, tên) hoặc (None, lý do tiếng Việt)."""
    if not rel or "\x00" in rel or len(rel) > 512:
        return None, "đường dẫn không hợp lệ"
    win, posix = PureWindowsPath(rel), PurePosixPath(rel.replace("\\", "/"))
    if win.drive or win.root or posix.is_absolute() or rel.startswith("~"):
        return None, "chỉ nhận đường dẫn TƯƠNG ĐỐI trong thư mục làm việc của hội thoại"
    parts = [p for p in posix.parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None, "không được ra ngoài thư mục làm việc (..)"
    try:
        base = Path(os.path.realpath(root))
    except OSError:
        return None, "không có thư mục làm việc"
    if not base.is_dir():
        return None, "không có thư mục làm việc"
    cur = base
    for i, part in enumerate(parts):  # không thành phần nào là symlink/junction (kể cả tệp cuối)
        cur = cur / part
        try:
            st = os.lstat(cur)
        except OSError:
            return None, "không tìm thấy tệp"
        is_junction = getattr(cur, "is_junction", lambda: False)()
        if stat.S_ISLNK(st.st_mode) or is_junction:
            return None, "là liên kết (symlink) — không gửi"
        if i < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
            return None, "không tìm thấy tệp"
    try:
        real = Path(os.path.realpath(cur))
        real.relative_to(base)
    except (OSError, ValueError):
        return None, "nằm ngoài thư mục làm việc"
    try:
        fd = os.open(cur, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    except OSError:
        return None, "không mở được tệp"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "không phải tệp thường"
        if st.st_size > max_bytes:
            return None, f"quá lớn ({st.st_size // 1024} KB > {max_bytes // 1024} KB)"
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        return None, f"quá lớn (> {max_bytes // 1024} KB)"
    if _has_secret(data):
        return None, "có vẻ chứa secret/token — không gửi"
    return data, parts[-1]


def _has_secret(data: bytes) -> bool:
    from ..redact import mask_bytes, redact_str

    if mask_bytes(data) != data:  # giá trị secret hub biết (H-12)
        return True
    try:
        t = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return redact_str(t) != t


def _ext(name: str) -> str:
    n = name.lower()
    if n.endswith(".env.example"):
        return ".env.example"
    return os.path.splitext(n)[1]


def is_text_file(mime: Any, name: Any) -> bool:
    m = str(mime or "").lower()
    return m.startswith("text/") or m in TEXT_MIMES or _ext(str(name or "")) in TEXT_SUFFIXES


def is_image_file(mime: Any, name: Any) -> bool:
    m = str(mime or "").lower()
    return m in IMAGE_MIMES or (not m.startswith("image/svg") and _ext(str(name or "")) in IMAGE_SUFFIXES)


def classify(mime: Any, name: Any) -> str | None:
    """"image" | "file" (văn bản) | None (chưa hỗ trợ) — cùng quy tắc mọi kênh."""
    if is_image_file(mime, name):
        return "image"
    if is_text_file(mime, name):
        return "file"
    return None


UNSUPPORTED = "chưa hỗ trợ loại tệp này (chỉ ảnh PNG/JPEG/GIF/WebP và tệp văn bản)"


def _b64(name: str, data: bytes) -> dict[str, str]:
    return {"name": name, "data_b64": base64.b64encode(data).decode("ascii")}


class MediaItem:
    """Một ảnh/tệp người gửi, chờ tải. `fetch(cap)` → bytes (stream, dừng khi vượt `cap`); lỗi thuộc `fetch_errors` → báo người."""

    __slots__ = ("kind", "name", "size", "fetch")

    def __init__(self, kind: str, name: str, size: int, fetch: Callable[[int], Awaitable[bytes]] | None) -> None:
        self.kind, self.name, self.size, self.fetch = kind, name, size, fetch


class MediaBase:
    """Phần dùng chung mọi kênh. Lớp con chỉ dựng danh sách `MediaItem` từ tin của nền tảng rồi gọi `submit`."""

    fetch_errors: tuple[type[BaseException], ...] = (TelegramError,)

    def __init__(self, bot: "ChatBot") -> None:
        self.bot = bot
        self.hub = bot.hub
        self.downloads = 0  # đếm số lần tải (test chứng minh người lạ không kích hoạt tải)

    def cfg(self, key: str, default: Any) -> Any:
        v = self.bot.cfg().get(key)
        return default if v is None else v

    def doc_cap(self) -> int:
        return min(int(self.cfg("document_max_bytes", 1048576)), 10 * 1024 * 1024)

    # ------------------------------------------------------------------ vào
    async def collect(self, items: list[MediaItem], errors: list[str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """Kiểm số lượng + cỡ khai báo TRƯỚC khi tải, tải (đếm `downloads`), kiểm văn bản UTF-8/không NUL → ([ảnh], [tệp]) dạng b64."""
        doc_cap = self.doc_cap()
        images: list[dict[str, str]] = []
        files: list[dict[str, str]] = []
        for it in items:
            kind, name, size = it.kind, it.name, it.size
            cap = MAX_IMAGE_BYTES if kind == "image" else doc_cap
            lst = images if kind == "image" else files
            if len(lst) >= (MAX_IMAGES if kind == "image" else MAX_DOCS):
                errors.append(f"{name}: tối đa {MAX_IMAGES if kind == 'image' else MAX_DOCS} {'ảnh' if kind == 'image' else 'tệp'} mỗi tin — bỏ qua")
                continue
            if size > cap:
                errors.append(f"{name}: quá lớn ({size // 1024} KB > {cap // 1024} KB)")
                continue
            if it.fetch is None:
                continue
            try:
                self.downloads += 1
                data = await it.fetch(cap)
            except self.fetch_errors as e:
                errors.append(f"{name}: không tải được ({clip(e, 120)})")
                continue
            if kind == "file":
                if b"\x00" in data:
                    errors.append(f"{name}: không phải tệp văn bản (UTF-8)")
                    continue
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError:
                    errors.append(f"{name}: không phải tệp văn bản (UTF-8)")
                    continue
            lst.append(_b64(name, data))
        return images, files

    async def submit(self, cid: str, caption: str, items: list[MediaItem], errors: list[str], api: Any, *, reply_to: Any,
                     message_id: Any = None, conv: str | None = None) -> None:
        """Tải + kiểm, báo lý do từ chối (nếu có), rồi gửi điều phối viên qua `bot.manager.handle_text` (cổng người + audit kênh)."""
        images, files = await self.collect(items, errors)
        if errors:
            await self._send_errors(api, cid, errors, reply_to)
        if not images and not files:
            return
        await self.bot.manager.handle_text(cid, caption, api, message_id=reply_to if message_id is None else message_id, conv=conv,
                                           images=images or None, files=files or None)

    async def _send_errors(self, api: Any, cid: str, errors: list[str], reply_to: Any) -> None:
        """Lý do từ chối (chữ hub; tên tệp do người gửi → trong <pre>, escape)."""
        body = "\n".join(f"• {visible(e)}" for e in errors[:10])
        try:
            await self.bot._send(api, cid, f"<b>Một số tệp/ảnh không gửi được cho điều phối viên:</b>\n<pre>{esc(body)}</pre>",
                                 html=True, reply_to=reply_to)
        except Exception as e:
            log.warning("không gửi được báo lỗi tệp: %s", clip(e))

    # ------------------------------------------------------------------ ra
    def conv_root(self, conv: str | None) -> Path | None:
        if not conv:
            return None
        mgr = getattr(self.hub, "manager", None)
        try:
            row = mgr._conv_row(conv)  # type: ignore[union-attr]
            home = Path(self.hub.agents.get_row(row["agent_id"])["home_path"])
        except Exception:
            return None
        return home / "workspaces" / f"conv-{conv}"

    async def deliver_files(self, api: Any, chat: Any, conv: str | None, wanted: list[str]) -> int:
        """Gửi các tệp điều phối viên đánh dấu (qua `bot.send_document`: Telegram sendDocument, Slack files v2). Trả số tệp đã gửi.
        Tệp bị từ chối → MỘT tin hub liệt kê lý do."""
        if not self.cfg("deliver_files", True):
            return 0
        root = self.conv_root(conv)
        max_bytes = int(self.cfg("deliver_max_bytes", 20 * 1024 * 1024))
        max_files = int(self.cfg("deliver_max_files", 5))
        errors: list[str] = []
        sent = 0
        for i, rel in enumerate(wanted):
            if i >= max_files:
                errors.append(f"{rel}: tối đa {max_files} tệp mỗi trả lời — bỏ qua")
                continue
            if root is None:
                errors.append(f"{rel}: không xác định được thư mục làm việc của hội thoại")
                continue
            data, why = resolve_deliverable(root, rel, max_bytes)
            if data is None:
                errors.append(f"{rel}: {why}")
                continue
            name = why
            try:
                res = await self.bot.send_document(api, chat, name, data, f"📎 Tệp từ điều phối viên: {visible(name)[:200]}",
                                                   timeout=max(60.0, float(self.bot.cfg().get("timeout_s") or 15)))
            except Exception as e:
                errors.append(f"{rel}: gửi lỗi ({clip(e, 120)})")
                continue
            sent += 1
            mid = res.get("message_id") if isinstance(res, dict) else None
            if conv and mid is not None:  # reply vào tệp → tiếp tục đúng cuộc trò chuyện
                self.bot.manager._put(self.bot.manager.MSGMAP, f"{chat}:{mid}", conv)
            audit = getattr(self.hub, "audit", None)
            if audit is not None:
                audit.record(f"{self.bot.NAME}.file_sent", None, None, {"chat_id": str(chat), "conversation_id": conv, "path": rel, "bytes": len(data)},
                             actor="hub", channel=self.bot.channel_of(chat), summary=f"gửi tệp {name}")
        if errors:
            await self._send_errors_out(api, chat, errors)
        return sent

    async def _send_errors_out(self, api: Any, chat: Any, errors: list[str]) -> None:
        body = "\n".join(f"• {visible(e)}" for e in errors[:10])
        try:
            await self.bot._send(api, chat, f"<b>Không gửi được một số tệp điều phối viên đánh dấu:</b>\n<pre>{esc(body)}</pre>", html=True)
        except Exception as e:
            log.warning("không gửi được báo lỗi tệp: %s", clip(e))


class MediaChat(MediaBase):
    """Telegram: `photo`/`document` (+ album `media_group_id`), tải qua `getFile` (`TelegramApi.download`)."""

    def __init__(self, bot: "ChatBot") -> None:
        super().__init__(bot)
        self._groups: dict[str, list[dict[str, Any]]] = {}

    # ------------------------------------------------------------------ vào
    async def handle(self, cid: str, uid: str | None, m: dict[str, Any], api: "TelegramApi", *, conv: str | None = None) -> None:
        """Tin có tệp/ảnh/thoại từ chat + người ĐÃ được phép (bot.handle_update kiểm trước)."""
        if any(k in m for k in VOICE_KINDS):
            await self.bot._send(api, cid, VOICE_REPLY, reply_to=m.get("message_id"))
            return
        if not ("photo" in m or "document" in m):
            await self.bot._send(api, cid, OTHER_REPLY, reply_to=m.get("message_id"))
            return
        gid = m.get("media_group_id")
        if not gid:
            await self._process(cid, uid, [m], api, conv)
            return
        key = f"{cid}:{uid}:{gid}"
        first = key not in self._groups
        self._groups.setdefault(key, []).append(m)
        if first:
            self.bot._spawn(self._flush_group(key, cid, uid, api, conv))

    async def _flush_group(self, key: str, cid: str, uid: str | None, api: "TelegramApi", conv: str | None) -> None:
        await asyncio.sleep(float(self.cfg("media_group_wait_s", 1.5)))
        msgs = self._groups.pop(key, [])
        if msgs:
            await self._process(cid, uid, msgs, api, conv)

    async def _process(self, cid: str, uid: str | None, msgs: list[dict[str, Any]], api: "TelegramApi", conv: str | None) -> None:
        if not self.bot.is_allowed(cid, uid):  # cấu hình có thể đổi giữa lúc nhận và lúc tải → kiểm lại, không tải
            return
        caption = next((str(x.get("caption")).strip() for x in msgs if str(x.get("caption") or "").strip()), "")
        items: list[MediaItem] = []
        errors: list[str] = []
        for i, m in enumerate(msgs, 1):
            if "photo" in m:
                sizes = [p for p in (m.get("photo") or []) if isinstance(p, dict) and p.get("file_id")]
                if not sizes:
                    continue
                best = max(sizes, key=lambda p: (int(p.get("width") or 0) * int(p.get("height") or 0), int(p.get("file_size") or 0)))
                kind, fid, size, name = "image", best.get("file_id"), int(best.get("file_size") or 0), f"telegram-photo-{i}.jpg"
            else:
                doc = m.get("document") or {}
                name = str(doc.get("file_name") or f"tep-{i}")
                k = classify(doc.get("mime_type"), doc.get("file_name"))
                if k is None:
                    errors.append(f"{name}: {UNSUPPORTED}")
                    continue
                kind, fid, size = k, doc.get("file_id"), int(doc.get("file_size") or 0)
            items.append(MediaItem(kind, name, size, self._fetcher(api, fid) if fid else None))
        await self.submit(cid, caption, items, errors, api, reply_to=msgs[0].get("message_id"), conv=conv)

    @staticmethod
    def _fetcher(api: "TelegramApi", fid: Any) -> Callable[[int], Awaitable[bytes]]:
        async def fetch(cap: int) -> bytes:
            return await api.download(str(fid), cap)

        return fetch
