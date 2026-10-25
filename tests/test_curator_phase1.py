"""M4-B: curator pha 1 tất định (FR-LN-08). Không gọi model."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from cfharness.events import iso
from cfharness.hub import Hub
from cfharness.learning_ops.curator import plan_transitions
from m3_fake import ScriptedRuntime

NOW = datetime(2026, 10, 18, 8, 0, tzinfo=timezone.utc)


def row(name, *, used_days=None, created_days=60, use_count=1, state="active", pinned=False):
    return {"name": name, "owner_id": "a", "state": state, "pinned": pinned, "use_count": use_count,
            "last_used_at": iso(NOW - timedelta(days=used_days)) if used_days is not None else None, "created_at": iso(NOW - timedelta(days=created_days))}


def test_plan_transitions():
    rows = [
        row("old", used_days=31),  # → archive
        row("stale", used_days=15),  # → stale
        row("fresh", used_days=2),  # không đổi
        row("pinned", used_days=90, pinned=True),  # bỏ qua
        row("young-unused", use_count=0, created_days=5),  # chưa dùng nhưng còn trẻ → không đụng
        row("old-unused", use_count=0, created_days=40),  # chưa dùng, mốc created 40 ngày → archive
        row("back", used_days=1, state="stale"),  # dùng lại → active
        row("gone", used_days=99, state="archived"),  # đã archive
        row("stale-older", used_days=20, state="stale"),  # stale rồi, chưa đủ 30 → giữ
    ]
    plan = {a["name"]: a["action"] for a in plan_transitions(rows, NOW)}
    assert plan == {"old": "archive", "stale": "stale", "old-unused": "archive", "back": "reactivate"}


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {"text": "ok"}))
    h.agents.create({"id": "a", "name": "A"})
    yield h
    h.runs.shutting_down = True
    h.store.close()


class FakeKnowledge:
    def __init__(self, rows):
        self.rows = rows
        self.states: list[tuple] = []

    def list_skills(self, scope, owner):
        return [r for r in self.rows if r["owner_id"] == owner]

    def set_state(self, scope, owner, name, state):
        self.states.append((owner, name, state))

    def archive_skill(self, owner, name, actor, reason):
        self.states.append((owner, name, "archived:" + actor))


async def test_run_with_fake_port_dry_and_live(hub):
    fk = FakeKnowledge([row("old", used_days=31), row("stale", used_days=15), row("pin", used_days=60, pinned=True)])
    hub.knowledge = fk
    evs = []
    hub.bus.add_listener(lambda e: evs.append(e))
    rep = await hub.learning_ops.curator.run(dry_run=True, now=NOW)
    assert rep["dry_run"] and fk.states == [] and rep["counts"]["archived"] == 1 and rep["counts"]["pinned_skipped"] == 1
    assert Path(rep["report_path"]).read_text(encoding="utf-8").startswith("# Curator pha 1")
    rep = await hub.learning_ops.curator.run(now=NOW)
    assert sorted(fk.states) == [("a", "old", "archived:curator"), ("a", "stale", "stale")]
    cr = [e for e in evs if e.type == "curator.report"]
    assert len(cr) == 2 and cr[-1].payload["archived"] == ["a/old"] and cr[-1].payload["marked_stale"] == 1


async def test_without_knowledge_port_reports_only(hub):
    hub.knowledge = None
    rep = await hub.learning_ops.curator.run()
    assert rep["dry_run"] and "chưa có" in rep["note"]


def test_should_run_now_seeds_then_waits_interval_and_idle(hub):
    cur = hub.learning_ops.curator
    assert cur.should_run_now(NOW) is False  # lần đầu: gieo mốc
    assert hub.store.state_get("curator.state")["last_run_at"]
    assert cur.should_run_now(NOW + timedelta(days=3)) is False
    assert cur.should_run_now(NOW + timedelta(days=8)) is True  # không có run nào → rảnh
    hub.store.insert_run({"id": "r1", "agent_id": "a", "kind": "adhoc", "prompt": "x", "status": "completed", "queued_at": iso(), "ended_at": iso()})
    assert cur.should_run_now(NOW + timedelta(days=8)) is False  # vừa có run → chưa rảnh 30 phút


async def test_archive_through_m4a_ledger(hub):
    """Tích hợp với kho tri thức + ledger thật của M4-A (nếu có): archive = xoá mềm, ledger actor=curator, rollback được."""
    ks = getattr(hub, "knowledge", None)
    ledger = getattr(getattr(hub, "learning", None), "ledger", None)
    if ks is None or ledger is None or not hasattr(ks, "skill_dir"):
        pytest.skip("M4-A chưa gắn hub.knowledge / hub.learning.ledger")
    d = ks.skill_dir("agent", "a", "old-skill")
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: old-skill\ndescription: Old thing\n---\nBody\n", encoding="utf-8")
    ks.sync_index("agent", "a")
    old = iso(datetime.now(timezone.utc) - timedelta(days=40))
    hub.store.conn.execute("UPDATE skills SET last_used_at=?, use_count=3, created_at=? WHERE name='old-skill'", (old, old))
    rep = await hub.learning_ops.curator.run()
    assert rep["counts"]["archived"] == 1, rep
    assert not d.exists()
    entries = ledger.list("a", actor="curator")
    assert entries and entries[0]["op"] == "archive"
    ledger.rollback(entries[0]["id"], actor="user", reason="khôi phục")
    assert (d / "SKILL.md").exists()
