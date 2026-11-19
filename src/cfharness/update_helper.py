"""Tiến trình phụ cập nhật cfharness — CHỈ stdlib, không import `cfharness`.

Daemon chép file này sang `HUB_HOME/update/helper.py` và chạy bằng python GỐC (ngoài venv của tool, cờ `-I`), tách session, rồi tự dừng.
Lý do: `uv tool install --force` xoá + tạo lại venv của tool — tiến trình phụ không được phụ thuộc file nào trong venv đó.

Các bước (trạng thái ghi nguyên tử vào `--state`, log nối vào `--log`):
1. `waiting_daemon`: chờ pid daemon thoát (≤ 60 s, quá thì SIGTERM, thêm 15 s).
1b. `stopping_holders`: dừng MỌI tiến trình khác còn chạy từ venv của tool (`--venv`: web UI `cfharness web`, `mcp serve`, TUI…).
   Windows không cho xoá file đang mở → `uv tool install --force` lỗi "Access is denied" giữa chừng, venv bị xoá dở, quay về cũng lỗi
   theo và hub chết hẳn (sự cố khi lên v0.2.11). Tiến trình `cfharness web` bị dừng được chạy lại sau cập nhật (bỏ `--open`).
2. `installing`: chạy `--install` (argv JSON), timeout 15 phút. Lỗi vì file bị khoá → dừng tiến trình giữ venv lần nữa, cài lại một lần.
3. `restarting`: chạy `--restart` (argv JSON — khởi động lại daemon) — LUÔN chạy, kể cả khi cài lỗi (venv cũ có thể còn dùng được).
4. `health`: chờ daemon trả lời `hub.ping` qua `--socket` (≤ `--health-timeout` s, rồi kiểm lại sau `STABLE_S` s — không trả lời
   = daemon không lên). Không có `--socket` (Windows) → dựa vào mã thoát của lệnh khởi động lại.
5. `verifying`: chạy `--verify` (argv JSON, in phiên bản) → `version_after`.
6. Lưới an toàn — daemon KHÔNG lên và có `--rollback-install`: `rolling_back` = dừng daemon còn sót (`hub.pid`), nếu `user_version`
   của hub.db đã TĂNG so với `--db-version` (migration chỉ tiến) thì khôi phục hub.db từ bản sao lưu pre-update `--db-backup`
   (tarfile + sqlite3 stdlib; DB đang có được giữ ở `backups/hub.db.replaced-<ts>`), cài lại tag cũ, khởi động lại + kiểm lần nữa.
Kết thúc: `state = done | failed` (+ `error`, `rollback{…}` khi đã quay về).

Lệnh `rollback` (`cfharness update --rollback`): chờ daemon thoát → khôi phục DB nếu cần → cài `--install` (tag cũ) → khởi động lại → kiểm.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

WAIT_DAEMON_S = 60.0
INSTALL_TIMEOUT_S = 900.0
RESTART_TIMEOUT_S = 90.0
HEALTH_TIMEOUT_S = 60.0
STABLE_S = 3.0  # daemon trả lời rồi chết ngay (lỗi lúc nạp dịch vụ nền) cũng là "không lên"


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------- log JSON lines (L7, plan-logging.md B1 — chỉ stdlib)
class JsonLog:
    """Bọc file log: mỗi DÒNG chữ ghi qua `write()` thành một dòng JSON cùng trường lõi với `logs.JsonFormatter` của hub
    (`ts`, `level`, `logger`, `msg`, `proc`, `pid`) — `cfharness logs` / gói chẩn đoán đọc được. Chữ người đọc giữ nguyên trong `msg`.
    Không import cfharness (helper chạy ngoài venv bị cài đè)."""

    def __init__(self, f: Any, *, logger: str = "cfharness.update_helper", proc: str = "helper") -> None:
        self.f, self.logger, self.proc = f, logger, proc
        self.pid = os.getpid()
        self._buf = ""

    @staticmethod
    def _level(line: str) -> str:
        low = line.lower()
        if "lỗi" in low or "không chạy" in low or "không trả lời" in low or "không khôi phục" in low:
            return "error"
        if "quá " in low or "sigterm" in low or "dừng" in low:
            return "warning"
        return "info"

    def _emit(self, line: str) -> None:
        line = line.rstrip("\r")
        if not line.strip():
            return
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"
        rec = {"ts": ts, "level": self._level(line), "logger": self.logger, "msg": line, "proc": self.proc, "pid": self.pid}
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)
        return len(s)

    def flush(self) -> None:
        self.f.flush()

    def close(self) -> None:
        if self._buf:
            self._emit(self._buf)
            self._buf = ""
        self.f.flush()


class open_log:  # noqa: N801 — dùng như `open()`
    """`with open_log(path) as log:` — file append (0600 khi tạo mới) + `JsonLog`."""

    def __init__(self, path: Any) -> None:
        self.path = path

    def __enter__(self) -> JsonLog:
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        self._f = os.fdopen(fd, "a", encoding="utf-8")
        self._log = JsonLog(self._f)
        return self._log

    def __exit__(self, *exc: Any) -> None:
        try:
            self._log.close()
        finally:
            self._f.close()


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


def ping(sock: str, timeout: float = 2.0) -> bool:
    """`hub.ping` qua Unix socket (JSON-RPC một dòng). Không cần token: method đọc công khai."""
    if os.name == "nt" or not hasattr(socket, "AF_UNIX"):  # pragma: no cover
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(sock)
            s.sendall(b'{"jsonrpc":"2.0","id":1,"method":"hub.ping","params":{}}\n')
            buf = b""
            while not buf.endswith(b"\n") and len(buf) < 65536:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        return bool((json.loads(buf or b"{}").get("result") or {}).get("pong"))
    except (OSError, ValueError, AttributeError):
        return False


def wait_healthy(sock: str | None, timeout: float, restart_rc: int, log) -> bool:
    if not sock or os.name == "nt":  # pragma: no cover — Windows: chưa có kênh dò đơn giản bằng stdlib
        return restart_rc == 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ping(sock):
            time.sleep(STABLE_S)
            if ping(sock):
                log.write("daemon trả lời hub.ping — đã lên\n")
                return True
        time.sleep(0.5)
    log.write(f"daemon KHÔNG trả lời hub.ping sau {int(timeout)} s\n")
    return False


def db_user_version(db: Path) -> int | None:
    if not db.is_file():
        return None
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            return int(c.execute("PRAGMA user_version").fetchone()[0])
        finally:
            c.close()
    except sqlite3.Error:
        return None


# ---------------------------------------------------------------- tiến trình khác đang chạy từ venv của tool
LOCKED_MARKERS = ("access is denied", "os error 5)", "os error 32)", "being used by another process", "permission denied")


def _windows() -> bool:
    return os.name == "nt"


def _process_table() -> list[tuple[int, int, str]]:
    """(pid, ppid, exe + dòng lệnh) mọi tiến trình. Không đọc được → rỗng (không chặn cập nhật)."""
    rows: list[tuple[int, int, str]] = []
    try:
        if os.name == "nt":  # pragma: no cover - chỉ chạy trên Windows
            ps = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine "
                  "| ConvertTo-Json -Compress")
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60, **no_window_kwargs())
            data = json.loads(r.stdout or "[]")
            for d in data if isinstance(data, list) else [data]:
                rows.append((int(d.get("ProcessId") or 0), int(d.get("ParentProcessId") or 0),
                             f"{d.get('ExecutablePath') or ''} {d.get('CommandLine') or ''}"))
        else:
            r = subprocess.run(["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True, errors="replace", timeout=30)
            for line in (r.stdout or "").splitlines():
                parts = line.split(None, 2)
                if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                    rows.append((int(parts[0]), int(parts[1]), parts[2]))
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return rows


def venv_holders(venv: str, table: list[tuple[int, int, str]] | None = None) -> list[tuple[int, str]]:
    """Tiến trình có exe/dòng lệnh nằm trong `venv` (trừ chính helper và cha của nó)."""
    if not venv:
        return []
    needle = os.path.normcase(os.path.normpath(venv)).rstrip("\\/") + os.sep
    me = {os.getpid(), os.getppid()}
    out = []
    for pid, _ppid, cmd in table if table is not None else _process_table():
        if pid > 0 and pid not in me and needle in os.path.normcase(cmd.replace("/", os.sep)):
            out.append((pid, cmd))
    return out


def web_restart_argv(cmd: str) -> list[str] | None:
    """Dòng lệnh `…python[.exe] <launcher cfharness> web [args]` → argv chạy lại web (bỏ `--open`: không bật tab trình duyệt mới)."""
    try:
        parts = [x.strip('"') for x in shlex.split(cmd, posix=False)] if os.name == "nt" else shlex.split(cmd)
    except ValueError:
        return None
    for i, tok in enumerate(parts):
        if tok == "web" and i >= 1 and "cfharness" in os.path.basename(parts[i - 1]).lower():
            return [parts[i - 1], "web", *[x for x in parts[i + 1:] if x != "--open"]]
    return None


def _kill(pid: int) -> None:
    if os.name == "nt":  # pragma: no cover
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, **no_window_kwargs())
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    if not _wait_exit(pid, 10.0):
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass


def stop_venv_holders(venv: str | None, log, restarts: list[list[str]]) -> None:
    """Dừng tiến trình giữ file trong venv của tool; argv `cfharness web` cần chạy lại sau cập nhật được thêm vào `restarts`.
    CHỈ Windows: macOS/Linux xoá được file đang mở nên uv thay venv khi tiến trình vẫn chạy — không dừng gì (tránh làm chết
    daemon của hub khác dùng chung bản cài)."""
    if not venv or not _windows():
        return
    holders = venv_holders(venv)
    for pid, cmd in holders:
        log.write(f"dừng tiến trình đang chạy từ venv của tool (pid {pid}): {cmd.strip()[:200]}\n")
        w = web_restart_argv(cmd)
        if w and w not in restarts:
            restarts.append(w)
        _kill(pid)
    for pid, _ in holders:
        _wait_exit(pid, 10.0)
    log.flush()


def start_detached(argv: list[str], env: dict[str, str], log) -> None:
    """Chạy lại `cfharness web` tách khỏi helper (helper sắp thoát)."""
    log.write(f"chạy lại: {' '.join(argv)}\n")
    kw: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "env": env}
    if os.name == "nt":  # pragma: no cover
        kw["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
                               | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(argv, **kw)
    except OSError as e:
        log.write(f"[không chạy lại được: {e}]\n")


def install_with_retry(a: argparse.Namespace, install: list[str], log, env: dict[str, str], restarts: list[list[str]]) -> tuple[int, str]:
    """Cài; lỗi vì file trong venv bị khoá (tiến trình mở lại sau bước dừng) → dừng lần nữa rồi cài lại MỘT lần."""
    rc, out = _run(install, log, INSTALL_TIMEOUT_S, env)
    if rc != 0 and getattr(a, "venv", None) and any(m in out.lower() for m in LOCKED_MARKERS):
        log.write("cài lỗi vì file trong venv đang bị giữ — dừng tiến trình giữ venv rồi thử lại\n")
        stop_venv_holders(a.venv, log, restarts)
        rc, out = _run(install, log, INSTALL_TIMEOUT_S, env)
    return rc, out


def stop_daemon(home: Path, log) -> None:
    """Dừng daemon còn sót của HUB_HOME (theo `hub.pid`): SIGTERM, 15 s, rồi SIGKILL. Không bao giờ đụng chính mình/cha."""
    try:
        pid = int((home / "hub.pid").read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return
    if pid <= 0 or pid in (os.getpid(), os.getppid()) or not _alive(pid):
        return
    log.write(f"dừng daemon còn sót (pid {pid})\n")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    if not _wait_exit(pid, 15.0):
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except OSError:
            pass
        _wait_exit(pid, 5.0)


def _lock(home: Path, wait_s: float = 15.0) -> Any:
    """Khoá `daemon.lock` (như daemon) để không daemon nào mở hub.db trong lúc thay. Windows: không khoá (trả True)."""
    if os.name == "nt":  # pragma: no cover
        return True
    import fcntl

    deadline = time.monotonic() + wait_s
    while True:
        f = os.fdopen(os.open(home / "daemon.lock", os.O_RDWR | os.O_CREAT, 0o600), "r+b")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except OSError:
            f.close()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.2)


def restore_db(home: Path, archive: Path, log) -> str:
    """Thay HUB_HOME/hub.db bằng hub.db trong bản sao lưu (tar.gz của backup.py). DB hiện có → backups/hub.db.replaced-<ts>(+ -wal/-shm)."""
    bdir = home / "backups"
    bdir.mkdir(parents=True, exist_ok=True)
    tmp = bdir / f".helper-db-{os.getpid()}"
    with tarfile.open(archive, "r:gz") as tf:
        m = tf.getmember("hub.db")
        src = tf.extractfile(m)
        if not m.isreg() or src is None:
            raise RuntimeError("bản sao lưu không có hub.db")
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
            shutil.copyfileobj(src, f)
    c = sqlite3.connect(str(tmp))
    try:
        ok = str(c.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        c.close()
    if ok != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"hub.db trong bản sao lưu hỏng: {ok}")
    db = home / "hub.db"
    aside = bdir / f"hub.db.replaced-{time.strftime('%Y%m%dT%H%M%S')}"
    if db.exists():
        shutil.move(str(db), str(aside))
    for suffix in ("-wal", "-shm", "-journal"):
        side = Path(f"{db}{suffix}")
        if side.exists():
            shutil.move(str(side), f"{aside}{suffix}")
    os.replace(tmp, db)
    os.chmod(db, 0o600)
    restore_anchors(home, archive, aside, log)
    log.write(f"đã khôi phục hub.db từ {archive.name} (DB trước đó giữ ở {aside.name})\n")
    return str(aside)


ANCHORS = "audit-anchors.jsonl"  # = audit_chain.ANCHORS_FILE (helper không import cfharness)


def restore_anchors(home: Path, archive: Path, aside: Path, log) -> None:
    """L4: bản neo chuỗi băm audit phải khớp hub.db vừa khôi phục — lấy bản trong cùng bản sao lưu; bản sao lưu không có (trước L4) →
    chuyển file hiện có sang `<aside>.audit-anchors.jsonl` (neo trỏ tới dòng DB khôi phục không có → kiểm sẽ báo nhầm là bị xoá)."""
    cur = home / ANCHORS
    try:
        if cur.exists() or cur.is_symlink():
            shutil.move(str(cur), f"{aside}.{ANCHORS}")
        with tarfile.open(archive, "r:gz") as tf:
            try:
                m = tf.getmember(f"home/{ANCHORS}")
            except KeyError:
                return
            src = tf.extractfile(m)
            if not m.isreg() or src is None:
                return
            with os.fdopen(os.open(cur, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
                shutil.copyfileobj(src, f)
    except (OSError, tarfile.TarError) as e:  # không chặn quay về; tệ nhất là audit verify báo bản neo lệch
        log.write(f"không khôi phục được {ANCHORS}: {e}\n")


def restore_db_if_migrated(a: argparse.Namespace, log) -> dict[str, Any]:
    """Migration chỉ tiến: user_version đã TĂNG so với lúc chụp → khôi phục hub.db từ bản pre-update (daemon phải dừng)."""
    out: dict[str, Any] = {"db_restored": False}
    if not a.home or not a.db_backup or a.db_version is None or a.db_version < 0:
        return out
    home = Path(a.home)
    cur = db_user_version(home / "hub.db")
    out.update({"user_version_before": a.db_version, "user_version_now": cur})
    if cur is None or cur <= a.db_version:
        return out
    log.write(f"schema DB đã tăng v{a.db_version} → v{cur} — khôi phục hub.db từ bản sao lưu trước cập nhật\n")
    stop_daemon(home, log)
    lk = _lock(home)
    if lk is None:
        out["db_error"] = "daemon vẫn giữ khoá daemon.lock — không thay được hub.db"
        log.write(out["db_error"] + "\n")
        return out
    try:
        out["db_replaced_kept_at"] = restore_db(home, Path(a.db_backup), log)
        out["db_restored"] = True
    except Exception as e:  # best-effort: lỗi ghi vào out.db_error + update.log
        out["db_error"] = f"không khôi phục được hub.db: {e}"
        log.write(out["db_error"] + "\n")
    finally:
        if lk is not True:
            lk.close()
    return out


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
    with open_log(a.log) as log:
        log.write(f"\n=== cập nhật cfharness {st.get('version_before') or '?'} → {st.get('tag') or '?'} · {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        phase("waiting_daemon")
        if a.wait_pid and not _wait_exit(int(a.wait_pid), WAIT_DAEMON_S):
            log.write(f"daemon (pid {a.wait_pid}) chưa thoát sau {int(WAIT_DAEMON_S)} s — gửi SIGTERM\n")
            try:
                os.kill(int(a.wait_pid), signal.SIGTERM)
            except OSError:
                pass
            _wait_exit(int(a.wait_pid), 15.0)
        phase("stopping_holders")
        restarts: list[list[str]] = []
        stop_venv_holders(a.venv, log, restarts)
        phase("installing")
        rc, out = install_with_retry(a, json.loads(a.install), log, env, restarts)
        error = None if rc == 0 else f"cài đặt lỗi (mã {rc}): {out.strip().splitlines()[-1] if out.strip() else 'không có output'}"
        phase("restarting", **({"error": error} if error else {}))
        rrc, rout = _run(json.loads(a.restart), log, RESTART_TIMEOUT_S, env)
        phase("health")
        healthy = wait_healthy(a.socket, a.health_timeout, rrc, log)
        if rrc != 0 or not healthy:
            msg = f"khởi động lại daemon lỗi (mã {rrc})" if rrc != 0 else "daemon không lên sau cập nhật"
            error = f"{error}; {msg}" if error else msg
        if not healthy and a.rollback_install:
            phase("rolling_back", error=error)
            rb = rollback_steps(a, log, env, json.loads(a.rollback_install), phase, restarts)
            st["rollback"] = rb
            back = f"đã quay về {st.get('version_before') or 'bản trước'}" + (" + hub.db trước cập nhật" if rb.get("db_restored") else "")
            error = f"{error} → {back if rb['ok'] else 'quay về LỖI: ' + str(rb.get('error'))}"
        elif not healthy:
            error = f"{error} — chạy tay: cfharness daemon start"
        phase("verifying")
        vrc, vout = _run(json.loads(a.verify), log, 30.0, env) if a.verify else (1, "")
        version_after = (vout.strip().split() or [""])[-1] if vrc == 0 else None
        if vrc == 0:  # cfharness chạy được (bản mới hoặc bản cũ đã quay về) → bật lại web UI đã dừng
            for w in restarts:
                start_detached(w, env, log)
        st.update({"state": "failed" if error else "done", "phase": "finished", "finished_at": _now(), "version_after": version_after,
                   "error": error, "updated_at": _now(), "rolled_back": bool((st.get("rollback") or {}).get("ok"))})
        write_state(state_path, st)
        log.write(f"=== {'LỖI: ' + error if error else 'xong'} · phiên bản sau cập nhật: {version_after or '?'} ===\n")
    return 0 if not error else 1


def rollback_steps(a: argparse.Namespace, log, env: dict[str, str], install: list[str], phase,
                   restarts: list[list[str]] | None = None) -> dict[str, Any]:
    """Quay về: dừng daemon còn sót + tiến trình giữ venv → khôi phục hub.db nếu migration đã tăng → cài tag cũ → khởi động lại → kiểm."""
    restarts = restarts if restarts is not None else []
    if a.home:
        stop_daemon(Path(a.home), log)
    stop_venv_holders(getattr(a, "venv", None), log, restarts)
    rb: dict[str, Any] = restore_db_if_migrated(a, log)
    phase("rolling_back")
    irc, iout = install_with_retry(a, install, log, env, restarts)
    phase("rollback_restarting")
    rrc, _ = _run(json.loads(a.restart), log, RESTART_TIMEOUT_S, env)
    healthy = wait_healthy(a.socket, a.health_timeout, rrc, log)
    err = None
    if irc != 0:
        err = f"cài lại bản cũ lỗi (mã {irc}): {iout.strip().splitlines()[-1] if iout.strip() else 'không có output'}"
    elif not healthy:
        err = "daemon vẫn không lên sau khi quay về — chạy tay: cfharness daemon start, xem logs/daemon.out"
    if rb.get("db_error"):
        err = f"{err}; {rb['db_error']}" if err else rb["db_error"]
    rb.update({"ok": err is None, "error": err, "install_rc": irc, "healthy": healthy, "at": _now()})
    return rb


def rollback(a: argparse.Namespace) -> int:
    """`cfharness update --rollback`: quay về tag trước (`--install`) + DB trước (nếu schema đã tăng)."""
    state_path = Path(a.state)
    st: dict[str, Any] = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    st.update({"state": "running", "pid": os.getpid()})

    def phase(name: str, **extra: Any) -> None:
        st.update({"phase": name, "updated_at": _now(), **extra})
        write_state(state_path, st)

    env = dict(os.environ)
    if a.home:
        env["CFHARNESS_HOME"] = a.home
    Path(a.log).parent.mkdir(parents=True, exist_ok=True)
    with open_log(a.log) as log:
        log.write(f"\n=== quay về cfharness {st.get('version_before') or '?'} → {st.get('tag') or '?'} · {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        phase("waiting_daemon")
        if a.wait_pid and not _wait_exit(int(a.wait_pid), WAIT_DAEMON_S):
            try:
                os.kill(int(a.wait_pid), signal.SIGTERM)
            except OSError:
                pass
            _wait_exit(int(a.wait_pid), 15.0)
        phase("rolling_back")
        restarts: list[list[str]] = []
        rb = rollback_steps(a, log, env, json.loads(a.install), phase, restarts)
        phase("verifying")
        vrc, vout = _run(json.loads(a.verify), log, 30.0, env) if a.verify else (1, "")
        version_after = (vout.strip().split() or [""])[-1] if vrc == 0 else None
        if vrc == 0:
            for w in restarts:
                start_detached(w, env, log)
        st.update({"state": "done" if rb["ok"] else "failed", "phase": "finished", "finished_at": _now(), "version_after": version_after,
                   "error": rb.get("error"), "rollback": rb, "rolled_back": rb["ok"], "updated_at": _now()})
        write_state(state_path, st)
        log.write(f"=== {'LỖI: ' + str(rb.get('error')) if not rb['ok'] else 'đã quay về'} · phiên bản: {version_after or '?'} ===\n")
    return 0 if rb["ok"] else 1


def _common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--state", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--home", default=None)
    ap.add_argument("--wait-pid", type=int, default=0)
    ap.add_argument("--install", required=True, help="argv JSON lệnh cài")
    ap.add_argument("--restart", required=True, help="argv JSON lệnh khởi động lại daemon")
    ap.add_argument("--verify", default=None, help="argv JSON lệnh in phiên bản")
    ap.add_argument("--socket", default=None, help="Unix socket của daemon để dò hub.ping")
    ap.add_argument("--health-timeout", type=float, default=HEALTH_TIMEOUT_S)
    ap.add_argument("--db-backup", default=None, help="archive bản sao lưu pre-update (backup.py)")
    ap.add_argument("--db-version", type=int, default=None, help="user_version của hub.db lúc chụp")
    ap.add_argument("--venv", default=None, help="venv của tool: tiến trình khác chạy từ đây bị dừng trước khi cài (Windows khoá file)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cfharness-update-helper")
    sub = p.add_subparsers(dest="cmd", required=True)
    ap = sub.add_parser("apply")
    _common(ap)
    ap.add_argument("--rollback-install", default=None, help="argv JSON cài lại tag cũ khi daemon không lên")
    rp = sub.add_parser("rollback")
    _common(rp)
    a = p.parse_args(argv)
    try:
        return rollback(a) if a.cmd == "rollback" else apply(a)
    except Exception as e:  # pragma: no cover — mọi lỗi phải về trạng thái failed, không để web chờ mãi
        try:
            st = json.loads(Path(a.state).read_text(encoding="utf-8"))
        except Exception:  # best-effort: state.json hỏng → ghi state mới
            st = {}
        st.update({"state": "failed", "phase": "finished", "error": f"lỗi tiến trình cập nhật: {e}", "finished_at": _now(), "updated_at": _now()})
        write_state(Path(a.state), st)
        return 1


if __name__ == "__main__":
    sys.exit(main())
