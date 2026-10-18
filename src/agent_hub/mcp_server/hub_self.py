"""MCP stdio nội bộ `hub-self` cho AGENT đang chạy task (FR-MS-08). Hub tự thêm vào `mcp.json` của run có task.

Bộ tool theo vai trò (`--role`):
- worker  : `task_show`, `task_comment`, `task_complete(summary, result?)`, `task_request_review(summary)`, `task_block(reason)`,
  `memory_search(query)` (hardening H-14, mọi vai trò task)
- lead    : worker + `task_create`, `task_link`, `team_roster`
- reviewer: `task_show`, `task_comment` (phán quyết trả bằng `--json-schema`)
- M4-A — mọi vai trò trên + `skills_list`, `skill_view` (FR-RT-10); `knowledge` (run thường có skill): chỉ hai tool đó;
  `learning` (review fork, FR-LN-03): `skills_list`, `skill_view`, `memory_view`, `propose_*` → RPC `learnself.call`
  (pipeline học tự áp dụng; review KHÔNG ghi file trực tiếp).
Mọi lời gọi gửi RPC `taskself.call {run_id, op, args}` tới daemon; daemon ràng với `run_id` (do hub cấp qua argv/env)
và chỉ cho thao tác task của chính run (FR-MS-08). Tự cài giao thức MCP tối thiểu (JSON-RPC theo dòng) như `hub-guard`
— chỉ stdlib, khởi động nhanh, không phụ thuộc thư viện `mcp`.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

SERVER_NAME = "hub-self"
PROTOCOL_DEFAULT = "2025-06-18"

_S = {"type": "string"}
TOOLS: dict[str, dict[str, Any]] = {
    "task_show": {
        "description": "Show your current task (title, body, parent results, comments). Optional task_id: a parent task, or (lead) a subtask.",
        "inputSchema": {"type": "object", "properties": {"task_id": _S}},
        "annotations": {"readOnlyHint": True},
    },
    "task_comment": {
        "description": "Add a comment to your current task (visible to the team and the human).",
        "inputSchema": {"type": "object", "properties": {"text": _S}, "required": ["text"]},
    },
    "task_complete": {
        "description": "Mark your current task as complete. Call once at the end, then finish your turn with a short summary.",
        "inputSchema": {"type": "object", "properties": {"summary": _S, "result": {"type": "object"}}, "required": ["summary"]},
    },
    "task_request_review": {
        "description": "Finish your task and ask for a review by a team reviewer.",
        "inputSchema": {"type": "object", "properties": {"summary": _S}, "required": ["summary"]},
    },
    "task_block": {
        "description": "Report that you cannot proceed without a human/decision. The task becomes blocked with your reason.",
        "inputSchema": {"type": "object", "properties": {"reason": _S}, "required": ["reason"]},
    },
    "task_create": {
        "description": "(lead) Create a subtask of your task. assignee = member id (see team_roster) or omit to route by role; parents = subtask ids it depends on.",
        "inputSchema": {
            "type": "object",
            "properties": {"title": _S, "body": _S, "assignee": _S, "role": _S, "parents": {"type": "array", "items": _S}},
            "required": ["title", "body"],
        },
    },
    "task_link": {
        "description": "(lead) Add a dependency: child waits for parent to be done.",
        "inputSchema": {"type": "object", "properties": {"parent_id": _S, "child_id": _S}, "required": ["parent_id", "child_id"]},
    },
    "memory_search": {
        "description": "Search your memory (agent/team/global MEMORY.md and USER.md), your skills and your past runs (prompt + summary) by keywords. "
                       "Read-only, keyword match (no LLM). scope: all | memory | skills | transcripts.",
        "inputSchema": {"type": "object", "properties": {"query": _S, "scope": {"type": "string", "enum": ["all", "memory", "skills", "transcripts"]},
                                                         "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, "required": ["query"]},
        "annotations": {"readOnlyHint": True},
    },
    "team_roster": {
        "description": "(lead) List team members with their roles and descriptions.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
}
# M4-A — tri thức (FR-RT-10: đọc skill theo nhu cầu) và review fork (FR-LN-03: chỉ ĐỀ XUẤT, hub tự áp dụng qua pipeline)
_PROPOSAL_NOTE = " agent-hub validates and applies it automatically (no human approval queue); the result says applied / deferred / blocked + reason."
LEARN_TOOLS: dict[str, dict[str, Any]] = {
    "skills_list": {
        "description": "List your skills (name, short description, tier). Skills are procedures for classes of tasks.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    "skill_view": {
        "description": "Load a skill's SKILL.md (or one support file via file_path, e.g. 'references/x.md'). Follow it when it matches your task.",
        "inputSchema": {"type": "object", "properties": {"name": _S, "file_path": _S}, "required": ["name"]},
        "annotations": {"readOnlyHint": True},
    },
    "memory_view": {
        "description": "Show the CURRENT entries of MEMORY.md (target='memory') or USER.md (target='user') with char usage/limit. "
                       "scope='team' shows your team's shared memory; scope='global' the hub-wide memory (target='user' = the shared user profile).",
        "inputSchema": {"type": "object", "properties": {"target": {"type": "string", "enum": ["memory", "user"]},
                                                         "scope": {"type": "string", "enum": ["agent", "team", "global"]}}},
        "annotations": {"readOnlyHint": True},
    },
    "propose_memory": {
        "description": "Change one memory entry. target='memory' (MEMORY.md: facts about the environment) or 'user' (USER.md: who the user is). "
                       "action add (content) | patch (old_text → content) | remove (old_text). Declarative facts, English, one fact in ONE store. "
                       "scope: 'agent' (default, your own notes) | 'team' (shared with your whole team) | 'global' (every agent) — team/global changes "
                       "are evaluated, tried on you first (canary) and then promoted to the others automatically." + _PROPOSAL_NOTE,
        "inputSchema": {"type": "object", "properties": {"target": {"type": "string", "enum": ["memory", "user"]}, "action": {"type": "string", "enum": ["add", "patch", "remove"]},
                                                         "scope": {"type": "string", "enum": ["agent", "team", "global"]},
                                                         "content": _S, "old_text": _S, "rationale": _S}, "required": ["target", "action"]},
    },
    "propose_skill_new": {
        "description": "Create a NEW class-level skill. name: lowercase-hyphen class name (no PR numbers/dates); description <= 60 chars; body: SKILL.md markdown "
                       "(procedure, pitfalls, user preferences). scope 'team'/'global' shares it (evaluated + canary on you first)." + _PROPOSAL_NOTE,
        "inputSchema": {"type": "object", "properties": {"name": _S, "description": _S, "body": _S, "rationale": _S,
                                                         "scope": {"type": "string", "enum": ["agent", "team", "global"]}}, "required": ["name", "description", "body"]},
    },
    "propose_skill_patch": {
        "description": "Edit an EXISTING skill. Targeted: old_string (exact, unique) → new_string. Or content = full new text of the file. file_path for a support file "
                       "under references/ templates/ scripts/ (content creates/overwrites it; remove_file=true removes it). Read-before-write: call skill_view on the "
                       "same file in this review first." + _PROPOSAL_NOTE,
        "inputSchema": {"type": "object", "properties": {"name": _S, "file_path": _S, "old_string": _S, "new_string": _S, "content": _S, "remove_file": {"type": "boolean"},
                                                         "rationale": _S}, "required": ["name"]},
    },
    "propose_skill_merge": {
        "description": "Merge skill `name` into umbrella `absorbed_into` (which must already contain the absorbed content — patch it first) and archive `name` "
                       "(soft delete, recoverable)." + _PROPOSAL_NOTE,
        "inputSchema": {"type": "object", "properties": {"name": _S, "absorbed_into": _S, "rationale": _S}, "required": ["name", "absorbed_into"]},
    },
    "propose_skill_archive": {
        "description": "Archive (soft delete) skill `name`; absorbed_into = the skill that now holds its content (required)." + _PROPOSAL_NOTE,
        "inputSchema": {"type": "object", "properties": {"name": _S, "absorbed_into": _S, "reason": _S, "rationale": _S}, "required": ["name", "absorbed_into"]},
    },
    "propose_config_change": {
        "description": "Change your own agent configuration: changes = {model, effort, fallback_model, tool_policy, mcp_servers, limits} and/or soul (full new SOUL.md). "
                       "Evaluated on golden tasks BEFORE applying; bounded by the human's config_bounds; never escalates destructive permissions." + _PROPOSAL_NOTE,
        "inputSchema": {"type": "object", "properties": {"changes": {"type": "object"}, "soul": _S, "rationale": _S}},
    },
    "propose_user_profile": {
        "description": "Change the SHARED user profile (global USER.md, read by every agent): action add|patch|remove, content, old_text. Evaluated before applying." + _PROPOSAL_NOTE,
        "inputSchema": {"type": "object", "properties": {"action": {"type": "string", "enum": ["add", "patch", "remove"]}, "content": _S, "old_text": _S, "rationale": _S},
                        "required": ["action"]},
    },
}
TOOLS.update(LEARN_TOOLS)
_KNOW = ("skills_list", "skill_view")
ROLE_TOOLS = {
    # hardening H-14 (FR-MS-08): memory_search cho mọi vai trò trong task
    "worker": ("task_show", "task_comment", "task_complete", "task_request_review", "task_block", "memory_search") + _KNOW,
    "lead": ("task_show", "task_comment", "task_complete", "task_request_review", "task_block", "task_create", "task_link", "team_roster", "memory_search") + _KNOW,
    "reviewer": ("task_show", "task_comment", "memory_search") + _KNOW,
    "knowledge": _KNOW,
    "learning": tuple(LEARN_TOOLS),
}

_out_lock = threading.Lock()


def server_entry(run_id: str, socket: Path, hub_home: Path, role: str, guard_cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    py = str((guard_cfg or {}).get("python") or sys.executable)
    return {
        "type": "stdio",
        "command": py,
        "args": ["-I", "-m", "agent_hub.mcp_server.hub_self", "--run", run_id, "--socket", str(socket), "--role", role],
        "env": {"AGENT_HUB_HOME": str(hub_home), "AGENT_HUB_RUN_ID": run_id},
    }


def _send(msg: dict) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _text(mid: Any, obj: Any, is_error: bool = False) -> None:
    _send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False, default=str) if not isinstance(obj, str) else obj}], "isError": is_error}})


def handle(msg: dict, run_id: str, sock: str, role: str) -> None:
    from ..guard.hook import rpc_call

    mid = msg.get("id")
    method = msg.get("method")
    if mid is None:
        return
    tools = ROLE_TOOLS.get(role, ROLE_TOOLS["worker"])
    try:
        if method == "initialize":
            pv = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_DEFAULT
            _send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": pv, "capabilities": {"tools": {"listChanged": False}},
                                                          "serverInfo": {"name": SERVER_NAME, "version": "1"},
                                                          "instructions": "agent-hub tools for the current run (task protocol, skills, learning proposals)."}})
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": n, **TOOLS[n]} for n in tools]}})
        elif method == "tools/call":
            p = msg.get("params") or {}
            name = p.get("name")
            if name not in tools:
                _text(mid, f"tool '{name}' is not available for role {role}", True)
                return
            try:
                if name == "memory_search":  # hardening H-14: RPC riêng (mcp_server/surface_rpc.py), ràng với agent của run
                    method = "memory.search_self"
                else:
                    method = "learnself.call" if name in LEARN_TOOLS else "taskself.call"
                res = rpc_call(sock, method, {"run_id": run_id, "op": name, "args": p.get("arguments") or {}}, 60)
            except Exception as e:  # daemon trả lỗi nghiệp vụ hoặc không với tới
                _text(mid, f"agent-hub error: {e}", True)
                return
            _text(mid, res)
        elif method in ("resources/list", "prompts/list"):
            _send({"jsonrpc": "2.0", "id": mid, "result": {method.split("/")[0]: []}})
        else:
            _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
    except BaseException as e:  # pragma: no cover
        _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(e)[:200]}})


def main() -> int:
    from ..guard.hook import _args

    a = _args(sys.argv[1:])
    run_id, sock, role = a.get("run", ""), a.get("socket", ""), a.get("role", "worker")
    threads: list[threading.Thread] = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if isinstance(msg, dict) and msg.get("method") == "tools/call":
            t = threading.Thread(target=handle, args=(msg, run_id, sock, role), daemon=True)
            t.start()
            threads.append(t)
        elif isinstance(msg, dict):
            handle(msg, run_id, sock, role)
    for t in threads:
        t.join(65)
    return 0


if __name__ == "__main__":
    sys.exit(main())
