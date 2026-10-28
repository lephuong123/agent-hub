"""Slack GIẢ chạy cục bộ (127.0.0.1) cho test — không gọi Slack thật.

- Web API (HTTP, thân form như SlackApi gửi): auth.test, apps.connections.open (app token) → URL WebSocket cục bộ, chat.postMessage,
  chat.update, chat.postEphemeral, conversations.open, users.info, files.getUploadURLExternal (+ POST /upload/<id>),
  files.completeUploadExternal, views.open (ghi lại view; `fail_views` → `expired_trigger_id`). Token sai → `invalid_auth`.
- P7: assistant.threads.setStatus (`fail_status` → `method_not_supported_for_channel_type`), reactions.add/remove (`fail_reactions` →
  `missing_scope`); `ratelimit[method] = n` → n lần kế tiếp trả HTTP 429 + `Retry-After: 1` (ghi vào `calls` là `<method>#429`).
- Tệp người gửi (P4): `user_files()` lưu nội dung và bơm tin `file_share`; GET `/files-pri/<id>/<tên>` trả nội dung khi có bot token
  ở header (thiếu/sai → trang HTML 200 như Slack thật khi thiếu `files:read`); `downloads` ghi mọi lần GET.
- Socket Mode (WebSocket, `websockets.sync.server` trong luồng riêng): gửi `hello`, rồi các envelope test bơm vào
  (`user_message()`, `press()`, `slash()`, `submit_view()`); ghi nhận ACK (`acked()`), `disconnect()` để thử kết nối lại; gửi trùng = cùng `event_id`.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote

from websockets.sync.server import serve

BOT_TOKEN = "xoxb-1111-2222-fakeBotTokenForTests0123"
APP_TOKEN = "xapp-1-A111-2222-fakeAppTokenForTests0123"
TEAM = "T0TEAM001"
BOT_USER = "U0BOT0001"


class FakeSlack:
    def __init__(self, *, team: str = TEAM) -> None:
        self.team = team
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.next_ts = 1000
        self.hubs: list[Any] = []  # hub của daemon test (fixture gắn) — tra nonce nút → id approval/câu hỏi
        self.uploads: dict[str, bytes] = {}
        self.files: dict[str, tuple[bytes, str]] = {}  # đường dẫn → (nội dung, mime) — tệp người gửi
        self.downloads: list[str] = []  # đường dẫn đã GET (kể cả bị từ chối vì token)
        self.outbox: list[dict[str, Any]] = []  # envelope chờ gửi qua WebSocket
        self.acks: set[str] = set()
        self.connections = 0
        self.next_env = 1
        self.cond = threading.Condition()
        self._ws: Any = None
        self._kick = False
        self.fail_views = False
        self.fail_status = False
        self.fail_reactions = False
        self.ratelimit: dict[str, int] = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                with outer.cond:
                    outer.downloads.append(self.path)
                    item = outer.files.get(self.path.split("?", 1)[0])
                    outer.cond.notify_all()
                auth = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                if item is None:
                    data, ctype, code = b"not found", "text/plain", 404
                elif auth != BOT_TOKEN:  # Slack thật: thiếu quyền → trang đăng nhập HTML, mã 200
                    data, ctype, code = b"<!DOCTYPE html><html>sign in</html>", "text/html; charset=utf-8", 200
                else:
                    data, ctype, code = item[0], item[1], 200
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) or b""
                if self.path.startswith("/upload/"):
                    with outer.cond:
                        outer.uploads[self.path.rsplit("/", 1)[-1]] = raw
                    self._reply(200, {"ok": True})
                    return
                form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}
                for k, v in list(form.items()):
                    if v[:1] in ("[", "{"):
                        try:
                            form[k] = json.loads(v)
                        except ValueError:
                            pass
                auth = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                method = self.path.strip("/")
                with outer.cond:
                    limited = outer.ratelimit.get(method, 0) > 0
                    if limited:
                        outer.ratelimit[method] -= 1
                        outer.calls.append((method + "#429", form))
                if limited:
                    self.send_response(429)
                    self.send_header("Retry-After", "1")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._reply(200, outer.handle(method, form, auth))

            def _reply(self, code: int, res: dict[str, Any]) -> None:
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
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.ws_srv = serve(self._ws_handler, "127.0.0.1", 0)
        threading.Thread(target=self.ws_srv.serve_forever, daemon=True).start()
        self.ws_url = f"ws://127.0.0.1:{self.ws_srv.socket.getsockname()[1]}/link"

    def close(self) -> None:
        self.srv.shutdown()
        self.ws_srv.shutdown()

    # ------------------------------------------------------------------ Web API
    def _ts(self) -> str:
        with self.cond:
            self.next_ts += 1
            return f"{self.next_ts}.000100"

    def handle(self, method: str, p: dict[str, Any], auth: str) -> dict[str, Any]:
        with self.cond:
            self.calls.append((method, p))
            self.cond.notify_all()
        if method == "apps.connections.open":
            return {"ok": True, "url": self.ws_url} if auth == APP_TOKEN else {"ok": False, "error": "invalid_auth"}
        if auth != BOT_TOKEN:
            return {"ok": False, "error": "invalid_auth"}
        if method == "auth.test":
            return {"ok": True, "team_id": self.team, "team": "Thử nghiệm", "user_id": BOT_USER, "user": "cfharness"}
        if method == "chat.postMessage":
            p["_ts"] = self._ts()  # test đọc lại ts của tin bot gửi
            return {"ok": True, "channel": p.get("channel"), "ts": p["_ts"]}
        if method in ("chat.update", "chat.postEphemeral"):
            return {"ok": True, "channel": p.get("channel"), "ts": p.get("ts") or self._ts()}
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "D" + str(p.get("users"))[1:]}}
        if method == "users.info":
            return {"ok": True, "user": {"id": p.get("user"), "name": "sep", "profile": {"real_name": "Sếp Thử"}}}
        if method == "views.open":
            if self.fail_views or not p.get("trigger_id"):
                return {"ok": False, "error": "expired_trigger_id"}
            return {"ok": True, "view": {"id": f"V{self.next_ts}", **(p.get("view") or {})}}
        if method == "assistant.threads.setStatus":
            return {"ok": False, "error": "method_not_supported_for_channel_type"} if self.fail_status else {"ok": True}
        if method in ("reactions.add", "reactions.remove"):
            return {"ok": False, "error": "missing_scope"} if self.fail_reactions else {"ok": True}
        if method == "files.getUploadURLExternal":
            fid = f"F{self.next_ts}"
            return {"ok": True, "upload_url": f"{self.url}/upload/{fid}", "file_id": fid}
        if method == "files.completeUploadExternal":
            return {"ok": True, "files": [{"id": f["id"]} for f in (p.get("files") or [])]}
        return {"ok": False, "error": "unknown_method"}

    def sent(self, channel: str | None = None) -> list[dict[str, Any]]:
        """Tin bot đã post (chat.postMessage) — kèm `blocks` đã giải JSON."""
        with self.cond:
            return [p for m, p in self.calls if m == "chat.postMessage" and (channel is None or p.get("channel") == channel)]

    def calls_of(self, method: str) -> list[dict[str, Any]]:
        with self.cond:
            return [p for m, p in self.calls if m == method]

    @staticmethod
    def text_of(p: dict[str, Any]) -> str:
        return "\n".join(b["text"]["text"] for b in (p.get("blocks") or []) if b.get("type") == "section")

    def ref_of(self, button: dict[str, Any]) -> str | None:
        """Id đối tượng mà nút trỏ tới — tin gọn không in id nên test tra qua kho nút của hub."""
        parts = str(button.get("value") or "").split(".")
        if len(parts) != 3:
            return None
        for h in self.hubs:
            rec = h.slack.callbacks._all().get(parts[1])
            if rec:
                return rec.get("r")
        return None

    def msg_for(self, channel: str, ref: str) -> dict[str, Any] | None:
        """Tin GỬI mới nhất có nút trỏ tới `ref`."""
        return next((m for m in reversed(self.sent(channel)) if any(self.ref_of(b) == ref for b in self.buttons(m))), None)

    def root_of(self, channel: str, msg: dict[str, Any] | None) -> dict[str, Any] | None:
        """Tin gốc của thread mà `msg` nằm trong (tin nội dung của cặp duyệt/câu hỏi)."""
        root = (msg or {}).get("thread_ts")
        return next((m for m in self.sent(channel) if m.get("_ts") == root), None) if root else None

    @staticmethod
    def buttons(p: dict[str, Any]) -> list[dict[str, Any]]:
        return [e for b in (p.get("blocks") or []) if b.get("type") == "actions" for e in b.get("elements") or []]

    # ------------------------------------------------------------------ Socket Mode
    def _ws_handler(self, ws: Any) -> None:
        with self.cond:
            self.connections += 1
            self._ws = ws
            self._kick = False
        ws.send(json.dumps({"type": "hello", "num_connections": 1}))
        while True:
            with self.cond:
                if self._kick:
                    self._kick = False
                    ws.send(json.dumps({"type": "disconnect", "reason": "refresh_requested"}))
                out, self.outbox = self.outbox, []
            for env in out:
                ws.send(json.dumps(env))
            try:
                raw = ws.recv(timeout=0.02)
            except TimeoutError:
                continue
            except Exception:
                return
            try:
                eid = json.loads(raw).get("envelope_id")
            except ValueError:
                continue
            with self.cond:
                self.acks.add(eid)
                self.cond.notify_all()

    def _push(self, typ: str, payload: dict[str, Any], *, eid: str | None = None) -> str:
        with self.cond:
            eid = eid or f"env-{self.next_env}"
            self.next_env += 1
            self.outbox.append({"type": typ, "envelope_id": eid, "payload": payload, "accepts_response_payload": typ != "events_api"})
        return eid

    def acked(self, eid: str) -> bool:
        with self.cond:
            return eid in self.acks

    def connected(self) -> bool:
        with self.cond:
            return self._ws is not None

    def disconnect(self) -> None:
        with self.cond:
            self._kick = True

    def user_message(self, channel: str, user: str, text: str, *, thread_ts: str | None = None, team: str | None = None,
                     event_id: str | None = None, subtype: str | None = None) -> str:
        ts = self._ts()
        ev = {"type": "message", "channel": channel, "user": user, "text": text, "ts": ts, "channel_type": "im" if channel.startswith("D") else "channel"}
        if thread_ts:
            ev["thread_ts"] = thread_ts
        if subtype:
            ev["subtype"] = subtype
        return self._push("events_api", {"team_id": team or self.team, "event": ev, "event_id": event_id or f"Ev{ts.replace('.', '')}"})

    def mention(self, channel: str, user: str, text: str, *, thread_ts: str | None = None, team: str | None = None) -> str:
        """Sự kiện `app_mention` (tag bot trong kênh) — `text` chứa `<@BOT_USER>` như Slack thật; không có `channel_type`."""
        ts = self._ts()
        ev = {"type": "app_mention", "channel": channel, "user": user, "text": text, "ts": ts, "event_ts": ts}
        if thread_ts:
            ev["thread_ts"] = thread_ts
        return self._push("events_api", {"team_id": team or self.team, "event": ev, "event_id": f"Ev{ts.replace('.', '')}"})

    def user_files(self, channel: str, user: str, files: list[tuple[str, bytes, str]], *, text: str = "", thread_ts: str | None = None,
                   sizes: dict[str, int] | None = None, urls: dict[str, str] | None = None, team: str | None = None) -> str:
        """Tin `file_share` kèm `files` [(tên, nội dung, mime)]. `sizes` = cỡ khai báo giả; `urls` = URL tải thay (thử host lạ)."""
        objs = []
        for name, data, mime in files:
            with self.cond:
                self.next_ts += 1
                fid = f"F{self.next_ts}"
                path = f"/files-pri/{self.team}-{fid}/{quote(name)}"  # Slack thật: tên đã mã hoá %
                self.files[path] = (data, mime)
            url = (urls or {}).get(name) or f"{self.url}{path}"
            dl = (urls or {}).get(name) or f"{self.url}{path}?dl=1"
            objs.append({"id": fid, "name": name, "title": name, "mimetype": mime, "filetype": name.rsplit(".", 1)[-1],
                         "size": (sizes or {}).get(name, len(data)), "mode": "hosted", "url_private": url, "url_private_download": dl})
        ts = self._ts()
        ev = {"type": "message", "subtype": "file_share", "channel": channel, "user": user, "text": text, "ts": ts, "files": objs,
              "channel_type": "im" if channel.startswith("D") else "channel"}
        if thread_ts:
            ev["thread_ts"] = thread_ts
        return self._push("events_api", {"team_id": team or self.team, "event": ev, "event_id": f"Ev{ts.replace('.', '')}"})

    def downloaded(self) -> list[str]:
        with self.cond:
            return list(self.downloads)

    def press(self, channel: str, user: str, value: str, ts: str, *, team: str | None = None) -> str:
        with self.cond:
            self.next_env += 1
            trig = f"trig-{self.next_env}"
        return self._push("interactive", {"type": "block_actions", "user": {"id": user, "team_id": team or self.team}, "team": {"id": team or self.team},
                                          "channel": {"id": channel}, "message": {"ts": ts}, "container": {"type": "message", "message_ts": ts, "channel_id": channel},
                                          "trigger_id": trig, "actions": [{"action_id": "cfh_0_0", "value": value, "type": "button"}]})

    def slash(self, user: str, text: str, *, channel: str = "C0GENERAL", team: str | None = None) -> str:
        with self.cond:
            self.next_env += 1
            trig = f"trig-{self.next_env}"
        return self._push("slash_commands", {"command": "/cfh", "text": text, "user_id": user, "channel_id": channel, "team_id": team or self.team,
                                             "trigger_id": trig, "response_url": f"{self.url}/response"})

    def views(self) -> list[dict[str, Any]]:
        """View đã mở (`views.open`, đã giải JSON) — mới nhất cuối."""
        return [p["view"] for p in self.calls_of("views.open") if isinstance(p.get("view"), dict)]

    def submit_view(self, user: str, view: dict[str, Any], text: str, *, team: str | None = None, metadata: str | None = None,
                    callback_id: str | None = None) -> str:
        """Người bấm "Gửi" trên modal → envelope `interactive` `view_submission` (giá trị ô nhập theo block_id/action_id của view)."""
        values: dict[str, Any] = {}
        for b in view.get("blocks") or []:
            if b.get("type") == "input":
                aid = (b.get("element") or {}).get("action_id")
                values[b.get("block_id")] = {aid: {"type": "plain_text_input", "value": text}}
        with self.cond:
            self.next_env += 1
            trig = f"trig-{self.next_env}"
        v = {"id": view.get("id") or "V0", "type": "modal", "callback_id": callback_id or view.get("callback_id"),
             "private_metadata": view.get("private_metadata") if metadata is None else metadata, "state": {"values": values}}
        return self._push("interactive", {"type": "view_submission", "user": {"id": user, "team_id": team or self.team},
                                          "team": {"id": team or self.team}, "trigger_id": trig, "view": v})

    def wait(self, fn, timeout: float = 10.0) -> Any:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            v = fn()
            if v:
                return v
            time.sleep(0.02)
        raise AssertionError("hết giờ chờ (fake Slack)")
