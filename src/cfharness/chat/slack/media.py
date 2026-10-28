"""Slack ↔ điều phối viên: ảnh/tệp người gửi trong DM (P4, docs/plan/slack-2way.md). Phần dùng chung (phân loại, giới hạn,
kiểm UTF-8/NUL, báo lỗi, chuyển cho điều phối viên, chiều RA `[[file: …]]` → files v2) ở `chat/media.py` (`MediaBase`).

VÀO — chỉ phần riêng Slack:
- Gọi từ `SlackBot._on_message` (tin `subtype: file_share`) SAU khi đã kiểm workspace + allowlist chat/người; trước khi tải còn kiểm lại
  `bot.is_allowed` → người lạ KHÔNG BAO GIỜ làm bot tải tệp (đếm `downloads`).
- Mỗi tệp trong `files: [...]`: cỡ khai báo (`size`) kiểm TRƯỚC khi tải; tải `url_private_download` (hoặc `url_private`) bằng bot token
  ở header `Authorization` (app cần scope `files:read`), dạng stream, dừng ngay khi vượt trần; KHÔNG theo redirect.
- URL tải phải thuộc host tệp của Slack (`https://files.slack.com/…`) — không bao giờ theo URL tuỳ ý trong payload. Test: thay gốc
  bằng `notify.slack.files_base` (chỉ dùng khi đặt; khi đặt thì THAY gốc mặc định, không cộng thêm).
- Slack trả trang HTML đăng nhập (200) khi token thiếu `files:read` → báo lỗi thay vì đưa HTML cho điều phối viên.
- Cùng giới hạn Telegram: ảnh PNG/JPEG/GIF/WebP ≤ 5 MB, ≤ 4/tin; tệp văn bản ≤ `notify.slack.document_max_bytes` (trần cứng 10 MB),
  UTF-8, không NUL, ≤ 4/tin; loại khác → từ chối kèm lời giải thích (chữ hub, tên tệp escape).
- Chú thích = chữ của tin. Trong thread → cuộc trò chuyện điều phối viên của thread (cùng cách dò tin như `_on_thread_reply`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from urllib.parse import urlsplit

from ...notify.telegram import _httpx
from ..media import UNSUPPORTED, MediaBase, MediaItem, classify
from .api import SlackApi, SlackError

if TYPE_CHECKING:
    from .bot import SlackBot

log = logging.getLogger("cfharness.chat.slack.media")

SLACK_FILES_BASE = "https://files.slack.com/"


def allowed_url(url: Any, files_base: Any = None) -> bool:
    """URL tải tệp có thuộc gốc tệp của Slack (hoặc `files_base` khi đặt) không. Không userinfo, không cổng/scheme lạ."""
    if not isinstance(url, str) or not url or any(ch in url for ch in ("\\", "\x00", "\r", "\n", " ")):
        return False
    base = str(files_base or SLACK_FILES_BASE)
    try:
        u, b = urlsplit(url), urlsplit(base)
        up, bp = u.port, b.port
    except ValueError:
        return False
    if u.username or u.password or u.scheme not in ("https", "http") or u.scheme != b.scheme:
        return False
    if (u.hostname or "").lower() != (b.hostname or "").lower() or not u.hostname:
        return False
    if (up or (443 if u.scheme == "https" else 80)) != (bp or (443 if b.scheme == "https" else 80)):
        return False
    prefix = b.path.rstrip("/") + "/"
    return u.path.startswith(prefix) and ".." not in u.path.split("/")


async def download(api: SlackApi, url: str, max_bytes: int, *, html_ok: bool = False, timeout: float | None = None) -> bytes:
    """GET `url` với bot token (header), stream, dừng khi vượt `max_bytes`; không theo redirect. Lỗi → SlackError đã gỡ token."""
    try:
        async with _httpx.AsyncClient(timeout=timeout or max(api.timeout, 30.0), follow_redirects=False) as c:
            async with c.stream("GET", url, headers={"Authorization": f"Bearer {api.token}"}) as r:
                if r.status_code != 200:
                    raise SlackError(f"tải tệp: HTTP {r.status_code}" + (" (thiếu scope files:read?)" if r.status_code in (301, 302, 303, 401, 403) else ""))
                ctype = str(r.headers.get("Content-Type") or "").lower()
                if ctype.startswith("text/html") and not html_ok:
                    raise SlackError("Slack trả trang HTML thay vì tệp (app thiếu scope files:read?)")
                declared = r.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise SlackError(f"tệp quá lớn ({int(declared) // 1024} KB)")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise SlackError(f"tệp quá lớn (> {max_bytes // 1024} KB)")
                return bytes(buf)
    except SlackError:
        raise
    except Exception as e:  # lỗi mạng: không để token lọt ra log/tin
        raise SlackError(f"tải tệp: {type(e).__name__}: {api._scrub(e)}") from None


class SlackMedia(MediaBase):
    """`bot.media` của Slack: VÀO = tin `file_share` trong DM; RA = `MediaBase.deliver_files` → `SlackBot.send_document` (files v2)."""

    fetch_errors = (SlackError,)

    def __init__(self, bot: "SlackBot") -> None:
        super().__init__(bot)
        self.bot: "SlackBot" = bot

    def conv_of_thread(self, cid: str, ev: dict[str, Any]) -> tuple[str | None, Any]:
        """(cuộc trò chuyện điều phối viên, tin để trả lời vào) — tin trong thread → dò tin của bot trong thread như `_on_thread_reply`."""
        root = ev.get("thread_ts")
        if not root or root == ev.get("ts"):
            return None, ev.get("ts")
        for mid in self.bot._candidates(str(root), cid):
            conv = self.bot.manager.conv_of_message(cid, mid)
            if conv:
                return conv, str(root)
        return None, str(root)

    async def handle(self, cid: str, uid: str | None, ev: dict[str, Any], api: SlackApi) -> None:
        """Tin `file_share` từ DM + người ĐÃ được phép (bot kiểm trước); kiểm lại ở đây trước khi tải bất cứ thứ gì."""
        if not self.bot.is_allowed(cid, uid):  # cấu hình có thể đổi giữa lúc nhận và lúc tải → không tải
            return
        conv, reply_to = self.conv_of_thread(cid, ev)
        caption = str(ev.get("text") or "").strip()
        files = [f for f in (ev.get("files") or []) if isinstance(f, dict)]
        items: list[MediaItem] = []
        errors: list[str] = []
        files_base = self.bot.cfg().get("files_base")
        for i, f in enumerate(files, 1):
            name = str(f.get("name") or f.get("title") or f"slack-file-{i}")
            if f.get("file_access") == "check_file_info" or f.get("mode") in ("tombstone", "hidden_by_limit"):
                errors.append(f"{name}: Slack không gửi kèm tệp này (tệp đã xoá, bị ẩn, hoặc từ kênh ngoài) — gửi lại trực tiếp trong DM")
                continue
            mime = str(f.get("mimetype") or "")
            kind = classify(mime, name)
            if kind is None:
                errors.append(f"{name}: {UNSUPPORTED}")
                continue
            url = f.get("url_private_download") or f.get("url_private")
            if not allowed_url(url, files_base):
                errors.append(f"{name}: địa chỉ tải không thuộc Slack — bỏ qua")
                log.warning("Slack: bỏ URL tải tệp không thuộc host tệp của Slack")
                continue
            try:
                size = int(f.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            items.append(MediaItem(kind, name, size, self._fetcher(api, str(url), html_ok=mime.lower().startswith("text/html"))))
        if not files:
            errors.append("không thấy tệp nào trong tin")
        await self.submit(cid, caption, items, errors, api, reply_to=reply_to, conv=conv)

    @staticmethod
    def _fetcher(api: SlackApi, url: str, *, html_ok: bool) -> Callable[[int], Awaitable[bytes]]:
        async def fetch(cap: int) -> bytes:
            return await download(api, url, cap, html_ok=html_ok)

        return fetch
