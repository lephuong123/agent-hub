"""Khối "Tài khoản Claude" ở trang Cài đặt (docs/dev-notes/plan-claude-accounts.md — luồng web).

- Danh sách từ `account.list`: nhãn, huy hiệu "Đang dùng", chip trạng thái (ok / hết hạn mức tới HH:MM / lỗi đăng nhập / token sắp hết
  hạn), lần dùng cuối, chi phí hôm nay; nút Dùng / Đổi tên / Xoá / Kiểm tra mỗi dòng.
- "Thêm tài khoản": (1) nhập tên → `account.login_start` → lưu `{login_id, url}` trong `Session.extra` (RAM, theo phiên) → trang hiện link
  mở trang đăng nhập (tab mới) + ô dán code → (2) `account.login_finish`. Tab "Dán token" → `account.add_token`.
- Banner "Đổi sang tài khoản…" khi `hub.status` có `auth_block` hoặc cổng hạn mức (`rate_limit_gate`) đang đóng — chọn tài khoản + bấm
  → `account.use`. KHÔNG tự đổi: mọi lần đổi do người bấm (đã chốt với người dùng).

- "MCP của tài khoản" (plan-mcp-claude-native PR3): mỗi hàng token/machine — server MCP do claude quản lý trong cấu hình claude của tài
  khoản (`mcp.claude_status{cached}` — KHÔNG chạy claude khi tải trang; nút «Kiểm tra MCP» mới chạy `claude mcp list`), Đăng nhập (2 bước:
  link authorize + ô dán URL chuyển hướng, `mcp.claude_login_start|finish`) / Đăng xuất / Gỡ (xác nhận), form Thêm MCP (giá trị header ô
  password, không bao giờ hiện lại). URL chuyển hướng/giá trị header: `Action.secret_fields`, không vào flash/trang xác nhận/log.

Mọi thao tác ghi đi qua `ACTIONS` của app (CSRF + Origin + khoá phiên WT-1, `audit=True` kênh `web`); `account.*` (trừ `list`) ∈ HUMAN_ONLY
và daemon bọc audit (bỏ `token`, `code`). Token và code: ô `type=password`, không bao giờ hiện lại trên trang/trang xác nhận
(`Action.secret_fields`), không vào thông báo flash, không ghi log. Module chỉ gọi RPC — không chạm `accounts/`.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

from starlette.datastructures import FormData
from starlette.requests import Request

from ..rpc import DaemonUnavailable, RpcError
from . import present as P
from .app import rpc, session_of

LOGIN_KEY = "claude_login"
NEXT = "/settings/accounts"
LOGIN_TTL_S = 600.0
LABEL_MAX = 60
KIND_LABEL = {"machine": "Đăng nhập của máy", "custom": "Token tự đặt"}
# dòng phụ khi nhãn đã trùng loại: nói rõ tài khoản ảo này là gì thay vì lặp tên
KIND_HINT = {"machine": "đăng nhập sẵn của claude trên máy chạy hub", "custom": "đặt tay ở secret CLAUDE_CODE_OAUTH_TOKEN"}


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
    kl = KIND_LABEL.get(kind, "")
    if kl and kl == (a.get("label") or ""):  # tài khoản ảo có nhãn trùng loại ("Đăng nhập của máy") → không hiện hai lần
        kl = KIND_HINT.get(kind, "")
    cai = a.get("claudeai") if isinstance(a.get("claudeai"), dict) else None
    return {**a, "chip": label, "tone": tone, "kind_label": kl, "can_use": kind in ("token", "machine") and not a.get("active"),
            "mcp": None,  # accounts_ctx điền (mcp.claude_status cached)
            "can_rm": kind == "token" and not a.get("active"), "can_rename": kind == "token",  # core từ chối đổi tên machine/custom
            # phiên claude.ai (connector) RIÊNG của tài khoản — từ cache của account.list (không chạy claude mỗi dòng); custom: không có
            "cai": cai if kind in ("token", "machine") else None}


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
    cai = await claudeai_ctx(request)  # TRƯỚC account.list: kiểm phiên của `machine` → cache mới cho dòng "Đăng nhập của máy"
    try:
        res = await rpc(request, "account.list", {}) or {}
    except (RpcError, DaemonUnavailable, OSError):
        res = None
    rows = [_view(a) for a in (res or {}).get("accounts") or [] if isinstance(a, dict)] if res is not None else None
    for a in rows or []:
        if a.get("kind") in ("token", "machine"):
            a["mcp"] = await mcp_ctx(request, str(a["id"]))
    until = gate_closed(status)
    blocked = bool((status or {}).get("auth_block")) or until is not None
    others = [a for a in rows or [] if a["can_use"] and a.get("status") != "auth_error"]
    cur = (status or {}).get("account") or next(({"id": a["id"], "label": a.get("label")} for a in rows or [] if a.get("active")), None)
    return {"accounts": rows, "acct_active": (res or {}).get("active"), "acct_current": cur, "acct_login": _login(request),
            "acct_blocked": blocked, "acct_gate_until": _hhmm(until) if until else "", "acct_auth_block": bool((status or {}).get("auth_block")),
            "acct_switch": others, "claudeai": cai}


async def _chain_claudeai(request: Request, r: Any) -> bool:
    """Thêm tài khoản xong → mở luôn lượt gắn connector CHO TÀI KHOẢN VỪA THÊM (phiên claude.ai riêng của nó). Lỗi = bỏ qua
    (người bấm tay sau)."""
    aid = str((((r or {}) if isinstance(r, dict) else {}).get("account") or {}).get("id") or "")
    if not aid:
        return False
    try:
        await _cai_start(request, {"id": aid})
        return True
    except (RpcError, DaemonUnavailable, OSError):
        return False


MCP_STATUS = {"connected": ("đã kết nối", "ok"), "needs_auth": ("cần đăng nhập", "warn"), "failed": ("lỗi kết nối", "danger"),
              "unknown": ("chưa kiểm", "muted")}


async def mcp_ctx(request: Request, account_id: str) -> dict[str, Any] | None:
    """MCP do claude quản lý của một tài khoản (từ CACHE — không chạy claude khi tải trang). Daemon cũ → None (ẩn mục)."""
    try:
        st = await rpc(request, "mcp.claude_status", {"account_id": account_id, "cached": True}) or {}
    except (RpcError, DaemonUnavailable, OSError):
        return None
    servers = []
    for s in st.get("servers") or []:
        if not isinstance(s, dict):
            continue
        label, tone = MCP_STATUS.get(str(s.get("status")), MCP_STATUS["unknown"])
        servers.append({**s, "status_label": label, "status_tone": tone, "oauth": s.get("transport") in ("http", "sse")})
    logins = [lg for lg in st.get("logins") or [] if isinstance(lg, dict) and str(lg.get("url") or "").startswith("https://")]
    return {"servers": servers, "login": logins[0] if logins else None, "checked_at": st.get("checked_at"), "error": st.get("error"),
            "secrets_ok": True}  # header lưu trong cấu hình claude của tài khoản — agent bị chặn đọc (claude_files, luật claude_credentials)


async def claudeai_ctx(request: Request) -> dict[str, Any] | None:
    """Khối "Connector claude.ai" (Google Drive…): phiên `claude auth login` của user OS chạy hub. Daemon cũ → None (ẩn khối)."""
    try:
        st = await rpc(request, "account.claudeai_status", {}) or {}
    except (RpcError, DaemonUnavailable, OSError):
        return None
    lg = st.get("login") if isinstance(st.get("login"), dict) else None
    if lg and not str(lg.get("url") or "").startswith("https://"):  # chỉ link https vào href
        lg = None
    return {**st, "login": lg}


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
    return {**(r or {}), "cai_started": await _chain_claudeai(request, r)}


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
    return {**(r or {}), "cai_started": await _chain_claudeai(request, r)}


def _added_ok(r: Any, f: FormData) -> str:
    msg = f"Đã thêm tài khoản {_acct_label(r, f)} — bấm «Dùng» để chuyển sang"
    if isinstance(r, dict) and r.get("cai_started"):
        msg += ". Bước cuối: gắn connector — mở link, đăng nhập CÙNG tài khoản, dán code (hoặc «Bỏ qua»)"
    return msg


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


async def _cai_start(request: Request, p: dict[str, Any]) -> dict[str, Any]:
    r = await rpc(request, "account.claudeai_login_start", {"id": str(p.get("id") or "machine")}, audit=True) or {}
    if not str(r.get("url") or "").startswith("https://"):
        try:
            await rpc(request, "account.login_cancel", {"login_id": r.get("login_id")}, audit=True)
        except (RpcError, DaemonUnavailable, OSError):
            pass
        raise RpcError(-32000, "Không lấy được link đăng nhập hợp lệ từ claude — thử lại")
    return r


async def _cai_finish(request: Request, p: dict[str, Any]) -> dict[str, Any]:
    r = await rpc(request, "account.claudeai_login_finish", p, audit=True)
    p.clear()  # bỏ code khỏi bộ nhớ yêu cầu sớm nhất có thể
    return r


def _cai_ok(r: Any, f: FormData) -> str:
    who = (r or {}).get("email") or "tài khoản claude.ai"
    org = (r or {}).get("org")
    return (f"Đã gắn connector: claude.ai {who}" + (f" (tổ chức {org})" if org else "") + ". Khi tài khoản này đang dùng, run có "
            "connector chạy bằng phiên claude.ai này — hạn mức/chi phí của các run đó tính vào tài khoản claude.ai vừa đăng nhập")


def _cai_id(f: FormData) -> str:
    v = str(f.get("id") or "").strip() or "machine"
    if v != "machine" and not v.startswith("a_"):
        raise ValueError("tài khoản không hợp lệ")
    return v


def _cai_logout_confirm(f: FormData) -> str:
    return (f"Gỡ connector của tài khoản «{_who(f)}» (đăng xuất claude.ai của tài khoản này khỏi máy chạy hub)? Khi tài khoản này "
            "đang dùng, agent sẽ không dùng được connector (Google Drive…) cho tới khi gắn lại.")


# ---------------------------------------------------------------- MCP của tài khoản (mcp.claude_*)
_MCP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")


def _mcp_acct(f: FormData) -> str:
    v = str(f.get("account_id") or "").strip() or "machine"
    if v != "machine" and not re.match(r"^a_[0-9a-f]{8}$", v):
        raise ValueError("tài khoản không hợp lệ")
    return v


def _mcp_name(f: FormData) -> str:
    v = str(f.get("mcp_name") or "").strip()
    if not _MCP_NAME.match(v):
        raise ValueError("tên MCP: chữ, số, '-', '_' (≤ 63 ký tự)")
    return v


def _mcp_add_params(f: FormData) -> dict[str, Any]:
    url = str(f.get("url") or "").strip()
    if not re.match(r"^https?://\S+$", url) or len(url) > 2048:
        raise ValueError("url phải bắt đầu bằng https:// (hoặc http://)")
    tr = str(f.get("transport") or "http").strip()
    if tr not in ("http", "sse"):
        raise ValueError("transport phải là http hoặc sse")
    p: dict[str, Any] = {"account_id": _mcp_acct(f), "name": _mcp_name(f), "transport": tr, "url": url}
    hn = str(f.get("header_name") or "").strip()
    hv = str(f.get("header_value") or "")
    if hn or hv:
        if not _HEADER_NAME.match(hn) or not hv or "\n" in hv or "\r" in hv:  # thông điệp lỗi KHÔNG chứa giá trị đã nhập
            raise ValueError("header: cần cả tên (chữ, số, '-') và giá trị")
        p["headers"] = {hn: hv}
    return p


async def _mcp_add(request: Request, p: dict[str, Any]) -> dict[str, Any]:
    try:
        return await rpc(request, "mcp.claude_add", p, audit=True)
    finally:
        p.clear()  # bỏ url/header khỏi bộ nhớ yêu cầu sớm nhất có thể


def _mcp_add_ok(r: Any, f: FormData) -> str:
    r = r if isinstance(r, dict) else {}
    msg = f"Đã thêm MCP {r.get('name') or _mcp_name(f)} vào cấu hình claude của tài khoản {_who(f)}"
    if r.get("registered"):
        msg += " (đã đăng ký cho hub — gán cho agent ở trang MCP)"
    for w in r.get("warnings") or []:
        msg += f". {w}"
    if r.get("transport") in ("http", "sse"):
        msg += ". Server dùng OAuth: bấm «Đăng nhập» ở dòng của nó"
    return msg


async def _mcp_login_start(request: Request, p: dict[str, Any]) -> dict[str, Any]:
    r = await rpc(request, "mcp.claude_login_start", p, audit=True) or {}
    if not str(r.get("url") or "").startswith("https://"):
        try:
            await rpc(request, "mcp.claude_login_cancel", {"login_id": r.get("login_id")}, audit=True)
        except (RpcError, DaemonUnavailable, OSError):
            pass
        raise RpcError(-32000, "Không lấy được link đăng nhập hợp lệ từ claude — thử lại")
    return r


async def _mcp_login_finish(request: Request, p: dict[str, Any]) -> dict[str, Any]:
    try:
        return await rpc(request, "mcp.claude_login_finish", p, audit=True)
    finally:
        p.clear()


async def _mcp_login_cancel(request: Request, p: dict[str, Any]) -> dict[str, Any]:
    try:
        return await rpc(request, "mcp.claude_login_cancel", p, audit=True)
    except RpcError:
        return {"ok": True}


def _mcp_login_ok(r: Any, f: FormData) -> str:
    r = r if isinstance(r, dict) else {}
    st = MCP_STATUS.get(str(r.get("status")), MCP_STATUS["unknown"])[0]
    return f"Đã đăng nhập MCP {r.get('name') or ''}: {st}"


def _mcp_check_ok(r: Any, f: FormData) -> str:
    r = r if isinstance(r, dict) else {}
    if r.get("error"):
        return f"Kiểm tra MCP: {r['error']}"
    sv = r.get("servers") or []
    need = [s["name"] for s in sv if s.get("status") == "needs_auth"]
    return f"Kiểm tra MCP của tài khoản {_who(f)}: {len(sv)} server" + (f", cần đăng nhập: {', '.join(need)}" if need else "")


def _mcp_register_params(f: FormData) -> dict[str, Any]:
    """Đăng ký server có sẵn trong cấu hình claude của tài khoản cho hub (= `mcp.add{config:{source:"claude"}}` — daemon kiểm tên có
    trong cấu hình, lấy transport/host từ đó, từ chối server trỏ về hub)."""
    trust = "trusted" if str(f.get("trust") or "") in ("trusted", "on", "1", "yes") else "untrusted"
    return {"config": {"name": _mcp_name(f), "source": "claude", "trust": trust}, "account_id": _mcp_acct(f)}


def _mcp_register_confirm(f: FormData) -> str:
    trusted = str(f.get("trust") or "") in ("trusted", "on", "1", "yes")
    return (f"Đăng ký MCP «{_mcp_name(f)}» (cấu hình claude của tài khoản «{_who(f)}») cho hub? Sau đó gán cho agent ở trang MCP / agent. "
            + ("Tin cậy: tool ghi của server chạy không hỏi (vẫn ghi audit)." if trusted
               else "Không tin cậy: tool ghi của server phải chờ duyệt."))


def _mcp_register_ok(r: Any, f: FormData) -> str:
    r = r if isinstance(r, dict) else {}
    tr = "tin cậy" if r.get("trust") == "trusted" else "không tin cậy"
    return f"Đã đăng ký MCP {r.get('name') or _mcp_name(f)} cho hub (nguồn claude, {tr}) — gán cho agent ở trang MCP"


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
                                             _added_ok, call=_login_finish, secret_fields=("code",))
    actions["account_login_cancel"] = Action("account.login_cancel", lambda f: {"login_id": _req(f, "login_id", "lượt đăng nhập")},
                                             "Đã huỷ lượt đăng nhập", call=_login_cancel)
    # phiên claude.ai cho connector (`claude auth login` của user OS chạy hub) — người dùng TỰ đăng nhập trên web hub của mình
    actions["claudeai_login_start"] = Action("account.claudeai_login_start", lambda f: {"id": _cai_id(f)},
                                             "Gắn connector — bấm «Mở trang đăng nhập», đăng nhập tài khoản claude.ai, rồi dán code vào ô bên dưới",
                                             call=_cai_start)
    actions["claudeai_login_finish"] = Action("account.claudeai_login_finish", lambda f: {"login_id": _req(f, "login_id", "lượt đăng nhập"),
                                                                                         "code": _secret(f, "code", "code")},
                                              _cai_ok, call=_cai_finish, secret_fields=("code",))
    actions["claudeai_login_cancel"] = Action("account.login_cancel", lambda f: {"login_id": _req(f, "login_id", "lượt đăng nhập")},
                                              "Đã bỏ qua gắn connector", call=_login_cancel)
    actions["claudeai_logout"] = Action("account.claudeai_logout", lambda f: {"id": _cai_id(f)},
                                        lambda r, f: f"Đã gỡ connector của tài khoản {_who(f)} (đăng xuất claude.ai)",
                                        confirm=_cai_logout_confirm, danger=True)
    # MCP của tài khoản (mcp.claude_* ∈ HUMAN_ONLY; ghi được daemon bọc audit — chỉ name/account_id/transport/login_id)
    actions["mcp_claude_add"] = Action("mcp.claude_add", _mcp_add_params, _mcp_add_ok, call=_mcp_add, secret_fields=("header_value", "url"))
    actions["mcp_claude_rm"] = Action("mcp.claude_remove", lambda f: {"account_id": _mcp_acct(f), "name": _mcp_name(f)},
                                      lambda r, f: f"Đã gỡ MCP {_mcp_name(f)} khỏi tài khoản {_who(f)}"
                                      + (" (registry giữ nguyên: tài khoản khác còn server này)" if (r or {}).get("registry") == "kept" else ""),
                                      danger=True, confirm=lambda f: f"Gỡ MCP «{_mcp_name(f)}» khỏi cấu hình claude của tài khoản «{_who(f)}»? "
                                      "Đăng nhập của server này trên tài khoản đó cũng mất; agent đang gán sẽ không dùng được nó khi tài khoản này đang dùng.")
    actions["mcp_claude_login_start"] = Action("mcp.claude_login_start", lambda f: {"account_id": _mcp_acct(f), "name": _mcp_name(f)},
                                               "Đã mở lượt đăng nhập MCP — bấm «Mở trang đăng nhập», đồng ý cấp quyền, rồi dán URL chuyển hướng",
                                               call=_mcp_login_start)
    actions["mcp_claude_login_finish"] = Action("mcp.claude_login_finish",
                                                lambda f: {"login_id": _req(f, "login_id", "lượt đăng nhập"),
                                                           "redirect_url": _secret(f, "redirect_url", "URL chuyển hướng")},
                                                _mcp_login_ok, call=_mcp_login_finish, secret_fields=("redirect_url",))
    actions["mcp_claude_login_cancel"] = Action("mcp.claude_login_cancel", lambda f: {"login_id": _req(f, "login_id", "lượt đăng nhập")},
                                                "Đã huỷ lượt đăng nhập MCP", call=_mcp_login_cancel)
    actions["mcp_claude_logout"] = Action("mcp.claude_logout", lambda f: {"account_id": _mcp_acct(f), "name": _mcp_name(f)},
                                          lambda r, f: f"Đã đăng xuất MCP {_mcp_name(f)} (tài khoản {_who(f)})", danger=True,
                                          confirm=lambda f: f"Đăng xuất MCP «{_mcp_name(f)}» của tài khoản «{_who(f)}»? Agent không dùng được "
                                          "server này (khi tài khoản đó đang dùng) tới khi đăng nhập lại.")
    actions["mcp_claude_register"] = Action("mcp.add", _mcp_register_params, _mcp_register_ok, confirm=_mcp_register_confirm)
    actions["mcp_claude_check"] = Action("mcp.claude_status", lambda f: {"account_id": _mcp_acct(f), "refresh": True}, _mcp_check_ok, audit=False)
    actions["account_add_token"] = Action("account.add_token", lambda f: {"label": _label(f), "token": _secret(f, "token", "token")},
                                          _added_ok, call=_add_token, secret_fields=("token",))
