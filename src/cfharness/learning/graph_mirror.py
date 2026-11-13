"""Mirror MEMORY.md / USER.md / team memory.md vào graph memory (`on_memory_write` của Hermes — dev note `graph-memory.md` § "Mirror").

Vì sao: bộ trích xuất (graph_extract.py) chỉ đọc lượt người + câu trả lời agent, KHÔNG đọc tool_call — mục agent ghi bằng tool
`memory` / `propose_memory` (và mục người/curator ghi) không bao giờ vào graph. Module này giữ graph khớp với các file memory:

- Đơn vị = một file memory (`agent/<id>/memory|user`, `team/<id>/memory`, `global/-/memory|user`). Trạng thái
  (`hub_state learning.graph_mirror`) = {khoá file: {sha mục: [chữ ký fact sinh từ mục]}}.
- `sync` so mục HIỆN CÓ trong file với trạng thái: mục mới → MỘT lần gọi LLM có cấu trúc (cùng `EXTRACT_SCHEMA`, prompt riêng) đổi
  ghi chú thành fact; mục đã mất (xoá, sửa, rollback, người sửa tay) → đóng hiệu lực các fact mà CHỈ mục đó sinh ra (tất định, không
  LLM). File chưa từng sync → mọi mục là mới (backfill).
- Trigger: pipeline áp dụng thay đổi memory/user_profile (`on_applied`, debounce) + vòng nền (`tick`, mỗi `graph_mirror_interval_s`)
  bắt rollback / sửa tay / file có từ trước.
- Ghi: `graph_write(created_by="agent")` ở ĐÚNG tầng của file — cùng đường tool `memory target=graph` (tầng chung: áp dụng ngay +
  watch, tự rollback theo mục). Nội dung đã qua guard khi vào file memory; lô graph vẫn qua guard + ledger + suất ghi graph.
  Episode `source_kind=memory_write`, `source_ref=memory:<khoá file>`.
- Chi phí: chung trần với trích xuất (`graph_extract_*`, run kind `graph_extract`, trigger `memory_write`).
- Prefetch bỏ fact mirror (`graph_memory.recall.prefetch_mirrored=false`): nội dung đã nằm trong system prompt — chèn lại là trùng.
  `memory_recall` / web / CLI vẫn thấy.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from ..errors import Blocked, HubError
from ..store import now_iso
from .graph_extract import EXTRACT_SCHEMA, EXTRACT_SYSTEM

if TYPE_CHECKING:
    from ..hub import Hub
    from .service import LearningService

log = logging.getLogger("cfharness.learning.graph_mirror")

STATE_KEY = "learning.graph_mirror"
TRIGGER = "memory_write"
SOURCE_KIND = "memory_write"

MIRROR_PROMPT = """Below are the notes saved in an AI agent's long-term memory file ({label}). Each note is already curated and durable.
Convert them into a knowledge graph of entities and facts so they can be searched and linked.

How to write it:
- fact: ONE short declarative sentence in English (at most 200 characters), understandable on its own. Keep every concrete detail
  (names, commands, paths, dates, numbers). A note may give several facts; a note with nothing factual gives none.
- Never turn an instruction addressed to an AI ("always…", "never…", "you must…") into a rule: state it as a fact about a preference or
  convention (e.g. "The team prefers squash merges for feature branches.").
- Entity names keep their original language and spelling (Vietnamese names keep their diacritics). Reuse the exact name of a known
  entity when it is the same thing; put other spellings or nicknames in aliases.
- relation: a short snake_case verb phrase (works_on, prefers, uses, owns, located_in, deadline_is, decided, build_command).
- Set multi=true when the subject can have several values at once for that relation.
- source: always "assistant". scope: always "agent". invalidations: always empty.
- evidence: the number(s) after # of the note(s) the fact comes from. Required.
- At most {max_facts} facts.
{known}
<memory>
{notes}
</memory>"""


def entry_sha(text: str) -> str:
    from ..knowledge.graph.text import nfc

    return hashlib.sha1(" ".join(nfc(text).split()).encode("utf-8")).hexdigest()[:16]


def signature(relation: Any, fact: Any) -> str:
    """Chữ ký fact (bền qua gộp thực thể của consolidation: không dùng tên thực thể)."""
    from ..knowledge.graph.text import canon, norm_relation

    return f"{norm_relation(relation)}|{canon(fact)}"


def file_key(scope: str, owner: str | None, store: str) -> str:
    return f"{scope}/{owner or '-'}/{store}"


def parse_key(key: str) -> tuple[str, str | None, str]:
    scope, owner, store = key.split("/", 2)
    return scope, (None if owner == "-" else owner), store


class MemoryMirror:
    """`hub.learning.graph_mirror`. LLM qua `svc.graph_extract._runtime()` (test gán `graph_extract.runtime`)."""

    def __init__(self, hub: "Hub", svc: "LearningService") -> None:
        self.hub = hub
        self.svc = svc
        self._pending: dict[str, Any] = {}
        self._lock: asyncio.Lock | None = None
        self._last_tick = 0.0
        self.tasks: set[asyncio.Task] = set()

    @property
    def ge(self) -> Any:
        return self.svc.graph_extract

    @property
    def conn(self) -> Any:
        return self.hub.store.conn

    def cfg(self, key: str, default: Any = None) -> Any:
        return self.ge.cfg(key, default)

    def enabled(self) -> bool:
        return bool(self.cfg("graph_mirror_memory", True)) and bool(self.cfg("graph_extract", True))

    # ------------------------------------------------------------------ trạng thái
    def state(self) -> dict[str, dict[str, list[str]]]:
        return dict(self.hub.store.state_get(STATE_KEY, {}) or {})

    def _save(self, key: str, entries: dict[str, list[str]]) -> None:
        st = self.state()
        st[key] = entries
        self.hub.store.state_set(STATE_KEY, st)

    # ------------------------------------------------------------------ các file memory
    def files(self) -> list[tuple[str, str | None, str]]:
        out: list[tuple[str, str | None, str]] = []
        for a in self.hub.store.list_agents():
            out += [("agent", a["id"], "memory"), ("agent", a["id"], "user")]
        try:
            teams = [r[0] for r in self.conn.execute("SELECT id FROM teams WHERE archived_at IS NULL ORDER BY id")]
        except Exception:  # pragma: no cover - bảng team chưa có
            teams = []
        out += [("team", t, "memory") for t in teams]
        out += [("global", None, "memory"), ("global", None, "user")]
        return out

    def read(self, scope: str, owner: str | None, store: str) -> list[str]:
        try:
            return self.hub.knowledge.read_memory(scope, owner, store)
        except (HubError, OSError):
            return []

    def dirty(self, scope: str, owner: str | None, store: str) -> bool:
        cur = {entry_sha(e) for e in self.read(scope, owner, store) if e.strip()}
        return cur != set((self.state().get(file_key(scope, owner, store)) or {}).keys())

    def author(self, scope: str, owner: str | None, store: str, hint: str | None = None) -> str | None:
        """Agent chịu chi phí + quyền tầng của lô graph: tầng agent = chủ; tầng chung = agent vừa ghi (`hint`) hoặc agent của ledger
        entry mới nhất trên file đó. Agent phải được ghi tầng đó (`memory_scopes`) — không thì không mirror (không tốn tiền)."""
        if scope == "agent":
            return owner
        cands = [hint] if hint else []
        target_ref = self.hub.knowledge.memory_path(scope, owner, store).name
        try:
            cands += [r[0] for r in self.conn.execute(
                "SELECT agent_id FROM ledger_entries WHERE scope=? AND IFNULL(owner_id,'')=? AND target IN ('memory','user_profile') "
                "AND target_ref=? AND agent_id IS NOT NULL AND op!='rollback' ORDER BY applied_at DESC LIMIT 5",
                (scope, owner or "", target_ref))]
        except Exception:  # pragma: no cover
            pass
        for a in dict.fromkeys(c for c in cands if c):
            if self.hub.store.get_agent(a) is None or scope not in self.svc.memory_scopes(a):
                continue
            if scope == "team":
                try:
                    self.svc._shared_team(a, owner, "global" in self.svc.memory_scopes(a))
                except (Blocked, HubError):
                    continue
            return a
        return None

    # ------------------------------------------------------------------ trigger
    def on_applied(self, prop: dict[str, Any]) -> None:
        """Pipeline vừa áp dụng thay đổi memory/user_profile → sync file đó sau debounce (nhiều lần ghi liền nhau gộp một lần gọi LLM)."""
        if not self.enabled() or prop.get("target") not in ("memory", "user_profile"):
            return
        pl = prop.get("payload") or {}
        if prop["target"] == "user_profile":
            scope, owner, store = "global", None, "user"
        else:
            scope = prop.get("scope") or "agent"
            store = "user" if (pl.get("store") or pl.get("target")) == "user" else "memory"
            owner = prop.get("agent_id") if scope == "agent" else (prop.get("team_id") if scope == "team" else None)
        if scope == "team":
            store = "memory"
        if scope != "global" and not owner:
            return
        self.schedule(scope, owner, store, hint=prop.get("agent_id"), delay=float(self.cfg("graph_mirror_debounce_s", 20) or 0))

    def schedule(self, scope: str, owner: str | None, store: str, *, hint: str | None = None, delay: float = 0.0) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False  # CLI/test đồng bộ: vòng nền của daemon sẽ sync
        key = file_key(scope, owner, store)
        old = self._pending.pop(key, None)
        if old is not None:
            old.cancel()

        def fire() -> None:
            self._pending.pop(key, None)
            t = loop.create_task(self._safe_sync(scope, owner, store, hint))
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)

        if delay > 0:
            self._pending[key] = loop.call_later(delay, fire)
        else:
            fire()
        return True

    def cancel(self) -> None:
        for h in list(self._pending.values()):
            h.cancel()
        self._pending.clear()

    async def _safe_sync(self, scope: str, owner: str | None, store: str, hint: str | None) -> dict[str, Any]:
        try:
            return await self.sync(scope, owner, store, hint=hint)
        except Exception as e:  # pragma: no cover - nền
            log.exception("mirror memory → graph lỗi (%s)", file_key(scope, owner, store))
            return {"status": "failed", "reason": repr(e)[:300]}

    async def tick(self, *, force: bool = False, max_files: int = 5) -> list[dict[str, Any]]:
        """Vòng nền: sync các file lệch trạng thái (rollback, sửa tay, file có từ trước). Tối đa `max_files` file mỗi lượt."""
        if not self.enabled():
            return []
        now = time.monotonic()
        if not force and now - self._last_tick < float(self.cfg("graph_mirror_interval_s", 300) or 300):
            return []
        self._last_tick = now
        out: list[dict[str, Any]] = []
        for scope, owner, store in self.files():
            if len(out) >= max_files:
                break
            if self.dirty(scope, owner, store):
                res = await self.sync(scope, owner, store)
                if res.get("status") != "skipped" or res.get("reason") not in ("no_author", "cap", "learning_disabled"):
                    out.append({"file": file_key(scope, owner, store), **{k: res.get(k) for k in ("status", "reason", "added", "removed")}})
        return out

    # ------------------------------------------------------------------ sync một file
    async def sync(self, scope: str, owner: str | None, store: str, *, hint: str | None = None) -> dict[str, Any]:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            return await self._sync(scope, owner, store, hint)

    async def _sync(self, scope: str, owner: str | None, store: str, hint: str | None) -> dict[str, Any]:
        key = file_key(scope, owner, store)
        if not self.enabled():
            return {"status": "skipped", "reason": "disabled"}
        entries = [e for e in self.read(scope, owner, store) if e.strip()]
        cur = {entry_sha(e): e for e in entries}
        prev: dict[str, list[str]] = dict(self.state().get(key) or {})
        added = [s for s in cur if s not in prev]
        removed = [s for s in prev if s not in cur]
        if not added and not removed:
            return {"status": "skipped", "reason": "in_sync"}
        agent = self.author(scope, owner, store, hint)
        if agent is None:
            return {"status": "skipped", "reason": "no_author"}
        if not self.svc.switches(agent)["enabled"]:
            return {"status": "skipped", "reason": "learning_disabled"}
        ops = self._invalidations(scope, owner, key, prev, removed, keep={s: prev[s] for s in prev if s in cur})
        sigs: dict[str, list[str]] = {s: prev[s] for s in prev if s in cur}
        cost, model, started, t0, xrun, summary = 0.0, None, now_iso(), time.monotonic(), None, {}
        if added:
            why = self.ge.cap_problem(agent)
            if why:
                return {"status": "skipped", "reason": "cap", "detail": why}
            res = await self._extract(agent, scope, owner, store, [(s, cur[s]) for s in added])
            cost, model = res["cost_usd"], res["model"]
            xrun = "r_" + uuid.uuid4().hex[:12]
            if res.get("ops") is None:  # LLM hỏng: không tiến trạng thái (lượt sau thử lại); lỗi có tốn tiền vẫn tính vào trần
                if cost > 0:
                    self.ge._record(xrun, agent, model, started, cost, "failed", f"mirror memory lỗi: {res.get('error')}"[:300],
                                    {}, TRIGGER, f"memory:{key}", t0)
                return {"status": "failed", "reason": str(res.get("error") or "")[:300], "cost_usd": cost}
            ops += res["ops"]
            sigs.update(res["sigs"])
            summary = {"dropped": res["dropped"]}
        written: dict[str, Any] = {"status": "noop"}
        if ops and any(o.get("action") != "add_entity" for o in ops):
            episode = None
            if added:
                episode = {"text": "\n".join(f"[#{n} memory] {cur[s]}" for n, s in enumerate(added, 1)), "source_kind": SOURCE_KIND,
                           "source_ref": f"memory:{key}"}
            try:
                written = self.svc.graph_write(agent, ops, scope=scope, team_id=owner if scope == "team" else None, episode=episode,
                                               created_by="agent", rationale=f"mirror of {key}: {len(added)} new, {len(removed)} removed note(s)")
            except (Blocked, HubError) as e:
                written = {"status": "blocked", "reason": e.message[:300]}
        # lô bị chặn (guard) vẫn tiến trạng thái: gọi lại LLM cho cùng mục chỉ tốn tiền ra cùng kết quả; hoãn (suất) do pipeline tự thử lại
        self._save(key, {s: sigs.get(s, []) for s in cur})
        out = {"status": "done", "file": key, "added": len(added), "removed": len(removed), "ops": len(ops),
               "graph": {k: written.get(k) for k in ("status", "reason", "proposal_id") if written.get(k) is not None}, "cost_usd": cost, **summary}
        if xrun:
            self.ge._record(xrun, agent, model or "", started, cost, "completed",
                            f"mirror {key}: +{len(added)}/-{len(removed)} notes, {len(ops)} ops"[:300], {}, TRIGGER, f"memory:{key}", t0, out)
        return out

    def _invalidations(self, scope: str, owner: str | None, key: str, prev: dict[str, list[str]], removed: list[str],
                       keep: dict[str, list[str]]) -> list[dict[str, Any]]:
        """Đóng hiệu lực fact mirror của các mục đã mất — chỉ fact có episode `memory:<khoá>` và chữ ký KHÔNG còn mục nào khác giữ."""
        from ..knowledge.graph import owner_key

        gone = {g for s in removed for g in prev.get(s) or []} - {g for v in keep.values() for g in v}
        if not gone:
            return []
        rows = self.conn.execute(
            "SELECT f.id, f.relation, f.fact FROM mem_facts f JOIN mem_episodes e ON e.id=f.source_episode_id "
            "WHERE f.scope=? AND f.owner_id=? AND f.retracted_by IS NULL AND f.invalidated_at IS NULL AND e.source_kind=? AND e.source_ref=?",
            (scope, owner_key(scope, owner), SOURCE_KIND, f"memory:{key}")).fetchall()
        return [{"action": "invalidate_fact", "fact_id": r[0]} for r in rows if signature(r[1], r[2]) in gone]

    async def _extract(self, agent: str, scope: str, owner: str | None, store: str, notes: list[tuple[str, str]]) -> dict[str, Any]:
        from ..runtime import OneShotRequest

        max_facts = int(self.cfg("graph_extract_max_facts", 30) or 30)
        known = self.ge.known_entities(agent)
        label = {"user": "USER.md — what the agent knows about the user", "memory": "MEMORY.md"}[store] + f", {scope} tier"
        prompt = MIRROR_PROMPT.format(label=label, max_facts=max_facts, notes="\n".join(f"[#{n} memory] {t}" for n, (_, t) in enumerate(notes, 1)),
                                      known=("\nKnown entities (reuse these exact names): " + "; ".join(known) + "\n") if known else "")
        model = str(self.cfg("graph_extract_model", "haiku") or "haiku")
        r = await self.ge._runtime().oneshot(OneShotRequest(
            prompt=prompt, schema=EXTRACT_SCHEMA, system=EXTRACT_SYSTEM, model=model,
            budget_usd=float(self.cfg("graph_extract_budget_usd") or 0) or None,
            timeout_s=float(self.cfg("graph_extract_timeout_s", 180) or 180), label=f"graph_mirror:{agent}"))
        cost = float(r.cost_usd or 0.0)
        out = r.output if r.ok and isinstance(r.output, dict) else None
        if out is None:
            return {"ops": None, "error": r.error or "không có structured_output", "cost_usd": cost, "model": model}
        # tầng = tầng của file (không theo LLM); nguồn = ghi chú đã lưu; không có phép vô hiệu (mục mất xử lý tất định)
        facts = [{**f, "scope": scope, "source": "assistant"} for f in out.get("facts") or [] if isinstance(f, dict)]
        win = [{"id": n, "role": "assistant", "text": t} for n, (_, t) in enumerate(notes, 1)]
        batches, dropped = self.ge.to_batches(agent, {"team_id": owner if scope == "team" else None},
                                              {"entities": out.get("entities") or [], "facts": facts, "invalidations": []}, win)
        ops: list[dict[str, Any]] = []
        for (sc, _ow), b in batches.items():
            if sc != scope:  # agent không được ghi tầng file (đã kiểm ở author) — không bao giờ hạ tầng nội dung chung về agent
                dropped.append({"batch": sc, "reason": "tier_mismatch"})
                continue
            ops += b["ops"]
        sigs: dict[str, list[str]] = {}
        for f in facts[:max_facts]:
            for ev in f.get("evidence") or []:
                try:
                    i = int(ev)
                except (TypeError, ValueError):
                    continue
                if 1 <= i <= len(notes):
                    sigs.setdefault(notes[i - 1][0], []).append(signature(f.get("relation"), f.get("fact")))
        return {"ops": ops, "sigs": sigs, "dropped": dropped, "cost_usd": cost, "model": model}
