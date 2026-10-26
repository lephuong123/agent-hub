# Học bị `deferred_rate_limited` cả ngày dù thay đổi đã bị rollback (fix/learn-rate-rollback)

## Sự cố (hub thật, 2026-10-26, agent `web-coder`)
- 02:49–03:08Z: 5 thay đổi học được áp dụng → đủ `rate.max_changes_per_agent_per_day` (5) sau 20 phút.
- 03:45–03:57Z: regression monitor rollback 3 trong 5 (cùng một lý do "chi phí/run 0.48 → 0.81 USD").
- Mọi proposal sau đó (12) vẫn `deferred_rate_limited` tới nửa đêm; 5 fact memory bị `superseded` (mất).

## Nguyên nhân gốc
1. `Pipeline.rate_check` đếm cả entry đã rollback (chỉ loại `op='rollback'`, không loại `rolled_back_by IS NOT NULL`).
2. Regression monitor báo nhầm: chi phí so bằng TRUNG BÌNH trên 3 run (một task $2.06 kéo lệch), `unevaluated_factor`
   hạ ngưỡng chi phí 50% → 25%, mốc gồm cả run bị huỷ; và sau rollback, cửa sổ các entry còn lại vẫn chứa đúng những run
   đó → tick sau gỡ tiếp (dây chuyền 3 rollback cho một đợt).
3. `_supersede` so theo `target_ref` — mọi memory add đều là `MEMORY.md` → fact mới thay fact khác đang chờ; patch skill
   khác `old_string` cũng thay nhau.
4. Review định kỳ vẫn chạy khi agent đã hết suất → chỉ đẻ proposal bị hoãn (tốn tiền, dồn sang hôm sau).

## Sửa
- `pipeline.rate_check` / `agent_quota_full`: 3 câu đếm hạn mức bỏ entry có `rolled_back_by`. Cooldown theo đích vẫn tính
  entry đã rollback (không áp lại ngay vào chỗ vừa bị gỡ).
- `ledger._wake_rate_deferred`: rollback đặt `not_before=now` cho proposal `deferred_rate_limited/rate_limited` của cùng
  agent (hoặc tầng team/global) → tick kế tiếp thử lại.
- `pipeline._replaces`: memory — chỉ thay khi cùng fact (`_norm` nội dung) hoặc sửa/xoá cùng `old_text`; skill — add/merge/
  archive/delete thay mọi thứ đang chờ, patch chỉ thay patch cùng file + cùng `old_string` (patch ghi cả file thay patch cũ);
  config giữ như cũ (cùng tập khoá).
- `pipeline.retry(revive=True)` + `cfharness learn retry <id> --revive`: khôi phục proposal `superseded`.
- `retry` của proposal hoãn mà đích đã biến mất (`not_found`, vd skill bị rollback) → `superseded`, `block_reason=target_gone`,
  không phát `guard.blocked`.
- `ReviewScheduler._try_start`: job `counter` khi agent hết suất → giữ `queued`, `not_before = min(nửa đêm, +1 h)`,
  `reason="chờ suất học: …"`. Signal mạnh / `refine` vẫn chạy.
- `RegressionMonitor`: chi phí so TRUNG VỊ (bỏ run `cancelled`), `min_runs_cost` 3 → 5, `unevaluated_factor` không áp cho chi
  phí; sau rollback cửa sổ các watch còn lại chỉ tính run sau thời điểm rollback (một đợt = một rollback); vi phạm CHỈ chi phí
  trên thay đổi memory/skill/user_profile → `regression.detected{cost_only: true, rolled_back: false}` một lần, tiếp tục theo dõi.

## Chưa làm (cần chốt — đổi BRD FR-LN-18/Q-19)
- Hạn mức riêng cho memory add L1 (tách khỏi 5 thay đổi/ngày).

## Khôi phục dữ liệu hub thật sau khi deploy
- 6 proposal `superseded` ngày 2026-10-26 của `web-coder`: `cfharness learn retry <id> --revive` (guard + trùng lặp lọc lại).

## Test
- `test_learn_pipeline.py`: `test_rollback_refunds_rate_quota_and_wakes_deferred`, `test_memory_adds_do_not_supersede_each_other`,
  `test_skill_patch_supersedes_only_same_edit_then_revive`, `test_deferred_patch_on_rolled_back_skill_is_dropped_quietly`,
  `test_counter_review_waits_while_quota_full`.
- `test_regress_monitor.py`: `test_cost_uses_median_and_needs_enough_runs`, `test_one_regression_rolls_back_one_change_not_a_cascade`,
  `test_cost_only_on_knowledge_change_warns_once_no_rollback`.
- Cả 8 test đỏ trên code cũ, xanh sau sửa.
- `test_hardening_lrn.py::test_h21_lock_race_second_task_never_spawned` chập chờn theo tải máy — đỏ cả trên `main` khi chạy riêng.
