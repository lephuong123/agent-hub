"""RPC quản lý skill cho NGƯỜI (`knowledge.skill_*`) — nền cho CLI và trang web skill manager.

Đọc: `knowledge.skill_list`, `knowledge.skill_get`. Ghi: `knowledge.skill_save|skill_archive|skill_restore|skill_import|skill_promote`.
Mọi GHI đi qua pipeline học (`hub.learning.pipeline.propose`, `created_by="user"`) — cùng đường với `learn.propose`, nên guard
(quét nội dung, pin, hạn mức), ledger (snapshot + entry) và rollback vẫn áp dụng; khôi phục = `Ledger.rollback` của entry archive.
Tầng team/global: pipeline tự xử lý L3 (thay đổi của NGƯỜI áp dụng ngay + canary mode `watch`, không eval trước — quyết định
hardening trong `pipeline._continue_tier`). Các method ghi là HUMAN_ONLY (auth.py) + nằm trong MUTATING (bọc audit).
Hợp đồng chi tiết: `docs/dev-notes/skill-manager.md`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import Conflict, HubError, NotFound
from ..knowledge.store import SKILL_FILE, KnowledgeError, check_rel_path, check_skill_name, parse_skill, render_skill

if TYPE_CHECKING:
    from ..daemon import Daemon

IMPORT_DIRS = ("references", "templates", "scripts")
MAX_SUPPORT_FILES = 100
ARCHIVE_OPS = ("archive", "merge", "delete")
ROW_KEYS = ("name", "description", "scope", "owner", "created_by", "state", "use_count", "last_used_at", "pinned")
ARCHIVED_KEYS = ("deleted_at", "archived_path", "purge_after")


def _req(p: dict[str, Any], key: str) -> Any:
    if p.get(key) is None or p.get(key) == "":
        raise HubError(f"thiếu tham số '{key}'", code=-32602)
    return p[key]


class SkillManageRpc:
    def __init__(self, daemon: "Daemon") -> None:
        self.hub = daemon.hub

    @property
    def K(self):
        return self.hub.knowledge

    @property
    def P(self):
        return self.hub.learning.pipeline

    # ------------------------------------------------------------------ tiện ích
    def _tier(self, p: dict[str, Any]) -> tuple[str, str | None, str | None]:
        """(scope, owner, agent) từ {agent?, scope?, team?}. scope mặc định agent khi có agent. owner: agent id | team id | None (global)."""
        agent = p.get("agent") or None
        scope = p.get("scope") or ("agent" if agent else None)
        if scope not in ("agent", "team", "global"):
            raise HubError("cần agent, hoặc scope team|global (scope: agent|team|global)", code=-32602)
        if agent:
            self.hub.agents.get_row(agent)
        if scope == "agent":
            if not agent:
                raise HubError("scope agent cần tham số 'agent'", code=-32602)
            return scope, agent, agent
        if scope == "team":
            team = p.get("team")
            if team:
                self.hub.teams.row(team)
            else:
                team = self.P._resolve_team(agent, None)  # team DUY NHẤT của agent
            return scope, team, agent
        return scope, None, agent

    def _exists(self, scope: str, owner: str | None, name: str) -> bool:
        return (self.K.skill_dir(scope, owner, name) / SKILL_FILE).exists()

    def _desc_limit(self) -> int:
        return int(self.K.limits()["skill_desc_chars"])

    def _check_desc(self, desc: str) -> str:
        desc = " ".join(str(desc or "").split())
        if not desc:
            raise KnowledgeError("skill cần description (một dòng tiếng Anh)", "invalid")
        if len(desc) > self._desc_limit():
            raise KnowledgeError(f"description {len(desc)} > {self._desc_limit()} ký tự (learning.limits.skill_desc_chars)", "description_limit")
        return desc

    def _restore_entry(self, scope: str, owner: str | None, name: str) -> str | None:
        """Ledger entry archive/merge/delete MỚI NHẤT của skill chưa bị rollback — rollback nó = khôi phục."""
        r = self.hub.store.conn.execute(
            "SELECT id FROM ledger_entries WHERE scope=? AND IFNULL(owner_id,'')=IFNULL(?,'') AND target='skill' AND target_ref=? "
            f"AND op IN ({','.join('?' * len(ARCHIVE_OPS))}) AND rolled_back_by IS NULL ORDER BY applied_at DESC, rowid DESC LIMIT 1",
            (scope, owner, name, *ARCHIVE_OPS)).fetchone()
        return r[0] if r else None

    def _row(self, s: dict[str, Any], scope: str, owner: str | None) -> dict[str, Any]:
        out = {k: s.get(k) for k in ROW_KEYS}
        out["scope"] = s.get("scope") or scope
        out["owner"] = s["owner"] if "owner" in s else owner  # dòng DB có owner_id ("global" cho tầng global) — trả id thật / None
        out["pinned"] = bool(out["pinned"])
        out["use_count"] = int(out["use_count"] or 0)
        if s.get("shadows"):
            out["shadows"] = s["shadows"]
        if s.get("state") in ("archived", "deleted"):
            out.update({k: s.get(k) for k in ARCHIVED_KEYS})
            out["restore_entry_id"] = self._restore_entry(out["scope"], out["owner"], out["name"])
        return out

    def _tier_rows(self, scope: str, owner: str | None, include_archived: bool) -> list[dict[str, Any]]:
        return [self._row(s, scope, owner) for s in self.K.list_skills(scope, owner, include_archived=include_archived)]

    def _support_files(self, root: Path) -> list[str]:
        if not root.is_dir():
            return []
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.name != SKILL_FILE)

    def _propose(self, *, op: str, scope: str, owner: str | None, agent: str | None, payload: dict[str, Any], rationale: str) -> dict[str, Any]:
        res = self.P.propose(target="skill", op=op, payload=payload, agent_id=agent if scope == "agent" else None,
                             scope=scope, team_id=owner if scope == "team" else None, target_ref=payload.get("name"), rationale=rationale, created_by="user")
        return {**res, "scope": scope, "owner": owner, "name": payload.get("name")}

    # ------------------------------------------------------------------ 1. list
    def skill_list(self, p: dict[str, Any]) -> dict[str, Any]:
        inc = bool(p.get("include_archived"))
        if p.get("agent") and not p.get("scope"):
            agent = p["agent"]
            self.hub.agents.get_row(agent)
            rows = [self._row(s, s.get("scope") or "agent", s.get("owner")) for s in self.K.effective_skills(agent)]
            if inc:
                rows += [r for r in self._tier_rows("agent", agent, True) if r["state"] in ("archived", "deleted")]
            return {"mode": "effective", "agent": agent, "skills": rows}
        if p.get("scope"):
            scope, owner, _ = self._tier(p)
            return {"mode": "tier", "scope": scope, "owner": owner, "skills": self._tier_rows(scope, owner, inc)}
        rows: list[dict[str, Any]] = []
        for a in self.hub.store.list_agents():
            rows += self._tier_rows("agent", a["id"], inc)
        for t in self.hub.teams.list():
            rows += self._tier_rows("team", t["id"], inc)
        rows += self._tier_rows("global", None, inc)
        return {"mode": "all", "skills": rows}

    # ------------------------------------------------------------------ 2. get
    def _locate(self, p: dict[str, Any], name: str) -> tuple[str, str | None, Path, dict[str, Any] | None]:
        if p.get("agent") and not p.get("scope"):
            s = self.K.find_skill(p["agent"], name)
            if s is None:
                raise NotFound(f"agent '{p['agent']}' không có skill '{name}' (knowledge.skill_list; skill đã archive: include_archived=true)")
            return s.get("scope") or "agent", s.get("owner"), Path(s["path"]), s
        scope, owner, _ = self._tier(p)
        if not self._exists(scope, owner, name):
            raise NotFound(f"skill '{name}' không có ở tầng {scope}{':' + owner if owner else ''} (skill đã archive: knowledge.skill_list include_archived=true)")
        return scope, owner, self.K.skill_dir(scope, owner, name), None

    def skill_get(self, p: dict[str, Any]) -> dict[str, Any]:
        name = str(_req(p, "name"))
        scope, owner, root, eff = self._locate(p, name)
        text = (root / SKILL_FILE).read_text(encoding="utf-8", errors="replace")
        fm, body = parse_skill(text)
        prov = self.K._row(scope, self.K._owner_key(scope, owner), name)
        if prov is not None:
            prov["pinned"] = bool(prov.get("pinned")) or self.K.is_pinned(scope, owner, "skill", name)
        ledger = [e for e in self.hub.learning.ledger.list(target="skill", target_ref=name, scope=scope, limit=200)
                  if (e.get("owner_id") or None) == (owner or None)][: int(p.get("ledger_limit") or 20)]
        out = {"name": name, "scope": scope, "owner": owner, "path": str(root), "frontmatter": fm, "body": body, "content": text,
               "support_files": self._support_files(root), "provenance": prov,
               "ledger": [{k: e.get(k) for k in ("id", "op", "actor", "applied_at", "summary", "reason", "proposal_id", "rolled_back_by", "rollback_of", "risk_class")}
                          for e in ledger]}
        if eff is not None and eff.get("shadows"):
            out["shadows"] = eff["shadows"]
        if p.get("file"):
            rel = check_rel_path(str(p["file"]))
            f = root / rel
            if not f.is_file():
                raise NotFound(f"skill '{name}' không có file '{rel}'")
            out["file"] = {"path": rel, "content": f.read_text(encoding="utf-8", errors="replace")}
        return out

    # ------------------------------------------------------------------ 3. save
    def _compose(self, name: str, p: dict[str, Any], current: str | None) -> str:
        """SKILL.md đầy đủ từ `content` (có/không frontmatter) hoặc description+body; giữ khoá frontmatter phụ."""
        cur_fm = parse_skill(current)[0] if current else {}
        if p.get("content") is not None:
            text = str(p["content"])
            if text.startswith("---\n"):
                fm, body = parse_skill(text)
                if fm.get("name") not in (None, name):
                    raise KnowledgeError(f"frontmatter name '{fm.get('name')}' khác tên skill '{name}'", "invalid")
            else:
                fm, body = dict(cur_fm), text
            desc = p.get("description") or fm.get("description") or cur_fm.get("description")
        else:
            fm = dict(cur_fm)
            body = p.get("body")
            if body is None:
                if current is None:
                    raise HubError("cần 'content' (cả SKILL.md) hoặc 'description' + 'body'", code=-32602)
                body = parse_skill(current)[1]
            desc = p.get("description") or cur_fm.get("description")
        if not str(body or "").strip():
            raise KnowledgeError("SKILL.md cần phần thân (body)", "invalid")
        extra = {k: v for k, v in fm.items() if k not in ("name", "description")}
        return render_skill(name, self._check_desc(str(desc or "")), str(body), extra)

    def skill_save(self, p: dict[str, Any]) -> dict[str, Any]:
        name = str(_req(p, "name"))
        check_skill_name(name)
        scope, owner, agent = self._tier(p)
        exists = self._exists(scope, owner, name)
        rationale = str(p.get("rationale") or "")[:2000] or "edited by user (skill manager)"
        rel = str(p.get("file") or SKILL_FILE)
        if rel != SKILL_FILE:  # file hỗ trợ (references/ templates/ scripts/): tạo hoặc thay cả file
            check_rel_path(rel)
            if not exists:
                raise NotFound(f"skill '{name}' chưa có ở tầng {scope} — tạo SKILL.md trước")
            content = p.get("content")
            if content is None:
                raise HubError("lưu file hỗ trợ cần 'content'", code=-32602)
            f = self.K.skill_dir(scope, owner, name) / rel
            if f.is_file() and f.read_text(encoding="utf-8", errors="replace") == str(content):
                return {"status": "unchanged", "name": name, "scope": scope, "owner": owner, "created": False, "file": rel}
            res = self._propose(op="patch", scope=scope, owner=owner, agent=agent, payload={"name": name, "file_path": rel, "content": str(content)}, rationale=rationale)
            return {**res, "created": False, "file": rel}
        cur_path = self.K.skill_dir(scope, owner, name) / SKILL_FILE
        current = cur_path.read_text(encoding="utf-8", errors="replace") if exists else None
        text = self._compose(name, p, current)
        if current is not None and current == text:
            return {"status": "unchanged", "name": name, "scope": scope, "owner": owner, "created": False, "file": SKILL_FILE}
        if exists:
            payload = {"name": name, "file_path": SKILL_FILE, "content": text}
            res = self._propose(op="patch", scope=scope, owner=owner, agent=agent, payload=payload, rationale=rationale)
        else:
            res = self._propose(op="add", scope=scope, owner=owner, agent=agent, payload={"name": name, "content": text}, rationale=rationale)
        return {**res, "created": not exists, "file": SKILL_FILE}

    # ------------------------------------------------------------------ 4. archive / 5. restore
    def skill_archive(self, p: dict[str, Any]) -> dict[str, Any]:
        name = str(_req(p, "name"))
        scope, owner, agent = self._tier(p)
        if not self._exists(scope, owner, name):
            raise NotFound(f"skill '{name}' không có ở tầng {scope}{':' + owner if owner else ''}")
        why = str(p.get("rationale") or "archived by user (skill manager)")[:2000]
        # reason bắt đầu bằng "user" → miễn luật absorbed_into của guard (người xoá mềm, không gộp)
        return self._propose(op="archive", scope=scope, owner=owner, agent=agent, payload={"name": name, "reason": f"user: {why}"}, rationale=why)

    def skill_restore(self, p: dict[str, Any]) -> dict[str, Any]:
        name = str(_req(p, "name"))
        scope, owner, _ = self._tier(p)
        eid = self._restore_entry(scope, owner, name)
        if eid is None:
            raise NotFound(f"không tìm thấy lần archive nào còn khôi phục được của skill '{name}' ở tầng {scope}{':' + owner if owner else ''} — "
                           "xem `cfharness learn ledger --target skill` (skill đã purge hoặc đã khôi phục rồi)")
        if self._exists(scope, owner, name):
            raise Conflict(f"skill '{name}' đang tồn tại ở tầng {scope} — archive bản hiện tại trước khi khôi phục bản cũ")
        entry = self.hub.learning.ledger.rollback(eid, "user", str(p.get("rationale") or "restored by user (skill manager)"))
        return {"status": "restored", "name": name, "scope": scope, "owner": owner, "restored_entry_id": eid, "ledger_entry_id": entry["id"]}

    # ------------------------------------------------------------------ 6. import
    def _read_dir(self, root: Path, lim: int) -> tuple[dict[str, str], list[dict[str, str]]]:
        files: dict[str, str] = {}
        skipped: list[dict[str, str]] = []
        base = root.resolve()
        for top in sorted(root.iterdir()):
            if top.name == SKILL_FILE:
                continue
            if top.name not in IMPORT_DIRS or not top.is_dir():
                skipped.append({"path": top.name, "reason": f"chỉ nhập SKILL.md + {'/'.join(IMPORT_DIRS)}/"})
                continue
            for f in sorted(top.rglob("*")):
                rel = f.relative_to(root).as_posix()
                if f.is_symlink() or not f.resolve().is_relative_to(base):
                    raise KnowledgeError(f"'{rel}' trỏ ra ngoài thư mục skill (symlink/đường dẫn thoát)", "path_escape")
                if not f.is_file():
                    continue
                if any(part.startswith(".") for part in Path(rel).parts):
                    skipped.append({"path": rel, "reason": "file ẩn"})
                    continue
                check_rel_path(rel)
                try:
                    c = f.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    skipped.append({"path": rel, "reason": "không phải văn bản UTF-8"})
                    continue
                if len(c) > lim:
                    raise KnowledgeError(f"file '{rel}' {len(c)} > {lim} ký tự (learning.limits.skill_chars)", "skill_limit")
                files[rel] = c
                if len(files) > MAX_SUPPORT_FILES:
                    raise KnowledgeError(f"quá {MAX_SUPPORT_FILES} file hỗ trợ", "skill_limit")
        return files, skipped

    def skill_import(self, p: dict[str, Any]) -> dict[str, Any]:
        src = Path(str(_req(p, "path"))).expanduser()
        if not src.exists():
            raise NotFound(f"không thấy '{src}'")
        if src.is_dir():
            root, md = src, src / SKILL_FILE
            if not md.is_file():
                raise NotFound(f"thư mục '{src}' không có {SKILL_FILE}")
        else:
            md = src
            root = src.parent if src.name == SKILL_FILE else None
        lim = int(self.K.limits()["skill_chars"])
        text = md.read_text(encoding="utf-8", errors="replace")
        if len(text) > lim:
            raise KnowledgeError(f"{SKILL_FILE} {len(text)} > {lim} ký tự (learning.limits.skill_chars) — đẩy chiều sâu vào references/", "skill_limit")
        fm, body = parse_skill(text)
        name = str(p.get("name") or fm.get("name") or (root.name if root is not None else "")).strip()
        check_skill_name(name)
        if not body.strip():
            raise KnowledgeError(f"{SKILL_FILE} không có phần thân", "invalid")
        extra = {k: v for k, v in fm.items() if k not in ("name", "description")}
        content = render_skill(name, self._check_desc(str(p.get("description") or fm.get("description") or "")), body, extra)
        files, skipped = self._read_dir(root, lim) if root is not None else ({}, [])
        scope, owner, agent = self._tier(p)
        exists = self._exists(scope, owner, name)
        if exists and not p.get("overwrite"):
            raise Conflict(f"skill '{name}' đã có ở tầng {scope}{':' + owner if owner else ''} — dùng overwrite=true để thay, hoặc name=<tên khác>")
        why = str(p.get("rationale") or f"imported by user from {src}")[:2000]
        if exists:
            payload = {"name": name, "file_path": SKILL_FILE, "content": content, "files": files, "replace_tree": True}
            res = self._propose(op="patch", scope=scope, owner=owner, agent=agent, payload=payload, rationale=why)
        else:
            res = self._propose(op="add", scope=scope, owner=owner, agent=agent, payload={"name": name, "content": content, "files": files}, rationale=why)
        return {**res, "created": not exists, "imported_files": [SKILL_FILE, *sorted(files)], "skipped": skipped}

    # ------------------------------------------------------------------ 7. promote
    def skill_promote(self, p: dict[str, Any]) -> dict[str, Any]:
        """Chép skill tầng agent lên team/global bằng proposal `op=add` ở tầng đích (pipeline L3). Không dùng đường curator
        (`curation.submit_team_proposal`): đường đó gắn cụm/curator (`created_by=curator`, eval trước + canary, tự archive bản agent)."""
        agent = str(_req(p, "agent"))
        name = str(_req(p, "name"))
        to = _req(p, "to")
        if to not in ("team", "global"):
            raise HubError("to: team | global", code=-32602)
        self.hub.agents.get_row(agent)
        src = self.K.skill_dir("agent", agent, name)
        if not (src / SKILL_FILE).is_file():
            raise NotFound(f"agent '{agent}' không có skill '{name}' ở tầng agent")
        scope, owner, _ = self._tier({"agent": agent, "scope": to, "team": p.get("team")})
        if self._exists(scope, owner, name):
            raise Conflict(f"tầng {scope}{':' + owner if owner else ''} đã có skill '{name}' — sửa trực tiếp bằng knowledge.skill_save")
        text = (src / SKILL_FILE).read_text(encoding="utf-8", errors="replace")
        files, skipped = self._read_dir(src, int(self.K.limits()["skill_chars"]))
        why = str(p.get("rationale") or f"promoted by user from agent {agent}")[:2000]
        res = self.P.propose(target="skill", op="add", payload={"name": name, "content": text, "files": files}, agent_id=agent if scope == "team" and self.hub.teams.member(owner, agent) is not None else None,
                             scope=scope, team_id=owner, target_ref=name, rationale=why, created_by="user")
        out = {**res, "scope": scope, "owner": owner, "name": name, "from_agent": agent, "skipped": skipped}
        if res.get("status") in ("applied", "canary", "promoted") and p.get("archive_source"):
            out["source_archive"] = self._propose(op="archive", scope="agent", owner=agent, agent=agent,
                                                  payload={"name": name, "reason": f"user: promoted to {scope}{':' + owner if owner else ''}"},
                                                  rationale=f"promoted to {scope}")
        else:
            out["shadowed_by_agent_copy"] = True  # bản tầng agent còn đó → che bản mới với chính agent này
        return out

    def methods(self) -> dict[str, Any]:
        return {
            "knowledge.skill_list": self.skill_list,
            "knowledge.skill_get": self.skill_get,
            "knowledge.skill_save": self.skill_save,
            "knowledge.skill_archive": self.skill_archive,
            "knowledge.skill_restore": self.skill_restore,
            "knowledge.skill_import": self.skill_import,
            "knowledge.skill_promote": self.skill_promote,
        }


MUTATING_SKILLS = ("knowledge.skill_save", "knowledge.skill_archive", "knowledge.skill_restore", "knowledge.skill_import", "knowledge.skill_promote")
