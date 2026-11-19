"""L2 (docs/dev-notes/plan-logging.md §3, B3): mã yêu cầu `req_id` đi từ client → daemon → run → event → hook guard → audit → hub.log,
và access log RPC `cfharness.rpc`.

Daemon THẬT trên Unix socket THẬT (như tests/test_sec_auth.py); hook guard chạy là tiến trình con THẬT (`python -m cfharness.guard.hook`)
với env của run — không gọi `claude`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from cfharness import logs, oscompat, trace, transport
from cfharness.daemon import Daemon
from cfharness.oscompat import IS_WINDOWS
from cfharness.paths import HubPaths
from cfharness.rpc import RpcClient, RpcError
from cfharness.runtime.fake import FakeRuntime
from cfharness.tui.client import AsyncHubClient


@pytest.fixture
def home(monkeypatch):
    base = Path(tempfile.gettempdir()) if IS_WINDOWS else Path(os.path.realpath("/tmp"))
    h = base / f"ahtr-{os.getpid()}-{time.time_ns() % 10**6}"
    monkeypatch.setenv("CFHARNESS_HOME", str(h))
    monkeypatch.delenv("CFHARNESS_SOCKET", raising=False)
    monkeypatch.delenv(trace.ENV_REQ_ID, raising=False)
    p = HubPaths.resolve(h)
    p.ensure()
    p.config.write_text("guard:\n  git_probe: false\nhub:\n  max_concurrent_runs: 20\n")
    yield p
    shutil.rmtree(h, ignore_errors=True)


@pytest.fixture
def restore_logging():
    """setup_logging đổi trạng thái logging toàn cục → trả lại nguyên trạng (như tests/test_logging_l1.py)."""
    root, lg = logging.getLogger(), logging.getLogger("cfharness")
    saved = (list(root.handlers), root.level, list(lg.handlers), lg.level, getattr(lg, logs._FILE_MARK, None), logs._PROC)
    yield
    for h in list(root.handlers):
        if h not in saved[0]:
            root.removeHandler(h)
            h.close()
    for h in list(lg.handlers):
        if h not in saved[2]:
            lg.removeHandler(h)
            h.close()
    for h in saved[2]:
        if h not in lg.handlers:
            lg.addHandler(h)
    root.setLevel(saved[1])
    lg.setLevel(saved[3])
    if saved[4] is None and hasattr(lg, logs._FILE_MARK):
        delattr(lg, logs._FILE_MARK)
    logs._PROC = saved[5]


async def _start(p: HubPaths) -> tuple[Daemon, asyncio.base_events.Server]:
    d = Daemon(p, runtime=FakeRuntime(hold=True))
    d.hub.start()
    srv = await transport.start_server(d._handle_conn, p.socket, limit=16 * 1024 * 1024)
    os.chmod(p.socket, 0o600)
    return d, srv


async def _stop(d: Daemon, srv) -> None:
    srv.close()
    oscompat.JOBS.close_all()
    d.hub.runs._runtime.release()
    await d.hub.stop()


async def _raw(p: HubPaths, req: dict) -> dict:
    reader, writer = await transport.open_connection(str(p.socket), limit=1 << 20)
    try:
        writer.write((json.dumps(req) + "\n").encode())
        await writer.drain()
        return json.loads(await reader.readline())
    finally:
        writer.close()


# ---------------------------------------------------------------------------- trace.py thuần
def test_req_id_format_sortable_and_strict():
    a = trace.new_req_id(1_700_000_000_000)
    b = trace.new_req_id(1_700_000_000_001)
    assert trace.valid_req_id(a) and len(a) == 28 and a.startswith("r_") and a < b
    assert trace.req_id_ms(a) == 1_700_000_000_000
    for bad in (None, 1, "", "r_", a.lower(), a + "0", a[:-1], "r_" + "I" * 26, "r_" + "8" + "0" * 25, a[:-1] + "\n",
                "r_01J0000000000000000000000; DROP", "x" * 10_000, {"req_id": a}):
        assert not trace.valid_req_id(bad), bad
    assert trace.accept({"req_id": "bad"}) != "bad" and trace.valid_req_id(trace.accept({"req_id": "bad"}))
    assert trace.accept({"req_id": a}) == a and trace.valid_req_id(trace.accept(None))


def test_bind_outgoing_child_and_filter():
    assert trace.current_req_id() is None
    rid = trace.new_req_id()
    with trace.bind(rid, channel="web"):
        assert trace.outgoing({"x": 1}) == {"x": 1, "_trace": {"req_id": rid}}
        with trace.child("schedule") as c:
            assert c["parent_req_id"] == rid and c["req_id"] != rid and c["channel"] == "schedule"
        rec = logging.LogRecord("cfharness.x", logging.INFO, __file__, 1, "m", (), None)
        trace.TraceFilter().filter(rec)
        assert rec.req_id == rid and rec.channel == "web"
    assert trace.current_req_id() is None
    out = trace.outgoing(None)
    assert trace.valid_req_id(out["_trace"]["req_id"])
    with trace.bind("garbage") as c:  # sai định dạng → mã mới
        assert trace.valid_req_id(c["req_id"])


def test_traced_decorator_sync_and_async():
    seen: list = []

    @trace.traced("dispatcher")
    def f():
        seen.append(trace.current())

    @trace.traced("manager.tick")
    async def g():
        seen.append(trace.current())

    rid = trace.new_req_id()
    with trace.bind(rid):
        f()
        asyncio.run(g())
    assert seen[0]["parent_req_id"] == rid and seen[0]["channel"] == "dispatcher"
    assert seen[1]["parent_req_id"] == rid and seen[1]["req_id"] != seen[0]["req_id"]


def test_hook_with_trace_only_passes_safe_ids(monkeypatch):
    from cfharness.guard.hook import with_trace

    rid = trace.new_req_id()
    monkeypatch.setenv(trace.ENV_REQ_ID, rid)
    assert with_trace({})["_trace"] == {"req_id": rid}
    monkeypatch.setenv(trace.ENV_REQ_ID, "x\n" * 100)
    assert "_trace" not in with_trace({})


# ---------------------------------------------------------------------------- daemon thật
def test_one_web_request_same_req_id_everywhere(home, restore_logging):
    """Một lệnh từ client web (AsyncHubClient) → CÙNG req_id ở: phản hồi `_meta`, dòng audit, run, event của run, hook guard
    (guard.decision), dòng hub.log."""
    logs.setup_logging(home.logs_dir, logging.INFO)

    async def go():
        d, srv = await _start(home)
        try:
            d.hub.agents.create({"id": "a1", "name": "A1", "limits": {"max_concurrent_runs": 20}})
            cli = AsyncHubClient(home.socket, channel="web", cred="web")  # L3: token web riêng → kênh web do daemon chứng thực
            run = await cli.call("run.start", {"agent": "a1", "prompt": "hello"}, audit=True)
            rid = cli.last_req_id
            assert trace.valid_req_id(rid)
            for _ in range(200):
                ar = d.hub.runs.active.get(run["id"])
                if ar is not None and ar.handle is not None:
                    break
                await asyncio.sleep(0.01)
            spec = d.hub.runs._runtime.started[-1]
            assert spec.env_extra.get(trace.ENV_REQ_ID) == rid
            # hook guard THẬT với env của run (như claude spawn) → guard.check mang req_id
            env = {**os.environ, **{k: v for k, v in spec.env_extra.items() if k.startswith("CFHARNESS_")}}
            stdin = json.dumps({"tool_name": "Read", "tool_input": {"file_path": str(spec.cwd / "x.txt")}, "session_id": run["session_id"],
                                "cwd": str(spec.cwd)})
            cp = await asyncio.to_thread(subprocess.run, [sys.executable, "-I", "-m", "cfharness.guard.hook", "--run", run["id"],
                                                          "--socket", str(home.socket), "--timeout", "30"],
                                         input=stdin, capture_output=True, text=True, env=env, timeout=60)
            assert '"allow"' in cp.stdout, (cp.stdout, cp.stderr)
            return d, srv, run, rid
        except BaseException:
            await _stop(d, srv)
            raise

    async def main():
        d, srv, run, rid = await go()
        try:
            st = d.hub.store
            assert st.get_run(run["id"])["req_id"] == rid
            arow = st.conn.execute("SELECT req_id, channel FROM audit_log WHERE kind='run.start' ORDER BY id DESC LIMIT 1").fetchone()
            assert tuple(arow) == (rid, "web")
            evs = st.query_events(run_id=run["id"])
            types = {e.type for e in evs}
            assert {"run.queued", "run.started", "guard.decision"} <= types, types
            assert all(e.req_id == rid for e in evs), [(e.type, e.req_id) for e in evs if e.req_id != rid]
            # wire (TUI/web) cũng mang req_id
            assert evs[0].to_wire()["req_id"] == rid
        finally:
            await _stop(d, srv)
        lines = [json.loads(x) for x in (home.logs_dir / "hub.log").read_text(encoding="utf-8").splitlines() if x.strip()]
        acc = [x for x in lines if x.get("logger") == "cfharness.rpc" and x.get("method") == "run.start"]
        assert acc and acc[-1]["req_id"] == rid and acc[-1]["outcome"] == "ok" and acc[-1]["level"] == "info"
        assert acc[-1]["channel"] == "web" and acc[-1]["via"] == "socket:human" and isinstance(acc[-1]["duration_ms"], (int, float))
        dump = json.dumps(acc)
        assert "hello" not in dump  # không bao giờ params/kết quả

    asyncio.run(main())


def test_malformed_trace_replaced_and_error_carries_req_id(home):
    async def main():
        d, srv = await _start(home)
        try:
            bad = await _raw(home, {"jsonrpc": "2.0", "id": 1, "method": "hub.ping", "params": {"_trace": {"req_id": "r_" + "x" * 500}}})
            assert "result" in bad and trace.valid_req_id(bad["_meta"]["req_id"]) and "x" not in bad["_meta"]["req_id"]
            good = trace.new_req_id()
            ok = await _raw(home, {"jsonrpc": "2.0", "id": 2, "method": "hub.ping", "params": {"_trace": {"req_id": good}}})
            assert ok["_meta"] == {"req_id": good}
            nf = await _raw(home, {"jsonrpc": "2.0", "id": 3, "method": "no.such", "params": {}})
            assert "error" in nf and trace.valid_req_id(nf["_meta"]["req_id"])
            # client đồng bộ (CLI/MCP): lỗi mang req_id; req_id đang bind được dùng lại
            mine = trace.new_req_id()

            def call():
                with trace.bind(mine):
                    RpcClient(home.socket).call("agent.get", {"id": "nope"})

            with pytest.raises(RpcError) as ei:
                await asyncio.to_thread(call)
            assert ei.value.req_id == mine
            # handler không bao giờ thấy `_trace`
            seen: dict = {}
            d.methods["test.echo"] = lambda p: seen.update(p) or {}
            await _raw(home, {"jsonrpc": "2.0", "id": 4, "method": "test.echo", "params": {"a": 1, "_trace": {"req_id": good}}})
            assert seen == {"a": 1}
        finally:
            await _stop(d, srv)

    asyncio.run(main())


def test_cli_prints_req_id_on_error(home, capsys):
    from cfharness import cli

    async def main():
        d, srv = await _start(home)
        try:
            rc = await asyncio.to_thread(cli.main, ["agent", "show", "khong-co"])
            return rc
        finally:
            await _stop(d, srv)

    rc = asyncio.run(main())
    err = capsys.readouterr().err
    assert rc != 0 and "mã yêu cầu r_" in err


def test_access_log_levels_and_slow(home, caplog):
    home.config.write_text("guard:\n  git_probe: false\nlogs:\n  slow_rpc_ms: 50\n")

    async def main():
        d, srv = await _start(home)
        try:
            async def slow(p):
                await asyncio.sleep(0.12)
                return {}

            d.methods["test.slow"] = slow
            caplog.set_level(logging.DEBUG, logger="cfharness.rpc")
            caplog.clear()
            await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "hub.ping", "params": {}})
            await d._dispatch({"jsonrpc": "2.0", "id": 2, "method": "agent.get", "params": {"id": "nope"}})
            await d._dispatch({"jsonrpc": "2.0", "id": 3, "method": "agent.create", "params": {"config": {"id": "b1", "name": "B"}}})
            await d._dispatch({"jsonrpc": "2.0", "id": 4, "method": "test.slow", "params": {}})
            # agent / thiếu token gọi method chỉ-người qua socket → denied (WARNING)
            await _raw(home, {"jsonrpc": "2.0", "id": 5, "method": "secret.set", "params": {"name": "X", "value": "y"}})
            # run.wait chờ lâu theo thiết kế → không rpc.slow
            d.methods["run.wait"] = slow
            await d._dispatch({"jsonrpc": "2.0", "id": 6, "method": "run.wait", "params": {}})
        finally:
            await _stop(d, srv)

    asyncio.run(main())
    recs = {r.method: r for r in caplog.records if r.name == "cfharness.rpc" and hasattr(r, "method")}
    assert recs["hub.ping"].levelno == logging.DEBUG and recs["hub.ping"].outcome == "ok"
    assert recs["agent.get"].levelno == logging.INFO and recs["agent.get"].outcome == "error" and recs["agent.get"].code == -32004
    assert recs["agent.create"].levelno == logging.INFO and recs["agent.create"].outcome == "ok"
    assert recs["test.slow"].levelno == logging.WARNING and recs["test.slow"].event == "rpc.slow" and recs["test.slow"].duration_ms >= 50
    assert recs["secret.set"].levelno == logging.WARNING and recs["secret.set"].outcome == "denied"
    assert recs["run.wait"].event == "rpc" and recs["run.wait"].levelno == logging.DEBUG
    for r in recs.values():
        assert trace.valid_req_id(r.req_id)
        assert "y" != getattr(r, "value", None) and "nope" not in r.getMessage()


def test_internal_entry_points_start_child_req_id(home):
    """Lịch / call_as trong daemon: req_id mới có cha, run tạo từ đó mang mã con."""
    async def main():
        d, srv = await _start(home)
        try:
            d.hub.agents.create({"id": "a1", "name": "A1"})
            parent = trace.new_req_id()
            with trace.bind(parent):
                # call_as (chat, điều phối viên) trong lượt đang bind → cùng mã
                from cfharness.auth import Caller

                resp = await d.call_as(Caller(via="internal"), "hub.ping", {})
                assert resp["_meta"]["req_id"] == parent

                @trace.traced("schedule")
                def fire():
                    return d.hub.runs.submit("a1", "x", kind="schedule"), trace.current()

                run, ctx = fire()
            assert ctx["parent_req_id"] == parent and d.hub.store.get_run(run["id"])["req_id"] == ctx["req_id"] != parent
            # run tạo ngoài mọi lượt vẫn có mã riêng
            r2 = d.hub.runs.submit("a1", "y")
            assert trace.valid_req_id(d.hub.store.get_run(r2["id"])["req_id"])
        finally:
            await _stop(d, srv)

    asyncio.run(main())
