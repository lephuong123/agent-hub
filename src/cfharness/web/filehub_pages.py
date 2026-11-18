"""Thẻ "Không gian số — cổng cfharness" ở Cài đặt (filehub/) — `/partials/filehub/lane` (htmx `load`; `/partials/filehub` = cùng thẻ, cho
liên kết cũ) và `/filehub/open` (link "Mở trên Không gian số"). docs/dev-notes/filehub.md, plan-filehub-lane.md.

- Đọc: RPC `filehub.lane_status` (không bao giờ có khoá) + `filehub.status.legacy` (thư mục còn cấu hình đồng bộ 2 chiều CŨ — đã bỏ ở H5:
  chữ "cần chuyển" do daemon dựng). Daemon không trả lời → thẻ báo, không lỗi trang.
- Ghi: mọi thao tác là `ACTIONS` gọi RPC `filehub.*` (HUMAN_ONLY, audit `web`, CSRF + Origin + khoá phiên WT-1): `filehub_lane_setup` (khoá
  CHỈ ở bước 2 — `Action.reenter` + `secret_fields`), `filehub_lane_test`, `filehub_lane_disable`, `filehub_lane_orphan`, `filehub_lane_deletes`,
  `filehub_remove`, `filehub_rename`.
- `/filehub/open?run=…&path=…` → RPC `filehub.link` → chuyển hướng tới `<server>/files/<folder_id>?file=<file_id>`; URL dựng ở daemon từ
  cấu hình `filehub.server` (người đặt) + id do cổng trả — không lấy từ client.
"""

from __future__ import annotations

from typing import Any

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from ..rpc import DaemonUnavailable, RpcError
from .app import rpc, session_of, ws


async def partial_lane(request: Request) -> Response:
    """Thẻ "Cổng cfharness": RPC `filehub.lane_status` (không bao giờ có khoá) + thư mục đồng bộ cũ cần chuyển (`filehub.status.legacy`)."""
    ctx: dict[str, Any]
    try:
        ctx = {"data": await rpc(request, "filehub.lane_status")}
        st = await rpc(request, "filehub.status")
        ctx["legacy"] = [x for x in (st or {}).get("legacy") or [] if isinstance(x, dict) and x.get("name")]
        ctx["batches"] = held_batches(ctx["data"])
    except (DaemonUnavailable, OSError):
        ctx = {"down": True}
    except RpcError as e:
        ctx = {"error": e.message}
    sess = session_of(request)
    return ws(request).templates.TemplateResponse(request, "_filehub_lane.html", {**ctx, "csrf": sess.csrf if sess else ""})


async def filehub_open(request: Request) -> Response:
    q = request.query_params
    path = str(q.get("path") or "")
    run = str(q.get("run") or "").strip()
    if not path or len(path) > 4096 or len(run) > 200:
        raise HTTPException(400, "Thiếu/sai tham số path")
    try:
        res = await rpc(request, "filehub.link", {"path": path, **({"run_id": run} if run else {})})
    except RpcError as e:
        raise HTTPException(404, e.message) from None
    except (DaemonUnavailable, OSError):
        raise HTTPException(503, "Daemon không trả lời") from None
    url = str((res or {}).get("url") or "")
    if not url.startswith(("https://", "http://")):
        raise HTTPException(404, "Chưa có link cho tệp này")
    return RedirectResponse(url, status_code=303)


NEXT_LANE = "/settings#filehub-lane"  # thao tác của thẻ quay về đúng thẻ


def _name(f: Any, key: str = "name", default: str | None = None) -> str:
    from ..filehub.service import valid_name

    v = str(f.get(key) or "").strip() or (default or "")
    if not valid_name(v):
        raise ValueError("tên thư mục: chữ, số, khoảng trắng, - _ . (≤ 80 ký tự)")
    return v


def held_list(runs: Any, root_id: str | None = None) -> tuple[list[str], int]:
    """Lô xoá đang giữ (từ `filehub.lane_status.runs[]` / `orphans[]` `.held_paths` do daemon cắt ≤ 50) → (đường dẫn, số mục không hiện)."""
    paths: list[str] = []
    total = 0
    for w in runs or []:
        if not isinstance(w, dict) or not w.get("held") or (root_id is not None and str(w.get("root_id")) != str(root_id)):
            continue
        total += int(w.get("held") or 0)
        paths += [str(x) for x in (w.get("held_paths") or [])[:50]]
    return paths, max(0, total - len(paths))


def held_batches(st: Any) -> list[dict[str, Any]]:
    """Lô xoá hàng loạt đang chờ người — gộp theo GỐC (Cho xoá / Không xoá áp cho cả gốc), từ run đang chạy lẫn run đã kết thúc (cây mồ côi)."""
    out: dict[str, dict[str, Any]] = {}
    st = st if isinstance(st, dict) else {}
    for ended, w in [*((False, x) for x in st.get("runs") or []), *((True, x) for x in st.get("orphans") or [])]:
        if not isinstance(w, dict) or not w.get("held") or not w.get("root_id"):
            continue
        b = out.setdefault(str(w["root_id"]), {"root_id": str(w["root_id"]), "folder": w.get("folder"), "agents": [], "tasks": [], "count": 0,
                                               "paths": [], "ended": False})
        b["count"] += int(w.get("held") or 0)
        b["paths"] += [str(x) for x in (w.get("held_paths") or [])[:50]]
        b["ended"] = b["ended"] or ended
        if w.get("agent_id") and w["agent_id"] not in b["agents"]:
            b["agents"].append(w["agent_id"])
        if w.get("task_id") and w["task_id"] not in b["tasks"]:
            b["tasks"].append(w["task_id"])
    for b in out.values():
        b["more"] = max(0, b["count"] - len(b["paths"]))
    return list(out.values())


def _bullets(lines: list[str], more: int) -> str:
    return "\n".join(f"• {x}" for x in lines) + (f"\n… và {more} mục nữa" if more else "")


async def _held_preview(request: Request, f: Any) -> str:
    st = await rpc(request, "filehub.lane_status")
    paths, more = held_list([*((st or {}).get("runs") or []), *((st or {}).get("orphans") or [])], str(f.get("root_id") or ""))
    if not paths:
        return ""
    head = ("Các tệp sẽ Ở LẠI Không gian số (lệnh xoá của agent bị bỏ):" if f.get("discard")
            else "Các mục sẽ bị XOÁ trên Không gian số (vào thùng rác):")
    return head + "\n" + _bullets(paths, more)


def _item_line(x: dict[str, Any]) -> str:
    op, path = str(x.get("op") or ""), str(x.get("path") or "")
    if op == "move" and x.get("from"):
        return f"Di chuyển {x['from']} → {path}"
    label = str(x.get("label") or op)
    return f"{label[:1].upper()}{label[1:]} {path}"


async def _orphan_preview(request: Request, f: Any) -> str:
    st = await rpc(request, "filehub.lane_status")
    o = next((x for x in (st or {}).get("orphans") or [] if isinstance(x, dict) and str(x.get("tree")) == str(f.get("tree"))), None)
    if o is None:
        return ""
    items = [x for x in o.get("items") or [] if isinstance(x, dict) and not x.get("held")]
    more = max(0, int(o.get("count") or 0) - int(o.get("held") or 0) - len(items))
    if f.get("action") == "drop":
        txt = "Sẽ KHÔNG làm các thay đổi này trên Không gian số:\n" + _bullets([_item_line(x) for x in items], more)
        if o.get("held"):
            txt += f"\n(và bỏ {o['held']} mục xoá đang chờ duyệt — các tệp đó ở lại Không gian số)"
        return txt
    txt = "Sẽ làm trên Không gian số:\n" + _bullets([_item_line(x) for x in items], more)
    if o.get("held"):
        txt += (f"\n{o['held']} mục xoá đang chờ duyệt KHÔNG đi kèm — quyết riêng bằng Cho xoá / Không xoá.")
    return txt


def _n(f: Any) -> str:
    try:
        return str(max(0, int(str(f.get("n") or "0"))))
    except ValueError:
        return "các"


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    def simple(method: str) -> Any:
        async def call(request: Request, p: dict[str, Any]) -> Any:
            return await rpc(request, method, p, audit=True)

        return call

    actions["filehub_remove"] = Action(
        "filehub.remove", lambda f: {"name": _req(f, "name")}, lambda r, f: f"Đã gỡ «{f.get('name')}» khỏi hub (tệp giữ nguyên)",
        call=simple("filehub.remove"), confirm=lambda f: f"Gỡ thư mục chung «{f.get('name')}» khỏi hub?",
        step2=lambda f: (f"Agent không còn giao việc vào workspace filehub:{f.get('name')} được. Tệp trên Không gian số và bản sao cũ trên máy "
                         "(nếu có) GIỮ NGUYÊN — không xoá gì. Khoá cổng còn cấp gốc này thì lần kiểm tra sau hub thêm lại."),
        danger=True, goto=lambda r, f: NEXT_LANE)
    # ---- cổng cfharness (chế độ lane): khoá CHỈ nhập ở bước 2 (ô password trống, không qua trang xác nhận/carry, không vào HTML/log/audit)
    def _lane_setup_build(f: Any) -> dict[str, Any]:
        # tên cổng + địa chỉ đều tuỳ chọn: trống → daemon thử 8223 rồi 8233 và lấy tên theo Không gian số trả lời (chỉ cần dán khoá)
        server = str(f.get("server") or "").strip().lower()
        url = str(f.get("url") or "").strip()
        from ..filehub.lane.client import SERVER_RE, LaneError, check_url

        if server and not SERVER_RE.match(server):
            raise ValueError("tên cổng: chữ thường/số/_ (vd qltm)")
        if url:
            try:
                url = check_url(url)
            except LaneError as e:
                raise ValueError(e.message) from None
        p: dict[str, Any] = {**({"server": server} if server else {}), **({"url": url} if url else {})}
        if f.get("confirm2") == "yes":
            key = str(f.get("key") or "").strip()
            if not key:
                raise ValueError("nhập khoá cổng cfharness (tt_lane_…)")
            p["key"] = key
        return p

    def _lane_setup_ok(r: Any, f: Any) -> str:
        r = r or {}
        msg = f"Đã lưu khoá cổng «{r.get('server')}» ({r.get('key_prefix')}…) — tài khoản {r.get('user')}."
        if r.get("added"):
            msg += " Dùng được ngay: " + ", ".join(f"filehub:{x.get('name')}" for x in r["added"]) + "."
        if r.get("converted"):
            msg += (" Đã chuyển thư mục đồng bộ cũ sang cổng (bản sao trên máy để nguyên): "
                    + ", ".join(f"filehub:{x.get('name')}" for x in r["converted"]) + ".")
        if not r.get("added") and not r.get("converted") and not r.get("roots"):
            msg += " Khoá chưa được cấp thư mục/workspace nào — cấp ở Không gian số › Hồ sơ › Cổng cfharness (hub tự thêm trong vài phút)."
        return msg

    actions["filehub_lane_setup"] = Action(
        "filehub.lane_setup", _lane_setup_build, _lane_setup_ok,
        confirm=lambda f: (f"Nối hub với cổng cfharness {str(f.get('url') or '').strip() or 'trên máy này (thử cổng 8223 rồi 8233)'}"
                           + (f" (tên «{str(f.get('server') or '').strip()}»)" if str(f.get("server") or "").strip() else "") + "?\n"
                           "Mọi thư mục/workspace khoá được cấp sẽ thành thư mục chung dùng được ngay (filehub:<tên>)."),
        step2=("Dán khoá cổng tạo ở Không gian số › Hồ sơ › Cổng cfharness. Khoá cho hub làm việc với thư mục được cấp DƯỚI TÊN TÀI KHOẢN CỦA BẠN "
               "(agent không bao giờ thấy khoá; chỉ daemon dùng, chỉ gửi tới 127.0.0.1). Hub lưu khoá trong kho secret (0600)."),
        reenter=lambda f: [("key", "Khoá cổng cfharness (tt_lane_…)", "password")], secret_fields=("key",), danger=True,
        goto=lambda r, f: NEXT_LANE)
    actions["filehub_lane_disable"] = Action(
        "filehub.lane_disable", lambda f: {"server": _req(f, "server")}, lambda r, f: f"Đã xoá khoá cổng «{f.get('server')}»",
        confirm=lambda f: (f"Xoá khoá + cấu hình cổng «{f.get('server')}»? Thư mục chung chỉ có qua cổng này được gỡ khỏi hub (tệp trên Không gian "
                           "số GIỮ NGUYÊN); dán lại khoá là chúng trở lại."),
        danger=True, goto=lambda r, f: NEXT_LANE)
    def _orphan_ok(r: Any, f: Any) -> str:
        r = r or {}
        if r.get("dropped"):
            return "Đã bỏ thay đổi của agent — tệp trên Không gian số giữ nguyên"
        held = f" Còn {r['held']} mục xoá chờ bạn quyết (Cho xoá / Không xoá)." if r.get("held") else ""
        if r.get("pushed"):
            return "Đã làm xong các thay đổi trên Không gian số"
        n = len(r.get("left") or []) - int(r.get("held") or 0)
        return (f"Còn {n} thay đổi chưa làm được ({r.get('code')})." if n > 0 else "Đã làm các thay đổi khác.") + held

    actions["filehub_lane_orphan"] = Action(
        "filehub.lane_orphan", lambda f: {"tree": _req(f, "tree"), "action": "drop" if f.get("action") == "drop" else "push"}, _orphan_ok,
        confirm=lambda f: ("Bỏ thay đổi của agent? Các thay đổi dưới đây KHÔNG được làm trên Không gian số (tệp trên Không gian số giữ nguyên); "
                           "bản làm việc trên máy bị xoá." if f.get("action") == "drop"
                           else "Làm lại trên Không gian số các thay đổi còn kẹt của run này (từng mục dưới đây)? Tệp chứa secret vẫn bị chặn."),
        preview=_orphan_preview, danger=True, goto=lambda r, f: NEXT_LANE)
    actions["filehub_lane_deletes"] = Action(
        "filehub.resume", lambda f: {"root_id": _req(f, "root_id"), **({"discard": True} if f.get("discard") else {})},
        lambda r, f: (f"Không xoá — giữ {(r or {}).get('discarded', 0)} tệp trên Không gian số, đã bỏ lệnh xoá của agent" if f.get("discard")
                      else f"Đã xoá {(r or {}).get('released', 0)} mục trên Không gian số (vào thùng rác, khôi phục được)"),
        call=simple("filehub.resume"),
        confirm=lambda f: (f"Không xoá — giữ {_n(f)} tệp trên Không gian số, bỏ thay đổi của agent (lệnh xoá hàng loạt)?" if f.get("discard")
                           else f"Xoá {_n(f)} tệp trên Không gian số (vào thùng rác, khôi phục được)?"),
        preview=_held_preview, danger=True, goto=lambda r, f: str(f.get("next") or "") if str(f.get("next") or "") == "/inbox" else NEXT_LANE)
    actions["filehub_lane_test"] = Action(
        "filehub.lane_test", lambda f: {"server": _req(f, "server")},
        lambda r, f: (f"Cổng «{(r or {}).get('server')}» trả lời — {len((r or {}).get('roots') or [])} thư mục/workspace được cấp"
                      + (f"; mới thêm: {', '.join('filehub:' + str(x) for x in (r or {}).get('added') or [])}" if (r or {}).get("added") else "")),
        goto=lambda r, f: NEXT_LANE)

    actions["filehub_rename"] = Action(
        "filehub.rename", lambda f: {"name": _req(f, "name"), "new_name": _name(f, "new_name")},
        lambda r, f: f"Đã đổi tên «{(r or {}).get('previous')}» → «{(r or {}).get('name')}» (giao việc: workspace {(r or {}).get('workspace')})",
        call=simple("filehub.rename"), goto=lambda r, f: NEXT_LANE,
        confirm=lambda f: (f"Đổi tên thư mục chung «{f.get('name')}» thành «{str(f.get('new_name') or '').strip()}» trong cfharness?\n"
                           "Chỉ đổi tên dùng để giao việc (filehub:<tên>) — tên trên Không gian số giữ nguyên; tên cũ vẫn khớp."))


def routes() -> list[Any]:
    return [Route("/partials/filehub", partial_lane), Route("/partials/filehub/lane", partial_lane), Route("/filehub/open", filehub_open)]


__all__ = ["register_actions", "routes", "held_batches", "held_list"]
