"""Phần guard trong cấu hình MỖI run (FR-RT-09, FR-RT-17, FR-SG-03/05).

- `settings.json`: hook `PreToolUse` (matcher "*") gọi `python -I -m agent_hub.guard.hook … || exit 2`
  → fail-CLOSED cả khi Python không chạy được (exit≠0 → `|| exit 2` → CLI chặn); `timeout` hook đủ lớn để giữ
  chờ người 300 s; `permissions.deny` cho Edit/Write/MultiEdit/NotebookEdit vào `HUB_HOME/policy/` (FR-SG-05).
- `mcp.json`: entry `hub-guard` (MCP stdio nội bộ) cho `--permission-prompt-tool mcp__hub-guard__approve`.
- Lớp tĩnh `--disallowedTools` cho mẫu luôn-phá-huỷ (CLI tự chặn nếu hook không chạy). Run resume có vé cho phép
  thì bỏ ĐÚNG các mẫu khớp thao tác đã duyệt (spike: hook allow không thắng deny tĩnh).
- **Sandbox OS cho Bash** (hardening SEC, spike thật với claude 2.1.302 — `docs/dev-notes/hardening-SEC.md`): khoá
  `sandbox` của settings (Seatbelt trên macOS, bubblewrap trên Linux; KẾ THỪA cho mọi tiến trình con kể cả `nohup`/
  double-fork/`setsid`; `launchctl submit` bị chặn). Chặn GHI vào `HUB_HOME` (trừ workspace của run), mã nguồn hub;
  chặn ĐỌC token người, secret, `mcp.json` đã resolve, token MCP HTTP; mọi Unix socket (gồm `hub.sock`) bị chặn trừ
  danh sách cho phép. Không phụ thuộc cách agent viết lệnh (python/node/symlink…).
- `check_extra_args` (H-18): chặn cờ bypass/ghi đè settings trong `runtime.claude.isolation.extra_args`.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

GUARD_SERVER = "hub-guard"
PERMISSION_TOOL = f"mcp__{GUARD_SERVER}__approve"
FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def python_bin(cfg: dict[str, Any]) -> str:
    return str(cfg.get("python") or sys.executable)


def hook_timeouts(cfg: dict[str, Any], approval_timeout_s: float) -> tuple[int, int]:
    """(timeout hook cho CLI, timeout RPC của hook). CLI hết giờ hook = fail-OPEN nên hook phải tự xong trước."""
    cli_t = int(max(float(cfg.get("hook_timeout_s") or 900), approval_timeout_s + 120))
    return cli_t, cli_t - 30


def hook_command(run_id: str, socket: Path, rpc_timeout: int, cfg: dict[str, Any]) -> str:
    py = shlex.quote(python_bin(cfg))
    return f"{py} -I -m agent_hub.guard.hook --run {shlex.quote(run_id)} --socket {shlex.quote(str(socket))} --timeout {rpc_timeout} || exit 2"


def policy_deny_rules(policy_dir: Path) -> list[str]:
    # `//` = đường dẫn tuyệt đối trong luật quyền của Claude Code
    p = "/" + str(policy_dir).rstrip("/")
    real = "/" + os.path.realpath(policy_dir).rstrip("/")
    out = []
    for base in dict.fromkeys((p, real)):
        for t in FILE_TOOLS:
            out += [f"{t}({base}/**)", f"{t}({base})"]
    return out


def _abs_rule(p: str | Path) -> str:
    return "/" + str(p).rstrip("/")


def secret_read_deny_rules(hub_home: Path, socket: Path) -> list[str]:
    """`permissions.deny` cho tool Read/Grep/Glob: token người, secret, mcp.json đã resolve, token MCP HTTP."""
    from ..auth import token_path

    out: list[str] = []
    # WEB: `auth/` giữ khoá ký link đăng nhập web (auth/web.json) — sandbox đã denyRead cả thư mục, đây là lớp Read/Grep/Glob
    targets = [str(token_path(socket)), f"{hub_home}/secrets/**", f"{hub_home}/agents/*/runtime/**", f"{hub_home}/mcp_server/tokens.json", f"{hub_home}/auth/**"]
    real = os.path.realpath(hub_home)
    if real != str(hub_home):
        targets += [f"{real}/secrets/**", f"{real}/agents/*/runtime/**", f"{real}/mcp_server/tokens.json", f"{real}/auth/**"]
    for t in dict.fromkeys(targets):
        for tool in ("Read", "Grep", "Glob"):
            out.append(f"{tool}({_abs_rule(t)})")
    for t in (str(token_path(socket)),):
        for tool in FILE_TOOLS:
            out.append(f"{tool}({_abs_rule(t)})")
    return out


AGENT_HOME_KEEP = ("workspaces",)
HUB_WRITE_DENY_NAMES = (
    "policy", "hub.db", "hub.db-wal", "hub.db-shm", "hub.db-journal", "config.yaml", "secrets", "mcp", "mcp_server", "ledger",
    "snapshots", "audit", "trash", "runs", "logs", "prompts", "checkpoints", "global", "teams", "hub.pid", "hub.sock", "auth",
)
AGENT_WRITE_DENY_NAMES = ("config.yaml", "SOUL.md", "MEMORY.md", "USER.md", "skills", ".archive", "runtime", "home", ".tier_overlay")


def sandbox_settings(hub_home: Path, socket: Path, sb_cfg: dict[str, Any], *, cwd: Path | None = None,
                     overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Khoá `sandbox` cho settings.json của run (Claude Code ≥ 2.1.302). Thuần ngoài việc liệt kê thư mục HUB_HOME."""
    from ..auth import token_path
    from .policy import package_dir

    home = Path(hub_home)
    homes = list(dict.fromkeys([str(home), os.path.realpath(home)]))
    deny_w: list[str] = []
    for h in homes:
        hp = Path(h)
        names = set(HUB_WRITE_DENY_NAMES)
        try:
            names |= {e.name for e in hp.iterdir() if e.name != "agents"}
        except OSError:
            pass
        deny_w += [str(hp / n) for n in sorted(names)]
        deny_w += [f"{hp}/agents/*/{n}" for n in AGENT_WRITE_DENY_NAMES]
        try:
            for a in (hp / "agents").iterdir():
                if not a.is_dir():
                    continue
                for e in a.iterdir():
                    if e.name not in AGENT_HOME_KEEP:
                        deny_w.append(str(e))
                deny_w += [str(a / n) for n in AGENT_WRITE_DENY_NAMES]
        except OSError:
            pass
    tok = token_path(socket)
    deny_w += [str(socket), str(tok)]
    if sb_cfg.get("protect_hub_code", True):
        deny_w.append(os.path.realpath(package_dir()))
    # V2-N2: nơi đặt thứ chạy trễ/chạy khi NGƯỜI dùng shell/git/claude — ngoài run, ngoài sandbox
    uh = os.path.expanduser("~")
    deny_w += [f"{uh}/{x}" for x in (".ssh", ".gitconfig", ".config/git", ".config/systemd", ".config/autostart", ".config/fish", ".git-templates",
                                     "Library/LaunchAgents", "Library/LaunchDaemons", ".bashrc", ".bash_profile", ".profile", ".zshrc",
                                     ".zprofile", ".zshenv", ".zlogin", ".claude", ".claude.json", ".local/bin")]
    deny_w += [str(x) for x in sb_cfg.get("extra_deny_write") or []]
    deny_r: list[str] = [str(tok)]
    for h in homes:
        deny_r += [f"{h}/secrets", f"{h}/agents/*/runtime", f"{h}/mcp_server/tokens.json", f"{h}/auth"]
        try:
            for a in (Path(h) / "agents").iterdir():
                deny_r.append(str(a / "runtime"))
        except OSError:
            pass
    deny_r += [str(x) for x in sb_cfg.get("extra_deny_read") or []]
    socks = [str(x) for x in sb_cfg.get("allow_unix_sockets") or []]
    if sb_cfg.get("allow_ssh_agent", True) and os.environ.get("SSH_AUTH_SOCK"):
        socks.append(os.environ["SSH_AUTH_SOCK"])
    bad = {str(socket), os.path.realpath(socket)}
    socks = [x for x in dict.fromkeys(socks) if x not in bad and os.path.realpath(x) not in bad]
    if cwd is not None:  # không bao giờ chặn ghi workspace của chính run (vd eval chạy trên bản sao home)
        cw = {str(cwd).rstrip("/"), os.path.realpath(cwd).rstrip("/")}
        deny_w = [p for p in deny_w if not any(c == p.rstrip("/") or c.startswith(p.rstrip("/") + "/") for c in cw)]
    sb: dict[str, Any] = {
        "enabled": True,
        "failIfUnavailable": bool(sb_cfg.get("fail_if_unavailable", True)),
        "autoAllowBashIfSandboxed": False,  # giữ nguyên luồng quyền: hook guard vẫn quyết từng lệnh
        "allowUnsandboxedCommands": False,  # bỏ qua `dangerouslyDisableSandbox` của model
        "filesystem": {"denyWrite": list(dict.fromkeys(deny_w)), "denyRead": list(dict.fromkeys(deny_r))},
        "network": {"allowedDomains": [str(x) for x in (sb_cfg.get("allowed_domains") if sb_cfg.get("allowed_domains") is not None else ["*"])]},
    }
    aw = [str(x) for x in sb_cfg.get("extra_allow_write") or []]
    if aw:
        sb["filesystem"]["allowWrite"] = aw
    if socks:
        sb["network"]["allowUnixSockets"] = socks
    if (overrides or {}).get("allow_localhost"):
        # ngoại lệ THEO AGENT do người đặt (spike 2.1.302): `allowLocalBinding` cho bind/listen cổng local VÀ kết nối loopback
        # (127.0.0.1/::1, kể cả curl qua proxy). KHÔNG nới filesystem, KHÔNG mở Unix socket (hub.sock vẫn bị chặn — đã kiểm thật).
        sb["network"]["allowLocalBinding"] = True
    return sb


def build_settings(base: dict[str, Any], *, run_id: str, socket: Path, policy_dir: Path, guard_cfg: dict[str, Any], approval_timeout_s: float,
                   cwd: Path | None = None, sandbox_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    s = dict(base)
    cli_t, rpc_t = hook_timeouts(guard_cfg, approval_timeout_s)
    hooks = dict(s.get("hooks") or {})
    pre = list(hooks.get("PreToolUse") or [])
    pre.append({"matcher": "*", "hooks": [{"type": "command", "command": hook_command(run_id, socket, rpc_t, guard_cfg), "timeout": cli_t}]})
    hooks["PreToolUse"] = pre
    s["hooks"] = hooks
    perms = dict(s.get("permissions") or {})
    hub_home = Path(policy_dir).parent
    perms["deny"] = list(perms.get("deny") or []) + policy_deny_rules(policy_dir) + secret_read_deny_rules(hub_home, Path(socket))
    s["permissions"] = perms
    sb_cfg = dict(guard_cfg.get("sandbox") or {})
    if sb_cfg.get("enabled", True):
        user_sb = dict(s.get("sandbox") or {}) if isinstance(s.get("sandbox"), dict) else {}
        user_sb.pop("excludedCommands", None)  # lệnh loại khỏi sandbox = lỗ — không nhận từ cấu hình
        ours = sandbox_settings(hub_home, Path(socket), sb_cfg, cwd=cwd, overrides=sandbox_overrides)
        fs = dict(user_sb.get("filesystem") or {})
        for k in ("denyWrite", "denyRead"):
            ours["filesystem"][k] = list(dict.fromkeys(list(fs.get(k) or []) + ours["filesystem"][k]))
        if fs.get("allowWrite"):
            ours["filesystem"]["allowWrite"] = list(dict.fromkeys(list(fs["allowWrite"]) + ours["filesystem"].get("allowWrite", [])))
        s["sandbox"] = {**user_sb, **ours, "filesystem": {**fs, **ours["filesystem"]}}
    return s


# ---------------------------------------------------------------------------- extra_args (H-18)
_BYPASS_RE = re.compile(r"^--(allow-)?dangerously-skip-permissions(=.*)?$")
_OVERRIDE_FLAGS = ("--settings", "--setting-sources", "--permission-prompt-tool", "--mcp-config", "--strict-mcp-config",
                   "--dangerously-load-development-channels", "--plugin-dir", "--add-dir", "--allowedTools", "--allowed-tools", "--disallowedTools",
                   "--disallowed-tools", "--tools", "--bare")


def check_extra_args(extra: list[str], workspace_kind: str, *, guard_on: bool = True) -> str | None:
    """Trả lý do từ chối nếu `extra_args` lách NFR-15 / ghi đè cấu hình an toàn của hub; None nếu hợp lệ.
    - Mọi biến thể `--dangerously-skip-permissions` / `--allow-dangerously-skip-permissions` → luôn chặn.
    - `--permission-mode bypassPermissions` (kể cả `=`) → chặn trên workspace `dir` (repo thật).
    - Cờ ghi đè settings/hook/MCP/quyền tool của hub → chặn khi guard bật (hub tự sinh các cờ này)."""
    toks = [str(x) for x in extra]
    for i, t in enumerate(toks):
        tl = t.strip()
        if _BYPASS_RE.match(tl):
            return f"extra_args không được chứa {tl} (NFR-15)"
        if tl.startswith("--permission-mode"):
            val = tl.split("=", 1)[1] if "=" in tl else (toks[i + 1] if i + 1 < len(toks) else "")
            if val.strip().lower() == "bypasspermissions" and workspace_kind not in ("scratch", "worktree"):
                return "extra_args: --permission-mode bypassPermissions bị từ chối trên workspace dir (NFR-15)"
        if guard_on and (tl in _OVERRIDE_FLAGS or any(tl.startswith(f + "=") for f in _OVERRIDE_FLAGS)):
            return f"extra_args không được ghi đè cờ an toàn do hub sinh ({tl.split('=', 1)[0]})"
    return None


def guard_mcp_entry(run_id: str, socket: Path, hub_home: Path, guard_cfg: dict[str, Any], mcp_tool_timeout_ms: int) -> dict[str, Any]:
    rpc_t = max(30, int(mcp_tool_timeout_ms / 1000) - 30)
    return {
        "type": "stdio",
        "command": python_bin(guard_cfg),
        "args": ["-I", "-m", "agent_hub.guard.mcp_guard", "--run", run_id, "--socket", str(socket), "--timeout", str(rpc_t)],
        "env": {"AGENT_HUB_HOME": str(hub_home)},
    }


# ---------------------------------------------------------------------------- mẫu tĩnh kiểu CLI
_RULE = re.compile(r"^([A-Za-z0-9_\-*]+)(?:\((.*)\))?$", re.S)


def pattern_matches(pattern: str, tool_name: str, tool_input: dict[str, Any] | None) -> bool:
    """Luật quyền kiểu Claude Code: `Tool`, `Tool(prefix:*)`, `Tool(exact)` — đủ để biết luật nào chặn thao tác đã duyệt.
    Bash: so với từng lệnh con (CLI tách `&&`/`;`/`|`) và cả lệnh đầy đủ."""
    m = _RULE.match(pattern.strip())
    if not m:
        return False
    tool, spec = m.group(1), m.group(2)
    if tool != tool_name and not (tool.endswith("*") and tool_name.startswith(tool[:-1])):
        return False
    if spec is None or spec in ("", "*"):
        return True
    ti = tool_input or {}
    if tool_name == "Bash":
        cmd = re.sub(r"\s+", " ", str(ti.get("command") or "")).strip()
        parts = [cmd] + [p.strip() for p in re.split(r"&&|\|\||;|\||\n", cmd) if p.strip()]
        if spec.endswith(":*"):
            pre = spec[:-2]
            return any(p == pre or p.startswith(pre + " ") or p.startswith(pre) for p in parts)
        return any(p == spec for p in parts)
    target = str(ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or "")
    if spec.startswith("//"):
        spec = spec[1:]
    if spec.endswith("/**"):
        return target == spec[:-3] or target.startswith(spec[:-3] + "/")
    return target == spec


def static_disallowed(guard_cfg: dict[str, Any]) -> list[str]:
    return [str(x) for x in guard_cfg.get("static_disallowed_tools") or []]


def strip_for_tickets(patterns: list[str], tickets: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Bỏ các mẫu deny khớp thao tác đã được người duyệt (vé chưa dùng). Trả (giữ lại, đã bỏ)."""
    keep, removed = [], []
    for p in patterns:
        if any(pattern_matches(p, t["tool_name"], t.get("tool_input") if isinstance(t.get("tool_input"), dict) else {}) for t in tickets):
            removed.append(p)
        else:
            keep.append(p)
    return keep, removed
