"""FR-RT-05 (resume), FR-TK-14 (`run continue`), chi phí theo chênh lệch luỹ kế session (spike A-03)."""

from __future__ import annotations

import json

import pytest

from agent_hub.errors import HubError, NotFound
from agent_hub.guard.cli import rewrite_argv
from agent_hub.hub import Hub
from agent_hub.runtime import FakeRuntime, build_argv, default_fixture


def fixture_with_cost(tmp_path, name: str, total: float):
    out = []
    for ln in default_fixture().read_text().splitlines():
        if ln.strip():
            o = json.loads(ln)
            if o.get("type") == "result":
                o["total_cost_usd"] = total
            out.append(json.dumps(o))
    p = tmp_path / name
    p.write_text("\n".join(out) + "\n")
    return p


@pytest.fixture
def hub_rt(hub_home, tmp_path):
    rt = FakeRuntime(fixture_seq=[fixture_with_cost(tmp_path, "a.jsonl", 0.01), fixture_with_cost(tmp_path, "b.jsonl", 0.025), fixture_with_cost(tmp_path, "c.jsonl", 0.03)])
    h = Hub(hub_home, runtime=rt)
    h.start()
    h.agents.create({"id": "a", "name": "A"})
    yield h, rt
    h.store.close()


async def test_continue_uses_resume_same_session_and_cost_delta(hub_rt, tmp_path):
    hub, rt = hub_rt
    cwd = tmp_path / "w"
    cwd.mkdir()
    r1 = hub.runs.submit("a", "first", cwd=str(cwd))
    f1 = await hub.runs.wait(r1["id"], 10)
    assert f1["cost_usd"] == pytest.approx(0.01)
    r2 = hub.runs.continue_run(r1["id"], "and then?")
    f2 = await hub.runs.wait(r2["id"], 10)
    assert f2["session_id"] == r1["session_id"] and f2["resumed_from_session"] == r1["session_id"] and f2["parent_run_id"] == r1["id"]
    assert f2["cost_usd"] == pytest.approx(0.015)  # 0.025 luỹ kế − 0.01 trước đó
    assert f2["session_cost_total_usd"] == pytest.approx(0.025)
    assert f2["cwd"] == str(cwd)
    spec = rt.started[1]
    assert spec.resume_session_id == r1["session_id"] and not spec.fork
    argv, _ = build_argv(spec, {"input_format": "stream-json"})
    assert argv[argv.index("--resume") + 1] == r1["session_id"] and "--session-id" not in argv
    # continue theo session id
    r3 = hub.runs.continue_run(r1["session_id"], "third")
    f3 = await hub.runs.wait(r3["id"], 10)
    assert f3["cost_usd"] == pytest.approx(0.005)
    assert hub.store.cost_today("a") == pytest.approx(0.03)  # tổng = luỹ kế cuối, không đếm trùng


async def test_continue_errors(hub_rt):
    hub, rt = hub_rt
    with pytest.raises(NotFound):
        hub.runs.continue_run("r_khongco", "x")
    rt.hold = True
    r1 = hub.runs.submit("a", "first")
    with pytest.raises(HubError, match="session đang bận"):
        hub.runs.continue_run(r1["id"], "x")
    import asyncio

    while not rt.gates:
        await asyncio.sleep(0.01)
    rt.release()
    await hub.runs.wait(r1["id"], 10)


def test_cli_rewrite():
    assert rewrite_argv(["run", "continue", "r_1", "hi"]) == ["run-continue", "r_1", "hi"]
    assert rewrite_argv(["--json", "run", "continue", "r_1", "hi"]) == ["--json", "run-continue", "r_1", "hi"]
    assert rewrite_argv(["agent", "purge", "x", "--confirm", "x"]) == ["agent-purge", "x", "--confirm", "x"]
    assert rewrite_argv(["run", "coder", "continue please"]) == ["run", "coder", "continue please"]
