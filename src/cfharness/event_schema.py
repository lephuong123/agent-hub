"""Hợp đồng event có phiên bản (NFR-23, hardening H-20): JSON Schema của envelope `HubEvent` (dạng wire, `to_wire()`).

`python -m cfharness.event_schema [--write docs/schema/event.v1.json]` sinh lại file. Test `tests/test_event_schema.py`
so file đã commit với schema sinh từ code (envelope phải KHỚP; danh mục loại event chỉ được THÊM, không bớt) và kiểm mọi
event phát ra từ fixture thật hợp lệ theo schema.

Quy tắc tiến hoá: thêm trường tuỳ chọn / loại event mới = tương thích (v giữ 1); đổi tên/xoá trường hoặc đổi kiểu = tăng
`SCHEMA_VERSION` và tạo `event.v2.json`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .events import EVENT_LEVELS, NOT_PERSISTED, SCHEMA_VERSION, HubEvent

SCHEMA_ID = f"https://cfharness.local/schema/event.v{SCHEMA_VERSION}.json"


def envelope_schema() -> dict[str, Any]:
    s = HubEvent.model_json_schema(mode="serialization")
    props = s["properties"]
    props["v"] = {"const": SCHEMA_VERSION, "title": "V", "description": "phiên bản schema envelope"}
    props["ts"] = {"type": "string", "format": "date-time", "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", "title": "Ts",
                   "description": "UTC ISO-8601 mili giây, hậu tố Z"}
    props["level"] = {"enum": ["summary", "detail", "debug"], "title": "Level", "type": "string"}
    props["source"] = {"enum": ["cli", "hub", "tui", "mcp", "hook"], "title": "Source", "type": "string"}
    props["seq"] = {**props["seq"], "minimum": 0}
    props["type"] = {**props["type"], "pattern": r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$"}
    required = sorted(set(s.get("required", [])) | {"v", "epoch", "seq", "ts", "type", "level", "source", "payload"})
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "HubEvent",
        "description": "Envelope event của cfharness (BRD §9.1). Loại event không có trong x-event-types vẫn hợp lệ (mở rộng).",
        "type": "object",
        "properties": dict(sorted(props.items())),
        "required": required,
        "additionalProperties": False,
        "x-event-types": dict(sorted(EVENT_LEVELS.items())),
        "x-not-persisted": sorted(NOT_PERSISTED),
    }


def render() -> str:
    return json.dumps(envelope_schema(), ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--write"]:
        out = Path(argv[1] if len(argv) > 1 else "docs/schema/event.v1.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(), encoding="utf-8")
        print(f"đã ghi {out}")
    else:
        sys.stdout.write(render())
    return 0


if __name__ == "__main__":
    sys.exit(main())
