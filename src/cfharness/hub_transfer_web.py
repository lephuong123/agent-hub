"""Xuất / nhập hub từ giao diện WEB (PR6 của `docs/dev-notes/plan-hub-clone.md` §7b) — phần chạy trong DAEMON + tiến trình `hub import-job`.

Web (`web/transfer_pages.py`) là client thuần: mọi quyết định đi qua RPC dưới đây (đều ∈ `auth.HUMAN_ONLY`, không ở `MANAGER_ALLOWED`):
- `hub.transfer_status {}` (đọc): hub trống?, đóng băng, `imported_from`, tác vụ xuất, lần nhập gần đây, `max_upload_mb`, số run đang chạy.
- `hub.export_start {profile, for, stop, secrets, passphrase?, with_totp, with_mcp_tokens}` (GHI, tự audit — khoá `passphrase` bị gỡ khỏi
  params TRƯỚC khi chạy): chạy NỀN trong daemon đúng đường của CLI (`export_in_daemon` → gói phần không mật), rồi gắn `secrets.enc`
  (`seal_package`) NGAY trong daemon. Passphrase chỉ sống trong RAM của tác vụ (hộp một phần tử, xoá sau khi gắn) — không vào audit,
  log, DB, file trạng thái. `for=move`: đóng băng như CLI (gắn hỏng → gói bị xoá → tự gỡ đóng băng).
- `hub.export_status {}` (đọc): phase/progress/error/kết quả gọn (KHÔNG passphrase, không đường dẫn ngoài `exports/`).
- `hub.import_plan {upload, passphrase?, maps, mode, skip_secrets, clone_repos}` (đọc): gói đã tải lên `imports/<upload>.cfhx` → đúng
  `hub_transfer.import_plan` của CLI `--dry-run` (passphrase chỉ để thử giải mã).
- `hub.import_start {upload, confirm_id, passphrase?, maps, mode, replace, skip_secrets, clone_repos, stop}` (GHI, tự audit, passphrase bị gỡ):
  kiểm nhanh (id gõ lại khớp manifest, passphrase đúng, hub trống hoặc `replace`, run đang chạy → từ chối trừ `stop`) → ghi
  `imports/<upload>/job.json` (KHÔNG passphrase) + `imports/<upload>/pass` (0600, O_EXCL; job xoá NGAY khi đọc) → spawn tách rời
  `python -m cfharness hub import-job <upload>` (cùng interpreter, close_fds, không token người — token người nằm trong file socket `.auth`,
  không trong env) → daemon tự dừng. Dưới launchd: thoát `SUPERVISED_RESTART_CODE`, daemon mới CHỜ (`import_busy` trong
  `daemon.run_foreground`) tới khi job xong.
- `hub.import_status {upload}` (đọc): `imports/<upload>/state.json` theo danh sách trắng.

Tiến trình `hub import-job` (`run_import_job`): đọc job + xoá `pass` → chờ daemon nhả `daemon.lock` → gọi ĐÚNG `hub_transfer.import_package`
của CLI (lỗi → tự hoàn tác như PR2) → ghi state → khởi động lại daemon (kể cả khi lỗi) → doctor do web gọi `hub.doctor` khi daemon lên.
Daemon khởi động: `cleanup()` xoá `pass` sót (job chết trước khi đọc), đánh dấu job chết là `failed`, xoá file tải lên quá hạn.

Hub "trống" (`hub_fresh`): chưa có agent (kể cả đã xoá mềm), team, task, run, lịch, secret — daemon đang chạy luôn có `hub.db` nên
`hub_transfer.home_blank` không dùng được cho web; hub trống → nhập không cần tick "thay thế" (vẫn chụp `pre-restore` vì có hub.db).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import hub_transfer as T
from .errors import Conflict, HubError, NotFound
from .paths import HubPaths

if TYPE_CHECKING:
    from .daemon import Daemon
    from .hub import Hub

log = logging.getLogger("cfharness.hub_transfer_web")

UPLOAD_RE = re.compile(r"^up-[0-9a-f]{16}$")
JOB_FILE = "job.json"
PASS_FILE = "pass"
STATE_FILE = "state.json"
DEFAULT_MAX_UPLOAD_MB = 4096
IMPORT_BUSY_PHASES = ("starting", "waiting_daemon", "importing")
BUSY_MAX_S = 2 * 3600  # job treo quá 2 giờ → không coi là bận nữa (daemon không chờ mãi)
UPLOAD_TTL_S = 24 * 3600  # file tải lên chưa nhập quá hạn → xoá lúc daemon khởi động
PART_TTL_S = 3600
MUTATING_TRANSFER_WEB = ("hub.export_start", "hub.import_start")
HUMAN_ONLY_TRANSFER_WEB = ("hub.transfer_status", "hub.export_start", "hub.export_status", "hub.import_plan", "hub.import_start",
                           "hub.import_status")
# state.json / hub.export_status: CHỈ các khoá này (không passphrase, không chữ tự do ngoài lỗi do mã hub soạn)
STATE_KEYS = ("state", "phase", "progress", "upload", "package_id", "mode", "replace", "clone_repos", "started_at", "updated_at",
              "finished_at", "job_pid", "error", "report", "restarted", "supervised")
REPORT_KEYS = ("imported", "mode", "files_written", "files_removed", "runs_files", "pre_restore", "missing_secrets", "runs_interrupted",
               "tasks_blocked", "schedules_paused", "approvals_cancelled", "questions_cancelled", "agents_paused", "agents_unpaused",
               "repos_cloned", "secrets", "claude_sessions")
EXPORT_KEYS = ("state", "phase", "progress", "error", "started_at", "finished_at", "profile", "for", "stop", "secrets", "result", "written")
FRESH_TABLES = ("agents", "teams", "tasks", "runs", "schedules")


# ============================================================================ đường dẫn / trạng thái
def valid_upload(uid: Any) -> str:
    s = str(uid or "")
    if not UPLOAD_RE.match(s):
        raise HubError("mã gói tải lên không hợp lệ", code=-32602)
    return s


def new_upload_id() -> str:
    return "up-" + secrets.token_hex(8)


def upload_path(paths: HubPaths, uid: str) -> Path:
    return T.imports_dir(paths) / f"{valid_upload(uid)}{T.EXT}"


def job_dir(paths: HubPaths, uid: str) -> Path:
    return T.imports_dir(paths) / valid_upload(uid)


def _write_private(p: Path, data: bytes, *, exclusive: bool = False) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(p, flags, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(p, 0o600)
    except OSError:  # pragma: no cover — Windows
        pass


def _clean_state(st: Any) -> dict[str, Any]:
    if not isinstance(st, dict):
        return {}
    return {k: st[k] for k in STATE_KEYS if k in st}


def read_state(paths: HubPaths, uid: str) -> dict[str, Any] | None:
    try:
        raw = (job_dir(paths, uid) / STATE_FILE).read_text(encoding="utf-8")
        return _clean_state(json.loads(raw))
    except (OSError, ValueError, HubError):
        return None


def write_state(paths: HubPaths, uid: str, **fields: Any) -> dict[str, Any]:
    """Gộp `fields` vào state.json (ghi nguyên tử, 0600). Chỉ khoá trong `STATE_KEYS` được ghi."""
    d = T._private_dir(job_dir(paths, uid))
    cur = read_state(paths, uid) or {}
    cur.update({k: v for k, v in fields.items() if k in STATE_KEYS})
    cur["updated_at"] = time.time()
    tmp = d / f".{STATE_FILE}.{os.getpid()}.tmp"
    _write_private(tmp, json.dumps(cur, ensure_ascii=False, indent=1, default=str).encode("utf-8"))
    os.replace(tmp, d / STATE_FILE)
    return cur


def _pid_alive(pid: Any) -> bool:
    from .oscompat import pid_alive

    try:
        return pid_alive(int(pid))
    except (TypeError, ValueError):
        return False


def _busy(st: dict[str, Any] | None, now: float | None = None) -> bool:
    if not st or st.get("state") not in ("starting", "running") or st.get("phase") not in IMPORT_BUSY_PHASES:
        return False
    now = time.time() if now is None else now
    upd = float(st.get("updated_at") or st.get("started_at") or 0)
    if now - upd > BUSY_MAX_S:
        return False
    if st.get("job_pid"):
        return _pid_alive(st["job_pid"])
    return now - upd < 120  # daemon vừa ghi "starting", chưa kịp ghi pid của job


def import_busy(paths: HubPaths) -> bool:
    """Có lần nhập từ web đang chạy (job còn sống) → daemon mới (launchd chạy lại / người `daemon start`) phải CHỜ, không lấy `daemon.lock`
    chen ngang lúc job ghi đè hub.db. Chỉ đọc file — gọi được trước khi dựng Hub."""
    d = paths.home / T.IMPORTS_DIRNAME
    try:
        dirs = [x for x in d.iterdir() if x.is_dir() and UPLOAD_RE.match(x.name)]
    except OSError:
        return False
    return any(_busy(read_state(paths, x.name)) for x in dirs)


def hold_while_importing(paths: HubPaths, *, poll_s: float = 1.0, max_s: float = BUSY_MAX_S) -> bool:
    """`daemon.run_foreground`: chờ job nhập xong (job tự khởi động daemon / launchd chạy lại). Trả True nếu đã phải chờ."""
    if not import_busy(paths):
        return False
    print("daemon: đang nhập hub từ web (hub import-job) — chờ nhập xong rồi mới chạy", file=sys.stderr)
    deadline = time.monotonic() + max_s
    while import_busy(paths) and time.monotonic() < deadline:
        time.sleep(poll_s)
    return True


def cleanup(paths: HubPaths, *, now: float | None = None) -> dict[str, int]:
    """Lúc daemon khởi động: `pass` sót (job chết trước khi đọc) → xoá; job bận mà tiến trình đã chết → `failed`; file tải lên / `.part`
    quá hạn → xoá. Không bao giờ đụng job đang sống."""
    now = time.time() if now is None else now
    out = {"pass": 0, "dead": 0, "uploads": 0}
    d = paths.home / T.IMPORTS_DIRNAME
    try:
        items = list(d.iterdir())
    except OSError:
        return out
    for x in items:
        try:
            if x.is_dir() and UPLOAD_RE.match(x.name):
                st = read_state(paths, x.name)
                if _busy(st, now):
                    continue
                if (x / PASS_FILE).exists():
                    (x / PASS_FILE).unlink(missing_ok=True)
                    out["pass"] += 1
                if st and st.get("state") in ("starting", "running"):
                    write_state(paths, x.name, state="failed", phase="finished", finished_at=now,
                                error=st.get("error") or "tiến trình nhập dừng bất thường — hub giữ nguyên hoặc đã tự hoàn tác; xem logs/import-job.out")
                    out["dead"] += 1
            elif x.is_file() and x.name.endswith(".part") and now - x.stat().st_mtime > PART_TTL_S:
                x.unlink(missing_ok=True)
                out["uploads"] += 1
            elif x.is_file() and x.name.endswith(T.EXT) and UPLOAD_RE.match(x.name[: -len(T.EXT)]):
                st = read_state(paths, x.name[: -len(T.EXT)])
                if not _busy(st, now) and now - x.stat().st_mtime > UPLOAD_TTL_S:
                    x.unlink(missing_ok=True)
                    out["uploads"] += 1
        except OSError:  # pragma: no cover
            continue
    return out


def hub_fresh(hub: "Hub") -> bool:
    """Hub "trống": chưa có agent (kể cả xoá mềm), team, task, run, lịch, secret — đích tự nhiên của "nhập hub từ máy khác"."""
    conn = hub.store.conn
    for t in FRESH_TABLES:
        try:
            if conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone():  # noqa: S608 — tên bảng cố định
                return False
        except Exception:  # bảng chưa có (schema cũ) → bỏ qua
            continue
    return not T._secret_names(hub.paths.home)


def _maps(v: Any) -> list[tuple[str, str]]:
    """`maps`: list "CŨ=MỚI" hoặc [[cũ, mới]] (web gửi chuỗi nhiều dòng đã tách)."""
    if not v:
        return []
    items: list[str] = []
    for x in v if isinstance(v, list) else [v]:
        if isinstance(x, (list, tuple)) and len(x) == 2:
            items.append(f"{x[0]}={x[1]}")
        elif isinstance(x, str):
            items += [ln.strip() for ln in x.splitlines() if ln.strip()]
    try:
        return T.parse_map_paths(items)
    except ValueError as e:
        raise HubError(str(e), code=-32602) from e


def _mode(v: Any) -> str:
    m = str(v or T.DEFAULT_MODE)
    if m not in T.MODES:
        raise HubError(f"chế độ '{m}' không hợp lệ (clone|move)", code=-32602)
    return m


def _brief_report(rep: dict[str, Any]) -> dict[str, Any]:
    out = {k: rep.get(k) for k in REPORT_KEYS if k in rep}
    cs = out.get("claude_sessions")
    if isinstance(cs, dict):
        out["claude_sessions"] = {"written": len(cs.get("written") or []), "conflicts": len(cs.get("conflicts") or []), "target": cs.get("target")}
    if isinstance(out.get("agents_paused"), dict):
        out["agents_paused"] = sorted(out["agents_paused"])
    return out


# ============================================================================ xuất (trong daemon)
class ExportJobs:
    """Tác vụ xuất NỀN của web (một tác vụ một lúc). Trạng thái chỉ trong RAM daemon — gói đã ghi vẫn nằm ở `exports/`."""

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.job: dict[str, Any] | None = None
        self.task: asyncio.Task[Any] | None = None

    def status(self) -> dict[str, Any]:
        j = dict(self.job or {})
        if j.get("state") == "running" and j.get("phase") == "packing":
            j["written"] = self._part_bytes()
        return {k: j[k] for k in EXPORT_KEYS if k in j} if j else {"state": None}

    def _part_bytes(self) -> int:
        try:
            parts = [p for p in T.exports_dir(self.hub.paths).glob(f".*{T.EXT}.part")]
            return max((p.stat().st_size for p in parts), default=0)
        except OSError:
            return 0

    def busy(self) -> bool:
        return bool(self.job and self.job.get("state") == "running")

    def start(self, *, profile: str, purpose: str, stop: bool, passphrase: str | None, with_totp: bool, with_mcp_tokens: bool,
              channel: str) -> dict[str, Any]:
        from . import transfer_secrets as S

        hub = self.hub
        if profile not in T.PROFILES:
            raise HubError(f"profile '{profile}' không hợp lệ (core|full)", code=-32602)
        if purpose not in T.MODES:
            raise HubError(f"for '{purpose}' không hợp lệ (clone|move)", code=-32602)
        if self.busy() or hub.runs.quiesced:
            raise Conflict("đang xuất một gói khác — chờ xong rồi thử lại")
        if purpose == "move" and hub.runs.frozen:
            raise Conflict("hub đã đóng băng (chuyển nhà) — mở lại hub trước nếu muốn xuất lại để chuyển nhà", data={"migrated_to": hub.runs.frozen})
        active = sorted(hub.runs.active)
        if active and not stop:
            raise Conflict(f"có {len(active)} run đang chạy — chờ xong, hoặc tick \"dừng run đang chạy\"", data={"running": active})
        has = bool(S.collect(hub.paths.home, with_totp=with_totp, with_mcp_tokens=with_mcp_tokens))
        box: list[str] = []
        if passphrase is not None and has:
            box.append(S.check_passphrase(passphrase, for_export=True))
        self.job = {"state": "running", "phase": "quiesce", "progress": 2, "started_at": time.time(), "profile": profile, "for": purpose,
                    "stop": bool(stop), "secrets": bool(box)}
        self.task = asyncio.get_running_loop().create_task(self._run(box, profile, purpose, stop, with_totp, with_mcp_tokens, channel))
        return {"started": True, "secrets": bool(box), "has_secrets": has, "profile": profile, "for": purpose}

    async def _run(self, box: list[str], profile: str, purpose: str, stop: bool, with_totp: bool, with_mcp_tokens: bool, channel: str) -> None:
        from .hub_freeze import freeze, unfreeze

        hub = self.hub
        j = self.job
        assert j is not None
        move = purpose == "move"
        try:
            j.update(phase="packing", progress=10)
            res = await T.export_in_daemon(hub, profile=profile, stop=stop, by="user", for_move=move, channel=channel)
            sealed: dict[str, Any] = {}
            if box:
                j.update(phase="sealing", progress=85)
                try:
                    sealed = await asyncio.to_thread(T.seal_package, Path(res["path"]), hub.paths.home, box[0], with_totp=with_totp,
                                                     with_mcp_tokens=with_mcp_tokens)
                except BaseException:
                    if move:  # gói đã bị xoá → không có gì mang sang máy mới: gỡ đóng băng vừa đặt (như CLI)
                        try:
                            unfreeze(hub, export_id=res["id"])
                        except Exception:  # pragma: no cover
                            log.exception("không gỡ được đóng băng sau khi gắn secret lỗi")
                    raise
                finally:
                    box.clear()  # passphrase rời khỏi RAM của tác vụ (tham chiếu cuối)
                if sealed.get("sealed"):
                    res.update(size=sealed["size"], sha256=sealed["sha256"], missing_secrets=sealed["missing_secrets"])
                    if move:
                        res["migrated_to"] = freeze(hub, {"export_id": res["id"], "sha256": sealed["sha256"], "sealed": True})
            j["result"] = {"id": res["id"], "file": Path(res["path"]).name, "size": res.get("size"), "sha256": res.get("sha256"),
                           "profile": res.get("profile"), "files": res.get("files"), "sealed": bool(sealed.get("sealed")),
                           "secret_names": len(sealed.get("names") or []), "totp": bool(sealed.get("totp")),
                           "mcp_tokens": bool(sealed.get("mcp_tokens")), "missing_secrets": list(res.get("missing_secrets") or []),
                           "stopped_runs": len(res.get("stopped_runs") or []), "migrated_to": bool(res.get("migrated_to")),
                           "repos": len(res.get("repos") or []),
                           "repos_dirty": [{"path": r.get("path"), "uncommitted": r.get("uncommitted")} for r in res.get("repos") or [] if r.get("dirty")]}
            j.update(state="done", phase="done", progress=100, finished_at=time.time())
        except HubError as e:
            j.update(state="failed", phase="done", error=e.message, finished_at=time.time())
        except Exception as e:  # noqa: BLE001
            log.exception("xuất hub từ web lỗi")
            j.update(state="failed", phase="done", error=f"lỗi nội bộ: {type(e).__name__}", finished_at=time.time())
        finally:
            box.clear()


# ============================================================================ nhập: tiến trình `hub import-job`
def _spawn_job(argv: list[str], env: dict[str, str], log_path: Path) -> int:
    from .oscompat import detached_kwargs

    log_path.parent.mkdir(parents=True, exist_ok=True)
    out = open(log_path, "ab")  # noqa: SIM115 — giữ mở cho tiến trình con
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env, close_fds=True, **detached_kwargs())
    finally:
        out.close()
    return p.pid


def job_argv(uid: str, *, supervised: bool = False) -> list[str]:
    """argv của job — KHÔNG BAO GIỜ chứa passphrase (nằm trong file `pass` 0600)."""
    return [sys.executable, "-m", "cfharness", "hub", "import-job", valid_upload(uid), *(["--supervised"] if supervised else [])]


def _default_restart(paths: HubPaths, supervised: bool) -> int | None:
    from .daemon import spawn_background, wait_supervised

    if supervised:  # launchd chạy lại daemon (đang chờ ở hold_while_importing) — chỉ chờ nó trả lời
        pid = wait_supervised(paths, 120.0)
        if pid:
            return pid
    return spawn_background(paths, wait_s=30.0)


def run_import_job(paths: HubPaths, uid: str, *, supervised: bool = False, wait_s: float = 120.0,
                   restart: Callable[[HubPaths, bool], int | None] | None = None, claude_home: Path | None = None) -> dict[str, Any]:
    """Thân của `cfharness hub import-job <upload>`. Luôn cố khởi động lại daemon ở cuối (kể cả lỗi). Trả state cuối."""
    from .daemon import instance_lock_held

    uid = valid_upload(uid)
    d = job_dir(paths, uid)
    pw: str | None = None
    pf = d / PASS_FILE
    try:  # đọc passphrase rồi XOÁ file ngay — trước mọi việc khác
        fd = os.open(pf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as f:
            pw = f.read(8192).decode("utf-8")
    except FileNotFoundError:
        pw = None
    finally:
        pf.unlink(missing_ok=True)
    try:
        job = json.loads((d / JOB_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        st = write_state(paths, uid, state="failed", phase="finished", finished_at=time.time(), error=f"không đọc được job.json: {e}")
        _restart(paths, supervised, restart, uid)
        return st
    write_state(paths, uid, state="running", phase="waiting_daemon", progress=5, job_pid=os.getpid(), supervised=supervised)
    err: str | None = None
    rep: dict[str, Any] | None = None
    try:
        deadline = time.monotonic() + wait_s
        while instance_lock_held(paths):
            if time.monotonic() > deadline:
                raise HubError(f"daemon không dừng sau {wait_s:.0f}s — chưa ghi gì, thử lại")
            time.sleep(0.2)
        write_state(paths, uid, phase="importing", progress=20)
        cfg = None
        try:
            from .config import HubConfig

            cfg = HubConfig(paths.config).get("backup") if paths.config.exists() else None
        except Exception:  # config.yaml hỏng không được chặn nhập
            pass
        replace = not T.home_blank(paths)
        rep = T.import_package(paths, upload_path(paths, uid), maps=_maps(job.get("maps")), mode=_mode(job.get("mode")), replace=replace,
                               cfg=cfg, passphrase=None if job.get("skip_secrets") else pw, skip_secrets=bool(job.get("skip_secrets")),
                               clone_repos=bool(job.get("clone_repos")), claude_home=claude_home)
    except HubError as e:
        err = e.message
    except Exception as e:  # noqa: BLE001
        log.exception("nhập hub (import-job) lỗi")
        err = f"lỗi nội bộ: {type(e).__name__}: {str(e)[:300]}"
    finally:
        pw = None
    if rep is not None:
        write_state(paths, uid, phase="restarting", progress=90, report=_brief_report(rep))
        try:
            upload_path(paths, uid).unlink(missing_ok=True)  # gói chứa toàn bộ dữ liệu hub — đã nhập xong thì không giữ bản thứ hai
        except OSError:  # pragma: no cover
            pass
    else:
        write_state(paths, uid, phase="restarting", progress=90, error=err)
    pid = _restart(paths, supervised, restart, uid)
    return write_state(paths, uid, state="done" if rep is not None else "failed", phase="finished", progress=100, finished_at=time.time(),
                       restarted=pid)


def _restart(paths: HubPaths, supervised: bool, restart: Callable[[HubPaths, bool], int | None] | None, uid: str) -> int | None:
    try:
        return (restart or _default_restart)(paths, supervised)
    except Exception as e:  # noqa: BLE001
        log.exception("không khởi động lại được daemon sau khi nhập")
        cur = read_state(paths, uid) or {}
        write_state(paths, uid, error=((cur.get("error") + " · ") if cur.get("error") else "")
                    + f"không tự khởi động lại được daemon ({type(e).__name__}) — chạy `cfharness daemon start`")
        return None


# ============================================================================ RPC
def register_rpc(daemon: "Daemon") -> None:
    from . import transfer_secrets as S
    from .hub_freeze import frozen
    from .mcp_server.rpc import wrap_with_audit

    hub = daemon.hub
    jobs = ExportJobs(hub)
    hub.transfer_web = jobs  # type: ignore[attr-defined]
    try:
        n = cleanup(daemon.paths)
        if any(n.values()):
            log.info("dọn imports/: %s", n)
    except Exception:  # pragma: no cover
        log.exception("dọn imports/ lỗi")

    def _max_mb() -> int:
        try:
            return int(hub.config.get("transfer.max_upload_mb") or DEFAULT_MAX_UPLOAD_MB)
        except (TypeError, ValueError):
            return DEFAULT_MAX_UPLOAD_MB

    def _recent_imports(limit: int = 5) -> list[dict[str, Any]]:
        d = T.imports_dir(daemon.paths)
        try:
            dirs = sorted((x for x in d.iterdir() if x.is_dir() and UPLOAD_RE.match(x.name)), key=lambda x: x.stat().st_mtime, reverse=True)
        except OSError:
            return []
        return [{"upload": x.name, **(read_state(daemon.paths, x.name) or {})} for x in dirs[:limit]]

    def _status(p: dict[str, Any]) -> dict[str, Any]:
        st = hub.status()
        return {"fresh": hub_fresh(hub), "migrated_to": frozen(hub), "imported_from": hub.store.state_get("imported_from"),
                "export": jobs.status(), "imports": _recent_imports(), "max_upload_mb": _max_mb(),
                "running": int(st.get("running") or 0), "queued": int(st.get("queued") or 0),
                "has_secrets": bool(T._secret_names(daemon.paths.home)), "home": str(daemon.paths.home)}

    async def _export_start(p: dict[str, Any]) -> dict[str, Any]:
        pw = p.pop("passphrase", None)
        try:
            secrets_on = bool(p.get("secrets", True))
            if secrets_on and pw is None and S.collect(daemon.paths.home, with_totp=bool(p.get("with_totp")), with_mcp_tokens=bool(p.get("with_mcp_tokens"))):
                raise HubError("gói mặc định mang secret đã mã hoá — cần passphrase (hoặc bỏ chọn \"mang secret\")", code=-32602)
            return jobs.start(profile=str(p.get("profile") or T.DEFAULT_PROFILE), purpose=str(p.get("for") or "clone"), stop=bool(p.get("stop")),
                              passphrase=pw if secrets_on else None, with_totp=bool(p.get("with_totp")),
                              with_mcp_tokens=bool(p.get("with_mcp_tokens")), channel="web")
        finally:
            pw = None

    def _export_status(p: dict[str, Any]) -> dict[str, Any]:
        return jobs.status()

    def _pkg(p: dict[str, Any]) -> tuple[str, Path]:
        uid = valid_upload(p.get("upload"))
        pkg = upload_path(daemon.paths, uid)
        if not pkg.is_file() or pkg.is_symlink():
            raise NotFound(f"không có gói tải lên {uid} (đã nhập xong / bị xoá / quá hạn) — tải lên lại")
        return uid, pkg

    async def _import_plan(p: dict[str, Any]) -> dict[str, Any]:
        pw = p.pop("passphrase", None) or None
        try:
            uid, pkg = _pkg(p)
            plan = await asyncio.to_thread(T.import_plan, daemon.paths, pkg, maps=_maps(p.get("maps")), mode=_mode(p.get("mode")),
                                           passphrase=pw, skip_secrets=bool(p.get("skip_secrets")))
        finally:
            pw = None
        st = hub.status()
        plan.update(upload=uid, fresh=hub_fresh(hub), running=int(st.get("running") or 0) + int(st.get("queued") or 0),
                    clone_repos=bool(p.get("clone_repos")), skip_secrets=bool(p.get("skip_secrets")), maps_extra=[list(x) for x in _maps(p.get("maps"))])
        plan.pop("package", None)
        return plan

    async def _import_start(p: dict[str, Any]) -> dict[str, Any]:
        pw = p.pop("passphrase", None) or None
        try:
            return await _do_import_start(p, pw)
        finally:
            pw = None

    async def _do_import_start(p: dict[str, Any], pw: str | None) -> dict[str, Any]:
        from .daemon import SUPERVISED_RESTART_CODE, supervisor

        uid, pkg = _pkg(p)
        mode = _mode(p.get("mode"))
        maps = _maps(p.get("maps"))
        skip = bool(p.get("skip_secrets"))
        if jobs.busy() or hub.runs.quiesced:
            raise Conflict("đang xuất gói — chờ xong rồi nhập")
        if import_busy(daemon.paths):
            raise Conflict("đang có một lần nhập chạy")
        m = await asyncio.to_thread(T.read_manifest, pkg)
        errs, _w, _s = T.check_manifest(m)
        if errs:
            raise HubError("gói không hợp lệ: " + "; ".join(errs[:3]))
        if str(p.get("confirm_id") or "").strip() != str(m.get("id") or ""):
            raise Conflict("id gói gõ lại không khớp — chưa làm gì", data={"expected_len": len(str(m.get("id") or ""))})
        if T.has_secrets(m) and not skip:
            if not pw:
                raise HubError("gói có secret mã hoá — nhập passphrase (hoặc chọn \"bỏ qua secret\")", code=-32602)
            await asyncio.to_thread(T.check_passphrase, pkg, pw)  # sai → HubError, daemon vẫn chạy, chưa ghi gì
        fresh = hub_fresh(hub)
        if not fresh and not p.get("replace"):
            raise Conflict("hub này đã có dữ liệu — tick \"thay thế hub hiện có\" (hiện trạng được chụp thành bản pre-restore trước khi ghi đè)")
        active = sorted(hub.runs.active)
        if active and not p.get("stop"):
            raise Conflict(f"có {len(active)} run đang chạy — chờ xong, hoặc tick \"dừng run đang chạy\"", data={"running": active})
        for rid in active:
            try:
                await hub.runs.stop(rid)
            except HubError:
                pass
        deadline = time.monotonic() + 30
        while hub.runs.active and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        d = T._private_dir(job_dir(daemon.paths, uid))
        (d / PASS_FILE).unlink(missing_ok=True)
        supervised = supervisor() == "launchd"
        job = {"upload": uid, "package_id": m.get("id"), "mode": mode, "maps": [list(x) for x in maps], "replace": bool(p.get("replace")) or fresh,
               "skip_secrets": skip, "clone_repos": bool(p.get("clone_repos")), "created_at": time.time()}
        _write_private(d / JOB_FILE, json.dumps(job, ensure_ascii=False, indent=1).encode("utf-8"))
        if pw and not skip:
            _write_private(d / PASS_FILE, pw.encode("utf-8"), exclusive=True)
        (d / STATE_FILE).unlink(missing_ok=True)
        write_state(daemon.paths, uid, state="starting", phase="starting", progress=1, upload=uid, package_id=m.get("id"), mode=mode,
                    replace=job["replace"], clone_repos=job["clone_repos"], started_at=time.time(), supervised=supervised)
        from .auth import ENV_RUN_ID
        from .names import LEGACY_ENV_RUN_ID

        # không mang danh tính run/launchd của daemon sang job; token người KHÔNG nằm trong env (file `<socket>.auth`)
        env = {k: v for k, v in os.environ.items() if k not in ("XPC_SERVICE_NAME", ENV_RUN_ID, LEGACY_ENV_RUN_ID)}
        env["CFHARNESS_HOME"] = str(daemon.paths.home)
        try:
            pid = _spawn(job_argv(uid, supervised=supervised), env, daemon.paths.logs_dir / "import-job.out")
        except Exception as e:
            (d / PASS_FILE).unlink(missing_ok=True)
            write_state(daemon.paths, uid, state="failed", phase="finished", finished_at=time.time(), error=f"không chạy được tiến trình nhập: {e}")
            raise HubError(f"không chạy được tiến trình nhập: {e}") from e
        write_state(daemon.paths, uid, job_pid=pid)
        if supervised:  # thoát ≠ 0 để launchd chạy lại daemon; daemon mới chờ job (hold_while_importing)
            daemon.exit_code = SUPERVISED_RESTART_CODE
        asyncio.get_running_loop().call_later(0.5, daemon.stopping.set)  # trả lời RPC trước, rồi dừng để job lấy daemon.lock
        log.warning("nhập hub từ web: gói %s (%s) — daemon dừng để tiến trình nhập (pid %s) ghi", m.get("id"), mode, pid)
        return {"started": True, "upload": uid, "package_id": m.get("id"), "mode": mode, "job_pid": pid}

    def _import_status(p: dict[str, Any]) -> dict[str, Any]:
        uid = valid_upload(p.get("upload"))
        st = read_state(daemon.paths, uid)
        return {"upload": uid, "state": st, "package": upload_path(daemon.paths, uid).is_file()}

    daemon.methods.update({
        "hub.transfer_status": _status,
        "hub.export_start": wrap_with_audit(hub, "hub.export_start", _export_start),
        "hub.export_status": _export_status,
        "hub.import_plan": _import_plan,
        "hub.import_start": wrap_with_audit(hub, "hub.import_start", _import_start),
        "hub.import_status": _import_status,
    })


_spawn = _spawn_job  # test thay để không chạy tiến trình thật


__all__ = ["register_rpc", "run_import_job", "import_busy", "hold_while_importing", "cleanup", "hub_fresh", "new_upload_id", "upload_path",
           "job_dir", "read_state", "write_state", "valid_upload", "job_argv", "MUTATING_TRANSFER_WEB", "HUMAN_ONLY_TRANSFER_WEB",
           "DEFAULT_MAX_UPLOAD_MB", "UPLOAD_RE"]
