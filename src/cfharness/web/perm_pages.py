"""Trang "Quyền đã cấp" `/permissions` (đề xuất #3, docs/dev-notes/web-permissions.md).

Một chỗ xem + gỡ mọi quyền NGƯỜI đã nới cho agent:
- cho phép theo agent + luật (`guard.grant.list|rm`, `hub_state.agent_rule_grants`);
- trust full theo agent (`guard.trust.list|set{mode: off}`) + mặc định cho agent mới (`guard.trust_new_agents`, chỉ xem);
- luật "luôn cho phép" (`guard.policy` → `always_allow`; gỡ = `guard.protect.rm {id: aa_…}` → `GuardService.always_rm`, vô hiệu hoá có lịch sử);
- ngoại lệ sandbox theo agent (localhost / domain / host dev — `guard.sandbox.*`);
- tài nguyên bảo vệ (`guard.policy` → `resources`; thêm `guard.protect.add`, gỡ `guard.protect.rm`).

Quy tắc bậc xác nhận: thao tác THU HẸP quyền (gỡ grant/trust/luật luôn cho phép/ngoại lệ sandbox, thêm tài nguyên bảo vệ) = 1 bước xác nhận;
thao tác NỚI quyền (gỡ một tài nguyên bảo vệ) = 2 bước, bước 2 nêu rõ tài nguyên sẽ hết được bảo vệ (`Action.step2`, `app.plan_action`).
Mọi method đều ∈ `auth.HUMAN_ONLY` và ngoài `MANAGER_ALLOWED`; service tự ghi audit theo `channel=web` (không ghi thêm ở web). Ghi `policy/`
đi qua `PolicyStore` của daemon (writer đã bọc `guard/integrity.attach` → chụp lại bản chuẩn). Web chỉ gọi RPC, không đọc `policy/`.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from ..rpc import RpcError
from .app import CHANNEL, render, rpc

PROTECT_KINDS = (("path", "Đường dẫn (file/thư mục, glob)"), ("repo", "Repo git"), ("db", "Cơ sở dữ liệu"), ("mcp_tool", "Tool MCP"))
PROTECT_OPS = (("delete", "xoá"), ("overwrite", "ghi đè"), ("write", "ghi"))
ALWAYS_KIND = {"exact": "đúng thao tác", "web_domain": "tên miền web", "mcp_tool": "tool MCP"}


async def _opt(request: Request, method: str, params: dict[str, Any] | None = None, default: Any = None) -> Any:
    try:
        return await rpc(request, method, params or {})
    except RpcError:
        return default


def grant_rows(grants: dict[str, Any] | None) -> list[dict[str, Any]]:
    """`guard.grant.list` {agent: {rule: {...}}} → hàng phẳng, sắp theo agent rồi luật (thuần)."""
    out = []
    for aid, rules in sorted((grants or {}).items()):
        for rule, g in sorted((rules or {}).items()):
            if isinstance(g, dict):
                out.append({"agent_id": aid, "rule": rule, **g})
    return out


def sandbox_rows(overrides: dict[str, Any] | None) -> list[dict[str, Any]]:
    """`guard.sandbox.list` → hàng mỗi ngoại lệ: {agent_id, kind: localhost|domain|dev_host, value, by, channel, at} (thuần).
    Host dev cũng nằm trong allow_domains (sandbox_dev_host thêm cả hai) — chỉ hiện MỘT lần dưới dạng host dev."""
    out = []
    for aid, o in sorted((overrides or {}).items()):
        if not isinstance(o, dict):
            continue
        meta = {"by": o.get("by"), "channel": o.get("channel"), "at": o.get("at")}
        if o.get("allow_localhost"):
            out.append({"agent_id": aid, "kind": "localhost", "value": "127.0.0.1 / ::1", **meta})
        dev = [str(h) for h in o.get("dev_hosts") or []]
        for h in dev:
            out.append({"agent_id": aid, "kind": "dev_host", "value": h, **meta})
        for d in o.get("allow_domains") or []:
            if str(d) not in dev:
                out.append({"agent_id": aid, "kind": "domain", "value": str(d), **meta})
    return out


def always_view(rule: dict[str, Any]) -> str:
    """Câu mô tả một luật luôn cho phép (chữ hub; giá trị là id/host/tên tool/vân tay do hub dựng)."""
    who = rule.get("agent_id") or "mọi agent"
    k = rule.get("kind")
    if k == "web_domain":
        return f"{who} mở trang trên {rule.get('domain')} (không gồm tên miền con)"
    if k == "mcp_tool":
        return f"{who} gọi tool {rule.get('tool')}"
    return f"{who} chạy lại ĐÚNG thao tác {rule.get('tool') or ''} (vân tay {str(rule.get('fingerprint') or '')[:12]})"


async def permissions_page(request: Request) -> Response:
    pol = await _opt(request, "guard.policy", {}, {}) or {}
    always = [r for r in pol.get("always_allow") or [] if isinstance(r, dict)]
    for r in always:
        r["_view"] = always_view(r)
    ctx = {
        "nav": "permissions",
        "grants": grant_rows(await _opt(request, "guard.grant.list", {}, {})),
        "trust": sorted((await _opt(request, "guard.trust.list", {}, {}) or {}).items()),
        "trust_new": await _opt(request, "config.get", {"key": "guard.trust_new_agents"}, "off"),
        "always": [r for r in always if not r.get("disabled")],
        "always_off": [r for r in always if r.get("disabled")][-20:],
        "sandbox": sandbox_rows(await _opt(request, "guard.sandbox.list", {}, {})),
        "protected": [r for r in pol.get("resources") or [] if isinstance(r, dict)],
        "gates": await _opt(request, "config.get", {"key": "gates.mode"}, "human") or "human",
        "profile": (await _opt(request, "approval.profile", {}, {}) or {}).get("profile"),
        "kinds": PROTECT_KINDS, "ops": PROTECT_OPS, "always_kind": ALWAYS_KIND,
    }
    return await render(request, "permissions.html", ctx)


# ============================================================================ thao tác (POST /do/<tên>)
def _who() -> dict[str, str]:
    return {"by": "user", "channel": CHANNEL}


def _always_id(f: Any, _req: Any) -> str:
    rid = _req(f, "id", "luật")
    if not rid.startswith("aa_"):  # cùng RPC guard.protect.rm gỡ cả tài nguyên bảo vệ — thao tác này chỉ cho luật luôn cho phép
        raise ValueError("id luật luôn cho phép phải bắt đầu bằng aa_")
    return rid


def _protect_id(f: Any, _req: Any) -> str:
    rid = _req(f, "id", "tài nguyên")
    if rid.startswith("aa_"):
        raise ValueError("đây là luật luôn cho phép, không phải tài nguyên bảo vệ")
    return rid


def _protect_add(f: Any, _req: Any) -> dict[str, Any]:
    kind = _req(f, "kind", "loại tài nguyên")
    if kind not in {k for k, _ in PROTECT_KINDS}:
        raise ValueError("loại tài nguyên không hợp lệ")
    ops = [str(o) for o in f.getlist("ops") if str(o) in {k for k, _ in PROTECT_OPS}] or [k for k, _ in PROTECT_OPS]
    return {"kind": kind, "selector": _req(f, "selector", "đích cần bảo vệ"), "ops": ops, "note": str(f.get("note") or "").strip(), **_who()}


def register_actions(actions: dict[str, Any], Action: Any, _req: Any) -> None:
    """Thao tác ghi của trang Quyền đã cấp (form POST `/do/<tên>`, CSRF + khoá phiên). Service tự audit kênh web → `audit=False`."""
    actions["perm_grant_rm"] = Action(
        "guard.grant.rm", lambda f: {"id": _req(f, "agent", "agent"), "rule": _req(f, "rule", "luật"), **_who()},
        lambda r, f: f"Đã gỡ — {f.get('agent')} lại bị hỏi duyệt khi gặp luật {f.get('rule')}",
        confirm=lambda f: f"Gỡ cho phép theo luật «{f.get('rule')}» của agent {f.get('agent')}?\nTừ nay thao tác khớp luật này của agent lại chờ duyệt.",
        audit=False)
    actions["perm_trust_off"] = Action(
        "guard.trust.set", lambda f: {"id": _req(f, "agent", "agent"), "mode": "off", **_who()},
        lambda r, f: f"Đã tắt trust full của {f.get('agent')} — guard áp lại ngay; run mới có lại sandbox",
        confirm=lambda f: f"Tắt trust full của agent {f.get('agent')}?\nGuard áp lại ngay cho mọi tool call; run đang chạy giữ sandbox/cờ cũ tới khi kết thúc.",
        audit=False)
    actions["perm_always_rm"] = Action(
        "guard.protect.rm", lambda f: {"id": _always_id(f, _req), **_who()},
        lambda r, f: f"Đã gỡ luật luôn cho phép {f.get('id')} (giữ trong lịch sử)",
        confirm=lambda f: f"Gỡ luật luôn cho phép {f.get('id')}?\n{f.get('summary') or ''}\nLần sau gặp thao tác này hub lại hỏi duyệt.",
        audit=False)
    actions["perm_domain_rm"] = Action(
        "guard.sandbox.allow_domain", lambda f: {"id": _req(f, "agent", "agent"), "domain": _req(f, "value", "domain"), "remove": True, **_who()},
        lambda r, f: f"Đã gỡ domain {f.get('value')} khỏi ngoại lệ mạng của {f.get('agent')} (áp dụng từ run kế tiếp)",
        confirm=lambda f: f"Gỡ domain {f.get('value')} khỏi ngoại lệ mạng của agent {f.get('agent')}?", audit=False)
    actions["perm_dev_host_rm"] = Action(
        "guard.sandbox.dev_host", lambda f: {"id": _req(f, "agent", "agent"), "host": _req(f, "value", "host"), "remove": True, **_who()},
        lambda r, f: f"Đã bỏ tin host dev {f.get('value')} của {f.get('agent')}",
        confirm=lambda f: f"Bỏ tin host dev {f.get('value')} của agent {f.get('agent')}?\nWebFetch/Chrome tới host này lại chờ duyệt; Bash không ra mạng tới nó.",
        audit=False)
    actions["perm_localhost_off"] = Action(
        "guard.sandbox.set", lambda f: {"id": _req(f, "agent", "agent"), "allow_localhost": False, **_who()},
        lambda r, f: f"Đã tắt ngoại lệ localhost của {f.get('agent')} (áp dụng từ run kế tiếp)",
        confirm=lambda f: f"Tắt ngoại lệ localhost của agent {f.get('agent')}?", audit=False)
    actions["perm_protect_add"] = Action(
        "guard.protect.add", lambda f: _protect_add(f, _req), lambda r, f: f"Đã bảo vệ {(r or {}).get('selector') or f.get('selector')} ({(r or {}).get('id')})",
        confirm=lambda f: f"Thêm tài nguyên bảo vệ ({f.get('kind')}): {f.get('selector')}?\nAgent muốn {', '.join(f.getlist('ops')) or 'xoá, ghi đè, ghi'} "
                          "đích này luôn phải chờ bạn duyệt.", audit=False)
    # NỚI quyền → 2 bước: bước 2 nêu rõ đích sẽ hết được bảo vệ
    actions["perm_protect_rm"] = Action(
        "guard.protect.rm", lambda f: {"id": _protect_id(f, _req), **_who()}, lambda r, f: f"Đã gỡ bảo vệ {f.get('selector') or f.get('id')}",
        confirm=lambda f: f"Gỡ tài nguyên bảo vệ {f.get('id')} ({f.get('selector') or '?'})?", danger=True, audit=False,
        step2=lambda f: (f"Xác nhận lần 2 — NỚI QUYỀN.\nSau khi gỡ, {f.get('selector') or f.get('id')} ({f.get('kind') or '?'}) KHÔNG còn là tài nguyên "
                         "được bảo vệ: thao tác xoá/ghi đè/ghi lên nó chỉ còn chịu luật phân loại thường (có thể không hỏi bạn nữa).\n"
                         "Thêm lại bất cứ lúc nào ở trang này."))


def routes() -> list[Any]:
    from starlette.routing import Route

    return [Route("/permissions", permissions_page)]


__all__ = ["register_actions", "routes", "grant_rows", "sandbox_rows", "always_view"]
