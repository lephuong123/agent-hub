"""Định tuyến task giao cho team (FR-TK-08b): chọn worker theo `role`/`description` khớp nhất, rồi ít tải nhất."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Callable

STOP = {"the", "and", "for", "with", "this", "that", "from", "into", "của", "và", "cho", "các", "những", "một", "được", "trong", "task", "agent"}
WORK_ROLES = ("worker",)
REVIEW_ROLES = ("reviewer", "verifier")


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def tokens(*texts: str | None) -> set[str]:
    out: set[str] = set()
    for t in texts:
        for w in re.findall(r"\w+", _fold(t or "")):
            if len(w) >= 3 and w not in STOP and not w.isdigit():
                out.add(w)
                if len(w) > 5:
                    out.add(w[:5])  # gốc từ thô: research/researcher, write/writer
    return out


def score(task: dict[str, Any], member: dict[str, Any]) -> float:
    agent_toks = tokens(member.get("agent_role"), member.get("agent_description"), member.get("agent_id"))
    s = 0.0
    role = task.get("role")
    if role:
        rt = tokens(role)
        s += 3.0 * len(rt & agent_toks)
    s += len(tokens(task.get("title"), task.get("body")) & agent_toks) * 1.0
    return s


def choose_worker(task: dict[str, Any], members: list[dict[str, Any]], load: Callable[[str], int], *, default: str | None = None) -> str | None:
    cands = [m for m in members if m["role"] in WORK_ROLES and not m.get("agent_archived_at")]
    if not cands:
        cands = [m for m in members if m["role"] not in ("lead", *REVIEW_ROLES) and not m.get("agent_archived_at")]
    if not cands:
        return default
    ranked = sorted(enumerate(cands), key=lambda im: (-score(task, im[1]), load(im[1]["agent_id"]), im[0]))
    best = ranked[0][1]
    if score(task, best) == 0 and default and any(m["agent_id"] == default for m in cands):
        return default
    return best["agent_id"]


def choose_reviewer(members: list[dict[str, Any]], load: Callable[[str], int], exclude: str | None = None) -> str | None:
    cands = [m for m in members if m["role"] in REVIEW_ROLES and not m.get("agent_archived_at") and m["agent_id"] != exclude]
    if not cands:
        return None
    return sorted(enumerate(cands), key=lambda im: (load(im[1]["agent_id"]), 0 if im[1]["role"] == "reviewer" else 1, im[0]))[0][1]["agent_id"]
