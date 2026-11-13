"""P0-9 (docs/dev-notes/plan-chat-ui-claude.md, Q4): suy nghĩ của agent ghi DB (`reasoning.complete`) và trang run phát lại
được sau khi tải lại. Chạy bằng FakeRuntime + fixture `claude-thinking.jsonl` — KHÔNG gọi claude thật."""

from __future__ import annotations

from conftest import FIXTURES

from cfharness.config import DEFAULTS
from cfharness.hub import Hub
from cfharness.runtime import FakeRuntime
from cfharness.web import present as P
from cfharness.web.app import replay_items

FIX = FIXTURES / "claude-thinking.jsonl"


def test_defaults():
    c = DEFAULTS["runtime"]["claude"]
    assert c["persist_thinking"] is True and c["thinking_max_chars"] == 8000


async def _run(hub_home, **cfg):
    h = Hub(hub_home, runtime=FakeRuntime(FIX))
    h.start()
    try:
        for k, v in cfg.items():
            h.config.set(f"runtime.claude.{k}", v)
        h.agents.create({"id": "a", "name": "A"})
        run = h.runs.submit("a", "1+3?")
        final = await h.runs.wait(run["id"], 20)
        assert final["outcome"] == "completed", final.get("error")
        return [e.to_wire() for e in h.store.query_events(run_id=run["id"])]
    finally:
        h.store.close()


async def test_reasoning_persisted_and_replayed(hub_home):
    evs = await _run(hub_home)
    rc = [e for e in evs if e["type"] == "reasoning.complete"]
    assert [e["payload"]["redacted"] for e in rc] == [False, True]
    assert "sk-ant-" not in str(rc)
    assert not [e for e in evs if e["type"] == "reasoning.delta"]  # delta vẫn không ghi DB
    items = replay_items(evs)
    reasoning = [i for i in items if i["kind"] == "reasoning"]
    assert [i["text"] for i in reasoning] == ["The user asks for the sum. Key in env is ***, never echo it.", "(suy nghĩ bị ẩn)"]
    kinds = [i["kind"] for i in items]
    assert kinds.index("reasoning") < kinds.index("msg")


async def test_persist_thinking_off_no_db_rows(hub_home):
    evs = await _run(hub_home, persist_thinking=False)
    assert not [e for e in evs if e["type"] == "reasoning.complete"]
    assert not [i for i in replay_items(evs) if i["kind"] == "reasoning"]


async def test_thinking_max_chars_from_config(hub_home):
    evs = await _run(hub_home, thinking_max_chars=9)
    think = next(e for e in evs if e["type"] == "reasoning.complete" and not e["payload"]["redacted"])
    assert think["payload"]["text"] == "The user " and think["payload"]["truncated"] is True


def test_subagent_reasoning_hidden_like_subagent_text():
    ev = {"type": "reasoning.complete", "seq": 1, "subagent_id": "toolu_p", "payload": {"text": "x", "redacted": False}}
    assert P.transcript_item(ev) is None
