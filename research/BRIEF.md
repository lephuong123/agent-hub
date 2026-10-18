# BRIEF chung cho các agent làm agent-hub (đọc trước khi làm)

## Quyết định đã chốt (2026-10-18, chủ sản phẩm: phuonglq)
1. **Runtime = `claude` CLI (Claude Code) cài trên chính máy chạy hub.** Hub KHÔNG tự viết vòng lặp agent,
   KHÔNG gọi Anthropic API trực tiếp cho việc chạy agent. Mỗi lượt chạy của một agent = một tiến trình
   `claude -p ... --output-format stream-json` do hub spawn và đọc stdout. Dùng tài khoản/đăng nhập Claude sẵn có trên máy.
2. **Mức tự cải thiện theo đề xuất**: agent được tự áp dụng thay đổi nhỏ cho memory/skill CỦA RIÊNG NÓ
   (có ledger + rollback + eval gate). Mọi thay đổi tầng team/global, hồ sơ user, xoá skill → phải qua người duyệt.
3. Kiến trúc tổng thể, data model, lộ trình M0–M5: xem `research/00-de-xuat-kien-truc.md`
   (thay mục "Agent runtime = Claude Agent SDK" bằng quyết định 1 ở trên).
   Chi tiết nghiên cứu Hermes: `research/notes/01..05-*.md`.

## Năng lực `claude` CLI đã kiểm chứng trên máy (v2.1.302)
Đã chạy thật; mẫu output ở `research/notes/claude-stream-sample.jsonl` (35 dòng).

| Nhu cầu của hub | Cờ / lệnh `claude` |
|---|---|
| Chạy không tương tác | `-p "<prompt>"` |
| Luồng event realtime | `--output-format stream-json --verbose` (+ `--include-partial-messages` cho delta token, `--include-hook-events`) |
| Định danh phiên do hub cấp / tiếp tục / rẽ nhánh | `--session-id <uuid>`, `--resume <id>`, `--fork-session` |
| Persona agent (SOUL) | `--append-system-prompt` hoặc `--system-prompt` (`--system-prompt-snapshot on` giữ prompt cố định → cache) |
| MCP riêng từng agent, cô lập MCP của máy | `--mcp-config <file>` + `--strict-mcp-config` |
| Quyền tool | `--allowedTools`, `--disallowedTools`, `--tools`, `--permission-mode (acceptEdits|auto|bypassPermissions|manual|dontAsk|plan)` |
| Hooks (thu tín hiệu, policy, đẩy event) | `--settings <file-json>` chứa `hooks`; `--setting-sources` để chọn nguồn settings |
| Cô lập khỏi cấu hình chung của máy | `--bare` (bỏ hooks/CLAUDE.md/auto-memory; auth CHỈ qua ANTHROPIC_API_KEY — lưu ý với tài khoản OAuth), hoặc `--setting-sources` + `--strict-mcp-config` + `--add-dir` |
| Subagent định nghĩa động | `--agents '<json>'` |
| Model / effort / fallback | `--model opus|sonnet|haiku|<full id>`, `--effort`, `--fallback-model` |
| Giới hạn chi phí / output có cấu trúc | `--max-budget-usd`, `--json-schema` |
| Steer giữa chừng (gửi thêm tin vào phiên đang chạy) | `--input-format stream-json` (stdin streaming) |
| Phiên nền có sẵn của Claude Code | `claude --bg`, `claude agents --json`, `claude attach|logs|stop|rm <id>` |
| Cô lập thư mục làm việc | `-w/--worktree`, `--add-dir` |

Các loại event stream-json quan sát được: `system/init` (session_id, cwd, tools[], mcp_servers[], model),
`system/status`, `system/hook_started|hook_response`, `system/thinking_tokens`, `stream_event` (message_start,
content_block_start/delta/stop, message_delta/stop), `assistant` (message.content có `text`/`tool_use`),
`user` (`tool_result`), `rate_limit_event`, `result` (subtype success|error…, `total_cost_usd`, `usage`, `duration_ms`, `session_id`).

**Phát hiện khi chạy thử**: hook SessionStart của máy (Serena) vẫn chạy trong phiên headless → hub phải
chủ động cô lập cấu hình từng agent (settings/mcp riêng), nếu không mọi agent thừa hưởng cấu hình người dùng.

## Bối cảnh người dùng
- Đang dùng Claude Code + Serena (MCP). Có nhiều MCP nội bộ (quản lý công việc, văn bản, theo dõi… phần lớn chỉ đọc, tên tool tiếng Việt).
- Tài liệu viết bằng **tiếng Việt**. Hệ thống nội bộ của họ lưu BRD/PRD/AGENTS trong repo.

## Quyết định bổ sung (2026-10-18, trả lời câu hỏi mở BRD)
- Q-01: dùng `claude` CLI đã đăng nhập trên máy, được chạy nhiều agent → không dùng `--bare`.
- Q-02: số agent đồng thời và ngân sách là setting, không trần cứng.
- Q-03: đồng ý ngưỡng tự áp dụng ≤ 800 ký tự, 1 skill/1 mục memory.
- Q-05: cho dùng tool ghi của MCP nội bộ từ đầu, có nhật ký kiểm toán.
- Q-06: agent ghi thẳng vào repo thật → khoá ghi theo repo, checkpoint + diff mỗi run, `run revert`.

## Quyết định bổ sung lần 2 (2026-10-18) — NÂNG MỨC TỰ CẢI THIỆN (thay thế quyết định số 2 ở đầu file)
- Agent được TỰ làm (không cần người duyệt): thay đổi tri thức tầng agent/team/global, hồ sơ user (USER), xoá/gộp/archive skill, sửa cấu hình agent (model, prompt/SOUL, toolsets, MCP, giới hạn…). Mục tiêu: con người tham gia ít nhất có thể.
- CHỈ cần quyền con người cho thao tác PHÁ HUỶ / KHÔNG HOÀN TÁC ĐƯỢC liên quan tới xoá hệ thống hoặc dữ liệu quan trọng
  (vd. xoá vĩnh viễn dữ liệu, drop DB/bảng, tool MCP xoá bản ghi nghiệp vụ, `rm -rf` ngoài phạm vi thay đổi của chính run, `git push --force`/xoá nhánh/xoá lịch sử, purge ledger/event/backup, xoá cứng agent/team, gỡ MCP server khỏi hệ thống).
- Bù cho việc bỏ duyệt: mọi xoá trong phạm vi hub là xoá mềm (archive + hoàn tác được), ledger + snapshot, eval gate, tự rollback khi tụt điểm, giới hạn tần suất thay đổi, thông báo/digest cho người (báo chứ không hỏi). Agent KHÔNG được tự sửa chính sách an toàn (danh sách thao tác phá huỷ, danh sách tài nguyên được bảo vệ, audit, guard).
- Q-08: chốt Python 3.12+ + Textual. Q-14: persona/skill/memory viết bằng TIẾNG ANH (tài liệu BRD vẫn tiếng Việt). Q-04: timeout phê duyệt 300 s → sau đó KHÔNG tự từ chối, run tạm dừng và chờ đến khi có người (nhắc lại/thông báo).

## Quyết định bổ sung lần 3 (2026-10-18) — BRD 0.5
- Q-16 Slack; Q-21 waiting_human nhả slot, giữ khoá repo; Q-17..Q-20, Q-22, Q-23 theo đề xuất (xem BRD 14.1).
- Q-07: golden task + cách chấm do NGƯỜI DÙNG quyết định; agent chỉ gợi ý, không tự tạo/sửa.
- Q-09: daemon restart → dừng-và-resume tự động mọi run (trừ waiting_human).
- Q-11: ai cài hub thì người đó dùng — một người dùng/một máy; HTTP MCP chỉ bind localhost.
- Q-10: event/transcript giữ 30 ngày. Q-12: KPI như đề xuất. Q-13: mượn NGUYÊN VĂN prompt review/curator của Hermes, kèm NOTICE (MIT). Q-15: chỉ takeover, không tích hợp `claude --bg`. → BRD 0.6, không còn câu hỏi mở.
