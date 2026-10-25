"""Contract test cho `claude` 2.1.310 trên fixture stream-json THẬT (tests/fixtures/claude-2.1.310/).

Ghi ngày 2026-10-25 trên Windows 11 bằng `claude` 2.1.310, model haiku, theo đúng cách spike M0 (`docs/spikes/m0/run_iso.sh`,
`a02_steer.py`, `a03_interrupt.py`): cờ cô lập `--setting-sources "" --strict-mcp-config --mcp-config '{"mcpServers":{}}'`,
`--settings` có `autoMemoryEnabled:false` + hook PreToolUse `docs/spikes/m0/hooks/pretool.py` (trừ `claude-isolated`: không
hook, `--tools ""`), `--include-hook-events`. `claude-hub-isolated-run` là stdout thô của một run thật qua daemon cfharness
(HUB_HOME tạm, agent template researcher, guard + observe hook bật). Đường dẫn chứa tên user đã che (`user`, `USER~1`).
Tổng chi phí ghi ≈ $0.005. Chi tiết: docs/dev-notes/windows.md §1.
"""

import json
from pathlib import Path

import pytest

from cfharness.claude_adapter import adapt_lines
from cfharness.config import DEFAULTS
from cfharness.hub import Hub
from cfharness.observe_hook import EVENTS as OBSERVE_EVENTS
from cfharness.runtime import FakeRuntime

VERSION = "2.1.310"
FIX = Path(__file__).parent / "fixtures" / f"claude-{VERSION}"
# cấu hình cô lập như spike: chỉ hook PreToolUse của guard là hợp lệ, không MCP, plugin builtin cc-plugin-*
SPIKE = dict(allowed_hooks=["PreToolUse"], allowed_mcp_servers=[], allowed_plugin_prefixes=["cc-plugin-"], require_no_auto_memory=True)
# cấu hình mà RunManager dựng cho run có guard + observe hook (runs.py: iso.allowed_hooks + observe_events + PreToolUse)
HUB = dict(allowed_hooks=["PreToolUse", *OBSERVE_EVENTS], allowed_mcp_servers=["hub-guard"], allowed_plugin_prefixes=["cc-plugin-"],
           require_no_auto_memory=True)


def load(name: str) -> list[str]:
    return [ln for ln in (FIX / name).read_text(encoding="utf-8").splitlines() if ln.strip()]


def adapt(name: str, iso: dict = SPIKE):
    return adapt_lines("a", load(name), **iso)


def final(evs):
    fin = [e for e in evs if e.type in ("run.completed", "run.failed")]
    assert len(fin) <= 1
    return fin[0] if fin else None


ALL = sorted(p.name for p in FIX.glob("*.jsonl"))


def test_fixture_set_complete():
    assert set(ALL) == {"claude-isolated.jsonl", "claude-permission-denied.jsonl", "claude-budget-exceeded.jsonl", "claude-steer.jsonl",
                        "claude-interrupt-control.jsonl", "claude-ctrl-break-win.jsonl", "claude-hub-isolated-run.jsonl",
                        "claude-permission-denied-8dot3-win.jsonl"}


@pytest.mark.parametrize("name", ALL)
def test_recorded_with_this_version_and_parses_cleanly(name):
    lines = load(name)
    init = next(json.loads(ln) for ln in lines if '"subtype":"init"' in ln)
    assert init["claude_code_version"] == VERSION
    iso = HUB if name == "claude-hub-isolated-run.jsonl" else SPIKE
    evs = adapt(name, iso)
    t = [e.type for e in evs]
    assert "raw.unknown" not in t and "raw.invalid" not in t, name
    assert "isolation.violation" not in t, [e.payload for e in evs if e.type == "isolation.violation"]
    started = next(e for e in evs if e.type == "run.started")
    if iso is SPIKE:
        assert started.payload["mcp_servers"] == []


@pytest.mark.parametrize("name", ALL)
def test_redacted(name):
    raw = (FIX / name).read_text(encoding="utf-8").lower()
    assert "lequyp" not in raw and "phuong" not in raw and "@creativeforce" not in raw


def test_isolated_no_hooks_no_violation():
    evs = adapt("claude-isolated.jsonl")
    assert not [e for e in evs if e.type == "hook.event"]
    fin = final(evs)
    assert fin.type == "run.completed" and fin.payload["outcome"] == "completed" and fin.payload["cost_usd"] == 0.00054009


def test_permission_denied_by_hook():
    evs = adapt("claude-permission-denied.jsonl")
    pd = [e.payload for e in evs if e.type == "permission.denied"]
    assert len(pd) == 1 and pd[0]["decision_reason_type"] == "hook"
    assert final(evs).payload["outcome"] == "completed"


def test_permission_denied_safety_check_on_8dot3_cwd():
    """Windows: cwd dạng tên ngắn 8.3 (`C:/Users/USER~1/…`, như %TEMP% mặc định) → CLI 2.1.310 tự từ chối ghi bằng
    `safetyCheck` ("suspicious Windows path pattern") TRƯỚC khi xét quyết định `ask` của hook. Adapter vẫn phát
    `permission.denied` (decision_reason_type khác `hook`) — cwd của run phải là đường dẫn dài (realpath)."""
    evs = adapt("claude-permission-denied-8dot3-win.jsonl")
    pd = [e.payload for e in evs if e.type == "permission.denied"]
    assert len(pd) == 1 and pd[0]["decision_reason_type"] == "safetyCheck"
    assert "suspicious Windows path" in pd[0]["message"]


def test_budget_exceeded():
    fin = final(adapt("claude-budget-exceeded.jsonl"))
    assert fin.type == "run.failed" and fin.payload["outcome"] == "budget_exceeded"
    assert fin.payload["terminal_reason"] == "budget_exhausted" and fin.payload["cost_usd"] == pytest.approx(0.00062344)


def test_steer_ack_and_background_task():
    evs = adapt("claude-steer.jsonl")
    assert len([e for e in evs if e.type == "steer.ack"]) == 1  # tin isReplay thứ 2 = steer
    assert [e for e in evs if e.type == "task.background"]
    assert final(evs).payload["outcome"] == "completed"


def test_control_interrupt_aborted_tools():
    """Windows ngắt run bằng `control_request interrupt` qua stdin: kết quả như SIGINT trên POSIX (2.1.302)."""
    evs = adapt("claude-interrupt-control.jsonl")
    assert [e for e in evs if e.type == "control.response"]
    fin = final(evs)
    assert fin.type == "run.failed" and fin.payload["terminal_reason"] == "aborted_tools" and fin.payload["outcome"] == "failed"


def test_ctrl_break_has_no_result():
    """CTRL_BREAK cho process group trên Windows giết tiến trình KHÔNG phát `result` (giống SIGTERM của spike A-03):
    adapter không tự phát event kết thúc — RunManager quyết outcome từ mã thoát / stop_requested."""
    evs = adapt("claude-ctrl-break-win.jsonl")
    assert final(evs) is None
    tc = [e for e in evs if e.type == "tool.complete"]
    assert tc and tc[0].payload.get("is_error") is True


def test_hub_real_run_clean_under_hub_isolation():
    evs = adapt("claude-hub-isolated-run.jsonl", HUB)
    fin = final(evs)
    assert fin.type == "run.completed" and fin.payload["cost_usd"] == 0.00084657


def test_version_registered_as_tested():
    c = DEFAULTS["runtime"]["claude"]
    assert VERSION in c["tested_versions"] and VERSION in c["flag_matrix"]


async def test_fake_runtime_replays_budget_fixture_through_hub(hub_home):
    hub = Hub(hub_home, runtime=FakeRuntime(FIX / "claude-budget-exceeded.jsonl"))
    try:
        hub.agents.create({"id": "b", "name": "B"})
        run = hub.runs.submit("b", "hi")
        done = await hub.runs.wait(run["id"], timeout=5)
        assert done["outcome"] == "budget_exceeded"
    finally:
        hub.store.close()
