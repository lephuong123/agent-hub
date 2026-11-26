"""Spike K-09: `grok agent stdio` — initialize + session/new (không gửi prompt, không tốn lượt model).
Xem authMethods, session/new có đòi đăng nhập không, modes/models/configOptions trả về.
Dùng: python acp_auth_probe.py <out.jsonl>   (GROK_HOME / GROK_AUTH_PATH / GROK_CONFIG lấy từ env)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

GROK = os.path.join(os.path.expanduser("~"), ".grok", "bin", "grok.exe")


async def main() -> None:
    out = open(sys.argv[1], "w", encoding="utf-8")
    t0 = time.time()

    def rec(d: str, m: dict) -> None:
        out.write(json.dumps({"t": round(time.time() - t0, 2), "dir": d, "msg": m}, ensure_ascii=False) + "\n")
        out.flush()

    p = await asyncio.create_subprocess_exec(GROK, "agent", "--reasoning-effort", "low", "stdio",
                                             stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                             stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
    pending: dict[int, asyncio.Future] = {}

    async def reader() -> None:
        while True:
            line = await p.stdout.readline()
            if not line:
                break
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec("in", m)
            if "id" in m and "method" not in m and m["id"] in pending:
                pending.pop(m["id"]).set_result(m)

    async def req(i: int, method: str, params: dict) -> dict:
        fut = asyncio.get_running_loop().create_future()
        pending[i] = fut
        msg = {"jsonrpc": "2.0", "id": i, "method": method, "params": params}
        rec("out", msg)
        p.stdin.write((json.dumps(msg) + "\n").encode())
        await p.stdin.drain()
        return await asyncio.wait_for(fut, 60)

    rt = asyncio.create_task(reader())
    try:
        init = await req(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
        r = init.get("result") or {}
        print("authMethods:", json.dumps(r.get("authMethods"), ensure_ascii=False)[:600])
        print("agentCapabilities:", json.dumps(r.get("agentCapabilities"), ensure_ascii=False)[:400])
        sess = await req(2, "session/new", {"cwd": os.getcwd(), "mcpServers": []})
        if "error" in sess:
            print("session/new ERROR:", json.dumps(sess["error"], ensure_ascii=False)[:500])
        else:
            s = sess["result"]
            print("session/new OK sid", s.get("sessionId"))
            print("modes:", json.dumps(s.get("modes"), ensure_ascii=False)[:800])
            print("models current:", json.dumps((s.get("models") or {}).get("currentModelId")),
                  "n=", len((s.get("models") or {}).get("availableModels") or []))
            print("configOptions:", json.dumps(s.get("configOptions"), ensure_ascii=False)[:1200])
    finally:
        p.stdin.close()
        try:
            await asyncio.wait_for(p.wait(), 10)
        except asyncio.TimeoutError:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        await asyncio.gather(rt, return_exceptions=True)
        err = (await p.stderr.read()).decode(errors="replace")
        rec("stderr", {"text": err[-3000:]})
        print("stderr tail:", err[-300:].replace("\n", " | "))


asyncio.run(main())
