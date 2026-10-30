"""Quản lý agent (FR-AG-01..05): tạo, liệt kê, xem, sửa (có revision), xoá mềm, khôi phục, pause.

Thư mục home: `HUB_HOME/agents/<id>/` gồm `config.yaml`, `SOUL.md` (tiếng Anh), `MEMORY.md`,
`USER.md`, `skills/`, `.archive/`, `runtime/`, `workspaces/`.
Xoá = xoá mềm (FR-SG-06): chuyển home vào `HUB_HOME/trash/agents/`, giữ `retention.soft_delete_days`.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from . import names as _names
from .config import VALID_PERMISSION_MODES, HubConfig
from .errors import Blocked, Conflict, HubError, NotFound
from .events import HubEvent, make_event
from .paths import HubPaths
from .store import Store, now_iso, purge_after

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
MODEL_ALIASES = ("opus", "sonnet", "haiku", "fable")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
STATUSES = ("idle", "working", "waiting", "paused", "error", "disabled")
# H-08: id agent KHÔNG được trùng từ khoá con của `cfharness run …`/`stop`/`steer` — nếu không, `run logs <x>` có thể
# bị hiểu thành "chạy agent `logs` với prompt <x>". Chỉ kiểm khi TẠO (agent cũ trùng tên vẫn đọc/sửa được).
RESERVED_IDS = frozenset({
    "list", "show", "logs", "stop", "follow", "continue", "resume", "revert", "diff", "steer", "status", "all",
    "help", "detach", "wait", "takeover", "attach",
})
EDITABLE = (
    "name",
    "role",
    "description",
    "model",
    "effort",
    "fallback_model",
    "tool_policy",
    "mcp_servers",
    "limits",
    "workspace_default",
    # M3: subagent của Claude Code → `--agents` (FR-RT-13); schema output → `--json-schema` (FR-RT-14)
    "subagents",
    "result_schema",
)

DEFAULT_SOUL = """# {name}

You are **{name}**, an agent managed by cfharness.
Role: {role}

## How you work
- Be concise and concrete. Prefer doing the task over describing it.
- Stay within the working directory you were given unless told otherwise.
- When you finish, end with a short summary of what you did and anything left open.
"""


def _valid_model(m: str) -> bool:
    return m in MODEL_ALIASES or m.startswith("claude-")


class ToolPolicy(BaseModel):
    permission_mode: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)
    tools: list[str] | None = None
    # NFR-15: chỉ khi người khai báo rõ VÀ workspace là scratch/worktree
    allow_bypass: bool = False
    permission_prompts: str | None = None
    # Claude in Chrome (browser.py): None = theo runtime.claude.chrome.default_mode của hub (mặc định auto) | none | playwright |
    # chrome (bỏ Playwright khỏi run) | auto (Chrome trước)
    browser: Literal["none", "playwright", "chrome", "auto"] | None = None
    # allowlist domain WebFetch/Chrome theo agent (guard/service.agent_web_check): None = không giới hạn (hành vi cũ) | [] = chặn mọi WebFetch
    web_allow_domains: list[str] | None = None

    @field_validator("permission_mode")
    @classmethod
    def _pm(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_PERMISSION_MODES:
            raise ValueError(f"permission_mode phải là một trong {VALID_PERMISSION_MODES}")
        return v

    @field_validator("web_allow_domains", mode="before")
    @classmethod
    def _web_domains(cls, v: Any) -> list[str] | None:
        from .guard.url_safety import normalize_agent_domains

        return normalize_agent_domains(v)


class Limits(BaseModel):
    max_concurrent_runs: int = Field(default=1, ge=1)
    max_budget_usd_per_run: float | None = Field(default=None, gt=0)
    budget_usd_per_day: float | None = Field(default=None, gt=0)
    max_runtime_s: int | None = Field(default=None, gt=0)


class AgentConfig(BaseModel):
    id: str
    name: str
    role: str = ""
    description: str = ""
    model: str = "sonnet"
    effort: str | None = None
    fallback_model: str | None = None
    tool_policy: ToolPolicy = Field(default_factory=ToolPolicy)
    mcp_servers: list[str] = Field(default_factory=list)
    limits: Limits = Field(default_factory=Limits)
    workspace_default: str | None = None
    subagents: dict[str, dict[str, Any]] | None = None
    result_schema: dict[str, Any] | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError("id phải là slug: chữ thường, số, '-' hoặc '_', bắt đầu bằng chữ/số, ≤ 63 ký tự")
        return v

    @field_validator("model")
    @classmethod
    def _model(cls, v: str) -> str:
        if not _valid_model(v):
            raise ValueError(f"model không hợp lệ: dùng {'/'.join(MODEL_ALIASES)} hoặc id đầy đủ 'claude-…'")
        return v

    @field_validator("fallback_model")
    @classmethod
    def _fb(cls, v: str | None) -> str | None:
        if v is not None and not _valid_model(v):
            raise ValueError("fallback_model không hợp lệ")
        return v

    @field_validator("effort")
    @classmethod
    def _effort(cls, v: str | None) -> str | None:
        if v is not None and v not in EFFORTS:
            raise ValueError(f"effort phải là một trong {EFFORTS}")
        return v


def _validation_message(e: ValidationError) -> tuple[str, list[dict[str, str]]]:
    fields = [{"field": ".".join(str(x) for x in err["loc"]), "error": err["msg"]} for err in e.errors()]
    return "cấu hình agent không hợp lệ: " + "; ".join(f"{f['field']}: {f['error']}" for f in fields), fields


def load_mcp_registry(paths: HubPaths) -> dict[str, dict[str, Any]]:
    """Registry MCP tối thiểu cho M0: `HUB_HOME/mcp/registry.yaml` dạng `{servers: {name: {command, args, env…}}}`."""
    if not paths.mcp_registry.exists():
        return {}
    data = yaml.safe_load(paths.mcp_registry.read_text(encoding="utf-8")) or {}
    servers = data.get("servers", data) if isinstance(data, dict) else {}
    return servers if isinstance(servers, dict) else {}


class AgentService:
    def __init__(
        self,
        store: Store,
        paths: HubPaths,
        config: HubConfig,
        publish: Callable[[HubEvent], Any],
        *,
        active_runs: Callable[[str], int] = lambda _a: 0,
    ) -> None:
        self.store = store
        self.paths = paths
        self.config = config
        self.publish = publish
        self.active_runs = active_runs
        self.hub: Any = None  # gắn sau trong Hub.__init__ (validate_workspace cần config/guard)

    # ------------------------------------------------------------------ helpers
    def _check_mcp(self, cfg: AgentConfig) -> None:
        if not cfg.mcp_servers:
            return
        reg = load_mcp_registry(self.paths)
        missing = [s for s in cfg.mcp_servers if s not in reg]
        if missing:
            raise HubError(
                f"MCP server không có trong registry ({self.paths.mcp_registry}): {', '.join(missing)}",
                data={"fields": [{"field": "mcp_servers", "error": f"không có: {m}"} for m in missing]},
            )

    @staticmethod
    def _to_row_fields(cfg: AgentConfig) -> dict[str, Any]:
        d = cfg.model_dump()
        return {k: d[k] for k in EDITABLE}

    def _write_config_file(self, home: Path, cfg: AgentConfig) -> None:
        (home / "config.yaml").write_text(
            yaml.safe_dump(cfg.model_dump(), allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    def config_of(self, row: dict[str, Any]) -> AgentConfig:
        return AgentConfig(id=row["id"], **{k: row[k] for k in EDITABLE if row.get(k) is not None})

    def get_row(self, agent_id: str, *, allow_archived: bool = False) -> dict[str, Any]:
        row = self.store.get_agent(agent_id)
        if row is None or (row.get("archived_at") and not allow_archived):
            raise NotFound(f"không tìm thấy agent '{agent_id}'")
        return row

    # ------------------------------------------------------------------ CRUD
    def create(self, data: dict[str, Any], soul: str | None = None) -> dict[str, Any]:
        try:
            cfg = AgentConfig(**{k: v for k, v in data.items() if v is not None})
        except ValidationError as e:
            msg, fields = _validation_message(e)
            raise HubError(msg, data={"fields": fields}) from None
        if cfg.id in RESERVED_IDS:
            raise HubError(
                f"id agent '{cfg.id}' trùng từ khoá lệnh CLI ({', '.join(sorted(RESERVED_IDS))}) — chọn tên khác",
                data={"fields": [{"field": "id", "error": "trùng từ khoá CLI"}]},
            )
        existing = self.store.get_agent(cfg.id)
        if existing is not None:
            extra = " (đang trong thùng rác — dùng `agent restore`)" if existing.get("deleted_at") else ""
            raise Conflict(f"agent đã tồn tại: {cfg.id}{extra}")
        self._check_mcp(cfg)
        if cfg.workspace_default:
            from .workspace import validate_workspace
            validate_workspace(self.hub, cfg.workspace_default, "workspace_default")
        home = self.paths.agent_home(cfg.id)
        if home.exists():
            raise Conflict(f"agent đã tồn tại: thư mục {home} đã có")
        for sub in ("skills", ".archive", "runtime", "workspaces"):
            (home / sub).mkdir(parents=True, exist_ok=True)
        self._write_config_file(home, cfg)
        soul_text = soul or DEFAULT_SOUL.format(name=cfg.name, role=cfg.role or "general assistant")
        (home / "SOUL.md").write_text(soul_text, encoding="utf-8")
        (home / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
        (home / "USER.md").write_text("# User profile\n", encoding="utf-8")
        now = now_iso()
        with self.store.tx():
            self.store.insert_agent(
                {
                    "id": cfg.id,
                    "home_path": str(home),
                    "status": "idle",
                    "config_revision": 1,
                    "created_at": now,
                    "updated_at": now,
                    **self._to_row_fields(cfg),
                }
            )
            self.store.add_revision(cfg.id, 1, {**cfg.model_dump(), "soul": soul_text}, "user")
        self.publish(make_event("agent.created", cfg.id, {"agent_id": cfg.id, "revision": 1, "model": cfg.model}))
        return self.show(cfg.id)

    def list(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        return self.store.list_agents(include_archived=include_archived)

    def show(self, agent_id: str, *, allow_archived: bool = True) -> dict[str, Any]:
        row = self.get_row(agent_id, allow_archived=allow_archived)
        home = Path(row["trash_path"] or row["home_path"]) if row.get("deleted_at") else Path(row["home_path"])
        soul_path = home / "SOUL.md"
        row["soul"] = soul_path.read_text(encoding="utf-8") if soul_path.exists() else None
        skills_dir = home / "skills"
        row["skills"] = sorted(p.name for p in skills_dir.iterdir() if p.is_dir()) if skills_dir.exists() else []
        mem = home / "MEMORY.md"
        row["memory_chars"] = len(mem.read_text(encoding="utf-8")) if mem.exists() else 0
        row["cost_today_usd"] = round(self.store.cost_today(agent_id), 8)
        row["recent_runs"] = [
            {k: r[k] for k in ("id", "status", "outcome", "cost_usd", "queued_at", "agent_config_revision")}
            for r in self.store.list_runs(agent_id=agent_id, limit=5)
        ]
        return row

    def soul(self, agent_id: str) -> str:
        row = self.get_row(agent_id)
        p = Path(row["home_path"]) / "SOUL.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def update(self, agent_id: str, changes: dict[str, Any], *, soul: str | None = None, actor: str = "user") -> dict[str, Any]:
        row = self.get_row(agent_id)
        if "id" in changes and changes["id"] != agent_id:
            raise HubError("không được đổi id của agent")
        unknown = [k for k in changes if k not in EDITABLE and k != "id"]
        if unknown:
            raise HubError(f"trường không sửa được/không tồn tại: {', '.join(unknown)}")
        current = self.config_of(row).model_dump()
        merged = dict(current)
        for k, v in changes.items():
            if k == "id":
                continue
            if isinstance(v, dict) and isinstance(current.get(k), dict):
                merged[k] = {**current[k], **v}
            else:
                merged[k] = v
        try:
            cfg = AgentConfig(**merged)
        except ValidationError as e:
            msg, fields = _validation_message(e)
            raise HubError(msg, data={"fields": fields}) from None
        self._check_mcp(cfg)
        if cfg.workspace_default and cfg.workspace_default != current.get("workspace_default"):
            from .workspace import validate_workspace
            validate_workspace(self.hub, cfg.workspace_default, "workspace_default")
        home = Path(row["home_path"])
        changed = {k: v for k, v in cfg.model_dump().items() if current.get(k) != v}
        if not changed and soul is None:
            return self.show(agent_id)
        rev = int(row["config_revision"]) + 1
        if soul is not None:
            (home / "SOUL.md").write_text(soul, encoding="utf-8")
            changed["soul"] = "(updated)"
        self._write_config_file(home, cfg)
        with self.store.tx():
            self.store.update_agent(agent_id, {**self._to_row_fields(cfg), "config_revision": rev, "updated_at": now_iso()})
            cur_soul = (home / "SOUL.md").read_text(encoding="utf-8") if (home / "SOUL.md").exists() else None
            # H-07: snapshot giữ cả SOUL để `agent revert --to N` khôi phục đúng (revision cũ trước bản này không có khoá `soul`)
            self.store.add_revision(agent_id, rev, {**cfg.model_dump(), "soul_changed": soul is not None, "soul": cur_soul}, actor)
        self.publish(make_event("agent.updated", agent_id, {"agent_id": agent_id, "revision": rev, "changed": sorted(changed)}))
        return self.show(agent_id)

    def team_memberships(self, agent_id: str) -> list[dict[str, Any]]:
        """Team (chưa archive) mà agent là thành viên — FR-AG-04."""
        try:
            rows = self.store.conn.execute(
                "SELECT m.team_id, m.role FROM team_members m JOIN teams t ON t.id = m.team_id "
                "WHERE m.agent_id=? AND t.archived_at IS NULL ORDER BY m.team_id",
                (agent_id,),
            ).fetchall()
        except Exception:  # bảng team chưa có (DB rất cũ) → không có thành viên
            return []
        return [{"team_id": r[0], "role": r[1]} for r in rows]

    def remove(self, agent_id: str, *, detach_teams: bool = False) -> dict[str, Any]:
        """Xoá mềm: archive + chuyển home vào trash (FR-AG-04, FR-SG-06).

        H-06: agent còn là thành viên team → `Blocked` kèm danh sách team (tránh team mồ côi lead/worker), trừ khi
        `detach_teams=True` thì gỡ khỏi mọi team trước (event `team.member{op=remove, reason=agent_removed}`)."""
        row = self.get_row(agent_id)
        n = self.active_runs(agent_id)
        if n:
            raise Blocked(f"agent '{agent_id}' đang có {n} run chạy/chờ — dừng trước (`cfharness stop {agent_id}` hoặc `cfharness run stop <run>`)")
        teams = self.team_memberships(agent_id)
        if teams and not detach_teams:
            desc = ", ".join(f"{t['team_id']} ({t['role']})" for t in teams)
            raise Blocked(
                f"agent '{agent_id}' đang là thành viên team: {desc} — gỡ khỏi team trước "
                f"(`cfharness team member rm <team> {agent_id}`) hoặc dùng `agent rm {agent_id} --detach-teams`",
                data={"teams": teams},
            )
        for t in teams:
            self.store.conn.execute("DELETE FROM team_members WHERE team_id=? AND agent_id=?", (t["team_id"], agent_id))
            ev = make_event("team.member", None, {"team_id": t["team_id"], "agent_id": agent_id, "op": "remove", "role": t["role"], "reason": "agent_removed"})
            ev.team_id = t["team_id"]
            self.publish(ev)
        home = Path(row["home_path"])
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = self.paths.trash_dir / "agents" / f"{agent_id}__{ts}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if home.exists():
            shutil.move(str(home), str(dest))
        days = int(self.config.get("retention.soft_delete_days"))
        now = now_iso()
        self.store.update_agent(
            agent_id,
            {"archived_at": now, "deleted_at": now, "purge_after": purge_after(days), "trash_path": str(dest), "updated_at": now},
        )
        self.publish(make_event("agent.archived", agent_id, {"agent_id": agent_id, "detached_teams": [t["team_id"] for t in teams]}))
        self.publish(make_event("agent.deleted", agent_id, {"agent_id": agent_id, "soft": True, "trash_path": str(dest), "purge_after_days": days}))
        out = self.show(agent_id)
        out["detached_teams"] = teams
        return out

    def revert(self, agent_id: str, to_revision: int, *, actor: str = "user") -> dict[str, Any]:
        """H-07 (FR-AG-03): tạo revision MỚI có nội dung bằng revision `to_revision` (không xoá lịch sử).

        Run đang chạy giữ `agent_config_revision` cũ (spec đã dựng lúc spawn); run kế tiếp dùng revision mới.
        SOUL được khôi phục nếu snapshot đích có lưu SOUL (revision tạo từ bản hardening trở đi)."""
        row = self.get_row(agent_id)
        revs = {int(r["revision"]): r for r in self.store.list_revisions(agent_id)}
        target = revs.get(int(to_revision))
        if target is None:
            raise NotFound(f"agent '{agent_id}' không có revision {to_revision} (có: {', '.join(str(k) for k in sorted(revs)) or '-'})")
        snap = dict(target["config_snapshot"] or {})
        changes = {k: snap.get(k) for k in EDITABLE if k in snap}
        soul = snap.get("soul") if isinstance(snap.get("soul"), str) else None
        if soul is not None and soul == self.soul(agent_id):
            soul = None
        before = int(row["config_revision"])
        # nội dung đã bằng revision đích vẫn ghi revision mới — lịch sử thể hiện thao tác revert
        res = self._apply_revert(agent_id, changes, soul, actor=actor, to_revision=int(to_revision))
        res["reverted_from_revision"] = before
        res["reverted_to_revision"] = int(to_revision)
        res["soul_restored"] = soul is not None
        res["soul_in_snapshot"] = "soul" in snap
        return res

    def _apply_revert(self, agent_id: str, changes: dict[str, Any], soul: str | None, *, actor: str, to_revision: int) -> dict[str, Any]:
        row = self.get_row(agent_id)
        try:
            cfg = AgentConfig(id=agent_id, **{k: v for k, v in changes.items() if v is not None})
        except ValidationError as e:
            msg, fields = _validation_message(e)
            raise HubError(msg, data={"fields": fields}) from None
        self._check_mcp(cfg)
        home = Path(row["home_path"])
        rev = int(row["config_revision"]) + 1
        if soul is not None:
            (home / "SOUL.md").write_text(soul, encoding="utf-8")
        self._write_config_file(home, cfg)
        cur_soul = (home / "SOUL.md").read_text(encoding="utf-8") if (home / "SOUL.md").exists() else None
        with self.store.tx():
            self.store.update_agent(agent_id, {**self._to_row_fields(cfg), "config_revision": rev, "updated_at": now_iso()})
            self.store.add_revision(
                agent_id, rev, {**cfg.model_dump(), "soul_changed": soul is not None, "soul": cur_soul, "reverted_to": to_revision}, actor
            )
        self.publish(make_event("agent.updated", agent_id, {"agent_id": agent_id, "revision": rev, "reverted_to": to_revision, "changed": ["(revert)"]}))
        return self.show(agent_id)

    def restore(self, agent_id: str) -> dict[str, Any]:
        row = self.store.get_agent(agent_id)
        if row is None:
            raise NotFound(f"không tìm thấy agent '{agent_id}'")
        if not row.get("archived_at"):
            raise HubError(f"agent '{agent_id}' không ở trạng thái đã xoá/archive")
        home = Path(row["home_path"])
        trash = Path(row["trash_path"]) if row.get("trash_path") else None
        if trash is not None and trash.exists():
            if home.exists():
                raise Conflict(f"không khôi phục được: {home} đã tồn tại")
            home.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(trash), str(home))
        elif not home.exists():
            raise HubError(f"không tìm thấy dữ liệu agent trong thùng rác ({trash})")
        self.store.update_agent(
            agent_id,
            {"archived_at": None, "deleted_at": None, "purge_after": None, "trash_path": None, "status": "idle", "updated_at": now_iso()},
        )
        self.publish(make_event("agent.restored", agent_id, {"agent_id": agent_id}))
        return self.show(agent_id)

    def set_paused(self, agent_id: str, paused: bool) -> dict[str, Any]:
        row = self.get_row(agent_id)
        if paused:
            status = "paused"
        else:
            status = "working" if self.active_runs(agent_id) else "idle"
            if row["status"] != "paused":
                return self.show(agent_id)
        self.set_status(agent_id, status, current_run_id=row.get("current_run_id"), force=True)
        return self.show(agent_id)

    def set_status(
        self, agent_id: str, status: str, *, current_run_id: str | None = None, reason: str | None = None, force: bool = False
    ) -> None:
        row = self.store.get_agent(agent_id)
        if row is None:
            return
        if not force and row["status"] == "paused" and status in ("idle", "working", "error"):
            # paused là trạng thái do người đặt; vẫn ghi run hiện tại
            self.store.update_agent(agent_id, {"current_run_id": current_run_id})
            return
        if row["status"] == status and row.get("current_run_id") == current_run_id:
            return
        self.store.update_agent(agent_id, {"status": status, "current_run_id": current_run_id, "updated_at": now_iso()})
        self.publish(make_event("agent.status", agent_id, {"status": status, "current_run_id": current_run_id, "reason": reason}))


# ============================================================================ M3: clone / export / import (FR-AG-06)
EXPORT_FORMAT = "cfharness-agent/1"


def _copy_tree(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=False, ignore=shutil.ignore_patterns("__pycache__"))


def clone_agent(svc: AgentService, src: str, dst: str, *, with_memory: bool = False, name: str | None = None) -> dict[str, Any]:
    """Sao chép cấu hình + SOUL + skill (giữ nguyên nội dung/`created_by` trong SKILL.md); memory chỉ khi `with_memory`."""
    row = svc.get_row(src)
    cfg = svc.config_of(row).model_dump()
    cfg["id"] = dst
    cfg["name"] = name or dst
    src_home = Path(row["home_path"])
    soul = (src_home / "SOUL.md").read_text(encoding="utf-8") if (src_home / "SOUL.md").exists() else None
    svc.create(cfg, soul)
    home = svc.paths.agent_home(dst)
    _copy_tree(src_home / "skills", home / "skills")
    if with_memory:
        for f in ("MEMORY.md", "USER.md"):
            if (src_home / f).exists():
                shutil.copy2(src_home / f, home / f)
    svc.store.update_agent(dst, {"cloned_from": src})
    svc.publish(make_event("agent.cloned", dst, {"agent_id": dst, "cloned_from": src, "with_memory": with_memory}))
    return svc.show(dst)


def export_agent(svc: AgentService, agent_id: str, dest: str | Path, *, with_memory: bool = False, scrub: Callable[[str], str] | None = None) -> dict[str, Any]:
    """`.tar.gz` gồm manifest, config.yaml, SOUL.md, skills/ (+ MEMORY/USER nếu `with_memory`). KHÔNG chứa secret:
    cấu hình chỉ có tham chiếu `${NAME}`; mọi file text được lọc qua `scrub` (che giá trị secret đã biết)."""
    import io
    import json
    import tarfile

    row = svc.get_row(agent_id)
    home = Path(row["home_path"])
    dest = Path(dest).expanduser()
    if dest.is_dir():
        dest = dest / f"{agent_id}.agent.tar.gz"
    cfg = svc.config_of(row).model_dump()
    manifest = {"format": EXPORT_FORMAT, "agent_id": agent_id, "exported_at": now_iso(), "with_memory": with_memory,
                "config_revision": row["config_revision"], "cloned_from": row.get("cloned_from")}
    files: list[tuple[str, bytes]] = [
        ("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode()),
        ("config.yaml", yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False).encode()),
    ]
    names = ["SOUL.md"] + (["MEMORY.md", "USER.md"] if with_memory else [])
    for n in names:
        if (home / n).exists():
            files.append((n, (home / n).read_bytes()))
    skills = home / "skills"
    if skills.is_dir():
        for p in sorted(skills.rglob("*")):
            if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts:
                files.append(((Path("skills") / p.relative_to(skills)).as_posix(), p.read_bytes()))  # tên trong tar luôn `/` (export mở được trên OS khác)
    dest.parent.mkdir(parents=True, exist_ok=True)
    scrubbed = 0
    with tarfile.open(dest, "w:gz") as tar:
        for arc, data in files:
            if scrub is not None:
                try:
                    txt = data.decode("utf-8")
                    new = scrub(txt)
                    if new != txt:
                        scrubbed += 1
                    data = new.encode("utf-8")
                except UnicodeDecodeError:
                    pass
            ti = tarfile.TarInfo(name=f"agent/{arc}")
            ti.size = len(data)
            ti.mode = 0o600
            ti.mtime = int(datetime.now(timezone.utc).timestamp())
            tar.addfile(ti, io.BytesIO(data))
    return {"agent_id": agent_id, "path": str(dest), "files": [a for a, _ in files], "with_memory": with_memory, "scrubbed_files": scrubbed}


def import_agent(svc: AgentService, path: str | Path, *, new_id: str | None = None, name: str | None = None) -> dict[str, Any]:
    import json
    import tarfile
    import tempfile

    path = Path(path).expanduser()
    if not path.is_file():
        raise NotFound(f"không có file export: {path}")
    with tarfile.open(path, "r:gz") as tar, tempfile.TemporaryDirectory() as tmp:
        for m in tar.getmembers():
            if not m.isfile() and not m.isdir():
                raise HubError(f"file export chứa mục không hợp lệ (symlink/thiết bị): {m.name}")
            if m.name.startswith("/") or ".." in Path(m.name).parts or not m.name.startswith("agent/"):
                raise HubError(f"file export chứa đường dẫn không an toàn: {m.name}")
        tar.extractall(tmp, filter="data")
        root = Path(tmp) / "agent"
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("format") != EXPORT_FORMAT and manifest.get("format") not in _names.LEGACY_EXPORT_FORMATS:  # gói xuất từ bản tên cũ
            raise HubError(f"định dạng export không hỗ trợ: {manifest.get('format')}")
        cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) or {}
        cfg["id"] = new_id or manifest["agent_id"]
        if name:
            cfg["name"] = name
        soul = (root / "SOUL.md").read_text(encoding="utf-8") if (root / "SOUL.md").exists() else None
        svc.create(cfg, soul)
        home = svc.paths.agent_home(cfg["id"])
        _copy_tree(root / "skills", home / "skills")
        for f in ("MEMORY.md", "USER.md"):
            if (root / f).exists():
                shutil.copy2(root / f, home / f)
    svc.store.update_agent(cfg["id"], {"cloned_from": f"import:{manifest['agent_id']}"})
    svc.publish(make_event("agent.imported", cfg["id"], {"agent_id": cfg["id"], "from": manifest["agent_id"], "path": str(path)}))
    return svc.show(cfg["id"])
