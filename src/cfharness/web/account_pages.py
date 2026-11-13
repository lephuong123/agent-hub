"""Khối "Tài khoản Claude" ở trang Cài đặt (docs/dev-notes/plan-claude-accounts.md — luồng web).

- Danh sách từ `account.list`: nhãn, huy hiệu "Đang dùng", chip trạng thái (ok / hết hạn mức tới HH:MM / lỗi đăng nhập / token sắp hết
  hạn), lần dùng cuối, chi phí hôm nay; nút Dùng / Đổi tên / Xoá / Kiểm tra mỗi dòng.
- "Thêm tài khoản": (1) nhập tên → `account.login_start` → lưu `{login_id, url}` trong `Session.extra` (RAM, theo phiên) → trang hiện link
  mở trang đăng nhập (tab mới) + ô dán code → (2) `account.login_finish`. Tab "Dán token" → `account.add_token`.
- Banner "Đổi sang tài khoản…" khi `hub.status` có `auth_block` hoặc cổng hạn mức (`rate_limit_gate`) đang đóng — chọn tài khoản + bấm
  → `account.use`. KHÔNG tự đổi: mọi lần đổi do người bấm (đã chốt với người dùng).

Mọi thao tác ghi đi qua `ACTIONS` của app (CSRF + Origin + khoá phiên WT-1, `audit=True` kênh `web`); `account.*` (trừ `list`) ∈ HUMAN_ONLY
và daemon bọc audit (bỏ `token`, `code`). Token và code: ô `type=password`, không bao giờ hiện lại trên trang/trang xác nhận
(`Action.secret_fields`), không vào thông báo flash, không ghi log. Module chỉ gọi RPC — không chạm `accounts/`.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from starlette.datastructures import FormData
from starlette.requests import Request

from ..rpc import DaemonUnavailable, RpcError
from . import present as P
from .app import rpc, session_of

LOGIN_KEY = "claude_login"
NEXT = "/settings#accounts"
LOGIN_TTL_S = 600.0
LABEL_MAX = 60
KIND_LABEL = {"machine": "Đăng nhập của máy", "custom": "Token tự đặt"}


# ============================================================================ trình bày
def _hhmm(ts: Any) -> str:
    t = P.parse_ts(ts) if isinstance(ts, str) else (float(ts) if isinstance(ts, (int, float)) else None)
    if t is None:
        return ""
    d = datetime.fromtimestamp(t)
    return d.strftime("%H:%M") if d.date() == datetime.now().date() else d.strftime("%d/%m %H:%M")


def _date(ts: Any) -> str:
    t = P.parse_ts(ts) if isinstance(ts, str) else None
    return datetime.fromtimestamp(t).strftime("%d/%m/%Y") if t is not None else ""


def status_chip(a: dict[str, Any]) -> tuple[str, str]:
    """(nhãn, tông pill) cho trạng thái tài khoản."""
    st = a.get("status")
    if st == "ok":
        return "ok", "ok"
    if st == "rate_limited":
        until = _hhmm(((a.get("rate_limit") or {}) if isinstance(a.get("rate_limit"), dict) else {}).get("until"))
        return ("hết hạn mức tới " + until) if until else "hết hạn mức", "warn"
    if st == "auth_error":
        return "lỗi đăng nhập", "danger"
    if st == "expiring":
        d = _date(a.get("expires_at"))
        return "token sắp hết hạn" + (f" ({d})" if d else ""), "warn"
    return "chưa kiểm", "muted"


def _view(a: dict[str, Any]) -> dict[str, Any]:
    label, tone = status_chip(a)
    kind = a.get("kind") or "token"
    return {**a, "chip": label, "tone": tone, "kind_label": KIND_LABEL.get(kind, ""), "can_use": kind in ("token", "machine") and not a.get("active"),
            "can_rm": kind == "token" and not a.get("active"), "can_rename": kind == "token"}  # core từ chối đổi tên machine/custom


def gate_closed(status: dict[str, Any] | None, now: float | None = None) -> str | None:
    """`rate_limit_gate` của tài khoản đang dùng còn đóng → `until` (ISO), không thì None."""
    g = (status or {}).get("rate_limit_gate")
    if not isinstance(g, dict) or not g.get("until"):
        return None
    t = P.parse_ts(str(g["until"]))
    return str(g["until"]) if t is not None and t > (now or time.time()) else None


def _login(request: Request) -> dict[str, Any] | None:
    """Lượt đăng nhập phiên này vừa mở (chưa hết hạn)."""
    sess = session_of(request)
    if sess is None:
        return None
    lg = sess.extra.get(LOGIN_KEY)
    if not lg:
        return None
    if float(lg.get("exp") or 0) < time.time():
        sess.extra.pop(LOGIN_KEY, None)
        return None
    return lg


async def accounts_ctx(request: Request, status: dict[str, Any] | None) -> dict[str, Any]:
    """Ngữ cảnh khối "Tài khoản Claude" cho settings.html. Daemon chưa có `account.*` → `accounts=None` (khối hiện hướng dẫn)."""
    try:
        res = await rpc(request, "account.list", {}) or {}
    except (RpcError, DaemonUnavailable, OSError):
        res = None
    rows = [_view(a) for a in (res or {}).get("accounts") or [] if isinstance(a, dict)] if res is not None else None
    until = gate_closed(status)
    blocked = bool((status or {}).get("auth_block")) or until is not None
    others = [a for a in rows or [] if a["can_use"] and a.get("status") != "auth_error"]
    cur = (status or {}).get("account") or next(({"id": a["id"], "label": a.get("label")} for a in rows or [] if a.get("active")), None)
    return {"accounts": rows, "acct_active": (res or {}).get("active"), "acct_current": cur, "acct_login": _login(request),
            "acct_blocked": blocked, "acct_gate_until": _hhmm(until) if until else "", "acct_auth_block": bool((status or {}).get("auth_block")),
            "acct_switch": others}


# ============================================================================ thao tác
def _label(f: FormData) -> str:
    v = " ".join(str(f.get("label") or "").split())
    if not v:
        raise ValueError("thiếu tên tài khoản")
    if len(v) > LABEL_MAX:
        raise ValueError(f"tên tài khoản tối đa {LABEL_MAX} ký tự")
    return v


def _id(f: FormData) -> str:
    v = str(f.get("id") or "").strip()
    if not v:
        raise ValueError("thiếu tài khoản")
    return v


def _secret(f: FormData, key: str, what: str) -> str:
    v = str(f.get(key) or "").strip()
    if not v:
        raise ValueError(f"thiếu {what}")
    if any(c.isspace() for c in v):  # thông điệp lỗi KHÔNG chứa giá trị đã dán
        raise ValueError(f"{what} không được chứa khoảng trắng")
    return v


def _who(f: FormData) -> str:
    return str(f.get("name") or f.get("label") or f.get("id") or "?")


async def _login_start(request: Request, p: dict[str, Any]) -> dict[str, Any]:
    r = await rpc(request, "account.login_start", p, audit=True) or {}
    url = str(r.get("url") or "")
    if not url.startswith("https://"):  # chỉ link https — không đưa `javascript:`/link lạ vào href
        try:
            await rpc(request, "account.login_cancel", {"login_id": r.get("login_id")}, audit=True)
        except (RpcError, DaemonUnavailable, OSError):
            pass
        raise RpcError(-32000, "Không lấy được link đăng nhập hợp lệ từ claude — thử lại")
    exp = P.parse_ts(r["expires_at"]) if isinstance(r.get("expires_at"), str) else None
    sess = session_of(request)
    if sess is not None:
        sess.extra[LOGIN_KEY] = {"login_id": str(r.get("login_id") or ""), "url": url, "label": p.get("label"),
                                 "exp": exp if exp is not None else time.time() + LOGIN_TTL_S}
    return r


async def _login_finish(request: Request, p: dict[str, Any]) -> dict[str, Any]:
    r = await rpc(request, "account.login_finish", p, audit=True)
    p.clear()  # bỏ code khỏi bộ nhớ yêu cầu sớm nhất có thể
    sess = session_of(request)
    if sess is not None:
        sess.extra.pop(LOGIN_KEY, None)
    return r


async def _login_cancel(request: Request, p: dict[str, Any]) -> dict[str, Any]:
    sess = session_of(request)
    if sess is not None:
        sess.extra.pop(LOGIN_KEY, None)
    try:
        return await rpc(request, "account.login_cancel", p, audit=True)
    except RpcError:
        return {"ok": True}  # lượt đã hết hạn/không còn ở daemon — phía web đã quên, coi như huỷ xong


async def _add_token(request: Request, p: dict[str, Any]) -> dict[str, Any]:
    r = await rpc(request, "account.add_token", p, audit=True)
    p.clear()
    return r


def _acct_label(r: Any, f: FormData) -> str:
    return str(((r or {}).get("account") or {}).get("label") or f.get("label") or "")


def _check_ok(r: Any, f: FormData) -> str:
    a = (r or {}).get("account") or {}
    chip, _ = status_chip(a)
    err = f" — {a['last_error']}" if a.get("last_error") and a.get("status") != "ok" else ""
    return f"Kiểm tra {a.get('label') or _who(f)}: {chip}{err}"


def _use_ok(r: Any, f: FormData) -> str:
    msg = f"Đã chuyển sang tài khoản {_who(f)} — run mới dùng tài khoản này, việc đang chờ hạn mức sẽ chạy lại"
    saved = r.get("saved_custom") if isinstance(r, dict) else None
    if saved:
        msg += f". Token tự đặt trước đó đã được lưu thành tài khoản «{saved.get('label')}»"
    return msg


def _use_confirm(f: FormData) -> str:
    return (f"Chuyển sang tài khoản Claude «{_who(f)}»?\nRun MỚI (và việc đang chờ hạn mức / chờ đăng nhập) dùng tài khoản này; "
            "run đang chạy giữ tài khoản cũ tới khi xong.")


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    """Thêm thao tác tài khoản Claude vào `ACTIONS` (form POST `/do/<tên>`). `account.*` ghi ∈ HUMAN_ONLY, daemon ghi audit kênh `web`."""
    actions["account_use"] = Action("account.use", lambda f: {"id": _id(f)}, _use_ok, confirm=_use_confirm)
    actions["account_rename"] = Action("account.rename", lambda f: {"id": _id(f), "label": _label(f)},
                                       lambda r, f: f"Đã đổi tên tài khoản thành {_acct_label(r, f)}")
    actions["account_rm"] = Action("account.rm", lambda f: {"id": _id(f)}, lambda r, f: f"Đã xoá tài khoản {_who(f)}", danger=True,
                                   confirm=lambda f: f"Xoá tài khoản Claude «{_who(f)}»? Token của tài khoản này bị xoá khỏi secret store; "
                                                     "muốn dùng lại phải đăng nhập lần nữa.")
    actions["account_check"] = Action("account.check", lambda f: {"id": _id(f)}, _check_ok)
    actions["account_login_start"] = Action("account.login_start", lambda f: {"label": _label(f)},
                                            "Đã mở lượt đăng nhập — bấm «Mở trang đăng nhập», đăng nhập, rồi dán code vào ô bên dưới",
                                            call=_login_start)
    actions["account_login_finish"] = Action("account.login_finish", lambda f: {"login_id": _req(f, "login_id", "lượt đăng nhập"),
                                                                               "code": _secret(f, "code", "code")},
                                             lambda r, f: f"Đã thêm tài khoản {_acct_label(r, f)} — bấm «Dùng» để chuyển sang",
                                             call=_login_finish, secret_fields=("code",))
    actions["account_login_cancel"] = Action("account.login_cancel", lambda f: {"login_id": _req(f, "login_id", "lượt đăng nhập")},
                                             "Đã huỷ lượt đăng nhập", call=_login_cancel)
    actions["account_add_token"] = Action("account.add_token", lambda f: {"label": _label(f), "token": _secret(f, "token", "token")},
                                          lambda r, f: f"Đã thêm tài khoản {_acct_label(r, f)} — bấm «Dùng» để chuyển sang",
                                          call=_add_token, secret_fields=("token",))
