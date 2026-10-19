"""Nối vòng học vào `RunManager._prepare_spec` (FR-RT-10, FR-LN-03).

- `system_prompt_for(hub, run, agent)`: run MỚI → quy ước hub + SOUL + charter team (run có `team_id`, H-15) + memory
  snapshot + chỉ mục skill (FR-RT-10, đúng thứ tự (1)…(6));
  run RESUME/FORK → dùng lại ĐÚNG system prompt của session gốc (frozen snapshot như Hermes: thay đổi memory/skill chỉ
  có hiệu lực từ session mới; hash giống → giữ prompt cache, FR-OB-04). Văn bản lưu content-addressed ở
  `HUB_HOME/prompts/<hash>.md` (0600).
- `apply_mcp(...)`: run review (`run_opts.learning_review`) → CHỈ MCP `hub-self` vai trò `learning`, `--tools Read,Grep,Glob`,
  `--permission-prompts none`, không hub-guard/permission tool, không MCP của agent (FR-LN-03). Run thường có skill →
  thêm `hub-self` vai trò `knowledge` (skills_list/skill_view) nếu run chưa có hub-self.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..prompt import build_system_prompt, prompt_hash

if TYPE_CHECKING:
    from ..hub import Hub

REVIEW_TOOLS = ["Read", "Grep", "Glob"]


def _prompts_dir(hub: "Hub") -> Path:
    d = hub.paths.home / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_prompt(hub: "Hub", text: str) -> str:
    h = prompt_hash(text)
    p = _prompts_dir(hub) / f"{h}.md"
    if not p.exists():
        tmp = p.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(p)
    return h


def load_prompt(hub: "Hub", h: str | None) -> str | None:
    if not h:
        return None
    p = _prompts_dir(hub) / f"{h}.md"
    return p.read_text(encoding="utf-8") if p.exists() else None


def system_prompt_for(hub: "Hub", run: dict[str, Any], agent: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    sess = run.get("resumed_from_session")
    if sess:
        r = hub.store.conn.execute("SELECT system_prompt_hash FROM runs WHERE session_id=? AND system_prompt_hash IS NOT NULL ORDER BY queued_at LIMIT 1", (sess,)).fetchone()
        text = load_prompt(hub, r[0] if r else None)
        if text is not None:
            return text, {"reused_session_prompt": True, "skills": None}
    home = Path(agent["home_path"])
    soul_path = home / "SOUL.md"
    soul = soul_path.read_text(encoding="utf-8") if soul_path.exists() else ""
    blocks: list[str] = []
    info: dict[str, Any] = {"skills": 0}
    ks = getattr(hub, "knowledge", None)
    if ks is not None:
        try:
            from ..knowledge.prompt_blocks import build_knowledge_blocks

            blocks, info = build_knowledge_blocks(ks, agent["id"], home=home)
        except Exception:  # pragma: no cover - tri thức hỏng không chặn run
            import logging

            logging.getLogger("cfharness.learning").exception("không dựng được khối tri thức cho %s", agent["id"])
    text = build_system_prompt(agent["id"], soul, blocks, charter=team_charter(hub, run.get("team_id")))
    save_prompt(hub, text)
    return text, info


def team_charter(hub: "Hub", team_id: str | None) -> str:
    """Khối charter (3) của team mà run thuộc về (FR-RT-10). Run không có team → rỗng."""
    if not team_id:
        return ""
    from ..prompt import charter_block

    teams = getattr(hub, "teams", None)
    team = teams.find(team_id) if teams is not None else None
    if not team:
        return ""
    return charter_block(team["id"], team.get("name") or team["id"], team.get("charter") or "")


def apply_mcp(hub: "Hub", run: dict[str, Any], agent: dict[str, Any], *, servers: dict[str, Any], tp: dict[str, Any], allowed_mcp: list[str],
              env_extra: dict[str, str], perm_tool: str | None, guard_cfg: dict[str, Any], info: dict[str, Any]) -> tuple[str | None, dict[str, str]]:
    from ..mcp_server.hub_self import SERVER_NAME, server_entry

    opts = run.get("run_opts") or {}
    run_id = run["id"]
    if opts.get("learning_review"):
        servers.clear()
        servers[SERVER_NAME] = server_entry(run_id, hub.paths.socket, hub.paths.home, "learning", guard_cfg)
        allowed_mcp[:] = [SERVER_NAME]
        tp["tools"] = list(REVIEW_TOOLS)
        tp["allowed_tools"] = list(REVIEW_TOOLS) + [f"mcp__{SERVER_NAME}"]
        tp["permission_prompts"] = "none"
        if tp.get("permission_mode") in ("bypassPermissions", "acceptEdits", "auto", "dontAsk"):
            tp["permission_mode"] = None
        return None, {**env_extra, "CFHARNESS_RUN_ID": run_id}
    if SERVER_NAME in servers or not hub.config.get("learning.knowledge_tools"):
        return perm_tool, env_extra
    has_skills = info.get("skills")
    if has_skills is None:  # prompt dùng lại của session: có hub-self nếu session gốc có skill
        has_skills = bool(hub.knowledge.effective_skills(agent["id"], home=Path(agent["home_path"]))) if getattr(hub, "knowledge", None) else False
    if has_skills:
        servers[SERVER_NAME] = server_entry(run_id, hub.paths.socket, hub.paths.home, "knowledge", guard_cfg)
        allowed_mcp.append(SERVER_NAME)
        tp["allowed_tools"] = list(dict.fromkeys(list(tp.get("allowed_tools") or []) + [f"mcp__{SERVER_NAME}"]))
        env_extra = {**env_extra, "CFHARNESS_RUN_ID": run_id}
    return perm_tool, env_extra
