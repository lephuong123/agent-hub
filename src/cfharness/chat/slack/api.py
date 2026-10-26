"""Slack Web API cho bot 2 chiều (docs/plan/slack-2way.md) — `httpx2` async như `TelegramApi` (huỷ được khi daemon dừng).

- Bot token `xoxb-…` cho mọi method; app token `xapp-…` CHỈ cho `apps.connections.open` (Socket Mode).
- Gửi dạng form (`application/x-www-form-urlencoded`, giá trị lồng → chuỗi JSON): Slack nhận cách này ở MỌI method.
- Lỗi: `SlackError(error, retry_after)`; 429 đọc header `Retry-After`. Mọi thông báo lỗi được gỡ token trước khi ra log/event.
"""

from __future__ import annotations

import json
from typing import Any

from ...redact import redact_str
from ...notify.telegram import _httpx


class SlackError(Exception):
    def __init__(self, message: str, *, code: str | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class SlackApi:
    def __init__(self, base: str, bot_token: str, app_token: str | None = None, *, timeout: float = 15.0) -> None:
        self.base = base.rstrip("/")
        self.token = bot_token
        self.app_token = app_token
        self.timeout = timeout

    def _scrub(self, s: Any) -> str:
        t = str(s)
        for tok in (self.token, self.app_token):
            if tok:
                t = t.replace(tok, "***")
        return redact_str(t)

    async def call(self, method: str, params: dict[str, Any] | None = None, *, app: bool = False, timeout: float | None = None) -> dict[str, Any]:
        tok = self.app_token if app else self.token
        if not tok:
            raise SlackError(f"{method}: thiếu {'app token (xapp-)' if app else 'bot token (xoxb-)'}")
        form = {k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)) for k, v in (params or {}).items() if v is not None}
        try:
            async with _httpx.AsyncClient(timeout=timeout or self.timeout) as c:
                r = await c.post(f"{self.base}/{method}", data=form, headers={"Authorization": f"Bearer {tok}"})
            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                raise SlackError(f"{method}: bị giới hạn tốc độ (429)", code="ratelimited", retry_after=float(ra) if ra and ra.isdigit() else 1.0)
            try:
                data = r.json()
            except ValueError:
                raise SlackError(f"{method}: HTTP {r.status_code} (không phải JSON)") from None
        except SlackError:
            raise
        except Exception as e:  # lỗi mạng: thông báo có thể chứa token → gỡ
            raise SlackError(f"{method}: {type(e).__name__}: {self._scrub(e)}") from None
        if not isinstance(data, dict) or not data.get("ok"):
            d = data if isinstance(data, dict) else {}
            err = str(d.get("error") or d)
            raise SlackError(f"{method}: {self._scrub(err)}", code=err)
        return data

    async def upload(self, channel: str, name: str, data: bytes, comment: str, *, thread_ts: str | None = None,
                     timeout: float | None = None) -> dict[str, Any]:
        """files.getUploadURLExternal → POST nội dung → files.completeUploadExternal (thay files.upload đã ngừng)."""
        info = await self.call("files.getUploadURLExternal", {"filename": name, "length": str(len(data))}, timeout=timeout)
        url, fid = info.get("upload_url"), info.get("file_id")
        if not url or not fid:
            raise SlackError("files.getUploadURLExternal: thiếu upload_url/file_id")
        try:
            async with _httpx.AsyncClient(timeout=timeout or max(self.timeout, 60.0)) as c:
                r = await c.post(str(url), content=data, headers={"Content-Type": "application/octet-stream"})
            if r.status_code != 200:
                raise SlackError(f"tải tệp lên: HTTP {r.status_code}")
        except SlackError:
            raise
        except Exception as e:
            raise SlackError(f"tải tệp lên: {type(e).__name__}: {self._scrub(e)}") from None
        return await self.call("files.completeUploadExternal", {"files": [{"id": fid, "title": name}], "channel_id": channel,
                                                                "initial_comment": comment, "thread_ts": thread_ts}, timeout=timeout)
