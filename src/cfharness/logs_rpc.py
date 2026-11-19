"""RPC + CLI đổi level log lúc chạy (L1, docs/dev-notes/plan-logging.md B2).

RPC (đều HUMAN_ONLY — `auth.HUMAN_ONLY`; không thuộc MANAGER_ALLOWED):
- `logs.levels` → level đang áp dụng (`effective`), cấu hình (`config`), level tạm còn hạn (`temp`), lỗi đếm được (`errors`).
- `logs.level {logger, level, for_s?}` → level TẠM (mặc định 30 phút, tối đa 24 h) ở `hub_state logs.temp_levels`, tự hết hạn;
  `level: "clear"` gỡ. Tự bọc audit (`MUTATING_LOGS`). Muốn đổi lâu dài: `cfharness config set logs.level debug`.

Chỉ đổi được logger `cfharness[.x]` và thư viện trong `logs.LIB_LOGGERS` (không httpx/urllib3 — DEBUG in URL có token bot).
CLI: `cfharness logs level [<logger> <level|clear>] [--for 30m]` — `logs` ∈ HUB_HUMAN_ONLY_CLI.

L6 (công cụ debug, B6) — đều HUMAN_ONLY + ∈ `SENSITIVE_READS` (daemon ghi `read:<method>`, gộp 10 phút), chỉ đọc:
- `logs.query {since?, until?, level?, logger?, req?, run?, agent?, task?, proc?, limit?, max_bytes?}` → `log_query.query` (lọc theo trường JSON,
  đọc cả bản xoay `.N`/`.gz`, trần dòng + byte). CLI `cfharness logs [--since 2h] [--level warning] … [-n 200] [-f] [--json]`: gọi RPC, daemon
  dừng → đọc file trực tiếp (gỡ lỗi khi daemon chết); `-f` theo dõi file cục bộ.
- `trace.get {id, limit?, logs?}` → `trace_view.build` (audit + log + event tóm tắt + guard + approval + run con). CLI `cfharness trace <id>`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
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

    async def _query(p: dict[str, Any]) -> dict[str, Any]:
        filters, limit, max_bytes = query_args(p)
        return await asyncio.to_thread(run_query, hub.paths.logs_dir, filters, limit, max_bytes)

    async def _trace(p: dict[str, Any]) -> dict[str, Any]:
        ident = str(p.get("id") or "").strip()
        try:
            limit = int(p.get("limit") or 1000)
        except (TypeError, ValueError):
            raise HubError("limit phải là số", code=-32602) from None
        try:
            return await asyncio.to_thread(run_trace, hub.paths.db, hub.paths.logs_dir, ident, limit, p.get("logs", True) is not False)
        except ValueError as e:
            raise HubError(str(e), code=-32602) from None

    daemon.methods.update({"logs.levels": _levels, "logs.level": wrap_with_audit(hub, "logs.level", _level), "logs.query": _query,
                           "trace.get": _trace})


# ============================================================================ L6: truy vấn log + dòng thời gian (dùng chung RPC + CLI cục bộ)
QUERY_KEYS = ("since", "until", "level", "logger", "req", "run", "agent", "task", "proc")


def query_args(p: dict[str, Any]) -> tuple[Any, int, int]:
    """Tham số `logs.query` → (Filters, limit, max_bytes). Sai → HubError -32602."""
    from .log_query import DEFAULT_LIMIT, DEFAULT_SCAN_BYTES, Filters, parse_time

    try:
        f = Filters(since=parse_time(p.get("since")), until=parse_time(p.get("until")), level=p.get("level") or None,
                    logger=p.get("logger") or None, req=p.get("req") or None, run=p.get("run") or None, agent=p.get("agent") or None,
                    task=p.get("task") or None, proc=p.get("proc") or None)
        limit = int(p.get("limit") or DEFAULT_LIMIT)
        max_bytes = int(p.get("max_bytes") or DEFAULT_SCAN_BYTES)
    except (TypeError, ValueError) as e:
        raise HubError(str(e) or "tham số không hợp lệ", code=-32602) from None
    return f, limit, max_bytes


def _mask_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Che lại theo GIÁ TRỊ secret đã biết (log đã che lúc ghi; lớp thứ hai cho bản ghi cũ/thư viện ngoài)."""
    from .redact import mask_values

    for d in rows:
        for k in ("msg", "exc"):
            if isinstance(d.get(k), str):
                d[k] = mask_values(d[k])
    return rows


def run_query(logs_dir: Any, filters: Any, limit: int, max_bytes: int) -> dict[str, Any]:
    from .log_query import query

    res = query(logs_dir, filters, limit=limit, max_bytes=max_bytes)
    res["rows"] = _mask_rows(res["rows"])
    return res


def run_trace(db: Any, logs_dir: Any, ident: str, limit: int = 1000, include_logs: bool = True) -> dict[str, Any]:
    """Kết nối SQLite CHỈ ĐỌC riêng (chạy trong thread, WAL cho đọc song song với writer của daemon; daemon dừng cũng đọc được)."""
    import sqlite3

    from .redact import mask_values
    from .trace_view import build

    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as e:
        raise ValueError(f"không mở được hub.db: {e}") from None
    conn.row_factory = sqlite3.Row
    try:
        res = build(conn, logs_dir, ident, limit=limit, include_logs=include_logs)
    except sqlite3.Error as e:
        raise ValueError(f"không đọc được hub.db: {e}") from None
    finally:
        conn.close()
    for it in res["items"]:
        det = it.get("detail") or {}
        for k in ("msg", "exc", "summary"):
            if isinstance(det.get(k), str):
                det[k] = mask_values(det[k])
    return res


# ============================================================================ CLI
_DUR = re.compile(r"^\s*(\d+)\s*([smh]?)\s*$")


def parse_duration(s: str) -> int:
    m = _DUR.match(s or "")
    if not m:
        raise ValueError(f"thời hạn không hợp lệ: {s!r} (vd 30m, 2h, 90s)")
    return int(m.group(1)) * {"": 60, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def add_parsers(sub: Any, common: argparse.ArgumentParser) -> None:
    lg = sub.add_parser("logs", help="log hệ thống: xem/lọc log (mọi tiến trình, cả file đã xoay), đổi level lúc chạy — chỉ người",
                        parents=[common], description="Không lệnh con: lọc log theo trường JSON. `logs level …`: xem/đổi level.")
    lg.add_argument("--since", help="từ (2h, 30m, 1d hoặc ISO 8601)")
    lg.add_argument("--until", help="tới (như --since)")
    lg.add_argument("--level", help="mức tối thiểu: debug|info|warning|error|critical")
    lg.add_argument("--logger", help="logger (khớp tiền tố, vd cfharness.guard)")
    lg.add_argument("--req", help="mã yêu cầu r_…")
    lg.add_argument("--run", help="run_id")
    lg.add_argument("--agent", help="agent_id")
    lg.add_argument("--task", help="task_id")
    lg.add_argument("--proc", help="tiến trình: daemon|web|mcp|cli")
    lg.add_argument("-n", "--lines", type=int, default=200, help="số dòng mới nhất (mặc định 200)")
    lg.add_argument("-f", "--follow", action="store_true", help="theo dõi dòng mới (đọc file cục bộ, Ctrl-C để dừng)")
    lg.add_argument("--local", action="store_true", help="đọc file trực tiếp, không qua daemon")
    lg.set_defaults(fn=cmd_query)
    ls = lg.add_subparsers(dest="logs_cmd")
    lv = ls.add_parser("level", help="xem level log, hoặc đặt level TẠM cho một logger (tự hết hạn)", parents=[common])
    lv.add_argument("logger", nargs="?", help="vd cfharness, cfharness.guard, cfharness.runs, asyncio")
    lv.add_argument("level", nargs="?", help="debug|info|warning|error|critical|clear")
    lv.add_argument("--for", dest="for_", default="30m", help="thời hạn (mặc định 30m, tối đa 24h)")
    lv.set_defaults(fn=cmd_level)

    tr = sub.add_parser("trace", help="dòng thời gian của một yêu cầu (r_…), run hoặc task: audit + log + event + guard + approval — chỉ người",
                        parents=[common])
    tr.add_argument("id", help="mã yêu cầu r_<26 ký tự>, run_id hoặc task_id")
    tr.add_argument("--limit", type=int, default=1000, help="số mục tối đa (mặc định 1000)")
    tr.add_argument("--no-logs", action="store_true", help="bỏ dòng log (nhanh hơn)")
    tr.add_argument("--local", action="store_true", help="đọc hub.db + log trực tiếp, không qua daemon")
    tr.set_defaults(fn=cmd_trace)


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


def _rpc_or_local(ctx: Any, method: str, params: dict[str, Any], local: Any) -> dict[str, Any]:
    """Gọi daemon (không tự khởi động); daemon dừng / daemon cũ chưa có method → chạy cục bộ (gỡ lỗi khi daemon chết)."""
    from .rpc import METHOD_NOT_FOUND, DaemonUnavailable, RpcError

    if not getattr(ctx.args, "local", False):
        try:
            return ctx.client(autostart=False).call(method, params)
        except DaemonUnavailable:
            print("(daemon không chạy — đọc file trực tiếp)", file=sys.stderr)
        except RpcError as e:
            if e.code != METHOD_NOT_FOUND:
                raise
    return local()


def _level_tag(lv: Any) -> str:
    return str(lv or "").upper()[:5].ljust(5)


def line_text(d: dict[str, Any]) -> str:
    """Một dòng log cho terminal — chữ log là DỮ LIỆU: qua `termsafe.one_line` (bỏ ANSI/điều khiển/bidi)."""
    from .termsafe import one_line

    ts = str(d.get("ts") or "")[:23].replace("T", " ")
    ids = " ".join(one_line(str(d[k]), 80) for k in ("req_id", "run_id", "agent_id") if d.get(k))
    out = (f"{ts} {_level_tag(d.get('level'))} {one_line(str(d.get('proc') or '-'), 6):<6} {one_line(str(d.get('logger') or ''), 80)}: "
           f"{one_line(str(d.get('msg') or ''), 2000)}")
    if ids:
        out += f"  [{ids}]"
    if d.get("exc"):
        out += "\n    " + one_line(str(d["exc"]).strip().splitlines()[-1], 300)
    return out


def cmd_query(ctx: Any) -> int:
    from .log_query import follow

    a = ctx.args
    p = {k: getattr(a, k, None) for k in QUERY_KEYS}
    p = {k: v for k, v in p.items() if v not in (None, "")}
    p["limit"] = int(a.lines or 200)
    try:
        filters, limit, max_bytes = query_args(p)
    except HubError as e:
        raise SystemExit(f"lỗi: {e.message}") from None
    if a.follow:
        try:
            for d in follow(ctx.paths.logs_dir, filters):
                d = _mask_rows([d])[0]
                print(json.dumps(d, ensure_ascii=False) if ctx.json else line_text(d), flush=True)
        except KeyboardInterrupt:
            return 0
        return 0
    res = _rpc_or_local(ctx, "logs.query", p, lambda: run_query(ctx.paths.logs_dir, filters, limit, max_bytes))
    rows = res.get("rows") or []
    lines = [line_text(d) for d in rows] or [f"(không có dòng log khớp trong {ctx.paths.logs_dir})"]
    if res.get("truncated"):
        lines.append("! chạm trần byte quét — dữ liệu cũ hơn không được đọc (thu hẹp --since)")
    if res.get("more"):
        lines.append(f"! còn dòng khớp cũ hơn — tăng -n (hiện {len(rows)})")
    ctx.out(res, "\n".join(lines))
    return 0


def cmd_trace(ctx: Any) -> int:
    from .trace_view import text

    a = ctx.args
    params = {"id": a.id, "limit": a.limit, "logs": not a.no_logs}

    def local() -> dict[str, Any]:
        try:
            return run_trace(ctx.paths.db, ctx.paths.logs_dir, a.id, a.limit, not a.no_logs)
        except ValueError as e:
            raise SystemExit(f"lỗi: {e}") from None

    res = _rpc_or_local(ctx, "trace.get", params, local)
    ctx.out(res, text(res))
    return 0 if res.get("found") else 1


__all__ = ["register_rpc", "add_parsers", "MUTATING_LOGS", "parse_duration", "query_args", "run_query", "run_trace", "line_text"]
