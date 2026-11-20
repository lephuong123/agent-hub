# Plan: chuyển xử lý MCP bên thứ ba sang `claude` (MCP do claude quản lý theo tài khoản)

Trạng thái: PLAN (2026-11-20). Chưa làm. Phụ thuộc nhánh connector theo tài khoản (`accounts/claude_config.py`, v0.2.27).

## 1. Kết luận phân tích

**Chưa bỏ hẳn được MCP khỏi cfharness, và không nên bỏ hẳn.** Tách làm hai phần:

| Phần | Bỏ được? | Lý do |
|---|---|---|
| Server MCP của chính hub: `hub-guard` (permission-prompt-tool), `hub-self` (task/manager/knowledge/learning/`fs_*`), mục proxy theo run | **Không** | Sinh mới mỗi run, mang `--run`, socket, vai trò, cờ `--fs`, khoá proxy theo run. `--mcp-config <runtime/mcp.json>` phải giữ vĩnh viễn |
| Server MCP bên thứ ba: lưu cấu hình + đăng nhập OAuth | **Được một phần** | `claude` tự lưu (`claude mcp add-json`) và tự đăng nhập (`claude mcp login <name> --no-browser`) theo `CLAUDE_CONFIG_DIR` của từng tài khoản |
| Lớp an toàn trên MCP bên thứ ba: chọn server theo agent, trust/require_approval/annotation, audit `tool.write`, host sandbox, khung quyền điều phối viên | **Không** | `claude` không có các khái niệm này; bỏ registry thì mọi tool thành `mcp_unknown_tool` (luôn hỏi người) hoặc phải tin theo tên |

Đã kiểm thật (`claude` 2.1.329, 2026-11-20):
- `--setting-sources ""` KHÔNG chặn MCP user-scope trong `~/.claude.json` lẫn connector claude.ai — chỉ `--strict-mcp-config` chặn.
- `--strict-mcp-config` chặn CẢ connector claude.ai (0 server) → run muốn dùng MCP do claude quản lý phải bỏ strict (như chế độ connector hiện có) và dựa vào guard.
- `--mcp-config` mặc định là CỘNG THÊM → server của hub vẫn vào được cùng server do claude quản lý.
- `claude mcp login <name> --no-browser`: in URL, dán lại URL chuyển hướng → dùng lại được khung PTY của `accounts/login.py`.

Rủi ro lớn nhất nếu chuyển ngây thơ: secret của server bên thứ ba sẽ nằm RÕ trong `<config_dir>/.claude.json`, mà hiện agent chỉ bị chặn GHI file này, không bị chặn ĐỌC.

## 2. Mục tiêu

Giữ registry hub làm nơi giữ **metadata an toàn** (server nào cho agent nào, trust, require_approval, include/exclude, annotation đã probe),
nhưng cho phép **nguồn** của một server là `claude` (cấu hình + token do claude giữ trong config dir của tài khoản) thay vì hub.
Sau khi chuyển xong, gỡ OAuth + proxy tự viết của hub (`oauth.py` 752 + `oauth_proxy.py` 431 + `oauth_cli.py` 145 + `web/mcp_oauth.py` 117 dòng).

Không làm: bỏ `--mcp-config`, bỏ strict cho run judge/eval/review/curator/oneshot (luôn giữ strict + rỗng/hub-self).

## 3. Các bước (mỗi bước một PR, đều có test không gọi `claude` thật)

### PR0 — Spike còn lại (thủ công, ghi kết quả vào file này)
- Token của `claude mcp login` lưu ở đâu (Keychain theo config dir?), `-p` dùng lại được không, tự làm mới khi hết hạn.
- Server local-scope (`projects[<cwd>].mcpServers`) có nạp dưới `--setting-sources user` không.
- `--disallowedTools "mcp__<server>__*"` có gỡ hẳn server khỏi ngữ cảnh (không connect) không.
- `system/init.mcp_servers[].status` các giá trị (connected / needs-auth / failed).

### PR1 — Tiền đề an toàn (làm trước mọi thứ, có giá trị ngay cả khi dừng ở đây)
- Chặn ĐỌC `~/.claude.json`, `~/.claude/.credentials.json`, `HUB_HOME/claude-config/*/.claude.json|.credentials.json`:
  sandbox `denyRead`, `permissions.deny` Read/Grep/Glob (`guard/runtime.credential_deny_rules`), classifier `sensitive_read` (Bash `cat`/`grep`…).
  Áp cho mọi hub kể cả `trust_full` (thêm vào `TRUST_NEVER_RULES`). Tiến trình `claude` tự đọc vẫn được (sandbox chỉ áp tool của agent).
- `redact`: che theo giá trị các header/env trong `mcpServers` của config dir tài khoản đang dùng (đọc lúc run bắt đầu).
- Test: ca đỏ→xanh trong `tests/test_guard_classifier.py`, `tests/test_sec_*.py`.
- **Đã làm (phần chặn đọc):** module `claude_files.py` (danh sách tệp/thư mục, thuần), rule riêng `claude_credentials` (forbidden, ∈
  `TRUST_NEVER_RULES` + `NEVER_DOWNGRADE_RULES` + `classifier.STICKY_FORBIDDEN_RULES` — khi một lệnh trúng nhiều luật forbidden, luật trust
  không gỡ thắng) thay vì `sensitive_read`/`hub_secret_read` (trust full gỡ được). Thêm so với kế hoạch: bản sao `~/.claude.json*` cạnh
  bản chính, `~/.claude/backups/` (Claude Code chép `.claude.json.backup.<ts>` vào đây), `$CLAUDE_CONFIG_DIR` của daemon; Edit/Write vào các
  tệp này cũng forbidden (Edit trả lại một đoạn nội dung); Bash xét cả dạng `--data=@…`/`-F f=@…`. `permissions.deny` qua
  `runtime.claude_files_deny_rules` (giữ cả khi trust_full); sandbox `denyRead` qua `claude_files.deny_read_paths` (cả `confine_only_sandbox`).
  **Chưa làm:** che theo giá trị header/env `mcpServers` (`redact`).

### PR2 — Registry nhận server nguồn `claude`
- `McpServerConfig.source: hub|claude` (mặc định `hub`, không migration bảng — cột JSON sẵn có; nếu cần cột → migration mới).
- `source=claude`: hub KHÔNG ghi entry vào `mcp.json` của run; chỉ giữ tên + trust/include/exclude/require_approval/annotation/agents.
- Đọc danh sách server của tài khoản: đọc `<config_dir>/.claude.json` (CHỈ tên, transport, url host — không bao giờ header/env), không spawn.
- Run có server `source=claude`: bỏ strict (dùng đường connector mode có sẵn: `--setting-sources user`, guard bắt buộc bật),
  thêm `--disallowedTools mcp__<x>__*` cho MỌI server claude-quản-lý KHÔNG gán cho agent (gỡ khỏi ngữ cảnh) + guard `mcp_not_in_run` giữ nguyên.
- `allowed_mcp` (adapter, `isolation.violation`) = server hub + server claude đã gán + `claude.ai *` + `plugin:` như connector mode.
- `agent_mcp_domains`: lấy host từ url đọc được ở trên.
- Annotation: probe qua run (`system/init` không có annotation) → dùng `cfharness mcp annotate` thủ công + `classify_connector` theo tên làm sàn;
  `mcp_unknown_tool` vẫn hỏi người, "luôn cho phép" theo tool (`mcp_tool` rule) giữ nguyên.
- `ToolWriteWatcher` audit cả server `source=claude` (dựa trên tên server trong registry).
- Test: `tests/test_mcp_registry.py`, `tests/test_connector_auth.py` (claude giả in `system/init` có server lạ → violation; server đã gán → không).

### PR3 — Thêm server + đăng nhập qua `claude` (web + CLI)
- `cfharness mcp add --via claude <name> <url>` / web "Thêm MCP" chọn tài khoản: daemon chạy
  `CLAUDE_CONFIG_DIR=<dir> claude mcp add-json -s user <name> -` (JSON qua stdin, không argv), rồi tạo dòng registry `source=claude`.
- Secret tĩnh: KHUYÊN giữ `source=hub` (secret store + mcp.json 0600). Chỉ OAuth đi đường claude.
- Đăng nhập: RPC `mcp.claude_login_start|finish{account_id, name}` (HUMAN_ONLY, MUTATING) — PTY `claude mcp login <name> --no-browser`
  (tái dùng `accounts/login.start_login(args=...)`), nhận URL chuyển hướng như luồng connector. Đăng xuất: `claude mcp logout`.
- Trạng thái: `claude mcp list` (parse text, có cache) hoặc `connector.unavailable`/`needs-auth` từ `system/init` của run gần nhất.
- Hàng tài khoản trên web: thêm mục "MCP của tài khoản" cạnh connector.
- `HUB_HUMAN_ONLY_CLI`, `auth.HUMAN_ONLY`, `tests/test_sec_auth.py`.

### PR4 — Chuyển dần + gỡ OAuth/proxy của hub
- `cfharness mcp migrate-to-claude <name> --account <id>`: server OAuth của hub → `claude mcp add-json` + `claude mcp login` (người dán URL), đổi `source=claude`, xoá token `MCP_OAUTH_*`.
- Khi không còn server OAuth `source=hub`: gỡ `oauth.py`, `oauth_proxy.py`, `oauth_cli.py`, `web/mcp_oauth.py`, RPC `mcp.oauth_*` (giữ stub trả lỗi hướng dẫn 1 phiên bản), config `mcp.oauth_proxy.*`.
- `hub_transfer`/`backup`: server `source=claude` không mang token — gói ghi `import_followups` "đăng nhập lại MCP X trên tài khoản Y"; doctor báo.
- CHANGELOG mục "Lưu ý khi cập nhật".

### PR5 — Điều phối viên + doctor
- `mcp_registry_list` hiện cả `source`; khung `read_only_trusted` áp như server hub (dựa annotation thủ công).
- `hub doctor`: server claude chưa đăng nhập / tài khoản đang dùng thiếu server được gán.

## 4. Đánh đổi phải chấp nhận
- Server `source=claude` gắn với **tài khoản**, không với hub: đổi tài khoản đang dùng → server đó chỉ có nếu tài khoản mới cũng có (doctor + `connector.auth_fallback`-tương tự cảnh báo).
- Run dùng server `source=claude` không còn `--strict-mcp-config` → mọi MCP khác trong config dir có thể connect (bị `--disallowedTools` gỡ khỏi ngữ cảnh + guard chặn gọi), hook/plugin user cũng nạp như connector mode hiện tại.
- Annotation tự động mất (không probe được khi token do claude giữ) → thêm bước gắn nhãn tay hoặc chấp nhận hỏi người lần đầu + "luôn cho phép".

## 5. Thứ tự đề xuất
PR1 (an toàn, làm ngay) → PR0 spike → PR2 → PR3 → PR4 → PR5. Ước lượng: PR1 nhỏ; PR2–PR3 vừa; PR4 gỡ ~1.450 dòng + test OAuth/proxy (~30 test).
