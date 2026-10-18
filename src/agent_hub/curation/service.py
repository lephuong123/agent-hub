"""`CurationService` (`hub.curation`) — curator liên agent (FR-LN-12) + curator pha 2 LLM (FR-LN-09), M5-B.

Liên agent (tất định, tuỳ chọn LLM gộp):
  quét mỗi team còn hoạt động có ≥ 2 thành viên → skill tầng agent (bỏ pin) → `similarity.find_clusters` (cặp của 2 agent
  khác nhau, điểm ≥ `curation.cross_agent.threshold`) → nội dung gộp (tất định; hoặc `claude -p --json-schema` khi
  `cross_agent.llm_merge` và còn ngân sách) → Proposal L3 `scope=team op=add` (`created_by=curator`) qua pipeline của
  M5-A (`hub.learning.proposals.submit` nếu có, ngược lại `hub.learning.pipeline.propose` của M4) — KHÔNG tự ghi tầng team.
  Eval trước + canary là việc của A. Khi proposal được `promoted` (hoặc `applied` nếu hub chưa có canary) → archive (xoá
  mềm) các bản agent với `absorbed_into=<skill team>` qua pipeline. Dấu vân tay cụm chống đề xuất lặp.
Pha 2 (LLM gộp trong MỘT agent, bật bằng `curation.phase2.enabled`):
  snapshot cả cây `skills/` của agent (blob ledger + dòng `snapshots` kind=`bulk_tree`) → Run `kind=curator` bằng `claude`
  CLI, prompt = CURATOR_REVIEW_PROMPT Hermes NGUYÊN VĂN + ghi chú hub + danh sách ứng viên; tool = MCP `hub-self` vai trò
  `learning`, nhưng lời gọi của run curator được chặn lại ở đây: chỉ skill tầng agent, read-before-write BẮT BUỘC (cả đích
  hấp thụ), archive phải có `absorbed_into` (xoá mềm), mọi ghi qua pipeline `created_by=curator` (ledger actor=curator →
  vẫn được eval gate/regression tự rollback). Kết thúc → đọc khối YAML "Structured summary" → REPORT.md + event.
  `rollback(id)` hoàn tác mọi entry ledger của lượt (mới → cũ).
Chạy nền: mỗi `curation.tick_s` khi hub RẢNH ≥ `curation.min_idle_min`, trong ngân sách ngày `curation.daily_budget_usd`.
Bản ghi: `HUB_HOME/logs/curation/<id>/record.json` + `REPORT.md` (không cần bảng DB mới — không chiếm migration).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ..errors import Conflict, HubError, NotFound
from ..events import HubEvent, TERMINAL_RUN_EVENTS, make_event
from ..store import now_iso
from . import prompts as P
from .similarity import cluster_key, find_clusters, merge_deterministic

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("agent_hub.curation")

PROPOSE_OPS = {"propose_skill_new": "add", "propose_skill_patch": "patch", "propose_skill_archive": "archive", "propose_skill_merge": "merge"}
DEAD_STATUSES = ("blocked", "eval_failed", "rolled_back", "superseded")


def _utc(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_structured_summary(text: str) -> dict[str, Any]:
    """Khối ```yaml consolidations/prunings``` cuối output curator (định dạng Hermes). Lỗi → {}."""
    blocks = re.findall(r"```(?:yaml|yml)?\s*\n(.*?)```", text or "", re.S)
    for b in reversed(blocks):
        try:
            d = yaml.safe_load(b)
        except yaml.YAMLError:
            continue
        if isinstance(d, dict) and ("consolidations" in d or "prunings" in d):
            return {"consolidations": list(d.get("consolidations") or []), "prunings": list(d.get("prunings") or [])}
    return {}


class CurationService:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self._task: asyncio.Task | None = None
        self._busy = False
        self._orig_learnself = None
        hub.bus.add_listener(self._on_event)
        self._wrap_learnself()

    # ------------------------------------------------------------------ cấu hình / trạng thái
    def cfg(self, key: str | None = None) -> Any:
        c = self.hub.config.get("curation")
        return c if key is None else c.get(key)

    @property
    def conn(self):
        return self.hub.store.conn

    def state(self) -> dict[str, Any]:
        return dict(self.hub.store.state_get("curation.state", {}) or {})

    def _state_update(self, **kw: Any) -> None:
        s = self.state()
        s.update(kw)
        self.hub.store.state_set("curation.state", s)

    def _index(self) -> dict[str, Any]:
        return dict(self.hub.store.state_get("curation.index", {}) or {})

    def _index_set(self, key: str, value: Any) -> None:
        i = self._index()
        i[key] = value
        self.hub.store.state_set("curation.index", i)

    def logs_dir(self) -> Path:
        d = self.hub.paths.home / "logs" / "curation"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save(self, rec: dict[str, Any]) -> dict[str, Any]:
        d = self.logs_dir() / rec["id"]
        d.mkdir(parents=True, exist_ok=True)
        rec["updated_at"] = now_iso()
        tmp = d / "record.json.tmp"
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        tmp.replace(d / "record.json")
        (d / "REPORT.md").write_text(render_report(rec), encoding="utf-8")
        rec["report_path"] = str(d / "REPORT.md")
        return rec

    def get(self, cid: str) -> dict[str, Any]:
        base = self.logs_dir()
        hits = [p for p in base.iterdir() if p.is_dir() and p.name.startswith(cid)] if base.exists() else []
        if len(hits) != 1:
            raise NotFound(f"không tìm thấy lượt curation '{cid}'")
        rec = json.loads((hits[0] / "record.json").read_text(encoding="utf-8"))
        rec["report_path"] = str(hits[0] / "REPORT.md")
        return rec

    def list(self, kind: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.logs_dir().iterdir(), key=lambda x: x.name, reverse=True):
            f = p / "record.json"
            if not f.exists():
                continue
            try:
                r = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if kind and r.get("kind") != kind:
                continue
            out.append({k: r.get(k) for k in ("id", "kind", "status", "agent_id", "team_id", "created_at", "cost_usd", "summary", "dry_run")})
            if len(out) >= limit:
                break
        return out

    def _new_id(self, kind: str) -> str:
        return f"cu_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{kind[:2]}{uuid.uuid4().hex[:6]}"

    def _emit(self, etype: str, agent_id: str | None, payload: dict[str, Any], run_id: str | None = None, team_id: str | None = None) -> None:
        ev = make_event(etype, agent_id, payload, run_id=run_id)
        ev.team_id = team_id
        self.hub.publish(ev)

    # ------------------------------------------------------------------ ngân sách / rảnh
    def spent_today(self) -> float:
        start = _utc(datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0))
        r = self.conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM runs WHERE kind='curator' AND queued_at>=?", (start,)).fetchone()
        return float(r[0] or 0)

    def budget_left(self) -> float:
        return max(0.0, float(self.cfg("daily_budget_usd") or 0) - self.spent_today())

    def idle_minutes(self) -> float:
        if self.hub.runs.active or self.hub.runs.queue:
            return 0.0
        r = self.conn.execute("SELECT MAX(COALESCE(ended_at, queued_at)) FROM runs").fetchone()
        last = _parse(r[0]) if r else None
        return 1e9 if last is None else (datetime.now(timezone.utc) - last).total_seconds() / 60

    # ------------------------------------------------------------------ đọc skill
    def _agent_skills(self, agent_id: str, *, include_pinned: bool = False) -> list[dict[str, Any]]:
        from ..knowledge.store import SKILL_FILE, parse_skill

        out = []
        for s in self.hub.knowledge.list_skills("agent", agent_id):
            if s.get("pinned") and not include_pinned:
                continue
            f = Path(s["path"]) / SKILL_FILE
            if not f.exists():
                continue
            fm, body = parse_skill(f.read_text(encoding="utf-8", errors="replace"))
            out.append({"agent_id": agent_id, "name": s["name"], "description": str(fm.get("description") or s.get("description") or ""), "body": body,
                        "use_count": s.get("use_count") or 0, "pinned": bool(s.get("pinned")), "created_at": s.get("created_at"),
                        "last_used_at": s.get("last_used_at"), "created_by": s.get("created_by")})
        return out

    # ================================================================== LIÊN AGENT (FR-LN-12)
    def detect(self, team_id: str | None = None) -> list[dict[str, Any]]:
        """Phát hiện cụm skill gần trùng giữa các agent cùng team (tất định, không ghi gì)."""
        cx = self.cfg("cross_agent") or {}
        teams = [self.hub.teams.row(team_id)] if team_id else self.hub.teams.list()
        out = []
        for t in teams:
            tid = t["id"]
            members = [m["agent_id"] for m in self.hub.teams.members(tid) if not m.get("agent_archived_at")]
            if len(members) < int(cx.get("min_agents") or 2):
                continue
            skills: list[dict[str, Any]] = []
            for a in members:
                try:
                    skills += self._agent_skills(a)
                except NotFound:
                    continue
            team_names = {s["name"] for s in self.hub.knowledge.list_skills("team", tid)}
            for c in find_clusters(skills, float(cx.get("threshold") or 0.5), int(cx.get("min_agents") or 2)):
                c["team_id"] = tid
                c["key"] = cluster_key(tid, c["members"])
                c["team_skill_names"] = sorted(team_names)
                out.append(c)
        return out

    async def cross_agent(self, *, dry_run: bool = False, team_id: str | None = None, llm: bool | None = None) -> dict[str, Any]:
        if self._busy:
            raise Conflict("curation đang chạy")
        self._busy = True
        try:
            return await self._cross_agent(dry_run=dry_run, team_id=team_id, llm=llm)
        finally:
            self._busy = False

    async def _cross_agent(self, *, dry_run: bool, team_id: str | None, llm: bool | None) -> dict[str, Any]:
        cx = self.cfg("cross_agent") or {}
        use_llm = bool(cx.get("llm_merge")) if llm is None else bool(llm)
        rec: dict[str, Any] = {"id": self._new_id("cross_agent"), "kind": "cross_agent", "status": "running", "dry_run": dry_run, "team_id": team_id,
                               "created_at": now_iso(), "llm_merge": use_llm, "clusters": [], "cost_usd": 0.0}
        index = self._index()
        limit = int(cx.get("max_promotions_per_run") or 3)
        n_prop = 0
        for c in self.detect(team_id):
            item: dict[str, Any] = {"key": c["key"], "team_id": c["team_id"], "agents": c["agents"], "score": c["score"], "pairs": c["pairs"],
                                    "members": [{"agent_id": m["agent_id"], "name": m["name"]} for m in c["members"]]}
            prev = index.get("cluster:" + c["key"])
            if prev and prev.get("status") not in DEAD_STATUSES:
                item.update(action="skip", reason=f"đã đề xuất ({prev.get('proposal_id')}, {prev.get('status')})")
                rec["clusters"].append(item)
                continue
            if n_prop >= limit:
                item.update(action="skip", reason=f"đủ {limit} đề xuất/lượt (curation.cross_agent.max_promotions_per_run)")
                rec["clusters"].append(item)
                continue
            merged = merge_deterministic(c["members"], int(self.hub.knowledge.limits().get("skill_desc_chars") or 60))
            item["merge"] = "deterministic"
            if use_llm and not dry_run:
                budget = min(float(self.cfg("run_budget_usd") or 0.3), self.budget_left())
                if budget < 0.01:
                    item["llm_skipped"] = "hết ngân sách curation hôm nay → gộp tất định"
                else:
                    from .llm import structured_call

                    res, cost, rid = await structured_call(self.hub, agent_id=c["members"][0]["agent_id"], prompt=P.render_merge_prompt(c["team_id"], c["members"], merged["name"]),
                                                           schema=P.MERGE_SCHEMA, system=P.MERGE_SYSTEM, label=f"merge {c['key']}", budget_usd=budget, curation_id=rec["id"])
                    rec["cost_usd"] = round(rec["cost_usd"] + cost, 8)
                    item["llm_run_id"] = rid
                    if res is not None and res.get("same_class") is False:
                        item.update(action="skip", reason=f"LLM: không cùng loại việc — {str(res.get('reason') or '')[:200]}")
                        rec["clusters"].append(item)
                        self._index_set("cluster:" + c["key"], {"status": "not_same_class", "curation_id": rec["id"]})
                        continue
                    if res is not None:
                        merged = self._accept_llm_merge(res, merged)
                        item["merge"] = "llm"
            item["team_skill"] = merged["name"]
            if merged["name"] in c["team_skill_names"]:
                item.update(action="skip", reason=f"team '{c['team_id']}' đã có skill '{merged['name']}' — để A/pha 2 xử lý che khuất")
                rec["clusters"].append(item)
                continue
            if dry_run:
                item.update(action="would_propose", preview={"name": merged["name"], "description": merged["description"], "body_chars": len(merged["body"])})
                rec["clusters"].append(item)
                continue
            origin = sorted(c["members"], key=lambda m: (-(int(m.get("use_count") or 0)), m["agent_id"]))[0]["agent_id"]
            res = self.submit_team_proposal(team_id=c["team_id"], agent_id=origin, merged=merged, cluster=item, curation_id=rec["id"])
            n_prop += 1
            item.update(action="proposed", proposal_id=res.get("proposal_id"), proposal_status=res.get("status"), proposal_reason=res.get("reason"),
                        origin_agent=origin, api=res.get("_api"))
            self._index_set("cluster:" + c["key"], {"status": res.get("status"), "proposal_id": res.get("proposal_id"), "curation_id": rec["id"]})
            if res.get("proposal_id"):
                self._index_set("proposal:" + str(res["proposal_id"]), {"curation_id": rec["id"], "key": c["key"], "team_id": c["team_id"], "team_skill": merged["name"],
                                                                         "members": item["members"], "archived": False})
            self._emit("curation.promotion", origin, {"curation_id": rec["id"], "team_id": c["team_id"], "team_skill": merged["name"], "members": item["members"],
                                                      "score": c["score"], "proposal_id": res.get("proposal_id"), "status": res.get("status"), "merge": item["merge"]},
                       team_id=c["team_id"])
            rec["clusters"].append(item)
            if res.get("status") in ("applied", "promoted"):
                self._maybe_archive_members(str(res.get("proposal_id")), res.get("status"))
        rec["status"] = "done"
        rec["summary"] = (f"{len(rec['clusters'])} cụm; đề xuất {sum(1 for x in rec['clusters'] if x.get('action') == 'proposed')}, "
                          f"bỏ qua {sum(1 for x in rec['clusters'] if x.get('action') == 'skip')}" + (" (dry-run)" if dry_run else ""))
        if not dry_run:
            self._state_update(last_cross_at=_utc())
        self.save(rec)
        self._emit("curation.report", None, {"curation_id": rec["id"], "kind": "cross_agent", "summary": rec["summary"], "dry_run": dry_run,
                                             "proposals": [x.get("proposal_id") for x in rec["clusters"] if x.get("proposal_id")], "cost_usd": rec["cost_usd"]})
        return rec

    def _accept_llm_merge(self, res: dict[str, Any], fallback: dict[str, str]) -> dict[str, str]:
        from ..knowledge.store import KnowledgeError, check_skill_name

        name = str(res.get("name") or "").strip().lower()
        try:
            check_skill_name(name)
        except KnowledgeError:
            name = fallback["name"]
        desc = " ".join(str(res.get("description") or "").split())[:60] or fallback["description"]
        body = str(res.get("body") or "").strip()
        if body.startswith("---\n"):
            from ..knowledge.store import parse_skill

            _, body = parse_skill(body)
        return {"name": name, "description": desc, "body": (body or fallback["body"]).rstrip() + "\n"}

    def submit_team_proposal(self, *, team_id: str, agent_id: str, merged: dict[str, str], cluster: dict[str, Any], curation_id: str) -> dict[str, Any]:
        """Hợp đồng M5 mục 1: proposal promote đi qua pipeline của A (scope=team) — không tự ghi tầng team."""
        members = ", ".join(f"{m['agent_id']}/{m['name']}" for m in cluster["members"])
        kw = dict(target="skill", op="add", scope="team", team_id=team_id, agent_id=agent_id, created_by="curator",
                  payload={"name": merged["name"], "description": merged["description"], "body": merged["body"],
                           "curation": {"id": curation_id, "key": cluster["key"], "members": cluster["members"], "score": cluster["score"]}},
                  rationale=f"cross-agent curator: near-duplicate skills across agents ({members}; similarity {cluster['score']}) → one team skill")
        learning = self.hub.learning
        sub = getattr(getattr(learning, "proposals", None), "submit", None)
        try:
            if callable(sub):
                res = dict(sub(**kw))
                res["_api"] = "learning.proposals.submit"
            else:
                res = dict(learning.pipeline.propose(**kw))
                res["_api"] = "learning.pipeline.propose (M4)"
        except HubError as e:
            return {"status": "error", "reason": e.message, "_api": "?"}
        return res

    def _maybe_archive_members(self, proposal_id: str, status: str | None) -> list[dict[str, Any]]:
        info = self._index().get("proposal:" + proposal_id)
        if not info or info.get("archived"):
            return []
        out = []
        for m in info.get("members") or []:
            try:
                r = self.hub.learning.pipeline.propose(
                    target="skill", op="merge", agent_id=m["agent_id"], created_by="curator",
                    payload={"name": m["name"], "absorbed_into": info["team_skill"], "absorbed_scope": "team", "absorbed_owner": info["team_id"],
                             "reason": f"promoted to team {info['team_id']}"},
                    rationale=f"absorbed into team skill {info['team_id']}/{info['team_skill']} (proposal {proposal_id})")
            except HubError as e:
                r = {"status": "error", "reason": e.message}
            if r.get("status") == "blocked" and m["name"] == info["team_skill"] and self._team_skill_exists(info["team_id"], info["team_skill"]):
                # bản agent TRÙNG TÊN che khuất skill team → pipeline không trỏ được absorbed_into tầng team; dùng cổng archive của
                # M4-A/M5-A (hợp đồng M5-A: `hub.knowledge.archive_skill`, ledger actor=curator, xoá mềm) — đích hấp thụ đã kiểm tồn tại.
                try:
                    r = dict(self.hub.knowledge.archive_skill(m["agent_id"], m["name"], "curator",
                                                              f"absorbed into team skill {info['team_id']}/{info['team_skill']} (proposal {proposal_id})"))
                    r["via"] = "knowledge.archive_skill (shadowed name)"
                except HubError as e:
                    r = {"status": "blocked", "reason": f"{r.get('reason')}; archive_skill: {e.message}"}
            out.append({"agent_id": m["agent_id"], "name": m["name"], "status": r.get("status"), "reason": r.get("reason"), "proposal_id": r.get("proposal_id"),
                        "via": r.get("via") or "pipeline merge"})
        info["archived"] = True
        info["archive_results"] = out
        info["promoted_status"] = status
        self._index_set("proposal:" + proposal_id, info)
        self._index_set("cluster:" + info["key"], {"status": status, "proposal_id": proposal_id, "curation_id": info["curation_id"]})
        try:
            rec = self.get(info["curation_id"])
            for c in rec.get("clusters", []):
                if c.get("proposal_id") == proposal_id:
                    c["proposal_status"] = status
                    c["members_archived"] = out
            self.save(rec)
        except (NotFound, OSError, json.JSONDecodeError):
            pass
        self._emit("curation.members_archived", None, {"proposal_id": proposal_id, "team_id": info["team_id"], "team_skill": info["team_skill"], "results": out},
                   team_id=info["team_id"])
        return out

    def _team_skill_exists(self, team_id: str, name: str) -> bool:
        from ..knowledge.store import SKILL_FILE

        try:
            return (self.hub.knowledge.skill_dir("team", team_id, name) / SKILL_FILE).exists()
        except HubError:
            return False

    # ================================================================== PHA 2 (FR-LN-09)
    def bulk_snapshot(self, agent_id: str) -> dict[str, Any]:
        """Snapshot cả cây skills/ của agent TRƯỚC lượt gộp (FR-LN-06 thao tác hàng loạt) — blob ledger + dòng snapshots."""
        from ..learning.ledger import tar_dir

        d = self.hub.knowledge.skills_dir("agent", agent_id)
        data = tar_dir(d) or b""
        sha = self.hub.learning.ledger.blobs.put(data)
        sid = "sn_" + uuid.uuid4().hex[:12]
        self.conn.execute("INSERT INTO snapshots (id, kind, scope, owner_id, target, target_ref, blob, created_at) VALUES (?,?,?,?,?,?,?,?)",
                          (sid, "bulk_tree", "agent", agent_id, "skill", "*", sha, now_iso()))
        return {"snapshot_id": sid, "blob": sha, "bytes": len(data)}

    def phase2(self, agent_id: str, *, dry_run: bool = False, focus: str | None = None, force: bool = False) -> dict[str, Any]:
        self.hub.agents.get_row(agent_id)
        p2 = self.cfg("phase2") or {}
        if not force and not p2.get("enabled"):
            raise HubError("curator pha 2 đang tắt (curation.phase2.enabled=false) — bật bằng `agent-hub config set curation.phase2.enabled true` hoặc dùng --force", code=-32003)
        if not self.hub.learning.switches(agent_id)["enabled"]:
            raise HubError(f"học đang tắt cho agent '{agent_id}'", code=-32003)
        for rec0 in self.list("phase2", 5):
            if rec0.get("agent_id") == agent_id and rec0.get("status") == "running":
                raise Conflict(f"agent '{agent_id}' đang có lượt curator {rec0['id']}")
        skills = self._agent_skills(agent_id)
        if len(skills) < int(p2.get("min_skills") or 2):
            raise HubError(f"agent '{agent_id}' có {len(skills)} skill không pin — chưa đủ để gộp")
        skills = skills[: int(p2.get("max_candidates") or 80)]
        prompt = P.render_phase2_prompt(agent_id, skills, focus)
        rec: dict[str, Any] = {"id": self._new_id("phase2"), "kind": "phase2", "agent_id": agent_id, "status": "running", "dry_run": dry_run,
                               "created_at": now_iso(), "candidates": [s["name"] for s in skills], "cost_usd": 0.0, "prompt_chars": len(prompt)}
        if dry_run:
            rec.update(status="dry_run", summary=f"{len(skills)} ứng viên; prompt {len(prompt)} ký tự (không chạy)", prompt_preview=prompt[-2000:])
            return self.save(rec)
        budget = min(float(self.cfg("run_budget_usd") or 0.3), self.budget_left())
        if budget < 0.01:
            raise HubError(f"hết ngân sách curation hôm nay (curation.daily_budget_usd, đã dùng ${self.spent_today():.4f})", code=-32003)
        rec["snapshot"] = self.bulk_snapshot(agent_id)
        run = self.hub.runs.submit(agent_id, prompt, budget_usd=budget, model=self.cfg("model") or None, kind="curator",
                                   run_opts={"learning_review": True, "read_only": True, "curator_pass": True, "curation_id": rec["id"]},
                                   max_runtime_s=float(self.cfg("timeout_s") or 600))
        rec["run_id"] = run["id"]
        rec["proposals"] = []
        self.save(rec)
        self._index_set("run:" + run["id"], rec["id"])
        self._emit("curation.started", agent_id, {"curation_id": rec["id"], "kind": "phase2", "candidates": len(skills), "snapshot_id": rec["snapshot"]["snapshot_id"]},
                   run_id=run["id"])
        return rec

    # ---- chặn tool của run curator (hub-self vai trò learning)
    def _wrap_learnself(self) -> None:
        learning = getattr(self.hub, "learning", None)
        if learning is None or getattr(learning, "_curation_wrapped", False):
            return
        orig = learning.learnself_call
        self._orig_learnself = orig

        def wrapped(run_id: str, op: str, args: dict[str, Any]) -> Any:
            run = self.hub.store.get_run(run_id)
            if run is None or not (run.get("run_opts") or {}).get("curator_pass"):
                return orig(run_id, op, args)
            return self.curator_tool(run, op, args or {})

        learning.learnself_call = wrapped
        learning._curation_wrapped = True

    def curator_tool(self, run: dict[str, Any], op: str, args: dict[str, Any]) -> Any:
        assert self._orig_learnself is not None
        run_id, agent_id = run["id"], run["agent_id"]
        if op == "skills_list":
            res = self._orig_learnself(run_id, op, args)
            res["skills"] = [s for s in res.get("skills", []) if s.get("scope") == "agent"]
            return res
        if op == "skill_view":
            return self._orig_learnself(run_id, op, args)
        if op not in PROPOSE_OPS:
            return {"status": "blocked", "reason": f"tool '{op}' is not available in a curator pass (skills only)"}
        if run.get("status") not in ("running", "queued"):
            raise HubError(f"run {run_id} không còn chạy", code=-32003)
        kop = PROPOSE_OPS[op]
        name = str(args.get("name") or "")
        marks = self.hub.learning.read_marks.get(run_id, set())
        ks = self.hub.knowledge
        if kop != "add":
            s = ks.find_skill(agent_id, name)
            if s is None or s.get("scope") != "agent":
                return {"status": "blocked", "reason": f"skill '{name}' is not an agent-tier skill of {agent_id} — out of curator scope"}
            rel = str(args.get("file_path") or "SKILL.md") if kop == "patch" else "SKILL.md"
            exists = (Path(s["path"]) / rel).exists()
            if (kop != "patch" or exists) and ("agent", agent_id, name, rel) not in marks:
                return {"status": "blocked", "rule": "read_before_write", "reason": f"call skill_view('{name}'{', file_path=' + repr(rel) if rel != 'SKILL.md' else ''}) in this pass first"}
        if kop in ("archive", "merge"):
            target = str(args.get("absorbed_into") or "")
            if not target:
                return {"status": "blocked", "rule": "absorbed_into", "reason": "archiving requires absorbed_into=<umbrella that already contains the content>"}
            t = ks.find_skill(agent_id, target)
            if t is None or t.get("scope") != "agent" or target == name:
                return {"status": "blocked", "rule": "absorbed_into", "reason": f"absorbed_into '{target}' must be another existing agent-tier skill"}
            if ("agent", agent_id, target, "SKILL.md") not in marks:
                return {"status": "blocked", "rule": "read_before_write", "reason": f"call skill_view('{target}') to confirm it contains the absorbed content first"}
        if kop == "add":
            payload = {k: args.get(k) for k in ("name", "description", "body")}
        elif kop == "patch":
            payload = {k: args.get(k) for k in ("name", "file_path", "old_string", "new_string", "content", "remove_file")}
        else:
            payload = {"name": name, "absorbed_into": args.get("absorbed_into"), "reason": "consolidated: " + str(args.get("reason") or "")[:300]}
        cid = (run.get("run_opts") or {}).get("curation_id")
        res = self.hub.learning.pipeline.propose(target="skill", op="archive" if kop == "merge" else kop, agent_id=agent_id, scope="agent", payload=payload,
                                                 created_by="curator", run_id=run_id, rationale=str(args.get("rationale") or args.get("reason") or f"curator pass {cid}"))
        return res

    def finalize(self, run: dict[str, Any]) -> dict[str, Any] | None:
        cid = self._index().get("run:" + run["id"])
        if not cid:
            return None
        try:
            rec = self.get(cid)
        except NotFound:
            return None
        if rec.get("status") != "running":
            return rec
        props = [self.hub.store._row("proposals", r) for r in self.conn.execute("SELECT * FROM proposals WHERE run_id=? ORDER BY created_at, rowid", (run["id"],))]
        rec["proposals"] = [{k: p.get(k) for k in ("id", "status", "op", "target_ref", "absorbed_into", "risk_class", "block_reason", "ledger_entry_id")} for p in props if p]
        text = self._run_text(run)
        rec["structured"] = parse_structured_summary(text)
        archived = {p["target_ref"] for p in rec["proposals"] if p["op"] in ("archive", "merge") and p["status"] == "applied"}
        listed = {str(x.get("from") or "") for x in rec["structured"].get("consolidations", []) if isinstance(x, dict)} | \
                 {str(x.get("name") or "") for x in rec["structured"].get("prunings", []) if isinstance(x, dict)}
        rec["unlisted_archives"] = sorted(archived - listed)
        rec["cost_usd"] = float(run.get("cost_usd") or 0)
        rec["outcome"] = run.get("outcome") or run.get("status")
        rec["status"] = "done" if rec["outcome"] in ("completed", "budget_exceeded") else "failed"
        n_app = sum(1 for p in rec["proposals"] if p["status"] == "applied")
        rec["summary"] = (f"{len(rec['proposals'])} đề xuất, áp dụng {n_app}, archive {len(archived)}; "
                          f"outcome {rec['outcome']}; ${rec['cost_usd']:.4f}")
        rec["rollback_cmd"] = f"agent-hub curation rollback {rec['id']}"
        rec["agent_text_tail"] = text[-3000:]
        self.save(rec)
        self.hub.learning.read_marks.pop(run["id"], None)
        st = self.state().get("phase2_last") or {}
        st[run["agent_id"]] = _utc()
        self._state_update(phase2_last=st)
        self._emit("curation.completed", run["agent_id"], {"curation_id": rec["id"], "kind": "phase2", "summary": rec["summary"], "archived": sorted(archived),
                                                           "rollback_cmd": rec["rollback_cmd"], "cost_usd": rec["cost_usd"]}, run_id=run["id"])
        return rec

    def _run_text(self, run: dict[str, Any]) -> str:
        texts = [str(e.payload.get("text") or "") for e in self.hub.store.query_events(run_id=run["id"], types=["message.complete"], limit=500)]
        full = "\n".join(t for t in texts if t)
        return full or str(run.get("summary") or "")

    def rollback(self, cid: str, *, actor: str = "user") -> dict[str, Any]:
        """Hoàn tác MỌI entry ledger của một lượt curation (mới → cũ) qua `Ledger.rollback` — có snapshot riêng từng entry."""
        rec = self.get(cid)
        pids = [p["id"] for p in rec.get("proposals") or []]
        if rec.get("kind") == "cross_agent":
            pids = [c["proposal_id"] for c in rec.get("clusters", []) if c.get("proposal_id")] + \
                   [m["proposal_id"] for c in rec.get("clusters", []) for m in (c.get("members_archived") or []) if m.get("proposal_id")]
        if rec.get("run_id"):
            pids += [r[0] for r in self.conn.execute("SELECT id FROM proposals WHERE run_id=?", (rec["run_id"],))]
        entries = []
        for pid in dict.fromkeys(pids):
            for r in self.conn.execute("SELECT id FROM ledger_entries WHERE proposal_id=? AND op!='rollback' AND rolled_back_by IS NULL ORDER BY rowid DESC", (pid,)):
                entries.append(r[0])
        order = [r[0] for r in self.conn.execute(
            f"SELECT id FROM ledger_entries WHERE id IN ({','.join('?' * len(entries))}) ORDER BY rowid DESC", entries)] if entries else []
        done, errors = [], []
        ledger = self.hub.learning.ledger
        for eid in order:
            if ledger.get(eid).get("rolled_back_by"):
                continue
            try:
                done.append(ledger.rollback(eid, actor=actor, reason=f"curation rollback {rec['id']}")["id"])
            except Conflict:
                try:
                    done.append(ledger.rollback(eid, actor=actor, reason=f"curation rollback {rec['id']}", chain=True)["id"])
                except HubError as e:
                    errors.append({"entry": eid, "error": e.message})
            except HubError as e:
                errors.append({"entry": eid, "error": e.message})
        rec["rolled_back"] = {"at": now_iso(), "by": actor, "entries": order, "rollback_entries": done, "errors": errors}
        rec["status"] = "rolled_back" if not errors else "rollback_partial"
        self.save(rec)
        self._emit("curation.rolled_back", rec.get("agent_id"), {"curation_id": rec["id"], "entries": len(done), "errors": len(errors)})
        return rec

    # ================================================================== bus + vòng nền
    def _on_event(self, ev: HubEvent) -> None:
        try:
            if ev.type == "hub.started":
                self.start()
            elif ev.type == "hub.stopping":
                self.stop()
            elif ev.type in TERMINAL_RUN_EVENTS and ev.source == "hub" and ev.run_id and ev.type != "run.waiting_human":
                if ("run:" + ev.run_id) in self._index():
                    run = self.hub.store.get_run(ev.run_id)
                    if run is not None:
                        self._call_soon(self.finalize, run)
            elif ev.type in ("learning.promoted", "learning.applied"):
                pid = str(ev.payload.get("proposal_id") or "")
                if pid and ("proposal:" + pid) in self._index():
                    if ev.type == "learning.promoted" or not self._has_canary():
                        self._call_soon(self._maybe_archive_members, pid, ev.type.split(".")[1])
        except Exception:  # pragma: no cover - listener không được làm hỏng bus
            log.exception("curation listener lỗi với %s", ev.type)

    def _has_canary(self) -> bool:
        lr = getattr(self.hub, "learning", None)
        return any(getattr(lr, n, None) is not None for n in ("canary", "canaries"))

    def _call_soon(self, fn: Any, *a: Any) -> None:
        try:
            asyncio.get_running_loop().call_soon(fn, *a)
        except RuntimeError:
            fn(*a)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = asyncio.get_running_loop().create_task(self.loop())
        except RuntimeError:
            self._task = None

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(float(self.cfg("tick_s") or 300))
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover
                log.exception("curation tick lỗi")

    async def tick(self, now: datetime | None = None) -> dict[str, Any]:
        """Một lượt lịch: chỉ khi hub RẢNH đủ lâu; liên agent theo `interval_h`; pha 2 theo `interval_days` + ngân sách."""
        c = self.cfg()
        out: dict[str, Any] = {}
        if not c.get("enabled", True) or self._busy or self.hub.runs.estop.get("on"):
            return out
        if self.idle_minutes() < float(c.get("min_idle_min") or 0):
            return {"skipped": "not_idle"}
        now = now or datetime.now(timezone.utc)
        st = self.state()
        cx = c.get("cross_agent") or {}
        last = _parse(st.get("last_cross_at"))
        if cx.get("enabled", True) and (last is None or now - last >= timedelta(hours=float(cx.get("interval_h") or 24))):
            out["cross_agent"] = (await self.cross_agent())["id"]
        p2 = c.get("phase2") or {}
        if p2.get("enabled") and self.budget_left() >= min(0.05, float(c.get("run_budget_usd") or 0.3)):
            lastp = st.get("phase2_last") or {}
            for a in self.hub.agents.list():
                t = _parse(lastp.get(a["id"]))
                if t is not None and now - t < timedelta(days=float(p2.get("interval_days") or 7)):
                    continue
                try:
                    if len(self._agent_skills(a["id"])) < int(p2.get("min_skills") or 2):
                        continue
                    out["phase2"] = self.phase2(a["id"])["id"]
                except HubError as e:
                    log.info("curator pha 2 bỏ qua %s: %s", a["id"], e.message)
                    continue
                break  # một lượt pha 2 mỗi tick
        return out

    def status(self) -> dict[str, Any]:
        return {"config": self.cfg(), "state": self.state(), "spent_today_usd": round(self.spent_today(), 6), "budget_left_usd": round(self.budget_left(), 6),
                "idle_min": round(min(self.idle_minutes(), 1e6), 1), "busy": self._busy, "recent": self.list(limit=10),
                "pending_promotions": {k.split(":", 1)[1]: v for k, v in self._index().items() if k.startswith("proposal:") and not v.get("archived")}}


def render_report(rec: dict[str, Any]) -> str:
    L = [f"# Curation {rec['kind']} — {rec['id']}", "", f"- trạng thái: {rec.get('status')}" + (" (dry-run)" if rec.get("dry_run") else ""),
         f"- tạo lúc: {rec.get('created_at')}", f"- chi phí: ${float(rec.get('cost_usd') or 0):.4f}"]
    if rec.get("summary"):
        L.append(f"- tóm tắt: {rec['summary']}")
    if rec["kind"] == "cross_agent":
        L += ["", "## Cụm skill gần trùng giữa agent (FR-LN-12)"]
        for c in rec.get("clusters", []):
            mem = ", ".join(f"{m['agent_id']}/{m['name']}" for m in c["members"])
            L.append(f"- team `{c['team_id']}` · {mem} · điểm {c['score']} → **{c.get('action')}**"
                     + (f" `{c.get('team_skill')}` proposal {c.get('proposal_id')} ({c.get('proposal_status')})" if c.get("proposal_id") else "")
                     + (f" — {c.get('reason')}" if c.get("reason") else ""))
            for m in c.get("members_archived") or []:
                L.append(f"    - archive {m['agent_id']}/{m['name']}: {m.get('status')} {m.get('reason') or ''}")
    else:
        L += [f"- agent: {rec.get('agent_id')}", f"- run: {rec.get('run_id')}", f"- snapshot trước: {(rec.get('snapshot') or {}).get('snapshot_id')}",
              f"- rollback: `{rec.get('rollback_cmd') or 'agent-hub curation rollback ' + rec['id']}`", "", "## Đề xuất (qua pipeline, actor=curator)"]
        for p in rec.get("proposals") or []:
            L.append(f"- {p['op']} {p['target_ref']}" + (f" → {p['absorbed_into']}" if p.get("absorbed_into") else "") + f": {p['status']}"
                     + (f" ({p.get('block_reason')})" if p.get("block_reason") else ""))
        st = rec.get("structured") or {}
        if st:
            L += ["", "## Structured summary (từ curator)", "```yaml", yaml.safe_dump(st, allow_unicode=True, sort_keys=False).strip(), "```"]
        if rec.get("unlisted_archives"):
            L.append(f"\n⚠ skill bị archive nhưng không có trong structured summary: {', '.join(rec['unlisted_archives'])}")
    if rec.get("rolled_back"):
        rb = rec["rolled_back"]
        L += ["", f"## Đã rollback lúc {rb['at']} ({len(rb['rollback_entries'])} entry, lỗi {len(rb['errors'])})"]
    return "\n".join(L) + "\n"
