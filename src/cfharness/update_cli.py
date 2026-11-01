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
          "restarting": "khởi động lại daemon", "verifying": "kiểm tra phiên bản", "finished": "xong"}


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    u = sub.add_parser("update", help="cập nhật cfharness lên bản phát hành mới nhất (dừng + khởi động lại daemon; chỉ người)", parents=[common])
    u.add_argument("--check", action="store_true", help="chỉ kiểm tra có bản mới không")
    u.add_argument("--yes", "-y", action="store_true", help="không hỏi lại")
    u.add_argument("--force", action="store_true", help="cập nhật cả khi đang có run chạy (run bị ngắt sẽ tự tiếp tục) hoặc cài lại cùng bản")
    u.add_argument("--tag", default=None, help="cài đúng tag vX.Y.Z thay vì bản mới nhất")
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
            if st["state"] == "done":
                print(f"đã cập nhật {st.get('version_before')} → {st.get('version_after') or st.get('tag')} · daemon đã chạy lại")
                print("Giao diện web / `cfharness chat` đang mở vẫn là bản cũ — tắt và mở lại.")
                return 0
            print(f"lỗi: {st.get('error')}\nlog: {ctx.paths.logs_dir / 'update.log'}", file=sys.stderr)
            return 1
        time.sleep(1.0)
    print(f"chưa xong sau {int(timeout)} s — xem log {ctx.paths.logs_dir / 'update.log'}", file=sys.stderr)
    return 1


def cmd_update(ctx: Any) -> int:
    a = ctx.args
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
