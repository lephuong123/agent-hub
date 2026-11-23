"""Giảm chi phí RPC mỗi lần chuyển trang (perf): RPC độc lập chạy đồng thời (`gather_ordered`), cache số trên menu theo phiên (TTL +
POST làm mới), cache chuỗi tổ tiên của daemon theo (pid, thời điểm khởi động), hồ kết nối đọc của `AsyncHubClient`.
Daemon IN-PROCESS + FakeRuntime — không gọi `claude`.
"""

from __future__ import annotations

import asyncio

import httpx2 as httpx
import pytest

from cfharness import auth, perf
from cfharness.rpc import DaemonUnavailable, RpcError
from cfharness.tui.client import AsyncHubClient
from cfharness.web import app as web_app


@pytest.fixture
async def env(hub_home):
    from test_web import ISOLATED, _start, _stop

    from cfharness.runtime import FakeRuntime

    e = await _start(hub_home, FakeRuntime(ISOLATED))
    yield e
    await _stop(e)


def _count_calls(env, monkeypatch, fail: dict[str, Exception] | None = None) -> dict[str, int]:
    """Đếm lời gọi RPC của web theo method; `fail`: method → lỗi ném ra (giả daemon lỗi)."""
    client = env.app.state.web.client
    orig = client.call
    seen: dict[str, int] = {}

    async def call(method, params=None, **kw):
        seen[method] = seen.get(method, 0) + 1
        if fail and method in fail:
            raise fail[method]
        return await orig(method, params, **kw)

    monkeypatch.setattr(client, "call", call)
    return seen


# ============================================================================ gather_ordered
async def test_gather_ordered_runs_concurrently_and_raises_first_error_in_arg_order():
    order: list[str] = []

    async def slow_fail():
        await asyncio.sleep(0.05)
        order.append("slow")
        raise ValueError("lỗi-đầu")

    async def fast_fail():
        order.append("fast")
        raise KeyError("lỗi-sau")

    async def ok():
        await asyncio.sleep(0.01)
        order.append("ok")
        return 1

    with pytest.raises(ValueError):  # lỗi của đối số ĐẦU (như bản tuần tự), dù lỗi kia tới trước
        await web_app.gather_ordered(slow_fail(), fast_fail(), ok())
    assert set(order) == {"slow", "fast", "ok"}  # chờ hết, không bỏ dở lời gọi khác
    assert await web_app.gather_ordered(ok(), ok()) == [1, 1]


async def test_perf_accounting_counts_every_rpc_under_gather():
    t = perf.ReqTiming()
    tok = perf.CURRENT.set(t)
    try:
        async def one(m):
            await asyncio.sleep(0)
            perf.note_rpc(m, 2.0)

        await web_app.gather_ordered(one("a.x"), one("b.y"), one("a.x"))
    finally:
        perf.CURRENT.reset(tok)
    assert t.rpc_n == 3 and t.by_method() == {"a.x": [2, 4.0], "b.y": [1, 2.0]}


# ============================================================================ badge: lỗi một RPC không làm hỏng trang
@pytest.mark.parametrize("method,exc,down", [
    ("question.list", RpcError(-32601, "không có method"), False),  # daemon cũ: 0 câu hỏi
    ("hub.frozen", RpcError(-32601, "không có method"), False),  # banner chuyển nhà bỏ qua
    ("estop.get", DaemonUnavailable("dừng"), True),  # → badge "down", trang vẫn hiện
    ("approval.list", OSError("hỏng"), True),
])
async def test_page_renders_when_one_badge_rpc_fails(env, monkeypatch, method, exc, down):
    await env.login()
    seen = _count_calls(env, monkeypatch, fail={method: exc})
    r = await env.client.get("/runs")
    assert r.status_code == 200 and seen.get(method) == 1
    for m in ("approval.list", "estop.get", "question.list", "hub.frozen"):
        assert seen.get(m, 0) >= 1  # cả bốn đều được hỏi (đồng thời) — không dừng ở lời gọi lỗi
    sess = next(iter(env.auth.sessions.values()))
    assert (web_app.NAV_BADGE_KEY in sess.extra) is (not down)  # "down" không bao giờ vào cache


async def test_manager_page_still_renders_with_batched_calls(env):
    await env.login()
    r = await env.client.get("/manager")
    assert r.status_code == 200
    r = await env.client.get("/manager?w=khong-co-luong")
    assert r.status_code == 200


# ============================================================================ cache số trên menu
async def test_nav_badge_cached_per_session_with_ttl_and_post_invalidation(env, monkeypatch):
    await env.login()
    seen = _count_calls(env, monkeypatch)
    assert (await env.client.get("/runs")).status_code == 200
    assert (await env.client.get("/agents")).status_code == 200
    assert seen.get("approval.list", 0) == 1 and seen.get("estop.get", 0) == 1  # chuyển tab trong TTL: không hỏi lại

    # phiên KHÁC không dùng chung cache
    other = httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url=str(env.client.base_url))
    try:
        r = await other.get("/login?t=" + env.auth.mint())
        assert r.status_code == 303
        assert (await other.get("/runs")).status_code == 200
        assert seen["estop.get"] == 2
    finally:
        await other.aclose()

    # hết TTL → hỏi lại
    sid = env.client.cookies.get(env.cookie)
    sess = env.auth.sessions[sid]
    t, gen, data = sess.extra[web_app.NAV_BADGE_KEY]
    sess.extra[web_app.NAV_BADGE_KEY] = (t - web_app.NAV_BADGE_TTL_S - 1, gen, data)
    await env.client.get("/runs")
    assert seen["estop.get"] == 3

    # POST của người (kể cả bị từ chối) → bỏ cache mọi phiên
    gen0 = env.app.state.web.badge_gen
    await env.post("/do/khong-co-hanh-dong", {})
    assert env.app.state.web.badge_gen > gen0
    await env.client.get("/runs")
    assert seen["estop.get"] >= 4


# ============================================================================ cache chuỗi tổ tiên (daemon, quyết định HUMAN_ONLY)
def test_cached_ancestors_keyed_by_pid_and_start_time():
    calls: list[int] = []
    starts = {100: 1.0, 200: 1.0}

    def compute(pid):
        calls.append(pid)
        return [pid, 1]

    def start_of(pid):
        return starts.get(pid)

    assert auth.cached_ancestors(100, now=0.0, start_of=start_of, compute=compute) == [100, 1]
    assert auth.cached_ancestors(100, now=1.0, start_of=start_of, compute=compute) == [100, 1]
    assert calls == [100]  # cùng pid + cùng thời điểm khởi động, trong TTL → dùng lại
    auth.cached_ancestors(200, now=1.0, start_of=start_of, compute=compute)
    assert calls == [100, 200]  # pid khác → không dùng lại
    starts[100] = 2.0  # pid 100 bị tiến trình KHÁC tái dùng (khởi động lúc khác) → không thừa hưởng chuỗi cũ
    auth.cached_ancestors(100, now=1.5, start_of=start_of, compute=compute)
    assert calls == [100, 200, 100]
    auth.cached_ancestors(100, now=1.5 + auth.ANCESTOR_TTL_S + 0.1, start_of=start_of, compute=compute)
    assert calls[-1] == 100 and len(calls) == 4  # hết TTL → tính lại


def test_cached_ancestors_never_caches_unknown_or_changing_start():
    calls: list[int] = []

    def compute(pid):
        calls.append(pid)
        return [pid]

    for _ in range(2):  # không đọc được thời điểm khởi động → không cache
        auth.cached_ancestors(300, now=0.0, start_of=lambda p: None, compute=compute)
    assert calls == [300, 300] and not auth._ANCESTOR_CACHE

    seq = iter([5.0, 6.0])  # tiến trình đổi trong lúc tính → không lưu
    auth.cached_ancestors(400, now=0.0, start_of=lambda p: next(seq), compute=compute)
    assert not any(k[0] == 400 for k in auth._ANCESTOR_CACHE)


def test_cached_ancestors_bounded(monkeypatch):
    monkeypatch.setattr(auth, "ANCESTOR_MAX", 4)
    for pid in range(10, 30):
        auth.cached_ancestors(pid, now=0.0, start_of=lambda p: 1.0, compute=lambda p: [p])
    assert len(auth._ANCESTOR_CACHE) <= 4


def test_resolver_still_detects_run_descendant_with_cache(monkeypatch):
    """Cache chỉ giữ chuỗi pid; pid run đang sống vẫn đọc MỚI mỗi lần → tiến trình con của run luôn bị nhận là agent."""

    class _Runs:
        active: dict = {}

    class _Hub:
        runs = _Runs()

    monkeypatch.setattr(auth, "proc_start_time", lambda pid: 42.0)
    monkeypatch.setattr(auth, "ancestors", lambda pid: [pid, 777])
    monkeypatch.setattr(auth, "proc_environ", lambda pid: {})
    monkeypatch.setattr(auth.CallerResolver, "_recent_runs", lambda self: {})
    r = auth.CallerResolver(_Hub())  # type: ignore[arg-type]
    c1 = r.resolve(555, None, "tok")
    assert c1.run_id is None
    monkeypatch.setattr(r, "_run_pids", lambda: {777: ("r_live", "ag")})  # run 777 bắt đầu được theo dõi
    c2 = r.resolve(555, None, "tok")
    assert c2.run_id == "r_live" and c2.is_agent


# ============================================================================ hồ kết nối AsyncHubClient
async def test_client_reuses_connection_for_reads_not_for_writes(env):
    c = AsyncHubClient(env.paths.socket, channel="web", cred="web")
    try:
        await c.call("hub.status")
        await c.call("hub.status")
        await c.call("estop.get")
        assert len(c._idle) == 1 and len(env.d._rr_conns) == 1  # ba lời gọi đọc, một kết nối
        await asyncio.gather(c.call("hub.status"), c.call("hub.status"), c.call("hub.status"))
        assert 1 <= len(c._idle) <= 3  # gọi đồng thời → mỗi lời gọi một kết nối riêng, xong thì vào hồ
        n = len(c._idle)
        await c.call("config.set", {"key": "perf.slow_ms", "value": 1600}, audit=True)
        assert len(c._idle) == n  # lời gọi ghi: kết nối riêng, đóng ngay
    finally:
        c.close_idle()


def _daemon_drops_connections(env) -> None:
    for w in list(env.d._rr_conns):
        w.transport.abort()


async def test_client_retries_read_once_when_pooled_connection_was_closed(env):
    c = AsyncHubClient(env.paths.socket, channel="web", cred="web")
    try:
        await c.call("hub.status")
        assert len(c._idle) == 1
        reader, writer, _, _ = c._idle[0]
        _daemon_drops_connections(env)  # daemon đóng kết nối (dừng/khởi động lại) — client chưa kịp thấy EOF (không có await xen giữa)
        assert not reader.at_eof() and not writer.is_closing()
        assert (await c.call("hub.status"))["epoch"]
        assert len(c._idle) == 1 and c._idle[0][1] is not writer
    finally:
        c.close_idle()


async def test_client_never_resends_write_on_closed_pooled_connection(env, monkeypatch):
    from cfharness.tui import client as client_mod

    c = AsyncHubClient(env.paths.socket, channel="web", cred="web")
    sent: list[str] = []
    orig_open = c._open

    async def counting_open():
        sent.append("open")
        return await orig_open()

    monkeypatch.setattr(c, "_open", counting_open)
    try:
        await c.call("hub.status")
        assert sent == ["open"] and len(c._idle) == 1
        assert client_mod._is_mutating("config.set")
        _daemon_drops_connections(env)
        with pytest.raises(RpcError):  # ghi: không thử lại trên kết nối mới (không biết daemon đã xử lý chưa)
            await c.call("config.set", {"key": "perf.slow_ms", "value": 1700})
        assert sent == ["open"]
    finally:
        c.close_idle()


async def test_daemon_stop_closes_pooled_connections(hub_home):
    from test_web import ISOLATED, _start

    from cfharness.runtime import FakeRuntime

    e = await _start(hub_home, FakeRuntime(ISOLATED))
    c = AsyncHubClient(e.paths.socket, channel="web", cred="web")
    await c.call("hub.status")
    assert len(c._idle) == 1
    await e.client.aclose()
    e.d.stopping.set()
    await asyncio.wait_for(e.task, 20)
    await asyncio.sleep(0.05)
    with pytest.raises(DaemonUnavailable):  # không gửi vào daemon đã dừng qua kết nối cũ
        await c.call("hub.status")
