"""Đo hiệu năng (perf.py, web/perf_pages.py, docs/dev-notes/perf-monitor.md): tổng hợp cuộn (phân vị, trần bộ nhớ), middleware đo request +
header Server-Timing + log `http.slow`, beacon trình duyệt (xác thực/CSRF/khoá phiên/trần kích thước/kiểm mẫu), trang /perf + /perf.json,
RPC `perf.stats`/`perf.report` (HUMAN_ONLY), `_stream(stamp=True)` gắn `ets`. Không gọi `claude`.
"""

from __future__ import annotations

import json
import logging
import random

import pytest

from cfharness import auth, perf
from cfharness.perf import ReqTiming, Rolling, percentile, server_timing


# ============================================================================ Rolling
def test_rolling_percentiles_exact_when_under_reservoir():
    r = Rolling(reservoir=1000)
    for v in range(1, 101):
        r.add("k", float(v), now=1000.0)
    (row,) = r.snapshot(window_s=600, now=1000.0)
    assert row["count"] == 100 and row["max"] == 100.0 and row["avg"] == 50.5
    assert row["p50"] == 50.0 and row["p95"] == 95.0


def test_rolling_reservoir_bounded_and_approximate():
    r = Rolling(reservoir=32, rng=random.Random(1))
    vals = [random.Random(2).uniform(0, 1000) for _ in range(5000)]
    for v in vals:
        r.add("k", v, now=50.0)
    b = r._data["k"][-1]
    assert len(b[4]) == 32 and b[1] == 5000  # mẫu bị chặn, count đủ
    (row,) = r.snapshot(now=50.0)
    assert row["count"] == 5000 and abs(row["p50"] - percentile(vals, 0.5)) < 200 and row["max"] == pytest.approx(max(vals), rel=1e-4)


def test_rolling_window_and_bucket_expiry():
    r = Rolling(bucket_s=60, keep_s=600)
    r.add("old", 900.0, now=0.0)
    r.add("new", 10.0, now=590.0)
    rows = {x["key"]: x for x in r.snapshot(window_s=120, now=600.0)}
    assert set(rows) == {"new"}  # "old" ngoài cửa sổ 2 phút
    for i in range(30):  # deque maxlen = keep_s / bucket_s thùng
        r.add("new", 1.0, now=600.0 + i * 60)
    assert len(r._data["new"]) == 10


def test_rolling_max_keys_overflow_and_prune():
    r = Rolling(max_keys=3, keep_s=120, bucket_s=60)
    for k in ("a", "b", "c", "d", "e"):
        r.add(k, 1.0, now=10.0)
    assert set(r._data) == {"a", "b", "c", perf.OTHER_KEY} or len(r._data) <= 4
    assert r.dropped_keys >= 1 and len(r._data) <= 4
    r.add("z", 5.0, now=10_000.0)  # khoá cũ hết hạn → được dọn, khoá mới có chỗ
    assert "z" in r._data and "a" not in r._data


def test_rolling_rejects_bad_values_and_sorts_by_p95():
    r = Rolling()
    for bad in (-1.0, float("nan"), "x"):
        r.add("k", bad)  # type: ignore[arg-type]
    assert len(r) == 0
    r.add("fast", 5.0)
    r.add("slow", 500.0)
    assert [x["key"] for x in r.snapshot()] == ["slow", "fast"]


def test_server_timing_header_format():
    t = ReqTiming()
    tok = perf.CURRENT.set(t)
    try:
        perf.note_rpc("approval.list", 12.0)
        perf.note_rpc("approval.list", 8.0)
        perf.note_rpc("estop.get", 1.5)
        perf.note_render(3.25)
    finally:
        perf.CURRENT.reset(tok)
    perf.note_rpc("ngoài.request", 1.0)  # không có request đang đo → no-op
    h = server_timing(t, 30.0)
    assert h.startswith("app;dur=30.0, rpc;dur=21.5;desc=\"3 RPC\", tpl;dur=3.2")
    assert 'rpc-approval-list;dur=20.0;desc="x2"' in h and "rpc-estop-get" in h
    assert t.by_method() == {"approval.list": [2, 20.0], "estop.get": [1, 1.5]}


# ============================================================================ web
@pytest.fixture
async def env(hub_home):
    from test_web import ISOLATED, _start, _stop

    from cfharness.runtime import FakeRuntime

    e = await _start(hub_home, FakeRuntime(ISOLATED))
    yield e
    await _stop(e)


async def test_middleware_records_route_template_and_server_timing(env):
    await env.login()
    r = await env.client.get("/runs")
    assert r.status_code == 200
    st = r.headers["server-timing"]
    assert st.startswith("app;dur=") and "rpc;dur=" in st and "tpl;dur=" in st and "rpc-approval-list" in st
    r = await env.client.get("/runs/r_doesnotexist0")
    wp = env.app.state.web.perf
    keys = {x["key"] for x in wp.backend.snapshot()}
    assert "GET /runs" in keys and "GET (404)" in keys  # run không tồn tại → 404 gom chung
    from cfharness.web.perf_pages import route_template

    assert route_template(env.app, "/runs/r_abc") == "/runs/{id}" and route_template(env.app, "/khong-co") == perf.OTHER_KEY
    assert not any("r_doesnotexist0" in k for k in keys)  # không bao giờ đường dẫn thô có id
    assert any(x["key"] == "approval.list" for x in wp.web_rpc.snapshot())
    # static + beacon không tính vào tổng hợp, nhưng vẫn có header
    r = await env.client.get("/static/app.js")
    assert "server-timing" in r.headers
    assert not any(k.startswith("GET /static") for k in {x["key"] for x in wp.backend.snapshot()})


async def _no_push(env) -> None:
    """Chờ lượt đẩy `perf.report` (nếu có) xong rồi chặn lượt sau — phản hồi mang cấu hình daemon sẽ ghi đè cờ đặt tay trong test."""
    import asyncio

    for _ in range(100):
        if not env.app.state.web.perf.pushing:
            break
        await asyncio.sleep(0.02)
    env.app.state.web.perf.last_push = float("inf")


async def test_slow_request_logged_with_breakdown(env, caplog):
    await env.login()
    await _no_push(env)
    env.app.state.web.perf.slow_ms = 0.001
    with caplog.at_level(logging.WARNING, logger="cfharness.web.perf"):
        await env.client.get("/runs")
    recs = [x for x in caplog.records if getattr(x, "event", None) == "http.slow"]
    assert recs and recs[0].route == "/runs" and recs[0].status == 200 and recs[0].rpc_n >= 1
    assert "render_ms" in recs[0].__dict__ and "duration_ms" in recs[0].__dict__
    caplog.clear()
    env.app.state.web.perf.slow_ms = 0  # 0 = tắt
    with caplog.at_level(logging.WARNING, logger="cfharness.web.perf"):
        await env.client.get("/runs")
    assert not [x for x in caplog.records if getattr(x, "event", None) == "http.slow"]


async def test_disabled_skips_measurement(env):
    await env.login()
    await _no_push(env)
    env.app.state.web.perf.enabled = False
    r = await env.client.get("/runs")
    assert "server-timing" not in r.headers and not any(x["key"] == "GET /runs" for x in env.app.state.web.perf.backend.snapshot())
    assert (await _beacon(env, [{"m": "htmx", "r": "/runs", "d": 1}])).status_code == 204 and not env.app.state.web.perf.frontend.snapshot()


def _beacon(env, samples, **over):
    body = {"csrf": env.csrf, "k": env.key, "s": samples, **over}
    return env.client.post("/perf/beacon", content=json.dumps(body).encode(), headers={"content-type": "text/plain"})


async def test_beacon_accepts_valid_and_maps_routes(env):
    await env.login()
    r = await _beacon(env, [{"m": "nav.ttfb", "r": "/runs", "d": 120.5}, {"m": "htmx", "r": "/partials/run/r_abc123?x=1", "d": 40},
                            {"m": "switch.ws", "r": "/manager", "d": 900}, {"m": "sse.lag", "r": "/sse/manager/v2", "d": 15},
                            {"m": "evil", "r": "/runs", "d": 1}, {"m": "htmx", "r": "http://x/y", "d": 1}, {"m": "htmx", "r": "/runs", "d": -5},
                            {"m": "htmx", "r": "/runs", "d": True}, {"m": "htmx", "r": "/runs", "d": 1e12}, "rác"])
    assert r.status_code == 204
    keys = {x["key"]: x for x in env.app.state.web.perf.frontend.snapshot()}
    assert set(keys) == {"nav.ttfb /runs", "htmx /partials/run/{id}", "switch.ws /manager", "sse.lag /sse/manager/v2"}
    assert keys["nav.ttfb /runs"]["max"] == 120.5


async def test_beacon_auth_csrf_key_origin_and_limits(env):
    from cfharness.web.perf_pages import MAX_BEACON_BYTES, MAX_SAMPLES

    r = await env.client.post("/perf/beacon", content=b"{}")
    assert r.status_code == 401  # chưa đăng nhập
    await env.login()
    sess = next(iter(env.auth.sessions.values()))
    last = sess.last
    assert (await _beacon(env, [], csrf="sai")).status_code == 403
    assert (await _beacon(env, [], k="sai")).status_code == 403
    assert (await _beacon(env, [], k=None)).status_code == 403
    r = await env.client.post("/perf/beacon", content=json.dumps({"csrf": env.csrf, "k": env.key, "s": []}).encode(),
                              headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    assert (await env.client.post("/perf/beacon", content=b"not json")).status_code == 400
    assert (await env.client.post("/perf/beacon", content=b"[1]")).status_code == 400
    assert (await _beacon(env, "x")).status_code == 400
    assert (await _beacon(env, [{"m": "htmx", "r": "/", "d": 1}] * (MAX_SAMPLES + 1))).status_code == 413
    big = json.dumps({"csrf": env.csrf, "k": env.key, "s": [], "pad": "x" * (MAX_BEACON_BYTES + 10)}).encode()
    assert (await env.client.post("/perf/beacon", content=big)).status_code == 413
    assert (await _beacon(env, [])).status_code == 204
    assert sess.last == last  # beacon không gia hạn phiên (như polling/SSE)


async def test_perf_page_and_json(env):
    await env.login()
    await env.client.get("/runs")
    await _beacon(env, [{"m": "nav.load", "r": "/runs", "d": 333}])
    r = await env.client.get("/perf")
    assert r.status_code == 200 and "Hiệu năng" in r.text and "/runs" in r.text and 'href="/perf"' in r.text
    j = (await env.client.get("/perf.json?w=5")).json()
    assert j["window_min"] == 5.0 and j["daemon_error"] is None
    assert any(x["key"] == "nav.load /runs" for x in j["frontend"]) and any(x["key"] == "GET /runs" for x in j["backend"])
    assert any(x["key"] == "approval.list" for x in j["daemon_rpc"])  # daemon đo từng RPC (access log)
    env.client.cookies.clear()
    assert (await env.client.get("/perf.json")).status_code in (303, 401)


async def test_push_snapshot_to_daemon_and_stats_rpc(env):
    from cfharness.web.perf_pages import push_snapshot

    await env.login()
    await env.client.get("/runs")
    env.hub.config.set("perf.slow_ms", 777)
    await push_snapshot(env.app.state.web)
    wp = env.app.state.web.perf
    assert wp.slow_ms == 777.0 and wp.pushing is False  # cấu hình đi ngược về web qua phản hồi perf.report
    st = await env.app.state.web.client.call("perf.stats", {"window_min": 10})
    assert st["window_min"] == 10.0 and st["web"]["at"] and any(x["key"] == "GET /runs" for x in st["web"]["backend"])
    assert any(x["key"] == "approval.list" for x in st["rpc"]) and st["config"]["slow_ms"] == 777.0
    from cfharness.perf import format_stats

    assert "RPC daemon" in format_stats(st) and "GET /runs" in format_stats(st)


def test_perf_rpcs_human_only_and_cli_blocked():
    from cfharness.guard.classifier import HUB_HUMAN_ONLY_CLI

    assert {"perf.stats", "perf.report"} <= auth.HUMAN_ONLY and not ({"perf.stats", "perf.report"} & set(auth.MANAGER_ALLOWED))
    assert HUB_HUMAN_ONLY_CLI["perf"] is None


def test_report_rows_are_sanitized():
    rows = perf._clean_rows([{"key": "k" * 500, "count": 3, "p95": float("inf"), "max": "x"}, {"nokey": 1}, "rác"])
    assert rows == [{"key": "k" * 200, "count": 3, "avg": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}]


async def test_stream_stamp_adds_ets_only_when_asked(env):
    import asyncio

    from starlette.requests import Request

    from cfharness.events import make_event
    from cfharness.web.app import _stream

    await env.login()
    st = env.hub.status()
    req = Request({"type": "http", "app": env.app, "path": "/", "headers": [], "query_string": b""})
    params = {"since_seq": st["last_seq"], "epoch": st["epoch"], "level": "summary"}

    async def collect(stamp: bool) -> list[dict]:
        got: list[dict] = []

        async def reader() -> None:
            async for chunk in _stream(req, params, lambda ev: [{"t": "ev", "type": ev.get("type")}], stamp=stamp):
                if chunk.startswith("data: "):
                    got.append(json.loads(chunk[6:]))
                    if any(m.get("t") == "ev" for m in got):
                        return

        task = asyncio.create_task(reader())
        await asyncio.sleep(0.2)
        env.hub.publish(make_event("run.started", "ag0", {}, run_id="r_x"))
        await asyncio.wait_for(task, 10)
        return got

    ev = next(m for m in await collect(True) if m["t"] == "ev")
    assert isinstance(ev.get("ets"), str) and ev["ets"][:2] == "20"
    params["since_seq"] = env.hub.status()["last_seq"]
    ev = next(m for m in await collect(False) if m["t"] == "ev")
    assert "ets" not in ev


async def test_perf_page_hides_admin_traces_for_non_admin(env):
    await env.login()
    assert (await env.client.get("/admin/overview")).status_code == 404
    wp = env.app.state.web.perf
    assert any(x["key"] == "GET (404)" for x in wp.backend.snapshot())
    assert not any("/admin" in x["key"] for x in wp.backend.snapshot())
    await _beacon(env, [{"m": "nav.ttfb", "r": "/admin/users", "d": 5}])
    wp.backend.add("GET /admin/users", 1.0)
    j = (await env.client.get("/perf.json")).json()
    keys = [r["key"] for k in ("backend", "frontend", "web_rpc", "daemon_rpc") for r in j[k]]
    assert keys and not any("/admin" in k or k.startswith("admin.") for k in keys)
