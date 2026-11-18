"""CLI `cfharness support bundle|list` (support_bundle.py, L7). `support` ∈ HUB_HUMAN_ONLY_CLI (agent gõ, kể cả chỉ xem, đều bị chặn).

`support bundle [--since 24h] [--run R …] [--include-raw] [--yes]`: in KẾ HOẠCH (RPC `support.plan`; daemon dừng → tính tại chỗ, giữ
`daemon.lock`) → hỏi y/N (bỏ qua bằng `--yes`) → có `--include-raw` thì hỏi THÊM bước 2 (gõ `raw`; không TTY cần `--confirm-raw` — `--yes`
KHÔNG bỏ qua bước này) → tạo (RPC `support.create` hoặc tại chỗ).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    s = sub.add_parser("support", help="gói chẩn đoán đã che secret (log, hub doctor, trạng thái, dòng thời gian run) — chỉ người",
                       parents=[common])
    ss = s.add_subparsers(dest="support_cmd", required=True)
    b = ss.add_parser("bundle", allow_abbrev=False, help="tạo gói chẩn đoán HUB_HOME/exports/support-<ts>.tar.gz (in kế hoạch, hỏi xác nhận)",
                      parents=[common])
    b.add_argument("--since", default="24h", help="log từ (24h, 2h, 7d hoặc ISO 8601; mặc định 24h)")
    b.add_argument("--run", dest="runs", action="append", default=[], help="kèm dòng thời gian của run này (lặp lại được)")
    b.add_argument("--include-raw", action="store_true", help="kèm raw transcript runs/<id>.jsonl của các --run (CÓ nội dung hội thoại)")
    b.add_argument("--yes", "-y", action="store_true", help="bỏ qua xác nhận bước 1 (KHÔNG bỏ qua xác nhận raw)")
    b.add_argument("--confirm-raw", action="store_true", help="xác nhận bước 2 cho --include-raw không cần gõ")
    b.add_argument("--local", action="store_true", help="làm tại chỗ, không qua daemon (daemon phải dừng)")
    b.set_defaults(fn=cmd_bundle)
    ls = ss.add_parser("list", help="liệt kê gói chẩn đoán đã tạo", parents=[common])
    ls.set_defaults(fn=cmd_list)


def _size(n: Any) -> str:
    from .backup_cli import _size as s

    return s(n)


def plan_text(r: dict[str, Any]) -> str:
    lines = [f"Gói chẩn đoán — log từ {r.get('since')}" + (" · KÈM raw transcript" if r.get("include_raw") else "")]
    for f in r.get("files") or []:
        flags = []
        if f.get("truncated"):
            flags.append("cắt theo trần")
        if (r.get("redaction_report") or {}).get("files", {}).get(f["path"]):
            flags.append(f"che {r['redaction_report']['files'][f['path']]} chỗ")
        lines.append(f"  {f['path']:<48} {_size(f.get('size')):>9}" + (f"  ({', '.join(flags)})" if flags else ""))
    for s in r.get("skipped") or []:
        lines.append(f"  BỎ {s['path']:<45} {_size(s.get('size')):>9}  (vượt trần tổng {_size(r.get('max_bytes'))})")
    rep = r.get("redaction_report") or {}
    lines.append(f"Tổng {_size(r.get('total'))} (chưa nén) / trần {_size(r.get('max_bytes'))} · đã che {rep.get('total', 0)} chỗ trông như secret")
    lines.append("Không bao giờ có trong gói: " + ", ".join(r.get("never_included") or []))
    return "\n".join(lines)


def _ask(prompt: str, ok: tuple[str, ...] = ("y", "yes", "c", "co", "có")) -> bool:
    try:
        return input(prompt).strip().lower() in ok
    except EOFError:
        return False


def _offline(ctx: Any, fn: Any) -> Any:
    """Daemon dừng: giữ `daemon.lock` suốt lúc đọc (daemon không thể khởi động chen), như các lệnh ngoại tuyến khác."""
    from .daemon import acquire_instance_lock, release_instance_lock
    from .errors import Conflict
    from .support_bundle import require_human_process

    require_human_process()
    lock = acquire_instance_lock(ctx.paths)
    if lock is None:
        raise Conflict("daemon đang chạy — bỏ --local để làm qua daemon")
    try:
        from .secrets import SecretStore

        SecretStore(ctx.paths.home / "secrets", env_fallback=False).preload()  # che theo GIÁ TRỊ như trong daemon (chỉ trong RAM tiến trình này)
        return fn()
    finally:
        release_instance_lock(lock)


def cmd_bundle(ctx: Any) -> int:
    from .errors import HubError, exit_code_for

    try:
        return _bundle(ctx)
    except HubError as e:  # lỗi của phần chạy tại chỗ — cùng mã thoát như lỗi RPC
        print(f"lỗi: {e}", file=sys.stderr)
        return exit_code_for(e.code)


def _bundle(ctx: Any) -> int:
    from . import support_bundle as SB
    from .rpc import METHOD_NOT_FOUND, DaemonUnavailable, RpcError

    a = ctx.args
    if a.include_raw and not a.runs:
        print("lỗi: --include-raw cần ít nhất một --run <id>", file=sys.stderr)
        return 1
    params = {"since": a.since, "runs": list(a.runs), "include_raw": bool(a.include_raw)}
    cfg = None
    client = None
    if not a.local:
        try:
            client = ctx.client(autostart=False)
            plan = client.call("support.plan", params, timeout=600)
        except DaemonUnavailable:
            client = None
            print("(daemon không chạy — làm tại chỗ)", file=sys.stderr)
        except RpcError as e:
            if e.code != METHOD_NOT_FOUND:
                raise
            client = None
    if client is None:
        from .config import HubConfig

        try:
            cfg = HubConfig(ctx.paths.config).data
        except Exception:  # noqa: BLE001 — config hỏng: dùng trần mặc định
            cfg = None
        kw = SB.params_of(params)
        plan = _offline(ctx, lambda: SB.create(ctx.paths, dry_run=True, cfg=cfg, **kw))
    print(plan_text(plan), file=sys.stderr if ctx.json else sys.stdout)  # --json: stdout chỉ là kết quả cuối
    if not a.yes:
        if not sys.stdin.isatty():
            print("lỗi: không chạy trong terminal — thêm --yes để tạo gói", file=sys.stderr)
            return 1
        if not _ask("Tạo gói này? Gói nằm trong HUB_HOME/exports/ (0600) — chỉ gửi cho người bạn tin. [y/N] "):
            print("đã huỷ")
            return 0
    confirm_raw = False
    if a.include_raw:
        if a.confirm_raw:
            confirm_raw = True
        elif not sys.stdin.isatty():
            print("lỗi: --include-raw cần xác nhận bước 2 — không chạy trong terminal nên cần --confirm-raw", file=sys.stderr)
            return 1
        else:
            confirm_raw = _ask("Bước 2: raw transcript chứa NỘI DUNG hội thoại (prompt, trả lời, input/output tool) của các run đã chọn — "
                               "secret đã biết được che, chữ còn lại giữ nguyên. Gõ `raw` để kèm: ", ok=("raw",))
            if not confirm_raw:
                print("đã huỷ")
                return 0
    if client is not None:
        res = client.call("support.create", {**params, "confirm_raw": confirm_raw}, timeout=1800)
    else:
        kw = SB.params_of(params)
        res = _offline(ctx, lambda: SB.create(ctx.paths, dry_run=False, cfg=cfg, **kw))
    rep = res.get("redaction_report") or {}
    nfiles = res["files"] if isinstance(res.get("files"), int) else len(res.get("files") or [])
    ctx.out(res, f"đã tạo gói chẩn đoán: {res['path']} ({_size(res.get('size'))}, {nfiles} file, che {rep.get('total', 0)} chỗ)\n"
                 f"sha256 {res.get('sha256')}")
    return 0


def cmd_list(ctx: Any) -> int:
    from . import support_bundle as SB
    from .rpc import METHOD_NOT_FOUND, DaemonUnavailable, RpcError

    try:
        r = ctx.client(autostart=False).call("support.list", {})
    except DaemonUnavailable:
        r = {"bundles": SB.list_bundles(ctx.paths), "dir": str(SB.exports_dir(ctx.paths))}
    except RpcError as e:
        if e.code != METHOD_NOT_FOUND:
            raise
        r = {"bundles": SB.list_bundles(ctx.paths), "dir": str(SB.exports_dir(ctx.paths))}
    lines = [f"{b['name']:<40} {_size(b.get('size')):>9}  {b.get('created_at')}" for b in r.get("bundles") or []]
    ctx.out(r, "\n".join(lines) or f"(chưa có gói chẩn đoán trong {r.get('dir')})")
    return 0


__all__ = ["add_parsers", "plan_text"]
