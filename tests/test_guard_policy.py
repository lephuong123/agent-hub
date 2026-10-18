"""FR-SG-02/05 — tài nguyên được bảo vệ (chỉ người sửa qua CLI), CLI approvals/guard."""

from __future__ import annotations

import json

import pytest
import yaml

from agent_hub.cli import main
from agent_hub.daemon import spawn_background
from agent_hub.guard.policy import PolicyStore, path_selector_matches


def cli(capsys, *args):
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def daemon(hub_home):
    hub_home.config.write_text(yaml.safe_dump({"runtime": {"kind": "fake"}}))
    spawn_background(hub_home)
    yield hub_home
    main(["daemon", "stop"])


def test_selector_matching():
    assert path_selector_matches("/data/prod/**", "/data/prod/a/b.csv")
    assert path_selector_matches("/data/prod", "/data/prod/x")
    assert not path_selector_matches("/data/prod", "/data/production")
    assert path_selector_matches("**/.env*", "/repo/sub/.env.local")
    assert path_selector_matches("/home/u/.claude*", "/home/u/.claude/settings.json")


def test_policy_store_versioned_and_builtin_protected(tmp_path):
    ps = PolicyStore(tmp_path / "hub", user_home=tmp_path)
    ps.ensure()
    v0 = ps.load().version
    r = ps.add("path", "/data/prod/**", ["delete", "overwrite"], "x")
    assert ps.load().version > v0 and any(x.id == r.id for x in ps.load().resources)
    assert list((tmp_path / "hub" / "policy" / "history").iterdir())
    with pytest.raises(ValueError):
        ps.remove("hub-policy")  # mặc định không gỡ được
    ps.remove(r.id)
    assert all(x.id != r.id for x in ps.load().resources)
    with pytest.raises(ValueError):
        ps.add("path", "/x", ["nuke"])


def test_cli_guard_and_approvals(daemon, capsys):
    code, out, _ = cli(capsys, "guard", "test", "rm -rf /", "--json")
    v = json.loads(out)
    assert code == 0 and v["verdict"] == "destructive" and v["hold"]
    code, out, _ = cli(capsys, "guard", "test", "ls -la")
    assert "an toàn" in out
    code, out, _ = cli(capsys, "guard", "protect", "add", "--kind", "path", "--selector", "/data/prod/**", "--note", "prod", "--json")
    pr = json.loads(out)
    assert code == 0 and pr["id"].startswith("pr_")
    code, out, _ = cli(capsys, "guard", "test", "cp a /data/prod/x.csv", "--json")
    assert json.loads(out)["protected_id"] == pr["id"]
    code, out, _ = cli(capsys, "guard", "protect", "list")
    assert "/data/prod/**" in out and "hub-policy" in out
    code, _, err = cli(capsys, "guard", "protect", "rm", "hub-policy")
    assert code == 1 and "mặc định" in err
    code, out, _ = cli(capsys, "approvals")
    assert code == 0 and "không có yêu cầu" in out
    code, _, err = cli(capsys, "approve", "ap_khongco")
    assert code == 1
    code, out, _ = cli(capsys, "notify", "test")
    assert code == 1 and "chưa cấu hình" in out
    cli(capsys, "agent", "create", "x")
    cli(capsys, "agent", "rm", "x")
    code, _, err = cli(capsys, "agent", "purge", "x", "--confirm", "sai")
    assert code == 1 and "gõ lại id" in err
    code, out, _ = cli(capsys, "agent", "purge", "x", "--confirm", "x")
    assert code == 0 and "đã purge" in out
