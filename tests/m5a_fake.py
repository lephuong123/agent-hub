"""Helper test M5-A (tầng team/global, canary): hub in-process + gate giả + run giả. Không gọi claude."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from cfharness.events import iso
from cfharness.hub import Hub
from cfharness.learning.runtime import system_prompt_for
from cfharness.prompt import prompt_hash


class FakeGate:
    """`hub.evals.gate` giả: kết quả theo agent (mặc định passed)."""

    def __init__(self, results: dict[str, str] | None = None) -> None:
        self.results = results or {}
        self.calls: list[dict[str, Any]] = []

    def check(self, p: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(p))
        st = self.results.get(p["agent_id"], "passed")
        sc = {"before": 1.0, "after": 1.0 if st == "passed" else 0.5}
        return {"status": st, "scores": sc, "reason": f"{st} @ {p['agent_id']}", "eval_id": "ev_" + p["agent_id"]}

    def post_apply(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {"status": "passed"}


def make_hub(hub_home, agents=("a1", "a2", "a3"), team: str | None = "t1", golden=("a1", "a2", "a3")) -> Hub:
    h = Hub(hub_home)
    for a in agents:
        h.agents.create({"id": a, "name": a.upper(), "model": "haiku"})
    if team:
        h.teams.create({"id": team, "name": "Team"})
        for a in agents:
            h.teams.member_set(team, a, "worker")
    h.events = []  # type: ignore[attr-defined]
    h.bus.add_listener(lambda ev: h.events.append(ev))
    gs = set(golden)
    h.evals.golden.active_for = lambda a: [{"id": "g_" + a}] if a in gs else []  # type: ignore[method-assign]
    h.evals.golden.suggest = lambda a, **kw: {"agent_id": a}  # type: ignore[method-assign]
    h.evals.gate = FakeGate()
    return h


def prompt_of(h: Hub, agent_id: str) -> str:
    text, _ = system_prompt_for(h, {"id": "r_x"}, h.store.get_agent(agent_id))
    return text


def phash(h: Hub, agent_id: str) -> str:
    return prompt_hash(prompt_of(h, agent_id))


def add_run(h: Hub, agent: str, rid: str, when: datetime | None = None, status: str = "completed", correction: bool = False) -> None:
    when = when or datetime.now(timezone.utc)
    h.store.insert_run({"id": rid, "agent_id": agent, "kind": "adhoc", "prompt": "x", "status": status, "outcome": status, "queued_at": iso(when),
                        "ended_at": iso(when + timedelta(seconds=1)), "cost_usd": 0.001})
    if correction:
        h.learning.signals.record(agent, "correction", {"note": "wrong"}, run_id=rid, source="cli")


def team_memory(h: Hub, content: str, agent: str = "a1", by: str = "review") -> dict[str, Any]:
    return h.learning.pipeline.propose(target="memory", op="add", agent_id=agent, scope="team",
                                       payload={"store": "memory", "action": "add", "content": content}, created_by=by)


def types(h: Hub) -> list[str]:
    return [e.type for e in h.events]  # type: ignore[attr-defined]
