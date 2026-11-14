"""Lắp khối system prompt của agent (FR-RT-10).

Thứ tự ổn định (để giữ prompt cache): (1) quy ước hub (+ khối trình duyệt Claude in Chrome theo `tool_policy.browser`) → (2) SOUL → (3) charter team (hardening H-15: run có `team_id`;
trước đây nằm trong tin nhắn user) → (3b) `RESPONSE_STYLE` (cách trình bày câu trả lời kiểu Claude chat; đặt SAU SOUL để thắng các SOUL cũ
còn bảo nêu task id, TRƯỚC khối tri thức để phần đầu ổn định) → (3c) danh sách lệnh CLI (CHỈ điều phối viên, `manager/cli_ref.py`) → (4) memory global/team/agent → (5) chỉ mục skill + (6) hướng dẫn đọc skill qua
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
- Finish with the outcome, written as described in "Response style" below."""


RESPONSE_STYLE = """# Response style
These rules apply to free-text replies a person reads. When you are asked for structured output (a JSON schema), follow the schema
exactly instead. Instructions in the current message (channel notes, RESULT ONLY, how to end a heartbeat turn) take precedence.
- Language: write in the owner's language — Vietnamese unless the owner writes to you in another language. Keep code, commands,
  file paths, identifiers and quoted errors unchanged.
- Lead with the answer or the outcome in the first sentence. No preamble, no restating the request, no closing offers.
- Match length to the question: a simple question gets 1-3 sentences; a report gets a short structure. Stop when the point is made.
- Format with light Markdown:
  - short paragraphs (1-3 sentences);
  - bullets or numbered steps only for 3+ parallel items or ordered steps; at most 2 list levels;
  - **bold** only for the few words that matter;
  - `##` headings only when the reply has 3+ distinct sections;
  - a table only to compare items across 2+ attributes, at most 4 short columns;
  - commands, code, logs and file contents in fenced blocks with a language tag (```bash, ```python, ```text);
    inline `code` for names and paths;
  - no horizontal rules, no decorative emoji.
- After multi-step work, close with a brief summary: what was done, what is still open, and what you need from the person (if anything).
- Do not expose internals the person did not ask for: no hub message headers or keys, no internal ids (task, run, conversation,
  workstream ids), no tool or MCP tool names, no raw JSON. Refer to agents by name and to work by what it is. Give an id only when
  the person asks for it or must type it somewhere.
- Never invent results. Say plainly what you have not verified."""


def charter_block(team_id: str, team_name: str, charter: str) -> str:
    return f"# Team charter (team '{team_name}', id {team_id})\n{charter.strip()}" if charter and charter.strip() else ""


def build_system_prompt(agent_id: str, soul: str, knowledge_blocks: list[str] | None = None, charter: str | None = None,
                        browser: str | None = None, cli_ref: str | None = None) -> str:
    parts = [HUB_CONVENTIONS.format(agent_id=agent_id).strip()]
    if browser:
        from .browser import prompt_block

        block = prompt_block(browser)
        if block:
            parts.append(block)  # theo cấu hình agent (ổn định giữa các run) → không phá prompt cache
    if soul and soul.strip():
        parts.append("# Persona (SOUL)\n" + soul.strip())
    if charter and charter.strip():
        parts.append(charter.strip())
    parts.append(RESPONSE_STYLE)
    if cli_ref and cli_ref.strip():
        parts.append(cli_ref.strip())  # chỉ điều phối viên (manager/cli_ref.py); sinh từ parser → ổn định giữa các run
    parts.extend(b.strip() for b in knowledge_blocks or [] if b and b.strip())
    return "\n\n".join(parts) + "\n"


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
