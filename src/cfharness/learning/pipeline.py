"""Proposal (FR-LN-04) + pipeline TỰ ÁP DỤNG L1/L2/L3-cấu-hình (FR-LN-05) — "báo chứ không hỏi", KHÔNG có hàng chờ duyệt.

propose() → lập kế hoạch (tính nội dung mới, diff, kích thước — chưa ghi gì) → `learning.proposal`
  → công tắc học (FR-LN-15: tắt học → blocked `learning_disabled`; tắt tự áp dụng → `suggested`)
  → guard (FR-LN-07/FR-SG-05/FR-LN-19) → `blocked` + `guard.blocked`
  → phân lớp rủi ro L1/L2/L3 → giới hạn tần suất (FR-LN-18) → `deferred_rate_limited` (tự áp dụng khi hết cửa sổ;
    proposal mới hơn cùng đích thay thế → `superseded`)
  → L3: eval gate TRƯỚC (hợp đồng B) → `deferred_no_eval` | `eval_failed` | áp dụng
  → L3 tầng team/global/USER (M5-A, `canary.py`): eval trên golden task của các agent bị ảnh hưởng (mẫu ≥ 3) →
    canary (agent khởi nguồn dùng bản mới, agent khác ghim bản cũ) / shadow (USER) → `promoted` | tự rollback
  → áp dụng qua `Ledger.apply` (snapshot → ghi → LedgerEntry) → `applied` + `learning.applied`
  → L2: eval gate SAU ngay (B tự rollback khi tụt); L1: cờ `batch_eval` (B chấm theo lô hằng ngày).
Trạng thái kết thúc: applied | canary → promoted | deferred_rate_limited | deferred_no_eval | eval_failed | blocked | suggested |
superseded | rolled_back (sau rollback). Không có pending/approved/rejected.
"""

from __future__ import annotations

import asyncio
import difflib
import inspect
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import HubError, NotFound
from ..events import make_event
from ..knowledge.store import (
    SKILL_FILE,
    KnowledgeError,
    apply_memory_op,
    check_rel_path,
    check_skill_name,
    memory_chars,
    parse_memory,
    parse_skill,
    render_memory,
    render_skill,
)
from ..store import now_iso
from . import gate as gate_mod
from .guard import Block
from .canary import is_tier_proposal
from .ledger import Change

if TYPE_CHECKING:
    from ..hub import Hub
    from .service import LearningService

log = logging.getLogger("cfharness.learning")

TARGETS = ("memory", "skill", "config", "user_profile")
OPS = ("add", "patch", "merge", "archive", "delete")
LEARNING_ACTORS = ("review", "curator", "agent", "learning")
FINAL = ("applied", "promoted", "deferred_rate_limited", "deferred_no_eval", "eval_failed", "blocked", "rolled_back", "superseded", "suggested")


def _utc(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _local_midnight_utc(next_day: bool = False) -> datetime:
    d = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    if next_day:
        d += timedelta(days=1)
    return d.astimezone(timezone.utc)


def _diff(before: str, after: str, name: str) -> tuple[str, int]:
    lines = list(difflib.unified_diff(before.splitlines(True), after.splitlines(True), f"a/{name}", f"b/{name}"))
    size = sum(len(x) - 1 for x in lines if x[:1] in "+-" and not x.startswith(("+++", "---")))
    return "".join(x if x.endswith("\n") else x + "\n" for x in lines), size


def _norm(s: str) -> str:
    return " ".join((s or "").lower().split())


class Pipeline:
    def __init__(self, hub: "Hub", svc: "LearningService") -> None:
        self.hub = hub
        self.svc = svc
        self._tasks: set[asyncio.Task] = set()

    @property
    def conn(self):
        return self.hub.store.conn

    @property
    def ks(self):
        return self.hub.knowledge

    # ================================================================== kế hoạch (thuần, không ghi)
    def plan(self, prop: dict[str, Any]) -> dict[str, Any]:
        t = prop["target"]
        if t in ("memory", "user_profile"):
            return self._plan_memory(prop)
        if t == "skill":
            return self._plan_skill(prop)
        if t == "config":
            return self._plan_config(prop)
        raise KnowledgeError(f"target không hợp lệ: {t}", "invalid")

    def _owner(self, prop: dict[str, Any]) -> str | None:
        return {"agent": prop.get("agent_id"), "team": prop.get("team_id"), "global": None}[prop["scope"]]

    def _plan_memory(self, prop: dict[str, Any]) -> dict[str, Any]:
        pl = prop.get("payload") or {}
        if prop["target"] == "user_profile":
            prop["scope"], store = "global", "user"
        else:
            store = "user" if (pl.get("store") or pl.get("target")) == "user" else "memory"
        scope, owner = prop["scope"], self._owner(prop)
        action = pl.get("action") or {"add": "add", "patch": "patch", "delete": "remove"}.get(prop["op"], "add")
        path = self.ks.memory_path(scope, owner, store)
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        entries = parse_memory(before)
        content = pl.get("content") if pl.get("content") is not None else pl.get("new_text")
        new_entries = apply_memory_op(entries, action, content, pl.get("old_text"))
        touched = [e for e in entries if pl.get("old_text") and pl["old_text"] in e] if action in ("patch", "remove") else []
        after = render_memory(new_entries, store)
        prop["op"] = {"add": "add", "patch": "patch", "remove": "delete"}[action]
        prop["target_ref"] = path.name
        diff, size = _diff(before, after, path.name)
        dup = None
        if action in ("add", "patch") and content:
            c = _norm(content)
            other = "memory" if store == "user" else "user"
            if scope != "team" and any(c in _norm(e) or _norm(e) in c for e in self.ks.read_memory(scope, owner, other) if len(_norm(e)) > 20):
                dup = "USER.md" if other == "user" else "MEMORY.md"
            if not dup and scope == "agent" and store == "user" and action == "add":
                # M5-A (FR-LN-14): quan sát riêng của agent (peer) không lặp lại hồ sơ dùng chung
                if any(c in _norm(e) or _norm(e) in c for e in self.ks.read_memory("global", None, "user") if len(_norm(e)) > 20):
                    dup = "global USER.md (hồ sơ dùng chung)"
            if not dup and scope == "agent" and len(c) > 30:
                for s in self.ks.list_skills("agent", owner):
                    f = Path(s["path"]) / SKILL_FILE
                    if f.exists() and c in _norm(f.read_text(encoding="utf-8", errors="replace")):
                        dup = f"skill {s['name']}"
                        break
        ch = Change(scope=scope, owner_id=owner, target=prop["target"], target_ref=path.name, op=prop["op"], actor="?", kind="file", path=path,
                    after=after.encode("utf-8"), agent_id=prop.get("agent_id"))
        return {"scope": scope, "owner": owner, "change": ch, "diff": diff, "size": size, "new_text": content or "",
                "chars_before": memory_chars(entries), "chars_after": memory_chars(new_entries), "limit": self.ks.memory_limit(scope, store), "duplicate_of": dup, "touched_entries": touched}

    def _was_read(self, prop: dict[str, Any], scope: str, owner: str | None, name: str, rel: str) -> bool:
        return (scope, owner or "", name, rel) in self.svc.read_marks.get(prop.get("run_id") or "", set())

    def _plan_skill(self, prop: dict[str, Any]) -> dict[str, Any]:
        pl = prop.get("payload") or {}
        op = prop["op"]
        name = str(pl.get("name") or prop.get("target_ref") or "").strip()
        prop["target_ref"] = name
        agent_id = prop.get("agent_id")
        if op == "add":
            check_skill_name(name)
            scope, owner = prop["scope"], self._owner(prop)
            body = str(pl.get("body") or pl.get("content") or "")
            desc = " ".join(str(pl.get("description") or "").split())
            if body.startswith("---\n"):
                fm, body = parse_skill(body)
                desc = desc or " ".join(str(fm.get("description") or "").split())
            if not body.strip():
                raise KnowledgeError("skill mới cần body (nội dung SKILL.md)", "invalid")
            if not desc:
                raise KnowledgeError("skill mới cần description (≤ 60 ký tự, tiếng Anh)", "invalid")
            d = self.ks.skill_dir(scope, owner, name)
            if (d / SKILL_FILE).exists():
                raise KnowledgeError(f"skill '{name}' đã tồn tại — dùng propose_skill_patch (skill_view trước)", "exists")
            text = render_skill(name, desc, body)
            diff, size = _diff("", text, f"{name}/{SKILL_FILE}")
            ch = Change(scope=scope, owner_id=owner, target="skill", target_ref=name, op="add", actor="?", kind="tree", path=d,
                        files={SKILL_FILE: text.encode("utf-8")}, agent_id=agent_id)
            return {"scope": scope, "owner": owner, "change": ch, "diff": diff, "size": size, "new_text": desc + "\n" + body,
                    "description": desc, "skill_chars": len(text)}
        s = None
        if prop["scope"] in ("team", "global"):  # M5-A: sửa thẳng skill của tầng chỉ định (kể cả khi tầng agent che khuất)
            okey = self._owner(prop)
            s = next(({**x, "scope": prop["scope"], "owner": okey} for x in self.ks.list_skills(prop["scope"], okey) if x["name"] == name), None)
        if s is None and agent_id:
            s = self.ks.find_skill(agent_id, name)
        if s is None:
            raise KnowledgeError(f"skill '{name}' không tồn tại (skills_list để xem) — tạo mới bằng propose_skill_new", "not_found")
        scope, owner = s["scope"], s["owner"]
        prop["scope"] = scope
        if scope == "team":
            prop["team_id"] = owner
        d = Path(s["path"])
        if op == "patch":
            rel = check_rel_path(str(pl.get("file_path") or SKILL_FILE))
            f = d / rel
            cur = f.read_text(encoding="utf-8", errors="replace") if f.exists() else None
            added = ""
            if pl.get("remove_file"):
                if rel == SKILL_FILE:
                    raise KnowledgeError("không xoá SKILL.md bằng patch — dùng propose_skill_archive", "invalid")
                if cur is None:
                    raise KnowledgeError(f"file '{rel}' không tồn tại", "not_found")
                new = None
            elif pl.get("old_string") is not None:
                if cur is None:
                    raise KnowledgeError(f"file '{rel}' không tồn tại", "not_found")
                old, rep = str(pl["old_string"]), str(pl.get("new_string") or "")
                n = cur.count(old) if old else 0
                if n == 0:
                    raise KnowledgeError("old_string không có trong file — skill_view lại và trích đúng nguyên văn", "not_found")
                if n > 1:
                    raise KnowledgeError("old_string xuất hiện nhiều lần — trích dài hơn cho duy nhất", "ambiguous")
                new, added = cur.replace(old, rep), rep
            elif pl.get("content") is not None:
                content = str(pl["content"])
                if rel == SKILL_FILE and cur is not None and not content.startswith("---\n"):
                    fm, _ = parse_skill(cur)
                    new = render_skill(str(fm.get("name") or name), str(fm.get("description") or ""), content, {k: v for k, v in fm.items() if k not in ("name", "description")})
                else:
                    new = content
                added = content
            else:
                raise KnowledgeError("patch cần old_string+new_string, content, hoặc remove_file", "invalid")
            diff, size = _diff(cur or "", new or "", f"{name}/{rel}")
            desc = None
            skill_chars = 0
            if rel == SKILL_FILE and new is not None:
                fm, _ = parse_skill(new)
                desc = " ".join(str(fm.get("description") or "").split())
                skill_chars = len(new)
            ch = Change(scope=scope, owner_id=owner, target="skill", target_ref=name, op="patch", actor="?", kind="tree", path=d,
                        files={rel: None if new is None else new.encode("utf-8")}, agent_id=agent_id)
            return {"scope": scope, "owner": owner, "change": ch, "diff": diff, "size": size, "new_text": added, "description": desc,
                    "skill_chars": skill_chars, "read_required": cur is not None, "was_read": self._was_read(prop, scope, owner, name, rel)}
        if op in ("merge", "archive", "delete"):
            if scope != "agent":
                prop["scope"] = scope  # xoá skill tầng team/global → L3
            target = pl.get("absorbed_into")
            t = self.ks.find_skill(agent_id, target) if (target and agent_id) else None
            prop["absorbed_into"] = target
            cur = (d / SKILL_FILE).read_text(encoding="utf-8", errors="replace") if (d / SKILL_FILE).exists() else ""
            diff, _ = _diff(cur, "", f"{name}/{SKILL_FILE}")
            ch = Change(scope=scope, owner_id=owner, target="skill", target_ref=name, op=op, actor="?", kind="tree", path=d, archive=True, agent_id=agent_id)
            return {"scope": scope, "owner": owner, "change": ch, "diff": diff, "size": len(cur), "new_text": "",
                    "read_required": True, "was_read": self._was_read(prop, scope, owner, name, SKILL_FILE),
                    "absorbed_exists": t is not None and target != name,
                    "absorbed_read": t is not None and self._was_read(prop, t["scope"], t["owner"], target, SKILL_FILE)}
        raise KnowledgeError(f"op skill không hợp lệ: {op}", "invalid")

    def _plan_config(self, prop: dict[str, Any]) -> dict[str, Any]:
        pl = prop.get("payload") or {}
        agent_id = prop.get("agent_id")
        if not agent_id:
            raise KnowledgeError("đề xuất cấu hình cần agent_id", "invalid")
        prop["scope"] = "agent"
        changes = dict(pl.get("changes") or {})
        soul = pl.get("soul")
        if "soul" in changes:
            soul = changes.pop("soul")
        if not changes and soul is None:
            raise KnowledgeError("đề xuất cấu hình rỗng", "invalid")
        state = self.svc.ledger._config_state(agent_id)
        cur = state["config"]
        merged = dict(cur)
        for k, v in changes.items():
            merged[k] = {**cur[k], **v} if isinstance(v, dict) and isinstance(cur.get(k), dict) else v
        before_txt = json.dumps(cur, indent=1, sort_keys=True, ensure_ascii=False) + ("\n--- SOUL ---\n" + state["soul"] if soul is not None else "")
        after_txt = json.dumps(merged, indent=1, sort_keys=True, ensure_ascii=False) + ("\n--- SOUL ---\n" + soul if soul is not None else "")
        keys = sorted(list(changes) + (["soul"] if soul is not None else []))
        prop["target_ref"] = ",".join(keys)
        prop["op"] = "patch"
        diff, size = _diff(before_txt, after_txt, f"{agent_id}/config")
        ch = Change(scope="agent", owner_id=agent_id, target="config", target_ref=prop["target_ref"], op="patch", actor="?", kind="config",
                    config_changes=changes, soul=soul, agent_id=agent_id)
        return {"scope": "agent", "owner": agent_id, "change": ch, "diff": diff, "size": size,
                "new_text": (soul or "") + "\n" + json.dumps(changes, ensure_ascii=False), "current_config": cur}

    # ================================================================== phân lớp + tần suất
    def risk_class(self, prop: dict[str, Any], plan: dict[str, Any]) -> str:
        if prop["scope"] != "agent" or prop["target"] in ("config", "user_profile"):
            return "L3"
        if prop["op"] in ("merge", "archive", "delete"):
            return "L2"
        if plan.get("size", 0) > int(self.hub.config.get("learning.auto_apply_max_chars") or 800):
            return "L2"
        return "L1"

    def _count(self, sql: str, args: tuple) -> int:
        return int(self.conn.execute(sql, args).fetchone()[0])

    def rate_check(self, prop: dict[str, Any]) -> tuple[str, str] | None:
        """(not_before, lý do) nếu phải hoãn (FR-LN-18); None nếu trong hạn. Rollback không bị giới hạn."""
        by = prop.get("created_by")
        if by == "user" or (by == "curator" and prop["op"] == "archive"):
            return None
        r = self.svc.policy.get("rate")
        acts = ",".join(f"'{a}'" for a in LEARNING_ACTORS)
        now = datetime.now(timezone.utc)
        if prop["scope"] in ("team", "global"):
            lim = int(r.get("max_team_global_changes_per_day") or 0)
            if lim and self._count(f"SELECT COUNT(*) FROM ledger_entries WHERE scope IN ('team','global') AND actor IN ({acts}) AND applied_at>=?", (_utc(_local_midnight_utc()),)) >= lim:
                return _utc(_local_midnight_utc(True)), f"đã đủ {lim} thay đổi tầng team/global hôm nay"
        agent = prop.get("agent_id")
        if agent:
            lim = int(r.get("max_changes_per_agent_per_day") or 0)
            if lim and self._count(f"SELECT COUNT(*) FROM ledger_entries WHERE agent_id=? AND actor IN ({acts}) AND op!='rollback' AND applied_at>=?", (agent, _utc(_local_midnight_utc()))) >= lim:
                return _utc(_local_midnight_utc(True)), f"agent đã có {lim} thay đổi hôm nay (learning.rate.max_changes_per_agent_per_day)"
            if prop["target"] == "config":
                lim = int(r.get("max_config_changes_per_agent_per_week") or 0)
                since = now - timedelta(days=7)
                rows = self.conn.execute(f"SELECT applied_at FROM ledger_entries WHERE agent_id=? AND target='config' AND actor IN ({acts}) AND op!='rollback' AND applied_at>=? ORDER BY applied_at",
                                         (agent, _utc(since))).fetchall()
                if lim and len(rows) >= lim:
                    return _utc(_parse(rows[0][0]) + timedelta(days=7)), f"đã đủ {lim} thay đổi cấu hình trong 7 ngày"
        cd = float(r.get("cooldown_per_target_h") or 0)
        if cd and prop["target"] in ("skill", "config"):
            since = _utc(now - timedelta(hours=cd))
            if prop["target"] == "config":
                keys = (prop.get("target_ref") or "").split(",")
                rows = []
                for k in keys:
                    rows += self.conn.execute(f"SELECT applied_at FROM ledger_entries WHERE agent_id=? AND target='config' AND op!='rollback' AND actor IN ({acts}) "
                                              "AND (','||target_ref||',') LIKE ? AND applied_at>=?", (agent, f"%,{k},%", since)).fetchall()
            else:
                rows = self.conn.execute(f"SELECT applied_at FROM ledger_entries WHERE scope=? AND IFNULL(owner_id,'')=IFNULL(?,'') AND target='skill' AND target_ref=? "
                                         f"AND op!='rollback' AND actor IN ({acts}) AND applied_at>=?", (prop["scope"], self._owner(prop), prop["target_ref"], since)).fetchall()
            if rows:
                last = max(_parse(x[0]) for x in rows)
                return _utc(last + timedelta(hours=cd)), f"'{prop['target_ref']}' vừa được sửa lúc {_utc(last)} (cooldown {cd:g} h)"
        return None

    # ================================================================== propose
    def propose(self, *, target: str, op: str, payload: dict[str, Any] | None = None, agent_id: str | None = None, scope: str = "agent",
                team_id: str | None = None, target_ref: str | None = None, rationale: str = "", evidence_signal_ids: list[str] | None = None,
                created_by: str = "review", review_job_id: str | None = None, run_id: str | None = None) -> dict[str, Any]:
        if target not in TARGETS:
            raise HubError(f"target phải là một trong {TARGETS}")
        if op not in OPS:
            raise HubError(f"op phải là một trong {OPS}")
        if scope not in ("agent", "team", "global"):
            raise HubError("scope phải là agent|team|global")
        if agent_id:
            self.hub.agents.get_row(agent_id)
        if target == "user_profile":
            scope = "global"
        if scope == "team":
            team_id = self._resolve_team(agent_id, team_id)
        prop: dict[str, Any] = {
            "id": "pr_" + uuid.uuid4().hex[:12], "scope": scope, "agent_id": agent_id, "team_id": team_id, "target": target, "op": op,
            "target_ref": target_ref or (payload or {}).get("name"), "payload": dict(payload or {}), "rationale": (rationale or "")[:2000],
            "evidence_signal_ids": list(evidence_signal_ids or []), "created_by": created_by, "review_job_id": review_job_id, "run_id": run_id,
            "status": "applying", "created_at": now_iso(), "updated_at": now_iso(),
        }
        plan: dict[str, Any] | None = None
        plan_err: KnowledgeError | None = None
        try:
            plan = self.plan(prop)
        except KnowledgeError as e:
            plan_err = e
        except NotFound as e:
            plan_err = KnowledgeError(e.message, "not_found")
        if plan is not None:
            prop["diff"], prop["size_chars"] = plan["diff"][:50_000], plan["size"]
        row = {k: prop.get(k) for k in ("id", "scope", "agent_id", "team_id", "target", "op", "target_ref", "payload", "diff", "size_chars", "rationale",
                                         "evidence_signal_ids", "created_by", "review_job_id", "run_id", "status", "absorbed_into", "created_at", "updated_at")}
        self.hub.store._insert("proposals", row)
        self._supersede(prop)
        self._emit("learning.proposal", prop, {"target": target, "op": prop["op"], "target_ref": prop.get("target_ref"), "created_by": created_by,
                                               "scope": prop["scope"], "rationale": prop["rationale"][:300]})
        pt = self.svc.guard._policy_touch(prop, plan or {})  # chạm chính sách an toàn: báo đúng loại kể cả khi kế hoạch lỗi
        if pt is not None:
            return self._block(prop, pt)
        if plan_err is not None:
            return self._block(prop, Block("guard_rule", plan_err.reason, plan_err.message))
        assert plan is not None
        # công tắc học (chính sách an toàn, FR-LN-15)
        sw = self.svc.switches(agent_id)
        if not sw["enabled"] and created_by in LEARNING_ACTORS:
            return self._block(prop, Block("guard_rule", "learning_disabled", "học đang tắt cho agent này (learning.enabled=false)"))
        b = self.svc.guard.check(prop, plan)
        if b is not None:
            return self._block(prop, b)
        prop["risk_class"] = self.risk_class(prop, plan)
        self._update(prop["id"], risk_class=prop["risk_class"], scope=prop["scope"], target_ref=prop.get("target_ref"), op=prop["op"],
                     team_id=prop.get("team_id"), absorbed_into=prop.get("absorbed_into"))
        if not sw["auto_apply"] and created_by != "user":
            self._update(prop["id"], status="suggested")
            return self._result(prop, "suggested", reason="learning.auto_apply=false — chỉ ghi đề xuất")
        return self._continue(prop, plan)

    def _resolve_team(self, agent_id: str | None, team_id: str | None) -> str:
        """scope=team: team chỉ định, hoặc team DUY NHẤT của agent (review của agent đề xuất tầng team)."""
        if team_id:
            self.hub.teams.row(team_id)
            if agent_id and self.hub.teams.member(team_id, agent_id) is None:
                raise HubError(f"agent '{agent_id}' không thuộc team '{team_id}'")
            return team_id
        if not agent_id:
            raise HubError("scope team cần team id (--team)")
        teams = [r[0] for r in self.conn.execute("SELECT m.team_id FROM team_members m JOIN teams t ON t.id=m.team_id WHERE m.agent_id=? AND t.archived_at IS NULL ORDER BY m.team_id", (agent_id,))]
        if len(teams) != 1:
            raise HubError(f"agent '{agent_id}' thuộc {len(teams)} team — chỉ rõ team" if teams else f"agent '{agent_id}' không thuộc team nào — không ghi được tầng team")
        return teams[0]

    def _continue(self, prop: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        rl = self.rate_check(prop)
        if rl is not None:
            nb, why = rl
            self._update(prop["id"], status="deferred_rate_limited", not_before=nb, block_reason="rate_limited", block_detail=why)
            self._emit("learning.deferred", prop, {"reason": "rate_limited", "detail": why, "not_before": nb})
            return self._result(prop, "deferred_rate_limited", reason=why, not_before=nb)
        if prop["risk_class"] == "L3" and is_tier_proposal(prop):
            return self._continue_tier(prop, plan)
        if prop["risk_class"] == "L3":
            v = gate_mod.check(self.hub, self._public(prop))
            if inspect.isawaitable(v):
                self._update(prop["id"], status="applying", block_detail="eval gate đang chạy")
                self._spawn(self._after_gate(prop, plan, v))
                st = self.get(prop["id"])["status"]
                return self._result(prop, st, reason="eval gate (L3) đang chạy — kết quả báo qua event learning.*" if st == "applying" else self.get(prop["id"]).get("block_detail"))
            return self._on_gate(prop, plan, gate_mod.normalize(v))
        return self._apply(prop, plan)

    # ------------------------------------------------------------------ M5-A: L3 tầng team/global/USER
    def _continue_tier(self, prop: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        cn = self.svc.canary
        if prop.get("created_by") == "user":
            # Quyết định coordinator (hardening): thay đổi tầng team/global/USER do NGƯỜI tạo áp dụng NGAY cho mọi agent,
            # không eval trước, không ghim; chỉ theo dõi (canary mode `watch`) + cảnh báo — không bao giờ tự rollback.
            busy = cn.busy(plan)
            if busy is not None:
                cn.supersede(busy["id"], f"đích bị thay bởi thay đổi của người (proposal {prop['id']})")
            return self._apply(prop, plan, tier_verdict={"status": "skipped", "scores": {}, "reason": "thay đổi của người — áp dụng ngay, chỉ theo dõi"})
        busy = cn.busy(plan)
        if busy is not None:
            why = f"đích đang canary ({busy['id']}) — áp dụng sau khi canary kết thúc"
            nb = busy.get("deadline") or now_iso()
            self._update(prop["id"], status="deferred_rate_limited", not_before=nb, block_reason="canary_active", block_detail=why)
            self._emit("learning.deferred", prop, {"reason": "canary_active", "detail": why, "not_before": nb, "canary_id": busy["id"]})
            return self._result(prop, "deferred_rate_limited", reason=why, not_before=nb)
        v = cn.gate(self._public(prop))
        if inspect.isawaitable(v):
            self._update(prop["id"], status="applying", block_detail="eval gate tầng team/global (L3) đang chạy")
            self._spawn(self._after_tier_gate(prop, v))
            cur = self.get(prop["id"])
            st = cur["status"]
            if st in ("canary", "promoted", "rolled_back"):
                c = next((x for x in self.svc.canary.list(None, limit=20) if x.get("proposal_id") == prop["id"]), None) or {}
                return self._result(prop, st, ledger_entry_id=cur.get("ledger_entry_id"), canary_id=c.get("id"), canary_agent=c.get("canary_agent_id"), mode=c.get("mode"))
            return self._result(prop, st, reason="eval gate tầng team/global đang chạy — kết quả báo qua event learning.*" if st == "applying" else cur.get("block_detail"))
        return self._on_tier_gate(prop, plan, gate_mod.normalize(v))

    async def _after_tier_gate(self, prop: dict[str, Any], aw: Any) -> None:
        try:
            v = gate_mod.normalize(await aw)
        except Exception as e:
            log.warning("eval gate tầng team/global lỗi cho %s: %r", prop["id"], e)
            v = {"status": "deferred_no_eval", "reason": f"eval_error: {e!r}"[:300]}
        try:
            plan = self.plan(dict(prop, payload=prop["payload"]))
        except (KnowledgeError, NotFound) as e:
            self._block(prop, Block("guard_rule", getattr(e, "reason", "not_found"), e.message))
            return
        self._on_tier_gate(prop, plan, v)

    def _on_tier_gate(self, prop: dict[str, Any], plan: dict[str, Any], v: dict[str, Any]) -> dict[str, Any]:
        if v.get("status") != "passed":
            return self._on_gate(prop, plan, v)
        self._update(prop["id"], eval={k: v.get(k) for k in ("status", "scores", "reason")}, block_detail=None)
        busy = self.svc.canary.busy(plan)  # trong lúc chấm có canary khác cùng đích vừa mở
        if busy is not None:
            return self._continue_tier(prop, plan)
        return self._apply(prop, plan, eval_scores=v.get("scores"), tier_verdict=v)

    async def _after_gate(self, prop: dict[str, Any], plan: dict[str, Any], aw: Any) -> None:
        try:
            v = gate_mod.normalize(await aw)
        except Exception as e:  # lỗi hạ tầng eval → hoãn, không áp dụng
            log.warning("eval gate lỗi cho %s: %r", prop["id"], e)
            v = {"status": "deferred_no_eval", "reason": f"eval_error: {e!r}"[:300]}
        try:
            plan = self.plan(dict(prop, payload=prop["payload"]))  # dựng lại: đích có thể đã đổi trong lúc chấm
        except (KnowledgeError, NotFound) as e:
            self._block(prop, Block("guard_rule", getattr(e, "reason", "not_found"), e.message))
            return
        self._on_gate(prop, plan, v)

    def _on_gate(self, prop: dict[str, Any], plan: dict[str, Any], v: dict[str, Any]) -> dict[str, Any]:
        st = v.get("status")
        self._update(prop["id"], eval={k: v.get(k) for k in ("status", "scores", "reason", "eval_id")})
        if st == "passed":
            return self._apply(prop, plan, eval_scores=v.get("scores"))
        if st == "eval_failed":
            self._update(prop["id"], status="eval_failed", block_reason="eval_failed", block_detail=str(v.get("reason") or "")[:500])
            self._emit("learning.eval_failed", prop, {"scores": v.get("scores"), "reason": v.get("reason")})
            return self._result(prop, "eval_failed", reason=v.get("reason"), scores=v.get("scores"))
        why = str(v.get("reason") or "chưa có golden task (Q-23)")
        self._update(prop["id"], status="deferred_no_eval", block_reason="no_eval", block_detail=why[:500], not_before=_utc(datetime.now(timezone.utc) + timedelta(hours=1)))
        self._emit("learning.deferred", prop, {"reason": "no_eval", "detail": why, "hint": "đánh dấu run tốt làm golden task: cfharness golden add …"})
        return self._result(prop, "deferred_no_eval", reason=why)

    def _apply(self, prop: dict[str, Any], plan: dict[str, Any], eval_scores: dict[str, Any] | None = None,
               tier_verdict: dict[str, Any] | None = None) -> dict[str, Any]:
        ch: Change = plan["change"]
        ch.actor = prop["created_by"] if prop["created_by"] in ("review", "curator", "user", "agent") else "learning"
        ch.proposal_id, ch.risk_class = prop["id"], prop["risk_class"]
        ch.reason = prop.get("rationale") or None
        ch.created_by = {"review": "agent", "agent": "agent", "curator": "curator", "user": "user"}.get(prop["created_by"], "agent")
        plus = sum(1 for x in plan["diff"].splitlines() if x.startswith("+") and not x.startswith("+++"))
        minus = sum(1 for x in plan["diff"].splitlines() if x.startswith("-") and not x.startswith("---"))
        verb = {"add": "add", "patch": "patch", "merge": "merge", "archive": "archive", "delete": "remove from"}[prop["op"]]
        ch.summary = f"{prop.get('agent_id') or prop['scope']} learned: {verb} {prop['target'].replace('_', ' ')} {prop.get('target_ref') or ''} (+{plus}/-{minus} lines)"
        if prop["risk_class"] == "L1":
            ch.flags = ["batch_eval"]
        cid = self.svc.canary.open(prop, plan, tier_verdict) if tier_verdict is not None else None  # ghim agent khác TRƯỚC khi ghi
        try:
            entry = self.svc.ledger.apply(ch)
        except HubError as e:
            if cid:
                self.svc.canary.abort(cid, e.message)
            return self._block(prop, Block("guard_rule", "apply_failed", e.message))
        if eval_scores:
            try:
                self.conn.execute("UPDATE ledger_entries SET eval_before=?, eval_after=? WHERE id=?", (eval_scores.get("before"), eval_scores.get("after"), entry["id"]))
            except Exception:  # pragma: no cover
                pass
        self._update(prop["id"], status="applied", ledger_entry_id=entry["id"])
        canary = self.svc.canary.attach_entry(cid, prop, entry) if cid else None
        if canary is not None:
            ch.summary += f" [{canary['mode']}" + (f" on {canary['canary_agent_id']}]" if canary["mode"] == "canary" else "]")
        self._emit("learning.applied", prop, {"canary_id": cid, "ledger_entry_id": entry["id"], "snapshot_id": entry["snapshot_id"], "scope": prop["scope"], "target": prop["target"],
                                              "target_ref": prop.get("target_ref"), "op": prop["op"], "risk_class": prop["risk_class"], "summary": ch.summary,
                                              "diff": plan["diff"][:1500], "rollback_cmd": f"cfharness learn rollback {entry['id']}"})
        if prop["risk_class"] == "L2":
            self._post_apply(entry)
        if canary is not None:
            return self._result(prop, "canary", ledger_entry_id=entry["id"], summary=ch.summary, canary_id=cid, canary_agent=canary.get("canary_agent_id"), mode=canary["mode"])
        return self._result(prop, "applied", ledger_entry_id=entry["id"], summary=ch.summary)

    def _post_apply(self, entry: dict[str, Any]) -> None:
        v = gate_mod.post_apply(self.hub, entry)
        if inspect.isawaitable(v):
            async def _w() -> None:
                try:
                    self._mark_eval(entry, gate_mod.normalize(await v))
                except Exception as e:  # pragma: no cover
                    log.warning("post_apply lỗi %s: %r", entry["id"], e)

            self._spawn(_w())
        else:
            self._mark_eval(entry, gate_mod.normalize(v))

    def _mark_eval(self, entry: dict[str, Any], v: dict[str, Any]) -> None:
        if v.get("status") == "deferred_no_eval" or "unevaluated" in (v.get("flags") or []):
            e = self.svc.ledger.get(entry["id"])
            flags = list(dict.fromkeys(list(e.get("flags") or []) + ["unevaluated"]))
            self.conn.execute("UPDATE ledger_entries SET flags=? WHERE id=?", (json.dumps(flags), entry["id"]))

    # ================================================================== hoãn → áp dụng lại
    def process_deferred(self) -> list[dict[str, Any]]:
        now = now_iso()
        out = []
        rows = self.conn.execute("SELECT * FROM proposals WHERE status='deferred_rate_limited' AND not_before<=? ORDER BY created_at", (now,)).fetchall()
        for r in rows:
            out.append(self.retry(dict(r)["id"]))
        # deferred_no_eval: thử lại khi agent đã có golden task (Q-23), tối đa mỗi giờ
        golden = getattr(getattr(self.hub, "evals", None), "golden", None)
        if golden is not None and hasattr(golden, "active_for"):
            for r in self.conn.execute("SELECT id, agent_id FROM proposals WHERE status='deferred_no_eval' AND not_before<=?", (now,)).fetchall():
                try:
                    if r[1] and golden.active_for(r[1]):
                        out.append(self.retry(r[0]))
                    else:
                        self._update(r[0], not_before=_utc(datetime.now(timezone.utc) + timedelta(hours=1)))
                except Exception:  # pragma: no cover
                    log.debug("retry deferred_no_eval lỗi", exc_info=True)
        return out

    def retry(self, proposal_id: str) -> dict[str, Any]:
        prop = self.get(proposal_id)
        if prop["status"] not in ("deferred_rate_limited", "deferred_no_eval", "suggested"):
            raise HubError(f"proposal {proposal_id} ở trạng thái {prop['status']} — không thử lại được")
        try:
            plan = self.plan(prop)
        except (KnowledgeError, NotFound) as e:
            return self._block(prop, Block("guard_rule", getattr(e, "reason", "not_found"), e.message))
        b = self.svc.guard.check(prop, plan)
        if b is not None:
            return self._block(prop, b)
        prop["risk_class"] = self.risk_class(prop, plan)
        self._update(prop["id"], status="applying", diff=plan["diff"][:50_000], size_chars=plan["size"])
        return self._continue(prop, plan)

    # ================================================================== tiện ích
    def _supersede(self, prop: dict[str, Any]) -> None:
        rows = self.conn.execute(
            "SELECT id FROM proposals WHERE id!=? AND scope=? AND IFNULL(agent_id,'')=IFNULL(?,'') AND target=? AND IFNULL(target_ref,'')=IFNULL(?,'') "
            "AND status IN ('deferred_rate_limited','deferred_no_eval')", (prop["id"], prop["scope"], prop.get("agent_id"), prop["target"], prop.get("target_ref"))).fetchall()
        for (pid,) in rows:
            self._update(pid, status="superseded", superseded_by=prop["id"])

    def _block(self, prop: dict[str, Any], b: Block) -> dict[str, Any]:
        self._update(prop["id"], status="blocked", block_reason=b.rule, block_detail=b.detail[:1000])
        ev = make_event("guard.blocked", prop.get("agent_id"), {"source": "learning", "proposal_id": prop["id"], "reason": b.category, "rule": b.rule, "detail": b.detail[:500],
                                                                "target": prop["target"], "target_ref": prop.get("target_ref"), "op": prop["op"], "created_by": prop.get("created_by")},
                        run_id=prop.get("run_id"))
        self.hub.publish(ev)
        return self._result(prop, "blocked", reason=b.detail, rule=b.rule, category=b.category)

    def _emit(self, etype: str, prop: dict[str, Any], extra: dict[str, Any]) -> None:
        ev = make_event(etype, prop.get("agent_id"), {"proposal_id": prop["id"], "risk_class": prop.get("risk_class"), **extra}, run_id=prop.get("run_id"))
        ev.team_id = prop.get("team_id")
        self.hub.publish(ev)

    def _update(self, pid: str, **data: Any) -> None:
        data["updated_at"] = now_iso()
        self.hub.store._update("proposals", "id", pid, data)

    def _result(self, prop: dict[str, Any], status: str, **kw: Any) -> dict[str, Any]:
        return {"proposal_id": prop["id"], "status": status, "risk_class": prop.get("risk_class"), "target": prop["target"], "op": prop["op"],
                "target_ref": prop.get("target_ref"), **{k: v for k, v in kw.items() if v is not None}}

    def _public(self, prop: dict[str, Any]) -> dict[str, Any]:
        p = self.get(prop["id"])
        p["owner_id"] = self._owner(p)
        return p

    def _spawn(self, coro: Any) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return
        t = loop.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def get(self, proposal_id: str) -> dict[str, Any]:
        r = self.conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if r is None:
            rows = self.conn.execute("SELECT * FROM proposals WHERE id LIKE ? LIMIT 2", (proposal_id + "%",)).fetchall()
            if len(rows) != 1:
                raise NotFound(f"không tìm thấy proposal '{proposal_id}'")
            r = rows[0]
        return self.hub.store._row("proposals", r)  # type: ignore[return-value]

    def list(self, agent_id: str | None = None, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where, args = [], []
        if agent_id:
            where.append("agent_id=?")
            args.append(agent_id)
        if status:
            where.append("status=?")
            args.append(status)
        sql = "SELECT * FROM proposals" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        return [self.hub.store._row("proposals", r) for r in self.conn.execute(sql, (*args, int(limit)))]  # type: ignore[misc]

    # ================================================================== hợp đồng B: áp proposal lên BẢN SAO home để eval
    def materialize(self, proposal: dict[str, Any], home: Path, cfg: dict[str, Any]) -> dict[str, Any]:
        p = self.get(proposal["id"]) if proposal.get("id") else dict(proposal)
        plan = self.plan(p)
        ch: Change = plan["change"]
        if ch.kind == "config":
            for k, v in (ch.config_changes or {}).items():
                cfg[k] = {**(cfg.get(k) or {}), **v} if isinstance(v, dict) and isinstance(cfg.get(k), dict) else v
            if ch.soul is not None:
                (home / "SOUL.md").write_text(ch.soul, encoding="utf-8")
            return cfg
        if ch.path is None:
            return cfg
        if ch.scope != "agent":  # M5-A: ứng viên tầng team/global → <home>/.tier_overlay/<rel HUB_HOME> (chỉ bản sao eval)
            from ..knowledge.tiers import OVERLAY_DIR, REMOVED_SUFFIX

            dest = home / OVERLAY_DIR / ch.path.relative_to(self.hub.paths.home)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if ch.kind == "tree" and not ch.archive and ch.path.is_dir() and not dest.exists():
                import shutil

                shutil.copytree(ch.path, dest)  # cây hiện tại rồi áp các file đổi
            if ch.kind == "tree" and ch.archive:
                Path(str(dest) + REMOVED_SUFFIX).write_text("", encoding="utf-8")
                return cfg
        else:
            real_home = Path(self.hub.agents.get_row(p["agent_id"])["home_path"])
            dest = home / ch.path.relative_to(real_home)
        if ch.kind == "file":
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(ch.after or b"")
        elif ch.archive:
            import shutil

            if dest.exists():
                shutil.rmtree(dest)
        else:
            for f, data in ch.files.items():
                q = dest / f
                if data is None:
                    if q.exists():
                        q.unlink()
                else:
                    q.parent.mkdir(parents=True, exist_ok=True)
                    q.write_bytes(data)
        return cfg
