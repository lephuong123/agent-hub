"""M5-A: CLI/RPC tầng team/global + canary qua daemon thật (FakeRuntime) — `knowledge tier|versions|user|pin memory_entry`,
`learn propose --scope team`, `learn canary list|check`. Không gọi claude."""

import json

import pytest
import yaml

from agent_hub.cli import main
from agent_hub.daemon import spawn_background


@pytest.fixture
def daemon(hub_home):
    hub_home.config.write_text(yaml.safe_dump({"runtime": {"kind": "fake"}}))
    (hub_home.home / "global").mkdir(parents=True, exist_ok=True)
    (hub_home.home / "global" / "USER.md").write_text("# User profile\nThe user prefers concise answers in English.\n")
    spawn_background(hub_home)
    yield hub_home
    main(["daemon", "stop"])


def run_json(capsys, *args):
    capsys.readouterr()
    code = main([*args, "--json"])
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else None


def test_cli_tier_flow(daemon, capsys):
    for a in ("w1", "w2"):
        assert main(["agent", "create", a, "--model", "haiku"]) == 0
    assert main(["team", "create", "docs", "--worker", "w1", "--worker", "w2"]) == 0
    code, r = run_json(capsys, "learn", "propose", "w1", "--scope", "team", "--target", "memory", "--op", "add",
                       "--payload", '{action: add, content: "The docs site is built with mkdocs."}')
    # hardening: `learn propose` của NGƯỜI tầng team → áp dụng ngay (mode watch, không eval trước — quyết định coordinator);
    # nếu propose không mang danh người (H-05 của SEC) thì vẫn là deferred_no_eval (chưa agent nào có golden task, Q-23)
    human = r["status"] == "canary"
    assert code == 0 and (r.get("mode") == "watch" if human else r["status"] == "deferred_no_eval"), r
    code, t = run_json(capsys, "knowledge", "tier", "team", "docs")
    assert code == 0 and t["owner"] == "docs" and sorted(t["members"]) == ["w1", "w2"]
    code, g = run_json(capsys, "knowledge", "tier", "global")
    assert g["memory"]["USER.md"]["entries"][0]["text"].startswith("The user prefers concise")
    code, v = run_json(capsys, "knowledge", "versions", "w1")
    assert code == 0 and any(x["ref"] == "USER.md" and x["source"] == "live" for x in v["versions"])
    assert main(["knowledge", "pin", "-", "memory_entry", "concise answers", "--scope", "global"]) == 0
    code, u = run_json(capsys, "knowledge", "user")
    assert u["shared"][0]["pinned"] is True
    code, c = run_json(capsys, "learn", "canary", "list", "--all")
    assert code == 0 and ([x["mode"] for x in c] == ["watch"] if human else c == [])
    code, c = run_json(capsys, "learn", "canary", "check")
    assert code == 0 and ([x["action"] for x in c] == ["watching"] if human else c == [])
