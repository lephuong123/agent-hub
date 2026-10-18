"""Server Bot API Telegram GIẢ chạy cục bộ (127.0.0.1) cho test — không gọi Telegram thật.

Hỗ trợ: getMe, deleteWebhook, getUpdates (long poll có offset/timeout), sendMessage, answerCallbackQuery,
editMessageReplyMarkup. Token sai → 401. Test bơm update bằng `user_message()` / `press()` và đọc tin bot gửi bằng `sent()`.
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
        if method in ("answerCallbackQuery", "editMessageReplyMarkup", "deleteWebhook"):
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

    def press(self, chat_id: int, data: str, *, message_id: int = 1, from_id: int | None = None) -> int:
        return self._push({"callback_query": {"id": f"cq{self.next_update}", "data": data, "from": {"id": from_id or chat_id},
                                              "message": {"message_id": message_id, "chat": {"id": chat_id, "type": "private"}}}})

    def sent(self, chat_id: int | None = None) -> list[dict[str, Any]]:
        with self.cond:
            return [b for m, b in self.calls if m == "sendMessage" and (chat_id is None or str(b.get("chat_id")) == str(chat_id))]

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
