"""Spike G-04/G-05 qua `agy acp`:
1) prompt dài (đếm chậm bằng run_command) → sau ~T1 s gửi prompt thứ 2 cùng session (steer?) → ghi phản hồi.
2) session/cancel giữa chừng → stopReason.
3) kill tiến trình → tiến trình mới `session/load` (và `session/resume` nếu có) cùng sessionId → hỏi lại điều đã nói ở lượt 1.
Dùng: python acp_steer_resume.py <out.jsonl> <gemini_dir>
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

AGY = os.path.join(os.environ["LOCALAPPDATA"], "agy", "bin", "agy.exe")
OUT, GEM = sys.argv[1], sys.argv[2]
log = open(OUT, "w", encoding="utf-8")
T0 = time.time()


def rec(tag, d, msg):
    log.write(json.dumps({"t": round(time.time() - T0, 2), "phase": tag, "dir": d, "msg": msg}, ensure_ascii=False) + "\n")
    log.flush()


class Client:
    def __init__(self, tag):
        self.tag, self.nid, self.pending, self.text = tag, 0, {}, ""

    async def start(self):
        self.p = await asyncio.create_subprocess_exec(AGY, "acp", "--gemini-dir", GEM, "--app-data-dir", "run-app",
                                                      stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                                      stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024 * 1024)
        self.rt = asyncio.create_task(self.reader())
        r = await self.req("initialize", {"protocolVersion": 1, "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
        return r

    async def send(self, m):
        rec(self.tag, "out", m)
        self.p.stdin.write((json.dumps(m) + "\n").encode())
        await self.p.stdin.drain()

    def req_nowait(self, method, params):
        self.nid += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.nid] = fut
        asyncio.create_task(self.send({"jsonrpc": "2.0", "id": self.nid, "method": method, "params": params}))
        return fut

    async def req(self, method, params, timeout=600):
        return await asyncio.wait_for(self.req_nowait(method, params), timeout)

    async def reader(self):
        while True:
            line = await self.p.stdout.readline()
            if not line:
                break
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec(self.tag, "in", m)
            if "id" in m and "method" not in m:
                f = self.pending.pop(m["id"], None)
                if f and not f.done():
                    f.set_result(m)
            elif m.get("method") == "session/request_permission":
                opt = next(o["optionId"] for o in m["params"]["options"] if o["kind"].startswith("allow"))
                await self.send({"jsonrpc": "2.0", "id": m["id"], "result": {"outcome": {"outcome": "selected", "optionId": opt}}})
            elif m.get("method") == "session/update":
                u = m["params"]["update"]
                if u.get("sessionUpdate") == "agent_message_chunk":
                    self.text += u["content"].get("text", "")
            elif "id" in m and "method" in m:
                await self.send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32601, "message": "no"}})

    async def close(self, kill=False):
        if kill:
            self.p.kill()
        else:
            self.p.stdin.close()
            try:
                await asyncio.wait_for(self.p.wait(), 10)
            except asyncio.TimeoutError:
                self.p.kill()
        await asyncio.gather(self.rt, return_exceptions=True)


async def main():
    cwd = os.getcwd()
    # --- G-04 steer
    a = Client("steer")
    await a.start()
    sid = (await a.req("session/new", {"cwd": cwd, "mcpServers": []}))["result"]["sessionId"]
    print("session", sid)
    p1 = a.req_nowait("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text":
        "Remember the code word BANANA-42. Then use run_command to run: 1..6 | % { $_; Start-Sleep -Seconds 4 } and then summarize the output."}]})
    await asyncio.sleep(9)
    a.text = ""
    p2 = a.req_nowait("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "STEER: also tell me the current year at the end."}]})
    r1 = await asyncio.wait_for(p1, 600)
    print("p1:", json.dumps(r1.get("result") or r1.get("error"))[:300])
    r2 = await asyncio.wait_for(p2, 600)
    print("p2:", json.dumps(r2.get("result") or r2.get("error"))[:300])
    print("text after steer:", a.text[-400:].replace("\n", " "))
    # --- G-05a cancel
    a.text = ""
    p3 = a.req_nowait("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text":
        "Use run_command to run: 1..20 | % { $_; Start-Sleep -Seconds 3 }"}]})
    await asyncio.sleep(10)
    await a.send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
    t = time.time()
    r3 = await asyncio.wait_for(p3, 120)
    print("cancel ->", json.dumps(r3.get("result") or r3.get("error"))[:300], "after", round(time.time() - t, 1), "s")
    # --- G-05b kill process, load in a fresh process
    await a.close(kill=True)
    b = Client("resume")
    init = await b.start()
    rl = await b.req("session/load", {"sessionId": sid, "cwd": cwd, "mcpServers": []})
    print("session/load:", json.dumps(rl.get("result") or rl.get("error"))[:300])
    replay = b.text
    b.text = ""
    r4 = await b.req("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "What was the code word I asked you to remember? Answer with just the word."}]})
    print("after load:", json.dumps(r4.get("result") or r4.get("error"))[:200], "| answer:", b.text.strip()[:200])
    print("replayed text chars during load:", len(replay))
    await b.close()


asyncio.run(main())
