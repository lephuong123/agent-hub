"""Trang MCP mở rộng: sửa chính sách server, điều khiển từng tool, xem MCP theo agent.

MCP bên thứ ba do `claude` giữ theo tài khoản (cấu hình kết nối + đăng nhập — Cài đặt › Tài khoản Claude › «MCP của tài khoản»);
hub chỉ giữ chính sách (trust, include/exclude/require_approval, ghi đè annotations, mô tả, timeout).

- `GET /mcp/{name}`: chi tiết + form sửa chính sách (action `mcp_edit` = RPC `mcp.update`, hộp xác nhận) + bảng tool có nút chặn /
  cho phép / luôn hỏi duyệt / ghi đè annotations (POST `/mcp/{name}/tool` → `mcp.update{patch.tools}`, ngang `cfharness mcp edit
  --include…` và `mcp annotate`). Bảng tool dựng từ annotations còn lưu của lần probe ở bản cũ (≤ v0.2.30); tool có mô tả đáng ngờ
  (`suspicious`) bị chặn mặc định cho tới khi người cho phép rõ ràng.
- `GET /mcp/agent/{id}`: `mcp.agent_config` — server + luật allow/deny sẽ dùng khi chạy agent.
Mọi thao tác ghi: `post_guard` (CSRF + Origin + khoá phiên WT-1), `_audit.channel=web` qua `rpc(..., audit=True)`.
"""

from __future__ import annotations

import re
from typing import Any

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import Response

from ..mcp_client.config import ToolFilter, annotation_override, match_any, tool_selected
from ..rpc import RpcError
from .app import carry, err_text, flash_redirect, post_guard, render, rpc


# ============================================================================ tiện ích form
def _globs(raw: Any) -> list[str]:
    return [x.strip() for x in re.split(r"[,\n]", str(raw or "")) if x.strip()]


def edit_params(f: FormData) -> dict[str, Any]:
    """Form sửa MCP (`/mcp/{name}`) → params `mcp.update` (ngang `cfharness mcp edit`): chỉ chính sách."""
    from .app import _num, _req

    name = _req(f, "name", "tên server")
    patch: dict[str, Any] = {"description": str(f.get("description") or "").strip(),
                             "trust": "trusted" if f.get("trust") == "trusted" else "untrusted"}
    t = _num(f.get("timeout_s"), float, "Timeout")
    if t is not None:
        patch["timeout_s"] = t
    patch["tools"] = {"include": _globs(f.get("include")), "exclude": _globs(f.get("exclude")),
                      "require_approval": _globs(f.get("require_approval"))}
    return {"name": name, "patch": patch}


def edit_confirm(f: FormData) -> str:
    try:
        p = edit_params(f)
    except ValueError as e:
        return f"Lưu MCP server {f.get('name')}? ({e})"
    trust = "TIN CẬY (tool ghi chạy không cần duyệt)" if p["patch"]["trust"] == "trusted" else "cần duyệt"
    return f"Lưu chính sách MCP server {p['name']} ({trust})?"


def edit_ok(r: Any, f: FormData) -> str:
    r = r or {}
    msg = f"Đã lưu MCP {r.get('name') or f.get('name')}"
    if r.get("warnings"):
        msg += " — " + "; ".join(r["warnings"])
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
    form = {"description": cfg.get("description") or "", "trust": s.get("trust"), "timeout_s": cfg.get("timeout_s") or "",
            "include": "\n".join(tools.get("include") or []), "exclude": "\n".join(tools.get("exclude") or []),
            "require_approval": "\n".join(tools.get("require_approval") or [])}
    rows = tool_rows(s)
    return await render(request, "mcp_show.html", {"nav": "mcp", "s": s, "f": form, "rows": rows, "annotations": tools.get("annotations") or {},
                                                   "blocked_suspicious": [r for r in rows if r["suspicious"] and r["state"] == "blocked"]})


# ============================================================================ cấu hình MCP theo agent
async def agent_config(request: Request) -> Response:
    aid = request.path_params["id"]
    res = await rpc(request, "mcp.agent_config", {"agent": aid}) or {}
    servers = [{"name": name, "entry": dict(e or {})} for name, e in sorted((res.get("mcpServers") or {}).items())]
    rules = res.get("tool_rules") or {}
    return await render(request, "mcp_agent.html", {"nav": "mcp", "agent_id": aid, "servers": servers, "rules": rules,
                                                    "problems": res.get("problems") or []})


def routes() -> list[Any]:
    """Đăng ký TRƯỚC `/mcp/{name}` (Starlette khớp theo thứ tự): `/mcp/agent/{id}` không được rơi vào `/mcp/{name}`."""
    from starlette.routing import Route

    return [
        Route("/mcp/agent/{id}", agent_config),
        Route("/mcp/{name}", mcp_show),
        Route("/mcp/{name}/tool", mcp_tool, methods=["POST"]),
    ]


__all__ = ["routes", "register_actions", "tool_rows", "tool_patch", "edit_params"]
