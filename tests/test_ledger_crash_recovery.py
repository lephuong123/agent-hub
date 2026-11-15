"""S8-B: `Ledger.apply` an toàn khi daemon chết giữa chừng (write-ahead `ledger_pending` + `recover_pending` lúc Hub khởi động).

Mô phỏng "chết" bằng ngoại lệ `Crash(BaseException)` ở từng bước (sau ghi ý định, sau ghi đích, trong transaction commit; graph:
giữa ghi hàng graph và ghi entry), rồi chạy recovery và kiểm bất biến: mọi thay đổi trên đĩa do ledger tạo đều CÓ entry, hoặc đích
vẫn ở trạng thái trước, hoặc hàng pending bị gắn cờ (giữ snapshot before). Rollback vẫn chạy được sau recovery. Không gọi claude.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import pytest
from m3_fake import ScriptedRuntime
from m5a_fake import make_hub

from cfharness.hub import Hub
from cfharness.knowledge.graph import GraphStore
from cfharness.learning.ledger import Change, Ledger
from cfharness.store import Store


class Crash(BaseException):
    """Tiến trình chết: không phải Exception → không nhánh `except Exception` nào dọn dẹp (như SIGKILL)."""


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


SKILL = b"---\nname: demo\ndescription: demo skill\n---\nDo the thing.\n"


def mem_change(h: Hub, text: str = "- The deploy target is staging.") -> Change:
    p = h.knowledge.memory_path("agent", "r1", "memory")
    return Change(scope="agent", owner_id="r1", target="memory", target_ref="MEMORY.md", op="add", actor="review", kind="file", path=p,
                  after=(text + "\n").encode(), agent_id="r1")


def skill_change(h: Hub) -> Change:
    return Change(scope="agent", owner_id="r1", target="skill", target_ref="demo", op="add", actor="review", kind="tree",
                  path=h.knowledge.skill_dir("agent", "r1", "demo"), files={"SKILL.md": SKILL}, replace_tree=True, agent_id="r1")


def cfg_change(h: Hub) -> Change:
    return Change(scope="agent", owner_id="r1", target="config", target_ref="effort", op="patch", actor="review", kind="config",
                  config_changes={"effort": "medium"}, agent_id="r1")


BUILDERS = {"memory": mem_change, "skill": skill_change, "config": cfg_change}


def sha(b: bytes | None) -> str | None:
    return hashlib.sha256(b).hexdigest() if b is not None else None


def crash_at(monkeypatch, stage: str) -> None:
    if stage == "after_intent":  # ý định đã commit, đích chưa ghi
        monkeypatch.setattr(Ledger, "_write_target", lambda self, ch: (_ for _ in ()).throw(Crash(stage)))
    elif stage == "after_write":  # đích đã ghi, chưa vào transaction commit
        monkeypatch.setattr(Ledger, "_commit", lambda self, *a, **k: (_ for _ in ()).throw(Crash(stage)))
    elif stage == "before_entry":  # trong transaction commit, ngay trước INSERT ledger_entries (có trên code cũ)
        orig = Store._insert

        def _ins(self, table: str, data: dict[str, Any]) -> None:
            if table == "ledger_entries":
                raise Crash(stage)
            orig(self, table, data)

        monkeypatch.setattr(Store, "_insert", _ins)
    else:  # pragma: no cover
        raise AssertionError(stage)


def assert_invariant(h: Hub, ch: Change, before: bytes | None) -> str:
    """Sau recovery: không còn pending chưa xử lý; đích == before (không entry), hoặc có entry khớp đích, hoặc pending bị gắn cờ."""
    L = h.learning.ledger
    assert L.pending("pending") == []
    cur = L.state_bytes(ch)
    entries = L.list("r1", target=ch.target)
    if L.pending("flagged"):
        return "flagged"
    if entries:
        assert sha(cur) == entries[0]["after_blob"], "đích đã đổi nhưng entry không khớp"
        return "completed"
    assert sha(cur) == sha(before), "đích đã đổi mà không có entry ledger, không bị revert, không bị gắn cờ"
    return "discarded"


@pytest.mark.parametrize("stage, expected", [("after_intent", "discarded"), ("after_write", "completed"), ("before_entry", "completed")])
@pytest.mark.parametrize("kind", ["memory", "skill", "config"])
def test_crash_mid_apply_then_recovery_keeps_ledger_consistent(hub, monkeypatch, kind, stage, expected):
    ch = BUILDERS[kind](hub)
    before = hub.learning.ledger.state_bytes(ch)
    with monkeypatch.context() as m:
        crash_at(m, stage)
        with pytest.raises(Crash):
            hub.learning.ledger.apply(ch)
    assert hub.learning.ledger.pending("pending"), "ý định phải được ghi TRƯỚC khi đụng đích"
    res = hub.learning.ledger.recover_pending()
    assert assert_invariant(hub, ch, before) == expected
    assert sum(len(v) for v in res.values()) == 1
    ev = [e for e in hub.events if e.type == "learning.ledger_recovery"]
    assert ev and ev[-1].payload["outcome"] == expected
    if expected == "completed":
        e = hub.learning.ledger.list("r1", target=ch.target)[0]
        assert "recovered" in e["flags"] and e["before_blob"] == sha(before)
        hub.learning.ledger.rollback(e["id"], actor="user")  # rollback vẫn chạy sau recovery
        assert sha(hub.learning.ledger.state_bytes(ch)) == sha(before) or kind == "skill"
        if kind == "skill":
            assert not ch.path.exists()  # skill do entry tạo → rollback = xoá mềm vào .archive
    # chạy lại recovery là no-op
    assert hub.learning.ledger.recover_pending() == {"completed": [], "discarded": [], "flagged": []}


@pytest.mark.parametrize("kind", ["memory", "skill", "config"])
def test_normal_apply_predicts_after_and_leaves_no_pending(hub, kind):
    """Hash after dự kiến (ghi trong ý định) phải KHỚP trạng thái thật — nếu không recovery sẽ gắn cờ thay vì hoàn tất."""
    seen: list[tuple[bool, str | None]] = []
    L = hub.learning.ledger
    orig = L._expected_after
    L._expected_after = lambda ch, b: seen.append(orig(ch, b)) or seen[-1]  # type: ignore[method-assign]
    e = L.apply(BUILDERS[kind](hub))
    assert seen == [(True, e["after_blob"])]
    assert L.pending() == []


def test_skill_archive_crash_after_move_is_completed_with_archive_path(hub, monkeypatch):
    L = hub.learning.ledger
    L.apply(skill_change(hub))
    ch = Change(scope="agent", owner_id="r1", target="skill", target_ref="demo", op="archive", actor="curator", kind="tree",
                path=hub.knowledge.skill_dir("agent", "r1", "demo"), archive=True, agent_id="r1")
    with monkeypatch.context() as m:
        crash_at(m, "after_write")
        with pytest.raises(Crash):
            L.apply(ch)
    assert not ch.path.exists()
    assert L.recover_pending()["completed"]
    e = L.list("r1", target="skill")[0]
    assert e["op"] == "archive" and "→" in e["summary"]
    row = hub.store.conn.execute("SELECT state, archived_path FROM skills WHERE name='demo'").fetchone()
    assert row["state"] == "archived" and row["archived_path"]
    L.rollback(e["id"], actor="user")
    assert (ch.path / "SKILL.md").read_bytes() == SKILL


def test_target_changed_after_crash_is_flagged_and_snapshot_kept(hub, monkeypatch):
    """Đích bị sửa (người) sau khi daemon chết → không khớp before/after → gắn cờ + cảnh báo, giữ snapshot before, không ghi đè."""
    L = hub.learning.ledger
    L.apply(mem_change(hub, "- First fact."))
    ch = mem_change(hub, "- First fact.\n- Second fact.")
    before = L.state_bytes(ch)
    with monkeypatch.context() as m:
        crash_at(m, "after_write")
        with pytest.raises(Crash):
            L.apply(ch)
    ch.path.write_text("- Human edit.\n", encoding="utf-8")
    assert L.recover_pending()["flagged"]
    assert ch.path.read_text(encoding="utf-8") == "- Human edit.\n"
    fl = L.pending("flagged")
    assert len(fl) == 1 and fl[0]["detail"]
    snap = hub.store.conn.execute("SELECT blob FROM snapshots WHERE id=?", (fl[0]["snapshot_id"],)).fetchone()
    assert snap["blob"] == sha(before) and L.blobs.get(snap["blob"]) == before
    ev = [e for e in hub.events if e.type == "learning.ledger_recovery"][-1]
    assert ev.payload["outcome"] == "flagged" and ev.payload["warning"] is True and ev.payload["snapshot_id"] == fl[0]["snapshot_id"]
    assert L.recover_pending()["flagged"] == []  # không phát lại cảnh báo mỗi lần


def test_ordinary_error_in_write_drops_intent_immediately(hub, monkeypatch):
    """Lỗi thường (không chết) khi chưa đụng đích → bỏ ý định ngay, không để lại pending."""
    from cfharness.errors import HubError

    monkeypatch.setattr(Ledger, "_write_target", lambda self, ch: (_ for _ in ()).throw(HubError("validation")))
    with pytest.raises(HubError):
        hub.learning.ledger.apply(cfg_change(hub))
    assert hub.learning.ledger.pending() == []


# ============================================================================ graph: hàng graph ghi CÙNG transaction với entry
@pytest.fixture
def ghub(hub_home):
    h = make_hub(hub_home)
    yield h
    h.store.close()


def gfact(text: str) -> dict[str, Any]:
    return {"action": "add_fact", "src": "deploy", "relation": "target", "dst": None, "fact": text}


def live(h: Hub) -> list[str]:
    return [f["fact"] for f in GraphStore(h.store.conn).facts([("agent", "a1")])]


@pytest.mark.parametrize("stage", ["after_intent", "after_write", "between_graph_and_entry"])
def test_graph_crash_never_leaves_rows_without_entry(ghub, monkeypatch, stage):
    with monkeypatch.context() as m:
        if stage == "between_graph_and_entry":
            orig = GraphStore.write_rows

            def _write(self, rows, eid):
                orig(self, rows, eid)
                raise Crash(stage)  # hàng graph đã ghi trong transaction, entry chưa

            m.setattr(GraphStore, "write_rows", _write)
        else:
            crash_at(m, stage)
        with pytest.raises(Crash):
            ghub.learning.graph_write("a1", [gfact("Deploys go to staging first.")], created_by="review")
    assert live(ghub) == []  # transaction bị huỷ → không có hàng graph mồ côi
    assert ghub.store.conn.execute("SELECT COUNT(*) FROM ledger_entries WHERE blob_kind='graph'").fetchone()[0] == 0
    assert ghub.learning.ledger.recover_pending()["discarded"]
    assert ghub.learning.ledger.pending() == []
    # sau recovery vẫn ghi + rollback bình thường
    r = ghub.learning.graph_write("a1", [gfact("Deploys go to staging first.")], created_by="review")
    assert r["status"] == "applied" and live(ghub) == ["Deploys go to staging first."]
    ghub.learning.ledger.rollback(r["ledger_entry_id"], actor="user")
    assert live(ghub) == []


# ============================================================================ gắn vào đường khởi động Hub
def test_hub_start_runs_ledger_recovery(hub_home, monkeypatch):
    h = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {"text": "ok"}))
    h.agents.create({"id": "r1", "name": "R1", "model": "sonnet"})
    ch = mem_change(h)
    with monkeypatch.context() as m:
        crash_at(m, "after_write")
        with pytest.raises(Crash):
            h.learning.ledger.apply(ch)
    h.runs.shutting_down = True
    h.store.close()

    async def restart() -> None:
        h2 = Hub(hub_home, runtime=ScriptedRuntime(lambda s: {"text": "ok"}))
        try:
            h2.start()
            assert h2.learning.ledger.pending() == []
            e = h2.learning.ledger.list("r1", target="memory")
            assert len(e) == 1 and "recovered" in e[0]["flags"]
        finally:
            h2.runs.shutting_down = True
            h2.auto_resume.stop()
            h2.store.close()

    asyncio.run(restart())
