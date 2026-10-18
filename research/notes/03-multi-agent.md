# 03 — Quản lý nhiều agent & điều phối trong Hermes Agent

Phạm vi: profiles, subagent/delegation, kanban, managed worker, cron, batch runners, lifecycle (interrupt/estop/lock), `pm/`.
Repo gốc: `research/hermes-agent` (đường dẫn dưới đây tương đối với thư mục đó). Ký hiệu **[suy luận]** = không đọc thấy trực tiếp, là nhận định của người viết.

---

## 0. Tóm tắt nhanh

Hermes có **4 tầng "đa agent"** khác nhau, không phải một mô hình thống nhất:

| Tầng | Định danh | Bền vững? | Dùng để |
|---|---|---|---|
| **Profile** | tên thư mục `~/.hermes/profiles/<name>` (HERMES_HOME riêng) | Có (filesystem) | "Một agent" lâu dài: config, `.env`, SOUL.md, memory, skills, sessions, cron riêng |
| **Subagent** (`delegate_task`) | `sa-<idx>-<hex8>` | Không — sống trong process, mất khi restart | Fork→join ngắn hạn trong 1 lượt chat |
| **Kanban worker** | task_id + run_id, assignee = tên profile | Có (SQLite) | Hàng đợi việc bền vững giữa nhiều profile, retry, review, human-in-loop |
| **Cron job** | job_id trong `cron/jobs.json` của từng profile | Có (JSON) | Việc định kỳ/one-shot |

Thêm: **Bot Mode** (desktop) biến profile thành "Bot" trong roster, có group chat và `message_agent` để agent nhắn nhau.

Chú ý triết lý: "profiles are islands on purpose" (`AGENTS.md:~38`, `hermes_cli/AGENTS.md:153`) — không có kế thừa config sống giữa profile; clone chỉ copy lúc tạo.

---

## 1. Profiles — "một agent = một HERMES_HOME"

### 1.1 Mô hình dữ liệu
- Profile = một thư mục. Default = `~/.hermes`; named = `~/.hermes/profiles/<name>/` (`website/docs/user-guide/profiles.md` mục "What are profiles?", "How it works").
- Thư mục con chuẩn: `memories, sessions, skills, skins, logs, plans, workspace, cron, home` (`hermes_cli/profiles.py:35` `_PROFILE_DIRS`).
- Một dir chỉ được coi là profile "sống" khi có identity marker: `config.yaml`/`.env`/`SOUL.md`/`profile.yaml`/`auth.json`/`state.db` và không bị tombstone (`hermes_cli/AGENTS.md:163-170`, `hermes_constants.named_profile_has_identity`).
- Metadata: `profile.yaml` chứa `description`, `description_auto`, `display_name`, `previous_names`, `ui_meta['hermes-bots']` (title/avatar Bot Mode) — đọc/ghi qua `read_profile_meta` / `write_profile_meta` (`hermes_cli/profiles.py:917`, `:959`).
- Bản ghi tổng hợp `ProfileInfo` (`hermes_cli/profiles.py:660-693`): `name, path, is_default, gateway_running, model, provider, has_env, skill_count, alias_path/alias_name, distribution_{name,version,source}, description, description_auto, display_name, bot_title, previous_names`.
- `description` là "nhãn vai trò" — được **kanban decomposer dùng để định tuyến việc theo vai trò** (comment tại `profiles.py:679-681`; docs kanban mục "Auto vs Manual orchestration").
- Tên hợp lệ: `PROFILE_ID_RE` trong `hermes_constants` (`profiles.py:30`, `validate_profile_name` `:390`).

### 1.2 CRUD có sẵn (CLI `hermes profile ...`, ref `website/docs/reference/profile-commands.md`)
| Thao tác | Lệnh | Code |
|---|---|---|
| Create | `profile create <n> [--clone | --clone-all | --clone-from X] [--clone-channels] [--sync-imports] [--description "..."]` | `create_profile` `profiles.py:1384` (build ở `profiles/.<name>.staging-<pid>` rồi `os.rename` atomically, `hermes_cli/AGENTS.md:159-161`) |
| Read | `profile list`, `profile show <n>`, `profile` (current), `profile info` | `list_profiles` `profiles.py:1034` |
| Update | `profile describe <n> --text/--auto`, `profile rename`, `profile alias`, `-p <n> config set ...`, sửa SOUL.md | `rename_profile` `profiles.py:2324`; describe dùng aux LLM `auxiliary.profile_describer` |
| Delete | `profile delete <n> [--yes]` (dừng gateway, gỡ service, alias, xoá data) | `delete_profile` `profiles.py:1760` |
| Chọn mặc định | `profile use <n>` (kiểu kubectl context) | — |
| Đóng gói | `profile export/import` (tar.gz, strip key), `profile install/update <git-url>` (distribution, `distribution.yaml`) | `profile_distribution.py` |
| Sửa lỗi danh tính | `profile migrate-identity`, `purge-identity` | — |

- Mỗi profile tự sinh lệnh alias `~/.local/bin/<name>` = `hermes -p <name>` (`create_wrapper_script` `profiles.py:541`).
- `-p` được áp trước khi import module (`_apply_profile_override()` trong `hermes_cli/main.py`, `hermes_cli/AGENTS.md:148`).
- Clone **không copy messaging channels** (bot token...) trừ khi `--clone-channels` (`hermes_cli/AGENTS.md:154-158`).
- Cảnh báo docs: **không được chạy 2 process agent cùng một profile** — memory sẽ chéo nhau (profiles.md, caution box).
- Profile KHÔNG phải sandbox: filesystem access vẫn như user; workdir điều khiển riêng bằng `terminal.cwd`; `HOME` chỉ tách khi `terminal.home_mode: profile`.
- Có REST/dashboard: `hermes_cli/web_server_profiles.py` (cùng đi qua `create_profile`).

### 1.3 Một process phục vụ nhiều profile (multiplex)
- Gateway multiplex (`gateway.multiplex_profiles`, mặc định on) phục vụ default + mọi profile sống: `profiles_to_serve(multiplex=True)` (`profiles.py:1136`). Scope profile bind qua contextvar, **không** qua `os.environ` (`AGENTS.md` mục "One process serves many profiles"; `hermes_cli/AGENTS.md:150-153`).
- Secret isolation giữa profile: `agent/secret_scope.py::get_secret` fail-closed khi multiplex (`agent/AGENTS.md:136-139`).

---

## 2. Subagent / delegation (`delegate_task`)

### 2.1 Cách spawn
- Tool `delegate_task`, toolset `delegation` (`tools/delegate_tool.py:696` schema, `:800` register). Tham số model thấy: `tasks[] {goal, context, output_schema, images, group}`, `action ∈ {spawn,list,steer,stop}`, `subagent_id`, `message`.
- Hàm chính `delegate_task(...)` `tools/delegate_tool.py:488`: kiểm tra pause (`is_spawn_paused`), giới hạn độ sâu (`:522`), đọc config, resolve credentials, tạo live transcript `cache/delegation/live/<id>/task-<n>.log`, build children, `_run_batch`.
- Child là một `AIAgent` mới trong **cùng process**, chạy trên thread (`_build_child_agent` `:199`); id `sa-{idx}-{uuid8}` (`:238`), có `parent_subagent_id` khi lồng.
- Top-level delegation **luôn chạy background** (model không chọn); depth>0 luôn đồng bộ (`_model_background_value` `:782`). Kết quả quay về qua hàng đợi completion của `tools/async_delegation.py`. ⚠ `tools/AGENTS.md:127` vẫn ghi "parent waits unless background=true" — lệch với code hiện tại.
- Plugin có API chính thức: `SubagentLifecycleService` (`agent/subagent_lifecycle.py:269`) với `launch/status/wait/cancel/steer/result/reconnect`; trạng thái `PENDING, STARTING, RUNNING, SUCCEEDED, FAILED, INTERRUPTED, CANCEL_REQUESTED, CANCELLED, UNKNOWN` (`:36`; docs `website/docs/developer-guide/subagent-lifecycle-api.md`). Handle serializable nhưng **giữ in-process 1 giờ**, restart → `RECONNECT_UNAVAILABLE`.

### 2.2 Cô lập context
- Child chỉ thấy `goal` + `context` của nó, không có lịch sử hội thoại cha (schema description `delegate_tool.py:715-722`).
- ContextVar `_DELEGATED_CHILD_CONTEXT` + scoped session id (`agent/delegation_context.py:15-40`); env marker `HERMES_DELEGATED_CHILD_CONTEXT` mang root board kanban bị "rào" (`:20`, `kanban_path_is_fenced` `:141`); `scrub_kanban_env` (`:122`) để child không giả danh kanban worker.
- Role theo độ sâu: `leaf` (không có `delegate_task, clarify, memory, send_message, cronjob`) / `orchestrator` (giữ `delegate_task`) (`tools/AGENTS.md:135-138`; `delegate_tool.py:229-232`).
- Toolset của child chỉ được thu hẹp, không mở rộng hơn cha (subagent-lifecycle-api.md "parent-broadening toolsets are rejected").
- Có worktree isolation tuỳ chọn (`tools/subagent_worktree.py`; docs delegation.md "Worktree Isolation").

### 2.3 Giới hạn (giá trị mặc định thực tế trong `hermes_cli/config_defaults.py:1360-1395`)
- `max_concurrent_children: 10`, `max_spawn_depth: 1` (phẳng), `orchestrator_enabled: true`, `max_iterations: 250`, `child_timeout_seconds: 0` (=không cap, timeout tính theo **không có tiến triển**), `max_summary_chars: 24000`, `oneshot_max_children: 2`, `subagent_auto_approve: false`.
- ⚠ `tools/AGENTS.md:130,138` ghi default 3 và depth 2 — đã lỗi thời so với `config_defaults.py` và `tools/delegate_tool_config.py:17,21`.
- Heartbeat cha mỗi 30s, stale khi idle 450s / kẹt tool 1200s (`delegate_tool.py:106-112`).

### 2.4 Trả kết quả
- Mỗi task 1 entry: `status`, `summary` (text cuối của child), có thể `schema_valid/schema_errors` nếu `output_schema`; tiến trình nền bỏ lại → `orphaned_processes`, `unread_completions` (`tools/AGENTS.md:139-144`). Summary bị cắt thì phần thừa spill vào `~/.hermes/cache/delegation/`.
- Batch background trả **1 message gộp**, hoặc theo `group` nếu bật `independent_completions`.
- Điều khiển sống: `action=list|steer|stop`; TUI `/agents` (`hermes_cli/cli_subagent_monitor.py`), RPC `delegation.pause` chặn spawn mới.
- **Không bền**: restart = mất (`cron/AGENTS.md` "Background delegate_task is process-local").

---

## 3. Kanban — bảng việc nhiều worker (mô hình điều phối chính cho "team")

### 3.1 Mô hình dữ liệu (`hermes_cli/kanban_db.py:796-990`)
- **Board**: một SQLite riêng + `workspaces/` + `logs/`; default `~/.hermes/kanban.db`, khác: `~/.hermes/kanban/boards/<slug>/kanban.db`. Board là ranh giới cô lập cứng; **tenant** là namespace mềm trong board.
- `tasks`: `id, title, body, assignee (=tên profile), status (triage|todo|ready|running|blocked|review|done|archived), priority, created_by, workspace_kind/path (scratch|dir:|worktree), branch_name, project_id, claim_lock, claim_expires, tenant, result, idempotency_key, consecutive_failures, worker_pid, worker_started_at, last_heartbeat_at, max_runtime_seconds, current_run_id, skills, model_override, provider_override, reasoning_effort, max_retries, goal_mode, goal_max_turns, session_id, block_kind, block_recurrences, workflow_template_id/current_step_key (v2, chưa dùng)`.
- `task_links(parent_id, child_id)` — DAG phụ thuộc; dispatcher đẩy `todo→ready` khi mọi cha `done`.
- `task_comments` — "giao thức giữa các agent"; worker đọc toàn bộ thread khi spawn.
- `task_events(kind, payload, run_id)` — audit/event log.
- `task_runs` — **mỗi lần thử 1 row**: `profile, status, claim_lock, worker_pid, heartbeat, started/ended, outcome (completed|blocked|crashed|timed_out|spawn_failed|gave_up|reclaimed), summary, metadata, error`.
- `task_attachments`, `kanban_notify_subs` (đẩy thông báo về chat gốc qua profile sở hữu).

### 3.2 Dispatcher
- Vòng lặp mặc định 60s, chạy **trong gateway** (`kanban.dispatch_in_gateway: true`) hoặc systemd riêng (`plugins/kanban/systemd/hermes-kanban-dispatcher.service`) (`cron/AGENTS.md` mục Kanban).
- `dispatch_once` (`hermes_cli/kanban_db_dispatch.py:1976`) chạy dưới tick-lock không chặn theo đường dẫn DB → 2 dispatcher không giẫm nhau. Mỗi tick: reclaim claim hết hạn, reclaim worker chết (`detect_crashed_workers` `:1318`), `enforce_max_runtime` `:649`, `detect_stale_running` `:752`, respawn guard `:1552`, giới hạn `max_in_progress` (+ `_per_profile`), memory-pressure, claim nguyên tử, spawn.
- Spawn mặc định: subprocess `hermes -p <assignee> chat -q ...` với env `HERMES_HOME` của assignee, `HERMES_KANBAN_TASK/RUN_ID/CLAIM_LOCK/WORKSPACE/DB/BOARD`, `HERMES_TENANT` (`_default_spawn` `kanban_db_dispatch.py:2802-2900`).
- Liveness = `(worker_pid, worker_started_at)` fingerprint, không phải PID trần.
- Circuit breaker: `kanban.failure_limit` (mặc định 2) lỗi liên tiếp → auto-block; exit 75 = rate-limit (requeue, không tính lỗi), 78 = lỗi provider không thể retry → block ngay; exit 0 mà task vẫn `running` = `protocol_violation` (tối đa 3).
- Worker protocol (inject vào system prompt): `kanban_show` → làm việc trong `$HERMES_KANBAN_WORKSPACE` → `kanban_heartbeat` → kết thúc bằng `kanban_complete | kanban_request_review | kanban_block`; có "stop nudge" nếu model định dừng mà chưa gọi tool kết thúc (`agent/kanban_stop.py`).
- Tool cho worker: `tools/kanban_tools.py` (`kanban_show, complete, request_review, request_changes, block, schedule, heartbeat, comment, create, link, attach...`); profile chat bật thêm `kanban_list/unblock` để điều phối.

### 3.3 Orchestration
- **Decomposer** tự động: task ở `triage` được aux LLM `auxiliary.kanban_decomposer` tách thành đồ thị con, chọn assignee theo `description` của profile (`hermes_cli/kanban_decompose.py`; config `kanban.auto_decompose`, `orchestrator_profile`, `default_assignee`).
- `hermes kanban specify` — mở rộng ý tưởng thô thành spec.
- `hermes kanban swarm "<goal>" --workers a,b,c --verifier r --synthesizer w` — tạo atomically root/blackboard + N worker song song + verifier + synthesizer (`hermes_cli/kanban_swarm.py:106`, blackboard = comment JSON `:261-290`).
- 9 pattern được docs liệt kê: fan-out, pipeline, voting, journal dài hạn, human-in-loop, @mention, workspace theo thread, fleet farming, triage specifier.
- Goal-mode: worker lặp kiểu Ralph với aux judge đến khi đạt (`goal_mode`, `goal_max_turns`).
- CLI đầy đủ: `init, create, list/ls, show, assign, link, unlink, comment, attach, complete, request-review, request-changes, block, unblock, archive, tail, watch, stats, runs, log, assignees, heartbeat, notify-*, dispatch [--dry-run], daemon, gc, decompose, specify, swarm` (`cron/AGENTS.md`). Có dashboard plugin `plugins/kanban/dashboard/` + REST.
- Kanban worker **không** được `delegate_task` (docs kanban "Kanban vs delegate_task"; `agent/managed_worker.py:29` `WORKER_DISABLED_TOOLSETS = ('delegation',)`).

---

## 4. Managed worker & runtime workers

- `agent/managed_worker.py`: entry exec riêng — gateway chạy mỗi lượt agent trong **subprocess con** giao tiếp qua frame JSON trên stdin/stdout (`MAX_FRAME` 4MB, `BOOTSTRAP_FIELDS` `:30`, `RESULT_FIELDS` `:33` gồm `final_response, interrupted, api_calls, tokens, estimated_cost_usd, failure_reason...`). Phía chủ: `gateway/session_managed_worker.py` (HELLO 60s, STOP_ACK 30s, `start_new_session` để giết cả nhóm khi chủ chết).
- `worker_executions` trong `state.db` (`hermes_state_common.py:564`): `execution_id, session_id, kind ∈ {cron, child, compute, kanban}, owner_epoch, generation, status ∈ {registered, running, unknown, terminal}`; `worker_receipts` để replay/idempotent. `hermes_state_runtime_workers.py` kiểm tra loại trừ worker theo toàn bộ lineage hội thoại (không cho reset khi có worker đang chạy); `hermes_state_worker_lifecycle.py` chặn worker tự tạo session/đổi định danh.
- **[suy luận]** Đây là lớp "process sandbox cho một lượt", không phải registry agent — nhưng là mẫu tốt cho "Run" có receipt.

---

## 5. Cron, batch_runner, mini_swe_runner

- Cron: store theo profile `<HERMES_HOME>/cron/jobs.json` (`cron/jobs.py:2,90`). Trường do người viết: `name, prompt, skills, model, provider, base_url, script, no_agent, monitor_script/url, context_from, schedule, deliver, origin, enabled_toolsets, workdir, attach_to_session, reasoning_effort, failure_deliver` (`cron/job_definition.py:13`); scheduler tự quản `next_run_at, state, repeat, pending_slot...`.
- CRUD: CLI `hermes cron list|add|edit|pause|resume|run|remove`, slash `/cron`, tool `cronjob(action=create|list|update|pause|resume|remove|run)` (`tools/cronjob_tools.py:1068`).
- Bảo đảm: tick-lock theo home `<home>/cron/.tick.lock`; một ticker tuần tự qua mọi profile; watchdog idle 600s; catch-up; at-most-once qua `pending_slot` + executions ledger (`cron/AGENTS.md`). Cron chạy `skip_memory=True`.
- `batch_runner.py`: chạy `AIAgent` trên dataset JSONL bằng `multiprocessing.Pool`, checkpoint `--resume`, gom thống kê tool — **dùng cho sinh trajectory/đánh giá, không phải điều phối team**.
- `mini_swe_runner.py`: chạy task tool-calling trong env local/docker/modal, ghi trajectory định dạng Hermes — cũng phục vụ data/eval.

---

## 6. Lifecycle: start / stop / interrupt / estop / heartbeat / lock

- **Start**: `hermes -p <n> chat`, `<n> gateway start|install` (systemd/launchd/s6 per profile), hoặc gateway multiplex phục vụ tất cả.
- **Interrupt**: `InterruptControlMixin` trên AIAgent (`agent/interrupt_control.py:158`): `interrupt(message, hard_cancel)`, `hard_interrupt`, `clear_interrupt`, `steer(text)` (chèn vào ranh giới lượt kế), `redirect(text)`. Vòng lặp kiểm tra `_interrupt_requested` mỗi iteration (`agent/AGENTS.md:23-28`). `/stop` lan xuống subagent qua `async_delegation.interrupt_for_session` (`tools/AGENTS.md:147-151`).
- **ESTOP**: `hermes pause` ghi sentinel `$HERMES_HOME/ESTOP`, `hermes resume` xoá; cron, kanban dispatcher, gateway turn mới đều bỏ qua; việc đang chạy không bị giết; profile cũng tôn trọng ESTOP ở fleet root (`agent/estop.py:1-50`).
- **Heartbeat**: kanban `kanban_heartbeat` + tự gia hạn claim; delegation heartbeat 30s; process registry `heartbeat=N`.
- **Lock**: turn lease bền trong state.db (`agent/turn_facade_lease.py:34` `DurableTurnLease`, TTL + refresh 60s) → một session một lượt tại một thời điểm; token lock gateway (`acquire_scoped_lock`, cấm 2 profile dùng chung bot token); `cron/.tick.lock`; kanban dispatch tick-lock; flock cho `jobs.json`.
- **Liveness**: luôn `(pid, start_time)`, không bao giờ PID trần (`hermes_cli/AGENTS.md` cuối mục Profiles).

---

## 7. `pm/` là gì

Không liên quan đến "project manager". `pm/` = **package/environment manager** của Hermes: khoá dependency (`hermes pm lock`, `uv.lock`), cài/sửa môi trường (`hermes pm install|repair`), cách ly môi trường plugin/tool, chính sách pin upper-bound (`pm/AGENTS.md`). Bỏ qua với mục tiêu agent-hub.

---

## 8. Bot Mode (bonus — gần nhất với "team chat")

- Desktop biến profile thành Bot trong roster (`website/docs/user-guide/bot-mode.md`); Bot = profile + `ui_meta` trong `profile.yaml`.
- Group chat nhiều Bot, @mention, `@all`, stop/hold từng thành viên; tool `message_agent(target, message)` để bot nhắn bot (`tools/bot_mode_dm.py`), relay xuyên máy qua file `bot_relay/` (`tools/bot_relay.py:1-12`), `hermes peer`.
- "Active now" dựa trên turn đang chạy / ghi trong 90s / heartbeat worker.

---

## 9. Áp dụng cho agent-hub

### 9.1 Hermes đã có gì đáng tái dùng
1. **Agent = thư mục home cô lập** (config + secrets + persona + memory + skills + sessions). CRUD/clone/export/distribution đầy đủ. Mô hình này đơn giản, dễ backup, dễ git hoá.
2. **Kanban SQLite** là bộ điều phối bền vững hoàn chỉnh: DAG task, runs-per-attempt, events, comments làm kênh giao tiếp, claim/lease + heartbeat, circuit breaker, phân loại exit code, giới hạn đồng thời theo profile, decomposer định tuyến theo `description`.
3. Phân biệt rõ **delegation (ephemeral, in-process)** vs **queue (durable, cross-agent)**.
4. ESTOP toàn hạm đội bằng sentinel file; interrupt/steer/redirect ở mức lượt.
5. `mcp_serve.py` đã có MCP server (nhưng chỉ expose hội thoại/approvals messaging, **không** expose quản lý agent/kanban).

### 9.2 Thiếu gì so với nhu cầu "quản lý team agent"
- **Không có thực thể Team.** Nhóm agent chỉ ngầm định qua board/tenant/Bot group chat. Không có membership, vai trò trong team, quyền, ngân sách theo team. **[suy luận]**
- **Agent registry phân tán**: trạng thái agent nằm rải rác (filesystem profile, `gateway.pid`, `task_runs`, `worker_executions` trong state.db *của từng profile*). Không có một bảng "Agent status" trung tâm (idle/busy/paused/error, run hiện tại, chi phí luỹ kế). Câu "Active now" của Bot Mode phải suy từ nhiều nguồn.
- **Subagent không bền**, không resume sau restart; handle chỉ in-process 1h.
- **Tự cải thiện** có ở mức skill/memory/curator (mảng khác), nhưng **không có vòng "đánh giá run → sửa cấu hình agent"** gắn với kanban outcome. `batch_runner` thiên về sinh dữ liệu huấn luyện. **[suy luận]**
- **Không có API điều khiển agent qua MCP** (create agent, assign task, xem run) — phải đi qua CLI `hermes kanban`/`hermes profile` hoặc REST dashboard.
- CLI xem hoạt động: có `kanban watch/tail/runs`, `/agents` (chỉ subagent trong session), dashboard web — chưa có một TUI "hạm đội" tổng thể. **[suy luận]**
- Docs/AGENTS.md có chỗ lệch code (default delegation 3 vs 10; depth 2 vs 1; "background=true" vs luôn background) → khi dựa vào Hermes phải kiểm code.

### 9.3 Đề xuất data model (phác thảo)

```text
Agent                       # ~ Hermes profile
  id (slug, bất biến), display_name, description/role (dùng để định tuyến)
  home_path                 # thư mục cô lập: config, secrets ref, persona(SOUL), memory, skills
  model, provider, reasoning_effort, toolsets_allowed[], tool_policy
  limits {max_concurrent_runs, max_iterations, budget_usd_per_day, idle_timeout_s}
  status (idle|busy|paused|disabled|error), last_heartbeat_at, current_run_id
  version / config_revision  # để tự cải thiện có lịch sử + rollback
  previous_ids[], created_at, updated_at, archived_at

Team
  id, name, goal/charter, board_id (1 team ~ 1 board, ranh giới cứng)
  members: TeamMember(agent_id, role: lead|worker|reviewer|verifier, weight, max_in_progress)
  policies {failure_limit, auto_decompose, review_required, budget}
  estop (bool, reason)       # ESTOP theo team ngoài ESTOP toàn cục

Task                        # ~ kanban tasks
  id, team_id, title, body, status(triage|todo|ready|running|blocked|review|done|archived)
  assignee_agent_id, created_by (human|agent_id), priority, parents[] (DAG)
  workspace {kind: scratch|dir|worktree, path, branch}
  overrides {model, skills[], reasoning}, max_retries, scheduled_at, idempotency_key
  consecutive_failures, block_kind

Run                         # ~ task_runs + worker_executions + managed_worker RESULT_FIELDS
  id, task_id?, agent_id, agent_config_revision, kind (task|delegation|cron|adhoc)
  parent_run_id (cây delegation), session_id, pid + start_fingerprint
  status/outcome (running|completed|blocked|crashed|timed_out|rate_limited|cancelled)
  started_at, ended_at, last_heartbeat_at, claim_lock, claim_expires
  summary, result_json (+schema_valid), error, exit_code
  usage {api_calls, tokens in/out/cache, cost_usd}
  transcript_path, events[] (tool calls, steer, interrupt)

Evaluation (cho self-improve)
  run_id, judge (human|llm|test), score, notes, proposed_change (diff vào Agent config/skill)
```

Nguyên tắc rút ra từ Hermes:
- Tách **Task** (việc) khỏi **Run** (lần thử) — giữ toàn bộ lịch sử retry.
- Liveness bằng `(pid, start_time)` + lease có TTL, không dùng PID trần.
- Một nơi duy nhất ghi trạng thái (SQLite WAL + tick-lock), mọi UI/CLI/MCP chỉ đọc/ghi qua API đó.
- Định tuyến theo `description/role` của agent; giữ "comment" làm kênh giao tiếp agent↔agent↔người.
- Delegation ngắn hạn để in-process; mọi thứ cần bền phải là Task.
- Expose qua MCP các tool: `agent_list/create/update/archive`, `team_*`, `task_create/assign/comment/unblock`, `run_list/tail/cancel/steer`, `fleet_pause/resume` — **[đề xuất]**, Hermes chưa có.
