# Báo cáo spike — Grok Build CLI (`grok`) làm runtime cho cfharness

| Mục | Giá trị |
|---|---|
| Ngày chạy | 2026-11-25 |
| CLI | `grok` **1.0.92** (Windows 11, `~/.grok/bin/grok.exe`) |
| Đăng nhập | `grok login --device-auth` — tài khoản **gói FREE** (không phải SuperGrok) |
| Chi phí | 16 lượt model. `-p` tối thiểu ≈ 19K token vào; một lượt ACP ≈ 55K. Hết quota free (600K token / 24 h trượt) ở K-07c → **429** `subscription:free-usage-exhausted` |
| Script + fixture | `docs/spikes/codex-grok/grok/scripts/` (`acp_probe.py` thêm `ACP_CMD`, `acp_steer_resume.py`), `fixtures/` (37 file) |
| Plan liên quan | `docs/dev-notes/plan-codex-grok-runtime.md` |

`~/.grok/config.toml` sao lưu trước bẫy K-04 và trả lại nguyên trạng (`cmp` khớp từng byte); `rules/` và `trusted_folders.toml` do spike tạo đã xoá. Còn lại `sessions/` và `memory-v2/` (chỉ index rỗng). Quét fixture: không có token/email; hostname đã che.

## 0. Kết luận nhanh

**Về kỹ thuật: ĐẠT nếu đi qua `grok agent stdio` (ACP) — dùng chung được `AcpRuntime` với `agy`. `-p` chỉ hợp cho oneshot.**
**Chốt chặn: cô lập cấu hình (K-04) — Grok mặc định nạp MCP/skill/hook/plugin của Claude Code trên máy.**

| ID | Giả định | Kết quả | Fixture |
|---|---|---|---|
| K-01 | Event chuẩn hoá được | **Xác nhận** — xem mục 1 | `k01-*`, `k01b-*` |
| K-02 | Chặn quyền khi headless | **Xác nhận, fail-closed**; `--sandbox` **không có tác dụng trên Windows** | `k02a..d-*` |
| K-03 | MCP do hub cấp | **Xác nhận có điều kiện**: `<cwd>/.grok/config.toml` chỉ nạp khi thư mục được *trust*; `--deny 'MCPTool(...)'` chặn từng tool | `k03a-*`, `k03b-*` |
| K-04 | Cô lập cấu hình người dùng | **Bác bỏ (mặc định)** — xem mục 3 | `k04-*` |
| K-05 | Resume | **Xác nhận**: `-s <uuid do client chọn>` rồi `-r <uuid>` nhớ đúng | `k05a-*`, `k05b-*` |
| K-06 | Prompt dài tiếng Việt | **Xác nhận**: 105 KB qua `--prompt-file`, dòng đầu/cuối đúng dấu | `k06-*` |
| K-07 | Kênh duyệt động (ACP) | **Xác nhận** — xem mục 4 | `k07a..c-*` |
| K-08 | `--json-schema`, `--max-turns` | **Xác nhận**: `structuredOutput`; `--max-turns` hết lượt → `stopReason: max_turn_requests`, **exit 1** | `k08a-*`, `k08b-*` |

## 1. Event `-p` (`--output-format streaming-json`)

- `-p` **không đi chung** `--prompt-file` (exit 2) → dùng `--prompt-file` một mình.
- Event: `available_commands` (lặp, bỏ qua) · `thought` (chữ ở `data`) · `tool_call` / `tool_call_update` (`toolCallId`, `toolName`, `kind`, `rawInput`, `status`, `content`, `rawOutput`) · `text` (trả lời ở `data`) · `usage` · `end` (`stopReason`, `sessionId`, `num_turns`, `total_cost_usd` ước lượng, `modelUsage`).
- Tool MCP hiện dưới dạng `toolName: "use_tool"`, `rawInput.tool_name: "<server>__<tool>"`.
- `--output-format streaming-messages-json` có dạng gần như stream-json của Claude → `ClaudeStreamAdapter` gần như dùng lại được.

## 2. Quyền khi chạy `-p`

| Chế độ | Kết quả |
|---|---|
| `default`, không luật allow | Shell bị từ chối: stderr `permission required … headless run cannot ask`; lượt kết thúc `cancelled`, exit 0 |
| `bypassPermissions` + `--deny 'Bash(*)' --deny 'Write(*)' --deny 'Edit(*)'` | Cả 3 bước bị chặn "Denied by permission policy"; agent làm tiếp các bước sau |
| `dontAsk` + `--allow 'Edit(created.txt)'` | Chỉ ghi `created.txt`; mọi thứ khác bị từ chối, lượt dừng |
| `acceptEdits` | Vẫn từ chối ghi **trong** cwd — chưa rõ lý do (có thể do thư mục chưa trust) |
| `--sandbox <profile>` | `warning: … no sandbox enforcement for this platform` → **không bảo vệ gì trên Windows** |

Không có kênh hỏi người trong `-p` → nếu dùng `-p`, guard phải quyết tĩnh trước khi chạy (mục 3 của plan).

## 3. Cô lập cấu hình — chốt chặn

Mặc định Grok nạp **cấu hình Claude Code** của máy:
- MCP server trong `~/.claude.json` (playwright, fast-browser, video-context, csdl-van-ban), 62 skill, rules, hooks, plugin `vercel` (MCP + hook).
- `GROK_CLAUDE_{SKILLS,MCPS,RULES,AGENTS,HOOKS}_ENABLED=0` và `GROK_CURSOR_*_ENABLED=0` tắt phần Claude/Cursor, **nhưng MCP + hook của plugin `vercel` vẫn nạp**.
- Bẫy MCP trong `~/.grok/config.toml` `[mcp_servers]` được nạp; rule trong `~/.grok/rules/*.md` làm lộ mã bẫy `GROK-HOME-TRAP-7731`. **Không có cờ tắt** hai thứ này.
- Cách cô lập duy nhất thấy được: `GROK_HOME` riêng — nhưng home mới cần đăng nhập riêng hoặc `XAI_API_KEY`.
- `--no-memory` / `GROK_MEMORY=0` tắt bộ nhớ liên phiên.

MCP theo thư mục (`<cwd>/.grok/config.toml`) chỉ nạp khi thư mục được trust. Cờ ẩn `--trust` nạp được nhưng **ghi trust cho cả git root** vào `~/.grok/trusted_folders.toml` → không dùng. Đường ACP (`session/new.mcpServers`) **không cần trust**.

## 4. `grok agent stdio` (ACP) — đường tích hợp được

- Phiên mặc định ở chế độ hỏi: `session/request_permission` cho từng lần sửa file, shell **và mọi tool MCP**; tool chỉ đọc chạy không hỏi.
- Giữ chờ 60 s không timeout.
- `reject_once` → tool `failed`, không chạy (`_meta.toolNotRun: "rejected"`), lượt kết thúc `cancelled`. `allow_once` → tool chạy.
- MCP của hub qua `session/new.mcpServers` chạy được, không cần trust thư mục.
- `session/cancel` → `cancelled` (`MidTurnAbort`). `session/load` ở tiến trình mới phát lại lịch sử, nhớ đúng BANANA-42.
- Reasoning mặc định `high` → chạy `grok agent --reasoning-effort low stdio`.
- Option có `allow_always` / `reject_always` — Grok **lưu theo project** → hub **chỉ được chọn `*_once`**.
- Cùng một `acp_probe.py` chạy được cả `agy` lẫn `grok` → `AcpRuntime` chung khả thi.

## 5. Điều khoản

- Docs đi kèm CLI (`~/.grok/docs/user-guide/02-authentication.md`): *"For CI/CD, automation, or environments without browser access, use an API key from console.x.ai"*.
- `15-agent-mode.md`: ACP dành cho *"IDEs, SDKs, eval harnesses, and custom apps"*.
- Không thấy câu nào cấm hay cho phép rõ việc dùng gói thuê bao từ app bên thứ ba. Bản consumer ToS 2024-12-20 cấm bot/công cụ tự động gửi nhiều yêu cầu hơn người dùng bình thường; không có điều khoản riêng về Grok Build/CLI.
- Bản hiện hành chưa đọc được bằng máy → **phải đọc trong trình duyệt**: [Consumer ToS](https://x.ai/legal/terms-of-service), [bản 2024-12-20](https://x.ai/legal/terms-of-service/previous-2024-12-20), [X-Subscription](https://x.ai/legal/x-subscription-terms-of-service), [Enterprise](https://x.ai/legal/terms-of-service-enterprise).

## 6. Đề xuất

- Chạy Grok qua **`AcpRuntime` chung** (`grok agent --reasoning-effort low stdio`), không làm runtime `-p` riêng.
  - Env: `GROK_DISABLE_AUTOUPDATER=1`, `GROK_MEMORY=0`, mọi `GROK_CLAUDE_*_ENABLED=0` / `GROK_CURSOR_*_ENABLED=0`.
  - Guard trả `request_permission` bằng `allow_once` / `reject_once` duy nhất.
  - MCP hub qua `session/new`; stop = `session/cancel` rồi kill; resume = `session/load`.
- `-p` + `dontAsk` (+ `--json-schema`) chỉ dùng cho oneshot (curation, judge…).
- Cô lập hoàn toàn cần `GROK_HOME` riêng + `XAI_API_KEY` (hoặc đăng nhập SuperGrok vào home đó). Trước khi có, `doctor` phải cảnh báo plugin `vercel` và `~/.grok/{config.toml,rules}` sẽ lọt vào run.
- Gói free không đủ cho hub (hết quota sau ~12 lượt ACP).

## 7. Chưa đo

- Steer (prompt thứ 2 giữa lượt) — dính 429 trước khi tới.
- `yoloMode` / `session/set_mode` / `session/set_config_option`.
- `GROK_HOME` riêng + `XAI_API_KEY`; tắt được plugin `vercel` bằng cách nào.
- Vì sao `acceptEdits` vẫn từ chối dưới `-p`.
- Quota thật của SuperGrok; điều khoản xAI bản hiện hành; macOS/Linux.

## 8. Đo bổ sung

K-09, K-10: xem `docs/spikes/codex-grok-spike-report.md` mục 3 (fixture cùng thư mục `fixtures/`).
