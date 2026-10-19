#!/usr/bin/env python3
"""`claude` giả cho test steer (FR-RT-06) với ClaudeCliRuntime thật — mô phỏng hành vi đã kiểm ở spike A-02:

- prompt chứa "midtool": phát tool_use (Bash sleep), trong lúc "tool chạy" đọc stdin; tin tới được phát lại
  (`isReplay`) NGAY SAU tool_result, rồi trả lời và MỘT `result` (cùng lượt).
- prompt chứa "late": phát tool_use, ngủ 0.6 s KHÔNG đọc stdin rồi phát `result`; sau đó mỗi tin user còn trong
  stdin → phát lại + một lượt mới + `result` thứ hai (spike "twoturns"). Thoát khi stdin đóng.
- prompt khác: trả lời một câu, `result`, thoát khi stdin đóng.
Không gọi model.
"""
import json
import select
import sys
import time

args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.302 (Claude Code)")
    sys.exit(0)
sid = args[args.index("--session-id") + 1] if "--session-id" in args else "s"
turns = 0


def out(o):
    o.setdefault("session_id", sid)
    print(json.dumps(o), flush=True)


def replay(text):
    out({"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None, "isReplay": True})


def result(text):
    global turns
    turns += 1
    out({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.001 * turns,
         "usage": {"input_tokens": 1, "output_tokens": 2}, "modelUsage": {"claude-haiku-5-5": {"costBasis": "list"}},
         "result": text, "terminal_reason": "completed", "duration_ms": 5, "num_turns": turns})


first = json.loads(sys.stdin.readline())

if sys.platform == "win32":  # select() chỉ dùng được với socket trên Windows → luồng đọc stdin + hàng đợi
    import queue
    import threading

    _q = queue.Queue()
    threading.Thread(target=lambda: ([_q.put(ln) for ln in sys.stdin], _q.put("")), daemon=True).start()

    def readline(timeout):
        try:
            return _q.get(timeout=timeout) or None
        except queue.Empty:
            return None

    def rest_lines():
        while True:
            ln = _q.get()
            if not ln:
                return
            yield ln
else:
    def readline(timeout):
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        return sys.stdin.readline() if r else None

    def rest_lines():
        return sys.stdin
prompt = first["message"]["content"]
out({"type": "system", "subtype": "init", "model": "claude-haiku-5-5", "cwd": ".", "tools": ["Bash"], "mcp_servers": [],
     "plugins": [], "memory_paths": None, "claude_code_version": "2.1.302"})
replay(prompt)
if "midtool" in prompt or "late" in prompt:
    out({"type": "assistant", "message": {"id": "m1", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "sleep 1"}}]}})
    steers = []
    if "midtool" in prompt:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not steers:
            line = readline(0.05)
            if line:
                steers.append(json.loads(line)["message"]["content"])
    else:
        time.sleep(0.6)
    out({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "done"}]}})
    for s in steers:
        replay(s)
    text = "steered: " + " | ".join(steers) if steers else "no steer"
    out({"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": text}]}})
    result(text)
else:
    out({"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": f"echo: {prompt}"}]}})
    result("ok")
# như CLI thật: tin tới sau `result` → lượt mới; thoát khi stdin đóng
for line in rest_lines():
    msg = json.loads(line)
    if msg.get("type") != "user":
        continue
    text = msg["message"]["content"]
    out({"type": "system", "subtype": "init", "model": "claude-haiku-5-5", "cwd": ".", "tools": ["Bash"], "mcp_servers": [],
         "plugins": [], "memory_paths": None, "claude_code_version": "2.1.302"})
    replay(text)
    out({"type": "assistant", "message": {"id": f"m{turns + 3}", "content": [{"type": "text", "text": f"late steer: {text}"}]}})
    result(f"late steer: {text}")
