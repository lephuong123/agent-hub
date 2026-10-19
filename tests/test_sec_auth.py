"""Hardening SEC — H-01/H-05: agent không tự duyệt / không mạo danh người qua RPC, dù viết lệnh kiểu gì.

Daemon THẬT trên Unix socket THẬT; "tiến trình run" là một tiến trình Python thật được đăng ký làm pid của run
(giống pid `claude`), client là tiến trình con/cháu của nó — đúng kịch bản QA:
`python -m cfharness.cli approve`, `echo approve <id> | xargs cfharness`, client socket Python tự viết (kể cả đã đọc
được token người), nohup/double-fork mồ côi. Người (test process / CLI con của test) vẫn duyệt được, không bước tay.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from cfharness import auth
from cfharness.daemon import Daemon
from cfharness.paths import HubPaths
from cfharness.rpc import RpcClient, RpcError
from cfharness.runtime.fake import FakeRuntime

CFHARNESS_BIN = shutil.which("cfharness") or str(Path(sys.executable).parent / "cfharness")


@pytest.fixture
def home(tmp_path, monkeypatch):
    # đường dẫn ngắn để socket nằm trong HUB_HOME (và test cả nhánh /tmp ở test khác)
    h = Path(os.path.realpath("/tmp")) / f"ahsec-{os.getpid()}-{time.time_ns() % 10**6}"
    monkeypatch.setenv("CFHARNESS_HOME", str(h))
    monkeypatch.delenv("CFHARNESS_SOCKET", raising=False)
    p = HubPaths.resolve(h)
    p.ensure()
    p.config.write_text("guard:\n  git_probe: false\nhub:\n  max_concurrent_runs: 20\n")
    yield p
    shutil.rmtree(h, ignore_errors=True)


async def _start(p: HubPaths) -> tuple[Daemon, asyncio.base_events.Server]:
    d = Daemon(p, runtime=FakeRuntime(hold=True))
    d.hub.start()
    srv = await asyncio.start_unix_server(d._handle_conn, path=str(p.socket))
    os.chmod(p.socket, 0o600)
    return d, srv


async def _stop(d: Daemon, srv) -> None:
    srv.close()
    d.hub.runs._runtime.release()
    await d.hub.stop()


def _open_approval(d: Daemon, run: dict) -> str:
    ap = d.hub.approvals.open(run, tool_name="Bash", tool_input={"command": "rm -rf docs"}, tool_use_id=None, fingerprint="fp",
                              verdict={"verdict": "destructive", "rule": "delete_not_run_created", "klass": "file"}, channel="hook")
    return ap["id"]


async def _start_run(d: Daemon) -> tuple[dict, "subprocess.Popen[bytes]", Path]:
    """Run thật (FakeRuntime giữ) + tiến trình 'claude' giả thật — pid của nó được ghi làm pid của run."""
    h = d.hub
    if not h.store.get_agent("a1"):
        h.agents.create({"id": "a1", "name": "A1", "limits": {"max_concurrent_runs": 20}})
    r = h.runs.submit("a1", "x")
    for _ in range(200):
        await asyncio.sleep(0.01)
        ar = h.runs.active.get(r["id"])
        if ar is not None and ar.handle is not None:
            break
    ar = h.runs.active[r["id"]]
    go = Path(d.paths.home) / f"go-{r['id']}"
    out = Path(d.paths.home) / f"out-{r['id']}.json"
    helper = textwrap.dedent(f"""
        import json, os, subprocess, sys, time
        go, out = {str(go)!r}, {str(out)!r}
        while not os.path.exists(go):
            time.sleep(0.02)
        spec = json.load(open(go))
        env = dict(os.environ); env.update(spec.get("env") or {{}})
        for k in spec.get("unset") or []:
            env.pop(k, None)
        p = subprocess.run(spec["cmd"], shell=True, capture_output=True, text=True, env=env, timeout=60)
        json.dump({{"rc": p.returncode, "out": p.stdout, "err": p.stderr}}, open(out + ".tmp", "w")); os.replace(out + ".tmp", out)
        time.sleep(spec.get("linger", 0))
    """)
    proc = subprocess.Popen([sys.executable, "-c", helper], start_new_session=True,
                            env={**os.environ, "CFHARNESS_RUN_ID": r["id"], "CFHARNESS_HOME": str(d.paths.home)})
    ar.handle.pid = proc.pid  # = pid tiến trình `claude` của run
    info = auth.proc_info(proc.pid)
    h.store.update_run(r["id"], {"pid": proc.pid, "pid_start_time": info.start if info else time.time()})
    return h.store.get_run(r["id"]), proc, go


async def _in_run(go: Path, cmd: str, *, env: dict | None = None, unset: list[str] | None = None, linger: float = 0) -> dict:
    out = Path(str(go).replace("/go-", "/out-") + ".json")
    go.write_text(json.dumps({"cmd": cmd, "env": env or {}, "unset": unset or [], "linger": linger}))
    for _ in range(3000):
        await asyncio.sleep(0.02)
        if out.exists():
            return json.loads(out.read_text())
    raise AssertionError("lệnh trong run không xong")


def _client_py(home: HubPaths, method: str, params: dict, *, steal_token: bool, setsid: bool = False, double_fork: bool = False, result: Path | None = None) -> str:
    """Client socket tự viết (QA): kết nối thẳng hub.sock, tuỳ chọn đọc trộm token người, setsid/double-fork."""
    code = textwrap.dedent(f"""
        import json, os, socket, sys, time
        def go():
            p = dict({params!r})
            if {steal_token!r}:
                try: p["_auth"] = open({str(auth.token_path(home.socket))!r}).read().strip()
                except OSError: pass
            s = socket.socket(socket.AF_UNIX); s.connect({str(home.socket)!r})
            s.sendall((json.dumps({{"jsonrpc": "2.0", "id": 1, "method": {method!r}, "params": p}}) + "\\n").encode())
            r = s.makefile().readline()
            out = {str(result) if result else ''!r}
            if out:
                open(out + ".tmp", "w").write(r); os.replace(out + ".tmp", out)
            print(r)
        if {double_fork!r}:
            if os.fork() > 0: sys.exit(0)
            if {setsid!r}: os.setsid()
            if os.fork() > 0: sys.exit(0)
            time.sleep(0.5)
        elif {setsid!r}:
            os.setsid()
        go()
    """)
    import shlex

    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


# =============================================================================== người vẫn duyệt được
async def test_human_cli_and_client_can_approve_without_manual_step(home):
    d, srv = await _start(home)
    try:
        run, proc, go = await _start_run(d)
        # token tự sinh cạnh socket, 0600, không nằm trong env của run
        tp = auth.token_path(home.socket)
        assert tp.exists() and oct(tp.stat().st_mode & 0o777) == "0o600"
        aid = _open_approval(d, run)
        # (1) client của người trong tiến trình test
        res = await asyncio.to_thread(RpcClient(home.socket).call, "approval.approve", {"id": aid})
        assert res["status"] == "allowed"
        # (2) CLI thật của người (tiến trình con của test, KHÔNG thuộc run) — tự đọc token
        aid2 = _open_approval(d, run)
        cp = await asyncio.to_thread(subprocess.run, [CFHARNESS_BIN, "approve", aid2], capture_output=True, text=True, timeout=60,
                                     env={**os.environ, "CFHARNESS_HOME": str(home.home)})
        assert cp.returncode == 0, cp.stderr
        assert d.hub.approvals.get(aid2)["status"] == "allowed"
        proc.kill()
    finally:
        await _stop(d, srv)


# =============================================================================== QA H-01: 3 cách tự duyệt từ trong run
async def test_agent_cannot_self_approve_python_m_xargs_or_raw_socket(home):
    d, srv = await _start(home)
    try:
        run, proc, go = await _start_run(d)
        aid = _open_approval(d, run)
        # QA #1: python3 -m cfharness.cli approve <id>
        r1 = await _in_run(go, f"{sys.executable} -m cfharness.cli approve {aid}")
        assert r1["rc"] != 0 and "chỉ dành cho NGƯỜI" in (r1["err"] + r1["out"]), r1
        assert d.hub.approvals.get(aid)["status"] in ("open", "waiting_human")
        proc.wait(5)
        # QA #2: echo approve <id> | xargs cfharness
        run2, proc2, go2 = await _start_run(d)
        r2 = await _in_run(go2, f"echo approve {aid} | xargs {CFHARNESS_BIN}")
        assert r2["rc"] != 0 and "chỉ dành cho NGƯỜI" in (r2["err"] + r2["out"]), r2
        proc2.wait(5)
        # QA #3: client socket Python tự viết — kể cả khi ĐÃ đọc trộm được token người
        run3, proc3, go3 = await _start_run(d)
        r3 = await _in_run(go3, _client_py(home, "approval.approve", {"id": aid}, steal_token=True))
        assert r3["out"].strip(), r3
        resp = json.loads(r3["out"].strip().splitlines()[-1])
        assert resp["error"]["code"] == -32003 and resp["error"]["data"]["caller"]["evidence"], resp
        proc3.wait(5)
        assert d.hub.approvals.get(aid)["status"] in ("open", "waiting_human")
        # mỗi lần bị từ chối: event auth.denied + audit
        evs = [e for e in d.hub.store.query_events() if e.type == "auth.denied"]
        assert len(evs) >= 3 and all(e.payload["method"] == "approval.approve" for e in evs)
    finally:
        await _stop(d, srv)


async def test_orphaned_double_fork_and_env_escape_still_detected(home):
    """nohup/double-fork (mồ côi về launchd SAU khi tiến trình run đã thoát) → vẫn nhận ra qua session id của run gần đây;
    setsid + double-fork nhưng còn CFHARNESS_RUN_ID trong env → nhận ra qua env."""
    d, srv = await _start(home)
    try:
        run, proc, go = await _start_run(d)
        aid = _open_approval(d, run)
        res_file = Path(home.home) / "orphan.json"
        await _in_run(go, _client_py(home, "approval.approve", {"id": aid}, steal_token=True, double_fork=True, result=res_file), unset=["CFHARNESS_RUN_ID"])
        proc.wait(5)  # tiến trình 'claude' đã thoát — cháu mồ côi kết nối sau đó
        for _ in range(300):
            await asyncio.sleep(0.02)
            if res_file.exists():
                break
        resp = json.loads(res_file.read_text())
        assert "error" in resp and any("session" in e for e in resp["error"]["data"]["caller"]["evidence"]), resp
        # setsid + double-fork, env còn CFHARNESS_RUN_ID
        run2, proc2, go2 = await _start_run(d)
        res2 = Path(home.home) / "setsid.json"
        await _in_run(go2, _client_py(home, "approval.approve", {"id": aid}, steal_token=True, setsid=True, double_fork=True, result=res2))
        for _ in range(300):
            await asyncio.sleep(0.02)
            if res2.exists():
                break
        resp2 = json.loads(res2.read_text())
        assert "error" in resp2 and "env_run_id" in resp2["error"]["data"]["caller"]["evidence"], resp2
        # thiếu token (sandbox chặn đọc) + mọi dấu vết bị xoá → vẫn bị từ chối vì không có token người
        run3, proc3, go3 = await _start_run(d)
        res3 = Path(home.home) / "notoken.json"
        await _in_run(go3, _client_py(home, "approval.approve", {"id": aid}, steal_token=False, setsid=True, double_fork=True, result=res3),
                      unset=["CFHARNESS_RUN_ID"])
        for _ in range(300):
            await asyncio.sleep(0.02)
            if res3.exists():
                break
        assert "error" in json.loads(res3.read_text())
        assert d.hub.approvals.get(aid)["status"] in ("open", "waiting_human")
        for p in (proc2, proc3):
            p.kill()
    finally:
        await _stop(d, srv)


# =============================================================================== QA H-05: mạo danh người qua learn/agent
async def test_agent_learn_propose_downgraded_and_agent_update_denied(home):
    d, srv = await _start(home)
    try:
        run, proc, go = await _start_run(d)
        # learn propose từ trong run (CLI thật) → KHÔNG là created_by=user; đi qua guard học
        r = await _in_run(go, f"{CFHARNESS_BIN} --json learn propose a1 --target memory --op add --payload '{{\"text\": \"x\"}}'")
        props = d.hub.learning.pipeline.list("a1", None, 50)
        assert props, r
        assert all(p["created_by"] != "user" for p in props) and props[0]["created_by"] == "agent"
        proc.wait(5)
        run2, proc2, go2 = await _start_run(d)
        r2 = await _in_run(go2, _client_py(home, "agent.update", {"id": "a1", "changes": {"tool_policy": {"allow_bypass": True}}}, steal_token=True))
        resp = json.loads(r2["out"].strip().splitlines()[-1])
        assert resp["error"]["code"] == -32003
        assert not (d.hub.store.get_agent("a1").get("tool_policy") or {}).get("allow_bypass")
        for m, prm in (("learn.rollback", {"id": "le_x"}), ("config.set", {"key": "approval.timeout_s", "value": 1}),
                       ("guard.protect.rm", {"id": "x"}), ("golden.add", {"agent_id": "a1", "prompt": "p"}), ("secret.set", {"name": "X", "value": "yyyyyyyy"})):
            run_n, proc_n, go_n = await _start_run(d)
            rr = await _in_run(go_n, _client_py(home, m, prm, steal_token=True))
            resp = json.loads(rr["out"].strip().splitlines()[-1])
            assert resp.get("error", {}).get("code") == -32003, (m, resp)
            proc_n.wait(5)
        assert d.hub.config.get("approval.timeout_s") != 1
        proc2.kill()
    finally:
        await _stop(d, srv)


async def test_agent_mutating_calls_are_audited_as_agent(home):
    """Lời gọi ghi KHÔNG nhạy cảm từ run (vd feedback) vẫn chạy nhưng audit ghi actor=agent (không tin `_audit` client tự khai)."""
    d, srv = await _start(home)
    try:
        run, proc, go = await _start_run(d)
        rr = await _in_run(go, _client_py(home, "feedback.record", {"run_id": run["id"], "kind": "good", "_audit": {"actor": "user", "channel": "cli"}}, steal_token=True))
        rows = [dict(r) for r in d.hub.store.conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 5")]
        mine = [r for r in rows if "feedback" in str(r.get("kind") or r.get("action") or "")]
        assert mine and all(str(r.get("actor")) != "user" for r in mine), (rr, rows)
        proc.kill()
    finally:
        await _stop(d, srv)


async def test_socket_caller_without_token_denied_even_if_not_in_run(home):
    d, srv = await _start(home)
    try:
        def raw() -> dict:
            import socket

            s = socket.socket(socket.AF_UNIX)
            s.connect(str(home.socket))
            s.sendall((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "config.set", "params": {"key": "approval.timeout_s", "value": 2}}) + "\n").encode())
            return json.loads(s.makefile().readline())

        # tiến trình con (không token) — không phải hậu duệ run nhưng thiếu token → từ chối
        cp = await asyncio.to_thread(subprocess.run, [sys.executable, "-c",
            "import json,socket,sys;s=socket.socket(socket.AF_UNIX);s.connect(sys.argv[1]);"
            "s.sendall((json.dumps({'jsonrpc':'2.0','id':1,'method':'config.set','params':{'key':'approval.timeout_s','value':2}})+'\\n').encode());"
            "print(s.makefile().readline())", str(home.socket)], capture_output=True, text=True, timeout=30)
        assert json.loads(cp.stdout)["error"]["code"] == -32003
        assert d.hub.config.get("approval.timeout_s") != 2
        # đọc vẫn được (không đổi hành vi cho client cũ/hook)
        r = await asyncio.to_thread(RpcClient(home.socket).call, "hub.ping")
        assert r["pong"]
    finally:
        await _stop(d, srv)


def test_human_only_covers_sensitive_surface():
    for m in ("approval.approve", "approval.deny", "guard.protect.add", "guard.protect.rm", "config.set", "learn.propose", "learn.rollback",
              "learn.retry", "learn.refine", "agent.update", "golden.add", "golden.rm", "learn.policy_set", "secret.set", "mcp.add", "estop.set"):
        assert m in auth.HUMAN_ONLY, m
    assert "guard.check" not in auth.HUMAN_ONLY and "taskself.call" not in auth.HUMAN_ONLY  # hook/hub-self không bị chặn
