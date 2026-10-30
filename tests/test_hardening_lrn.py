"""Hardening luồng LRN (docs/plan/hardening.md): mỗi test tái hiện đúng kịch bản QA (`scratchpad/audit/repro_*.py`,
`scripts/lock_race.py`) — đỏ trước khi sửa, xanh sau. Không gọi claude."""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cfharness.errors import Blocked, Conflict
from cfharness.events import iso
from cfharness.hub import Hub
from cfharness.learning.ledger import Change
from m3_fake import ScriptedRuntime
from m4b_fake import settle


class PassGate:
    def check(self, p):
        return {"status": "passed", "scores": {"before": 0.9, "after": 0.9}}

    def post_apply(self, e):
        return {"status": "passed"}


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {"text": "ok"}))
    h.agents.create({"id": "r1", "name": "R1", "model": "sonnet"})
    h.evals.gate = PassGate()
    h.learning.policy.set("rate.cooldown_per_target_h", 0)
    h.events = []  # type: ignore[attr-defined]
    h.bus.add_listener(lambda ev: h.events.append(ev))
    yield h
    h.runs.shutting_down = True
    h.store.close()


def cfg_change(h, changes, actor="review", soul=None, agent="r1", risk="L2"):
    ch = Change(scope="agent", owner_id=agent, target="config", target_ref=",".join(sorted(list(changes) + (["soul"] if soul is not None else []))),
                op="patch", actor=actor, kind="config", config_changes=changes, soul=soul, agent_id=agent, risk_class=risk)
    return h.learning.ledger.apply(ch)


def human_warnings(h):
    return [e for e in h.events if e.type == "regression.detected" and e.payload.get("human_change")]


# ============================================================================ H-04: rollback cấu hình bằng Ledger THẬT
def test_h04_repro_cfg_rollback_keeps_human_model(hub):
    """repro_cfg.py: review đổi effort; người sửa model=haiku trực tiếp; rollback tự động entry effort KHÔNG được đưa model về sonnet."""
    r = hub.learning.pipeline.propose(target="config", op="patch", agent_id="r1", payload={"changes": {"effort": "medium"}}, created_by="review")
    assert r["status"] == "applied"
    hub.agents.update("r1", {"model": "haiku"})  # người sửa trực tiếp
    hub.learning.ledger.rollback(r["ledger_entry_id"], actor="regression_monitor", reason="test")
    a = hub.store.get_agent("r1")
    assert a["model"] == "haiku" and a["effort"] is None


def test_h04_repro_cfg2_user_entry_then_auto_rollback(hub):
    """repro_cfg2.py: entry review(effort) rồi entry user(model) qua pipeline; rollback tự động entry review giữ model của người."""
    r1 = hub.learning.pipeline.propose(target="config", op="patch", agent_id="r1", payload={"changes": {"effort": "low"}}, created_by="review")
    r2 = hub.learning.pipeline.propose(target="config", op="patch", agent_id="r1", payload={"changes": {"model": "haiku"}}, created_by="user")
    assert r1["status"] == r2["status"] == "applied"
    hub.learning.ledger.rollback(r1["ledger_entry_id"], actor="regression_monitor", reason="t")
    a = hub.store.get_agent("r1")
    assert a["model"] == "haiku" and a["effort"] is None


def test_h04_soul_restored_only_when_entry_changed_soul(hub):
    soul_p = Path(hub.store.get_agent("r1")["home_path"]) / "SOUL.md"
    e = cfg_change(hub, {"effort": "low"})
    soul_p.write_text("Human persona v2\n")  # người sửa SOUL sau entry (entry không đổi SOUL)
    hub.learning.ledger.rollback(e["id"], actor="eval_gate", reason="t")
    assert soul_p.read_text() == "Human persona v2\n" and hub.store.get_agent("r1")["effort"] is None
    e2 = cfg_change(hub, {}, soul="Learned persona\n")
    hub.learning.ledger.rollback(e2["id"], actor="regression_monitor", reason="t")
    assert soul_p.read_text() == "Human persona v2\n"


def test_h04_auto_rollback_conflicts_when_human_edited_same_key(hub):
    e = cfg_change(hub, {"effort": "low"})
    hub.agents.update("r1", {"effort": "high"})  # người sửa CÙNG khoá
    n = hub.store.conn.execute("SELECT count(*) FROM ledger_entries").fetchone()[0]
    with pytest.raises(Conflict) as ex:
        hub.learning.ledger.rollback(e["id"], actor="regression_monitor", reason="hồi quy")
    assert ex.value.data["human_change"] and ex.value.data["drifted"] == ["effort"]
    assert hub.store.get_agent("r1")["effort"] == "high"  # không ghi
    assert hub.store.conn.execute("SELECT count(*) FROM ledger_entries").fetchone()[0] == n
    w = human_warnings(hub)
    assert w and w[-1].payload["source"] == "regression_monitor" and w[-1].payload["ledger_entry_id"] == e["id"]
    from cfharness.notify import format_message

    assert "không tự hoàn tác" in format_message(w[-1]).lower()
    # người vẫn rollback được bằng --force
    with pytest.raises(Conflict):
        hub.learning.ledger.rollback(e["id"], actor="user")
    hub.learning.ledger.rollback(e["id"], actor="user", force=True)
    assert hub.store.get_agent("r1")["effort"] is None


def test_h04_auto_never_rolls_back_user_entry_even_in_chain(hub):
    e1 = cfg_change(hub, {"effort": "low"}, actor="review")
    e2 = cfg_change(hub, {"effort": "high"}, actor="user")  # người đổi cùng khoá QUA ledger
    for actor in ("regression_monitor", "eval_gate", "canary"):
        with pytest.raises(Conflict) as ex:
            hub.learning.ledger.rollback(e1["id"], actor=actor, reason="t", chain=True)
        assert ex.value.data.get("human_change")
        with pytest.raises(Conflict):
            hub.learning.ledger.rollback(e2["id"], actor=actor, reason="t")
    assert hub.store.get_agent("r1")["effort"] == "high"
    assert hub.learning.ledger.get(e1["id"])["rolled_back_by"] is None and hub.learning.ledger.get(e2["id"])["rolled_back_by"] is None
    # chuỗi theo KHOÁ trùng: người rollback e1 phải rollback chuỗi
    with pytest.raises(Conflict) as ex:
        hub.learning.ledger.rollback(e1["id"], actor="user")
    assert e2["id"] in ex.value.data["chain"]


def _add_run(h, rid, when, status="completed", correction=False, agent="r1"):
    h.store.insert_run({"id": rid, "agent_id": agent, "kind": "adhoc", "prompt": "x", "status": status, "outcome": status, "queued_at": iso(when),
                        "ended_at": iso(when + timedelta(seconds=5)), "cost_usd": 0.01})
    if correction:
        h.store.conn.execute("INSERT INTO signals (id, agent_id, run_id, kind, source, evidence, created_at) VALUES (?,?,?,?,?,?,?)",
                             ("sg_" + rid, agent, rid, "correction", "cli", "{}", iso(when + timedelta(seconds=10))))


async def test_h04_regression_monitor_real_ledger_human_edit_between(hub):
    """Thay FakeLedger: regression monitor + Ledger thật, người sửa model xen giữa → rollback chỉ effort."""
    now = datetime.now(timezone.utc)
    _add_run(hub, "b0", now - timedelta(days=1))
    e = cfg_change(hub, {"effort": "low"}, actor="review")
    hub.agents.update("r1", {"model": "haiku"})
    _add_run(hub, "w0", now + timedelta(seconds=1), status="failed")
    _add_run(hub, "w1", now + timedelta(seconds=2), status="failed")
    acts = await hub.learning_ops.regression.check_agent("r1")
    assert acts[0]["action"] == "rollback" and acts[0]["entry_id"] == e["id"]
    a = hub.store.get_agent("r1")
    assert a["model"] == "haiku" and a["effort"] is None


async def test_h04_regression_monitor_real_ledger_same_key_warns_once(hub):
    now = datetime.now(timezone.utc)
    _add_run(hub, "b0", now - timedelta(days=1))
    e = cfg_change(hub, {"model": "haiku"}, actor="review")
    hub.agents.update("r1", {"model": "opus"})  # người sửa cùng khoá
    _add_run(hub, "w0", now + timedelta(seconds=1), status="failed")
    _add_run(hub, "w1", now + timedelta(seconds=2), status="failed")
    acts = await hub.learning_ops.regression.check_agent("r1")
    assert acts[0]["action"] == "warned_human" and hub.store.get_agent("r1")["model"] == "opus"
    assert hub.learning.ledger.get(e["id"])["rolled_back_by"] is None
    assert hub.learning_ops.regression.list("r1")[0]["status"] == "warned"
    assert len(human_warnings(hub)) == 1
    assert await hub.learning_ops.regression.check_agent("r1") == []  # không lặp, không spam Slack
    assert len(human_warnings(hub)) == 1


def _model_script(spec):
    good = spec.model in ("sonnet", "haiku")
    return {"text": "ANSWER: 42" if good else "I am not sure", "cost": 0.001}


@pytest.fixture
def ehub(hub_home):
    h = Hub(hub_home, runtime=ScriptedRuntime(_model_script))
    h.config.set("eval.baseline_cache_h", 0)
    h.start()
    h.agents.create({"id": "a", "name": "A", "model": "sonnet"})
    for i in range(2):
        h.evals.golden.add({"agent_id": "a", "prompt": f"Q{i}: 6*7?", "checker": {"kind": "regex", "pattern": r"\b42\b"}}, created_by="user:cli")
    h.events = []  # type: ignore[attr-defined]
    h.bus.add_listener(lambda ev: h.events.append(ev))
    yield h
    h.runs.shutting_down = True
    h.store.close()


async def test_h04_eval_gate_real_ledger_restores_only_entry_keys(ehub):
    e = cfg_change(ehub, {"model": "opus"}, actor="review", agent="a")
    ehub.agents.update("a", {"effort": "high"})  # người sửa khoá khác xen giữa
    v = await ehub.evals.gate.post_apply(e)
    assert v.status == "eval_failed" and v.rolled_back == [e["id"]]
    a = ehub.store.get_agent("a")
    assert a["model"] == "sonnet" and a["effort"] == "high"


async def test_h04_eval_gate_real_ledger_human_same_key_not_overwritten(ehub):
    e = cfg_change(ehub, {"effort": "low"}, actor="review", agent="a")
    ehub.agents.update("a", {"effort": "high", "model": "opus"})  # người sửa cùng khoá (và làm eval tụt)
    v = await ehub.evals.gate.post_apply(e)
    assert v.status == "eval_failed" and v.rolled_back == []
    a = ehub.store.get_agent("a")
    assert a["effort"] == "high" and a["model"] == "opus"
    assert human_warnings(ehub) and human_warnings(ehub)[-1].payload["source"] == "eval_gate"


# ============================================================================ canary không rollback actor=user (quyết định coordinator)
FACT = "The team release train leaves every Thursday at 15:00 Hanoi time."


@pytest.fixture
def chub(hub_home):
    from m5a_fake import make_hub

    h = make_hub(hub_home)
    yield h
    h.store.close()


def test_canary_user_team_change_applies_to_all_and_is_never_rolled_back(chub):
    from m5a_fake import add_run, prompt_of

    now = datetime.now(timezone.utc)
    for i in range(5):
        add_run(chub, "a1", f"b{i}", now - timedelta(days=1, minutes=i))
    r = chub.learning.pipeline.propose(target="memory", op="add", scope="team", team_id="t1", payload={"action": "add", "content": FACT}, created_by="user")
    assert r["status"] in ("applied", "canary"), r
    for a in ("a1", "a2", "a3"):  # áp dụng NGAY cho cả team (không ghim bản cũ)
        assert FACT in prompt_of(chub, a)
    c = next(x for x in chub.learning.canary.list(None) if x.get("proposal_id") == r["proposal_id"])
    assert c["mode"] == "watch"
    add_run(chub, "a1", "w1", correction=True)
    add_run(chub, "a2", "w2", correction=True)
    add_run(chub, "a3", "w3", correction=True)
    res = chub.learning.canary.check(c["id"])  # Signal đã kích hoạt kiểm tự động → có thể đã kết thúc
    assert res["action"] == "warned_human" or res.get("status") == "warned", res
    assert FACT in (chub.paths.home / "teams" / "t1" / "memory.md").read_text()
    assert not [e for e in chub.events if e.type == "learning.rolled_back"]
    w = [e for e in chub.events if e.type == "regression.detected" and e.payload.get("human_change")]
    assert w and w[-1].payload["source"] == "canary"
    assert chub.learning.canary.get(c["id"])["status"] == "warned"


def test_canary_ledger_refuses_user_entry_directly(chub):
    r = chub.learning.pipeline.propose(target="memory", op="add", scope="team", team_id="t1", payload={"action": "add", "content": FACT}, created_by="user")
    with pytest.raises(Conflict):
        chub.learning.ledger.rollback(r["ledger_entry_id"], actor="canary", reason="t")
    assert FACT in (chub.paths.home / "teams" / "t1" / "memory.md").read_text()


# ============================================================================ H-21: race khoá repo (scripts/lock_race.py)
def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "sub").mkdir(parents=True)
    (r / "a.txt").write_text("x")
    (r / "sub" / "b.txt").write_text("y")
    _git(r, "init", "-q")
    _git(r, "-c", "user.email=a@b", "-c", "user.name=a", "add", "-A")
    _git(r, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "i")
    return r


async def test_h21_lock_race_second_task_never_spawned(hub_home, repo):
    rt = ScriptedRuntime(lambda spec: {"hold": True})
    h = Hub(hub_home, runtime=rt)
    h.config.set("tasks.tick_s", 0.05)
    h.start()
    try:
        h.agents.create({"id": "c1", "name": "C1"})
        h.agents.create({"id": "c2", "name": "C2"})
        t1 = h.tasks.create({"assignee": "c1", "title": "a", "body": "a", "workspace": str(repo)})
        t2 = h.tasks.create({"assignee": "c2", "title": "b", "body": "b", "workspace": str(repo / "sub")})
        await settle(lambda: h.tasks.row(t1["id"])["status"] == "running" and h.tasks.row(t2["id"])["wait_reason"] == "workspace_locked")
        # task "running" ≠ run đã spawn: run còn `queued` trong lúc checkpoint git (chậm trên Windows) → chờ runtime.start (gate tồn
        # tại), nếu không release() rơi vào khoảng chưa có gate, mất tín hiệu và run giữ khoá mãi (nguyên nhân test chập chờn)
        rid1 = h.tasks.row(t1["id"])["current_run_id"]
        await settle(lambda: h.store.get_run(rid1)["status"] == "running" and rid1 in rt.gates, timeout=15.0)
        await asyncio.sleep(0.5)  # vài tick nữa
        n = lambda tid: h.store.conn.execute("SELECT count(*) FROM runs WHERE task_id=?", (tid,)).fetchone()[0]  # noqa: E731
        r2 = h.tasks.row(t2["id"])
        assert n(t2["id"]) == 0, h.store.conn.execute("SELECT id,status,error FROM runs WHERE task_id=?", (t2["id"],)).fetchall()
        assert r2["status"] == "ready" and int(r2.get("consecutive_failures") or 0) == 0 and int(r2.get("attempts") or 0) == 0
        rt.release(rid1)
        await settle(lambda: h.tasks.row(t2["id"])["status"] == "running")
        assert n(t2["id"]) == 1
    finally:
        h.runs.shutting_down = True
        h.store.close()


async def test_h21_lock_reserved_synchronously_in_tick_and_released_on_early_failure(hub_home, repo):
    rt = ScriptedRuntime(lambda spec: {"hold": True})
    h = Hub(hub_home, runtime=rt)
    try:
        h.agents.create({"id": "c1", "name": "C1"})
        h.agents.create({"id": "c2", "name": "C2"})
        r1 = h.runs.submit("c1", "x", cwd=str(repo))
        assert list(h.runs.locks.list()) == [str(repo.resolve())]  # giữ ngay trong tick, trước khi coroutine chạy
        r2 = h.runs.submit("c2", "y", cwd=str(repo / "sub"))
        assert h.store.get_run(r2["id"])["status"] == "queued"  # chờ, không spawn rồi chết
        await settle(lambda: h.store.get_run(r1["id"])["status"] == "running")
        rt.release(r1["id"])
        await settle(lambda: h.store.get_run(r2["id"])["status"] == "running")
        rt.release(r2["id"])
        await settle(lambda: h.store.get_run(r2["id"])["status"] == "completed")
        assert h.runs.locks.list() == {}

        # run kết thúc sớm (lỗi trước spawn) → nhả khoá
        def boom(*a, **kw):
            raise OSError("spawn lỗi")

        h.runs._prepare_spec = boom  # type: ignore[method-assign]
        r3 = h.runs.submit("c1", "z", cwd=str(repo))
        await settle(lambda: h.store.get_run(r3["id"])["status"] == "failed")
        assert h.runs.locks.list() == {}
    finally:
        h.runs.shutting_down = True
        h.store.close()


# ============================================================================ H-19: ngân sách ngày trừ run đang chạy
async def test_h19_parallel_runs_cannot_exceed_daily_budget(hub_home):
    rt = ScriptedRuntime(lambda spec: {"hold": True})
    h = Hub(hub_home, runtime=rt)
    try:
        h.config.set("budget.hub_usd_per_day", 1.0)
        h.config.set("runs.default_max_budget_usd", 0.4)
        h.config.set("hub.max_concurrent_runs", 8)
        for i in range(4):
            h.agents.create({"id": f"p{i}", "name": f"P{i}"})
        caps = [h.runs.submit(f"p{i}", "x")["max_budget_usd"] for i in range(3)]
        assert caps == [0.4, 0.4, 0.2]
        with pytest.raises(Blocked):
            h.runs.submit("p3", "x")
        assert sum(caps) <= 1.0
        # team + agent cũng trừ phần đang giữ chỗ
        h.config.set("budget.hub_usd_per_day", None)
        h.agents.update("p3", {"limits": {"budget_usd_per_day": 0.5}})
        h.config.set("hub.max_concurrent_runs", 8)
        h.agents.update("p3", {"limits": {"max_concurrent_runs": 3, "budget_usd_per_day": 0.5}})
        c1 = h.runs.submit("p3", "a")["max_budget_usd"]
        c2 = h.runs.submit("p3", "b")["max_budget_usd"]
        assert (c1, c2) == (0.4, 0.1)
    finally:
        h.runs.shutting_down = True
        h.store.close()


# ============================================================================ H-17: L1 không golden → unevaluated (repro_l1.py)
async def test_h17_l1_without_golden_flagged_unevaluated(hub_home):
    h = Hub(hub_home)
    try:
        h.agents.create({"id": "r1", "name": "R1", "model": "sonnet"})
        r = h.learning.pipeline.propose(target="memory", op="add", agent_id="r1", payload={"action": "add", "content": "The repo uses pnpm workspaces."}, created_by="review")
        assert r["status"] == "applied" and r["risk_class"] == "L1"
        out = await h.evals.gate.run_batch_l1()
        assert [o["status"] for o in out] == ["deferred_no_eval"]
        await h.learning_ops.regression.sync_watches()
        assert "unevaluated" in h.learning.ledger.get(r["ledger_entry_id"])["flags"]
        assert [x[0] for x in h.store.conn.execute("SELECT unevaluated FROM regression_watch")] == [1]
    finally:
        h.store.close()


async def test_h17_flag_set_even_if_watch_exists(hub_home):
    h = Hub(hub_home)
    try:
        h.agents.create({"id": "r1", "name": "R1", "model": "sonnet"})
        r = h.learning.pipeline.propose(target="memory", op="add", agent_id="r1", payload={"action": "add", "content": "Use pnpm."}, created_by="review")
        await h.learning_ops.regression.sync_watches()  # watch có trước lô eval
        await h.evals.gate.run_batch_l1()
        assert [x[0] for x in h.store.conn.execute("SELECT unevaluated FROM regression_watch WHERE entry_id=?", (r["ledger_entry_id"],))] == [1]
    finally:
        h.store.close()


# ============================================================================ H-15: charter team trong system prompt
def test_h15_charter_in_system_prompt_ordered_and_stable(hub):
    from cfharness.learning.runtime import system_prompt_for

    hub.teams.create({"id": "docs", "name": "Docs", "charter": "Write accurate docs. Cite sources.", "members": [{"agent_id": "r1", "role": "worker"}]})
    soul_p = Path(hub.store.get_agent("r1")["home_path"]) / "SOUL.md"
    soul_p.write_text("I am R1 persona.\n")
    hub.knowledge.memory_path("agent", "r1", "memory").parent.mkdir(parents=True, exist_ok=True)
    hub.learning.pipeline.propose(target="memory", op="add", agent_id="r1", payload={"action": "add", "content": "Repo uses pnpm."}, created_by="review")
    agent = hub.store.get_agent("r1")
    text, _ = system_prompt_for(hub, {"id": "r_1", "team_id": "docs"}, agent)
    i_soul, i_ch, i_mem = text.index("I am R1 persona."), text.index("Write accurate docs."), text.index("Repo uses pnpm.")
    assert text.index("cfharness conventions") < i_soul < i_ch < i_mem
    text2, _ = system_prompt_for(hub, {"id": "r_2", "team_id": "docs"}, agent)
    assert text2 == text  # ổn định → giữ prompt cache
    solo, _ = system_prompt_for(hub, {"id": "r_3"}, agent)
    assert "Write accurate docs." not in solo
    tk = {"id": "t_x", "team_id": "docs", "title": "x", "body": "y"}
    assert "Write accurate docs." not in hub.tasks.worker_prompt(tk, "r1", hub_self=True)  # charter KHÔNG còn trong tin user


async def test_h15_system_prompt_snapshot_on_by_default(hub):
    from cfharness.runtime.claude_cli import build_argv

    assert hub.config.get("runtime.claude.system_prompt_snapshot") == "on"
    spec = hub.runs._prepare_spec(hub.store.get_run(hub.runs.submit("r1", "hi")["id"]) or {}, hub.store.get_agent("r1"))
    argv, _ = build_argv(spec, hub.config.get("runtime.claude"))
    assert argv[argv.index("--system-prompt-snapshot") + 1] == "on"


# ============================================================================ H-09: retention 30 ngày (giả thời gian)
def test_h09_retention_purges_old_data_keeps_aggregates(hub):
    from cfharness.approvals import audit_record
    from cfharness.recovery import purge_retention

    store = hub.store
    store.insert_run({"id": "r_old", "agent_id": "r1", "kind": "adhoc", "prompt": "x", "status": "completed", "outcome": "completed",
                      "queued_at": iso(datetime.now(timezone.utc)), "cost_usd": 0.5})
    from cfharness.events import make_event

    hub.publish(make_event("text", "r1", {"text": "old transcript"}, run_id="r_old"))
    e = cfg_change(hub, {"effort": "low"})
    audit_record("test.kind", "r1", None, {"x": 1})
    runs_dir = hub.paths.runs_dir
    runs_dir.mkdir(parents=True, exist_ok=True)
    old_raw = runs_dir / "r_old.jsonl.gz"
    old_raw.write_bytes(gzip.compress(b'{"type":"x"}\n'))
    (runs_dir / "r_old.stderr.log").write_text("err")
    n_events = store.conn.execute("SELECT count(*) FROM events").fetchone()[0]
    assert n_events > 0
    # append-only: xoá ngoài job retention bị chặn
    with pytest.raises(Exception):
        store.conn.execute("DELETE FROM events")
    # skill xoá mềm + team xoá mềm (purge_after = +30 ngày, Q-17)
    from cfharness.store import purge_after

    arch = hub.paths.home / "agents" / "r1" / "skills" / ".archive" / "s1__x"
    arch.mkdir(parents=True)
    (arch / "SKILL.md").write_text("old")
    hub.knowledge.register_skill("agent", "r1", "s1", created_by="agent", description="d")
    hub.knowledge.mark_archived("agent", "r1", "s1", str(arch))
    trash = hub.paths.trash_dir / "teams" / "tz__x"
    trash.mkdir(parents=True)
    hub.teams.create({"id": "tz", "name": "TZ"})
    store._update("teams", "id", "tz", {"archived_at": iso(datetime.now(timezone.utc)), "deleted_at": iso(datetime.now(timezone.utc)),
                                        "purge_after": purge_after(30), "trash_path": str(trash)})
    team = store.conn.execute("SELECT trash_path, purge_after FROM teams WHERE id='tz'").fetchone()

    # hôm nay: chưa quá hạn → không xoá gì
    out = purge_retention(hub)
    assert out["events"] == 0 and old_raw.exists()
    # giả thời gian: 31 ngày sau
    future = datetime.now(timezone.utc) + timedelta(days=31)
    newer = runs_dir / "r_new.jsonl.gz"
    newer.write_bytes(b"x")
    os.utime(newer, (future.timestamp() - 3600, future.timestamp() - 3600))
    out = purge_retention(hub, now=future)
    assert out["events"] >= n_events
    assert store.conn.execute("SELECT count(*) FROM events WHERE ts < ?", (iso(future - timedelta(days=30)),)).fetchone()[0] == 0
    assert not old_raw.exists() and not (runs_dir / "r_old.stderr.log").exists() and newer.exists()
    # giữ: cost tổng hợp (runs), ledger, audit, guard_decisions
    assert store.get_run("r_old")["cost_usd"] == 0.5
    assert hub.learning.ledger.get(e["id"])
    assert store.conn.execute("SELECT count(*) FROM audit_log WHERE kind='test.kind'").fetchone()[0] == 1
    # team xoá mềm quá purge_after → purge thùng rác
    t2 = store.conn.execute("SELECT trash_path, purge_after FROM teams WHERE id='tz'").fetchone()
    assert t2["trash_path"] is None and not trash.exists()
    assert not arch.exists()
    sk = store.conn.execute("SELECT state, archived_path, purge_after FROM skills WHERE name='s1'").fetchone()
    assert sk["state"] == "deleted" and sk["archived_path"] is None and sk["purge_after"] is None
    assert store.conn.execute("SELECT count(*) FROM audit_log WHERE kind='retention.purged'").fetchone()[0] >= 1
    # retention=forever (0) → không xoá
    hub.config.set("retention.events_days", 0)
    hub.publish(make_event("text", "r1", {"text": "x"}))
    assert purge_retention(hub, now=future + timedelta(days=400))["events"] == 0


def test_h09_purge_expired_runs_retention_throttled(hub, monkeypatch):
    from cfharness import recovery

    calls = []
    monkeypatch.setattr(recovery, "purge_retention", lambda h, now=None: calls.append(now) or {})
    recovery.purge_expired(hub)
    recovery.purge_expired(hub)
    assert len(calls) == 1  # tối đa 1 lần / retention.interval_s


# ============================================================================ NFR-22 (yêu cầu coordinator): LLM judge qua AgentRuntime.oneshot
async def test_judge_goes_through_runtime_oneshot(hub, monkeypatch):
    from cfharness.evals import judge as judge_mod
    from cfharness.evals.judge import JUDGE_SCHEMA, JUDGE_SYSTEM
    from cfharness.runtime import ClaudeCliRuntime, OneShotResult

    calls = []

    async def fake_oneshot(self, req):
        calls.append((self.cfg, req))
        return OneShotResult(True, {"score": 0.8, "pass": True, "reason": "fine"}, 0.002, 12, "", {})

    async def no_spawn(*a, **kw):  # judge KHÔNG được tự spawn `claude`
        raise AssertionError("judge spawn trực tiếp")

    monkeypatch.setattr(ClaudeCliRuntime, "oneshot", fake_oneshot)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)
    hub.config.set("eval.judge", {**hub.config.get("eval.judge"), "binary": "/opt/judge-claude", "model": "haiku"})
    g = await judge_mod.judge(hub, agent_id="r1", spec={"kind": "llm_judge", "rubric": "be nice", "pass_threshold": 0.7}, prompt="p",
                              output_text="o", budget_usd=0.05, eval_id="ev1", golden_task_id="g1")
    assert g.passed and g.score == 0.8 and g.cost_usd == 0.002
    cfg, req = calls[0]
    assert cfg["binary"] == "/opt/judge-claude" and cfg.get("settings") == hub.config.get("runtime.claude").get("settings")
    assert req.schema == JUDGE_SCHEMA and req.system == JUDGE_SYSTEM and req.model == "haiku" and req.budget_usd == 0.05
    assert "<agent_output>" in req.prompt
    row = hub.store.get_run(g.judge_run_ids[0])
    assert row["kind"] == "eval_judge" and row["cost_usd"] == 0.002


# ============================================================================ retention checkpoints/ (yêu cầu coordinator)
def test_checkpoints_retention_keeps_live_and_recent(hub):
    from cfharness.recovery import purge_retention

    now = datetime.now(timezone.utc)
    base = hub.paths.home / "checkpoints"
    for rid, status in (("r_done", "completed"), ("r_wait", "waiting_human"), ("r_int", "interrupted")):
        hub.store.insert_run({"id": rid, "agent_id": "r1", "kind": "adhoc", "prompt": "x", "status": status, "outcome": status,
                              "queued_at": iso(now), "ended_at": iso(now), "diff": {"kind": "git", "files": [], "revertible": True}})
        (base / rid).mkdir(parents=True)
        (base / rid / "diff.patch").write_text("x")
    (base / "r_orphan").mkdir()
    assert purge_retention(hub)["checkpoints"] == 0  # chưa quá 30 ngày
    out = purge_retention(hub, now=now + timedelta(days=31))
    assert out["checkpoints"] == 2
    assert not (base / "r_done").exists() and not (base / "r_orphan").exists()
    assert (base / "r_wait").exists() and (base / "r_int").exists()  # run còn có thể resume/đối soát → giữ
    d = hub.store.get_run("r_done")["diff"]
    assert d["revertible"] is False and d.get("checkpoint_purged_at")
    from cfharness.dispatch.rpc import OrchestrationRpc
    from cfharness.errors import HubError

    class _D:
        pass

    dd = _D()
    dd.hub, dd.methods = hub, {}
    with pytest.raises(HubError) as ex:
        OrchestrationRpc(dd).run_revert({"id": "r_done"})  # type: ignore[arg-type]
    assert "retention" in ex.value.message
