"""M4-B: eval gate (FR-LN-10) — check(proposal) trước áp dụng L3, post_apply(entry) sau áp dụng, eval L1 theo lô, ngân sách,
cache baseline, bản sao home tạm (không đụng home thật), --no-session-persistence. Không gọi claude: ScriptedRuntime."""

from __future__ import annotations

from pathlib import Path

import pytest

from cfharness.hub import Hub
from m3_fake import ScriptedRuntime
from m4b_fake import attach_fake_ledger, settle, write_fake_judge


def model_script(spec):
    """Agent 'giỏi' với sonnet/haiku, 'kém' với opus; SOUL có 'GOOD-SOUL' → output có GOOD."""
    good_model = spec.model in ("sonnet", "haiku")
    text = ("ANSWER: 42" if good_model else "I am not sure") + (" GOOD" if "GOOD-SOUL" in (spec.system_prompt or "") else "")
    cost = 0.01 if spec.model == "sonnet" else 0.002
    return {"text": text, "cost": cost}


@pytest.fixture
def make_hub(hub_home):
    hubs = []

    def _make(script=model_script, **cfg):
        rt = ScriptedRuntime(script)
        h = Hub(hub_home, runtime=rt)
        h.config.set("eval.baseline_cache_h", 0)
        for k, v in cfg.items():
            h.config.set(k, v)
        h.start()
        h.agents.create({"id": "a", "name": "A", "model": "sonnet"})
        hubs.append(h)
        return h, rt

    yield _make
    for h in hubs:
        h.runs.shutting_down = True
        h.store.close()


def add_golden(h, n=2):
    out = []
    for i in range(n):
        out.append(h.evals.golden.add({"agent_id": "a", "prompt": f"Q{i}: what is 6*7?", "checker": {"kind": "regex", "pattern": r"\b42\b"}}, created_by="user:cli"))
    return out


def proposal(**changes):
    return {"id": "p1", "scope": "agent", "agent_id": "a", "target": "config", "op": "patch", "target_ref": ",".join(changes), "payload": {"changes": changes}}


async def test_check_passes_model_change_and_does_not_touch_real_home(make_hub):
    h, rt = make_hub()
    add_golden(h)
    home = Path(h.agents.get_row("a")["home_path"])
    before_files = sorted(p.name for p in home.iterdir())
    v = await h.evals.gate.check(proposal(model="haiku"))
    assert v.status == "passed", v
    assert v.scores["before"] == 1.0 and v.scores["after"] == 1.0 and v.eval_id
    assert h.agents.get_row("a")["model"] == "sonnet"  # gate KHÔNG áp dụng
    specs = list(rt.specs.values())
    assert len(specs) == 8  # 2 task × N=2 × (baseline + candidate)
    assert all(s.no_session_persistence for s in specs)
    assert all("evals/tmp" in str(s.cwd) for s in specs)
    assert sorted(s.model for s in specs).count("haiku") == 4
    assert sorted(p.name for p in home.iterdir()) == before_files and not (home / "workspaces").exists() or not any((home / "workspaces").iterdir())
    assert not (h.paths.home / "evals" / "tmp" / v.eval_id).exists()  # dọn bản sao
    runs = h.store.list_runs(agent_id="a", limit=100)
    assert {r["kind"] for r in runs} == {"eval"} and all(r["status"] == "completed" for r in runs)
    row = h.store.conn.execute("SELECT status, before_score, after_score FROM eval_runs WHERE id=?", (v.eval_id,)).fetchone()
    assert tuple(row) == ("passed", 1.0, 1.0)


async def test_check_eval_failed_records_failure_signal(make_hub):
    h, rt = make_hub()
    add_golden(h, 3)
    evs = []
    h.bus.add_listener(lambda e: evs.append(e))
    v = await h.evals.gate.check(proposal(model="opus"))
    assert v.status == "eval_failed" and v.scores["after"] == 0.0 and v.scores["drop"] == 1.0
    assert "tụt" in v.reason
    sig = h.store.conn.execute("SELECT kind, source, evidence FROM signals WHERE agent_id='a'").fetchall()
    assert [s[0] for s in sig] == ["failure"]
    done = [e for e in evs if e.type == "eval.completed"]
    assert done and done[-1].payload["status"] == "eval_failed"
    assert h.agents.get_row("a")["model"] == "sonnet"


async def test_check_without_golden_defers_and_suggests(make_hub):
    h, rt = make_hub()
    r = h.runs.submit("a", "Real task")
    await h.runs.wait(r["id"], 5)
    evs = []
    h.bus.add_listener(lambda e: evs.append(e))
    v = await h.evals.gate.check(proposal(model="haiku"))
    assert v.status == "deferred_no_eval" and "no_golden_tasks" in v.reason
    sugg = [e for e in evs if e.type == "golden.suggested"]
    assert sugg and sugg[0].payload["reason"] == "deferred_no_eval" and sugg[0].payload["candidates"][0]["run_id"] == r["id"]
    assert len(rt.specs) == 1  # không chạy eval nào


async def test_soul_candidate_only_in_candidate_runs(make_hub):
    h, rt = make_hub()
    h.evals.golden.add({"agent_id": "a", "prompt": "say", "checker": {"kind": "contains", "text": "GOOD"}})
    v = await h.evals.gate.check({"id": "p2", "scope": "agent", "agent_id": "a", "target": "config", "op": "patch", "target_ref": "soul",
                                   "candidate": {"soul": "# A\nYou are GOOD-SOUL."}})
    assert v.status == "passed" and v.scores["before"] == 0.0 and v.scores["after"] == 1.0
    assert "GOOD-SOUL" not in Path(h.agents.get_row("a")["home_path"], "SOUL.md").read_text()


async def test_baseline_cache_reused(make_hub):
    h, rt = make_hub(**{"eval.baseline_cache_h": 24})
    add_golden(h, 1)
    await h.evals.gate.check(proposal(model="haiku"))
    n1 = len(rt.specs)
    await h.evals.gate.check(proposal(effort="low"))
    assert len(rt.specs) - n1 == 2  # baseline lấy cache, chỉ chạy candidate N=2


async def test_budget_exhausted_is_deferred(make_hub):
    h, rt = make_hub(**{"eval.budget_usd": 0.015})  # sonnet 0.01/run → chỉ đủ ~2 run baseline
    add_golden(h, 1)
    v = await h.evals.gate.check(proposal(model="haiku"))
    assert v.status in ("deferred_no_eval", "passed")
    assert v.cost_usd <= 0.03
    if v.status == "deferred_no_eval":
        assert "eval_incomplete" in v.reason


async def test_golden_files_seeded_and_command_checker(make_hub):
    h, rt = make_hub(script=lambda spec: {"text": "done", "files": {"out.txt": "42\n"}})
    h.evals.golden.add({"agent_id": "a", "prompt": "write 42 to out.txt", "files": {"in/data.txt": "seed"},
                        "checker": {"kind": "command", "cmd": "test -f in/data.txt && grep -q 42 out.txt"}})
    r = await h.evals.gate.run_manual("a", n=1)
    assert r["score"] == 1.0


async def test_llm_judge_in_gate(make_hub, tmp_path):
    h, rt = make_hub(**{"eval.judge": {"model": "haiku", "budget_usd": 0.05, "timeout_s": 30, "binary": str(write_fake_judge(tmp_path))}})
    h.evals.golden.add({"agent_id": "a", "prompt": "say", "checker": {"kind": "all", "checks": [
        {"kind": "regex", "pattern": "42"}, {"kind": "llm_judge", "rubric": "Output must contain the word GOOD.", "pass_threshold": 0.5}]}})
    v = await h.evals.gate.check({"id": "p3", "scope": "agent", "agent_id": "a", "target": "config", "op": "patch", "candidate": {"soul": "GOOD-SOUL"}})
    assert v.status == "passed" and v.scores["after"] > v.scores["before"]
    judges = [r for r in h.store.list_runs(agent_id="a", limit=100) if r["kind"] == "eval_judge"]
    assert len(judges) == 4 and v.cost_usd > 0


async def test_post_apply_rolls_back_on_drop(make_hub):
    h, rt = make_hub()
    add_golden(h)
    lg = attach_fake_ledger(h)
    e = lg.apply_config("a", {"model": "opus"}, risk_class="L2")
    assert h.agents.get_row("a")["model"] == "opus"
    v = await h.evals.gate.post_apply(e)
    assert v.status == "eval_failed" and v.rolled_back == [e["id"]]
    assert lg.rollbacks[0][1] == "eval_gate" and "eval tụt" in lg.rollbacks[0][2]
    assert h.agents.get_row("a")["model"] == "sonnet"
    befores = [s for s in rt.specs.values() if s.model == "sonnet"]
    assert len(befores) == 4  # trạng thái TRƯỚC dựng bằng materialize_before trên bản sao


async def test_post_apply_without_golden_is_unevaluated(make_hub):
    h, rt = make_hub()
    lg = attach_fake_ledger(h)
    e = lg.apply_config("a", {"effort": "low"}, risk_class="L1")
    v = await h.evals.gate.post_apply(e)
    assert v.status == "deferred_no_eval" and "unevaluated" in v.flags and not lg.rollbacks


async def test_batch_l1_groups_and_passes(make_hub):
    h, rt = make_hub()
    add_golden(h, 1)
    lg = attach_fake_ledger(h)
    lg.apply_config("a", {"model": "haiku"}, risk_class="L1")
    lg.apply_config("a", {"effort": "low"}, risk_class="L1")
    lg.apply_config("a", {"limits": {}}, risk_class="L2")
    out = await h.evals.gate.run_batch_l1()
    assert len(out) == 1 and out[0]["status"] == "passed" and len(out[0]["entries"]) == 2
    assert await h.evals.gate.run_batch_l1() == []  # đã chấm → không chấm lại


async def test_eval_runs_blocked_by_estop_are_handled(make_hub):
    h, rt = make_hub()
    add_golden(h, 1)
    await h.runs.set_estop(True, reason="test")
    v = await h.evals.gate.check(proposal(model="haiku"))
    assert v.status == "deferred_no_eval"


async def test_rpc_eval_check_and_list(hub_home):
    from cfharness.daemon import Daemon

    d = Daemon(hub_home, runtime=ScriptedRuntime(model_script))
    d.hub.start()
    try:
        d.hub.agents.create({"id": "a", "name": "A", "model": "sonnet"})
        d.hub.evals.golden.add({"agent_id": "a", "prompt": "q", "checker": {"kind": "regex", "pattern": "42"}})
        r = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "eval.check", "params": {"proposal": proposal(model="opus")}})
        assert r["result"]["status"] == "eval_failed"
        r = await d._dispatch({"jsonrpc": "2.0", "id": 2, "method": "eval.list", "params": {"agent_id": "a"}})
        assert r["result"][0]["status"] == "eval_failed"
        r = await d._dispatch({"jsonrpc": "2.0", "id": 3, "method": "eval.show", "params": {"id": r["result"][0]["id"]}})
        assert len(r["result"]["results"]) == 4
        assert d.hub.audit.query(kind="eval.check")
    finally:
        d.hub.runs.shutting_down = True
        d.hub.store.close()


async def test_post_apply_never_rolls_back_human_change(make_hub):
    h, rt = make_hub()
    add_golden(h)
    lg = attach_fake_ledger(h)
    e = lg.apply_config("a", {"model": "opus"}, risk_class="L2", actor="user")
    evs = []
    h.bus.add_listener(lambda ev: evs.append(ev))
    v = await h.evals.gate.post_apply(e)
    assert v.status == "eval_failed" and v.rolled_back == [] and "human_change" in v.flags and not lg.rollbacks
    assert h.agents.get_row("a")["model"] == "opus"
    w = [x for x in evs if x.type == "regression.detected"]
    assert w and w[0].payload["human_change"] and w[0].payload["source"] == "eval_gate"


async def test_baseline_cache_invalidated_by_team_and_global_knowledge(make_hub):
    """Dấu vân tay baseline gồm prompt thật (tri thức team/global/USER theo phiên bản gán cho agent): đổi tri thức team hoặc
    USER toàn cục → baseline tính lại; không đổi gì → dùng cache."""
    h, rt = make_hub(**{"eval.baseline_cache_h": 24})
    h.teams.create({"id": "t1", "name": "T1", "members": [{"agent_id": "a", "role": "worker"}]})
    add_golden(h, 1)
    await h.evals.gate.check(proposal(model="haiku"))
    n0 = len(rt.specs)
    await h.evals.gate.check(proposal(model="haiku"))
    assert len(rt.specs) - n0 == 2  # baseline từ cache
    team = h.paths.home / "teams" / "t1"
    team.mkdir(parents=True, exist_ok=True)
    (team / "memory.md").write_text("# Memory\nThe team ships on Thursdays.\n")
    n1 = len(rt.specs)
    await h.evals.gate.check(proposal(model="haiku"))
    new = list(rt.specs.values())[n1:]
    assert len(new) == 4 and any("Thursdays" in (s.system_prompt or "") for s in new)  # baseline chạy lại với tri thức team mới
    (h.paths.home / "global").mkdir(exist_ok=True)
    (h.paths.home / "global" / "USER.md").write_text("# User profile\nThe user prefers concise answers.\n")
    n2 = len(rt.specs)
    await h.evals.gate.check(proposal(model="haiku"))
    assert len(rt.specs) - n2 == 4


def test_fingerprint_tracks_prompt_inputs(make_hub, tmp_path):
    from cfharness.evals import overlay as ov

    h, rt = make_hub()
    home = ov.make_home(Path(h.agents.get_row("a")["home_path"]), tmp_path / "copy")
    cfg = ov.base_config(h.agents.get_row("a"))
    f0 = ov.fingerprint(home, cfg, hub=h, agent_id="a")
    assert ov.fingerprint(home, cfg, hub=h, agent_id="a") == f0
    (home / "SOUL.md").write_text("# A\nNew persona.\n")
    f1 = ov.fingerprint(home, cfg, hub=h, agent_id="a")
    assert f1 != f0
    assert ov.fingerprint(home, {**cfg, "effort": "low"}, hub=h, agent_id="a") != f1
    (h.paths.home / "global").mkdir(exist_ok=True)
    (h.paths.home / "global" / "USER.md").write_text("# User profile\nThe user writes in Vietnamese.\n")
    assert ov.fingerprint(home, cfg, hub=h, agent_id="a") != f1
