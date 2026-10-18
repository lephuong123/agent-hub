# 04 — Bề mặt tích hợp của hermes-agent

> Phạm vi: Hermes tích hợp ra ngoài và nhận tích hợp vào bằng cách nào (MCP server/client, ACP, TUI gateway JSON-RPC, API OpenAI-compatible, A2A, webhooks, các nền tảng nhắn tin, plugin/hook) và bảo mật (approvals, allowlist, sandbox).
> Nguồn: code tại `/Users/phuonglq/Workspace/agent-hub/research/hermes-agent` và tài liệu `website/docs`. Tham chiếu dạng `path:line`. Đoạn nào ghi **[Suy luận]** là nhận định của tôi, không đọc thấy trực tiếp trong repo.

---

## 0. Tóm tắt nhanh

- Mọi bề mặt đều chạy chung một lõi `AIAgent` (`run_agent.py`). Các bề mặt chỉ khác nhau ở giao thức truyền tải (`website/docs/developer-guide/programmatic-integration.md:9-17`).
- **Hermes làm MCP server** (`hermes mcp serve`) **chỉ là cầu nối nhắn tin**: 10 tool đọc/gửi hội thoại Telegram/Slack/…, chạy qua stdio. Nó **không** cho bên ngoài gọi "hãy chạy agent làm việc X". Khác với Serena: Serena expose năng lực cốt lõi của nó (phân tích code) thành tool.
- **Hermes làm MCP client** thì rất đầy đủ: stdio, Streamable HTTP, SSE, OAuth 2.1, mTLS, lọc tool include/exclude, trust tier `untrusted`, lazy start, sampling, elicitation, catalog `optional-mcps/`. Đây là phần đáng học nhất cho agent-hub.
- Muốn "giao việc cho agent" từ bên ngoài thì Hermes dùng các kênh khác: **ACP** (stdio, cho editor), **API server OpenAI-compatible + Runs API** (HTTP/SSE), **A2A** (HTTP JSON-RPC, agent↔agent), **TUI gateway JSON-RPC** (stdio/WebSocket).

---

## 1. Bảng các cách tích hợp

| # | Cách tích hợp | Hướng | Giao thức / transport | Lệnh khởi động | File chính |
|---|---|---|---|---|---|
| 1 | MCP server (cầu nối nhắn tin) | Vào: client MCP gọi Hermes | MCP qua **stdio** (chỉ stdio) | `hermes mcp serve` | `mcp_serve.py:1-8`, `:719-763`; `hermes_cli/mcp_config.py:1143-1146` |
| 2 | MCP client | Ra: Hermes gọi MCP server khác | stdio, Streamable HTTP, SSE; OAuth 2.1, mTLS | cấu hình `mcp_servers:` trong `config.yaml` | `tools/mcp_tool.py:1-10` (+ ~20 file `tools/mcp_tool_*.py`, ~8k dòng) |
| 3 | Catalog MCP (`optional-mcps/`) | Ra | manifest YAML → ghi vào `mcp_servers` | `hermes mcp install <name>` / `hermes mcp` | `optional-mcps/<name>/manifest.yaml`, `hermes_cli/mcp_picker` |
| 4 | ACP (Agent Client Protocol) | Vào: editor (Zed, VS Code, JetBrains, Buzz) điều khiển Hermes | JSON-RPC qua stdio | `hermes acp` / `hermes-acp` / `python -m acp_adapter` | `acp_adapter/entry.py:163`, `acp_adapter/server.py:534-824`, `acp_adapter/permissions.py` |
| 5 | TUI gateway JSON-RPC | Vào: TUI Ink, Desktop, dashboard, host tùy biến | NDJSON JSON-RPC qua stdio hoặc WebSocket | `hermes --tui`, `hermes serve` | `tui_gateway/server.py`, `tui_gateway/contracts/`, `tui_gateway/AGENTS.md:14-50` |
| 6 | API server OpenAI-compatible | Vào: Open WebUI, LobeChat, curl, CI | HTTP + SSE (`/v1/chat/completions`, `/v1/responses`, `/v1/runs`, `/api/sessions`, `/api/jobs`) | `API_SERVER_ENABLED=true` + `hermes gateway` (cổng 8642) | `gateway/platforms/api_server*.py` (15 file) |
| 7 | A2A (Agent2Agent v1.0) | Hai chiều | HTTP JSON-RPC 2.0, SSE, push webhook có ký HMAC | platform `a2a` (cổng 9900), toolset `a2a` | `plugins/platforms/a2a/adapter.py:241`; `website/docs/user-guide/messaging/a2a.md` |
| 8 | Webhooks | Vào: GitHub, GitLab, Jira, Stripe… kích hoạt một lượt chạy agent | HTTP POST + HMAC (cổng 8644) | `hermes webhook subscribe` | `gateway/platforms/webhook*.py`; `website/docs/user-guide/messaging/webhooks.md` |
| 9 | Nền tảng nhắn tin | Hai chiều | SDK riêng của từng nền tảng (Telegram, Slack, Discord, Teams, Matrix, Email, … khoảng 27 adapter) | `hermes gateway` | `gateway/platforms/base.py:1856` (`BasePlatformAdapter`), `plugins/platforms/<name>/` |
| 10 | Plugin (native) | Mở rộng bên trong process | Python `register(ctx)` | `hermes plugins install/enable` | `hermes_cli/plugins.py:454` (register_tool), `:507` (call_mcp), `:775` (register_platform), `:931` (register_hook) |
| 11 | Portable Agent Plugins v1 | Mở rộng | `plugin.json` + `skills/` + `mcp.json` | `hermes plugins install owner/repo` | `website/docs/developer-guide/plugins/index.md:58-184`; `hermes_cli/agent_plugins.py` |
| 12 | Gateway hooks / shell hooks | Sự kiện đi ra | Thư mục `~/.hermes/hooks/<name>/HOOK.yaml` + `handler.py`; shell hook theo định dạng Claude Code | đặt file vào đúng chỗ là đã bật | `website/docs/user-guide/features/hooks.md:20-370` |
| 13 | Dashboard web | Vào: trình duyệt | REST + WebSocket `/api/pty` (nhúng nguyên TUI thật qua PTY) | `hermes dashboard` | `web/AGENTS.md:7-30`, `hermes_cli/web_server.py`, `hermes_cli/pty_bridge.py` |
| 14 | Nhúng in-process | Vào | Python import `run_agent.AIAgent` | — | `programmatic-integration.md:225-230` |
| 15 | Import cấu hình từ Claude Code / Codex | Một lần | đọc `~/.claude.json` `mcpServers` → `mcp_servers` | `hermes import-agent claude-code` | `hermes_cli/agent_import.py:1-6`, `:307-311`, `:459` |

---

## 2. Hermes làm MCP server — `mcp_serve.py`

### 2.1 Cách hoạt động
- Dùng `mcp.server.MCPServer` của SDK Python. Mỗi method được đăng ký bằng `@server.tool()`; docstring thành description, signature thành input schema (`mcp_serve.py:27-36`).
- **Chỉ có stdio**: `run_mcp_server()` gọi `server.run_stdio_async()` (`mcp_serve.py:742-763`). Tài liệu ghi rõ đây là giới hạn hiện tại: "stdio-only MCP server today" (`website/docs/user-guide/features/mcp.md:1092-1097`).
- Dữ liệu được đọc thẳng từ SQLite `~/.hermes/state.db`. Một `EventBridge` chạy nền, poll khoảng 200 ms và dùng `PRAGMA data_version` để bỏ qua các lần DB không đổi (`mcp_serve.py:53-90`, `:296-330`, `:430-472`). Đọc thì không cần gateway chạy. Gửi tin thì cần gateway chạy, và đi qua `tools/send_message_tool.py` (`mcp_serve.py:635-659`).
- Docstring ghi rằng bề mặt này được làm cho khớp với "9-tool channel bridge" của OpenClaw (`mcp_serve.py:5-6`).

### 2.2 Danh sách tool (`mcp_serve.py:719-724`)
| Tool | Chức năng |
|---|---|
| `conversations_list`, `conversation_get` | Liệt kê và xem hội thoại trên các nền tảng |
| `messages_read`, `attachments_fetch` | Đọc lịch sử tin nhắn và tệp đính kèm |
| `events_poll`, `events_wait` | Poll hoặc long-poll sự kiện mới theo cursor |
| `messages_send` | Gửi tin tới `platform:chat_id` |
| `channels_list` | Liệt kê các đích có thể gửi tới |
| `permissions_list_open`, `permissions_respond` | Liệt kê và trả lời yêu cầu phê duyệt |

**Điểm đáng chú ý (đọc thấy trong code):** `_pending_approvals` được khai báo với chú thích "populated from events" (`mcp_serve.py:313`). Tuy vậy, `_poll_once` chỉ enqueue sự kiện `message` (`mcp_serve.py:462-466`), và không có chỗ nào ghi vào `_pending_approvals` hay phát `approval_requested`. `respond_to_approval` cũng tự ghi là "best-effort without gateway IPC" (`mcp_serve.py:368-376`). **[Suy luận]** Vì vậy hai tool `permissions_*` thực tế gần như không hoạt động: phê duyệt chưa đi xuyên qua MCP server.

### 2.3 Cấu hình phía client (Claude Code)
```json
{ "mcpServers": { "hermes": { "command": "hermes", "args": ["mcp", "serve"] } } }
```
(`mcp_serve.py:7`, `website/docs/user-guide/features/mcp.md:1018-1044`)

---

## 3. Hermes làm MCP client

### 3.1 Kiến trúc
- Một event loop nền duy nhất (`_mcp_loop`) chạy trong daemon thread. Mỗi server là một Task sống lâu (`MCPServerTask`) (`tools/mcp_tool.py:1-10`).
- Config được đọc ở `_load_mcp_config()` (`tools/mcp_tool_config.py:628+`):
  - `${VAR}` được thay từ env và `.env`.
  - Ở safe mode hoặc safe-worker thì trả về rỗng (tắt hết MCP).
  - Server lấy từ plugin ("portable") được gộp vào, và config gốc thắng khi trùng tên (`:613-625`).
  - Mọi entry đi qua `validate_mcp_server_entry`, chặn ba dạng cấu hình có vẻ để rò rỉ dữ liệu: IOC đã biết, shell kèm network egress, shell ghi vào chỗ tạo persistence (`tools/mcp_tool_config.py:597-610`, `hermes_cli/mcp_security.py:89-114`).
- **Tên tool**: `mcp__<server>__<tool>`, cắt về tối đa 64 ký tự và thêm hash nếu dài hơn (`tools/mcp_tool_schema.py:159`, `:172-184`). Cách đặt tên này giống quy ước của Claude Code. Lưu ý: `features/mcp.md:568-580` vẫn ghi `mcp_<server>_<tool>`; **tài liệu đó đã lỗi thời so với code**.
- Mỗi server có toolset runtime riêng `mcp-<server>`. Server cũng có thể gửi `notifications/tools/list_changed` để Hermes tự refresh danh sách tool (`features/mcp.md:769-799`).
- Discovery mở tối đa 4 kết nối song song (`mcp.discovery_concurrency`) (`tools/mcp_tool_discovery.py:27-36`).
- Có thêm các tool tiện ích `list_resources`, `read_resource`, `list_prompts`, `get_prompt`. Chúng chỉ được đăng ký khi server thực sự hỗ trợ resources hoặc prompts (`features/mcp.md:597-617`).

### 3.2 Ví dụ cấu hình (`~/.hermes/config.yaml`)
```yaml
mcp:
  discovery_concurrency: 4
mcp_servers:
  # stdio
  filesystem:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/srv/project"]
    env: { }                       # chỉ env khai báo + baseline an toàn được truyền
    idle_timeout_seconds: 900      # tái chế server tốn RAM
  # HTTP nội bộ, header bearer, read-only → cho chạy song song
  cong_viec_noi_bo:
    url: "https://mcp.internal.example/mcp"
    headers: { Authorization: "Bearer ${INTERNAL_MCP_TOKEN}" }
    identity_header: { name: "X-User-Id", value_from: profile }
    supports_parallel_tool_calls: true
    lazy: true                     # đăng ký từ schema cache, spawn khi gọi lần đầu
    tools:
      include: ["*_tim*", "*_doc", "boi_canh"]   # glob
      prompts: false
  # OAuth 2.1 (DCR/CIMD tự động)
  linear:
    url: "https://mcp.linear.app/mcp"
    auth: oauth
  # server không tin cậy: mọi tool không có readOnlyHint phải được duyệt
  vendor_x:
    url: "https://mcp.vendor.example/mcp"
    trust: untrusted
    sampling: { enabled: false }
```
Nguồn của các key: `features/mcp.md:482-506` (bảng key), `:428-480` (mTLS, identity_header), `:619-735` (lọc tool), `:950-998` (sampling, elicitation); `reference/mcp-config-reference.md:71` (`trust`).

### 3.3 Các cơ chế đáng học
- **Trust tier** `full` hoặc `untrusted`. Với `untrusted`, mọi tool thiếu `readOnlyHint: true` phải được phê duyệt trước khi chạy. Cổng duyệt này đứng **trước mọi thao tác transport**, kể cả lazy spawn (`tools/mcp_tool_handlers.py:60-85`, `:573-577`). Giá trị trust không nhận ra được sẽ bị coi là `untrusted`, tức fail-closed (`tools/mcp_tool_registration.py:38-44`).
- **Retry khi session hết hạn**: chỉ tool `readOnlyHint: true` được gọi lại tự động. Tool có thể ghi sẽ trả `outcome_uncertain` (`mcp-config-reference.md:71`).
- **Làm sạch kết quả**: bỏ ký tự Unicode TAG ẩn (một kênh prompt-injection), lọc khóa `_meta` thuộc phạm vi protocol (`features/mcp.md:586-591`). Description của tool được quét theo các mẫu injection (`tools/mcp_tool_schema.py:32-41`).
- **Env của server stdio** chỉ gồm whitelist `PATH, HOME, USER, LANG, LC_ALL, TERM, SHELL, TMPDIR, XDG_*` cộng với `env` khai báo (`tools/mcp_tool_config.py:147-192`).
- **Sampling**: server có thể nhờ Hermes gọi LLM. Có rate limit theo rpm, giới hạn token, giới hạn số vòng tool, và `allowed_models` (`tools/mcp_tool_sampling.py:88`, `features/mcp.md:950-981`).
- **Elicitation**: form-mode được chuyển qua bề mặt phê duyệt sẵn có (CLI, nút Telegram/Slack). URL-mode bị từ chối (`tools/mcp_tool_sampling.py:254`, `features/mcp.md:983-998`).
- **Hot reload**: gateway theo dõi `config.yaml` và tự kết nối hoặc ngắt server trong khoảng 1 phút. Server lỗi được retry với backoff từ 30 s, nhân đôi dần tới 10 phút (`features/mcp.md:777-789`).

### 3.4 Catalog `optional-mcps/`
- Mỗi entry là `optional-mcps/<name>/manifest.yaml` với `manifest_version`, `transport` (http hoặc stdio), `auth` (none, oauth, env), `suggest` (keywords, hosts), `post_install`, và tùy chọn `tools.default_enabled` hoặc `default_excluded`. Ví dụ: `optional-mcps/atlassian/manifest.yaml`, `optional-mcps/context7/manifest.yaml`.
- Trust model: nằm trong thư mục này nghĩa là Nous đã duyệt qua PR. Không có tầng cộng đồng tự gửi (`features/mcp.md:58-128`, `:192-210`).
- Khi cài, Hermes probe server rồi hiện checklist chọn tool và ghi kết quả vào `tools.include` (`features/mcp.md:153-190`).

---

## 4. ACP — cắm vào editor

- Lệnh: `hermes acp`. Transport là JSON-RPC qua stdio; stdout dành riêng cho giao thức, log đi stderr (`website/docs/developer-guide/acp-internals.md:22-33`).
- Các method: `initialize`, `authenticate`, `new_session(cwd, mcp_servers)`, `load_session`, `prompt`, cùng fork, list, cancel (`acp_adapter/server.py:534`, `:559`, `:622`, `:629`, `:824`).
- **Editor có thể truyền MCP server riêng** khi gọi `session/new`. Các server này gắn với session; spec (gồm cả secret) chỉ giữ trong RAM. Khi resume, server phải khớp fingerprint, nếu không thì bị từ chối (`website/docs/user-guide/features/acp.md:62-80`).
- **[Đọc thấy, đáng chú ý]** Ở phiên bản này, `hermes acp` chạy như một "viewer" của gateway daemon. Process ACP không tự discover MCP; gateway khởi động các `mcp_servers` (`acp.md:337-357`).
- Toolset mặc định là `hermes-acp` (file, terminal, web, memory, skills, delegate…), không có messaging và cron. Ghi đè bằng `platform_toolsets.acp` (`acp.md:28-60`).
- Phê duyệt: `allow_once`, `allow_session`, `allow_always`, `deny`, `deny_always` được ánh xạ sang `once`, `session`, `always`, `deny` của Hermes. Hết giờ hoặc lỗi thì mặc định deny (`acp_adapter/permissions.py:18-74`; `acp.md:386-416`).
- Cấu hình cho Zed:
```json
{ "agent_servers": { "hermes-agent": { "type": "custom", "command": "hermes", "args": ["acp"] } } }
```
(`acp.md:241-258`). VS Code dùng extension "ACP Client" với `acp.agents` (`acp.md:218-238`).

---

## 5. Gateway nhắn tin — adapter và session theo từng chat

- `gateway/run.py` là facade. Các pha nằm ở `run_*.py`, session ở `session*.py`, phân quyền ở `authz_mixin.py`, adapter ở `platforms/<name>.py` kế thừa `platforms/base.py` (`gateway/AGENTS.md:6-13`).
- `BasePlatformAdapter` (`gateway/platforms/base.py:1856`) có các method trừu tượng `connect`, `disconnect`, `send` (`:2674-2686`). Adapter mới nên viết thành plugin (`plugins/platforms/<name>/adapter.py` + `plugin.yaml`) và đăng ký qua `ctx.register_platform()`, không cần sửa core (`gateway/platforms/ADDING_A_PLATFORM.md:5-15`). Hiện đã có khoảng 27 platform plugin trong `plugins/platforms/`.
- **Session key mỗi chat** được tạo ở `build_session_key` (`gateway/session.py:686-728`) theo dạng `<ns>:<platform>:<chat_type>[:<slack scope>][:<chat_id>][:<thread_id>][:<user>]`. DM tách theo chat_id. Group tách theo từng user nếu bật `group_sessions_per_user`. Thread mặc định dùng chung.
- Có hai lớp chặn tin nhắn khi agent đang chạy: adapter xếp hàng tin mới, còn runner chặn các lệnh `/stop`, `/approve`, `/deny`… (`gateway/AGENTS.md:19-27`).
- Mỗi sự kiện đến có một identity duy nhất (`session_identity.resolve_identity`) để định tuyến đúng profile khi một process phục vụ nhiều profile (`gateway/AGENTS.md:85-110`).
- **API server** (`gateway/platforms/api_server*.py`) cũng là một "platform" của gateway:
  - Endpoint `/v1/chat/completions` và `/v1/responses` tương thích OpenAI.
  - `/v1/runs` có SSE events, `stop`, `approval`, `clarify`, và idempotency key được lưu bền.
  - `/api/sessions/*` để fork, chat, chat/stream.
  - `/v1/skills` và `/v1/toolsets` để khám phá năng lực.
  - Auth bằng bearer `API_SERVER_KEY` (bắt buộc), mặc định bind `127.0.0.1:8642`. Có thể định tuyến nhiều profile qua `/p/<profile>/…`, mỗi profile một key riêng. Mặc định tối đa 10 run đồng thời (`website/docs/user-guide/features/api-server.md:17-55`, `:544-760`, `:787-935`).

---

## 6. TUI gateway và web/

- `tui_gateway` là backend Python dùng chung cho TUI Ink, Desktop (qua WebSocket) và dashboard `/chat`. Giao thức là NDJSON JSON-RPC hai chiều: server có thể *hỏi ngược* client các loại `approval`, `clarify`, `sudo`, `secret` (`tui_gateway/AGENTS.md:7-28`).
- Contract được khai báo bằng Pydantic (`tui_gateway/contracts/`) rồi sinh ra TypeScript và OpenRPC bằng `scripts/gen_gateway_contracts.py` (`tui_gateway/AGENTS.md:30-41`).
- `web/` là SPA dashboard. Nó **nhúng nguyên TUI thật** qua xterm.js và WebSocket `/api/pty` (PTY), không viết lại giao diện chat bằng React (`web/AGENTS.md:7-25`). `hermes serve` là bản headless chỉ có API/WS, dành cho Desktop (`web/AGENTS.md:31-37`).

---

## 7. Plugin, hook, đóng gói

- Các loại plugin: general (`register(ctx)`), memory provider, model provider, context engine, image-gen, platform adapter. Mỗi loại có cơ chế discovery riêng (`plugins/AGENTS.md:63-75`).
- Hàm của `ctx`: `register_tool`, `register_hook`, `register_cli_command`, `register_platform`, `call_mcp` (`hermes_cli/plugins.py:454, 507, 631, 775, 931`).
- Hook chính, xem catalog đầy đủ ở `website/docs/user-guide/features/hooks.md:445-496`:
  - `pre_tool_call` trả về `block`, `approve` hoặc `modify`. Hook timeout hay ném lỗi thì **fail-closed** (`hooks.md:546-600`).
  - `post_tool_call`, `transform_tool_result`, `pre_llm_call` (chèn context), `post_llm_call`.
  - `on_session_start/end`, `subagent_start/stop`, `pre_gateway_dispatch`, `pre/post_approval_request`, `on_human_input_request`, cùng các hook `kanban_*`.
- Shell hook nhận được định dạng của Claude Code (`{"decision": "modify", "tool_input": …}`) (`hooks.md:590-596`).
- **Cách ly plugin**: đặt `plugins.isolation: host` thì plugin ngoài chạy trong một process host riêng cho mỗi profile, giao tiếp bằng framed JSON (`plugins/AGENTS.md:96-122`).
- **Portable Agent Plugins v1**: `plugin.json` + `skills/*/SKILL.md` + `mcp.json` (stdio hoặc streamable-http). Package có thể tự hạ trust server của mình xuống `untrusted` nhưng không thể nâng lên (`developer-guide/plugins/index.md:58-184`).
- **`plugin-catalog/`**: mỗi entry là một YAML với SHA 40 ký tự bắt buộc, được người merge qua PR, không cho phép code tự cập nhật. `removed.yaml` là danh sách cấm (`plugin-catalog/README.md:8-40`, ví dụ `plugin-catalog/jev-mcp-router.yaml`).
- Ví dụ plugin bảo mật có sẵn: `plugins/security-guidance/plugin.yaml` dùng hook `pre_tool_call` và `transform_tool_result` để cảnh báo pattern nguy hiểm khi ghi file.

---

## 8. Bảo mật

Tài liệu mô tả 8 lớp (`website/docs/user-guide/security.md:11-22`):

| Lớp | Cơ chế | Tham chiếu |
|---|---|---|
| Ai được nói chuyện với agent | Allowlist theo nền tảng (`TELEGRAM_ALLOWED_USERS`…), `GATEWAY_ALLOWED_USERS`, DM pairing. **Mặc định deny** | `security.md:418-466`; `gateway/authz_mixin.py:631` |
| Lệnh nguy hiểm | `approvals.mode: smart` (LLM phụ đánh giá rủi ro) hoặc `manual` hoặc `off`. Mặc định `timeout: 300`. Ngữ cảnh không có người (cron, `-q`, webhook/API) mặc định `deny` | `security.md:24-63` |
| Blocklist cứng | `rm -rf /`, fork bomb, `mkfs`, `dd`, `curl \| sh`… luôn bị chặn, kể cả khi bật yolo | `security.md:133-155`; `tools/approval.py` |
| Deny rule do người dùng đặt | `approvals.deny` (glob, sau chuẩn hóa và gỡ obfuscation) | `security.md:157-184` |
| Allowlist lâu dài | `command_allowlist` trong config (được ghi khi chọn "always") | `security.md:281-300`; `tools/approval_floors.py:189` |
| Ghi file an toàn | Đường dẫn được bảo vệ, có `HERMES_WRITE_SAFE_ROOT` | `security.md:362-410` |
| Sandbox | Backend terminal `docker`/`singularity`/`modal`/`daytona`/`vercel_sandbox`. Docker chạy với `--cap-drop ALL`, `no-new-privileges`, `pids-limit 256`, tmpfs. Không chuyển env nào vào trừ khi khai báo `docker_forward_env`. **Trong container thì bỏ qua kiểm tra lệnh nguy hiểm** (container được coi là ranh giới) | `security.md:544-606`; `tools/environments/docker.py:307` |
| MCP | Lọc env cho stdio, trust tier, lọc include/exclude, quét description, `mcp_security` | mục 3 |
| A2A | Không có token thì chỉ nghe localhost. Token riêng cho từng peer, rate limit, lọc injection, ẩn credential ở đầu ra, audit log `a2a_audit.jsonl`, chặn ping-pong giữa hai agent | `messaging/a2a.md:118-128` |
| API server | Bearer bắt buộc, CORS theo allowlist, key riêng cho từng profile | `api-server.md:857-894` |

Ghi chú: thư mục `docker/` ở root là để **đóng gói chính Hermes thành image** (s6-overlay, entrypoint). Sandbox cho lệnh terminal nằm ở `tools/environments/docker.py` (`docker/entrypoint.sh:1-27`).

---

## 9. Áp dụng cho agent-hub

### 9.1 Nhận xét từ Hermes
1. **Hermes tách hai vai trò rất rõ.** MCP *client* là bề mặt chính để lấy năng lực từ bên ngoài. Còn để *cho bên ngoài giao việc* thì Hermes dùng ACP, HTTP/Runs hoặc A2A, **không** dùng MCP: MCP server của Hermes chỉ là cầu nối nhắn tin. **[Suy luận]** Lý do có thể là MCP tool call vốn ngắn và đồng bộ, khó chứa một lượt agent dài có streaming và phê duyệt giữa chừng. Hermes giải quyết chuyện đó bằng Runs API (run_id, events, approval, stop).
2. Phía client có những chi tiết chín muồi nên làm theo: tên `mcp__server__tool`, `include`/`exclude` dạng glob, `trust: untrusted` dựa trên `readOnlyHint`, `lazy` với schema cache, `supports_parallel_tool_calls` cho server read-only, env whitelist, quét injection, hot-reload config.
3. Tổ chức theo profile hoặc tenant cùng session key xác định (`build_session_key`) là cách cho nhiều agent dùng chung một process mà không lẫn dữ liệu.

### 9.2 Đề xuất kiến trúc
```
Claude Code / hệ khác ──MCP (stdio + Streamable HTTP)──▶ [agent-hub: MCP SERVER]
                                                            │  tools: hub_list_agents, hub_run(agent, task)→run_id,
                                                            │         hub_run_status/events(run_id, cursor), hub_approve,
                                                            │         hub_cancel, hub_memory_search, ...
                                                            ▼
                                                     [hub core: registry agent, run queue, session store, policy]
                                                            │
                                                            ▼  MCP CLIENT (giống mcp_servers của Hermes)
                     MCP nội bộ (quan-ly-cong-viec, vb, theo-doi, ...) — đều read-only → trust/parallel/lazy
```
- **agent-hub làm MCP server**, ưu tiên chạy được ngay trong Claude Code giống Serena:
  - Hỗ trợ **stdio** để Claude Code spawn trực tiếp, và **Streamable HTTP** để nhiều client hoặc máy dùng chung một hub. Hermes chỉ có stdio, và tài liệu của họ tự ghi đó là giới hạn (`features/mcp.md:1094`).
  - Tool giao việc dùng mô hình **run bất đồng bộ** theo kiểu `/v1/runs` của Hermes: `hub_run` trả `run_id` ngay, rồi client gọi `hub_events(run_id, after_cursor)` hoặc long-poll giống `events_wait` (`mcp_serve.py:349-361`). Cách này tránh timeout của MCP tool call. **[Suy luận]**
  - Phê duyệt phải được nối thật tới runtime. Không lặp lại tình trạng của Hermes, nơi `permissions_*` không có nguồn sự kiện (mục 2.2). Có thể dùng MCP **elicitation** để hỏi ngược người dùng trong Claude Code. **[Suy luận]**
  - Kèm `instructions` và resource hướng dẫn, giống cách các MCP nội bộ của người dùng đang làm (`boi_canh`, `*://huong-dan`).
- **agent-hub làm MCP client** để dùng các MCP nội bộ:
  - Schema cấu hình mượn của Hermes: `command/args/env` hoặc `url/headers`, `tools.include/exclude` (glob), `trust`, `lazy`, `supports_parallel_tool_calls`, `timeout`, `identity_header`.
  - Vì các MCP nội bộ đều read-only, đặt `supports_parallel_tool_calls: true` và để `trust: untrusted` làm mặc định. Như vậy, nếu sau này có tool ghi (ví dụ `viec-nhom-2` cho tạo/sửa nhiệm vụ) mà không có `readOnlyHint`, nó sẽ tự động phải qua phê duyệt.
  - Cần một lệnh import `mcpServers` từ `~/.claude.json`, giống `hermes import-agent claude-code` (`hermes_cli/agent_import.py:307-311`). Lệnh này phải bỏ các env trông như secret.
  - Mỗi agent trong hub là một profile với tập MCP riêng (lọc theo `platform_toolsets`) và session key xác định.

### 9.3 Thứ tự ưu tiên giao thức
1. **MCP** (server và client), làm trước. Đây là giao thức chung với Claude Code và với toàn bộ hệ nội bộ hiện có.
2. **HTTP Runs API + SSE** (lấy từ thiết kế `/v1/runs` của Hermes), làm song song hoặc ngay sau. Phục vụ dashboard, cron, webhook và các hệ không nói MCP. Có idempotency key và endpoint approval. Có thể thêm một lớp tương thích OpenAI nếu cần Open WebUI.
3. **A2A**: khi cần nối hub với agent ở máy hoặc framework khác. Lấy mô hình bảo mật của Hermes làm mẫu (token riêng cho từng peer, mặc định localhost, audit, anti-loop).
4. **ACP**: chỉ làm khi cần cắm vào Zed hoặc JetBrains. Claude Code đã được phủ bằng MCP. **[Suy luận]**
5. Hook theo kiểu `pre_tool_call` (block, approve, modify; fail-closed) làm điểm cắm chính sách và audit trong lõi hub. Đây cũng là nền cho "harness tự cải thiện" (thu thập `post_tool_call`, `post_llm_call`). **[Suy luận]**
