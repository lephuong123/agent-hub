"""Tiếp quản run bằng Claude Code tương tác (FR-UI-10, Q-15: chỉ takeover, không `claude --bg`).

`prepare(hub, ref)` (RPC `run.takeover`): với run ĐÃ dừng, sinh `settings.json`/`mcp.json` của agent (cùng cờ cô lập
như run headless, 0600, ở `agents/<id>/runtime/takeover-<run>/`) và trả argv `claude --resume <session>` + cwd của run.
Client (CLI `agent-hub takeover`, phím `T` của TUI) chạy argv đó ở tiền cảnh trong terminal của người; khi thoát
gọi `record` (RPC `run.takeover_record`) → hub ghi một Run `kind=human_takeover` (cùng session, `takeover_of_run_id`),
phát `run.takeover`, xoá file runtime (mcp.json có thể chứa secret đã thay).

Khác run headless: KHÔNG gắn hook guard/hub-guard (người đang ngồi trước Claude Code và tự duyệt bằng prompt quyền
của chính Claude Code), KHÔNG `-p`/stream-json. Giữ: `--setting-sources ""`, `--settings`, `--strict-mcp-config
--mcp-config`, SOUL (`--append-system-prompt`), model, tool_policy (tools/allowed/disallowed, permission_mode
trừ `bypassPermissions`).
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import Conflict, HubError, NotFound
from ..events import make_event
from ..prompt import build_system_prompt
from ..redact import redact_argv
from ..store import now_iso

if TYPE_CHECKING:
    from ..hub import Hub

RUNNING = ("queued", "running")


def _write_0600(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _rt_dir(agent: dict[str, Any], run_id: str) -> Path:
    return Path(agent["home_path"]) / "runtime" / f"takeover-{run_id}"


def build_takeover_argv(
    *,
    binary: str,
    session_id: str,
    settings_path: Path,
    mcp_path: Path,
    system_prompt: str | None,
    model: str | None,
    tool_policy: dict[str, Any],
    isolation: dict[str, Any],
) -> list[str]:
    """argv `claude --resume …` tương tác (hàm thuần, test được)."""
    argv = [binary, "--resume", session_id]
    ss = isolation.get("setting_sources", "")
    if ss is not None:
        argv += ["--setting-sources", ",".join(ss) if isinstance(ss, (list, tuple)) else str(ss)]
    if isolation.get("pass_settings_file", True):
        argv += ["--settings", str(settings_path)]
    argv += ["--mcp-config", str(mcp_path)]
    if isolation.get("strict_mcp_config", True):
        argv.append("--strict-mcp-config")
    if system_prompt:
        argv += ["--append-system-prompt", system_prompt]
    if model:
        argv += ["--model", model]
    pm = tool_policy.get("permission_mode")
    if pm and pm != "bypassPermissions":
        argv += ["--permission-mode", pm]
    if tool_policy.get("tools") is not None:
        argv += ["--tools", ",".join(tool_policy["tools"])]
    if tool_policy.get("allowed_tools"):
        argv += ["--allowedTools", *tool_policy["allowed_tools"]]
    if tool_policy.get("disallowed_tools"):
        argv += ["--disallowedTools", *tool_policy["disallowed_tools"]]
    return argv


def prepare(hub: "Hub", ref: str) -> dict[str, Any]:
    from ..steer import resolve_run

    try:
        run = resolve_run(hub, ref)
    except Conflict:
        # agent không có run đang chạy → lấy run gần nhất của agent
        runs = hub.store.list_runs(agent_id=ref, limit=1)
        if not runs:
            raise NotFound(f"agent '{ref}' chưa có run nào") from None
        run = runs[0]
    if run["status"] in RUNNING:
        raise Conflict(f"run {run['id']} đang {run['status']} — dừng trước (`agent-hub runs stop {run['id']}`) rồi mới takeover")
    if not run.get("session_id"):
        raise HubError(f"run {run['id']} không có session_id")
    agent = hub.store.get_agent(run["agent_id"])
    if agent is None:
        raise NotFound(f"agent '{run['agent_id']}' không còn tồn tại")
    cfg = hub.config.data
    claude_cfg = cfg["runtime"]["claude"]
    iso = claude_cfg.get("isolation") or {}
    rt = _rt_dir(agent, run["id"])
    settings = dict(claude_cfg.get("settings") or {})
    settings_path, mcp_path = rt / "settings.json", rt / "mcp.json"
    _write_0600(settings_path, settings)
    _write_0600(mcp_path, {"mcpServers": hub.runs._agent_mcp_servers(agent)})
    soul_p = Path(agent["home_path"]) / "SOUL.md"
    system_prompt = build_system_prompt(agent["id"], soul_p.read_text(encoding="utf-8") if soul_p.exists() else "")
    tp = dict(agent.get("tool_policy") or {})
    argv = build_takeover_argv(
        binary=str(claude_cfg.get("binary") or "claude"),
        session_id=run["session_id"],
        settings_path=settings_path,
        mcp_path=mcp_path,
        system_prompt=system_prompt,
        model=run.get("model") or agent.get("model"),
        tool_policy=tp,
        isolation=iso,
    )
    cwd = run.get("cwd") or str(Path(agent["home_path"]) / "workspaces" / run["id"])
    shown = redact_argv(argv)
    for i, a in enumerate(shown):
        if i > 0 and shown[i - 1] == "--append-system-prompt":
            shown[i] = "<SOUL>"
    return {
        "run_id": run["id"],
        "agent_id": run["agent_id"],
        "session_id": run["session_id"],
        "status": run["status"],
        "cwd": cwd,
        "cwd_exists": Path(cwd).is_dir(),
        "argv": argv,
        "command": f"cd {shlex.quote(cwd)} && " + " ".join(shlex.quote(a) for a in shown),
        "env_strip": list(claude_cfg.get("env_strip") or []),
        "runtime_dir": str(rt),
    }


def record(hub: "Hub", ref: str, *, exit_code: int | None = None, started_at: str | None = None, launched: bool = True) -> dict[str, Any]:
    run = hub.store.find_run(ref)
    if run is None:
        raise NotFound(f"không tìm thấy run '{ref}'")
    agent = hub.store.get_agent(run["agent_id"])
    if agent is not None:
        shutil.rmtree(_rt_dir(agent, run["id"]), ignore_errors=True)
    if not launched:
        return {"run_id": run["id"], "recorded": False}
    tid = "r_" + uuid.uuid4().hex[:12]
    now = now_iso()
    hub.store.insert_run(
        {
            "id": tid,
            "agent_id": run["agent_id"],
            "agent_config_revision": (agent or {}).get("config_revision"),
            "kind": "human_takeover",
            "prompt": f"(takeover của run {run['id']} bằng Claude Code tương tác)",
            "session_id": run["session_id"],
            "resumed_from_session": run["session_id"],
            "cwd": run.get("cwd"),
            "status": "completed",
            "outcome": "completed",
            "queued_at": started_at or now,
            "started_at": started_at or now,
            "ended_at": now,
            "exit_code": exit_code,
            "parent_run_id": run["id"],
            "takeover_of_run_id": run["id"],
        }
    )
    hub.publish(
        make_event("run.takeover", run["agent_id"], {"takeover_run_id": tid, "of_run_id": run["id"], "exit_code": exit_code}, run_id=tid, session_id=run["session_id"])
    )
    return hub.store.get_run(tid)  # type: ignore[return-value]
