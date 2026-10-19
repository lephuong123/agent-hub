"""M5-B: curator liên agent (FR-LN-12) + curator pha 2 LLM (FR-LN-09). Không gọi claude thật."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
import yaml

from cfharness.curation.prompts import PHASE2_TAG, render_phase2_prompt
from cfharness.curation.service import parse_structured_summary
from cfharness.curation.similarity import cluster_key, find_clusters, merge_deterministic, skill_similarity
from cfharness.errors import HubError
from cfharness.hub import Hub
from cfharness.learning.prompts import hermes_curator
from m3_fake import ScriptedRuntime
from conftest import fake_cli

WEEKLY_A = """# Weekly report
1. Collect merged pull requests from the last 7 days.
2. Group them by component and summarize each group in one line.
3. List open blockers with owner names.
4. Post the report as a markdown table.
"""
WEEKLY_B = """# Weekly status report
1. Collect the merged pull requests of the last seven days.
2. Group the pull requests by component; one summary line per group.
3. List open blockers with their owner names.
4. Always include a short risks section at the end.
"""
DEPLOY = """# Deploy checklist
1. Run database migrations before restarting services.
2. Verify health endpoints return 200.
"""


# ============================================================================ hàm thuần
def test_similarity_and_clusters():
    a = {"agent_id": "a", "name": "weekly-report", "description": "Write the weekly engineering report", "body": WEEKLY_A}
    b = {"agent_id": "b", "name": "weekly-status-report", "description": "Weekly engineering status report", "body": WEEKLY_B}
    c = {"agent_id": "b", "name": "deploy-checklist", "description": "Steps to deploy services safely", "body": DEPLOY}
    a2 = {"agent_id": "a", "name": "weekly-report-copy", "description": "Write the weekly engineering report", "body": WEEKLY_A}
    assert skill_similarity(a, a)["score"] == pytest.approx(1.0)
    assert skill_similarity(a, b)["score"] >= 0.5
    assert skill_similarity(a, c)["score"] < 0.2
    cl = find_clusters([a, b, c, a2], 0.5)
    assert len(cl) == 1 and cl[0]["agents"] == ["a", "b"]
    assert {m["name"] for m in cl[0]["members"]} == {"weekly-report", "weekly-status-report", "weekly-report-copy"}
    # trùng trong MỘT agent không tạo cụm liên agent
    assert find_clusters([a, a2], 0.5) == []
    assert cluster_key("t", [a, b]) == cluster_key("t", [b, a]) != cluster_key("t2", [a, b])


def test_merge_deterministic_keeps_unique_rules():
    a = {"agent_id": "a", "name": "weekly-report", "description": "Write the weekly engineering report", "body": WEEKLY_A, "use_count": 5}
    b = {"agent_id": "b", "name": "weekly-status-report", "description": "x", "body": WEEKLY_B, "use_count": 1}
    m = merge_deterministic([a, b])
    assert m["name"] == "weekly-report" and m["description"] == "Write the weekly engineering report"
    assert "risks section" in m["body"] and m["body"].count("Collect merged pull requests") == 1
    assert "Additional rules" in m["body"]


def test_parse_structured_summary():
    text = "I merged things.\n\n## Structured summary (required)\n```yaml\nconsolidations:\n  - from: b\n    into: a\n    reason: same\nprunings: []\n```\n"
    assert parse_structured_summary(text) == {"consolidations": [{"from": "b", "into": "a", "reason": "same"}], "prunings": []}
    assert parse_structured_summary("no block") == {}


def test_phase2_prompt_is_hermes_verbatim():
    p = render_phase2_prompt("a", [{"name": "x", "description": "d", "use_count": 0}])
    assert p.startswith(PHASE2_TAG)
    # đoạn đặc trưng của Hermes giữ nguyên văn (chỉ đổi tên tool)
    assert "UMBRELLA-BUILDING consolidation pass" in p and "## Structured summary (required)" in p
    assert "skill_manage" not in p and "propose_skill_archive" in p
    assert "Consolidation means DISTILLING" in hermes_curator.CURATOR_REVIEW_PROMPT
    assert "- x: d | use=0 | pinned=no" in p


# ============================================================================ tích hợp
class CuratorRuntime(ScriptedRuntime):
    """Run curator (kind=curator): khi start, gọi các tool hub-self theo `plan` IN-PROCESS (như MCP thật gọi learnself.call)."""

    def __init__(self, hub_ref: list, plan: list, final_text: str) -> None:
        super().__init__(lambda s: {"text": final_text if s.prompt.startswith(PHASE2_TAG) else "ok", "cost": 0.01})
        self.hub_ref = hub_ref
        self.plan = plan
        self.results: list = []

    async def start(self, spec):
        h = await super().start(spec)
        if spec.prompt.startswith(PHASE2_TAG):
            hub = self.hub_ref[0]
            for op, args in self.plan:
                try:
                    self.results.append((op, args.get("name"), hub.learning.learnself_call(spec.run_id, op, args)))
                except HubError as e:
                    self.results.append((op, args.get("name"), {"status": "error", "reason": e.message}))
        return h


def _seed(hub, agent, name, desc, body):
    r = hub.learning.pipeline.propose(target="skill", op="add", agent_id=agent, created_by="user", payload={"name": name, "description": desc, "body": body})
    assert r["status"] == "applied", r


async def _until(fn, timeout=10.0):
    t0 = asyncio.get_running_loop().time()
    while asyncio.get_running_loop().time() - t0 < timeout:
        v = fn()
        if v:
            return v
        await asyncio.sleep(0.02)
    raise AssertionError("hết giờ")


@pytest.fixture
def team_hub(hub_home):
    hub_home.config.write_text(yaml.safe_dump({"guard": {"git_probe": False}}))
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {"text": "ok"}))
    for a in ("a", "b", "c"):
        h.agents.create({"id": a, "name": a.upper()})
    h.teams.create({"id": "t", "name": "T"})
    h.teams.member_set("t", "a")
    h.teams.member_set("t", "b")
    _seed(h, "a", "weekly-report", "Write the weekly engineering report", WEEKLY_A)
    _seed(h, "b", "weekly-status-report", "Weekly engineering status report", WEEKLY_B)
    _seed(h, "b", "deploy-checklist", "Steps to deploy services safely", DEPLOY)
    _seed(h, "c", "weekly-report", "Write the weekly engineering report", WEEKLY_A)  # c không cùng team → không tính
    yield h
    h.runs.shutting_down = True
    h.store.close()


async def test_cross_agent_proposes_team_promotion_via_pipeline(team_hub):
    hub = team_hub
    evs = []
    hub.bus.add_listener(lambda e: evs.append(e))
    dry = await hub.curation.cross_agent(dry_run=True)
    assert [c["action"] for c in dry["clusters"]] == ["would_propose"]
    assert not hub.store.conn.execute("SELECT 1 FROM proposals WHERE created_by='curator'").fetchone()
    rec = await hub.curation.cross_agent()
    (c,) = rec["clusters"]
    assert c["action"] == "proposed" and c["team_id"] == "t" and c["agents"] == ["a", "b"] and c["team_skill"] == "weekly-status-report"  # gốc = thân dài nhất
    p = hub.learning.pipeline.get(c["proposal_id"])
    assert p["scope"] == "team" and p["team_id"] == "t" and p["created_by"] == "curator" and p["target"] == "skill" and p["op"] == "add"
    assert p["risk_class"] == "L3" or p["status"] == "blocked" or p["risk_class"] is None
    assert p["payload"]["curation"]["members"] == [{"agent_id": "a", "name": "weekly-report"}, {"agent_id": "b", "name": "weekly-status-report"}]
    # không tự ghi tầng team: nếu pipeline chưa áp dụng thì thư mục team chưa có skill
    if p["status"] not in ("applied", "promoted", "canary"):
        assert not (hub.paths.home / "teams" / "t" / "skills" / "weekly-status-report").exists()
    assert any(e.type == "curation.promotion" and e.payload["proposal_id"] == p["id"] for e in evs)
    assert Path(rec["report_path"]).read_text().startswith("# Curation cross_agent")
    # lượt sau: cụm đã đề xuất → bỏ qua (không lặp)
    rec2 = await hub.curation.cross_agent()
    assert rec2["clusters"][0]["action"] == "skip" and "đã đề xuất" in rec2["clusters"][0]["reason"]


async def test_after_promoted_agent_copies_archived(team_hub):
    hub = team_hub
    rec = await hub.curation.cross_agent()
    pid = rec["clusters"][0]["proposal_id"]
    # mô phỏng A: skill team đã được áp dụng + promoted
    d = hub.paths.home / "teams" / "t" / "skills" / "weekly-status-report"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("---\nname: weekly-status-report\ndescription: Weekly report\n---\n\n" + WEEKLY_B)
    hub.knowledge.sync_index("team", "t")
    from cfharness.events import make_event

    hub.publish(make_event("learning.promoted", "a", {"proposal_id": pid}))
    info = await _until(lambda: hub.curation._index().get("proposal:" + pid, {}).get("archived") and hub.curation._index()["proposal:" + pid])
    res = {(r["agent_id"], r["name"]): r for r in info["archive_results"]}
    # bản của a (tên khác) được archive với absorbed_into = skill team
    assert res[("a", "weekly-report")]["status"] == "applied", res
    assert not (Path(hub.agents.get_row("a")["home_path"]) / "skills" / "weekly-report").exists()
    led = hub.learning.ledger.list("a", target_ref="weekly-report")
    assert led[0]["actor"] == "curator" and led[0]["op"] in ("merge", "archive")
    # bản của b trùng TÊN skill team (che khuất) → archive qua cổng hub.knowledge.archive_skill (hợp đồng M5-A)
    assert res[("b", "weekly-status-report")]["status"] == "applied" and "archive_skill" in res[("b", "weekly-status-report")]["via"]
    assert not (Path(hub.agents.get_row("b")["home_path"]) / "skills" / "weekly-status-report").exists()
    assert hub.learning.ledger.list("b", target_ref="weekly-status-report")[0]["actor"] == "curator"


async def test_cross_agent_llm_merge_with_fake_binary(team_hub, tmp_path):
    hub = team_hub
    fake = tmp_path / "fake_llm.py"
    fake.write_text(
        "import sys, json\nargv=sys.argv[1:]\nsys.stdin.read()\n"
        "assert '--json-schema' in argv and argv[argv.index('--tools')+1]=='' and '--no-session-persistence' in argv\n"
        "print(json.dumps({'type':'result','is_error':False,'total_cost_usd':0.0042,'structured_output':{'name':'weekly-engineering-report',"
        "'description':'Weekly engineering report for the team','body':'# Weekly\\n1. Collect merged PRs.\\n','same_class':True,'reason':'same task'}}))\n")
    binp = fake_cli(tmp_path, fake)
    hub.config.set("curation.binary", str(binp))
    rec = await hub.curation.cross_agent(llm=True)
    c = rec["clusters"][0]
    assert c["merge"] == "llm" and c["team_skill"] == "weekly-engineering-report" and rec["cost_usd"] == pytest.approx(0.0042)
    r = hub.store.get_run(c["llm_run_id"])
    assert r["kind"] == "curator" and r["cost_usd"] == pytest.approx(0.0042)
    assert hub.curation.spent_today() == pytest.approx(0.0042)


async def test_tick_respects_idle_and_budget(team_hub):
    hub = team_hub
    hub.config.set("curation.min_idle_min", 10_000)
    hub.store.insert_run({"id": "r_recent", "agent_id": "a", "kind": "adhoc", "prompt": "x", "status": "completed", "queued_at": "2999-01-01T00:00:00.000Z"})
    assert (await hub.curation.tick()) == {"skipped": "not_idle"}
    hub.config.set("curation.min_idle_min", 0)
    hub.store.conn.execute("DELETE FROM runs WHERE id='r_recent'")
    out = await hub.curation.tick()
    assert "cross_agent" in out
    assert "cross_agent" not in await hub.curation.tick()  # chưa tới interval_h
    with pytest.raises(HubError, match="pha 2 đang tắt"):
        hub.curation.phase2("b")
    hub.config.set("curation.daily_budget_usd", 0)
    with pytest.raises(HubError, match="ngân sách"):
        hub.curation.phase2("b", force=True)


SUMMARY = ("Merged the release notes cluster.\n\n## Structured summary (required)\n```yaml\nconsolidations:\n"
           "  - from: release-notes-format\n    into: release-notes\n    reason: same output format rules\nprunings: []\n```\n")
RN = "# Release notes\n1. Group changes by type.\n"
RNF = "# Release notes format\n1. Use past tense in every bullet.\n"


@pytest.fixture
def p2_hub(hub_home):
    hub_home.config.write_text(yaml.safe_dump({"guard": {"git_probe": False}, "curation": {"phase2": {"enabled": True}}}))
    ref: list = []
    plan = [
        ("propose_skill_archive", {"name": "release-notes-format", "absorbed_into": "release-notes"}),  # chưa đọc → chặn
        ("skills_list", {}),
        ("skill_view", {"name": "release-notes"}),
        ("skill_view", {"name": "release-notes-format"}),
        ("propose_skill_archive", {"name": "release-notes-format"}),  # thiếu absorbed_into → chặn
        ("propose_skill_patch", {"name": "release-notes", "content": RN + "2. Use past tense in every bullet.\n"}),
        ("propose_skill_archive", {"name": "release-notes-format", "absorbed_into": "release-notes", "reason": "stale bypass attempt"}),
        ("propose_memory", {"action": "add", "content": "x"}),  # ngoài phạm vi curator
        ("propose_skill_archive", {"name": "pinned-skill", "absorbed_into": "release-notes"}),  # pin → guard chặn
    ]
    rt = CuratorRuntime(ref, plan, SUMMARY)
    h = Hub(hub_home, runtime=rt)
    ref.append(h)
    h.agents.create({"id": "w", "name": "W"})
    _seed(h, "w", "release-notes", "Write release notes", RN)
    _seed(h, "w", "release-notes-format", "Format rules for release notes", RNF)
    _seed(h, "w", "pinned-skill", "A pinned helper skill", "# Pinned\n1. Keep.\n")
    h.knowledge.set_pin("agent", "w", "skill", "pinned-skill", True)
    yield h, rt
    h.runs.shutting_down = True
    h.store.close()


async def test_phase2_merge_with_snapshot_and_rollback(p2_hub):
    hub, rt = p2_hub
    home = Path(hub.agents.get_row("w")["home_path"])
    before_rn = (home / "skills" / "release-notes" / "SKILL.md").read_text()
    hub.start()
    rec = hub.curation.phase2("w")
    assert rec["status"] == "running" and rec["candidates"] == ["release-notes", "release-notes-format"]  # pin không vào danh sách
    snap = hub.store.conn.execute("SELECT kind, blob FROM snapshots WHERE id=?", (rec["snapshot"]["snapshot_id"],)).fetchone()
    assert snap[0] == "bulk_tree" and (hub.paths.home / "ledger" / "blobs" / snap[1]).exists()
    run = hub.store.get_run(rec["run_id"])
    assert run["kind"] == "curator" and run["run_opts"]["learning_review"] and run["run_opts"]["curator_pass"]
    done = await _until(lambda: (r := hub.curation.get(rec["id"]))["status"] != "running" and r)
    res = [(op, name, r.get("status"), r.get("rule")) for op, name, r in rt.results]
    assert res[0][2] == "blocked" and res[0][3] == "read_before_write"
    assert res[4][2] == "blocked" and res[4][3] == "absorbed_into"
    assert res[5][2] == "applied"  # patch umbrella
    assert res[6][2] == "applied"  # archive có đích hấp thụ (lý do "stale" không lách được: tiền tố consolidated:)
    assert res[7][2] == "blocked"  # memory ngoài phạm vi
    assert res[8][2] in ("blocked",)  # skill pin
    assert done["status"] == "done" and done["structured"]["consolidations"][0]["into"] == "release-notes"
    assert done["unlisted_archives"] == []
    assert not (home / "skills" / "release-notes-format").exists()
    assert list((home / ".archive" / "skills").glob("release-notes-format__*"))
    assert "past tense" in (home / "skills" / "release-notes" / "SKILL.md").read_text()
    led = hub.learning.ledger.list("w", actor="curator")
    assert {e["target_ref"] for e in led} == {"release-notes", "release-notes-format"}
    assert Path(done["report_path"]).read_text().count("applied") >= 2
    # rollback cả lượt
    rb = hub.curation.rollback(rec["id"])
    assert rb["status"] == "rolled_back" and len(rb["rolled_back"]["rollback_entries"]) == 2
    assert (home / "skills" / "release-notes-format" / "SKILL.md").exists()
    assert (home / "skills" / "release-notes" / "SKILL.md").read_text() == before_rn
    assert {s["name"] for s in hub.knowledge.list_skills("agent", "w")} >= {"release-notes", "release-notes-format"}
    hub.learning.stop()
    hub.learning_ops.stop()
    hub.curation.stop()


def test_cli_rewrite_and_parsers():
    from cfharness.cli import build_parser
    from cfharness.curation.cli import rewrite_argv

    assert rewrite_argv(["report", "kpi", "--weeks", "6"]) == ["report-kpi", "--weeks", "6"]
    assert rewrite_argv(["--json", "report", "overhead"]) == ["--json", "report-overhead"]
    assert rewrite_argv(["report", "--since", "7d"]) == ["report", "--since", "7d"]
    p = build_parser()
    a = p.parse_args(["curation", "phase2", "w", "--dry-run"])
    assert a.action == "phase2" and a.agent == "w" and a.dry_run
    a = p.parse_args(["report-kpi", "--weeks", "6"])
    assert a.weeks == 6
    assert json.dumps(p.parse_args(["telemetry", "status"]).action) == '"status"'
