"""CLI `cfharness backup create|list|verify|restore` (backup.py). Lệnh chỉ người: `backup` ∈ HUB_HUMAN_ONLY_CLI (agent gõ đều bị chặn).

- `create|list|verify`: client thuần của daemon (RPC `backup.*`).
- `restore <id>`: daemon PHẢI dừng — kế hoạch (RPC `backup.restore_plan` nếu daemon đang chạy, không thì tính tại chỗ) → xác nhận bước 1
  (y/N) → xác nhận bước 2 (gõ lại id) → dừng daemon nếu có `--stop` → khôi phục ngoại tuyến (giữ `daemon.lock`) → khởi động lại daemon
  nếu lệnh này đã dừng nó. Không có TTY: cần `--yes` VÀ `--confirm-id <id>`.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

KIND_LABEL = {"manual": "thủ công", "auto": "tự động", "pre-update": "trước cập nhật", "pre-restore": "trước khôi phục"}


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    b = sub.add_parser("backup", help="sao lưu / khôi phục HUB_HOME (hub.db + cấu hình + tri thức; bỏ secrets/auth) — chỉ người",
                       parents=[common])
    bs = b.add_subparsers(dest="backup_cmd", required=True)
    c = bs.add_parser("create", help="tạo bản sao lưu ngay", parents=[common])
    c.add_argument("--label", default=None, help="nhãn ngắn (a-z, 0-9, -)")
    c.set_defaults(fn=cmd_create)
    ls = bs.add_parser("list", help="liệt kê bản sao lưu", parents=[common])
    ls.set_defaults(fn=cmd_list)
    v = bs.add_parser("verify", help="kiểm sha256 + PRAGMA integrity_check của một bản sao lưu", parents=[common])
    v.add_argument("id")
    v.set_defaults(fn=cmd_verify)
    r = bs.add_parser("restore", help="khôi phục một bản sao lưu (daemon phải dừng; hiện trạng được chụp thành bản pre-restore)",
                      parents=[common])
    r.add_argument("id")
    r.add_argument("--stop", action="store_true", help="tự dừng daemon nếu đang chạy (và khởi động lại sau khi khôi phục)")
    r.add_argument("--yes", "-y", action="store_true", help="bỏ qua xác nhận bước 1")
    r.add_argument("--confirm-id", default=None, help="xác nhận bước 2 không cần gõ (phải trùng id)")
    r.set_defaults(fn=cmd_restore)


def _size(n: Any) -> str:
    try:
        f = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return "?"  # pragma: no cover


def _row(b: dict[str, Any]) -> str:
    flag = " HỎNG" if b.get("broken") else ""
    return (f"{b['id']:<44} {KIND_LABEL.get(str(b.get('kind')), b.get('kind')):<16} {_size(b.get('size')):>9}  "
            f"cfharness {b.get('cfharness_version') or '?'} · schema v{b.get('user_version')}{flag}")


def cmd_create(ctx: Any) -> int:
    r = ctx.call("backup.create", {"label": ctx.args.label})
    ctx.out(r, f"đã tạo bản sao lưu {r['id']} ({r.get('files')} file + hub.db, {_size(r.get('size'))})"
               + (f"\nđã xoá bản cũ theo backup.keep: {', '.join(r['pruned'])}" if r.get("pruned") else ""))
    return 0


def cmd_list(ctx: Any) -> int:
    r = ctx.call("backup.list", {})
    rows = r.get("backups") or []
    sch = r.get("schedule") or {}
    lines = [f"thư mục: {r.get('dir')} · {len(rows)} bản, tổng {_size(r.get('total_size'))}"]
    lines.append(f"lịch tự động: {sch.get('expr')} · lần tới {sch.get('next_at') or '?'}" if sch.get("enabled")
                 else f"lịch tự động: tắt{' — ' + sch['error'] if sch.get('error') else ''}")
    lines += [_row(b) for b in rows] or ["(chưa có bản sao lưu nào — tạo: cfharness backup create)"]
    ctx.out(r, "\n".join(lines))
    return 0


def _verify_text(v: dict[str, Any]) -> str:
    if v.get("ok"):
        out = f"OK — {v['id']}: {v.get('files')} file khớp sha256, hub.db integrity_check={v.get('integrity')}, schema v{v.get('user_version')}"
    else:
        out = f"LỖI — {v['id']}:\n" + "\n".join(f"  - {e}" for e in v.get("errors") or [])
    return out + "".join(f"\n  ! {w}" for w in v.get("warnings") or [])


def cmd_verify(ctx: Any) -> int:
    v = ctx.call("backup.verify", {"id": ctx.args.id})
    ctx.out(v, _verify_text(v))
    return 0 if v.get("ok") else 1


def cmd_restore(ctx: Any) -> int:
    from .errors import HubError, exit_code_for

    try:
        return _restore(ctx)
    except HubError as e:  # lỗi của phần chạy tại chỗ (daemon đã dừng) — cùng mã thoát như lỗi RPC
        print(f"lỗi: {e}", file=sys.stderr)
        return exit_code_for(e.code)


def _restore(ctx: Any) -> int:
    from . import backup as B
    from .auth import ENV_RUN_ID
    from .home_migrate import daemon_running, stop_daemon

    a = ctx.args
    if os.environ.get(ENV_RUN_ID):
        print("lỗi: khôi phục bản sao lưu chỉ người làm được (đang ở trong run của agent)", file=sys.stderr)
        return 3
    running = daemon_running(ctx.paths.home) is not None
    if running:
        plan = ctx.call("backup.restore_plan", {"id": a.id})
    else:
        plan = B.restore_plan(ctx.paths, a.id, daemon_running=False)
    b = plan["backup"]
    print(f"Khôi phục {b['id']} ({KIND_LABEL.get(str(b.get('kind')), b.get('kind'))}, cfharness {b.get('cfharness_version')}, "
          f"schema v{b.get('user_version')}, tạo {b.get('created_at')})")
    print(_verify_text(plan["verify"]))
    print("Sẽ thay: " + ", ".join(plan["replaces"]))
    print("Giữ nguyên: " + ", ".join(plan["kept"]))
    print("Trước khi ghi đè: " + plan["pre_restore"])
    for w in plan.get("warnings") or []:
        print(f"! {w}")
    if plan.get("blockers"):
        for x in plan["blockers"]:
            print(f"lỗi: {x}", file=sys.stderr)
        return 1
    tty = sys.stdin.isatty()
    if not tty and not (a.yes and a.confirm_id):
        print("lỗi: không chạy trong terminal — cần --yes VÀ --confirm-id <id>", file=sys.stderr)
        return 1
    if running and not a.stop:
        print("lỗi: daemon đang chạy — thêm --stop để lệnh tự dừng daemon (và chạy lại sau khi khôi phục)", file=sys.stderr)
        return 2
    if not a.yes:
        ans = input("Bước 1/2 — khôi phục bản này, GHI ĐÈ hub.db + cấu hình + tri thức hiện tại? [y/N] ").strip().lower()
        if ans not in ("y", "yes", "c", "co", "có"):
            print("đã huỷ")
            return 0
    typed = a.confirm_id if a.confirm_id is not None else input(f"Bước 2/2 — gõ lại id bản sao lưu để xác nhận ({b['id']}): ").strip()
    if typed != b["id"]:
        print("id không khớp — đã huỷ, không thay đổi gì", file=sys.stderr)
        return 1
    if running:
        print("dừng daemon…", flush=True)
        stop_daemon(ctx.paths.home)
    cfg = None
    try:
        from .config import HubConfig

        cfg = HubConfig(ctx.paths.config).get("backup")
    except Exception:  # config.yaml hỏng không được chặn khôi phục
        pass
    r = B.restore(ctx.paths, a.id, cfg=cfg)
    msg = (f"đã khôi phục {r['restored']}: {r['files_written']} file ghi, {r['files_removed']} file gỡ, "
           f"hub.db {'đã thay' if r['db_restored'] else 'giữ nguyên'} · bản hiện trạng trước đó: {r.get('pre_restore') or '—'}")
    if running:
        from .daemon import spawn_background

        pid = spawn_background(ctx.paths)
        msg += f"\ndaemon đã chạy lại (pid {pid})"
    else:
        msg += "\nchạy daemon: cfharness daemon start"
    ctx.out(r, msg)
    return 0
