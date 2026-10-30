"""Khối tri thức lắp vào system prompt (FR-RT-10, M4): memory snapshot + chỉ mục skill + hướng dẫn `skill_view`.

Thứ tự ổn định (giữ prompt cache): memory global → team → agent (USER rồi MEMORY) → chỉ mục skill (tầng agent → team →
global, mô tả ≤ 60 ký tự) → hướng dẫn đọc skill. Nội dung là SNAPSHOT lúc dựng run: thay đổi memory/skill chỉ có hiệu
lực từ session mới (run resume dùng lại đúng system prompt của session — `runs._prepare_spec`).
Viết tiếng Anh (Q-14).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from .store import ENTRY_DELIMITER, memory_chars, parse_memory

if TYPE_CHECKING:
    from .store import KnowledgeStore

DESC_MAX = 60
_SEP = "═" * 46


def _usage(chars: int, limit: int) -> str:
    pct = int(round(100 * chars / limit)) if limit else 0
    return f"{pct}% — {chars}/{limit} chars"


def _block(title: str, entries: list[str], limit: int) -> str:
    content = ENTRY_DELIMITER.join(_safe(e) for e in entries)
    return f"{_SEP}\n{title} [{_usage(memory_chars(entries), limit)}]\n{_SEP}\n{content}"


def _safe(entry: str) -> str:
    """Quét lúc nạp (như Hermes): mục có mẫu injection/Unicode ẩn — kể cả mục sửa tay ngoài pipeline — không vào prompt."""
    from ..learning.threats import scan_load

    found = scan_load(entry)
    return f"[BLOCKED: this entry failed the safety scan ({', '.join(found[:3])}) and was not loaded]" if found else entry


# Như MEMORY_GUIDANCE của Hermes: chỉ khi agent có tool `memory` (learning.agent_memory_tool)
MEMORY_GUIDANCE = (
    "# Memory\n"
    "You have persistent memory across sessions (the MEMORY / USER PROFILE blocks above; a snapshot taken when this session started). "
    "Use the hub-self tool `memory` to keep it useful: save when the user corrects you or states a preference (target='user'), and when "
    "you learn a stable fact about the environment, projects or tools that will save time later (target='memory'). Keep entries short, "
    "declarative and in English. Do not save task progress, PR/ticket status, outcomes of this session or anything easy to re-discover. "
    "When memory is near its limit, merge or remove stale entries in the same call (`operations`). Procedures for classes of tasks "
    "belong in skills, not in memory.")


def short_desc(desc: str) -> str:
    d = " ".join((desc or "").split())
    return d if len(d) <= DESC_MAX else d[: DESC_MAX - 1].rstrip() + "…"


def build_knowledge_blocks(ks: "KnowledgeStore", agent_id: str, home: Path | None = None) -> tuple[list[str], dict[str, Any]]:
    """Trả (các khối text theo thứ tự, thông tin phụ: số skill…). `home` = home hiệu lực của run (run eval của M4-B dùng
    BẢN SAO home) — tầng agent đọc thẳng từ filesystem của `home`."""
    blocks: list[str] = []
    info: dict[str, Any] = {"skills": 0}
    tiers = ks.agent_tiers(agent_id)
    # (4) memory: global → team → agent (tầng hẹp đứng sau)
    for scope, owner in reversed(tiers):
        stores = ("user", "memory") if scope != "team" else ("memory",)
        for store in stores:
            try:
                if scope == "agent" and home is not None:
                    p = home / ("USER.md" if store == "user" else "MEMORY.md")
                    entries = parse_memory(p.read_text(encoding="utf-8")) if p.exists() else []
                else:  # M5-A: tầng team/global theo phiên bản gán cho agent (canary) / ứng viên eval
                    entries = ks.versions.read_memory(agent_id, scope, owner, store, home=home)
            except Exception:
                entries = []
            if not entries:
                continue
            label = {"agent": "", "team": f" — team {owner}", "global": " — shared"}[scope]
            title = ("USER PROFILE (who the user is)" if store == "user" else "MEMORY (your notes)" if scope == "agent" else "MEMORY") + label
            blocks.append(_block(title, entries, ks.memory_limit(scope, store)))
    try:
        mem_tool = bool(ks.hub.config.get("learning.agent_memory_tool")) and bool(ks.hub.config.get("learning.knowledge_tools"))
    except Exception:  # pragma: no cover
        mem_tool = False
    if mem_tool:
        blocks.append(MEMORY_GUIDANCE)
    # (5) chỉ mục skill
    skills = ks.effective_skills(agent_id, home=home)
    info["skills"] = len(skills)
    if skills:
        lines = ["# Skills index",
                 "Skills are procedures for classes of tasks, written for this user. Before you start a task, check this index; "
                 "if a skill matches, load it with the hub-self MCP tool `skill_view(name)` and follow it (support files: "
                 "`skill_view(name, file_path=...)`)."]
        for s in skills:
            tier = "" if s["scope"] == "agent" else f" [{s['scope']}]"
            lines.append(f"- {s['name']}: {short_desc(s.get('description') or '')}{tier}")
        blocks.append("\n".join(lines))
    return blocks, info
