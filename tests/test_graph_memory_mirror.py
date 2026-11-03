"""on_memory_write — mirror MEMORY.md / USER.md / team memory.md vào graph (learning/graph_mirror.py). LLM GIẢ — không gọi claude."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from cfharness.knowledge.graph import GraphStore
from cfharness.learning.graph_extract import RUN_KIND
from cfharness.learning.graph_mirror import STATE_KEY
from cfharness.runtime.base import OneShotResult
from m5a_fake import make_hub

NOTE = re.compile(r"^\[#(\d+) memory\] (.*)$", re.M)

# ghi chú → fact (subject, relation, fact). Ghi chú không có trong bảng → không fact.
FACTS = {
    "Build: cd web && npx vite build": ("web-1-hub", "build_command", "web-1-hub is built with `cd web && npx vite build`."),
    "Tests: CI=true npm test": ("web-1-hub", "test_command", "web-1-hub tests run with `CI=true npm test`."),
    "Build: pnpm build": ("web-1-hub", "build_command", "web-1-hub is built with `pnpm build`."),
    "Phương prefers Vietnamese reports": ("Phương", "prefers", "Phương prefers status reports in Vietnamese."),
    "Reports in Vietnamese for Phương": ("Phương", "prefers", "Phương prefers status reports in Vietnamese."),
}


class FakeLLM:
    def __init__(self, fail: bool = False) -> None:
        self.prompts: list[str] = []
        self.fail = fail

    async def oneshot(self, req: Any) -> OneShotResult:
        self.prompts.append(req.prompt)
        if self.fail:
            return OneShotResult(False, None, 0.0, 1, "fake failure")
        facts = []
        for n, text in NOTE.findall(req.prompt):
            if text.strip() in FACTS:
                s, r, f = FACTS[text.strip()]
                facts.append({"subject": s, "relation": r, "fact": f, "source": "assistant", "scope": "global", "evidence": [int(n)]})
        out = {"entities": [], "facts": facts, "invalidations": []}
        return OneShotResult(True, out, 0.002, 1, "", {"structured_output": out})


@pytest.fixture
def hub(hub_home):
    h = make_hub(hub_home)
    h.learning.graph_extract.runtime = FakeLLM()
    yield h
    h.store.close()


def write(h, content: str, *, action: str = "add", old: str | None = None, scope: str = "agent", agent: str = "a1", team: str | None = None,
          store: str = "memory") -> dict:
    pl = {"store": store, "action": action, "content": content, "old_text": old}
    return h.learning.pipeline.propose(target="memory", op={"add": "add", "patch": "patch", "remove": "delete"}[action], scope=scope,
                                       agent_id=agent, team_id=team, payload=pl, created_by="user")


def sync(h, scope="agent", owner="a1", store="memory") -> dict:
    return asyncio.run(h.learning.graph_mirror.sync(scope, owner, store))


def valid(h, tiers=(("agent", "a1"),)) -> list[str]:
    return sorted(f["fact"] for f in GraphStore(h.store.conn).facts(list(tiers)))


def test_new_entries_become_facts_with_provenance_and_cost_row(hub):
    assert write(hub, "Build: cd web && npx vite build")["status"] == "applied"
    write(hub, "Tests: CI=true npm test")
    write(hub, "Meeting notes are boring")  # không có fact → mục vẫn được đánh dấu đã mirror
    res = sync(hub)
    assert res["status"] == "done" and res["added"] == 3 and res["graph"]["status"] == "applied"
    assert valid(hub) == ["web-1-hub is built with `cd web && npx vite build`.", "web-1-hub tests run with `CI=true npm test`."]
    f = GraphStore(hub.store.conn).facts([("agent", "a1")])[0]
    ep = hub.store.conn.execute("SELECT source_kind, source_ref, text FROM mem_episodes WHERE id=?", (f["source_episode_id"],)).fetchone()
    assert ep[0] == "memory_write" and ep[1] == "memory:agent/a1/memory" and "[#1 memory]" in ep[2]
    assert f["scope"] == "agent"  # LLM nói global — tầng luôn theo file
    run = hub.store.conn.execute("SELECT kind, cost_usd, run_opts FROM runs WHERE kind=?", (RUN_KIND,)).fetchone()
    assert run[0] == RUN_KIND and run[1] == pytest.approx(0.002) and "memory_write" in run[2]
    llm = hub.learning.graph_extract.runtime
    assert sync(hub)["reason"] == "in_sync" and len(llm.prompts) == 1  # không đổi → không gọi LLM


def test_removed_entry_invalidates_only_its_facts_without_llm(hub):
    write(hub, "Build: cd web && npx vite build")
    write(hub, "Tests: CI=true npm test")
    sync(hub)
    write(hub, "", action="remove", old="Tests: CI=true")
    llm = hub.learning.graph_extract.runtime
    n = len(llm.prompts)
    res = sync(hub)
    assert res["removed"] == 1 and res["added"] == 0 and len(llm.prompts) == n
    assert valid(hub) == ["web-1-hub is built with `cd web && npx vite build`."]
    hist = [f["fact"] for f in GraphStore(hub.store.conn).facts([("agent", "a1")], include_invalid=True) if f.get("invalidated_at")]
    assert hist == ["web-1-hub tests run with `CI=true npm test`."]  # đóng hiệu lực, không xoá


def test_patched_entry_replaces_fact(hub):
    write(hub, "Build: cd web && npx vite build")
    sync(hub)
    write(hub, "Build: pnpm build", action="patch", old="Build: cd web && npx vite build")
    res = sync(hub)
    assert res["added"] == 1 and res["removed"] == 1
    assert valid(hub) == ["web-1-hub is built with `pnpm build`."]


def test_fact_kept_while_another_entry_still_supports_it(hub):
    write(hub, "Phương prefers Vietnamese reports")
    write(hub, "Reports in Vietnamese for Phương")
    sync(hub)
    assert valid(hub) == ["Phương prefers status reports in Vietnamese."]
    write(hub, "", action="remove", old="Phương prefers Vietnamese")
    sync(hub)
    assert valid(hub) == ["Phương prefers status reports in Vietnamese."]


def test_manual_mirror_fact_from_session_is_not_invalidated(hub):
    """Fact cùng câu nhưng từ nguồn khác (trích xuất session, tool memory) không bị đóng khi mục memory mất."""
    hub.learning.graph_write("a1", [{"action": "add_fact", "src": "web-1-hub", "relation": "test_command",
                                     "fact": "web-1-hub tests run with `CI=true npm test`."}], created_by="user")
    write(hub, "Tests: CI=true npm test")
    sync(hub)
    write(hub, "", action="remove", old="Tests: CI=true")
    sync(hub)
    assert valid(hub) == ["web-1-hub tests run with `CI=true npm test`."]


def test_team_tier_needs_an_author_allowed_to_write_it(hub):
    write(hub, "Build: cd web && npx vite build", scope="team", team="t1", agent="a2")  # người ghi, a2 là worker
    llm = hub.learning.graph_extract.runtime
    res = sync(hub, "team", "t1")
    assert res == {"status": "skipped", "reason": "no_author"} and not llm.prompts  # không ai được ghi graph team → không tốn tiền
    hub.teams.member_set("t1", "a1", "lead")
    write(hub, "Tests: CI=true npm test", scope="team", team="t1", agent="a1")
    res = sync(hub, "team", "t1")
    assert res["status"] == "done" and res["graph"]["status"] in ("applied", "canary")  # tầng chung: áp dụng ngay + watch
    assert len(valid(hub, (("team", "t1"),))) == 2 and valid(hub) == []


def test_llm_failure_keeps_state_for_retry(hub):
    write(hub, "Build: cd web && npx vite build")
    hub.learning.graph_extract.runtime = FakeLLM(fail=True)
    assert sync(hub)["status"] == "failed"
    assert "agent/a1/memory" not in (hub.store.state_get(STATE_KEY, {}) or {})
    hub.learning.graph_extract.runtime = FakeLLM()
    assert sync(hub)["status"] == "done" and len(valid(hub)) == 1


def test_disabled_switch_and_daily_cap(hub):
    write(hub, "Build: cd web && npx vite build")
    hub.config.set("learning.graph_mirror_memory", False)
    assert sync(hub)["reason"] == "disabled"
    hub.config.set("learning.graph_mirror_memory", True)
    hub.config.set("learning.graph_extract_max_per_agent_per_day", 1)
    hub.learning.graph_extract._reserved["a1"] = 1
    assert sync(hub)["reason"] == "cap"
    hub.learning.graph_extract._reserved.clear()
    assert sync(hub)["status"] == "done"


def test_tick_backfills_existing_files_and_catches_rollback(hub):
    write(hub, "Build: cd web && npx vite build")
    res = write(hub, "Tests: CI=true npm test")
    out = asyncio.run(hub.learning.graph_mirror.tick(force=True))
    assert {o["file"] for o in out} == {"agent/a1/memory"} and len(valid(hub)) == 2
    hub.learning.ledger.rollback(res["ledger_entry_id"])  # rollback mục memory → fact của nó đóng ở lượt sau
    asyncio.run(hub.learning.graph_mirror.tick(force=True))
    assert valid(hub) == ["web-1-hub is built with `cd web && npx vite build`."]
    assert asyncio.run(hub.learning.graph_mirror.tick(force=True)) == []


def test_applied_memory_change_schedules_sync(hub):
    hub.config.set("learning.graph_mirror_debounce_s", 0)

    async def go():
        write(hub, "Build: cd web && npx vite build")
        for _ in range(5):
            await asyncio.sleep(0)
            if hub.learning.graph_mirror.tasks:
                await asyncio.gather(*hub.learning.graph_mirror.tasks)
    asyncio.run(go())
    assert valid(hub) == ["web-1-hub is built with `cd web && npx vite build`."]


def test_prefetch_skips_mirrored_facts_but_recall_returns_them(hub):
    write(hub, "Build: cd web && npx vite build")
    sync(hub)
    hub.learning.graph_write("a1", [{"action": "add_fact", "src": "web-1-hub", "relation": "deploy_target", "fact": "web-1-hub deploys to Vercel."}],
                             created_by="user")
    from cfharness.knowledge.graph.provider import provider_for

    p = provider_for(hub)
    block = p.prefetch("how do I build and deploy web-1-hub", agent_id="a1")
    assert "Vercel" in block and "vite build" not in block
    facts = [f.get("fact") for f in p.recall("a1", {"query": "web-1-hub build"})["facts"]]
    assert any("vite build" in (f or "") for f in facts)
    hub.config.set("graph_memory.recall.prefetch_mirrored", True)
    assert "vite build" in p.prefetch("how do I build and deploy web-1-hub", agent_id="a1")
