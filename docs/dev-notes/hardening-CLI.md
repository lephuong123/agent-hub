# Hardening — luồng CLI (bề mặt & runtime)

Phạm vi: H-06, H-07, H-08, H-10, H-11, H-13, H-14, H-20 (docs/acceptance/traceability.md §3) + mục "Thấp" FR-AG-02, FR-AG-08, NFR-26.
Test hồi quy: `tests/test_hardening_cli.py` (34 ca), `tests/test_event_schema.py` (3 ca), thêm assert trong
`tests/test_approval_flow.py::test_mcp_hub_approve_resolves_approval`. `uv run pytest -q` cuối cùng: **371 passed**.
Không gọi `claude` thật, không tốn tiền.

## File mới / sửa
| File | Thay đổi |
|---|---|
| `agents.py` | `RESERVED_IDS`; `remove(detach_teams)` + `team_memberships`; `revert()`; snapshot revision lưu cả `soul` |
| `mcp_server/surface_rpc.py` (mới) | RPC `agent.delete{detach_teams}`, `agent.archive`, `agent.revert`, `trash.list|restore|purge`, `memory.search`, `memory.search_self`, `hook.observe`, `hook.timeout`, `mcp.agent_run`, `mcp.cancel_task`, `hub.compat`. Đăng ký trong `mcp_server/rpc.register_rpc` TRƯỚC bước bọc audit (không sửa `daemon.py` của SEC) |
| `mcp_server/rpc.py` | thêm tên method ghi vào `MUTATING` + gọi `surface_rpc.register` |
| `cli_surface.py` (mới) | `rewrite_argv` (alias) + lệnh `stop`, `trash`, `hook` |
| `cli.py` | `daemon logs`, `doctor` thử kết nối MCP + tỉ lệ event lạ, `agent list --team/--status`, `agent rm|archive --detach-teams`, `agent revert --to N`, `run --detach/--continue`, cảnh báo compat trong `daemon status` |
| `runtime/claude_cli.py` | `argv_flags`, `check_flag_matrix`, `build_argv(spec, cfg, cli_version)`, `oneshot_argv`, `ClaudeCliRuntime.oneshot` |
| `runtime/base.py`, `runtime/fake.py` | `AgentRuntime.oneshot(OneShotRequest) -> OneShotResult` (+ bản giả có `oneshot_results`) |
| `runtime/compat.py` (mới) | `hub.compat`: kiểm phiên bản lúc `Hub.start`, cảnh báo `raw.unknown`, nạp spool hook quá giờ |
| `observe_hook.py` (mới) | hook quan sát fail-open + `add_observe_hooks()` |
| `logs.py` | `RunLogger`/`run_logger`, `set_run_context` + `RunContextFilter` (contextvar) |
| `event_schema.py` (mới), `docs/schema/event.v1.json` | JSON Schema envelope + danh mục loại event |
| `hub.py`, `runs.py`, `config.py`, `events.py` (dùng chung, chỉ thêm) | gắn `compat`; nối hook quan sát + logger trong `_prepare_spec`/`_run`; khoá cấu hình mới; loại event `hook.observed`, `cli.unknown_rate_alert` (`hook.timeout` lên `summary`) |
| `mcp_server/server.py`, `mcp_server/hub_self.py` | tool/tham số H-14, H-06 |
| `mcp_client/registry.py` | `tool_rules_for_servers`: luật cho server chưa probe (H-13) |
| `curation/llm.py` | gọi model qua `ClaudeCliRuntime.oneshot` (H-20) |
| `tui/app.py`, `tui/model.py`, `tui/rpc.py` | màn `?` (NFR-26); header đọc `hub_state.cli_compat` + cảnh báo đỏ `raw.unknown` |

## Từng mã

### H-06 — `agent rm` kiểm thành viên team (FR-AG-04)
- `AgentService.remove(id, detach_teams=False)`: còn là thành viên team chưa archive → `Blocked` (exit 3) với thông điệp liệt kê
  `team (role)` + gợi ý `--detach-teams`, `data.teams`. `detach_teams=True` → xoá dòng `team_members`, phát `team.member{op=remove,
  reason=agent_removed}`, rồi xoá mềm như cũ. Phát thêm `agent.archived` (trước `agent.deleted`). Kết quả có `detached_teams`.
- CLI `agent rm|archive <id> [--detach-teams]`; RPC `agent.delete|agent.archive {id, detach_teams}`; MCP `hub_agent_archive(detach_teams)`.
- Test: `test_h06_rm_blocked_when_team_member_then_detach` (QA: trước đây rm thành công → team mồ côi lead),
  `test_h06_h07_h08_cli_e2e` (exit 3 + `--detach-teams`), `test_h14_mcp_tools_params` (MCP).

### H-07 — `agent revert <id> --to N` (FR-AG-03)
- `AgentService.revert(id, N)` lấy snapshot revision N → ghi revision MỚI (N+…) có cùng cấu hình (không xoá lịch sử, snapshot có
  `reverted_to`), phát `agent.updated{reverted_to}`. Snapshot revision giờ lưu cả nội dung SOUL → revert khôi phục SOUL; revision tạo
  TRƯỚC bản hardening không có SOUL → giữ SOUL hiện tại (`soul_in_snapshot=false`, CLI ghi chú).
- RPC `agent.revert {id, to}` (MUTATING → audit), CLI `agent revert <id> --to N`.
- Test: `test_h07_revert_creates_new_revision_equal_to_target` (rev 4 → revert 3 → rev 5 = rev 3, kể cả SOUL),
  `test_h07_running_run_keeps_old_revision` (run đang chạy giữ `agent_config_revision` cũ, run kế dùng revision mới).

### H-08 — bộ lệnh CLI (FR-UI-08)
- **Không bao giờ chạy nhầm**: `cli_surface.rewrite_argv` chạy trước mọi bộ viết lại: `run list|show|logs|stop|follow …` → `runs …`.
  Thêm lớp thứ hai: id agent trùng từ khoá (`agents.RESERVED_IDS`: list, show, logs, stop, follow, continue, resume, revert, diff,
  steer, status, all, help, detach, wait, takeover, attach) bị từ chối khi TẠO (clone/import đi qua `create` nên cũng bị chặn).
  Agent cũ lỡ trùng tên vẫn đọc/sửa được; với chúng, `run <tên> …` sẽ bị hiểu là `runs …` → dùng `hub_run`/TUI hoặc đổi tên (clone).
- Lệnh/alias mới: `stop <run|agent>`, `requests` (= `approvals`), `approve <id> --deny` (= `deny`), `agent archive|revert`,
  `learn pin|unpin` (→ `knowledge pin|unpin`), `learn enable|disable [agent]` (→ `learn policy set [agents.<id>.]enabled true|false`)
  — alias chỉ áp khi parser `learn` của luồng LRN CHƯA có lệnh cùng tên (kiểm động), `eval golden …` (→ `golden …`),
  `eval results` (→ `eval list`), `run <agent> "<prompt>" --detach|--continue <run>`, `feedback <run> --good|--wrong|--partial`,
  `daemon logs [-n N] [--run] [--agent]`, `trash list|restore|purge`, `hook <Event>`.
- Test: `test_h08_run_subcommands_never_parsed_as_new_run` (QA: trên mã cũ `run logs r_abc` → `cmd_run(agent=logs)`),
  `test_h08_agent_id_cannot_be_cli_keyword`, `test_h08_aliases_parse`, `test_h06_h07_h08_cli_e2e` (daemon thật).

### H-10 — tương thích phiên bản CLI (FR-RT-02, FR-RT-15)
- `runtime.claude.flag_matrix` (phiên bản → danh sách cờ; 2.1.302 lấy từ `claude --help` trên máy, BỎ cố ý
  `--dangerously-skip-permissions`/`--allow-dangerously-skip-permissions`). `build_argv(..., cli_version)` → `check_flag_matrix`: phiên
  bản có trong bảng mà argv (kể cả `extra_args`, dạng `--x=y`) có cờ ngoài danh sách → `ArgvError` → run `failed`, KHÔNG spawn.
  Phiên bản lạ/không biết → không kiểm (BRD: chỉ cảnh báo vàng). `ClaudeCliRuntime.start` đọc phiên bản trước khi dựng argv.
- `Hub.start` → `compat.check_on_start()`: đọc phiên bản (thread), lưu `hub_state.cli_compat {version, tested, status ok|untested|missing|fake}`,
  phát `cli.version_warning{at: daemon_start}`. Header TUI (`tui.snapshot`), `daemon status`, `doctor` đọc trạng thái này (RPC `hub.compat`).
- `CliCompat` (listener bus) đếm event `source=cli` (bỏ delta) và `raw.unknown` trong cửa sổ trượt `runtime.claude.unknown_alert`
  (5 %, 3600 s, tối thiểu 50 event) → `cli.unknown_rate_alert` (summary, một lần/cửa sổ, có gợi ý `estop on`) + `hub_state.cli_unknown_rate`;
  TUI hiện nhãn đỏ, `doctor` báo lỗi.
- Test: `test_h10_flag_matrix_rejects_unsupported_flags`, `test_h10_spawn_refused_with_unsupported_flag` (QA: trước đây spawn với cờ lạ),
  `test_h10_version_checked_on_start_and_shown_in_tui`, `test_h10_raw_unknown_rate_alert`, `test_h10_unknown_rate_below_threshold_no_alert`.
- Còn lại: ma trận chỉ có 2.1.302; khi nâng CLI phải thêm danh sách cờ (cùng fixture + contract test như AGENTS.md hướng dẫn).

### H-11 — hook quan sát fail-open (FR-RT-17)
- `cfharness/observe_hook.py` (stdlib): `python -I -m cfharness.observe_hook <PostToolUse|UserPromptSubmit|Stop> --run --socket --timeout 2 --home`
  (CLI tương đương: `cfharness hook <Event>`). Gửi RPC `hook.observe` CHỈ metadata (event, tool_name, session_id, độ dài prompt —
  không gửi input/output tool, prompt). Quá 2 s hoặc daemon không với tới → luôn exit 0, không in stdout; ghi `hook.timeout` bằng
  notification JSON-RPC không chờ trả lời; nếu cả việc đó hỏng → spool `logs/hook-timeouts.jsonl` (0600), daemon nạp thành event lúc khởi động.
- Lắp vào settings: hàm RIÊNG `add_observe_hooks()` gọi trong `RunManager._prepare_spec` SAU `guard.runtime.build_settings` — không
  sửa file của SEC; entry PreToolUse (fail-closed) giữ nguyên; lệnh hook kết thúc `|| true`, timeout CLI 5 s. Các event thêm vào
  `allowed_hooks` (không thành `isolation.violation`). Tắt: `runtime.claude.observe_hooks.enabled=false`.
- Test: `test_h11_observe_hooks_in_settings_keep_guard`, `test_h11_run_settings_contain_observe_hooks`, `test_h11_hook_timeout_fail_open`
  (server giả chậm 3,5 s → hook trả về sau ~2 s, có notification `hook.timeout`, không lộ input tool),
  `test_h11_hook_unreachable_spools_and_daemon_ingests` (tiến trình `python -I` thật), `test_h11_hook_cli_and_rpc_record_events`.
- Chưa kiểm với `claude` thật (overhead một tiến trình python ~50 ms/tool call). Nếu cần giảm: đặt `observe_hooks.events` hẹp hơn.

### H-13 — MCP chưa probe / untrusted (FR-MC-02, FR-MC-03)
- `tool_rules_for_servers` (server chưa probe): mẫu `exclude` không glob → `--disallowedTools mcp__s__tool`; `include` không glob
  (server trusted, không `require_approval`) → `--allowedTools mcp__s__tool`; glob → cảnh báo. Tool ngoài luật vẫn qua hook guard
  (`mcp_unknown_tool`/`mcp_untrusted_write` → giữ chờ người). `mcp add/import` vẫn probe mặc định như trước.
- Test: `test_h13_unprobed_literal_include_exclude_rules`, `test_h13_unprobed_rules_reach_argv` (e2e argv), `test_h13_classify_untrusted_requires_approval`,
  `test_h13_untrusted_mcp_tool_opens_approval` (guard.check mở approval, không tự cho phép).

### H-14 — MCP `hub_*` + hub-self (FR-MS-02, FR-MS-05, FR-MS-08)
- Tool mới readOnly: `hub_policy_get` (guard policy + `learn.policy_get`), `hub_memory_search` (memory các tầng, skill hiệu lực,
  transcript = prompt/summary run, ẩn run kind review/eval/eval_judge/curator/learning; tìm từ khoá, không LLM).
- Tham số: `hub_events(agent_id)` (run hiện tại/gần nhất), `hub_cancel(task_id)` (dừng run của task + task con), `hub_estop(team)`,
  `hub_list_agents(team, status)`, `hub_agent_archive(detach_teams)`.
- hub-self: `memory_search` cho worker/lead/reviewer → RPC `memory.search_self` (ràng agent của run đang chạy).
- `test_mcp_hub_approve_resolves_approval`: assert `open_requests` của `hub_open_requests` VÀ `hub_events` có `tool_name` + `tool_input`.
- Test: `test_h14_*`.

### H-20 — log, hợp đồng event, runtime (NFR-21/22/23)
- `logs.run_logger(log, run_id, agent_id, task_id=…)` (`LoggerAdapter`) dùng trong `runs.py`; `set_run_context` đặt contextvar trong
  task asyncio của run, `RunContextFilter` (gắn vào handler `hub.log`) tự thêm `run_id/agent_id` cho MỌI log phát trong task đó.
- `docs/schema/event.v1.json` sinh bằng `python -m cfharness.event_schema --write docs/schema/event.v1.json`; `tests/test_event_schema.py`:
  envelope khớp snapshot, loại event chỉ thêm (không xoá/đổi level), mọi event từ fixture stream-json thật hợp lệ (jsonschema).
- `AgentRuntime.oneshot()` (Claude + Fake); `curation/llm.structured_call` đi qua `ClaudeCliRuntime.oneshot` (giữ `curation.binary`).
- Test: `test_h20_logger_adapter_and_context`, `test_h20_oneshot_via_runtime`, `tests/test_event_schema.py`.

### Mục "Thấp"
- FR-AG-02: `agent list --team T --status S`, MCP `hub_list_agents(team, status)`.
- FR-AG-08: `doctor <agent>` thử kết nối thật từng MCP server (RPC `mcp.probe`, `--probe-timeout`), lỗi → ✗ + exit 2; thêm dòng tỉ lệ event lạ.
  Test `test_low_doctor_probes_agent_mcp`.
- NFR-26: phím `?` (toàn app) mở `HelpScreen` liệt kê MỌI phím của màn đang mở (kể cả phím ẩn khỏi footer). Test `test_low_tui_help_screen_lists_all_keys`.
- FR-TK-01 `--detach`, FR-TK-10 `hub_estop(team)`: làm cùng H-08/H-14.

## Việc cần luồng khác / coordinator
- **LRN** (`evals/judge.py`, `dispatch/dispatcher.py`, `recovery.py` là file của LRN):
  1. Chuyển LLM judge sang `ClaudeCliRuntime({**runtime.claude, "binary": judge.binary}).oneshot(OneShotRequest(...))` như `curation/llm.py`
     (cùng argv `--json-schema --no-session-persistence --tools ""`, nay có kiểm ma trận cờ). Khi đó NFR-22 đạt hoàn toàn.
  2. Log có `run_id`: dùng `from cfharness.logs import run_logger` cho các dòng `log.warning("run %s …", run_id)` trong dispatcher/recovery
     (log phát TRONG task run đã tự có nhờ contextvar; log của dispatcher chạy ngoài task run thì chưa).
- **SEC** (xác thực người gọi RPC, H-01): các method sau được gọi TỪ BÊN TRONG run và phải tiếp tục được phép như `guard.check`:
  `hook.observe`, `hook.timeout` (hook quan sát), `memory.search_self` (hub-self). Các method người dùng mới cần token điều khiển:
  `agent.archive`, `agent.revert`, `trash.restore`, `trash.purge` (purge gọi `agent.purge` của SEC, giữ nguyên bước gõ lại id).
  `flag_matrix` cố ý loại `--dangerously-skip-permissions`/`--allow-dangerously-skip-permissions` → lớp chặn thêm cho H-18 khi phiên bản đã biết.
  Agent tự gọi `hook.observe` chỉ tạo event debug (không đổi trạng thái).

## Lệch BRD cần cập nhật (FR-UI-08 và liên quan) — đề nghị coordinator sửa BRD
| BRD | Thực tế | Đề xuất |
|---|---|---|
| `run list\|show\|logs <run>` | Có, và nhóm gốc `runs list\|show\|logs\|stop\|follow` vẫn giữ (alias hai chiều) | Ghi cả hai dạng; thêm `run follow` |
| `agent archive` và `agent rm` là hai lệnh | Cùng một thao tác (archive + chuyển home vào trash, khôi phục bằng `restore`) | Gộp mô tả: `archive` = `rm` |
| Id agent tự do (slug) | Cấm id trùng từ khoá CLI (`RESERVED_IDS`) | Thêm vào FR-AG-01 |
| `learn pin\|unpin` | Alias của `knowledge pin\|unpin <agent> <skill\|memory\|config\|memory_entry> <ref>` | Ghi cú pháp thật |
| `learn enable\|disable` | Alias `learn policy set [agents.<id>.]enabled true\|false` | Ghi cú pháp |
| `eval golden add\|list\|rm`, `eval results` | Nhóm gốc `golden add\|from-run\|list\|show\|rm\|restore\|edit\|candidates`; `eval list\|show` (alias `eval golden …`, `eval results`) | Ghi nhóm `golden` là chính |
| `approve`, `requests` | `approvals` (danh sách) + `approve`/`deny`; alias `requests`, `approve --deny` | Ghi `deny` là lệnh riêng |
| `feedback <run> --good\|--wrong [--note]` | Positional `feedback <run> good\|wrong\|partial` (alias dạng cờ) | Ghi cả hai |
| `run … --continue <run>` | Có; dạng chính `run continue <run\|session> "<tin>"` (+ `run resume`) | Ghi cả hai |
| `trash list\|restore\|purge` | Gồm agent, team, MCP server; `purge` chỉ agent (team/MCP hết hạn retention tự xoá; skill archive nằm ở `knowledge`) | Ghi phạm vi |
| `guard policy show\|edit` | `guard policy` (xem) + `guard protect add\|list\|rm`; sửa catalog = sửa file `policy/` bằng tay | Ghi đúng lệnh |
| `daemon logs` | Đọc `logs/hub.log` (+ `daemon.out`), lọc `--run/--agent`, `-n` | — |
| Lệnh thêm không có trong BRD | `stop <run\|agent>`, `hook <Event>`, `top`, `attach`, `steer`, `takeover`, `template`, `report`, `cost`, `digest`, `regression`, `curation`, `telemetry`, `secret`, `audit`, `notify`, `config get\|set` | Bổ sung bảng FR-UI-08 |
| FR-RT-17 "hook gọi `cfharness hook <event>`" | settings gọi `python -I -m cfharness.observe_hook <event>` (không phụ thuộc PATH, khởi động nhanh); `cfharness hook` là cùng thân, cho người/script | Ghi lại |
| FR-RT-15 ma trận tương thích | Ma trận theo CỜ (`runtime.claude.flag_matrix`), phiên bản lạ không bị kiểm cờ (chỉ cảnh báo) | Ghi khoá cấu hình |
| FR-MS-02 `hub_cancel(run_id\|task_id)` | Có; `task_id` dừng cả run của task con (`origin_task_id`) | — |
| FR-MS-02 `hub_memory_search` | Tìm từ khoá trên file + bảng `runs` (prompt/summary), chưa có FTS5/transcript đầy đủ | Ghi "FTS5" là mục tiêu, hiện là tìm từ khoá tất định |
