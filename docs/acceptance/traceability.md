# Ma trận truy vết nghiệm thu — agent-hub (BRD 0.6)

| Mục | Nội dung |
|---|---|
| Ngày kiểm | 2026-10-18 |
| Người kiểm | QA độc lập (agent), không sửa code, không gọi `claude` thật |
| Nguồn đối chiếu | `docs/BRD-chi-tiet.md` 0.6 (§6 FR, §10 NFR, §13 DoD) ↔ `src/agent_hub/`, `tests/`, `docs/dev-notes/*.md`, `AGENTS.md` |
| Cách kiểm | Đọc hàm, grep, chạy test cụ thể, `uv run agent-hub … --help`, chạy chức năng với `AGENT_HUB_HOME` tạm + `FakeRuntime`/`claude` giả, script tái hiện trong scratchpad |
| Test suite | `uv run pytest -q` → **313 passed** (≈116 s). Bench `python -m agent_hub.tui.bench` (FakeRuntime, 50 agent / 10 chạy, 60 s) chạy xong |

**Quy ước trạng thái.** *Đạt*: có code VÀ có test (hoặc QA tự chạy được) cho tiêu chí chính. *Một phần*: thiếu một phần mô tả/tiêu chí, hoặc có code mà tiêu chí chính không có test. *Thiếu*: không có code. *Lệch BRD có chủ ý*: khác BRD và đã ghi lý do trong dev-notes.
Đường dẫn không có tiền tố là tương đối với `src/agent_hub/`; `tests/…` là test. NFR không có MoSCoW trong BRD → cột Ưu tiên ghi "NFR" (đều mang tính bắt buộc).

---

## 1. Bảng truy vết

### 1.1 FR-AG — Quản lý agent

| Mã | Tên ngắn | Ưu tiên | Mốc | Trạng thái | Bằng chứng | Ghi chú |
|---|---|---|---|---|---|---|
| FR-AG-01 | Tạo agent | Must | M0 | Đạt | `agents.py:191` (create), `:225` (event); `tests/test_agents.py::test_create_layout_db_and_event`, `::test_create_with_soul_and_duplicate`, `::test_create_validation_lists_fields`, `::test_create_unknown_mcp_rejected`; QA chạy CLI thật | Báo "agent đã tồn tại", exit 1. Lệch nhỏ: file là `config.yaml` (BRD `agent.yaml`); sai cả model lẫn MCP chỉ báo model. TUI form (Should) chưa kiểm |
| FR-AG-02 | Liệt kê/xem agent | Must | M0 | Đạt | `daemon.py:98` `_agent_list`, `store.py:807`; `tests/test_agents.py::test_list_show_edit_revisions`, `::test_soft_delete_and_restore`; QA chạy: agent đang chạy có `status=working` + `current_run_id` | Thiếu lọc `--team/--status` (cả CLI và `hub_list_agents`); tiêu chí `working` chỉ kiểm tay |
| FR-AG-03 | Sửa + revision | Must | M0 | Một phần | `agents.py:252`, `store.py:811`, `runs.py:226`; `tests/test_agents.py::test_list_show_edit_revisions` | **Không có `agent revert --to N`** (CLI "invalid choice", không RPC/MCP). `agent_revisions` lưu snapshot, không lưu diff. "Run đang chạy giữ revision cũ" đúng khi chạy tay, không có test |
| FR-AG-04 | Xoá mềm/restore/purge | Must | M0 | Một phần | `agents.py:289` remove, `:310` restore, `recovery.py:201` purge; `tests/test_agents.py::test_rm_refused_with_active_run`, `tests/test_recovery.py::test_purge_requires_soft_delete_and_retention` | **Không kiểm tư cách thành viên team, không `--detach-teams`**: QA xoá được agent đang là lead, team vẫn trỏ lead đã xoá (không gán lead mới được). Không có event `agent.archived`. Gợi ý lỗi ghi sai lệnh (`run stop` → đúng là `runs stop`) |
| FR-AG-05 | Pause/resume | Should | M0 | Đạt | `agents.py:332`; `tests/test_agents.py::test_pause_resume`, `tests/test_team_task.py::test_claim_is_atomic_across_connections` | Thiếu cờ `--stop-running` |
| FR-AG-06 | Clone/export/import | Should | M3 | Đạt | `agents.py:368/388/437`; `tests/test_team_mcp.py::test_clone_export_import_no_secret` | Đủ `created_by`, `cloned_from`, memory rỗng, export không secret |
| FR-AG-07 | Template | Could | M2 | Đạt | `templates/*.yaml`; `tests/test_templates.py::test_agent_from_template_runs_immediately` | Bộ template có `ops` thay `lead` |
| FR-AG-08 | doctor | Should | M0 | Một phần | `cli.py:151-220` | Chỉ kiểm MCP có trong registry, **không thử kết nối**; không có `--live`; không có test nào cho doctor |

### 1.2 FR-TM — Team

| Mã | Tên ngắn | Ưu tiên | Mốc | Trạng thái | Bằng chứng | Ghi chú |
|---|---|---|---|---|---|---|
| FR-TM-01 | Tạo/sửa/archive team | Must | M3 | Đạt | `teams.py`; `tests/test_team_task.py::test_team_crud_lead_unique_archive_restore` | Từ chối archive khi có task running; `--cancel-open-tasks` đúng. `team purge` chưa làm (ghi ở M3 §3) |
| FR-TM-02 | Thành viên/vai trò | Must | M3 | Đạt | `teams.py:109,225`; cùng test trên | Lead thứ hai bị từ chối; một agent nhiều team được |
| FR-TM-03 | Chính sách team | Must | M3 | Đạt | `TeamPolicies` (`teams.py`); `tests/test_team_task.py::test_review_request_changes_then_approve`, `::test_team_budget_blocks_claims` | |
| FR-TM-04 | Tổng quan team | Should | M3 | Đạt | `dispatch/cli.py:47`; `tests/test_team_mcp.py::test_mcp_team_flow_lead_two_workers_result` | Test không có ca "2 running + 3 done" |

### 1.3 FR-TK — Task & điều phối

| Mã | Tên ngắn | Ưu tiên | Mốc | Trạng thái | Bằng chứng | Ghi chú |
|---|---|---|---|---|---|---|
| FR-TK-01 | Run ad-hoc | Must | M0 | Đạt | `cli.py:413`, `runs.py`; `tests/test_dispatcher.py::test_run_records_cost_session_and_events`, `tests/test_e2e.py::test_e2e_daemon_run_follow` | Không có cờ `--detach` (mặc định đã detach) → cú pháp BRD lỗi argparse |
| FR-TK-02 | Tạo task | Must | M3 | Đạt | `tasks.py`; `tests/test_team_task.py::test_task_idempotency_dag_cycle_and_promotion` | |
| FR-TK-03 | Vòng đời task | Must | M3 | Đạt | `tasks.py`; `tests/test_team_task.py::test_hub_self_complete_block_and_scope`, `tests/test_team_mcp.py::test_rpc_task_list_get_contract` | |
| FR-TK-04 | Task DAG | Must | M3 | Đạt | `tasks.py`; `test_task_idempotency_dag_cycle_and_promotion` | **Assert kiểm prompt yếu** (`a and b or c` thiếu ngoặc) → test qua dù prompt thiếu tóm tắt cha |
| FR-TK-05 | Run tách task, retry | Must | M3 | Đạt | `dispatch/dispatcher.py`; `test_crash_retry_resumes_session_then_circuit_opens`, `tests/test_dispatch_crash.py::test_kill_worker_mid_run_retries_with_resume` | Spawn lỗi là `failed` (không `spawn_failed`) — ghi ở M3 §3 |
| FR-TK-06 | Dispatcher/lease/heartbeat | Must | M3 | Lệch BRD có chủ ý | `config.py:101-102` (tick 5 s, TTL 120 s); `test_claim_is_atomic_across_connections`, `test_kill_worker_mid_run_retries_with_resume` | Liveness khi đang chạy dùng returncode/`kill -0` thay `(pid,start_time)` (M3 §3). Test claim viết SQL tay, không gọi hàm claim thật |
| FR-TK-07 | Giới hạn đồng thời | Must | M0/M3 | Đạt | `runs.py`, `config.py:21`; `tests/test_dispatcher.py::test_global_limit`, `::test_per_agent_limit`, `tests/test_team_task.py::test_global_limit_and_member_limit` | |
| FR-TK-08 | Định tuyến + lead tách việc | Must | M3 | Đạt | `dispatch/plan.py`, `dispatch/routing.py`; `test_lead_decomposes_routes_by_role_and_aggregates`, `test_invalid_plan_blocks_root_without_children`, `tests/test_dispatch_crash.py::test_lead_plan_via_json_schema_subprocess` | Tổng hợp task gốc là ghép cơ học (M3 §3) |
| FR-TK-09 | Circuit breaker | Must | M3 | Đạt | `test_crash_retry_resumes_session_then_circuit_opens`, `test_rate_limited_requeued_with_backoff_not_counted`, `tests/test_dispatcher.py::test_auth_block` | |
| FR-TK-10 | ESTOP | Must | M0/M3 | Đạt | `runs.py:203-206`, `cli.py:473`; `test_estop_blocks_and_stops`, `test_estop_without_stop_running_lets_run_finish`, `tests/test_team_task.py::test_team_estop` | `hub_estop` (MCP) không có tham số `team` |
| FR-TK-11 | Bình luận task | Should | M3 | Đạt | `tests/test_team_task.py::test_comments_in_prompt` | `comment --steer` không có test |
| FR-TK-12 | Review/verify | Should | M3 | Đạt | `test_review_request_changes_then_approve` | |
| FR-TK-13 | Thời gian chạy tối đa | Must | M3 | Đạt | `tests/test_team_task.py::test_timeout_marks_timed_out_and_retries` | |
| FR-TK-14 | Continue run | Should | M1 | Đạt | `guard/cli.py:14`; `tests/test_resume_continue.py::test_continue_uses_resume_same_session_and_cost_delta` | Cú pháp `run continue <run>` (BRD: `run --continue`) |

### 1.4 FR-RT — Runtime adapter

| Mã | Tên ngắn | Ưu tiên | Mốc | Trạng thái | Bằng chứng | Ghi chú |
|---|---|---|---|---|---|---|
| FR-RT-01 | Interface AgentRuntime | Must | M0 | Đạt | `runtime/base.py:80`, `runtime/fake.py:31`, `runtime/claude_cli.py:199`; `tests/test_dispatcher.py::test_run_records_cost_session_and_events` | `capabilities()` báo `approval: False` dù đã có phê duyệt |
| FR-RT-02 | Dựng argv | Must | M0 | Một phần | `runtime/claude_cli.py:29-156`; `tests/test_argv.py::test_basic_argv`, `::test_bypass_guard_nfr15`, `::test_redaction_for_storage` | Các cờ trong bảng đều có, trừ `--forward-subagent-text`; `--system-prompt-snapshot` mặc định không bật (`config.py:260`). **Không có ma trận tương thích theo cờ** → tiêu chí 2 (từ chối spawn khi cờ không hỗ trợ) chưa đạt |
| FR-RT-03 | Cô lập cấu hình | Must | M0 | Đạt | `runs.py:323-446`, `claude_adapter.py:255-328`; `tests/test_adapter.py::test_serena_hook_flagged_as_isolation_violation`, `::test_unknown_mcp_server_flagged`, `::test_hub_real_isolated_run_fixture_clean` | Vi phạm lưu ở `runs.isolation_violations` (không thành outcome riêng) |
| FR-RT-04 | Chuẩn hoá stream-json | Must | M0 | Đạt | `claude_adapter.py:218-504`, `runtime/base.py:112`; `tests/test_adapter.py::test_sample_fixture_contract` (35 dòng, cost 0.00474188), `::test_unknown_type_and_invalid_line_do_not_break_run`, `::test_subagent_id_from_parent_tool_use_id` | `tool.generating` phát ở `content_block_start`, không ở delta |
| FR-RT-05 | Session cấp/resume/fork | Must | M0/M1/M4 | Đạt | `runs.py:219-221`, `claude_cli.py:62-70`; `tests/test_learn_review.py::test_correction_review_fork_patch_then_no_repeat`, `tests/test_resume_continue.py::test_continue_uses_resume_same_session_and_cost_delta` | Kiểm "transcript gốc không đổi" chỉ có trong nghiệm thu thật (M4-A) |
| FR-RT-06 | Steer giữa chừng | Must | M2 | Lệch BRD có chủ ý | `claude_cli.py:262-313`, `claude_adapter.py:451`; `tests/test_steer.py::test_claude_cli_steer_midtool_same_turn`, `::test_fake_runtime_steer_roundtrip` | Event `steer.sent` → `user.message` (M2 §4). Phương án dự phòng interrupt+resume không làm vì A-02 đã xác nhận |
| FR-RT-07 | Ngắt/dừng process group | Must | M0 | Đạt | `claude_cli.py:231` (`start_new_session`), `:347-373` (SIGINT→grace→SIGTERM→SIGKILL `killpg`); `tests/test_claude_runtime.py::test_stop_running_subprocess_is_cancelled` | QA chạy `claude` giả có con `sleep` bỏ qua tín hiệu: con chết sau grace+5 s. Chưa có test tự động "không còn tiến trình con" |
| FR-RT-08 | Ngân sách run | Must | M0 | Đạt | `runs.py:266-282`, `claude_cli.py:96-99`; `tests/test_dispatcher.py::test_budget_per_day`, `tests/test_adapter.py::test_budget_exceeded_outcome` | QA chạy: còn $0.30 → `--max-budget-usd 0.3` |
| FR-RT-09 | Cầu nối phê duyệt | Must | M1 | Đạt | `guard/runtime.py:50`, `guard/hook.py:31`, `approvals.py:270,321`; `tests/test_approval_flow.py::test_destructive_held_then_approved_runs_once`, `::test_timeout_waiting_human_releases_slot_then_resume_once`, `::test_safe_commands_run_without_approval`, `::test_static_deny_beats_hook_allow_restart_with_rule_lifted` | Nhánh deny không assert Signal `reject`/`permission_denials`. `default_permission_mode=None` thay `acceptEdits` (hook trả allow cho write — M1-B §3). Lớp `--disallowedTools` tĩnh chỉ 5 mẫu tiền tố |
| FR-RT-10 | Lắp prompt + cache | Must | M0/M4/M5 | Một phần | `prompt.py:21`, `learning/runtime.py:50-74`, `knowledge/prompt_blocks.py`; `tests/test_knowledge.py` (hash resume giữ, run mới đổi) | Đóng băng prompt bằng phát lại prompt đã lưu theo hash, không dùng `--system-prompt-snapshot on`. **Charter team nằm trong tin user** (`tasks.py:377,420`), không ở system prompt bước (3). Cả hai không ghi là lệch |
| FR-RT-11 | Model/effort/fallback | Must | M0 | Đạt | `claude_cli.py:88-93`, `runs.py:633-638,706`; `tests/test_argv.py::test_basic_argv` | Không test assert `runs.model` thực tế; `fallback_model` chỉ nhận 1 chuỗi |
| FR-RT-12 | Workspace repo thật | Should | M3 | Một phần | `runs.py:466,580-617`, `workspace.py`, `dispatch/rpc.py:205`; `tests/test_workspace.py::test_two_tasks_same_repo_serialized_and_readonly_not_locked`, `::test_checkpoint_diff_revert_keeps_user_changes`, `::test_revert_conflict_when_modified_after_run` | **Race khoá repo** (QA tái hiện, xem §6): task thứ hai cùng repo vẫn được spawn rồi chết `failed` ("workspace_locked"), tăng `attempts` và `consecutive_failures` trước khi về `ready/workspace_locked`. Worktree chưa checkpoint/khoá; bỏ qua file `.gitignore` (M3 §3) |
| FR-RT-13 | Subagent | Could | M3 | Một phần | `claude_cli.py:134`, `runs.py:724`; `tests/test_team_mcp.py::test_subagents_and_schema_reach_runspec` | Thiếu `--forward-subagent-text`; TUI chỉ có dòng `⤷`, chưa có cây |
| FR-RT-14 | Output có cấu trúc | Should | M3 | Đạt | `claude_cli.py:132`, `runs.py:723`; `tests/test_dispatch_crash.py::test_lead_plan_via_json_schema_subprocess` | `schema_valid` dựa vào có `structured_output`, hub không validate lại |
| FR-RT-15 | Tương thích phiên bản | Must | M0 | Một phần | `claude_cli.py:184` (cache 600 s), `runs.py:477-485` (`cli.version_warning`), `cli.py:169` (doctor) | Không kiểm lúc daemon khởi động; **không có cảnh báo đỏ `raw.unknown` > 5%/giờ + đề nghị ESTOP**; không test warning/doctor/TUI |
| FR-RT-16 | Rate limit | Must | M3 | Đạt | `runs.py:124-131,666`; `tests/test_team_task.py::test_rate_limited_requeued_with_backoff_not_counted`, `::test_rate_limit_event_throttles_concurrency` | |
| FR-RT-17 | Hooks của hub | Must | M1/M4 | Một phần | `guard/runtime.py:29-62`, `guard/hook.py`; `tests/test_guard_hook.py::test_hook_daemon_unreachable_denies`, `::test_hook_daemon_silent_times_out_denies`, `::test_hook_command_wraps_exit2` | PreToolUse fail-closed: đạt. **Không có `PostToolUse/UserPromptSubmit/Stop`, không hook quan sát fail-open; `hook.timeout` khai báo (`events.py:68`) nhưng không nơi nào phát** → tiêu chí GWT chưa đạt. M4-A ghi hook học "không cần" (A-11) |
| FR-RT-18 | Xác thực | Must | M0 | Đạt | `runs.py:44-47,207-213,812-821`, `cli.py:171-185`; `tests/test_dispatcher.py::test_auth_block` | Phát hiện theo mẫu chữ lỗi, không dùng `apiKeySource` |
| FR-RT-19 | Khôi phục sau restart | Must | M1 | Đạt | `recovery.py:96-178`; `tests/test_recovery.py::test_reconcile_kills_orphan_marks_interrupted_and_resumes` (<30 s, giữ `waiting_human`), `tests/test_workspace.py::test_interrupted_run_not_resumed_when_human_edited_files` | QA chạy kịch bản 1 task + 1 ad-hoc: cả hai `interrupted` rồi resume cùng session (0,08 s). Người sửa file → chỉ event `workspace_changed`, **không báo Slack** |

### 1.5 FR-LN — Tự cải thiện

| Mã | Tên ngắn | Ưu tiên | Mốc | Trạng thái | Bằng chứng | Ghi chú |
|---|---|---|---|---|---|---|
| FR-LN-01 | Thu Signal | Must | M4 | Đạt | `learning/signals.py:50-220`; `tests/test_learn_review.py::test_signal_detectors`, `::test_edit_output_detected_from_human_edit` | Không test riêng "không lặp lần 4" (code có `_open_fp`) |
| FR-LN-02 | Kích hoạt review | Must | M4 | Đạt | `learning/review.py`; `test_review_jobs_merge_and_counter_trigger` | |
| FR-LN-03 | Review fork | Must | M4 | Đạt | `learning/review.py:228`; `test_correction_review_fork_patch_then_no_repeat` (argv `--resume --fork-session`, tools `Read,Grep,Glob`, MCP chỉ hub-self) | Không test review vượt ngân sách. `tools[]` khác run gốc (M4-A, có chủ ý) |
| FR-LN-04 | Proposal | Must | M4 | Đạt | `learning/pipeline.py:58`; `test_guard_blocks`, `test_learning_rpc_status_has_no_approval_queue` | Không có trạng thái chờ duyệt |
| FR-LN-05 | Pipeline L1/L2/L3 | Must | M4/M5 | Một phần | `learning/pipeline.py:282-289,408-534`; `test_l1_skill_patch…`, `test_large_patch_is_l2…`, `test_l3_config_change_needs_eval`, `test_policy_touch_and_self_escalation_and_bounds` | `auto_apply_max_items` khai báo (`config.py:133`) nhưng không dùng. **Agent có thể đi đường `agent-hub learn propose` với `created_by="user"`** (xem FR-LN-15) |
| FR-LN-06 | Ledger/rollback | Must | M4 | Một phần | `learning/ledger.py:157-355`; `test_ledger_rollback_chain_conflict_and_append_only` | **Rollback cấu hình bỏ qua kiểm xung đột và khôi phục TOÀN BỘ cấu hình** từ `before_blob` → ghi đè thay đổi khác (QA tái hiện) |
| FR-LN-07 | Guard học | Must | M4 | Đạt | `learning/guard.py:94-252`; `test_guard_blocks`, `test_memory_limit_requires_consolidation` | "Review kế tiếp nhận yêu cầu gộp" chỉ ở dạng lý do chặn |
| FR-LN-08 | Curator pha 1 | Should | M4 | Đạt | `learning_ops/curator.py`; `tests/test_curator_phase1.py::test_plan_transitions`, `::test_archive_through_m4a_ledger` | |
| FR-LN-09 | Curator pha 2 | Could | M5 | Lệch BRD có chủ ý | `curation/service.py`; `tests/test_curation.py::test_phase2_merge_with_snapshot_and_rollback` | Dùng tool hub-self thay một kế hoạch `--json-schema` (M5-B) |
| FR-LN-10 | Eval gate | Must | M4 | Một phần | `evals/gate.py:154-229`; `tests/test_eval_gate.py::test_check_eval_failed…`, `::test_post_apply_rolls_back_on_drop`, `::test_check_without_golden_defers…` | L1 không golden **không được gắn cờ `unevaluated`** vào ledger/`regression_watch` (QA tái hiện) → theo dõi hồi quy không chặt hơn |
| FR-LN-11 | Ba tầng tri thức | Must | M4/M5 | Đạt | `knowledge/tiers.py`; `test_tier_shadowing_and_global_skills`, `tests/test_tier_knowledge.py::test_prompt_order_global_team_agent`, `tests/test_canary.py::test_team_change_eval_canary_then_promoted_without_asking` | |
| FR-LN-12 | Curator liên agent | Should | M5 | Đạt | `curation/service.py`; `test_cross_agent_proposes_team_promotion_via_pipeline`, `test_after_promoted_agent_copies_archived` | Chỉ từ vựng, chưa embedding (M5-B) |
| FR-LN-13 | Event + digest | Must | M4 | Một phần | `learning_ops/digest.py`; `tests/test_digest.py::test_digest_content`, `::test_tui_first_open_of_day`, `::test_scheduled_digest_sent_to_fake_slack` | **TUI không hiện thông báo "X learned: …" khi áp dụng** (app không nghe `learning.applied`) |
| FR-LN-14 | Hồ sơ USER chung | Should | M5 | Đạt | `tests/test_user_profile.py::test_user_profile_shadow_eval_then_applies_to_all_with_watch`, `::test_line_pin_blocks_learning_patch` | |
| FR-LN-15 | Công tắc học | Must | M4 | Một phần | `learning/policy.py:94-105`, `learning/pipeline.py:380-391`; `test_switches_disable_and_suggest`, `test_learning_disabled_no_review` | Proposal đổi công tắc bị chặn (đạt). **Lỗ: classifier xếp `agent-hub learn propose|rollback` là `write`, RPC gán cứng `created_by="user"`** → agent có Bash mạo danh người, vượt `auto_apply=false`/pin, né tự rollback (QA xác minh classifier) |
| FR-LN-16 | Canary/shadow | Must | M5 | Lệch BRD có chủ ý | `learning/canary.py:298-315`; `test_regression_on_canary_rolls_back_before_spreading` | Canary rollback cả thay đổi tầng team do NGƯỜI tạo (M5-A) — mâu thuẫn bất biến "tự rollback không chạm thay đổi của người" (§4) |
| FR-LN-17 | Theo dõi hồi quy | Must | M4 | Một phần | `learning_ops/regression.py:150-208`; `test_correction_spike_rolls_back_latest_change`, `test_human_change_*` | Lọc actor đúng, nhưng rollback bên dưới (FR-LN-06) ghi đè cấu hình của người. Test dùng `FakeLedger` nên che lỗi |
| FR-LN-18 | Giới hạn tần suất | Must | M4 | Đạt | `learning/pipeline.py:294-333,556-588`; `test_rate_limit_defers_then_applies`, `test_cooldown_per_target` | Không test riêng trần cấu hình/tuần và team-global/ngày |
| FR-LN-19 | Tự sửa cấu hình | Must | M4 | Một phần | `learning/guard.py:194-252`, `learning/policy.py:30-36`; `test_policy_touch_and_self_escalation_and_bounds` | `config_bounds` + chặn nâng quyền đúng. Digest không ghi cũ→mới/chi phí; revision luôn `actor=learning` kể cả do người; đường vòng CLI (FR-LN-15) |

### 1.6 FR-MS — MCP server của hub

| Mã | Tên ngắn | Ưu tiên | Mốc | Trạng thái | Bằng chứng | Ghi chú |
|---|---|---|---|---|---|---|
| FR-MS-01 | Transport stdio + HTTP | Must | M1 | Đạt | `mcp_server/cli.py:84-99`, `mcp_server/http.py:62-79`; `tests/test_mcp_server.py::test_stdio_transport`, `::test_http_transport_tokens` (401) | |
| FR-MS-02 | Danh mục tool `hub_*` | Must | M1 | Một phần | `mcp_server/server.py:242-566,595`; `tests/test_mcp_server.py::test_tools_annotations_and_resources` | **Thiếu `hub_policy_get`** (M1). Đổi tên có ghi (M1-A). Chưa ghi: `hub_events` không lọc `agent_id`, `hub_cancel` không nhận `task_id`, `hub_estop` không có `team`, `hub_status` thiếu rate limit/phiên bản CLI; M4 dùng `hub_learn_*`/`hub_knowledge_*` thay `hub_memory_search`, `hub_skill_*`, `hub_learning_pin/rollback`. `hub_golden_rm` không đánh destructive |
| FR-MS-03 | Run bất đồng bộ | Must | M1 | Đạt | `server.py:366` (`wait_s le=25`), `mcp_server/rpc.py:127,274`; `test_events_long_poll_waits`, `test_run_events_flow_idempotency_feedback` | Không test đếm số lần gọi ≤ ceil(T/20)+1 |
| FR-MS-04 | Instructions + resource | Must | M1 | Một phần | `mcp_server/guide.py`, `server.py:569-574` | Thiếu `hub://events/{run_id}` (tuỳ chọn, M1-A việc dở) |
| FR-MS-05 | Phê duyệt qua MCP | Must | M1 | Một phần | `mcp_server/rpc.py:267,323`; `tests/test_approval_flow.py::test_mcp_hub_approve_resolves_approval` | Không test `open_requests` trong phản hồi **`hub_events`** kèm mô tả tool + input |
| FR-MS-06 | Phân quyền, localhost | Must | M1 | Đạt | `mcp_server/http.py:26-38,62-66`, `server.py:189-199`; `test_read_level_denied_and_cancel`, `test_http_transport_tokens` | Xem §4 bất biến 6 |
| FR-MS-07 | Idempotency | Should | M1 | Đạt | `mcp_server/rpc.py` `_idem_put`; `test_run_events_flow_idempotency_feedback` | `hub_task_create` không có test riêng |
| FR-MS-08 | hub-self MCP | Must | M3/M4 | Một phần | `mcp_server/hub_self.py:131-136`, `dispatcher.py:682-693`; `tests/test_team_mcp.py::test_hub_self_stdio_server_roundtrip`, `tests/test_team_task.py` (task_complete task khác → từ chối), `test_require_completion_call_protocol_violation` | **Worker thiếu `memory_search`**. `protocol_violation` tắt mặc định (M3, có chủ ý); không có lượt nhắc qua resume |

### 1.7 FR-MC — MCP client cho agent

| Mã | Tên ngắn | Ưu tiên | Mốc | Trạng thái | Bằng chứng | Ghi chú |
|---|---|---|---|---|---|---|
| FR-MC-01 | Registry | Must | M1 | Lệch BRD có chủ ý | `mcp_client/config.py:29-38`; `tests/test_mcp_registry.py::test_add_validate_trust_and_literal_secret` | DB là nguồn sự thật, yaml là bản sao; `trusted|untrusted` (`full` = alias) — M1-A |
| FR-MC-02 | Gán MCP, sinh mcp.json | Must | M1 | Một phần | `runs.py:363-412,544`, `mcp_client/registry.py:384-410`; `test_probe_annotations_and_contract` | mcp.json 0600, xoá sau run (QA chạy). **Server chưa probe có `tools.include` → argv không có allow/deny `mcp__vb__…`** (QA chạy); không test e2e argv/`permission_denials` |
| FR-MC-03 | Trust / readOnlyHint | Must | M1 | Một phần | `guard/classifier.py:230-265`, `mcp_client/wiring.py`; `tests/test_mcp_tool_write.py::test_tool_write_audit_and_event`, `tests/test_guard_classifier.py::test_brd_acceptance_examples` | **GWT 2 (untrusted → mở approval) không có test** (nhánh `mcp_untrusted_write`/`mcp_require_approval` không ca nào chạm). Server chưa probe → mọi tool kể cả đọc đều phải duyệt |
| FR-MC-04 | Import `~/.claude.json` | Must | M1 | Đạt | `mcp_client/importer.py`; `tests/test_mcp_import.py::test_import_all_in_process` (10 server, checksum, grep secret), `::test_import_cli_via_daemon_and_home_isolated` | |
| FR-MC-05 | Secret | Must | M1 | Một phần | `secrets.py`, `audit.py`, `redact.py`; `tests/test_secrets.py`, `tests/test_audit.py` | Grep secret chỉ phủ đường import, không phủ đường chạy run. Lọt tiềm năng: stdout thô, stderr probe vào `logs/daemon.out`, env-fallback không vào bộ che (xem §4 bất biến 5) |
| FR-MC-06 | `mcp test` | Should | M1 | Đạt | `mcp_client/probe.py:28`; `test_probe_bad_command_gives_hint` | Lệnh thật là `mcp probe` (alias `test`) |

### 1.8 FR-UI — Giao diện CLI/TUI

| Mã | Tên ngắn | Ưu tiên | Mốc | Trạng thái | Bằng chứng | Ghi chú |
|---|---|---|---|---|---|---|
| FR-UI-01 | Màn htop `top` | Must | M2 | Một phần | `tui/app.py:73-92,121`, `tui/rpc.py:186-200`; `tests/test_tui.py::test_top_three_agents_live`, `::test_filter_sort_and_learnings_placeholder` | **Thiếu phím `f` feedback**; ô "học hôm nay" đếm mọi `learning.*` thay vì số thay đổi |
| FR-UI-02 | Attach/detach | Must | M2 | Đạt | `tui/app.py:611`; `test_attach_replay_then_live_and_detach_keeps_run`, `test_closing_tui_does_not_affect_runs` | Chưa có cây subagent; `ctx%` ước lượng (M2 §4) |
| FR-UI-03 | Replay attach muộn | Must | M2 | Đạt | `bus.py:166-195`; `test_attach_late_after_finish_reads_db`, `tests/test_bus.py::test_ring_truncation_and_backfill` | Không test "hàng nghìn event" |
| FR-UI-04 | Duyệt từ TUI | Must | M2 | Đạt | `tui/app.py:76-78,1372`; `test_approve_from_tui_and_second_viewer_conflict`, `test_deny_with_note_from_attach` | Viewer 2 trong test là client RPC; phím `N` không test; CLI là `deny` riêng (BRD `approve --deny`) |
| FR-UI-05 | Steer/stop | Must | M2 | Một phần | `tui/app.py:79-81`; `test_steer_and_stop_from_tui` | **Không có `agent-hub stop <run|agent>`**; chỉ `runs stop <run>`, không nhận agent |
| FR-UI-06 | Tab Learnings | Must | M4 | Một phần | `tui/learnings.py`; `tests/test_tui_learnings.py::test_learnings_tab_feed_rollback_pin_digest_and_golden` | Thiếu phím `L` (M4-B việc dở), mục Ledger lọc theo agent, báo cáo curator; feed bỏ `regression.detected`, `cache.warning` |
| FR-UI-07 | Tab Tasks/Teams | Should | M3 | Một phần | `tui/app.py:844-975`; `test_tasks_tab_lists_and_creates` | Chưa có cây DAG (M2 §5); test không kiểm "2 cha + trạng thái" |
| FR-UI-08 | Bộ lệnh CLI | Must | M0→M5 | Một phần | `cli.py:640-672` (mã thoát); QA chạy `--help` mọi lệnh; `agent list --json` sạch, exit 1/3 đúng | **18 lệnh thiếu/đổi tên**, đa số không ghi lệch (danh sách §3, lỗ hổng H-08). Nguy hiểm: `run list|show|logs` bị hiểu là `run <agent> <prompt>` |
| FR-UI-09 | Firehose summary | Must | M2 | Đạt | `events.py:30-62`, `bus.py`; `test_firehose_summary_only_and_resubscribe_after_slow_consumer` | |
| FR-UI-10 | Takeover | Could | M2 | Đạt | `tui/takeover.py`; `test_build_takeover_argv_pure`, `test_takeover_print_and_record` | Không `--fork-session`, không Signal `edit_output` (M2 §5) |

### 1.9 FR-OB — Quan sát & chi phí

| Mã | Tên ngắn | Ưu tiên | Mốc | Trạng thái | Bằng chứng | Ghi chú |
|---|---|---|---|---|---|---|
| FR-OB-01 | Event store/bus | Must | M0 | Đạt | `events.py:148`, `bus.py:117-125`; `tests/test_bus.py::test_slow_consumer_dropped_agent_not_blocked` (10.000 event), `tests/test_store.py::test_events_append_only_and_query` | Ring buffer gom **theo agent** (BRD: theo run) — chưa ghi lệch |
| FR-OB-02 | Usage & chi phí | Must | M0 | Đạt | `reports.py:43`, `store.py:86`; `tests/test_resume_continue.py`; QA chạy 3 run → `cost --by agent` = 3 × 0.00474188 | Không test CLI tự động đúng tiêu chí |
| FR-OB-03 | Ngân sách ngày | Must | M0/M3 | Đạt | `runs.py:144-164,275`, `dispatcher.py:208`; `test_team_budget_blocks_claims`, `test_agent_and_hub_daily_budget` | Ngân sách còn lại không trừ run đang chạy → nhiều run song song có thể cùng vượt (NFR-17) |
| FR-OB-04 | Cache metric | Should | M4 | Một phần | `learning/cache.py`, `learning/service.py:142-156` | Cảnh báo không lên `doctor`/TUI; không test "đổi hash > 3 lần/ngày" |
| FR-OB-05 | Log | Must | M0 | Đạt | `logs.py` (JSON, 10 MB×5), `runtime/claude_cli.py:221`; QA chạy spawn lỗi → `runs logs` in stderr | Không test tự động; `run logs` (tên BRD) không chạy |
| FR-OB-06 | Report | Should | M3 | Đạt | `reports.py:55-108`; `tests/test_team_cli.py::test_cli_team_task_flow` | Test mỏng |
| FR-OB-07 | OpenTelemetry | Could | M5 | Một phần | `telemetry/otel.py`; `tests/test_otel.py::test_disabled_by_default`, `::test_spans_metadata_only`, `::test_include_payloads_never_for_mcp` | Mặc định tắt, MCP không xuất: đúng. **Khi `include_payloads=true`, secret không khớp mẫu regex lọt vào span** (QA tái hiện `sk-live-…`); `reason` tự do xuất cả khi tắt payload |
| FR-OB-08 | Audit điều khiển | Must | M1 | Đạt | `audit.py:33-58`, `approvals.py:290`; `tests/test_audit.py::test_record_query_and_append_only`, `tests/test_approval_flow.py` | before/after chỉ là params + tóm tắt (M1-A) |

### 1.10 FR-SG — Bảo vệ thao tác phá huỷ

| Mã | Tên ngắn | Ưu tiên | Mốc | Trạng thái | Bằng chứng | Ghi chú |
|---|---|---|---|---|---|---|
| FR-SG-01 | Danh mục phá huỷ | Must | M1 | Một phần | `guard/classifier.py:274,356,371,1184`; `tests/test_guard_classifier.py::test_recall_100_percent`, `::test_brd_acceptance_examples` | 3 tiêu chí BRD đạt. **Lọt**: xoá/ghi đè qua trình thông dịch (`pathlib.unlink`, `subprocess.run(['rm',…])`, `open(f,'w')`, `writeFileSync`), `cp -r`/`rsync` đè file có sẵn, `vim -c`/`ed`; SQL trong tham số tool MCP không phân tích |
| FR-SG-02 | Tài nguyên bảo vệ | Must | M1 | Đạt | `guard/policy.py:197`; `tests/test_guard_policy.py::test_policy_store_versioned_and_builtin_protected`, `test_cli_guard_and_approvals` | Mặc định đủ theo BRD + mã nguồn hub |
| FR-SG-03 | Phát hiện/phân loại | Must | M1 | Một phần | `guard/hook.py`, `guard/runtime.py`, `guard/service.py:125` | Bước 2 (LLM phụ) chưa làm — `uncertain` luôn giữ chờ người (M1-B, lệch có chủ ý, an toàn hơn). Hook timeout → deny nhưng không mở approval nếu daemon không nhận được. Lỗ phân tích như FR-SG-01 |
| FR-SG-04 | Phê duyệt / waiting_human | Must | M1 | Một phần | `approvals.py` (`hold`, `to_waiting_human`, `consume_ticket`), `runs.py:902,940`; `test_timeout_waiting_human_releases_slot_then_resume_once`, `test_destructive_held_then_approved_runs_once` | Luồng chờ người, nhả slot, giữ khoá, vé một lần: đạt. **Agent tự duyệt được** qua `python3 -m agent_hub.cli approve`, `… | xargs agent-hub`, client socket Python (QA xác minh) — RPC `approval.approve` (`guard/rpc.py:84`) không xác thực người gọi |
| FR-SG-05 | Bất biến chính sách | Must | M1/M4 | Một phần | `guard/runtime.py` `policy_deny_rules`, `guard/classifier.py` `policy_write`, `learning/guard.py:28` | Write/Edit vào `policy/` bị chặn 2 lớp; vòng học `policy_touch` chặn. **Lọt qua Bash**: `python3 -c "open('<HUB>/policy/…','a')…"` và `ln -s <HUB>/policy pol; echo x > pol/…` → `write` (QA xác minh) |
| FR-SG-06 | Xoá mềm | Must | M1/M4 | Đạt | `recovery.py:181,201`, `agent purge --confirm`; teams/tasks `deleted_at`; `test_purge_requires_soft_delete_and_retention`, `test_phase2_merge_with_snapshot_and_rollback` | `team purge` chưa có (M3). `classify_mcp` trả kết quả cho `hub-self` trước khi xét tên mang nghĩa xoá (tiềm ẩn) |
| FR-SG-07 | Bộ test phân loại | Must | M1 | Một phần | `tests/test_guard_classifier.py`: 163 ca phá huỷ + 116 ca an toàn = 279 | Recall 100%, báo nhầm 0% **trên bộ test**; recall thực < 100% (FR-SG-01). Assert kích thước chỉ ≥ 60; **không có CI** (`.github/` không tồn tại), không chạy khi nâng CLI |
| FR-SG-08 | Audit ghi/phá huỷ | Must | M1 | Đạt | `guard/service.py` `_record`, `approvals.py` `resolve`, trigger append-only `store.py:166,243`; `test_mcp_hub_approve_resolves_approval` (`resolved_channel=mcp:`) | |
| FR-SG-09 | Slack | Must | M1 | Đạt | `notify/slack.py`; `tests/test_notify_slack.py` (5 test) | Thử lại tối đa 5 lần; Slack cho `workspace_changed` chưa có |

### 1.11 NFR

| Mã | Tên ngắn | Ưu tiên | Mốc | Trạng thái | Bằng chứng | Ghi chú |
|---|---|---|---|---|---|---|
| NFR-01 | Độ trễ event | NFR | M2 | Đạt | bench: event→TUI p95 24,3 ms | Trên FakeRuntime; không đo riêng stdout→bus |
| NFR-02 | ≥5 run đồng thời | NFR | M2 | Đạt | bench: max 10, 100/100 completed | Tải giả |
| NFR-03 | `top` ≥1 Hz | NFR | M2 | Đạt | bench: render gap p95 500 ms | |
| NFR-04 | Overhead daemon | NFR | M2 | Đạt | bench: RSS 59 MB, CPU 4,5% | Sát trần 5%; chưa đo với `claude` thật |
| NFR-05 | Agent sống độc lập UI | NFR | M2 | Một phần | `tests/test_tui.py::test_closing_tui_does_not_affect_runs`, bench | Không test ngắt client MCP giữa run |
| NFR-06 | Khôi phục ≤30 s | NFR | M1 | Đạt | `tests/test_recovery.py::test_reconcile_kills_orphan_marks_interrupted_and_resumes` | Mô phỏng bằng Hub mới, không `kill -9` thật |
| NFR-07 | Toàn vẹn dữ liệu | NFR | M0 | Một phần | `store.py:668` (WAL), `:696` `tx()`, claim `dispatcher.py:417-419` | `Tasks.set_status`, `update_run` không CAS/không transaction; test claim viết SQL tay |
| NFR-08 | Backpressure | NFR | M2 | Đạt | `test_slow_consumer_dropped_agent_not_blocked`, bench | |
| NFR-09 | Không chạy trùng | NFR | M3 | Đạt | `store.py:307` (UNIQUE idempotency), claim `current_run_id IS NULL` | Không có ràng buộc DB "1 run running/task" |
| NFR-10 | Secret | NFR | M1 | Một phần | `tests/test_mcp_import.py:85-91`, `tests/test_secrets.py`, `test_clone_export_import_no_secret` | Stdout thô lưu nguyên văn; `redact` theo regex, không theo giá trị SecretStore; daemon chết để lại `runtime/<run>/mcp.json` có secret; không test e2e run có secret |
| NFR-11 | Trust MCP | NFR | M1 | Đạt | `guard/classifier.py:245-265`, `mcp_client/importer.py:155`; `test_add_validate_trust_and_literal_secret` | Nhánh untrusted/require_approval không có test |
| NFR-12 | Fail-closed, chờ người | NFR | M1 | Một phần | `guard/hook.py`, `guard/service.py`; `tests/test_guard_hook.py` (4 nhánh) | Fail-closed + không tự từ chối: đạt. "Không bao giờ chạy khi chưa có người" bị phá bởi đường tự duyệt (FR-SG-04) |
| NFR-13 | Cô lập cấu hình | NFR | M0 | Đạt | `claude_adapter.py:266,318-327`; `test_isolated_fixture_no_serena_hook_no_user_mcp` | Fixture từ run thật |
| NFR-14 | Bề mặt mạng | NFR | M1 | Đạt | `mcp_server/http.py:19,64-68`, socket 0600 `daemon.py:294`; `test_http_transport_tokens` | So token không hằng thời gian (nhỏ) |
| NFR-15 | Quyền nguy hiểm | NFR | M0/M1 | Một phần | `runtime/claude_cli.py:115-145`; `tests/test_argv.py::test_bypass_guard_nfr15`, `learning/guard.py:224` | `tool_policy` bypass trên `dir:` bị chặn. **`runtime.claude.isolation.extra_args` lọt** `--permission-mode bypassPermissions`, `--permission-mode=…`, `--dangerously-skip-permissions=true`, `--allow-dangerously-skip-permissions` (cấu hình của người, nhưng là bẫy). Không dùng `acceptEdits` trên repo thật |
| NFR-16 | Prompt injection | NFR | M4 | Một phần | `mcp_client/scan.py`, `learning/threats.py`; `test_guard_blocks`, `test_scan_and_parse` | System prompt không dặn "output tool là dữ liệu"; Unicode TAG/zero-width không có test |
| NFR-17 | Ngân sách cưỡng chế | NFR | M3 | Một phần | `runs.py:135-151,266-281`; `test_team_budget_blocks_claims` | Không trừ run đang chạy → song song vượt trần |
| NFR-18 | Giữ prompt cache | NFR | M4 | Một phần | `learning/cache.py`; `tests/test_learn_review.py:105,123` | Không test tỷ lệ ≥70%; số đo chỉ trong tài liệu (≈84%) |
| NFR-19 | Overhead học ≤15% | NFR | M5 | Một phần | `config.py:123,155-156`; `tests/test_overhead.py` | Có trần riêng; **đo thật: chi phí học 68,6%** (mẫu nhỏ); công thức học/làm-việc thay học/tổng |
| NFR-20 | Truy vết | NFR | M0 | Đạt | `store.py:60-95`, `runs.py:226,511`; `tests/test_dispatcher.py:118-123` | |
| NFR-21 | Log có cấu trúc | NFR | M0 | Một phần | `logs.py` | Log không mang `run_id`/`agent_id`; không test |
| NFR-22 | Interface Runtime | NFR | M0 | Một phần | `runtime/*` | `runs.py:326-412` dựng file riêng của Claude; `evals/judge.py:115`, `curation/llm.py:61` spawn `claude` trực tiếp; `config.py:411` cứng `claude|fake` |
| NFR-23 | Event có phiên bản | NFR | M0 | Một phần | `events.py:17,165,215` | `event_json_schema()` không dùng/test; không xuất JSON Schema; không test hợp đồng envelope |
| NFR-24 | Tương thích CLI | NFR | M0 | Đạt | `config.py:252`, `runs.py:481-484`; `tests/test_adapter.py` (12 test fixture) | Ma trận 1 phiên bản |
| NFR-25 | Ngôn ngữ | NFR | M0 | Đạt | `mcp_server/guide.py` (VI), `prompt.py` (EN) | |
| NFR-26 | Bàn phím, `?` | NFR | M2 | Một phần | `tui/app.py:75-92` | **Không có màn trợ giúp `?`** |
| NFR-27 | Nền tảng | NFR | M0 | Đạt | `pyproject.toml` (`>=3.12`, textual) | Linux chưa kiểm |
| NFR-28 | Lưu giữ 30 ngày | NFR | M3 | Thiếu | `recovery.py:182-198` chỉ purge agent xoá mềm | **Không purge event, stdout thô, transcript sau 30 ngày**; skill/team `purge_after` không có job |
| NFR-29 | Bất biến policy (test) | NFR | M1/M4 | Một phần | test Write/Edit/`sed -i` vào policy | Không test "mọi đường ghi"; có ≥2 đường lọt thật (FR-SG-05) |
| NFR-30 | Hoàn tác được | NFR | M4 | Đạt | `test_ledger_rollback_chain_conflict_and_append_only`, `test_soft_delete_and_restore` | |
| NFR-31 | Ít làm phiền | NFR | M1 | Một phần | báo nhầm 0/116 trên bộ test | Không đo p95 ≤ 50 ms; recall thực < 100% |
| NFR-32 | Ổn định khi tự sửa | NFR | M4 | Đạt | `test_rate_limit_defers_then_applies`, `test_l3_config_change_needs_eval`, `test_correction_spike_rolls_back_latest_change` | |

### 1.12 Tiêu chí hoàn thành từng mốc (BRD §13)

| Mốc | Tiêu chí DoD | Trạng thái | Bằng chứng | Ghi chú |
|---|---|---|---|---|
| M0 | `run researcher "…"` chạy thật | Chỉ bằng chứng tài liệu | README; các run thật ở dev-notes M1–M3 | Không có dev-note M0 riêng |
| M0 | Event vào DB; cost = `result.total_cost_usd` | Đạt | `tests/test_dispatcher.py:120-127`, `tests/test_claude_runtime.py::test_real_subprocess_stream_json_run` | |
| M0 | Test FakeRuntime trên fixture xanh | Đạt | suite 313/313 | |
| M0 | Không hook Serena | Đạt | `test_hub_real_isolated_run_fixture_clean` | |
| M0 | Báo cáo spike A-01..A-05 | Đạt (tài liệu) | `docs/spikes/M0-spike-report.md` | |
| M1 | Claude Code `hub_run` + `hub_events` | Đạt | `test_stdio_transport`, `test_run_events_flow_idempotency_feedback`; chạy thật M1-A | |
| M1 | Agent dùng MCP nội bộ, tool ghi có audit, không hỏi | Một phần | `test_tool_write_audit_and_event` (giả lập) | Chưa chạy thật (agent không gán MCP); FR-MC-02 allow/deny chưa sinh khi chưa probe |
| M1 | Phá huỷ bị giữ, 300 s → `waiting_human`, duyệt đúng 1 lần | Một phần | `test_approval_flow.py`, `test_mcp_hub_approve_resolves_approval`; chạy thật M1-B (timeout 20 s) | Cơ chế đạt, nhưng agent tự duyệt được (FR-SG-04) |
| M1 | Recall 100% bộ test phân loại | Đạt trên bộ test | `test_recall_100_percent` | Recall thực < 100% |
| M1 | Agent không ghi được `policy/` | Một phần | test policy; chạy thật M1-B (Write bị chặn) | Lọt qua trình thông dịch/symlink |
| M1 | Kill daemon → khôi phục ≤30 s | Đạt (mô phỏng) | `tests/test_recovery.py:53` | |
| M2 | 3 agent live; đóng TUI agent vẫn chạy; approve/steer/stop từ TUI | Đạt | `tests/test_tui.py` (Pilot), chạy thật M2 | |
| M2 | NFR-01..05, 08 | Một phần | bench | NFR-05 thiếu phần client MCP |
| M2 | Baseline KPI G-1..G-3 | Chỉ bằng chứng tài liệu | `docs/dev-notes/M2.md:75-79` | |
| M3 | Giao team qua MCP, lead tách ≥2 task, tổng hợp trả về | Đạt | `test_mcp_team_flow_lead_two_workers_result`; chạy thật M3 | Tổng hợp cơ học |
| M3 | Crash worker → retry resume | Đạt | `test_kill_worker_mid_run_retries_with_resume` | |
| M3 | Ngân sách team chặn đúng | Đạt (một phần) | `test_team_budget_blocks_claims` | Có thể vượt khi song song |
| M4 | Sửa 2 lần → review fork → skill patch → không lặp; chỉ thấy trong digest | Đạt | `test_correction_review_fork_patch_then_no_repeat`, `tests/test_digest.py`; chạy thật M4-A | Thông báo TUI khi áp dụng chưa có |
| M4 | Đổi model/effort qua eval rồi tự áp dụng; tụt eval → `eval_failed` | Đạt | `tests/test_eval_gate.py`; chạy thật M4-B | |
| M4 | Correction tăng → tự rollback | Đạt | `test_correction_spike_rolls_back_latest_change` | Rollback cấu hình có thể ghi đè thay đổi của người |
| M4 | Chạm chính sách → `guard.blocked`; không hàng chờ duyệt | Đạt | `test_policy_touch_and_self_escalation_and_bounds`, `test_learning_rpc_status_has_no_approval_queue` | |
| M4 | Review không chạm session gốc | Một phần | test kiểm argv; sha256 transcript chỉ ở tài liệu M4-A | |
| M5 | Team: eval → canary → promoted không hỏi người; hồi quy canary → rollback trước khi lan | Đạt | `tests/test_canary.py`; chạy thật M5-A | |
| M5 | Skill trùng 2 agent tự gom lên team | Đạt | `tests/test_curation.py`; chạy thật M5-B | |
| M5 | Overhead học ≤ 15% | **Chưa đạt** | `docs/dev-notes/M5-B.md:65` (chi phí 68,6%) | Mẫu nhỏ; cần đo tải thật |
| M5 | KPI G-4 sau 4 tuần | Chưa đến hạn | `report kpi` có | |

---

## 2. Tổng hợp

### 2.1 Tổng thể

| Nhóm | Tổng | Đạt | Một phần | Thiếu | Lệch có chủ ý |
|---|---|---|---|---|---|
| FR | 105 | 65 (62%) | 35 | 0 | 5 |
| NFR | 32 | 16 (50%) | 15 | 1 | 0 |
| **Cộng** | **137** | **81 (59%)** | **50 (36%)** | **1** | **5** |

Nếu tính "Lệch có chủ ý" là chấp nhận được: 86/137 = **63%**. Không có FR nào "Thiếu" hoàn toàn — mọi FR đều có code, phần lớn "Một phần" là thiếu tiêu chí phụ, thiếu test cho tiêu chí, hoặc lỗ hổng an toàn.

### 2.2 Theo ưu tiên (FR)

| Ưu tiên | Tổng | Đạt | Một phần | Lệch có chủ ý | Tỉ lệ đạt (+lệch) |
|---|---|---|---|---|---|
| Must | 83 | 50 | 29 | 4 | 60% (65%) |
| Should | 17 | 13 | 4 | 0 | 76% |
| Could | 5 | 2 | 2 | 1 | 40% (60%) |

### 2.3 Theo mốc (FR + NFR, xếp theo mốc đầu tiên ghi trong BRD)

| Mốc | FR (Đạt / MP / Lệch) | NFR (Đạt / MP / Thiếu) | Tổng đạt | DoD |
|---|---|---|---|---|
| M0 | 25 (18 / 7 / 0) | 10 (5 / 5 / 0) | 23/35 | Đạt (chạy thật chỉ có bằng chứng tài liệu) |
| M1 | 27 (14 / 12 / 1) | 7 (3 / 4 / 0) | 17/34 | **Một phần** — lỗ tự duyệt và ghi `policy/` |
| M2 | 9 (6 / 2 / 1) | 7 (5 / 2 / 0) | 11/16 | Đạt (trừ NFR-05 phần MCP) |
| M3 | 22 (17 / 4 / 1) | 3 (1 / 1 / 1) | 18/25 | Đạt |
| M4 | 17 (8 / 9 / 0) | 4 (2 / 2 / 0) | 10/21 | Đạt theo kịch bản DoD, nhưng rollback cấu hình sai |
| M5 | 5 (2 / 1 / 2) | 1 (0 / 1 / 0) | 2/6 | **Một phần** — overhead học 68,6% > 15% |

---

## 3. Lỗ hổng Must chưa đạt (xếp theo mức nghiêm trọng)

### Nghiêm trọng — an toàn (chặn nghiệm thu M1)

| # | Mã | Lỗ hổng | Đề xuất sửa cụ thể |
|---|---|---|---|
| H-01 | FR-SG-04, NFR-12 | Agent **tự duyệt** thao tác phá huỷ: `python3 -m agent_hub.cli approve <id>`, `echo approve <id> \| xargs agent-hub`, client socket Python, `from agent_hub.rpc import call` — classifier trả `write`; RPC `approval.approve` (`guard/rpc.py:84`) không xác thực người gọi | (1) Bắt buộc **token điều khiển** cho `approval.*`, `guard.protect.*`, `config.set`, `learn.propose|rollback`, `agent.update`: file 0600 ngoài vùng agent đọc, không đưa vào env run; CLI/TUI người đọc token, tiến trình trong run không có. (2) Phòng thủ thêm: daemon kiểm `getpeereid`/`LOCAL_PEERPID` + cây tiến trình, từ chối nếu là hậu duệ tiến trình `claude` của một run. (3) Classifier: `python -m agent_hub*`, `xargs|find -exec … agent-hub`, mọi chuỗi chứa đường dẫn `hub.sock` → `forbidden`. Thêm ca vào `tests/test_guard_classifier.py` |
| H-02 | FR-SG-05, NFR-29 | Ghi `HUB_HOME/policy/` (kể cả `policy/golden/`) qua trình thông dịch có đường dẫn literal hoặc symlink tạo cùng lệnh → `write` | `scan_code` trong `guard/classifier.py`: mọi literal chứa đường dẫn tài nguyên bảo vệ → destructive; `ln -s` có đích trong vùng bảo vệ → destructive; giải symlink cho đích ghi. Thêm test NFR-29 duyệt **mọi** đường ghi (python/node/perl/ruby, cp/mv/tee/ln/dd/install, symlink) |
| H-03 | FR-SG-01, FR-SG-07 | Recall thực < 100%: `pathlib.Path(...).unlink()`, `subprocess.run(['rm','-rf',…])`, `open(f,'w')`, `write_text`, `writeFileSync`, `cp -r`/`rsync` đè file có sẵn → `write` | Mở rộng `PY_DESTRUCTIVE`/bộ quét mã; không giải được đích → `uncertain`. `mv_cp` với nguồn thư mục: duyệt file đích đã tồn tại. Nâng assert kích thước bộ test lên ≥ 200 và thêm các ca lách. Thêm CI (`.github/workflows`) chạy `pytest` + bộ phân loại |
| H-04 | FR-LN-06, FR-LN-17, FR-LN-10 | **Tự rollback cấu hình ghi đè thay đổi của người**: `Ledger._rollback_one` bỏ kiểm xung đột với `kind=config` và khôi phục toàn bộ `CONFIG_FIELDS`+SOUL từ `before_blob`; `later_entries` so theo `target_ref` (QA tái hiện: rollback entry `effort` của review làm `model=haiku` của người về `sonnet`) | Chỉ khôi phục các khoá trong `target_ref` của entry (SOUL chỉ khi entry có đổi SOUL); với actor tự động, nếu giá trị hiện tại ≠ `after_blob` → Conflict, không ghi, phát `regression.detected{human_change}`. Thay `FakeLedger` bằng ledger thật trong `tests/test_regress_monitor.py`/`test_eval_gate.py` cho ca người sửa xen giữa |
| H-05 | FR-LN-15, FR-LN-05, FR-LN-19 | Agent mạo danh người qua `agent-hub learn propose|rollback|retry|refine` (classifier `hub_cli` → `write`; RPC gán `created_by="user"`): vượt `auto_apply=false`, pin, guard học, né tự rollback | `hub_cli`: các lệnh này và `agent update/edit` → `forbidden` khi chạy trong run. CLI/RPC đọc `AGENT_HUB_RUN_ID` (hoặc thiếu token người, H-01) → `created_by="agent"` và đi qua guard học đầy đủ |

### Cao — chức năng Must

| # | Mã | Lỗ hổng | Đề xuất sửa cụ thể |
|---|---|---|---|
| H-06 | FR-AG-04 | `agent rm` không kiểm thành viên team, không `--detach-teams` → team mồ côi lead, không gán lại được | `AgentService.remove` (`agents.py:289`): truy vấn `team_members`, có thì `Blocked` kèm danh sách team; tham số `detach_teams` xoá dòng thành viên; nối RPC `agent.delete`, CLI `agent rm --detach-teams`, `hub_agent_archive`. Phát thêm `agent.archived`. Test trong `tests/test_agents.py` |
| H-07 | FR-AG-03 | Không có `agent revert --to N` (tiêu chí chấp nhận) | `AgentService.revert(agent_id, to_rev)` lấy snapshot từ `store.list_revisions` → `update(...)` tạo revision mới; RPC `agent.revert` (thêm vào `MUTATING`), CLI `agent revert <id> --to N`. Test "rev 4 → revert 3 → rev 5 = rev 3"; thêm test "run đang chạy giữ revision cũ" |
| H-08 | FR-UI-08, FR-UI-05 | 18 lệnh CLI thiếu/đổi tên; `run list|show|logs` bị parse thành `run <agent> <prompt>` — nếu tồn tại agent tên `list`/`logs` sẽ **khởi chạy run thật** | Thêm `("run","list"|"show"|"logs"|"stop") → runs …` vào `HIDDEN` (`dispatch/cli.py`) và cấm id agent trùng từ khoá; thêm alias: `stop <run|agent>`, `requests`, `approve --deny`, `agent archive|revert`, `learn pin|unpin|enable|disable`, `eval golden …`, `eval results`, `run --detach|--continue`; làm `daemon logs`, `trash list|restore|purge`. Phần cố ý giữ khác thì ghi vào dev-notes và BRD |
| H-09 | NFR-28 | Không có retention 30 ngày cho event, stdout thô, transcript → DB và `runs/` phình vô hạn | `retention.events_days/raw_days = 30`; tick trong daemon/`recovery.py`: `DELETE FROM events WHERE ts < now-30d` (giữ cost tổng hợp, ledger, audit, guard_decisions), xoá `runs/*.jsonl.gz` quá hạn, job purge skill/team có `purge_after`. Test giả thời gian |
| H-10 | FR-RT-02, FR-RT-15 | Không có ma trận tương thích theo cờ (không từ chối spawn khi cờ không hỗ trợ); không kiểm phiên bản lúc daemon khởi động; không cảnh báo đỏ `raw.unknown` > 5%/giờ | `runtime.claude.flag_matrix` theo phiên bản; `build_argv`/`start` ném `ArgvError` với cờ lạ; gọi `_check_cli_version` trong `Hub.start()` và lưu vào `hub_state` cho header TUI; đếm tỉ lệ `raw.unknown` trong `RunManager` theo cửa sổ 1 giờ → `cli.unknown_rate_alert` + gợi ý ESTOP. Test cho cả ba |
| H-11 | FR-RT-17 | Không có hook quan sát (`PostToolUse/UserPromptSubmit/Stop`) fail-open, không phát `hook.timeout`, không có `agent-hub hook <event>` | Thêm hook quan sát trong `guard/runtime.build_settings` gọi module hook có timeout 2 s, luôn `exit 0`; hết giờ thì ghi `hook.timeout`. Hoặc nếu A-11 đã thay thế nhu cầu này: cập nhật BRD FR-RT-17 cho khớp (xem §5) |
| H-12 | NFR-10, FR-MC-05 | Secret có thể lọt: stdout thô lưu nguyên văn; `redact` theo regex, không theo giá trị SecretStore; `mcp.json` có secret còn lại khi daemon chết; stderr probe vào `logs/daemon.out`; giá trị env-fallback không vào bộ che | Bộ che theo giá trị (nạp SecretStore + giá trị env-fallback đã resolve) dùng chung cho RawLog, events, log, OTel; recovery xoá mọi `agents/*/runtime/*` khi khởi động; probe dùng `errlog` riêng đã che. Test e2e: run có MCP mang secret → grep hub.db, WAL, `runs/*.gz`, `logs/`, `argv_redacted`, kiểm thư mục runtime đã xoá |

### Trung bình

| # | Mã | Lỗ hổng | Đề xuất |
|---|---|---|---|
| H-13 | FR-MC-02, FR-MC-03 | Server chưa probe có `include` → không có allow/deny trong argv; không test "untrusted → approval" | `mcp add/import` ép probe hoặc sinh `--allowedTools mcp__<s>__<tool>` cho mẫu không glob; thêm test classify `trust: untrusted` + test approval_flow với server untrusted và e2e argv |
| H-14 | FR-MS-02, FR-MS-05, FR-MS-08 | Thiếu `hub_policy_get`; worker thiếu `memory_search`; `hub_events` thiếu `agent_id`, `hub_cancel` thiếu `task_id`, `hub_estop` thiếu `team`; không test `open_requests` trong `hub_events` | Thêm tool readOnly `hub_policy_get`; `memory_search` vào `ROLE_TOOLS["worker"]`; bổ sung tham số; assert `open_requests` (tool + input) trong `test_mcp_hub_approve_resolves_approval` |
| H-15 | FR-RT-10 | Charter team nằm trong tin user, không ở system prompt; `--system-prompt-snapshot` không bật | Đưa charter vào `learning/runtime.system_prompt_for` theo thứ tự (3), hoặc ghi lệch vào M3.md + BRD |
| H-16 | FR-UI-06, FR-UI-01, FR-LN-13 | Thiếu phím `L` (tắt học), mục Ledger theo agent, phím `f` trên `top`, thông báo TUI "X learned" | `L` → `learn.policy set agents.<id>.enabled=false` (xác nhận, audit `tui`); mục Ledger dùng `learn.ledger {agent}`; binding `f`; `TopApp` nghe `learning.applied` → `notify(summary)` |
| H-17 | FR-LN-10 | L1 không golden không gắn `unevaluated` | `EvalGate.run_batch_l1` cập nhật `ledger_entries.flags` và `regression_watch.unevaluated` theo verdict |
| H-18 | NFR-15 | `extra_args` lọt các biến thể bypass trên `dir:`; `hub_agent_update` qua MCP hub đăng ký dưới tên khác là `write` | Chặn token khớp `^--(allow-)?dangerously-skip-permissions` và `--permission-mode[= ]bypassPermissions` khi workspace là `dir`; nhận diện MCP hub theo lệnh/URL; thêm `hub_agent_update`, `hub_estop` vào `MCP_FORBIDDEN` khi gọi từ agent |
| H-19 | NFR-17, FR-OB-03 | Ngân sách ngày không trừ run đang chạy → song song vượt trần | Trừ tổng `max_budget_usd` của run active trong `_budget_for`; test nhiều run song song |
| H-20 | NFR-21, NFR-23, NFR-22 | Log không có `run_id/agent_id`; không JSON Schema/test hợp đồng event; judge/curator gọi `claude` trực tiếp | `LoggerAdapter` trong `runs.py`/dispatcher/recovery; xuất `docs/schema/event.v1.json` + test snapshot; đưa judge/curator qua `AgentRuntime` |
| H-21 | FR-RT-12 (Should) | Race khoá repo (xem §6) | Giữ khoá **đồng bộ** lúc submit: `RunManager.tick` gọi `locks.try_acquire` trước `create_task` (hoặc dispatcher đặt chỗ khoá trong `_start`); nhả khi run kết thúc sớm. Sửa test để assert task thứ hai **không có run nào** trong lúc chờ |

### Thấp

FR-AG-02 (lọc team/status), FR-AG-08 (doctor không thử kết nối MCP, không test), FR-TK-01 (`--detach`), FR-TK-04 (assert thiếu ngoặc), FR-TK-10 (`hub_estop` team), FR-RT-07 (test tự động tiến trình con), FR-RT-19 (Slack khi `workspace_changed`), FR-RT-09 (`acceptEdits`, lớp tĩnh 5 mẫu), NFR-26 (màn `?`), NFR-31 (đo p95 phân loại), NFR-07 (CAS cho `set_status`), NFR-05 (test ngắt client MCP), NFR-16 (câu "tool output là dữ liệu" trong system prompt).

---

## 4. Rủi ro an toàn — kiểm riêng các bất biến

| # | Bất biến | Kết luận | Code | Test có không / có kiểm đúng không |
|---|---|---|---|---|
| 1 | Thao tác phá huỷ luôn cần người | **VI PHẠM (một phần)** | `approvals.py` (`hold`, `to_waiting_human`, `consume_ticket`), `guard/service.py:125`, `guard/classifier.py` (`self_approval`) | **Có, đúng phần nó kiểm**: `test_timeout_waiting_human_releases_slot_then_resume_once` assert `outcome is None` và thư mục còn nguyên sau timeout (không tự allow/deny); `test_destructive_held_then_approved_runs_once` assert lệnh chạy đúng 1 lần và approval mới có id khác (vé một lần). Test tự duyệt chỉ phủ 4 dạng lệnh (`agent-hub approve`, `uv run …`, …). **Không có test** cho `python -m agent_hub.cli approve`, `xargs agent-hub`, client socket — cả ba lọt (QA xác minh độc lập). Đường phụ: `learn propose` mạo danh người (H-05). Lỗ gốc: RPC daemon không xác thực người gọi |
| 2 | Hook fail-closed | **Đạt** | `guard/hook.py` (deny mọi lỗi, `signal.alarm`, `|| exit 2`), `guard/mcp_guard.py`, `guard/runtime.py` (`--disallowedTools` tĩnh) | **Có và kiểm thật**: `test_hook_daemon_unreachable_denies`, `test_hook_daemon_silent_times_out_denies`, `test_hook_command_wraps_exit2` assert `permissionDecision=deny`/returncode 2; `test_static_deny_beats_hook_allow_restart_with_rule_lifted` kiểm lớp tĩnh thắng hook. Điểm yếu: lớp tĩnh chỉ 5 mẫu tiền tố (không bắt `git push origin main --force`); `guard.enabled=false` tắt cả hai lớp (chỉ người đổi được vì `config.yaml` được bảo vệ) |
| 3 | Agent không ghi được `policy/`, `evals/golden` | **VI PHẠM (một phần)** | `guard/runtime.py` `policy_deny_rules`, `guard/classifier.py` `policy_write`/`protected_*`, `evals/golden.py` `_require_human`, `learning/guard.py:28` | Write/Edit/MultiEdit/NotebookEdit: chặn 2 lớp, có test và chạy thật M1-B. Golden: `test_golden_rejects_non_human` (kiểm thật actor agent/learning/review/curator/eval_gate/regression_monitor) và `test_guard_forbids_agent_golden_writes` (CLI `golden …`, MCP `hub_golden_*` → forbidden) — **kiểm đúng**. **Lọt**: Bash qua trình thông dịch có đường dẫn literal và symlink cùng lệnh (H-02). Test NFR-29 chỉ có Write/Edit/`sed -i` → không phủ "mọi đường ghi" như BRD yêu cầu |
| 4 | Tự rollback không chạm thay đổi của người | **VI PHẠM** | `evals/gate.py`, `learning_ops/regression.py`, `learning_ops/ports.py:34` (`is_auto_change`), `learning/ledger.py:~325-352`, `learning/canary.py` | Có test lọc actor: `test_post_apply_never_rolls_back_human_change`, `test_human_change_is_never_auto_rolled_back`, `test_human_change_skipped_but_learning_change_rolled_back` — **kiểm đúng việc chọn entry, nhưng dùng `FakeLedger`** (khôi phục đúng khoá) nên **che** lỗi của `Ledger.rollback` thật: rollback cấu hình khôi phục toàn bộ cấu hình, ghi đè thay đổi của người (QA tái hiện bằng ledger thật). Canary cố ý rollback cả thay đổi tầng team do người tạo (M5-A) — không có test, trái bất biến; cần chủ sản phẩm quyết |
| 5 | Secret không lộ ra argv/log/DB/OTel | **Một phần** | `secrets.py` (0600), `runs.py:363-412,544` (mcp.json 0600, xoá sau run), `redact.py`, `audit.py` (`mask_values`), `telemetry/otel.py` (allowlist) | Thiết kế đúng ở đường chính: secret chỉ trong `mcp.json`, không trong argv; QA chạy e2e FakeRuntime grep toàn `HUB_HOME` (trừ `secrets/`) không thấy giá trị. Test: `test_import_all_in_process` grep hub.db/WAL/registry/logs — **thật nhưng chỉ sau import, không có run**; `test_otel.py::test_include_payloads_never_for_mcp` chỉ kiểm khoá có mặt, **không assert secret bị che** — QA tái hiện `sk-live-…` lọt span khi `include_payloads=true`. Lỗ khác: stdout thô nguyên văn, `mcp.json` sót khi daemon chết, stderr probe, env-fallback (H-12) |
| 6 | HTTP MCP chỉ bind localhost | **Đạt** | `mcp_server/http.py:19,64-68` (chỉ `127.0.0.1`/`::1`/`localhost`, ValueError cho host khác) | `test_http_transport_tokens` assert `build_http_app(host="0.0.0.0")` raise — **kiểm thật** nhưng chỉ một giá trị và không qua đường CLI. QA chạy `mcp serve --http --host` với `0.0.0.0`, `::`, rỗng, `192.168.1.5`: đều bị từ chối. Không có khoá config `mcp_server.host`. Lỗi nhỏ: CLI in "tại http://0.0.0.0…" trước khi từ chối; so token không hằng thời gian |

Ghi chú thêm: với `bypassPermissions` (NFR-15), `tool_policy` trên `dir:` bị chặn và có test, vòng học không đặt được `permission_mode`/`allow_bypass`; lỗ còn lại nằm ở `extra_args` do người cấu hình (H-18).

---

## 5. Lệch BRD đã ghi trong dev-notes — có cần cập nhật BRD?

| Nguồn | Lệch | Đánh giá | Cập nhật BRD? |
|---|---|---|---|
| M1-A | Tên tool: `hub_run_status`, `hub_open_requests`, tách `hub_deny`, `hub_run(prompt)`, `hub_events(wait_s)` | Hợp lý, client đã dùng | **Có** — sửa bảng FR-MS-02 |
| M1-A | Registry: DB là nguồn sự thật, yaml bản sao; `trusted|untrusted` (`full` alias); thêm `tools.require_approval` | Hợp lý | **Có** — FR-MC-01 (BRD còn ghi `untrusted|full`, mâu thuẫn FR-MC-03) |
| M1-A | Audit chỉ params đã che + tóm tắt, không before/after đủ | Chấp nhận được cho người dùng đơn | Có — nới FR-OB-08 thành "tóm tắt", hoặc giữ BRD và đưa vào backlog |
| M1-A | Secret env-fallback mặc định bật | **Rủi ro** (giá trị env không vào bộ che) | Có — ghi vào FR-MC-05 kèm yêu cầu che theo giá trị; cân nhắc mặc định tắt |
| M1-B | FR-SG-03 bước 2 (LLM phụ) chưa làm; `uncertain` luôn giữ | An toàn hơn, đổi lại báo nhầm cao hơn | Có — hạ bước 2 thành Should/M-sau, ghi rõ "uncertain → giữ" là hành vi chuẩn |
| M1-B | "File run tự tạo" dùng birthtime thay checkpoint (classifier vẫn dùng birthtime sau M3) | Checkpoint đã có từ M3 → nên chuyển code về checkpoint | **Không** — sửa code |
| M1-B | `protected_resources` ở `policy/protected.yaml` thay bảng DB | Đúng tinh thần FR-SG-05 | Có — §8.1 data model |
| M1-B | Hook trả allow cho safe/write; `default_permission_mode=None` (không `acceptEdits`) | Nhất quán mô hình "ghi chạy ngay, có audit" | Có — FR-RT-09/NFR-15 bỏ yêu cầu `acceptEdits` hoặc đặt lại mặc định trong code |
| M1-B | Agent cùng user OS tự viết client socket ("cần token M2+") | **Không phải lệch chấp nhận được** — vi phạm bất biến cốt lõi | **Không** — phải sửa code (H-01) |
| M2 | `steer.sent` → `user.message` + `steer.ack` | Hợp lý | Có — FR-RT-06 và danh mục event §9 |
| M2 | `N` báo nhầm lưu bằng note; takeover không gắn hook guard; `ctx%` ước lượng; cây subagent chỉ `⤷` | Takeover không guard là có chủ ý (người tự duyệt) | Có — ghi takeover không qua guard vào FR-UI-10; phần còn lại là backlog |
| M3 | `crashed` vs `failed`, spawn lỗi là `failed` | Hợp lý | Có — danh sách outcome FR-TK-05 |
| M3 | Tổng hợp task gốc là ghép cơ học (không lead tổng hợp lần 2) | Đạt DoD nhưng yếu | Không — giữ BRD, đưa vào backlog |
| M3 | `task_complete` không bắt buộc mặc định (`tasks.require_completion_call`) | Hợp lý với model nhỏ | Có — FR-MS-08 ghi là setting |
| M3 | `team purge` chưa làm | Thiếu chức năng | Không — backlog |
| M3 | Liveness bằng returncode/`kill -0`; worktree chưa kiểm; bỏ qua `.gitignore` | Hợp lý | Có — chú thích FR-TK-06/FR-RT-12 |
| M4-A | Review fork đổi `tools[]` (cache vẫn ≈84%) | Hợp lý, đã đo | Có — FR-LN-03/NFR-18 bỏ "cùng tools" |
| M4-A | Memory `target_ref` = tên file; cooldown không áp cho memory | Nhỏ | Có — FR-LN-18 |
| M4-B | Golden ở DB + bản sao YAML `policy/golden/` | Hợp lý | Có — §8 |
| M4-B | Khoá cấu hình `eval.regression.*`, `digest.schedule` (BRD `learning.*`) | Chỉ đặt tên | Có — đồng bộ tên khoá trong FR-LN-13/17 |
| M5-A | **Canary rollback cả thay đổi tầng team của người** | Mâu thuẫn bất biến "tự rollback không chạm thay đổi của người" | **Có, cần chủ sản phẩm quyết**: hoặc BRD ghi ngoại lệ "rollout canary áp cho mọi thay đổi tầng team/global", hoặc code bỏ qua entry `actor=user` |
| M5-A | Cửa sổ canary có thể đóng trước feedback | Có regression monitor bù | Có — chú thích FR-LN-16 |
| M5-B | Từ vựng, chưa embedding; pha 2 qua hub-self thay `--json-schema`; run curator dùng system prompt agent | Hợp lý | Có — FR-LN-09/12 |
| M5-B | NFR-19 tính học/làm-việc thay học/tổng | Đổi định nghĩa chỉ tiêu, và số đo vẫn trượt (68,6%) | **Có** — chốt định nghĩa; đồng thời M5 chưa đạt chỉ tiêu này |

**Lệch chưa được ghi ở dev-notes** (phát hiện khi kiểm, cần ghi hoặc sửa): charter team trong tin user (FR-RT-10); `--system-prompt-snapshot` không bật; ring buffer theo agent (FR-OB-01); 18 lệnh CLI đổi tên/thiếu (FR-UI-08); tool MCP tầng học đổi tên `hub_learn_*`/`hub_knowledge_*` (FR-MS-02); `config.yaml` thay `agent.yaml`; template `ops` thay `lead`; `run continue` thay `run --continue`; `hub_golden_rm` không đánh destructive; hook quan sát/`hook.timeout` không làm (FR-RT-17).

---

## 6. Ghi chú về độ ổn định test: `test_workspace.py::test_two_tasks_same_repo_serialized_and_readonly_not_locked`

- Test này dùng `settle` cục bộ của `tests/test_workspace.py:35` với **timeout 15 s**, không phải 5 s (5 s là `settle` trong `tests/m4b_fake.py:24`, không dùng ở đây). Vậy giả thuyết "flaky do timeout 5 s trong `settle`" **không đúng**. Chạy lại riêng 8 lần: 8/8 xanh (≈0,3 s mỗi lần).
- Nguyên nhân gốc là **race thật trong code**, QA tái hiện bằng script (`scratchpad/audit/scripts/lock_race.py`): khoá repo chỉ được *kiểm* lúc submit (`dispatcher._lock_block`, `RunManager.tick` → `locks.blocked_for`) nhưng chỉ được *giữ* bất đồng bộ trong `RunManager._run` (`runs.py:492-494`, sau `create_task`). Trong cùng một tick, task thứ hai cùng repo qua được cả hai lần kiểm, được spawn, rồi `_run` thất bại `HubError("workspace_locked")` → run `failed`, `attempts=1`, `consecutive_failures=1`, sau đó task mới về `ready` + `wait_reason=workspace_locked`.
- Test vẫn xanh vì chỉ chờ trạng thái cuối. Khi máy tải nặng, thứ tự tick/terminal-callback/backoff thay đổi nên có thể hết 15 s trước khi điều kiện xuất hiện, hoặc task thứ hai đi vào nhánh retry/backoff khác. Hệ quả thực tế ngoài test: mỗi lần race tốn một lần thất bại của task; với `failure_limit=2`, hai lần race có thể làm **circuit breaker chặn task oan**.
- Đề xuất: giữ khoá đồng bộ trước `create_task` (H-21) và sửa test để assert task thứ hai không có run nào trong lúc chờ (`SELECT count(*) FROM runs WHERE task_id=?` = 0).

---

*Phụ lục bằng chứng (ngoài repo):* script và kết quả QA ở `/private/tmp/claude-501/-Users-phuonglq-Workspace-agent-hub/e77b5236-4485-4813-8b88-ed8c360a2291/scratchpad/audit/` — `scripts/verify_cls.py` (xác minh lỗ classifier), `repro_cfg.py`/`repro_cfg2.py` (rollback cấu hình), `repro_l1.py`, `otel_leak.py`, `scripts/lock_race.py`, `scripts/secret_e2e.py`, `bench.json`, `pytest.txt`, `sg_results.txt`/`sg2_results.txt` (kết quả thử lách `guard test`).
