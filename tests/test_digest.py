"""M4-B: digest học (FR-LN-13, Q-18) — dựng từ event, lịch 08:00 giờ máy, gửi Slack giả, TUI lần đầu trong ngày."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from agent_hub.events import make_event
from agent_hub.hub import Hub
from agent_hub.notify import format_message
from m3_fake import ScriptedRuntime
from test_notify_slack import FakeSlack


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {"text": "ok"}))
    for a in ("researcher", "coder"):
        h.agents.create({"id": a, "name": a})
    h.teams.create({"id": "docs", "name": "Docs", "members": [{"agent_id": "researcher", "role": "worker"}]})
    yield h
    h.runs.shutting_down = True
    h.store.close()


def emit(h, typ, agent, **p):
    h.publish(make_event(typ, agent, p))


def seed(h):
    emit(h, "learning.applied", "researcher", proposal_id="p1", ledger_entry_id="le_1", scope="agent", target="skill", target_ref="vb-lookup", op="patch",
         risk_class="L1", diff="--- a\n+++ b\n+line1\n+line2\n+line3\n-old\n")
    emit(h, "learning.rolled_back", "researcher", ledger_entry_id="le_0", actor="regression_monitor", reason="tỉ lệ correction 0% → 30%")
    emit(h, "learning.eval_failed", "coder", proposal_id="p2", before=0.9, after=0.5)
    emit(h, "learning.deferred", "coder", proposal_id="p3", reason="no_eval")
    emit(h, "guard.blocked", "coder", proposal_id="p4", source="learning", reason="policy_touch", detail="đề xuất sửa danh mục phá huỷ")
    emit(h, "golden.suggested", "coder", reason="deferred_no_eval", proposal_id="p3", candidates=[{"run_id": "r_abc", "reason": "good", "prompt_preview": "Fix bug"}])
    emit(h, "curator.report", None, dry_run=False, checked=5, marked_stale=1, archived=1, reactivated=0)
    h.store.insert_run({"id": "r_rev", "agent_id": "researcher", "kind": "review", "prompt": "x", "status": "completed", "queued_at": datetime.now().astimezone().isoformat(),
                        "cost_usd": 0.05})


def test_digest_content(hub):
    seed(hub)
    d = hub.learning_ops.digest.run(since="2000-01-01T00:00:00Z", send=False)
    t = d["text"]
    assert d["counts"]["applied"] == 1 and d["counts"]["rolled_back"] == 1 and d["counts"]["blocked"] == 1 and d["counts"]["eval_failed"] == 1
    assert "*Team docs*" in t and "`researcher`" in t and "`coder`" in t
    assert "agent-hub learn rollback le_1" in t and "+3/−1 dòng" in t
    assert "regression_monitor" in t and "0.9 → 0.5" in t and "🔴" in t
    assert "agent-hub golden from-run r_abc" in t and "Q-23" in t
    assert "curator" in t and "chưa có golden task" in t
    row = hub.learning_ops.digest.get(d["id"])
    assert row["slack_status"] == "skipped" and row["counts"]["applied"] == 1
    # digest kế tiếp bắt đầu từ cuối digest trước → rỗng
    d2 = hub.learning_ops.digest.run(send=False)
    assert d2["counts"]["applied"] == 0 and "Không có thay đổi" in d2["text"]


def test_schedule_due(hub):
    dg = hub.learning_ops.digest
    tz = datetime.now().astimezone().tzinfo
    assert dg.due(datetime(2026, 10, 18, 7, 59, tzinfo=tz)) is False
    assert dg.due(datetime(2026, 10, 18, 8, 0, tzinfo=tz)) is True
    hub.store.state_set("digest.last_local_date", "2026-10-18")
    assert dg.due(datetime(2026, 10, 18, 9, 0, tzinfo=tz)) is False
    assert dg.due(datetime(2026, 10, 19, 8, 1, tzinfo=tz)) is True
    hub.config.set("digest.schedule", "21:30")
    assert dg.due(datetime(2026, 10, 19, 21, 0, tzinfo=tz)) is False
    hub.config.set("digest.schedule", 21 * 60 + 30)  # YAML đọc 21:30 không nháy thành số cơ số 60
    assert dg.due(datetime(2026, 10, 19, 21, 0, tzinfo=tz)) is False and dg.due(datetime(2026, 10, 19, 21, 31, tzinfo=tz)) is True


def test_tui_first_open_of_day(hub):
    seed(hub)
    hub.learning_ops.digest.run(since="2000-01-01T00:00:00Z", send=False)
    emit(hub, "learning.applied", "coder", ledger_entry_id="le_9", target="memory", target_ref="MEMORY.md", op="add", risk_class="L1")
    hub.learning_ops.digest.run(send=False)
    r = hub.learning_ops.digest.tui_open()
    assert r["show"] and len(r["digests"]) == 2 and "le_9" in r["text"] and "le_1" in r["text"]  # gộp nhiều digest chưa xem
    r2 = hub.learning_ops.digest.tui_open()
    assert r2["show"] is False and r2["first_open_today"] is False


async def test_scheduled_digest_sent_to_fake_slack(hub):
    slack = FakeSlack()
    try:
        hub.config.set("notify.slack", {**hub.config.get("notify.slack"), "webhook_url": slack.url, "backoff_base_s": 0.05})
        hub.start()
        seed(hub)
        tz = datetime.now().astimezone().tzinfo
        out = await hub.learning_ops.tick(now_local=datetime.now(tz).replace(hour=8, minute=5))
        assert out.get("digest") and out["digest"]["slack"] == "queued"
        for _ in range(100):
            await asyncio.sleep(0.02)
            if any("digest học" in m["text"] for m in slack.received):
                break
        texts = [m["text"] for m in slack.received]
        dig = [t for t in texts if "digest học" in t]
        assert dig and "agent-hub learn rollback le_1" in dig[0]
        assert any("deferred_no_eval" in t and "golden from-run r_abc" in t for t in texts)  # nhắc golden (Q-23)
        for _ in range(50):
            await asyncio.sleep(0.02)
            if hub.learning_ops.digest.get(out["digest"]["id"])["slack_status"] == "sent":
                break
        assert hub.learning_ops.digest.get(out["digest"]["id"])["slack_status"] == "sent"
        again = await hub.learning_ops.tick(now_local=datetime.now(tz).replace(hour=9))
        assert "digest" not in again  # mỗi ngày một lần
    finally:
        slack.close()


def test_slack_format_messages():
    e = make_event("learning.digest", None, {"send": True, "text": "*agent-hub — digest học*\nx"})
    assert format_message(e).startswith("*agent-hub — digest học*")
    assert format_message(make_event("learning.digest", None, {"send": False, "text": "x"})) is None
    g = make_event("golden.suggested", "coder", {"reason": "unevaluated", "candidates": []})
    assert format_message(g) is None
    g = make_event("golden.suggested", "coder", {"reason": "deferred_no_eval", "candidates": []})
    assert "golden add coder" in format_message(g)


async def test_rpc_digest_and_learn_alias(hub_home):
    from agent_hub.daemon import Daemon

    d = Daemon(hub_home, runtime=ScriptedRuntime(lambda s: {"text": "ok"}))
    try:
        d.hub.agents.create({"id": "a", "name": "A"})
        r = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "digest.run", "params": {"no_send": True}})
        assert r["result"]["slack"] == "skipped"
        r = await d._dispatch({"jsonrpc": "2.0", "id": 2, "method": "digest.list", "params": {}})
        assert len(r["result"]) == 1
        r = await d._dispatch({"jsonrpc": "2.0", "id": 3, "method": "digest.show", "params": {}})
        assert "digest học" in r["result"]["text"]
        r = await d._dispatch({"jsonrpc": "2.0", "id": 4, "method": "curator.status", "params": {}})
        assert "enabled" in r["result"]
    finally:
        d.hub.runs.shutting_down = True
        d.hub.store.close()
