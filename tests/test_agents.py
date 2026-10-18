import asyncio
from pathlib import Path

import pytest
import yaml

from agent_hub.errors import Blocked, Conflict, HubError, NotFound
from agent_hub.hub import Hub
from agent_hub.runtime import FakeRuntime


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home, runtime=FakeRuntime())
    yield h
    h.store.close()


def event_types(hub, agent_id):
    return [e.type for e in hub.store.query_events(agent_id=agent_id)]


def test_create_layout_db_and_event(hub, hub_home):
    ag = hub.agents.create({"id": "researcher", "name": "Researcher", "role": "Nghiên cứu", "model": "sonnet"})
    home = hub_home.agent_home("researcher")
    for f in ("config.yaml", "SOUL.md", "MEMORY.md", "USER.md"):
        assert (home / f).is_file()
    for d in ("skills", ".archive", "runtime", "workspaces"):
        assert (home / d).is_dir()
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["id"] == "researcher" and cfg["model"] == "sonnet"
    assert "You are **Researcher**" in (home / "SOUL.md").read_text()
    assert ag["config_revision"] == 1 and ag["status"] == "idle"
    assert "agent.created" in event_types(hub, "researcher")


def test_create_with_soul_and_duplicate(hub):
    hub.agents.create({"id": "r1", "name": "R"}, soul="# Custom soul\nBe terse.")
    assert hub.agents.soul("r1").startswith("# Custom soul")
    with pytest.raises(Conflict, match="agent đã tồn tại"):
        hub.agents.create({"id": "r1", "name": "R2"})
    assert hub.agents.show("r1")["name"] == "R"


def test_create_validation_lists_fields(hub):
    with pytest.raises(HubError) as ei:
        hub.agents.create({"id": "Bad Id", "name": "x", "model": "gpt-4"})
    fields = {f["field"] for f in ei.value.data["fields"]}
    assert {"id", "model"} <= fields


def test_create_unknown_mcp_rejected(hub, hub_home):
    with pytest.raises(HubError, match="registry"):
        hub.agents.create({"id": "c", "name": "c", "mcp_servers": ["vb"]})
    hub_home.mcp_registry.write_text(yaml.safe_dump({"servers": {"vb": {"command": "echo"}}}))
    assert hub.agents.create({"id": "c", "name": "c", "mcp_servers": ["vb"]})["mcp_servers"] == ["vb"]


def test_list_show_edit_revisions(hub):
    hub.agents.create({"id": "a", "name": "A"})
    hub.agents.create({"id": "b", "name": "B"})
    assert [r["id"] for r in hub.agents.list()] == ["a", "b"]
    ag = hub.agents.update("a", {"model": "haiku", "limits": {"budget_usd_per_day": 1.5}, "tool_policy": {"allowed_tools": ["Read", "Grep"]}})
    assert ag["config_revision"] == 2 and ag["model"] == "haiku"
    assert ag["limits"]["budget_usd_per_day"] == 1.5 and ag["limits"]["max_concurrent_runs"] == 1
    ag = hub.agents.update("a", {}, soul="# new soul")
    assert ag["config_revision"] == 3 and hub.agents.soul("a") == "# new soul"
    revs = hub.store.list_revisions("a")
    assert [r["revision"] for r in revs] == [1, 2, 3]
    assert revs[1]["config_snapshot"]["model"] == "haiku"
    with pytest.raises(HubError):
        hub.agents.update("a", {"id": "zzz"})
    with pytest.raises(HubError):
        hub.agents.update("a", {"model": "nope"})
    assert hub.agents.show("a")["config_revision"] == 3


def test_soft_delete_and_restore(hub, hub_home):
    hub.agents.create({"id": "x", "name": "X"}, soul="# soul x")
    (hub_home.agent_home("x") / "skills" / "s1").mkdir()
    ag = hub.agents.remove("x")
    assert ag["deleted_at"] and ag["archived_at"] and ag["purge_after"]
    assert not hub_home.agent_home("x").exists()
    assert Path(ag["trash_path"]).is_dir() and str(hub_home.trash_dir) in ag["trash_path"]
    assert [r["id"] for r in hub.agents.list()] == []
    assert [r["id"] for r in hub.agents.list(include_archived=True)] == ["x"]
    with pytest.raises(Conflict, match="thùng rác"):
        hub.agents.create({"id": "x", "name": "X2"})
    with pytest.raises(NotFound):
        hub.agents.get_row("x")
    ag = hub.agents.restore("x")
    assert ag["status"] == "idle" and not ag["deleted_at"]
    assert hub.agents.soul("x") == "# soul x"
    assert (hub_home.agent_home("x") / "skills" / "s1").is_dir()
    t = event_types(hub, "x")
    assert "agent.deleted" in t and "agent.restored" in t


async def test_rm_refused_with_active_run(hub):
    hub.runtime = None
    hub.runs._runtime = FakeRuntime(hold=True)
    hub.agents.create({"id": "busy", "name": "B"})
    run = hub.runs.submit("busy", "hi")
    for _ in range(10):
        await asyncio.sleep(0.01)
    with pytest.raises(Blocked, match="dừng trước"):
        hub.agents.remove("busy")
    hub.runs._runtime.release()
    await hub.runs.wait(run["id"], timeout=5)
    hub.agents.remove("busy")


def test_pause_resume(hub):
    hub.agents.create({"id": "p", "name": "P"})
    assert hub.agents.set_paused("p", True)["status"] == "paused"
    with pytest.raises(Blocked, match="paused"):
        hub.runs.submit("p", "hi")
    assert hub.agents.set_paused("p", False)["status"] == "idle"
