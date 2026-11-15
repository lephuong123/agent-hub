"""CLI `cfharness hub export|import` (hub_transfer.py) — chuyển TOÀN BỘ hub sang máy khác bằng MỘT file `.cfhx`.
Lệnh chỉ người: `hub export|import` ∈ HUB_HUMAN_ONLY_CLI (agent gõ bị chặn thẳng) + từ chối khi có `CFHARNESS_RUN_ID`.

- `export [<file>] [--profile core|full] [--stop]`: daemon đang chạy → RPC `hub.export` (daemon làm lặng hub, ghi vào
  `HUB_HOME/exports/`); daemon dừng → xuất ngoại tuyến (giữ `daemon.lock`). Có `<file>` → CLI chuyển gói tới đó (không ghi đè).
- `import <file> [--map-path CŨ=MỚI]… [--mode clone|move] [--dry-run] [--replace] [--stop] [--yes] [--confirm-id <id>]`: CHỈ ngoại
  tuyến — in kế hoạch → xác nhận (y/N; `--replace` thêm bước 2 gõ lại id gói) → dừng daemon nếu `--stop` → nhập → chạy lại daemon nếu
  lệnh đã dừng nó. Không có TTY: cần `--yes` (và `--confirm-id` khi `--replace`).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Any

MODE_LABEL = {"clone": "nhân bản (2 hub cùng chạy)", "move": "chuyển nhà (giữ nguyên kênh/lịch)"}


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    h = sub.add_parser("hub", help="xuất / nhập TOÀN BỘ hub sang máy khác bằng một file .cfhx — chỉ người", parents=[common])
    hs = h.add_subparsers(dest="hub_cmd", required=True)
    e = hs.add_parser("export", help="đóng gói toàn bộ hub thành một file .cfhx (mặc định HUB_HOME/exports/)", parents=[common])
    e.add_argument("file", nargs="?", default=None, help="nơi đặt gói (mặc định HUB_HOME/exports/<máy>-<ngày>.cfhx)")
    e.add_argument("--profile", choices=["core", "full"], default="full", help="core = như sao lưu; full = + stdout thô của run (mặc định)")
    e.add_argument("--stop", action="store_true", help="dừng run đang chạy trước khi xuất (không thì từ chối khi có run đang chạy)")
    e.set_defaults(fn=cmd_export)
    i = hs.add_parser("import", help="nhập gói .cfhx vào HUB_HOME (daemon phải dừng; mặc định chế độ clone)", parents=[common])
    i.add_argument("file", help="file gói .cfhx")
    i.add_argument("--map-path", action="append", default=[], metavar="CŨ=MỚI", help="đổi thêm tiền tố đường dẫn (lặp lại được)")
    i.add_argument("--mode", choices=["clone", "move"], default="clone",
                   help="clone (mặc định): tắt Telegram/Slack, tạm dừng lịch, chặn task đang chạy; move: giữ nguyên")
    i.add_argument("--dry-run", action="store_true", help="chỉ in kế hoạch, không ghi gì")
    i.add_argument("--replace", action="store_true", help="home đích đã có hub → ghi đè (chụp bản sao lưu pre-restore trước)")
    i.add_argument("--stop", action="store_true", help="tự dừng daemon của home đích nếu đang chạy (và chạy lại sau khi nhập)")
    i.add_argument("--yes", "-y", action="store_true", help="bỏ qua xác nhận bước 1")
    i.add_argument("--confirm-id", default=None, help="xác nhận bước 2 khi --replace (phải trùng id gói)")
    i.set_defaults(fn=cmd_import)


def _size(n: Any) -> str:
    from .backup_cli import _size as s

    return s(n)


def _guard() -> int | None:
    from .auth import ENV_RUN_ID

    if os.environ.get(ENV_RUN_ID):
        print("lỗi: xuất/nhập hub chỉ người làm được (đang ở trong run của agent)", file=sys.stderr)
        return 3
    return None


def _wrap(fn: Any, ctx: Any) -> int:
    from .errors import HubError, exit_code_for

    rc = _guard()
    if rc is not None:
        return rc
    try:
        return int(fn(ctx) or 0)
    except HubError as e:  # lỗi của phần chạy tại chỗ (daemon dừng) — cùng mã thoát như lỗi RPC
        print(f"lỗi: {e}", file=sys.stderr)
        return exit_code_for(e.code)


# ============================================================================ export
def cmd_export(ctx: Any) -> int:
    return _wrap(_export, ctx)


def _export(ctx: Any) -> int:
    from . import hub_transfer as T
    from .home_migrate import daemon_running

    a = ctx.args
    dest = Path(a.file).expanduser() if a.file else None
    if dest is not None:
        if dest.is_dir():
            print(f"lỗi: {dest} là thư mục — ghi rõ tên file (vd {dest / 'hub.cfhx'})", file=sys.stderr)
            return 1
        if dest.exists():
            print(f"lỗi: {dest} đã tồn tại — không ghi đè", file=sys.stderr)
            return 1
    if daemon_running(ctx.paths.home) is not None:
        r = ctx.call("hub.export", {"profile": a.profile, "stop": bool(a.stop)}, timeout=3600)
    else:
        r = T.export_offline(ctx.paths, profile=a.profile)
    path = Path(r["path"])
    if dest is not None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        os.chmod(dest, 0o600)
        path = dest
        r["path"] = str(dest)
    secs = r.get("sections") or {}
    lines = [f"đã xuất hub → {path}",
             f"  id {r['id']} · profile {r['profile']} · {_size(r.get('size'))} · {r.get('files')} phần tử · schema v{r.get('user_version')}",
             f"  sha256 {r.get('sha256')}"]
    lines += [f"  {k}: {s.get('count')} file, {_size(s.get('bytes'))} trước nén" for k, s in sorted(secs.items())]
    if r.get("stopped_runs"):
        lines.append(f"  đã dừng {len(r['stopped_runs'])} run trước khi xuất: {', '.join(r['stopped_runs'][:5])}")
    if r.get("missing_secrets"):
        lines.append(f"  ! KHÔNG mang theo {len(r['missing_secrets'])} secret ({', '.join(r['missing_secrets'][:8])}"
                     f"{'…' if len(r['missing_secrets']) > 8 else ''}) — đặt lại trên máy kia bằng `cfharness secret set <TÊN>`")
    lines.append("  ! gói KHÔNG mã hoá: chứa memory, transcript, audit của hub — giữ riêng tư (file 0600), đừng đưa lên nơi công khai")
    lines.append(f"Chép file này sang máy kia rồi chạy: cfharness hub import {path.name} --dry-run  (rồi bỏ --dry-run)")
    ctx.out(r, "\n".join(lines))
    return 0


# ============================================================================ import
def cmd_import(ctx: Any) -> int:
    return _wrap(_import, ctx)


def _plan_text(p: dict[str, Any]) -> str:
    src = p.get("source") or {}
    out = [f"Gói {p.get('id')} ({p.get('profile')}, {_size(p.get('size'))}) — xuất lúc {p.get('created_at')} từ máy {src.get('host')} "
           f"({src.get('os')}/{src.get('arch')}, cfharness {src.get('cfharness_version')}, schema v{src.get('user_version')})",
           f"Nhập vào: {p.get('target_home')} — {'trống' if p.get('home_blank') else 'ĐÃ CÓ HUB (cần --replace, chụp pre-restore trước)'}",
           f"Chế độ: {p.get('mode')} — {MODE_LABEL.get(str(p.get('mode')), '')}"]
    v = p.get("verify") or {}
    out.append(f"Kiểm gói: {'OK' if v.get('ok') else 'LỖI'} — {v.get('files')} phần tử, hub.db integrity_check={v.get('integrity')}")
    pm = p.get("path_map") or []
    out.append("Bản đồ đường dẫn:" if pm else "Bản đồ đường dẫn: (không đổi — cùng đường dẫn)")
    out += [f"  {x['from']} → {x['to']}" for x in pm]
    dbr = p.get("db_rewrite") or {}
    if dbr:
        out.append(f"  DB: {sum(dbr.values())} giá trị sẽ viết lại")
        out += [f"    {k:<34} {n}" for k, n in sorted(dbr.items())]
    if p.get("files_rewrite"):
        out.append("  file cấu hình viết lại: " + ", ".join(p["files_rewrite"]))
    if p.get("review") or p.get("knowledge_rows_with_old_paths"):
        out.append(f"  CHƯA tự sửa (tri thức thuộc ledger, ghi vào hub_state.import_followups): {len(p.get('review') or [])} file"
                   + (f" ({', '.join((p.get('review') or [])[:5])}{'…' if len(p.get('review') or []) > 5 else ''})" if p.get("review") else "")
                   + (f", {p['knowledge_rows_with_old_paths']} hàng graph memory" if p.get("knowledge_rows_with_old_paths") else ""))
    if p.get("state_cleared"):
        out.append("Dọn trạng thái máy cũ: " + ", ".join(p["state_cleared"]))
    out.append(f"Run đang dở → interrupted: {p.get('runs_interrupted', 0)}"
               + (f" (tự resume ở máy này: {p['runs_pending_resume']})" if p.get("runs_pending_resume") else ""))
    if p.get("mode") == "clone":
        out.append(f"Task bị chặn: {p.get('tasks_blocked', 0)} · lịch tạm dừng: {p.get('schedules_paused', 0)} · tắt: "
                   + (", ".join(p.get("config_changed") or []) or "(kênh đã tắt sẵn)"))
    for m in p.get("mcp_missing") or []:
        out.append(f"! MCP '{m['server']}': không thấy lệnh {m['command']} ở máy này — sửa bằng `cfharness mcp edit {m['server']}`")
    for w in p.get("warnings") or []:
        out.append(f"! {w}")
    return "\n".join(out)


def _import(ctx: Any) -> int:
    from . import hub_transfer as T
    from .home_migrate import daemon_running, stop_daemon

    a = ctx.args
    try:
        maps = T.parse_map_paths(a.map_path)
    except ValueError as e:
        print(f"lỗi: {e}", file=sys.stderr)
        return 1
    pkg = Path(a.file).expanduser()
    plan = T.import_plan(ctx.paths, pkg, maps=maps, mode=a.mode)
    if ctx.json and a.dry_run:
        ctx.out(plan)
        return 1 if plan["blockers"] else 0
    print(_plan_text(plan))
    if plan["blockers"]:
        for x in plan["blockers"]:
            print(f"lỗi: {x}", file=sys.stderr)
        return 1
    if a.dry_run:
        print("(dry-run — chưa ghi gì)")
        return 0
    if not plan["home_blank"] and not a.replace:
        print(f"lỗi: home đích {ctx.paths.home} đã có hub — thêm --replace để ghi đè (bản pre-restore được chụp trước) hoặc dùng --home khác",
              file=sys.stderr)
        return 1
    running = daemon_running(ctx.paths.home) is not None
    if running and not a.stop:
        print("lỗi: daemon của home đích đang chạy — thêm --stop để lệnh tự dừng daemon (và chạy lại sau khi nhập)", file=sys.stderr)
        return 2
    tty = sys.stdin.isatty()
    if not tty and not a.yes:
        print("lỗi: không chạy trong terminal — cần --yes" + (" VÀ --confirm-id <id gói>" if a.replace else ""), file=sys.stderr)
        return 1
    if not a.yes:
        ans = input(f"Bước 1/{2 if a.replace else 1} — nhập gói này vào {ctx.paths.home}? [y/N] ").strip().lower()
        if ans not in ("y", "yes", "c", "co", "có"):
            print("đã huỷ")
            return 0
    if a.replace:
        if not tty and a.confirm_id is None:
            print("lỗi: --replace không chạy trong terminal — cần --confirm-id <id gói>", file=sys.stderr)
            return 1
        typed = a.confirm_id if a.confirm_id is not None else input(f"Bước 2/2 — GHI ĐÈ hub hiện có; gõ lại id gói để xác nhận ({plan['id']}): ").strip()
        if typed != plan["id"]:
            print("id không khớp — đã huỷ, không thay đổi gì", file=sys.stderr)
            return 1
    if running:
        print("dừng daemon…", flush=True)
        stop_daemon(ctx.paths.home)
    cfg = None
    try:
        from .config import HubConfig

        cfg = HubConfig(ctx.paths.config).get("backup") if ctx.paths.config.exists() else None
    except Exception:  # config.yaml hỏng không được chặn nhập
        pass
    r = T.import_package(ctx.paths, pkg, maps=maps, mode=a.mode, replace=a.replace, cfg=cfg)
    lines = [f"đã nhập {r['imported']} vào {r['home']} (chế độ {r['mode']}): {r['files_written']} file, hub.db đã thay"
             + (f", {r['runs_files']} file stdout run" if r.get("runs_files") else "")
             + (f" · bản hiện trạng trước đó: {r['pre_restore']}" if r.get("pre_restore") else "")]
    if r.get("missing_secrets"):
        lines.append(f"Đặt lại {len(r['missing_secrets'])} secret: " + ", ".join(f"cfharness secret set {n}" for n in r["missing_secrets"][:6])
                     + ("…" if len(r["missing_secrets"]) > 6 else ""))
    if r["mode"] == "clone":
        lines.append("Nhân bản: Telegram/Slack đã tắt (cần bot RIÊNG cho máy này: `cfharness notify telegram setup`), lịch đã tạm dừng "
                     "(`cfharness schedule list` rồi bật lịch cần chạy), task đang dở đã bị chặn (`cfharness task unblock <id>`).")
    if r.get("review"):
        lines.append(f"Tri thức còn đường dẫn máy cũ (chưa tự sửa): {', '.join(r['review'][:5])}{'…' if len(r['review']) > 5 else ''}")
    if running:
        from .daemon import spawn_background

        pid = spawn_background(ctx.paths)
        lines.append(f"daemon đã chạy lại (pid {pid})")
    else:
        lines.append("chạy daemon: cfharness daemon start")
    ctx.out(r, "\n".join(lines))
    return 0
