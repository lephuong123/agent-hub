"""Thẻ "Gói chẩn đoán" ở Cài đặt + trang xem trước `/support` (support_bundle.py, L7 — docs/dev-notes/plan-logging.md B7).

- Đọc: RPC `support.list` (gói đã có) và `support.plan` (kế hoạch: file + cỡ + số chỗ đã che — chỉ đọc, HUMAN_ONLY).
- Ghi: `ACTIONS` `support_create` (xác nhận) và `support_create_raw` (xác nhận + BƯỚC 2 bắt buộc, gửi `confirm_raw` — Q5); RPC `support.create`
  tự audit kênh `web`.
- Tải về: CÙNG đường của gói chuyển hub (`transfer_pages.mint_download/take_download/stream_file`): `POST /support/download` (post_guard)
  cấp token MỘT lần gắn phiên, hết hạn 10 phút, kho riêng `support_tokens` → `GET /support/download/<token>` stream đúng
  `exports/support-*.tar.gz` (tên khớp `support_bundle.NAME_RE`, `O_NOFOLLOW`). Không bao giờ qua `/static`.
"""

from __future__ import annotations

from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from .. import support_bundle as SB
from ..rpc import DaemonUnavailable, RpcError
from .app import err_text, flash_redirect, post_guard, render, rpc, session_of, ws
from .transfer_pages import mint_download, stream_file, take_download

SINCE_CHOICES = (("1h", "1 giờ"), ("6h", "6 giờ"), ("24h", "24 giờ"), ("3d", "3 ngày"), ("7d", "7 ngày"))
PLAN_TIMEOUT_S = 600.0
CREATE_TIMEOUT_S = 1800.0
NS = "support"


def _size(n: Any) -> str:
    from .backup_pages import fmt_size

    return fmt_size(n)


def _since(v: Any) -> str:
    s = str(v or "24h")
    return s if s in dict(SINCE_CHOICES) else "24h"


def _runs(v: Any) -> str:
    """Ô run_id (cách nhau bởi khoảng trắng/dấu phẩy) — chỉ giữ token đúng dạng run_id, tối đa MAX_RUNS."""
    import re

    ids = [x for x in re.split(r"[\s,]+", str(v or "")) if SB.RUN_ID_RE.match(x)]
    return " ".join(list(dict.fromkeys(ids))[: SB.MAX_RUNS])


async def partial(request: Request) -> Response:
    ctx: dict[str, Any]
    try:
        ctx = {"data": await rpc(request, "support.list")}
    except (DaemonUnavailable, OSError):
        ctx = {"down": True}
    except RpcError as e:
        ctx = {"error": err_text(e)}
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_support.html", {**ctx, "csrf": sess.csrf if sess else "", "size": _size,
                                                                             "since_choices": SINCE_CHOICES})


async def page(request: Request) -> Response:
    q = request.query_params
    since, runs, raw = _since(q.get("since")), _runs(q.get("runs")), q.get("include_raw") == "1" and bool(_runs(q.get("runs")))
    ctx: dict[str, Any] = {"since": since, "runs": runs, "include_raw": raw, "size": _size, "since_choices": SINCE_CHOICES}
    try:
        ctx["plan"] = await ws(request).client.call("support.plan", {"since": since, "runs": runs.split(), "include_raw": raw},
                                                    timeout=PLAN_TIMEOUT_S)
    except (DaemonUnavailable, OSError):
        ctx["down"] = True
    except RpcError as e:
        ctx["error"] = err_text(e)
    return await render(request, "support.html", {"nav": "settings_support", **ctx})


async def download_link(request: Request) -> Response:
    sess, form = await post_guard(request)
    name = str(form.get("name") or "")
    if not SB.NAME_RE.match(name):
        return flash_redirect(request, "/settings/support", "error", "Tên gói chẩn đoán không hợp lệ")
    return RedirectResponse(f"/support/download/{mint_download(request, sess, name, NS)}", status_code=303)


async def download(request: Request) -> Response:
    name = take_download(request, NS)
    if not SB.NAME_RE.match(name):  # pragma: no cover — đã kiểm lúc cấp token
        raise HTTPException(404, "Gói không hợp lệ")
    return stream_file(SB.bundle_path(ws(request).paths, name), name)


# ============================================================================ thao tác (ACTIONS)
def _build(raw: bool):
    def build(f: Any) -> dict[str, Any]:
        runs = _runs(f.get("runs")).split()
        if raw and not runs:
            raise ValueError("raw transcript cần ít nhất một run_id")
        p: dict[str, Any] = {"since": _since(f.get("since")), "runs": runs, "include_raw": raw}
        if raw:
            p["confirm_raw"] = True  # chỉ tới được RPC sau bước 2 (plan_action)
        return p

    return build


async def _create(request: Request, p: dict[str, Any]) -> Any:
    return await ws(request).client.call("support.create", p, audit=True, timeout=CREATE_TIMEOUT_S)


def _ok(r: Any, f: Any) -> str:
    r = r or {}
    rep = r.get("redaction_report") or {}
    return (f"Đã tạo gói chẩn đoán {r.get('name')} ({_size(r.get('size'))}, {r.get('files')} file, che {rep.get('total', 0)} chỗ) — "
            "bấm \"Tải về\" ở thẻ Gói chẩn đoán")


def _confirm(f: Any) -> str:
    return ("Tạo gói chẩn đoán?\nGói gồm log hệ thống (đã che secret), hub doctor, trạng thái hub, cấu hình đã lọc"
            + (" và dòng thời gian (metadata) của run đã chọn" if _runs(f.get("runs")) else "") + ". Không có hub.db, secret, đăng nhập web, "
            "memory/skill. Chỉ gửi gói cho người bạn tin.")


def _step2(f: Any) -> str:
    return ("KÈM RAW TRANSCRIPT: gói sẽ chứa NỘI DUNG hội thoại (prompt, trả lời, input/output tool) của run "
            f"{_runs(f.get('runs')) or '?'}. Secret đã biết được che; chữ còn lại giữ nguyên. Chỉ tiếp tục khi người nhận cần đọc hội thoại.")


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    actions["support_create"] = Action("support.create", _build(False), _ok, confirm=_confirm, call=_create,
                                       goto=lambda r, f: "/settings/support")
    actions["support_create_raw"] = Action("support.create", _build(True), _ok, confirm=_confirm, step2=_step2, danger=True, call=_create,
                                           goto=lambda r, f: "/settings/support")


def routes() -> list[Any]:
    return [Route("/partials/support", partial), Route("/support", page), Route("/support/download", download_link, methods=["POST"]),
            Route("/support/download/{token}", download)]


__all__ = ["register_actions", "routes"]
