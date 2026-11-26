"""Thẻ "Tài khoản ChatGPT (Codex)" và "Tài khoản Grok" ở Cài đặt → Tài khoản AI — NHIỀU tài khoản mỗi runtime, như "Tài khoản Claude".

Hub không nhận/không giữ token: mỗi tài khoản là thư mục phiên của chính CLI (runtime/accounts.py). Trang hiện NGAY trạng thái đã
cache (email, gói, workspace, hạn mức); daemon kiểm lại ngầm khi cũ — thẻ tự làm mới 3 s/lần (htmx, `/partials/runtime-accounts`)
chỉ khi đang kiểm hoặc đang chờ người xác nhận đăng nhập. Thao tác: Dùng, Kiểm tra, Đăng nhập lại, Đăng xuất, Xoá, Thêm tài khoản
(link + mã một lần do CLI cấp), cấu hình đường dẫn CLI / model / effort (`config.set`, chỉ người).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

import logging

from ..rpc import DaemonUnavailable, RpcError
from .app import rpc, session_of, ws

log = logging.getLogger("cfharness.web.runtime_accounts")
KINDS = ("codex", "grok")
LABEL = {"codex": "ChatGPT (Codex)", "grok": "Grok"}
EFFORTS = ("low", "medium", "high", "xhigh")
LABEL_MAX = 60
PLAN_LABEL = {"team": "Business", "business": "Business", "free": "Free", "plus": "Plus", "pro": "Pro", "go": "Go",
              "enterprise": "Enterprise", "edu": "Edu"}


def _kind(f: FormData) -> str:
    k = str(f.get("kind") or "")
    if k not in KINDS:
        raise ValueError("runtime không hợp lệ")
    return k


def _acct(f: FormData) -> str:
    a = str(f.get("id") or "")
    if not a or len(a) > 20:
        raise ValueError("thiếu tài khoản")
    return a


def _window(w: dict[str, Any] | None) -> str | None:
    if not isinstance(w, dict) or w.get("used_percent") is None:
        return None
    mins = int(w.get("window_mins") or 0)
    span = "5 giờ" if mins == 300 else "tuần" if mins == 10080 else f"{mins // 60} giờ" if mins >= 60 else f"{mins} phút"
    return f"{span} {w['used_percent']}%"


def _iso(ts: Any) -> str | None:
    """`checked_at` (epoch giây từ daemon) → ISO — macro `m.rel` của template chỉ nhận chuỗi ISO (epoch làm vỡ trang)."""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat() if ts else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _acct_view(a: dict[str, Any]) -> dict[str, Any]:
    a["checked_at"] = _iso(a.get("checked_at"))
    rl = a.get("rate_limits") or {}
    a["limits"] = [x for x in (_window(rl.get("primary")), _window(rl.get("secondary"))) if x]
    a["plan_label"] = PLAN_LABEL.get(str(a.get("plan") or ""), a.get("plan"))
    if a.get("logged_in"):
        a["chip"], a["tone"] = "Đã đăng nhập", "ok"
    elif a.get("logged_in") is False:
        a["chip"], a["tone"] = "Chưa đăng nhập", "warn"
    elif a.get("checking") or not a.get("checked_at"):
        a["chip"], a["tone"] = "Đang kiểm…", "muted"
    else:
        a["chip"], a["tone"] = "Không rõ", "muted"
    return a


def _card(c: dict[str, Any]) -> dict[str, Any]:
    c["accounts"] = [_acct_view(a) for a in c.get("accounts") or []]
    lg = c.get("login") or {}
    err = str(lg.get("error") or "")
    c["device_disabled"] = lg.get("state") == "failed" and "device" in err.lower()
    c["busy"] = lg.get("state") == "pending" or any(a.get("checking") for a in c["accounts"])
    return c


async def ctx(request: Request) -> dict[str, Any]:
    try:
        res = await rpc(request, "runtime_account.list", {}) or {}
        cards = [_card(res[k]) for k in KINDS if isinstance(res, dict) and isinstance(res.get(k), dict)]
        err = None
    except (RpcError, DaemonUnavailable, OSError) as e:
        cards, err = [], f"Daemon chưa hỗ trợ tài khoản ChatGPT/Grok (cập nhật cfharness rồi khởi động lại daemon): {e}"
    except Exception as e:  # noqa: BLE001 — best-effort: thẻ phụ KHÔNG được làm hỏng cả trang Tài khoản (khối Claude vẫn dùng được)
        log.exception("thẻ tài khoản ChatGPT/Grok lỗi")
        cards, err = [], f"Không hiển thị được tài khoản ChatGPT/Grok: {type(e).__name__}"
    sess = session_of(request)
    return {"rt_cards": cards, "rt_error": err, "rt_busy": any(c["busy"] for c in cards), "efforts": EFFORTS,
            "csrf": sess.csrf if sess else ""}


async def partial(request: Request) -> Response:
    return ws(request).templates.TemplateResponse(request, "_runtime_accounts.html", await ctx(request))


def routes() -> list[Route]:
    return [Route("/partials/runtime-accounts", partial)]


async def _configure(request: Request, p: dict[str, Any]) -> Any:
    """Ghi `runtime.<kind>.binary|model|effort` qua `config.set` (chỉ người, có audit). Trống = về mặc định."""
    k = p["kind"]
    cur = await rpc(request, "config.get", {"key": f"runtime.{k}.binary"})
    if (cur or k) != p["binary"]:
        from .file_pages import is_local_request

        if not is_local_request(request):  # đổi chương trình hub sẽ chạy — như khoá bảo vệ: chỉ trên máy chạy hub
            raise RpcError(-32003, "Đường dẫn CLI chỉ đổi được khi dùng web trên chính máy chạy hub (không qua link public).")
    for key in ("binary", "model", "effort"):
        await rpc(request, "config.set", {"key": f"runtime.{k}.{key}", "value": p[key]}, audit=True)
    return await rpc(request, "runtime_account.refresh", {"kind": k}) or {}


def _cfg_params(f: FormData) -> dict[str, Any]:
    k = _kind(f)
    b = str(f.get("binary") or "").strip()
    model = str(f.get("model") or "").strip()
    eff = str(f.get("effort") or "").strip()
    if eff and eff not in EFFORTS:
        raise ValueError(f"effort phải là một trong {', '.join(EFFORTS)}")
    if model and (len(model) > 80 or any(c.isspace() for c in model)):
        raise ValueError("model không hợp lệ")
    return {"kind": k, "binary": b or k, "model": model or None, "effort": eff or None}


def _login_params(f: FormData) -> dict[str, Any]:
    # ô form tên `via` (KHÔNG `method`: input tên "method" che `form.method` → JS gắn khoá phiên lỗi, POST bị từ chối)
    p: dict[str, Any] = {"kind": _kind(f), "method": "browser" if f.get("via") == "browser" else "device"}
    if f.get("new") == "1":
        label = " ".join(str(f.get("label") or "").split())
        if not label:
            raise ValueError("đặt tên cho tài khoản (vd Công ty, Cá nhân)")
        if len(label) > LABEL_MAX:
            raise ValueError(f"tên tài khoản tối đa {LABEL_MAX} ký tự")
        p["label"] = label
    else:
        p["id"] = _acct(f)
    return p


def _login_ok(r: Any, f: FormData) -> str:
    lg = r if isinstance(r, dict) else {}
    if lg.get("state") == "failed":
        return f"Không mở được lượt đăng nhập: {lg.get('error') or 'lỗi không rõ'}"
    return "Đã mở lượt đăng nhập — bấm link, đăng nhập tài khoản của bạn, nhập mã nếu được hỏi; trang tự cập nhật khi xong"


def _who(f: FormData) -> str:
    return str(f.get("name") or f.get("id") or "")


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    actions["rtacct_login"] = Action("runtime_account.login_start", _login_params, _login_ok)
    actions["rtacct_cancel"] = Action("runtime_account.login_cancel", lambda f: {"kind": _kind(f)}, "Đã huỷ lượt đăng nhập")
    actions["rtacct_use"] = Action(
        "runtime_account.use", lambda f: {"kind": _kind(f), "id": _acct(f)},
        lambda r, f: f"Agent {LABEL[_kind(f)]} giờ chạy bằng tài khoản «{_who(f)}»",
        confirm=lambda f: f"Dùng tài khoản «{_who(f)}» cho mọi agent chạy {LABEL[_kind(f)]}? Run mới dùng tài khoản này; run đang chạy giữ tài khoản cũ.")
    actions["rtacct_logout"] = Action(
        "runtime_account.logout", lambda f: {"kind": _kind(f), "id": _acct(f)}, lambda r, f: f"Đã đăng xuất «{_who(f)}»", danger=True,
        confirm=lambda f: f"Đăng xuất tài khoản {LABEL[_kind(f)]} «{_who(f)}»? Agent dùng tài khoản này sẽ không chạy được tới khi đăng nhập lại.")
    actions["rtacct_rm"] = Action(
        "runtime_account.rm", lambda f: {"kind": _kind(f), "id": _acct(f)}, lambda r, f: f"Đã xoá tài khoản «{_who(f)}»", danger=True,
        confirm=lambda f: f"Xoá tài khoản {LABEL[_kind(f)]} «{_who(f)}» khỏi hub? Phiên đăng nhập (thư mục riêng của hub) bị xoá; "
                          "muốn dùng lại phải đăng nhập lần nữa.")
    actions["rtacct_refresh"] = Action("runtime_account.refresh", lambda f: {"kind": _kind(f), "id": str(f.get("id") or "") or None},
                                       lambda r, f: f"Đang kiểm lại {LABEL[_kind(f)]} — kết quả hiện sau vài giây", audit=False)
    actions["rtacct_config"] = Action("config.set", _cfg_params, lambda r, f: f"Đã lưu cấu hình {LABEL[_kind(f)]}", call=_configure)
