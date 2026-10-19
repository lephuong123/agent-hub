"""Xin phép người bằng câu hỏi có nút (`ask_owner_permission`): điều phối viên ĐỀ XUẤT, NGƯỜI bấm, HUB thực hiện.

Mục tiêu UX: người chỉ nhắn và trả lời câu hỏi — không gõ `cfharness approve …`, `guard sandbox …`. Bất biến an toàn giữ nguyên:
- Điều phối viên KHÔNG BAO GIỜ tự thực hiện thao tác này: nó chỉ tạo câu hỏi (status `open`, routed người — `question.answer` của
  manager bị từ chối khi câu hỏi không `with_manager`). Thao tác chỉ chạy khi NGƯỜI chọn phương án 1 ("Cho phép").
- Thao tác được CHỐT lúc tạo câu hỏi (`questions.action` = {kind, method, params}) và chữ mô tả do HUB dựng từ dữ liệu thật
  (approval trong DB, cấu hình agent hiện tại → mới); lý do của điều phối viên chỉ là ghi chú đánh dấu rõ. Đổi gì sau đó cũng không
  đổi được thao tác người đã thấy.
- Thực hiện qua `Daemon.call_as(<người, internal>)` → cổng thường + bọc audit (actor `user`, kênh trả lời).
- Không có loại nào đụng agent điều phối viên, `bypassPermissions`/`allow_bypass`/`extra_args`/`subagents`, policy, golden, purge.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from ..errors import Conflict, HubError

if TYPE_CHECKING:
    from ..hub import Hub

KINDS = ("approve_action", "sandbox_localhost", "sandbox_domain", "agent_update")
OPTIONS = [{"label": "Cho phép", "description": "hub thực hiện ngay thao tác ở trên"}, {"label": "Từ chối", "description": "không làm gì"}]
ALLOW_KEY = "1"
# trường cấu hình agent người có thể cho phép qua nút (mọi thứ khác → người tự sửa bằng web/CLI)
AGENT_FIELDS = ("name", "role", "description", "model", "effort", "fallback_model", "tool_policy", "mcp_servers", "limits", "workspace_default")
TOOL_POLICY_FIELDS = ("permission_mode", "tools", "allowed_tools", "disallowed_tools")
_DOMAIN_RE = re.compile(r"^(\*\.)?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$|^localhost$", re.I)
PREVIEW_MAX = 1200


def _short(v: Any, n: int = 160) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


def _manager_ids(hub: "Hub") -> set[str]:
    mgr = getattr(hub, "manager", None)
    return set(mgr.perms.manager_ids()) if mgr is not None else set()


def _agent(hub: "Hub", agent_id: Any) -> dict[str, Any]:
    aid = str(agent_id or "").strip()
    if not aid:
        raise HubError("thiếu 'agent'")
    row = hub.store.get_agent(aid)
    if row is None or row.get("archived_at"):
        raise HubError(f"không có agent '{aid}'")
    if aid in _manager_ids(hub):
        raise HubError("không xin phép thay đổi cho agent điều phối viên — chỉ người tự sửa")
    return row


def _approval_text(ap: dict[str, Any]) -> str:
    from ..web.present import action_view

    view = action_view(ap)
    body = view["text"]
    if len(body) > PREVIEW_MAX:
        body = body[:PREVIEW_MAX] + f"\n… (cắt, đủ {view['length']} ký tự: `cfharness approvals` hoặc trang Duyệt)"
    why = ap.get("reason") or ap.get("matched_rule") or ""
    return (f"Agent `{ap['agent_id']}` muốn chạy {view['tool']} (phân loại: {ap.get('destructive_class') or ap.get('kind')}"
            + (f" — {why}" if why else "") + f", yêu cầu {ap['id']}):\n{body}")


def build(hub: "Hub", kind: str, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """→ (action chốt sẵn, mô tả do HUB dựng). Kiểm hợp lệ NGAY (lỗi trả về cho điều phối viên, không tạo câu hỏi)."""
    if kind not in KINDS:
        raise HubError(f"loại xin phép không hỗ trợ: {kind} (có: {', '.join(KINDS)})")
    if kind == "approve_action":
        aid = str(args.get("approval_id") or "").strip()
        ap = hub.approvals.get(aid)
        if ap["status"] not in ("open", "waiting_human"):
            raise Conflict(f"approval {ap['id']} đã {ap['status']}")
        return ({"kind": kind, "method": "approval.approve", "params": {"id": ap["id"]}, "target": f"approval:{ap['id']}"},
                _approval_text(ap))
    if kind == "sandbox_localhost":
        row = _agent(hub, args.get("agent"))
        return ({"kind": kind, "method": "guard.sandbox.set", "params": {"id": row["id"], "allow_localhost": True}, "target": f"agent:{row['id']}"},
                f"Cho agent `{row['id']}` kết nối mạng tới máy này (localhost/127.0.0.1 — vd NocoDB, dev server, Docker). "
                "Vẫn chặn socket của hub và mọi file token/secret của hub. Thu hồi: `cfharness guard sandbox allow-localhost "
                f"{row['id']} off`.")
    if kind == "sandbox_domain":
        row = _agent(hub, args.get("agent"))
        dom = str(args.get("domain") or "").strip().lower()
        if not _DOMAIN_RE.match(dom):
            raise HubError(f"domain không hợp lệ: {dom!r} (vd api.github.com hoặc *.example.com)")
        return ({"kind": kind, "method": "guard.sandbox.allow_domain", "params": {"id": row["id"], "domain": dom}, "target": f"agent:{row['id']}"},
                f"Cho agent `{row['id']}` truy cập mạng tới `{dom}` từ lệnh Bash.")
    # agent_update
    row = _agent(hub, args.get("agent"))
    changes = args.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise HubError("agent_update cần 'changes' (object)")
    bad = [k for k in changes if k not in AGENT_FIELDS]
    if bad:
        raise HubError(f"không xin phép được các trường: {', '.join(bad)} (được: {', '.join(AGENT_FIELDS)})")
    tp = changes.get("tool_policy")
    if tp is not None:
        if not isinstance(tp, dict):
            raise HubError("tool_policy phải là object")
        bad = [k for k in tp if k not in TOOL_POLICY_FIELDS]
        if bad:
            raise HubError(f"tool_policy không xin phép được: {', '.join(bad)}")
        if tp.get("permission_mode") == "bypassPermissions":
            raise HubError("bypassPermissions không xin qua nút — người tự đặt nếu thật sự cần")
        changes = {**changes, "tool_policy": {**(row.get("tool_policy") or {}), **tp}}
    lines = [f"Đổi cấu hình agent `{row['id']}`:"]
    for k, v in changes.items():
        lines.append(f"- {k}: {_short(row.get(k))} → {_short(v)}")
    return ({"kind": kind, "method": "agent.update", "params": {"id": row["id"], "changes": changes}, "target": f"agent:{row['id']}"},
            "\n".join(lines))


def question_text(desc: str, reason: Any) -> str:
    from ..questions.service import clean

    r = clean(reason, 400, oneline=True) if reason else ""
    return ("[Xin phép] " + desc + (f"\n\nLý do (điều phối viên ghi): {r}" if r else "")
            + "\n\nChọn 1 = Cho phép (hub thực hiện ngay), 2 = Từ chối.")


async def execute(hub: "Hub", action: dict[str, Any], *, by: str, channel: str) -> dict[str, Any]:
    """Người đã chọn "Cho phép" → thực hiện thao tác đã chốt với quyền NGƯỜI (cổng + audit của daemon)."""
    from .. import auth as _auth

    mgr = getattr(hub, "manager", None)
    d = getattr(mgr, "daemon", None) if mgr is not None else None
    params = dict(action.get("params") or {})
    if action.get("method") in ("guard.sandbox.set", "guard.sandbox.allow_domain"):
        params.update({"by": by, "channel": channel})
    if action.get("method") == "approval.approve":
        params.update({"by": by, "channel": channel})
    params["_audit"] = {"channel": channel, "actor": by}
    if d is None:
        raise HubError("cần daemon để thực hiện thao tác người cho phép")
    resp = await d.call_as(_auth.Caller(via="internal"), str(action["method"]), params)
    if "error" in resp:
        return {"ok": False, "error": str(resp["error"].get("message") or "lỗi")[:300]}
    return {"ok": True}
