"""CLI `agent-hub notify telegram setup|status|test|disable|allow|revoke` (client thuần của daemon, RPC `telegram.*`).

`setup`: nhập token bot (ẩn, hoặc `--token-stdin`) → daemon kiểm bằng getMe + lưu secret store (KHÔNG in ra) → in mã ghép
nối → người nhắn `/start <mã>` cho bot → CLI hiện chat vừa nhắn và hỏi xác nhận trước khi đưa vào allowlist.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import time
from typing import Any


def add_notify_parsers(ns: Any, common: argparse.ArgumentParser) -> None:
    tg = ns.add_parser("telegram", help="bot Telegram: thông báo + duyệt bằng nút + lệnh chat", parents=[common])
    ts = tg.add_subparsers(dest="tg_action", required=True)
    s = ts.add_parser("setup", help="nhập token bot (@BotFather) + ghép nối chat của bạn", parents=[common])
    s.add_argument("--token-stdin", action="store_true", help="đọc token từ stdin (không hỏi)")
    s.add_argument("--keep-token", action="store_true", help="giữ token đã lưu, chỉ ghép nối thêm chat")
    s.add_argument("--chat-id", help="đưa thẳng chat_id này vào allowlist (bỏ bước /start)")
    s.add_argument("--wait", type=float, default=300, help="chờ tin /start tối đa (giây)")
    s.add_argument("--yes", "-y", action="store_true", help="tự xác nhận chat đầu tiên nhắn đúng mã")
    ts.add_parser("status", help="trạng thái bot (không in token)", parents=[common])
    t = ts.add_parser("test", help="gửi tin thử tới các chat trong allowlist", parents=[common])
    t.add_argument("--text")
    dd = ts.add_parser("disable", help="tắt kênh Telegram", parents=[common])
    dd.add_argument("--forget-token", action="store_true", help="xoá luôn token khỏi secret store")
    a = ts.add_parser("allow", help="thêm chat_id vào allowlist", parents=[common])
    a.add_argument("chat_id")
    a.add_argument("--user-id", help="from.id người được phép (bắt buộc với nhóm)")
    r = ts.add_parser("revoke", help="gỡ chat_id khỏi allowlist", parents=[common])
    r.add_argument("chat_id")
    tg.set_defaults(fn=cmd_telegram)


def _ask(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes", "c", "co", "có")
    except EOFError:
        return False


def cmd_telegram(ctx: Any) -> int:
    from ..cli import eprint

    a = ctx.args
    act = a.tg_action
    if act == "status":
        st = ctx.call("telegram.status")
        if ctx.json:
            ctx.out(st)
            return 0
        if not st.get("configured"):
            print("Telegram: CHƯA cấu hình — chạy `agent-hub notify telegram setup` (tạo bot bằng @BotFather trước).")
            return 0
        print(f"Telegram: {'BẬT' if st['enabled'] else 'TẮT'} · bot @{st.get('bot_username') or '?'} · token: đã lưu (secret store)")
        print(f"  allowlist: {', '.join(st['allowed_chat_ids']) or '(trống)'}")
        print(f"  long polling: {'đang chạy' if st['polling'] else 'không'}" + (f" · lỗi gần nhất: {st['last_error']}" if st.get("last_error") else ""))
        print(f"  đã gửi {st['sent']} · lỗi {st['failed']} · nút còn hiệu lực {st['active_buttons']} · run đang theo dõi {st['watching_runs']}")
        return 0
    if act == "test":
        r = ctx.call("telegram.test", {"text": a.text})
        ctx.out(r, f"đã gửi tới {r.get('chats')} chat" if r.get("sent") else f"không gửi: {r.get('reason')}")
        return 0 if r.get("sent") else 1
    if act == "disable":
        r = ctx.call("telegram.disable", {"forget_token": a.forget_token})
        ctx.out(r, "đã tắt Telegram" + (" và xoá token" if r.get("token_removed") else ""))
        return 0
    if act in ("allow", "revoke"):
        r = ctx.call(f"telegram.{act}", {"chat_id": a.chat_id, **({"user_id": a.user_id} if getattr(a, "user_id", None) else {})})
        ctx.out(r, f"allowlist: {', '.join(r['allowed_chat_ids']) or '(trống)'}")
        return 0
    # ------------------------------------------------------------------ setup
    params: dict[str, Any] = {}
    if not a.keep_token:
        if a.token_stdin:
            token = sys.stdin.readline().strip()
        else:
            if not sys.stdin.isatty():
                eprint("lỗi: không có terminal để nhập token — dùng --token-stdin")
                return 1
            print("Tạo bot: nhắn @BotFather trên Telegram → /newbot → chép token (dạng 123456:ABC…).")
            token = getpass.getpass("Token bot (không hiện khi gõ): ").strip()
        if not token:
            eprint("lỗi: token rỗng")
            return 1
        params["token"] = token
    r = ctx.call("telegram.setup", params)
    print(f"Đã kết nối bot @{r.get('bot_username')}. Token lưu trong secret store (TELEGRAM_BOT_TOKEN) — không in ra.")
    if a.chat_id:
        rr = ctx.call("telegram.allow", {"chat_id": a.chat_id})
        print(f"Đã thêm chat {a.chat_id}. allowlist: {', '.join(rr['allowed_chat_ids'])}")
        return 0
    code = r["pair_code"]
    print(f"\nBước tiếp: mở Telegram, nhắn cho bot @{r.get('bot_username')}:  /start {code}")
    if r.get("deep_link"):
        print(f"  (hoặc mở link: {r['deep_link']} rồi bấm Start)")
    print(f"Mã dùng một lần, hết hạn sau {int(r.get('pair_ttl_s') or 600) // 60} phút. Đang chờ… (Ctrl+C để thôi)")
    deadline = time.monotonic() + float(a.wait)
    seen: set[str] = set()
    try:
        while time.monotonic() < deadline:
            for c in ctx.call("telegram.pending", {"code": code}) or []:
                if c["chat_id"] in seen:
                    continue
                seen.add(c["chat_id"])
                who = " ".join(x for x in (c.get("name"), f"@{c['username']}" if c.get("username") else None) if x) or "(không tên)"
                print(f"\nChat vừa nhắn đúng mã: {who} · chat_id {c['chat_id']} · loại {c.get('type')}")
                if a.yes or (sys.stdin.isatty() and _ask("Đưa chat này vào allowlist (nhận thông báo + điều khiển hub)? [y/N] ")):
                    rr = ctx.call("telegram.allow", {"chat_id": c["chat_id"]})
                    print(f"Đã thêm. allowlist: {', '.join(rr['allowed_chat_ids'])}. Thử: agent-hub notify telegram test")
                    return 0
                print(f"Bỏ qua. (Thêm sau: agent-hub notify telegram allow {c['chat_id']})")
            time.sleep(1.5)
    except KeyboardInterrupt:
        print()
    eprint("chưa ghép nối chat nào — chạy lại `agent-hub notify telegram setup --keep-token` khi sẵn sàng")
    return 1
