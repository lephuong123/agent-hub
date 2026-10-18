#!/usr/bin/env python3
"""MCP server stdio tối giản làm --permission-prompt-tool cho spike M0 (A-01a, A-13).
Chạy: uv run --no-project --with 'mcp<2' python mcp/hubperm.py
Tool `approve(tool_name, input, tool_use_id?)` trả về JSON dạng text:
  {"behavior":"allow","updatedInput":{...}}  hoặc  {"behavior":"deny","message":"..."}
Luật giống hooks/pretool.py: 'rm -rf' -> deny; 'SLOWAPPROVE' -> ngủ $PERM_SLEEP (150 s) rồi allow; còn lại allow.
Mọi lần gọi được ghi vào $PERM_LOG (mặc định fixtures/permtool-inputs.jsonl)."""
import json, os, time, asyncio
from typing import Any, Optional
from mcp.server.fastmcp import FastMCP

here = os.path.dirname(os.path.abspath(__file__))
LOG = os.environ.get('PERM_LOG', os.path.join(here, '..', 'fixtures', 'permtool-inputs.jsonl'))
mcp = FastMCP('hubperm')


@mcp.tool(structured_output=False)  # bắt buộc: CLI chỉ nhận đúng 1 text block, không structuredContent
async def approve(tool_name: str, input: dict[str, Any], tool_use_id: Optional[str] = None) -> str:
    """Hub permission bridge: decide allow/deny for a pending tool call."""
    t0 = time.time()
    s = json.dumps(input)
    if 'rm -rf' in s:
        out = {'behavior': 'deny', 'message': 'hub guard: destructive operation (rm -rf) denied by human'}
    else:
        if 'SLOWAPPROVE' in s:
            await asyncio.sleep(float(os.environ.get('PERM_SLEEP', '150')))
        out = {'behavior': 'allow', 'updatedInput': input}
    with open(LOG, 'a') as f:
        f.write(json.dumps({'ts_start': t0, 'ts_end': time.time(),
                            'args': {'tool_name': tool_name, 'input': input, 'tool_use_id': tool_use_id},
                            'output': out}, ensure_ascii=False) + '\n')
    return json.dumps(out)


if __name__ == '__main__':
    mcp.run()
