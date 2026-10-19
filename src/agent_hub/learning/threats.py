"""Quét nội dung tri thức trước khi áp dụng (FR-LN-07 mục 4): prompt injection, exfiltration, Unicode ẩn.

Danh sách mẫu `_PATTERNS` và `INVISIBLE_CHARS` phỏng theo hermes-agent `tools/threat_patterns.py`
(MIT, Copyright (c) 2025 Nous Research — xem NOTICE). Bản hub bỏ mẫu riêng của Hermes (`~/.hermes/.env`) và thêm
`POLICY_TOUCH_PATTERNS`: nội dung dặn sửa chính sách an toàn của hub (FR-SG-05) → chặn với lý do `policy_touch`.
"""

from __future__ import annotations

import re
import unicodedata

import unicodedata
from typing import List, Optional, Tuple

from .. import names as _names

# Hard cap on scanned text: scanners are advisory, so bound worst-case runtime.
MAX_SCAN_CHARS = 65_536
# Bounded filler between key attack words (unbounded ``(?:\w+\s+)*`` backtracks badly).
_FILLER = r"(?:\w+\s+){0,8}"
# Env var reference ending in a secret-ish suffix (see exfil comment below).
_SECRET_VAR = r"\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)S?\b"
# Verb prefix for "modify agent config" patterns.
_MODIFY = r"(update|modify|edit|write|change|append|add\s+to)\s+[^\n]{0,2048}"
# (regex, pattern_id, scope); scope ∈ {"all", "context", "strict"}
_PATTERNS: list[tuple[str, str, str]] = [
    # ── Classic prompt injection (applies everywhere) ────────────────
    (rf'ignore\s+{_FILLER}(previous|all|above|prior)\s+{_FILLER}instructions', "prompt_injection", "all"),
    (r'system\s+prompt\s+override', "sys_prompt_override", "all"),
    (rf'disregard\s+{_FILLER}(your|all|any)\s+{_FILLER}(instructions|rules|guidelines)', "disregard_rules", "all"),
    (rf'act\s+as\s+(if|though)\s+{_FILLER}you\s+{_FILLER}(have\s+no|don\'t\s+have)\s+{_FILLER}(restrictions|limits|rules)', "bypass_restrictions", "all"),
    (r'<!--[^>]{0,512}(?:ignore|override|system|secret|hidden)[^>]{0,512}-->', "html_comment_injection", "all"),
    (r'<\s*div\s+style\s*=\s*["\'][^>]{0,2048}display\s*:\s*none', "hidden_div", "all"),
    (
        r"translate\s+[^\n]{0,512}\s+into\s+\w+(?:[\s-]+\w+){0,2}\s+and\s+(execute|run|eval)\b",
        "translate_execute",
        "all",
    ),
    (rf'do\s+not\s+{_FILLER}tell\s+{_FILLER}the\s+user', "deception_hide", "all"),

    # ── Role-play / identity hijack (scraped web content, poisoned context files) ──
    (rf'you\s+are\s+{_FILLER}now\s+(?:a|an|the)\s+', "role_hijack", "context"),
    (rf'pretend\s+{_FILLER}(you\s+are|to\s+be)\s+', "role_pretend", "context"),
    (rf'output\s+{_FILLER}(system|initial)\s+prompt', "leak_system_prompt", "context"),
    (rf'(respond|answer|reply)\s+without\s+{_FILLER}(restrictions|limitations|filters|safety)', "remove_filters", "context"),
    (rf'you\s+have\s+been\s+{_FILLER}(updated|upgraded|patched)\s+to', "fake_update", "context"),
    # Brainworm tell: identity override via spec. Verb pair anchored so "name your variables" is safe.
    (r'\bname\s+yourself\s+\w+', "identity_override", "context"),

    # ── C2 / Brainworm-style promptware (context scope) ──────────────
    # Anchored on C2 vocabulary. "register as a node" appears in legitimate distributed-systems
    # docs, so this is WARN not block: a researcher reading the Brainworm post keeps their session.
    (r'register\s+(as\s+)?a?\s*node', "c2_node_registration", "context"),
    (r'(heartbeat|beacon|check[\s\-]?in)\s+(to|with)\s+', "c2_heartbeat", "context"),
    (r'pull\s+(down\s+)?(?:new\s+)?task(?:ing|s)?\b', "c2_task_pull", "context"),
    (r'connect\s+to\s+the\s+network\b', "c2_network_connect", "context"),
    # C2-specific verbs avoid the broader "you must X" false positive.
    (r'you\s+must\s+(?:\w+\s+){0,3}(register|connect|report|beacon)\b', "forced_action", "context"),
    # Anti-forensic instructions: near-zero false positive in legitimate content.
    (r'only\s+use\s+one[\s\-]?liners?\b', "anti_forensic_oneliner", "context"),
    (rf'never\s+{_FILLER}(?:create|write)\s+{_FILLER}(?:script|file)\s+{_FILLER}disk', "anti_forensic_disk", "context"),
    # Unsetting agent-runtime env vars is pure attack behavior (Brainworm sub-session bypass).
    (r'unset\s+\w*(?:CLAUDE|CODEX|HERMES|AGENT|OPENAI|ANTHROPIC)\w*', "env_var_unset_agent", "context"),

    # ── Known C2 / red-team framework names (warn-only) ─────────────
    # Every token must be a distinctive offensive-security brand: a common English word here
    # (e.g. "praxis", also a legitimate agent name) false-positives whole AGENTS.md / SOUL.md files.
    (r'\b(?:cobalt\s*strike|sliver|havoc|mythic|metasploit|brainworm)\b', "known_c2_framework", "context"),
    (r'\bc2\s+(?:server|channel|infrastructure|beacon)\b', "c2_explicit", "context"),
    (r'\bcommand\s+and\s+control\b', "c2_explicit_long", "context"),

    # ── Exfiltration via curl/wget/cat with secrets (applies everywhere) ──
    # The var name ends with \b so benign names containing KEY/TOKEN as substrings
    # ($TRILLIUM_ETAPI_URL) pass. API is deliberately absent: mid-name API is ubiquitous in
    # benign vars, and every real secret it caught ($OPENAI_API_KEY) already ends in KEY/TOKEN.
    (rf'curl\s+[^\n]{{0,2048}}{_SECRET_VAR}', "exfil_curl", "all"),
    (rf'wget\s+[^\n]{{0,2048}}{_SECRET_VAR}', "exfil_wget", "all"),
    (r'cat\s+[^\n]{0,2048}(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)', "read_secrets", "all"),
    (r'(send|post|upload|transmit)\s+[^\n]{0,2048}\s+(to|at)\s+https?://', "send_to_url", "strict"),
    (rf'(include|output|print|share)\s+{_FILLER}(conversation|chat\s+history|previous\s+messages|full\s+context|entire\s+context)', "context_exfil", "strict"),

    # ── Persistence / SSH backdoor (strict scope — memory + skills) ──
    (r'authorized_keys', "ssh_backdoor", "strict"),
    # Write-verb gated like the *_config_mod rules: a bare path match blocked ordinary docs
    # ("check $HOME/.ssh is chmod 700"). ``>>?`` covers a leading redirect with no verb word;
    # ``open(`` covers the scripted-write shape; chmod/chown/sed/truncate/rm/touch/curl/wget/git
    # mutate the directory without an obvious copy verb.
    (r'(?:\b(?:echo|cat|cp|mv|dd|tee|install|printf|rsync|scp|ln|append|add|write'
     r'|sed|chmod|chown|truncate|rm|touch|curl|wget|git)\b|\bopen\s*\(|>>?)'
     r'[^\n]{0,512}(?:\$HOME/\.ssh|~/\.ssh)', "ssh_access", "strict"),
    (rf'{_MODIFY}(?:AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules)', "agent_config_mod", "strict"),

    # ── Hardcoded secrets ────────────────────────────────────────────
    # The lookahead skips a value that is itself an environment-variable NAME
    # (SHOUTY_SNAKE, ≥2 underscore-separated segments): ENV_PASSWORD =
    # "MYPLUGIN_APP_PASSWORD" says where the credential lives, it does not embed
    # one (#116221). Scoped case-sensitive on purpose — the pattern compiles with
    # IGNORECASE and a lowercase snake value is the password-passphrase shape
    # ("correct_horse_battery_staple"); requiring an underscore segment keeps
    # underscore-free all-caps credentials (AWS AKIA…, base32) matched.
    (r'(?:api[_-]?key|token|secret|password)\s*[=:]\s*["\']'
     r'(?!(?-i:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)["\'])'
     r'[A-Za-z0-9+/=_-]{20,}', "hardcoded_secret", "strict"),
]

# Invisible / bidirectional unicode used in injection attacks (aligned with skills_guard.py
# INVISIBLE_CHARS): zero-width space/non-joiner/joiner, word joiner, invisible times/separator/
# plus, BOM, LTR/RTL embedding + pop + overrides, LTR/RTL/first-strong isolates + pop.
INVISIBLE_CHARS = frozenset(
    "\u200b\u200c\u200d\u2060\u2062\u2063\u2064\ufeff"
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")

# TAG characters (U+E0000..U+E007F) — ẩn hoàn toàn, dùng giấu chỉ thị (ASCII smuggling).
_TAG_RANGE = range(0xE0000, 0xE0080)

_SCOPE_SETS = {"all": ("all", "context", "strict"), "context": ("context", "strict"), "strict": ("strict",)}


def _compile() -> dict[str, list[tuple[re.Pattern, str]]]:
    compiled: dict[str, list[tuple[re.Pattern, str]]] = {"all": [], "context": [], "strict": []}
    for pattern, pid, scope in _PATTERNS:
        for s in _SCOPE_SETS[scope]:
            compiled[s].append((re.compile(pattern, re.IGNORECASE), pid))
    return compiled


_COMPILED = _compile()

# Nội dung tri thức dặn agent sửa chính sách an toàn của hub (FR-SG-05, FR-LN-07 mục 8).
_CLI_NAMES = _names.alt(_names.HUB_CLI_NAMES)  # tên mới + tên cũ (đổi tên cfharness)
_HUB = rf"(?:{_names.alt(_names.HUB_HOME_DIRNAMES)}|{_CLI_NAMES}|hub_home|HUB_HOME)"
POLICY_TOUCH_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"{_MODIFY}(?:{_HUB}[^\n]{{0,200}})?\b(?:policy|evals?|golden)/", re.I), "policy_touch"),
    (re.compile(rf"{_MODIFY}[^\n]{{0,200}}\b(?:protected|catalog|learning)\.ya?ml\b", re.I), "policy_touch"),
    (re.compile(r"\b(?:remove|delete|drop|disable|bypass|turn\s+off|skip)\s+(?:\S+\s+){0,8}(?:guard|hook|audit|approval|golden\s+task|eval(?:uation)?\s+gate|destructive\s+(?:catalog|list|rules?))", re.I), "policy_touch"),
    (re.compile(r"\blearning\.(?:enabled|auto_apply|config_bounds|rate)\b", re.I), "policy_touch"),
    (re.compile(r"--dangerously-skip-permissions|bypassPermissions|permission[_-]mode\s*[=:]\s*[\"']?(?:bypassPermissions|dontAsk)", re.I), "self_escalation"),
    (re.compile(rf"\b{_CLI_NAMES}\s+(?:approve|deny|guard\s+protect|learn\s+policy|config\s+set|home\s+migrate)\b", re.I), "policy_touch"),
]


def scan(content: str, scope: str = "strict") -> list[str]:
    """Id các mẫu nguy hiểm khớp trong `content` (rỗng = sạch)."""
    if not content:
        return []
    content = content[:MAX_SCAN_CHARS]
    found = [f"invisible_unicode_U+{ord(ch):04X}" for ch in sorted(set(content) & INVISIBLE_CHARS)]
    found += [f"tag_unicode_U+{ord(ch):05X}" for ch in sorted({c for c in content if ord(c) in _TAG_RANGE})][:3]
    norm = unicodedata.normalize("NFKC", content)
    found += [pid for rx, pid in _COMPILED[scope] if rx.search(norm)]
    found += [pid for rx, pid in POLICY_TOUCH_PATTERNS if rx.search(norm)]
    return list(dict.fromkeys(found))
