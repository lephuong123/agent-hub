"""CLI `cfharness filehub setup|login|status|add|remove|pause|resume` — client thuần của daemon (RPC `filehub.*`).

`setup`/`login` in link đồng ý thiết bị (dòng `LOGIN_URL` của binary sync) rồi chờ người bấm đồng ý trên Không gian số.
"""

from __future__ import annotations

import argparse
import time
from typing import Any

STATE_VI = {"idle": "đã đồng bộ", "syncing": "đang đồng bộ", "conflict": "có bản xung đột", "offline": "mất kết nối", "auth_expired": "hết phiên đăng nhập",
            "error": "lỗi", "paused": "tạm dừng", "not_configured": "chưa thiết lập", "version_mismatch": "binary sync sai phiên bản", "starting": "đang khởi động"}


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    s = sub.add_parser("filehub", help="thư mục dùng chung với \"Không gian số\" (đồng bộ 2 chiều; agent làm việc trong workspace filehub:<tên>)",
                       parents=[common])
    ss = s.add_subparsers(dest="fh_action")
    st = ss.add_parser("setup", help="thiết lập: server + binary sync, đăng nhập (đồng ý thiết bị), tạo thư mục rỗng (chỉ người)", parents=[common])
    st.add_argument("--server", help="URL Không gian số, vd https://thumucai.trungtamdhs.com")
    st.add_argument("--binary", help="đường dẫn tang-thu-sync-headless")
    st.add_argument("--name", default="cfharness", help="tên thư mục chung (mặc định cfharness)")
    st.add_argument("--local-parent", help="thư mục cha của bản sao cục bộ (mặc định ~/KhongGianSo)")
    st.add_argument("--local-path", help="đường dẫn bản sao cục bộ (mặc định <local-parent>/<tên>)")
    st.add_argument("--no-browser", action="store_true", help="không tự mở trình duyệt — chỉ in link")
    st.add_argument("--no-wait", action="store_true", help="in link rồi thoát, không chờ đồng ý")
    lg = ss.add_parser("login", help="đăng nhập lại (khi hết phiên) — chỉ người", parents=[common])
    lg.add_argument("--no-browser", action="store_true")
    lg.add_argument("--no-wait", action="store_true")
    ss.add_parser("status", help="trạng thái đồng bộ, thư mục chung, bản xung đột", parents=[common])
    ad = ss.add_parser("add", help="thêm một thư mục chung từ root đã cấp lúc đăng nhập (chỉ người)", parents=[common])
    ad.add_argument("root_id")
    ad.add_argument("--name")
    ad.add_argument("--local-path")
    rm = ss.add_parser("remove", help="gỡ thư mục chung khỏi hub (không xoá tệp trên máy lẫn trên Không gian số; chỉ người)", parents=[common])
    rm.add_argument("name")
    ss.add_parser("pause", help="tạm dừng đồng bộ (dừng tiến trình sync)", parents=[common])
    rs = ss.add_parser("resume", help="chạy lại đồng bộ; --root <id>: cho chiều đẩy lên đang bị chặn (xoá hàng loạt) đi tiếp", parents=[common])
    rs.add_argument("--root")
    s.set_defaults(fn=cmd_filehub)


def _print_status(r: dict[str, Any]) -> None:
    sv = r.get("supervisor") or {}
    print(f"Không gian số: {r.get('server') or '(chưa đặt)'} · thiết bị «{r.get('device')}» · {STATE_VI.get(r['state'], r['state'])}"
          + (f" · sync {r['sync_version']}" if r.get("sync_version") else ""))
    if sv.get("last_error") and r["state"] in ("error", "version_mismatch"):
        print(f"  lỗi: {sv['last_error']}")
    if not r.get("folders"):
        print("  (chưa có thư mục chung — `cfharness filehub setup --server URL --binary /đường/dẫn/tang-thu-sync-headless`)")
    for f in r.get("folders") or []:
        lock = f.get("lock")
        print(f"  • {f['name']}  →  {f['local_path']}  (workspace filehub:{f['name']})"
              + (f"  ⬆{f['pending_up']} ⬇{f['pending_down']}" if f.get("pending_up") or f.get("pending_down") else "")
              + (f"  · đang có run {lock.get('run_id')} ghi" if lock else ""))
        if f.get("blocked"):
            print(f"    ⛔ chiều đẩy lên đang dừng ({f['blocked']}) — `cfharness filehub resume --root {f['root_id']}` để đẩy tiếp")
        for c in f.get("conflicts") or []:
            print(f"    ⚠ bản xung đột: {c['path']}")
        if f.get("url"):
            print(f"    mở: {f['url']}")
    lg = r.get("login") or {}
    if lg.get("state") in ("waiting", "error"):
        print(f"  đăng nhập: {lg['state']}" + (f" — {lg.get('error')}" if lg.get("error") else f" — {lg.get('url')}"))


def _wait_login(ctx: Any, timeout_s: float = 900) -> int:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        st = ctx.call("filehub.status", {})
        lg = st.get("login") or {}
        if lg.get("state") == "done":
            names = ", ".join(r.get("name", "?") for r in lg.get("roots") or [])
            print(f"Đã đăng nhập. Thư mục được cấp: {names or '(không có)'}")
            _print_status(st)
            return 0
        if lg.get("state") == "error":
            print(f"lỗi đăng nhập: {lg.get('error')}")
            return 1
        time.sleep(1.5)
    print("hết thời gian chờ đồng ý — chạy lại `cfharness filehub login`")
    return 1


def cmd_filehub(ctx: Any) -> int:
    a = ctx.args
    act = a.fh_action or "status"
    if act == "status":
        r = ctx.call("filehub.status", {})
        if ctx.json:
            ctx.out(r)
        else:
            _print_status(r)
        return 0
    if act in ("setup", "login"):
        if act == "setup":
            r = ctx.call("filehub.setup", {"server": a.server, "binary": a.binary, "name": a.name, "local_parent": a.local_parent,
                                           "local_path": a.local_path, "no_browser": a.no_browser})
        else:
            r = ctx.call("filehub.login", {"no_browser": a.no_browser})
        if ctx.json:
            ctx.out(r)
            return 0
        print(f"Mở link sau để đồng ý thiết bị «cfharness» (chọn/tạo thư mục rỗng ngay ở trang đồng ý):\n  {r['login_url']}")
        return 0 if a.no_wait else _wait_login(ctx)
    if act == "add":
        r = ctx.call("filehub.add", {"root_id": a.root_id, "name": a.name, "local_path": a.local_path})
        ctx.out(r, f"Đã thêm thư mục chung «{r['name']}» → {r['local_path']} (giao việc: workspace {r['workspace']})")
        return 0
    if act == "remove":
        r = ctx.call("filehub.remove", {"name": a.name})
        ctx.out(r, f"Đã gỡ «{r['removed']}» khỏi hub — {r['note']}")
        return 0
    if act == "pause":
        ctx.out(ctx.call("filehub.pause", {}), "Đã tạm dừng đồng bộ.")
        return 0
    if act == "resume":
        r = ctx.call("filehub.resume", {"root_id": a.root})
        ctx.out(r, "Đã cho chiều đẩy lên đi tiếp." if a.root else "Đã chạy lại đồng bộ.")
        return 0
    return 1


__all__ = ["add_parsers", "cmd_filehub", "STATE_VI"]
