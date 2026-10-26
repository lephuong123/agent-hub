"""CLI `cfharness notify slack setup|status|test|disable|allow|revoke` (client thuần của daemon, RPC `slack.*`).

`setup`: nhập bot token `xoxb-` + app token `xapp-` (ẩn, hoặc `--token-stdin`: 2 dòng) → daemon kiểm + lưu secret store (KHÔNG in ra)
→ in mã ghép nối → người nhắn `ghepnoi <mã>` cho bot trong DM → CLI hiện người vừa nhắn và hỏi xác nhận trước khi đưa vào allowlist.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from typing import Any

from ..cli import _ask


def add_parser(ns: Any, common: argparse.ArgumentParser) -> None:
    sl = ns.add_parser("slack", help="Slack 2 chiều (Socket Mode): thông báo + duyệt bằng nút + lệnh /cfh", parents=[common])
    ss = sl.add_subparsers(dest="sl_action", required=True)
    s = ss.add_parser("setup", help="nhập bot token + app token, ghép nối DM của bạn", parents=[common])
    s.add_argument("--token-stdin", action="store_true", help="đọc 2 dòng từ stdin: bot token (xoxb-) rồi app token (xapp-)")
    s.add_argument("--keep-token", action="store_true", help="giữ token đã lưu, chỉ ghép nối thêm người")
    s.add_argument("--wait", type=float, default=300, help="chờ tin ghepnoi tối đa (giây)")
    s.add_argument("--yes", "-y", action="store_true", help="tự xác nhận người đầu tiên nhắn đúng mã")
    ss.add_parser("status", help="trạng thái bot (không in token)", parents=[common])
    t = ss.add_parser("test", help="gửi tin thử tới các DM trong allowlist", parents=[common])
    t.add_argument("--text")
    dd = ss.add_parser("disable", help="tắt Slack 2 chiều (webhook một chiều, nếu có, vẫn chạy)", parents=[common])
    dd.add_argument("--forget-token", action="store_true", help="xoá luôn bot token + app token khỏi secret store")
    a = ss.add_parser("allow", help="thêm DM/kênh vào allowlist", parents=[common])
    a.add_argument("chat_id")
    a.add_argument("--user-id", help="user id Slack (U…) của người được phép")
    r = ss.add_parser("revoke", help="gỡ DM/kênh khỏi allowlist", parents=[common])
    r.add_argument("chat_id")
    sl.set_defaults(fn=cmd_slack)


def cmd_slack(ctx: Any) -> int:
    from ...cli import eprint

    a = ctx.args
    act = a.sl_action
    if act == "status":
        st = ctx.call("slack.status")
        if ctx.json:
            ctx.out(st)
            return 0
        if not st.get("configured") or not st.get("app_token"):
            print("Slack 2 chiều: CHƯA cấu hình — chạy `cfharness notify slack setup` (tạo app từ manifest trên web: Cài đặt → Slack).")
            return 0
        print(f"Slack 2 chiều: {'BẬT' if st['interactive'] and st['enabled'] else 'TẮT'} · workspace {st.get('team_id') or '?'}"
              f" · lệnh {st.get('slash_command')} · token: đã lưu (secret store)")
        print(f"  allowlist: {', '.join(st['allowed_chat_ids']) or '(trống)'} · người: {', '.join(st['allowed_user_ids']) or '(trống)'}")
        print(f"  Socket Mode: {'đã kết nối' if st['polling'] else 'chưa kết nối'}" + (f" · lỗi gần nhất: {st['last_error']}" if st.get("last_error") else ""))
        print(f"  đã gửi {st['sent']} · lỗi {st['failed']} · nút còn hiệu lực {st['active_buttons']} · run đang theo dõi {st['watching_runs']}")
        return 0
    if act == "test":
        r = ctx.call("slack.test", {"text": a.text})
        ctx.out(r, f"đã gửi tới {r.get('chats')} DM" if r.get("sent") else f"không gửi: {r.get('reason')}")
        return 0 if r.get("sent") else 1
    if act == "disable":
        r = ctx.call("slack.disable", {"forget_token": a.forget_token})
        ctx.out(r, "đã tắt Slack 2 chiều" + (" và xoá token" if r.get("token_removed") else ""))
        return 0
    if act in ("allow", "revoke"):
        r = ctx.call(f"slack.{act}", {"chat_id": a.chat_id, **({"user_id": a.user_id} if getattr(a, "user_id", None) else {})})
        ctx.out(r, f"allowlist: {', '.join(r['allowed_chat_ids']) or '(trống)'}")
        return 0
    # ------------------------------------------------------------------ setup
    params: dict[str, Any] = {}
    if not a.keep_token:
        if a.token_stdin:
            bt, at = sys.stdin.readline().strip(), sys.stdin.readline().strip()
        else:
            if not sys.stdin.isatty():
                eprint("lỗi: không có terminal để nhập token — dùng --token-stdin")
                return 1
            print("Tạo app: web cfharness → Cài đặt → Slack → 'Tạo app Slack 2 chiều' (manifest điền sẵn) → Install to Workspace.")
            bt = getpass.getpass("Bot User OAuth Token (xoxb-…, không hiện khi gõ): ").strip()
            at = getpass.getpass("App-Level Token (xapp-…, scope connections:write): ").strip()
        if not bt or not at:
            eprint("lỗi: thiếu token")
            return 1
        params.update({"bot_token": bt, "app_token": at})
    r = ctx.call("slack.setup", params)
    print(f"Đã kết nối workspace {r.get('team') or r.get('team_id')} (bot {r.get('bot_name') or r.get('bot_user_id')}). Token lưu trong secret store — không in ra.")
    code = r["pair_code"]
    print(f"\nBước tiếp: mở Slack, nhắn DM cho bot:  ghepnoi {code}")
    print(f"  (hoặc gõ ở bất kỳ đâu: {r.get('slash_command') or '/cfh'} ghepnoi {code})")
    print(f"Mã dùng một lần, hết hạn sau {int(r.get('pair_ttl_s') or 600) // 60} phút. Đang chờ… (Ctrl+C để thôi)")
    deadline = time.monotonic() + float(a.wait)
    seen: set[str] = set()
    try:
        while time.monotonic() < deadline:
            for c in ctx.call("slack.pending", {"code": code}) or []:
                if c["chat_id"] in seen:
                    continue
                seen.add(c["chat_id"])
                who = " ".join(x for x in (c.get("name"), f"@{c['username']}" if c.get("username") else None) if x) or "(chưa rõ tên)"
                print(f"\nNgười vừa nhắn đúng mã: {who} · user {c.get('user_id')} · DM {c['chat_id']}")
                if a.yes or (sys.stdin.isatty() and _ask("Đưa người này vào allowlist (nhận thông báo + điều khiển hub)? [y/N] ")):
                    rr = ctx.call("slack.allow", {"chat_id": c["chat_id"], "user_id": c.get("user_id")})
                    print(f"Đã thêm. allowlist: {', '.join(rr['allowed_chat_ids'])}. Thử: cfharness notify slack test")
                    return 0
                print(f"Bỏ qua. (Thêm sau: cfharness notify slack allow {c['chat_id']} --user-id {c.get('user_id')})")
            time.sleep(1.5)
    except KeyboardInterrupt:
        print()
    eprint("chưa ghép nối ai — chạy lại `cfharness notify slack setup --keep-token` khi sẵn sàng")
    return 1
