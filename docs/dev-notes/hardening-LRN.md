# Hardening — luồng LRN (học & điều phối)

Nguồn: `docs/plan/hardening.md`, `docs/acceptance/traceability.md` §3 (H-04, H-09, H-15, H-16, H-17, H-19, H-21), §4 bất biến 4,
§6 (race khoá repo). Mỗi mục: test tái hiện đúng kịch bản QA viết trước (đỏ) rồi sửa (xanh). Test chung: `tests/test_hardening_lrn.py`.
Migration của luồng: **v12** (SEC đã có v11). Không gọi `claude` thật, không tốn tiền.

## Quyết định coordinator áp dụng
Canary/eval gate/regression monitor **không bao giờ rollback entry `actor=user`**. Thay đổi tầng team/global/USER do người tạo
áp dụng NGAY cho cả team (không eval trước, không ghim bản cũ), chỉ theo dõi + cảnh báo (digest + Slack).
→ BRD FR-LN-16 cần cập nhật (lệch M5-A "canary rollback cả thay đổi của người" đã bị bỏ).

## H-04 — rollback cấu hình ghi đè thay đổi của người (Nghiêm trọng)
- `learning/ledger.py`:
  - Rollback cấu hình chỉ khôi phục **khoá entry đã đổi** (`target_ref` ∪ khoá khác nhau giữa blob before/after); SOUL chỉ
    khi entry đổi SOUL (`config_keys`). Trước đây khôi phục toàn bộ `CONFIG_FIELDS` + SOUL và bỏ qua kiểm xung đột.
  - "Cùng đích" của cấu hình = trùng ≥ 1 khoá (`later_entries`), trước đây so chuỗi `target_ref` (entry `effort` và `effort,model`
    bị coi khác đích).
  - Kiểm xung đột cho cấu hình: giá trị hiện tại của khoá ≠ `after_blob` → `Conflict` (người dùng `--force` được).
  - Bên gọi tự động `AUTO_ROLLBACK_CALLERS = {regression_monitor, eval_gate, canary}`: gặp đích bị sửa ngoài ledger HOẶC entry
    (kể cả trong chuỗi) có actor ngoài `AUTO_CHANGE_ACTORS = {learning, review, agent, curator}` → **không ghi gì**, phát
    `regression.detected{human_change, source, drifted, hint}` (Slack/digest đã có sẵn định dạng), ném `Conflict(data.human_change)`.
    `force` không có tác dụng với bên gọi tự động. Đây là lớp phòng thủ ở HÀM ÁP DỤNG DUY NHẤT — mọi bên gọi đều được che.
- `learning_ops/ports.ledger_rollback` trả `{ok: False, human_change: True}`; `regression.py` đặt watch `warned` (cảnh báo một
  lần, không thử lại mỗi vòng); `evals/gate.py` gắn cờ `human_change`.
- Canary (`learning/canary.py`, `pipeline._continue_tier`): proposal `created_by=user` → áp dụng ngay, bản ghi canary mode
  **`watch`** (không ghim — `tiers.py` chỉ ghim `mode='canary'`), theo dõi mọi agent bị ảnh hưởng; xấu hơn → `_warn_human`
  (`regression.detected{human_change, source=canary}`, canary `warned`), không rollback. Đích đang canary của thay đổi tự động
  mà người ghi đè → canary cũ kết thúc `superseded` (bỏ ghim). Canary tự động gặp `Conflict(human_change)` → `warned`.
- Test: `test_h04_repro_cfg_*` (đúng `repro_cfg.py`/`repro_cfg2.py`), `test_h04_soul_*`, `test_h04_auto_rollback_conflicts_*`,
  `test_h04_auto_never_rolls_back_user_entry_even_in_chain`, regression monitor + eval gate với **Ledger thật** (người sửa xen
  giữa: khoá khác → chỉ khôi phục khoá của entry; cùng khoá → không ghi, cảnh báo một lần), `test_canary_user_*`,
  `test_canary_ledger_refuses_user_entry_directly`. Sửa `test_canary.py::test_human_team_change_*` và `test_tier_cli.py`
  theo quyết định mới (test tier CLI chấp nhận cả hai nhánh vì `learn propose` mang danh người còn phụ thuộc H-05 của SEC).
- Còn lại: test cũ dùng `FakeLedger` (`m4b_fake.py`) vẫn giữ cho các ca chọn entry; ca người sửa xen giữa đã có bản Ledger thật.
  Rollback của NGƯỜI trên cấu hình giờ cũng báo `Conflict` khi khoá bị sửa ngoài ledger (trước đây ghi đè im lặng) — TUI hiện lỗi,
  CLI gợi ý `--force`.

## H-21 — race khoá repo (§6)
- `runs.py`: `RunManager.tick` **giữ khoá đồng bộ** (`locks.try_acquire`) trước `create_task`; `ActiveRun.lock_key` đặt sẵn,
  `_run` kế thừa (cùng session). Task bị huỷ trước khi coroutine chạy → `add_done_callback(_release_unstarted)` nhả khoá.
  Run kết thúc sớm (lỗi trước spawn) → `finally` của `_run` nhả như cũ.
- `RunManager.lock_pending(key, session)` + `dispatcher._lock_block`: run ghi cùng repo còn đang XẾP HÀNG cũng tính là khoá →
  task thứ hai không được submit. `_lock_block` bỏ qua agent chỉ đọc (giống `_lock_key`) — trước đây chỉ "lọt" nhờ chính race.
- `tests/test_workspace.py`: `settle` trả về **5.0 s** (bỏ che 15 s); test hai task cùng repo assert task thứ hai **0 run**,
  `consecutive_failures=0`, `attempts=0` trong lúc chờ. Test mới: `test_h21_lock_race_second_task_never_spawned` (đúng
  `scripts/lock_race.py`), `test_h21_lock_reserved_synchronously_in_tick_and_released_on_early_failure`.

## H-19 — ngân sách ngày không trừ run đang chạy
- `RunManager.reserved_budget(scope, owner)` = Σ(`max_budget_usd` − cost đã ghi) của run `queued|running` (hub/team/agent);
  `_budget_for` trừ phần này. Còn hạn mức nhưng toàn bộ đang được giữ chỗ → `Blocked` (`reason=budget_reserved`, không phát
  `budget.exceeded`); dispatcher để task chờ `blocked:…` và thử lại tick sau.
- Test: `test_h19_parallel_runs_cannot_exceed_daily_budget` (hub $1, mặc định $0.4/run → 0.4, 0.4, 0.2, run thứ 4 bị chặn;
  agent $0.5 → 0.4, 0.1).
- Còn lại: run `waiting_human` không giữ chỗ (cost tới lúc dừng đã ghi; run resume giữ chỗ mới).

## H-17 — L1 không golden không gắn `unevaluated`
- `EvalGate.run_batch_l1`: verdict `deferred_no_eval` hoặc cờ `unevaluated` → `_mark_unevaluated`: thêm `unevaluated` vào
  `ledger_entries.flags` (trường được phép cập nhật) và `regression_watch.unevaluated=1` (watch có trước hay sau đều đúng).
- Test: `test_h17_l1_without_golden_flagged_unevaluated` (đúng `repro_l1.py`), `test_h17_flag_set_even_if_watch_exists`.

## H-15 — charter team trong system prompt (FR-RT-10)
- `prompt.build_system_prompt(..., charter=)` + `learning/runtime.team_charter`: khối (3) `# Team charter (team '<name>', id <id>)`
  giữa SOUL (2) và memory (4) cho run có `team_id`; ổn định theo (agent, team) → hash giống giữa các run cùng team (giữ cache).
  Resume/fork vẫn dùng lại prompt đã đóng băng của session.
- `tasks.py`: bỏ charter khỏi tin nhắn user (worker/lead); worker chỉ còn câu vai trò + "Follow the team charter in your system prompt".
- `--system-prompt-snapshot`: `build_argv` (luồng CLI) ĐÃ hỗ trợ khoá `runtime.claude.system_prompt_snapshot` từ M0 → không cần
  cờ mới, không sửa `runtime/`. Chỉ đổi mặc định trong `config.py` thành `"on"` (CLI 2.1.302 `--help`: on là mặc định của CLI;
  truyền tường minh để không phụ thuộc mặc định đổi). Không có yêu cầu nào gửi luồng CLI.
- Test: `test_h15_charter_in_system_prompt_ordered_and_stable`, `test_h15_system_prompt_snapshot_on_by_default`.
- Còn lại: run adhoc của agent thuộc team (không có `team_id`) không có charter — charter theo run, không theo thành viên.

## H-16 — TUI (FR-UI-06/01, FR-LN-13)
- `tui/learnings.py`: `L` bật/tắt học cho agent của dòng đang chọn (ConfirmScreen → `learn.policy_set agents.<id>.enabled`,
  `_audit.channel=tui`); `b` → `LedgerScreen(agent)` (`learn.ledger {agent}`, diff `learn.show`, `r` rollback có xác nhận).
- `tui/app.py` (thêm, không đổi hành vi cũ): `f` trên `top` → PromptScreen `good|wrong|partial [ghi chú]` → `feedback.record`
  (source `tui`, audit); `TopApp.on_hub_event` nghe `learning.applied` → `notify(summary)` ("X learned: …" + lệnh rollback).
- Test: `tests/test_tui_learnings.py::test_h16_learning_off_key_ledger_by_agent_feedback_and_learned_toast`.

## H-09 — retention 30 ngày (NFR-28, Q-10, Q-17)
- Migration **v12**: bảng `retention_cutoff`; trigger `events_no_delete` chỉ cho xoá hàng có `ts` < mốc `retention_cutoff('events')`
  (không có mốc → vẫn append-only tuyệt đối).
- `recovery.purge_retention(hub, now)`: xoá `events` (gồm transcript) quá `retention.events_days`; xoá `runs/<id>.jsonl(.gz)`,
  `.stderr.log` quá `retention.raw_days` (trừ run đang chạy); xoá mềm quá `purge_after`: thư mục `.archive` của skill
  (state → `deleted`, vẫn khôi phục được từ blob ledger) và thùng rác team (`trash_path`). Giữ: `runs` (cost), ledger/snapshot/
  blob, `audit_log`, `guard_decisions`, `signals`. Có xoá → audit `retention.purged`. Gọi từ `purge_expired` (vòng nền approvals)
  tối đa 1 lần/`retention.interval_s` (3600).
- Config mới (`retention.*`, chỉ thêm): `events_days: 30`, `raw_days: 30`, `interval_s: 3600`; 0/null = vĩnh viễn.
- Test giả thời gian: `test_h09_retention_purges_old_data_keeps_aggregates` (now + 31 ngày), `test_h09_purge_expired_runs_retention_throttled`.
- `checkpoints/<run>/` (yêu cầu bổ sung của coordinator): xoá sau `retention.checkpoints_days` (30) tính từ `ended_at`.
  **Quyết định**: cửa sổ `run revert` = 30 ngày — không giữ vô hạn checkpoint của run "còn revert được" (mọi run hoàn tất đều
  revert được nên ngoại lệ đó sẽ làm retention vô nghĩa). GIỮ checkpoint của run `queued|running|waiting_human|interrupted`
  (resume/`resume_conflicts` cần). Sau khi xoá: `runs.diff.revertible=false` + `checkpoint_purged_at` (danh sách file/stat còn);
  `run revert` báo lỗi rõ "đã bị xoá theo retention". Test `test_checkpoints_retention_keeps_live_and_recent`.
- Còn lại: `logs/` daemon chưa xoay vòng.

## NFR-22 — LLM judge qua `AgentRuntime.oneshot` (yêu cầu bổ sung của coordinator, sau H-20 của CLI)
- `evals/judge.py`: bỏ spawn `claude` trực tiếp; gọi `ClaudeCliRuntime({**runtime.claude, "binary": eval.judge.binary}).oneshot(
  OneShotRequest(schema=JUDGE_SCHEMA, system=JUDGE_SYSTEM, …))` như `curation/llm.py` → cùng cô lập (settings tắt auto-memory, MCP
  rỗng + `--strict-mcp-config`, `--setting-sources ""`, `--tools ""`, env_strip), giữ `--json-schema`/`--no-session-persistence`,
  thêm kiểm ma trận cờ. Vẫn ghi dòng `runs kind=eval_judge` (ngân sách ngày). `build_judge_argv` giờ ủy quyền cho `oneshot_argv`.
- Test: `test_judge_goes_through_runtime_oneshot` (chặn `create_subprocess_exec`), các test judge cũ (`test_eval_golden.py`) xanh.
- Chưa làm (dev-notes CLI mục 2): `run_logger` cho log dispatcher/recovery.

## Phối hợp với luồng khác
- File dùng chung đã chạm (chỉ thêm): `store.py` (v12), `config.py` (`retention.*` mới; đổi mặc định `system_prompt_snapshot`),
  `tui/app.py` (binding `f`, toast). Không sửa `runtime/`, `guard/`, `cli.py`.
- Pytest cuối: 383 passed, 2 đỏ thuộc SEC (`test_eval_golden::test_guard_forbids_agent_golden_writes` — guard đang sửa;
  `test_sec_auth::test_agent_mutating_calls_are_audited_as_agent` — xanh khi chạy riêng). Không sửa `guard/`.
- Trong lúc chạy, daemon từng không khởi động do SEC đang thêm `guard/integrity.py` (đã có lại) — không phải lỗi của LRN.
- Thư mục `home-ln-*` ở gốc repo là sản phẩm của script QA (`repro_*.py` tạo home theo cwd), không phải của luồng này — nên xoá.
