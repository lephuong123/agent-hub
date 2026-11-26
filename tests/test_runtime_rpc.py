"""Runtime Codex (`codex app-server`) và Grok (`grok agent stdio`, ACP) với server GIẢ (tests/fake_codex_appserver.py,
tests/fake_acp_agent.py — dạng message đo thật ở docs/spikes/codex-grok/). Không gọi model.

Kiểm: event chuẩn hoá qua ClaudeStreamAdapter, mọi lần hỏi quyền đi qua control_handler (guard) và FAIL-CLOSED, không bao giờ chọn
"luôn cho phép", MCP của hub truyền đúng (bỏ hub-guard), cô lập cấu hình (GROK_HOME riêng, codex mxc/apps=false/tắt MCP lạ),
resume theo id phiên của runtime, stop/interrupt.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from cfharness.agents import AgentConfig
from cfharness.config import DEFAULTS
from cfharness.runtime import AcpRuntime, ClaudeCliRuntime, CodexRuntime, FakeRuntime, RunSpec, make_runtime
from cfharness.runtime.acp import grok_home_config, map_tool
from cfharness.runtime.codex import _inner_command

HERE = Path(__file__).parent
FAKE_ACP = HERE / "fake_acp_agent.py"
FAKE_CODEX = HERE / "fake_codex_appserver.py"


def _bin(script: Path) -> str:
    # POSIX: script có shebang + bit thực thi; Windows: exec_argv chạy `.py` bằng python hiện tại
    return str(script)


def _spec(tmp: Path, *, run_id: str = "run-1", session_id: str = "hub-sess-1", prompt: str = "do it", resume: str | None = None,
          system_prompt: str | None = "You are a careful agent.", tool_policy: dict | None = None) -> RunSpec:
    rt = tmp / "rt" / run_id
    rt.mkdir(parents=True, exist_ok=True)
    mcp = rt / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {
        "hub-guard": {"type": "stdio", "command": sys.executable, "args": ["-m", "guard"]},
        "hub-self": {"type": "stdio", "command": sys.executable, "args": ["-m", "cfharness.mcp_server.hub_self", "--run", run_id],
                     "env": {"CFHARNESS_RUN_ID": run_id}},
    }}), encoding="utf-8")
    return RunSpec(run_id=run_id, agent_id="ag", prompt=prompt, cwd=tmp / "ws", session_id=session_id,
                   raw_log_path=tmp / "runs" / f"{run_id}.jsonl", stderr_log_path=tmp / "runs" / f"{run_id}.stderr.log",
                   resume_session_id=resume, system_prompt=system_prompt, mcp_config_path=mcp,
                   allowed_mcp_servers=["hub-self"], tool_policy=tool_policy or {})


class Guard:
    """control_handler giả: ghi lại yêu cầu, quyết theo bảng `deny` (tên tool)."""

    def __init__(self, deny: tuple[str, ...] = (), delay: float = 0.0, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.deny, self.delay, self.fail = deny, delay, fail

    async def __call__(self, request: dict[str, Any], rid: str) -> dict[str, Any]:
        self.calls.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("guard exploded")
        if request["tool_name"] in self.deny:
            return {"behavior": "deny", "message": "no"}
        return {"behavior": "allow"}


async def _run(rt, spec: RunSpec, *, stop_after: float | None = None, steer: str | None = None) -> tuple[list, Any]:
    h = await rt.start(spec)
    evs = []

    async def consume():
        async for ev in rt.events(h):
            evs.append(ev)

    task = asyncio.ensure_future(consume())
    if steer is not None:
        await asyncio.sleep(0.5)
        await rt.send(h, steer)
    if stop_after is not None:
        await asyncio.sleep(stop_after)
        await rt.stop(h, grace_s=3)
    await asyncio.wait_for(task, 30)
    return evs, h


def _log(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def _types(evs) -> list[str]:
    return [e.type for e in evs]


# ------------------------------------------------------------------ Grok (ACP)
@pytest.fixture
def grok(tmp_path, monkeypatch):
    log = tmp_path / "acp.log"
    monkeypatch.setenv("FAKE_ACP_LOG", str(log))
    monkeypatch.setenv("FAKE_ACP_STATE", str(tmp_path / "acp.state"))
    cfg = {**DEFAULTS["runtime"]["grok"], "binary": _bin(FAKE_ACP), "hub_home": str(tmp_path / "hub")}
    return AcpRuntime(cfg), log


@pytest.mark.asyncio
async def test_grok_allow_flow_events_and_isolation(tmp_path, grok):
    rt, log = grok
    spec = _spec(tmp_path, prompt="please mcp")
    g = Guard()
    spec.control_handler = g
    evs, h = await _run(rt, spec)
    t = _types(evs)
    assert t[0] == "run.started" and "run.completed" in t and "run.failed" not in t
    starts = [e.payload["name"] for e in evs if e.type == "tool.start"]
    assert starts == ["Bash", "mcp__hub-self__task_list"]
    done = [e.payload for e in evs if e.type == "tool.complete"]
    assert [d["is_error"] for d in done] == [False, False] and done[0]["result_preview"] == "hi"
    assert any(e.type == "message.complete" and "Done." in e.payload["text"] for e in evs)
    assert any(e.type == "reasoning.delta" for e in evs)
    # guard hỏi đúng tool + input kiểu Claude
    assert [c["tool_name"] for c in g.calls] == ["Bash", "mcp__hub-self__task_list"]
    assert g.calls[0]["input"]["command"] == "echo hi" and g.calls[1]["input"] == {"status": "open"}
    entries = _log(log)
    argv = next(e for e in entries if e["kind"] == "argv")
    assert argv["argv"][:2] == ["agent", "--no-leader"] and argv["argv"][-1] == "stdio"
    assert argv["argv"][argv["argv"].index("--reasoning-effort") + 1] == "low"
    env = argv["env"]
    assert env["GROK_MEMORY"] == "0" and env["GROK_CLAUDE_MCPS_ENABLED"] == "0" and env["GROK_DISABLE_AUTOUPDATER"] == "1"
    assert Path(env["GROK_HOME"]) == tmp_path / "hub" / "runtime-homes" / "grok" and env["GROK_AUTH_PATH"].endswith("auth.json")
    assert "[compat.claude]" in (Path(env["GROK_HOME"]) / "config.toml").read_text(encoding="utf-8")
    new = next(e for e in entries if e["kind"] == "request" and e["method"] == "session/new")
    assert [m["name"] for m in new["params"]["mcpServers"]] == ["hub-self"]  # hub-guard bị bỏ
    assert {"name": "CFHARNESS_RUN_ID", "value": "run-1"} in new["params"]["mcpServers"][0]["env"]
    prompt = next(e for e in entries if e["kind"] == "prompt")["text"]
    assert "<hub_instructions>" in prompt and "careful agent" in prompt
    assert [e["choice"] for e in entries if e["kind"] == "permission"] == ["allow-once", "allow-once"]
    usage = next(e for e in evs if e.type == "usage.tick" and e.payload.get("estimated") is False).payload
    assert usage["input_tokens"] == 60 and usage["cache_read_input_tokens"] == 40 and usage["cost_usd"] == pytest.approx(0.1)
    assert h.exit_code == 0 and h.raw_log_final is not None and h.raw_log_final.exists()


@pytest.mark.asyncio
async def test_grok_deny_never_picks_always(tmp_path, grok):
    rt, log = grok
    spec = _spec(tmp_path)
    spec.control_handler = Guard(deny=("Bash",))
    evs, _ = await _run(rt, spec)
    assert [e["choice"] for e in _log(log) if e["kind"] == "permission"] == ["reject-once"]
    assert any(e.type == "permission.denied" and e.payload["tool_name"] == "Bash" for e in evs)
    assert [e.payload["is_error"] for e in evs if e.type == "tool.complete"] == [True]
    assert "run.completed" in _types(evs)  # Grok kết thúc lượt sau khi bị từ chối — run vẫn xong bình thường


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", [None, Guard(fail=True)])
async def test_grok_fail_closed(tmp_path, grok, handler):
    rt, log = grok
    spec = _spec(tmp_path)
    spec.control_handler = handler
    evs, _ = await _run(rt, spec)
    assert [e["choice"] for e in _log(log) if e["kind"] == "permission"] == ["reject-once"]


@pytest.mark.asyncio
async def test_grok_resume_loads_native_session(tmp_path, grok):
    rt, log = grok
    s1 = _spec(tmp_path, run_id="r1")
    s1.control_handler = Guard()
    await _run(rt, s1)
    native = json.loads((tmp_path / "runs" / "native-sessions" / "hub-sess-1.json").read_text())["id"]
    s2 = _spec(tmp_path, run_id="r2", session_id="hub-sess-2", resume="hub-sess-1", prompt="again")
    s2.control_handler = Guard()
    evs, _ = await _run(rt, s2)
    loads = [e for e in _log(log) if e["kind"] == "request" and e["method"] == "session/load"]
    assert loads and loads[0]["params"]["sessionId"] == native
    # lịch sử phát lại lúc load không thành event mới
    assert not any(e.type == "message.complete" and "OLD" in e.payload["text"] for e in evs)
    prompt = [e for e in _log(log) if e["kind"] == "prompt"][-1]["text"]
    assert "<hub_instructions>" not in prompt  # phiên cũ đã có chỉ dẫn


@pytest.mark.asyncio
async def test_grok_stop_cancels(tmp_path, grok):
    rt, log = grok
    spec = _spec(tmp_path, prompt="slow job")
    spec.control_handler = Guard()
    evs, h = await _run(rt, spec, stop_after=1.0)
    assert "run.failed" in _types(evs) and h.stop_requested
    assert any(e["kind"] == "request" and e["method"] == "session/cancel" for e in _log(log))


def test_grok_map_tool_variants():
    assert map_tool({"rawInput": {"variant": "Write", "file_path": "a.txt", "content": "x"}}) == ("Write", {"file_path": "a.txt", "content": "x"})
    assert map_tool({"rawInput": {"variant": "SearchReplace", "file_path": "a", "old_string": "o", "new_string": "n"}})[0] == "Edit"
    assert map_tool({"rawInput": {"variant": "UseTool", "tool_name": "srv__t", "tool_input": {"a": 1}}}) == ("mcp__srv__t", {"a": 1})
    assert map_tool({"kind": "edit", "locations": [{"path": "p"}]}) == ("Edit", {"file_path": "p"})
    assert map_tool({"title": "x", "_meta": {"x.ai/tool": {"label": "Read"}}, "rawInput": {"variant": "Read", "path": "f"}}) == ("grok:Read", {"path": "f"})
    assert 'disabled = ["vercel"]' in grok_home_config(["vercel"])


# ------------------------------------------------------------------ Codex (app-server)
@pytest.fixture
def codex(tmp_path, monkeypatch):
    log = tmp_path / "codex.log"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    cfg = {**DEFAULTS["runtime"]["codex"], "binary": _bin(FAKE_CODEX)}
    return CodexRuntime(cfg), log


@pytest.mark.asyncio
async def test_codex_allow_flow(tmp_path, codex):
    rt, log = codex
    spec = _spec(tmp_path, prompt="file and mcp please")
    g = Guard()
    spec.control_handler = g
    evs, h = await _run(rt, spec)
    t = _types(evs)
    assert t[0] == "run.started" and "run.completed" in t
    assert [e.payload["name"] for e in evs if e.type == "tool.start"] == ["Bash", "Write", "mcp__hub-self__task_list"]
    assert [e.payload["is_error"] for e in evs if e.type == "tool.complete"] == [False, False, False]
    assert [c["tool_name"] for c in g.calls] == ["Bash", "Write", "mcp__hub-self__task_list"]
    assert g.calls[0]["input"] == {"command": "echo hi"} and g.calls[1]["input"]["file_path"] == "notes.md"
    entries = _log(log)
    argv = next(e for e in entries if e["kind"] == "argv")["argv"]
    assert argv == ["-c", 'windows.sandbox="mxc"', "app-server"]
    start = next(e for e in entries if e["kind"] == "request" and e["method"] == "thread/start")["params"]
    assert start["approvalPolicy"] == "untrusted" and start["sandbox"] == "workspace-write" and start["ephemeral"] is False
    assert start["developerInstructions"] == "You are a careful agent."
    c = start["config"]
    assert c["windows"]["sandbox"] == "mxc" and c["features"]["apps"] is False and c["project_doc_max_bytes"] == 0
    assert list(c["mcp_servers"]) == ["hub-self"] and c["mcp_servers"]["hub-self"]["default_tools_approval_mode"] == "prompt"
    assert c["model_reasoning_effort"] == "medium"
    decisions = [e["result"] for e in entries if e["kind"] == "decision"]
    assert decisions == [{"decision": "accept"}, {"decision": "accept"}, {"action": "accept"}]
    assert any(e.type == "message.complete" and e.payload["text"] == "Done." for e in evs)
    assert any(e.type == "ratelimit.update" for e in evs)
    usage = next(e for e in evs if e.type == "usage.tick" and e.payload.get("estimated") is False).payload
    assert usage["input_tokens"] == 300 and usage["cache_read_input_tokens"] == 200 and usage["cost_usd"] == 0.0
    assert h.exit_code == 0


@pytest.mark.asyncio
async def test_codex_deny_declines_each_kind(tmp_path, codex):
    rt, log = codex
    spec = _spec(tmp_path, prompt="file and mcp please")
    spec.control_handler = Guard(deny=("Bash", "Write", "mcp__hub-self__task_list"))
    evs, _ = await _run(rt, spec)
    decisions = [e["result"] for e in _log(log) if e["kind"] == "decision"]
    assert decisions == [{"decision": "decline"}, {"decision": "decline"}, {"action": "decline"}]
    assert [e.payload["is_error"] for e in evs if e.type == "tool.complete"] == [True, True, True]
    assert sum(e.type == "permission.denied" for e in evs) == 3


@pytest.mark.asyncio
async def test_codex_read_only_and_plan(tmp_path, codex):
    rt, log = codex
    spec = _spec(tmp_path, tool_policy={"permission_mode": "plan"})
    spec.control_handler = Guard()
    await _run(rt, spec)
    start = next(e for e in _log(log) if e["kind"] == "request" and e["method"] == "thread/start")["params"]
    assert start["sandbox"] == "read-only"


@pytest.mark.asyncio
async def test_codex_stop_interrupts_turn(tmp_path, codex):
    rt, log = codex
    spec = _spec(tmp_path, prompt="slow job")
    spec.control_handler = Guard()
    evs, h = await _run(rt, spec, stop_after=1.0)
    assert any(e["kind"] == "request" and e["method"] == "turn/interrupt" for e in _log(log))
    assert "run.failed" in _types(evs) and h.stop_requested
    assert [e.payload["is_error"] for e in evs if e.type == "tool.complete"] == [True]  # lệnh treo được đóng


@pytest.mark.asyncio
async def test_codex_resume_uses_thread(tmp_path, codex):
    rt, log = codex
    s1 = _spec(tmp_path, run_id="r1")
    s1.control_handler = Guard()
    await _run(rt, s1)
    s2 = _spec(tmp_path, run_id="r2", session_id="hub-sess-2", resume="hub-sess-1")
    s2.control_handler = Guard()
    await _run(rt, s2)
    res = [e for e in _log(log) if e["kind"] == "request" and e["method"] == "thread/resume"]
    assert res and res[0]["params"]["threadId"] == "thread-1" and res[0]["params"]["config"]["windows"]["sandbox"] == "mxc"


def test_codex_inner_command():
    assert _inner_command({"command": "pwsh -Command 'x'", "commandActions": [{"command": "x"}]}) == "x"
    assert _inner_command({"command": "raw"}) == "raw"


# ------------------------------------------------------------------ registry + cấu hình agent
def test_make_runtime_by_kind(tmp_path):
    cfg = {"runtime": {**DEFAULTS["runtime"]}}
    assert isinstance(make_runtime(cfg, kind="codex"), CodexRuntime)
    g = make_runtime(cfg, kind="grok", hub_home=tmp_path)
    assert isinstance(g, AcpRuntime) and g.cfg["hub_home"] == str(tmp_path)
    assert isinstance(make_runtime(cfg, kind=None), ClaudeCliRuntime)
    assert isinstance(make_runtime({"runtime": {**DEFAULTS["runtime"], "kind": "fake"}}, kind="codex"), FakeRuntime)


def test_agent_config_runtime_validation():
    assert AgentConfig(id="a", name="a").runtime == "claude"
    assert AgentConfig(id="b", name="b", runtime="codex").model == "default"
    assert AgentConfig(id="c", name="c", runtime="grok", model="grok-4.7").model == "grok-4.7"
    with pytest.raises(Exception):
        AgentConfig(id="d", name="d", model="gpt-5")  # claude runtime chỉ nhận model Claude
    for bad in ({"plugins": ["x@y"]}, {"subagents": {"r": {"description": "d", "prompt": "p"}}}, {"result_schema": {"type": "object"}},
                {"tool_policy": {"connectors": ["Slack"]}}):
        with pytest.raises(Exception):
            AgentConfig(id="e", name="e", runtime="codex", **bad)
    with pytest.raises(Exception):
        AgentConfig(id="f", name="f", runtime="gemini")


@pytest.mark.asyncio
async def test_codex_steer_mid_turn(tmp_path, codex):
    rt, log = codex
    spec = _spec(tmp_path, prompt="slow job")
    spec.control_handler = Guard()
    evs, _ = await _run(rt, spec, steer="also say STEER-OK", stop_after=1.0)
    steer = [e for e in _log(log) if e["kind"] == "request" and e["method"] == "turn/steer"]
    assert steer and steer[0]["params"]["expectedTurnId"] == "turn-1"
    assert steer[0]["params"]["input"] == [{"type": "text", "text": "also say STEER-OK"}]
    assert any(e.type == "steer.ack" and "STEER-OK" in e.payload["text"] for e in evs)


# ------------------------------------------------------------------ tích hợp: Hub + RunManager + guard thật
@pytest.mark.asyncio
@pytest.mark.parametrize("kind,script,env", [("codex", FAKE_CODEX, "FAKE_CODEX_LOG"), ("grok", FAKE_ACP, "FAKE_ACP_LOG")])
async def test_hub_runs_agent_on_selected_runtime(hub_home, tmp_path, monkeypatch, kind, script, env):
    from conftest import fake_cli

    from cfharness.hub import Hub

    log = tmp_path / f"{kind}.log"
    monkeypatch.setenv(env, str(log))
    monkeypatch.setenv("FAKE_ACP_STATE", str(tmp_path / "acp.state"))
    h = Hub(hub_home)
    h.config.set(f"runtime.{kind}.binary", str(fake_cli(tmp_path, script, kind)))
    h.start()
    try:
        h.agents.create({"id": "x", "name": "X", "runtime": kind})
        assert h.agents.show("x")["runtime"] == kind
        run = h.runs.submit("x", "say hi")
        final = await h.runs.wait(run["id"], 60)
        types = [e.type for e in h.store.query_events(run_id=run["id"])]
        assert final["status"] == "completed" and final["outcome"] == "completed", final
        assert "run.started" in types and "tool.start" in types
        # mọi lần hỏi quyền đi qua guard (kênh control) — có bản ghi quyết định
        decided = [e.payload.get("action") for e in h.store.query_events(run_id=run["id"]) if e.type == "guard.decision"]
        assert decided and set(decided) == {"allow"} and types.count("control.request") == len(decided), types
        assert any(v in str(final["cli_version"]) for v in ("0.172.0", "1.0.92"))
    finally:
        await h.stop() if asyncio.iscoroutinefunction(getattr(h, "stop", None)) else None
        h.store.close()


@pytest.mark.asyncio
async def test_codex_process_dies_mid_turn_fails_with_stderr(tmp_path, monkeypatch):
    """CLI chết giữa lượt (không có turn/completed) → run.failed có lý do (kèm stderr), không treo."""
    script = tmp_path / "dying_codex.py"
    script.write_text(
        "import sys, json\n"
        "if sys.argv[1:] == ['--version']:\n    print('codex-cli 0.172.0'); sys.exit(0)\n"
        "for line in sys.stdin:\n"
        "    m = json.loads(line)\n"
        "    if m.get('method') == 'initialize': print(json.dumps({'id': m['id'], 'result': {}}), flush=True)\n"
        "    elif m.get('method') == 'config/read': print(json.dumps({'id': m['id'], 'result': {'config': {}}}), flush=True)\n"
        "    elif m.get('method') == 'thread/start': print(json.dumps({'id': m['id'], 'result': {'thread': {'id': 't'}}}), flush=True)\n"
        "    elif m.get('method') == 'turn/start':\n"
        "        print(json.dumps({'id': m['id'], 'result': {'turn': {'id': 'u'}}}), flush=True)\n"
        "        sys.stderr.write('fatal: Not logged in\n'); sys.exit(3)\n",
        encoding="utf-8")
    from conftest import fake_cli

    rt = CodexRuntime({**DEFAULTS["runtime"]["codex"], "binary": str(fake_cli(tmp_path, script, "codex"))})
    spec = _spec(tmp_path)
    spec.control_handler = Guard()
    evs, h = await _run(rt, spec)
    failed = [e.payload for e in evs if e.type == "run.failed"]
    assert failed and "Not logged in" in json.dumps(failed[0], ensure_ascii=False)
    assert h.exit_code not in (None, 0)


# ------------------------------------------------------------------ khung quyền (tool_policy.tools) — vd điều phối viên strict
@pytest.mark.asyncio
async def test_codex_tool_allowlist_denies_outside_tools_without_guard(tmp_path, codex):
    rt, log = codex
    spec = _spec(tmp_path, prompt="file and mcp please", tool_policy={"tools": ["Read", "Grep", "Glob"]})
    g = Guard()
    spec.control_handler = g
    evs, _ = await _run(rt, spec)
    decisions = [e["result"] for e in _log(log) if e["kind"] == "decision"]
    assert decisions == [{"decision": "decline"}, {"decision": "decline"}, {"action": "accept"}]  # Bash/Write ngoài khung; MCP hub được
    assert [c["tool_name"] for c in g.calls] == ["mcp__hub-self__task_list"]  # tool ngoài khung KHÔNG tới guard (không hỏi người)
    start = next(e for e in _log(log) if e["kind"] == "request" and e["method"] == "thread/start")["params"]
    assert start["sandbox"] == "read-only" and start["config"]["web_search"] == "disabled"


@pytest.mark.asyncio
async def test_grok_tool_allowlist(tmp_path, grok):
    rt, log = grok
    spec = _spec(tmp_path, prompt="please mcp", tool_policy={"tools": ["Read", "Grep", "Glob"]})
    g = Guard()
    spec.control_handler = g
    evs, _ = await _run(rt, spec)
    assert [e["choice"] for e in _log(log) if e["kind"] == "permission"] == ["reject-once"]  # Bash bị từ chối → Grok dừng lượt
    assert g.calls == []


@pytest.mark.asyncio
async def test_codex_wide_allowlist_keeps_workspace_write(tmp_path, codex):
    rt, log = codex
    spec = _spec(tmp_path, tool_policy={"tools": ["Bash", "Read", "Write", "Edit", "Grep", "Glob"]})
    spec.control_handler = Guard()
    await _run(rt, spec)
    start = next(e for e in _log(log) if e["kind"] == "request" and e["method"] == "thread/start")["params"]
    assert start["sandbox"] == "workspace-write" and "web_search" in start["config"]  # không có WebSearch/WebFetch → tắt web


def test_manager_config_problem_is_runtime_aware(hub_home):
    from cfharness.hub import Hub

    h = Hub(hub_home)
    try:
        h.agents.create({"id": "cl", "name": "CL"})
        h.agents.create({"id": "gp", "name": "GP", "runtime": "codex"})
        h.agents.create({"id": "gk", "name": "GK", "runtime": "grok", "tool_policy": {"tools": ["Read", "Bash"]}})
        perms = h.manager.perms
        assert "tool_policy.tools trống" in (perms.manager_config_problem(h.agents.get_row("cl")) or "")  # Claude: vẫn như cũ
        assert perms.manager_config_problem(h.agents.get_row("gp")) is None  # codex: khung ép lúc chạy
        assert "Bash" in (perms.manager_config_problem(h.agents.get_row("gk")) or "")  # liệt kê vượt khung → vẫn chặn
    finally:
        h.store.close()


@pytest.mark.asyncio
async def test_hub_codex_agent_can_be_manager(hub_home, tmp_path, monkeypatch):
    """Chỉ định agent codex làm điều phối viên được (lỗi người dùng gặp: bị chặn vì tool_policy.tools trống)."""
    from conftest import fake_cli

    from cfharness.hub import Hub

    monkeypatch.setenv("FAKE_CODEX_LOG", str(tmp_path / "codex.log"))
    h = Hub(hub_home)
    h.config.set("runtime.codex.binary", str(fake_cli(tmp_path, FAKE_CODEX, "codex")))
    h.start()
    try:
        h.agents.create({"id": "boss", "name": "Boss", "runtime": "codex"})
        h.manager.set("boss")  # trước đây: Blocked "tool_policy.tools trống" — tên tool Claude không áp lên codex
        assert h.manager.is_manager("boss")
    finally:
        h.store.close()
