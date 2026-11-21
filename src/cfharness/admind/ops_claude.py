"""Kho tài khoản Claude (FR-SA-CLA-01..03): token `claude setup-token` trong vault admind, gán cho hub user (ghi vào secret store
của hub qua stdin — không bao giờ argv/log/DB), đổi hàng loạt, xoay token, cảnh báo token sắp hết hạn (> 330 ngày tuổi)."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from .alerts import sync_alerts
from .db import new_id
from .ops import OpError, op
from .ptypes import Name, ObjId, Params, clean_text

TOKEN_OLD_DAYS = 330


def _check_token(tok: str) -> str:
    tok = (tok or "").strip().replace(" ", "").replace("\n", "")
    if not tok.startswith("sk-ant-") or len(tok) < 40:
        raise OpError("invalid", "token phải dạng sk-ant-… (claude setup-token)")
    return tok


@op("claude.list", group="claude", risk="read")
def claude_list(ctx, p) -> list[dict[str, Any]]:
    """Tài khoản trong kho: tên, trạng thái, tuổi token, số user đang gán (token không bao giờ hiện lại)."""
    out = []
    for a in ctx.db.all("SELECT * FROM claude_accounts ORDER BY name"):
        age = ctx.state.vault.age_days(f"claude.{a['id']}", ctx.state.clock())
        a["age_days"] = round(age, 1) if age is not None else None
        a["users"] = ctx.db.one("SELECT COUNT(*) AS n FROM user_claude WHERE account_id=?", (a["id"],))["n"]
        a["user_names"] = [r["os_user"] for r in ctx.db.all("SELECT u.os_user FROM user_claude c JOIN hub_users u ON u.id=c.user_id WHERE c.account_id=?",
                                                            (a["id"],))]
        a["old"] = bool(age is not None and age > TOKEN_OLD_DAYS)
        out.append(a)
    return out


class AddP(Params):
    name: Name
    token: str = Field(min_length=20, max_length=400)
    quota_note: str = Field(default="", max_length=200)


@op("claude.account_add", group="claude", risk="high", params=AddP, target=lambda p: p.name)
def account_add(ctx, p: AddP) -> dict[str, Any]:
    """Thêm token vào kho."""
    tok = _check_token(p.token)
    if ctx.db.one("SELECT id FROM claude_accounts WHERE name=?", (p.name,)):
        raise OpError("conflict", "trùng tên")
    aid = new_id("ca")
    ctx.state.vault.set(f"claude.{aid}", tok)
    ctx.db.exec("INSERT INTO claude_accounts(id, name, state, set_at, quota_note) VALUES (?,?,'unknown',?,?)",
                (aid, clean_text(p.name), ctx.state.clock(), clean_text(p.quota_note)))
    return {"id": aid}


class IdP(Params):
    id: ObjId


def _probe_user(ctx) -> str:
    u = ctx.db.one("SELECT os_user FROM hub_users WHERE state='active' ORDER BY created_at LIMIT 1")
    if not u:
        raise OpError("conflict", "cần ít nhất một hub đang hoạt động để kiểm token (chạy claude dưới user đó)")
    return u["os_user"]


@op("claude.check", group="claude", risk="normal", params=IdP, target=lambda p: p.id)
def account_check(ctx, p: IdP) -> dict[str, Any]:
    """Gọi thử Claude bằng token (một câu rất ngắn, chạy dưới uid một hub user; token qua stdin)."""
    tok = ctx.state.vault.get(f"claude.{p.id}")
    if not tok:
        raise OpError("not_found", "không có tài khoản")
    u = _probe_user(ctx)
    script = 'IFS= read -r T; cd "$HOME" && CLAUDE_CODE_OAUTH_TOKEN="$T" exec "$HOME/.local/bin/claude" -p --max-turns 1 --setting-sources "" "Reply with just: OK"'
    r = ctx.state.runner.as_user(u, ["/bin/sh", "-c", script], stdin=tok + "\n", timeout=120)
    out = (r.out or "") + (r.err or "")
    state = "ok" if "OK" in (r.out or "") else ("rate_limited" if "rate" in out.lower() or "limit" in out.lower() else "auth_error")
    ctx.db.exec("UPDATE claude_accounts SET state=?, checked_at=? WHERE id=?", (state, ctx.state.clock(), p.id))
    return {"state": state}


class AssignP(Params):
    users: list[str] = Field(min_length=1, max_length=200)
    account_id: ObjId


def _push(ctx, os_user: str, tok: str) -> None:
    r = ctx.state.runner.cfh(os_user, "secret", "set", "CLAUDE_CODE_OAUTH_TOKEN", "--value-stdin", stdin=tok, timeout=60)
    if not r.ok:
        raise OpError("failed", f"không ghi được token cho {os_user}: {(r.err or r.out).strip()[:200]}")
    ctx.state.runner.run(["launchctl", "kickstart", "-k", f"system/com.cfharness.daemon.{os_user}"], timeout=60)


@op("claude.assign", group="claude", risk="high", params=AssignP, target=lambda p: f"{p.account_id}:{','.join(p.users)}"[:200])
def assign(ctx, p: AssignP) -> dict[str, Any]:
    """Gán / đổi tài khoản Claude cho một hoặc nhiều hub (ghi token vào secret store của hub + khởi động lại daemon)."""
    from .ops_users import find_user

    tok = ctx.state.vault.get(f"claude.{p.account_id}")
    if not tok:
        raise OpError("not_found", "không có tài khoản")
    done = []
    for key in p.users:
        u = find_user(ctx, key)
        if u["state"] not in ("active", "suspended"):
            raise OpError("conflict", f"{u['os_user']} không hoạt động")
        _push(ctx, u["os_user"], tok)
        ctx.db.exec("INSERT OR REPLACE INTO user_claude(user_id, account_id, assigned_at) VALUES (?,?,?)", (u["id"], p.account_id, ctx.state.clock()))
        done.append(u["os_user"])
    return {"assigned": done}


class RotateP(IdP):
    token: str = Field(min_length=20, max_length=400)


@op("claude.rotate", group="claude", risk="high", params=RotateP, target=lambda p: p.id)
def rotate(ctx, p: RotateP) -> dict[str, Any]:
    """Xoay token: thay token trong kho → ghi lại cho mọi user đang dùng tài khoản này."""
    if not ctx.db.one("SELECT id FROM claude_accounts WHERE id=?", (p.id,)):
        raise OpError("not_found", "không có tài khoản")
    tok = _check_token(p.token)
    ctx.state.vault.set(f"claude.{p.id}", tok)
    ctx.db.exec("UPDATE claude_accounts SET set_at=?, state='unknown' WHERE id=?", (ctx.state.clock(), p.id))
    users = [r["os_user"] for r in ctx.db.all("SELECT u.os_user FROM user_claude c JOIN hub_users u ON u.id=c.user_id WHERE c.account_id=? AND "
                                             "u.state IN ('active','suspended')", (p.id,))]
    for u in users:
        _push(ctx, u, tok)
    sync_alerts(ctx.state, "claude", p.id, set(), managed={"claude_token_old"})
    return {"rotated_users": users}


@op("claude.remove", group="claude", risk="high", params=IdP, target=lambda p: p.id)
def remove(ctx, p: IdP) -> dict[str, Any]:
    """Gỡ tài khoản khỏi kho (không còn user nào đang gán)."""
    if ctx.db.one("SELECT 1 FROM user_claude WHERE account_id=?", (p.id,)):
        raise OpError("conflict", "còn user đang dùng — đổi tài khoản cho họ trước")
    ctx.state.vault.delete(f"claude.{p.id}")
    ctx.db.exec("DELETE FROM claude_accounts WHERE id=?", (p.id,))
    return {"ok": True}


def check_ages(state: Any) -> None:
    for a in state.db.all("SELECT id FROM claude_accounts"):
        age = state.vault.age_days(f"claude.{a['id']}", state.clock())
        sync_alerts(state, "claude", a["id"], {"claude_token_old"} if age is not None and age > TOKEN_OLD_DAYS else set(),
                    managed={"claude_token_old"})
