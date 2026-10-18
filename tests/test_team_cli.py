"""CLI M3 qua daemon thật (tiến trình con, FakeRuntime): team/task/report/cost/run revert/estop --team/agent clone."""

import json

import pytest
import yaml

from agent_hub.cli import main
from agent_hub.daemon import spawn_background


@pytest.fixture
def daemon(hub_home):
    hub_home.config.write_text(yaml.safe_dump({"runtime": {"kind": "fake"}, "tasks": {"tick_s": 0.2}}))
    spawn_background(hub_home)
    yield hub_home
    main(["daemon", "stop"])


def run_json(capsys, *args):
    capsys.readouterr()
    code = main([*args, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def test_cli_team_task_flow(daemon, capsys, tmp_path):
    for a in ("lead", "w1"):
        assert main(["agent", "create", a, "--model", "haiku"]) == 0
    assert main(["team", "create", "docs", "--lead", "lead", "--worker", "w1", "--policy", "auto_decompose=false", "--policy", "budget_usd_per_day=5"]) == 0
    code, t = run_json(capsys, "team", "show", "docs")
    assert code == 0 and t["policies"]["auto_decompose"] is False and t["policies"]["budget_usd_per_day"] == 5
    code, res = run_json(capsys, "task", "create", "Summarize", "--team", "docs", "--body", "say hi", "--wait")
    assert code == 0 and res["status"] == "done"
    code, rows = run_json(capsys, "task", "list", "--team", "docs")
    assert rows[0]["assignee_agent_id"] == "w1" and rows[0]["status"] == "done"
    run_id = run_json(capsys, "task", "show", rows[0]["id"])[1]["runs"][0]["id"]
    assert main(["run", "revert", run_id]) == 1  # không có diff (scratch) → lỗi người dùng
    assert main(["estop", "on", "--team", "docs", "--reason", "test"]) == 0
    code, t = run_json(capsys, "team", "show", "docs")
    assert t["estop"] is True
    assert main(["estop", "off", "--team", "docs"]) == 0
    code, rep = run_json(capsys, "report", "--since", "1d")
    assert rep["runs_total"] == 1
    code, cost = run_json(capsys, "cost", "--by", "team")
    assert cost["rows"][0]["team"] == "docs"
    assert main(["agent", "clone", "w1", "w2"]) == 0
    assert main(["agent", "export", "w2", str(tmp_path)]) == 0
    assert main(["agent", "import", str(tmp_path / "w2.agent.tar.gz"), "--id", "w3"]) == 0
    assert main(["team", "member", "add", "docs", "w3", "--role", "reviewer"]) == 0
    assert main(["team", "archive", "docs"]) == 0
    assert main(["team", "restore", "docs"]) == 0
