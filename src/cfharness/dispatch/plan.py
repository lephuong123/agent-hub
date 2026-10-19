"""JSON Schema cho run của lead (kế hoạch tách việc, FR-TK-08a) và reviewer (FR-TK-12) + kiểm hợp lệ phía hub.

`claude --json-schema` buộc model trả `structured_output` khớp schema; hub vẫn kiểm lại (key trùng, cha không tồn
tại, chu trình, assignee lạ). Không hợp lệ → run `protocol_violation`, task gốc `blocked`, không tạo task con nào.
"""

from __future__ import annotations

from typing import Any

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "subtasks": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "assignee": {"type": "string"},
                    "role": {"type": "string"},
                    "parents": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["key", "title", "body"],
            },
        },
    },
    "required": ["subtasks"],
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
        "comment": {"type": "string"},
    },
    "required": ["verdict", "comment"],
}


class PlanError(ValueError):
    pass


def validate_plan(plan: Any, members: list[dict[str, Any]], *, lead: str | None, max_subtasks: int = 20) -> list[dict[str, Any]]:
    """Trả danh sách subtask đã chuẩn hoá theo thứ tự topo: {key, title, body, assignee|None, role|None, parents:[key]}."""
    if not isinstance(plan, dict):
        raise PlanError("output của lead không phải object JSON (thiếu structured_output)")
    subs = plan.get("subtasks")
    if not isinstance(subs, list) or not subs:
        raise PlanError("kế hoạch không có subtasks")
    if len(subs) > max_subtasks:
        raise PlanError(f"kế hoạch có {len(subs)} subtask > giới hạn {max_subtasks}")
    member_ids = {m["agent_id"]: m for m in members}
    out: list[dict[str, Any]] = []
    keys: set[str] = set()
    for i, s in enumerate(subs):
        if not isinstance(s, dict):
            raise PlanError(f"subtask #{i} không phải object")
        key = str(s.get("key") or f"s{i + 1}").strip()
        if key in keys:
            raise PlanError(f"key subtask trùng: {key}")
        keys.add(key)
        title = str(s.get("title") or "").strip()
        body = str(s.get("body") or "").strip()
        if not title or not body:
            raise PlanError(f"subtask '{key}' thiếu title/body")
        assignee = s.get("assignee") or s.get("agent_id")
        role = s.get("role")
        if assignee and (assignee not in member_ids or assignee == lead or member_ids[assignee]["role"] in ("reviewer", "verifier")):
            role = role or str(assignee)  # id lạ → dùng làm gợi ý vai trò khi định tuyến
            assignee = None
        parents = s.get("parents") or []
        if not isinstance(parents, list) or not all(isinstance(p, str) for p in parents):
            raise PlanError(f"subtask '{key}': parents phải là danh sách key")
        out.append({"key": key, "title": title, "body": body, "assignee": assignee, "role": role, "parents": list(dict.fromkeys(parents))})
    for s in out:
        for p in s["parents"]:
            if p not in keys:
                raise PlanError(f"subtask '{s['key']}' phụ thuộc key không tồn tại: {p}")
            if p == s["key"]:
                raise PlanError(f"subtask '{s['key']}' phụ thuộc chính nó")
    # topo (Kahn) — phát hiện chu trình
    by_key = {s["key"]: s for s in out}
    indeg = {k: len(by_key[k]["parents"]) for k in by_key}
    order: list[dict[str, Any]] = []
    ready = [s["key"] for s in out if indeg[s["key"]] == 0]
    while ready:
        k = ready.pop(0)
        order.append(by_key[k])
        for s in out:
            if k in s["parents"]:
                indeg[s["key"]] -= 1
                if indeg[s["key"]] == 0:
                    ready.append(s["key"])
    if len(order) != len(out):
        raise PlanError("kế hoạch có chu trình phụ thuộc")
    return order


def validate_review(v: Any) -> tuple[str, str]:
    if not isinstance(v, dict) or v.get("verdict") not in ("approve", "request_changes"):
        raise PlanError("phán quyết review không hợp lệ (cần verdict approve|request_changes)")
    return v["verdict"], str(v.get("comment") or "")
