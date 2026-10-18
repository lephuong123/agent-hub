"""Tiện ích test M3: `ScriptedRuntime` — FakeRuntime sinh stream-json theo kịch bản (không gọi claude, không tốn tiền).

`script(spec) -> dict` quyết định hành vi của từng run:
  text: str               — text cuối (summary)
  structured: dict|None   — `structured_output` của result (run có --json-schema)
  cost: float             — chi phí run (hub tính chênh lệch luỹ kế session như CLI thật)
  subtype: str            — "success" | "error_during_execution" | "error_max_budget_usd" …
  error_text: str         — text lỗi (vd "rate limit exceeded")
  crash: bool             — không phát `result` (tiến trình chết)
  hold: bool              — giữ trước dòng result tới khi `release(run_id)`
  files: {rel: str|None}  — ghi/xoá file trong cwd của run lúc start (mô phỏng agent sửa repo)
  rate_limit: dict        — phát rate_limit_event với rate_limit_info này
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from agent_hub.runtime import FakeRuntime
from agent_hub.runtime.base import RunHandle, RunSpec


class ScriptedRuntime(FakeRuntime):
    def __init__(self, script: Callable[[RunSpec], dict[str, Any]], **kw: Any) -> None:
        super().__init__(**kw)
        self.script = script
        self.behaviors: dict[str, dict[str, Any]] = {}
        self.session_cost: dict[str, float] = {}
        self.specs: dict[str, RunSpec] = {}

    async def start(self, spec: RunSpec) -> RunHandle:
        b = dict(self.script(spec) or {})
        self.behaviors[spec.run_id] = b
        self.specs[spec.run_id] = spec
        h = await super().start(spec)
        if b.get("hold"):
            import asyncio

            self.gates[spec.run_id] = asyncio.Event()
        for rel, content in (b.get("files") or {}).items():
            p = Path(spec.cwd) / rel
            if content is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
        return h

    def _lines(self, spec: RunSpec, fixture: Path | None = None) -> list[str]:
        b = self.behaviors.get(spec.run_id) or {}
        sid = spec.resume_session_id or spec.session_id
        out = [
            {"type": "system", "subtype": "init", "session_id": sid, "cwd": str(spec.cwd), "model": "claude-haiku-5-5", "tools": [], "mcp_servers": [],
             "plugins": [], "memory_paths": None, "claude_code_version": "2.1.302"},
            {"type": "assistant", "session_id": sid, "message": {"id": "m1", "content": [{"type": "text", "text": b.get("text", "done")}]}},
        ]
        if b.get("rate_limit"):
            out.append({"type": "rate_limit_event", "session_id": sid, "rate_limit_info": b["rate_limit"]})
        if not b.get("crash"):
            prev = self.session_cost.get(sid, 0.0)
            total = prev + float(b.get("cost", 0.001))
            self.session_cost[sid] = total
            sub = b.get("subtype", "success")
            res = {"type": "result", "subtype": sub, "is_error": sub != "success", "session_id": sid, "total_cost_usd": total,
                   "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20},
                   "modelUsage": {"claude-haiku-5-5": {"costBasis": "list"}}, "duration_ms": 5, "num_turns": 1,
                   "result": b.get("error_text") if sub != "success" else b.get("text", "done"), "terminal_reason": "completed"}
            if b.get("structured") is not None:
                res["structured_output"] = b["structured"]
            if b.get("api_error_status"):
                res["api_error_status"] = b["api_error_status"]
            out.append(res)
        return [json.dumps(o, ensure_ascii=False) for o in out]


def kind_of(spec: RunSpec) -> str:
    p = spec.prompt
    if p.startswith("[agent-hub plan"):
        return "lead"
    if p.startswith("[agent-hub review"):
        return "review"
    return "task"
