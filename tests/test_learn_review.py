"""M4-A Signal (FR-LN-01), trigger (FR-LN-02), review fork (FR-LN-03) end-to-end với `claude` giả (tests/fake_claude_learn.py):
daemon in-process + ClaudeCliRuntime thật + MCP hub-self stdio thật qua socket. Không gọi model, không tốn tiền."""

import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from cfharness.daemon import Daemon
from cfharness.errors import HubError
from cfharness.events import make_event
from cfharness.runs import ActiveRun
from cfharness import transport
from conftest import fake_cli

FAKE = Path(__file__).parent / "fake_claude_learn.py"
BODY = "# Daily status\n\n1. Reply with one line: `Status <date>: OK`.\n"


async def rpc(d, method, params):
    resp = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if "error" in resp:
        raise HubError(resp["error"]["message"])
    return resp["result"]


async def until(fn, timeout=30.0):
    t0 = asyncio.get_running_loop().time()
    while asyncio.get_running_loop().time() - t0 < timeout:
        v = fn()
        if v:
            return v
        await asyncio.sleep(0.05)
    raise AssertionError("hết giờ chờ điều kiện")


@pytest.fixture
async def env(hub_home, tmp_path, monkeypatch):
    fake_bin = fake_cli(tmp_path, FAKE)
    proj = tmp_path / "projects"
    cfg = {
        "runtime": {"kind": "claude", "claude": {"binary": str(fake_bin), "env_extra": {"FAKE_CLAUDE_PROJECTS": str(proj)}}},
        "guard": {"git_probe": False},
        "learning": {"review_debounce_s": 0.3, "tick_s": 0.2},
    }
    hub_home.config.write_text(yaml.safe_dump(cfg))
    d = Daemon(hub_home)
    server = await transport.start_server(d._handle_conn, hub_home.socket, limit=16 * 1024 * 1024)
    d.hub.start()
    await rpc(d, "agent.create", {"config": {"id": "r1", "name": "R1", "model": "haiku"}})
    r = await rpc(d, "learn.propose", {"agent": "r1", "target": "skill", "op": "add", "payload": {"name": "daily-status", "description": "Write the daily status line", "body": BODY}})
    assert r["status"] == "applied"
    yield d, proj, monkeypatch
    server.close()
    d.hub.runs.shutting_down = True
    d.hub.learning.stop()
    for ar in list(d.hub.runs.active.values()):
        if ar.task:
            ar.task.cancel()
    d.hub.store.close()


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


async def test_correction_review_fork_patch_then_no_repeat(env):
    d, proj, mp = env
    h = d.hub
    r1 = await rpc(d, "run.start", {"agent": "r1", "prompt": "Write today's status line."})
    run1 = await h.runs.wait(r1["id"], 30)
    assert run1["outcome"] == "completed" and "2026-10-18" in run1["summary"]  # lỗi: định dạng ngày ISO
    plan = [
        {"tool": "skills_list"},
        {"tool": "skill_view", "args": {"name": "daily-status"}},
        {"tool": "propose_skill_patch", "args": {"name": "daily-status", "old_string": "1. Reply with one line: `Status <date>: OK`.\n",
                                                  "new_string": "1. Reply with one line: `Status <date>: OK`.\n2. Write <date> as dd/mm/yyyy (the user's required format).\n"}},
        {"tool": "propose_config_change", "args": {"changes": {"learning": {"enabled": False}}}},
        {"tool": "task_complete", "args": {"summary": "x"}},
    ]
    mp.setenv("FAKE_REVIEW_PLAN", json.dumps(plan))
    src_tr = proj / f"{run1['session_id']}.jsonl"
    before = sha(src_tr)
    # người sửa CÙNG lỗi 2 lần: hub_feedback + steer/feedback lần hai → gộp vào MỘT review job (newest-wins)
    await rpc(d, "feedback.record", {"run_id": r1["id"], "kind": "wrong", "note": "Dates must be dd/mm/yyyy."})
    await rpc(d, "feedback.record", {"run_id": r1["id"], "kind": "wrong", "note": "Again: use dd/mm/yyyy, not ISO dates."})
    jobs = await rpc(d, "learn.reviews", {"agent": "r1"})
    assert len(jobs) == 1 and jobs[0]["status"] == "queued" and len(jobs[0]["signal_ids"]) == 2  # xếp hàng ngay (≤ 5 s)
    job = await until(lambda: (j := h.learning.reviews.get(jobs[0]["id"]))["status"] in ("done", "failed") and j)
    assert job["status"] == "done", job
    rv = h.store.get_run(job["run_id"])
    argv = rv["argv_redacted"]
    assert argv[argv.index("--resume") + 1] == run1["session_id"] and "--fork-session" in argv
    assert argv[argv.index("--session-id") + 1] == rv["session_id"] != run1["session_id"]
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob" and argv[argv.index("--permission-prompts") + 1] == "none"
    assert "--permission-prompt-tool" not in argv and "--max-budget-usd" in argv
    started = next(e for e in h.store.query_events(run_id=rv["id"], types=["run.started"]))
    assert [m["name"] for m in started.payload["mcp_servers"]] == ["hub-self"]  # chỉ hub-learning MCP
    assert not any(t in started.payload["tools"] for t in ("Bash", "Write", "Edit"))
    assert rv["system_prompt_hash"] == run1["system_prompt_hash"]  # cùng system prompt → cache
    assert rv["cost_usd"] == pytest.approx(0.002) and rv["kind"] == "review" and rv["forked_from_session"] == run1["session_id"]
    assert sha(src_tr) == before  # session gốc KHÔNG đổi
    res = [json.loads(x) for x in (proj / "review-results.jsonl").read_text().splitlines()][-1]["results"]
    assert res[1]["data"]["name"] == "daily-status"
    assert res[2]["data"]["status"] == "applied" and res[2]["data"]["risk_class"] == "L1"
    assert res[3]["data"]["status"] == "blocked" and res[3]["data"]["category"] == "policy_touch"
    assert res[4]["error"]  # tool task_* không có trong vai trò learning
    props = job["result"]["proposals"]
    assert {p["status"] for p in props} == {"applied", "blocked"}
    sigs = h.learning.signals.many(job["signal_ids"])
    assert all(s["processed_by_review_id"] == job["id"] for s in sigs)
    types = [e.type for e in h.store.query_events(agent_id="r1")]
    assert "review.started" in types and "review.completed" in types and "learning.applied" in types and "guard.blocked" in types
    # run mới (session mới) đọc skill đã vá → không lặp lỗi
    r3 = await rpc(d, "run.start", {"agent": "r1", "prompt": "Write today's status line."})
    run3 = await h.runs.wait(r3["id"], 30)
    assert "18/10/2026" in run3["summary"], run3["summary"]
    assert run3["system_prompt_hash"] == run1["system_prompt_hash"]  # chỉ mục skill không đổi (chỉ thân skill đổi)
    st = await rpc(d, "learn.status", {"agent": "r1"})
    assert st["pending_approval_queue"] == 0


async def test_review_jobs_merge_and_counter_trigger(env):
    d, proj, mp = env
    h = d.hub
    h.config.set("learning.review_debounce_s", 30)
    jobs = [h.learning.reviews.enqueue("r1", "signal", signal_ids=[f"sg_{i}"]) for i in range(3)]
    assert len({j["id"] for j in jobs}) == 1 and h.learning.reviews.get(jobs[0]["id"])["signal_ids"] == ["sg_0", "sg_1", "sg_2"]
    h.learning.reviews.cancel(jobs[0]["id"])
    h.config.set("learning.trigger_turns", 2)
    for _ in range(2):
        r = await rpc(d, "run.start", {"agent": "r1", "prompt": "Write today's status line."})
        await h.runs.wait(r["id"], 30)
    await until(lambda: h.learning.reviews.list("r1", "queued"))
    assert h.learning.reviews.list("r1", "queued")[0]["trigger"] == "counter"


async def test_learning_disabled_no_review(env):
    d, proj, mp = env
    h = d.hub
    await rpc(d, "learn.policy_set", {"key": "enabled", "value": "false"})
    r = await rpc(d, "run.start", {"agent": "r1", "prompt": "Write today's status line."})
    await h.runs.wait(r["id"], 30)
    await rpc(d, "feedback.record", {"run_id": r["id"], "kind": "wrong", "note": "wrong date format"})
    await asyncio.sleep(0.3)
    assert await rpc(d, "learn.reviews", {"agent": "r1"}) == []
    audit = h.store.conn.execute("SELECT COUNT(*) FROM audit_log WHERE kind LIKE '%learn.policy_set%' OR summary LIKE '%learn.policy_set%' OR data LIKE '%learn.policy_set%'").fetchone()[0]
    assert audit >= 1


async def test_signal_detectors(env):
    d, proj, mp = env
    h = d.hub
    r = await rpc(d, "run.start", {"agent": "r1", "prompt": "Write today's status line."})
    await h.runs.wait(r["id"], 30)
    # steer có cụm phủ định → correction
    h.publish(make_event("user.message", "r1", {"text": "sai rồi, đừng dùng ISO", "steer": True, "by": "user", "channel": "cli"}, run_id=r["id"]))
    h.publish(make_event("user.message", "r1", {"text": "please continue", "steer": True}, run_id=r["id"]))
    # revert → undo
    h.publish(make_event("run.reverted", "r1", {"files": ["a.txt"], "warnings": []}, run_id=r["id"]))
    kinds = [s["kind"] for s in h.learning.signals.list("r1")]
    assert kinds.count("correction") == 1 and "undo" in kinds
    # người dừng run → cancel (qua RPC run.stop đã bọc)
    h.store.update_run(r["id"], {"status": "running"})
    try:
        await rpc(d, "run.stop", {"id": r["id"]})
    except HubError:
        pass
    assert "cancel" in [s["kind"] for s in h.learning.signals.list("r1")]
    # lỗi tool lặp ở 2 run → failure; chuỗi tool lặp ở 3 run → repeat_pattern
    rows = []
    for i in range(3):
        rid = f"r_fake{i}"
        h.store.insert_run({"id": rid, "agent_id": "r1", "kind": "adhoc", "status": "completed", "queued_at": f"2026-10-18T00:00:0{i}.000Z"})
        for j, name in enumerate(["vb_tim", "vb_doc", "vb_doc"]):
            h.publish(make_event("tool.start", "r1", {"tool_id": f"t{i}{j}", "name": name}, run_id=rid))
        h.publish(make_event("tool.complete", "r1", {"tool_id": f"t{i}0", "name": "vb_tim", "is_error": True, "result_preview": f"Error 500 at /x/{i}"}, run_id=rid))
        rows += h.learning.signals.on_run_terminal(h.store.get_run(rid))
    kinds = [s["kind"] for s in h.learning.signals.list("r1")]
    assert kinds.count("failure") == 1 and kinds.count("repeat_pattern") == 1
    rp = next(s for s in h.learning.signals.list("r1") if s["kind"] == "repeat_pattern")
    assert rp["evidence"]["pattern"] == ["vb_tim", "vb_doc", "vb_doc"]


async def test_edit_output_detected_from_human_edit(env, tmp_path):
    d, proj, mp = env
    h = d.hub
    repo = tmp_path / "repo"
    repo.mkdir()
    for c in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"], ["git", "config", "user.name", "t"]):
        subprocess.run(c, cwd=repo, check=True)
    (repo / "a.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    r = await rpc(d, "run.start", {"agent": "r1", "prompt": "Write today's status line.", "cwd": str(repo)})
    await h.runs.wait(r["id"], 30)
    # mô phỏng: run vừa ghi a.txt (diff có after_sha) rồi người sửa lại
    from cfharness.workspace import compute_diff, take_checkpoint

    cp_dir = h.runs.checkpoint_dir(r["id"])
    cp = take_checkpoint(repo, cp_dir)
    (repo / "a.txt").write_text("agent output 2026-10-18\n")
    diff = compute_diff(cp, cp_dir)
    h.store.update_run(r["id"], {"diff": diff, "workspace_key": str(repo.resolve())})
    (repo / "a.txt").write_text("agent output 18/10/2026\n")  # người sửa output
    r2 = await rpc(d, "run.start", {"agent": "r1", "prompt": "Write today's status line.", "cwd": str(repo)})
    await h.runs.wait(r2["id"], 30)
    sig = next(s for s in h.learning.signals.list("r1") if s["kind"] == "edit_output")
    assert sig["run_id"] == r["id"] and "18/10/2026" in sig["evidence"]["diff"] and sig["evidence"]["files"] == ["a.txt"]


def test_review_runs_do_not_take_agent_slot(hub_home):
    from cfharness.hub import Hub

    h = Hub(hub_home)
    h.runs.active["r_a"] = ActiveRun("r_a", "x", kind="review")
    h.runs.active["r_b"] = ActiveRun("r_b", "x")
    assert h.runs.running_count("x") == 2 and h.runs.running_count("x", exclude_kind="review") == 1
    h.store.close()


def test_hermes_prompt_verbatim_and_renamed():
    from cfharness.learning.prompts import hermes_review, render_review_prompt

    raw = Path(hermes_review.__file__).read_text()
    assert "Copyright (c) 2025 Nous Research" in raw and "MIT" in raw
    src = Path(__file__).parents[1] / "research" / "hermes-agent" / "agent" / "background_review.py"
    if src.exists():  # văn bản NGUYÊN VĂN (Q-13)
        lines = src.read_text().splitlines()[369:569]
        assert "\n".join(lines) in raw
    p = render_review_prompt(signals=[{"kind": "correction", "run_id": "r_1", "source": "mcp", "evidence": {"note": "use dd/mm/yyyy"}}], focus="dates")
    assert "skill_manage" not in p and "propose_skill_patch" in p and "propose_memory tool, target='user'" in p
    assert "use dd/mm/yyyy" in p and "focus — prioritize it" in p
    assert (Path(__file__).parents[1] / "NOTICE").read_text().count("Nous Research") >= 1
