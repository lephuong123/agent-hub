"""Bản sao từ docs/spikes/agy/scripts/acp_probe.py, thêm ACP_CMD (JSON list lệnh khởi động agent, mặc định `agy acp`)
để chạy với `grok agent stdio`. Ví dụ: ACP_CMD='["C:/Users/x/.grok/bin/grok.exe","agent","stdio"]'.

Spike: điều khiển `agy acp` (Agent Client Protocol, JSON-RPC qua stdio) và đóng vai client duyệt tool.

Dùng:  python acp_probe.py <out.jsonl> <policy> "<prompt>" [--hold S] [--extra-arg ...]
policy: deny-all | allow-all | deny-match:<chuỗi>  (deny khi toolCall chứa chuỗi, còn lại allow)
--hold S: giữ chờ S giây trước khi trả lời request_permission (đo A-13 tương đương)
Ghi mọi message hai chiều vào out.jsonl (dir: "in" = từ agy, "out" = từ client).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

AGY = os.path.join(os.environ.get("LOCALAPPDATA", ""), "agy", "bin", "agy.exe")


async def main() -> None:
    out_path, policy, prompt = sys.argv[1], sys.argv[2], sys.argv[3]
    rest = sys.argv[4:]
    hold = 0.0
    extra: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == "--hold":
            hold = float(rest[i + 1]); i += 2
        else:
            extra.append(rest[i]); i += 1
    log = open(out_path, "w", encoding="utf-8")
    t0 = time.time()

    def rec(direction: str, msg: dict) -> None:
        log.write(json.dumps({"t": round(time.time() - t0, 2), "dir": direction, "msg": msg}, ensure_ascii=False) + "\n")
        log.flush()

    cmd = json.loads(os.environ["ACP_CMD"]) if os.environ.get("ACP_CMD") else [AGY, "acp"]
    proc = await asyncio.create_subprocess_exec(*cmd, *extra, stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                limit=16 * 1024 * 1024)
    nid = 0
    pending: dict[int, asyncio.Future] = {}

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

    def decide(params: dict) -> dict:
        blob = json.dumps(params.get("toolCall", {}), ensure_ascii=False)
        opts = params.get("options", [])
        def pick(kinds):
            for k in kinds:
                for o in opts:
                    if o.get("kind") == k:
                        return {"outcome": {"outcome": "selected", "optionId": o["optionId"]}}
            return {"outcome": {"outcome": "cancelled"}}
        allow = pick(["allow_once", "allow_always"])
        deny = pick(["reject_once", "reject_always"])
        if policy == "deny-all":
            return deny
        if policy == "allow-all":
            return allow
        if policy.startswith("deny-match:"):
            return deny if policy.split(":", 1)[1].lower() in blob.lower() else allow
        return deny

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
            elif msg.get("method") == "session/request_permission":
                async def answer(m=msg):
                    if hold:
                        await asyncio.sleep(hold)
                    await send({"jsonrpc": "2.0", "id": m["id"], "result": decide(m["params"])})
                asyncio.create_task(answer())
            elif "id" in msg and "method" in msg:
                # request khác từ agent (fs/*, terminal/*) — client không khai năng lực nên trả lỗi
                await send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}})

    async def stderr_drain() -> None:
        data = await proc.stderr.read()
        rec("stderr", {"text": data.decode(errors="replace")[-4000:]})

    rt = asyncio.create_task(reader())
    st = asyncio.create_task(stderr_drain())
    init = await request("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
    print("initialize:", json.dumps(init.get("result", init), ensure_ascii=False)[:800])
    mcp = json.loads(os.environ.get("ACP_MCP", "[]"))  # danh sách McpServer theo ACP (stdio: name/command/args/env)
    sess = await request("session/new", {"cwd": os.getcwd(), "mcpServers": mcp})
    print("session/new:", json.dumps(sess.get("result", sess), ensure_ascii=False)[:800])
    sid = sess["result"]["sessionId"]
    if os.environ.get("ACP_MODEL"):
        sm = await request("session/set_model", {"sessionId": sid, "modelId": os.environ["ACP_MODEL"]})
        print("set_model:", json.dumps(sm.get("result", sm.get("error")), ensure_ascii=False)[:300])
    if os.environ.get("ACP_PROMPT_FILE"):
        prompt = open(os.environ["ACP_PROMPT_FILE"], encoding="utf-8").read()
    res = await asyncio.wait_for(request("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]}), 900)
    print("prompt result:", json.dumps(res.get("result", res), ensure_ascii=False)[:500])
    proc.stdin.close()
    try:
        await asyncio.wait_for(proc.wait(), 15)
    except asyncio.TimeoutError:
        proc.kill()
    await asyncio.gather(rt, st, return_exceptions=True)
    print("elapsed", round(time.time() - t0, 1), "s")


asyncio.run(main())
