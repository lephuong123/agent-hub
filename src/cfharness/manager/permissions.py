"""Kiểm quyền của điều phối viên (manager) ở DAEMON — "toàn quyền trừ an toàn" (docs/plan/M7-manager.md).

Hai lớp, cùng một bộ luật (`check_rpc`):
1. `ManagerService.self_call` kiểm TRƯỚC khi thực hiện tool hub-self (lý do trả về cho manager + audit `manager.denied`);
2. `Daemon._dispatch` kiểm LẠI mọi RPC mà người gọi `Caller.manager` thực hiện (`MANAGER_ALLOWED` + luật dưới) — kể cả khi
   lớp 1 có lỗi, method ngoài tập hoặc trường cấm vẫn bị từ chối + `auth.denied` + audit actor `manager:<agent>`.

Luật (mỗi luật có test đỏ→xanh trong `tests/test_manager_security.py`):
- không đụng agent manager (chính mình hoặc manager khác/trước đây): sửa, xoá mềm, khôi phục, run/steer/stop, vào team, học;
- agent: chỉ trường cho phép (`AGENT_FIELDS`, `TOOL_POLICY_FIELDS`); cấm `permission_mode=bypassPermissions`, `allow_bypass`,
  `permission_prompts`, `extra_args`, `sandbox*`, `subagents`, `result_schema`, đổi id;
- không BỎ mẫu `disallowed_tools` có sẵn trừ mẫu do chính manager đã thêm (`hub_state.manager_disallowed_added`);
- MCP: chỉ server có trong registry (DB) và không phải MCP của chính hub;
- workspace/cwd: không trong HUB_HOME (trừ workspaces của agent), không phải /, $HOME, vùng bảo vệ;
- team: không chạm `policies.learning` (công tắc học = chính sách an toàn), không ESTOP qua team.update;
- `estop.set` chỉ `on=true`.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..auth import Caller
    from ..hub import Hub

AGENT_FIELDS = frozenset({"name", "role", "description", "model", "effort", "fallback_model", "tool_policy", "mcp_servers", "limits", "workspace_default"})
TOOL_POLICY_FIELDS = frozenset({"permission_mode", "tools", "allowed_tools", "disallowed_tools"})
FORBIDDEN_MODES = frozenset({"bypassPermissions"})
TEAM_FIELDS = frozenset({"name", "charter", "policies"})
TEAM_POLICY_FORBIDDEN = frozenset({"learning"})
STATE_DISALLOWED = "manager_disallowed_added"
REVIEW_ROLES = ("reviewer", "verifier")
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
# MGR-6: trần tần suất (config `manager.*`, chỉ người đặt) — method → (khoá cấu hình, cửa sổ giây, nhãn)
RATE_LIMITS = {
    "agent.create": ("max_agents_created_per_day", 86400, "agent tạo/ngày"),
    "task.create": ("max_tasks_per_hour", 3600, "task/giờ"),
    "run.start": ("max_runs_per_hour", 3600, "run ad-hoc/giờ"),
    "system.job": ("max_system_ops_per_hour", 3600, "thao tác job hệ thống/giờ"),
}


# §9 (M7-core.md): khung quyền của điều phối viên — NGƯỜI đặt ở `manager.capabilities` (nhánh `manager` ∈ integrity.SECURITY_KEYS,
# `config.set` ∈ HUMAN_ONLY). Tool ngoài CAP_TOOLS_MAX không bao giờ được (Task/Agent = subagent, có thể mang permissionMode riêng).
CAP_PROFILES: dict[str, dict[str, Any]] = {
    "strict": {"tools": ["Read", "Grep", "Glob"], "mcp": "none", "web": False},
    "hermes": {"tools": ["Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Grep", "Glob", "TodoWrite"],
               "mcp": "read_only_trusted", "web": True},
}
CAP_WEB_TOOLS = ("WebFetch", "WebSearch")
CAP_TOOLS_MAX = frozenset(CAP_PROFILES["hermes"]["tools"]) | frozenset(CAP_WEB_TOOLS)
CAP_MCP_MODES = ("none", "read_only_trusted", "trusted")
_CAP_ORDER = ("Read", "Grep", "Glob", "Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "TodoWrite", "WebFetch", "WebSearch")


def fmt_tools(ts: Any) -> str:
    return "/".join(sorted(ts, key=lambda t: (_CAP_ORDER.index(t) if t in _CAP_ORDER else 99, t)))
CAP_KEYS = ("profile", "tools", "mcp", "web", "connectors")
# Connector claude.ai (Claude Docs, Atlassian…) — `claude` CLI chỉ nạp khi có nguồn setting `user` và KHÔNG `--strict-mcp-config`
# (đã kiểm 2.1.310, docs/dev-notes/manager-connectors.md). Tên server trong CLI: "claude.ai <Tên>", tool: mcp__claude_ai_<slug>__<tool>.
CONNECTOR_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()\-]{0,79}$")
CONNECTOR_TOOL_PREFIX = "claude_ai_"


def connector_slug(name: str) -> str:
    """"Claude Docs" | "claude.ai Claude Docs" | "claude_ai_Claude_Docs" → "Claude_Docs" (phần sau `mcp__claude_ai_`)."""
    n = str(name).strip()
    for pre in ("claude.ai ", CONNECTOR_TOOL_PREFIX):
        if n.startswith(pre):
            n = n[len(pre):]
    return re.sub(r"[^A-Za-z0-9_-]", "_", n)


def valid_connector(name: Any) -> bool:
    return isinstance(name, str) and bool(CONNECTOR_NAME_RE.match(name.strip())) and bool(connector_slug(name).strip("_"))


def connector_server(name: str) -> str:
    """Tên server dùng trong tên tool (`mcp__<server>__<tool>`)."""
    return CONNECTOR_TOOL_PREFIX + connector_slug(name)


def validate_capabilities(key: str, value: Any) -> None:
    """Kiểm khi `config set manager.capabilities[.x]` (gọi từ `config.validate_setting`) — sai → ValueError, không ghi."""
    sub = key[len("manager.capabilities"):].lstrip(".")
    if not sub:
        if value is None:
            return
        if not isinstance(value, dict):
            raise ValueError("manager.capabilities phải là object {profile, tools, mcp, web}")
        bad = sorted(set(value) - set(CAP_KEYS))
        if bad:
            raise ValueError(f"manager.capabilities: khoá lạ {bad} (được: {', '.join(CAP_KEYS)})")
        for k, v in value.items():
            validate_capabilities(f"manager.capabilities.{k}", v)
        return
    if sub == "profile":
        if value not in CAP_PROFILES:
            raise ValueError(f"manager.capabilities.profile phải là {'|'.join(CAP_PROFILES)}")
    elif sub == "tools":
        if value is not None and (not isinstance(value, list) or not all(isinstance(x, str) for x in value)):
            raise ValueError("manager.capabilities.tools phải là danh sách tên tool hoặc null (= theo profile)")
        bad = sorted(set(value or []) - CAP_TOOLS_MAX)
        if bad:
            raise ValueError(f"manager.capabilities.tools: {bad} không bao giờ cấp cho điều phối viên (tối đa: {sorted(CAP_TOOLS_MAX)})")
    elif sub == "mcp":
        if value is not None and value not in CAP_MCP_MODES:
            raise ValueError(f"manager.capabilities.mcp phải là {'|'.join(CAP_MCP_MODES)} hoặc null")
    elif sub == "web":
        if value is not None and not isinstance(value, bool):
            raise ValueError("manager.capabilities.web phải là true/false hoặc null")
    elif sub == "connectors":
        if value is None:
            return
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError("manager.capabilities.connectors phải là danh sách tên connector claude.ai hoặc null")
        bad = [x for x in value if not valid_connector(x)]
        if bad:
            raise ValueError(f"manager.capabilities.connectors: tên không hợp lệ {bad}")
    else:
        raise ValueError(f"manager.capabilities: khoá lạ '{sub}' (được: {', '.join(CAP_KEYS)})")


def resolve_capabilities(raw: Any) -> dict[str, Any]:
    """Cấu hình thô → khung hiệu lực `{profile, tools: frozenset, mcp, web, warnings}`. Giá trị lạ (sửa tay config.yaml) → FAIL-CLOSED
    (bỏ qua, về strict/không) + cảnh báo."""
    raw = raw if isinstance(raw, dict) else {}
    warnings: list[str] = []
    profile = raw.get("profile") or "strict"
    if profile not in CAP_PROFILES:
        warnings.append(f"profile '{profile}' không hợp lệ → strict")
        profile = "strict"
    base = CAP_PROFILES[profile]
    tools_raw = raw.get("tools")
    tools = list(base["tools"]) if tools_raw is None else [str(x) for x in tools_raw] if isinstance(tools_raw, list) else list(CAP_PROFILES["strict"]["tools"])
    dropped = sorted(set(tools) - CAP_TOOLS_MAX)
    if dropped:
        warnings.append(f"tool {dropped} không bao giờ cấp cho điều phối viên — bỏ qua")
    web = raw.get("web") if isinstance(raw.get("web"), bool) else bool(base["web"])
    ts = {t for t in tools if t in CAP_TOOLS_MAX and t not in CAP_WEB_TOOLS}
    if web:
        ts |= set(CAP_WEB_TOOLS)
    mcp = raw.get("mcp") if raw.get("mcp") in CAP_MCP_MODES else base["mcp"]
    if raw.get("mcp") is not None and raw.get("mcp") not in CAP_MCP_MODES:
        mcp = "none"
        warnings.append(f"mcp '{raw.get('mcp')}' không hợp lệ → none")
    conns_raw = raw.get("connectors")
    connectors: tuple[str, ...] = ()
    if isinstance(conns_raw, list) and conns_raw:
        slugs = [connector_slug(x) for x in conns_raw if valid_connector(x)]
        if len(slugs) != len(conns_raw):
            warnings.append("connectors có tên không hợp lệ — bỏ qua tên đó")
        if mcp != "trusted":
            warnings.append(f"connectors claude.ai cần mcp=trusted (đang {mcp}) — tắt connectors")
        else:
            connectors = tuple(dict.fromkeys(slugs))
    elif conns_raw is not None and not isinstance(conns_raw, list):
        warnings.append("connectors không phải danh sách → tắt")
    return {"profile": profile, "tools": frozenset(ts), "mcp": mcp, "web": web, "connectors": connectors, "warnings": warnings}


def _deep_keys(obj: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            out.append(path)
            out.extend(_deep_keys(v, path))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_deep_keys(v, prefix))
    return out


class ManagerPermissions:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub

    # ------------------------------------------------------------------ ai là manager
    def manager_ids(self) -> set[str]:
        """Agent manager hiện tại + mọi agent từng được chỉ định (cấm sửa "bất kỳ agent manager nào")."""
        st = self.hub.store.state_get("manager") or {}
        ids = {str(x) for x in (st.get("history") or []) if x}
        if st.get("agent_id"):
            ids.add(str(st["agent_id"]))
        return ids

    # ------------------------------------------------------------------ đường dẫn
    def check_path(self, path: Any, what: str, *, agent_id: str | None = None) -> str | None:
        if path in (None, "", "scratch", "worktree"):
            return None
        if isinstance(path, dict):
            if path.get("kind") in ("scratch", "worktree") and not path.get("path"):
                return None
            path = path.get("path")
        try:
            p = Path(str(path)).expanduser().resolve()
        except (OSError, RuntimeError):
            return f"{what}: đường dẫn không hợp lệ"
        home = Path(os.path.expanduser("~")).resolve()
        hh = self.hub.paths.home.resolve()
        if str(p) in ("/", str(home)):
            return f"{what} không được là '/' hoặc thư mục home của người ({p})"
        if p == hh or p.is_relative_to(hh):
            rel = p.relative_to(hh).parts if p != hh else ()
            if not (len(rel) >= 3 and rel[0] == "agents" and rel[2] == "workspaces"):
                return f"{what} nằm trong dữ liệu nội bộ của hub ({p}) — vùng an toàn"
            # MGR-10: chỉ workspaces của CHÍNH agent đó; không bao giờ thư mục phiên/hội thoại (`conv-*`) hay của agent manager
            if rel[1] != agent_id or rel[1] in self.manager_ids() or (len(rel) >= 4 and rel[3].startswith("conv-")):
                return f"{what} trỏ vào thư mục làm việc/phiên của agent khác ({rel[1]}) — không được"
        try:
            from ..guard.classifier import protected_path

            ctx = self.hub.guard.context_for(None, cwd=str(p) if p.is_dir() else None)
            r = protected_path(ctx, str(p), "write")
            if r is not None:
                return f"{what} là tài nguyên được bảo vệ ({r.selector}: {r.note or r.id})"
            under = ctx.policy.protected_under(str(p))
            # như workspace.validate_workspace: tài nguyên NGƯỜI khai báo nằm BÊN TRONG (vd `<repo>/.next` prod) là hợp lệ —
            # guard vẫn chặn ghi vào nó theo từng tool call; chỉ tài nguyên builtin bên trong mới làm cả thư mục bị từ chối
            if under is not None and under.builtin:
                return f"{what} chứa tài nguyên được bảo vệ ({under.selector})"
        except Exception:  # pragma: no cover — kiểm phụ, không làm hỏng luật chính
            pass
        return None

    # ------------------------------------------------------------------ MGR-9: tên/vai dễ nhầm với điều phối viên
    @staticmethod
    def _canon(text: str) -> str:
        import unicodedata

        from ..guard.shell import _TRANS

        s = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text).translate(_TRANS))
        s = "".join(c for c in s if unicodedata.category(c) not in ("Mn", "Cf")).replace("đ", "d").replace("Đ", "D").casefold()
        return "".join(c for c in s if c.isalnum())

    CONFUSABLE_WORDS = ("dieuphoi", "coordinator", "hubmanager", "manageragent", "agentmanager")

    def _confusable(self, text: str) -> bool:
        c = self._canon(text)
        if not c:
            return False
        if any(w in c for w in self.CONFUSABLE_WORDS):
            return True
        for mid in self.manager_ids():
            row = self.hub.store.get_agent(mid) or {}
            for v in (mid, row.get("name"), row.get("role")):
                if v and self._canon(str(v)) == c:
                    return True
        return False

    def _mcp_trust(self) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            for m in self.hub.mcp.list():
                out[str(m["name"])] = str(m.get("trust") or (m.get("config") or {}).get("trust") or "untrusted")
        except Exception:
            pass
        return out

    # ------------------------------------------------------------------ MGR-8: cấu hình tối thiểu của agent được chỉ định làm manager
    MANAGER_TOOLS_OK = frozenset({"Read", "Grep", "Glob"})  # khung strict (mặc định) — giữ tên cũ

    def capabilities(self) -> dict[str, Any]:
        """Khung quyền của điều phối viên do NGƯỜI đặt (`manager.capabilities`, nhánh config kiểm toàn vẹn) — §9 M7-core.md."""
        try:
            raw = self.hub.config.get("manager.capabilities")
        except KeyError:
            raw = None
        from .sandbox_gate import gate  # Windows (không sandbox OS): khung rộng chỉ khi người opt-in, không thì về strict

        try:
            mgr = self.hub.config.get("manager")
        except KeyError:
            mgr = None
        return gate(resolve_capabilities(raw), mgr)

    def manager_config_problem(self, row: dict[str, Any]) -> str | None:
        caps = self.capabilities()
        ok_tools = caps["tools"]
        aid = row.get("id") or "<agent>"
        fix = (f"`cfharness agent edit {aid} --tools {fmt_tools(ok_tools).replace('/', ',')}` hoặc nới khung: "
               "`cfharness manager capabilities set --profile hermes`")
        tp = row.get("tool_policy") or {}
        tools = tp.get("tools")
        if tools is None:
            return (f"tool_policy.tools trống (= mọi tool, có Task/subagent) — khung '{caps['profile']}' chỉ cho {fmt_tools(ok_tools)}; "
                    f"liệt kê tool rõ ràng: {fix}")
        bad = sorted(set(tools) - ok_tools)
        if bad:
            return f"khung điều phối viên '{caps['profile']}' chỉ cho tool {fmt_tools(ok_tools)}, agent đang có {bad} — {fix}"
        if tp.get("permission_mode") not in (None, "default", "plan"):
            return f"permission_mode '{tp.get('permission_mode')}' không hợp với điều phối viên (chỉ default/plan)"
        if tp.get("allow_bypass"):
            return "allow_bypass phải tắt"
        mcp_ok = list(row.get("mcp_servers") or []) if caps["mcp"] != "none" else []
        prefixes = tuple(sorted(ok_tools)) + ("mcp__hub-self",) + tuple(f"mcp__{s}" for s in mcp_ok)
        extra = [x for x in tp.get("allowed_tools") or [] if not str(x).startswith(prefixes)]
        if extra:
            return f"allowed_tools vượt khung '{caps['profile']}': {extra}"
        if row.get("mcp_servers"):
            if caps["mcp"] == "none":
                return (f"khung '{caps['profile']}' không cho MCP ngoài hub-self (agent đang có {row['mcp_servers']}) — "
                        "`cfharness manager capabilities set --mcp read_only_trusted` nếu muốn")
            names, own = self._mcp_names()
            trust = self._mcp_trust()
            for s in row["mcp_servers"]:
                if s in own:
                    return f"MCP '{s}' là MCP của chính hub — điều phối viên điều khiển hub qua hub-self, không qua MCP này"
                if s not in names:
                    return f"MCP '{s}' không có trong registry"
                if trust.get(s, "untrusted") != "trusted":
                    return f"MCP '{s}' có trust=untrusted — điều phối viên chỉ dùng MCP người đã đánh dấu trusted (`cfharness mcp edit {s} --trust trusted`)"
        if row.get("subagents"):
            return "điều phối viên không được có subagents (có thể mang permissionMode riêng)"
        if row.get("workspace_default"):
            return "điều phối viên dùng workspace scratch (không workspace_default)"
        if set(tools) - self.MANAGER_TOOLS_OK or row.get("mcp_servers"):
            # tool rộng hơn strict CHỈ khi đủ lớp an toàn của agent thường: hook guard (approval) + sandbox OS
            g = self.hub.config.get("guard") or {}
            if not g.get("enabled", True):
                return "khung rộng (Bash/Write/Web/MCP) cần guard.enabled=true (hook guard + approval) — đang tắt"
            if not (g.get("sandbox") or {}).get("enabled", True):
                return "khung rộng (Bash/Write/Web/MCP) cần guard.sandbox.enabled=true (sandbox OS) — đang tắt"
        return None

    def mcp_run_disallowed(self, row: dict[str, Any]) -> list[str]:
        """`mcp: read_only_trusted` — tool MCP đã probe mà KHÔNG readOnlyHint → --disallowedTools cho run manager (tool chưa probe
        vẫn qua hook guard: `mcp_unknown_tool` → giữ chờ người)."""
        if self.capabilities()["mcp"] != "read_only_trusted":
            return []
        out: list[str] = []
        for s in row.get("mcp_servers") or []:
            try:
                r = self.hub.mcp.get_row(s) or {}  # type: ignore[attr-defined]
            except Exception:
                r = {}
            for tool_name, rec in sorted((r.get("annotations_cache") or {}).items()):
                if not (rec or {}).get("readOnlyHint"):
                    out.append(f"mcp__{s}__{tool_name}")
        return out

    # ------------------------------------------------------------------ agent
    def _mcp_names(self) -> tuple[set[str], frozenset[str]]:
        names: set[str] = set()
        try:
            names = {str(m["name"]) for m in self.hub.mcp.list()}
        except Exception:
            pass
        try:
            own = self.hub.guard.hub_mcp_servers()
        except Exception:
            own = frozenset()
        return names, own

    def check_agent_spec(self, target: str, changes: dict[str, Any], soul: Any = None, *, creating: bool, mid: str | None = None) -> str | None:
        if not isinstance(changes, dict):
            return "cấu hình agent phải là object"
        if target in self.manager_ids():
            return f"'{target}' là agent điều phối viên — không được tạo/sửa cấu hình agent manager (kể cả chính mình)"
        for k in _deep_keys(changes):
            leaf = k.rsplit(".", 1)[-1].lower()
            if leaf == "extra_args" or "extra_args" in k:
                return "không được đặt extra_args (cờ claude ngoài hub — NFR-15/H-18)"
            if leaf.startswith("sandbox"):
                return "ngoại lệ sandbox chỉ người đặt (`cfharness guard sandbox`)"
            if leaf == "allow_bypass":
                return "allow_bypass chỉ người đặt (NFR-15)"
            if leaf in ("dangerously_skip_permissions", "dangerouslyskippermissions"):
                return "không được bỏ qua phân quyền"
        extra = sorted(k for k in changes if k not in AGENT_FIELDS and not (k == "id" and changes[k] == target) and not (creating and k == "id"))
        if extra:
            return f"trường không được phép với điều phối viên: {', '.join(extra)} (được: {', '.join(sorted(AGENT_FIELDS))})"
        if creating and changes.get("id") not in (None, target):
            return "id trong spec khác id agent"
        tp = changes.get("tool_policy")
        if tp is not None:
            if not isinstance(tp, dict):
                return "tool_policy phải là object"
            bad = sorted(k for k in tp if k not in TOOL_POLICY_FIELDS)
            if bad:
                return f"tool_policy: không được đặt {', '.join(bad)} (chỉ người: allow_bypass, permission_prompts)"
            if tp.get("permission_mode") in FORBIDDEN_MODES:
                return "permission_mode=bypassPermissions bị cấm với điều phối viên (NFR-15)"
            if "disallowed_tools" in tp and not creating:
                why = self._disallowed_removal(target, tp.get("disallowed_tools"))
                if why:
                    return why
        for k in ("name", "role", "id") if creating else ("name", "role"):
            if changes.get(k) and self._confusable(str(changes[k])):
                return f"{k} '{changes[k]}' dễ nhầm với điều phối viên — chọn tên/vai khác (MGR-9)"
        if creating and self._confusable(target):
            return f"id '{target}' dễ nhầm với điều phối viên (MGR-9)"
        if "mcp_servers" in changes:
            names, own = self._mcp_names()
            trust = self._mcp_trust()
            for s in changes.get("mcp_servers") or []:
                if s in own:
                    return f"MCP '{s}' là MCP của chính hub (quyền điều khiển hub) — không gán cho agent"
                if s not in names:
                    return f"MCP '{s}' không có trong registry — thêm server MCP là việc của người (`cfharness mcp add`)"
                if trust.get(s, "untrusted") == "untrusted":
                    return f"MCP '{s}' có trust=untrusted — chỉ người gán (điều phối viên chỉ gán server người đã đánh dấu tin cậy)"
        if "workspace_default" in changes:
            why = self.check_path(changes.get("workspace_default"), "workspace_default", agent_id=target)
            if why:
                return why
        if soul is not None and not isinstance(soul, str):
            return "soul phải là chuỗi (toàn văn SOUL.md)"
        return None

    def _disallowed_removal(self, target: str, new: Any) -> str | None:
        if not isinstance(new, list):
            return "disallowed_tools phải là danh sách"
        row = self.hub.store.get_agent(target) or {}
        cur = list(((row.get("tool_policy") or {}).get("disallowed_tools")) or [])
        mine = self._mine(target, row)
        removed = [x for x in cur if x not in new and x not in mine]
        if removed:
            return f"không được BỎ mẫu chặn do người đặt khỏi disallowed_tools: {removed} (chỉ được thêm)"
        return None

    def _mine(self, target: str, row: dict[str, Any] | None = None) -> set[str]:
        """MGR-12: mẫu do manager thêm gắn với ĐÚNG bản agent (id + created_at) — agent bị purge rồi tạo lại cùng id không thừa hưởng."""
        row = row if row is not None else (self.hub.store.get_agent(target) or {})
        rec = (self.hub.store.state_get(STATE_DISALLOWED) or {}).get(target)
        if not isinstance(rec, dict) or rec.get("created_at") != row.get("created_at"):
            return set()
        return set(rec.get("patterns") or [])

    def record_disallowed(self, target: str, before: list[str], after: list[str], *, creating: bool) -> None:
        st = dict(self.hub.store.state_get(STATE_DISALLOWED) or {})
        row = self.hub.store.get_agent(target) or {}
        mine = set() if creating else self._mine(target, row)
        added = set(after) - (set() if creating else set(before))
        removed = set(before) - set(after)
        mine = (mine | added) - removed
        st[target] = {"created_at": row.get("created_at"), "patterns": sorted(mine)}
        self.hub.store.state_set(STATE_DISALLOWED, st)

    def forget_agent(self, agent_id: str) -> None:
        """Dọn trạng thái manager gắn theo id khi agent bị purge (MGR-12)."""
        for key in (STATE_DISALLOWED, "manager_archived"):
            st = dict(self.hub.store.state_get(key) or {})
            if agent_id in st:
                st.pop(agent_id)
                self.hub.store.state_set(key, st)

    # ------------------------------------------------------------------ team
    def check_team(self, cfg: dict[str, Any], *, creating: bool) -> str | None:
        keys = set(cfg) - ({"id", "members"} if creating else set())
        bad = sorted(k for k in keys if k not in TEAM_FIELDS)
        if bad:
            return f"trường team không được phép: {', '.join(bad)}"
        pol = cfg.get("policies") or {}
        if not isinstance(pol, dict):
            return "policies phải là object"
        hit = sorted(k for k in pol if k in TEAM_POLICY_FORBIDDEN)
        if hit:
            return "policies.learning (công tắc học) là chính sách an toàn — chỉ người đổi"
        if pol.get("workspace"):
            why = self.check_path(pol["workspace"], "policies.workspace")
            if why:
                return why
        mgrs = self.manager_ids()
        for m in cfg.get("members") or []:
            if not isinstance(m, dict):
                return "members phải là danh sách object {agent_id, role}"
            if str(m.get("agent_id")) in mgrs:
                return f"không đưa agent điều phối viên '{m.get('agent_id')}' vào team"
        if pol.get("default_assignee") and str(pol["default_assignee"]) in mgrs:
            return "default_assignee không được là agent điều phối viên"
        return None

    # ------------------------------------------------------------------ MGR-6: tần suất + config_bounds
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            v = self.hub.config.get(f"manager.{key}")
        except KeyError:
            return default
        return default if v is None else v

    def bounds(self) -> dict[str, Any]:
        try:
            return dict(self.hub.learning.policy.get("config_bounds") or {})
        except Exception:  # pragma: no cover
            return {"models": ["opus", "sonnet", "haiku"], "max_budget_usd_per_run": 5.0, "budget_usd_per_day": 20.0, "max_concurrent_runs": 3, "max_effort": "high"}

    def rate_block(self, method: str) -> str | None:
        rl = RATE_LIMITS.get(method)
        if rl is None:
            return None
        limit = int(self._cfg(rl[0], {"max_agents_created_per_day": 5, "max_tasks_per_hour": 20, "max_runs_per_hour": 10,
                                       "max_system_ops_per_hour": 10}[rl[0]]))
        since = (datetime.now(timezone.utc) - timedelta(seconds=rl[1])).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        n = self.hub.store.conn.execute("SELECT COUNT(*) FROM audit_log WHERE kind=? AND actor LIKE 'manager:%' AND ts>=?", (method, since)).fetchone()[0]
        if int(n) >= limit:
            return f"vượt trần tần suất của điều phối viên: {n}/{limit} {rl[2]} (`manager.{rl[0]}`, chỉ người đổi)"
        return None

    def clamp_limits(self, limits: dict[str, Any] | None) -> dict[str, Any]:
        """Agent do manager tạo: ngân sách run/ngày + đồng thời luôn có trần ≤ config_bounds (kể cả khi để trống)."""
        b = self.bounds()
        out = dict(limits or {})
        for k, bk, dflt in (("max_budget_usd_per_run", "max_budget_usd_per_run", 5.0), ("budget_usd_per_day", "budget_usd_per_day", 20.0),
                            ("max_concurrent_runs", "max_concurrent_runs", 3)):
            cap = b.get(bk, dflt)
            if cap is None:
                continue
            v = out.get(k)
            out[k] = min(v, cap) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else (1 if k == "max_concurrent_runs" else cap)
        return out

    def check_bounds(self, spec: dict[str, Any], *, require_limits: bool) -> str | None:
        b = self.bounds()
        models = b.get("models") or []
        for k in ("model", "fallback_model"):
            m = spec.get(k)
            if m and models and m not in models:
                return f"{k} '{m}' ngoài config_bounds.models {models} (chỉ người đổi: `cfharness learn policy`)"
        eff = spec.get("effort")
        mx = b.get("max_effort") or "high"
        if eff and (eff not in EFFORT_ORDER or (mx in EFFORT_ORDER and EFFORT_ORDER.index(eff) > EFFORT_ORDER.index(mx))):
            return f"effort '{eff}' vượt trần config_bounds.max_effort '{mx}'"
        lim = spec.get("limits")
        if lim is None and not require_limits:
            return None
        if not isinstance(lim, dict):
            return "limits phải là object"
        for k, bk in (("max_budget_usd_per_run", "max_budget_usd_per_run"), ("budget_usd_per_day", "budget_usd_per_day"), ("max_concurrent_runs", "max_concurrent_runs")):
            cap = b.get(bk)
            if cap is None or (k not in lim and not require_limits):
                continue
            v = lim.get(k)
            if v is None:
                return f"limits.{k} không được để trống (không giới hạn) — trần config_bounds.{bk}={cap}"
            if float(v) > float(cap):
                return f"limits.{k}={v} vượt config_bounds.{bk}={cap}"
        if "max_runtime_s" in lim and lim["max_runtime_s"] is None:
            return "không được bỏ giới hạn thời gian chạy"
        return None

    def check_run_budget(self, p: dict[str, Any], key: str = "budget_usd") -> str | None:
        cap = self.bounds().get("max_budget_usd_per_run")
        v = p.get(key)
        if cap is not None and (v is None or float(v) > float(cap)):
            return f"{key} của run/task do điều phối viên giao phải ≤ config_bounds.max_budget_usd_per_run={cap}"
        m = p.get("model")
        models = self.bounds().get("models") or []
        if m and models and m not in models:
            return f"model '{m}' ngoài config_bounds.models {models}"
        return None

    # ------------------------------------------------------------------ MGR-4: ESTOP team theo thành viên
    def estop_teams_of(self, agent_id: str) -> list[str]:
        return [r[0] for r in self.hub.store.conn.execute(
            "SELECT t.id FROM team_members m JOIN teams t ON t.id=m.team_id WHERE m.agent_id=? AND t.estop=1 AND t.archived_at IS NULL", (agent_id,))]

    def _team_estop(self, team_id: str) -> bool:
        r = self.hub.store.conn.execute("SELECT estop FROM teams WHERE id=?", (team_id,)).fetchone()
        return bool(r and r[0])

    # ------------------------------------------------------------------ MGR-7: cổng review của người
    def review_roles(self, agent_id: str) -> list[str]:
        """Team có review mà agent giữ vai reviewer/verifier."""
        out = []
        for r in self.hub.store.conn.execute(
                "SELECT t.id, t.policies, m.role FROM team_members m JOIN teams t ON t.id=m.team_id WHERE m.agent_id=? AND t.archived_at IS NULL", (agent_id,)):
            import json as _j

            pol = _j.loads(r[1] or "{}") if isinstance(r[1], str) else (r[1] or {})
            if r[2] in REVIEW_ROLES and pol.get("review_required"):
                out.append(f"{r[0]}:{r[2]}")
        return out

    def review_teams_of(self, agent_id: str) -> list[str]:
        """Team có review mà agent là thành viên (bất kỳ vai) — việc của họ chỉ được giao qua team có review (MGR-7)."""
        import json as _j

        out = []
        for r in self.hub.store.conn.execute(
                "SELECT t.id, t.policies FROM team_members m JOIN teams t ON t.id=m.team_id WHERE m.agent_id=? AND t.archived_at IS NULL", (agent_id,)):
            pol = _j.loads(r[1] or "{}") if isinstance(r[1], str) else (r[1] or {})
            if pol.get("review_required"):
                out.append(r[0])
        return out

    def _team_policies(self, team_id: str) -> dict[str, Any]:
        import json as _j

        r = self.hub.store.conn.execute("SELECT policies FROM teams WHERE id=?", (team_id,)).fetchone()
        return (_j.loads(r[0] or "{}") if r and isinstance(r[0], str) else {}) if r else {}

    # ------------------------------------------------------------------ RPC (lớp 2, daemon)
    def _run_agent(self, ref: Any) -> str | None:
        run = self.hub.store.find_run(str(ref or ""))
        return run["agent_id"] if run else None

    def check_rpc(self, caller: "Caller", method: str, p: dict[str, Any]) -> str | None:
        """Lý do từ chối (chuỗi) hoặc None. Gọi cho MỌI RPC của `Caller.manager` (sau kiểm MANAGER_ALLOWED)."""
        mgrs = self.manager_ids() | {str(caller.manager)}
        why = self.rate_block(method)
        if why:
            return why
        if method in ("question.answer", "question.escalate"):
            # UX: chỉ câu hỏi ĐANG CHỜ điều phối viên (agent trong task hỏi); câu hỏi đã lên người / của chính manager → người quyết
            q = self.hub.questions.maybe(str(p.get("id") or p.get("question_id") or ""))  # type: ignore[attr-defined]
            if q is None:
                return "không có câu hỏi này"
            if q["status"] != "with_manager" or q.get("routed_to") != "manager":
                return f"câu hỏi {q['id']} không chờ điều phối viên (đang {q['status']}) — chỉ người trả lời"
            if q["agent_id"] in mgrs:
                return "câu hỏi của chính điều phối viên — chỉ người trả lời"
            return None
        if method == "agent.create":
            cfg = dict(p.get("config") or {})
            aid = str(cfg.get("id") or "")
            if aid in mgrs:
                return f"'{aid}' là agent điều phối viên"
            return self.check_agent_spec(aid, cfg, p.get("soul"), creating=True) or self.check_bounds(cfg, require_limits=True)
        if method == "agent.update":
            aid = str(p.get("id") or "")
            ch = dict(p.get("changes") or {})
            why = self.check_agent_spec(aid, ch, p.get("soul"), creating=False) or self.check_bounds(ch, require_limits=False)
            if why:
                return why
            rr = self.review_roles(aid)
            if rr and (p.get("soul") is not None or set(ch) - {"name", "description"}):
                return (f"'{aid}' giữ vai {', '.join(rr)} trong team có review — chỉ sửa được name/description; SOUL/tools/model/limits… "
                        "chỉ đổi qua learn_propose (eval/canary) hoặc bởi người")
            return None
        if method in ("agent.archive", "agent.restore", "learn.refine"):
            aid = str(p.get("id") or p.get("agent") or "")
            if aid in mgrs:
                return f"'{aid}' là agent điều phối viên — không được tác động"
            if method == "agent.archive" and self.review_roles(aid):
                return f"'{aid}' giữ vai reviewer/verifier trong team có review — không xoá mềm"
            if method == "agent.restore" and aid not in (self.hub.store.state_get("manager_archived") or {}):
                return f"'{aid}' không do điều phối viên xoá mềm — chỉ người khôi phục"
            return None
        if method == "team.create":
            cfg = dict(p.get("config") or {})
            why = self.check_team(cfg, creating=True)
            if why:
                return why
            review = bool((cfg.get("policies") or {}).get("review_required"))
            for m in cfg.get("members") or []:
                est = self.estop_teams_of(str(m.get("agent_id") or ""))
                if est:
                    return f"'{m.get('agent_id')}' thuộc team đang ESTOP ({', '.join(est)}) — không đưa sang team khác"
                rv = self.review_teams_of(str(m.get("agent_id") or ""))
                if rv and not review:
                    return f"'{m.get('agent_id')}' thuộc team có review ({', '.join(rv)}) — team mới cũng phải review_required"
                if review and m.get("role") in REVIEW_ROLES:
                    return "vai reviewer/verifier trong team có review do người giao"
            return None
        if method == "team.update":
            tid = str(p.get("id") or "")
            ch = dict(p.get("changes") or {})
            why = self.check_team(ch, creating=False)
            if why:
                return why
            pol, cur = dict(ch.get("policies") or {}), self._team_policies(tid)
            if "review_required" in pol and not pol["review_required"] and cur.get("review_required"):
                return "không được tắt review_required của team (cổng chất lượng của người)"
            for k in ("max_review_rounds", "failure_limit"):
                if k in pol and cur.get(k) is not None and (pol[k] is None or int(pol[k]) > int(cur[k])):
                    return f"policies.{k} chỉ được siết (giảm), không nới"
            if "budget_usd_per_day" in pol:
                cap = self.bounds().get("budget_usd_per_day")
                v = pol["budget_usd_per_day"]
                if v is None and cur.get("budget_usd_per_day") is not None:
                    return "không được bỏ ngân sách ngày của team"
                if v is not None and cap is not None and float(v) > float(cap) and (cur.get("budget_usd_per_day") is None or float(v) > float(cur["budget_usd_per_day"])):
                    return f"policies.budget_usd_per_day={v} vượt config_bounds.budget_usd_per_day={cap}"
            return None
        if method in ("team.member_set", "team.member_rm"):
            tid, aid = str(p.get("team") or ""), str(p.get("agent") or "")
            if aid in mgrs:
                return "không đưa agent điều phối viên vào team"
            if self._team_estop(tid):
                return f"team '{tid}' đang ESTOP — không thêm/bớt thành viên (chỉ người)"
            est = [t for t in self.estop_teams_of(aid) if t != tid]
            if est:
                return f"'{aid}' thuộc team đang ESTOP ({', '.join(est)})"
            if self._team_policies(tid).get("review_required"):
                cur = self.hub.store.conn.execute("SELECT role FROM team_members WHERE team_id=? AND agent_id=?", (tid, aid)).fetchone()
                if cur and cur[0] in REVIEW_ROLES and (method == "team.member_rm" or (p.get("role") or "worker") not in REVIEW_ROLES):
                    return f"không gỡ/đổi vai {cur[0]} của '{aid}' trong team có review (cổng chất lượng của người)"
                if method == "team.member_set" and (p.get("role") or "worker") in REVIEW_ROLES and not (cur and cur[0] == p.get("role")):
                    return "giao vai reviewer/verifier trong team có review là việc của người"
            elif method == "team.member_set":
                rv = self.review_teams_of(aid)
                if rv:
                    return f"'{aid}' thuộc team có review ({', '.join(rv)}) — không đưa sang team không review"
            return None
        if method == "task.create":
            t = dict(p.get("task") or p)
            if str(t.get("assignee") or "") in mgrs:
                return "không giao task cho agent điều phối viên"
            if t.get("assignee") and self.estop_teams_of(str(t["assignee"])):
                return f"'{t['assignee']}' thuộc team đang ESTOP — không giao việc"
            if t.get("team") and self._team_estop(str(t["team"])):
                return f"team '{t['team']}' đang ESTOP"
            if t.get("assignee"):
                rv = self.review_teams_of(str(t["assignee"]))
                if rv and not (t.get("team") and self._team_policies(str(t["team"])).get("review_required")):
                    return f"'{t['assignee']}' thuộc team có review ({', '.join(rv)}) — giao việc qua team có review"
            return (self.check_path(t.get("workspace"), "workspace của task", agent_id=str(t.get("assignee") or "") or None)
                    or self.check_run_budget(t))
        if method == "run.start":
            if str(p.get("agent") or "") in mgrs:
                return "không chạy run cho agent điều phối viên"
            est = self.estop_teams_of(str(p.get("agent") or ""))
            if est:
                return f"'{p.get('agent')}' thuộc team đang ESTOP ({', '.join(est)})"
            rv = self.review_teams_of(str(p.get("agent") or ""))
            if rv:
                return f"'{p.get('agent')}' thuộc team có review ({', '.join(rv)}) — giao việc qua task của team đó"
            return self.check_path(p.get("cwd"), "cwd", agent_id=str(p.get("agent") or "") or None) or self.check_run_budget(p)
        if method in ("run.steer", "run.stop"):
            if self._run_agent(p.get("id")) in mgrs:
                return "không điều khiển run của agent điều phối viên"
            return None
        if method == "system.job":
            # chỉ job thuộc hệ thống NGƯỜI đã đăng ký, không bị người khoá (systems/service.job_check — service kiểm lại)
            return self.hub.systems.job_check(str(p.get("label") or ""), str(p.get("action") or ""), manager=True)  # type: ignore[attr-defined]
        if method.startswith("schedule."):
            # Lịch chạy tự động: chỉ lịch do điều phối viên tạo; trần số lịch/tần suất/ngân sách; đích theo luật run.start/task.create
            # (schedules/service.manager_check — service kiểm lại lần 3)
            return self.hub.schedules.manager_check(caller, method, p)  # type: ignore[attr-defined]
        if method == "estop.set":
            if p.get("on") is not True:
                return "điều phối viên chỉ được BẬT ESTOP — tắt ESTOP là việc của người"
            return None
        return None
