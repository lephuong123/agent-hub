"""MCP server của hub (FR-MS-01..07) — luồng A của M1.

- `server.py`: tool `hub_*` (client mỏng của daemon qua JSON-RPC).
- `http.py`: Streamable HTTP chỉ loopback + token `read`/`control`.
- `rpc.py`: method `mcp.*`/`secret.*`/`audit.*`/`feedback.*` trên daemon + bọc audit (FR-OB-08).
- `cli.py`: lệnh `cfharness mcp …`, `secret …`, `audit …`, `feedback …`.
"""
