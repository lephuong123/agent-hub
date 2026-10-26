"""Phân loại tool Claude in Chrome (`mcp__claude-in-chrome__*`) — bảng dựng sẵn, THUẦN (docs/dev-notes/chrome.md).

Server `claude-in-chrome` do CLI tự nạp (`--chrome`, source "dynamic"), không có trong registry nên không có annotations probe →
không có bảng này thì mọi tool rơi vào `mcp_unknown_tool` (luôn giữ). Lớp:
- đọc → `safe`;
- `navigate` / `tabs_create_mcp` → URL qua CÙNG luật WebFetch (url_safety tĩnh ở đây; DNS + guard.web allow/deny ở GuardService.net_check)
  rồi `write` (`chrome_navigate`, audit);
- hành động (click/gõ/phím/form/đóng tab/đổi cửa sổ…) → `uncertain` luật `chrome_action`: strict giữ chờ người; balanced/autonomous
  cho chạy + audit (cùng cơ chế `guard.hold_uncertain`);
- `javascript_tool` / `file_upload` / `upload_image` → `uncertain` luật `chrome_sensitive` (∈ ALWAYS_HOLD_RULES: giữ ở MỌI profile);
- `browser_batch` → mức NẶNG NHẤT của các hành động con (lồng nhau/không đọc được → `chrome_action`);
- tool chrome lạ (phiên bản mới) → `mcp_unknown_tool`.
Ghi đè của NGƯỜI (`runtime.claude.chrome.annotations`, đặt bằng `cfharness mcp annotate claude-in-chrome <glob> …`) thắng bảng
nhưng KHÔNG gỡ luật URL/SSRF, tài nguyên bảo vệ. Agent không bật Chrome (`tool_policy.browser`) hoặc kill switch tắt → `forbidden`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..browser import CHROME_PREFIX, CHROME_SERVER

if TYPE_CHECKING:
    from .classifier import GuardContext, Verdict

READ_TOOLS = frozenset({
    "tabs_context_mcp", "list_connected_browsers", "read_page", "get_page_text", "find", "read_console_messages",
    "read_network_requests", "shortcuts_list",
})
URL_TOOLS = frozenset({"navigate", "tabs_create_mcp"})
ACTION_TOOLS = frozenset({
    "computer", "form_input", "browser_batch", "shortcuts_execute", "select_browser", "switch_browser", "resize_window", "tabs_close_mcp",
    "gif_creator",
})
SENSITIVE_TOOLS = frozenset({"javascript_tool", "file_upload", "upload_image"})
# `computer`: các action chỉ nhìn/cuộn (không click/gõ) → safe
COMPUTER_READ_ACTIONS = frozenset({"screenshot", "zoom", "wait", "scroll", "scroll_to", "hover"})
GIF_READ_ACTIONS = frozenset({"start_recording", "stop_recording", "clear"})
SENSITIVE_REASON = {
    "javascript_tool": "chạy JavaScript tuỳ ý trong trang đang đăng nhập của Chrome THẬT (đọc cookie/DOM, gửi request thay người)",
    "file_upload": "tải FILE trên máy lên trang web (có thể rò rỉ dữ liệu ra ngoài)",
    "upload_image": "tải ảnh (ảnh chụp màn hình) lên trang web (có thể rò rỉ nội dung đang hiển thị)",
}
MAX_BATCH = 50


def chrome_tool(tool_name: str) -> str:
    return tool_name[len(CHROME_PREFIX):] if tool_name.startswith(CHROME_PREFIX) else tool_name


def _url_of(tool: str, ti: dict[str, Any]) -> str | None:
    if tool not in URL_TOOLS:
        return None
    u = ti.get("url")
    if not isinstance(u, str) or not u.strip():
        return None
    u = u.strip()
    if tool == "navigate" and u.lower() in ("back", "forward"):
        return None
    if "://" not in u and not u.lower().startswith(("javascript:", "data:", "file:", "about:", "chrome:", "view-source:", "blob:")):
        u = "https://" + u  # extension mặc định https:// khi thiếu scheme
    return u


def _base(tool: str, ti: dict[str, Any], ctx: "GuardContext", depth: int = 0) -> "Verdict":
    from .classifier import SEVERITY, Verdict, url_verdict

    tgt = [f"{CHROME_SERVER}/{tool}"]
    if tool in READ_TOOLS:
        return Verdict("safe", "chrome_read")
    if tool in URL_TOOLS:
        url = _url_of(tool, ti)
        if url is not None:
            uv = url_verdict(url, f"Chrome {tool}")
            if uv is not None:
                return uv
            return Verdict("write", "chrome_navigate", "mcp", f"Chrome {tool} tới {url[:120]} (Chrome THẬT của người, audit)", [url[:200]], net_urls=[url])
        return Verdict("write", "chrome_navigate", "mcp", f"Chrome {tool} (không URL mới)", tgt)
    if tool in SENSITIVE_TOOLS:
        return Verdict("uncertain", "chrome_sensitive", "mcp", f"Chrome {tool}: {SENSITIVE_REASON[tool]}", tgt)
    if tool == "computer" and str(ti.get("action") or "") in COMPUTER_READ_ACTIONS:
        return Verdict("safe", "chrome_read")
    if tool == "gif_creator" and str(ti.get("action") or "") in GIF_READ_ACTIONS:
        return Verdict("safe", "chrome_read")
    if tool == "browser_batch":
        acts = ti.get("actions")
        if depth > 0 or not isinstance(acts, list) or not acts or len(acts) > MAX_BATCH:
            return Verdict("uncertain", "chrome_action", "mcp", "Chrome browser_batch: không đọc được danh sách hành động con → coi như hành động", tgt)
        worst: Verdict | None = None
        urls: list[str] = []
        for i, a in enumerate(acts):
            if not isinstance(a, dict) or not isinstance(a.get("name"), str) or not isinstance(a.get("input", {}), dict):
                return Verdict("uncertain", "chrome_action", "mcp", f"Chrome browser_batch: hành động con #{i + 1} không hợp lệ", tgt)
            sub = chrome_tool(a["name"])
            if sub == "browser_batch":
                return Verdict("uncertain", "chrome_action", "mcp", "Chrome browser_batch lồng nhau", tgt)
            v = classify_chrome_tool(sub, a.get("input") or {}, ctx, depth=depth + 1)
            urls += list(v.net_urls or [])
            if v.forbidden:
                return Verdict(v.verdict, v.rule, v.klass, f"browser_batch #{i + 1}: {v.reason}", v.targets, v.protected_id, True, urls)
            if worst is None or SEVERITY[v.verdict] > SEVERITY[worst.verdict]:
                worst = v
        assert worst is not None
        return Verdict(worst.verdict, worst.rule, worst.klass, f"browser_batch ({len(acts)} hành động), nặng nhất: {worst.reason or worst.rule}",
                       worst.targets, worst.protected_id, False, urls)
    if tool in ACTION_TOOLS:
        act = str(ti.get("action") or "")
        what = f"{tool}{f' {act}' if act else ''}"
        summ = str(ti.get("action_summary") or "")[:120]
        return Verdict("uncertain", "chrome_action", "mcp", f"Chrome {what}: thao tác trên Chrome THẬT của người" + (f" — {summ}" if summ else ""), tgt)
    return Verdict("uncertain", "mcp_unknown_tool", "mcp", f"tool Claude in Chrome lạ '{tool}' (chưa có trong bảng phân loại) → cần duyệt", tgt)


def _override(tool: str, ctx: "GuardContext") -> dict[str, Any]:
    if ctx.mcp_annotations is None:
        return {}
    try:
        ann = ctx.mcp_annotations(CHROME_SERVER, tool) or {}
    except Exception:
        return {}
    ov = ann.get("human_override")
    return ov if isinstance(ov, dict) else {}


def classify_chrome_tool(tool: str, ti: dict[str, Any], ctx: "GuardContext", depth: int = 0) -> "Verdict":
    from .classifier import Verdict

    prot = ctx.policy.match_mcp(CHROME_SERVER, tool)
    if prot is not None:
        return Verdict("destructive", "protected_mcp_tool", "mcp", f"tool MCP được bảo vệ ({prot.selector})", [f"{CHROME_SERVER}/{tool}"], prot.id)
    v = _base(tool, ti, ctx, depth)
    if v.forbidden or v.klass == "network" or tool == "browser_batch":
        return v  # luật URL/SSRF không gỡ được; batch đã áp ghi đè cho từng hành động con
    ov = _override(tool, ctx)
    if not ov:
        return v
    tgt = v.targets or [f"{CHROME_SERVER}/{tool}"]
    if ov.get("destructiveHint") is True:
        return Verdict("destructive", "chrome_human_destructive", "mcp", f"người đánh dấu Chrome {tool} là phá huỷ (runtime.claude.chrome.annotations)", tgt)
    if ov.get("readOnlyHint") is True:
        return Verdict("safe", "chrome_human_read_only", net_urls=v.net_urls)
    if ov.get("destructiveHint") is False and v.verdict == "uncertain":
        return Verdict("write", "chrome_human_override", "mcp", f"người ghi đè Chrome {tool} = không phá huỷ (phân loại gốc: {v.rule})", tgt, net_urls=v.net_urls)
    if ov.get("readOnlyHint") is False and v.verdict == "safe":
        return Verdict("write", "chrome_human_override", "mcp", f"người ghi đè Chrome {tool} = không chỉ đọc", tgt)
    return v


def classify_chrome(tool_name: str, ti: dict[str, Any], ctx: "GuardContext") -> "Verdict":
    from .classifier import Verdict

    tool = chrome_tool(tool_name)
    if ctx.chrome_allowed is False:
        return Verdict("destructive", "chrome_not_enabled", "mcp",
                       f"{tool_name}: Claude in Chrome không bật cho agent này (tool_policy.browser) hoặc người đã tắt (runtime.claude.chrome.enabled)",
                       [tool_name[:200]], forbidden=True)
    return classify_chrome_tool(tool, ti, ctx)
