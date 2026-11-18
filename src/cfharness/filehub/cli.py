"""CLI `cfharness filehub status|remove|rename|resume|lane …` — client thuần của daemon (RPC `filehub.*`).

Thư mục chung Không gian số đi qua CỔNG CFHARNESS (`filehub lane setup`: dán khoá tạo ở Không gian số › Hồ sơ › Cổng cfharness). Đồng bộ
2 chiều cũ (`filehub setup|login|add|pause|switch…`) đã bỏ — thư mục còn cấu hình cũ được báo "cần chuyển" và tự chuyển khi khoá cấp đúng gốc.
"""

from __future__ import annotations

import argparse
from typing import Any

REMOVED = ("setup", "login", "switch-to-workspace", "add", "pause", "switch")
REMOVED_TEXT = ("Đồng bộ 2 chiều cũ đã bỏ — thư mục chung Không gian số giờ đi qua cổng cfharness: `cfharness filehub lane setup` (dán khoá "
                "tạo ở Không gian số › Hồ sơ › Cổng cfharness). Thư mục đồng bộ cũ cùng gốc được tự chuyển; bản sao trên máy để nguyên.")


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    s = sub.add_parser("filehub", help="thư mục dùng chung với \"Không gian số\" qua cổng cfharness (agent làm việc trong workspace filehub:<tên>)",
                       parents=[common])
    ss = s.add_subparsers(dest="fh_action")
    ss.add_parser("status", help="thư mục chung, cổng, thư mục đồng bộ cũ cần chuyển", parents=[common])
    rm = ss.add_parser("remove", help="gỡ thư mục chung khỏi hub (không xoá tệp trên máy lẫn trên Không gian số; chỉ người)", parents=[common])
    rm.add_argument("name")
    rn = ss.add_parser("rename", help="đổi tên thư mục chung trong cfharness (filehub:<tên>) — không đổi tên trên Không gian số; tên cũ vẫn khớp "
                                      "làm bí danh (chỉ người)", parents=[common])
    rn.add_argument("name")
    rn.add_argument("new_name")
    rs = ss.add_parser("resume", help="lô xoá hàng loạt đang giữ của một gốc: đẩy tiếp (mặc định) hoặc --discard (không xoá) — chỉ người",
                       parents=[common])
    rs.add_argument("--root", required=True, help="id gốc (xem `filehub lane status`)")
    rs.add_argument("--discard", action="store_true", help="KHÔNG xoá lô đang giữ — kéo tệp về lại cây run, báo agent")
    for old in REMOVED:  # lệnh của đồng bộ cũ: nhận để in hướng dẫn (không làm gì)
        o = ss.add_parser(old, help=argparse.SUPPRESS, parents=[common])
        o.add_argument("rest", nargs="*")
        for opt in ("--server", "--binary", "--scope", "--name", "--local-parent", "--local-path", "--to"):
            o.add_argument(opt, help=argparse.SUPPRESS)
        for flag in ("--no-browser", "--no-wait", "--force"):
            o.add_argument(flag, action="store_true", help=argparse.SUPPRESS)
    # cổng cfharness (docs/dev-notes/plan-filehub-lane.md): khoá hub do NGƯỜI tạo ở Không gian số › Hồ sơ › Cổng cfharness
    ln = ss.add_parser("lane", help="cổng cfharness: thiết lập khoá, trạng thái, thêm thư mục, cây mồ côi (chỉ người)", parents=[common])
    ls = ln.add_subparsers(dest="lane_action")
    lst = ls.add_parser("setup", help="nhập khoá cổng (hỏi bằng getpass, không qua tham số) + kiểm `hello`", parents=[common])
    lst.add_argument("--server", help="tên cổng (qltm, cchc…) — mặc định theo Không gian số trả lời (qltm/cchc)")
    lst.add_argument("--url", help="http://127.0.0.1:<cổng> — bỏ trống: thử 8223 (qltm) rồi 8233 (cchc); chỉ loopback")
    lst.add_argument("--key-file", help="đọc khoá từ tệp 0600 (thay cho gõ tay); tệp người khác đọc được → từ chối")
    ls.add_parser("status", help="trạng thái cổng, thư mục qua cổng, cây làm việc đang chạy, cây mồ côi", parents=[common])
    lt = ls.add_parser("test", help="thử khoá đã lưu (hello + cursor)", parents=[common])
    lt.add_argument("--server")
    ld = ls.add_parser("disable", help="xoá khoá + cấu hình cổng", parents=[common])
    ld.add_argument("--server", required=True)
    ld.add_argument("--force", action="store_true", help=argparse.SUPPRESS)  # giữ cho script cũ — không còn cần
    la = ls.add_parser("add", help="thêm thư mục chung từ một gốc khoá được cấp (không bản sao trên máy)", parents=[common])
    la.add_argument("root_id")
    la.add_argument("--name")
    la.add_argument("--server")
    lo = ls.add_parser("orphan", help="cây mồ côi (thay đổi chưa đẩy lên được khi run kết thúc): push = đẩy lại, drop = bỏ", parents=[common])
    lo.add_argument("orphan_action", choices=("push", "drop"))
    lo.add_argument("tree")
    s.set_defaults(fn=cmd_filehub)


def _print_legacy(rows: list[dict[str, Any]]) -> None:
    for f in rows or []:
        print(f"  ⚠ {f.get('message')}" + (f" (bản sao: {f['old_copy']})" if f.get("old_copy") else ""))


def _print_status(r: dict[str, Any]) -> None:
    if r.get("state") == "not_configured" and not r.get("legacy"):
        print("Không gian số: chưa thiết lập — `cfharness filehub lane setup` rồi dán khoá cổng (Không gian số › Hồ sơ › Cổng cfharness)")
        return
    print("Không gian số: qua cổng cfharness (không đồng bộ về máy) — chi tiết cổng: `cfharness filehub lane status`")
    for f in r.get("folders") or []:
        print(f"  • {f['name']}{' (toàn bộ workspace)' if f.get('kind') == 'workspace' else ''} (cổng {f.get('server') or '?'})  →  "
              f"workspace {f.get('workspace') or 'filehub:' + f['name']}"
              + ("  ⛔ khoá không còn được cấp gốc này" if f.get("revoked") else ""))
        if f.get("old_copy"):
            print(f"    bản sao cũ của đồng bộ 2 chiều còn ở {f['old_copy']} (hub không xoá — kiểm rồi tự xoá)")
    _print_legacy(r.get("legacy") or [])
    o = r.get("orphan_sync") or {}
    if o.get("found"):
        print(f"  (đã dừng tiến trình đồng bộ cũ còn sót, pid {o.get('pid')})")


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


def _print_roots(r: dict[str, Any]) -> None:
    """Gốc được cấp ↔ thư mục chung (sau setup/test: hub đã tự thêm gốc mới — dùng được ngay)."""
    added = {x.get("name") for x in r.get("added") or []}
    for x in r.get("roots") or []:
        kind = "workspace" if x.get("kind") == "workspace" else "thư mục"
        if x.get("folder") and x.get("mode") == "lane":
            print(f"  ✓ {kind} «{x['name']}» → giao việc: workspace filehub:{x['folder']}" + ("  (mới thêm)" if x["folder"] in added else ""))
        elif x.get("folder"):
            print(f"  ⚠ {kind} «{x['name']}»: thư mục chung {x['folder']} còn cấu hình đồng bộ cũ")
        else:
            print(f"  • {kind} «{x['name']}» (gốc {x['id']})")
    for x in r.get("converted") or []:
        print(f"  ✓ thư mục đồng bộ cũ «{x.get('name')}» đã chuyển sang cổng — giao việc: workspace {x.get('workspace')}"
              + (f" (bản sao cũ {x['old_copy']} để nguyên — kiểm rồi tự xoá)" if x.get("old_copy") else ""))
    for n in r.get("revoked") or []:
        print(f"  ⛔ thư mục chung «{n}»: khoá không còn được cấp gốc này (tệp không bị xoá) — cấp lại ở Không gian số › Hồ sơ › Cổng cfharness")


def _print_lane(r: dict[str, Any]) -> None:
    print("Cổng cfharness")
    if not r.get("servers"):
        print("  (chưa thiết lập — `cfharness filehub lane setup` rồi dán khoá)")
    for s in r.get("servers") or []:
        exp = s.get("expiry") or {}
        print(f"  • {s['name']}: {s.get('url')} · tài khoản {s.get('user') or '?'} · khoá {s.get('key_prefix') or '?'}… ({s.get('scope') or '?'})"
              f" · {s.get('label') or s.get('state')}")
        if s.get("hint"):
            print(f"    {s['hint']}")
        if exp.get("date") and s.get("state") not in ("auth_failed",):
            print(f"    hết hạn: {exp['date']} (còn {exp.get('days')} ngày)" + ("  ⚠ sắp hết hạn — tạo khoá mới rồi `filehub lane setup`" if exp.get("warn") else ""))
    for f in r.get("folders") or []:
        print(f"  thư mục {f['name']} (cổng {f.get('server') or '?'}) → workspace {f['workspace']}"
              + ("  ⛔ khoá không còn được cấp gốc này (tệp không bị xoá) — cấp lại ở Không gian số › Hồ sơ › Cổng cfharness" if f.get("revoked") else "")
              + (f"\n    bản sao cũ của đồng bộ 2 chiều giữ nguyên ở {f['old_copy']} (hub không xoá — kiểm rồi tự xoá)" if f.get("old_copy") else ""))
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
        r = ctx.call("filehub.lane_setup", {**({"server": a.server} if a.server else {}), **({"url": a.url} if a.url else {}), "key": key})
        del key
        if ctx.json:
            ctx.out(r)
            return 0
        print(f"Đã lưu khoá cổng «{r['server']}» ({r.get('key_prefix')}…, {r.get('scope')}) ở {r.get('url')} — tài khoản {r.get('user') or '?'}, "
              f"hub «{r.get('hub_label')}».")
        _print_roots(r)
        return 0
    if act == "test":
        r = ctx.call("filehub.lane_test", {"server": a.server})
        if ctx.json:
            ctx.out(r)
            return 0
        print(f"Cổng «{r['server']}» trả lời (giao thức {r.get('protocol')}, tài khoản {r.get('user')}, {len(r.get('roots') or [])} gốc).")
        _print_roots(r)
        return 0
    if act == "disable":
        r = ctx.call("filehub.lane_disable", {"server": a.server})
        ctx.out(r, f"Đã xoá khoá + cấu hình cổng «{r['server']}».")
        return 0
    return _cmd_lane_more(ctx, act)


def _cmd_lane_more(ctx: Any, act: str) -> int:
    a = ctx.args
    if act == "add":
        r = ctx.call("filehub.lane_add", {"root_id": a.root_id, "name": a.name, "server": a.server})
        ctx.out(r, f"Đã thêm thư mục chung «{r['name']}» (chế độ lane, cổng {r['server']}) — giao việc: workspace {r['workspace']}")
        return 0
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
    if act in REMOVED:
        print(REMOVED_TEXT)
        return 2
    if act == "status":
        r = ctx.call("filehub.status", {})
        if ctx.json:
            ctx.out(r)
        else:
            _print_status(r)
        return 0
    if act == "remove":
        r = ctx.call("filehub.remove", {"name": a.name})
        ctx.out(r, f"Đã gỡ «{r['removed']}» khỏi hub — {r['note']}")
        return 0
    if act == "rename":
        r = ctx.call("filehub.rename", {"name": a.name, "new_name": a.new_name})
        ctx.out(r, f"Đã đổi tên «{r['previous']}» → «{r['name']}» (giao việc: workspace {r['workspace']}; filehub:{r['previous']} vẫn khớp) — {r['note']}")
        return 0
    if act == "resume":
        r = ctx.call("filehub.resume", {"root_id": a.root, **({"discard": True} if a.discard else {})})
        msg = (f"Đã bỏ lô xoá ({(r or {}).get('discarded', 0)} mục) — tệp được kéo về lại, agent đã được báo." if a.discard
               else f"Đã duyệt lô xoá ({(r or {}).get('released', 0)} mục) — đẩy tiếp lên Không gian số.")
        ctx.out(r, msg)
        return 0
    return 1


__all__ = ["add_parsers", "cmd_filehub", "REMOVED_TEXT"]
