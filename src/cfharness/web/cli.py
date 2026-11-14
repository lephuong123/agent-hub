"""Lệnh `cfharness web [--host 127.0.0.1] [--port 8765] [--open] [--url]`.

- Chưa có server web chạy → khởi động (foreground, Ctrl+C để dừng), in link đăng nhập một lần; `--open` mở trình duyệt.
- Đã có server chạy (`HUB_HOME/auth/web.json`, pid còn sống) → chỉ xin link đăng nhập MỚI và in/mở — không mở server thứ hai.
- `--url`: chỉ in link (không khởi động server, không mở trình duyệt) — lỗi nếu chưa có server.
Host khác loopback bị từ chối (Q-11). Link chỉ in ra terminal của người chạy lệnh — không ghi vào log (access log tắt).
- `--no-link`: không in link (chạy làm dịch vụ — stdout vào file log mà agent có thể đọc).
- `web.public_origin` (nếu người đã đặt) được đọc qua RPC `config.get` lúc khởi động: link in ra dùng origin công khai (https qua tunnel).
- `cfharness web totp enroll|status|disable`: đăng nhập bằng mã TOTP cho web public (web/totp.py, docs/dev-notes/web-public.md).
- `cfharness web devices list [--json] | revoke <id> | revoke-all`: thiết bị tin cậy (web/devices.py) — phiên web đang mở trên thiết bị bị
  thu hồi chết ngay ở request kế tiếp (server web đọc lại file theo mtime). Tắt / đăng ký lại TOTP cũng thu hồi mọi thiết bị.
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


def _web_cfg(ctx: Any) -> dict[str, Any]:
    """Nhánh `web` của config qua daemon (tự khởi động). Lỗi → {} (chỉ dùng cục bộ)."""
    try:
        v = ctx.call("config.get", {"key": "web"})
    except Exception:  # noqa: BLE001
        return {}
    return v if isinstance(v, dict) else {}


def _public_origin(cfg: dict[str, Any]) -> str | None:
    from .public import parse_public_origin

    try:
        return parse_public_origin(cfg.get("public_origin"))
    except ValueError as e:
        print(f"CẢNH BÁO: bỏ qua web.public_origin — {e}", file=sys.stderr)
        return None


def cmd_totp(ctx: Any, words: list[str]) -> int:
    from .totp import ENROLL_TTL_S, TotpStore, otpauth_uri

    a = ctx.args
    act = words[1] if len(words) > 1 else "status"
    store = TotpStore(ctx.paths)
    if act == "status":
        st = store.status()
        ctx.out(st, ("TOTP: ĐÃ BẬT" if st["enrolled"] else "TOTP: chưa bật") +
                (f"; có link đăng ký đang chờ{' (đã mở)' if st['pending_viewed'] else ''}" if st["pending"] else ""))
        return 0
    if act == "disable":
        gone = store.disable()
        _audit(ctx, "web.totp_disabled")
        if store.devices_forgotten:
            _notify(ctx, "web.devices_revoked_all", {"count": store.devices_forgotten, "reason": "totp_disabled"})
        ctx.out({"disabled": gone}, "đã tắt đăng nhập TOTP (phiên web đang mở vẫn còn tới khi hết hạn — khởi động lại web để huỷ ngay)"
                if gone else "TOTP chưa bật")
        return 0
    if act != "enroll":
        print("lỗi: dùng `cfharness web totp enroll|status|disable`", file=sys.stderr)
        return 1
    cfg = _web_cfg(ctx)
    origin = _public_origin(cfg)
    ttl = int(a.ttl or ENROLL_TTL_S)
    secret, token = store.enroll(ttl_s=ttl, activate=bool(a.activate))
    from .public import public_hosts

    account = sorted(public_hosts(origin))[0] if origin else os.environ.get("USER") or "hub"
    st = read_state(ctx.paths)
    base = origin or (f"http://{st.get('name') or st['host']}:{st['port']}" if st else f"http://{WEB_HOST}:{DEFAULT_PORT}")
    link = f"{base}/enroll?t={token}"
    _audit(ctx, "web.totp_enrolled", {"activate": bool(a.activate), "ttl_s": ttl})
    if store.devices_forgotten:
        _notify(ctx, "web.devices_revoked_all", {"count": store.devices_forgotten, "reason": "totp_reenrolled"})
    print("Đăng ký đăng nhập TOTP cho web của hub này.")
    if getattr(a, "show_uri", False):  # mặc định KHÔNG in bí mật: màn hình/log/scrollback hay bị chép vào chat — link /enroll đã đủ
        print(f"URI cho ứng dụng xác thực (BÍ MẬT — chỉ trao tận tay người dùng, đừng dán vào chat/log):\n  {otpauth_uri(secret, account)}")
    print(f"Link đăng ký MỘT LẦN (hết hạn sau {ttl // 3600 or 1} giờ; hiện mã đúng một lần, người dùng nhập mã để kích hoạt):\n  {link}")
    if a.activate:
        print("--activate: mã TOTP có hiệu lực NGAY (thay mã cũ nếu có).")
    elif store.enrolled():
        print("Mã cũ vẫn dùng được cho tới khi người dùng kích hoạt mã mới qua link.")
    if not origin:
        print("Lưu ý: chưa đặt web.public_origin — link trên chỉ mở được trên máy này (`cfharness config set web.public_origin https://…`).")
    return 0


def _notify(ctx: Any, kind: str, data: dict[str, Any]) -> None:
    """`web.notify` (HUMAN_ONLY: audit + event → Telegram). Daemon cũ (chưa có method) → audit thường."""
    try:
        ctx.call("web.notify", {"kind": kind, "data": data, "channel": "cli"})
    except Exception:  # noqa: BLE001
        _audit(ctx, kind, data)


def cmd_devices(ctx: Any, words: list[str]) -> int:
    import time as _t
    from datetime import datetime

    from .devices import DID_RE, DeviceStore

    act = words[1] if len(words) > 1 else "list"
    store = DeviceStore(ctx.paths)
    if act == "list":
        rows = store.list()

        def _d(v: Any) -> str:
            return datetime.fromtimestamp(float(v)).strftime("%d/%m/%Y %H:%M") if v else "—"

        lines = [f"{r['did']}  {r['ua_label']:<24}  dùng {_d(r.get('last_used_at'))}  IP {r.get('last_ip') or '?'}  hết hạn {_d(r.get('expires_at'))}"
                 for r in rows]
        ctx.out(rows, "\n".join(lines) if lines else "chưa có thiết bị tin cậy nào")
        return 0
    if act == "revoke":
        did = words[2] if len(words) > 2 else ""
        if not DID_RE.match(did):
            print("lỗi: dùng `cfharness web devices revoke <id>` (id từ `cfharness web devices list`)", file=sys.stderr)
            return 1
        pub = store.revoke(did)
        if pub is None:
            print(f"lỗi: không có thiết bị {did}", file=sys.stderr)
            return 1
        _notify(ctx, "web.device_revoked", {"did": did, "reason": "user", "ua_label": pub.get("ua_label")})
        ctx.out(pub, f"đã thu hồi thiết bị {did} ({pub.get('ua_label')}) — phiên web đang mở trên thiết bị đó bị đăng xuất ngay")
        return 0
    if act == "revoke-all":
        n = store.revoke_all()
        _notify(ctx, "web.devices_revoked_all", {"count": n, "reason": "user"})
        ctx.out({"count": n, "at": _t.time()}, f"đã thu hồi {n} thiết bị tin cậy")
        return 0
    print("lỗi: dùng `cfharness web devices list|revoke <id>|revoke-all`", file=sys.stderr)
    return 1


def _audit(ctx: Any, kind: str, data: dict[str, Any] | None = None) -> None:
    try:
        ctx.call("audit.record", {"kind": kind, "actor": "user", "channel": "cli", "summary": kind, "data": data or {}})
    except Exception:  # noqa: BLE001
        pass


def cmd_web(ctx: Any) -> int:
    a = ctx.args
    words = list(getattr(a, "words", None) or [])
    if words:
        if words[0] == "totp":
            return cmd_totp(ctx, words)
        if words[0] == "devices":
            return cmd_devices(ctx, words)
        print(f"lỗi: lệnh con lạ `{words[0]}` — dùng `cfharness web [--open]`, `cfharness web totp enroll|status|disable` hoặc "
              "`cfharness web devices list|revoke <id>|revoke-all`", file=sys.stderr)
        return 1
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
        key = bytes.fromhex(st["key"])
        url = login_url(st.get("name") or st["host"], int(st["port"]), mint_login_token(key))
        print(f"Giao diện web đang chạy (pid {st['pid']}). Link đăng nhập một lần (hết hạn sau 5 phút):\n  {url}")
        if st.get("public_origin"):
            print(f"Qua Internet (tunnel):\n  {login_url('', 0, mint_login_token(key), st['public_origin'])}")
        if a.open and not a.url:
            _open(url)
        return 0
    if a.url:
        print("lỗi: chưa có server web nào chạy — chạy `cfharness web --open`", file=sys.stderr)
        return 1
    if _port_busy(host, a.port):
        print(f"lỗi: cổng {a.port} đang bận — chọn cổng khác: cfharness web --port {a.port + 1}", file=sys.stderr)
        return 1
    ctx.client()  # tự khởi động daemon nếu chưa chạy
    import uvicorn

    from .app import create_app
    from .public import LoginLimiter

    cfg = _web_cfg(ctx)
    origin = _public_origin(cfg)
    auth = WebAuth(idle_s=int(cfg.get("session_idle_s") or 3600), max_s=int(cfg.get("session_max_age_s") or 12 * 3600))
    app = create_app(ctx.paths, auth=auth, host=host, port=a.port, name=name, public_origin=origin,
                     ip_header=str(cfg.get("client_ip_header") or "x-forwarded-for"), limiter=LoginLimiter.from_config(cfg.get("login")),
                     trusted_devices=cfg.get("trusted_devices") if isinstance(cfg.get("trusted_devices"), dict) else None)
    write_state(ctx.paths, host=host, port=a.port, key=auth.key, name=name, public_origin=origin)
    print(f"cfharness web: http://{name}:{a.port}/  (nghe trên {host}:{a.port}, chỉ máy này; Ctrl+C để dừng)")
    if origin:
        print(f"Web public (qua tunnel TLS): {origin}/  — đăng nhập bằng mã TOTP (`cfharness web totp enroll`)")
    if not a.no_link:
        url = login_url(name, a.port, auth.mint())
        print(f"Link đăng nhập một lần (hết hạn sau 5 phút; lấy link mới: cfharness web --open):\n  {url}", flush=True)
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
    w.add_argument("--no-link", action="store_true", help="không in link đăng nhập (chạy làm dịch vụ: stdout vào file log)")
    w.add_argument("words", nargs="*", help="`totp enroll|status|disable`: đăng nhập bằng mã TOTP cho web public; "
                   "`devices list|revoke <id>|revoke-all`: thiết bị tin cậy (chỉ người)")
    w.add_argument("--ttl", type=int, default=None, help="totp enroll: hạn của link đăng ký (giây, mặc định 86400)")
    w.add_argument("--activate", action="store_true", help="totp enroll: dùng mã mới NGAY (không chờ người dùng xác nhận qua link)")
    w.add_argument("--show-uri", action="store_true", help="totp enroll: in cả URI otpauth:// (chứa BÍ MẬT) — mặc định chỉ in link đăng ký một lần")
    w.set_defaults(fn=cmd_web)
