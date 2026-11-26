#!/usr/bin/env python3
"""`codex` giả (`codex app-server`, JSON-RPC stdio) cho test `CodexRuntime` — dạng message đo thật ở spike X-07
(docs/spikes/codex-grok/codex/fixtures/x07*.jsonl). Không gọi model.

Ghi nhật ký vào `$FAKE_CODEX_LOG` (argv, request nhận được, quyết định client trả). Kịch bản theo prompt:
- mặc định: lệnh `echo hi` cần duyệt (requestApproval) → accept: completed "hi" / decline: status declined.
- chứa "file": thêm fileChange (add notes.md) cần duyệt.
- chứa "mcp": thêm tool MCP `hub-self.task_list` qua `mcpServer/elicitation/request`.
- chứa "slow": chờ `turn/interrupt` (tối đa 10 s) → turn/completed status interrupted.
Cuối lượt: agentMessage "Done." + tokenUsage + turn/completed completed.
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
    print("codex-cli 0.172.0")
    sys.exit(0)

LOG = os.environ.get("FAKE_CODEX_LOG")


def log(kind, **kw):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": kind, **kw}, ensure_ascii=False) + "\n")


log("argv", argv=sys.argv[1:])
inbox: "queue.Queue[dict]" = queue.Queue()


def reader():
    for line in sys.stdin:
        if line.strip():
            inbox.put(json.loads(line))
    inbox.put({"_eof": True})


threading.Thread(target=reader, daemon=True).start()


def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def notify(method, params):
    send({"method": method, "params": params})


pending: list = []
next_req = [0]
interrupted = [False]


def wait_response(rid, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            m = inbox.get(timeout=0.1)
        except queue.Empty:
            continue
        if m.get("id") == rid and "method" not in m:
            return m
        if handle_inline(m):
            continue
        pending.append(m)
    return None


def server_request(method, params):
    rid = next_req[0]
    next_req[0] += 1
    send({"method": method, "id": rid, "params": params})
    r = wait_response(rid) or {}
    log("decision", method=method, result=r.get("result"))
    return r.get("result") or {}


def handle_inline(m):
    """Request client gửi GIỮA lượt (steer/interrupt) — trả lời ngay."""
    meth = m.get("method")
    if meth == "turn/steer":
        log("request", method=meth, params=m.get("params"))
        send({"id": m["id"], "result": {"turnId": m["params"].get("expectedTurnId")}})
        return True
    if meth == "turn/interrupt":
        log("request", method=meth, params=m.get("params"))
        interrupted[0] = True
        send({"id": m["id"], "result": {}})
        return True
    return False


THREAD = "thread-1"


def run_turn(text, turn_id):
    log("prompt", text=text)
    notify("turn/started", {"threadId": THREAD, "turn": {"id": turn_id, "status": "inProgress"}})
    if "slow" in text:
        notify("item/started", {"item": {"type": "commandExecution", "id": "exec-slow", "command": "sleep 60",
                                         "commandActions": [{"type": "unknown", "command": "sleep 60"}], "status": "inProgress"}})
        end = time.time() + 10
        while time.time() < end and not interrupted[0]:
            try:
                m = inbox.get(timeout=0.1)
            except queue.Empty:
                continue
            if not handle_inline(m):
                pending.append(m)
        notify("turn/completed", {"threadId": THREAD, "turn": {"id": turn_id, "status": "interrupted", "error": None}})
        return
    item = {"type": "commandExecution", "id": "exec-1", "command": "\"powershell.exe\" -Command 'echo hi'",
            "commandActions": [{"type": "unknown", "command": "echo hi"}], "status": "inProgress"}
    notify("item/started", {"item": item, "threadId": THREAD, "turnId": turn_id})
    d = server_request("item/commandExecution/requestApproval", {"threadId": THREAD, "turnId": turn_id, "itemId": "exec-1",
                                                                 "command": item["command"], "commandActions": item["commandActions"]})
    ok = d.get("decision") == "accept"
    notify("item/completed", {"item": {**item, "status": "completed" if ok else "declined", "aggregatedOutput": "hi" if ok else None,
                                       "exitCode": 0 if ok else None}, "threadId": THREAD, "turnId": turn_id})
    if "file" in text:
        fc = {"type": "fileChange", "id": "patch-1", "changes": [{"path": "notes.md", "kind": {"type": "add"}, "diff": "alpha\n"}],
              "status": "inProgress"}
        notify("item/started", {"item": fc, "threadId": THREAD, "turnId": turn_id})
        d = server_request("item/fileChange/requestApproval", {"threadId": THREAD, "turnId": turn_id, "itemId": "patch-1",
                                                               "reason": None, "grantRoot": None})
        notify("item/completed", {"item": {**fc, "status": "completed" if d.get("decision") == "accept" else "declined"}})
    if "mcp" in text:
        mc = {"type": "mcpToolCall", "id": "mcp-1", "server": "hub-self", "tool": "task_list", "arguments": {"status": "open"},
              "status": "inProgress"}
        notify("item/started", {"item": mc})
        d = server_request("mcpServer/elicitation/request", {"threadId": THREAD, "turnId": turn_id, "serverName": "hub-self",
                                                             "mode": "form", "_meta": {"codex_approval_kind": "mcp_tool_call",
                                                                                       "tool_name": "task_list",
                                                                                       "tool_params": {"status": "open"}}})
        ok = d.get("action") == "accept"
        notify("item/completed", {"item": {**mc, "status": "completed" if ok else "failed",
                                           "result": {"content": [{"type": "text", "text": "[]"}]} if ok else None,
                                           "error": None if ok else {"message": "user cancelled MCP tool call"}}})
    notify("item/started", {"item": {"type": "agentMessage", "id": "msg-1", "text": ""}})
    notify("item/agentMessage/delta", {"itemId": "msg-1", "delta": "Done"})
    notify("item/agentMessage/delta", {"itemId": "msg-1", "delta": "."})
    notify("item/completed", {"item": {"type": "agentMessage", "id": "msg-1", "text": "Done."}})
    notify("thread/tokenUsage/updated", {"threadId": THREAD, "tokenUsage": {"total": {"inputTokens": 500, "cachedInputTokens": 200,
                                                                                      "outputTokens": 30, "cacheWriteInputTokens": 0}}})
    notify("account/rateLimits/updated", {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 3}}})
    notify("turn/completed", {"threadId": THREAD, "turn": {"id": turn_id, "status": "completed", "error": None}})


def handle(m):
    meth, mid, p = m.get("method"), m.get("id"), m.get("params") or {}
    if meth is None:
        return
    log("request", method=meth, params=p)
    if meth == "initialize":
        send({"id": mid, "result": {"userAgent": "fake", "platformOs": "windows"}})
    elif meth == "initialized":
        return
    elif meth == "account/read":
        logged_out = bool(os.environ.get("FAKE_CODEX_LOGGED_OUT"))
        send({"id": mid, "result": {"account": None if logged_out else {"type": "chatgpt", "email": "dev@example.com", "planType": "team",
                                                                       "workspaceName": "Acme"}}})
    elif meth == "account/rateLimits/read":
        send({"id": mid, "result": {"rateLimits": {"planType": "team", "primary": {"usedPercent": 12, "windowDurationMins": 300},
                                                   "secondary": {"usedPercent": 3, "windowDurationMins": 10080}}}})
    elif meth == "account/login/start":
        log("login", params=p)
        if p.get("type") == "chatgptDeviceCode":
            send({"id": mid, "result": {"type": "chatgptDeviceCode", "loginId": "L1", "userCode": "ABCD-EFGHI",
                                        "verificationUrl": "https://auth.openai.com/codex/device"}})
        else:
            send({"id": mid, "result": {"type": "chatgpt", "loginId": "L1", "authUrl": "https://auth.openai.com/oauth/authorize?x=1"}})
        if os.environ.get("FAKE_CODEX_LOGIN_OK"):
            notify("account/login/completed", {"loginId": "L1", "success": True})
    elif meth == "account/login/cancel":
        send({"id": mid, "result": {}})
    elif meth in ("thread/start", "thread/resume"):
        tid = p.get("threadId") or THREAD
        send({"id": mid, "result": {"thread": {"id": tid}}})
        notify("thread/started", {"thread": {"id": tid, "model": p.get("model") or "gpt-5.6-luna"}})
    elif meth == "turn/start":
        turn_id = "turn-1"
        send({"id": mid, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
        run_turn("".join(b.get("text", "") for b in p.get("input") or []), turn_id)
    elif handle_inline(m):
        return
    elif mid is not None:
        send({"id": mid, "error": {"code": -32601, "message": "not supported"}})


while True:
    m = pending.pop(0) if pending else inbox.get()
    if m.get("_eof"):
        break
    handle(m)
