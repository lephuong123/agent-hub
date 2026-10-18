"""Một lời gọi LLM có cấu trúc cho curation (gộp skill liên agent): `claude -p --json-schema --no-session-persistence`.

Như LLM judge của M4-B: không phải agent — không tool (`--tools ""`), không MCP, không nạp cấu hình máy, system prompt thay
thế ngắn. Ghi một dòng `runs` kind=`curator` (chi phí vào báo cáo cost + ngân sách curation). Lỗi → trả None (curation
rơi về gộp tất định).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..store import now_iso

if TYPE_CHECKING:
    from ..hub import Hub


def build_argv(binary: str, *, schema: dict[str, Any], system: str, model: str, budget_usd: float | None, mcp_path: Path, settings_path: Path) -> list[str]:
    argv = [
        binary, "-p", "--output-format", "json",
        "--json-schema", json.dumps(schema, separators=(",", ":")),
        "--no-session-persistence",
        "--model", model,
        "--system-prompt", system,
        "--setting-sources", "",
        "--settings", str(settings_path),
        "--strict-mcp-config", "--mcp-config", str(mcp_path),
    ]
    if budget_usd:
        argv += ["--max-budget-usd", f"{float(budget_usd):.4f}".rstrip("0").rstrip(".")]
    return argv + ["--tools", ""]


async def structured_call(hub: "Hub", *, agent_id: str, prompt: str, schema: dict[str, Any], system: str, label: str,
                          budget_usd: float | None, curation_id: str | None = None) -> tuple[dict[str, Any] | None, float, str]:
    """→ (structured_output | None, cost_usd, run_id)."""
    cc = hub.config.get("curation")
    claude_cfg = hub.config.get("runtime.claude")
    binary = str(cc.get("binary") or claude_cfg.get("binary") or "claude")
    model = str(cc.get("model") or "haiku")
    timeout = float(cc.get("timeout_s") or 600)
    run_id = "r_" + uuid.uuid4().hex[:12]
    started = now_iso()
    # hardening H-20 (NFR-22): gọi model qua AgentRuntime.oneshot (ClaudeCliRuntime, binary = curation.binary nếu đặt)
    from ..runtime import ClaudeCliRuntime, OneShotRequest

    rt = ClaudeCliRuntime({**claude_cfg, "binary": binary})
    r = await rt.oneshot(OneShotRequest(prompt=prompt, schema=schema, system=system, model=model, budget_usd=budget_usd, timeout_s=timeout, label=label))
    if not r.ok and not r.raw and r.error:
        _record(hub, run_id, agent_id, model, started, 0.0, "failed", f"curation LLM lỗi: {r.error}"[:300], label, curation_id)
        return None, 0.0, run_id
    ok, res, cost, err = r.ok, r.output, r.cost_usd, r.error
    _record(hub, run_id, agent_id, model, started, cost, "completed" if ok else "failed", (res or {}).get("reason", "")[:300] if ok else err,
            label, curation_id, duration_ms=r.duration_ms)
    return (res if ok else None), cost, run_id


def _record(hub: "Hub", run_id: str, agent_id: str, model: str, started: str, cost: float, status: str, summary: str, label: str,
            curation_id: str | None, duration_ms: int | None = None) -> None:
    try:
        hub.store.insert_run({
            "id": run_id, "agent_id": agent_id, "kind": "curator", "prompt": f"[agent-hub curation] {label}", "model": model, "status": status,
            "outcome": status, "queued_at": started, "started_at": started, "ended_at": now_iso(), "cost_usd": round(cost, 8),
            "summary": summary, "duration_ms": duration_ms, "run_opts": {"curation_id": curation_id, "curation_llm": label},
        })
    except Exception:  # pragma: no cover
        pass
