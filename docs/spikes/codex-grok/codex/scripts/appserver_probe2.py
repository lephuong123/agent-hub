"""Spike X-09..X-12: `codex app-server` — sandbox mxc với lệnh đã duyệt, thread/resume ở tiến trình mới,
cô lập cấu hình người dùng, approvalPolicy granular, acceptForSession.

Dùng: python appserver_probe2.py <out.jsonl> "<prompt>" [tuỳ chọn env]
  DECISIONS  danh sách quyết định theo thứ tự, phân cách bằng dấu phẩy (accept|acceptForSession|decline|cancel);
             hết danh sách thì dùng phần tử cuối. Mặc định "accept".
  APPROVAL   approvalPolicy: chuỗi (untrusted|on-request|never) hoặc JSON (vd {"granular":{...}})
  SANDBOX    sandbox (read-only|workspace-write), mặc định read-only
  ROOT_ARGS  JSON list cờ đặt trước/sau `app-server` (vd ["-c","windows.sandbox=\"mxc\""])
  CFG_JSON   JSON dict gộp vào thread/start.config
  PRE        JSON list [method, params] gọi trước thread/start (vd [["windowsSandbox/readiness",{}],["config/read",{}]])
  RESUME     threadId — gọi thread/resume thay vì thread/start
  EPHEMERAL  "0" để lưu thread (mặc định ephemeral=true)
  TURN2      prompt thứ hai cùng thread sau khi lượt 1 xong
  POST       như PRE nhưng gọi sau thread/start (POST_WAIT giây chờ trước); NO_TURN=1 bỏ qua turn
  EXPERIMENTAL=1  initialize.capabilities.experimentalApi=true (bắt buộc cho approvalPolicy granular)
  MODEL, EFFORT
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

CODEX = os.path.join(os.environ["LOCALAPPDATA"], "Volta", "bin", "codex.cmd")


async def main() -> None:
    out_path, prompt = sys.argv[1], sys.argv[2]
    log = open(out_path, "w", encoding="utf-8")
    t0 = time.time()
    decisions = os.environ.get("DECISIONS", "accept").split(",")
    n_appr = 0
    text: list[str] = []

    def rec(direction: str, msg: dict) -> None:
        log.write(json.dumps({"t": round(time.time() - t0, 2), "dir": direction, "msg": msg}, ensure_ascii=False) + "\n")
        log.flush()

    root = json.loads(os.environ.get("ROOT_ARGS", "[]"))
    proc = await asyncio.create_subprocess_exec(
        "cmd", "/c", CODEX, *root, "app-server", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
    nid = 0
    pending: dict[int, asyncio.Future] = {}
    turn_done = asyncio.Event()

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

    def nxt() -> str:
        return decisions[min(n_appr - 1, len(decisions) - 1)]

    async def answer(m: dict) -> None:
        nonlocal n_appr
        n_appr += 1
        method = m["method"]
        d = nxt()
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            print(f"approval #{n_appr} {method} cmd={m['params'].get('command')!r} -> {d}")
            await send({"jsonrpc": "2.0", "id": m["id"], "result": {"decision": d}})
        elif method == "mcpServer/elicitation/request":
            act = "accept" if d.startswith("accept") else "decline"
            print(f"approval #{n_appr} {method} -> {act}")
            await send({"jsonrpc": "2.0", "id": m["id"], "result": {"action": act, "content": {} if act == "accept" else None}})
        else:
            print("unhandled server request", method, json.dumps(m.get("params"))[:300])
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
                if meth == "item/agentMessage/delta":
                    text.append(p.get("delta", ""))
                elif meth == "item/completed":
                    it = p.get("item", {})
                    if it.get("type") == "commandExecution":
                        print("  cmd done:", it.get("status"), "exit", it.get("exitCode"),
                              "out:", (it.get("aggregatedOutput") or "")[-200:].replace("\n", " | "))
                    elif it.get("type") == "mcpToolCall":
                        print("  mcp done:", it.get("tool"), it.get("status"), json.dumps(it.get("result"))[:200])
                elif meth == "turn/completed":
                    turn_done.set()

    async def stderr_drain() -> None:
        data = await proc.stderr.read()
        rec("stderr", {"text": data.decode(errors="replace")[-4000:]})

    rt = asyncio.create_task(reader())
    st = asyncio.create_task(stderr_drain())
    try:
        init = await asyncio.wait_for(request("initialize", {"clientInfo": {"name": "cfharness-spike", "version": "0.0.2"},
                                                         **({"capabilities": {"experimentalApi": True}} if os.environ.get("EXPERIMENTAL") else {})}), 60)
        print("initialize:", json.dumps(init.get("result", init), ensure_ascii=False)[:300])
        await send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        for method, params in json.loads(os.environ.get("PRE", "[]")):
            r = await asyncio.wait_for(request(method, params), 60)
            print(f"{method}:", json.dumps(r.get("result", r.get("error")), ensure_ascii=False)[:1500])
        cfg = {"model_reasoning_effort": os.environ.get("EFFORT", "low")}
        cfg.update(json.loads(os.environ.get("CFG_JSON", "{}")))
        appr_raw = os.environ.get("APPROVAL", "untrusted")
        appr = json.loads(appr_raw) if appr_raw.startswith("{") else appr_raw
        common = {"cwd": os.getcwd(), "model": os.environ.get("MODEL", "gpt-5.6-luna"), "approvalPolicy": appr,
                  "sandbox": os.environ.get("SANDBOX", "read-only"),
                  "developerInstructions": "You are being driven by a test harness. Use tools exactly as instructed.",
                  "config": cfg}
        if os.environ.get("RESUME"):
            ts = await asyncio.wait_for(request("thread/resume", {**common, "threadId": os.environ["RESUME"]}), 120)
            print("thread/resume:", json.dumps(ts.get("result", ts.get("error")), ensure_ascii=False)[:600])
        else:
            ts = await asyncio.wait_for(request("thread/start", {**common, "ephemeral": os.environ.get("EPHEMERAL", "1") != "0"}), 120)
            print("thread/start:", json.dumps(ts.get("result", ts.get("error")), ensure_ascii=False)[:600])
        thread_id = ts["result"]["thread"]["id"]
        print("THREAD_ID", thread_id)
        await asyncio.sleep(float(os.environ.get("POST_WAIT", "0")))
        for method, params in json.loads(os.environ.get("POST", "[]")):
            r = await asyncio.wait_for(request(method, params), 60)
            print(f"{method}:", json.dumps(r.get("result", r.get("error")), ensure_ascii=False)[:1500])
        if os.environ.get("NO_TURN"):
            return
        for i, p in enumerate([prompt] + ([os.environ["TURN2"]] if os.environ.get("TURN2") else [])):
            turn_done.clear()
            text.clear()
            tr = await request("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": p}]})
            print(f"turn{i+1}/start:", json.dumps(tr.get("result", tr.get("error")), ensure_ascii=False)[:200])
            try:
                await asyncio.wait_for(turn_done.wait(), 400)
            except asyncio.TimeoutError:
                print("TURN TIMEOUT")
            print(f"assistant text turn{i+1}:", "".join(text)[-500:].replace("\n", " "))
    finally:
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 15)
        except asyncio.TimeoutError:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        await asyncio.gather(rt, st, return_exceptions=True)
        print("elapsed", round(time.time() - t0, 1), "s, approvals:", n_appr)


asyncio.run(main())
