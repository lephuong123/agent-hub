"""Đo NFR-01..05/08 của M2 trên máy dev bằng daemon THẬT (tiến trình riêng) + FakeRuntime + TUI headless.

    uv run python -m agent_hub.tui.bench --home /tmp/x --agents 50 --concurrent 10 --duration 60

Không gọi `claude`. In JSON kết quả:
- NFR-01: độ trễ event→TUI (ts envelope do adapter gán khi đọc dòng stdout → lúc TUI áp vào bảng), p50/p95/p99.
- NFR-02: số run đồng thời duy trì được, số run xong/lỗi.
- NFR-03: nhịp vẽ bảng (khoảng cách giữa các lần vẽ, p95) với `--agents` agent trong danh sách.
- NFR-04: RSS/CPU trung bình của daemon (không tính tiến trình claude — ở đây là FakeRuntime trong daemon).
- NFR-05: đóng TUI giữa chừng → mọi run vẫn hoàn tất.
- NFR-08: một subscriber "treo" (không đọc socket) → bị ngắt `slow_consumer`? run có chậm lại không.
- CPU/RAM của tiến trình TUI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

import yaml


def _ps(pid: int) -> tuple[float, float]:
    """(rss_mb, cpu_seconds) của pid."""
    out = subprocess.run(["ps", "-o", "rss=,time=", "-p", str(pid)], capture_output=True, text=True).stdout.split()
    if len(out) < 2:
        return 0.0, 0.0
    rss_mb = int(out[0]) / 1024
    t = out[1]
    parts = t.replace("-", ":").split(":")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return rss_mb, secs


def _q(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 1) if xs else 0.0


async def main_async(a: argparse.Namespace) -> dict:
    home = Path(a.home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    os.environ["AGENT_HUB_HOME"] = str(home)
    from ..daemon import spawn_background
    from ..paths import HubPaths
    from ..rpc import RpcClient
    from .app import TopApp
    from .client import AsyncHubClient

    paths = HubPaths.resolve(str(home))
    paths.ensure()
    paths.config.write_text(
        yaml.safe_dump(
            {
                "runtime": {"kind": "fake", "fake": {"line_delay_s": a.line_delay}},
                "hub": {"max_concurrent_runs": a.concurrent},
                # hàng đợi subscriber nhỏ để subscriber "treo" bị ngắt trong thời gian đo (NFR-08)
                "bus": {"subscriber_queue": a.sub_queue},
            }
        )
    )
    pid = spawn_background(paths)
    c = AsyncHubClient(paths.socket, channel="cli")
    ids = []
    for i in range(a.agents):
        aid = f"b{i:02d}"
        ids.append(aid)
        try:
            await c.call("agent.create", {"config": {"id": aid, "name": f"bench {i}", "model": "haiku", "limits": {"max_concurrent_runs": 1}}})
        except Exception:
            pass
    workers = ids[: a.concurrent]
    res: dict = {"agents": a.agents, "concurrent_target": a.concurrent, "duration_s": a.duration, "line_delay_s": a.line_delay, "subscriber_queue": a.sub_queue}

    # subscriber "treo" (NFR-08): gửi subscribe rồi không bao giờ đọc
    stuck = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stuck.connect(str(paths.socket))
    stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    stuck.sendall(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "events.subscribe", "params": {"level": "detail"}}).encode() + b"\n")

    app = TopApp(paths.socket, refresh_s=1.0, firehose_backlog=0)
    submitted: list[str] = []
    max_running = 0
    running_samples: list[int] = []
    d_rss: list[float] = []
    d_cpu0 = _ps(pid)[1]
    t_wall0 = time.monotonic()
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    close_at = a.duration * 0.6  # NFR-05: đóng TUI ở 60% thời gian, run vẫn tiếp tục
    async with app.run_test(size=(170, 60), headless=True) as pilot:
        t0 = time.monotonic()
        while time.monotonic() - t0 < close_at:
            snap = app.model.rows
            busy = {aid for aid, r in snap.items() if r["current_run_id"]}
            for w in workers:
                if w not in busy:
                    try:
                        r = await c.call("run.start", {"agent": w, "prompt": f"bench run {len(submitted)}"})
                        submitted.append(r["id"])
                        busy.add(w)
                    except Exception:
                        pass
            st = await c.call("hub.status")
            max_running = max(max_running, st["running"])
            running_samples.append(st["running"])
            rss, _ = _ps(pid)
            d_rss.append(rss)
            await pilot.pause(0.5)
        lat = list(app.model.latencies_ms)
        rt = app.render_times
        gaps = [(rt[i + 1] - rt[i]) * 1000 for i in range(len(rt) - 1)]
        ru1 = resource.getrusage(resource.RUSAGE_SELF)
        tui_wall = time.monotonic() - t_wall0
        res["tui"] = {
            "events_seen": app.model.events_seen,
            "latency_ms": {"n": len(lat), "p50": _q(lat, 0.5), "p95": _q(lat, 0.95), "p99": _q(lat, 0.99), "max": round(max(lat), 1) if lat else 0},
            "render_gap_ms": {"n": len(gaps), "p50": _q(gaps, 0.5), "p95": _q(gaps, 0.95), "max": round(max(gaps), 1) if gaps else 0},
            "snapshot_rpc_ms": {"p50": _q(list(app.model.snapshot_ms), 0.5), "p95": _q(list(app.model.snapshot_ms), 0.95)},
            "cpu_pct_avg": round(((ru1.ru_utime + ru1.ru_stime) - (ru0.ru_utime + ru0.ru_stime)) / tui_wall * 100, 1),
            "maxrss_mb": round(ru1.ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024), 1),
            "rows": len(app.model.rows),
        }
    # TUI đã đóng — run vẫn phải hoàn tất (NFR-05)
    in_flight = []
    for rid in submitted:
        r = await c.call("run.get", {"id": rid})
        if r["status"] in ("queued", "running"):
            in_flight.append(rid)
    t1 = time.monotonic()
    while time.monotonic() - t1 < a.duration * 0.4:
        st = await c.call("hub.status")
        running_samples.append(st["running"])
        d_rss.append(_ps(pid)[0])
        await asyncio.sleep(0.5)
    deadline = time.monotonic() + 60
    finals = {}
    for rid in submitted:
        while True:
            r = await c.call("run.get", {"id": rid})
            if r["status"] not in ("queued", "running") or time.monotonic() > deadline:
                finals[rid] = r["status"]
                break
            await asyncio.sleep(0.2)
    wall = time.monotonic() - t_wall0
    d_cpu1 = _ps(pid)[1]
    subs_after = (await c.call("hub.status"))["subscribers"]
    outcomes: dict[str, int] = {}
    for s in finals.values():
        outcomes[s] = outcomes.get(s, 0) + 1
    res["runs"] = {"submitted": len(submitted), "outcomes": outcomes, "max_running": max_running,
                   "mean_running": round(statistics.mean(running_samples), 1) if running_samples else 0}
    res["nfr05_close_tui"] = {"in_flight_when_closed": len(in_flight), "completed_after": sum(1 for r in in_flight if finals.get(r) == "completed")}
    res["daemon"] = {"rss_mb_max": round(max(d_rss), 1), "rss_mb_mean": round(statistics.mean(d_rss), 1), "cpu_pct_avg": round((d_cpu1 - d_cpu0) / wall * 100, 1)}
    # NFR-08: subscriber treo có bị ngắt không
    stuck.settimeout(2.0)
    got = b""
    try:
        while b"events.end" not in got:
            chunk = stuck.recv(1 << 20)
            if not chunk:
                break
            got += chunk
    except (TimeoutError, socket.timeout):
        pass
    # subscriber treo bị bus gỡ (subscribers về 0 sau khi TUI đóng) và nhận events.end{slow_consumer} khi đọc lại
    res["nfr08_stuck_subscriber"] = {"bus_subscribers_after": subs_after, "slow_consumer_seen": b"slow_consumer" in got, "bytes_read": len(got)}
    stuck.close()
    RpcClient(paths.socket).call("hub.shutdown")
    return res


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--home", required=True)
    p.add_argument("--agents", type=int, default=50)
    p.add_argument("--concurrent", type=int, default=10)
    p.add_argument("--duration", type=float, default=60)
    p.add_argument("--line-delay", type=float, default=0.1)
    p.add_argument("--sub-queue", type=int, default=1000, help="bus.subscriber_queue của daemon đo")
    a = p.parse_args()
    print(json.dumps(asyncio.run(main_async(a)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
