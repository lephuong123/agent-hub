"""SA4: chính sách qua admind (phạm vi, gộp chặt hơn, file cho hub, rev, rollback, mô phỏng, hiệu lực) + kho tài khoản Claude."""

from __future__ import annotations

import json

import pytest

from cfharness import admin_control as AC
from cfharness.admind.runner import Result

from admind_fakes import hub_status, install_fakes
from admind_helpers import make_env

TOKEN = "sk-ant-oat01-" + "z" * 90


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)
    for d in ("control", "admin-log", "policy"):
        (tmp_path / "shared" / d).mkdir(parents=True)
    e.fakes = install_fakes(e, tmp_path)
    m = e.fakes[0]
    m.group, m.plist_set, m.ports = ["an", "binh"], {"an", "binh"}, {"an": 8721, "binh": 8723}
    m.hubs = [hub_status("an", version="0.2.20"), hub_status("binh", version="0.2.28")]
    e.make_admin("boss")
    e.ok("user.import", {"os_user": "an"}, as_="boss")
    e.ok("user.import", {"os_user": "binh"}, as_="boss")
    from cfharness.admind.collector import collect

    collect(e.state)
    yield e
    e.state.db.close()


def _uid(env, u):
    return env.state.db.one("SELECT id FROM hub_users WHERE os_user=?", (u,))["id"]


def test_scopes_merge_and_files(env):
    env.ok("policy.save", {"scope": "default", "content": {"budget": {"usd_per_day": 20}, "require": {"sandbox": True}}}, as_="boss")
    g = env.ok("group.create", {"name": "Phòng A"}, as_="boss")["id"]
    env.ok("user.update", {"id": _uid(env, "an"), "group_id": g}, as_="boss")
    env.ok("policy.save", {"scope": f"group:{g}", "content": {"budget": {"usd_per_day": 5}, "models": {"allow": ["sonnet"]}}}, as_="boss")
    env.ok("policy.save", {"scope": "machine", "content": {"min_version": "0.2.10", "policy_required": True}}, as_="boss")
    d = AC.policy_dir()
    an = json.loads((d / "an.json").read_text())
    assert an["budget"]["usd_per_day"] == 5 and an["require"]["sandbox"] is True and an["models"]["allow"] == ["sonnet"] and an["rev"] > 0
    binh = json.loads((d / "binh.json").read_text())
    assert binh["budget"]["usd_per_day"] == 20 and "models" not in binh
    assert json.loads((d / "_machine.json").read_text())["policy_required"] is True
    eff = env.ok("policy.effective", {"user": "an"}, as_="boss")
    src = {r["key"]: r["source"] for r in eff["rows"]}
    assert "Nhóm Phòng A" in src["budget.usd_per_day"] and src["min_version"] == "Toàn máy"
    assert eff["rev_written"] == an["rev"]


def test_versions_diff_rollback(env):
    env.ok("policy.save", {"scope": "default", "content": {"budget": {"usd_per_day": 20}}}, as_="boss")
    r = env.ok("policy.save", {"scope": "default", "content": {"budget": {"usd_per_day": 10}}}, as_="boss")
    assert r["rev"] == 2 and any("budget.usd_per_day" in x and "→ 10" in x for x in r["diff"])
    env.ok("policy.rollback", {"scope": "default"}, as_="boss")
    cur = env.ok("policy.get", {"scope": "default"}, as_="boss")
    assert cur["rev"] == 3 and cur["content"]["budget"]["usd_per_day"] == 20
    assert env.err("policy.save", {"scope": "default", "content": {"lạ": 1}}, as_="boss") == "invalid"
    assert env.err("policy.save", {"scope": "default", "content": {"runs": {"max_concurrent": 999}}}, as_="boss") == "invalid"


def test_simulate_blocks_old_version(env):
    sim = env.ok("policy.simulate", {"scope": "machine", "content": {"min_version": "0.2.25"}}, as_="boss")
    assert sim["affected"] == 2 and [u["os_user"] for u in sim["would_block"]] == ["an"]


def test_policy_roles(env):
    env.make_admin("opr", role="operator")
    assert isinstance(env.ok("policy.list", as_="opr"), list)
    assert env.err("policy.save", {"scope": "default", "content": {}}, as_="opr") == "forbidden"


def test_user_create_budget_goes_to_policy(env):
    env.ok("policy.save", {"scope": f"user:{_uid(env, 'an')}", "content": {"budget": {"usd_per_day": 3}}}, as_="boss")
    assert json.loads((AC.policy_dir() / "an.json").read_text())["budget"]["usd_per_day"] == 3


def test_claude_vault(env):
    m, cf, r = env.fakes
    aid = env.ok("claude.account_add", {"name": "Max 1", "token": TOKEN}, as_="boss")["id"]
    assert env.err("claude.account_add", {"name": "Hỏng", "token": "abc" * 10}, as_="boss") == "invalid"
    rows = env.ok("claude.list", as_="boss")
    assert rows[0]["name"] == "Max 1" and "token" not in json.dumps(rows) and TOKEN not in json.dumps(rows)
    r.on("Reply with just: OK", Result(0, "OK", ""))
    assert env.ok("claude.check", {"id": aid}, as_="boss")["state"] == "ok"
    env.ok("claude.assign", {"users": ["an", "binh"], "account_id": aid}, as_="boss")
    assert sum(1 for a, s in r.calls if s == TOKEN and "secret" in a) == 2  # chỉ qua stdin
    assert not any(TOKEN in " ".join(a) for a, _ in r.calls)
    assert env.err("claude.remove", {"id": aid}, as_="boss") == "conflict"
    env.ok("claude.rotate", {"id": aid, "token": TOKEN + "B"}, as_="boss")
    assert sum(1 for a, s in r.calls if s == TOKEN + "B") == 2
    dump = json.dumps(env.state.db.all("SELECT * FROM audit")) + json.dumps(env.state.db.all("SELECT * FROM claude_accounts"))
    assert TOKEN not in dump
    env.make_admin("opr", role="operator")
    assert env.err("claude.assign", {"users": ["an"], "account_id": aid}, as_="opr") == "forbidden"
    cost = env.ok("cost.report", {"by": "claude"}, as_="boss")
    assert "rows" in cost


def test_claude_old_token_alert(env):
    aid = env.ok("claude.account_add", {"name": "Cũ", "token": TOKEN}, as_="boss")["id"]
    from cfharness.admind.ops_claude import check_ages

    env.state.clock.tick(340 * 86400) if hasattr(env.state.clock, "tick") else None
    import os

    p = env.state.vault._p(f"claude.{aid}")
    os.utime(p, (env.state.clock() - 340 * 86400, env.state.clock() - 340 * 86400))
    check_ages(env.state)
    assert env.state.db.one("SELECT * FROM alerts WHERE code='claude_token_old' AND state='on'")
