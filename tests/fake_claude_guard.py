#!/usr/bin/env python3
"""`claude` giả có guard cho test luồng B (không gọi model).

Mô phỏng đúng các hành vi CLI đã kiểm ở spike M0:
- đọc `--settings` và chạy hook PreToolUse (lệnh shell, JSON vào stdin); exit 2 → chặn; exit≠0 khác → fail-OPEN;
  `permissionDecision` allow/deny;
- `--disallowedTools "Bash(prefix:*)"` chặn TĨNH và THẮNG hook allow;
- `--resume <sid>` giữ session; `total_cost_usd` LUỸ KẾ theo session (lưu ở $FAKE_STATE_DIR);
- "agent" ngoan: chạy từng dòng `BASH: <lệnh>` trong prompt; prompt resume của hub ("APPROVED … `lệnh`") → chạy đúng
  lệnh đó; bị deny với thông điệp tạm dừng → dừng lượt.
Mỗi lệnh thực sự chạy được ghi vào $FAKE_EXEC_LOG (kiểm "đúng một lần").
"""
import json
import os
import re
import signal
import subprocess
import sys

args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.302 (Claude Code)")
    sys.exit(0)


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default


def multi(name):
    if name not in args:
        return []
    out = []
    for a in args[args.index(name) + 1 :]:
        if a.startswith("--"):
            break
        out.append(a)
    return out


resume = opt("--resume")
sid = resume or opt("--session-id", "s")
state_dir = os.environ.get("FAKE_STATE_DIR") or os.getcwd()
cost_file = os.path.join(state_dir, f"fake-cost-{sid}.json")
prev_cost = json.load(open(cost_file))["total"] if os.path.exists(cost_file) else 0.0
step_cost = float(os.environ.get("FAKE_STEP_COST", "0.001"))
settings = json.load(open(opt("--settings"))) if opt("--settings") else {}
hooks = [h for m in (settings.get("hooks") or {}).get("PreToolUse") or [] for h in m.get("hooks") or []]
disallowed = multi("--disallowedTools")
denials = []
first = json.loads(sys.stdin.readline())
prompt = first["message"]["content"]
n_tool = 0


def out(o):
    o.setdefault("session_id", sid)
    print(json.dumps(o), flush=True)


def result(subtype="success", reason="completed"):
    total = round(prev_cost + step_cost, 8)
    json.dump({"total": total}, open(cost_file, "w"))
    out({"type": "result", "subtype": subtype, "is_error": subtype != "success", "total_cost_usd": total,
         "usage": {"input_tokens": 1, "output_tokens": 2}, "modelUsage": {"claude-haiku-5-5": {"costBasis": "list"}},
         "result": "done" if subtype == "success" else None, "terminal_reason": reason, "duration_ms": 5, "num_turns": 1,
         "permission_denials": denials})


def on_int(*_):
    result("error_during_execution", "aborted_tools")
    sys.exit(0)


signal.signal(signal.SIGINT, on_int)


def static_denied(cmd):
    parts = [cmd] + [p.strip() for p in re.split(r"&&|\|\||;|\|", cmd) if p.strip()]
    for pat in disallowed:
        m = re.match(r"^Bash\((.*):\*\)$", pat)
        if m and any(p.startswith(m.group(1)) for p in parts):
            return pat
    return None


def run_hook(tool_input, tool_use_id):
    data = {"session_id": sid, "cwd": os.getcwd(), "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": tool_input, "tool_use_id": tool_use_id, "permission_mode": "default"}
    for h in hooks:
        try:
            p = subprocess.run(h["command"], shell=True, input=json.dumps(data), capture_output=True, text=True, timeout=h.get("timeout", 60))
        except subprocess.TimeoutExpired:
            continue  # CLI: hết giờ = fail-open
        if p.returncode == 2:
            return "deny", p.stderr.strip() or "blocked (exit 2)"
        if p.returncode != 0:
            continue  # fail-open
        try:
            o = json.loads(p.stdout.strip().splitlines()[-1])["hookSpecificOutput"]
            if o["permissionDecision"] in ("deny", "allow"):
                return o["permissionDecision"], o.get("permissionDecisionReason", "")
        except Exception:
            continue
    return "allow", ""


def bash(cmd):
    global n_tool
    n_tool += 1
    tid = f"toolu_{n_tool}"
    ti = {"command": cmd, "description": "run"}
    out({"type": "assistant", "message": {"id": f"m{n_tool}", "content": [{"type": "tool_use", "id": tid, "name": "Bash", "input": ti}]}})
    decision, reason = run_hook(ti, tid)
    pat = static_denied(cmd)
    if pat:
        decision, reason = "deny", f"Permission to use Bash with command {cmd} has been denied (rule {pat})."
    if decision == "deny":
        denials.append({"tool_name": "Bash", "tool_use_id": tid, "tool_input": ti})
        out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": True, "content": reason}]}})
        return reason
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    log = os.environ.get("FAKE_EXEC_LOG")
    if log:
        with open(log, "a") as f:
            f.write(cmd + "\n")
    out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": p.returncode != 0, "content": (p.stdout + p.stderr)[:500]}]}})
    return None


out({"type": "system", "subtype": "init", "model": "claude-haiku-5-5", "cwd": os.getcwd(), "tools": ["Bash"],
     "mcp_servers": [{"name": "hub-guard", "status": "connected"}] if opt("--permission-prompt-tool") else [],
     "plugins": [], "memory_paths": None, "claude_code_version": "2.1.302", "argv": args})
cmds = [m.group(1).strip() for m in re.finditer(r"^BASH: (.+)$", prompt, re.M)]
cmds += [m.group(1) for m in re.finditer(r"APPROVED \(request \S+\): Bash: `([^`]+)`", prompt)]
for c in cmds:
    reason = bash(c)
    if reason and ("pausing this session" in reason or "restart this session" in reason or "End your turn" in reason):
        break
out({"type": "assistant", "message": {"id": "mz", "content": [{"type": "text", "text": "done"}]}})
result()
for _ in sys.stdin:  # stream-json: thoát khi stdin đóng
    pass
