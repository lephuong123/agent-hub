"""Cập nhật cfharness lên bản phát hành mới nhất — nút "Cập nhật" ở trang Cài đặt (web) + `cfharness update`.

Nguồn bản mới: tag `vX.Y.Z` mới nhất của repo phát hành (`REPO`, GitHub Releases; không có release → tag lớn nhất). Cập nhật =
đúng lệnh README: `uv tool install --force --python <X.Y> git+<repo>@<tag>`.

Chỉ hỗ trợ bản cài bằng `uv tool install git+…` (nhận ra qua `uv-receipt.toml` cạnh venv). Bản cài từ mã nguồn (`uv run`, `-e`)
→ `can_update=false` kèm lý do (cập nhật bằng git).

Luồng (`hub.updater.start`, RPC `update.start` ∈ HUMAN_ONLY, tự audit):
1. Kiểm: đúng kiểu cài, tag hợp lệ (`TAG_RE`) và MỚI hơn bản đang chạy (trừ `force`), không có cập nhật đang chạy, không có run
   `running|queued` (trừ `force` — run bị ngắt sẽ tự tiếp tục khi daemon khởi động lại, recovery.py).
2. Chép `update_helper.py` (chỉ stdlib) sang `HUB_HOME/update/helper.py`, chạy bằng python GỐC ngoài venv (`-I`), tách session.
3. Daemon tự dừng (exit 0 → launchd/systemd không chạy lại); helper chờ daemon thoát → cài → `python -m cfharness daemon start` → ghi
   `HUB_HOME/update/state.json` (`done|failed`), log `HUB_HOME/logs/update.log`.

Giao diện web (`cfharness web`) và `cfharness chat` đang mở vẫn chạy code cũ cho tới khi mở lại — trang Cài đặt báo điều này.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import __version__
from .errors import Conflict, HubError
from .oscompat import detached_kwargs
from .paths import HubPaths

if TYPE_CHECKING:
    from .daemon import Daemon

REPO = "lephuong123/agent-hub"
GIT_URL = f"https://github.com/{REPO}"
RELEASES_URL = f"{GIT_URL}/releases"
API = f"https://api.github.com/repos/{REPO}"
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
CHECK_TTL_S = 600.0
MUTATING_UPDATE = ("update.start",)

Fetcher = Callable[[float], dict[str, Any]]
Spawner = Callable[[list[str], dict[str, Any]], int]


def parse_version(v: str | None) -> tuple[int, int, int] | None:
    m = TAG_RE.match("v" + str(v or "").strip().lstrip("v"))
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _get_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": f"cfharness/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — URL cố định https api.github.com
        return json.loads(r.read().decode("utf-8"))


def fetch_latest(timeout: float = 8.0) -> dict[str, Any]:
    """Bản phát hành mới nhất: `{tag, name, published_at, url}`. Không có release → tag `vX.Y.Z` lớn nhất."""
    try:
        r = _get_json(f"{API}/releases/latest", timeout)
        tag = str(r.get("tag_name") or "")
        if parse_version(tag):
            return {"tag": tag, "name": r.get("name") or tag, "published_at": r.get("published_at"), "url": r.get("html_url") or f"{RELEASES_URL}/tag/{tag}"}
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    tags = [str(t.get("name") or "") for t in _get_json(f"{API}/tags?per_page=100", timeout) or []]
    tags = [t for t in tags if parse_version(t)]
    if not tags:
        raise HubError("repo phát hành chưa có tag vX.Y.Z nào")
    tag = max(tags, key=lambda t: parse_version(t) or (0, 0, 0))
    return {"tag": tag, "name": tag, "published_at": None, "url": f"{RELEASES_URL}/tag/{tag}"}


def install_info(prefix: str | Path | None = None) -> dict[str, Any]:
    """Kiểu cài của bản đang chạy: `uv_git` (cập nhật được) | `editable` | `uv_other` | `source`."""
    base = Path(prefix or sys.prefix)
    receipt = base / "uv-receipt.toml"
    if not receipt.is_file():
        return {"kind": "source", "prefix": str(base), "reason": "đang chạy từ mã nguồn (uv run / pip) — cập nhật bằng `git pull` + `uv sync`"}
    try:
        reqs = (tomllib.loads(receipt.read_text(encoding="utf-8")).get("tool") or {}).get("requirements") or []
    except (OSError, tomllib.TOMLDecodeError) as e:
        return {"kind": "uv_other", "prefix": str(base), "reason": f"không đọc được {receipt.name}: {e}"}
    req = next((r for r in reqs if isinstance(r, dict) and r.get("name") == "cfharness"), {})
    if req.get("editable"):
        return {"kind": "editable", "prefix": str(base), "source": str(req["editable"]),
                "reason": f"cài dạng editable từ mã nguồn {req['editable']} — cập nhật bằng `git pull` trong thư mục đó"}
    if req.get("git"):
        return {"kind": "uv_git", "prefix": str(base), "source": str(req["git"])}
    return {"kind": "uv_other", "prefix": str(base), "source": str(req.get("path") or req.get("url") or ""),
            "reason": "không cài từ git — cài lại theo README: uv tool install git+" + GIT_URL}


def find_uv(env_path: str | None = None) -> str | None:
    found = shutil.which("uv", path=env_path)
    if found:
        return found
    for c in (Path.home() / ".local" / "bin" / "uv", Path.home() / ".cargo" / "bin" / "uv", Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv")):
        if c.is_file():
            return str(c)
    return None


def _default_spawn(argv: list[str], opts: dict[str, Any]) -> int:
    out = open(opts["log"], "ab")  # noqa: SIM115 — giữ mở cho tiến trình con
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, cwd=opts.get("cwd"), env=opts.get("env"), close_fds=True,
                             **detached_kwargs())
    finally:
        out.close()
    return p.pid


class Updater:
    """`hub.updater` — kiểm tra bản mới + khởi động tiến trình phụ cập nhật. Test thay `fetch`/`spawn`/`info`/`uv`."""

    def __init__(self, paths: HubPaths, *, fetch: Fetcher | None = None, spawn: Spawner | None = None,
                 info: Callable[[], dict[str, Any]] | None = None, uv: str | None | Callable[[], str | None] = find_uv,
                 python: str | None = None, base_python: str | None = None) -> None:
        self.paths = paths
        self.fetch = fetch or fetch_latest
        self.spawn = spawn or _default_spawn
        self.info = info or install_info
        self._uv = uv
        self.python = python or sys.executable
        self.base_python = base_python or getattr(sys, "_base_executable", None) or sys.executable
        self._cache: tuple[float, dict[str, Any]] | None = None

    # ------------------------------------------------------------------ đường dẫn
    @property
    def dir(self) -> Path:
        return self.paths.home / "update"

    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    @property
    def log_path(self) -> Path:
        return self.paths.logs_dir / "update.log"

    def uv(self) -> str | None:
        return self._uv() if callable(self._uv) else self._uv

    # ------------------------------------------------------------------ đọc
    def state(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def in_progress(self) -> bool:
        st = self.state()
        if not st or st.get("state") not in ("starting", "running"):
            return False
        return time.time() - float(st.get("updated_at") or st.get("started_at") or 0) < 1200  # helper treo quá 20 phút → coi như chết

    def latest(self, refresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not refresh and self._cache and now - self._cache[0] < CHECK_TTL_S:
            return self._cache[1]
        try:
            res = {"ok": True, **self.fetch(8.0)}
        except Exception as e:  # mạng/GitHub lỗi → báo, không ném (trang Cài đặt vẫn hiện)
            res = {"ok": False, "error": f"không kiểm tra được bản mới: {e}"}
        res["checked_at"] = time.time()
        self._cache = (now, res)
        return res

    def check(self, refresh: bool = False) -> dict[str, Any]:
        info = self.info()
        latest = self.latest(refresh)
        cur, new = parse_version(__version__), parse_version(latest.get("tag"))
        available = bool(cur and new and new > cur)
        reason = None
        if info["kind"] != "uv_git":
            reason = info.get("reason")
        elif not self.uv():
            reason = "không tìm thấy lệnh `uv` (daemon không thấy trong PATH)"
        return {"current": __version__, "latest": latest, "update_available": available, "install": info,
                "can_update": reason is None, "reason": reason, "state": self.state(), "in_progress": self.in_progress(),
                "releases_url": RELEASES_URL, "log": str(self.log_path)}

    def log_tail(self, lines: int = 40) -> str:
        try:
            return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, min(lines, 400)):])
        except OSError:
            return ""

    # ------------------------------------------------------------------ cập nhật
    def plan(self, tag: str) -> dict[str, list[str]]:
        pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
        return {
            "install": [str(self.uv()), "tool", "install", "--force", "--python", pyver, f"git+{GIT_URL}@{tag}"],
            "restart": [self.python, "-m", "cfharness", "daemon", "start"],
            "verify": [self.python, "-m", "cfharness", "--version"],
        }

    def start(self, *, tag: str | None = None, force: bool = False, busy: int = 0, wait_pid: int | None = None,
              env: dict[str, str] | None = None, by: str = "user") -> dict[str, Any]:
        if self.in_progress():
            raise Conflict("đang có một lần cập nhật chạy — chờ xong rồi thử lại", data={"state": self.state()})
        chk = self.check(refresh=True)
        if not chk["can_update"]:
            raise HubError(f"không cập nhật tự động được: {chk['reason']}", data={"install": chk["install"]})
        if tag is None:
            if not chk["latest"].get("ok"):
                raise HubError(chk["latest"].get("error") or "không lấy được bản mới nhất")
            tag = str(chk["latest"]["tag"])
        if not TAG_RE.match(str(tag)):
            raise HubError(f"tag '{tag}' không hợp lệ — dạng vX.Y.Z", code=-32602)
        cur, new = parse_version(__version__), parse_version(tag)
        if not force and cur and new and new <= cur:
            raise Conflict(f"đang chạy {__version__}, không có bản mới hơn ({tag})", data={"current": __version__, "tag": tag})
        if busy and not force:
            raise Conflict(f"đang có {busy} run chạy/chờ — cập nhật sẽ dừng daemon và ngắt chúng (tự tiếp tục sau khi khởi động lại). "
                           "Chờ xong, hoặc cập nhật dù vậy (force)", data={"busy": busy})
        plan = self.plan(tag)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.paths.logs_dir.mkdir(parents=True, exist_ok=True)
        helper = self.dir / "helper.py"
        shutil.copyfile(Path(__file__).with_name("update_helper.py"), helper)
        st = {"state": "starting", "phase": "starting", "tag": tag, "version_before": __version__, "started_at": time.time(),
              "updated_at": time.time(), "by": by, "forced": bool(force), "plan": plan}
        tmp = self.state_path.with_name("state.json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)
        argv = [self.base_python, "-I", str(helper), "apply", "--state", str(self.state_path), "--log", str(self.log_path),
                "--home", str(self.paths.home), "--install", json.dumps(plan["install"]), "--restart", json.dumps(plan["restart"]),
                "--verify", json.dumps(plan["verify"])]
        if wait_pid:
            argv += ["--wait-pid", str(wait_pid)]
        pid = self.spawn(argv, {"log": str(self.paths.logs_dir / "update.out"), "cwd": str(self.paths.home), "env": env})
        st["helper_pid"] = pid
        return {"started": True, "tag": tag, "from": __version__, "helper_pid": pid, "log": str(self.log_path),
                "note": "daemon sẽ dừng rồi tự khởi động lại bằng bản mới (thường < 1 phút)"}


# ============================================================================ RPC
def register_rpc(daemon: "Daemon") -> None:
    """`update.check {refresh?}` (đọc), `update.status {lines?}` (đọc), `update.start {tag?, force?}` (HUMAN_ONLY, tự audit)."""
    import os

    from .mcp_server.rpc import wrap_with_audit

    hub = daemon.hub
    up: Updater = getattr(hub, "updater", None) or Updater(daemon.paths)
    hub.updater = up  # type: ignore[attr-defined]

    async def _check(p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(up.check, bool(p.get("refresh")))

    def _status(p: dict[str, Any]) -> dict[str, Any]:
        return {"state": up.state(), "in_progress": up.in_progress(), "log_tail": up.log_tail(int(p.get("lines") or 40)), "current": __version__}

    async def _start(p: dict[str, Any]) -> dict[str, Any]:
        from .auth import current_caller

        c = current_caller()
        st = hub.status()
        busy = int(st.get("running") or 0) + int(st.get("queued") or 0)
        env = dict(os.environ)
        env["CFHARNESS_HOME"] = str(daemon.paths.home)
        res = await asyncio.to_thread(up.start, tag=p.get("tag") or None, force=bool(p.get("force")), busy=busy, wait_pid=os.getpid(), env=env,
                                      by=f"manager:{c.manager}" if c is not None and c.manager else "user")
        asyncio.get_running_loop().call_later(0.5, daemon.stopping.set)  # trả lời RPC trước, rồi dừng để helper cài đè
        return res

    methods: dict[str, Callable[[dict[str, Any]], Any]] = {"update.check": _check, "update.status": _status, "update.start": _start}
    methods["update.start"] = wrap_with_audit(hub, "update.start", methods["update.start"])
    daemon.methods.update(methods)


__all__ = ["Updater", "fetch_latest", "install_info", "parse_version", "register_rpc", "REPO", "GIT_URL", "TAG_RE", "MUTATING_UPDATE"]
