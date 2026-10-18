"""Tiện ích test M4-B (không gọi claude, không tốn tiền).

- `FakeLedger`: thay `hub.learning.ledger` của M4-A — `list()`, `rollback()`, `materialize_before()`; áp dụng/rollback
  cấu hình agent thật qua `hub.agents.update` để test kiểm được hậu quả.
- `write_fake_judge(dir)`: binary `claude` giả cho LLM judge — đọc prompt từ stdin, chấm theo từ khoá trong <agent_output>.
- `settle(cond)`: chờ điều kiện trong event loop.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_hub.events import make_event
from agent_hub.store import now_iso


async def settle(cond=None, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        await asyncio.sleep(0.02)
        if cond is not None and cond():
            return True
    if cond is not None:
        raise AssertionError("hết giờ chờ điều kiện")
    return False


class FakeLedger:
    def __init__(self, hub) -> None:
        self.hub = hub
        self.entries: list[dict[str, Any]] = []
        self.rollbacks: list[tuple[str, str, str]] = []

    def apply_config(self, agent_id: str, changes: dict[str, Any], *, risk_class: str = "L3", flags: list[str] | None = None,
                     applied_at: str | None = None, actor: str = "review") -> dict[str, Any]:
        row = self.hub.agents.get_row(agent_id)
        before = {k: row.get(k) for k in changes}
        self.hub.agents.update(agent_id, changes, actor="learning")
        e = {"id": "le_" + uuid.uuid4().hex[:8], "agent_id": agent_id, "scope": "agent", "owner_id": agent_id, "target": "config",
             "target_ref": ",".join(changes), "op": "patch", "risk_class": risk_class, "actor": actor, "applied_at": applied_at or now_iso(),
             "before": before, "after": changes, "flags": flags or [], "rolled_back_by": None, "proposal_id": "p_" + uuid.uuid4().hex[:6]}
        self.entries.append(e)
        self.hub.publish(make_event("learning.applied", agent_id, {"proposal_id": e["proposal_id"], "ledger_entry_id": e["id"], "scope": "agent",
                                                                   "target": "config", "target_ref": e["target_ref"], "op": "patch", "risk_class": risk_class,
                                                                   "diff": f"-model: {before}\n+model: {changes}"}))
        return e

    def list(self, agent_id=None, **kw) -> list[dict[str, Any]]:
        return [dict(e) for e in reversed(self.entries) if agent_id is None or e["agent_id"] == agent_id]

    def rollback(self, entry_id: str, actor: str = "user", reason: str | None = None, **kw) -> dict[str, Any]:
        e = next(x for x in self.entries if x["id"] == entry_id)
        self.hub.agents.update(e["agent_id"], dict(e["before"]), actor="rollback")
        e["rolled_back_by"] = "le_rb_" + entry_id
        self.rollbacks.append((entry_id, actor, reason or ""))
        self.hub.publish(make_event("learning.rolled_back", e["agent_id"], {"ledger_entry_id": entry_id, "actor": actor, "reason": reason}))
        return {"id": e["rolled_back_by"], "rollback_of": entry_id}

    def materialize_before(self, entry, home, cfg) -> bool:
        e = next(x for x in self.entries if x["id"] == entry["id"])
        cfg.update(e["before"])
        return True


def attach_fake_ledger(hub) -> FakeLedger:
    lg = FakeLedger(hub)
    hub.learning = SimpleNamespace(ledger=lg)
    return lg


def write_fake_judge(d: Path) -> Path:
    """`claude` giả cho judge: điểm 0.9 nếu output agent chứa GOOD, 0.1 nếu không; ghi argv vào judge_argv.jsonl."""
    p = d / "fake_judge.py"
    log = d / "judge_argv.jsonl"
    p.write_text(f"""#!{sys.executable}
import json, sys
argv = sys.argv[1:]
prompt = sys.stdin.read()
open({str(log)!r}, "a").write(json.dumps(argv) + "\\n")
out = prompt.split("<agent_output>")[-1]
good = "GOOD" in out
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.0012,
                   "result": "", "structured_output": {{"score": 0.9 if good else 0.1, "pass": good, "reason": "ok" if good else "missing GOOD"}}}}))
""", encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def judge_calls(d: Path) -> list[list[str]]:
    f = d / "judge_argv.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
