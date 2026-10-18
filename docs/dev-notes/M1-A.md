# M1 — luồng A (Tích hợp): ghi chú phát triển

Ngày 2026-10-18. Phạm vi: FR-MS-01..07 (trừ tool team/learning), FR-MC-01..06, FR-OB-08, `hub_feedback` ghi Signal (FR-LN-01 phần ghi).

## Đã làm
- **MCP server của hub** (`mcp_server/`), SDK `mcp` 2.3 (`MCPServer`):
  - `agent-hub mcp serve` (stdio, quyền `control`) và `--http` (Streamable HTTP, CHỈ loopback — host khác bị từ chối; token `read`/`control`
    sinh lần đầu ở `HUB_HOME/mcp_server/tokens.json` 0600; thiếu/sai token → 401; token `read` gọi tool ghi → lỗi quyền).
  - Tool: `hub_status`, `hub_list_agents`, `hub_agent_get`, `hub_agent_create` (idempotency_key), `hub_agent_update`, `hub_agent_archive`
    (destructive, có `restore`), `hub_run` (trả ngay, idempotency_key), `hub_run_status`, `hub_events` (long-poll ≤ 25 s, `after_seq`+`epoch`,
    `done`+`result`, `open_requests`), `hub_cancel` (destructive), `hub_open_requests`, `hub_approve` (destructive) / `hub_deny`, `hub_feedback`,
    `hub_estop` (destructive). Mô tả tiếng Việt, input/output JSON Schema từ Pydantic, annotations readOnly/destructive/idempotent/openWorld=false.
  - `instructions` + resource `hub://huong-dan` (Markdown) + `hub://agents` (snapshot).
  - Client mỏng: mọi thao tác là RPC tới daemon (`DaemonCaller` tự khởi động daemon). Long-poll chạy TRONG daemon (`mcp.events`), đánh thức
    bằng listener bus theo run_id.
  - `agent-hub mcp snippet [--http]` CHỈ in lệnh `claude mcp add …` + JSON; không đụng `~/.claude.json` (có test).
- **MCP client registry** (`mcp_client/`): schema mượn Hermes (command/args/env | url/headers, `tools.include/exclude/require_approval` glob,
  `trust` trusted|untrusted — giá trị lạ/thiếu = untrusted + cảnh báo, `full` = trusted; `lazy`, `supports_parallel_tool_calls`, `timeout_s`).
  CLI `mcp add|edit|list|show|rm|restore|probe(test)|import|assign|unassign|agent-config`. Xoá mềm (`deleted_at` + `trash/mcp/*.yaml`); đang
  được agent dùng → từ chối trừ `--force`.
  - Probe = kết nối + `tools/list` (không gọi tool), lưu annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`, title,
    description) + thời gian phản hồi + gợi ý khi lỗi (FR-MC-06). Env stdio chỉ gồm baseline an toàn (mượn Hermes) + env khai báo.
  - Quét mô tả tool tìm injection (mẫu Hermes + tiếng Việt + ký tự Unicode ẩn) → `suspicious`, cảnh báo, mặc định bị đưa vào disallowed.
  - `import --from claude [--file] [--project] [--all|--select] [--dry-run] [--overwrite] [--probe]`: CHỈ ĐỌC `~/.claude.json`; secret trong
    env/header/args/query url (tên chứa KEY/TOKEN/SECRET/PASSWORD/AUTH… hoặc entropy cao) → secret store, registry giữ `${MCP_<SERVER>_<KEY>}`;
    `${VAR}` sẵn có giữ nguyên; trust mặc định `trusted` (Q-05). Không tty → in danh sách, cần `--all/--select`.
  - Hàm hợp đồng: `build_agent_mcp_servers(agent)` (đã thay secret; server thiếu/xoá mềm/secret thiếu → `HubError`, fail-closed),
    `tool_annotations(server, tool)` (annotations + `trust`, `require_approval`, `selected`, `suspicious`; `None` = server lạ hoặc tool chưa
    probe), thêm `agent_mcp_tool_rules(agent)` → `allowed`/`disallowed` dạng `mcp__<server>__<tool>`.
- **Secret store** (`secrets.py`): file 0600 trong `HUB_HOME/secrets/` (thư mục 0700). Lý do không dùng Keychain ở M1: `security
  add-generic-password -w <giá trị>` lộ secret qua argv/`ps`, có thể bật hộp thoại xin quyền khi daemon đọc (treo spawn), không chạy Linux,
  khó test; BRD để Keychain là tuỳ chọn M2. `secrets.env_fallback` (mặc định bật) cho `${VAR}` lấy từ môi trường daemon. CLI `secret set
  (getpass|--value-stdin|--from-env)|list|rm` — `list` báo secret còn thiếu mà registry đang tham chiếu.
- **Audit** (`audit.py`, bảng `audit_log` append-only bằng trigger): `audit.record(kind, agent_id, run_id, data)`; che secret (redact +
  giá trị trong secret store). `register_rpc` bọc audit MỌI method thay đổi trạng thái của daemon (agent.*, run.start/stop/resume/continue,
  estop, config.set, mcp.*, secret.*, feedback.record…) với `channel` = `cli` hoặc `mcp:<tên client>[:http]`. `agent-hub audit list`.
- **tool.write**: `ToolWriteWatcher` (listener bus) — `tool.start` của tool `mcp__…` không `readOnlyHint: true` (kể cả chưa probe) → audit
  `tool.write` (agent, run, server, tool, input đã che) + event `tool.write` (phát qua `call_soon` để không lồng publish); `tool.complete`
  tương ứng → audit `tool.write.result`. Bỏ qua server trong `mcp.audit_ignore_servers` (mặc định `hub-guard`).
- **Signals**: bảng `signals`; `hub_feedback`/`agent-hub feedback`: good→`good`, wrong/partial→`correction`, có `corrected_output` → thêm
  `edit_output`; phát `learning.signal`.
- Migration v3 (`mcp_servers`, `audit_log`, `signals`, `idempotency_keys`); config `mcp.*`, `secrets.*`, `mcp_server.*`; event `tool.write`,
  `mcp.changed`, `learning.signal`.
- Test: `test_secrets.py`, `test_audit.py`, `test_mcp_registry.py` (probe server MCP giả stdio), `test_mcp_import.py` (~/.claude.json GIẢ 10
  server, checksum không đổi, grep hub.db/WAL/registry/logs không thấy secret, đường CLI qua daemon với HOME giả), `test_mcp_tool_write.py`,
  `test_mcp_server.py` (in-process, stdio thật, HTTP uvicorn + 401/read/control, idempotency, long-poll, cancel, audit channel). Toàn bộ
  `uv run pytest -q`: 88 passed (gồm test của luồng B tại thời điểm chạy).

## Kiểm chứng thật (một lần, tiêu chí M1 phần A)
HUB_HOME tạm trong scratch, daemon runtime `claude` thật, agent `haiku-helper` (model haiku, `--tools ""`, budget 0.1/run). Lệnh:
`claude -p "<yêu cầu dùng hub_run giao việc đếm từ cho haiku-helper rồi hub_events tới done>" --model haiku --max-budget-usd 0.3
--setting-sources "" --strict-mcp-config --mcp-config <file từ agent-hub mcp snippet> --allowedTools mcp__agent-hub --output-format json`
(bỏ biến môi trường CLAUDECODE/CLAUDE_CODE_* của phiên bao ngoài).
Kết quả: chạy thông. Claude Code gọi `hub_run` → `r_34dc90edd676`, lặp `hub_events` tới `done=true`, trả `status=completed`, `summary=5`
(đúng). Audit có `mcp.run` channel `mcp:claude-code`. **Chi phí: phiên ngoài $0.00445707 + run agent $0.00049708 = $0.00495415.**

## Lệch BRD / quyết định riêng
- Tên tool theo yêu cầu giao việc: `hub_run_status` (BRD: `hub_run_get`), `hub_open_requests` (BRD: `hub_requests_open`), tách `hub_deny`
  khỏi `hub_approve(decision)`; `hub_run(agent, prompt)` (BRD: `task`); `hub_events` dùng `wait_s` (BRD: `wait_ms`) và chỉ theo `run_id`
  (chưa có theo `agent_id`/`task_id`). Chưa có `hub_policy_get` (chính sách an toàn thuộc luồng B), `hub_team_*`/`hub_task_*` (M3).
- Registry: nguồn sự thật là DB, `registry.yaml` chỉ là bản sao (BRD ghi "yaml + DB"); trust dùng `trusted|untrusted` (BRD FR-MC-01 ghi
  `untrusted|full` — `full` được nhận như alias). Thêm `tools.require_approval` cho "chuyển một tool sang require_approval" (FR-MC-03).
- `lazy`, `supports_parallel_tool_calls` lưu nhưng không có tác dụng (kết nối MCP do `claude` CLI quản lý), đúng ghi chú BRD.
- Audit mọi thao tác thay đổi được ghi SAU khi thành công (thao tác lỗi không ghi); chưa lưu "before/after" đầy đủ, chỉ params đã che + tóm tắt
  kết quả. Lời gọi trùng idempotency_key cũng có một dòng audit (`idempotent_replay`).
- Secret: thêm env-fallback (mặc định bật) để dùng lại `${VAR}` sẵn có trong `~/.claude.json`; tắt bằng `config set secrets.env_fallback false`.

## Việc dở / cần luồng B nối
- `runs.py` (luồng B sở hữu) vẫn dùng `agents.load_mcp_registry` (đọc `registry.yaml` — giờ là bản sao có thêm trường `trust/tools/…` và
  `${…}` CHƯA thay). B cần thay bằng `mcp_client.build_agent_mcp_servers(agent)` và, nếu muốn, đưa `agent_mcp_tool_rules(agent)` vào
  `--allowedTools/--disallowedTools` (FR-MC-02). Lúc kiểm chứng thật, agent không gán MCP nên không bị ảnh hưởng.
- `hub_open_requests`/`hub_approve`/`hub_deny` gọi RPC `approval.list` (params `{status:"open", run_id?}`; nhận list hoặc `{requests:[…]}`),
  `approval.approve`/`approval.deny` (params `id` và `request_id`, `note`, `channel="mcp:<client>"`, `actor="user"`). Khi B đặt tên tham số
  khác thì sửa `_decide`/`open_requests` trong `mcp_server/`. Chưa có test end-to-end với approval thật (B chưa có RPC lúc viết).
- Elicitation MCP để hỏi người trong Claude Code (Could, M2); `hub_events` theo agent/task; resource `hub://events/{run_id}`.
- Keychain backend cho secret (M2); xoay vòng/thu hồi token HTTP theo client; ghi `cost`/rate limit vào `hub_status` chi tiết hơn.
