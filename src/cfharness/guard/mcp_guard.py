"""MCP stdio nội bộ `hub-guard` cho `--permission-prompt-tool mcp__hub-guard__approve` (A-01a, FR-RT-09).

Lớp thứ hai sau hook: CLI chỉ gọi tool này khi nó định HỎI quyền (vd hook không chạy → rơi về luật thường).
Tool `approve(tool_name, input, tool_use_id?)` hỏi daemon (`guard.check`, channel=permtool) — cùng bộ phân loại,
cùng ApprovalRequest — và trả ĐÚNG MỘT text block JSON:
`{"behavior":"allow","updatedInput":{…}}` | `{"behavior":"deny","message":"…"}` (không `structuredContent`,
spike: CLI coi kết quả có structuredContent là không hợp lệ). Mọi lỗi → deny (fail-closed).
Tự cài giao thức MCP tối thiểu (JSON-RPC theo dòng qua stdio) để không phụ thuộc thư viện `mcp`.
"""

from __future__ import annotations

import json
import sys
import threading
import time

from ..oscompat import utf8_stdio
from .hook import _args, rpc_call

PROTOCOL_DEFAULT = "2025-06-18"
TOOL = {
    "name": "approve",
    "description": "cfharness internal permission bridge (not for direct use).",
    "inputSchema": {
        "type": "object",
        "properties": {"tool_name": {"type": "string"}, "input": {"type": "object"}, "tool_use_id": {"type": "string"}},
        "required": ["tool_name", "input"],
    },
}

_out_lock = threading.Lock()


def _send(msg: dict) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def decide(args: dict, run_id: str, sock: str, timeout: float) -> dict:
    tool_name = str(args.get("tool_name") or "")
    tool_input = args.get("input") if isinstance(args.get("input"), dict) else {}
    try:
        if not tool_name:
            raise ValueError("thiếu tool_name")
        t0 = time.monotonic()
        res = rpc_call(
            sock,
            "guard.check",
            {
                "run_id": run_id,
                "channel": "permtool",
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_use_id": args.get("tool_use_id"),
                "max_wait_s": max(1.0, timeout - 10 - (time.monotonic() - t0)),
            },
            timeout,
        )
        if res.get("decision") == "allow":
            return {"behavior": "allow", "updatedInput": tool_input}
        return {"behavior": "deny", "message": str(res.get("reason") or "denied by hub guard")}
    except BaseException as e:
        return {"behavior": "deny", "message": f"hub guard unavailable — blocked (fail-closed): {type(e).__name__}: {str(e)[:200]}"}


def handle(msg: dict, run_id: str, sock: str, timeout: float) -> None:
    mid = msg.get("id")
    method = msg.get("method")
    if mid is None:
        return  # notification
    try:
        if method == "initialize":
            pv = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_DEFAULT
            _send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": pv, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "hub-guard", "version": "1"}}})
        elif method == "ping":
            _send({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL]}})
        elif method == "tools/call":
            p = msg.get("params") or {}
            if p.get("name") != "approve":
                _send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps({"behavior": "deny", "message": "unknown tool"})}], "isError": True}})
                return
            out = decide(p.get("arguments") or {}, run_id, sock, timeout)
            _send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}]}})
        elif method in ("resources/list", "prompts/list"):
            _send({"jsonrpc": "2.0", "id": mid, "result": {method.split("/")[0]: []}})
        else:
            _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
    except BaseException as e:  # pragma: no cover
        _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": str(e)[:200]}})


def main() -> int:
    utf8_stdio()
    a = _args(sys.argv[1:])
    run_id, sock, timeout = a.get("run", ""), a.get("socket", ""), float(a.get("timeout") or 870)
    threads: list[threading.Thread] = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if isinstance(msg, dict) and msg.get("method") == "tools/call":
            t = threading.Thread(target=handle, args=(msg, run_id, sock, timeout), daemon=True)
            t.start()
            threads.append(t)
        elif isinstance(msg, dict):
            handle(msg, run_id, sock, timeout)
    for t in threads:  # stdin đóng: trả nốt các lời gọi đang chờ
        t.join(timeout + 5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
