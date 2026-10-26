"""Trang MCP mở rộng (wave 1 — "giống trang connectors của Claude"): sửa server, điều khiển từng tool, xem cấu hình MCP theo agent,
duyệt danh mục MCP Registry và cài.

- `GET /mcp/{name}`: chi tiết + form sửa (action `mcp_edit` = RPC `mcp.update`, hộp xác nhận) + bảng tool có nút chặn / cho phép /
  luôn hỏi duyệt / ghi đè annotations (POST `/mcp/{name}/tool` → `mcp.update{patch.tools}`, ngang `cfharness mcp edit --include…`
  và `mcp annotate`). Tool có mô tả đáng ngờ (`suspicious` của probe) bị chặn mặc định cho tới khi người cho phép rõ ràng.
- `GET /mcp/agent/{id}`: `mcp.agent_config` — server + luật allow/deny sẽ dùng khi chạy agent. Không có giá trị secret (chỉ `${TÊN}`;
  giá trị rõ của khoá trông như secret bị che thêm lần nữa ở đây).
- `GET /mcp/browse?q=&cursor=`: `mcp.catalog` (MCP Registry chính thức, cache, offline → danh sách tuyển chọn).
- `GET|POST /mcp/browse/install?name=&opt=`: form cài dựng từ mục registry. Trường `isSecret` = ô mật khẩu → `secret.set`
  (tên `MCP_<server>_<khoá>`), cấu hình chỉ giữ `${TÊN}`. Gửi: `mcp.add{probe:false}` → `secret.set` → `mcp.probe` (cùng RPC với
  thêm tay; trust mặc định untrusted). Giá trị secret không bao giờ được vẽ lại vào HTML (kể cả khi lỗi / cần xác nhận).
  Remote cần OAuth → cài với `auth: {type: oauth}` (không probe vì chưa có token) rồi đưa về trang server để bấm Kết nối
  (luồng OAuth: `web/mcp_oauth.py`, docs/dev-notes/mcp-oauth.md).
Mọi thao tác ghi: `post_guard` (CSRF + Origin + khoá phiên WT-1), `_audit.channel=web` qua `rpc(..., audit=True)`.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import Response

from ..mcp_client.config import NAME_RE, ToolFilter, annotation_override, match_any, tool_selected
from ..mcp_client.importer import SECRET_KEY_RE
from ..rpc import RpcError
from ..secrets import is_ref
from .app import carry, err_text, flash_redirect, post_guard, render, rpc, safe_next

TRANSPORTS = ("stdio", "http", "sse")
PAGE_LIMIT = 20
MASK = "••••••"


# ============================================================================ tiện ích form
def _lines_to_map(raw: Any, *, sep: str) -> dict[str, str]:
    """Dòng `KEY=giá trị` (env) / `Tên: giá trị` (header, chấp nhận cả `=`) → dict. Dòng trống/thiếu dấu bỏ qua."""
    out: dict[str, str] = {}
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        s = sep if sep in line else ("=" if "=" in line else None)
        if s is None:
            raise ValueError(f"dòng '{line[:40]}' thiếu '{sep}'")
        k, _, v = line.partition(s)
        if k.strip():
            out[k.strip()] = v.strip()
    return out


def _map_patch(new: dict[str, str], old_keys: Any) -> dict[str, Any]:
    """Patch cho env/headers (mcp.update gộp khoá; None = xoá khoá cũ không còn trong form). Giá trị còn là `MASK` (đã che khi
    hiển thị) = giữ nguyên giá trị cũ — không bao giờ ghi chuỗi che vào cấu hình."""
    old = _globs(old_keys)
    changed = {k: v for k, v in new.items() if v != MASK}
    return {**{k: None for k in old if k not in new}, **changed}


def _globs(raw: Any) -> list[str]:
    return [x.strip() for x in re.split(r"[,\n]", str(raw or "")) if x.strip()]


def map_lines(d: dict[str, Any] | None, sep: str) -> str:
    return "\n".join(f"{k}{sep}{v}" for k, v in (d or {}).items())


def edit_params(f: FormData) -> dict[str, Any]:
    """Form sửa MCP (`/mcp/{name}`) → params `mcp.update` (ngang `cfharness mcp edit`). Giá trị trông như secret trong env/header
    được registry tự chuyển vào secret store (FR-MC-05) — form chỉ hiển thị `${TÊN}`."""
    from .app import _num, _req

    name = _req(f, "name", "tên server")
    transport = str(f.get("transport") or "stdio")
    if transport not in TRANSPORTS:
        raise ValueError("kiểu kết nối không hợp lệ")
    patch: dict[str, Any] = {"transport": transport, "description": str(f.get("description") or "").strip(),
                             "trust": "trusted" if f.get("trust") == "trusted" else "untrusted"}
    if transport == "stdio":
        try:
            parts = shlex.split(_req(f, "command", "lệnh chạy"))
        except ValueError as e:
            raise ValueError(f"lệnh chạy: {e}") from None
        patch.update(command=parts[0], args=parts[1:], url=None, headers=None,
                     env=_map_patch(_lines_to_map(f.get("env"), sep="="), f.get("env_keys_old")))
    else:
        patch.update(url=_req(f, "url", "URL"), command=None, args=None, env=None,
                     headers=_map_patch(_lines_to_map(f.get("headers"), sep=":"), f.get("header_keys_old")))
    t = _num(f.get("timeout_s"), float, "Timeout")
    if t is not None:
        patch["timeout_s"] = t
    patch["tools"] = {"include": _globs(f.get("include")), "exclude": _globs(f.get("exclude")),
                      "require_approval": _globs(f.get("require_approval"))}
    return {"name": name, "patch": patch, "probe": f.get("probe") == "1"}


def edit_confirm(f: FormData) -> str:
    try:
        p = edit_params(f)
    except ValueError as e:
        return f"Lưu MCP server {f.get('name')}? ({e})"
    pa = p["patch"]
    what = (pa["command"] + " " + " ".join(pa.get("args") or [])).strip() if pa["transport"] == "stdio" else pa.get("url", "")
    trust = "TIN CẬY (tool ghi chạy không cần duyệt)" if pa["trust"] == "trusted" else "cần duyệt"
    return (f"Lưu cấu hình MCP server {p['name']} ({trust})?\nServer MCP chạy NGOÀI sandbox của agent, dưới quyền của bạn:\n{what}"
            + ("\nHub sẽ kiểm tra kết nối ngay sau khi lưu." if p["probe"] else ""))


def edit_ok(r: Any, f: FormData) -> str:
    from .app import _probe_msg

    r = r or {}
    msg = f"Đã lưu MCP {r.get('name') or f.get('name')}"
    if r.get("secrets_moved"):
        msg += f" — đã chuyển giá trị bí mật vào secret store: {', '.join(r['secrets_moved'])}"
    if r.get("warnings"):
        msg += " — " + "; ".join(r["warnings"])
    if r.get("probe"):
        msg += f" — {_probe_msg(r['probe'])}"
    return msg


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    """Thêm thao tác ghi của trang MCP mở rộng vào `ACTIONS` (mcp.update ∈ HUMAN_ONLY; daemon audit theo `_audit.channel=web`)."""
    actions["mcp_edit"] = Action("mcp.update", edit_params, edit_ok, confirm=edit_confirm, danger=True,
                                 goto=lambda r, f: f"/mcp/{f.get('name')}")


# ============================================================================ bảng tool
def tool_rows(s: dict[str, Any]) -> list[dict[str, Any]]:
    """Trạng thái hiệu lực từng tool — cùng logic `McpRegistry.tool_rules_for_servers` (chặn / luôn hỏi duyệt / tự chạy / qua guard)."""
    tools = (s.get("config") or {}).get("tools") or {}
    try:
        filt = ToolFilter(**{k: v for k, v in tools.items() if v is not None})
    except Exception:  # noqa: BLE001 — cấu hình hỏng thì vẫn hiện trang
        filt = ToolFilter()
    trusted = s.get("trust") == "trusted"
    rows = []
    for name, rec in sorted((s.get("annotations_cache") or {}).items()):
        rec = rec if isinstance(rec, dict) else {}
        base = rec.get("annotations") if isinstance(rec.get("annotations"), dict) else rec
        ov = annotation_override(name, filt)
        eff = {"readOnlyHint": base.get("readOnlyHint"), "destructiveHint": base.get("destructiveHint"), **ov}
        suspicious = [str(x) for x in rec.get("suspicious") or []]
        explicit = name in filt.include
        selected = tool_selected(name, filt)
        excluded = match_any(name, filt.exclude)
        approval = match_any(name, filt.require_approval)
        if not selected or (suspicious and not explicit):
            why = "trong danh sách chặn (exclude)" if excluded else "mô tả đáng ngờ — chặn mặc định" if selected else "không khớp include"
            state = ("blocked", "Bị chặn", "danger", why)
        elif eff.get("destructiveHint") is True or approval:
            state = ("approval", "Luôn hỏi duyệt", "warn", "require_approval" if approval else "server đánh dấu phá huỷ")
        elif trusted or eff.get("readOnlyHint") is True:
            state = ("auto", "Tự chạy", "ok", "chỉ đọc" if eff.get("readOnlyHint") is True else "server tin cậy")
        else:
            state = ("guard", "Qua guard", "muted", "server chưa tin cậy — thao tác ghi sẽ hỏi duyệt")
        rows.append({"name": name, "description": rec.get("description") or "", "title": rec.get("title"),
                     "server_ro": base.get("readOnlyHint"), "server_de": base.get("destructiveHint"), "eff": eff,
                     "override": {k: v for k, v in ((filt.annotations or {}).get(name) or {}).items()}, "override_any": ov,
                     "suspicious": suspicious, "explicit": explicit, "excluded_exact": name in filt.exclude, "excluded": excluded,
                     "approval_exact": name in filt.require_approval, "approval": approval,
                     "state": state[0], "label": state[1], "tone": state[2], "why": state[3]})
    return rows


TOOL_OPS = ("block", "unblock", "allow", "unallow", "approval_on", "approval_off", "annotate")


def tool_patch(tools: dict[str, Any], op: str, tool: str, f: FormData) -> dict[str, Any]:
    """Patch `tools` cho một thao tác trên một tool (hoặc glob khi `annotate`). Trả cả 3 danh sách + annotations (thay nguyên)."""
    inc = list(tools.get("include") or [])
    exc = list(tools.get("exclude") or [])
    req = list(tools.get("require_approval") or [])
    ann = {k: dict(v) for k, v in (tools.get("annotations") or {}).items() if isinstance(v, dict)}
    if op == "block":
        exc += [tool] if tool not in exc else []
        inc = [x for x in inc if x != tool]
        inc = [] if inc == ["*"] else inc
    elif op == "unblock":
        exc = [x for x in exc if x != tool]
    elif op == "allow":  # cho phép RÕ RÀNG (cần cho tool đáng ngờ): include chứa đúng tên tool; include rỗng → giữ "mọi tool" bằng "*"
        exc = [x for x in exc if x != tool]
        if tool not in inc:
            inc = (["*"] if not inc else inc) + [tool]
    elif op == "unallow":
        inc = [x for x in inc if x != tool]
        inc = [] if inc == ["*"] else inc
    elif op == "approval_on":
        req += [tool] if tool not in req else []
    elif op == "approval_off":
        req = [x for x in req if x != tool]
    elif op == "annotate":
        cur: dict[str, bool] = {}
        for key, field in (("readOnlyHint", "ro"), ("destructiveHint", "de")):
            v = str(f.get(field) or "")
            if v in ("true", "false"):
                cur[key] = v == "true"
            elif v:
                raise ValueError(f"{field}: chọn mặc định / có / không")
        if cur:
            ann[tool] = cur
        else:
            ann.pop(tool, None)
    else:
        raise ValueError("thao tác không hợp lệ")
    return {"include": inc, "exclude": exc, "require_approval": req, "annotations": ann}


def tool_risk(op: str, tool: str, f: FormData, row: dict[str, Any] | None) -> str | None:
    """Thao tác NỚI an toàn → cần xác nhận (trang xác nhận khi không có JS)."""
    if op == "allow" and row and row.get("suspicious"):
        return (f"Cho phép tool ĐÁNG NGỜ {tool}?\nMô tả của tool có dấu hiệu chèn lệnh vào prompt: {'; '.join(row['suspicious'])}\n"
                "Agent sẽ thấy và gọi được tool này.")
    if op == "unblock":
        return f"Bỏ chặn tool {tool}? Agent sẽ gọi được tool này."
    if op == "approval_off":
        return f"Bỏ 'luôn hỏi duyệt' cho {tool}?"
    if op == "annotate" and (f.get("de") == "false" or f.get("ro") == "true"):
        return (f"Ghi đè annotations cho {tool}: " + ", ".join(x for x in ("không phá huỷ" if f.get("de") == "false" else "",
                                                                          "chỉ đọc" if f.get("ro") == "true" else "") if x)
                + "?\nTool khớp sẽ không còn bị hỏi duyệt vì cờ của server (luật tên-xoá, tài nguyên bảo vệ, require_approval vẫn áp dụng).")
    return None


async def mcp_tool(request: Request) -> Response:
    _, form = await post_guard(request)
    name = request.path_params["name"]
    back = f"/mcp/{name}#tools"
    op = str(form.get("op") or "")
    tool = str(form.get("tool") or "").strip()
    if op not in TOOL_OPS or not tool or len(tool) > 200:
        return flash_redirect(request, back, "error", "Thao tác tool không hợp lệ")
    try:
        s = await rpc(request, "mcp.show", {"name": name})
    except RpcError as e:
        return flash_redirect(request, "/mcp", "error", err_text(e))
    tools = (s.get("config") or {}).get("tools") or {}
    row = next((r for r in tool_rows(s) if r["name"] == tool), None)
    try:
        patch = tool_patch(tools, op, tool, form)
    except ValueError as e:
        return flash_redirect(request, back, "error", f"Chưa đủ thông tin: {e}")
    risk = tool_risk(op, tool, form, row)
    if risk and form.get("confirm") != "yes":
        return await render(request, "confirm.html", {"message": risk, "fields": carry(form), "action": f"/mcp/{name}/tool", "danger": True,
                                                      "back": back, "note": None})
    try:
        await rpc(request, "mcp.update", {"name": name, "patch": {"tools": patch}}, audit=True)
    except RpcError as e:
        return flash_redirect(request, back, "error", err_text(e))
    labels = {"block": "đã chặn", "unblock": "đã bỏ chặn", "allow": "đã cho phép rõ ràng", "unallow": "đã bỏ cho phép rõ ràng",
              "approval_on": "luôn hỏi duyệt", "approval_off": "bỏ luôn hỏi duyệt", "annotate": "đã cập nhật ghi đè annotations"}
    return flash_redirect(request, back, "ok", f"MCP {name}: {tool} — {labels[op]}")


# ============================================================================ trang chi tiết
async def mcp_show(request: Request) -> Response:
    s = await rpc(request, "mcp.show", {"name": request.path_params["name"]})
    cfg = s.get("config") or {}
    tools = cfg.get("tools") or {}
    command = shlex.join([cfg["command"], *(cfg.get("args") or [])]) if cfg.get("command") else ""
    form = {"transport": s.get("transport"), "command": command, "url": cfg.get("url") or "", "env": map_lines(_masked(cfg.get("env")), "="),
            "headers": map_lines(_masked(cfg.get("headers")), ": "), "env_keys_old": ",".join(cfg.get("env") or {}),
            "header_keys_old": ",".join(cfg.get("headers") or {}), "description": cfg.get("description") or "", "trust": s.get("trust"),
            "timeout_s": cfg.get("timeout_s") or "", "include": "\n".join(tools.get("include") or []),
            "exclude": "\n".join(tools.get("exclude") or []), "require_approval": "\n".join(tools.get("require_approval") or [])}
    rows = tool_rows(s)
    return await render(request, "mcp_show.html", {"nav": "mcp", "s": s, "f": form, "rows": rows, "annotations": tools.get("annotations") or {},
                                                   "blocked_suspicious": [r for r in rows if r["suspicious"] and r["state"] == "blocked"],
                                                   "masked": _has_mask(cfg)})


def _masked(d: dict[str, Any] | None) -> dict[str, Any]:
    """Giá trị rõ của khoá trông như secret (không phải `${…}`) → che. Bình thường registry đã tách hết vào secret store."""
    out = {}
    for k, v in (d or {}).items():
        out[k] = MASK if isinstance(v, str) and v and "${" not in v and SECRET_KEY_RE.search(str(k)) else v
    return out


def _has_mask(cfg: dict[str, Any]) -> bool:
    return MASK in map_lines(_masked(cfg.get("env")), "=") + map_lines(_masked(cfg.get("headers")), ":")


# ============================================================================ cấu hình MCP theo agent
async def agent_config(request: Request) -> Response:
    aid = request.path_params["id"]
    res = await rpc(request, "mcp.agent_config", {"agent": aid}) or {}
    servers = []
    for name, e in sorted((res.get("mcpServers") or {}).items()):
        e = dict(e or {})
        for k in ("env", "headers"):
            if isinstance(e.get(k), dict):
                e[k] = _masked(e[k])
        servers.append({"name": name, "entry": e})
    rules = res.get("tool_rules") or {}
    return await render(request, "mcp_agent.html", {"nav": "mcp", "agent_id": aid, "servers": servers, "rules": rules,
                                                    "problems": res.get("problems") or []})


# ============================================================================ danh mục + cài
async def browse(request: Request) -> Response:
    q = str(request.query_params.get("q") or "").strip()[:200]
    cursor = str(request.query_params.get("cursor") or "")[:500] or None
    featured = not q and not cursor  # chưa gõ gì → danh sách đề xuất (CURATED), không đổ cả registry theo thứ tự chữ cái
    try:
        res = await rpc(request, "mcp.catalog", {"search": q, "limit": PAGE_LIMIT, "cursor": cursor, "offline": featured}) or {}
    except RpcError as e:
        res = {"servers": [], "error": err_text(e), "source": "error"}
    from ..mcp_client.catalog import suggest_name

    installed = {m.get("name") for m in await rpc(request, "mcp.list") or []}
    for e in res.get("servers") or []:
        e["installed"] = suggest_name(str(e.get("name") or "")) in installed
    return await render(request, "mcp_browse.html", {"nav": "mcp", "q": q, "cursor": cursor, "res": res, "installed": installed,
                                                     "featured": featured})


def _field_key(prefix: str, name: str) -> str:
    return prefix + re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:120]


async def _install_ctx(request: Request, reg_name: str, opt_id: str | None) -> dict[str, Any]:
    res = await rpc(request, "mcp.catalog", {"name": reg_name, "check_auth": True})
    opts = res.get("options") or []
    for o in opts:  # khoá trường form (dùng chung cho template và `build_install`)
        for prefix, lst, attr in (("h.", "headers", "name"), ("e.", "env", "name"), ("v.", "variables", "name"), ("a.", "arg_fields", "key")):
            for fld in o.get(lst) or []:
                fld["fkey"] = _field_key(prefix, str(fld.get(attr) or ""))
    opt = next((o for o in opts if o.get("id") == opt_id), opts[0] if opts else None)
    return {"entry": res.get("server") or {}, "options": opts, "opt": opt, "suggested": res.get("suggested_name") or "mcp"}


async def install_page(request: Request) -> Response:
    reg_name = str(request.query_params.get("name") or "")
    if not reg_name:
        return flash_redirect(request, "/mcp/browse", "error", "Chọn một server trong danh mục")
    ctx = await _install_ctx(request, reg_name, request.query_params.get("opt"))
    return await render(request, "mcp_install.html", {"nav": "mcp", **ctx, "v": {"name": ctx["suggested"]}, "error": None})


def build_install(opt: dict[str, Any], entry: dict[str, Any], form: FormData) -> tuple[dict[str, Any], dict[str, str]]:
    """Form cài → (cấu hình `mcp.add`, {tên secret: giá trị}). Trường bí mật KHÔNG vào cấu hình — chỉ `${TÊN}`."""
    from ..mcp_client.catalog import secret_name

    name = str(form.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("tên server: chữ, số, '-', '_', bắt đầu bằng chữ/số, ≤ 63 ký tự")
    secrets: dict[str, str] = {}
    cfg: dict[str, Any] = {"name": name, "transport": opt["transport"], "trust": "trusted" if form.get("trust") == "trusted" else "untrusted",
                           "description": str(form.get("description") or "").strip()[:500]}

    def value(prefix: str, fld: dict[str, Any], key: str) -> str | None:
        raw = str(form.get(_field_key(prefix, fld["name"])) or "").strip()
        if not raw:
            if fld.get("required"):
                raise ValueError(f"thiếu {fld['name']}")
            return None
        if fld.get("secret") and not is_ref(raw):
            sn = secret_name(name, key)
            secrets[sn] = raw
            return f"${{{sn}}}"
        return raw

    if opt["kind"] == "remote":
        url = str(form.get("url") or opt["url"]).strip()
        for var in opt.get("variables") or []:
            v = str(form.get(_field_key("v.", var["name"])) or var.get("default") or "").strip()
            if not v and "{" + var["name"] + "}" in url:
                raise ValueError(f"thiếu biến URL {var['name']}")
            if not re.fullmatch(r"[A-Za-z0-9._~-]{0,200}", v):
                raise ValueError(f"biến URL {var['name']}: chỉ chữ, số, . _ ~ -")
            url = url.replace("{" + var["name"] + "}", v)
        if "{" in url:
            raise ValueError("URL còn biến chưa điền")
        cfg["url"] = url
        headers = {}
        for h in opt.get("headers") or []:
            v = value("h.", h, h["name"])
            if v is not None:
                headers[h["name"]] = v
        if headers:
            cfg["headers"] = headers
    else:
        try:
            parts = shlex.split(str(form.get("command") or "").strip())
        except ValueError as e:
            raise ValueError(f"lệnh chạy: {e}") from None
        if not parts:
            raise ValueError("thiếu lệnh chạy")
        args = parts[1:]
        for a in opt.get("arg_fields") or []:
            raw = str(form.get(_field_key("a.", a["key"])) or "").strip()
            if not raw:
                if a.get("required"):
                    raise ValueError(f"thiếu {a['name']}")
                continue
            if a.get("secret") and not is_ref(raw):
                sn = secret_name(name, str(a.get("named") or a["key"]).lstrip("-"))
                secrets[sn] = raw
                raw = f"${{{sn}}}"
            args += ([a["named"]] if a.get("named") else []) + [raw]
        cfg["command"], cfg["args"] = parts[0], args
        env = {}
        for e in opt.get("env") or []:
            v = value("e.", e, e["name"])
            if v is not None:
                env[e["name"]] = v
        if env:
            cfg["env"] = env
    return cfg, secrets


def install_confirm(cfg: dict[str, Any], secrets: dict[str, str]) -> str:
    what = (cfg.get("command", "") + " " + " ".join(cfg.get("args") or [])).strip() if cfg["transport"] == "stdio" else cfg.get("url", "")
    trust = "TIN CẬY (tool ghi chạy không cần duyệt)" if cfg["trust"] == "trusted" else "cần duyệt"
    if cfg.get("auth"):
        return f"Cài MCP server {cfg['name']} ({trust})?\nServer cần đăng nhập OAuth — cài xong bấm Kết nối ở trang server:\n{what}"
    return (f"Cài MCP server {cfg['name']} ({trust})?\nHub sẽ CHẠY NGAY để kiểm tra, ngoài sandbox, dưới quyền của bạn:\n{what}"
            + (f"\nSecret sẽ lưu vào secret store: {', '.join(sorted(secrets))}" if secrets else ""))


async def install_submit(request: Request) -> Response:
    _, form = await post_guard(request)
    reg_name = str(form.get("reg_name") or "")
    ctx = await _install_ctx(request, reg_name, str(form.get("opt") or "") or None)
    opt = ctx["opt"]
    if opt is None:
        return flash_redirect(request, "/mcp/browse", "error", "Server này không có cách cài tự động được hỗ trợ — thêm tay ở trang MCP")
    # giá trị form để vẽ lại khi lỗi — KHÔNG gồm trường bí mật
    secret_keys = {_field_key("h.", h["name"]) for h in opt.get("headers") or [] if h.get("secret")} | \
                  {_field_key("e.", e["name"]) for e in opt.get("env") or [] if e.get("secret")} | \
                  {_field_key("a.", a["key"]) for a in opt.get("arg_fields") or [] if a.get("secret")}
    keep = {k: v for k, v in carry(form) if k not in secret_keys}

    async def again(err: str) -> Response:
        return await render(request, "mcp_install.html", {"nav": "mcp", **ctx, "v": keep, "error": err}, status=400)

    try:
        cfg, secrets = build_install(opt, ctx["entry"], form)
    except ValueError as e:
        return await again(f"Chưa đủ thông tin: {e}")
    if opt.get("needs_oauth") and cfg["transport"] != "stdio":
        cfg["auth"] = {"type": "oauth"}
    if form.get("confirm") != "yes":
        # có JS thì hộp xác nhận đã hiện trước khi gửi; không JS → yêu cầu xác nhận lại (không mang secret sang trang khác)
        return await again("Cần xác nhận: " + install_confirm(cfg, secrets))
    try:
        existing = {i.get("name") for i in await rpc(request, "secret.list") or [] if not i.get("missing")}
    except RpcError as e:
        return await again(err_text(e))
    clash = sorted(n for n in secrets if n in existing)
    if clash and form.get("overwrite_secrets") != "1":
        return await again(f"Secret đã có: {', '.join(clash)} — tích 'Ghi đè secret đã có' hoặc đổi tên server")
    try:
        added = await rpc(request, "mcp.add", {"config": cfg, "probe": False}, audit=True)
    except RpcError as e:
        return await again(err_text(e))
    for sn, sv in secrets.items():
        try:
            await rpc(request, "secret.set", {"name": sn, "value": sv}, audit=True)
        except RpcError as e:
            return flash_redirect(request, f"/mcp/{cfg['name']}", "error", f"Đã thêm MCP {cfg['name']} nhưng chưa lưu được secret {sn}: {err_text(e)}")
    from .app import _probe_msg

    if cfg.get("auth"):  # chưa có token → probe chắc chắn 401; người bấm Kết nối ở trang server (probe chạy sau khi đăng nhập)
        return flash_redirect(request, safe_next(f"/mcp/{cfg['name']}#oauth", "/mcp"), "ok",
                              f"Đã cài MCP {added.get('name')} — bấm Kết nối để đăng nhập OAuth")
    try:
        pr = await rpc(request, "mcp.probe", {"name": cfg["name"]})
        msg = f"Đã cài MCP {added.get('name')} — {_probe_msg(pr)}"
    except RpcError as e:
        msg = f"Đã cài MCP {added.get('name')} — kiểm tra lỗi: {err_text(e)}"
    return flash_redirect(request, safe_next(f"/mcp/{cfg['name']}", "/mcp"), "ok", msg)


def routes() -> list[Any]:
    """Đăng ký TRƯỚC `/mcp/{name}` cũ (Starlette khớp theo thứ tự): `/mcp/browse` không được rơi vào `/mcp/{name}`."""
    from starlette.routing import Route

    return [
        Route("/mcp/browse", browse),
        Route("/mcp/browse/install", install_page),
        Route("/mcp/browse/install", install_submit, methods=["POST"]),
        Route("/mcp/agent/{id}", agent_config),
        Route("/mcp/{name}", mcp_show),
        Route("/mcp/{name}/tool", mcp_tool, methods=["POST"]),
    ]


__all__ = ["routes", "register_actions", "tool_rows", "tool_patch", "edit_params", "build_install"]
