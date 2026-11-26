"""Spike X-07: `codex app-server` (JSON-RPC qua stdio) làm kênh duyệt tool động cho guard của hub.

Dùng: python appserver_probe.py <out.jsonl> <policy> "<prompt>" [tuỳ chọn]
policy: decline-all | accept-all | decline-first (decline lần duyệt đầu, accept các lần sau)
tuỳ chọn (env):
  APPROVAL   approvalPolicy của thread (mặc định "untrusted"; on-request | never | untrusted)
  SANDBOX    sandbox của thread (mặc định "read-only")
  HOLD       giữ chờ N giây trước khi trả lời yêu cầu duyệt đầu tiên
  STEER      "giây:chữ" — gửi turn/steer sau N giây
  INTERRUPT  N — gửi turn/interrupt sau N giây
  MODEL      model (mặc định gpt-5.6-luna), EFFORT (mặc định low)
  MCP_JSON   JSON dict mcp_servers đưa vào thread/start.config
Ghi mọi message hai chiều vào out.jsonl.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

CODEX = os.path.join(os.environ["LOCALAPPDATA"], "Volta", "bin", "codex.cmd")


async def main() -> None:
    out_path, policy, prompt = sys.argv[1], sys.argv[2], sys.argv[3]
    log = open(out_path, "w", encoding="utf-8")
    t0 = time.time()
    hold = float(os.environ.get("HOLD", "0"))
    n_approvals = 0
    text: list[str] = []

    def rec(direction: str, msg: dict) -> None:
        log.write(json.dumps({"t": round(time.time() - t0, 2), "dir": direction, "msg": msg}, ensure_ascii=False) + "\n")
        log.flush()

    proc = await asyncio.create_subprocess_exec(
        "cmd", "/c", CODEX, "app-server", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
    nid = 0
    pending: dict[int, asyncio.Future] = {}
    turn_done = asyncio.Event()
    turn_id: dict[str, str] = {}

    async def send(msg: dict) -> None:
        rec("out", msg)
        proc.stdin.write((json.dumps(msg) + "\n").encode())
        await proc.stdin.drain()

    async def request(method: str, params: dict) -> dict:
        nonlocal nid
        nid += 1
        fut = asyncio.get_running_loop().create_future()
        pending[nid] = fut
        await send({"jsonrpc": "2.0", "id": nid, "method": method, "params": params})
        return await fut

    def decide() -> str:
        if policy == "accept-all":
            return "accept"
        if policy == "decline-all":
            return "decline"
        if policy == "decline-first":
            return "decline" if n_approvals == 1 else "accept"
        return "decline"

    async def answer(m: dict) -> None:
        nonlocal n_approvals
        n_approvals += 1
        if hold and n_approvals == 1:
            print(f"holding approval {hold}s ...")
            await asyncio.sleep(hold)
        method = m["method"]
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            d = decide()
            print(f"approval #{n_approvals} {method} -> {d}")
            await send({"jsonrpc": "2.0", "id": m["id"], "result": {"decision": d}})
        elif method in ("execCommandApproval", "applyPatchApproval"):
            d = "approved" if decide() == "accept" else "denied"
            await send({"jsonrpc": "2.0", "id": m["id"], "result": {"decision": d}})
        elif method == "mcpServer/elicitation/request":
            act = "accept" if decide() == "accept" else "decline"
            print(f"approval #{n_approvals} {method} -> {act}")
            await send({"jsonrpc": "2.0", "id": m["id"], "result": {"action": act, "content": {} if act == "accept" else None}})
        else:
            print("unhandled server request", method)
            await send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "not supported by probe"}})

    async def reader() -> None:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                rec("in-raw", {"line": line.decode(errors="replace")[:2000]})
                continue
            rec("in", msg)
            if "id" in msg and "method" not in msg:
                fut = pending.pop(msg["id"], None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif "id" in msg and "method" in msg:
                asyncio.create_task(answer(msg))
            else:
                meth = msg.get("method", "")
                p = msg.get("params", {})
                if meth == "turn/started":
                    turn_id["id"] = p.get("turn", {}).get("id") or p.get("turnId")
                elif meth == "item/agentMessage/delta":
                    text.append(p.get("delta", ""))
                elif meth == "turn/completed":
                    turn_done.set()

    async def stderr_drain() -> None:
        data = await proc.stderr.read()
        rec("stderr", {"text": data.decode(errors="replace")[-4000:]})

    rt = asyncio.create_task(reader())
    st = asyncio.create_task(stderr_drain())
    init = await request("initialize", {"clientInfo": {"name": "cfharness-spike", "version": "0.0.1"}})
    print("initialize:", json.dumps(init.get("result", init), ensure_ascii=False)[:400])
    await send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
    cfg = {"model_reasoning_effort": os.environ.get("EFFORT", "low")}
    if os.environ.get("MCP_JSON"):
        cfg["mcp_servers"] = json.loads(os.environ["MCP_JSON"])
    ts = await request("thread/start", {
        "cwd": os.getcwd(), "model": os.environ.get("MODEL", "gpt-5.6-luna"),
        "approvalPolicy": os.environ.get("APPROVAL", "untrusted"),
        "sandbox": os.environ.get("SANDBOX", "read-only"),
        "developerInstructions": "You are being driven by a test harness. Use tools as instructed.",
        "config": cfg, "ephemeral": True,
    })
    print("thread/start:", json.dumps(ts.get("result", ts), ensure_ascii=False)[:500])
    thread_id = ts["result"]["thread"]["id"]
    tr = await request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]})
    print("turn/start:", json.dumps(tr.get("result", tr), ensure_ascii=False)[:300])
    turn_id.setdefault("id", (tr.get("result") or {}).get("turn", {}).get("id"))

    async def side() -> None:
        if os.environ.get("STEER"):
            sec, msg = os.environ["STEER"].split(":", 1)
            await asyncio.sleep(float(sec))
            r = await request("turn/steer", {"threadId": thread_id, "expectedTurnId": turn_id["id"],
                                             "input": [{"type": "text", "text": msg}]})
            print("turn/steer:", json.dumps(r.get("result", r.get("error")), ensure_ascii=False)[:300])
        if os.environ.get("INTERRUPT"):
            await asyncio.sleep(float(os.environ["INTERRUPT"]))
            t = time.time()
            r = await request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id["id"]})
            print("turn/interrupt:", json.dumps(r.get("result", r.get("error")), ensure_ascii=False)[:300])
            await turn_done.wait()
            print("turn completed after interrupt in", round(time.time() - t, 1), "s")

    sd = asyncio.create_task(side())
    try:
        await asyncio.wait_for(turn_done.wait(), 600)
    except asyncio.TimeoutError:
        print("TURN TIMEOUT")
    await asyncio.gather(sd, return_exceptions=True)
    print("assistant text:", "".join(text)[-600:])
    proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), 15)
    except asyncio.TimeoutError:
        proc.kill()
    await asyncio.gather(rt, st, return_exceptions=True)
    print("elapsed", round(time.time() - t0, 1), "s, approvals:", n_approvals)


asyncio.run(main())
