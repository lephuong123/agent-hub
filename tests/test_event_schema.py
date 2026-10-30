"""Hợp đồng event (NFR-23, hardening H-20): `docs/schema/event.v1.json` là snapshot của envelope `HubEvent`.

- envelope sinh từ code phải KHỚP file đã commit (đổi envelope = sinh lại bằng `python -m cfharness.event_schema --write`
  và cân nhắc tăng `SCHEMA_VERSION`);
- danh mục loại event chỉ được THÊM (loại đã công bố không được biến mất/đổi level), và loại mới phải có trong snapshot (sinh lại);
- mọi event phát ra từ fixture stream-json thật + event hub hợp lệ theo schema.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from cfharness.claude_adapter import ClaudeStreamAdapter
from cfharness.event_schema import envelope_schema
from cfharness.events import EVENT_LEVELS, make_event

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = ROOT / "docs" / "schema" / "event.v1.json"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def committed():
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def test_envelope_matches_snapshot(committed):
    gen = envelope_schema()
    for k in ("properties", "required", "additionalProperties", "type", "$id"):
        assert gen[k] == committed[k], f"envelope đổi ở '{k}' — chạy `python -m cfharness.event_schema --write docs/schema/event.v1.json`"


def test_event_types_additive_only(committed):
    published = committed["x-event-types"]
    missing = sorted(t for t in published if t not in EVENT_LEVELS)
    assert not missing, f"loại event đã công bố bị xoá: {missing}"
    changed = sorted(t for t, lvl in published.items() if EVENT_LEVELS.get(t) not in (None, lvl))
    assert not changed, f"đổi level loại event đã công bố: {changed}"
    pat = committed["properties"]["type"]["pattern"]
    import re

    bad = [t for t in EVENT_LEVELS if not re.match(pat, t)]
    assert not bad, f"tên loại event sai mẫu: {bad}"


def test_snapshot_lists_every_event_type(committed):
    """Loại event mới (vd `guard.trust`) phải có trong snapshot đã commit — trước đây chỉ kiểm "không bớt" nên quên sinh lại vẫn xanh."""
    gen = envelope_schema()
    hint = "chạy `python -m cfharness.event_schema --write docs/schema/event.v1.json`"
    unpublished = sorted(set(gen["x-event-types"]) - set(committed["x-event-types"]))
    assert not unpublished, f"loại event chưa có trong snapshot: {unpublished} — {hint}"
    assert committed["x-not-persisted"] == gen["x-not-persisted"], hint
    assert committed == gen, hint


def _fixture_events():
    out = []
    for f in sorted(FIXTURES.rglob("*.jsonl")):  # gồm fixture theo phiên bản CLI (claude-<ver>/)
        ad = ClaudeStreamAdapter("agent-x", "r_1")
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.extend(ad.feed(line.encode("utf-8") if isinstance(line, str) else line))
    return out


def test_emitted_events_validate(committed):
    validator = jsonschema.Draft202012Validator(committed, format_checker=jsonschema.FormatChecker())
    evs = _fixture_events()
    evs += [make_event("run.queued", "a", {"x": 1}, run_id="r_1"), make_event("hook.timeout", None, {}, source="hook")]
    assert len(evs) > 20
    for i, ev in enumerate(evs):
        w = ev.to_wire()
        w["epoch"], w["seq"] = "e-test", i + 1
        errs = sorted(validator.iter_errors(w), key=lambda e: e.path)
        assert not errs, (ev.type, [e.message for e in errs])
    bad = make_event("run.queued", "a", {}).to_wire()
    bad["level"] = "loud"
    assert list(validator.iter_errors(bad))
