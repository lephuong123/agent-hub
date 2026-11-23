"""Đo hiệu năng nhẹ, BẬT mặc định (docs/dev-notes/perf-monitor.md): phần nào chậm — trình duyệt, web hay daemon.

- `Rolling`: tổng hợp cuộn trong RAM theo khoá (route/method/metric): thùng 1 phút, giữ `keep_s` gần nhất; mỗi thùng giữ count/sum/max
  + mẫu ngẫu nhiên (reservoir, `array('f')`) để tính p50/p95 có trọng số. Bộ nhớ BỊ CHẶN: `max_keys` khoá (thừa → khoá `(khác)`)
  × số thùng × `reservoir` mẫu × 4 byte.
- `ReqTiming` + contextvar `CURRENT`: phân rã một yêu cầu HTTP của web (RPC tới daemon: số lần + ms theo method; render template ms).
  `note_rpc`/`note_render` là no-op khi không có yêu cầu đang đo (TUI/CLI dùng chung `AsyncHubClient`).
- Daemon: `DaemonPerf` (thời gian MỖI RPC, nạp từ access log `cfharness.rpc`) + ảnh chụp mới nhất web gửi lên (`perf.report`)
  + độ trễ event loop `loop.lag` (daemon đo mỗi giây — `Daemon._loop_lag`; nằm trong bảng RPC như một khoá riêng).
  RPC `perf.stats` (đọc) / `perf.report` (web đẩy) — đều HUMAN_ONLY (token web là người). CLI `cfharness perf`.

Chỉ METADATA: route template (không đường dẫn thô có id), tên method, số ms — không bao giờ tham số/nội dung.
"""

from __future__ import annotations

import argparse
import contextvars
import math
import random
import time
from array import array
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # pragma: no cover
    from .daemon import Daemon

BUCKET_S = 60
KEEP_S = 60 * 60  # giữ tối đa 60 phút gần nhất
RESERVOIR = 32  # mẫu / thùng / khoá
MAX_KEYS = 256
OTHER_KEY = "(khác)"
DEFAULT_WINDOW_MIN = 15
DEFAULT_SLOW_MS = 1500
LOOP_LAG_KEY = "loop.lag"  # khoá độ trễ event loop của daemon trong bảng RPC (hiện ở /perf + `cfharness perf` không cần sửa web)
DEFAULT_LOOP_LAG_WARN_MS = 250


class Rolling:
    """Tổng hợp cuộn có trần bộ nhớ. Không thread-safe (dùng trong một event loop)."""

    def __init__(self, *, bucket_s: int = BUCKET_S, keep_s: int = KEEP_S, reservoir: int = RESERVOIR, max_keys: int = MAX_KEYS,
                 rng: random.Random | None = None) -> None:
        self.bucket_s = max(1, int(bucket_s))
        self.max_buckets = max(1, int(keep_s) // self.bucket_s)
        self.reservoir = max(1, int(reservoir))
        self.max_keys = max(2, int(max_keys))
        self._rng = rng or random.Random()
        # khoá → deque[[bucket_start, count, sum, max, array('f') mẫu]]
        self._data: dict[str, deque[list[Any]]] = {}
        self.dropped_keys = 0

    def __len__(self) -> int:
        return len(self._data)

    def add(self, key: str, ms: float, now: float | None = None) -> None:
        if not isinstance(ms, (int, float)) or ms != ms or ms < 0:  # NaN/âm: bỏ
            return
        now = time.time() if now is None else now
        start = int(now // self.bucket_s) * self.bucket_s
        dq = self._data.get(key)
        if dq is None:
            if len(self._data) >= self.max_keys:
                self._prune(now)
            if len(self._data) >= self.max_keys:
                self.dropped_keys += 1
                key = OTHER_KEY
                dq = self._data.get(key)
            if dq is None:
                dq = self._data[key] = deque(maxlen=self.max_buckets)
        b = dq[-1] if dq else None
        if b is None or b[0] != start:
            b = [start, 0, 0.0, 0.0, array("f")]
            dq.append(b)
        b[1] += 1
        b[2] += ms
        if ms > b[3]:
            b[3] = ms
        samples: array = b[4]
        if len(samples) < self.reservoir:
            samples.append(ms)
        else:
            j = self._rng.randrange(b[1])
            if j < self.reservoir:
                samples[j] = ms

    def _prune(self, now: float) -> None:
        """Bỏ khoá không còn thùng nào trong khoảng giữ."""
        cutoff = now - self.max_buckets * self.bucket_s
        for k in [k for k, dq in self._data.items() if not dq or dq[-1][0] < cutoff]:
            del self._data[k]

    def snapshot(self, window_s: float | None = None, now: float | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Mỗi khoá có dữ liệu trong `window_s` giây gần nhất: count, avg, p50, p95, max (ms). Sắp p95 giảm dần (chậm nhất trước)."""
        now = time.time() if now is None else now
        window_s = self.max_buckets * self.bucket_s if window_s is None else max(1.0, float(window_s))
        cutoff = now - window_s
        out: list[dict[str, Any]] = []
        for key, dq in self._data.items():
            n = 0
            total = 0.0
            mx = 0.0
            weighted: list[tuple[float, float]] = []
            for b in dq:
                if b[0] + self.bucket_s <= cutoff:
                    continue
                n += b[1]
                total += b[2]
                mx = max(mx, b[3])
                s = b[4]
                if s:
                    w = b[1] / len(s)
                    weighted.extend((float(v), w) for v in s)
            if not n:
                continue
            out.append({"key": key, "count": n, "avg": round(total / n, 1), "p50": round(_wpct(weighted, 0.50), 1),
                        "p95": round(_wpct(weighted, 0.95), 1), "max": round(mx, 1)})
        out.sort(key=lambda r: (-r["p95"], -r["count"]))
        return out[:limit] if limit else out


def _wpct(pairs: list[tuple[float, float]], q: float) -> float:
    """Phân vị có trọng số (mẫu của thùng đông được nhân trọng số count/số mẫu)."""
    if not pairs:
        return 0.0
    pairs.sort(key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    target = q * total
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= target - 1e-9:
            return v
    return pairs[-1][0]


def percentile(values: Iterable[float], q: float) -> float:
    """Phân vị không trọng số (tiện cho test/so sánh)."""
    return _wpct([(float(v), 1.0) for v in values], q)


# ============================================================================ phân rã một yêu cầu HTTP (web)
MAX_CALLS = 64


@dataclass
class ReqTiming:
    t0: float = field(default_factory=time.perf_counter)
    calls: list[tuple[str, float]] = field(default_factory=list)  # (method, ms) — tối đa MAX_CALLS, phần thừa chỉ cộng dồn
    rpc_n: int = 0
    rpc_ms: float = 0.0
    render_n: int = 0
    render_ms: float = 0.0

    def by_method(self) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for m, ms in self.calls:
            s = out.setdefault(m, [0, 0.0])
            s[0] += 1
            s[1] += ms
        return out


CURRENT: contextvars.ContextVar[ReqTiming | None] = contextvars.ContextVar("cfharness_perf_req", default=None)


def note_rpc(method: str, ms: float) -> None:
    t = CURRENT.get()
    if t is None:
        return
    t.rpc_n += 1
    t.rpc_ms += ms
    if len(t.calls) < MAX_CALLS:
        t.calls.append((method, ms))


def note_render(ms: float) -> None:
    t = CURRENT.get()
    if t is None:
        return
    t.render_n += 1
    t.render_ms += ms


def server_timing(t: ReqTiming, total_ms: float, *, top: int = 6) -> str:
    """Giá trị header `Server-Timing` (devtools → Timing): app (tới lúc gửi header), rpc (tổng), tpl (render), rồi top method RPC."""
    parts = [f"app;dur={total_ms:.1f}"]
    if t.rpc_n:
        parts.append(f'rpc;dur={t.rpc_ms:.1f};desc="{t.rpc_n} RPC"')
    if t.render_n:
        parts.append(f"tpl;dur={t.render_ms:.1f}")
    for m, (n, ms) in sorted(t.by_method().items(), key=lambda kv: -kv[1][1])[:top]:
        name = "rpc-" + "".join(c if c.isalnum() or c in "-_" else "-" for c in m)[:60]
        parts.append(f'{name};dur={ms:.1f};desc="x{int(n)}"')
    return ", ".join(parts)


# ============================================================================ daemon
class DaemonPerf:
    """Thời gian mỗi RPC của daemon + ảnh chụp web gửi lên (`perf.report`). Chỉ RAM — khởi động lại là mất."""

    def __init__(self) -> None:
        self.rpc = Rolling()
        self.web: dict[str, Any] | None = None
        self.web_at: float | None = None
        self.started = time.time()
        self.loop_lag_last: float | None = None
        self.loop_lag_max = 0.0

    def note(self, method: str, ms: float) -> None:
        self.rpc.add(method[:80], ms)

    def note_loop_lag(self, ms: float) -> None:
        """Một mẫu độ trễ event loop (ms) — khoá `loop.lag` trong bảng RPC + giá trị gần nhất/lớn nhất từ lúc khởi động."""
        self.rpc.add(LOOP_LAG_KEY, ms)
        self.loop_lag_last = ms
        if ms > self.loop_lag_max:
            self.loop_lag_max = ms


WEB_SECTIONS = ("backend", "frontend", "web_rpc")
MAX_REPORT_ROWS = 400


def _clean_rows(rows: Any) -> list[dict[str, Any]]:
    """Ảnh chụp từ web: chỉ giữ trường số + khoá ngắn (web là người, nhưng vẫn không tin dữ liệu tuỳ ý)."""
    out: list[dict[str, Any]] = []
    for r in (rows if isinstance(rows, list) else [])[:MAX_REPORT_ROWS]:
        if not isinstance(r, dict) or not isinstance(r.get("key"), str):
            continue
        row: dict[str, Any] = {"key": r["key"][:200]}
        for k in ("count", "avg", "p50", "p95", "max"):
            v = r.get(k)
            row[k] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else 0.0
        row["count"] = int(row["count"])
        out.append(row)
    return out


def _window_s(hub: Any, p: dict[str, Any]) -> float:
    try:
        m = float(p.get("window_min") or hub.config.get("perf.window_min") or DEFAULT_WINDOW_MIN)
    except (TypeError, ValueError, KeyError):
        m = DEFAULT_WINDOW_MIN
    return max(1.0, min(m, KEEP_S / 60)) * 60


def perf_config(hub: Any) -> dict[str, Any]:
    def _get(k: str, d: Any) -> Any:
        try:
            v = hub.config.get(k)
        except KeyError:
            return d
        return d if v is None else v

    return {"enabled": bool(_get("perf.enabled", True)), "slow_ms": float(_get("perf.slow_ms", DEFAULT_SLOW_MS) or 0),
            "window_min": float(_get("perf.window_min", DEFAULT_WINDOW_MIN) or DEFAULT_WINDOW_MIN),
            "loop_lag_warn_ms": float(_get("perf.loop_lag_warn_ms", DEFAULT_LOOP_LAG_WARN_MS) or 0)}


def register_rpc(daemon: "Daemon") -> None:
    hub = daemon.hub
    dp: DaemonPerf = daemon.perf

    async def _stats(p: dict[str, Any]) -> dict[str, Any]:
        win = _window_s(hub, p)
        try:
            limit = max(1, min(int(p.get("limit") or 50), 500))
        except (TypeError, ValueError):
            limit = 50
        web = dp.web or {}
        rows = dp.rpc.snapshot(win)
        lag = next((r for r in rows if r["key"] == LOOP_LAG_KEY), None)
        return {"window_min": round(win / 60, 1), "now": time.time(), "daemon_started": dp.started,
                "rpc": rows[:limit],
                "loop": {**(lag or {}), "last": None if dp.loop_lag_last is None else round(dp.loop_lag_last, 1),
                         "max_since_start": round(dp.loop_lag_max, 1)},
                "web": {"at": dp.web_at, **{s: (web.get(s) or [])[:limit] for s in WEB_SECTIONS}},
                "config": perf_config(hub)}

    async def _report(p: dict[str, Any]) -> dict[str, Any]:
        dp.web = {s: _clean_rows(p.get(s)) for s in WEB_SECTIONS}
        dp.web_at = time.time()
        return perf_config(hub)

    daemon.methods.update({"perf.stats": _stats, "perf.report": _report})


# ============================================================================ CLI `cfharness perf`
def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    pf = sub.add_parser("perf", help="hiệu năng: phần nào chậm nhất (trình duyệt, web, RPC daemon) — p50/p95/max trong N phút — chỉ người",
                        parents=[common])
    pf.add_argument("--window", type=float, default=None, help="số phút gần nhất (mặc định perf.window_min = 15)")
    pf.add_argument("-n", "--limit", type=int, default=15, help="số dòng mỗi bảng (mặc định 15)")
    pf.set_defaults(fn=cmd_perf)


def _table(title: str, rows: list[dict[str, Any]]) -> list[str]:
    lines = [f"{title}:"]
    if not rows:
        return lines + ["  (chưa có dữ liệu)"]
    w = min(60, max(len(r["key"]) for r in rows))
    lines.append(f"  {'khoá':<{w}} {'số lần':>7} {'p50':>9} {'p95':>9} {'max':>9}")
    for r in rows:
        lines.append(f"  {r['key'][:w]:<{w}} {int(r['count']):>7} {r['p50']:>7.0f}ms {r['p95']:>7.0f}ms {r['max']:>7.0f}ms")
    return lines


def format_stats(r: dict[str, Any]) -> str:
    web = r.get("web") or {}
    at = web.get("at")
    lines = [f"Hiệu năng {r.get('window_min')} phút gần nhất (sắp theo p95, chậm nhất trước)"]
    lines += _table("Trình duyệt (FE)", web.get("frontend") or [])
    lines += _table("Web (BE, theo route)", web.get("backend") or [])
    lines += _table("RPC daemon (theo method)", r.get("rpc") or [])
    lp = r.get("loop") or {}
    if lp.get("count"):
        lines.append(f"Độ trễ event loop daemon ({LOOP_LAG_KEY}): p50 {lp['p50']:.0f}ms · p95 {lp['p95']:.0f}ms · max {lp['max']:.0f}ms"
                     f" · lớn nhất từ lúc khởi động {lp.get('max_since_start', 0):.0f}ms")
    lines.append(f"Ảnh chụp web: {'%d giây trước' % int(time.time() - at) if at else 'chưa có (web chưa chạy hoặc chưa có request)'}"
                 " · trang web: /perf")
    return "\n".join(lines)


def cmd_perf(ctx: Any) -> int:
    a = ctx.args
    p: dict[str, Any] = {"limit": a.limit}
    if a.window:
        p["window_min"] = a.window
    r = ctx.call("perf.stats", p)
    ctx.out(r, format_stats(r))
    return 0


__all__ = ["LOOP_LAG_KEY", "Rolling", "ReqTiming", "CURRENT", "note_rpc", "note_render", "server_timing", "DaemonPerf", "register_rpc", "add_parsers",
           "percentile", "perf_config"]
