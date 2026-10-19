"""Lắp khối system prompt của agent (FR-RT-10).

Thứ tự ổn định (để giữ prompt cache): (1) quy ước hub → (2) SOUL → (3) charter team (hardening H-15: run có `team_id`;
trước đây nằm trong tin nhắn user) → (4) memory global/team/agent → (5) chỉ mục skill + (6) hướng dẫn đọc skill qua
`skill_view` (M4, `knowledge/prompt_blocks.py`). Charter ổn định theo (agent, team) → hash ổn định giữa các run cùng team.
Nội dung động (task, comment, vai trò trong task) KHÔNG vào đây mà vào tin nhắn user.
Persona/prompt viết bằng tiếng Anh (Q-14).
"""

from __future__ import annotations

import hashlib

HUB_CONVENTIONS = """# cfharness conventions
You are running as a managed agent inside cfharness (agent id: {agent_id}).
- You run non-interactively; nobody can answer questions mid-run. Make reasonable assumptions and state them.
- Work only inside your working directory unless the task explicitly says otherwise.
- Never perform destructive or irreversible operations (deleting data, force-pushing, dropping tables) unless the task explicitly requires it.
- Bash runs in an OS sandbox. If a command fails with "Operation not permitted" on a file in your workspace (e.g. under .claude/),
  change that file with the Edit/Write tools instead: they are not sandboxed and cfharness decides on them.
- Finish with a short plain-text summary of the outcome."""


def charter_block(team_id: str, team_name: str, charter: str) -> str:
    return f"# Team charter (team '{team_name}', id {team_id})\n{charter.strip()}" if charter and charter.strip() else ""


def build_system_prompt(agent_id: str, soul: str, knowledge_blocks: list[str] | None = None, charter: str | None = None) -> str:
    parts = [HUB_CONVENTIONS.format(agent_id=agent_id).strip()]
    if soul and soul.strip():
        parts.append("# Persona (SOUL)\n" + soul.strip())
    if charter and charter.strip():
        parts.append(charter.strip())
    parts.extend(b.strip() for b in knowledge_blocks or [] if b and b.strip())
    return "\n\n".join(parts) + "\n"


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
