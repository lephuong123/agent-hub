"""CLI `cfharness hub export|import` (hub_transfer.py) — chuyển TOÀN BỘ hub sang máy khác bằng MỘT file `.cfhx`.
Lệnh chỉ người: `hub export|import` ∈ HUB_HUMAN_ONLY_CLI (agent gõ bị chặn thẳng) + từ chối khi có `CFHARNESS_RUN_ID`.

- `export [<file>] [--profile core|full] [--stop] [--no-secrets | --passphrase-file F] [--with-totp] [--with-mcp-tokens]`: MẶC ĐỊNH mang
  secret đã mã hoá — hỏi passphrase 2 lần (TTY, ≥ 12 ký tự) TRƯỚC khi đóng gói; không TTY: `--passphrase-file` (0600). Daemon đang chạy →
  RPC `hub.export` (daemon làm lặng hub, ghi phần KHÔNG mật vào `HUB_HOME/exports/`); daemon dừng → xuất ngoại tuyến (giữ `daemon.lock`).
  Sau đó CHÍNH tiến trình CLI này gắn `secrets.enc` (`hub_transfer.seal_package`) — passphrase không bao giờ rời tiến trình (không argv,
  RPC, audit, log, file tạm). Có `<file>` → CLI chuyển gói tới đó (không ghi đè).
- `import <file> [--map-path CŨ=MỚI]… [--mode clone|move] [--dry-run] [--replace] [--stop] [--yes] [--confirm-id <id>]
  [--passphrase-file F | --skip-secrets]`: CHỈ ngoại tuyến — in kế hoạch → (gói có secret) hỏi passphrase + thử giải mã ngay (sai → dừng,
  chưa ghi gì; dry-run thử khi có `--passphrase-file`) → xác nhận (y/N; `--replace` thêm bước 2 gõ lại id gói) → dừng daemon nếu `--stop` → nhập → chạy lại daemon nếu
  lệnh đã dừng nó. Không có TTY: cần `--yes` (và `--confirm-id` khi `--replace`).
  PR5: `--clone-repos` (git clone repo workspace thiếu, sau khi nhập); in `hub doctor` sau khi nhập.
- PR5 `export --for move [--yes]`: chuyển nhà — gói qua kiểm tra → hub này ĐÓNG BĂNG (`hub_freeze.py`); `unfreeze [--yes]`: gỡ (y/N);
  `doctor [--probe] [--json]`: kiểm hub sau khi nhập (`hub_doctor.py`, chỉ đọc, chạy được khi daemon dừng).
  `doctor --cleanup [--older-than NGÀY] [--yes]`: dọn thư mục làm việc mồ côi qua daemon (`housekeeping.py`, RPC `hub.cleanup`, y/N, audit).
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
    e = hs.add_parser("export", allow_abbrev=False, help="đóng gói toàn bộ hub thành một file .cfhx (mặc định HUB_HOME/exports/)", parents=[common])
    e.add_argument("file", nargs="?", default=None, help="nơi đặt gói (mặc định HUB_HOME/exports/<máy>-<ngày>.cfhx)")
    e.add_argument("--profile", choices=["core", "full"], default="full", help="core = như sao lưu + session Claude Code đang dở (điều phối viên, task chưa xong, run dở); full = + stdout thô của run + mọi session Claude của hub (mặc định)")
    e.add_argument("--stop", action="store_true", help="dừng run đang chạy trước khi xuất (không thì từ chối khi có run đang chạy)")
    e.add_argument("--no-secrets", action="store_true", help="KHÔNG mang secret (chỉ ghi tên để đặt lại ở máy kia)")
    e.add_argument("--passphrase-file", default=None, metavar="FILE", help="đọc passphrase từ file (quyền 0600) thay vì hỏi — cho môi trường không TTY")
    e.add_argument("--with-totp", action="store_true", help="mang cả bí mật TOTP đăng nhập web (auth/totp.json)")
    e.add_argument("--with-mcp-tokens", action="store_true", help="mang cả token MCP HTTP của hub (mcp_server/tokens.json)")
    e.add_argument("--yes", "-y", dest="yes_move", action="store_true", help="bỏ qua xác nhận đóng băng của --for move")
    e.add_argument("--for", dest="purpose", choices=["clone", "move"], default="clone",
                   help="move = chuyển nhà: sau khi gói qua kiểm tra, ĐÓNG BĂNG hub này (không chạy run/lịch/Telegram/Slack) tới khi `hub unfreeze`")
    e.set_defaults(fn=cmd_export)
    i = hs.add_parser("import", allow_abbrev=False, help="nhập gói .cfhx vào HUB_HOME (daemon phải dừng; mặc định chế độ clone)", parents=[common])
    i.add_argument("file", help="file gói .cfhx")
    i.add_argument("--map-path", action="append", default=[], metavar="CŨ=MỚI", help="đổi thêm tiền tố đường dẫn (lặp lại được)")
    i.add_argument("--mode", choices=["clone", "move"], default="clone",
                   help="clone (mặc định): tắt Telegram/Slack, tạm dừng lịch, chặn task đang chạy; move: giữ nguyên")
    i.add_argument("--dry-run", action="store_true", help="chỉ in kế hoạch, không ghi gì")
    i.add_argument("--replace", action="store_true", help="home đích đã có hub → ghi đè (chụp bản sao lưu pre-restore trước)")
    i.add_argument("--stop", action="store_true", help="tự dừng daemon của home đích nếu đang chạy (và chạy lại sau khi nhập)")
    i.add_argument("--yes", "-y", action="store_true", help="bỏ qua xác nhận bước 1")
    i.add_argument("--confirm-id", default=None, help="xác nhận bước 2 khi --replace (phải trùng id gói)")
    i.add_argument("--passphrase-file", default=None, metavar="FILE", help="đọc passphrase của phần secret từ file (quyền 0600)")
    i.add_argument("--skip-secrets", action="store_true", help="không nhập secret (quên passphrase) — nhập phần còn lại, liệt kê secret cần đặt lại")
    i.add_argument("--clone-repos", action="store_true", help="tự `git clone` repo workspace chưa có ở máy này (sau khi nhập; không bao giờ vào thư mục đã có)")
    i.set_defaults(fn=cmd_import)
    u = hs.add_parser("unfreeze", allow_abbrev=False, help="gỡ đóng băng sau `hub export --for move` — máy này chạy lại run/lịch/kênh", parents=[common])
    u.add_argument("--yes", "-y", action="store_true", help="bỏ qua xác nhận")
    u.set_defaults(fn=cmd_unfreeze)
    dr = hs.add_parser("doctor", allow_abbrev=False, help="kiểm hub sau khi nhập (chỉ đọc, gợi ý lệnh; chạy được khi daemon dừng)", parents=[common])
    dr.add_argument("--probe", action="store_true", help="thử thật: `claude auth status`, kết nối MCP (cần daemon chạy) — không gọi model")
    dr.add_argument("--cleanup", action="store_true",
                    help="dọn thư mục làm việc mồ côi (hội thoại điều phối viên đã lưu trữ/không còn, workspace/worktree không ai tham chiếu): "
                         "liệt kê rồi hỏi y/N; không bao giờ xoá run/ledger/audit hay thư mục có run đang chạy")
    dr.add_argument("--older-than", type=float, default=None, metavar="NGÀY",
                    help="cùng --cleanup: dọn cả workspace scratch của run đã xong cũ hơn số ngày này")
    dr.add_argument("--yes", "-y", action="store_true", help="cùng --cleanup: bỏ qua xác nhận")
    dr.set_defaults(fn=cmd_doctor)
    ij = hs.add_parser("import-job", allow_abbrev=False, help="(nội bộ) tiến trình nhập do trang web /hub-transfer chạy — đừng gọi tay", parents=[common])
    ij.add_argument("upload", help="mã gói tải lên (up-…)")
    ij.add_argument("--supervised", action="store_true", help=argparse.SUPPRESS)
    ij.set_defaults(fn=cmd_import_job)


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
    pw = None
    if not a.no_secrets:
        from . import transfer_secrets as S

        if S.collect(ctx.paths.home, with_totp=a.with_totp, with_mcp_tokens=a.with_mcp_tokens):  # không có gì để mã hoá → không hỏi
            if a.passphrase_file:
                pw = S.check_passphrase(S.read_passphrase_file(a.passphrase_file), for_export=True)
            elif not sys.stdin.isatty():
                print("lỗi: gói mặc định mang secret đã mã hoá — không chạy trong terminal nên cần --passphrase-file <file 0600> "
                      "(hoặc --no-secrets để không mang secret)", file=sys.stderr)
                return 1
            else:
                print(f"Gói sẽ mang secret của hub, mã hoá bằng passphrase (≥ {S.MIN_PASSPHRASE} ký tự). Quên passphrase = mất phần secret "
                      "(phần còn lại vẫn nhập được với --skip-secrets).")
                pw = S.prompt_passphrase(confirm=True)
    move = a.purpose == "move"
    online = daemon_running(ctx.paths.home) is not None
    if move and not a.yes_move and not sys.stdin.isatty():
        print("lỗi: --for move đóng băng hub này sau khi xuất — không chạy trong terminal nên cần --yes", file=sys.stderr)
        return 1
    if move and not a.yes_move:
        ans = input("--for move: sau khi xuất, hub này bị ĐÓNG BĂNG (không chạy run/lịch/Telegram/Slack) cho tới `cfharness hub unfreeze`. "
                    "Tiếp tục? [y/N] ").strip().lower()
        if ans not in ("y", "yes", "c", "co", "có"):
            print("đã huỷ")
            return 0
    if online:
        r = ctx.call("hub.export", {"profile": a.profile, "stop": bool(a.stop), "secrets": pw is not None, "for": a.purpose}, timeout=3600)
    else:
        r = T.export_offline(ctx.paths, profile=a.profile, for_move=move)
    path = Path(r["path"])
    sealed: dict = {}
    if pw is not None:
        try:
            sealed = T.seal_package(path, ctx.paths.home, pw, with_totp=a.with_totp, with_mcp_tokens=a.with_mcp_tokens)
        except BaseException:
            if move:  # gói đã bị xoá → không có gì để mang sang máy mới: gỡ đóng băng vừa đặt
                _unfreeze_after_failure(ctx, r["id"], online)
            raise
        pw = None
        if sealed.get("sealed"):
            r.update(size=sealed["size"], sha256=sealed["sha256"], missing_secrets=sealed["missing_secrets"])
            r.setdefault("sections", {})["secrets"] = {"count": len(sealed["names"]), "bytes": None}
            if move:  # sha256 của gói ĐÃ gắn secret vào bản ghi đóng băng (cùng export_id)
                info = {"export_id": r["id"], "sha256": sealed["sha256"], "sealed": True}
                if online:
                    r["migrated_to"] = ctx.call("hub.freeze", info)
                else:
                    from .hub_freeze import freeze_offline

                    r["migrated_to"] = freeze_offline(ctx.paths, info)
    r["secrets"] = {k: sealed.get(k) for k in ("sealed", "names", "totp", "mcp_tokens")} if sealed else {"sealed": False, "names": []}
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
    lines += [f"  {k}: {s.get('count')} file, {_size(s.get('bytes'))} trước nén" for k, s in sorted(secs.items()) if k != "secrets"]
    if sealed.get("sealed"):
        extra = [x for x, on in (("TOTP web", sealed.get("totp")), ("token MCP HTTP", sealed.get("mcp_tokens"))) if on]
        lines.append(f"  secret: {len(sealed['names'])} đã mã hoá (scrypt + AES-256-GCM){' + ' + ', '.join(extra) if extra else ''} — "
                     "cần đúng passphrase khi nhập")
    if r.get("stopped_runs"):
        lines.append(f"  đã dừng {len(r['stopped_runs'])} run trước khi xuất: {', '.join(r['stopped_runs'][:5])}")
    if r.get("missing_secrets"):
        lines.append(f"  ! KHÔNG mang theo {len(r['missing_secrets'])} secret ({', '.join(r['missing_secrets'][:8])}"
                     f"{'…' if len(r['missing_secrets']) > 8 else ''}) — đặt lại trên máy kia bằng `cfharness secret set <TÊN>`")
    lines.append(("  ! ngoài phần secret, gói KHÔNG mã hoá" if sealed.get("sealed") else "  ! gói KHÔNG mã hoá")
                 + ": chứa memory, transcript, audit của hub — giữ riêng tư (file 0600), đừng đưa lên nơi công khai")
    for rp in r.get("repos") or []:
        if rp.get("dirty"):
            lines.append(f"  ! repo {rp['path']} có {rp.get('uncommitted')} file chưa commit — thay đổi chưa commit KHÔNG đi theo gói (commit + push trước)")
    if r.get("repos"):
        lines.append(f"  repo workspace: {len(r['repos'])} (chỉ ghi remote/nhánh/HEAD — repo KHÔNG nằm trong gói; máy kia clone lại"
                     + (", URL đã bỏ thông tin đăng nhập" if any(x.get("remote_masked") for x in r["repos"]) else "") + ")")
    if r.get("migrated_to"):
        from .hub_freeze import banner

        lines.append(f"  ■ {banner(r['migrated_to'])}")
        lines.append(f"Chép file này sang máy mới rồi chạy: cfharness hub import {path.name} --mode move --dry-run  (rồi bỏ --dry-run)")
    else:
        lines.append(f"Chép file này sang máy kia rồi chạy: cfharness hub import {path.name} --dry-run  (rồi bỏ --dry-run)")
    ctx.out(r, "\n".join(lines))
    return 0


def _unfreeze_after_failure(ctx: Any, export_id: str, online: bool) -> None:
    try:
        if online:
            ctx.call("hub.unfreeze", {"export_id": export_id})
        else:
            import sqlite3

            from .hub_freeze import KEY

            c = sqlite3.connect(str(ctx.paths.db), isolation_level=None)
            try:
                c.execute("DELETE FROM hub_state WHERE key=? AND json_extract(value, '$.export_id')=?", (KEY, export_id))
            finally:
                c.close()
        print("gắn secret lỗi — gói đã bị xoá, đã gỡ đóng băng (hub này chạy bình thường)", file=sys.stderr)
    except Exception as e:  # best-effort: lỗi đã in cho người kèm cách gỡ  # noqa: BLE001
        print(f"gắn secret lỗi VÀ không gỡ được đóng băng ({e}) — chạy `cfharness hub unfreeze`", file=sys.stderr)


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
    sec = p.get("secrets") or {}
    if sec.get("present"):
        if sec.get("skipped"):
            out.append(f"Secret: gói có {len(sec.get('names') or [])} secret mã hoá — BỎ QUA (--skip-secrets)")
        else:
            st = {True: "passphrase đúng", False: "passphrase SAI", None: "cần passphrase"}[sec.get("unlocked")]
            out.append(f"Secret: {len(sec.get('names') or [])} mã hoá ({st}) — sẽ nhập {len(sec.get('import') or [])}"
                       + (" + TOTP web" if sec.get("totp") else "") + (" + token MCP HTTP" if sec.get("mcp_tokens") else ""))
        if sec.get("skip_mode"):
            out.append(f"  bỏ theo chế độ {p.get('mode')} (bot/OAuth của máy nguồn): {', '.join(sec['skip_mode'])}")
    if p.get("missing_secrets"):
        out.append(f"Secret phải đặt lại sau khi nhập: {', '.join(p['missing_secrets'][:8])}{'…' if len(p['missing_secrets']) > 8 else ''}")
    cs = p.get("claude_sessions") or {}
    if cs.get("present"):
        out.append(f"Session Claude Code: {cs.get('sessions', 0)} session ({cs.get('files', 0)} file, {_size(cs.get('bytes'))}) → {cs.get('target')}/projects"
                   + (" — transcript đã che secret" if cs.get("redacted") else "")
                   + (" (cả ở chế độ clone: hội thoại điều phối viên tiếp tục ở máy này)" if p.get("mode") == "clone" else ""))
        if cs.get("conflicts"):
            out.append(f"  trùng id với session đã có ở máy này (giữ của máy này): {', '.join(cs['conflicts'][:5])}{'…' if len(cs['conflicts']) > 5 else ''}")
        if cs.get("cwd_unknown"):
            out.append(f"  {len(cs['cwd_unknown'])} session không rõ thư mục làm việc gốc — đặt vào thư mục project cũ")
    if p.get("approvals_cancelled") or p.get("questions_cancelled"):
        out.append(f"Huỷ (nhân bản — chưa chạy lại): {p.get('approvals_cancelled', 0)} yêu cầu duyệt, {p.get('questions_cancelled', 0)} câu hỏi đang chờ")
    if p.get("target_os"):
        out.append(f"Máy đích: {p['target_os']}" + (" · hệ thống file KHÔNG phân biệt hoa/thường" if p.get("case_insensitive") else ""))
    for r in (p.get("renamed") or [])[:10]:
        out.append(f"  đổi tên (tên không hợp lệ ở máy này): {r['from']} → {r['to']}")
    if len(p.get("renamed") or []) > 10:
        out.append(f"  … và {len(p['renamed']) - 10} file khác")
    for r in (p.get("names_refused") or [])[:10]:
        out.append(f"  ✗ {r['reason']}")
    for m in p.get("mcp_missing") or []:
        out.append(f"! MCP '{m['server']}': lệnh {m['command']} ({m.get('reason') or 'không thấy ở máy này'}) — nhập ở trạng thái ĐÁNH DẤU lỗi; "
                   f"sửa bằng `cfharness mcp edit {m['server']}`")
    repos = p.get("repos") or []
    if repos:
        out.append(f"Repo workspace: {len(repos)} ({sum(1 for r in repos if not r.get('exists'))} chưa có ở máy này)"
                   + (" — sẽ tự clone (--clone-repos)" if p.get("clone_repos") else ""))
        for r in repos[:10]:
            out.append(f"  {'✓' if r.get('exists') else '✗'} {r['to']}" + (f" ← {r['remote']}" if r.get("remote") else "")
                       + (f" [{r.get('branch')}]" if r.get("branch") else ""))
    if p.get("agents_paused"):
        out.append(f"Agent nhập ở trạng thái paused (workspace chưa có): {', '.join(sorted(p['agents_paused']))}")
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
    if a.skip_secrets and a.passphrase_file:
        print("lỗi: --skip-secrets và --passphrase-file loại trừ nhau", file=sys.stderr)
        return 1
    pw = None
    if a.passphrase_file:
        from . import transfer_secrets as S

        pw = S.read_passphrase_file(a.passphrase_file)
    plan = T.import_plan(ctx.paths, pkg, maps=maps, mode=a.mode, passphrase=pw if a.dry_run else None, skip_secrets=a.skip_secrets)
    plan["clone_repos"] = bool(a.clone_repos)
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
    sec = plan.get("secrets") or {}
    if sec.get("present") and not a.skip_secrets:
        from . import transfer_secrets as S

        if pw is None:
            if not tty:
                print("lỗi: gói có secret mã hoá — cần --passphrase-file <file 0600> (hoặc --skip-secrets để nhập phần còn lại)", file=sys.stderr)
                return 1
            pw = S.prompt_passphrase(confirm=False, prompt="Passphrase của phần secret: ")
        T.check_passphrase(pkg, pw)  # sai → HubError, chưa ghi gì
        print("passphrase đúng — phần secret giải mã được")
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
    except Exception:  # best-effort: config.yaml hỏng không được chặn nhập (dùng mặc định)
        pass
    r = T.import_package(ctx.paths, pkg, maps=maps, mode=a.mode, replace=a.replace, cfg=cfg, passphrase=pw, skip_secrets=a.skip_secrets,
                         clone_repos=a.clone_repos)
    pw = None
    lines = [f"đã nhập {r['imported']} vào {r['home']} (chế độ {r['mode']}): {r['files_written']} file, hub.db đã thay"
             + (f", {r['runs_files']} file stdout run" if r.get("runs_files") else "")
             + (f" · bản hiện trạng trước đó: {r['pre_restore']}" if r.get("pre_restore") else "")]
    rs = r.get("secrets") or {}
    if rs.get("imported"):
        lines.append(f"Secret đã nhập ({len(rs['imported'])}, file 0600): {', '.join(rs['imported'][:10])}{'…' if len(rs['imported']) > 10 else ''}"
                     + (" + TOTP web" if rs.get("totp") else "") + (" + token MCP HTTP" if rs.get("mcp_tokens") else ""))
    if rs.get("skipped_mode"):
        lines.append(f"Secret bỏ theo chế độ {r['mode']}: {', '.join(rs['skipped_mode'])} — bot Telegram/Slack RIÊNG (`cfharness notify … setup`), "
                     "MCP OAuth đăng nhập lại (`cfharness mcp login <tên>`)")
    if rs.get("kept_target"):
        lines.append(f"Giữ nguyên của máy này (khác gói): {', '.join(rs['kept_target'])}")
    if r.get("missing_secrets"):
        lines.append(f"Đặt lại {len(r['missing_secrets'])} secret: " + ", ".join(f"cfharness secret set {n}" for n in r["missing_secrets"][:6])
                     + ("…" if len(r["missing_secrets"]) > 6 else ""))
    if r.get("approvals_cancelled") or r.get("questions_cancelled"):
        lines.append(f"Đã huỷ {r.get('approvals_cancelled', 0)} yêu cầu duyệt + {r.get('questions_cancelled', 0)} câu hỏi đang chờ của máy nguồn "
                     "(nhân bản — người trả lời ở máy nguồn)")
    cs = r.get("claude_sessions") or {}
    if cs.get("written") or cs.get("conflicts"):
        lines.append(f"Session Claude Code: {len(cs.get('written') or [])} đã ghi vào {cs.get('target')}/projects (file 0600)"
                     + (f", bỏ {len(cs['conflicts'])} trùng id" if cs.get("conflicts") else ""))
    if r["mode"] == "clone":
        lines.append("Nhân bản: Telegram/Slack đã tắt (cần bot RIÊNG cho máy này: `cfharness notify telegram setup`), lịch đã tạm dừng "
                     "(`cfharness schedule list` rồi bật lịch cần chạy), task đang dở đã bị chặn (`cfharness task unblock <id>`).")
    if r.get("filehub"):
        lines.append(T.filehub_note(r["filehub"]))
    if r.get("renamed"):
        lines.append(f"Đổi tên {len(r['renamed'])} file tri thức không hợp lệ ở máy này (xem hub_state.import_followups.renamed)"
                     + (": skill " + ", ".join(f"{s['old']} → {s['new']}" for s in r.get("skills_renamed") or []) if r.get("skills_renamed") else ""))
    if r.get("mcp_missing"):
        lines.append("MCP thiếu lệnh (đã đánh dấu lỗi): " + ", ".join(f"cfharness mcp edit {m['server']}" for m in r["mcp_missing"][:6]))
    if r.get("review") or r.get("knowledge_rows_with_old_paths"):
        lines.append(f"Tri thức còn đường dẫn máy cũ ({len(r.get('review') or [])} file, {r.get('knowledge_rows_with_old_paths') or 0} hàng graph) — "
                     "daemon tự viết lại qua ledger (actor import, rollback được) ở lần lên đầu tiên")
    for c in r.get("repos_cloned") or []:
        lines.append(f"{'✓ đã clone' if c.get('ok') else '✗ không clone được'} {c.get('to')}" + (f": {c['error']}" if c.get("error") else "")
                     + (f" ({c['warning']})" if c.get("warning") else ""))
    if r.get("agents_unpaused"):
        lines.append(f"Agent đã có workspace sau khi clone → bỏ paused: {', '.join(r['agents_unpaused'])}")
    still = sorted(set(r.get("agents_paused") or {}) - set(r.get("agents_unpaused") or []))
    if still:
        lines.append(f"Agent paused vì workspace chưa có ở máy này: {', '.join(still)} — tạo/clone repo rồi `cfharness agent resume <id>`")
    if running:
        from .daemon import spawn_background

        pid = spawn_background(ctx.paths)
        lines.append(f"daemon đã chạy lại (pid {pid})")
    else:
        lines.append("chạy daemon: cfharness daemon start")
    from . import hub_doctor as D

    try:  # sau khi nhập: doctor (chỉ đọc) — việc còn phải làm ở máy này
        doc = D.run(ctx.paths)
        r["doctor"] = doc
        lines += ["", D.text(doc)]
    except Exception as e:  # noqa: BLE001 — doctor lỗi không làm hỏng lần nhập đã xong
        lines.append(f"(không chạy được hub doctor: {e} — chạy `cfharness hub doctor`)")
    ctx.out(r, "\n".join(lines))
    return 0


# ============================================================================ unfreeze / doctor
def cmd_unfreeze(ctx: Any) -> int:
    return _wrap(_unfreeze, ctx)


def _unfreeze(ctx: Any) -> int:
    from .hub_freeze import banner

    a = ctx.args
    st = ctx.call("hub.frozen") or {}
    mig = st.get("migrated_to")
    if not mig:
        ctx.out({"unfrozen": False, "migrated_to": None}, "hub không đóng băng — không có gì để gỡ")
        return 0
    print(banner(mig))
    if not a.yes:
        if not sys.stdin.isatty():
            print("lỗi: không chạy trong terminal — cần --yes", file=sys.stderr)
            return 1
        ans = input("Gỡ đóng băng? Máy này sẽ chạy lại run/lịch/Telegram/Slack — nếu máy mới cũng đang chạy, hai hub sẽ tranh bot/OAuth "
                    "(Telegram 409, Slack chia sự kiện). [y/N] ").strip().lower()
        if ans not in ("y", "yes", "c", "co", "có"):
            print("đã huỷ")
            return 0
    r = ctx.call("hub.unfreeze", {"export_id": mig.get("export_id")})
    ctx.out(r, "đã gỡ đóng băng — hub này chạy lại bình thường")
    return 0


def cmd_doctor(ctx: Any) -> int:
    return _wrap(_doctor, ctx)


def _doctor(ctx: Any) -> int:
    from . import hub_doctor as D
    from .home_migrate import daemon_running

    a = ctx.args
    if getattr(a, "cleanup", False):
        return _doctor_cleanup(ctx)
    if getattr(a, "older_than", None) is not None:
        print("lỗi: --older-than chỉ dùng cùng --cleanup", file=sys.stderr)
        return 1
    probe = None
    if a.probe and daemon_running(ctx.paths.home) is not None:
        def probe(name: str) -> dict:
            return ctx.call("mcp.probe", {"name": name, "timeout_s": 20})
    rep = D.run(ctx.paths, probe=bool(a.probe), mcp_probe=probe)
    ctx.out(rep, D.text(rep))
    return 0 if rep["ok"] else 1


def _doctor_cleanup(ctx: Any) -> int:
    """`hub doctor --cleanup`: daemon tính danh sách (`hub.housekeeping`) → in → y/N → `hub.cleanup` với ĐÚNG danh sách đã in (daemon tính
    lại và chỉ xoá phần vẫn còn dọn được — có run mới dùng thì giữ). Cần daemon (người duy nhất ghi; biết run nào đang sống)."""
    from . import hub_doctor as D
    from .housekeeping import human

    a = ctx.args
    days = a.older_than
    if days is not None and days < 1:
        print("lỗi: --older-than phải ≥ 1 ngày", file=sys.stderr)
        return 1
    plan = (ctx.call("hub.housekeeping", {"older_than_days": days, "refresh": True}) or {}).get("cleanup") or {}
    rels = [x["rel"] for x in plan.get("items") or [] if x.get("cleanable")]
    if not rels:
        ctx.out({"removed": [], "skipped": [], "freed_bytes": 0, "plan": plan}, "không có thư mục nào để dọn"
                + (f" (giữ {plan.get('busy')} thư mục đang có run/task dùng)" if plan.get("busy") else ""))
        return 0
    if not ctx.json:
        print(D.cleanup_text(plan))
    if not a.yes:
        if not sys.stdin.isatty():
            print("lỗi: không chạy trong terminal — cần --yes", file=sys.stderr)
            return 1
        ans = input(f"Xoá {len(rels)} thư mục trên? Chỉ xoá file trên đĩa (run/ledger/audit giữ nguyên); không hoàn tác được. [y/N] ").strip().lower()
        if ans not in ("y", "yes", "c", "co", "có"):
            print("đã huỷ")
            return 0
    r = ctx.call("hub.cleanup", {"paths": rels, "older_than_days": days}) or {}
    lines = [f"đã xoá {len(r.get('removed') or [])} thư mục, giải phóng {human(r.get('freed_bytes'))}"]
    for s in r.get("skipped") or []:
        lines.append(f"  giữ {s.get('rel')}: {s.get('reason')}")
    ctx.out(r, "\n".join(lines))
    return 0


def cmd_import_job(ctx: Any) -> int:
    return _wrap(_import_job, ctx)


def _import_job(ctx: Any) -> int:
    """PR6 (web): chạy lần nhập mà daemon đã chuẩn bị (`hub.import_start` → `imports/<upload>/job.json` + `pass`). Không hỏi gì — người đã
    xác nhận 2 bước trên web. Ghi `imports/<upload>/state.json`, khởi động lại daemon ở cuối (kể cả lỗi)."""
    import logging

    from .hub_transfer_web import run_import_job
    from .logs import setup_stream_json

    setup_stream_json("import-job")  # L7: stderr của job (= logs/import-job.out) là dòng JSON cùng schema hub.log
    jlog = logging.getLogger("cfharness.hub_transfer_web")
    jlog.info("nhập hub (web) bắt đầu: gói %s", ctx.args.upload)
    st = run_import_job(ctx.paths, ctx.args.upload, supervised=bool(ctx.args.supervised))
    (jlog.info if st.get("state") == "done" else jlog.error)("nhập hub (web): %s%s", st.get("state"),
                                                             f" — {st['error']}" if st.get("error") else "")
    return 0 if st.get("state") == "done" else 1
