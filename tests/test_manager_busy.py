"""Agent bận: điều phối viên không còn xếp hàng lặng lẽ việc cho agent đã hết slot (run_start/task_create → agent_busy + agent rảnh cùng
loại + id clone), `agent_clone` thêm agent cùng loại, `hub_overview` có free_slots, `hub_run` MCP cảnh báo khi run phải chờ."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from m7_fake import MgrScript, close, make_daemon, manager_run, rpc, tool, wait_status


class HoldScript(MgrScript):
    def __call__(self, spec: Any) -> dict[str, Any]:
        out = super().__call__(spec)
        if "HOLD" in spec.prompt:
            out["hold"] = True
        return out


@pytest.fixture
async def d(hub_home):
    dm = await make_daemon(hub_home, HoldScript())
    yield dm
    await close(dm)


def _mk(d, aid: str, role: str = "Software engineer", **kw) -> None:
    d.hub.agents.create({"id": aid, "name": aid, "role": role, "description": f"{aid} does web work", **kw}, f"# {aid} soul\n")


async def _busy(d, rid: str, agent: str) -> str:
    r = await tool(d, rid, "run_start", {"agent": agent, "prompt": "HOLD long job"})
    await wait_status(d, r["run_id"])
    return r["run_id"]


async def test_run_start_on_busy_agent_is_refused_with_alternatives(d):
    res = await manager_run(d)
    rid = res["run_id"]
    for a in ("cf-web-coder-4", "cf-web-coder-5", "web-reviewer"):
        _mk(d, a, role="Code reviewer" if a == "web-reviewer" else "Software engineer")
    held = await _busy(d, rid, "cf-web-coder-4")
    ov = await tool(d, rid, "hub_overview")
    slots = {a["id"]: a["free_slots"] for a in ov["agents"]}
    assert slots["cf-web-coder-4"] == 0 and slots["cf-web-coder-5"] == 1

    before = len(d.hub.store.list_runs(agent_id="cf-web-coder-4", limit=50))
    out = await tool(d, rid, "run_start", {"agent": "cf-web-coder-4", "prompt": "second job"})
    assert out["started"] is False and out["reason"] == "agent_busy"
    assert [x["run_id"] for x in out["busy_with"]] == [held]
    alts = out["idle_alternatives"]
    assert alts[0]["id"] == "cf-web-coder-5" and "same_kind" in alts[0]["match"]
    assert "web-reviewer" not in [x["id"] for x in alts] and "boss" not in [x["id"] for x in alts]
    assert out["suggested_clone_id"] == "cf-web-coder-6"
    assert len(d.hub.store.list_runs(agent_id="cf-web-coder-4", limit=50)) == before  # không tạo run nào
    acts = [e.payload for e in d.hub.store.query_events(types=["manager.action"]) if e.payload["tool"] == "run_start"]
    assert acts[-1]["ok"] is False and "agent_busy" in acts[-1]["error"]

    q = await tool(d, rid, "run_start", {"agent": "cf-web-coder-4", "prompt": "second job", "if_busy": "queue"})
    assert q["run_id"] and d.hub.store.get_run(q["run_id"])["status"] == "queued"


async def test_task_create_busy_assignee_refused_unless_parents_or_queue(d):
    res = await manager_run(d)
    rid = res["run_id"]
    _mk(d, "w")
    await _busy(d, rid, "w")
    out = await tool(d, rid, "task_create", {"title": "x", "body": "y", "assignee": "w"})
    assert out["reason"] == "agent_busy" and "task_id" not in out
    parent = await tool(d, rid, "task_create", {"title": "p", "body": "p", "assignee": "w", "if_busy": "queue"})
    assert parent["task_id"]
    child = await tool(d, rid, "task_create", {"title": "c", "body": "c", "assignee": "w", "parents": [parent["task_id"]]})
    assert child["task_id"]


async def test_team_task_warns_when_every_worker_busy(d):
    res = await manager_run(d)
    rid = res["run_id"]
    _mk(d, "lead1", role="Dev lead")
    _mk(d, "w")
    await tool(d, rid, "team_create", {"id": "dev", "name": "Dev", "members": [{"agent_id": "lead1", "role": "lead"}, {"agent_id": "w", "role": "worker"}]})
    await _busy(d, rid, "w")
    out = await tool(d, rid, "task_create", {"title": "x", "body": "y", "team": "dev", "decompose": False})
    assert out["task_id"] and any("every worker of team 'dev' is busy" in w for w in out["warnings"])


async def test_agent_clone_copies_config_soul_skills_and_joins_teams(d):
    res = await manager_run(d)
    rid = res["run_id"]
    _mk(d, "verify-a", role="QA lead", model="sonnet", tool_policy={"browser": "playwright", "disallowed_tools": ["Bash(rm:*)"]})
    _mk(d, "w")
    await tool(d, rid, "team_create", {"id": "qa", "name": "QA", "members": [{"agent_id": "verify-a", "role": "lead"}, {"agent_id": "w", "role": "worker"}]})
    src_home = Path(d.hub.store.get_agent("verify-a")["home_path"])
    (src_home / "skills" / "cf-check").mkdir(parents=True, exist_ok=True)
    (src_home / "skills" / "cf-check" / "SKILL.md").write_text("---\nname: cf-check\n---\nsteps\n", encoding="utf-8")

    out = await tool(d, rid, "agent_clone", {"src": "verify-a"})
    assert out["id"] == "verify-a-2" and out["cloned_from"] == "verify-a"
    assert out["teams"] == [{"team": "qa", "role": "worker"}]
    row = d.hub.store.get_agent("verify-a-2")
    assert row["role"] == "QA lead" and row["model"] == "sonnet" and row["cloned_from"] == "verify-a"
    assert row["tool_policy"]["browser"] == "playwright" and "Bash(rm:*)" in row["tool_policy"]["disallowed_tools"]
    home = Path(row["home_path"])
    assert (home / "SOUL.md").read_text(encoding="utf-8").startswith("# verify-a soul")
    assert (home / "skills" / "cf-check" / "SKILL.md").exists()
    assert d.hub.teams.member("qa", "verify-a-2")["role"] == "worker"


async def test_agent_clone_refuses_manager(d):
    from cfharness.errors import HubError

    res = await manager_run(d)
    with pytest.raises(HubError, match="điều phối viên"):
        await tool(d, res["run_id"], "agent_clone", {"src": "boss"})


async def test_hub_run_mcp_warns_when_agent_busy(d):
    res = await manager_run(d)
    _mk(d, "w")
    _mk(d, "w-2")
    await _busy(d, res["run_id"], "w")
    out = await rpc(d, "mcp.run", {"agent": "w", "prompt": "more"})
    assert out["status"] == "queued" and out["warnings"] and "w-2" in out["warnings"][0]
    free = await rpc(d, "mcp.run", {"agent": "w-2", "prompt": "ok"})
    assert "warnings" not in free
