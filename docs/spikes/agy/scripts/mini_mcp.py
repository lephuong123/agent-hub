"""MCP server stdio tối giản cho spike: 2 tool `spike_echo` (đọc) và `spike_write_note` (ghi giả, chỉ log).
Ghi mọi lời gọi vào file trong biến môi trường MINI_MCP_LOG."""
import json
import os
import sys

LOG = os.environ.get("MINI_MCP_LOG", "mini_mcp.log")


def log(x):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(x, ensure_ascii=False) + "\n")


TOOLS = [
    {"name": "spike_echo", "description": "Echo back the given text.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "spike_write_note", "description": "Write a note to the hub (side effect).",
     "inputSchema": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": True}},
]

for line in sys.stdin:
    try:
        m = json.loads(line)
    except json.JSONDecodeError:
        continue
    log({"in": m})
    if "id" not in m:
        continue
    meth = m.get("method")
    if meth == "initialize":
        res = {"protocolVersion": m["params"].get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
               "serverInfo": {"name": "spike-mini", "version": "0.1"}}
    elif meth == "tools/list":
        res = {"tools": TOOLS}
    elif meth == "tools/call":
        p = m["params"]
        a = p.get("arguments") or {}
        text = f"echo:{a.get('text')}" if p["name"] == "spike_echo" else f"note-saved:{a.get('note')}"
        res = {"content": [{"type": "text", "text": text}]}
    else:
        res = {}
    out = {"jsonrpc": "2.0", "id": m["id"], "result": res}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
