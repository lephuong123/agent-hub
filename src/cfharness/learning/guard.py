"""Guard an toàn khi học (FR-LN-07) + bất biến chính sách an toàn phần học (FR-SG-05, FR-LN-19).

`LearningGuard.check(prop, plan) -> Block | None`. Guard là một phần chính sách an toàn: vòng học không sửa được luật này.
Mỗi chặn có `category` theo BRD §9.2 (`policy_touch` | `self_escalation` | `bounds` | `guard_rule`) và `rule` chi tiết:
- policy_touch     : đích/đường dẫn/khoá/nội dung chạm `policy/`, `evals/`, golden task, guard, audit, công tắc học;
- self_escalation  : thêm tool/pattern phá huỷ vào allowed_tools, bỏ pattern khỏi disallowed_tools, permission_mode
                     bypassPermissions/dontAsk, allow_bypass, permission_prompts, MCP chưa có trong registry;
- bounds           : vượt `config_bounds` (model, effort, ngân sách, đồng thời — Q-20);
- guard_rule       : injection/Unicode ẩn (`threats.scan`), khẳng định tiêu cực về tool, lỗi môi trường, memory mệnh
                     lệnh/không phải tiếng Anh, một fact – một kho, hạn mức ký tự, tên skill quá hẹp, read-before-write,
                     gộp/xoá thiếu `absorbed_into`, đích không tồn tại, đích đã pin.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .policy import EFFORT_ORDER
from .threats import scan

if TYPE_CHECKING:
    from ..hub import Hub

CONFIG_KEYS = ("model", "effort", "fallback_model", "soul", "tool_policy", "mcp_servers", "limits")
POLICY_WORDS = re.compile(r"(^|[/\\.])(policy|evals?|golden|guard|audit|ledger|snapshots|secrets)([/\\.]|$)|learning\.(enabled|auto_apply|config_bounds|rate)", re.I)
NEGATIVE_CLAIM = re.compile(
    r"(\b(tool|tools|mcp|server|command|feature|api|plugin|integration)\b|`[^`]{1,60}`|mcp__\w+)[^.\n]{0,80}?\b(is|are)\s+(broken|unusable|not\s+working|unavailable|unreliable)\b"
    r"|\b(does|do)\s*(n't|\s+not)\s+work\b|\bcannot\s+(be\s+)?use[d]?\b[^.\n]{0,40}\b(tool|mcp|from)\b"
    # "never use X" chỉ là khẳng định tiêu cực khi kèm lý do hỏng — "Never use `--admin`" là quy tắc an toàn, phải học được
    r"|\b(never|avoid)\s+us(e|ing)\b[^.\n]{0,80}\b(broken|unusable|unreliable|flaky|buggy|fails?|failing|hangs?|crash(es)?)\b",
    re.I,
)
ENV_FAILURE = re.compile(r"command not found|not installed|no such file or directory|missing binary|ModuleNotFoundError|permission denied|ENOENT|not configured|unconfigured credentials", re.I)
IMPERATIVE_MEMORY = re.compile(r"^\s*(always|never|you\s+(must|should|need\s+to)|do\s+not|don't|make\s+sure|remember\s+to|ensure)\b", re.I)


@dataclass
class Block:
    category: str
    rule: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"category": self.category, "rule": self.rule, "detail": self.detail}


def _non_english_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 20:
        return 0.0
    odd = sum(1 for c in letters if ord(c) > 0x7F and unicodedata.category(c).startswith("L"))
    return odd / len(letters)


def _destructive_tool_pattern(hub: "Hub", pattern: str) -> str | None:
    """Pattern allowed_tools có cho phép thao tác phá huỷ không (dùng bộ phân loại của guard M1)."""
    p = pattern.strip()
    if p in ("Bash", "Bash(*)", "Bash(*:*)", "*") or p.startswith("mcp__") and p.count("__") < 2:
        return f"'{p}' cho phép mọi lệnh/tool (bao gồm phá huỷ)"
    m = re.match(r"^Bash\((.*)\)$", p, re.S)
    if m:
        cmd = m.group(1).replace(":*", " x").replace("*", "x")
        if re.search(r"\brm\b|\bgit\s+(push|reset|clean|branch\s+-D|filter-branch)\b|\bdrop\b|\bmkfs\b|\bdd\b|\btruncate\b|\bshred\b|\bchmod\b|\bsudo\b", cmd, re.I):
            return f"'{p}' giống lệnh phá huỷ"
        try:
            from ..guard.classifier import classify

            ctx = hub.guard.context_for(None, cwd=str(hub.paths.home / "global"))
            ctx.git_probe = False
            v = classify("Bash", {"command": cmd}, ctx)
            if v.verdict in ("destructive", "uncertain") or v.forbidden:
                return f"'{p}' khớp thao tác {v.verdict} ({v.rule})"
        except Exception:  # pragma: no cover - classifier lỗi → coi như phá huỷ (fail-closed)
            return f"'{p}' không phân loại được (fail-closed)"
    if p.startswith("mcp__"):
        server, _, tool = p[5:].partition("__")
        try:
            ann = hub.mcp.tool_annotations(server, tool) if hasattr(hub, "mcp") else None
        except Exception:
            ann = None
        if ann and ann.get("destructiveHint"):
            return f"tool MCP '{p}' có destructiveHint"
        if re.search(r"(delete|remove|drop|purge|destroy|xoa)", tool, re.I):
            return f"tool MCP '{p}' có tên dạng xoá"
    return None


class LearningGuard:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub

    # ------------------------------------------------------------------ entry
    def check(self, prop: dict[str, Any], plan: dict[str, Any]) -> Block | None:
        for fn in (self._policy_touch, self._pinned, self._content, self._limits, self._skill_rules, self._config):
            b = fn(prop, plan)
            if b is not None:
                return b
        return None

    # ------------------------------------------------------------------ luật
    def _policy_touch(self, prop: dict[str, Any], plan: dict[str, Any]) -> Block | None:
        if prop["target"] not in ("memory", "skill", "config", "user_profile"):
            return Block("policy_touch", "target", f"target '{prop['target']}' không thuộc tri thức/cấu hình agent")
        pl = prop.get("payload") or {}
        refs = [str(prop.get("target_ref") or ""), str(pl.get("file_path") or ""), str(pl.get("name") or ""), str(pl.get("absorbed_into") or "")]
        if isinstance(pl.get("files"), dict):  # file hỗ trợ đi kèm (skill manager: import/promote)
            refs += [str(k) for k in pl["files"]]
        for r in refs:
            if r and (POLICY_WORDS.search(r) or ".." in r or r.startswith("/")):
                return Block("policy_touch", "path", f"đích '{r}' chạm chính sách an toàn hoặc thoát khỏi kho tri thức")
        if prop["target"] == "config":
            ch = pl.get("changes") or {}
            for k in ch:
                if str(k).split(".")[0] in ("learning", "policy", "guard", "approval", "audit", "golden", "evals", "eval"):
                    return Block("policy_touch", "config_key", f"khoá '{k}' thuộc chính sách an toàn — chỉ người sửa")
        return None

    def _pinned(self, prop: dict[str, Any], plan: dict[str, Any]) -> Block | None:
        ks = self.hub.knowledge
        t = prop["target"]
        ref = prop.get("target_ref") or ""
        if t == "skill" and ks.is_pinned(plan.get("scope", prop["scope"]), plan.get("owner"), "skill", ref):
            if prop.get("created_by") != "user":
                return Block("guard_rule", "pinned", f"skill '{ref}' đã được người pin — vòng học không đụng vào")
        if t == "config":
            for k in (prop.get("payload") or {}).get("changes") or {}:
                if ks.is_pinned("agent", prop.get("agent_id"), "config", k) and prop.get("created_by") != "user":
                    return Block("guard_rule", "pinned", f"trường cấu hình '{k}' đã được người pin")
        if t in ("memory", "user_profile") and ks.is_pinned(plan.get("scope", prop["scope"]), plan.get("owner"), "memory", ref) and prop.get("created_by") != "user":
            return Block("guard_rule", "pinned", f"{ref} đã được người pin")
        if t in ("memory", "user_profile") and prop.get("created_by") != "user":  # M5-A (FR-LN-14): người pin TỪNG DÒNG
            from .canary import entry_hash

            for e in plan.get("touched_entries") or []:
                if ks.is_pinned(plan.get("scope", prop["scope"]), plan.get("owner"), "memory_entry", entry_hash(e)):
                    return Block("guard_rule", "pinned", f"dòng '{e[:60]}' của {ref} đã được người pin")
        return None

    def _content(self, prop: dict[str, Any], plan: dict[str, Any]) -> Block | None:
        text = plan.get("new_text") or ""
        if not text:
            return None
        found = scan(text)
        if found:
            cat = "policy_touch" if "policy_touch" in found else "self_escalation" if "self_escalation" in found else "guard_rule"
            return Block(cat, "threat_scan", "nội dung chứa mẫu nguy hiểm: " + ", ".join(found[:5]))
        if prop.get("created_by") == "user":
            return None
        if NEGATIVE_CLAIM.search(text):
            return Block("guard_rule", "negative_claim", "khẳng định tiêu cực về tool/tính năng không được học (_DO_NOT_CAPTURE_BLOCK) — ghi cách SỬA, không ghi 'X hỏng'")
        if ENV_FAILURE.search(text) and prop["target"] in ("memory", "user_profile"):
            return Block("guard_rule", "environment_failure", "lỗi môi trường/cài đặt không phải bài học bền — để vào skill troubleshooting dạng cách sửa")
        if _non_english_ratio(text) > 0.15:
            return Block("guard_rule", "not_english", "tri thức của agent phải viết tiếng Anh (Q-14)")
        if prop["target"] in ("memory", "user_profile") and IMPERATIVE_MEMORY.search(text):
            return Block("guard_rule", "imperative_memory", "memory viết dạng KHAI BÁO (fact), không mệnh lệnh — quy tắc làm việc đặt vào skill")
        return None

    def _limits(self, prop: dict[str, Any], plan: dict[str, Any]) -> Block | None:
        lim = self.hub.knowledge.limits()
        if prop["target"] in ("memory", "user_profile"):
            if plan.get("format_drift") and prop.get("created_by") != "user":
                return Block("guard_rule", "format_drift",
                             f"{prop.get('target_ref')} đã bị sửa tay lệch định dạng (mục ngăn bởi dòng '§') — vòng học không ghi đè; người sửa lại file "
                             "hoặc ghi qua `cfharness learn propose`")
            elim = int(lim.get("memory_entry_chars") or 0)
            if elim and plan.get("longest_written", 0) > elim and prop.get("created_by") != "user":
                return Block("guard_rule", "entry_too_long",
                             f"một mục memory {plan['longest_written']} > {elim} ký tự — mỗi mục là MỘT fact ngắn; tách thành nhiều mục hoặc rút gọn")
            if plan.get("chars_after", 0) > plan.get("limit", 10**9) and plan.get("chars_after", 0) > plan.get("chars_before", 0):
                return Block("guard_rule", "memory_limit",
                             f"{prop.get('target_ref')} sẽ là {plan['chars_after']}/{plan['limit']} ký tự — gửi MỘT lô `operations` vừa gộp/rút gọn/xoá mục cũ (patch/remove) vừa thêm mục mới")
            if plan.get("duplicate_of"):
                return Block("guard_rule", "one_fact_one_store", f"fact này đã có ở {plan['duplicate_of']} — một fact chỉ ở một kho")
        if prop["target"] == "skill":
            if plan.get("skill_chars", 0) > lim["skill_chars"]:
                return Block("guard_rule", "skill_limit", f"SKILL.md {plan['skill_chars']} > {lim['skill_chars']} ký tự — đẩy chiều sâu vào references/")
            d = plan.get("description")
            if d is not None and len(d) > lim["skill_desc_chars"]:
                return Block("guard_rule", "description_limit", f"mô tả skill {len(d)} > {lim['skill_desc_chars']} ký tự")
        return None

    def _skill_rules(self, prop: dict[str, Any], plan: dict[str, Any]) -> Block | None:
        if prop["target"] != "skill":
            return None
        op = prop["op"]
        pl = prop.get("payload") or {}
        by = prop.get("created_by")
        if plan.get("read_required") and by == "review" and not plan.get("was_read"):
            return Block("guard_rule", "read_before_write",
                         f"chưa đọc '{prop['target_ref']}' trong lượt review này — gọi skill_view(name{', file_path=...' if pl.get('file_path') else ''}) rồi đề xuất lại dựa trên nội dung vừa đọc")
        if op in ("merge", "archive", "delete"):
            exempt = by in ("curator", "user") and op == "archive" and (pl.get("reason") or "").startswith(("stale", "user"))
            target = pl.get("absorbed_into")
            if not exempt and not target:
                return Block("guard_rule", "absorbed_into", "gộp/xoá skill bắt buộc absorbed_into=<skill đích đã chứa nội dung hấp thụ>")
            if target and not plan.get("absorbed_exists"):
                return Block("guard_rule", "absorbed_into", f"skill đích '{target}' không tồn tại")
            if target and by == "review" and not plan.get("absorbed_read"):
                return Block("guard_rule", "absorbed_into", f"phải skill_view('{target}') (đã chứa nội dung hấp thụ) trong lượt review này trước khi gộp")
        return None

    def _config(self, prop: dict[str, Any], plan: dict[str, Any]) -> Block | None:
        if prop["target"] != "config":
            return None
        pl = prop.get("payload") or {}
        ch = dict(pl.get("changes") or {})
        if pl.get("soul") is not None:
            ch.setdefault("soul", pl["soul"])
        unknown = [k for k in ch if k not in CONFIG_KEYS]
        if unknown:
            return Block("guard_rule", "config_key", f"vòng học không sửa được trường: {', '.join(unknown)} (được: {', '.join(CONFIG_KEYS)})")
        b = self.hub.learning.policy.get("config_bounds")
        cur = plan.get("current_config") or {}
        if "model" in ch and ch["model"] not in (b.get("models") or []):
            return Block("bounds", "model", f"model '{ch['model']}' ngoài config_bounds.models {b.get('models')}")
        if ch.get("fallback_model") and ch["fallback_model"] not in (b.get("models") or []):
            return Block("bounds", "fallback_model", f"fallback_model '{ch['fallback_model']}' ngoài config_bounds.models")
        if ch.get("effort"):
            mx = b.get("max_effort") or "high"
            if ch["effort"] not in EFFORT_ORDER or EFFORT_ORDER.index(ch["effort"]) > EFFORT_ORDER.index(mx):
                return Block("bounds", "effort", f"effort '{ch['effort']}' vượt trần '{mx}'")
        lim = ch.get("limits") or {}
        for key, bound in (("max_budget_usd_per_run", "max_budget_usd_per_run"), ("budget_usd_per_day", "budget_usd_per_day"), ("max_concurrent_runs", "max_concurrent_runs")):
            v = lim.get(key)
            if v is not None and b.get(bound) is not None and float(v) > float(b[bound]):
                return Block("bounds", key, f"limits.{key}={v} vượt config_bounds.{bound}={b[bound]}")
        if "max_runtime_s" in lim and lim["max_runtime_s"] is None:
            return Block("bounds", "max_runtime_s", "không được bỏ giới hạn thời gian chạy")
        tp = ch.get("tool_policy") or {}
        cur_tp = cur.get("tool_policy") or {}
        if tp:
            if tp.get("permission_mode") in ("bypassPermissions", "dontAsk"):
                return Block("self_escalation", "permission_mode", f"permission_mode '{tp['permission_mode']}' là tự nâng quyền")
            if tp.get("allow_bypass"):
                return Block("self_escalation", "allow_bypass", "allow_bypass chỉ người đặt (NFR-15)")
            if "permission_prompts" in tp and tp["permission_prompts"] != cur_tp.get("permission_prompts"):
                return Block("self_escalation", "permission_prompts", "không được đổi kênh phê duyệt")
            if tp.get("browser") in ("chrome", "auto") and tp.get("browser") != cur_tp.get("browser"):
                return Block("self_escalation", "browser", "bật Claude in Chrome (Chrome THẬT của người) là việc của người/điều phối viên")
            if "web_allow_domains" in tp:
                from ..guard.url_safety import agent_domains_widening

                why = agent_domains_widening(cur_tp.get("web_allow_domains"), tp["web_allow_domains"])
                if why:
                    return Block("self_escalation", "web_allow_domains", why)
            if "disallowed_tools" in tp:
                removed = [x for x in cur_tp.get("disallowed_tools") or [] if x not in (tp.get("disallowed_tools") or [])]
                if removed:
                    return Block("self_escalation", "disallowed_tools", f"không được bỏ pattern khỏi disallowed_tools: {removed}")
            added = [x for x in tp.get("allowed_tools") or [] if x not in (cur_tp.get("allowed_tools") or [])]
            for pat in added:
                why = _destructive_tool_pattern(self.hub, pat)
                if why:
                    return Block("self_escalation", "allowed_tools", f"thêm quyền phá huỷ: {why}")
            if tp.get("tools") is not None and cur_tp.get("tools") is not None:
                extra = [t for t in tp["tools"] if t not in cur_tp["tools"] and t in ("Bash", "Write", "Edit", "MultiEdit", "NotebookEdit")]
                if extra:
                    return Block("self_escalation", "tools", f"mở thêm tool ghi/shell bị giới hạn: {extra}")
        if "mcp_servers" in ch:
            try:
                names = {m["name"] for m in self.hub.mcp.list()} if hasattr(self.hub, "mcp") else set()
            except Exception:
                names = set()
            new = [s for s in ch["mcp_servers"] or [] if s not in (cur.get("mcp_servers") or [])]
            missing = [s for s in new if s not in names]
            if missing:
                return Block("self_escalation", "mcp_servers", f"MCP chưa có trong registry (thêm server là việc của người): {missing}")
        return None
