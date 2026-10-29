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
if prompt.startswith("netask:"):
    # CLI 2.1.303–2.1.312: Bash trong sandbox mở kết nối tới host NGOÀI allowlist → `control_request can_use_tool
    # SandboxNetworkAccess` trên stdout, CHỜ `control_response` trên stdin (fixture thật tests/fixtures/claude-2.1.311/).
    # Kèm `tool_progress` (heartbeat) + `system/vcs_state_changed` như CLI 2.1.311+. Mỗi câu trả lời → một tin assistant
    # `net:<host>:<behavior>` và (nếu có FAKE_CLAUDE_CONTROL_LOG) ghi nguyên dòng control_response nhận được.
    import os
    import select

    hosts = prompt.split(":", 1)[1].split()[0].split(",")
    wait_s = float(os.environ.get("FAKE_CLAUDE_CONTROL_WAIT_S", "20"))
    log_path = os.environ.get("FAKE_CLAUDE_CONTROL_LOG")
    out({"type": "assistant", "message": {"id": "m2", "content": [{"type": "tool_use", "id": "toolu_net", "name": "Bash",
                                                                      "input": {"command": "curl -s https://" + hosts[0]}}]}})
    for i, host in enumerate(hosts):
        rid = f"req-{i}-{host}"
        out({"type": "control_request", "request_id": rid, "request": {
            "subtype": "can_use_tool", "tool_name": "SandboxNetworkAccess", "display_name": "SandboxNetworkAccess", "input": {"host": host},
            "permission_suggestions": [{"type": "addRules", "rules": [{"toolName": "WebFetch", "ruleContent": f"domain:{host}"}],
                                        "behavior": "allow", "destination": "session"}],
            "tool_use_id": f"c-{i}", "description": f"Allow network connection to {host}?"}})
        out({"type": "tool_progress", "tool_use_id": f"toolu_net-heartbeat-{i}", "tool_name": "Bash", "parent_tool_use_id": "toolu_net",
             "elapsed_time_seconds": 30 * (i + 1), "heartbeat": True})
        behavior = "timeout"
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            ready, _, _ = select.select([sys.stdin], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            line = sys.stdin.readline()
            if not line:
                behavior = "stdin_closed"
                break
            msg = json.loads(line)
            if msg.get("type") != "control_response":
                continue
            if log_path:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(line)
            r = msg.get("response") or {}
            if r.get("request_id") != rid:
                continue
            behavior = (r.get("response") or {}).get("behavior") if r.get("subtype") == "success" else "error"
            break
        out({"type": "assistant", "message": {"id": f"n{i}", "content": [{"type": "text", "text": f"net:{host}:{behavior}"}]}})
    out({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_net", "content": "done"}]}})
    out({"type": "system", "subtype": "vcs_state_changed", "kind": "push", "branch": "main", "cwd": "."})
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
