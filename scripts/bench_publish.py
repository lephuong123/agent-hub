"""Microbench đường publish của daemon (plan-api-caching PR0).

Dựng Hub in-process (FakeRuntime, HUB_HOME tạm), phát N `message.delta` qua `hub.publish` rồi in:
- tổng thời gian và µs/event,
- thời gian theo từng listener của bus (biết listener nào tốn),
- µs cho một lần `config.get` có khoá.

    uv run python scripts/bench_publish.py [N]
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path


def main(n: int) -> None:
    home = Path(tempfile.mkdtemp(prefix="cfh-bench-"))
    os.environ["CFHARNESS_HOME"] = str(home)
    os.environ.pop("CFHARNESS_SOCKET", None)
    from cfharness.events import HubEvent
    from cfharness.hub import Hub
    from cfharness.paths import HubPaths
    from cfharness.runtime import FakeRuntime

    paths = HubPaths.resolve()
    hub = Hub(paths, runtime=FakeRuntime())

    cost: dict[str, float] = defaultdict(float)
    listeners = hub.bus._listeners
    for i, fn in enumerate(list(listeners)):
        name = getattr(fn, "__qualname__", None) or type(fn).__name__

        def wrap(ev, _fn=fn, _name=f"{i:02d} {name}"):
            t = time.perf_counter()
            try:
                return _fn(ev)
            finally:
                cost[_name] += time.perf_counter() - t

        listeners[i] = wrap

    t0 = time.perf_counter()
    for k in range(n):
        hub.publish(HubEvent(type="message.delta", level="detail", source="cli", agent_id="a-bench", run_id="r-bench",
                             payload={"text": f"chunk {k} "}))
    total = time.perf_counter() - t0

    print(f"{n} message.delta: {total * 1000:.1f} ms tổng, {total / n * 1e6:.1f} µs/event")
    for name, s in sorted(cost.items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {s / n * 1e6:9.1f} µs/event  {name}")

    m = 2000
    t = time.perf_counter()
    for _ in range(m):
        hub.config.get("runtime.claude.version_cache_s")
    print(f"config.get (vô hướng): {(time.perf_counter() - t) / m * 1e6:.1f} µs")
    t = time.perf_counter()
    for _ in range(m):
        hub.config.get("notify.slack")
    print(f"config.get (nhánh dict): {(time.perf_counter() - t) / m * 1e6:.1f} µs")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1000)
