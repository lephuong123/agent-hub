"""CLI `cfharness account list|add|login|use|rename|rm|check` (client thuần của daemon, RPC `account.*`).

Đổi tài khoản chỉ bằng lệnh của NGƯỜI (`account use`) — hub không tự đổi. Token không bao giờ in ra.
"""

from __future__ import annotations

import argparse
import getpass
from typing import Any

STATUS_VI = {"ok": "ổn", "auth_error": "lỗi đăng nhập", "rate_limited": "hết hạn mức", "expiring": "sắp hết hạn", "unknown": "chưa kiểm"}


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    s = sub.add_parser("account", help="tài khoản Claude của hub: thêm, xem, chọn tài khoản đang dùng (đổi do người, không tự đổi)", parents=[common])
    ss = s.add_subparsers(dest="acct_action")
    ss.add_parser("list", help="danh sách tài khoản + tài khoản đang dùng", parents=[common])
    ad = ss.add_parser("add", help="thêm tài khoản bằng token `claude setup-token` (không có --token: nhập ẩn)", parents=[common])
    ad.add_argument("label")
    ad.add_argument("--token", default=None, help="token sk-ant-oat01-… (nên bỏ trống để nhập ẩn — tránh lộ qua lịch sử shell)")
    lg = ss.add_parser("login", help="thêm tài khoản bằng cách đăng nhập: mở link, đăng nhập, dán code", parents=[common])
    lg.add_argument("label")
    us = ss.add_parser("use", help="dùng tài khoản này cho run mới (`machine` = đăng nhập sẵn của máy)", parents=[common])
    us.add_argument("id")
    rn = ss.add_parser("rename", help="đổi tên tài khoản", parents=[common])
    rn.add_argument("id")
    rn.add_argument("label")
    rm = ss.add_parser("rm", help="xoá tài khoản (không xoá được tài khoản đang dùng)", parents=[common])
    rm.add_argument("id")
    ck = ss.add_parser("check", help="kiểm `claude auth status` với token của tài khoản", parents=[common])
    ck.add_argument("id")
    s.set_defaults(fn=cmd_account)


def _line(a: dict[str, Any]) -> str:
    mark = "●" if a.get("active") else "○"
    st = STATUS_VI.get(a.get("status") or "unknown", a.get("status") or "")
    extra = []
    if a.get("expires_at"):
        extra.append(f"hết hạn {str(a['expires_at'])[:10]}")
    if a.get("rate_limit"):
        extra.append(f"chờ tới {str(a['rate_limit'].get('until') or '')[:16]}")
    if a.get("cost_today_usd"):
        extra.append(f"hôm nay ${a['cost_today_usd']:.2f}")
    if a.get("last_error") and a.get("status") in ("auth_error", "unknown"):
        extra.append(str(a["last_error"])[:80])
    return f"  {mark} {a['id']:<12} {a['label'][:32]:<32} {st:<14}" + (" · " + " · ".join(extra) if extra else "")


def cmd_account(ctx: Any) -> int:
    a = ctx.args
    act = a.acct_action or "list"
    if act == "list":
        r = ctx.call("account.list", {})
        if ctx.json:
            ctx.out(r)
            return 0
        for acc in r["accounts"]:
            print(_line(acc))
        if r.get("active") is None:
            print("Chưa chọn tài khoản — claude dùng đăng nhập sẵn của máy. Thêm: `cfharness account login <tên>`.")
        return 0
    if act == "add":
        token = a.token or getpass.getpass("Token (sk-ant-oat01-…, không hiện khi gõ): ")
        r = ctx.call("account.add_token", {"label": a.label, "token": token})
        acc = r["account"]
        ctx.out(r) if ctx.json else print(f"Đã thêm tài khoản {acc['id']} ({acc['label']}). Dùng: `cfharness account use {acc['id']}`.")
        return 0
    if act == "login":
        st = ctx.call("account.login_start", {"label": a.label})
        print("Mở link sau trong trình duyệt, đăng nhập tài khoản Claude, rồi dán code trang đó hiển thị:")
        print(f"  {st['url']}")
        try:
            code = input("Code: ").strip()
        except (KeyboardInterrupt, EOFError):
            ctx.call("account.login_cancel", {"login_id": st["login_id"]})
            print("\nĐã huỷ đăng nhập.")
            return 1
        if not code:
            ctx.call("account.login_cancel", {"login_id": st["login_id"]})
            print("Không có code — đã huỷ đăng nhập.")
            return 1
        r = ctx.call("account.login_finish", {"login_id": st["login_id"], "code": code}, timeout=120)
        acc = r["account"]
        ctx.out(r) if ctx.json else print(f"Đã thêm tài khoản {acc['id']} ({acc['label']}). Dùng: `cfharness account use {acc['id']}`.")
        return 0
    if act == "use":
        r = ctx.call("account.use", {"id": a.id})
        ctx.out(r) if ctx.json else print(f"Đang dùng tài khoản {r['active']} (trước: {r['previous'] or 'chưa chọn'}). Run mới dùng tài khoản này; "
                                          "run đang chạy giữ tài khoản cũ.")
        return 0
    if act == "rename":
        r = ctx.call("account.rename", {"id": a.id, "label": a.label})
        ctx.out(r) if ctx.json else print(f"Đã đổi tên {r['account']['id']} → {r['account']['label']}.")
        return 0
    if act == "rm":
        r = ctx.call("account.rm", {"id": a.id})
        ctx.out(r) if ctx.json else print(f"Đã xoá tài khoản {a.id}.")
        return 0
    r = ctx.call("account.check", {"id": a.id}, timeout=120)
    ctx.out(r) if ctx.json else print(_line(r["account"]))
    return 0
