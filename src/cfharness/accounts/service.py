"""`AccountService` — danh sách tài khoản Claude của hub (docs/dev-notes/plan-claude-accounts.md).

Cơ chế:
- Token mỗi tài khoản ở secret store `CLAUDE_ACCT_<ID>`; bảng `claude_accounts` chỉ giữ nhãn + sha256 token + kết quả kiểm gần nhất.
- "Đổi tài khoản" = chép token vào secret `CLAUDE_CODE_OAUTH_TOKEN` (runtime/claude_cli.py đọc lại mỗi lần spawn) → run mới,
  takeover, doctor tự theo; run đang chạy giữ tài khoản cũ. Tài khoản đặc biệt `machine` (đăng nhập sẵn của máy) = XOÁ secret đó.
- Con trỏ `hub_state.claude_account_active` = `a_xxxxxxxx | machine | None` (None = chưa cấu hình, hành vi như cũ). Token đang
  dùng không khớp tài khoản nào → hiển thị tài khoản ảo `custom`.
- Chỉ NGƯỜI đổi tài khoản (RPC ∈ HUMAN_ONLY). KHÔNG có đường tự đổi nào trong hub — cố ý (điều khoản Anthropic).

Không bao giờ trả/ghi token: kết quả, event, audit, log chỉ có id/nhãn.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..errors import Conflict, HubError, NotFound
from ..events import iso, make_event
from ..redact import register_secret_values

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.accounts")

ACTIVE_KEY = "claude_account_active"
CHECKS_KEY = "claude_account_checks"  # kết quả kiểm của tài khoản ảo machine/custom: {id: {status, last_error, checked_at}}
MIGRATED_KEY = "claude_accounts_migrated"
GATES_KEY = "rate_limit_gates"  # luồng rate-limit ghi: {<account_key>: {until, source, run_id, set_at}}
RL_BY_ACCOUNT_KEY = "rate_limit_by_account"  # luồng rate-limit ghi: {<account_key>: <state rate_limit>}
DEFAULT_GATE_KEY = "_default"  # khoá cổng của dispatcher khi chưa cấu hình tài khoản
OAUTH_SECRET = "CLAUDE_CODE_OAUTH_TOKEN"
MACHINE = "machine"
CUSTOM = "custom"
DEFAULT_LABEL = "Mặc định"
MACHINE_LABEL = "Đăng nhập của máy"
CUSTOM_LABEL = "Token tự đặt"
TOKEN_VALID_DAYS = 365  # token `claude setup-token` có hạn 1 năm
EXPIRING_DAYS = 14
LOGIN_TTL_S = 600.0
LABEL_MAX = 80


def token_hash(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def secret_name(account_id: str) -> str:
    return f"CLAUDE_ACCT_{account_id.upper()}"


def _parse_ts(v: Any) -> float | None:
    """ISO hoặc epoch (giây) → epoch; không đọc được → None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _clean_label(label: Any) -> str:
    s = " ".join(str(label or "").split())
    if not s:
        raise HubError("thiếu tên tài khoản", code=-32602)
    if len(s) > LABEL_MAX:
        raise HubError(f"tên tài khoản dài quá {LABEL_MAX} ký tự", code=-32602)
    return s


def _clean_token(token: Any) -> str:
    t = str(token or "").strip()
    if not t:
        raise HubError("thiếu token — chạy `claude setup-token` rồi dán token (sk-ant-oat01-…)", code=-32602)
    if any(c.isspace() for c in t) or len(t) < 20:
        raise HubError("token không hợp lệ — token của `claude setup-token` có dạng sk-ant-oat01-…", code=-32602)
    return t


class _Login:
    def __init__(self, handle: Any, label: str, ttl_s: float) -> None:
        self.handle = handle
        self.label = label
        self.deadline = time.monotonic() + ttl_s
        self.expires_at = iso(datetime.now(timezone.utc) + timedelta(seconds=ttl_s))
        self.timer: asyncio.TimerHandle | None = None


class AccountService:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.login_ttl_s = LOGIN_TTL_S
        self._logins: dict[str, _Login] = {}  # RAM daemon: phiên `claude setup-token` đang chờ code (tối đa 1)

    # ------------------------------------------------------------------ hạ tầng
    @property
    def store(self):
        return self.hub.store

    @property
    def secrets(self):
        return self.hub.secrets  # type: ignore[attr-defined]

    def _publish(self, type_: str, payload: dict[str, Any]) -> None:
        self.hub.publish(make_event(type_, None, payload, source="hub"))

    def _claude_cfg(self) -> dict[str, Any]:
        return dict((self.hub.config.data.get("runtime") or {}).get("claude") or {})

    def _current_token(self) -> str | None:
        try:
            v = self.secrets.get(OAUTH_SECRET)
        except Exception:  # noqa: BLE001 — secret store hỏng → như chưa đặt
            return None
        v = (v or "").strip()
        return v or None

    def _rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.store.conn.execute("SELECT * FROM claude_accounts ORDER BY created_at, id")]

    def _row(self, account_id: str) -> dict[str, Any] | None:
        r = self.store.conn.execute("SELECT * FROM claude_accounts WHERE id=?", (account_id,)).fetchone()
        return dict(r) if r else None

    def _require_row(self, account_id: str) -> dict[str, Any]:
        r = self._row(account_id)
        if r is None:
            raise NotFound(f"không có tài khoản Claude '{account_id}'")
        return r

    def _by(self) -> str:
        from ..auth import current_caller

        c = current_caller()
        if c is None or c.internal:
            return "user"
        return f"manager:{c.manager}" if c.manager else ("user" if c.human else c.via)

    # ------------------------------------------------------------------ tài khoản đang dùng
    def active_id(self) -> str | None:
        """Tài khoản THỰC SỰ đang dùng: theo token trong `CLAUDE_CODE_OAUTH_TOKEN` (khớp hash → id, không khớp → `custom`);
        không có token → `machine` nếu người đã chọn đăng nhập của máy, ngược lại None (chưa cấu hình)."""
        tok = self._current_token()
        if tok:
            r = self.store.conn.execute("SELECT id FROM claude_accounts WHERE token_hash=?", (token_hash(tok),)).fetchone()
            return r[0] if r else CUSTOM
        return MACHINE if self.store.state_get(ACTIVE_KEY) is not None else None

    def active_brief(self) -> dict[str, Any] | None:
        """`hub.status.account` = `{id, label}` | None."""
        aid = self.active_id()
        if aid is None:
            return None
        if aid == MACHINE:
            return {"id": MACHINE, "label": MACHINE_LABEL}
        if aid == CUSTOM:
            return {"id": CUSTOM, "label": CUSTOM_LABEL}
        r = self._row(aid)
        return {"id": aid, "label": r["label"] if r else aid}

    # ------------------------------------------------------------------ dựng Account (KHÔNG có token)
    def _usage(self, account_id: str) -> tuple[float, str | None]:
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        cost = self.store.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE account_id=? AND ended_at >= ?",
                                       (account_id, iso(start))).fetchone()[0]
        last = self.store.conn.execute("SELECT MAX(started_at) FROM runs WHERE account_id=?", (account_id,)).fetchone()[0]
        return round(float(cost or 0), 8), last

    def _rate_limit(self, account_id: str) -> dict[str, Any] | None:
        gates = self.store.state_get(GATES_KEY) or {}
        g = gates.get(account_id) if isinstance(gates, dict) else None
        if not isinstance(g, dict):
            return None
        until = _parse_ts(g.get("until"))
        if until is None or until <= time.time():
            return None
        rl = (self.store.state_get(RL_BY_ACCOUNT_KEY) or {})
        rl = rl.get(account_id) if isinstance(rl, dict) else None
        util = rl.get("utilization") if isinstance(rl, dict) else None
        return {"until": g.get("until"), "utilization": util}

    def _account(self, *, aid: str, label: str, kind: str, active: str | None, created_at: str | None, expires_at: str | None,
                 last_used_at: str | None, status: str | None, last_error: str | None) -> dict[str, Any]:
        cost, last_run = self._usage(aid)
        last_used = max([x for x in (last_used_at, last_run) if x], default=None)
        is_active = active == aid
        rate_limit = self._rate_limit(aid)
        if rate_limit is None and is_active:  # dispatcher khoá cổng theo con trỏ `claude_account_active` ("_default" khi chưa đặt)
            ptr = self.store.state_get(ACTIVE_KEY) or DEFAULT_GATE_KEY
            rate_limit = self._rate_limit(ptr) if ptr != aid else None
        st = status or "unknown"
        ab = self.hub.runs.auth_block if is_active else None
        if rate_limit is not None:
            st = "rate_limited"
        elif ab:
            st, last_error = "auth_error", str(ab.get("message") or last_error or "")[:500]
        elif st != "auth_error" and (exp := _parse_ts(expires_at)) is not None and exp - time.time() < EXPIRING_DAYS * 86400:
            st = "expiring"
        return {"id": aid, "label": label, "kind": kind, "active": is_active, "created_at": created_at, "expires_at": expires_at,
                "last_used_at": last_used, "status": st, "last_error": last_error, "rate_limit": rate_limit, "cost_today_usd": cost}

    def _from_row(self, r: dict[str, Any], active: str | None) -> dict[str, Any]:
        return self._account(aid=r["id"], label=r["label"], kind="token", active=active, created_at=r["created_at"], expires_at=r["expires_at"],
                             last_used_at=r["last_used_at"], status=r["status"], last_error=r["last_error"])

    def _virtual(self, aid: str, active: str | None) -> dict[str, Any]:
        chk = (self.store.state_get(CHECKS_KEY) or {}).get(aid) or {}
        return self._account(aid=aid, label=MACHINE_LABEL if aid == MACHINE else CUSTOM_LABEL, kind=aid, active=active, created_at=None,
                             expires_at=None, last_used_at=None, status=chk.get("status"), last_error=chk.get("last_error"))

    def get(self, account_id: str) -> dict[str, Any]:
        active = self.active_id()
        if account_id == MACHINE or (account_id == CUSTOM and active == CUSTOM):
            return self._virtual(account_id, active)
        return self._from_row(self._require_row(account_id), active)

    def list(self) -> dict[str, Any]:
        active = self.active_id()
        out = [self._from_row(r, active) for r in self._rows()]
        if active == CUSTOM:
            out.append(self._virtual(CUSTOM, active))
        out.append(self._virtual(MACHINE, active))
        return {"active": active, "accounts": out}

    # ------------------------------------------------------------------ ghi
    def add_token(self, label: Any, token: Any, *, expires_at: str | None = None) -> dict[str, Any]:
        label, token = _clean_label(label), _clean_token(token)
        register_secret_values(token)  # che theo giá trị ngay — trước mọi đường có thể ghi lỗi/log
        h = token_hash(token)
        dup = self.store.conn.execute("SELECT id, label FROM claude_accounts WHERE token_hash=?", (h,)).fetchone()
        if dup is not None:
            raise Conflict(f"token này đã có ở tài khoản '{dup[1]}' ({dup[0]})", data={"id": dup[0]})
        aid = "a_" + uuid.uuid4().hex[:8]
        now = datetime.now(timezone.utc)
        self.secrets.set(secret_name(aid), token)
        try:
            self.store.conn.execute(
                "INSERT INTO claude_accounts(id, label, token_hash, created_at, expires_at, status) VALUES (?,?,?,?,?,?)",
                (aid, label, h, iso(now), expires_at or iso(now + timedelta(days=TOKEN_VALID_DAYS)), "unknown"),
            )
        except Exception:
            self.secrets.delete(secret_name(aid))
            raise
        self._publish("account.added", {"id": aid, "label": label})
        return self.get(aid)

    def use(self, account_id: str) -> dict[str, Any]:
        """NGƯỜI chọn tài khoản: chép token vào `CLAUDE_CODE_OAUTH_TOKEN` (machine → xoá), gỡ cờ chặn đăng nhập, phát
        `account.switched`, xếp lại hàng đợi. Run đang chạy giữ tài khoản cũ."""
        account_id = str(account_id or "").strip()
        previous = self.active_id()
        if account_id == MACHINE:
            self.secrets.delete(OAUTH_SECRET)
        elif account_id == CUSTOM:
            raise HubError("'custom' là token đặt tay ngoài danh sách — thêm nó bằng `cfharness account add` rồi chọn tài khoản đó")
        else:
            self._require_row(account_id)
            tok = (self.secrets.get(secret_name(account_id)) or "").strip()
            if not tok:
                raise HubError(f"thiếu token của tài khoản '{account_id}' trong secret store — xoá rồi thêm lại tài khoản",
                               data={"secret": secret_name(account_id)})
            self.secrets.set(OAUTH_SECRET, tok)
            self.store.conn.execute("UPDATE claude_accounts SET last_used_at=? WHERE id=?", (iso(), account_id))
        self.store.state_set(ACTIVE_KEY, account_id)
        self.hub.runs.clear_auth_block(by="account_switch")
        self._publish("account.switched", {"from": previous, "to": account_id, "by": self._by()})
        try:
            self.hub.runs.tick()  # run ad-hoc đang xếp hàng chạy ngay bằng tài khoản mới
        except RuntimeError:  # pragma: no cover — ngoài event loop (CLI/test đồng bộ)
            pass
        return {"active": account_id, "previous": previous}

    def rename(self, account_id: str, label: Any) -> dict[str, Any]:
        if account_id in (MACHINE, CUSTOM):
            raise HubError(f"không đổi tên được tài khoản '{account_id}'")
        self._require_row(account_id)
        label = _clean_label(label)
        self.store.conn.execute("UPDATE claude_accounts SET label=? WHERE id=?", (label, account_id))
        self._publish("account.renamed", {"id": account_id, "label": label})
        return self.get(account_id)

    def remove(self, account_id: str) -> dict[str, Any]:
        if account_id in (MACHINE, CUSTOM):
            raise HubError(f"không xoá được tài khoản '{account_id}'")
        self._require_row(account_id)
        if self.active_id() == account_id:
            raise Conflict("đang dùng tài khoản này — chọn tài khoản khác trước khi xoá", data={"id": account_id})
        self.secrets.delete(secret_name(account_id))
        self.store.conn.execute("DELETE FROM claude_accounts WHERE id=?", (account_id,))
        if self.store.state_get(ACTIVE_KEY) == account_id:  # con trỏ cũ (token đã bị đặt tay khác) — không trỏ vào tài khoản đã xoá
            self.store.state_set(ACTIVE_KEY, MACHINE if self._current_token() is None else None)
        self._publish("account.removed", {"id": account_id})
        return {"ok": True}

    def migrate_legacy(self) -> dict[str, Any] | None:
        """Lần đầu: có `CLAUDE_CODE_OAUTH_TOKEN` mà chưa có tài khoản nào → tài khoản "Mặc định" từ nó, đặt đang dùng. Chạy một lần."""
        if self.store.state_get(MIGRATED_KEY):
            return None
        tok = self._current_token()
        created = None
        if tok and not self._rows() and self.store.state_get(ACTIVE_KEY) is None:
            try:
                created = self.add_token(DEFAULT_LABEL, tok, expires_at=None)
            except HubError as e:  # token lạ (quá ngắn…) → để nguyên, hiển thị `custom`
                log.warning("không tạo được tài khoản Mặc định từ CLAUDE_CODE_OAUTH_TOKEN: %s", e.message)
            else:
                self.store.conn.execute("UPDATE claude_accounts SET expires_at=NULL WHERE id=?", (created["id"],))  # không biết ngày tạo token
                self.store.state_set(ACTIVE_KEY, created["id"])
        self.store.state_set(MIGRATED_KEY, True)
        return created

    # ------------------------------------------------------------------ kiểm `claude auth status`
    def _env(self, token: str | None) -> dict[str, str]:
        """Env cho `claude` (env_strip áp dụng). token None → đăng nhập của máy (bỏ CLAUDE_CODE_OAUTH_TOKEN của env daemon)."""
        from ..runtime.claude_cli import child_env

        env = child_env(self._claude_cfg(), oauth_token=token)
        if token is None:
            env.pop(OAUTH_SECRET, None)
        return env

    async def _auth_status(self, env: dict[str, str]) -> tuple[bool | None, str]:
        """`claude auth status` (JSON `loggedIn`) với env cho trước → (loggedIn|None, mô tả lỗi). Cùng kiểu ClaudeCliRuntime.auth_status."""
        from ..oscompat import exec_argv, no_window_kwargs

        cfg = self._claude_cfg()
        argv = exec_argv([cfg.get("binary") or "claude", "auth", "status"])
        try:
            proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE, env=env, **no_window_kwargs())
        except OSError as e:
            return None, f"không chạy được claude: {e}"
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), float(cfg.get("auth_check_timeout_s", 30)))
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:  # pragma: no cover
                pass
            return None, "claude auth status quá thời gian"
        try:
            info = json.loads(out_b.decode("utf-8", "replace"))
        except (json.JSONDecodeError, ValueError):
            info = None
        if not isinstance(info, dict) or "loggedIn" not in info:
            return None, (err_b.decode("utf-8", "replace").strip()[-300:] or "không đọc được kết quả claude auth status")
        return bool(info["loggedIn"]), ""

    async def check(self, account_id: str) -> dict[str, Any]:
        account_id = str(account_id or "").strip()
        if account_id == MACHINE:
            token = None
        elif account_id == CUSTOM:
            token = self._current_token()
            if token is None or self.active_id() != CUSTOM:
                raise NotFound("không có token tự đặt nào đang dùng")
        else:
            self._require_row(account_id)
            token = (self.secrets.get(secret_name(account_id)) or "").strip() or None
            if token is None:
                raise HubError(f"thiếu token của tài khoản '{account_id}' trong secret store")
        ok, err = await self._auth_status(self._env(token))
        if ok is True:
            status, last_error = "ok", None
        elif ok is False:
            status, last_error = "auth_error", "claude auth status: chưa đăng nhập (token sai, đã thu hồi hoặc hết hạn)"
        else:
            status, last_error = "unknown", f"không kiểm được: {err}"[:500]
        if account_id in (MACHINE, CUSTOM):
            checks = dict(self.store.state_get(CHECKS_KEY) or {})
            checks[account_id] = {"status": status, "last_error": last_error, "checked_at": iso()}
            self.store.state_set(CHECKS_KEY, checks)
        else:
            self.store.conn.execute("UPDATE claude_accounts SET status=?, last_error=?, checked_at=? WHERE id=?",
                                    (status, last_error, iso(), account_id))
        return self.get(account_id)

    # ------------------------------------------------------------------ đăng nhập qua `claude setup-token` (PTY — login.py)
    async def _close_login(self, login_id: str) -> bool:
        s = self._logins.pop(login_id, None)
        if s is None:
            return False
        if s.timer is not None:
            s.timer.cancel()
        try:
            await s.handle.close()
        except Exception:  # pragma: no cover — close idempotent, lỗi đóng không được che kết quả
            log.exception("không đóng được phiên đăng nhập %s", login_id)
        return True

    def _expire_soon(self, login_id: str) -> None:
        try:
            asyncio.get_running_loop().create_task(self._close_login(login_id))
        except RuntimeError:  # pragma: no cover
            self._logins.pop(login_id, None)

    async def login_start(self, label: Any) -> dict[str, Any]:
        label = _clean_label(label)
        for lid in list(self._logins):  # tối đa 1 phiên — mở phiên mới đóng phiên cũ
            await self._close_login(lid)
        from .login import start_login  # luồng LOGIN (PTY) — nạp trễ

        cfg = self._claude_cfg()
        # env_strip áp dụng, KHÔNG chèn token đang dùng của hub → setup-token đăng nhập mới từ đầu
        handle = await start_login(str(cfg.get("binary") or "claude"), self._env(None))
        login_id = "l_" + uuid.uuid4().hex[:12]
        s = _Login(handle, label, self.login_ttl_s)
        self._logins[login_id] = s
        try:
            s.timer = asyncio.get_running_loop().call_later(self.login_ttl_s, self._expire_soon, login_id)
        except RuntimeError:  # pragma: no cover
            pass
        return {"login_id": login_id, "url": handle.url, "expires_at": s.expires_at}

    async def login_finish(self, login_id: str, code: Any) -> dict[str, Any]:
        code = str(code or "").strip()
        if not code:
            raise HubError("thiếu code — dán code trang đăng nhập Claude hiển thị", code=-32602)
        register_secret_values(code)
        s = self._logins.get(login_id)
        if s is not None and time.monotonic() > s.deadline:
            await self._close_login(login_id)
            s = None
        if s is None:
            raise NotFound("phiên đăng nhập không tồn tại hoặc đã hết hạn (10 phút) — bấm thêm tài khoản lại")
        try:
            token = await s.handle.submit(code)
        finally:
            await self._close_login(login_id)
        register_secret_values(token)
        return {"account": self.add_token(s.label, token)}

    async def login_cancel(self, login_id: str) -> dict[str, Any]:
        await self._close_login(login_id)
        return {"ok": True}

    async def close_all_logins(self) -> None:
        for lid in list(self._logins):
            await self._close_login(lid)
