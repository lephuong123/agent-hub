"""Đo overhead học NFR-19 bằng FakeRuntime (không gọi model): `python -m agent_hub.curation.overhead --home <tmp> [-n 30]`.

Hai hub giống hệt nhau chạy N run làm việc tuần tự với cùng fixture:
- `off`: công tắc học tắt (`policy/learning.yaml enabled:false`), eval/curator/curation tắt;
- `on` : mặc định (review kích hoạt theo bộ đếm `learning.trigger_turns`, eval/curation bật).
Đo: tổng thời gian tường (gồm cả run học sinh ra), thời gian CPU của listener học trên bus (learning, learning_ops, curation,
telemetry — bọc từng listener), và chi phí run học / run làm việc theo `overhead_report` (chi phí fixture là MÔ PHỎNG:
review dùng cùng fixture với run làm việc → tỷ lệ chi phí ≈ tần suất review). Số thật lấy bằng `agent-hub report overhead`
trên HUB_HOME đã chạy `claude` thật.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

import yaml


async def _wait_terminal(hub: Any, run_id: str, timeout: float = 30.0) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        r = hub.store.get_run(run_id)
        if r and r["status"] not in ("queued", "running"):
            return
        await asyncio.sleep(0.005)
    raise TimeoutError(run_id)


async def _wait_idle(hub: Any, timeout: float = 30.0) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        n = hub.store.conn.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued','running')").fetchone()[0]
        q = hub.store.conn.execute("SELECT COUNT(*) FROM review_jobs WHERE status IN ('queued','running')").fetchone()[0] if _has(hub, "review_jobs") else 0
        if not n and not q and not hub.runs.active:
            return
        try:
            hub.learning.reviews.tick()
        except Exception:
            pass
        await asyncio.sleep(0.02)


def _has(hub: Any, table: str) -> bool:
    return hub.store.conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is not None


def _instrument(hub: Any) -> dict[str, float]:
    acc = {"listener_s": 0.0, "calls": 0}
    owners = [getattr(hub, n, None) for n in ("learning", "learning_ops", "curation", "telemetry")]
    owners += [getattr(getattr(hub, "learning", None), "canary", None)]
    owners = [o for o in owners if o is not None]

    def wrap(fn: Any) -> Any:
        def w(*a: Any, **k: Any) -> Any:
            t = time.perf_counter()
            try:
                return fn(*a, **k)
            finally:
                acc["listener_s"] += time.perf_counter() - t
                acc["calls"] += 1

        return w

    ls = hub.bus._listeners
    for i, fn in enumerate(list(ls)):
        if getattr(fn, "__self__", None) in owners:
            ls[i] = wrap(fn)
    lr = getattr(hub, "learning", None)
    if lr is not None:
        lr._after_run = wrap(lr._after_run)
    return acc


async def _one(home: Path, mode: str, n: int, line_delay_s: float) -> dict[str, Any]:
    from ..hub import Hub
    from ..paths import HubPaths
    from ..runtime import FakeRuntime
    from .kpi import overhead_report

    home.mkdir(parents=True, exist_ok=True)
    paths = HubPaths.resolve(str(home))
    paths.ensure()
    cfg: dict[str, Any] = {"runtime": {"kind": "fake"}, "guard": {"git_probe": False}, "learning": {"review_debounce_s": 0, "tick_s": 0.05}}
    if mode == "off":
        cfg.update({"eval": {"enabled": False}, "curator": {"enabled": False}, "curation": {"enabled": False}})
        (home / "policy").mkdir(parents=True, exist_ok=True)
        (home / "policy" / "learning.yaml").write_text(yaml.safe_dump({"enabled": False}), encoding="utf-8")
    paths.config.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    hub = Hub(paths, runtime=FakeRuntime(line_delay_s=line_delay_s))
    acc = _instrument(hub)
    hub.start()
    hub.agents.create({"id": "bench", "name": "Bench"})
    t0 = time.perf_counter()
    for i in range(n):
        r = hub.runs.submit("bench", f"benchmark task {i}")
        await _wait_terminal(hub, r["id"])
        await _wait_idle(hub)
    wall = time.perf_counter() - t0
    rep = overhead_report(hub)
    learn_runs = rep["total"]["learn_runs"]
    hub.runs.shutting_down = True
    try:
        hub.learning.stop()
        hub.learning_ops.stop()
        hub.curation.stop()
    except Exception:
        pass
    await asyncio.sleep(0.05)
    hub.store.close()
    return {"mode": mode, "runs": n, "wall_s": round(wall, 4), "per_run_ms": round(1000 * wall / n, 2), "learning_listener_s": round(acc["listener_s"], 4),
            "learning_listener_calls": acc["calls"], "learn_runs": learn_runs, "cost_overhead_pct_simulated": rep["total"]["cost_overhead_pct"]}


async def bench(home: Path, n: int = 30, line_delay_s: float = 0.0) -> dict[str, Any]:
    off = await _one(home / "off", "off", n, line_delay_s)
    on = await _one(home / "on", "on", n, line_delay_s)
    time_pct = round(100.0 * (on["wall_s"] - off["wall_s"]) / off["wall_s"], 2) if off["wall_s"] else None
    listener_pct = round(100.0 * on["learning_listener_s"] / on["wall_s"], 2) if on["wall_s"] else None
    return {"off": off, "on": on, "time_overhead_pct": time_pct, "hub_learning_cpu_pct": listener_pct, "limit_pct": 15.0,
            "note": "time_overhead_pct gồm cả run review/eval sinh ra (FakeRuntime); hub_learning_cpu_pct = CPU listener học / thời gian tường"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Đo overhead học NFR-19 bằng FakeRuntime")
    ap.add_argument("--home", required=True, help="thư mục tạm (sẽ tạo off/ và on/)")
    ap.add_argument("-n", type=int, default=30)
    ap.add_argument("--line-delay", type=float, default=0.002, help="độ trễ mỗi dòng fixture (mô phỏng run thật dài hơn)")
    a = ap.parse_args(argv)
    print(json.dumps(asyncio.run(bench(Path(a.home), a.n, a.line_delay)), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
