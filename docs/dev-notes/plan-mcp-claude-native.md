# Plan: chuyển xử lý MCP bên thứ ba sang `claude` (MCP do claude quản lý theo tài khoản)

Trạng thái: PLAN (2026-11-20). PR1 (#171), PR2, PR3, PR4 giai đoạn A + B (gỡ code OAuth/proxy, 2026-11-21) đã làm; PR5 một phần;
PR6 (gỡ hẳn đường nguồn hub, 2026-11-24, v0.2.32) đã làm. Phụ thuộc nhánh connector theo tài khoản (`accounts/claude_config.py`, v0.2.27).

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

### PR0 — Spike (kết quả 2026-11-20, `claude` 2.1.329)
- Token `claude mcp login` nằm CÙNG mục Keychain với phiên claude.ai (`Claude Code-credentials[-<hash config dir>]`, khoá `mcpOAuth`
  theo tên server) → tách theo `CLAUDE_CONFIG_DIR` như phiên đăng nhập; `-p` dùng lại token đã đăng nhập (cf-db-* connected trong run headless).
- `--disallowedTools "mcp__<server>__*"` gỡ HẾT tool của server khỏi ngữ cảnh (0 tool) nhưng server VẪN connect (tiến trình stdio vẫn spawn) —
  đủ cho chặn dùng; muốn không spawn thì phải không có server trong config dir đó.
- `system/init.mcp_servers[].status` thấy: `connected`, `needs-auth`, `pending`.
- Server local-scope (`claude mcp add -s local` trong thư mục tạm) KHÔNG xuất hiện trong run `-p` (cả `--setting-sources user` lẫn `""`) →
  chỉ dùng scope `user` trong config dir của tài khoản.

Danh sách kiểm ban đầu:
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
- **Đã làm (2026-11-21, nhánh `feat/mcp-claude-redact-register`) — che theo giá trị:** `mcp_client/claude_redact.py`
  (`hub.claude_redact`, gắn ở `mcp_client/wiring.attach`): đọc `mcpServers` (+ `projects[*].mcpServers`) của `.claude.json` và
  `managedMcpServers` của `remote-settings.json` — cấu hình mặc định + MỌI thư mục `claude-config/a_xxxxxxxx/` (quét thư mục, không
  cần DB) — chọn giá trị bằng CÙNG heuristic tách secret của `importer._Extractor` (khoá nhạy cảm, `Bearer …`, entropy cao; env, header,
  args, query của url) + mật khẩu userinfo của url (cả dạng đã giải mã %), rồi `redact.register_secret_values` (≥ `MIN_VALUE_LEN`).
  Làm mới theo `(mtime_ns, size)` — chỉ stat khi không đổi: `Hub.start` + mỗi lần run khởi động (`RunManager._refresh_claude_redact`,
  thêm thư mục của tài khoản đang dùng). Không bao giờ log/trả giá trị (chỉ số file đọc lại). Lệch: giá trị không còn trong file vẫn
  được che tới khi daemon khởi động lại (sổ che chỉ thêm, như SecretStore); header không nhạy cảm (vd `Content-Type`) không che.
  Test: `tests/test_redact_claude_config.py`.

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

**Đã làm (2026-11-20, nhánh `feat/mcp-source-claude`, trên nhánh connector theo tài khoản):**
- `McpServerConfig.source` (trường CÓ SẴN, trước là nguồn gốc `manual|claude-import`) nhận thêm `claude`; `hub` là bí danh của `manual`.
  Lưu trong JSON `config` + cột `source` có sẵn → **không migration**. `source=claude`: cấm command/args/env/headers/auth, `url` bị rút
  về `scheme://host[:port]`, `claude_entry()` ném lỗi, `probe` trả `ok=false` (không kết nối), `update` không đổi được nguồn hub↔claude.
- `mcp_client/claude_source.py`: `read_servers(config_dir)` đọc `mcpServers` TẦNG ĐẦU của `<config_dir>/.claude.json` → CHỈ
  `{transport, host, hub}` (`hub` = trỏ về chính hub, `is_hub_mcp_config`); cấu hình mặc định = `$CLAUDE_CONFIG_DIR/.claude.json` hoặc
  `~/.claude.json` (test: `CFHARNESS_TEST_CLAUDE_HOME`). `account_config_dir(hub, id)`: tài khoản token → `claude-config/<id>/`;
  machine/custom/chưa cấu hình → mặc định.
- RPC `mcp.claude_servers{account_id?}` (HUMAN_ONLY, chỉ đọc) + CLI `cfharness mcp claude-servers [--account]`; `mcp.add{config:{source:
  "claude"}, account_id?}` kiểm tên có trong cấu hình của tài khoản (mặc định đang dùng), lấy transport/host từ đó (bỏ mọi
  command/url/header người truyền), từ chối server trỏ về hub; CLI `cfharness mcp add --source claude <tên> [--account]`.
- `runs.py`: `_agent_claude_mcp` (run kỹ thuật review/eval/curator/graph_extract + bản sao eval → bỏ qua, giữ strict); server nguồn
  claude không vào mcp.json (`build_for_agent_servers` bỏ qua); run có server loại này → đường connector (`run_setup(())` nếu chưa có
  connector: bỏ strict, `--setting-sources user`, plugin user tắt), guard tắt → `RuntimeError_` (run failed); thiếu server trong
  `.claude.json` của tài khoản đang dùng → `HubError` nêu tên tài khoản + đường dẫn (fail-closed, như thiếu secret);
  `--disallowedTools mcp__<tên đã chuẩn hoá>__*` cho mọi server trong `.claude.json` không gán (và không trùng server hub);
  `CLAUDE_CONFIG_DIR` = thư mục tài khoản token (dựng lại symlink, phải `shared`) CẢ ở auth_mode token (`claude_cli._run_env`), machine/
  custom → mặc định; `ActiveRun.claude_session` = tài khoản (chặn đăng xuất/xoá khi run chạy), `ActiveRun.claude_mcp`.
- Adapter: `RunSpec.claude_mcp_servers` → event MỚI `mcp.unavailable{server, status, source}` (summary) khi server đã gán `missing` hoặc
  không `connected`/`pending` (vd `needs-auth`) — không dùng lại `connector.unavailable` (payload/nghĩa là connector claude.ai).
  `allowed_mcp` = mcp_servers của agent (gồm server claude đã gán) + server user-scope từ `run_setup`; server ngoài đó → `isolation.violation`.
- Guard: `_connector_ctx` coi run có server claude là run connector (`connectors=()` nếu không có connector) → MCP khác = `mcp_not_in_run`.
  `registry.tool_annotations` trả bản ghi cho MỌI tool của server nguồn claude (`source`, `probed`, trust, require_approval, selected,
  ghi đè người). `classify_mcp`: tool bị loại bởi include/exclude (glob) → `mcp_not_in_run` forbidden; protected / destructiveHint /
  tên xoá / require_approval / readOnlyHint như cũ; rồi suy theo tên: tên đọc (`_CONNECTOR_READ_RE`) → `mcp_claude_read_name` (safe,
  trừ khi người đặt readOnlyHint=false), còn lại → server `trusted`: `mcp_claude_write` (write, audit), untrusted: `mcp_untrusted_write`
  (hỏi) — KHÔNG `mcp_unknown_tool`. Sandbox: `agent_mcp_domains` lấy host từ `url` đã lưu.
- `ToolWriteWatcher`: audit tool ghi của server nguồn claude (tên dạng đọc bỏ qua như guard), `annotations_known=false`, `data.source`.
- `mcp_registry_list` của điều phối viên có `source: hub|claude`. `hub doctor`: server nguồn claude không probe (báo "nguồn claude").
- Test: `tests/test_mcp_source_claude.py` (14 ca, gồm claude giả qua `ClaudeCliRuntime` đọc `CLAUDE_CONFIG_DIR`),
  `tests/test_sec_auth.py::test_mcp_claude_servers_human_only`.

Lệch so với plan: dùng lại trường `source` có sẵn (giá trị cũ giữ nguyên, không đổi mặc định thành chữ `hub`); server untrusted không
"suy theo tên → write" mà hỏi duyệt như mọi tool ghi của server untrusted (giữ ý nghĩa trust); thêm event `mcp.unavailable` thay vì
dùng lại `connector.unavailable`; chặn đăng ký server trỏ về hub (H-18). Việc dở: web chưa có nút "đăng ký nguồn claude" (PR3), PR1
(chặn agent ĐỌC `~/.claude.json` của cấu hình MẶC ĐỊNH — dùng khi tài khoản machine/custom) chưa có trên nhánh này; `claude-config/<id>/` đã bị chặn đọc/ghi bởi nhánh connector theo tài khoản.

### PR3 — Thêm server + đăng nhập qua `claude` (web + CLI)
- `cfharness mcp add --via claude <name> <url>` / web "Thêm MCP" chọn tài khoản: daemon chạy
  `CLAUDE_CONFIG_DIR=<dir> claude mcp add-json -s user <name> -` (JSON qua stdin, không argv), rồi tạo dòng registry `source=claude`.
- Secret tĩnh: KHUYÊN giữ `source=hub` (secret store + mcp.json 0600). Chỉ OAuth đi đường claude.
- Đăng nhập: RPC `mcp.claude_login_start|finish{account_id, name}` (HUMAN_ONLY, MUTATING) — PTY `claude mcp login <name> --no-browser`
  (tái dùng `accounts/login.start_login(args=...)`), nhận URL chuyển hướng như luồng connector. Đăng xuất: `claude mcp logout`.
- Trạng thái: `claude mcp list` (parse text, có cache) hoặc `connector.unavailable`/`needs-auth` từ `system/init` của run gần nhất.
- Hàng tài khoản trên web: thêm mục "MCP của tài khoản" cạnh connector.
- `HUB_HUMAN_ONLY_CLI`, `auth.HUMAN_ONLY`, `tests/test_sec_auth.py`.

**Đã làm (2026-11-20, nhánh `feat/mcp-claude-login`, trên PR2):**
- Kiểm thật `claude` 2.1.329: `mcp add-json -s user <tên> -` đọc JSON từ stdin nhưng TỪ CHỐI khi có biến phiên Claude Code (`CLAUDECODE`…) →
  env bỏ mọi `CLAUDE_CODE_*`/`CLAUDECODE`/`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`; `mcp list` in `tên: <url|lệnh> (HTTP) - ✓ Connected |
  ! Needs authentication | ✘ Failed to connect — …` (in cả url/lệnh → hub chỉ lấy trạng thái); `mcp login --no-browser` in link authorize
  (OSC 8 + chữ, host tuỳ server) + "Or paste the redirect URL here:", URL sai → "That doesn't look like a redirect URL…" rồi hỏi lại (không
  thoát); `mcp remove|logout` tên lạ → exit 1. `redirect_uri` của claude là `http://localhost:<cổng>/callback` (claude cũng nghe cổng đó —
  đăng nhập trên trình duyệt của CHÍNH máy hub có thể tự xong; khi đó `finish` báo tiến trình đã thoát → bấm Kiểm tra).
- `mcp_client/claude_admin.py` (`hub.mcp_claude`): `add|remove|login_start|login_finish|login_cancel|logout|status`. Tài khoản →
  `claude_source.account_config_dir` (token → `claude-config/<id>/`, dựng/kiểm symlink; machine/custom → cấu hình mặc định, KHÔNG đặt
  CLAUDE_CONFIG_DIR — trừ khi test). cwd trung lập `claude-config/.mcp-cwd`. Đăng ký registry dùng chung `claude_source.register` (PR2
  `mcp.add{source:claude}` gọi cùng hàm, `reuse=False` giữ Conflict cũ); thêm lại tên đã xoá mềm → khôi phục dòng (giữ trust/tools).
- Quyết định gỡ: registry (của hub, chung mọi tài khoản) chỉ xoá mềm khi KHÔNG tài khoản nào khác (gồm cấu hình mặc định) còn server
  cùng tên; còn agent gán + đây là bản cuối → Conflict trừ `force`. Đăng xuất/gỡ khi run đang dùng server đó của tài khoản → Conflict.
- Quyết định secret: header/env CHỈ cho tài khoản token (thư mục `claude-config/<id>/` đã bị chặn với agent); cấu hình mặc định → từ chối
  (PR1 chưa chặn agent đọc `~/.claude.json`) — dùng nguồn hub. Giá trị header/env đăng ký che theo giá trị; url/lệnh/args không (chữ
  chung), nhưng không bao giờ vào audit (danh sách trắng `CLAUDE_AUDIT_KEYS`), kết quả (chỉ host) hay lỗi (`_clean_cli` bỏ mọi URL + che).
- Đăng nhập: `accounts/login.start_login(..., url_finder=find_any_url, what=, cwd=)` — link nhận diện bằng `response_type`+`client_id`
  (không nhận nhầm URL trong dòng lỗi); `LoginHandle.submit_redirect` (một lần thử, URL sai → lỗi cố định không nhắc lại URL); kết quả
  LUÔN kiểm lại `claude mcp list` (vẫn `needs_auth` → lỗi "Keychain"). Mỗi tài khoản một lượt (tổng ≤ 4), `close_all` ở `Hub.stop`.
- RPC `mcp.claude_add|remove|login_start|login_finish|login_cancel|logout|status` ∈ HUMAN_ONLY, không ∈ MANAGER_ALLOWED; ghi ∈ MUTATING.
  `FAILURE_DROP_KEYS` thêm `redirect_url`/`headers`/`env`. Classifier: `cfharness mcp claude …` = `hub_human_only`.
- CLI `cfharness mcp claude add|rm|login|logout|status [--account]` (giá trị header/env + URL chuyển hướng hỏi bằng getpass/stdin).
- Web: mục "MCP của tài khoản" trên hàng token/machine — đọc `mcp.claude_status{cached:true}` (KHÔNG chạy claude khi tải trang), nút
  «Kiểm tra MCP» (`refresh`), Đăng nhập 2 bước (link https + ô password `redirect_url`), Đăng xuất/Gỡ (xác nhận), form Thêm MCP (header
  chỉ hiện ở tài khoản token). `Action.secret_fields` cho `redirect_url`/`header_value`/`url`.
- Doctor `_claude_mcp`: tài khoản đang dùng thiếu server đã đăng ký nguồn claude (cảnh báo) + server cần đăng nhập theo cache lần kiểm gần
  nhất (doctor không chạy `claude mcp list`).
- Test: `tests/test_mcp_claude_login.py` (17 ca, `tests/fake_claude_mcp.py` theo CLAUDE_CONFIG_DIR + PTY thật),
  `tests/test_sec_auth.py::test_mcp_claude_admin_human_only`.

Lệch so với plan: không có `cfharness mcp add --via claude` (dùng nhóm lệnh riêng `mcp claude add`); trạng thái chỉ từ `claude mcp list`
(chưa gộp `mcp.unavailable` từ init của run); chữ "thành công" của `claude mcp login` thật chưa quan sát được (spike không có OAuth server
thật để hoàn tất) — hub không dựa vào nó, luôn kiểm lại `mcp list`.

### PR4 — Chuyển dần + gỡ OAuth/proxy của hub
- `cfharness mcp migrate-to-claude <name> --account <id>`: server OAuth của hub → `claude mcp add-json` + `claude mcp login` (người dán URL), đổi `source=claude`, xoá token `MCP_OAUTH_*`.
- Khi không còn server OAuth `source=hub`: gỡ `oauth.py`, `oauth_proxy.py`, `oauth_cli.py`, `web/mcp_oauth.py`, RPC `mcp.oauth_*` (giữ stub trả lỗi hướng dẫn 1 phiên bản), config `mcp.oauth_proxy.*`.
- `hub_transfer`/`backup`: server `source=claude` không mang token — gói ghi `import_followups` "đăng nhập lại MCP X trên tài khoản Y"; doctor báo.
- CHANGELOG mục "Lưu ý khi cập nhật".

**Đã làm (giai đoạn A, 2026-11-20, nhánh `feat/mcp-migrate-to-claude`, trên PR3) — chuyển, CHƯA gỡ code OAuth/proxy (hub cũ vẫn dùng):**
- `mcp_client/migrate.py` + RPC `mcp.migrate_to_claude{name, account_id, keep_hub_token?, login?, dry_run?, drop_hub_token?}` (HUMAN_ONLY, không
  ∈ MANAGER_ALLOWED, ∈ MUTATING; audit chỉ `MIGRATE_AUDIT_KEYS`), CLI `cfharness mcp migrate-to-claude <tên> --account <id> [--yes]
  [--keep-hub-token] [--no-login] | --drop-hub-token` (∈ `HUB_HUMAN_ONLY_CLI["mcp"]`; xem trước bằng `dry_run`, y/N, rồi hỏi URL chuyển hướng
  bằng getpass → `mcp.claude_login_finish`).
- Điều kiện: dòng nguồn hub, http/sse, `auth.type == oauth`. Từ chối: không OAuth (secret tĩnh giữ nguồn hub — giải thích trong lỗi), stdio,
  client có `client_secret_ref` (claude chỉ nhận client secret qua Keychain khi thêm tay), header + tài khoản `machine` (luật PR3), run của
  agent gán server đang chạy, đã là nguồn claude.
- Entry claude = `secrets.resolve(claude_entry())` (url/header đã thay `${…}`, chỉ trong bộ nhớ → stdin `claude mcp add-json`) + `oauth.clientId`
  khi hub dùng client đăng ký tay (DCR: bỏ — claude tự đăng ký). `ClaudeMcpAdmin.add` nhận thêm `oauth` + `allow_hub_row` (chỉ đường migrate).
  Tài khoản đã có server cùng tên + cùng transport/host → dùng lại (không thêm, không gỡ khi hoàn tác); khác → Conflict.
- `McpRegistry.convert_to_claude`: đổi tại chỗ (giữ tên → agent gán giữ nguyên; trust, `tools` include/exclude/require_approval/annotations,
  mô tả, timeout, cột `annotations_cache`); event `mcp.changed{op: migrated}`. Lỗi ở bước này → `claude mcp remove` entry vừa thêm.
- Token `MCP_OAUTH_*`: giữ tới khi server `connected` dưới đúng cấu hình claude của tài khoản (`migrate.on_connected`, gọi từ
  `ClaudeMcpAdmin.login_finish` và `status` làm mới — nút «Kiểm tra MCP» cũng dọn khi người đăng nhập xong ngoài hub); trạng thái chờ ở
  `hub_state mcp.migrations` (∈ MACHINE_STATE). `keep_hub_token` giữ; `drop_hub_token` xoá rõ (chỉ khi đã nguồn claude). Xoá → audit
  `mcp.migrate_hub_token_deleted` (chỉ tên + lý do). `login_finish` trả thêm `hub_token_deleted`.
- Web: thẻ OAuth ở `/mcp/{name}` có dòng khuyên chuyển + form chọn tài khoản (`account.list`; server có header → bỏ `machine`) → ACTIONS
  `mcp_migrate_claude` (xác nhận, CSRF, audit web) → chuyển về `/settings#acct-mcp-<id>` (lượt đăng nhập hiện ở hàng tài khoản).
- Khuyên chuyển: `cfharness mcp login|logout` in một dòng lưu ý (stderr); doctor `mcp_hub_oauth` (warning) liệt kê server OAuth nguồn hub
  "nên chuyển sang claude" kèm lệnh.
- hub_transfer: `mcp_claude_relogin` (đọc TRƯỚC khi dọn MACHINE_STATE) → `hub_state import_mcp_claude` [{server, transport, host, accounts}]
  (tài khoản theo cache `mcp.claude_status` của máy cũ, không có → tài khoản đang dùng); báo cáo `hub import` có một dòng; doctor
  `mcp_claude_import` "đăng nhập lại MCP X trên tài khoản Y" + lệnh `mcp claude add … --url https://<host>/… ; mcp claude login …`, thôi báo khi
  tài khoản đó đã có server + `connected`. (PR2/PR3 chưa có ghi chú này — chỉ có cảnh báo "thiếu ở tài khoản đang dùng".)
- Test: `tests/test_mcp_migrate.py` (14 ca: đường đẹp + giữ chính sách, thời điểm xoá token, keep/drop, client_id + header, từ chối, hoàn tác,
  classifier, manager bị chặn, doctor, nhập gói thật, CLI, web), `tests/test_sec_auth.py::test_mcp_migrate_to_claude_human_only`.

Lệch / ngoài phạm vi: server http có header tĩnh (không OAuth) KHÔNG chuyển — giữ nguồn hub (secret store + mcp.json 0600); scope đặt tay của
hub không chuyển (claude tự xin, cảnh báo); client OAuth có secret chưa chuyển tự động; client_id tay cần thêm redirect
`http://localhost:<cổng>/callback` của claude ở nhà cung cấp (cảnh báo). `move` giữ token hub còn chờ (lượt `mcp.migrations` bị dọn) → xoá
bằng `--drop-hub-token`.

**Giai đoạn B — ĐÃ LÀM (2026-11-21, nhánh `feat/mcp-oauth-removal`, v0.2.28):**
- Gỡ `mcp_client/oauth.py`, `oauth_proxy.py`, `oauth_cli.py`, `web/mcp_oauth.py`, template `_mcp_oauth.html`/`mcp_oauth_go.html`/
  `mcp_oauth_done.html`, route `/do/mcp_connect` + `/mcp/oauth/callback`, `hub.mcp_oauth`/`hub.mcp_proxy`, `ensure_fresh_for`/`inject_bearer`
  /proxy trong `runs._run`, config `mcp.oauth_proxy.*` + `mcp_client.oauth_callback_port` (DEFAULTS, `validate_setting`, `integrity.SECURITY_KEYS`
  — so khớp toàn vẹn tính lại cả bản chuẩn lẫn file hiện tại theo danh sách mới nên hub cũ còn khoá không bị báo sửa; config.yaml cũ vẫn đọc),
  dòng doctor_multi "cổng callback OAuth", test `test_mcp_oauth.py`/`test_mcp_oauth_proxy.py`/`oauth_fake.py`.
- Còn lại cho dòng cũ: `mcp_client/legacy_oauth.py` (tên bản ghi `MCP_OAUTH_*`, `is_oauth`/`needs_migration`, nạp bộ che, `delete_record`,
  stub RPC, cảnh báo khởi động). `migrate.py` chỉ dùng module này (url/transport/`auth.client_id` lấy từ cấu hình registry).
- RPC `mcp.oauth_start|callback|status|disconnect|refresh` = stub `HubError` "Đăng nhập OAuth do hub giữ đã gỡ từ v0.2.28 — chuyển server sang
  claude: cfharness mcp migrate-to-claude <tên> --account <id>" (giữ trong HUMAN_ONLY/MUTATING một bản). `cfharness mcp login|logout` in cùng
  câu (mã 1, không gọi daemon); `mcp add|edit --oauth*` → lỗi hướng dẫn `mcp claude add`; `--no-oauth` vẫn bỏ được auth của dòng cũ.
- `McpRegistry.add`/`update(auth=…)` từ chối `auth.type: oauth` nguồn hub (`legacy_oauth.ADD_REFUSED`; `add(allow_legacy_oauth=True)` chỉ cho
  test dựng dòng cũ). Sửa trường khác của dòng cũ vẫn được. Web: ô "Đăng nhập OAuth" ở form thêm MCP bỏ; danh mục MCP với server cần OAuth
  chỉ hướng sang Cài đặt › Tài khoản Claude › Thêm MCP (không tạo dòng nguồn hub).
- Dòng cũ (nguồn hub + `auth: oauth`): `mcp.list/show` có `oauth_status = needs_migration` + `migrate_cmd`; run của agent gán → `RunManager.
  _refuse_legacy_oauth_mcp` (trước `_prepare_spec`, chưa ghi gì vào `runtime/`) phát `mcp.unavailable{server, status: needs_migration,
  source: hub}` rồi `failed` với lỗi nêu tên + lệnh chuyển; `registry.runtime_entry` cũng ném lỗi này (takeover, probe — gợi ý = lệnh chuyển).
  Doctor: `mcp:<tên>` + `mcp_hub_oauth` mức error kèm lệnh. Web `/mcp`: nhãn "cần chuyển sang claude" (link `#migrate`); `/mcp/{name}`: thẻ
  `_mcp_migrate.html` (form «Chuyển sang claude», action `mcp_migrate_claude` chuyển sang `web/mcp_pages.py`). `Hub.start` → MỘT dòng
  WARNING liệt kê server (`legacy_oauth.warn_on_start`). Không bao giờ tự xoá dòng/secret.
- hub_transfer: giữ `MCP_OAUTH_` trong `CLONE_SKIP_PREFIXES` (gói cũ vẫn bỏ khi clone). `with_loopback_no_proxy` giữ (MCP http loopback).
- Test: `tests/test_mcp_migrate.py` (+ ca run fail-closed trước spawn, stub RPC, add bị từ chối, cảnh báo khởi động, web nhãn/thẻ, CLI
  login|logout), `tests/test_web_mcp.py` (danh mục OAuth → claude), `tests/test_mcp_cli_flags.py`, `tests/test_multi_instance.py`.
- Chưa làm: gỡ hẳn tên stub `mcp.oauth_*` + `mcp login|logout` (bản sau); `OAuthSettings` trong `mcp_client/config.py` giữ để đọc dòng cũ.

**Kế hoạch giai đoạn B ban đầu (lưu lại):** gỡ `oauth.py`, `oauth_proxy.py`, `oauth_cli.py`,
`web/mcp_oauth.py` (+ template `_mcp_oauth.html`, `mcp_oauth_go/done.html`), RPC `mcp.oauth_*` (giữ stub trả lỗi hướng dẫn migrate một
phiên bản), config `mcp.oauth_proxy.*` + `mcp_client.oauth_callback_port`, `inject_bearer`/`ensure_fresh_for` trong runs/registry, nhánh OAuth
của doctor/catalog/`mcp add --oauth`, test OAuth/proxy (~30); chuyển tự động lúc daemon lên (hoặc chặn run) cho server còn sót; CHANGELOG
"Lưu ý khi cập nhật".

### PR6 — Gỡ hẳn đường "nguồn hub" (2026-11-24, nhánh `chore/mcp-hub-source-removal`, v0.2.32)
Người dùng chọn chuyển nốt server stdio (`fast-browser`, `playwright`, `video-context` — vốn nhập từ `~/.claude.json`, đã có sẵn trong
cấu hình claude của tài khoản `machine`) rồi gỡ toàn bộ code + UI MCP nguồn hub:
- Migration v34 `mcp_client/schema.to_claude_source`: mọi dòng `manual`/`claude-import` → `claude` tại chỗ (giữ tên/trust/tools/annotations/
  `annotations_cache`; bỏ command/args/env/headers/auth; url → host; url còn `${…}` → bỏ). Dòng trỏ về chính hub → xoá mềm (H-18 — sau
  khi bỏ lệnh không còn nhận ra được). `McpServerConfig` chỉ còn nguồn claude; đọc dòng cũ thì bỏ trường kiểu hub (`_drop_legacy`).
- Gỡ: `mcp_client/catalog.py`, `importer.py` (heuristic secret → `secret_values.py` cho `claude_redact`), `probe.py`, `scan.py`,
  `legacy_oauth.py`, `migrate.py`; RPC `mcp.probe|import|catalog|migrate_to_claude` + stub `mcp.oauth_*`; CLI `mcp import|probe|test|
  login|logout|migrate-to-claude`, `mcp add -- <lệnh> / --url / --env / --header` (in hướng dẫn `mcp claude add`); web `/mcp/browse*`,
  `mcp_add|mcp_probe|mcp_import|mcp_migrate_claude`, template `_mcp_migrate|mcp_browse|mcp_install.html`; `runs._agent_mcp_servers`,
  `_refuse_legacy_oauth_mcp` (mcp.json chỉ còn hub-guard/hub-self); doctor `_mcp` (probe) + `mcp_hub_oauth`; config `mcp.probe_timeout_s`.
- `mcp.add` = đăng ký server có sẵn trong cấu hình claude (không nói nguồn mà có lệnh/header → lỗi hướng dẫn); `mcp.update` chỉ trust/tools/
  description/timeout_s. `mcp claude add` cho header/env cả ở cấu hình mặc định (PR1 đã chặn agent đọc `~/.claude.json`).
- Chrome / allowlist domain: Playwright gán cho agent bị gỡ bằng `--disallowedTools mcp__<x>__*` (nhận theo lệnh trong cấu hình claude —
  `claude_source.playwright_servers`), không còn bỏ khỏi mcp.json.
- `cfharness doctor <agent>`: MCP gán có trong cấu hình claude của tài khoản đang dùng + trạng thái lần kiểm gần nhất (thay probe).
- Lệch: web chưa có form thêm server stdio (CLI `mcp claude add <tên> -- <lệnh>`); `transfer_os.mark_mcp` (đánh dấu lệnh thiếu khi nhập
  gói cũ) giữ — vô hại với dòng mới (không có lệnh).

### PR5 — Điều phối viên + doctor
- `mcp_registry_list` hiện cả `source`; khung `read_only_trusted` áp như server hub (dựa annotation thủ công).
- `hub doctor`: server claude chưa đăng nhập / tài khoản đang dùng thiếu server được gán.

**Đã làm (2026-11-21, nhánh `feat/mcp-claude-redact-register`) — phần web còn thiếu:** mục "MCP của tài khoản" có nút
«Đăng ký cho hub» cho server có trong cấu hình claude mà registry chưa có (ACTIONS `mcp_claude_register` → RPC có sẵn
`mcp.add{config:{name, source:"claude", trust}, account_id}`, HUMAN_ONLY, xác nhận một bước nêu ý nghĩa tin cậy, CSRF/khoá phiên + audit
`web`, quay về `/settings#acct-mcp-<id>`); ô «Tin cậy» → `trusted`, không tích → `untrusted`. Server `hub=true` (trỏ về chính hub) không
có nút, hiện "trỏ về chính hub". Form agent: nhãn `claude` cạnh MCP nguồn claude. Tên đã xoá mềm trong registry → `mcp.add` báo trùng
(khôi phục ở trang MCP / `cfharness mcp restore`) — giữ nguyên hợp đồng `reuse=False` của PR2. Test:
`tests/test_mcp_claude_login.py::test_web_mcp_register_for_hub_button`.

## 4. Đánh đổi phải chấp nhận
- Server `source=claude` gắn với **tài khoản**, không với hub: đổi tài khoản đang dùng → server đó chỉ có nếu tài khoản mới cũng có (doctor + `connector.auth_fallback`-tương tự cảnh báo).
- Run dùng server `source=claude` không còn `--strict-mcp-config` → mọi MCP khác trong config dir có thể connect (bị `--disallowedTools` gỡ khỏi ngữ cảnh + guard chặn gọi), hook/plugin user cũng nạp như connector mode hiện tại.
- Annotation tự động mất (không probe được khi token do claude giữ) → thêm bước gắn nhãn tay hoặc chấp nhận hỏi người lần đầu + "luôn cho phép".

## 5. Thứ tự đề xuất
PR1 (an toàn, làm ngay) → PR0 spike → PR2 → PR3 → PR4 → PR5. Ước lượng: PR1 nhỏ; PR2–PR3 vừa; PR4 gỡ ~1.450 dòng + test OAuth/proxy (~30 test).
