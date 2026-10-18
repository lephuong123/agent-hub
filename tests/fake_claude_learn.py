#!/usr/bin/env python3
"""`claude` giả cho test vòng học M4-A (ClaudeCliRuntime thật: subprocess, stdin stream-json, MCP stdio thật). Không gọi model.

- Ghi "transcript" từng session vào `$FAKE_CLAUDE_PROJECTS/<session>.jsonl` (như ~/.claude/projects): run thường append;
  `--resume X --fork-session --session-id Y` CHÉP X sang Y rồi append vào Y — X không đổi (kiểm "review không chạm session gốc").
- Chi phí luỹ kế theo session (như CLI thật, spike A-03; fork kế thừa luỹ kế của session gốc — đã kiểm với CLI 2.1.302).
- Run thường: nếu system prompt có chỉ mục skill và MCP hub-self → `skill_view` từng skill; skill nói "dd/mm/yyyy" → trả ngày
  dạng 18/10/2026, ngược lại 2026-10-18 (lỗi mà người sửa).
- Review fork: chạy kế hoạch tool trong `$FAKE_REVIEW_PLAN` (JSON list {tool, args}) qua MCP hub-self thật; ghi kết quả
  từng lời gọi vào `$FAKE_CLAUDE_PROJECTS/review-results.jsonl`.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("2.1.302 (Claude Code)")
    sys.exit(0)


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default


proj = Path(os.environ.get("FAKE_CLAUDE_PROJECTS", "/tmp/fake-claude-projects"))
proj.mkdir(parents=True, exist_ok=True)
resume = opt("--resume")
fork = "--fork-session" in args
sid = opt("--session-id") if (not resume or fork) else resume
first = json.loads(sys.stdin.readline())
prompt = first["message"]["content"]
system = opt("--append-system-prompt") or opt("--system-prompt") or ""
mcp_cfg = json.loads(Path(opt("--mcp-config")).read_text()) if opt("--mcp-config") else {"mcpServers": {}}
tools = (opt("--tools") or "Bash,Read,Write,Edit").split(",") if "--tools" in args else ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
mcp_names = list(mcp_cfg.get("mcpServers", {}))
costs_f = proj / "costs.json"
costs = json.loads(costs_f.read_text()) if costs_f.exists() else {}


def out(o):
    o.setdefault("session_id", sid)
    print(json.dumps(o), flush=True)


# transcript: fork chép bản gốc sang session mới (bản gốc KHÔNG đổi)
tr = proj / f"{sid}.jsonl"
if fork and resume:
    src = proj / f"{resume}.jsonl"
    if src.exists():
        shutil.copyfile(src, tr)
with open(tr, "a") as f:
    f.write(json.dumps({"type": "user", "content": prompt[:500]}) + "\n")

out({"type": "system", "subtype": "init", "model": opt("--model") or "claude-haiku-5-5", "cwd": os.getcwd(), "tools": tools + [f"mcp__{n}__x" for n in mcp_names],
     "mcp_servers": [{"name": n, "status": "connected"} for n in mcp_names], "plugins": [], "memory_paths": None, "claude_code_version": "2.1.302",
     "permissionMode": opt("--permission-mode") or "default", "argv": args})


class Mcp:
    def __init__(self, entry):
        env = {**os.environ, **(entry.get("env") or {})}
        self.p = subprocess.Popen([entry["command"], *entry.get("args", [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        self.n = 0
        self.req("initialize", {"protocolVersion": "2025-06-18"})

    def req(self, method, params=None):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("mcp closed")
            m = json.loads(line)
            if m.get("id") == self.n:
                return m

    def call(self, name, a):
        r = self.req("tools/call", {"name": name, "arguments": a})
        res = r.get("result") or {}
        text = (res.get("content") or [{}])[0].get("text", "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = text
        return bool(res.get("isError")), data

    def close(self):
        self.p.stdin.close()
        self.p.wait(10)


n_tool = 0


def tool_event(name, a, is_error, data):
    global n_tool
    n_tool += 1
    tid = f"toolu_{n_tool}"
    out({"type": "assistant", "message": {"id": f"m{n_tool}", "content": [{"type": "tool_use", "id": tid, "name": name, "input": a}]}})
    out({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "is_error": is_error, "content": json.dumps(data)[:500]}]}})


text = "ok"
hub = mcp_cfg.get("mcpServers", {}).get("hub-self")
if fork:
    plan = json.loads(os.environ.get("FAKE_REVIEW_PLAN") or "[]")
    results = []
    if hub:
        m = Mcp(hub)
        for step in plan:
            err, data = m.call(step["tool"], step.get("args") or {})
            tool_event(f"mcp__hub-self__{step['tool']}", step.get("args") or {}, err, data)
            results.append({"tool": step["tool"], "error": err, "data": data})
        m.close()
    with open(proj / "review-results.jsonl", "a") as f:
        f.write(json.dumps({"session": sid, "results": results}) + "\n")
    text = "Review done."
else:
    fmt = "2026-10-18"
    if hub and "# Skills index" in system:
        m = Mcp(hub)
        names = [ln[2:].split(":")[0] for ln in system.split("# Skills index", 1)[1].splitlines() if ln.startswith("- ")]
        for nm in names:
            err, data = m.call("skill_view", {"name": nm})
            tool_event("mcp__hub-self__skill_view", {"name": nm}, err, data)
            if not err and "dd/mm/yyyy" in str(data.get("content", "")):
                fmt = "18/10/2026"
        m.close()
    text = f"Status {fmt}: OK"
with open(tr, "a") as f:
    f.write(json.dumps({"type": "assistant", "content": text}) + "\n")
out({"type": "assistant", "message": {"id": "mfinal", "content": [{"type": "text", "text": text}]}})
base = costs.get(resume, 0.0) if resume else 0.0
total = base + 0.002
costs[sid] = total
costs_f.write_text(json.dumps(costs))
out({"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": total, "result": text,
     "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 300, "cache_creation_input_tokens": 50},
     "modelUsage": {"claude-haiku-5-5": {"costBasis": "list"}}, "terminal_reason": "completed", "duration_ms": 5, "num_turns": 1})
for _ in sys.stdin:
    pass
