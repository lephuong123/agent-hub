"""Chạy ClaudeStreamAdapter trên file stream-json THẬT của `claude -p` (research/notes)."""

from pathlib import Path

import pytest

from agent_hub_demo.claude_adapter import ClaudeStreamAdapter, adapt_lines
from agent_hub_demo.events import ALL_EVENT_TYPES, HubEvent

SAMPLE = Path(__file__).resolve().parents[2] / "research" / "notes" / "claude-stream-sample.jsonl"


@pytest.fixture(scope="module")
def events() -> list[HubEvent]:
    assert SAMPLE.exists(), f"thiếu file mẫu {SAMPLE}"
    return adapt_lines("a1", SAMPLE.read_text().splitlines(), run_id="r1")


def test_types_hop_le(events):
    assert events
    for e in events:
        assert e.type in ALL_EVENT_TYPES
        assert e.agent_id == "a1" and e.run_id == "r1"


def test_init_thanh_turn_start(events):
    types = [e.type for e in events]
    assert types[0] == "turn.start"
    assert events[0].payload["model"] == "claude-haiku-5-5"
    assert events[0].session_id == "e716ae1d-3b52-4ead-9779-1f6eab1de0aa"
    assert "Bash" in events[0].payload["tools"]
    assert types[1] == "agent.status" and events[1].payload["status"] == "working"


def test_tool_start_va_complete(events):
    starts = [e for e in events if e.type == "tool.start"]
    dones = [e for e in events if e.type == "tool.complete"]
    assert len(starts) == 1 and len(dones) == 1
    assert starts[0].payload["name"] == "Bash"
    assert starts[0].payload["summary"] == "ls"
    assert dones[0].payload["tool_use_id"] == starts[0].payload["tool_use_id"]
    assert dones[0].payload["summary"] == "a.txt"
    assert dones[0].payload["is_error"] is False
    # thứ tự: tool.start trước tool.complete trước câu trả lời
    types = [e.type for e in events]
    assert types.index("tool.start") < types.index("tool.complete") < types.index("message.delta")


def test_chu_khong_bi_nhan_doi(events):
    """Có cả text_delta (partial) lẫn message `assistant` đầy đủ → chỉ phát chữ một lần."""
    text = "".join(e.payload["text"] for e in events if e.type == "message.delta")
    assert text == "xong"


def test_reasoning_co_uoc_luong_token(events):
    r = [e for e in events if e.type == "reasoning.delta"]
    assert r and r[0].payload["estimated_tokens"] == 50


def test_result_thanh_turn_complete(events):
    tc = [e for e in events if e.type == "turn.complete"]
    assert len(tc) == 1
    p = tc[0].payload
    assert p["status"] == "success"
    assert p["cost_usd"] == pytest.approx(0.00474188)
    assert p["output_tokens"] == 184
    assert p["total_tokens"] == 4 + 22152 + 21908 + 184
    assert p["num_turns"] == 2 and p["result"] == "xong"
    ticks = [e for e in events if e.type == "usage.tick"]
    assert ticks[-1].payload["estimated"] is False
    assert ticks[-1].payload["context_window"] == 1_000_000  # lấy từ modelUsage
    assert events[-1].type == "agent.status" and events[-1].payload["status"] == "idle"


def test_bo_qua_hook_va_rate_limit():
    ad = ClaudeStreamAdapter("a1")
    assert ad.feed('{"type":"system","subtype":"hook_started","session_id":"s"}') == []
    assert ad.feed('{"type":"rate_limit_event","rate_limit_info":{}}') == []
    assert ad.feed("") == []
    bad = ad.feed("không phải json")
    assert bad[0].type == "error"


def test_result_loi():
    ad = ClaudeStreamAdapter("a5")
    evs = ad.feed({"type": "result", "subtype": "error_during_execution", "is_error": True,
                   "result": "ECONNREFUSED", "total_cost_usd": 0.01, "usage": {"output_tokens": 5}})
    types = [e.type for e in evs]
    assert types == ["usage.tick", "error", "turn.complete", "agent.status"]
    assert evs[-1].payload["status"] == "error"


def test_khong_partial_messages_van_co_chu():
    """Chạy không có --include-partial-messages: chữ lấy từ message `assistant`."""
    ad = ClaudeStreamAdapter("a1")
    evs = ad.feed({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "chào"}]}})
    assert [e.type for e in evs] == ["message.delta"] and evs[0].payload["text"] == "chào"
