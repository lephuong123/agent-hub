#!/usr/bin/env python3
"""`claude` giả cho test ClaudeCliRuntime thật (subprocess, stdin stream-json, tín hiệu). Không gọi model."""
import json
import signal
import sys
import time

# như `claude` thật (node): stdin/stdout UTF-8 — Windows mặc định cp1252 làm hỏng prompt tiếng Việt
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.302 (Claude Code)")
    sys.exit(0)
sid = args[args.index("--session-id") + 1] if "--session-id" in args else "s"
first = json.loads(sys.stdin.readline())
prompt = first["message"]["content"]


def out(o):
    o.setdefault("session_id", sid)
    print(json.dumps(o), flush=True)


def result(subtype="success", cost=0.001, reason="completed"):
    out({"type": "result", "subtype": subtype, "is_error": subtype != "success", "total_cost_usd": cost,
         "usage": {"input_tokens": 1, "output_tokens": 2}, "modelUsage": {"claude-haiku-5-5": {"costBasis": "list"}},
         "result": "ok" if subtype == "success" else None, "terminal_reason": reason, "duration_ms": 5, "num_turns": 1})


def on_int(*_):
    result("error_during_execution", 0.0005, "aborted_tools")
    sys.exit(0)


signal.signal(signal.SIGINT, on_int)
out({"type": "system", "subtype": "init", "model": "claude-haiku-5-5", "cwd": ".", "tools": [], "mcp_servers": [],
     "plugins": [], "memory_paths": None, "claude_code_version": "2.1.302", "argv": args})
out({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": f"echo: {prompt}"}]}})
if "hang" in prompt:
    for line in sys.stdin:  # chờ control_request interrupt
        msg = json.loads(line)
        if msg.get("type") == "control_request":
            out({"type": "control_response", "response": {"subtype": "success", "request_id": msg["request_id"]}})
            on_int()
    time.sleep(60)
result()
# stream-json: chỉ thoát khi stdin đóng
for _ in sys.stdin:
    pass
