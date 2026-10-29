"""Tương thích phiên bản `claude` CLI (FR-RT-15, hardening H-10) + hook quan sát quá giờ (H-11).

- `CliCompat.check_on_start()` (gọi trong `Hub.start`): đọc phiên bản runtime (`claude --version`, cache), so với
  `runtime.claude.tested_versions` → `hub_state.cli_compat` {version, tested, status ok|untested|missing, checked_at}
  (header TUI, `daemon status`, `doctor`); phiên bản lạ → event `cli.version_warning` (vàng).
- Listener bus đếm event từ stream CLI (`source=cli`, bỏ delta token) và `raw.unknown` trong cửa sổ trượt
  `runtime.claude.unknown_alert.window_s` (1 giờ). Tỉ lệ > `threshold` (5%) với ≥ `min_events` → `cli.unknown_rate_alert`
  (level summary, đỏ trên TUI, gợi ý ESTOP) — tối đa một lần mỗi cửa sổ; `hub_state.cli_unknown_rate` luôn cập nhật.
- Nạp `logs/hook-timeouts.jsonl` (hook quan sát không với tới daemon) thành event `hook.timeout` lúc khởi động.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from ..events import HubEvent, iso, make_event

if TYPE_CHECKING:
    from ..hub import Hub

log = logging.getLogger("cfharness.compat")
_DELTA = ("message.delta", "reasoning.delta", "tool.generating")


class CliCompat:
    def __init__(self, hub: "Hub", *, clock: Any = time.monotonic) -> None:
        self.hub = hub
        self.clock = clock
        self.window: deque[tuple[float, bool]] = deque()
        self.unknown = 0
        self.last_alert: float | None = None
        self._last_state_write = 0.0
        hub.bus.add_listener(self.on_event)

    # ------------------------------------------------------------------ cấu hình
    def _alert_cfg(self) -> tuple[float, float, int]:
        c = self.hub.config.get("runtime.claude").get("unknown_alert") or {}
        return float(c.get("threshold", 0.05)), float(c.get("window_s", 3600)), int(c.get("min_events", 50))

    # ------------------------------------------------------------------ raw.unknown
    def on_event(self, ev: HubEvent) -> None:
        if ev.source != "cli" or ev.type in _DELTA:
            return
        now = self.clock()
        threshold, window_s, min_events = self._alert_cfg()
        is_unknown = ev.type == "raw.unknown"
        self.window.append((now, is_unknown))
        self.unknown += int(is_unknown)
        while self.window and self.window[0][0] < now - window_s:
            _, u = self.window.popleft()
            self.unknown -= int(u)
        total = len(self.window)
        rate = self.unknown / total if total else 0.0
        alert = total >= min_events and rate > threshold
        if alert and (self.last_alert is None or now - self.last_alert >= window_s):
            self.last_alert = now
            self.hub.publish(make_event("cli.unknown_rate_alert", None, {
                "unknown": self.unknown, "total": total, "rate": round(rate, 4), "threshold": threshold, "window_s": window_s,
                "cli_version": (self.hub.store.state_get("cli_compat") or {}).get("version"),
                "suggestion": "cfharness estop on --reason 'CLI stream lạ' (kiểm phiên bản claude, ghi fixture mới + contract test)",
            }))
            log.warning("raw.unknown %d/%d (%.1f%%) trong %ds — cảnh báo đỏ", self.unknown, total, rate * 100, window_s)
        if is_unknown or alert or now - self._last_state_write > 30:
            self._last_state_write = now
            try:
                self.hub.store.state_set("cli_unknown_rate", {"unknown": self.unknown, "total": total, "rate": round(rate, 4),
                                                              "alert": alert, "threshold": threshold, "at": iso()})
            except Exception:  # pragma: no cover
                log.exception("không ghi được cli_unknown_rate")

    # ------------------------------------------------------------------ phiên bản lúc khởi động
    def record_version(self, version: str | None, runtime_name: str) -> dict[str, Any]:
        tested = list(self.hub.config.get("runtime.claude.tested_versions") or [])
        if runtime_name != "claude":
            status = "fake"
        elif not version:
            status = "missing"
        else:
            status = "ok" if version in tested else "untested"
        state = {"version": version, "tested": tested, "status": status, "runtime": runtime_name, "checked_at": iso(),
                 "flag_matrix": bool(version and version in (self.hub.config.get("runtime.claude").get("flag_matrix") or {}))}
        self.hub.store.state_set("cli_compat", state)
        # một lần mỗi phiên bản mỗi daemon, chung bộ đếm với lần spawn đầu (RunManager.warn_cli_version)
        if status == "untested":
            self.hub.runs.warn_cli_version(version, tested, at="daemon_start")
        elif status == "missing":
            self.hub.runs.warn_cli_version(None, tested, missing=True, at="daemon_start")
        return state

    async def _check(self) -> None:
        try:
            rt = self.hub.runs.runtime()
            v = await asyncio.to_thread(rt.version)
            self.record_version(v, getattr(rt, "name", "?"))
        except Exception as e:  # pragma: no cover - không để kiểm phiên bản làm hỏng daemon
            log.warning("kiểm phiên bản CLI lỗi: %r", e)

    def check_on_start(self) -> None:
        self.ingest_hook_spool()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            self.task = loop.create_task(self._check())
        else:
            try:
                rt = self.hub.runs.runtime()
                self.record_version(rt.version(), getattr(rt, "name", "?"))
            except Exception as e:  # pragma: no cover
                log.warning("kiểm phiên bản CLI lỗi: %r", e)

    # ------------------------------------------------------------------ hook quan sát (spool)
    def ingest_hook_spool(self) -> int:
        f = self.hub.paths.logs_dir / "hook-timeouts.jsonl"
        if not f.exists():
            return 0
        n = 0
        try:
            tmp = f.with_suffix(".jsonl.ingest")
            f.replace(tmp)
            for line in tmp.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                run = self.hub.store.get_run(str(rec.get("run_id") or "")) if rec.get("run_id") else None
                self.hub.publish(make_event("hook.timeout", run["agent_id"] if run else None,
                                            {"hook_event": rec.get("event"), "timeout_s": rec.get("timeout_s"), "elapsed_ms": rec.get("elapsed_ms"),
                                             "reason": rec.get("reason"), "fail_open": True, "spooled": True},
                                            run_id=run["id"] if run else None, source="hook"))
                n += 1
            tmp.unlink(missing_ok=True)
        except OSError as e:  # pragma: no cover
            log.warning("không nạp được %s: %r", f, e)
        return n


def attach(hub: "Hub") -> CliCompat:
    hub.compat = CliCompat(hub)  # type: ignore[attr-defined]
    return hub.compat  # type: ignore[attr-defined]
