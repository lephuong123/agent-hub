"""CLI `cfharness update [--check] [--yes] [--force] [--tag vX.Y.Z]` — cập nhật cfharness lên bản phát hành mới nhất (updater.py).

Client thuần: `update.check` / `update.start` qua RPC; trong lúc daemon dừng để cài đè, theo dõi `HUB_HOME/update/state.json` (file do
tiến trình phụ ghi) tới khi `done|failed`. Lệnh chỉ người (classifier: `update` ∈ HUB_HUMAN_ONLY_CLI; RPC `update.start` ∈ HUMAN_ONLY).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PHASES = {"starting": "chuẩn bị", "waiting_daemon": "chờ daemon dừng", "installing": "đang tải + cài bản mới",
          "restarting": "khởi động lại daemon", "health": "chờ daemon trả lời", "rolling_back": "quay về bản trước (cài lại + hub.db nếu cần)",
          "rollback_restarting": "khởi động lại daemon bản trước", "verifying": "kiểm tra phiên bản", "finished": "xong"}


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    u = sub.add_parser("update", help="cập nhật cfharness lên bản phát hành mới nhất (dừng + khởi động lại daemon; chỉ người)", parents=[common])
    u.add_argument("--check", action="store_true", help="chỉ kiểm tra có bản mới không")
    u.add_argument("--yes", "-y", action="store_true", help="không hỏi lại")
    u.add_argument("--force", action="store_true", help="cập nhật cả khi đang có run chạy (run bị ngắt sẽ tự tiếp tục) hoặc cài lại cùng bản")
    u.add_argument("--tag", default=None, help="cài đúng tag vX.Y.Z thay vì bản mới nhất")
    u.add_argument("--rollback", action="store_true",
                   help="quay về bản trước lần cập nhật gần nhất (+ hub.db trước cập nhật nếu schema đã tăng)")
    u.set_defaults(fn=cmd_update)


def _summary(c: dict[str, Any]) -> str:
    lt = c.get("latest") or {}
    lines = [f"đang chạy: {c['current']}"]
    lines.append(f"mới nhất:  {lt.get('tag')}" + (f" ({str(lt.get('published_at') or '')[:10]})" if lt.get("published_at") else "")
                 if lt.get("ok") else f"mới nhất:  ? — {lt.get('error')}")
    if c.get("update_available"):
        lines.append(f"→ có bản mới: {lt.get('url') or c.get('releases_url')}")
    elif lt.get("ok"):
        lines.append("→ đã là bản mới nhất")
    if not c.get("can_update"):
        lines.append(f"! không cập nhật tự động được: {c.get('reason')}")
    return "\n".join(lines)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _follow(ctx: Any, timeout: float = 1200.0) -> int:
    path = ctx.paths.home / "update" / "state.json"
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        st = _read(path) or {}
        if st.get("phase") != last:
            last = st.get("phase")
            print(f"  … {PHASES.get(str(last), last)}", flush=True)
        if st.get("state") in ("done", "failed"):
            if st["state"] == "done" and st.get("kind") == "rollback":
                rb = st.get("rollback") or {}
                print(f"đã quay về {st.get('version_after') or st.get('tag')}" + (" + hub.db trước cập nhật" if rb.get("db_restored") else "")
                      + " · daemon đã chạy lại")
                _after_update(ctx, st.get("version_after") or st.get("tag"))
                return 0
            if st["state"] == "done":
                print(f"đã cập nhật {st.get('version_before')} → {st.get('version_after') or st.get('tag')} · daemon đã chạy lại")
                _after_update(ctx, st.get("version_after") or st.get("tag"))
                return 0
            print(f"lỗi: {st.get('error')}\nlog: {ctx.paths.logs_dir / 'update.log'}", file=sys.stderr)
            return 1
        time.sleep(1.0)
    print(f"chưa xong sau {int(timeout)} s — xem log {ctx.paths.logs_dir / 'update.log'}", file=sys.stderr)
    return 1


WEB_WAIT_S = 120.0  # > reload.GRACE_S (45) + RESPAWN_WAIT_S (35): daemon bản mới tự dừng web cũ để launchd bật lại trước khi CLI ra tay
TERMINAL_CMDS = ("chat", "top", "attach", "manager")


def _norm_version(v: Any) -> str:
    return str(v or "").strip().lstrip("vV")


def web_after_update(paths: Any, version: Any, *, wait_s: float = WEB_WAIT_S, read_state: Any = None, restart: Any = None,
                     sleep: Any = time.sleep, clock: Any = time.monotonic, progress: Any = None) -> str:
    """Sau cập nhật: chờ giao diện web chạy lại bằng bản mới (daemon bản mới dừng web cũ → launchd/daemon bật lại — web/reload.py).
    Hết `wait_s` mà web vẫn chạy bản cũ → tự chạy lại (`restart_stale_web`, cùng hàm daemon dùng). Người dùng trên host (web qua
    LaunchDaemon) không có cách nào tự "tắt và mở lại" web — nên CLI không được đẩy việc đó cho họ.
    Trả: none (không có web) | ok | restarted | failed."""
    import os

    if os.name == "nt":  # Windows: helper đã dừng + chạy lại web (update_helper.stop_venv_holders)
        return "none"
    if read_state is None:
        from .web.security import read_state
    want = _norm_version(version)
    end, told = clock() + wait_s, False
    while True:
        st = read_state(paths)
        if not st:
            return "none"
        if _norm_version(st.get("version")) == want:
            return "ok"
        if clock() >= end:
            break
        if not told and progress:
            progress("  … chờ giao diện web chạy lại bằng bản mới")
            told = True
        sleep(2.0)
    if restart is None:
        from .web.reload import restart_stale_web as restart
    try:
        res = restart(paths, want)
    except Exception:  # noqa: BLE001
        return "failed"
    if res in ("killed", "respawned"):
        return "restarted"
    st = read_state(paths)
    return "ok" if st and _norm_version(st.get("version")) == want else "failed"


def open_terminals(cmdline_rows: Any = None) -> int:
    """Số cửa sổ `cfharness chat|top|attach|manager` đang mở của user này (chạy code cũ tới khi thoát)."""
    import os
    import subprocess

    if cmdline_rows is None:
        if os.name == "nt":
            return 0
        try:
            r = subprocess.run(["ps", "-U", str(os.getuid()), "-o", "pid=,command="], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return 0
        cmdline_rows = r.stdout.splitlines()
    me, n = os.getpid(), 0
    for row in cmdline_rows:
        parts = row.split()
        if len(parts) < 3 or not parts[0].isdigit() or int(parts[0]) == me:
            continue
        for i, tok in enumerate(parts[1:-1], start=1):
            if (os.path.basename(tok).startswith("cfharness") or (tok == "cfharness" and parts[i - 1] == "-m")) and parts[i + 1] in TERMINAL_CMDS:
                n += 1
                break
    return n


def _after_update(ctx: Any, version: Any) -> None:
    res = web_after_update(ctx.paths, version, progress=lambda m: print(m, flush=True))
    if res == "ok":
        print("giao diện web đã tự chạy lại bằng bản mới — tải lại trang (có thể phải đăng nhập lại)")
    elif res == "restarted":
        print("đã chạy lại giao diện web bằng bản mới — tải lại trang (có thể phải đăng nhập lại)")
    elif res == "failed":
        print(f"! giao diện web chưa chạy lại được bằng bản mới — xem {ctx.paths.logs_dir / 'web.log'}", file=sys.stderr)
    n = open_terminals()
    if n:
        print(f"đang mở {n} cửa sổ `cfharness chat`/`top` chạy bản cũ — thoát (Ctrl-C hoặc q) rồi mở lại")


def cmd_rollback(ctx: Any) -> int:
    """`update --rollback`: qua daemon (RPC `update.rollback`); daemon không lên được (bản mới hỏng) → chạy helper tại chỗ từ state.json."""
    from .errors import HubError, exit_code_for
    from .rpc import DaemonUnavailable
    from .updater import Updater

    a = ctx.args
    up = Updater(ctx.paths)
    try:
        tgt = up.rollback_target()
    except HubError as e:
        print(f"lỗi: {e}", file=sys.stderr)
        return exit_code_for(e.code)
    bk = tgt.get("backup") or {}
    print(f"Quay về {tgt['tag']} (đang chạy {tgt['from']}, lần cập nhật lên {tgt.get('rollback_of')}).")
    print(f"hub.db: khôi phục từ bản sao lưu {bk['id']} NẾU schema đã tăng (dữ liệu ghi sau lúc đó được giữ riêng ở backups/)"
          if bk.get("id") else "hub.db: giữ nguyên (không có bản sao lưu trước cập nhật)")
    if not a.yes:
        if not sys.stdin.isatty():
            print("lỗi: cần --yes khi không chạy trong terminal", file=sys.stderr)
            return 1
        if input("Daemon sẽ dừng rồi chạy lại bằng bản trước. Tiếp tục? [y/N] ").strip().lower() not in ("y", "yes", "c", "co", "có"):
            print("đã huỷ")
            return 0
    try:
        ctx.client(autostart=False)
        r = ctx.call("update.rollback", {"force": a.force})
    except DaemonUnavailable:
        import os

        env = dict(os.environ)
        env["CFHARNESS_HOME"] = str(ctx.paths.home)
        env.pop("XPC_SERVICE_NAME", None)
        try:
            r = up.rollback(force=True, env=env)
        except HubError as e:
            print(f"lỗi: {e}", file=sys.stderr)
            return exit_code_for(e.code)
    print(f"bắt đầu quay về → {r.get('tag')} (log: {r.get('log')})", flush=True)
    return _follow(ctx)


def cmd_update(ctx: Any) -> int:
    a = ctx.args
    if a.rollback:
        return cmd_rollback(ctx)
    c = ctx.call("update.check", {"refresh": True})
    if a.check or (ctx.json and not a.yes):
        ctx.out(c, _summary(c))
        return 0
    print(_summary(c))
    if not c.get("can_update"):
        return 1
    if c.get("in_progress"):
        print("đang có một lần cập nhật chạy — theo dõi:")
        return _follow(ctx)
    if not c.get("update_available") and not a.force and not a.tag:
        return 0
    target = a.tag or (c.get("latest") or {}).get("tag")
    if not a.yes:
        if not sys.stdin.isatty():
            print("lỗi: cần --yes khi không chạy trong terminal", file=sys.stderr)
            return 1
        ans = input(f"Cập nhật {c['current']} → {target}? Daemon sẽ dừng ~1 phút rồi tự chạy lại. [y/N] ").strip().lower()
        if ans not in ("y", "yes", "c", "co", "có"):
            print("đã huỷ")
            return 0
    r = ctx.call("update.start", {"tag": a.tag, "force": a.force})
    print(f"bắt đầu cập nhật → {r.get('tag')} (log: {r.get('log')})", flush=True)
    return _follow(ctx)
