"""RPC chỉ đọc chạy NGOÀI event loop (`Store.read` + `daemon.OFFLOAD_READS`) và số đo độ trễ event loop (`loop.lag`).

- Kết nối chỉ đọc (`mode=ro`) của luồng đọc thấy dữ liệu đã commit, không ghi được; `store.conn` trên luồng đọc = kết nối ấy.
- Mỗi method ∈ `OFFLOAD_READS` được bọc, chạy trên luồng đọc và trả ĐÚNG kết quả của đường cũ (gọi handler gốc trên loop).
- Đọc ngay sau ghi (cùng luồng RPC) thấy bản ghi vừa ghi.
- Trong lúc một lượt đọc chậm chạy ở luồng đọc, `hub.ping` vẫn trả < 50 ms (loop không bị chặn).
- `loop.lag`: mẫu vào `DaemonPerf` (bảng RPC + `perf.stats.loop`), WARNING khi vượt `perf.loop_lag_warn_ms`; cấu hình được kiểm.
- Daemon dừng → đóng luồng đọc + kết nối chỉ đọc.
Không gọi `claude`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cfharness import perf
from cfharness.config import validate_setting
from cfharness.daemon import OFFLOAD_READS, Daemon
from cfharness.events import iso, make_event
from cfharness.runtime.fake import FakeRuntime
from cfharness.store import Store
from cfharness.systems.service import REGISTRY as SYS_REGISTRY

# một lượt đọc CHẬM thật trong SQLite (sqlite3 nhả GIL trong lúc chạy câu lệnh) — ~0,3–1 s tuỳ máy
SLOW_SQL = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 3000000) SELECT COUNT(*) FROM c"


async def _rpc(d: Daemon, method: str, params: dict | None = None):
    r = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": dict(params or {})})
    assert "error" not in r, r
    return r["result"]


# ============================================================================ Store.read
async def test_reader_sees_committed_writes_and_cannot_write(tmp_path):
    s = Store(tmp_path / "hub.db")
    try:
        assert s.can_offload
        s.state_set("k", {"v": 1})
        assert await s.read(s.state_get, "k") == {"v": 1}
        s.state_set("k", {"v": 2})  # ghi tiếp — lượt đọc sau thấy ngay (WAL, autocommit)
        assert await s.read(s.state_get, "k") == {"v": 2}
        with s.tx():
            s.state_set("k", {"v": 3})
        assert await s.read(s.state_get, "k") == {"v": 3}

        def where() -> tuple[str, bool]:
            return threading.current_thread().name, s.conn is s._wconn

        name, is_writer = await s.read(where)
        assert name.startswith("cfh-db-read") and not is_writer
        assert s.conn is s._wconn  # luồng loop vẫn dùng kết nối ghi
        with pytest.raises(sqlite3.OperationalError, match="readonly|read-only|query_only"):
            await s.read(s.state_set, "k", {"v": 4})
        assert s.state_get("k") == {"v": 3}
    finally:
        s.close()
    assert s._read_pool is None and s._ro_conns == []
    with pytest.raises(RuntimeError):
        await s.read(lambda: 1)


async def test_reader_copies_contextvars(tmp_path):
    import contextvars

    cv: contextvars.ContextVar[str] = contextvars.ContextVar("t", default="-")
    s = Store(tmp_path / "hub.db")
    try:
        cv.set("caller-x")
        assert await s.read(cv.get) == "caller-x"
    finally:
        s.close()


# ============================================================================ RPC được chuyển
def _seed(d: Daemon) -> dict:
    h = d.hub
    s = h.store
    for a in ("w1", "w2"):
        h.agents.create({"id": a, "name": a, "model": "haiku"})
    now = datetime.now(timezone.utc)
    for i in range(6):
        q = iso(now - timedelta(hours=i + 1))
        s.insert_run({"id": f"r_{i}", "agent_id": "w1" if i % 2 else "w2", "kind": "adhoc" if i % 3 else "review",
                      "status": "completed", "outcome": ("timed_out", "completed", "crashed")[i % 3], "queued_at": q, "started_at": q,
                      "ended_at": iso(now - timedelta(minutes=30)), "cost_usd": 0.01 * i, "duration_ms": 1000 * i, "error": "boom" if i == 2 else None})
    for i in range(20):
        h.publish(make_event("tool.start" if i % 2 else "learning.applied", "w1", {"tool": "Read", "i": i}, run_id="r_1"))
    h.audit.record("agent.update", "w1", None, {"x": 1}, actor="user", channel="cli", principal="os:boss", outcome="ok")
    return {"agent": "w1", "run": "r_1"}


PARAMS = {
    "run.list": [{}, {"agent": "w1", "limit": 3}, {"status": "completed"}],
    "events.query": [{"run_id": "r_1"}, {"agent_id": "w1", "limit": 5, "order": "desc"}, {"agent_id": "w1", "types": ["tool.start"]}],
    "task.list": [{}],
    "learn.feed": [{}, {"agent": "w1", "days": 3}],
    "learn.proposals": [{}],
    "report.get": [{}, {"since": "7d"}],
    "report.hangs": [{}, {"since": "30d", "top": 3}],
    "report.kpi": [{}],
    "report.overhead": [{}],
    "report.cost": [{}, {"by": "agent", "since": "7d"}],
    "approval.stats": [{}, {"days": 365}],
    "audit.list": [{}, {"principal": "os:boss"}],
    "audit.activity": [{}],
    "system.list": [{}],
}


def test_offload_list_is_covered_and_read_only(hub_home):
    d = Daemon(hub_home, runtime=FakeRuntime())
    assert set(PARAMS) == set(OFFLOAD_READS)  # thêm method vào OFFLOAD_READS → thêm ca so kết quả ở đây
    for m in OFFLOAD_READS:
        assert getattr(d.methods[m], "offloaded", False), m
        assert m not in d._mutating, m
    assert not getattr(d.methods["hub.status"], "offloaded", False)  # trộn trạng thái RAM → ở lại loop


def _norm(res) -> str:
    """So kết quả: bỏ các mốc thời gian CẤP ĐẦU (`since`… tính từ giờ hiện tại — hai lượt gọi lệch vài ms); dữ liệu bên trong so nguyên."""

    def _is_ts(v) -> bool:
        try:
            datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return False
        return True

    if isinstance(res, dict):
        res = {k: v for k, v in res.items() if not (isinstance(v, str) and "T" in v and _is_ts(v))}
    return json.dumps(res, sort_keys=True, default=str)


@pytest.mark.parametrize("method", sorted(PARAMS))
async def test_offloaded_rpc_matches_inline(hub_home, method):
    d = Daemon(hub_home, runtime=FakeRuntime())
    try:
        _seed(d)
        d.hub.systems._lc = SimpleNamespace(state=lambda: {}, disabled=lambda: set(), jobs=lambda *a: [{"label": "com.app.job", "kind": "agent", "schedule": "hằng ngày", "program": ["x"]}])  # không gọi PowerShell/launchctl thật
        d.hub.store.state_set(SYS_REGISTRY, {"app": {"path": str(hub_home.home / "app"), "title": "App", "added_at": "2026-01-01T00:00:00Z"}})
        wrapped = d.methods[method]
        threads: list[str] = []
        orig = wrapped.__wrapped__

        def spy(p):
            threads.append(threading.current_thread().name)
            return orig(p)

        wrapped_spy = d._offloaded(method, spy)
        for p in PARAMS[method]:
            inline = orig(dict(p))
            off = await wrapped(dict(p))
            assert _norm(off) == _norm(inline), (method, p)
            await wrapped_spy(dict(p))
        assert threads and all(t.startswith("cfh-db-read") for t in threads)
    finally:
        d.hub.store.close()


async def test_read_after_write_same_flow(hub_home):
    d = Daemon(hub_home, runtime=FakeRuntime())
    try:
        d.hub.agents.create({"id": "w1", "name": "w1", "model": "haiku"})
        d.hub.store.insert_run({"id": "r_new", "agent_id": "w1", "kind": "adhoc", "status": "queued", "queued_at": iso(datetime.now(timezone.utc))})
        assert [r["id"] for r in await _rpc(d, "run.list", {})] == ["r_new"]
        d.hub.publish(make_event("tool.start", "w1", {"tool": "Read"}, run_id="r_new"))
        assert [e["type"] for e in await _rpc(d, "events.query", {"run_id": "r_new"})] == ["tool.start"]
        d.hub.audit.record("agent.update", "w1", None, {}, actor="user", channel="cli", principal="os:me", outcome="ok")
        assert any(r["principal"] == "os:me" for r in await _rpc(d, "audit.list", {"principal": "os:me"}))
    finally:
        d.hub.store.close()


async def test_errors_from_offloaded_handler_keep_rpc_shape(hub_home):
    d = Daemon(hub_home, runtime=FakeRuntime())
    try:
        r = await d._dispatch({"jsonrpc": "2.0", "id": 1, "method": "audit.list", "params": {"outcome": "maybe"}})
        assert r["error"]["code"] == -32602
        r = await d._dispatch({"jsonrpc": "2.0", "id": 2, "method": "run.list", "params": {"limit": "x"}})
        assert "error" in r and r["id"] == 2
    finally:
        d.hub.store.close()


async def test_loop_stays_responsive_during_slow_read(hub_home):
    d = Daemon(hub_home, runtime=FakeRuntime())
    try:
        def slow(p):
            assert d.hub.store.conn is not d.hub.store._wconn  # đang trên kết nối chỉ đọc
            t = time.perf_counter()
            n = d.hub.store.conn.execute(SLOW_SQL).fetchone()[0]
            return {"n": n, "ms": (time.perf_counter() - t) * 1000}

        d.methods["test.slow"] = d._offloaded("test.slow", slow)
        await d.hub.store.read(lambda: None)  # khởi động luồng đọc trước khi đo
        task = asyncio.create_task(d._dispatch({"jsonrpc": "2.0", "id": 9, "method": "test.slow", "params": {}}))
        await asyncio.sleep(0.05)  # lượt đọc chậm đang chạy ở luồng đọc
        pings = []
        for _ in range(5):
            t = time.perf_counter()
            await _rpc(d, "hub.ping")
            pings.append((time.perf_counter() - t) * 1000)
            await asyncio.sleep(0.01)
        assert not task.done(), "lượt đọc chậm phải còn chạy trong lúc ping"
        res = (await task)["result"]
        assert res["n"] == 3_000_000 and res["ms"] > 100
        assert max(pings) < 50, pings
    finally:
        d.hub.store.close()


# ============================================================================ loop.lag
async def test_loop_lag_metric_and_warning(hub_home, caplog):
    d = Daemon(hub_home, runtime=FakeRuntime())
    try:
        caplog.set_level(logging.WARNING, logger="cfharness.daemon")
        lag = asyncio.create_task(d._loop_lag(interval=0.05))
        await asyncio.sleep(0.12)
        time.sleep(0.35)  # chặn loop bằng code đồng bộ
        await asyncio.sleep(0.15)
        d.stopping.set()
        await asyncio.wait_for(lag, 2)
        rows = {r["key"]: r for r in d.perf.rpc.snapshot()}
        assert perf.LOOP_LAG_KEY in rows and rows[perf.LOOP_LAG_KEY]["max"] >= 250
        assert d.perf.loop_lag_max >= 250 and d.perf.loop_lag_last is not None
        assert any(getattr(r, "event", None) == "loop.lag" for r in caplog.records)
        st = await d.methods["perf.stats"]({})
        assert st["loop"]["count"] >= 2 and st["loop"]["max_since_start"] >= 250 and st["config"]["loop_lag_warn_ms"] == 250
        assert "event loop" in perf.format_stats(st)
    finally:
        d.hub.store.close()


async def test_loop_lag_warning_threshold_configurable(hub_home, caplog):
    d = Daemon(hub_home, runtime=FakeRuntime())
    try:
        d.hub.config.set("perf.loop_lag_warn_ms", 0)  # 0 = không cảnh báo, vẫn đo
        caplog.set_level(logging.WARNING, logger="cfharness.daemon")
        lag = asyncio.create_task(d._loop_lag(interval=0.05))
        await asyncio.sleep(0.08)
        time.sleep(0.3)
        await asyncio.sleep(0.1)
        d.stopping.set()
        await asyncio.wait_for(lag, 2)
        assert d.perf.loop_lag_max >= 250
        assert not any(getattr(r, "event", None) == "loop.lag" for r in caplog.records)
    finally:
        d.hub.store.close()


def test_validate_loop_lag_setting():
    validate_setting("perf.loop_lag_warn_ms", 500)
    for bad in (-1, "x", True):
        with pytest.raises(ValueError):
            validate_setting("perf.loop_lag_warn_ms", bad)


# ============================================================================ tắt daemon
async def test_daemon_shutdown_closes_readers(hub_home):
    d = Daemon(hub_home, runtime=FakeRuntime())
    task = asyncio.create_task(d.serve())
    for _ in range(300):
        if hub_home.socket.exists():
            break
        await asyncio.sleep(0.02)
    await _rpc(d, "run.list", {})
    store = d.hub.store
    assert store._read_pool is not None and store._ro_conns
    conns = list(store._ro_conns)
    threads = list(store._read_pool._threads)
    d.stopping.set()
    await asyncio.wait_for(task, 20)
    assert store._read_pool is None and store._ro_conns == []
    with pytest.raises(sqlite3.ProgrammingError):
        conns[0].execute("SELECT 1")
    assert threads and not any(t.is_alive() for t in threads)
