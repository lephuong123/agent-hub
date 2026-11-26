"""Tài khoản runtime codex / grok — NHIỀU tài khoản mỗi runtime, giống "Tài khoản Claude" (Cài đặt → Tài khoản AI).

Hub KHÔNG giữ token: mỗi tài khoản là một THƯ MỤC PHIÊN của chính CLI.
- `machine` (luôn có) = đăng nhập sẵn của CLI trên máy: `~/.codex` / `~/.grok`.
- Tài khoản thêm từ hub: `<HUB_HOME>/runtime-accounts/<kind>/<id>/` — Codex dùng làm `CODEX_HOME` (auth.json, phiên, thread của tài
  khoản đó; KHÔNG nạp `~/.codex/config.toml`/`AGENTS.md` của người dùng → còn cô lập hơn), Grok dùng làm `GROK_HOME` khi đăng nhập và
  `GROK_AUTH_PATH=<dir>/auth.json` khi chạy (GROK_HOME khi chạy vẫn là home riêng của hub — runtime/acp.py).
- Run dùng tài khoản **Đang dùng** của runtime (`account_env`); người bấm **Dùng** để đổi — hub không tự đổi.

Trạng thái (email, gói, workspace, hạn mức) đọc NGẦM và CACHE xuống đĩa (`status-cache.json`): trang Cài đặt hiện ngay bản đã lưu,
kiểm lại chạy nền khi cũ hơn `STATUS_TTL_S` hoặc khi người bấm "Kiểm tra".
- Codex: API chính thức của `codex app-server` — `account/read`, `account/rateLimits/read`, `account/login/start {chatgptDeviceCode |
  chatgpt}` + `account/login/completed`, `account/login/cancel`. Đăng xuất: `codex logout`.
- Grok: `grok models` (không gọi model), `grok login --device-auth`, `grok logout`.

RPC (daemon.py; HUMAN_ONLY; ghi bọc audit): `runtime_account.list {kind?}`, `.refresh {kind, id?}`, `.detect {kind}`,
`.login_start {kind, method?: device|browser, id? | label?}` (label = thêm tài khoản mới), `.login_cancel {kind}`, `.logout {kind, id}`,
`.use {kind, id}`, `.rm {kind, id}`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import HubError
from ..oscompat import IS_WINDOWS, exec_argv, no_window_kwargs
from .rpcbase import RpcError, RpcProc, STREAM_LIMIT, binary_version

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.runtime.accounts")

KINDS = ("codex", "grok")
LABELS = {"codex": "ChatGPT (Codex)", "grok": "Grok"}
MACHINE = "machine"
STATUS_TTL_S = 600.0  # bản cache cũ hơn → kiểm lại NGẦM lần mở trang kế tiếp (vẫn hiện bản cũ ngay)
LOGIN_TTL_S = 900.0
LABEL_MAX = 60
_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b")
_URL_RE = re.compile(r"https://\S+")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ID_RE = re.compile(r"^(machine|a_[0-9a-f]{8})$")


def _kind(kind: Any) -> str:
    k = str(kind or "")
    if k not in KINDS:
        raise HubError(f"runtime phải là một trong {', '.join(KINDS)}", code=-32602)
    return k


def _root(hub_home: Path | str) -> Path:
    return Path(hub_home) / "runtime-accounts"


def account_home(hub_home: Path | str, kind: str, acct_id: str) -> Path | None:
    """Thư mục phiên của tài khoản; None = `machine` (thư mục mặc định của CLI)."""
    if acct_id == MACHINE:
        return None
    if not _ID_RE.match(acct_id):
        raise HubError(f"id tài khoản không hợp lệ: {acct_id}", code=-32602)
    return _root(hub_home) / kind / acct_id


def _read_store(hub_home: Path | str) -> dict[str, Any]:
    try:
        d = json.loads((_root(hub_home) / "accounts.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def active_account(hub_home: Path | str, kind: str) -> str:
    ent = _read_store(hub_home).get(kind) or {}
    a = str(ent.get("active") or MACHINE)
    known = {x.get("id") for x in ent.get("accounts") or [] if isinstance(x, dict)}
    return a if a == MACHINE or a in known else MACHINE


def account_env(hub_home: Path | str | None, kind: str, acct_id: str | None = None, *, login: bool = False) -> dict[str, str]:
    """Biến môi trường để CLI dùng đúng tài khoản. Run: tài khoản Đang dùng. `login=True` (Grok): GROK_HOME = thư mục tài khoản để
    `grok login` ghi auth.json vào đó."""
    if not hub_home:
        return {}
    aid = acct_id or active_account(hub_home, kind)
    home = account_home(hub_home, kind, aid)
    if home is None:
        return {}
    home.mkdir(parents=True, exist_ok=True)  # CODEX_HOME phải tồn tại trước
    if kind == "codex":
        return {"CODEX_HOME": str(home)}
    env = {"GROK_AUTH_PATH": str(home / "auth.json")}
    if login:
        env["GROK_HOME"] = str(home)
    return env


def candidates(kind: str) -> list[str]:
    """Chỗ CLI hay được cài (PATH, Volta, npm global, trình cài của Grok) — để gợi ý khi `binary` chưa trỏ đúng."""
    home = Path.home()
    la = Path(os.environ.get("LOCALAPPDATA") or (home / "AppData" / "Local"))
    ad = Path(os.environ.get("APPDATA") or (home / "AppData" / "Roaming"))
    out: list[str] = []
    w = shutil.which(kind)
    if w:
        out.append(w)
    if kind == "codex":
        extra = [la / "Volta" / "bin" / "codex.cmd", ad / "npm" / "codex.cmd"] if IS_WINDOWS else \
                [home / ".volta" / "bin" / "codex", Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex"), home / ".npm-global" / "bin" / "codex"]
    else:
        extra = [home / ".grok" / "bin" / ("grok.exe" if IS_WINDOWS else "grok")]
    out += [str(p) for p in extra if p.exists()]
    return list(dict.fromkeys(out))


def _resolve(binary: str) -> str | None:
    if not binary:
        return None
    p = Path(binary).expanduser()
    if p.is_absolute() or os.sep in binary or "/" in binary:
        return str(p) if p.exists() else None
    return shutil.which(binary)


class RuntimeAccounts:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.home = Path(hub.paths.home)
        self.root = _root(self.home)
        self._cache: dict[str, dict[str, Any]] = self._read_json("status-cache.json")
        self._checking: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self.logins: dict[str, dict[str, Any]] = {}  # kind → lượt đăng nhập đang mở (một lượt mỗi runtime)

    # ------------------------------------------------------------------ lưu trữ
    def _read_json(self, name: str) -> dict[str, Any]:
        try:
            d = json.loads((self.root / name).read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_json(self, name: str, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f".{name}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.root / name)

    def _store(self) -> dict[str, Any]:
        return self._read_json("accounts.json")

    def _entry(self, kind: str) -> dict[str, Any]:
        e = self._store().get(kind) or {}
        return {"active": e.get("active") or MACHINE, "accounts": [a for a in e.get("accounts") or [] if isinstance(a, dict)]}

    def _save_entry(self, kind: str, entry: dict[str, Any]) -> None:
        st = self._store()
        st[kind] = entry
        self._write_json("accounts.json", st)

    def _accounts(self, kind: str) -> list[dict[str, Any]]:
        e = self._entry(kind)
        return [{"id": MACHINE, "label": "Đăng nhập của máy", "machine": True}] + [
            {"id": a["id"], "label": a.get("label") or a["id"], "machine": False, "created_at": a.get("created_at")} for a in e["accounts"]]

    def _find(self, kind: str, acct_id: str) -> dict[str, Any]:
        for a in self._accounts(kind):
            if a["id"] == acct_id:
                return a
        raise HubError(f"không có tài khoản {LABELS[kind]} '{acct_id}'", code=-32602)

    # ------------------------------------------------------------------ cấu hình CLI
    def _cfg(self, kind: str) -> dict[str, Any]:
        v = self.hub.config.get(f"runtime.{kind}")
        return dict(v) if isinstance(v, dict) else {}

    def binary(self, kind: str) -> str | None:
        b = str(self._cfg(kind).get("binary") or kind)
        hit = _resolve(b)
        if hit is None and b == kind:  # mặc định (tên lệnh) không có trong PATH → chỗ cài quen thuộc, như RpcRuntime.binary
            found = candidates(kind)
            hit = found[0] if found else None
        return hit

    def detect(self, kind: Any) -> dict[str, Any]:
        k = _kind(kind)
        return {"kind": k, "configured": self._cfg(k).get("binary") or k, "resolved": self.binary(k),
                "candidates": [{"path": c, "version": binary_version(c)} for c in candidates(k)]}

    def _env(self, kind: str, acct_id: str, *, login: bool = False) -> dict[str, str]:
        return {**os.environ, "GROK_DISABLE_AUTOUPDATER": "1", **account_env(self.home, kind, acct_id, login=login)}

    # ------------------------------------------------------------------ danh sách + kiểm ngầm
    def list(self, kind: Any = None) -> dict[str, Any]:
        kinds = [_kind(kind)] if kind else list(KINDS)
        out: dict[str, Any] = {}
        for k in kinds:
            self._gc(k)
            active = active_account(self.home, k)
            b = self.binary(k)
            accts = []
            for a in self._accounts(k):
                key = f"{k}:{a['id']}"
                st = self._cache.get(key) or {}
                stale = not st or time.time() - float(st.get("checked_at") or 0) > STATUS_TTL_S
                if stale and b:
                    self._kick(k, a["id"])
                accts.append({**a, "active": a["id"] == active, "checking": key in self._checking, **{x: st.get(x) for x in (
                    "logged_in", "email", "plan", "workspace", "rate_limits", "models", "error", "checked_at")}})
            cfg = self._cfg(k)
            ver = next((x.get("cli_version") for x in (self._cache.get(f"{k}:{a['id']}") or {} for a in self._accounts(k))
                        if x.get("cli_version")), None)
            out[k] = {"kind": k, "label": LABELS[k], "active": active, "accounts": accts, "binary": b, "version": ver, "configured": cfg.get("binary") or k, "model": cfg.get("model"),
                      "effort": cfg.get("effort"), "login": self._login_view(k), "candidates": [] if b else candidates(k)}
        return out

    def refresh(self, kind: Any, acct_id: Any = None) -> dict[str, Any]:
        k = _kind(kind)
        for a in self._accounts(k):
            if acct_id in (None, "", a["id"]):
                self._kick(k, a["id"], force=True)
        return self.list(k)

    def _kick(self, kind: str, acct_id: str, force: bool = False) -> None:
        key = f"{kind}:{acct_id}"
        if key in self._checking:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._checking.add(key)
        t = loop.create_task(self._check(kind, acct_id))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _check(self, kind: str, acct_id: str) -> None:
        key = f"{kind}:{acct_id}"
        try:
            st = await (self._codex_status(acct_id) if kind == "codex" else self._grok_status(acct_id))
            b = self.binary(kind)
            st["cli_version"] = await asyncio.to_thread(binary_version, b, 0) if b else None
            st["checked_at"] = time.time()
            self._cache[key] = st
            self._write_json("status-cache.json", self._cache)
        except Exception:  # noqa: BLE001 — best-effort: lỗi kiểm ngầm không làm hỏng daemon; trang giữ bản cũ
            log.exception("kiểm tài khoản %s lỗi", key)
        finally:
            self._checking.discard(key)

    async def wait_idle(self, timeout: float = 60.0) -> None:
        """Chờ mọi lượt kiểm ngầm xong (test / CLI)."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=timeout)

    async def _codex_status(self, acct_id: str) -> dict[str, Any]:
        st: dict[str, Any] = {"logged_in": None, "email": None, "plan": None, "workspace": None, "rate_limits": None, "error": None}
        b = self.binary("codex")
        if not b:
            st["error"] = "không tìm thấy CLI `codex` — cài: npm i -g @openai/codex, rồi đặt đường dẫn ở Cấu hình"
            return st
        try:
            sess = await _AppServer.open(b, env=self._env("codex", acct_id))
        except (OSError, RpcError, asyncio.TimeoutError) as e:
            st["error"] = f"không mở được `codex app-server`: {e}"
            return st
        try:
            acc = ((await sess.rpc.request("account/read", {}, timeout=30)) or {}).get("account")
            st["logged_in"] = bool(acc)
            if isinstance(acc, dict):
                st.update(email=acc.get("email"), plan=acc.get("planType"), workspace=acc.get("workspaceName"))
            if acc:
                try:
                    rl = (await sess.rpc.request("account/rateLimits/read", {}, timeout=30)) or {}
                    st["rate_limits"] = _rate_view(rl.get("rateLimits") or rl)
                except RpcError:
                    pass
        except (RpcError, asyncio.TimeoutError) as e:
            st["error"] = f"đọc tài khoản lỗi: {getattr(e, 'message', e)}"
        finally:
            await sess.close()
        return st

    async def _grok_status(self, acct_id: str) -> dict[str, Any]:
        st: dict[str, Any] = {"logged_in": None, "email": None, "plan": None, "models": [], "error": None}
        b = self.binary("grok")
        if not b:
            st["error"] = "không tìm thấy CLI `grok` — cài theo https://x.ai/cli, rồi đặt đường dẫn ở Cấu hình"
            return st
        code, out = await asyncio.to_thread(_run, [b, "models"], 40, self._env("grok", acct_id, login=True))
        low = out.lower()
        st["logged_in"] = code == 0 and "logged in" in low and "not logged in" not in low
        m = re.search(r"logged in with (\S+?)\.?\s*$", out, re.I | re.M)
        st["plan"] = m.group(1) if m else None
        st["models"] = re.findall(r"^\s*\*?\s*([a-z0-9][\w.-]+)(?: \(default\))?\s*$", out, re.M)[:12] if st["logged_in"] else []
        if code != 0 and not st["logged_in"]:
            st["error"] = (out.strip().splitlines() or [f"exit {code}"])[-1][:300]
        return st

    # ------------------------------------------------------------------ chọn / xoá
    def use(self, kind: Any, acct_id: Any) -> dict[str, Any]:
        k = _kind(kind)
        a = self._find(k, str(acct_id or ""))
        e = self._entry(k)
        e["active"] = a["id"]
        self._save_entry(k, e)
        return self.list(k)

    def rm(self, kind: Any, acct_id: Any) -> dict[str, Any]:
        k = _kind(kind)
        a = self._find(k, str(acct_id or ""))
        if a["machine"]:
            raise HubError("không xoá được 'Đăng nhập của máy' — dùng Đăng xuất", code=-32602)
        e = self._entry(k)
        e["accounts"] = [x for x in e["accounts"] if x.get("id") != a["id"]]
        if e["active"] == a["id"]:
            e["active"] = MACHINE
        self._save_entry(k, e)
        home = account_home(self.home, k, a["id"])
        if home is not None and home.exists():
            _rm_later(home)
        if self._cache.pop(f"{k}:{a['id']}", None) is not None:
            self._write_json("status-cache.json", self._cache)
        return self.list(k)

    # ------------------------------------------------------------------ đăng nhập
    def _login_view(self, kind: str) -> dict[str, Any] | None:
        lg = self.logins.get(kind)
        if not lg:
            return None
        if lg["state"] == "pending" and time.monotonic() - lg["t0"] > LOGIN_TTL_S:
            asyncio.ensure_future(self.login_cancel(kind, why="hết hạn 15 phút"))
        return {k: lg.get(k) for k in ("id", "state", "method", "url", "code", "error", "started_at", "account_id", "label", "new")}

    async def login_start(self, kind: Any, method: str = "device", acct_id: Any = None, label: Any = None) -> dict[str, Any]:
        k = _kind(kind)
        method = "browser" if method == "browser" else "device"
        if k == "grok" and method == "browser":
            raise HubError("Grok chỉ hỗ trợ đăng nhập bằng mã thiết bị từ hub", code=-32602)
        b = self.binary(k)
        if not b:
            raise HubError(f"chưa tìm thấy CLI `{k}` — đặt đường dẫn trước", code=-32602)
        new = bool(label)
        if new:
            lab = " ".join(str(label).split())[:LABEL_MAX]
            if not lab:
                raise HubError("cần tên tài khoản", code=-32602)
            aid = f"a_{uuid.uuid4().hex[:8]}"
        else:
            a = self._find(k, str(acct_id or MACHINE))
            aid, lab = a["id"], a["label"]
        await self.login_cancel(k, why="mở lượt mới")
        lg: dict[str, Any] = {"id": uuid.uuid4().hex[:12], "kind": k, "state": "pending", "method": method, "url": None, "code": None,
                              "error": None, "t0": time.monotonic(), "started_at": time.time(), "account_id": aid, "label": lab, "new": new}
        self.logins[k] = lg
        try:
            if k == "codex":
                await self._codex_login(lg, b)
            else:
                await self._grok_login(lg, b)
        except Exception as e:  # noqa: BLE001 — best-effort: lỗi hiện trên trang, không làm hỏng daemon
            await self._close_login(lg)
            self._drop_new(lg)
            lg.update(state="failed", error=str(getattr(e, "message", e))[:500])
        return self._login_view(k) or {}

    def _finish(self, lg: dict[str, Any], ok: bool, error: str | None) -> None:
        if lg["state"] != "pending":
            return
        lg.update(state="done" if ok else "failed", error=error)
        k = lg["kind"]
        if ok and lg["new"]:
            e = self._entry(k)
            e["accounts"].append({"id": lg["account_id"], "label": lg["label"], "created_at": time.time()})
            self._save_entry(k, e)
        elif not ok:
            self._drop_new(lg)
        self._kick(k, lg["account_id"], force=True)

    def _drop_new(self, lg: dict[str, Any]) -> None:
        """Tài khoản mới chưa đăng nhập xong → xoá thư mục phiên dở dang (không để rác). Windows: tiến trình con của CLI có thể còn giữ
        file vài giây sau khi đóng → thử lại nền; sót nữa thì `_gc` (mỗi lần `list`) dọn."""
        if lg.get("new"):
            home = account_home(self.home, lg["kind"], lg["account_id"])
            if home is not None and home.exists():
                _rm_later(home)

    def _gc(self, kind: str) -> None:
        """Dọn thư mục tài khoản mồ côi: không có trong danh sách và không phải lượt đăng nhập đang mở (huỷ/hỏng mà xoá chưa được)."""
        d = self.root / kind
        if not d.is_dir():
            return
        keep = {a["id"] for a in self._accounts(kind)}
        lg = self.logins.get(kind)
        if lg and lg.get("state") == "pending":
            keep.add(lg["account_id"])
        for p in d.iterdir():
            if p.is_dir() and _ID_RE.match(p.name) and p.name not in keep:
                _rmtree(p)

    async def _codex_login(self, lg: dict[str, Any], b: str) -> None:
        def on_notify(m: str, p: dict[str, Any]) -> None:
            # phiên app-server này chỉ có MỘT lượt đăng nhập; notification có thể tới trước khi đọc xong loginId của phản hồi
            if m == "account/login/completed" and (p.get("loginId") is None or lg.get("native_id") in (None, p.get("loginId"))):
                self._finish(lg, bool(p.get("success")), p.get("error"))
                asyncio.ensure_future(self._close_login(lg))

        sess = await _AppServer.open(b, env=self._env("codex", lg["account_id"]), on_notify=on_notify)
        lg["sess"] = sess
        res = await sess.rpc.request("account/login/start", {"type": "chatgptDeviceCode" if lg["method"] == "device" else "chatgpt"},
                                     timeout=60) or {}
        lg["native_id"] = res.get("loginId")
        lg["url"] = res.get("verificationUrl") or res.get("authUrl")
        lg["code"] = res.get("userCode")
        if not lg["url"] and lg["state"] == "pending":
            raise HubError("codex không trả link đăng nhập")

    async def _grok_login(self, lg: dict[str, Any], b: str) -> None:
        proc = await asyncio.create_subprocess_exec(*exec_argv([b, "login", "--device-auth"]), stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                    env=self._env("grok", lg["account_id"], login=True), **no_window_kwargs())
        lg["proc"] = proc
        out: list[str] = []
        assert proc.stdout is not None

        async def read_until_code() -> None:
            while not (lg["url"] and lg["code"]):
                line = await proc.stdout.readline()
                if not line:
                    break
                text = _ANSI_RE.sub("", line.decode("utf-8", "replace"))
                out.append(text)
                if not lg["url"] and (u := _URL_RE.search(text)):
                    lg["url"] = u.group(0)
                if not lg["code"] and (c := _CODE_RE.search(text)):
                    lg["code"] = c.group(0)

        await asyncio.wait_for(read_until_code(), 60)
        if not lg["url"]:
            raise HubError("grok không in link đăng nhập: " + "".join(out)[-300:])

        async def wait_exit() -> None:
            rest = await proc.stdout.read()
            text = "".join(out) + _ANSI_RE.sub("", rest.decode("utf-8", "replace"))
            rc = await proc.wait()
            ok = rc == 0 and "signed in" in text.lower()
            self._finish(lg, ok, None if ok else text.strip()[-300:])

        lg["waiter"] = asyncio.ensure_future(wait_exit())

    async def _close_login(self, lg: dict[str, Any]) -> None:
        sess = lg.pop("sess", None)
        if sess is not None:
            await sess.close()
        proc = lg.pop("proc", None)
        if proc is not None and proc.returncode is None:
            proc.kill()

    async def login_cancel(self, kind: Any, why: str = "người huỷ") -> dict[str, Any]:
        k = _kind(kind)
        lg = self.logins.pop(k, None)
        if lg is None:
            return {"ok": True}
        pending = lg["state"] == "pending"
        lg["state"] = "cancelled" if pending else lg["state"]
        if pending and lg.get("sess") is not None and lg.get("native_id"):
            try:
                await lg["sess"].rpc.request("account/login/cancel", {"loginId": lg["native_id"]}, timeout=10)
            except (RpcError, asyncio.TimeoutError):
                pass
        await self._close_login(lg)
        if pending:
            self._drop_new(lg)
        return {"ok": True, "cancelled": pending, "why": why}

    async def logout(self, kind: Any, acct_id: Any = None) -> dict[str, Any]:
        k = _kind(kind)
        a = self._find(k, str(acct_id or MACHINE))
        b = self.binary(k)
        if not b:
            raise HubError(f"chưa tìm thấy CLI `{k}`", code=-32602)
        code, out = await asyncio.to_thread(_run, [b, "logout"], 30, self._env(k, a["id"], login=True))
        if code != 0:
            raise HubError(f"{k} logout lỗi: {out.strip()[-300:]}")
        self._kick(k, a["id"], force=True)
        return self.list(k)


def _rmtree(path: Path) -> None:
    """rmtree chịu được file chỉ đọc (Windows: pack của git mà Codex clone vào CODEX_HOME mới) — bỏ cờ read-only rồi thử lại;
    lỗi còn lại bỏ qua (file đang bị giữ → `_rm_later`/`_gc` thử lần sau)."""
    import stat

    def onexc(func: Any, p: str, _exc: Any) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onexc=onexc)  # Python ≥ 3.12
    except TypeError:  # pragma: no cover — Python cũ
        shutil.rmtree(path, onerror=lambda f, p, e: onexc(f, p, e))
    except OSError:
        pass


def _rm_later(path: Path, tries: int = 10, delay_s: float = 1.0) -> None:
    _rmtree(path)
    if not path.exists():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def retry() -> None:
        for _ in range(tries):
            await asyncio.sleep(delay_s)
            await asyncio.to_thread(_rmtree, path)
            if not path.exists():
                return

    loop.create_task(retry())


def _rate_view(rl: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(rl, dict):
        return None
    out: dict[str, Any] = {"plan": rl.get("planType"), "reached": rl.get("rateLimitReachedType")}
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if isinstance(w, dict):
            out[key] = {"used_percent": w.get("usedPercent"), "window_mins": w.get("windowDurationMins"), "resets_at": w.get("resetsAt")}
    return out


def _run(argv: list[str], timeout: float, env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(exec_argv(argv), capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
                           stdin=subprocess.DEVNULL, env=env or {**os.environ, "GROK_DISABLE_AUTOUPDATER": "1"}, **no_window_kwargs())
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)
    return r.returncode, _ANSI_RE.sub("", (r.stdout or "") + (r.stderr or ""))


class _AppServer:
    """Phiên `codex app-server` ngắn (không thuộc run nào): initialize xong là dùng được `rpc`."""

    def __init__(self, proc: asyncio.subprocess.Process, rpc: RpcProc, pump: asyncio.Task) -> None:
        self.proc, self.rpc, self.pump = proc, rpc, pump

    @classmethod
    async def open(cls, binary: str, env: dict[str, str] | None = None, on_notify: Any = None) -> "_AppServer":
        proc = await asyncio.create_subprocess_exec(*exec_argv([binary, "app-server"]), stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                                                    limit=STREAM_LIMIT, env=env, **no_window_kwargs())

        async def no_requests(method: str, params: dict[str, Any], rid: Any) -> Any:
            raise RpcError(-32601, f"cfharness không xử lý {method} ở phiên tài khoản")

        rpc = RpcProc(proc, None, no_requests, on_notify or (lambda m, p: None))
        pump = asyncio.ensure_future(rpc.pump())
        self = cls(proc, rpc, pump)
        try:
            await rpc.request("initialize", {"clientInfo": {"name": "cfharness", "version": "1"}}, timeout=60)
            await rpc.notify("initialized", {})
        except BaseException:
            await self.close()
            raise
        return self

    async def close(self) -> None:
        self.rpc.close_stdin()
        try:
            await asyncio.wait_for(self.proc.wait(), 5)
        except asyncio.TimeoutError:
            from ..oscompat import kill_tree

            kill_tree(self.proc.pid) if IS_WINDOWS else self.proc.kill()
        self.pump.cancel()


MUTATING = ("runtime_account.login_start", "runtime_account.login_cancel", "runtime_account.logout", "runtime_account.use",
            "runtime_account.rm")


def register_rpc(daemon: Any) -> None:
    """RPC `runtime_account.*` (HUMAN_ONLY — auth.py). Ghi bọc audit; không tham số bí mật (hub không nhận token của CLI)."""
    from ..mcp_server.rpc import wrap_with_audit

    s = RuntimeAccounts(daemon.hub)
    daemon.hub.runtime_accounts = s  # type: ignore[attr-defined]
    methods = {
        "runtime_account.list": lambda p: s.list(p.get("kind")),
        "runtime_account.refresh": lambda p: s.refresh(p.get("kind"), p.get("id")),
        "runtime_account.detect": lambda p: s.detect(p.get("kind")),
        "runtime_account.login_start": lambda p: s.login_start(p.get("kind"), str(p.get("method") or "device"), p.get("id"),
                                                               p.get("label")),
        "runtime_account.login_cancel": lambda p: s.login_cancel(p.get("kind")),
        "runtime_account.logout": lambda p: s.logout(p.get("kind"), p.get("id")),
        "runtime_account.use": lambda p: s.use(p.get("kind"), p.get("id")),
        "runtime_account.rm": lambda p: s.rm(p.get("kind"), p.get("id")),
    }
    for name in MUTATING:
        methods[name] = wrap_with_audit(daemon.hub, name, methods[name])
    daemon.methods.update(methods)
