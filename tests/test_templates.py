"""Template agent (FR-AG-07): researcher, coder, reviewer, ops."""

import pytest

from cfharness import templates
from cfharness.errors import NotFound
from cfharness.hub import Hub
from cfharness.runtime import FakeRuntime


def test_bundled_templates_listed():
    names = templates.names()
    assert {"researcher", "coder", "reviewer", "ops"} <= set(names)
    rows = {t["name"]: t for t in templates.list_templates()}
    assert rows["reviewer"]["permission_mode"] == "plan"
    assert set(rows["reviewer"]["tools"]) <= {"Read", "Grep", "Glob"}  # BRD: plan hoặc chỉ tool đọc


def test_render_and_overrides():
    cfg, soul = templates.render("reviewer", "rev1", {"model": "haiku", "limits": {"max_budget_usd_per_run": 0.2}})
    assert cfg["id"] == "rev1" and cfg["name"] == "rev1" and cfg["model"] == "haiku"
    assert cfg["limits"] == {"max_budget_usd_per_run": 0.2, "max_runtime_s": 900}  # gộp sâu
    assert cfg["tool_policy"]["permission_mode"] == "plan"
    assert "You are **rev1**" in soul and "{name}" not in soul
    assert soul.isascii() or "You" in soul  # persona tiếng Anh (Q-14)
    with pytest.raises(NotFound):
        templates.render("nope", "x")


@pytest.mark.parametrize("name", ["researcher", "coder", "reviewer", "ops"])
async def test_agent_from_template_runs_immediately(hub_home, name):
    """Agent tạo từ template hợp lệ với AgentConfig và chạy được run đầu tiên ngay (G-1: ≤ 1 phút)."""
    h = Hub(hub_home, runtime=FakeRuntime())
    h.start()
    try:
        cfg, soul = templates.render(name, f"{name}-1")
        ag = h.agents.create(cfg, soul)
        assert ag["soul"].startswith(f"# {name}-1")
        run = h.runs.submit(ag["id"], "hello")
        final = await h.runs.wait(run["id"], 15)
        assert final["outcome"] == "completed"
        if name == "reviewer":
            assert ag["tool_policy"]["permission_mode"] == "plan"
    finally:
        h.store.close()


def test_cli_template_and_steer_e2e(hub_home, capsys):
    """CLI thật qua daemon nền: `template list`, `agent create --template`, `steer` vào run đã xong → lỗi rõ (mã 1)."""
    import json

    import yaml

    from cfharness.cli import main
    from cfharness.daemon import spawn_background

    hub_home.config.write_text(yaml.safe_dump({"runtime": {"kind": "fake", "fake": {"line_delay_s": 0.0}}}))
    spawn_background(hub_home)
    try:
        assert main(["template", "list", "--json"]) == 0
        names = [t["name"] for t in json.loads(capsys.readouterr().out)]
        assert {"researcher", "coder", "reviewer", "ops"} <= set(names)
        assert main(["agent", "create", "rv", "--template", "reviewer", "--model", "haiku", "--json"]) == 0
        ag = json.loads(capsys.readouterr().out)
        assert ag["model"] == "haiku" and ag["tool_policy"]["permission_mode"] == "plan"
        assert main(["run", "rv", "hi", "--follow"]) == 0
        capsys.readouterr()
        assert main(["runs", "list", "--json"]) == 0
        rid = json.loads(capsys.readouterr().out)[0]["id"]
        assert main(["run", "steer", rid, "too late"]) == 1
        assert "run continue" in capsys.readouterr().err
    finally:
        main(["daemon", "stop"])
