"""Trang "Chuyển hub sang máy khác" (`/hub-transfer`) — xuất VÀ nhập toàn bộ hub trên trình duyệt (PR6, plan-hub-clone.md §7b).

Web vẫn là client thuần của daemon cho mọi QUYẾT ĐỊNH (RPC `hub.*` của `hub_transfer_web.py`, đều HUMAN_ONLY). Hai ngoại lệ có chủ đích,
chỉ đụng FILE GÓI (không bao giờ `hub.db`), vì gói hàng GB không đi qua JSON-RPC được:
- Tải lên: `POST /hub-transfer/upload` nhận THÂN THÔ (XHR `transfer.js`, `application/octet-stream`), CSRF + khoá phiên qua header
  (`X-CSRF-Token`, `X-Ahub-Key`) + kiểm Origin như `post_guard`; kiểm `Content-Length` ≤ `transfer.max_upload_mb` TRƯỚC khi đọc thân, ghi
  từng khúc xuống `HUB_HOME/imports/.<id>.cfhx.part` (0600, O_EXCL) → rename; quá trần / thiếu byte / lỗi → xoá file dở. Không đọc cả gói vào RAM.
- Tải về: gói do DAEMON ghi ở `HUB_HOME/exports/<id>.cfhx` (id lấy từ `hub.export_status`, khớp `hub_transfer.ID_RE`). `POST /hub-transfer/download`
  (post_guard) cấp token ngẫu nhiên MỘT LẦN trong RAM của tiến trình web (gắn phiên, hết hạn 10 phút) → `GET /hub-transfer/download/<token>`
  stream file (`O_NOFOLLOW`, file thường) với `Content-Disposition: attachment` + `Cache-Control: no-store`; dùng lại / phiên khác / hết hạn → 404.
  Không bao giờ qua `/static`.
Passphrase: KHÔNG có trong form trang chủ; chỉ nhập ở BƯỚC 2 (`Action.reenter`, ô password trống) — POST đó gửi thẳng tới RPC; trang xác
nhận không mang passphrase qua `carry()` (`secret_fields`), không vào HTML/log/audit (daemon gỡ khoá `passphrase` trước audit).
"""

from __future__ import annotations

import os
import secrets
import stat
import time
from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from .. import hub_transfer as T
from ..hub_transfer_web import DEFAULT_MAX_UPLOAD_MB, UPLOAD_RE, new_upload_id
from ..rpc import DaemonUnavailable, RpcError
from .app import err_text, flash_redirect, origin_guard, post_guard, render, rpc, session_of, ws
from .security import KEY_HEADER, WebAuth

TOKEN_TTL_S = 600.0
PLAN_TIMEOUT_S = 1800.0
CHUNK = 1024 * 1024
PHASES_EXPORT = {"quiesce": "Làm lặng hub", "packing": "Đóng gói", "sealing": "Mã hoá secret", "done": "Xong"}
PHASES_IMPORT = {"starting": "Chuẩn bị", "waiting_daemon": "Chờ daemon dừng", "importing": "Đang nhập gói", "restarting": "Khởi động lại daemon",
                 "finished": "Xong"}
MODE_LABEL = {"clone": "Nhân bản — hai máy cùng chạy (tắt Telegram/Slack, tạm dừng lịch, chặn task đang dở ở máy mới)",
              "move": "Chuyển nhà — giữ nguyên kênh/lịch (máy cũ đã đóng băng)"}


def _size(n: Any) -> str:
    from .backup_pages import fmt_size

    return fmt_size(n)


def _uid(v: Any) -> str:
    s = str(v or "")
    if not UPLOAD_RE.match(s):
        raise HTTPException(404, "Không có gói này")
    return s


# ============================================================================ trạng thái
async def _status(request: Request) -> dict[str, Any]:
    try:
        return {"st": await rpc(request, "hub.transfer_status") or {}}
    except (DaemonUnavailable, OSError):
        return {"down": True}
    except RpcError as e:
        return {"error": err_text(e)}


async def _import_state(request: Request, uid: str | None) -> dict[str, Any]:
    if not uid:
        return {}
    try:
        r = await rpc(request, "hub.import_status", {"upload": uid}) or {}
    except (DaemonUnavailable, OSError):
        return {"imp_down": True}
    except RpcError as e:
        return {"imp_error": err_text(e)}
    return {"imp": r.get("state") or {}}


def _track(request: Request) -> str | None:
    v = request.query_params.get("import") or ""
    return v if UPLOAD_RE.match(v) else None


async def _ctx(request: Request) -> dict[str, Any]:
    uid = _track(request)
    ctx = {**await _status(request), **await _import_state(request, uid), "track": uid, "phases_export": PHASES_EXPORT,
           "phases_import": PHASES_IMPORT, "size": _size}
    exp = ((ctx.get("st") or {}).get("export") or {})
    imp = ctx.get("imp") or {}
    ctx["polling"] = bool(exp.get("state") == "running" or (uid and (ctx.get("imp_down") or ctx.get("down")
                                                                     or imp.get("state") in ("starting", "running") or not imp)))
    return ctx


async def page(request: Request) -> Response:
    return await render(request, "hub_transfer.html", {"nav": "settings", **await _ctx(request)})


async def partial(request: Request) -> Response:
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_hub_transfer.html", {**await _ctx(request), "csrf": sess.csrf if sess else ""})


async def partial_doctor(request: Request) -> Response:
    try:
        ctx: dict[str, Any] = {"doc": await rpc(request, "hub.doctor")}
    except (DaemonUnavailable, OSError):
        ctx = {"down": True}
    except RpcError as e:
        ctx = {"error": err_text(e)}
    return ws(request).templates.TemplateResponse(request, "_hub_doctor.html", ctx)


# ============================================================================ tải lên
class _TooBig(Exception):
    pass


class _Short(Exception):
    pass


async def upload(request: Request) -> Response:
    """Thân thô = file gói. Không qua `post_guard` (sẽ parse multipart vào RAM/tmp) — cùng các kiểm của nó nhưng đọc header."""
    sess = session_of(request)
    if sess is None:
        raise HTTPException(401, "Chưa đăng nhập")
    origin_guard(request)
    if not WebAuth.csrf_ok(sess, request.headers.get("x-csrf-token")):
        raise HTTPException(403, "Token CSRF sai hoặc thiếu — tải lại trang rồi thử lại")
    if not WebAuth.key_ok(sess, request.headers.get(KEY_HEADER)):
        raise HTTPException(403, "Trình duyệt này thiếu khoá phiên — chạy `cfharness web --open` để đăng nhập lại")
    try:
        info = await rpc(request, "hub.transfer_status") or {}
    except (DaemonUnavailable, OSError):
        return JSONResponse({"error": "Daemon cfharness không chạy — chạy `cfharness daemon start` rồi thử lại."}, status_code=503)
    limit = int(info.get("max_upload_mb") or DEFAULT_MAX_UPLOAD_MB) * 1024 * 1024
    cl = request.headers.get("content-length") or ""
    if not cl.isdigit():
        return JSONResponse({"error": "Thiếu Content-Length"}, status_code=411)
    n = int(cl)
    if n > limit:
        return JSONResponse({"error": f"Gói {_size(n)} vượt trần {_size(limit)} (transfer.max_upload_mb) — dùng `cfharness hub import <file>` ở terminal, "
                                      "hoặc nâng trần: `cfharness config set transfer.max_upload_mb <MB>`"}, status_code=413)
    if n <= 0:
        return JSONResponse({"error": "File rỗng"}, status_code=400)
    d = T._private_dir(T.imports_dir(ws(request).paths))
    uid = new_upload_id()
    part = d / f".{uid}{T.EXT}.part"
    final = d / f"{uid}{T.EXT}"
    got = 0
    try:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                got += len(chunk)
                if got > n or got > limit:
                    raise _TooBig()
                f.write(chunk)
            if got != n:
                raise _Short()
            f.flush()
            os.fsync(f.fileno())
        os.replace(part, final)
    except _TooBig:
        part.unlink(missing_ok=True)
        return JSONResponse({"error": "Gói lớn hơn khai báo / vượt trần"}, status_code=413)
    except (_Short, ClientDisconnect):
        part.unlink(missing_ok=True)
        return JSONResponse({"error": "Tải lên bị ngắt giữa chừng — thử lại"}, status_code=400)
    except OSError as e:
        part.unlink(missing_ok=True)
        return JSONResponse({"error": f"Không ghi được gói: {e.strerror or e}"}, status_code=507)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return JSONResponse({"upload": uid, "size": got, "next": f"/hub-transfer/import/{uid}"})


async def discard(request: Request) -> Response:
    _, _form = await post_guard(request)
    uid = _uid(request.path_params["id"])
    try:
        st = await rpc(request, "hub.import_status", {"upload": uid}) or {}
        if (st.get("state") or {}).get("state") in ("starting", "running"):
            return flash_redirect(request, "/hub-transfer", "error", "Gói đang được nhập — không xoá được")
    except RpcError:
        pass
    (T.imports_dir(ws(request).paths) / f"{uid}{T.EXT}").unlink(missing_ok=True)
    return flash_redirect(request, "/hub-transfer", "ok", "Đã xoá gói tải lên")


# ============================================================================ kế hoạch nhập
def _plan_params(form: Any | None) -> dict[str, Any]:
    if form is None:
        return {"mode": "clone", "maps": [], "skip_secrets": False, "clone_repos": False}
    return {"mode": "move" if form.get("mode") == "move" else "clone",
            "maps": [ln.strip() for ln in [*str(form.get("maps") or "").splitlines(), *(str(x) for x in form.getlist("map"))] if ln.strip()],
            "skip_secrets": form.get("skip_secrets") == "1", "clone_repos": form.get("clone_repos") == "1"}


async def import_plan(request: Request) -> Response:
    uid = _uid(request.path_params["id"])
    form = None
    pw = None
    if request.method == "POST":
        _, form = await post_guard(request)
        pw = str(form.get("passphrase") or "") or None
    opts = _plan_params(form)
    try:
        # kiểm + giải nén thử cả gói (có thể hàng GB) → chờ lâu hơn trần 60 s mặc định của client web
        plan = await ws(request).client.call("hub.import_plan", {"upload": uid, **opts, **({"passphrase": pw} if pw else {})}, timeout=PLAN_TIMEOUT_S)
    except RpcError as e:
        return flash_redirect(request, "/hub-transfer", "error", err_text(e))
    finally:
        pw = None
    return await render(request, "hub_import_plan.html", {"nav": "settings", "p": plan, "opts": opts, "uid": uid, "size": _size,
                                                          "mode_label": MODE_LABEL, "maps_text": "\n".join(opts["maps"])})


# ============================================================================ tải về (link một lần)
# Dùng chung với gói chẩn đoán (web/support_pages.py): token ngẫu nhiên MỘT lần trong RAM của tiến trình web, gắn phiên, hết hạn 10 phút;
# mỗi loại một kho riêng (`app.state.<ns>_tokens`) để token của loại này không mở được file loại kia.
def _tokens(request: Request, ns: str = "transfer") -> dict[str, tuple[str, str, float]]:
    st = request.app.state
    attr = f"{ns}_tokens"
    if not hasattr(st, attr):
        setattr(st, attr, {})
    return getattr(st, attr)


def mint_download(request: Request, sess: Any, name: str, ns: str = "transfer") -> str:
    """Token một lần cho `name` (đã kiểm định dạng ở bên gọi), gắn phiên `sess`. Dọn token hết hạn."""
    now = time.time()
    toks = _tokens(request, ns)
    for k in [k for k, v in toks.items() if v[2] < now]:
        toks.pop(k, None)
    tok = secrets.token_urlsafe(24)
    toks[tok] = (sess.sid, name, now + TOKEN_TTL_S)
    return tok


def take_download(request: Request, ns: str = "transfer") -> str:
    """Tiêu token trong đường dẫn (MỘT lần — kể cả khi hết hạn) → tên đã gắn. Sai phiên / dùng lại / hết hạn → 404."""
    sess = session_of(request)
    tok = request.path_params["token"]
    toks = _tokens(request, ns)
    ent = toks.get(tok)
    if sess is None or ent is None or ent[0] != sess.sid:
        raise HTTPException(404, "Link tải không hợp lệ hoặc đã dùng")
    toks.pop(tok, None)
    if ent[2] < time.time():
        raise HTTPException(404, "Link tải đã hết hạn — bấm \"Tải về\" lại")
    return ent[1]


def stream_file(path: Any, filename: str) -> Response:
    """Stream file THƯỜNG (`O_NOFOLLOW`) dạng tải về, không cache. Không có / không phải file thường → 404."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise HTTPException(404, "File không còn trên máy") from None
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise HTTPException(404, "File không hợp lệ")

    def gen() -> Any:
        with os.fdopen(fd, "rb") as f:
            while True:
                b = f.read(CHUNK)
                if not b:
                    break
                yield b

    return StreamingResponse(gen(), media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store", "Content-Length": str(st.st_size)})


async def download_link(request: Request) -> Response:
    sess, _ = await post_guard(request)
    try:
        st = await rpc(request, "hub.export_status") or {}
    except RpcError as e:
        return flash_redirect(request, "/hub-transfer", "error", err_text(e))
    eid = str((st.get("result") or {}).get("id") or "")
    if st.get("state") != "done" or not T.ID_RE.match(eid):
        return flash_redirect(request, "/hub-transfer", "error", "Chưa có gói xuất xong để tải về")
    return RedirectResponse(f"/hub-transfer/download/{mint_download(request, sess, eid)}", status_code=303)


async def download(request: Request) -> Response:
    eid = take_download(request)
    if not T.ID_RE.match(eid):
        raise HTTPException(404, "Gói không hợp lệ")
    return stream_file(T.exports_dir(ws(request).paths) / f"{eid}{T.EXT}", f"{eid}{T.EXT}")


# ============================================================================ thao tác (ACTIONS)
def _flag(f: Any, k: str) -> bool:
    return f.get(k) == "1"


def _export_build(f: Any) -> dict[str, Any]:
    profile = str(f.get("profile") or T.DEFAULT_PROFILE)
    if profile not in T.PROFILES:
        raise ValueError("profile phải là core hoặc full")
    purpose = str(f.get("for") or "clone")
    if purpose not in T.MODES:
        raise ValueError("chế độ phải là clone hoặc move")
    secrets_on = _flag(f, "secrets")
    p: dict[str, Any] = {"profile": profile, "for": purpose, "stop": _flag(f, "stop"), "secrets": secrets_on,
                         "with_totp": _flag(f, "with_totp"), "with_mcp_tokens": _flag(f, "with_mcp_tokens")}
    if f.get("confirm2") == "yes" and secrets_on and _flag(f, "has_secrets"):
        from ..transfer_secrets import MIN_PASSPHRASE

        pw, pw2 = str(f.get("passphrase") or ""), str(f.get("passphrase2") or "")
        if pw != pw2:
            raise ValueError("hai ô passphrase không khớp")
        if len(pw) < MIN_PASSPHRASE:
            raise ValueError(f"passphrase phải dài ít nhất {MIN_PASSPHRASE} ký tự")
        p["passphrase"] = pw
    return p


def _export_confirm(f: Any) -> str:
    return (f"Xuất TOÀN BỘ hub thành MỘT file .cfhx (profile {f.get('profile') or 'full'})?\n"
            "Gói chứa memory, transcript hội thoại, nhật ký kiểm toán và lịch sử run của MỌI agent — giữ riêng tư, đừng đưa lên nơi công khai. "
            + ("Secret được mã hoá bằng passphrase; phần còn lại KHÔNG mã hoá." if _flag(f, "secrets") else "Gói KHÔNG mang secret (chỉ ghi tên).")
            + ("\nRun đang chạy sẽ bị DỪNG trước khi xuất." if _flag(f, "stop") else ""))


def _export_step2(f: Any) -> str:
    base = ("CHUYỂN NHÀ: sau khi gói qua kiểm tra, hub NÀY bị ĐÓNG BĂNG — không chạy run, lịch, Telegram/Slack — cho tới khi bạn bấm "
            "\"Mở lại hub này\" ở trang Chuyển hub." if f.get("for") == "move" else "Nhân bản: hub này vẫn chạy bình thường sau khi xuất.")
    if _flag(f, "secrets") and _flag(f, "has_secrets"):
        base += ("\nNhập passphrase để mã hoá secret (≥ 12 ký tự). Quên passphrase = mất phần secret (phần còn lại vẫn nhập được). "
                 "Passphrase chỉ dùng trong lần xuất này — hub không lưu.")
    return base


def _export_reenter(f: Any) -> list[tuple[str, str, str]]:
    if _flag(f, "secrets") and _flag(f, "has_secrets"):
        return [("passphrase", "Passphrase (≥ 12 ký tự)", "password"), ("passphrase2", "Nhập lại passphrase", "password")]
    return []


def _import_build(f: Any) -> dict[str, Any]:
    uid = str(f.get("upload") or "")
    if not UPLOAD_RE.match(uid):
        raise ValueError("thiếu gói tải lên")
    p: dict[str, Any] = {"upload": uid, **_plan_params(f), "replace": _flag(f, "replace"), "stop": _flag(f, "stop")}
    if f.get("confirm2") == "yes":
        cid = str(f.get("confirm_id") or "").strip()
        if not cid:
            raise ValueError("gõ lại id gói để xác nhận")
        p["confirm_id"] = cid
        if _flag(f, "has_secrets") and not p["skip_secrets"]:
            pw = str(f.get("passphrase") or "")
            if not pw:
                raise ValueError("nhập passphrase của phần secret (hoặc chọn bỏ qua secret ở trang kế hoạch)")
            p["passphrase"] = pw
    return p


def _import_step2(f: Any) -> str:
    return (f"Nhập gói {f.get('pkg_id') or '?'} vào hub này ({MODE_LABEL.get(str(f.get('mode') or 'clone'), '')})?\n"
            "Daemon sẽ DỪNG trong lúc nhập (thường vài phút) rồi tự chạy lại; trang này tự cập nhật. "
            + ("Hub hiện có được chụp thành bản sao lưu pre-restore rồi bị THAY THẾ. " if _flag(f, "replace") else "")
            + ("Run đang chạy sẽ bị dừng. " if _flag(f, "stop") else "")
            + "Lỗi giữa chừng → tự hoàn tác. Gõ lại id gói để xác nhận.")


def _import_reenter(f: Any) -> list[tuple[str, str, str]]:
    out = [("confirm_id", f"Gõ lại id gói ({f.get('pkg_id') or '?'})", "text")]
    if _flag(f, "has_secrets") and not _flag(f, "skip_secrets"):
        out.append(("passphrase", "Passphrase của phần secret", "password"))
    return out


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    def export_ok(r: Any, f: Any) -> str:
        return "Đang xuất hub — trang này tự cập nhật; xong thì bấm \"Tải về\""

    def import_ok(r: Any, f: Any) -> str:
        return f"Đang nhập gói {(r or {}).get('package_id')} — daemon dừng trong lúc ghi rồi tự chạy lại; trang này tự cập nhật"

    actions["hub_export"] = Action("hub.export_start", _export_build, export_ok, confirm=_export_confirm, danger=True, step2=_export_step2,
                                   reenter=_export_reenter, secret_fields=("passphrase", "passphrase2"), goto=lambda r, f: "/hub-transfer")
    actions["hub_import"] = Action("hub.import_start", _import_build, import_ok, danger=True, step2=_import_step2, reenter=_import_reenter,
                                   secret_fields=("passphrase", "confirm_id"),
                                   goto=lambda r, f: f"/hub-transfer?import={(r or {}).get('upload') or f.get('upload')}")
    actions["hub_unfreeze"] = Action(
        "hub.unfreeze", lambda f: {"export_id": str(f.get("export_id") or "") or None}, "Đã mở lại hub — máy này chạy lại run/lịch/kênh",
        confirm="Mở lại hub này?\nMáy này sẽ chạy lại run, lịch, Telegram/Slack. Nếu máy mới cũng đang chạy, hai hub sẽ tranh bot/OAuth "
                "(Telegram 409, Slack chia sự kiện).", danger=True, goto=lambda r, f: "/hub-transfer")


def routes() -> list[Any]:
    return [Route("/hub-transfer", page), Route("/partials/hub-transfer", partial), Route("/partials/hub-transfer/doctor", partial_doctor),
            Route("/hub-transfer/upload", upload, methods=["POST"]),
            Route("/hub-transfer/import/{id}", import_plan, methods=["GET", "POST"]),
            Route("/hub-transfer/import/{id}/discard", discard, methods=["POST"]),
            Route("/hub-transfer/download", download_link, methods=["POST"]),
            Route("/hub-transfer/download/{token}", download)]


__all__ = ["register_actions", "routes", "mint_download", "take_download", "stream_file"]
