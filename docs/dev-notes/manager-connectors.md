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
- ~~Run có connector dùng tài khoản của phiên `/login`, KHÔNG theo `cfharness account use`.~~ Đã đổi: phiên claude.ai theo TÀI
  KHOẢN ĐANG DÙNG — xem "Mỗi tài khoản một connector riêng (2026-11-20)" bên dưới.
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

## Mỗi tài khoản một connector riêng (2026-11-20)

Chủ repo chốt: phiên claude.ai (connector) đi theo TÀI KHOẢN Claude của hub, không còn một phiên chung cho cả user OS. Trước đó
run có connector luôn chạy bằng phiên `/login` mặc định, bất kể `cfharness account use` — hạn mức/chi phí tính vào tài khoản khác.

Sự thật đã kiểm (claude 2.1.325, macOS, spike của chủ repo):
- `CLAUDE_CONFIG_DIR=<dir> claude auth login --claudeai` (qua PTY của `accounts/login.py`) đăng nhập thành công; `auth status --json`
  trong `<dir>` → claude.ai, org KHÁC với thư mục mặc định, thư mục mặc định vẫn đăng nhập nguyên. Keychain có hai mục
  `Claude Code-credentials` (mặc định) và `Claude Code-credentials-<8 hex>` (theo thư mục). Thư mục chỉ có `.claude.json` (0600) +
  `backups/` (không `.credentials.json` với user có GUI; user LaunchDaemon không GUI có thể rơi về file — vẫn bảo vệ cả thư mục).
- Run `CLAUDE_CONFIG_DIR=<dir> --setting-sources user` (không `--strict-mcp-config`) → system/init có connector của CHÍNH tài khoản đó.
- `<dir>/projects` symlink sang `~/.claude/projects`: session tạo dưới `<dir>` nằm trong cây dùng chung; `claude --resume <sid>` dưới
  cấu hình mặc định (tài khoản khác) chạy được. → resume/continue/takeover chéo tài khoản không đổi gì.
- Hết hạn mức của một tài khoản → kết quả "You've hit your 5-hour limit · resets …" — luồng rate limit sẵn có (theo tài khoản) xử lý.

Cách làm (`accounts/claude_config.py`):
- Tài khoản token `a_xxxxxxxx` → `HUB_HOME/claude-config/<id>/` (0700, thư mục cha 0700). `machine` = cấu hình mặc định (không đặt
  `CLAUDE_CONFIG_DIR`). `custom` (token tự đặt ngoài danh sách) không có phiên riêng.
- Trong thư mục: symlink `projects` → `<claude home mặc định>/projects` (tạo đích nếu thiếu), `settings.json`/`CLAUDE.md` → bản mặc
  định nếu có. KHÔNG BAO GIỜ symlink `.claude.json` hay credentials. Dựng lại idempotent trước mỗi lần dùng (symlink sai đích → thay;
  file/thư mục thật ở đó → để nguyên). Claude home mặc định = `transfer_sessions.claude_home()` (`CLAUDE_CONFIG_DIR` của env daemon
  hoặc `~/.claude`). Windows không tạo được symlink → log cảnh báo, session không dùng chung (resume chéo tài khoản hỏng).
- `.claude.json` của thư mục tài khoản không có `mcpServers` của `~/.claude.json` và không có plugin (thư mục `plugins/` không symlink)
  → run connector bằng tài khoản token nạp ÍT cấu hình user hơn run bằng `machine`.

RPC (`account.claudeai_*`, HUMAN_ONLY, tự audit): `status {id?}` (không id = `machine` + `accounts: {<id>: {logged_in, email}}` từ
cache), `login_start {id?}` (id mặc định `machine` — tương thích bản cũ; trả thêm `account_id`, `account_label`), `login_finish
{login_id, code}` (kiểm `auth status --json` trong CÙNG thư mục), `logout {id?}`. Không bao giờ có `CLAUDE_CODE_OAUTH_TOKEN` trong env
của các lệnh này. Lượt đăng nhập dùng chung một chỗ RAM `_logins` với lượt thêm tài khoản; `_Login.account_id` ghi tài khoản được gắn;
`account.login_finish` (token) vẫn từ chối lượt claude.ai. Event `account.claudeai_login{id, logged_in}`.
- Cache `hub_state["accounts.claudeai"] = {id: {logged_in, email, checked_at}}`, cập nhật khi login/logout/`account.check`/status;
  `account.list` trả `claudeai: {logged_in, email}` mỗi tài khoản (null = chưa kiểm; custom = null) — KHÔNG chạy claude mỗi dòng.
- `account.rm` tài khoản token: đóng lượt gắn connector đang mở → `claude auth logout` trong thư mục (best-effort, để Keychain không
  sót mục) → xoá thư mục (chỉ thư mục đó; từ chối symlink/thoát khỏi `claude-config/`; symlink bên trong bị gỡ, không đi theo).
- CLI: `cfharness account connector <id> [login|logout|status]` (login trong terminal như `account login`); `account list` hiện
  "connector <email>"/"connector chưa gắn". Lệnh ∈ `HUB_HUMAN_ONLY_CLI` (classifier).

Run (`RunManager._connector_account`, `_account_login`, `_auto_connectors`, `_connector_auth`):
- Theo TÀI KHOẢN ĐANG DÙNG (`hub.accounts.active_id()`): tài khoản token có phiên trong thư mục riêng → `auth_mode="login"` +
  `RunSpec.claude_config_dir` → `_run_env` đặt `CLAUDE_CONFIG_DIR`, gỡ mọi `CLAUDE_CODE_OAUTH_TOKEN`. `machine`/chưa cấu hình → phiên
  `/login` mặc định như cũ. Còn lại → token + `connector.auth_fallback{reason: account_no_claudeai, account}` (tài khoản token/custom
  chưa gắn) hoặc `no_claude_ai_login` (máy). KHÔNG BAO GIỜ mượn phiên của tài khoản khác (chi phí sẽ tính sang tài khoản đó).
- `connectors.auto`: chỉ bật khi tài khoản đang dùng có phiên. `connectors_auth=login` (luôn): tài khoản token → thư mục của nó;
  `custom` → token + `account_no_claudeai` (không mượn phiên máy, như auto); dựng thư mục lỗi → token + `config_dir_error`.
- Tài khoản đang dùng + thư mục đọc MỘT lần mỗi lượt khởi động (`acct = _connector_account()` trong `_run`, truyền cho
  `_auto_connectors`, `_prepare_spec(connector_dir=)`, `_connector_auth`) — `account.use` giữa chừng không làm các bước lệch nhau.
- Sau khi dựng lại, `<dir>/projects` PHẢI là symlink sang claude home mặc định (`claude_config.shared`); không (vd Windows không tạo
  được symlink) → token + `connector.auth_fallback{reason: config_dir_unshared}` (session sẽ tách đôi, resume chéo tài khoản hỏng);
  doctor `--probe` báo.
- Run chạy bằng phiên claude.ai ghi `ActiveRun.claude_session` (`machine` | id). `account.rm` / `account.claudeai_logout` của tài
  khoản có run đang dùng phiên đó → `Conflict` ("… run đang chạy bằng connector của tài khoản này …", `data.runs`). Run đang xếp hàng
  không chặn: chúng chọn phiên của tài khoản ĐANG DÙNG lúc khởi động, mà tài khoản đang dùng thì không xoá được.
- Hạn mức/chi phí của run connector tính vào tài khoản claude.ai ĐÃ ĐĂNG NHẬP ở hàng đó (hub không đối chiếu được với tài khoản của
  token setup-token — không đọc được email của token rẻ); web/CLI hiện rõ email + tổ chức claude.ai sau khi gắn.
- `login_available(config_dir)` cache theo (binary, thư mục); thư mục chưa tồn tại → coi như chưa đăng nhập, không chạy claude, không tạo.
- `run_setup(conns, config_dir=…)`: hook/MCP "đã biết" cho kiểm cô lập = hợp của cấu hình mặc định và thư mục tài khoản (run có thể
  rơi về token = cấu hình mặc định).
- Run KHÔNG có connector: không đổi (token của tài khoản đang dùng).

Bảo vệ (thư mục giữ OAuth credentials): sandbox denyRead/denyWrite + `HUB_WRITE_DENY_NAMES`, `secret_read_deny_rules` (Read/Grep/
Glob), classifier `sensitive_read` + `sensitive_regions` (Bash `cat/tar/rm` → forbidden; tool Write → `protected_path` như `secrets/`).
`backup.EXCLUDED` (gắn với máy) → không vào bản sao lưu lẫn gói `.cfhx` (gói chỉ lấy `backup.included`), `pathmap.SCAN_SKIP_DIRS`
(home migrate/nhập không đi vào symlink sang `~/.claude`), `support_bundle.NEVER_INCLUDED`. Log/event/audit chỉ có id tài khoản + email.

Doctor: `connectors.auto` tính là "đang dùng connector" (trước đây bị bỏ qua — lỗi chủ repo báo); `--probe` kiểm phiên của tài khoản
đang dùng theo con trỏ `claude_account_active` (doctor không đọc giá trị secret nên không so token).

Lưu ý khi cập nhật: hub đã "Gắn connector" ở hàng "Đăng nhập của máy" (v0.2.25/26) mà đang dùng một tài khoản token (vd "Mặc định"
tạo từ `CLAUDE_CODE_OAUTH_TOKEN`) → run thôi dùng connector (event `connector.auth_fallback{account_no_claudeai}`) cho tới khi gắn
connector cho CHÍNH tài khoản đó (không chép được phiên: credentials ở Keychain theo thư mục).

Test: `tests/test_account_connectors.py` (`tests/fake_claude_auth.py` đọc `CLAUDE_CONFIG_DIR`: `<dir>/.fake_login`), `tests/test_web_accounts.py`.

## Rủi ro chấp nhận

- **Cấu hình user lọt vào run:** hook user (vd `SessionStart`) chạy trong run, CLAUDE.md của user được nạp, plugin synced nạp.
- **Không có lớp duyệt riêng cho connector:** agent gửi, sửa, tạo bằng tài khoản thật của người (Gmail, Slack, Jira…) mà không
  hỏi duyệt, trừ thao tác xoá. Chỉ cho phép connector thật sự cần.
- **Lớp chặn MCP lạ nằm ở hook guard (PreToolUse + permission-prompt-tool):** không có `--disallowedTools` tĩnh cho connector vì
  hub không biết trước tên connector của tài khoản.

Test: `tests/test_manager_connectors.py`.
