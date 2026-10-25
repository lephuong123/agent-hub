"""Server Bot API Telegram GIẢ chạy cục bộ (127.0.0.1) cho test — không gọi Telegram thật.

Hỗ trợ: getMe, deleteWebhook, setMyCommands, getUpdates (long poll có offset/timeout), sendMessage (cả reply_markup
inline / keyboard / force_reply), answerCallbackQuery, editMessageReplyMarkup, editMessageText, sendChatAction, sendDocument,
getFile + GET /file/bot<token>/<path> (tệp người gửi: `add_file`). Token sai → 401. Test bơm update bằng `user_message()` / `press()` và đọc tin bot gửi bằng `sent()`.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKEN = "123456789:AAH-fakeTokenFOR_tests_only_0123456789"


class FakeTelegram:
    def __init__(self, token: str = TOKEN, *, fail_send: int = 0) -> None:
        self.token = token
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.paths: list[str] = []
        self.updates: list[dict[str, Any]] = []
        self.next_update = 1000
        self.next_msg = 1
        self.fail_send = fail_send
        self.files: dict[str, bytes] = {}  # file_id → nội dung (tệp/ảnh người gửi)
        self.downloads: list[str] = []  # file_path đã bị tải (GET /file/...)
        self.cond = threading.Condition()
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) or b"{}"
                if "multipart/form-data" in (self.headers.get("Content-Type") or ""):
                    body = {"_raw": raw}  # sendDocument: giữ nguyên thân multipart để test tìm nội dung tệp
                    m = re.search(rb'name="chat_id"\r\n\r\n([^\r]*)', raw)
                    body["chat_id"] = m.group(1).decode() if m else None
                    c = re.search(rb'name="caption"\r\n\r\n([^\r]*)', raw)
                    body["caption"] = c.group(1).decode("utf-8", "replace") if c else None
                    f = re.search(rb'name="document"; filename="([^"]*)"', raw)
                    body["filename"] = f.group(1).decode("utf-8", "replace") if f else None
                else:
                    body = json.loads(raw)
                outer.paths.append(self.path)
                code, res = outer.handle(self.path, body)
                data = json.dumps(res).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802 — tải tệp: /file/bot<token>/<file_path>
                parts = self.path.strip("/").split("/", 2)
                data = None
                if len(parts) == 3 and parts[0] == "file" and parts[1] == f"bot{outer.token}":
                    fid = parts[2].rsplit("/", 1)[-1]
                    data = outer.files.get(fid)
                    with outer.cond:
                        outer.downloads.append(parts[2])
                if data is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    # ------------------------------------------------------------------ server
    def handle(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        parts = path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != f"bot{self.token}":
            return 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
        method = parts[1]
        with self.cond:
            self.calls.append((method, body))
            self.cond.notify_all()
        if method == "getMe":
            return 200, {"ok": True, "result": {"id": 42, "is_bot": True, "username": "hub_test_bot", "first_name": "Hub"}}
        if method == "getUpdates":
            off = int(body.get("offset") or 0)
            deadline = time.monotonic() + min(float(body.get("timeout") or 0), 2.0)
            with self.cond:
                while True:
                    ups = [u for u in self.updates if u["update_id"] >= off]
                    if ups or time.monotonic() >= deadline:
                        return 200, {"ok": True, "result": ups}
                    self.cond.wait(timeout=max(0.01, deadline - time.monotonic()))
        if method == "getFile":
            fid = str(body.get("file_id") or "")
            if fid not in self.files:
                return 400, {"ok": False, "error_code": 400, "description": "Bad Request: invalid file_id"}
            return 200, {"ok": True, "result": {"file_id": fid, "file_size": len(self.files[fid]), "file_path": f"documents/{fid}"}}
        if method == "sendDocument":
            with self.cond:
                mid = self.next_msg
                self.next_msg += 1
            return 200, {"ok": True, "result": {"message_id": mid, "chat": {"id": int(body.get("chat_id") or 0)}}}
        if method == "sendMessage":
            if self.fail_send > 0:
                self.fail_send -= 1
                return 500, {"ok": False, "error_code": 500, "description": "Internal Server Error"}
            with self.cond:
                mid = self.next_msg
                self.next_msg += 1
            return 200, {"ok": True, "result": {"message_id": mid, "chat": {"id": int(body["chat_id"])}, "text": body.get("text"),
                                                 "reply_markup": body.get("reply_markup")}}
        if method == "editMessageText":
            return 200, {"ok": True, "result": {"message_id": body.get("message_id"), "chat": {"id": int(body["chat_id"])}, "text": body.get("text"),
                                                 "reply_markup": body.get("reply_markup")}}
        if method in ("answerCallbackQuery", "editMessageReplyMarkup", "deleteWebhook", "sendChatAction", "setMyCommands"):
            return 200, {"ok": True, "result": True}
        return 404, {"ok": False, "error_code": 404, "description": "Not Found"}

    # ------------------------------------------------------------------ phía test
    def _push(self, upd: dict[str, Any]) -> int:
        with self.cond:
            upd["update_id"] = self.next_update
            self.next_update += 1
            self.updates.append(upd)
            self.cond.notify_all()
            return upd["update_id"]

    def user_message(self, chat_id: int, text: str, *, reply_to: int | None = None, chat_type: str = "private", username: str = "boss") -> int:
        m: dict[str, Any] = {"message_id": 10_000 + self.next_update, "date": int(time.time()), "text": text,
                             "chat": {"id": chat_id, "type": chat_type, "username": username, "first_name": "Sếp"},
                             "from": {"id": chat_id, "is_bot": False, "username": username, "first_name": "Sếp"}}
        if reply_to is not None:
            m["reply_to_message"] = {"message_id": reply_to, "chat": {"id": chat_id, "type": chat_type}}
        return self._push({"message": m})

    def add_file(self, data: bytes) -> str:
        fid = f"F{len(self.files) + 1}x{abs(hash(data)) % 10_000}"
        self.files[fid] = data
        return fid

    def user_media(self, chat_id: int, *, photo: bytes | None = None, document: tuple[str, bytes, str] | None = None,
                   caption: str | None = None, media_group_id: str | None = None, voice: bool = False, from_id: int | None = None,
                   declared_size: int | None = None, reply_to: int | None = None) -> int:
        """Tin có ảnh (`photo` — 2 cỡ, cỡ lớn = dữ liệu thật) / tài liệu `(tên, bytes, mime)` / thoại."""
        m: dict[str, Any] = {"message_id": 10_000 + self.next_update, "date": int(time.time()),
                             "chat": {"id": chat_id, "type": "private", "username": "boss", "first_name": "Sếp"},
                             "from": {"id": from_id or chat_id, "is_bot": False, "username": "boss", "first_name": "Sếp"}}
        if photo is not None:
            small = self.add_file(b"small-thumb")
            big = self.add_file(photo)
            m["photo"] = [{"file_id": small, "file_unique_id": "s", "width": 90, "height": 90, "file_size": 11},
                          {"file_id": big, "file_unique_id": "b", "width": 1280, "height": 720,
                           "file_size": declared_size if declared_size is not None else len(photo)}]
        if document is not None:
            name, data, mime = document
            m["document"] = {"file_id": self.add_file(data), "file_unique_id": "d", "file_name": name, "mime_type": mime,
                             "file_size": declared_size if declared_size is not None else len(data)}
        if voice:
            m["voice"] = {"file_id": self.add_file(b"OggS-voice"), "file_unique_id": "v", "duration": 3, "mime_type": "audio/ogg", "file_size": 10}
        if caption is not None:
            m["caption"] = caption
        if media_group_id:
            m["media_group_id"] = media_group_id
        if reply_to is not None:
            m["reply_to_message"] = {"message_id": reply_to, "chat": {"id": chat_id, "type": "private"}}
        return self._push({"message": m})

    def documents(self, chat_id: int | None = None) -> list[dict[str, Any]]:
        with self.cond:
            return [b for m, b in self.calls if m == "sendDocument" and (chat_id is None or str(b.get("chat_id")) == str(chat_id))]

    def message_id_of(self, msg: dict[str, Any]) -> int | None:
        """message_id server giả đã cấp cho một body sendMessage (đếm theo thứ tự gửi toàn cục)."""
        n = 0
        with self.cond:
            for m, b in self.calls:
                if m in ("sendMessage", "sendDocument"):
                    n += 1
                    if b is msg:
                        return n
        return msg.get("message_id")

    def press(self, chat_id: int, data: str, *, message_id: int = 1, from_id: int | None = None) -> int:
        return self._push({"callback_query": {"id": f"cq{self.next_update}", "data": data, "from": {"id": from_id or chat_id},
                                              "message": {"message_id": message_id, "chat": {"id": chat_id, "type": "private"}}}})

    def sent(self, chat_id: int | None = None) -> list[dict[str, Any]]:
        with self.cond:
            return [b for m, b in self.calls if m == "sendMessage" and (chat_id is None or str(b.get("chat_id")) == str(chat_id))]

    def edits(self, chat_id: int | None = None) -> list[dict[str, Any]]:
        """Các lần bot SỬA tin (editMessageText) — UX v2 sửa tại chỗ thay vì gửi tin mới."""
        with self.cond:
            return [b for m, b in self.calls if m == "editMessageText" and (chat_id is None or str(b.get("chat_id")) == str(chat_id))]

    def with_buttons(self, chat_id: int, labels: list[str]) -> dict[str, Any] | None:
        """Tin MỚI NHẤT (gửi hoặc sửa) có đúng bộ nút `labels`."""
        with self.cond:
            msgs = [b for m, b in self.calls if m in ("sendMessage", "editMessageText") and str(b.get("chat_id")) == str(chat_id)]
        return next((m for m in reversed(msgs) if [b["text"] for b in self.buttons(m)] == labels), None)

    def calls_of(self, method: str) -> list[dict[str, Any]]:
        with self.cond:
            return [b for m, b in self.calls if m == method]

    def processed(self, update_id: int) -> bool:
        """Bot đã gọi getUpdates với offset > update_id (đã nhận) — dùng để chờ update được xử lý."""
        with self.cond:
            return any(m == "getUpdates" and int(b.get("offset") or 0) > update_id for m, b in self.calls)

    @staticmethod
    def buttons(msg: dict[str, Any]) -> list[dict[str, Any]]:
        kb = (msg.get("reply_markup") or {}).get("inline_keyboard") or []
        return [b for row in kb for b in row]
