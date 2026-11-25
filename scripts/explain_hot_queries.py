"""In `EXPLAIN QUERY PLAN` của các truy vấn nóng trong daemon (plan-api-caching PR3) và đo `tui.snapshot` trên một hub tạm.

Cách chạy: `uv run python scripts/explain_hot_queries.py [--bench] [--agents N] [--runs N]`.
- Mặc định: tạo hub.db tạm ở migration mới nhất, in kế hoạch truy vấn của từng câu (dùng index nào, có quét bảng/sắp tạm không).
- `--bench`: dựng Hub tạm (FakeRuntime, không gọi `claude`) có N agent + N run, đo thời gian `tui.snapshot` (không cache và có cache).
Chỉ đọc/ghi thư mục tạm; không đụng hub thật.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cfharness.events import iso
from cfharness.store import MIGRATIONS, Store

NOW = datetime.now(timezone.utc)
KINDS = ("adhoc", "adhoc", "adhoc", "task", "task", "review", "curator", "graph_extract")
SINCE = iso(NOW - timedelta(hours=12))

# (nhãn, câu SQL, tham số) — đúng hình dạng câu lệnh trong code.
QUERIES: list[tuple[str, str, tuple]] = [
    ("dispatcher: task đang claim (tasks.current_run_id=?)", "SELECT * FROM tasks WHERE current_run_id = ?", ("__claiming__",)),
    ("store.query_events: backfill agent+epoch+since_seq",
     "SELECT * FROM events WHERE agent_id=? AND epoch=? AND seq>? ORDER BY ts, epoch, seq LIMIT ?", ("a0", "e1", 100, 10_000)),
    ("curation: chi phí curator hôm nay (kind=? AND queued_at>=?)",
     "SELECT COALESCE(SUM(cost_usd),0) FROM runs WHERE kind='curator' AND queued_at>=?", (SINCE,)),
    ("learning: chi phí review (kind='review' AND queued_at>=?)",
     "SELECT COALESCE(SUM(cost_usd),0) FROM runs WHERE kind='review' AND queued_at>=?", (SINCE,)),
    ("graph_quality: số lần trích trong ngày", "SELECT COUNT(*) FROM runs WHERE kind='graph_extract' AND queued_at>=?", (SINCE,)),
    ("graph_extract: ngân sách theo agent",
     "SELECT COUNT(*), COALESCE(SUM(cost_usd), 0) FROM runs WHERE agent_id=? AND kind=? AND queued_at>=?", ("a0", "graph_extract", SINCE)),
    ("cost_today (cả hub)", "SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE ended_at >= ?", (SINCE,)),
    ("cost_today_by_agent (agent.list)", "SELECT agent_id, COALESCE(SUM(cost_usd), 0) FROM runs WHERE ended_at >= ? GROUP BY +agent_id", (SINCE,)),
    ("tui.snapshot (cũ): token/chi phí hôm nay — kéo usage về Python", "SELECT agent_id, usage, cost_usd FROM runs WHERE ended_at >= ?", (SINCE,)),
    ("tui.snapshot: đếm learning.* hôm nay",
     "SELECT COUNT(*) FROM events WHERE type >= 'learning.' AND type < 'learning/' AND ts >= ?", (SINCE,)),
    ("tui.snapshot (cũ): run mới nhất của MỘT agent (N lần)", "SELECT * FROM runs WHERE agent_id=? ORDER BY queued_at DESC LIMIT 1", ("a0",)),
    ("tui.snapshot (cũ): run hiện tại của MỘT agent (N lần, kéo cả diff)", "SELECT * FROM runs WHERE id=?", ("r0",)),
    ("tui.snapshot (cũ): tiêu đề task của MỘT agent (N lần)", "SELECT title FROM tasks WHERE id=?", ("t0",)),
]
# Câu lệnh MỚI lấy đúng từ code (nếu module có — chạy trên nhánh cũ thì bỏ qua).
try:
    from cfharness.tui import rpc as _rpc

    QUERIES += [
        ("tui.snapshot (mới): token+chi phí hôm nay cộng trong SQLite", _rpc.TODAY_USAGE_SQL.format(agent=""), (SINCE,)),
        ("tui.agent_state (mới): token+chi phí hôm nay của một agent", _rpc.TODAY_USAGE_SQL.format(agent=" AND agent_id=?"), (SINCE, "a0")),
        ("tui.snapshot (mới): run mới nhất của MỌI agent (1 lần)", _rpc.LAST_RUNS_SQL.format(cols="id"), ('["a0","a1"]',)),
        ("tui.snapshot (mới): run hiện tại của MỌI agent (1 lần)", "SELECT id FROM runs WHERE id IN (SELECT value FROM json_each(?))", ('["r0"]',)),
        ("tui.snapshot (mới): tiêu đề task (1 lần)", "SELECT id, title FROM tasks WHERE id IN (SELECT value FROM json_each(?))", ('["t0"]',)),
    ]
except (ImportError, AttributeError):
    pass
QUERIES += [
    ("tui.snapshot: task chờ claim",
     "SELECT id FROM tasks WHERE status='ready' AND wait_reason IS NOT NULL ORDER BY updated_at LIMIT 100", ()),
    ("hub.status (cũ): list_agents()", "SELECT * FROM agents WHERE archived_at IS NULL ORDER BY id", ()),
    ("hub.status (mới): COUNT(*)", "SELECT COUNT(*) FROM agents WHERE archived_at IS NULL", ()),
]


def explain(store: Store) -> None:
    print(f"# user_version = {store.schema_version} / {len(MIGRATIONS)} migration")
    for label, sql, args in QUERIES:
        plan = [str(r[3]) for r in store.conn.execute("EXPLAIN QUERY PLAN " + sql, args)]
        print(f"\n## {label}\n   {sql}")
        for p in plan:
            print(f"   -> {p}")


def bench(n_agents: int, n_runs: int, reps: int) -> None:
    from cfharness.hub import Hub
    from cfharness.paths import HubPaths
    from cfharness.runtime import FakeRuntime
    from cfharness.tui import rpc as tui_rpc

    with tempfile.TemporaryDirectory() as td:
        os.environ["CFHARNESS_HOME"] = str(Path(td) / "hub")
        paths = HubPaths.resolve()
        paths.ensure()
        hub = Hub(paths, runtime=FakeRuntime())
        hub.start()
        try:
            for i in range(n_agents):
                hub.agents.create({"id": f"a{i}", "name": f"A{i}"})
            rnd = random.Random(1)
            with hub.store.tx() as c:
                for j in range(n_runs):
                    q = iso(NOW - timedelta(minutes=rnd.randint(0, 60 * 24 * 7)))
                    c.execute("INSERT INTO runs(id, agent_id, kind, status, queued_at, ended_at, cost_usd, usage, prompt, cli_version) "
                              "VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (f"r{j}", f"a{rnd.randrange(n_agents)}", rnd.choice(KINDS), "completed", q, q, 0.01, '{"input_tokens": 10}',
                               "x" * 200, "2.1.0"))
            # Không ANALYZE: hub thật không có sqlite_stat1 → planner chọn index theo luật mặc định, giống ở đây.
            t0 = time.perf_counter()
            for _ in range(reps):
                tui_rpc.snapshot(hub)
            raw = (time.perf_counter() - t0) / reps * 1000
            print(f"\n# bench: {n_agents} agent, {n_runs} run — tui.snapshot không cache: {raw:.2f} ms/lần")
            cache = getattr(tui_rpc, "SnapshotCache", None)
            if cache is not None:
                sc = cache(hub)
                sc.get()
                t0 = time.perf_counter()
                for _ in range(reps):
                    sc.get()
                hit = (time.perf_counter() - t0) / reps * 1000
                print(f"# bench: tui.snapshot qua cache (trúng): {hit:.3f} ms/lần")
        finally:
            hub.store.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--agents", type=int, default=40)
    ap.add_argument("--runs", type=int, default=20_000)
    ap.add_argument("--reps", type=int, default=50)
    a = ap.parse_args()
    with tempfile.TemporaryDirectory() as td:
        s = Store(Path(td) / "hub.db")
        try:
            explain(s)
        finally:
            s.close()
    if a.bench:
        bench(a.agents, a.runs, a.reps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
