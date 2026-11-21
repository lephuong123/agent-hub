"""CLI `cfharness admind serve|status|install` và `cfharness superadmin init|recover|call|serve`.

Lệnh chỉ người (∈ `guard.classifier.HUB_HUMAN_ONLY_CLI`). `admind serve`/`install`/`superadmin init|recover` cần root (sudo) trên máy;
`superadmin call` đăng nhập bằng tài khoản SAC (email + mật khẩu + TOTP) rồi gọi một thao tác (API-01).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Any


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    a = sub.add_parser("admind", help="tiến trình đặc quyền của Super Admin Console (root, macOS)", parents=[common])
    asub = a.add_subparsers(dest="admind_action", required=True)
    asub.add_parser("serve", help="chạy admind (LaunchDaemon gọi; cần root)", parents=[common])
    asub.add_parser("status", help="admind còn chạy không", parents=[common])
    ins = asub.add_parser("install", help="in (hoặc --write: cài) plist admind + SAC và user _cfh_sac", parents=[common])
    ins.add_argument("--write", action="store_true", help="ghi thật (cần root)")
    ins.add_argument("--port", type=int, default=8700, help="cổng loopback của web SAC")
    a.set_defaults(fn=cmd_admind)

    s = sub.add_parser("superadmin", help="Super Admin Console: khởi tạo, khôi phục, gọi thao tác, chạy web", parents=[common])
    ssub = s.add_subparsers(dest="sa_action", required=True)
    ini = ssub.add_parser("init", help="tạo Super Admin (cần root trên máy) — in link kích hoạt MỘT lần", parents=[common])
    ini.add_argument("--email", required=True)
    ini.add_argument("--name", required=True)
    rec = ssub.add_parser("recover", help="break-glass: cấp lại link kích hoạt cho Super Admin mất TOTP (cần root)", parents=[common])
    rec.add_argument("--email", required=True)
    call = ssub.add_parser("call", help="gọi một thao tác admind bằng tài khoản SAC (đăng nhập mỗi lần)", parents=[common])
    call.add_argument("op")
    call.add_argument("--params", default="{}", help="JSON tham số")
    call.add_argument("--email")
    srv = ssub.add_parser("serve", help="chạy web SAC (loopback; LaunchDaemon gọi bằng user _cfh_sac)", parents=[common])
    srv.add_argument("--port", type=int, default=8700)
    srv.add_argument("--host", default="127.0.0.1")
    s.set_defaults(fn=cmd_superadmin)


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0  # type: ignore[attr-defined]


def _out(ctx: Any, obj: Any, text: str) -> None:
    if getattr(ctx, "json", False):
        print(json.dumps(obj, ensure_ascii=False, default=str, indent=1))
    else:
        print(text)


def cmd_admind(ctx: Any) -> int:
    act = ctx.args.admind_action
    if act == "serve":
        return _serve()
    if act == "status":
        from .client import AdmindClient, AdmindError

        try:
            r = AdmindClient(timeout=5).call("sys.ping")
        except AdmindError as e:
            _out(ctx, {"up": False, "error": e.message}, f"admind: không chạy ({e.message})")
            return 1
        _out(ctx, {"up": True, **r}, f"admind: đang chạy (v{r.get('version')})")
        return 0
    if act == "install":
        from . import install

        if not ctx.args.write:
            p = install.plan(port=ctx.args.port)
            print(f"# {p['admind_plist']}\n{p['admind'].decode()}\n# {p['sac_plist']}\n{p['sac'].decode()}")  # type: ignore[union-attr]
            print("# lệnh (chạy bằng sudo, hoặc `sudo cfharness admind install --write`):")
            for c in p["commands"]:  # type: ignore[attr-defined]
                print("sudo " + " ".join(c))
            return 0
        if not _is_root():
            print("lỗi: --write cần root (sudo)", file=sys.stderr)
            return 2
        from ..admin_users import _default_runner

        for line in install.apply(_default_runner, port=ctx.args.port):
            print(line)
        return 0
    return 2


def _serve() -> int:
    import asyncio
    import logging

    if not _is_root() and os.environ.get("CFHARNESS_TESTING") != "1":
        print("lỗi: admind phải chạy bằng root (LaunchDaemon com.cfharness.admind)", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .dispatch import State
    from .runner import Runner
    from .scheduler import register
    from .server import Server

    state = State.open(runner=Runner())
    srv = Server(state)
    register(srv)
    try:
        asyncio.run(srv.serve())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


def cmd_superadmin(ctx: Any) -> int:
    from .client import AdmindClient, AdmindError

    act = ctx.args.sa_action
    if act == "serve":
        from ..superadmin.app import serve

        return serve(host=ctx.args.host, port=ctx.args.port)
    cl = AdmindClient()
    try:
        if act in ("init", "recover"):
            if not _is_root():
                print("lỗi: cần root (sudo) — thao tác này chỉ chạy trên chính máy chủ", file=sys.stderr)
                return 2
            op = "sys.bootstrap" if act == "init" else "sys.recover"
            params = {"email": ctx.args.email, **({"name": ctx.args.name} if act == "init" else {})}
            r = cl.call(op, params)
            msg = (f"Link kích hoạt (dùng MỘT lần, hạn 24 giờ — mở trên trình duyệt, đặt mật khẩu và quét TOTP):\n  {r['enroll_url']}")
            if act == "init" and r.get("super_admins", 0) <= 1:
                msg += "\nCảnh báo: hệ mới có 1 Super Admin — thao tác Tới hạn sẽ phải chờ 1 giờ thay cho người thứ hai duyệt. Nên có ≥ 2."
            _out(ctx, r, msg)
            return 0
        if act == "call":
            email = ctx.args.email or input("Email: ").strip()
            pw = getpass.getpass("Mật khẩu: ")
            code = input("Mã TOTP: ").strip()
            lg = cl.call("auth.login", {"email": email, "password": pw, "totp": code}, ip="loopback", ua="cli")
            try:
                params = json.loads(ctx.args.params)
            except ValueError:
                print("lỗi: --params không phải JSON", file=sys.stderr)
                return 2
            try:
                r = cl.call(ctx.args.op, params, ticket=lg["ticket"], ip="loopback", ua="cli")
            finally:
                try:
                    cl.call("auth.logout", ticket=lg["ticket"])
                except AdmindError:
                    pass
            print(json.dumps(r, ensure_ascii=False, default=str, indent=1))
            return 0
    except AdmindError as e:
        print(f"lỗi [{e.code}]: {e.message}", file=sys.stderr)
        return 1
    return 2
