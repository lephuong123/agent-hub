"""CLI `cfharness filehub setup|login|switch-to-workspace|status|add|remove|pause|resume` — client thuần của daemon (RPC `filehub.*`).

`setup`/`login`/`switch-to-workspace` in link đồng ý thiết bị (dòng `LOGIN_URL` của binary sync) rồi chờ người bấm đồng ý trên Không gian số.
Cùng thao tác làm được ở thẻ "Không gian số" của Cài đặt web (kể cả qua link public — đăng nhập relay OAuth).
"""

from __future__ import annotations

import argparse
import time
from typing import Any

STATE_VI = {"idle": "đã đồng bộ", "syncing": "đang đồng bộ", "conflict": "có bản xung đột", "offline": "mất kết nối", "auth_expired": "hết phiên đăng nhập",
            "error": "lỗi", "paused": "tạm dừng", "not_configured": "chưa thiết lập", "version_mismatch": "binary sync sai phiên bản", "starting": "đang khởi động",
            "revoked": "mất quyền truy cập"}


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    s = sub.add_parser("filehub", help="thư mục dùng chung với \"Không gian số\" (đồng bộ 2 chiều; agent làm việc trong workspace filehub:<tên>)",
                       parents=[common])
    ss = s.add_subparsers(dest="fh_action")
    st = ss.add_parser("setup", help="thiết lập: server + binary sync, đăng nhập (đồng ý thiết bị), tạo thư mục rỗng (chỉ người)", parents=[common])
    st.add_argument("--server", help="URL Không gian số, vd https://thumucai.trungtamdhs.com")
    st.add_argument("--binary", help="đường dẫn tang-thu-sync-headless")
    st.add_argument("--scope", choices=("workspace", "folder"),
                    help="workspace (mặc định) = TOÀN BỘ một workspace (tài khoản phải là Workspace Admin); folder = MỘT thư mục")
    st.add_argument("--name", help="gợi ý tên (chế độ folder: tên thư mục rỗng tạo ở trang đồng ý, mặc định cfharness; workspace: tên workspace)")
    st.add_argument("--local-parent", help="thư mục cha của bản sao cục bộ (mặc định ~/KhongGianSo)")
    st.add_argument("--local-path", help="đường dẫn bản sao cục bộ (mặc định <local-parent>/<tên>)")
    st.add_argument("--no-browser", action="store_true", help="không tự mở trình duyệt — chỉ in link")
    st.add_argument("--no-wait", action="store_true", help="in link rồi thoát, không chờ đồng ý")
    lg = ss.add_parser("login", help="đăng nhập lại (khi hết phiên / mất quyền) — giữ chế độ đang có; chỉ người", parents=[common])
    lg.add_argument("--no-browser", action="store_true")
    lg.add_argument("--no-wait", action="store_true")
    sw = ss.add_parser("switch-to-workspace", help="chuyển thư mục chung từ MỘT thư mục sang TOÀN BỘ workspace (giữ tệp cũ; chỉ người)",
                       parents=[common])
    sw.add_argument("--local-path", help="bản sao cục bộ mới (mặc định <local-parent>/<tên workspace>; không trùng/chứa bản cũ)")
    sw.add_argument("--no-browser", action="store_true")
    sw.add_argument("--no-wait", action="store_true")
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
        print(f"  • {f.get('display') or f['name']}" + (f" · đăng nhập bằng {f['account']}" if f.get("account") else ""))
        if f.get("revoked") and f.get("revoked_temporary"):
            print(f"    ⛔ tài khoản {f.get('account') or '(không rõ)'} không còn quyền với {f.get('display') or f['name']} (vd không còn là admin) — "
                  "đồng bộ tạm dừng, tệp trên máy giữ nguyên; cấp lại quyền admin cho tài khoản này là tự chạy tiếp (không cần đăng nhập lại), "
                  "hoặc đăng nhập bằng tài khoản admin khác: `cfharness filehub login`")
        elif f.get("revoked"):
            print(f"    ⛔ tài khoản {f.get('account') or '(không rõ)'} không còn quyền với {f.get('display') or f['name']} — đăng nhập lại bằng "
                  "tài khoản admin: `cfharness filehub login`")
        if f.get("renamed"):
            print(f"    (trên web đã đổi tên thành «{f['server_name']}» — trên máy giữ tên/đường dẫn cũ; filehub:{f['server_name']} cũng khớp)")
        print(f"    {f['name']}  →  {f['local_path']}  (workspace filehub:{f['name']})"
              + (f"  ⬆{f['pending_up']} ⬇{f['pending_down']}" if f.get("pending_up") or f.get("pending_down") else "")
              + (f"  · đang có run {lock.get('run_id')} ghi" if lock else ""))
        if f.get("blocked"):
            print(f"    ⛔ chiều đẩy lên đang dừng ({f['blocked']}) — `cfharness filehub resume --root {f['root_id']}` để đẩy tiếp")
        for c in f.get("conflicts") or []:
            print(f"    ⚠ bản xung đột: {c['path']}")
        for p in f.get("root_files") or []:
            print(f"    ⚠ tệp ở gốc workspace không đồng bộ (đưa vào một thư mục cấp 1): {p}")
        for x in f.get("unpushable") or []:
            print(f"    ⚠ không đẩy lên được ({x.get('kind')}): {x.get('path')}")
        if f.get("url"):
            print(f"    mở: {f['url']}")
    lg = r.get("login") or {}
    if lg.get("switched_from"):
        print("  Bản sao cũ (đã gỡ khỏi hub, tệp giữ nguyên — kiểm bản mới rồi tự xoá): " + ", ".join(lg["switched_from"]))
    if lg.get("state") in ("waiting", "error"):
        print(f"  đăng nhập: {lg['state']}" + (f" — {lg.get('message') or lg.get('error')}" if lg.get("error") else f" — {lg.get('url')}"))


def _wait_login(ctx: Any, timeout_s: float = 900) -> int:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        st = ctx.call("filehub.status", {})
        lg = st.get("login") or {}
        if lg.get("state") == "done":
            names = ", ".join(("workspace " if r.get("kind") == "workspace" else "") + r.get("name", "?") for r in lg.get("roots") or [])
            print(f"Đã đăng nhập{' bằng ' + lg['account'] if lg.get('account') else ''}. Được cấp: {names or '(không có)'}")
            _print_status(st)
            return 0
        if lg.get("state") == "error":
            print(f"lỗi đăng nhập: {lg.get('message') or lg.get('error')}")
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
    if act in ("setup", "login", "switch-to-workspace"):
        if act == "setup":
            r = ctx.call("filehub.setup", {"server": a.server, "binary": a.binary, "name": a.name, "scope": a.scope, "local_parent": a.local_parent,
                                           "local_path": a.local_path, "no_browser": a.no_browser})
        elif act == "login":
            r = ctx.call("filehub.login", {"no_browser": a.no_browser})
        else:
            r = ctx.call("filehub.switch_to_workspace", {"no_browser": a.no_browser, "local_path": a.local_path})
        if ctx.json:
            ctx.out(r)
            return 0
        if r.get("mode") == "workspace":
            print("Mở link sau để đồng ý thiết bị «cfharness» — chọn «Toàn bộ workspace» (chỉ hiện với Workspace Admin; đăng nhập bằng tài khoản\n"
                  f"admin của workspace, không dùng tài khoản Super Admin):\n  {r['login_url']}")
        else:
            print(f"Mở link sau để đồng ý thiết bị «cfharness» (chọn/tạo thư mục rỗng ngay ở trang đồng ý):\n  {r['login_url']}")
        if act == "switch-to-workspace":
            print(f"Sau khi đồng ý: {r.get('note')}")
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
