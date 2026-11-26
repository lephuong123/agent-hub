#!/usr/bin/env python3
"""`grok` giả (Agent Client Protocol qua stdio) cho test `AcpRuntime` — mô phỏng dạng message đo thật ở spike K-07
(docs/spikes/codex-grok/grok/fixtures/k07*.jsonl). Không gọi model.

Ghi nhật ký JSON từng dòng vào `$FAKE_ACP_LOG`: argv, env cô lập, request nhận được, lựa chọn quyền client trả.
Kịch bản theo nội dung prompt:
- mặc định: thought + chữ → tool_call Bash `echo hi` → request_permission → (allow) completed / (reject) failed + lượt kết thúc
  `cancelled` (PermissionRejected) → chữ "Done." → stopReason end_turn kèm usage.
- chứa "mcp": thêm tool MCP `hub-self__task_list` (variant UseTool) cần quyền.
- chứa "slow": chờ `session/cancel` (tối đa 10 s) → stopReason cancelled.
"""
import json
import os
import queue
import sys
import threading
import time

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
if sys.argv[1:] == ["--version"]:
    print("grok 1.0.92 (fake)")
    sys.exit(0)

LOG = os.environ.get("FAKE_ACP_LOG")
STATE = os.environ.get("FAKE_ACP_STATE")  # file lưu phiên (session/load ở tiến trình mới)


def log(kind, **kw):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": kind, **kw}, ensure_ascii=False) + "\n")


log("argv", argv=sys.argv[1:], env={k: v for k, v in os.environ.items() if k.startswith("GROK_")})
inbox: "queue.Queue[dict]" = queue.Queue()


def reader():
    for line in sys.stdin:
        line = line.strip()
        if line:
            inbox.put(json.loads(line))
    inbox.put({"_eof": True})


threading.Thread(target=reader, daemon=True).start()


def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def update(sid, upd):
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": upd}})


pending: list = []  # message tới khi đang chờ thứ khác
next_req = [100]
cancelled = set()


def wait_response(rid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            m = inbox.get(timeout=0.1)
        except queue.Empty:
            continue
        if m.get("id") == rid and "method" not in m:
            return m
        if m.get("method") == "session/cancel":
            cancelled.add(m["params"]["sessionId"])
            continue
        pending.append(m)
    return None


def ask(sid, tool_call, options):
    next_req[0] += 1
    rid = next_req[0]
    send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission",
          "params": {"sessionId": sid, "toolCall": tool_call, "options": options}})
    r = wait_response(rid)
    choice = (((r or {}).get("result") or {}).get("outcome") or {}).get("optionId")
    log("permission", tool=tool_call.get("title"), choice=choice)
    return choice == "allow-once"


OPTS = [{"optionId": "always-allow", "name": "Always", "kind": "allow_always"},
        {"optionId": "allow-once", "name": "Yes", "kind": "allow_once"},
        {"optionId": "reject-once", "name": "No", "kind": "reject_once"},
        {"optionId": "reject-always", "name": "Never", "kind": "reject_always"}]
USAGE = {"inputTokens": 100, "outputTokens": 20, "cachedReadTokens": 40, "cacheCreationTokens": 0, "costUsdTicks": 1_000_000_000,
         "numTurns": 1}


def run_prompt(mid, sid, text):
    log("prompt", text=text)
    if "slow" in text:
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Starting slow work"}})
        end = time.time() + 10
        while time.time() < end and sid not in cancelled:
            try:
                m = inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            if m.get("method") == "session/cancel":
                log("request", method="session/cancel", params=m.get("params"))
                cancelled.add(m["params"]["sessionId"])
            else:
                pending.append(m)
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "cancelled", "_meta": {"usage": USAGE}}})
        return
    update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "thinking"}})
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Working"}})
    tc = {"toolCallId": "call-1", "kind": "execute", "title": "Execute `echo hi`",
          "rawInput": {"variant": "Bash", "command": "echo hi", "description": "say hi"}}
    update(sid, {"sessionUpdate": "tool_call", **tc})
    if not ask(sid, tc, OPTS):
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "call-1", "status": "failed",
                     "content": [{"type": "content", "content": {"type": "text", "text": "User rejected the execution"}}]})
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "cancelled",
                                                      "_meta": {"cancellationCategory": "PermissionRejected", "usage": USAGE}}})
        return
    update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "call-1", "status": "completed",
                 "content": [{"type": "content", "content": {"type": "text", "text": "hi"}}]})
    if "mcp" in text:
        tc2 = {"toolCallId": "call-2", "kind": "other", "title": "hub-self__task_list",
               "rawInput": {"variant": "UseTool", "tool_name": "hub-self__task_list", "tool_input": {"status": "open"}}}
        update(sid, {"sessionUpdate": "tool_call", **tc2})
        ok = ask(sid, tc2, OPTS[:3])
        update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "call-2", "status": "completed" if ok else "failed",
                     "rawOutput": {"type": "MCP", "output": {"OkayOutput": "[]"}} if ok else None})
    update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": " Done."}})
    send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn", "_meta": {"modelId": "grok-4.7", "usage": USAGE}}})


def handle(m):
    meth, mid, p = m.get("method"), m.get("id"), m.get("params") or {}
    if meth is None:
        return
    log("request", method=meth, params=p)
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}})
    elif meth == "session/new":
        sid = f"sess-{os.getpid()}"
        if STATE:
            with open(STATE, "w", encoding="utf-8") as f:
                f.write(sid)
        send({"jsonrpc": "2.0", "method": "_x.ai/models/update", "params": {"currentModelId": "grok-4.7"}})
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": sid}})
    elif meth == "session/load":
        known = open(STATE, encoding="utf-8").read().strip() if STATE and os.path.exists(STATE) else None
        if p.get("sessionId") != known:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32002, "message": "session not found"}})
            return
        update(p["sessionId"], {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "OLD PROMPT"}})
        update(p["sessionId"], {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "OLD ANSWER"}})
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif meth == "session/prompt":
        run_prompt(mid, p["sessionId"], "".join(b.get("text", "") for b in p.get("prompt") or []))
    elif meth == "session/cancel":
        cancelled.add(p.get("sessionId"))
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "not supported"}})


while True:
    m = pending.pop(0) if pending else inbox.get()
    if m.get("_eof"):
        break
    handle(m)
