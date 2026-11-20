# Connector claude.ai cho điều phối viên

Ngày: 2026-10-26. Nhánh `feat/manager-claude-connectors`. Mục tiêu: chế độ chat (điều phối viên) dùng được connector claude.ai
của tài khoản đã đăng nhập (Claude Docs, Atlassian, Google Drive…), ví dụ để đọc link `claude.ai/artifact/...`.

## Bật

```bash
cfharness manager capabilities set --profile hermes --mcp trusted --connectors "Claude Docs,Atlassian" --sync-agent
cfharness manager capabilities set --connectors none     # tắt
```

### Cho agent thường (`tool_policy.connectors`, 2026-11-06)

```bash
cfharness agent edit <id> --connectors "Atlassian,Slack"   # bật cho run của agent này
cfharness agent edit <id> --connectors none                 # tắt
```

Web: form sửa agent, phần "Connector claude.ai" (checkbox + ô "Connector khác"; đổi phải xác nhận). Trang agent báo `thiếu: X` khi
`allowed_tools` có `mcp__claude_ai_X__*` mà `connectors` chưa có X (`web/app.py::connector_gaps`): chỉ cho phép tool thì run vẫn KHÔNG
nạp connector.

Lưu trong `tool_policy.connectors` (cột JSON có sẵn, không cần migration), chuẩn hoá về slug. Chỉ người đặt được: `agent.update`
thuộc `HUMAN_ONLY`; điều phối viên bị chặn vì `connectors` không nằm trong `TOOL_POLICY_FIELDS`. Run thường dùng chung đường đi
với run điều phối viên (`runs.run_connectors` cho cả spec lẫn `GuardContext`), nên cờ cô lập, tắt plugin user, `ToolSearch` và luật
guard bên dưới áp y hệt. Cần `guard.enabled=true`; guard tắt thì run chạy như không có connector.

### Điều kiện cho điều phối viên

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

## Kiểu đăng nhập: connector cần phiên `/login`, không dùng được token `setup-token` (2026-11-20)

Đã kiểm `claude` 2.1.325 (macOS), cùng cờ `--setting-sources user`, không `--strict-mcp-config`:

| Đăng nhập | Server `claude.ai *` trong system/init |
|---|---|
| Phiên `claude /login` (authMethod `claude.ai`) | có (29 connector của tài khoản, mỗi cái `connected`/`needs-auth`) |
| `CLAUDE_CODE_OAUTH_TOKEN` (token `claude setup-token`) | **không có cái nào** |

Hub dùng chung token setup-token (secret `CLAUDE_CODE_OAUTH_TOKEN`, `scripts/setup-hub-user.sh`) → trước bản này, bật
`--connectors` trên hub đó không có tác dụng và không báo gì.

Cách hub xử lý (`RunSpec.auth_mode`, `RunManager._connector_auth_mode`, `ClaudeCliRuntime.login_available`):
- `runtime.claude.connectors_auth` = `auto` (mặc định): run có connector kiểm `claude auth status` **không kèm token** (cache 10 phút).
  Có phiên claude.ai → run chạy bằng phiên đó (gỡ `CLAUDE_CODE_OAUTH_TOKEN` khỏi env của run, cả từ env daemon lẫn `env_extra`).
  Không có → giữ token như cũ + event `connector.auth_fallback{reason}`. `login` = luôn dùng phiên; `token` = không bao giờ.
- Run có connector dùng tài khoản của phiên `/login`, KHÔNG theo `cfharness account use` (đổi tài khoản chỉ chép token).
  Hạn mức/chi phí tính vào tài khoản đó.
- Adapter phát `connector.unavailable{connector, status, api_key_source}` cho mỗi connector người cho phép mà system/init không
  có (`missing`) hoặc chưa `connected` (vd `needs-auth` = chưa nối connector trên claude.ai). Chỉ ở init đầu.
- `cfharness hub doctor --probe` có mục "connector claude.ai" khi có agent/điều phối viên bật connector.

Các bước bật Google Drive cho hub dùng token chung (vd `cchcadmin`, admin.cchchn.com) — NGƯỜI DÙNG TỰ làm trên web hub của họ:
1. Web hub › Cài đặt › Tài khoản Claude › hàng "Đăng nhập của máy" › **Gắn connector** (hoặc tự hiện ngay sau khi thêm tài khoản
   mới — bước "Bỏ qua" được) → mở link, đăng nhập, dán code
   (daemon chạy `claude auth login --claudeai` trong PTY như luồng thêm tài khoản — `account.claudeai_login_start|finish`, HUMAN_ONLY,
   tự audit, code gỡ khỏi params; xong kiểm `claude auth status` không token và xoá cache `login_available`). Cách cũ (terminal của
   admin): `sudo -u <user> -H claude` → `/login`.
2. Trên claude.ai của tài khoản đó: Settings → Connectors → Google Drive → Connect (chọn tài khoản Google).
3. Không cần bật gì thêm (`connectors.auto` mặc định bật). Giới hạn theo agent: chọn connector ở form agent /
   `cfharness agent edit <id> --connectors "Google Drive"` (danh sách riêng thắng `*`).
4. `cfharness hub doctor --probe` → mục "connector claude.ai" phải xanh; chạy thử một run, không có event `connector.unavailable`.

## Tự động: mọi connector tài khoản đã Connect (`connectors.auto`, `--connectors all`) (2026-11-20)

- `connectors.auto` — BẬT MẶC ĐỊNH từ v0.2.26 (chủ repo chốt 2026-11-20, web không có công tắc; tắt: `cfharness config set
  connectors.auto false`, chỉ người, nhánh `connectors` ∈ `integrity.SECURITY_KEYS`). Bộ test chung tắt bằng env
  `CFHARNESS_CONNECTORS_AUTO=off` (conftest — claude giả không hiểu `auth status`). Run LÀM VIỆC của agent
  CHƯA đặt `tool_policy.connectors` tự bật connector `*` = mọi server `claude.ai *` CLI nạp — tức mọi connector tài khoản `/login`
  của user OS đã Connect. Không áp cho điều phối viên (vẫn `manager.capabilities.connectors`, nhận `all`), run kỹ thuật
  (`review.NON_WORK_KINDS`), guard tắt, hay `runtime.claude.connectors_auth=token`.
- Chỉ bật khi `login_available()` = True (`RunManager._auto_connectors`, TRƯỚC `_prepare_spec`); không có phiên → run như cũ
  (không nạp `~/.claude`, không event).
- `--connectors all` (hoặc `*`) cho một agent = như trên nhưng bật tường minh (khi không có phiên → token + `connector.auth_fallback`).
- Guard: `"*"` cho qua mọi `mcp__claude_ai_<x>__<tool>` theo luật connector (đọc safe / ghi write / xoá destructive); MCP khác của
  `~/.claude` vẫn `mcp_not_in_run` (forbidden). Guard đọc connector từ `ActiveRun.connectors` (đúng cái spec đã bật), không tự tính lại.
- Adapter: `"*"` mà không có connector nào `connected` → `connector.unavailable{connector:"*", status:"none_connected"}`.

## Rủi ro chấp nhận

- **Cấu hình user lọt vào run:** hook user (vd `SessionStart`) chạy trong run, CLAUDE.md của user được nạp, plugin synced nạp.
- **Không có lớp duyệt riêng cho connector:** agent gửi, sửa, tạo bằng tài khoản thật của người (Gmail, Slack, Jira…) mà không
  hỏi duyệt, trừ thao tác xoá. Chỉ cho phép connector thật sự cần.
- **Lớp chặn MCP lạ nằm ở hook guard (PreToolUse + permission-prompt-tool):** không có `--disallowedTools` tĩnh cho connector vì
  hub không biết trước tên connector của tài khoản.

Test: `tests/test_manager_connectors.py`.
