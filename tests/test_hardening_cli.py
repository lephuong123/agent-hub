"""Hardening luồng CLI (docs/plan/hardening.md): H-06, H-07, H-08, H-10, H-11, H-13, H-14, H-20 + mục "Thấp".

Mỗi test tái hiện kịch bản QA (docs/acceptance/traceability.md §3) — đỏ trước khi sửa, xanh sau. Không gọi `claude`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

from cfharness.errors import Blocked, HubError, NotFound
from cfharness.events import make_event
from cfharness.hub import Hub
from cfharness.runtime import ArgvError, FakeRuntime, RunSpec, build_argv
from cfharness.config import DEFAULTS


@pytest.fixture
def hub(hub_home):
    h = Hub(hub_home, runtime=FakeRuntime())
    yield h
    h.store.close()


def types_of(hub, **kw):
    return [e.type for e in hub.store.query_events(**kw)]


# ============================================================================ H-06 agent rm kiểm thành viên team
def test_h06_rm_blocked_when_team_member_then_detach(hub):
    hub.agents.create({"id": "lead1", "name": "Lead"})
    hub.agents.create({"id": "w1", "name": "W"})
    hub.teams.create({"id": "t1", "name": "T1", "members": [{"agent_id": "lead1", "role": "lead"}, {"agent_id": "w1", "role": "worker"}]})
    # QA: trước đây rm thành công → team t1 mồ côi lead, không gán lại được
    with pytest.raises(Blocked) as ei:
        hub.agents.remove("lead1")
    assert "t1 (lead)" in ei.value.message and "--detach-teams" in ei.value.message
    assert ei.value.data == {"teams": [{"team_id": "t1", "role": "lead"}]}
    assert hub.agents.show("lead1")["deleted_at"] is None  # không thay đổi gì
    res = hub.agents.remove("lead1", detach_teams=True)
    assert res["deleted_at"] and res["detached_teams"] == [{"team_id": "t1", "role": "lead"}]
    members = {m["agent_id"] for m in hub.teams.show("t1")["members"]}
    assert members == {"w1"}
    t = types_of(hub, agent_id="lead1")
    assert "agent.archived" in t and "agent.deleted" in t
    ev = [e for e in hub.store.query_events(types=["team.member"]) if e.payload.get("reason") == "agent_removed"]
    assert ev and ev[0].payload["agent_id"] == "lead1" and ev[0].team_id == "t1"
    # team đã archive không chặn
    hub.agents.create({"id": "w2", "name": "W2"})
    hub.teams.create({"id": "t2", "name": "T2", "members": [{"agent_id": "w2", "role": "worker"}]})
    asyncio.run(hub.teams.archive("t2"))
    hub.agents.remove("w2")


# ============================================================================ H-07 agent revert
def test_h07_revert_creates_new_revision_equal_to_target(hub):
    hub.agents.create({"id": "r", "name": "R", "model": "sonnet"}, soul="# soul v1\n")
    hub.agents.update("r", {"model": "haiku"})  # rev 2
    hub.agents.update("r", {"effort": "high"}, soul="# soul v3\n")  # rev 3
    hub.agents.update("r", {"model": "opus", "limits": {"max_runtime_s": 60}}, soul="# soul v4\n")  # rev 4
    assert hub.agents.show("r")["config_revision"] == 4
    res = hub.agents.revert("r", 3)
    assert res["config_revision"] == 5 and res["reverted_to_revision"] == 3 and res["soul_restored"]
    revs = {r["revision"]: r["config_snapshot"] for r in hub.store.list_revisions("r")}
    keys = ("model", "effort", "fallback_model", "tool_policy", "mcp_servers", "limits", "workspace_default", "role", "name", "description")
    assert {k: revs[5][k] for k in keys} == {k: revs[3][k] for k in keys}
    assert revs[5]["soul"] == revs[3]["soul"] == "# soul v3\n" == hub.agents.soul("r")
    assert revs[5]["reverted_to"] == 3 and len(revs) == 5  # không xoá lịch sử
    with pytest.raises(NotFound, match="revision 9"):
        hub.agents.revert("r", 9)


async def test_h07_running_run_keeps_old_revision(hub):
    rt = FakeRuntime(hold=True)
    hub.runs._runtime = rt
    hub.agents.create({"id": "k", "name": "K", "model": "sonnet"})
    hub.agents.update("k", {"model": "haiku"})  # rev 2
    run = hub.runs.submit("k", "việc dài")
    for _ in range(100):
        if rt.started:
            break
        await asyncio.sleep(0.01)
    assert rt.started and rt.started[0].model == "haiku"
    hub.agents.revert("k", 1)  # rev 3 = sonnet
    assert hub.store.get_run(run["id"])["agent_config_revision"] == 2  # run đang chạy giữ revision cũ
    rt.release()
    await hub.runs.wait(run["id"], timeout=5)
    assert hub.store.get_run(run["id"])["agent_config_revision"] == 2
    rt.hold = False
    r2 = hub.runs.submit("k", "việc mới")
    await hub.runs.wait(r2["id"], timeout=5)
    assert hub.store.get_run(r2["id"])["agent_config_revision"] == 3 and rt.started[-1].model == "sonnet"


# ============================================================================ H-08 bộ lệnh CLI
@pytest.mark.parametrize("bad", ["list", "logs", "show", "stop", "follow", "continue", "steer"])
def test_h08_agent_id_cannot_be_cli_keyword(hub, bad):
    with pytest.raises(HubError, match="trùng từ khoá"):
        hub.agents.create({"id": bad, "name": bad})


def _parse(argv):
    from cfharness.cli import build_parser
    from cfharness.cli_surface import rewrite_argv
    from cfharness.curation.cli import rewrite_argv as r5
    from cfharness.dispatch.cli import rewrite_argv as r3
    from cfharness.guard.cli import rewrite_argv as rb
    from cfharness.learning_ops.cli import rewrite_argv as r4

    p = build_parser()
    return p.parse_args(r5(r4(r3(rb(rewrite_argv(argv, p))))))


def test_h08_run_subcommands_never_parsed_as_new_run():
    from cfharness.cli import cmd_run, cmd_runs

    for sub in ("list", "show", "logs", "stop", "follow"):
        argv = ["run", sub] + ([] if sub == "list" else ["r_abc"])
        a = _parse(argv)
        assert a.fn is cmd_runs and a.action == sub, sub  # trước đây: cmd_run(agent=<sub>, prompt=r_abc) → run THẬT
    a = _parse(["--json", "run", "logs", "r_abc"])
    assert a.fn is cmd_runs
    a = _parse(["run", "coder", "làm gì đó", "--detach"])
    assert a.fn is cmd_run and a.follow is False
    a = _parse(["run", "coder", "tiếp", "--continue", "r_1", "--follow"])
    assert a.continue_from == "r_1" and a.follow is True


def test_h08_aliases_parse():
    from cfharness.cli_surface import _subcommands, cmd_stop, cmd_trash
    from cfharness.cli import build_parser

    assert _parse(["requests"]).cmd == "approvals"
    a = _parse(["feedback", "r_1", "--wrong", "--note", "sai định dạng"])
    assert a.cmd == "feedback" and a.run_id == "r_1" and a.kind == "wrong" and a.note == "sai định dạng"
    a = _parse(["feedback", "--good", "r_2"])
    assert a.run_id == "r_2" and a.kind == "good"
    a = _parse(["approve", "ap_1", "--deny", "--note", "không"])
    assert a.cmd == "deny" and a.id == "ap_1" and a.note == "không"
    assert _parse(["agent", "archive", "x", "--detach-teams"]).detach_teams is True
    assert _parse(["agent", "revert", "x", "--to", "3"]).to == 3
    learn_subs = _subcommands(build_parser(), "learn")
    if "pin" not in learn_subs:
        a = _parse(["learn", "pin", "a", "skill", "s1"])
        assert a.cmd == "knowledge" and a.action == "pin"
    if "enable" not in learn_subs:
        a = _parse(["learn", "disable", "coder"])
        assert a.cmd == "learn" and a.key == "agents.coder.enabled" and a.value == "false"
        assert _parse(["learn", "enable"]).key == "enabled"
    assert _parse(["eval", "golden", "list"]).cmd == "golden"
    assert _parse(["eval", "results"]).cmd == "eval"
    assert _parse(["stop", "coder"]).fn is cmd_stop
    for act in ("list",):
        assert _parse(["trash", act]).fn is cmd_trash
    assert _parse(["trash", "restore", "a", "--kind", "agent"]).kind == "agent"
    assert _parse(["daemon", "logs", "-n", "5"]).lines == 5
    assert _parse(["hook", "PostToolUse", "--run", "r_1"]).event == "PostToolUse"
    assert _parse(["agent", "list", "--team", "t1", "--status", "idle"]).team == "t1"


@pytest.fixture
def daemon(hub_home):
    hub_home.config.write_text(yaml.safe_dump({"runtime": {"kind": "fake", "fake": {"line_delay_s": 0.0}}, "runs": {"stop_grace_s": 1}}))
    from cfharness.daemon import spawn_background

    spawn_background(hub_home)
    yield hub_home
    from cfharness.cli import main

    main(["daemon", "stop"])


def cli(capsys, *args):
    from cfharness.cli import main

    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_h06_h07_h08_cli_e2e(daemon, capsys):
    from cfharness.rpc import RpcClient

    rpc = RpcClient(daemon.socket, timeout=30)
    assert cli(capsys, "agent", "create", "lead1")[0] == 0
    assert cli(capsys, "agent", "create", "w1")[0] == 0
    code, _, err = cli(capsys, "agent", "create", "logs")
    assert code == 1 and "trùng từ khoá" in err
    rpc.call("team.create", {"config": {"id": "t1", "name": "T1", "members": [{"agent_id": "lead1", "role": "lead"}, {"agent_id": "w1", "role": "worker"}]}})
    # H-08 lọc agent list (FR-AG-02)
    code, out, _ = cli(capsys, "agent", "list", "--team", "t1", "--json")
    assert {a["id"] for a in json.loads(out)} == {"lead1", "w1"}
    code, out, _ = cli(capsys, "agent", "list", "--status", "paused", "--json")
    assert json.loads(out) == []
    # H-06
    code, _, err = cli(capsys, "agent", "rm", "lead1")
    assert code == 3 and "t1 (lead)" in err
    code, out, _ = cli(capsys, "agent", "rm", "lead1", "--detach-teams")
    assert code == 0 and "đã gỡ khỏi team: t1" in out
    # H-08 trash
    code, out, _ = cli(capsys, "trash", "list", "--json")
    assert [(t["kind"], t["id"]) for t in json.loads(out)] == [("agent", "lead1")]
    code, out, _ = cli(capsys, "trash", "restore", "lead1")
    assert code == 0 and "đã khôi phục agent lead1" in out
    code, _, err = cli(capsys, "trash", "restore", "khong-co")
    assert code == 1
    # H-07
    assert cli(capsys, "agent", "edit", "w1", "--model", "haiku")[0] == 0
    code, out, _ = cli(capsys, "agent", "revert", "w1", "--to", "1", "--json")
    ag = json.loads(out)
    assert code == 0 and ag["config_revision"] == 3 and ag["model"] == "sonnet"
    audit = rpc.call("audit.list", {"kind": "agent.revert"})
    assert audit and audit[0]["channel"] == "cli"
    # H-08 run --detach / run list|show|logs / stop <agent> / requests / daemon logs
    code, out, _ = cli(capsys, "run", "w1", "chào", "--detach")
    rid = out.strip()
    assert code == 0 and rid.startswith("r_")
    rpc.call("run.wait", {"id": rid, "timeout": 10})
    code, out, _ = cli(capsys, "run", "list", "--json")
    assert [r["id"] for r in json.loads(out)] == [rid]
    code, out, _ = cli(capsys, "run", "logs", rid)
    assert code == 0 and "stdout thô" in out
    code, out, _ = cli(capsys, "run", "show", rid, "--json")
    assert json.loads(out)["id"] == rid
    assert len(rpc.call("run.list", {})) == 1  # không run nào bị tạo nhầm
    code, out, _ = cli(capsys, "stop", "w1")
    assert code == 0 and "0 run" in out
    code, out, _ = cli(capsys, "requests")
    assert code == 0
    code, out, _ = cli(capsys, "run", "w1", "tiếp", "--continue", rid)
    assert code == 0 and out.strip().startswith("r_") and out.strip() != rid
    code, out, _ = cli(capsys, "daemon", "logs", "-n", "5")
    assert code == 0
    code, out, _ = cli(capsys, "daemon", "status", "--json")
    st = json.loads(out)
    assert st["compat"]["cli"]["status"] == "fake"


# ============================================================================ H-10 ma trận cờ + phiên bản + raw.unknown
def _spec(**kw):
    base = dict(run_id="r_1", agent_id="c", prompt="hi", cwd=Path("/tmp/w"), session_id="11111111-2222-3333-4444-555555555555",
                raw_log_path=Path("/tmp/r.jsonl"), stderr_log_path=Path("/tmp/r.err"))
    base.update(kw)
    return RunSpec(**base)


def _cfg(**iso):
    c = {**DEFAULTS["runtime"]["claude"]}
    c["isolation"] = {**c["isolation"], **iso}
    return c


def test_h10_flag_matrix_rejects_unsupported_flags():
    full = _spec(model="sonnet", effort="high", fallback_model="haiku", system_prompt="S", max_budget_usd=0.3, display_name="c:r",
                 mcp_config_path=Path("/m.json"), settings_path=Path("/s.json"), permission_prompt_tool="mcp__hub-guard__approve",
                 add_dirs=[Path("/x")], json_schema={"type": "object"}, agents_json={"a": {"description": "d", "prompt": "p"}},
                 no_session_persistence=True, tool_policy={"tools": ["Read"], "allowed_tools": ["Read"], "disallowed_tools": ["Bash(rm:*)"],
                                                           "permission_mode": "plan", "permission_prompts": "host"})
    build_argv(full, _cfg(), "2.1.302")  # mọi cờ hub tự sinh đều có trong ma trận 2.1.302
    build_argv(_spec(resume_session_id="s0", fork=True), _cfg(), "2.1.302")
    with pytest.raises(ArgvError, match="--bogus-flag"):
        build_argv(_spec(), _cfg(extra_args=["--bogus-flag"]), "2.1.302")
    with pytest.raises(ArgvError):  # bị chặn bởi ma trận (và/hoặc kiểm NFR-15 của luồng SEC)
        build_argv(_spec(), _cfg(extra_args=["--allow-dangerously-skip-permissions"]), "2.1.302")
    with pytest.raises(ArgvError, match="ma trận"):
        build_argv(_spec(), _cfg(extra_args=["--chrome"]), "2.1.302")  # cờ có thật của CLI nhưng không trong ma trận
    with pytest.raises(ArgvError):
        build_argv(_spec(), _cfg(extra_args=["--dangerously-skip-permissions=true"]), "2.1.302")
    build_argv(_spec(), _cfg(extra_args=["--bogus-flag"]), "9.9.9")  # phiên bản lạ: chỉ cảnh báo vàng, không chặn
    build_argv(_spec(), _cfg(extra_args=["--bogus-flag"]))  # không biết phiên bản → không kiểm


async def test_h10_spawn_refused_with_unsupported_flag(hub_home, tmp_path):
    """QA: trước đây cờ không hỗ trợ vẫn được spawn — giờ run failed với lỗi ma trận, KHÔNG spawn tiến trình."""
    fake = tmp_path / "claude"
    marker = tmp_path / "spawned"
    fake.write_text(f"#!/bin/sh\nif [ \"$1\" = \"--version\" ]; then echo '2.1.302 (Claude Code)'; exit 0; fi\ntouch {marker}\nexit 1\n")
    fake.chmod(0o755)
    hub_home.config.write_text(yaml.safe_dump({"runtime": {"claude": {"binary": str(fake), "isolation": {"extra_args": ["--bogus-flag"]}}}}))
    h = Hub(hub_home)
    try:
        h.agents.create({"id": "a", "name": "A"})
        r = h.runs.submit("a", "x")
        done = await h.runs.wait(r["id"], timeout=10)
        assert done["status"] == "failed" and "--bogus-flag" in (done["error"] or "")
        assert not marker.exists()
    finally:
        await h.stop()


class _ClaudeLike(FakeRuntime):
    name = "claude"


async def test_h10_version_checked_on_start_and_shown_in_tui(hub_home):
    from cfharness.tui.rpc import snapshot

    h = Hub(hub_home, runtime=_ClaudeLike(cli_version="9.9.9"))
    try:
        h.start()
        await h.compat.task
        st = h.store.state_get("cli_compat")
        assert st["version"] == "9.9.9" and st["status"] == "untested" and st["tested"] == ["2.1.302"]
        warn = [e for e in h.store.query_events(types=["cli.version_warning"])]
        assert warn and warn[0].payload["version"] == "9.9.9" and warn[0].payload["at"] == "daemon_start"
        hdr = snapshot(h)["hub"]
        assert hdr["cli_version"] == "9.9.9" and hdr["cli_untested"] is True
    finally:
        await h.stop()


def test_h10_raw_unknown_rate_alert(hub):
    clock = [1000.0]
    hub.compat.clock = lambda: clock[0]

    def cli_ev(t):
        hub.publish(make_event(t, "a", {}, run_id="r_x", source="cli"))

    for i in range(60):
        cli_ev("raw.unknown" if i % 6 == 0 else "tool.start")  # 10/60 ≈ 16.7% > 5%
    for _ in range(500):
        hub.publish(make_event("message.delta", "a", {"text": "x"}, run_id="r_x", source="cli"))  # delta không làm loãng tỉ lệ
    alerts = hub.store.query_events(types=["cli.unknown_rate_alert"])
    assert len(alerts) == 1 and alerts[0].payload["unknown"] >= 9 and alerts[0].level == "summary"
    assert "estop" in alerts[0].payload["suggestion"]
    assert hub.store.state_get("cli_unknown_rate")["alert"] is True
    clock[0] += 4000  # hết cửa sổ 1 giờ → reset
    for _ in range(60):
        cli_ev("tool.start")
    assert hub.store.state_get("cli_unknown_rate")["alert"] is False
    assert len(hub.store.query_events(types=["cli.unknown_rate_alert"])) == 1


def test_h10_unknown_rate_below_threshold_no_alert(hub):
    for i in range(100):
        hub.publish(make_event("raw.unknown" if i % 40 == 39 else "tool.start", "a", {}, run_id="r_y", source="cli"))  # 2,5%
    assert hub.store.query_events(types=["cli.unknown_rate_alert"]) == []


# ============================================================================ H-11 hook quan sát fail-open
def test_h11_observe_hooks_in_settings_keep_guard(hub_home):
    from cfharness.observe_hook import add_observe_hooks

    base = {"autoMemoryEnabled": False, "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "guard || exit 2"}]}]}}
    s, evs = add_observe_hooks(base, run_id="r_1", socket_path="/tmp/h.sock", hub_home=str(hub_home.home), python=sys.executable, cfg=DEFAULTS["runtime"]["claude"]["observe_hooks"])
    assert evs == ["PostToolUse", "UserPromptSubmit", "Stop"]
    assert s["hooks"]["PreToolUse"] == base["hooks"]["PreToolUse"]  # guard fail-closed giữ nguyên
    for ev in evs:
        h = s["hooks"][ev][-1]["hooks"][0]
        assert "cfharness.observe_hook " + ev in h["command"] and h["command"].endswith("|| true") and h["timeout"] == 5
    assert s["hooks"]["PostToolUse"][-1]["matcher"] == "*"
    s2, evs2 = add_observe_hooks(base, run_id="r", socket_path="/s", hub_home="/h", python="py", cfg={"enabled": False})
    assert evs2 == [] and s2 is base


async def test_h11_run_settings_contain_observe_hooks(hub):
    rt = FakeRuntime(hold=True)
    hub.runs._runtime = rt
    hub.config.set("guard.enabled", True)
    hub.agents.create({"id": "o", "name": "O"})
    r = hub.runs.submit("o", "x")
    for _ in range(200):
        if rt.started:
            break
        await asyncio.sleep(0.01)
    spec = rt.started[0]
    settings = json.loads(Path(spec.settings_path).read_text())
    assert {"PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"} <= set(settings["hooks"])
    assert {"PostToolUse", "UserPromptSubmit", "Stop", "PreToolUse"} <= set(spec.allowed_hooks)
    rt.release()
    await hub.runs.wait(r["id"], timeout=5)


def _slow_server(path: str, delay: float, got: list):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(c,), daemon=True).start()

    def handle(c):
        data = c.recv(65536)
        got.append(json.loads(data.decode().strip().splitlines()[0]))
        if b'"hook.observe"' in data:
            time.sleep(delay)
        c.close()

    threading.Thread(target=loop, daemon=True).start()
    return srv


def test_h11_hook_timeout_fail_open(tmp_path):
    from cfharness.observe_hook import observe

    import tempfile

    sock = os.path.join(tempfile.mkdtemp(prefix="ah-", dir="/tmp"), "s.sock")  # AF_UNIX giới hạn độ dài đường dẫn
    got: list = []
    srv = _slow_server(sock, 3.5, got)
    try:
        t0 = time.monotonic()
        res = observe(["PostToolUse", "--run", "r_1", "--socket", sock, "--timeout", "2", "--home", str(tmp_path)],
                      stdin_text=json.dumps({"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "echo SECRET"}}))
        dt = time.monotonic() - t0
        assert res["ok"] is False and res["reason"] == "timeout" and res["notified"] is True
        assert 1.9 <= dt < 3.0  # không chờ quá 2 s + biên
        time.sleep(0.2)
        methods = [m["method"] for m in got]
        assert methods[0] == "hook.observe" and "hook.timeout" in methods
        assert "id" not in got[methods.index("hook.timeout")]  # notification: hook không chờ trả lời
        assert "SECRET" not in json.dumps(got)  # chỉ metadata
    finally:
        srv.close()


async def test_h11_hook_unreachable_spools_and_daemon_ingests(hub_home):
    import subprocess

    r = subprocess.run([sys.executable, "-I", "-m", "cfharness.observe_hook", "Stop", "--run", "r_zz", "--socket", str(hub_home.home / "nope.sock"),
                        "--timeout", "2", "--home", str(hub_home.home)], input=b"{not json", capture_output=True, timeout=20)
    assert r.returncode == 0 and r.stdout == b""  # fail-open, không in quyết định
    spool = hub_home.logs_dir / "hook-timeouts.jsonl"
    assert spool.exists() and json.loads(spool.read_text().splitlines()[0])["reason"] == "unreachable"
    h = Hub(hub_home, runtime=FakeRuntime())
    try:
        h.start()
        await h.compat.task
        ev = h.store.query_events(types=["hook.timeout"])
        assert ev and ev[0].payload["spooled"] and ev[0].payload["fail_open"] and ev[0].source == "hook"
        assert not spool.exists()
    finally:
        await h.stop()


def test_h11_hook_cli_and_rpc_record_events(daemon, capsys):
    from cfharness.observe_hook import observe
    from cfharness.rpc import RpcClient

    rpc = RpcClient(daemon.socket, timeout=30)
    rpc.call("agent.create", {"config": {"id": "hk", "name": "HK"}})
    run = rpc.call("run.start", {"agent": "hk", "prompt": "x"})
    res = observe(["PostToolUse", "--run", run["id"], "--socket", str(daemon.socket)], stdin_text=json.dumps({"tool_name": "Read", "session_id": "s1"}))
    assert res["ok"]
    code, out, _ = cli(capsys, "hook", "UserPromptSubmit", "--run", run["id"], "--json")
    assert code == 0
    rpc.call("hook.timeout", {"run_id": run["id"], "event": "Stop", "timeout_s": 2, "elapsed_ms": 2001})
    evs = rpc.call("events.query", {"run_id": run["id"]})
    obs = [e for e in evs if e["type"] == "hook.observed"]
    assert obs and obs[0]["payload"]["tool_name"] == "Read" and obs[0]["source"] == "hook"
    to = [e for e in evs if e["type"] == "hook.timeout"]
    assert to and to[0]["payload"]["fail_open"] is True


# ============================================================================ H-13 MCP chưa probe / untrusted
def test_h13_unprobed_literal_include_exclude_rules(hub):
    from cfharness.mcp_client import agent_mcp_tool_rules

    hub.mcp.add({"name": "viec", "command": "echo", "trust": "trusted", "tools": {"include": ["tim_viec", "doc_viec", "xem_*"], "exclude": ["xoa_viec"], "require_approval": ["doc_viec"]}})
    hub.agents.create({"id": "m", "name": "M", "mcp_servers": ["viec"]})
    rules = agent_mcp_tool_rules(hub.agents.get_row("m"))
    assert rules["allowed"] == ["mcp__viec__tim_viec"]  # doc_viec cần duyệt → không cho tĩnh
    assert rules["disallowed"] == ["mcp__viec__xoa_viec"]
    assert any("glob" in w for w in rules["warnings"])
    hub.mcp.update("viec", {"trust": "untrusted"})
    rules = agent_mcp_tool_rules(hub.agents.get_row("m"))
    assert rules["allowed"] == [] and rules["disallowed"] == ["mcp__viec__xoa_viec"]


async def test_h13_unprobed_rules_reach_argv(hub):
    rt = FakeRuntime()
    hub.runs._runtime = rt
    hub.mcp.add({"name": "viec", "command": "echo", "trust": "trusted", "tools": {"include": ["tim_viec"], "exclude": ["xoa_viec"]}})
    hub.agents.create({"id": "m", "name": "M", "mcp_servers": ["viec"]})
    r = hub.runs.submit("m", "x")
    await hub.runs.wait(r["id"], timeout=5)
    argv, _ = build_argv(rt.started[0], hub.config.data["runtime"]["claude"], "2.1.302")
    allowed = argv[argv.index("--allowedTools") + 1 :]
    assert "mcp__viec__tim_viec" in allowed
    assert "mcp__viec__xoa_viec" in argv[argv.index("--disallowedTools") + 1 :]


def test_h13_classify_untrusted_requires_approval(hub_home):
    from cfharness.guard.classifier import GuardContext, classify
    from cfharness.guard.policy import PolicyStore

    ps = PolicyStore(hub_home.home)
    ps.ensure()
    ann = {("srv", "tao_viec"): {"trust": "untrusted", "readOnlyHint": None}, ("srv", "tim_viec"): {"trust": "untrusted", "readOnlyHint": True},
           ("ok", "tao_viec"): {"trust": "trusted", "readOnlyHint": None}}
    ctx = GuardContext(cwd=hub_home.home, policy=ps.load(), mcp_annotations=lambda s, t: ann.get((s, t)), git_probe=False)
    v = classify("mcp__srv__tao_viec", {"x": 1}, ctx)
    assert v.verdict == "uncertain" and v.rule == "mcp_untrusted_write" and v.hold
    assert classify("mcp__srv__tim_viec", {}, ctx).verdict == "safe"
    assert classify("mcp__ok__tao_viec", {}, ctx).verdict == "write"
    v = classify("mcp__srv__chua_probe", {}, ctx)  # server chưa probe/tool lạ → cần duyệt
    assert v.verdict == "uncertain" and v.hold


async def test_h13_untrusted_mcp_tool_opens_approval(hub):
    rt = FakeRuntime(hold=True)
    hub.runs._runtime = rt
    hub.config.set("guard.enabled", True)
    hub.config.set("approval.timeout_s", 1)
    hub.mcp.add({"name": "viec", "command": "echo", "trust": "untrusted"})
    hub.agents.create({"id": "u", "name": "U", "mcp_servers": ["viec"]})
    r = hub.runs.submit("u", "x")
    for _ in range(200):
        if rt.started:
            break
        await asyncio.sleep(0.01)
    task = asyncio.ensure_future(hub.guard.check({"run_id": r["id"], "tool_name": "mcp__viec__tao_viec", "tool_input": {"ten": "a"}, "channel": "hook"}))
    for _ in range(200):
        reqs = hub.approvals.list("pending", run_id=r["id"])
        if reqs:
            break
        await asyncio.sleep(0.02)
    assert reqs and reqs[0]["tool_name"] == "mcp__viec__tao_viec"
    res = await asyncio.wait_for(task, 30)
    assert res["decision"] != "allow"  # không bao giờ tự cho phép khi chưa có người duyệt
    rt.release()
    await hub.runs.wait(r["id"], timeout=10)


# ============================================================================ H-14 MCP hub_* + hub-self
def test_h14_hub_self_worker_has_memory_search():
    from cfharness.mcp_server.hub_self import ROLE_TOOLS, TOOLS

    for role in ("worker", "lead", "reviewer"):
        assert "memory_search" in ROLE_TOOLS[role]
    assert TOOLS["memory_search"]["annotations"]["readOnlyHint"] is True


def test_h14_memory_search_finds_memory_skill_transcript(hub):
    from cfharness.mcp_server.surface_rpc import memory_search

    hub.agents.create({"id": "s", "name": "S"})
    home = Path(hub.agents.get_row("s")["home_path"])
    (home / "MEMORY.md").write_text("Build uses pnpm not npm\n§\nDates are dd/mm/yyyy in reports\n", encoding="utf-8")
    (home / "skills" / "weekly-report").mkdir(parents=True)
    (home / "skills" / "weekly-report" / "SKILL.md").write_text("---\nname: weekly-report\ndescription: weekly report\n---\nAlways use dd/mm/yyyy dates.\n")
    hub.store.insert_run({"id": "r_t1", "agent_id": "s", "kind": "adhoc", "prompt": "báo cáo tuần với dd/mm/yyyy", "status": "completed", "queued_at": "2026-10-01T00:00:00Z", "summary": "xong"})
    hub.store.insert_run({"id": "r_t2", "agent_id": "s", "kind": "review", "prompt": "dd/mm/yyyy review", "status": "completed", "queued_at": "2026-10-01T00:00:01Z"})
    res = memory_search(hub, "dd/mm/yyyy", agent_id="s")
    kinds = {r["kind"] for r in res["results"]}
    assert {"memory", "skill", "transcript"} <= kinds
    assert all(r.get("run_id") != "r_t2" for r in res["results"])  # ẩn phiên kỹ thuật
    assert memory_search(hub, "pnpm", agent_id="s", scope="memory")["results"][0]["text"].startswith("Build uses pnpm")
    with pytest.raises(HubError):
        memory_search(hub, "  ")


async def test_h14_mcp_tools_params(daemon):
    from mcp import Client

    from cfharness.mcp_server.server import READ_ONLY_TOOLS, build_server
    from cfharness.rpc import RpcClient

    rpc = RpcClient(daemon.socket, timeout=30)
    srv = build_server(lambda m, p: RpcClient(daemon.socket, timeout=60).call(m, p))
    async with Client(srv) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        for name in ("hub_policy_get", "hub_memory_search"):
            assert name in READ_ONLY_TOOLS and tools[name].annotations.read_only_hint is True
        assert "agent_id" in tools["hub_events"].input_schema["properties"]
        assert "task_id" in tools["hub_cancel"].input_schema["properties"]
        assert "team" in tools["hub_estop"].input_schema["properties"]
        assert {"team", "status"} <= set(tools["hub_list_agents"].input_schema["properties"])
        assert "detach_teams" in tools["hub_agent_archive"].input_schema["properties"]
        pol = (await c.call_tool("hub_policy_get", {})).structured_content
        assert pol["guard"] and "resources" in pol["guard"]
        rpc.call("agent.create", {"config": {"id": "ev", "name": "EV"}})
        rpc.call("agent.create", {"config": {"id": "w2", "name": "W2"}})
        rpc.call("team.create", {"config": {"id": "tt", "name": "TT", "members": [{"agent_id": "w2", "role": "worker"}]}})
        lst = (await c.call_tool("hub_list_agents", {"team": "tt"})).structured_content["agents"]
        assert [a["id"] for a in lst] == ["w2"]
        run = (await c.call_tool("hub_run", {"agent": "ev", "prompt": "hi"})).structured_content
        evs = (await c.call_tool("hub_events", {"agent_id": "ev", "wait_s": 5})).structured_content
        assert evs["run_id"] == run["run_id"] and "open_requests" in evs
        bad = await c.call_tool("hub_events", {"agent_id": "ev", "run_id": run["run_id"]})
        assert bad.is_error
        r = await c.call_tool("hub_cancel", {"task_id": "khong-co"})
        assert r.is_error and "task" in r.content[0].text
        es = (await c.call_tool("hub_estop", {"on": True, "team": "tt", "reason": "thử"})).structured_content
        assert es["on"] is True and rpc.call("estop.get")["on"] is False  # chỉ team, không toàn hub
        await c.call_tool("hub_estop", {"on": False, "team": "tt"})
        ms = (await c.call_tool("hub_memory_search", {"query": "hi", "agent_id": "ev"})).structured_content
        assert "results" in ms
        # H-06 qua MCP: archive bị chặn khi còn là thành viên team → detach_teams
        r = await c.call_tool("hub_agent_archive", {"agent_id": "w2"})
        assert r.is_error and "tt (worker)" in r.content[0].text
        r = (await c.call_tool("hub_agent_archive", {"agent_id": "w2", "detach_teams": True})).structured_content
        assert r["status"] == "archived" and r["detached_teams"] == ["tt"]


# ============================================================================ H-20 log có run_id/agent_id, oneshot qua runtime
def test_h20_logger_adapter_and_context(tmp_path):
    from cfharness.logs import JsonFormatter, RunContextFilter, run_logger, set_run_context

    rec_out: list[str] = []

    class H(logging.Handler):
        def emit(self, record):
            rec_out.append(self.format(record))

    lg = logging.getLogger("cfharness.test_h20")
    h = H()
    h.setFormatter(JsonFormatter())
    h.addFilter(RunContextFilter())
    lg.addHandler(h)
    lg.setLevel(logging.INFO)
    try:
        run_logger(lg, "r_1", "a1", task_id="t_1").warning("run %s lỗi", "r_1")
        d = json.loads(rec_out[-1])
        assert d["run_id"] == "r_1" and d["agent_id"] == "a1" and d["task_id"] == "t_1"

        async def in_run():
            set_run_context("r_2", "a2")
            lg.info("từ module khác trong task của run")

        asyncio.run(in_run())
        d = json.loads(rec_out[-1])
        assert d["run_id"] == "r_2" and d["agent_id"] == "a2"
        lg.info("ngoài run")
        assert "run_id" not in json.loads(rec_out[-1])
    finally:
        lg.removeHandler(h)


async def test_h20_oneshot_via_runtime(tmp_path):
    from cfharness.runtime import ClaudeCliRuntime, OneShotRequest

    fake = tmp_path / "judge.py"
    fake.write_text("import sys, json\nargs=sys.argv[1:]\nsys.stdin.read()\n"
                    "assert '--json-schema' in args and '--no-session-persistence' in args and args[args.index('--tools')+1] == ''\n"
                    "print(json.dumps({'type':'result','is_error':False,'total_cost_usd':0.001,'structured_output':{'score':0.9}}))\n")
    binp = tmp_path / "judge"
    binp.write_text(f"#!/bin/sh\nexec {sys.executable} {fake} \"$@\"\n")
    binp.chmod(0o755)
    rt = ClaudeCliRuntime({**DEFAULTS["runtime"]["claude"], "binary": str(binp)})
    r = await rt.oneshot(OneShotRequest(prompt="p", schema={"type": "object"}, system="s", model="haiku", budget_usd=0.05, timeout_s=20))
    assert r.ok and r.output == {"score": 0.9} and r.cost_usd == 0.001
    f = FakeRuntime()
    f.oneshot_results = [{"score": 1}]
    r2 = await f.oneshot(OneShotRequest(prompt="p", schema={}, system="s", model="haiku"))
    assert r2.ok and r2.output == {"score": 1} and len(f.oneshot_calls) == 1


# ============================================================================ Thấp: FR-AG-08 doctor thử kết nối MCP
def test_low_doctor_probes_agent_mcp(daemon, capsys):
    from cfharness.rpc import RpcClient

    rpc = RpcClient(daemon.socket, timeout=30)
    rpc.call("mcp.add", {"config": {"name": "vb", "command": "/khong/ton/tai/mcp"}, "probe": False})
    rpc.call("agent.create", {"config": {"id": "coder", "name": "C", "mcp_servers": ["vb"]}})
    code, out, _ = cli(capsys, "doctor", "coder", "--probe-timeout", "5")
    assert code != 0 and "✗ MCP vb: không kết nối được" in out


# ============================================================================ Thấp: NFR-26 màn trợ giúp `?`
async def test_low_tui_help_screen_lists_all_keys(hub_home):
    from test_tui import ISOLATED, SIZE, start_daemon, stop_daemon

    from cfharness.tui.app import HelpScreen, HtopScreen, TopApp

    ctl = await start_daemon(hub_home, FakeRuntime(ISOLATED))
    try:
        app = TopApp(hub_home.socket, refresh_s=0.5)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause(0.3)
            await pilot.press("question_mark")
            await pilot.pause(0.2)
            assert isinstance(app.screen, HelpScreen)
            keys = {k for k, _, _ in HelpScreen.rows_for(app.screen.target, app)}
            assert {"a", "d", "x", "E", "T", "N"} <= keys  # cả phím ẩn khỏi footer
            assert isinstance(app.screen.target, HtopScreen)
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert not isinstance(app.screen, HelpScreen)
    finally:
        await stop_daemon(ctl)
