"""Hồ sơ phê duyệt `approval.profile: strict | balanced | autonomous` (docs/dev-notes/approval-profiles.md).

Profile chỉ là cách đặt sẵn một nhóm khoá cụ thể (`KNOBS`). Khoá người đặt RÕ trong config.yaml luôn thắng profile
(`HubConfig.explicit`) — cài đặt cũ có `guard.hold_uncertain: true` giữ nguyên hành vi. Đọc khoá qua `knob()`, không đọc
`config.get()` trực tiếp (DEFAULTS của các khoá này là null = "theo profile").

`never_automatic()` là ranh giới DUY NHẤT cho mọi đường tự động (cho phép theo checkpoint, cho phép trong scratch, giao điều
phối viên duyệt, cho chạy uncertain khi không giữ): thao tác thuộc danh sách này LUÔN chờ người, ở mọi profile.

Chỉ người đặt profile: `config.set` ∈ `auth.HUMAN_ONLY`; nhánh `approval`/`guard` ∈ `integrity.SECURITY_KEYS` (sửa tay ngoài RPC →
khôi phục); `cfharness config set approval.*` từ agent = `hub_human_only` (classifier); vòng học chặn khoá `approval.*`/`guard.*`.
"""

from __future__ import annotations

import os
from typing import Any

PROFILES = ("strict", "balanced", "autonomous")
DEFAULT_PROFILE = "balanced"
ON_TIMEOUT = ("wait", "deny_and_continue")
# lớp approval (destructive_class) điều phối viên CÓ THỂ được giao — hub/policy/guard/network không bao giờ
DELEGABLE_CLASSES = ("file", "git", "mcp", "db", "system")

# khoá → giá trị theo profile
KNOBS: dict[str, dict[str, Any]] = {
    "guard.hold_uncertain": {"strict": True, "balanced": False, "autonomous": False},
    "guard.safe_reset_probe": {"strict": False, "balanced": True, "autonomous": True},
    "guard.own_worktree_writes": {"strict": False, "balanced": True, "autonomous": True},
    # script đã commit trong repo (track, chưa sửa so với HEAD, commit trước khi task bắt đầu) → không coi API xoá trong mã là phá huỷ
    "guard.trusted_repo_scripts": {"strict": False, "balanced": True, "autonomous": True},
    # "file run tự tạo" tính từ run ĐẦU của task (không chỉ session hiện tại); thư mục tạm của hệ thống do task tạo xoá không hỏi
    "guard.task_created_deletes": {"strict": False, "balanced": True, "autonomous": True},
    "approval.checkpoint_auto_allow": {"strict": False, "balanced": True, "autonomous": True},
    "approval.scratch_auto_allow": {"strict": False, "balanced": False, "autonomous": True},
    "approval.delegate_to_manager.enabled": {"strict": False, "balanced": True, "autonomous": True},
}
# giá trị lạ (sửa tay) → giá trị an toàn nhất
FAIL_CLOSED = {"guard.hold_uncertain": True}

# ---------------------------------------------------------------------------- KHÔNG BAO GIỜ tự động (mọi profile)
# lớp: vùng hub/chính sách/guard (gồm secret của hub), mạng (SSRF, domain người chặn/ngoài allowlist)
NEVER_KLASS = frozenset({"hub", "policy", "guard", "network"})
# luật: git push/force push/đổi remote, thông tin xác thực, vùng thực thi ngoài sandbox, chạy trễ, MCP người đánh dấu cần duyệt,
# MCP ngoài run, ESTOP/run không hoạt động, lỗi bộ phân loại
NEVER_RULES = frozenset({
    "git_push", "git_push_force_or_delete", "git_push_refspec", "protected_branch_push", "git_remote_change",
    "git_credential", "git_config_exec", "keychain_access", "keychain_delete", "forge_token_access", "hub_secret_read",
    "exec_zone_write", "schedule_persistence", "download_and_execute", "persistence",
    "mcp_require_approval", "mcp_not_in_run", "self_approval", "hub_mcp_human_only", "hub_human_only",
    "estop", "unknown_run", "classifier_error", "nesting_too_deep",
    "ssrf_metadata", "ssrf_private_ip", "ssrf_internal_name", "ssrf_private_resolved", "webfetch_local", "url_dns_failed",
    "web_domain_denied", "web_domain_not_allowed", "url_scheme", "url_credentials", "url_no_host", "url_unparsable",
    "delete_critical", "move_critical", "write_block_device", "disk_admin", "fork_bomb", "system_power",
    "chrome_sensitive",  # Claude in Chrome: JS tuỳ ý / tải file lên trên Chrome THẬT của người — chỉ người quyết
})
_NEVER_WORDS = ("credential", "keychain", "secret", "token", "sandbox", "estop", "protected", "push")


def profile(config: Any) -> str:
    """Profile hiệu lực. Giá trị lạ (sửa tay) → strict (fail-closed)."""
    try:
        v = config.get("approval.profile")
    except KeyError:
        v = None
    if v is None:
        return DEFAULT_PROFILE
    return v if v in PROFILES else "strict"


def knob(config: Any, key: str) -> Any:
    """Giá trị hiệu lực của khoá profile: người đặt rõ → giá trị đó; không thì theo profile."""
    has, v = config.explicit(key) if hasattr(config, "explicit") else (False, None)
    if has:
        if isinstance(v, bool):
            return v
        return FAIL_CLOSED.get(key, False)  # kiểu lạ → an toàn nhất
    return KNOBS[key][profile(config)]


def resolved(config: Any) -> dict[str, Any]:
    """Bảng hiệu lực cho CLI/web: {profile, knobs: {khoá: {value, source: profile|explicit}}}."""
    out: dict[str, Any] = {"profile": profile(config), "knobs": {}}
    for k in KNOBS:
        has, _ = config.explicit(k)
        out["knobs"][k] = {"value": knob(config, k), "source": "explicit" if has else "profile"}
    out["on_timeout"] = on_timeout(config, None)
    out["delegate_to_manager"] = delegation_cfg(config)
    return out


def on_timeout(config: Any, agent_id: str | None) -> str:
    try:
        per = config.get("approval.on_timeout_by_agent") or {}
    except KeyError:
        per = {}
    v = per.get(agent_id) if isinstance(per, dict) and agent_id else None
    if v is None:
        try:
            v = config.get("approval.on_timeout")
        except KeyError:
            v = "wait"
    return v if v in ON_TIMEOUT else "wait"  # lạ → wait (Q-04: không bao giờ tự từ chối)


def delegation_cfg(config: Any) -> dict[str, Any]:
    try:
        raw = config.get("approval.delegate_to_manager") or {}
    except KeyError:
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    classes = [c for c in (raw.get("classes") or []) if c in DELEGABLE_CLASSES] if isinstance(raw.get("classes"), list) else []
    try:
        timeout = max(30.0, float(raw.get("timeout_s") or 600))
    except (TypeError, ValueError):
        timeout = 600.0
    return {"enabled": bool(knob(config, "approval.delegate_to_manager.enabled")), "classes": classes, "timeout_s": timeout}


# ---------------------------------------------------------------------------- ranh giới
def _canon(p: str) -> str:
    """Khoá so sánh (Windows: không phân biệt hoa/thường, `\\`→`/`)."""
    from .policy import fold_path

    return fold_path(p).rstrip("/")


def _realpath(p: str) -> str:
    try:
        return os.path.realpath(os.path.expanduser(p))
    except (OSError, ValueError):
        return p


def _under_any(p: str, roots: list[str]) -> bool:
    c = _canon(p)
    for r in roots:
        if not r:
            continue
        if c == r or c.startswith(r + "/"):
            return True
    return False


def allowed_roots(hub: Any) -> list[str]:
    """`workspace.allowed_roots` (+ vùng scratch/worktree của hub) dạng chuẩn — khớp `workspace.validate_workspace`."""
    from pathlib import Path

    from .. import workspace as _ws

    out: list[str] = []
    try:
        roots = hub.config.get("workspace.allowed_roots") or []
    except KeyError:
        roots = []
    if not roots:
        roots = [os.path.join(os.path.expanduser("~"), "Workspace")]
    hh = Path(hub.paths.home)
    cand = [str(r) for r in roots] + [str(hh / "agents"), str(hh / "worktrees")] + list(_ws._EXTRA_ALLOWED_ROOTS)
    env_extra = os.environ.get("CFHARNESS_EXTRA_WORKSPACE_ROOTS")  # test-only (như validate_workspace)
    if env_extra:
        cand += [r for r in env_extra.split(os.pathsep) if r]
    for r in cand:
        try:
            out.append(_canon(os.path.realpath(os.path.expanduser(r))))
            out.append(_canon(os.path.expanduser(r)))
        except (OSError, ValueError):
            continue
    return list(dict.fromkeys(x for x in out if x))


def never_automatic(v: Any, *, tool_name: str = "", tool_input: dict[str, Any] | None = None, roots: list[str] | None = None,
                    cwd: str | None = None) -> str | None:
    """Lý do (chuỗi) nếu thao tác KHÔNG BAO GIỜ được quyết tự động (hub tự cho phép / điều phối viên duyệt); None = được xét.
    `v` là `Verdict` hoặc dict `{verdict, rule, klass, protected_id, forbidden, targets}`."""
    g = (lambda k, d=None: v.get(k, d)) if isinstance(v, dict) else (lambda k, d=None: getattr(v, k, d))
    rule = str(g("rule") or "")
    klass = str(g("klass") or "")
    if g("forbidden"):
        return "thao tác bị cấm (forbidden)"
    if g("protected_id"):
        return f"tài nguyên được bảo vệ ({g('protected_id')})"
    if klass in NEVER_KLASS:
        return f"vùng {klass} (hub/chính sách/secret/mạng)"
    base = rule[4:] if rule.startswith("llm:") else rule
    if base in NEVER_RULES or base.startswith("protected_") or any(w in base for w in _NEVER_WORDS):
        return f"luật {base} (push/remote/thông tin xác thực/chính sách người)"
    ti = tool_input or {}
    if ti.get("dangerouslyDisableSandbox"):
        return "lệnh xin chạy ngoài sandbox"
    if tool_name.startswith("mcp__"):
        return None  # MCP: đích là server/tool (luật bảo vệ/require_approval đã loại ở trên)
    if roots is not None:
        if cwd and not _under_any(cwd, roots):
            return f"cwd {cwd} ngoài workspace.allowed_roots"
        targets = [str(t) for t in (g("targets") or []) if t]
        if klass == "file" and not targets:
            return "không xác định được đích (không kiểm được workspace.allowed_roots)"
        out = [t for t in targets if not (_under_any(t, roots) and _under_any(_realpath(t), roots))]  # symlink ra ngoài cũng tính
        if out:
            return f"đường dẫn ngoài workspace.allowed_roots ({out[0]})"
    return None


def delegable(v: Any, config: Any, **kw: Any) -> str | None:
    """Lý do KHÔNG giao điều phối viên được (None = giao được): ngoài lớp người cho phép, hoặc thuộc danh sách "không bao giờ"."""
    cfg = delegation_cfg(config)
    if not cfg["enabled"]:
        return "approval.delegate_to_manager tắt"
    klass = str((v.get("klass") if isinstance(v, dict) else getattr(v, "klass", None)) or "")
    if klass not in cfg["classes"]:
        return f"lớp '{klass or '-'}' không nằm trong approval.delegate_to_manager.classes"
    return never_automatic(v, **kw)


# ---------------------------------------------------------------------------- kiểm khi config.set
def _bool_or_null(key: str, value: Any) -> None:
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{key} phải là true/false hoặc null (= theo approval.profile)")


def validate_setting(key: str, value: Any) -> None:
    if key == "approval":
        if not isinstance(value, dict):
            raise ValueError("approval phải là object")
        for k, x in value.items():
            validate_setting(f"approval.{k}", x)
        return
    if key == "guard":
        if isinstance(value, dict):
            for k in ("hold_uncertain", "safe_reset_probe", "own_worktree_writes", "trusted_repo_scripts", "task_created_deletes"):
                if k in value:
                    _bool_or_null(f"guard.{k}", value[k])
        return
    if key in ("guard.hold_uncertain", "guard.safe_reset_probe", "guard.own_worktree_writes", "guard.trusted_repo_scripts", "guard.task_created_deletes",
               "approval.checkpoint_auto_allow", "approval.scratch_auto_allow", "approval.delegate_to_manager.enabled"):
        _bool_or_null(key, value)
    elif key == "approval.profile":
        if value not in PROFILES:
            raise ValueError(f"approval.profile phải là {'|'.join(PROFILES)}")
    elif key == "approval.on_timeout":
        if value not in ON_TIMEOUT:
            raise ValueError(f"approval.on_timeout phải là {'|'.join(ON_TIMEOUT)}")
    elif key == "approval.on_timeout_by_agent":
        if not isinstance(value, dict) or not all(isinstance(k, str) and v in ON_TIMEOUT for k, v in value.items()):
            raise ValueError(f"approval.on_timeout_by_agent phải là {{agent_id: {'|'.join(ON_TIMEOUT)}}}")
    elif key.startswith("approval.on_timeout_by_agent."):
        if value is not None and value not in ON_TIMEOUT:
            raise ValueError(f"{key} phải là {'|'.join(ON_TIMEOUT)} hoặc null")
    elif key == "approval.delegate_to_manager":
        if not isinstance(value, dict):
            raise ValueError("approval.delegate_to_manager phải là {enabled, classes, timeout_s}")
        bad = sorted(set(value) - {"enabled", "classes", "timeout_s"})
        if bad:
            raise ValueError(f"approval.delegate_to_manager: khoá lạ {bad}")
        for k, x in value.items():
            validate_setting(f"approval.delegate_to_manager.{k}", x)
    elif key == "approval.delegate_to_manager.classes":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError("approval.delegate_to_manager.classes phải là danh sách lớp")
        bad = sorted(set(value) - set(DELEGABLE_CLASSES))
        if bad:
            raise ValueError(f"approval.delegate_to_manager.classes: {bad} không bao giờ giao cho điều phối viên (được: {', '.join(DELEGABLE_CLASSES)})")
    elif key == "approval.delegate_to_manager.timeout_s":
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 30:
            raise ValueError("approval.delegate_to_manager.timeout_s phải là số ≥ 30")
