"""Kho tri thức (FR-LN-11 tầng agent; đọc được tầng team/global): memory + skill trên filesystem, chỉ mục trong DB.

Bố cục (BRD §8.2):
  agents/<id>/MEMORY.md, USER.md, skills/<name>/SKILL.md (+references/ templates/ scripts/), .archive/
  teams/<id>/memory.md, skills/          (đọc vào prompt; ghi = L3 → eval gate trước, M5)
  global/USER.md, MEMORY.md, skills/     (đọc vào prompt; ghi = L3)
Memory: các mục ngăn bởi `\\n§\\n` (như Hermes), hạn mức ký tự `learning.limits.*` (2.200 / 1.375). Skill: SKILL.md có
frontmatter YAML `name`, `description` (≤ 60 ký tự, tiếng Anh — Q-14). Chỉ mục `skills` (DB) giữ provenance
`created_by`, `pinned`, `state`, `use_count`, `last_used_at`. Xoá = xoá mềm vào `.archive/` (FR-SG-06).

Module này chỉ ĐỌC và cung cấp phép biến đổi THUẦN (tính nội dung mới). Mọi GHI đi qua hàm áp dụng duy nhất
`learning.ledger.Ledger.apply` (FR-LN-06: một writer, snapshot trước, ledger sau).
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..errors import HubError, NotFound
from ..store import now_iso

if TYPE_CHECKING:
    from ..hub import Hub

ENTRY_DELIMITER = "\n§\n"
SKILL_FILE = "SKILL.md"
SUPPORT_DIRS = ("references", "templates", "scripts", "assets")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
SCOPES = ("agent", "team", "global")
MEMORY_HEADERS = {"memory": "# Memory", "user": "# User profile"}


class KnowledgeError(HubError):
    """Thao tác tri thức không hợp lệ (lý do có mã `reason` để guard/pipeline báo lại)."""

    def __init__(self, message: str, reason: str = "invalid") -> None:
        super().__init__(message, data={"reason": reason})
        self.reason = reason


# ============================================================================ memory (thuần)
def parse_memory(text: str) -> list[str]:
    body = text or ""
    lines = body.split("\n", 1)
    if lines and lines[0].startswith("# "):
        body = lines[1] if len(lines) > 1 else ""
    return [e.strip() for e in ("\n" + body + "\n").split(ENTRY_DELIMITER) if e.strip()] if body.strip() else []


def render_memory(entries: list[str], store: str = "memory") -> str:
    head = MEMORY_HEADERS.get(store, "# Memory")
    if not entries:
        return head + "\n"
    return head + "\n" + ENTRY_DELIMITER.join(e.strip() for e in entries) + "\n"


def memory_chars(entries: list[str]) -> int:
    return len(ENTRY_DELIMITER.join(entries))


def apply_memory_op(entries: list[str], action: str, content: str | None, old_text: str | None) -> list[str]:
    """Phép add/patch/remove trên danh sách mục (ngữ nghĩa tool memory của Hermes). Sai → KnowledgeError."""
    out = list(entries)
    content = (content or "").strip()
    if action == "add":
        if not content:
            raise KnowledgeError("add cần content", "invalid")
        if ENTRY_DELIMITER.strip() in content:
            raise KnowledgeError("một mục memory không được chứa ký tự phân cách '§'", "invalid")
        if any(content == e for e in out):
            raise KnowledgeError("mục memory này đã có (một fact – một kho)", "duplicate")
        out.append(content)
        return out
    if not old_text:
        raise KnowledgeError(f"{action} cần old_text", "invalid")
    hits = [i for i, e in enumerate(out) if old_text in e]
    if not hits:
        raise KnowledgeError("old_text không khớp mục memory nào", "not_found")
    if len(hits) > 1:
        raise KnowledgeError("old_text khớp nhiều mục — trích dài hơn cho duy nhất", "ambiguous")
    i = hits[0]
    if action == "remove":
        out.pop(i)
        return out
    if action == "patch":
        if not content:
            raise KnowledgeError("patch cần content", "invalid")
        if ENTRY_DELIMITER.strip() in content:
            raise KnowledgeError("một mục memory không được chứa ký tự phân cách '§'", "invalid")
        if out[i].count(old_text) != 1:
            raise KnowledgeError("old_text phải xuất hiện đúng một lần trong mục", "ambiguous")
        out[i] = out[i].replace(old_text, content)
        return out
    raise KnowledgeError(f"action memory không hợp lệ: {action}", "invalid")


MAX_MEMORY_OPS = 10


def apply_memory_ops(entries: list[str], ops: list[dict[str, Any]]) -> list[str]:
    """Lô thao tác add/patch/remove áp tuần tự (như `operations` của tool memory Hermes): hạn mức ký tự xét trên KẾT QUẢ
    cuối nên một lô vừa gộp/xoá mục cũ vừa thêm mục mới được khi kho đã đầy. Một thao tác sai → cả lô sai (KnowledgeError)."""
    if not ops:
        raise KnowledgeError("operations rỗng", "invalid")
    if len(ops) > MAX_MEMORY_OPS:
        raise KnowledgeError(f"tối đa {MAX_MEMORY_OPS} thao tác mỗi lô", "invalid")
    out = list(entries)
    for n, op in enumerate(ops, 1):
        if not isinstance(op, dict):
            raise KnowledgeError(f"thao tác #{n} phải là object", "invalid")
        try:
            out = apply_memory_op(out, str(op.get("action") or ""), op.get("content"), op.get("old_text"))
        except KnowledgeError as e:
            raise KnowledgeError(f"thao tác #{n} ({op.get('action')}): {e.message}", e.reason) from None
    return out


# ============================================================================ skill (thuần)
def parse_skill(text: str) -> tuple[dict[str, Any], str]:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end > 0:
            try:
                fm = yaml.safe_load(text[4:end]) or {}
            except yaml.YAMLError:
                fm = {}
            body = text[end + 4 :].lstrip("\n")
            return (fm if isinstance(fm, dict) else {}), body
    return {}, text


def render_skill(name: str, description: str, body: str, extra: dict[str, Any] | None = None) -> str:
    fm = {"name": name, "description": description, **(extra or {})}
    return "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip() + "\n---\n\n" + body.strip() + "\n"


def check_skill_name(name: str) -> None:
    if not name or not NAME_RE.match(name):
        raise KnowledgeError(f"tên skill không hợp lệ '{name}': chữ thường, số, '-', 2–63 ký tự", "bad_name")
    # FR-LN-07 mục 6: tên ở cấp LOẠI VIỆC — cấm số PR/issue, ngày, tiền tố phiên
    if re.search(r"(^|-)(pr|issue|ticket|bug)-?\d+", name) or re.search(r"\d{3,}", name) or re.search(r"(19|20)\d\d", name):
        raise KnowledgeError(f"tên skill '{name}' quá hẹp (số PR/issue/ngày) — đặt tên theo loại việc", "narrow_name")
    if re.match(r"^(fix|debug|audit|hotfix|todo|tmp|test)-", name) or name.endswith(("-today", "-tmp")):
        raise KnowledgeError(f"tên skill '{name}' là tên phiên làm việc — đặt tên theo loại việc", "narrow_name")


def check_rel_path(rel: str) -> str:
    p = Path(rel)
    if not rel or p.is_absolute() or ".." in p.parts or rel.startswith("."):
        raise KnowledgeError(f"đường dẫn file skill không hợp lệ: {rel!r}", "path_escape")
    if rel != SKILL_FILE and (len(p.parts) < 2 or p.parts[0] not in SUPPORT_DIRS):
        raise KnowledgeError(f"file hỗ trợ phải nằm dưới {'/'.join(SUPPORT_DIRS)}/: {rel!r}", "path_escape")
    return rel


# ============================================================================ store
class KnowledgeStore:
    """Đọc tri thức + chỉ mục skill. `hub.knowledge`."""

    def __init__(self, hub: "Hub") -> None:
        from .tiers import TierVersions

        self.hub = hub
        self.versions = TierVersions(self)  # M5-A: phiên bản tầng team/global theo agent (canary)

    @property
    def conn(self):
        return self.hub.store.conn

    def limits(self) -> dict[str, int]:
        lim = self.hub.config.get("learning.limits")
        return {k: int(v) for k, v in lim.items()}

    # ------------------------------------------------------------------ đường dẫn
    def tier_dir(self, scope: str, owner: str | None) -> Path:
        home = self.hub.paths.home
        if scope == "agent":
            row = self.hub.store.get_agent(owner or "")
            if row is None:
                raise NotFound(f"không tìm thấy agent '{owner}'")
            return Path(row["home_path"])
        if scope == "team":
            if not owner:
                raise HubError("scope team cần team id")
            return home / "teams" / owner
        if scope == "global":
            return home / "global"
        raise HubError(f"scope không hợp lệ: {scope}")

    def memory_path(self, scope: str, owner: str | None, store: str = "memory") -> Path:
        d = self.tier_dir(scope, owner)
        if scope == "team":
            return d / "memory.md"
        return d / ("USER.md" if store == "user" else "MEMORY.md")

    def memory_limit(self, scope: str, store: str) -> int:
        lim = self.limits()
        if store == "user":
            return lim["user_chars"]
        return lim["team_memory_chars"] if scope == "team" else lim["memory_chars"]

    def skills_dir(self, scope: str, owner: str | None) -> Path:
        return self.tier_dir(scope, owner) / "skills"

    def skill_dir(self, scope: str, owner: str | None, name: str) -> Path:
        return self.skills_dir(scope, owner) / name

    def archive_dir(self, scope: str, owner: str | None) -> Path:
        return self.tier_dir(scope, owner) / ".archive" / "skills"

    # ------------------------------------------------------------------ memory
    def read_memory(self, scope: str, owner: str | None, store: str = "memory") -> list[str]:
        p = self.memory_path(scope, owner, store)
        return parse_memory(p.read_text(encoding="utf-8")) if p.exists() else []

    def memory_view(self, agent_id: str, store: str = "memory") -> dict[str, Any]:
        entries = self.read_memory("agent", agent_id, store)
        lim = self.memory_limit("agent", store)
        return {"target": store, "entries": entries, "chars": memory_chars(entries), "limit": lim}

    # ------------------------------------------------------------------ skill index (DB)
    def _row(self, scope: str, owner: str, name: str) -> dict[str, Any] | None:
        r = self.conn.execute("SELECT * FROM skills WHERE scope=? AND owner_id=? AND name=?", (scope, owner, name)).fetchone()
        return dict(r) if r else None

    def _owner_key(self, scope: str, owner: str | None) -> str:
        return owner or "" if scope != "global" else "global"

    def sync_index(self, scope: str, owner: str | None) -> None:
        """Đăng ký skill có trên đĩa mà chưa có trong chỉ mục (người tạo tay → `created_by=user`)."""
        try:
            d = self.skills_dir(scope, owner)
        except (NotFound, HubError):
            return
        if not d.is_dir():
            return
        okey = self._owner_key(scope, owner)
        for sd in sorted(d.iterdir()):
            f = sd / SKILL_FILE
            if not sd.is_dir() or not f.exists():
                continue
            fm, _ = parse_skill(f.read_text(encoding="utf-8", errors="replace"))
            desc = str(fm.get("description") or "")[:200]
            row = self._row(scope, okey, sd.name)
            if row is None:
                now = now_iso()
                self.conn.execute(
                    "INSERT INTO skills (scope, owner_id, name, description, path, created_by, pinned, state, use_count, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,0,'active',0,?,?)",
                    (scope, okey, sd.name, desc, str(sd), "user", now, now),
                )
            elif row["state"] in ("archived", "deleted") or row["description"] != desc:
                self.conn.execute("UPDATE skills SET description=?, path=?, state=CASE WHEN state IN ('archived','deleted') THEN 'active' ELSE state END, "
                                  "deleted_at=NULL WHERE scope=? AND owner_id=? AND name=?", (desc, str(sd), scope, okey, sd.name))

    def register_skill(self, scope: str, owner: str | None, name: str, *, created_by: str, description: str = "") -> None:
        okey = self._owner_key(scope, owner)
        now = now_iso()
        path = str(self.skill_dir(scope, owner, name))
        if self._row(scope, okey, name) is None:
            self.conn.execute(
                "INSERT INTO skills (scope, owner_id, name, description, path, created_by, pinned, state, use_count, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,0,'active',0,?,?)",
                (scope, okey, name, description[:200], path, created_by, now, now),
            )
        else:
            self.conn.execute("UPDATE skills SET description=?, path=?, state='active', deleted_at=NULL, purge_after=NULL, updated_at=? "
                              "WHERE scope=? AND owner_id=? AND name=?", (description[:200], path, now, scope, okey, name))

    def mark_archived(self, scope: str, owner: str | None, name: str, archived_path: str | None) -> None:
        from ..store import purge_after

        okey = self._owner_key(scope, owner)
        days = int(self.hub.config.get("retention.soft_delete_days") or 30)
        self.conn.execute("UPDATE skills SET state='archived', deleted_at=?, purge_after=?, archived_path=?, updated_at=? WHERE scope=? AND owner_id=? AND name=?",
                          (now_iso(), purge_after(days), archived_path, now_iso(), scope, okey, name))

    def set_state(self, scope: str, owner: str | None, name: str, state: str) -> None:
        if state not in ("active", "stale", "archived", "deleted"):
            raise HubError(f"state skill không hợp lệ: {state}")
        self.conn.execute("UPDATE skills SET state=?, updated_at=? WHERE scope=? AND owner_id=? AND name=?",
                          (state, now_iso(), scope, self._owner_key(scope, owner), name))

    def touch_skill(self, scope: str, owner: str | None, name: str) -> None:
        self.conn.execute("UPDATE skills SET use_count=use_count+1, last_used_at=?, state=CASE WHEN state='stale' THEN 'active' ELSE state END "
                          "WHERE scope=? AND owner_id=? AND name=?", (now_iso(), scope, self._owner_key(scope, owner), name))

    def list_skills(self, scope: str, owner: str | None, *, include_archived: bool = False) -> list[dict[str, Any]]:
        self.sync_index(scope, owner)
        okey = self._owner_key(scope, owner)
        sql = "SELECT * FROM skills WHERE scope=? AND owner_id=?" + ("" if include_archived else " AND state NOT IN ('archived','deleted')") + " ORDER BY name"
        out = []
        for r in self.conn.execute(sql, (scope, okey)):
            d = dict(r)
            d["pinned"] = bool(d["pinned"]) or self.is_pinned(scope, owner, "skill", d["name"])
            if d["state"] not in ("archived", "deleted") and not (Path(d["path"]) / SKILL_FILE).exists():
                continue  # thư mục đã mất ngoài hub → bỏ khỏi chỉ mục hiển thị
            out.append(d)
        return out

    def agent_tiers(self, agent_id: str) -> list[tuple[str, str | None]]:
        """Thứ tự tầng cho agent: agent → team(s) → global (tầng hẹp đứng trước; che khuất cùng tên)."""
        tiers: list[tuple[str, str | None]] = [("agent", agent_id)]
        try:
            for r in self.conn.execute("SELECT m.team_id FROM team_members m JOIN teams t ON t.id=m.team_id WHERE m.agent_id=? AND t.archived_at IS NULL ORDER BY m.team_id", (agent_id,)):
                tiers.append(("team", r[0]))
        except Exception:  # pragma: no cover - DB cũ
            pass
        tiers.append(("global", None))
        return tiers

    def effective_skills(self, agent_id: str, home: Path | None = None) -> list[dict[str, Any]]:
        """Chỉ mục skill hiệu lực của agent (FR-LN-11): tầng agent che team che global; ghi chú `shadows`.
        `home` ≠ None: tầng agent đọc thẳng thư mục `home/skills` (bản sao home của run eval)."""
        seen: dict[str, dict[str, Any]] = {}
        for scope, owner in self.agent_tiers(agent_id):
            if scope == "agent":
                rows = scan_skills_dir(home / "skills") if home is not None else self.list_skills(scope, owner)
            else:  # M5-A: phiên bản gán cho agent (ghim khi canary) + ứng viên eval trong bản sao home
                rows = self.versions.tier_skills(agent_id, scope, owner, home=home)
            for s in rows:
                if s["name"] in seen:
                    seen[s["name"]].setdefault("shadows", []).append(f"{scope}:{owner or 'global'}")
                    continue
                seen[s["name"]] = {**s, "scope": scope, "owner": owner}
        return list(seen.values())

    def find_skill(self, agent_id: str, name: str, home: Path | None = None) -> dict[str, Any] | None:
        for s in self.effective_skills(agent_id, home=home):
            if s["name"] == name:
                return s
        return None

    def skill_view(self, agent_id: str, name: str, file_path: str | None = None, *, touch: bool = True, home: Path | None = None) -> dict[str, Any]:
        s = self.find_skill(agent_id, name, home=home)
        if s is None:
            raise NotFound(f"skill '{name}' không tồn tại (skills_list để xem danh sách)")
        root = Path(s["path"])
        rel = check_rel_path(file_path) if file_path else SKILL_FILE
        f = root / rel
        if not f.exists():
            raise NotFound(f"skill '{name}' không có file '{rel}'")
        files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.name != SKILL_FILE)  # "/" mọi OS
        if touch and rel == SKILL_FILE and home is None:
            self.touch_skill(s["scope"], s["owner"], name)
        return {"name": name, "scope": s["scope"], "file_path": rel, "content": f.read_text(encoding="utf-8", errors="replace"), "support_files": files}

    # ------------------------------------------------------------------ pin (người đặt — FR-LN-05)
    def is_pinned(self, scope: str, owner: str | None, target: str, ref: str) -> bool:
        r = self.conn.execute("SELECT 1 FROM knowledge_pins WHERE scope=? AND owner_id=? AND target=? AND ref=?",
                              (scope, self._owner_key(scope, owner), target, ref)).fetchone()
        return r is not None

    def set_pin(self, scope: str, owner: str | None, target: str, ref: str, on: bool, by: str = "user") -> dict[str, Any]:
        okey = self._owner_key(scope, owner)
        if on:
            self.conn.execute("INSERT OR IGNORE INTO knowledge_pins (scope, owner_id, target, ref, created_by, created_at) VALUES (?,?,?,?,?,?)",
                              (scope, okey, target, ref, by, now_iso()))
            if target == "skill":
                self.conn.execute("UPDATE skills SET pinned=1 WHERE scope=? AND owner_id=? AND name=?", (scope, okey, ref))
        else:
            self.conn.execute("DELETE FROM knowledge_pins WHERE scope=? AND owner_id=? AND target=? AND ref=?", (scope, okey, target, ref))
            if target == "skill":
                self.conn.execute("UPDATE skills SET pinned=0 WHERE scope=? AND owner_id=? AND name=?", (scope, okey, ref))
        return {"scope": scope, "owner": okey, "target": target, "ref": ref, "pinned": on}

    def pins(self, scope: str | None = None, owner: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM knowledge_pins", []
        if scope:
            sql += " WHERE scope=? AND owner_id=?"
            args = [scope, self._owner_key(scope, owner)]
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY created_at", args)]

    # ------------------------------------------------------------------ tổng quan
    def overview(self, agent_id: str) -> dict[str, Any]:
        self.hub.agents.get_row(agent_id)
        mem = self.memory_view(agent_id, "memory")
        usr = self.memory_view(agent_id, "user")
        return {
            "agent_id": agent_id,
            "memory": {"chars": mem["chars"], "limit": mem["limit"], "entries": len(mem["entries"])},
            "user": {"chars": usr["chars"], "limit": usr["limit"], "entries": len(usr["entries"])},
            "skills": [{k: s.get(k) for k in ("name", "description", "scope", "owner", "created_by", "pinned", "state", "use_count", "last_used_at", "shadows")}
                       for s in self.effective_skills(agent_id)],
            "archived": [{k: s.get(k) for k in ("name", "archived_path", "deleted_at", "purge_after")}
                         for s in self.list_skills("agent", agent_id, include_archived=True) if s["state"] in ("archived", "deleted")],
            "pins": self.pins("agent", agent_id),
        }


def scan_skills_dir(d: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not d.is_dir():
        return out
    for sd in sorted(d.iterdir()):
        f = sd / SKILL_FILE
        if sd.is_dir() and f.exists():
            fm, _ = parse_skill(f.read_text(encoding="utf-8", errors="replace"))
            out.append({"name": sd.name, "description": str(fm.get("description") or ""), "path": str(sd), "state": "active", "created_by": None, "pinned": False})
    return out


# ============================================================================ thao tác file cấp thấp (chỉ ledger gọi)
def move_to_archive(src: Path, archive_root: Path) -> Path:
    archive_root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    dest = archive_root / f"{src.name}__{ts}"
    shutil.move(str(src), str(dest))
    return dest
