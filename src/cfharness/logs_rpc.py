"""RPC + CLI đổi level log lúc chạy (L1, docs/dev-notes/plan-logging.md B2).

RPC (đều HUMAN_ONLY — `auth.HUMAN_ONLY`; không thuộc MANAGER_ALLOWED):
- `logs.levels` → level đang áp dụng (`effective`), cấu hình (`config`), level tạm còn hạn (`temp`), lỗi đếm được (`errors`).
- `logs.level {logger, level, for_s?}` → level TẠM (mặc định 30 phút, tối đa 24 h) ở `hub_state logs.temp_levels`, tự hết hạn;
  `level: "clear"` gỡ. Tự bọc audit (`MUTATING_LOGS`). Muốn đổi lâu dài: `cfharness config set logs.level debug`.

Chỉ đổi được logger `cfharness[.x]` và thư viện trong `logs.LIB_LOGGERS` (không httpx/urllib3 — DEBUG in URL có token bot).
CLI: `cfharness logs level [<logger> <level|clear>] [--for 30m]` — `logs` ∈ HUB_HUMAN_ONLY_CLI.
"""

from __future__ import annotations

import argparse
import re
import time
from typing import TYPE_CHECKING, Any

from .errors import HubError
from .logs import ERROR_COUNTS, LEVELS, MAX_TEMP_S, TEMP_KEY, parse_level, refresh_levels, valid_logger_name

if TYPE_CHECKING:  # pragma: no cover
    from .daemon import Daemon

MUTATING_LOGS = ("logs.level",)
DEFAULT_TEMP_S = 30 * 60
_LEVEL_NAME = {v: k for k, v in LEVELS.items()}


def register_rpc(daemon: "Daemon") -> None:
    from .mcp_server.rpc import wrap_with_audit

    hub = daemon.hub

    def _snapshot() -> dict[str, Any]:
        levels = refresh_levels(hub, hub.log_levels)
        temp = hub.store.state_get(TEMP_KEY, {}) or {}
        cfg = hub.config.get("logs") or {}
        return {"effective": {k: _LEVEL_NAME.get(v, str(v)) for k, v in sorted(levels.items())},
                "config": {"level": cfg.get("level") or "info", "levels": cfg.get("levels") or {}},
                "temp": {k: {"level": v.get("level"), "until": v.get("until")} for k, v in sorted(temp.items()) if isinstance(v, dict)},
                "errors": dict(ERROR_COUNTS, bus_listener=getattr(hub.bus, "listener_errors", 0))}

    async def _levels(p: dict[str, Any]) -> dict[str, Any]:
        return _snapshot()

    async def _level(p: dict[str, Any]) -> dict[str, Any]:
        name = str(p.get("logger") or "").strip()
        if not valid_logger_name(name):
            raise HubError("logger không hợp lệ — chỉ 'cfharness[.x]' hoặc " + ", ".join(sorted({"asyncio", "mcp", "uvicorn", "starlette", "websockets"})),
                           code=-32602)
        lv = str(p.get("level") or "").strip().lower()
        temp = dict(hub.store.state_get(TEMP_KEY, {}) or {})
        if lv in ("clear", "reset", "default"):
            temp.pop(name, None)
        else:
            if parse_level(lv) is None:
                raise HubError("level phải là debug|info|warning|error|critical|clear", code=-32602)
            try:
                for_s = int(p.get("for_s") or DEFAULT_TEMP_S)
            except (TypeError, ValueError):
                raise HubError("for_s phải là số giây", code=-32602) from None
            if not 60 <= for_s <= MAX_TEMP_S:
                raise HubError(f"thời hạn phải từ 60 giây tới {MAX_TEMP_S // 3600} giờ", code=-32602)
            temp[name] = {"level": lv, "until": round(time.time() + for_s, 3)}
        hub.store.state_set(TEMP_KEY, temp)
        return {"logger": name, "level": lv, **_snapshot()}

    daemon.methods.update({"logs.levels": _levels, "logs.level": wrap_with_audit(hub, "logs.level", _level)})


# ============================================================================ CLI
_DUR = re.compile(r"^\s*(\d+)\s*([smh]?)\s*$")


def parse_duration(s: str) -> int:
    m = _DUR.match(s or "")
    if not m:
        raise ValueError(f"thời hạn không hợp lệ: {s!r} (vd 30m, 2h, 90s)")
    return int(m.group(1)) * {"": 60, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    lg = sub.add_parser("logs", help="log hệ thống: đổi level lúc chạy — chỉ người", parents=[common])
    ls = lg.add_subparsers(dest="logs_cmd", required=True)
    lv = ls.add_parser("level", help="xem level log, hoặc đặt level TẠM cho một logger (tự hết hạn)", parents=[common])
    lv.add_argument("logger", nargs="?", help="vd cfharness, cfharness.guard, cfharness.runs, asyncio")
    lv.add_argument("level", nargs="?", help="debug|info|warning|error|critical|clear")
    lv.add_argument("--for", dest="for_", default="30m", help="thời hạn (mặc định 30m, tối đa 24h)")
    lv.set_defaults(fn=cmd_level)


def _fmt(r: dict[str, Any]) -> str:
    lines = ["Level đang áp dụng:"]
    lines += [f"  {k:<28} {v}" for k, v in r.get("effective", {}).items()]
    temp = r.get("temp") or {}
    if temp:
        lines.append("Level tạm (tự hết hạn):")
        now = time.time()
        for k, v in temp.items():
            left = max(0, int(float(v.get("until") or 0) - now))
            lines.append(f"  {k:<28} {v.get('level')}  · còn {left // 60} phút")
    errs = {k: v for k, v in (r.get("errors") or {}).items() if v}
    if errs:
        lines.append("Lỗi không ai bắt (từ khi daemon chạy): " + ", ".join(f"{k}={v}" for k, v in errs.items()))
    lines.append("Đổi lâu dài: cfharness config set logs.level <level>  ·  file: HUB_HOME/logs/hub.log")
    return "\n".join(lines)


def cmd_level(ctx: Any) -> int:
    a = ctx.args
    if not a.logger:
        r = ctx.call("logs.levels", {})
        ctx.out(r, _fmt(r))
        return 0
    if not a.level:
        raise SystemExit("thiếu level: cfharness logs level <logger> <debug|info|warning|error|critical|clear>")
    try:
        for_s = parse_duration(a.for_)
    except ValueError as e:
        raise SystemExit(f"lỗi: {e}") from None
    r = ctx.call("logs.level", {"logger": a.logger, "level": a.level, "for_s": for_s})
    msg = (f"đã gỡ level tạm của {a.logger}" if a.level.lower() in ("clear", "reset", "default")
           else f"{a.logger} = {a.level.lower()} trong {for_s // 60} phút (tự về mức cũ sau đó)")
    ctx.out(r, msg + "\n" + _fmt(r))
    return 0


__all__ = ["register_rpc", "add_parsers", "MUTATING_LOGS", "parse_duration"]
