"""Đề xuất golden task từ task `done` không bị sửa/hoàn tác; NGƯỜI duyệt một chạm (web + Telegram/Slack + CLI) — Q-07.

Lõi: lọc (chống trùng, đủ tín hiệu, không secret, người không chạm), quyết định chỉ người + audit, golden mới được gate thấy.
Telegram: server Bot API giả (tests/telegram_fake.py), nút nonce một lần như approval. Không gọi claude/Telegram thật."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from cfharness import auth
from cfharness.errors import Conflict, HubError
from cfharness.events import make_event
from cfharness.hub import Hub
from m3_fake import ScriptedRuntime
from test_slack import make, sk  # noqa: F401 — fixture Slack giả cho test e2e Slack

OLD = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
RESULT = "Report written to report.md: 3 sections, totals reconcile with the ledger."


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda spec: {"text": "ok"}))
    h.agents.create({"id": "a", "name": "A", "model": "sonnet"})
    yield h
    h.runs.shutting_down = True
    h.store.close()


def done_task(h: Hub, tid: str, *, title: str = "Write the monthly cost report", body: str = "Use the ledger in data/ and keep it short.",
              result: str = RESULT, done_at: str = OLD, run_status: str = "completed", agent: str = "a") -> str:
    c = h.store.conn
    c.execute("INSERT INTO tasks (id, assignee_agent_id, kind, title, body, status, result_summary, created_at, updated_at, done_at) "
              "VALUES (?,?,'task',?,?,'done',?,?,?,?)", (tid, agent, title, body, result, done_at, done_at, done_at))
    rid = "r_" + tid
    c.execute("INSERT INTO runs (id, agent_id, task_id, kind, status, prompt, summary, queued_at) VALUES (?,?,?,'task',?,?,?,?)",
              (rid, agent, tid, run_status, title, result, done_at))
    return rid


def sugg_ids(h: Hub) -> dict[str, str]:
    return {r[0]: r[1] for r in h.store.conn.execute("SELECT task_id, id FROM golden_suggestions WHERE task_id IS NOT NULL")}


# ============================================================================ lọc đề xuất
def test_auto_suggest_filters(hub):
    evs = []
    hub.bus.add_listener(lambda e: evs.append(e))
    done_task(hub, "t_ok")
    rid = done_task(hub, "t_corr", title="Summarize the incident timeline for ops")
    hub.store.conn.execute("INSERT INTO signals (id, agent_id, run_id, kind, source, evidence, created_at) VALUES ('s1','a',?,'correction','cli','{}',?)", (rid, OLD))
    rid2 = done_task(hub, "t_rev", title="Refactor the parser module into two files")
    hub.publish(make_event("run.reverted", "a", {"files": ["x.py"]}, run_id=rid2))
    done_task(hub, "t_sec", title="Deploy using token ghp_abcdefghijklmnopqrstuvwxyz0123456789AB now")
    done_task(hub, "t_weak", title="Fix it", body="", result="done")
    done_task(hub, "t_dup", title="write the  MONTHLY cost report", body="Use the ledger in data/ and keep it short.")
    done_task(hub, "t_young", title="Draft the release notes for v2", done_at=datetime.now(timezone.utc).isoformat())
    done_task(hub, "t_fail", title="Migrate the database schema to v3", run_status="failed")
    out = hub.evals.golden.auto_suggest()
    ids = sugg_ids(hub)
    assert set(ids) == {"t_ok"} and out["suggested"] == [ids["t_ok"]]
    assert out["skipped"] == {"human_touched": 2, "secret": 1, "weak_signal": 1, "duplicate": 1, "no_completed_run": 1}
    s = hub.evals.golden.suggestion(ids["t_ok"])
    assert s["status"] == "open" and s["agent_id"] == "a" and s["run_id"] == "r_t_ok"
    assert s["prompt"].startswith("Write the monthly cost report\n\n") and s["checker"]["kind"] == "llm_judge" and RESULT in s["checker"]["rubric"]
    assert [e.payload["suggestion_id"] for e in evs if e.type == "golden.suggestion_opened"] == [ids["t_ok"]]
    assert hub.evals.golden.auto_suggest()["suggested"] == []  # quét lại: không trùng
    assert hub.evals.golden.active_for("a") == []  # đề xuất KHÔNG tạo golden task


def test_auto_suggest_caps_and_disabled(hub):
    for i in range(5):
        done_task(hub, f"t{i}", title=f"Task number {i} with a long enough title")
    assert len(hub.evals.golden.auto_suggest()["suggested"]) == 3  # max_open_per_agent
    hub.config.set("eval.golden_suggest", {"enabled": False})
    done_task(hub, "t9", title="Another task with a long enough title")
    assert hub.evals.golden.auto_suggest() == {"suggested": [], "skipped": {}}


# ============================================================================ quyết định: chỉ người + audit + gate thấy golden
def test_approve_creates_golden_wakes_deferred_and_audits(hub):
    done_task(hub, "t_ok")
    hub.evals.golden.auto_suggest()
    sid = sugg_ids(hub)["t_ok"]
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    hub.store.conn.execute("INSERT INTO proposals (id, scope, agent_id, target, op, created_by, status, not_before, created_at, updated_at) "
                           "VALUES ('p1','agent','a','config','patch','review','deferred_no_eval',?,?,?)", (later, OLD, OLD))
    s = hub.evals.golden.decide_suggestion(sid, "approve", created_by="user:telegram:111")
    assert s["status"] == "accepted" and s["decided_by"] == "user:telegram:111" and s["decided_channel"] == "telegram:111"
    g = hub.evals.golden.get(s["golden_task_id"])
    assert g["created_by"] == "user:telegram:111" and g["source_run_id"] == "r_t_ok" and g["checker"]["kind"] == "llm_judge"
    assert [t["id"] for t in hub.evals.golden.active_for("a")] == [g["id"]]  # eval gate/canary đọc active_for
    nb = hub.store.conn.execute("SELECT not_before FROM proposals WHERE id='p1'").fetchone()[0]
    assert nb < later  # proposal hoãn vì thiếu golden được thử lại ngay ở tick kế tiếp
    a = hub.audit.query(kind="golden.suggestion_decided")
    assert len(a) == 1 and a[0]["actor"] == "user:telegram:111" and a[0]["data"]["decision"] == "approve"
    with pytest.raises(Conflict):
        hub.evals.golden.decide_suggestion(sid, "reject", created_by="user:cli")


def test_reject_dismisses_and_never_resuggests(hub):
    done_task(hub, "t_ok")
    hub.evals.golden.auto_suggest()
    sid = sugg_ids(hub)["t_ok"]
    s = hub.evals.golden.decide_suggestion(sid[:7], "reject", created_by="user:web")
    assert s["status"] == "dismissed" and s["golden_task_id"] is None
    assert hub.evals.golden.auto_suggest()["suggested"] == [] and hub.evals.golden.active_for("a") == []


@pytest.mark.parametrize("actor", ["agent", "agent:a", "learning", "review", "curator", "eval_gate"])
def test_agent_cannot_decide_own_suggestion(hub, actor):
    done_task(hub, "t_ok")
    hub.evals.golden.auto_suggest()
    sid = sugg_ids(hub)["t_ok"]
    for decision in ("approve", "reject"):
        with pytest.raises(HubError):
            hub.evals.golden.decide_suggestion(sid, decision, created_by=actor)
    assert hub.evals.golden.suggestion(sid)["status"] == "open"


def test_human_only_and_guard_cover_every_decision_path(hub_home, tmp_path):
    from cfharness.guard.classifier import GuardContext, classify
    from cfharness.guard.policy import PolicyStore

    assert {"golden.suggestion_approve", "golden.suggestion_reject"} <= auth.HUMAN_ONLY
    ps = PolicyStore(hub_home.home, user_home=tmp_path / "home")
    ps.ensure()
    ctx = GuardContext(tmp_path, ps.load(), git_probe=False)
    for cmd in ("cfharness golden approve gs_1", "cfharness golden reject gs_1", "uv run cfharness golden approve gs_1"):
        v = classify("Bash", {"command": cmd}, ctx)
        assert v.forbidden, cmd
    assert not classify("Bash", {"command": "cfharness golden suggestions"}, ctx).forbidden
    for tool in ("mcp__cfharness__hub_golden_approve", "mcp__cfharness__hub_golden_suggestion_approve", "mcp__x__golden_reject"):
        assert classify(tool, {}, ctx).forbidden, tool


async def test_rpc_channel_audit_and_agent_caller_refused(hub_home):
    from cfharness.daemon import Daemon

    d = Daemon(hub_home, runtime=ScriptedRuntime(lambda s: {"text": "ok"}))
    try:
        d.hub.agents.create({"id": "a", "name": "A"})
        done_task(d.hub, "t_ok")
        r = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "golden.suggest_scan", "params": {}})
        sid = r["result"]["suggested"][0]
        r = await d._dispatch({"jsonrpc": "2.0", "id": 2, "method": "golden.suggestions", "params": {}})
        assert [s["id"] for s in r["result"]] == [sid]
        agent = auth.Caller(via="socket", pid=1, token_ok=True, run_id="r_x", agent_id="a", evidence=["ppid:r_x"])  # kể cả đã đọc được token
        resp = await d.call_as(agent, "golden.suggestion_approve", {"id": sid})
        assert "error" in resp and d.hub.evals.golden.suggestion(sid)["status"] == "open"
        r = await d._dispatch({"jsonrpc": "2.0", "id": 3, "method": "golden.suggestion_approve",
                               "params": {"id": sid, "_audit": {"channel": "web", "actor": "user"}}})
        assert r["result"]["status"] == "accepted" and r["result"]["decided_by"] == "user:web"
        assert d.hub.audit.query(kind="golden.suggestion_approve")
    finally:
        d.hub.runs.shutting_down = True
        d.hub.store.close()


def test_cli_parsers():
    from cfharness.cli import build_parser

    p = build_parser()
    assert p.parse_args(["golden", "suggestions", "--all"]).action == "suggestions"
    assert p.parse_args(["golden", "approve", "gs_1"]).id == "gs_1"
    assert p.parse_args(["golden", "reject", "gs_1"]).action == "reject"
    assert p.parse_args(["golden", "scan"]).action == "scan"


async def test_ops_tick_runs_auto_suggest_hourly(hub):
    done_task(hub, "t_ok")
    out = await hub.learning_ops.tick()
    assert out.get("golden_suggest", {}).get("suggested")
    done_task(hub, "t_two", title="Another long enough task title here")
    assert "golden_suggest" not in await hub.learning_ops.tick()  # chưa tới every_min


# ============================================================================ Telegram: nút một chạm (nonce như approval)
async def test_telegram_one_tap_approve_replay_and_cross_chat_cleanup(hub_home):
    import yaml

    from cfharness.daemon import Daemon
    from cfharness.runtime import FakeRuntime
    from telegram_fake import TOKEN, FakeTelegram
    from test_telegram import BOSS, OTHER, STRANGER, press, until

    tg = FakeTelegram()
    hub_home.config.write_text(yaml.safe_dump({"guard": {"git_probe": False}, "notify": {"telegram": {
        "api_base": tg.url, "poll_timeout_s": 1, "idle_check_s": 0.05, "backoff_base_s": 0.05, "allowed_chat_ids": [str(BOSS), str(OTHER)]}}}))
    d = Daemon(hub_home, runtime=FakeRuntime())
    d.hub.secrets.set("TELEGRAM_BOT_TOKEN", TOKEN)
    d.hub.start()
    tg.hubs.append(d.hub)
    try:
        d.hub.agents.create({"id": "a", "name": "A"})
        done_task(d.hub, "t_ok")
        sid = d.hub.evals.golden.auto_suggest()["suggested"][0]

        def sysmsg(chat):
            return next((m for m in tg.sent(chat) if sid in m["text"] and FakeTelegram.buttons(m)), None)

        msg = await until(lambda: sysmsg(BOSS))
        await until(lambda: sysmsg(OTHER))
        content = next(m for m in tg.sent(BOSS) if "Write the monthly cost report" in m["text"])
        assert RESULT[:30] in content["text"] and not FakeTelegram.buttons(content)
        approve, reject = [b["callback_data"] for b in FakeTelegram.buttons(msg)]
        await press(tg, STRANGER, approve)  # người lạ / chat khác: không tiêu, không quyết
        await press(tg, OTHER, approve)
        assert d.hub.evals.golden.suggestion(sid)["status"] == "open"
        await press(tg, BOSS, approve)  # MỘT chạm
        s = d.hub.evals.golden.suggestion(sid)
        assert s["status"] == "accepted" and s["decided_by"] == f"user:telegram:{BOSS}"
        assert d.hub.evals.golden.get(s["golden_task_id"])["created_by"] == f"user:telegram:{BOSS}"
        await press(tg, BOSS, reject)  # cùng nhóm → đã tiêu
        await press(tg, BOSS, approve)  # replay
        assert len(d.hub.evals.golden.active_for("a")) == 1
        assert {a["data"]["reason"] for a in d.hub.audit.query(kind="telegram.callback_rejected")} >= {"wrong_chat", "unknown_or_used"}
        assert [a["data"]["method"] for a in d.hub.audit.query(kind="telegram.command")] == ["golden.suggestion_approve"]
        edited = await until(lambda: next((b for b in tg.edits(OTHER) if "đã duyệt" in b["text"]), None))
        assert not FakeTelegram.buttons(edited)
    finally:
        d.hub.runs.shutting_down = True
        await asyncio.wait_for(d.hub.stop(), 15)
        tg.close()


# ============================================================================ review PR #40: người chạm sau lượt quét, đề xuất cũ của gate
def legacy_row(h: Hub, rid: str, sid: str = "gs_legacy01") -> str:
    h.store.conn.execute("INSERT INTO golden_suggestions (id, run_id, agent_id, reason, status, created_at, updated_at) VALUES (?,?,'a','good','open',?,?)",
                         (sid, rid, OLD, OLD))
    return sid


def test_approve_legacy_gate_row_applies_same_filters(hub):
    g = hub.evals.golden
    rid = done_task(hub, "t_rev", title="Refactor the parser module into two files")
    hub.publish(make_event("run.reverted", "a", {"files": ["x.py"]}, run_id=rid))
    sid = legacy_row(hub, rid, "gs_legacy01")
    with pytest.raises(Conflict):  # run đã bị hoàn tác → không thành golden, đề xuất tự bỏ
        g.decide_suggestion(sid, "approve", created_by="user:web")
    assert g.suggestion(sid)["status"] == "dismissed" and g.suggestion(sid)["decided_by"] == "auto:human_touched"
    sid2 = legacy_row(hub, done_task(hub, "t_fail", title="Migrate the database schema to v3", run_status="failed"), "gs_legacy02")
    young = done_task(hub, "t_young", title="Draft the release notes for v2", done_at=datetime.now(timezone.utc).isoformat())
    sid3 = legacy_row(hub, young, "gs_legacy03")
    for s in (sid2, sid3):
        with pytest.raises(HubError):
            g.decide_suggestion(s, "approve", created_by="user:web")
        assert g.suggestion(s)["status"] == "open"
    sid4 = legacy_row(hub, done_task(hub, "t_ok"), "gs_legacy04")
    assert g.decide_suggestion(sid4, "approve", created_by="user:web")["status"] == "accepted"
    assert len(g.active_for("a")) == 1


def test_human_touch_after_scan_blocks_approve_and_auto_dismisses(hub):
    g = hub.evals.golden
    rid = done_task(hub, "t_ok")
    done_task(hub, "t_two", title="Summarize the incident timeline for ops")
    g.auto_suggest()
    ids = sugg_ids(hub)
    # edit_output chỉ được phát hiện ở run sau, undo có thể tới muộn → Signal xuất hiện SAU lượt quét
    hub.store.conn.execute("INSERT INTO signals (id, agent_id, run_id, kind, source, evidence, created_at) VALUES ('s9','a',?,'edit_output','detector','{}',?)",
                           (rid, OLD))
    with pytest.raises(Conflict):
        g.decide_suggestion(ids["t_ok"], "approve", created_by="user:telegram:1")
    assert g.suggestion(ids["t_ok"])["status"] == "dismissed" and g.active_for("a") == []
    hub.store.conn.execute("INSERT INTO signals (id, agent_id, run_id, kind, source, evidence, created_at) VALUES ('s10','a','r_t_two','undo','cli','{}',?)", (OLD,))
    out = g.auto_suggest()
    assert out["skipped"].get("human_touched_after_open") == 1 and g.suggestion(ids["t_two"])["status"] == "dismissed"


def test_human_steer_and_comment_count_as_touched(hub):
    rid = done_task(hub, "t_steer", title="Summarize the incident timeline for ops")
    hub.publish(make_event("user.message", "a", {"text": "also add a chart", "by": "user", "channel": "cli", "steer": True}, run_id=rid))
    done_task(hub, "t_cmt", title="Refactor the parser module into two files")
    hub.tasks.comment("t_cmt", "please keep the old API too", author="human")
    rid3 = done_task(hub, "t_agent", title="Draft the release notes for v2")
    hub.tasks.comment("t_agent", "progress: done", author="agent:a")
    hub.publish(make_event("user.message", "a", {"text": "approval granted", "by": "hub", "channel": "approval", "steer": True}, run_id=rid3))
    out = hub.evals.golden.auto_suggest()
    assert set(sugg_ids(hub)) == {"t_agent"} and out["skipped"] == {"human_touched": 2}


def test_scan_window_uses_hub_timestamp_format(hub):
    from cfharness.events import iso

    now = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
    done_task(hub, "t_edge", done_at=iso(now - timedelta(hours=6)))  # đúng mốc min_age_h, định dạng hub (`…000Z`)
    assert hub.evals.golden.auto_suggest(now=now)["suggested"]


def test_approve_wakes_only_this_agents_deferred_proposals(hub):
    hub.agents.create({"id": "b", "name": "B", "model": "sonnet"})
    done_task(hub, "t_ok")
    hub.evals.golden.auto_suggest()
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    for pid, ag in (("pa", "a"), ("pb", "b")):
        hub.store.conn.execute("INSERT INTO proposals (id, scope, agent_id, target, op, created_by, status, not_before, created_at, updated_at) "
                               "VALUES (?,'agent',?,'config','patch','review','deferred_no_eval',?,?,?)", (pid, ag, later, OLD, OLD))
    hub.evals.golden.decide_suggestion(sugg_ids(hub)["t_ok"], "approve", created_by="user:web")
    nb = dict(hub.store.conn.execute("SELECT id, not_before FROM proposals").fetchall())
    assert nb["pa"] < later and nb["pb"] == later


def test_suggestion_prefix_treats_like_wildcards_literally(hub):
    from cfharness.errors import NotFound

    done_task(hub, "t_ok")
    sid = hub.evals.golden.auto_suggest()["suggested"][0]
    assert hub.evals.golden.suggestion(sid[:6])["id"] == sid
    for ref in ("%%%%", "____", "gs_%"):
        with pytest.raises(NotFound):
            hub.evals.golden.suggestion(ref)


# ============================================================================ Slack: nút một chạm (cùng lõi ChatBot + CallbackStore)
async def test_slack_one_tap_approve_replay_and_cross_chat_cleanup(make, sk):
    from test_slack import BOSS, DM_BOSS, DM_OTHER, OTHER, STRANGER, deliver, until

    d = await make(users=(BOSS, OTHER), chats=(DM_BOSS, DM_OTHER))
    d.hub.agents.create({"id": "a", "name": "A"})
    done_task(d.hub, "t_ok")
    sid = d.hub.evals.golden.auto_suggest()["suggested"][0]
    msg = await until(lambda: sk.msg_for(DM_BOSS, sid))
    other = await until(lambda: sk.msg_for(DM_OTHER, sid))
    content = sk.root_of(DM_BOSS, msg) or next(m for m in sk.sent(DM_BOSS) if "Write the monthly cost report" in sk.text_of(m))
    assert RESULT[:30] in sk.text_of(content) and not sk.buttons(content)
    approve, reject = [b["value"] for b in sk.buttons(msg)]
    await deliver(sk, sk.press(DM_BOSS, STRANGER, approve, msg["_ts"]))  # người lạ / DM khác: không tiêu, không quyết
    await deliver(sk, sk.press(DM_OTHER, OTHER, approve, msg["_ts"]))
    assert d.hub.evals.golden.suggestion(sid)["status"] == "open"
    await deliver(sk, sk.press(DM_BOSS, BOSS, approve, msg["_ts"]))  # MỘT chạm
    s = d.hub.evals.golden.suggestion(sid)
    assert s["status"] == "accepted" and s["decided_by"] == f"user:slack:{DM_BOSS}"
    await deliver(sk, sk.press(DM_BOSS, BOSS, reject, msg["_ts"]))  # cùng nhóm → đã tiêu
    await deliver(sk, sk.press(DM_BOSS, BOSS, approve, msg["_ts"]))  # replay
    assert len(d.hub.evals.golden.active_for("a")) == 1
    assert [a["data"]["method"] for a in d.hub.audit.query(kind="slack.command")] == ["golden.suggestion_approve"]
    upd = await until(lambda: [u for u in sk.calls_of("chat.update") if u.get("channel") == DM_OTHER and u.get("ts") == other["_ts"]])
    assert not sk.buttons(upd[-1])
