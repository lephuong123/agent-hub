"""Probe một MCP server: kết nối, `tools/list`, lấy annotations (FR-MC-03, FR-MC-06). KHÔNG gọi tool nào.

Dùng SDK `mcp` (Client). Env của server stdio chỉ gồm baseline an toàn (mượn Hermes
`_SAFE_ENV_KEYS`) + `env` khai báo đã thay secret — không rò biến môi trường của daemon.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from typing import Any

import anyio

from ..redact import redact, redact_str
from .config import McpServerConfig
from .scan import scan_description

SAFE_ENV_KEYS = frozenset({"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "SHELL", "TMPDIR"})
DESCRIPTION_MAX = 1000
log = logging.getLogger("cfharness.mcp_client.probe")


def safe_env(extra: dict[str, str] | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in SAFE_ENV_KEYS or k.startswith("XDG_")}
    env.update(extra or {})
    return env


def _hint(cfg: McpServerConfig, err: BaseException) -> str:
    msg = f"{type(err).__name__}: {err}"
    if cfg.transport == "stdio":
        return f"không khởi động/kết nối được lệnh `{cfg.command}` — kiểm lệnh có trong PATH, chạy thử tay ({msg})"
    return f"không kết nối được {cfg.url} — kiểm URL (đường dẫn thường là /mcp), mạng, token/header ({msg})"


def _flatten_exc(err: BaseException) -> BaseException:
    while isinstance(err, BaseExceptionGroup) and err.exceptions:
        err = err.exceptions[0]
    return err


def tool_record(t: Any) -> dict[str, Any]:
    ann = getattr(t, "annotations", None)
    desc = (getattr(t, "description", None) or "")[:DESCRIPTION_MAX]
    title = getattr(t, "title", None) or (getattr(ann, "title", None) if ann else None)
    rec: dict[str, Any] = {
        "name": t.name,
        "title": title,
        "description": desc,
        "readOnlyHint": getattr(ann, "read_only_hint", None) if ann else None,
        "destructiveHint": getattr(ann, "destructive_hint", None) if ann else None,
        "idempotentHint": getattr(ann, "idempotent_hint", None) if ann else None,
        "openWorldHint": getattr(ann, "open_world_hint", None) if ann else None,
    }
    findings = scan_description(desc) + scan_description(title)
    if findings:
        rec["suspicious"] = findings
    return rec


async def probe_server(cfg: McpServerConfig, resolved: dict[str, Any], *, timeout_s: float | None = None) -> dict[str, Any]:
    """`resolved` = `cfg.claude_entry()` đã thay secret. Trả {ok, tools, elapsed_ms, server, error, hint}."""
    from mcp import Client

    timeout = float(timeout_s or cfg.timeout_s)
    t0 = time.monotonic()
    out: dict[str, Any] = {"ok": False, "tools": {}, "elapsed_ms": None, "server": None, "error": None, "hint": None}
    try:
        with anyio.fail_after(timeout):
            if cfg.transport == "stdio":
                from mcp.client.stdio import StdioServerParameters

                from mcp.client.stdio import stdio_client

                target: Any = StdioServerParameters(
                    command=resolved["command"], args=list(resolved.get("args") or []), env=safe_env(resolved.get("env"))
                )
                # H-12: stderr của server KHÔNG đổ thẳng vào stderr daemon (`logs/daemon.out`) — gom vào file tạm, che rồi mới dùng
                errlog = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
                try:
                    client = Client(stdio_client(target, errlog=errlog), read_timeout_seconds=timeout)
                    async with client as c:
                        res = await c.list_tools()
                        info = c.server_info
                finally:
                    out["stderr_tail"] = _read_errlog(errlog)
            else:
                from mcp.client.streamable_http import streamable_http_client
                from mcp.shared._httpx_utils import create_mcp_http_client

                if cfg.transport == "sse":
                    from mcp.client.sse import sse_client

                    transport = sse_client(resolved["url"], headers=resolved.get("headers") or None)
                    client = Client(transport, read_timeout_seconds=timeout)
                    async with client as c:
                        res = await c.list_tools()
                        info = c.server_info
                else:
                    async with create_mcp_http_client(headers=resolved.get("headers") or None) as http:
                        client = Client(streamable_http_client(resolved["url"], http_client=http), read_timeout_seconds=timeout)
                        async with client as c:
                            res = await c.list_tools()
                            info = c.server_info
        tools = {t.name: tool_record(t) for t in res.tools}
        out.update(ok=True, tools=tools, server=({"name": info.name, "version": info.version} if info else None))
    except BaseException as e:  # noqa: BLE001 — probe không được làm chết daemon
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        err = _flatten_exc(e)
        if isinstance(err, TimeoutError):
            out.update(error=f"quá thời gian {timeout:.0f}s", hint=_hint(cfg, err))
        else:
            out.update(error=f"{type(err).__name__}: {err}"[:500], hint=_hint(cfg, err))
    out["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    if not out.get("stderr_tail"):
        out.pop("stderr_tail", None)
    elif not out["ok"]:
        log.info("probe MCP %s lỗi; stderr (đã che): %s", cfg.name if hasattr(cfg, "name") else "?", out["stderr_tail"])
    return redact(out)  # H-12: lỗi/URL/stderr có thể chứa secret đã resolve → che theo giá trị + mẫu


def _read_errlog(f: Any, limit: int = 2000) -> str:
    try:
        f.seek(0)
        txt = f.read()[-limit:]
    except (OSError, ValueError):
        txt = ""
    finally:
        try:
            f.close()
        except OSError:  # pragma: no cover
            pass
    return redact_str(txt.strip())
