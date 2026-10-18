"""Lệnh `agent-hub web [--host 127.0.0.1] [--port 8765] [--open] [--url]`.

- Chưa có server web chạy → khởi động (foreground, Ctrl+C để dừng), in link đăng nhập một lần; `--open` mở trình duyệt.
- Đã có server chạy (`HUB_HOME/auth/web.json`, pid còn sống) → chỉ xin link đăng nhập MỚI và in/mở — không mở server thứ hai.
- `--url`: chỉ in link (không khởi động server, không mở trình duyệt) — lỗi nếu chưa có server.
Host khác loopback bị từ chối (Q-11). Link chỉ in ra terminal của người chạy lệnh — không ghi vào log (access log tắt).
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import webbrowser
from typing import Any

from .security import WEB_HOST, NotLoopback, WebAuth, clear_state, login_url, mint_login_token, read_state, require_loopback, valid_web_name, write_state

DEFAULT_PORT = 8765


def _port_busy(host: str, port: int) -> bool:
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(fam, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host.strip("[]") if host != "localhost" else "127.0.0.1", port)) == 0


def _open(url: str) -> None:
    try:
        webbrowser.open(url, new=2)
    except Exception:  # pragma: no cover
        pass


def cmd_web(ctx: Any) -> int:
    a = ctx.args
    try:
        host = require_loopback(a.host)
    except NotLoopback as e:
        print(f"lỗi: {e}", file=sys.stderr)
        return 1
    name = (a.host_name or WEB_HOST).strip().lower()
    if not valid_web_name(name):
        print(f"lỗi: --host-name phải là *.localhost (mặc định {WEB_HOST}) hoặc loopback", file=sys.stderr)
        return 1
    if name != WEB_HOST and not name.endswith(".localhost"):
        print("CẢNH BÁO: dùng host loopback trực tiếp → cookie phiên cũng được gửi tới mọi app khác trên host đó (WT-1); "
              "khoá phiên ngoài cookie vẫn bảo vệ thao tác ghi.", file=sys.stderr)
    st = read_state(ctx.paths)
    if st is not None:
        url = login_url(st.get("name") or st["host"], int(st["port"]), mint_login_token(bytes.fromhex(st["key"])))
        print(f"Giao diện web đang chạy (pid {st['pid']}). Link đăng nhập một lần (hết hạn sau 5 phút):\n  {url}")
        if a.open and not a.url:
            _open(url)
        return 0
    if a.url:
        print("lỗi: chưa có server web nào chạy — chạy `agent-hub web --open`", file=sys.stderr)
        return 1
    if _port_busy(host, a.port):
        print(f"lỗi: cổng {a.port} đang bận — chọn cổng khác: agent-hub web --port {a.port + 1}", file=sys.stderr)
        return 1
    ctx.client()  # tự khởi động daemon nếu chưa chạy
    import uvicorn

    from .app import create_app

    auth = WebAuth()
    app = create_app(ctx.paths, auth=auth, host=host, port=a.port, name=name)
    write_state(ctx.paths, host=host, port=a.port, key=auth.key, name=name)
    url = login_url(name, a.port, auth.mint())
    print(f"agent-hub web: http://{name}:{a.port}/  (nghe trên {host}:{a.port}, chỉ máy này; Ctrl+C để dừng)")
    print(f"Link đăng nhập một lần (hết hạn sau 5 phút; lấy link mới: agent-hub web --open):\n  {url}", flush=True)
    if a.open:
        _open(url)
    try:
        uvicorn.run(app, host=host.strip("[]"), port=a.port, log_level="warning", access_log=False, server_header=False, proxy_headers=False)
    finally:
        clear_state(ctx.paths, os.getpid())
    return 0


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    w = sub.add_parser("web", help="giao diện web (trình duyệt) — chỉ localhost, đăng nhập bằng link một lần", parents=[common])
    w.add_argument("--host", default="127.0.0.1", help="địa chỉ bind — CHỈ loopback (127.0.0.1 / ::1 / localhost)")
    w.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"cổng (mặc định {DEFAULT_PORT})")
    w.add_argument("--host-name", default=None, help=f"tên host trên trình duyệt (mặc định {WEB_HOST} — cookie tách khỏi app khác ở 127.0.0.1)")
    w.add_argument("--open", action="store_true", help="mở trình duyệt và đăng nhập tự động")
    w.add_argument("--url", action="store_true", help="chỉ in link đăng nhập mới của server đang chạy")
    w.set_defaults(fn=cmd_web)
