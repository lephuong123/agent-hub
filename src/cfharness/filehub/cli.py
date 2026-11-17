"""CLI `cfharness filehub setup|login|switch-to-workspace|status|add|remove|rename|pause|resume` — client thuần của daemon (RPC `filehub.*`).

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
    rn = ss.add_parser("rename", help="đổi tên thư mục chung trong cfharness (filehub:<tên>) — không đổi đường dẫn trên máy, không đổi tên trên "
                                      "Không gian số; tên cũ vẫn khớp làm bí danh (chỉ người)", parents=[common])
    rn.add_argument("name")
    rn.add_argument("new_name")
    ss.add_parser("pause", help="tạm dừng đồng bộ (dừng tiến trình sync)", parents=[common])
    rs = ss.add_parser("resume", help="chạy lại đồng bộ; --root <id>: cho chiều đẩy lên đang bị chặn (xoá hàng loạt) đi tiếp", parents=[common])
    rs.add_argument("--root")
    # cổng cfharness (chế độ lane — docs/dev-notes/plan-filehub-lane.md): khoá hub do NGƯỜI tạo ở Không gian số › Hồ sơ › Cổng cfharness
    ln = ss.add_parser("lane", help="cổng cfharness: thư mục chung KHÔNG qua đồng bộ (cây làm việc theo run) — thiết lập khoá, trạng thái (chỉ người)",
                       parents=[common])
    ls = ln.add_subparsers(dest="lane_action")
    lst = ls.add_parser("setup", help="nhập khoá cổng (hỏi bằng getpass, không qua tham số) + kiểm `hello`", parents=[common])
    lst.add_argument("--server", default="qltm", help="tên cổng (qltm, cchc…) — mặc định qltm")
    lst.add_argument("--url", required=True, help="http://127.0.0.1:<cổng> (qltm 8223, cchc 8233) — chỉ loopback")
    lst.add_argument("--key-file", help="đọc khoá từ tệp 0600 (thay cho gõ tay); tệp người khác đọc được → từ chối")
    ls.add_parser("status", help="trạng thái cổng, thư mục chế độ lane, cây làm việc đang chạy, cây mồ côi", parents=[common])
    lt = ls.add_parser("test", help="thử khoá đã lưu (hello + cursor)", parents=[common])
    lt.add_argument("--server")
    ld = ls.add_parser("disable", help="xoá khoá + cấu hình cổng", parents=[common])
    ld.add_argument("--server", required=True)
    ld.add_argument("--force", action="store_true", help="kể cả khi còn thư mục chế độ lane dùng cổng này")
    lo = ls.add_parser("orphan", help="cây mồ côi (thay đổi chưa đẩy lên được khi run kết thúc): push = đẩy lại, drop = bỏ", parents=[common])
    lo.add_argument("orphan_action", choices=("push", "drop"))
    lo.add_argument("tree")
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
        if f.get("workspace_deleted"):
            print(f"    ⛔ workspace «{f.get('server_name') or f['name']}» đã bị xoá trên Không gian số — đồng bộ đã ngắt, tệp trên máy giữ nguyên"
                  + (f" (bản trên máy nay ở {f['disconnected_path']})" if f.get("disconnected_path") else "")
                  + f"; gỡ khỏi hub: `cfharness filehub remove {f['name']}`")
        elif f.get("revoked") and f.get("revoked_temporary"):
            print(f"    ⛔ tài khoản {f.get('account') or '(không rõ)'} không còn quyền với {f.get('display') or f['name']} (vd không còn là admin) — "
                  "đồng bộ tạm dừng, tệp trên máy giữ nguyên; cấp lại quyền admin cho tài khoản này là tự chạy tiếp (không cần đăng nhập lại), "
                  "hoặc đăng nhập bằng tài khoản admin khác: `cfharness filehub login`")
        elif f.get("revoked"):
            print(f"    ⛔ tài khoản {f.get('account') or '(không rõ)'} không còn quyền với {f.get('display') or f['name']} — đăng nhập lại bằng "
                  "tài khoản admin: `cfharness filehub login`")
            if f.get("disconnected_path"):
                print(f"    (đã ngắt đồng bộ — bản trên máy nay ở {f['disconnected_path']}, tệp giữ nguyên)")
        if f.get("aliases"):
            print("    tên cũ (vẫn khớp): " + ", ".join(f"filehub:{x}" for x in f["aliases"]))
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


def _read_key(a: Any) -> str:
    import getpass
    import os
    import stat

    if a.key_file:
        p = os.path.expanduser(a.key_file)
        st = os.stat(p)
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SystemExit(f"{p}: tệp khoá phải chỉ chủ đọc được (chmod 600)")
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    return getpass.getpass("Khoá cổng cfharness (tt_lane_…, không hiện khi gõ): ").strip()


def _print_lane(r: dict[str, Any]) -> None:
    print(f"Cổng cfharness — chế độ mặc định: {r.get('mode')}")
    if not r.get("servers"):
        print("  (chưa thiết lập — `cfharness filehub lane setup --url http://127.0.0.1:8223`)")
    for s in r.get("servers") or []:
        exp = s.get("expiry") or {}
        print(f"  • {s['name']}: {s.get('url')} · tài khoản {s.get('user') or '?'} · khoá {s.get('key_prefix') or '?'}… ({s.get('scope') or '?'})"
              f" · {s.get('state')}" + (f" ({s['error']})" if s.get("error") else ""))
        if exp.get("expires_at"):
            print(f"    hết hạn: {exp['expires_at']} (còn {exp.get('days_left')} ngày)" + ("  ⚠ sắp hết hạn — tạo khoá mới rồi `filehub lane setup`" if exp.get("warn") else ""))
    for f in r.get("folders") or []:
        print(f"  thư mục {f['name']} (root {f.get('root_id')}, cổng {f.get('server') or '?'}) → workspace {f['workspace']}"
              + (f"\n    bản sao cũ của chế độ sync giữ nguyên ở {f['old_copy']} (hub không xoá — kiểm rồi tự xoá)" if f.get("old_copy") else ""))
    for w in r.get("runs") or []:
        print(f"  run {w.get('run_id')} ({w.get('agent_id')}) · {w.get('folder')}/{w.get('sub') or ''} · {w.get('mode')} · chờ đẩy {w.get('pending')}"
              + (f" · chặn (secret) {w.get('blocked')}" if w.get("blocked") else ""))
    for o in r.get("orphans") or []:
        print(f"  ⚠ cây mồ côi {o.get('tree')} ({o.get('folder')}): {o.get('count')} thay đổi chưa đẩy — "
              f"`cfharness filehub lane orphan push {o.get('tree')}` / `… orphan drop {o.get('tree')}`")


def _cmd_lane(ctx: Any) -> int:
    a = ctx.args
    act = getattr(a, "lane_action", None) or "status"
    if act == "status":
        r = ctx.call("filehub.lane_status", {})
        if ctx.json:
            ctx.out(r)
        else:
            _print_lane(r)
        return 0
    if act == "setup":
        key = _read_key(a)
        r = ctx.call("filehub.lane_setup", {"server": a.server, "url": a.url, "key": key})
        del key
        if ctx.json:
            ctx.out(r)
            return 0
        print(f"Đã lưu khoá cổng «{r['server']}» ({r.get('key_prefix')}…, {r.get('scope')}) — tài khoản {r.get('user') or '?'}, hub «{r.get('hub_label')}».")
        for x in r.get("roots") or []:
            print(f"  gốc {x['id']} · {'workspace' if x['kind'] == 'workspace' else 'thư mục'} «{x['name']}»"
                  + (f" ↔ thư mục chung {x['folder']} (chế độ {x['mode']})" if x.get("folder") else ""))
        return 0
    if act == "test":
        r = ctx.call("filehub.lane_test", {"server": a.server})
        ctx.out(r, f"Cổng «{r['server']}» trả lời (giao thức {r.get('protocol')}, tài khoản {r.get('user')}, {len(r.get('roots') or [])} gốc).")
        return 0
    if act == "disable":
        r = ctx.call("filehub.lane_disable", {"server": a.server, "force": a.force})
        ctx.out(r, f"Đã xoá khoá + cấu hình cổng «{r['server']}».")
        return 0
    return _cmd_lane_more(ctx, act)


def _cmd_lane_more(ctx: Any, act: str) -> int:
    a = ctx.args
    if act == "orphan":
        r = ctx.call("filehub.lane_orphan", {"tree": a.tree, "action": a.orphan_action})
        if r.get("dropped"):
            ctx.out(r, f"Đã bỏ cây mồ côi {r['tree']}.")
        elif r.get("pushed"):
            ctx.out(r, f"Đã đẩy hết thay đổi của cây {r['tree']} lên Không gian số.")
        else:
            ctx.out(r, f"Còn {len(r.get('left') or [])} thay đổi chưa đẩy được ({r.get('code')}): " + ", ".join(r.get("left") or [])[:500])
            return 1
        return 0
    return 1


def cmd_filehub(ctx: Any) -> int:
    a = ctx.args
    act = a.fh_action or "status"
    if act == "lane":
        return _cmd_lane(ctx)
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
    if act == "rename":
        r = ctx.call("filehub.rename", {"name": a.name, "new_name": a.new_name})
        ctx.out(r, f"Đã đổi tên «{r['previous']}» → «{r['name']}» (giao việc: workspace {r['workspace']}; filehub:{r['previous']} vẫn khớp) — {r['note']}")
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
