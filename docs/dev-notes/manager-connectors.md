# Connector claude.ai cho điều phối viên

Ngày: 2026-10-26. Nhánh `feat/manager-claude-connectors`. Mục tiêu: chế độ chat (điều phối viên) dùng được connector claude.ai
của tài khoản đã đăng nhập (Claude Docs, Atlassian, Google Drive…), ví dụ để đọc link `claude.ai/artifact/...`.

## Bật

```bash
cfharness manager capabilities set --profile hermes --mcp trusted --connectors "Claude Docs,Atlassian" --sync-agent
cfharness manager capabilities set --connectors none     # tắt
```

Điều kiện: `mcp=trusted`, `guard.enabled=true`. Thiếu một trong hai thì connector không được bật (có cảnh báo). Tên connector
lấy theo tên trong Claude Code (`claude.ai Claude Docs` → `Claude Docs`). Hub chuẩn hoá về slug của tên tool (`Claude_Docs`).

## Sự thật về `claude` CLI (đã kiểm 2.1.310, Windows)

| Tổ hợp cờ | Connector claude.ai |
|---|---|
| `--setting-sources ""` (mặc định của hub), có hoặc không `--strict-mcp-config` | không nạp |
| `--setting-sources project` / `local` | không nạp |
| `ENABLE_CLAUDEAI_MCP_SERVERS=true` + `--setting-sources ""` | không nạp |
| `--setting-sources user` + `--strict-mcp-config` | không nạp |
| `--setting-sources user`, không `--strict-mcp-config` | **nạp** (kèm MCP `~/.claude.json`, plugin, hook user) |

- Không có cách nạp riêng connector. Run connector vì vậy nạp kèm cấu hình user.
- Tool `Artifact` của Claude Code **không có** ở chế độ `-p`. Link artifact dạng doc đọc được qua connector Claude Docs
  (`mcp__claude_ai_Claude_Docs__read`, `ref={"object":"project","id":"<id trong link>"}`).
- `--tools <danh sách>` thiếu `ToolSearch` làm CLI nhét schema mọi tool MCP vào ngữ cảnh: 161k token so với 15k token mỗi lượt
  (haiku, 207 tool). Run thật với sonnet: $1.13 so với $0.13. Run connector luôn được thêm `ToolSearch` (guard xếp `safe`).

## Hub làm gì với run connector (`manager/connectors.py`, `runs.py::_prepare_spec`)

- **Cờ cô lập ghi đè cho riêng run:** `RunSpec.isolation_overrides = {setting_sources: "user", strict_mcp_config: false}`.
  `--settings` và `--mcp-config` của hub vẫn được truyền.
- **Plugin user bị tắt:** `enabledPlugins` của `~/.claude/settings.json` được đặt false trong settings hub sinh (cờ `--settings`
  thắng nguồn user). Plugin dạng *synced* (vd `cf-hubspot`) không nằm trong `enabledPlugins` nên vẫn nạp và vẫn báo
  `isolation.violation` (kind `plugin`). Cảnh báo này cố ý giữ lại.
- **Kiểm cô lập hậu nghiệm:** hook user (`hooks` của `~/.claude/settings.json`), MCP trong `~/.claude.json` và server tiền tố
  `claude.ai ` / `plugin:` được coi là đã biết (`RunSpec.allowed_mcp_prefixes`).
- **Guard (`classify_mcp`, `GuardContext.connectors`):** với run connector, tool MCP của server không thuộc agent và không phải
  connector được cho phép → `forbidden` (`mcp_not_in_run`). Ví dụ: Gmail khi chưa cho phép, Playwright của `~/.claude.json` khi
  agent không gắn, MCP của plugin. Connector được cho phép:
  - tên dạng đọc (`get|list|search|read|query|fetch|guide…`) → `safe`;
  - còn lại → `write` (ghi audit, KHÔNG hỏi duyệt — theo yêu cầu người dùng);
  - tên khớp mẫu xoá (`delete|trash|remove…`) hoặc tool được bảo vệ trong policy → `destructive` (approval như luật cũ).

## Rủi ro chấp nhận

- **Cấu hình user lọt vào run:** hook user (vd `SessionStart`) chạy trong run, CLAUDE.md của user được nạp, plugin synced nạp.
- **Không có lớp duyệt riêng cho connector:** agent gửi, sửa, tạo bằng tài khoản thật của người (Gmail, Slack, Jira…) mà không
  hỏi duyệt, trừ thao tác xoá. Chỉ cho phép connector thật sự cần.
- **Lớp chặn MCP lạ nằm ở hook guard (PreToolUse + permission-prompt-tool):** không có `--disallowedTools` tĩnh cho connector vì
  hub không biết trước tên connector của tài khoản.

Test: `tests/test_manager_connectors.py`.
