"""`cfharness mcp login <tên>` / `cfharness mcp logout <tên>` — đăng nhập OAuth MCP từ terminal.

Chọn listener loopback TẠM (không phụ thuộc server web đang chạy): CLI mở `http://127.0.0.1:<cổng>/callback` (ưu tiên cổng cố định
`DEFAULT_PORT` để bản đăng ký DCR dùng lại được; bận → cổng ngẫu nhiên), xin `mcp.oauth_start` với redirect đó, in/mở URL đăng nhập,
chờ ĐÚNG một callback có `state` hợp lệ (≤ 10 phút) rồi chuyển `state`/`code` cho daemon (`mcp.oauth_callback`). Listener chỉ bind
127.0.0.1, chỉ nhận `GET /callback`, đóng ngay sau khi xong. `state` (256 bit, một lần, do daemon giữ) là lớp chống CSRF/giả callback.
"""

from __future__ import annotations

import html
import socket
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

DEFAULT_PORT = 33418
WAIT_S = 600


def _bind(port: int) -> HTTPServer:
    try:
        return HTTPServer(("127.0.0.1", port), _Handler)
    except OSError:
        return HTTPServer(("127.0.0.1", 0), _Handler)


class _Handler(BaseHTTPRequestHandler):
    server_version = "cfharness"
    sys_version = ""

    def log_message(self, fmt: str, *args: Any) -> None:  # không in URL có code/state ra terminal/log
        return

    def do_GET(self) -> None:  # noqa: N802
        srv: Any = self.server
        u = urlsplit(self.path)
        if u.path != "/callback":
            self._send(404, "Không có trang này.")
            return
        q = {k: v[0] for k, v in parse_qs(u.query).items() if v}
        if not q.get("state"):
            self._send(400, "Thiếu state.")
            return
        try:
            res = srv.call("mcp.oauth_callback", {k: q.get(k) for k in ("state", "code", "error", "error_description", "iss")})
        except Exception as e:  # RpcError: state lạ/đã dùng → tiếp tục chờ callback thật
            self._send(400, f"Không hợp lệ: {getattr(e, 'message', e)}")
            return
        srv.result = res
        if res.get("ok"):
            self._send(200, f"Đã kết nối MCP server {res.get('name')}. Bạn có thể đóng tab này.")
        else:
            self._send(400, f"Kết nối {res.get('name')} thất bại: {res.get('error')}")

    def _send(self, code: int, text: str) -> None:
        body = ("<!doctype html><meta charset=utf-8><title>cfharness</title>"
                f"<body style='font-family:system-ui;margin:3em'><p>{html.escape(text)}</p></body>").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)


def cmd_login(ctx: Any) -> int:
    a = ctx.args
    s = ctx.call("mcp.show", {"name": a.name})
    if s.get("oauth_status") is None:
        print(f"lỗi: MCP server '{a.name}' chưa bật OAuth — `cfharness mcp edit {a.name} --oauth`", file=sys.stderr)
        return 1
    httpd = _bind(int(a.port or DEFAULT_PORT))
    httpd.timeout = 1.0
    httpd.result = None  # type: ignore[attr-defined]
    httpd.call = ctx.call  # type: ignore[attr-defined]
    try:
        port = httpd.server_address[1]
        redirect = f"http://127.0.0.1:{port}/callback"
        st = ctx.call("mcp.oauth_start", {"name": a.name, "redirect_uri": redirect})
        url = st["authorize_url"]
        print(f"Đăng nhập {a.name} tại {st.get('authorization_host')} (client: {st.get('client')}). Mở link sau trong trình duyệt:\n  {url}", flush=True)
        if not a.no_browser:
            try:
                webbrowser.open(url, new=2)
            except Exception:  # pragma: no cover
                pass
        print(f"Đang chờ trình duyệt quay về {redirect} (tối đa {WAIT_S // 60} phút, Ctrl+C để huỷ)…", flush=True)
        deadline = time.monotonic() + float(a.timeout or WAIT_S)
        while httpd.result is None and time.monotonic() < deadline:  # type: ignore[attr-defined]
            try:
                httpd.handle_request()
            except (socket.timeout, OSError):
                continue
    except KeyboardInterrupt:
        print("đã huỷ", file=sys.stderr)
        return 1
    finally:
        httpd.server_close()
    res = httpd.result  # type: ignore[attr-defined]
    if res is None:
        print("lỗi: hết thời gian chờ đăng nhập", file=sys.stderr)
        return 2
    exp = ((res.get("oauth") or {}).get("expires_at")) or "không rõ"
    ctx.out(res, f"đã kết nối {res['name']} (hết hạn token: {exp})" if res.get("ok") else f"lỗi: {res.get('error')}")
    return 0 if res.get("ok") else 2


def cmd_logout(ctx: Any) -> int:
    res = ctx.call("mcp.oauth_disconnect", {"name": ctx.args.name})
    ctx.out(res, f"đã ngắt kết nối OAuth {res['name']}" + (" (đã thu hồi token ở nhà cung cấp)" if res.get("revoked") else "")
            + ("" if res.get("disconnected") else " — vốn chưa kết nối"))
    return 0


def add_parsers(ms: Any, common: Any) -> None:
    lg = ms.add_parser("login", help="đăng nhập OAuth cho MCP server http/sse (mở trình duyệt, chờ callback ở 127.0.0.1)", parents=[common])
    lg.add_argument("name")
    lg.add_argument("--no-browser", action="store_true", help="chỉ in link, không tự mở trình duyệt")
    lg.add_argument("--port", type=int, default=None, help=f"cổng callback loopback (mặc định {DEFAULT_PORT}; bận → ngẫu nhiên)")
    lg.add_argument("--timeout", type=float, default=None, help="giây chờ (mặc định 600)")
    lo = ms.add_parser("logout", help="ngắt kết nối OAuth (thu hồi token nếu được, xoá khỏi secret store)", parents=[common])
    lo.add_argument("name")
