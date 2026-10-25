#!/usr/bin/env python3
"""`claude` giả cho test M3 (subprocess thật): hỗ trợ --resume, --json-schema (structured_output), treo tới khi bị giết.

- prompt chứa "HANG" và KHÔNG có --resume → phát init + assistant rồi treo (test giết tiến trình bằng SIGKILL).
- có --json-schema → result.structured_output: kế hoạch từ env FAKE_PLAN (JSON) hoặc phán quyết review approve.
- cost luỹ kế theo session (giống CLI thật sau --resume).
"""
import json
import os
import sys
import time

# như `claude` thật (node): stdin/stdout UTF-8 — Windows mặc định cp1252 làm hỏng prompt tiếng Việt
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.302 (Claude Code)")
    sys.exit(0)
resume = args[args.index("--resume") + 1] if "--resume" in args else None
sid = resume or (args[args.index("--session-id") + 1] if "--session-id" in args else "s")
first = json.loads(sys.stdin.readline())
prompt = first["message"]["content"]


def out(o):
    o.setdefault("session_id", sid)
    print(json.dumps(o), flush=True)


out({"type": "system", "subtype": "init", "model": "claude-haiku-5-5", "cwd": os.getcwd(), "tools": [], "mcp_servers": [], "plugins": [],
     "memory_paths": None, "claude_code_version": "2.1.302"})
out({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "working"}]}})
if "HANG" in prompt and not resume:
    with open(os.environ.get("FAKE_PID_FILE", "/dev/null"), "w") as f:
        f.write(str(os.getpid()))
    time.sleep(120)
res = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.002 if resume else 0.001,
       "usage": {"input_tokens": 1, "output_tokens": 2}, "modelUsage": {"claude-haiku-5-5": {"costBasis": "list"}},
       "result": ("resumed and finished" if resume else "finished"), "terminal_reason": "completed", "duration_ms": 5, "num_turns": 1}
if "--json-schema" in args:
    if prompt.startswith("[cfharness plan"):
        res["structured_output"] = json.loads(os.environ.get("FAKE_PLAN") or '{"subtasks":[{"key":"a","title":"A","body":"do A"}]}')
    else:
        res["structured_output"] = {"verdict": "approve", "comment": "ok"}
out(res)
for _ in sys.stdin:
    pass
