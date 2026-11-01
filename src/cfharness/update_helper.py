"""Tiến trình phụ cập nhật cfharness — CHỈ stdlib, không import `cfharness`.

Daemon chép file này sang `HUB_HOME/update/helper.py` và chạy bằng python GỐC (ngoài venv của tool, cờ `-I`), tách session, rồi tự dừng.
Lý do: `uv tool install --force` xoá + tạo lại venv của tool — tiến trình phụ không được phụ thuộc file nào trong venv đó.

Các bước (trạng thái ghi nguyên tử vào `--state`, log nối vào `--log`):
1. `waiting_daemon`: chờ pid daemon thoát (≤ 60 s, quá thì SIGTERM, thêm 15 s).
2. `installing`: chạy `--install` (argv JSON), timeout 15 phút.
3. `restarting`: chạy `--restart` (argv JSON — khởi động lại daemon) — LUÔN chạy, kể cả khi cài lỗi (venv cũ có thể còn dùng được).
4. `verifying`: chạy `--verify` (argv JSON, in phiên bản) → `version_after`.
Kết thúc: `state = done | failed` (+ `error`).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

WAIT_DAEMON_S = 60.0
INSTALL_TIMEOUT_S = 900.0
RESTART_TIMEOUT_S = 90.0


def _now() -> float:
    return time.time()


def write_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def no_window_kwargs() -> dict[str, Any]:
    """Bản stdlib của `oscompat.no_window_kwargs` (helper không import cfharness): Windows không bật cửa sổ console cho lệnh con."""
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, **no_window_kwargs())
        return str(pid) in (r.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.2)
    return not _alive(pid)


def _run(argv: list[str], log, timeout: float, env: dict[str, str]) -> tuple[int, str]:
    log.write(f"$ {' '.join(argv)}\n")
    log.flush()
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=timeout, env=env, text=True,
                           errors="replace", **no_window_kwargs())
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")
        log.write(out + f"\n[quá {int(timeout)} s — đã dừng]\n")
        return 124, out
    except OSError as e:
        log.write(f"[không chạy được: {e}]\n")
        return 127, str(e)
    log.write(p.stdout or "")
    log.write(f"[mã thoát {p.returncode}]\n")
    log.flush()
    return p.returncode, p.stdout or ""


def apply(a: argparse.Namespace) -> int:
    state_path = Path(a.state)
    st: dict[str, Any] = json.loads(Path(a.state).read_text(encoding="utf-8")) if state_path.exists() else {}
    st.update({"state": "running", "pid": os.getpid()})

    def phase(name: str, **extra: Any) -> None:
        st.update({"phase": name, "updated_at": _now(), **extra})
        write_state(state_path, st)

    env = dict(os.environ)
    if a.home:
        env["CFHARNESS_HOME"] = a.home
    Path(a.log).parent.mkdir(parents=True, exist_ok=True)
    with open(a.log, "a", encoding="utf-8") as log:
        log.write(f"\n=== cập nhật cfharness {st.get('version_before') or '?'} → {st.get('tag') or '?'} · {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        phase("waiting_daemon")
        if a.wait_pid and not _wait_exit(int(a.wait_pid), WAIT_DAEMON_S):
            log.write(f"daemon (pid {a.wait_pid}) chưa thoát sau {int(WAIT_DAEMON_S)} s — gửi SIGTERM\n")
            try:
                os.kill(int(a.wait_pid), signal.SIGTERM)
            except OSError:
                pass
            _wait_exit(int(a.wait_pid), 15.0)
        phase("installing")
        rc, out = _run(json.loads(a.install), log, INSTALL_TIMEOUT_S, env)
        error = None if rc == 0 else f"cài đặt lỗi (mã {rc}): {out.strip().splitlines()[-1] if out.strip() else 'không có output'}"
        phase("restarting", **({"error": error} if error else {}))
        rrc, rout = _run(json.loads(a.restart), log, RESTART_TIMEOUT_S, env)
        if rrc != 0:
            msg = f"khởi động lại daemon lỗi (mã {rrc}) — chạy tay: cfharness daemon start"
            error = f"{error}; {msg}" if error else msg
        phase("verifying")
        vrc, vout = _run(json.loads(a.verify), log, 30.0, env) if a.verify else (1, "")
        version_after = (vout.strip().split() or [""])[-1] if vrc == 0 else None
        st.update({"state": "failed" if error else "done", "phase": "finished", "finished_at": _now(), "version_after": version_after,
                   "error": error, "updated_at": _now()})
        write_state(state_path, st)
        log.write(f"=== {'LỖI: ' + error if error else 'xong'} · phiên bản sau cập nhật: {version_after or '?'} ===\n")
    return 0 if not error else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cfharness-update-helper")
    sub = p.add_subparsers(dest="cmd", required=True)
    ap = sub.add_parser("apply")
    ap.add_argument("--state", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--home", default=None)
    ap.add_argument("--wait-pid", type=int, default=0)
    ap.add_argument("--install", required=True, help="argv JSON lệnh cài")
    ap.add_argument("--restart", required=True, help="argv JSON lệnh khởi động lại daemon")
    ap.add_argument("--verify", default=None, help="argv JSON lệnh in phiên bản")
    a = p.parse_args(argv)
    try:
        return apply(a)
    except Exception as e:  # pragma: no cover — mọi lỗi phải về trạng thái failed, không để web chờ mãi
        try:
            st = json.loads(Path(a.state).read_text(encoding="utf-8"))
        except Exception:
            st = {}
        st.update({"state": "failed", "phase": "finished", "error": f"lỗi tiến trình cập nhật: {e}", "finished_at": _now(), "updated_at": _now()})
        write_state(Path(a.state), st)
        return 1


if __name__ == "__main__":
    sys.exit(main())
