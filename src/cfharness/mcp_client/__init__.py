"""MCP bên thứ ba của agent — do `claude` tự quản lý theo tài khoản (plan-mcp-claude-native PR7).

Hub không còn registry/chính sách MCP: mọi run làm việc nạp MỌI server + connector trong cấu hình claude của tài khoản đang dùng.
Còn lại:
- `claude_source`: đọc (chỉ đọc) danh sách server trong `.claude.json` của từng tài khoản — RPC `mcp.list`, Playwright (Chrome),
  server trỏ về chính hub (H-18);
- `claude_redact` + `secret_values`: che theo giá trị secret trong cấu hình MCP của claude;
- `wiring.attach`: gắn secret store, audit, bộ che vào `Hub`.
"""
