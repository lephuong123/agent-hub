# Plugin Claude Code cho run của agent

## Vấn đề
Run của agent chạy cô lập: `--setting-sources "" --settings <file hub sinh> --strict-mcp-config --mcp-config <file>`
(`runtime.claude.isolation`). Vì không nạp nguồn `user`, `enabledPlugins` trong `~/.claude/settings.json` bị bỏ qua, nên
agent không có skill, lệnh, subagent hay hook của plugin nào ngoài plugin builtin `cc-plugin-*`.

## Cách làm
Mỗi agent có trường `plugins` (danh sách). Lúc spawn, hub phân giải từng mục thành thư mục rồi truyền
`--plugin-dir <thư mục>` (`src/cfharness/plugins.py`). Các cờ cô lập giữ nguyên.

Đã kiểm với `claude` 2.1.322 (Windows): `--setting-sources "" --strict-mcp-config --plugin-dir <vercel 0.50.0>`
→ `system/init` có plugin `vercel` (source `vercel@inline`), skill `vercel:*` và 3 hook `SessionStart` của plugin.
MCP server của plugin (`.mcp.json`) **không** được nạp vì có `--strict-mcp-config`. Muốn có MCP đó thì khai báo nó
qua registry MCP của hub (`mcp_servers`), nơi guard và grant áp dụng như với mọi MCP khác.

Cách ghi một mục:
- `tên@marketplace`: tra `~/.claude/plugins/installed_plugins.json` (CLI ghi khi `claude plugin install`).
  Nhiều mục cài thì ưu tiên scope `user`, sau đó lấy mục cập nhật gần nhất.
- `tên`: chỉ hợp lệ khi đúng một marketplace có plugin tên đó, còn không thì báo lỗi và yêu cầu ghi rõ.
- đường dẫn tuyệt đối tới thư mục có `.claude-plugin/plugin.json`: dùng cho plugin tự viết, chưa cài.
- Có biến `CLAUDE_CONFIG_DIR` thì đọc ở đó thay cho `~/.claude`.

Plugin chưa cài hoặc thư mục cài đã mất: `runs.submit` báo `HubError` ngay. Lúc spawn sẽ kiểm lại, và nếu lỗi thì
`RuntimeError_` làm run thất bại. Hub không bao giờ âm thầm chạy thiếu plugin.

## Kiểm cô lập hậu nghiệm
Plugin người khai báo cho agent không bị coi là `isolation.violation`:
- tên plugin (từ `plugin.json`) được thêm vào `allowed_plugin_prefixes` của run;
- sự kiện hook của plugin (`hooks/hooks.json` hoặc `hooks` trong `plugin.json`) được thêm vào `allowed_hooks`.
  Stream chỉ ghi `hook_event`/`hook_name` chứ không ghi nguồn, nên kiểm theo sự kiện.

## Ai được đặt
Chỉ người đặt được, qua CLI hoặc RPC `agent.update`. Hook của plugin là lệnh shell do CLI chạy, nằm ngoài lớp guard
`PreToolUse`. Vì vậy `plugins` **không** có trong `manager.permissions.AGENT_FIELDS`: điều phối viên không tự thêm được
plugin cho mình hay cho agent khác. `--plugin-dir` trong `isolation.extra_args` vẫn bị chặn (H-18), vì hub tự sinh cờ này.

## Dùng
```
cfharness agent edit <id> --plugin vercel@claude-plugins-official
cfharness agent edit <id> --plugin none          # bỏ hết
cfharness agent show <id>                         # dòng "plugins:"
cfharness doctor --agent <id>                     # kiểm phân giải plugin
```
Takeover (`claude --resume` tương tác) cũng nhận cùng `--plugin-dir`.

Schema DB: v23 thêm cột `agents.plugins` (JSON).
