# Tự resume có giới hạn + thống kê run treo (2026-10-30)

Bối cảnh: 14 ngày gần nhất hub thật có 19 run `timed_out` và 9 `interrupted`. Trước đây chỉ run `interrupted` do daemon khởi
động lại được resume (Q-09, `recovery.py`) và KHÔNG có trần — daemon chết lặp lại thì resume mãi. Run ad-hoc `crashed`/
`timed_out`/`rate_limited` thì dừng hẳn. Run của task đã có retry `--resume` + circuit breaker ở dispatcher (FR-TK-05) — giữ nguyên.

## Chính sách (`src/cfharness/auto_resume.py`, `hub.auto_resume`)
- Nguyên nhân tạm thời theo DANH SÁCH CHO PHÉP (`transient_cause`, fail-closed): `crashed` → `process_killed`, `timed_out` →
  `timeout`, `rate_limited` → `rate_limit`, `failed` có lỗi mạng/API 5xx/overloaded (`NETWORK_PATTERNS`) → `network`,
  `interrupted` → `daemon_restart`. Không bao giờ: `budget_exceeded`, `blocked`, `cancelled` (người dừng, ESTOP dừng run),
  `protocol_violation`, `failed` khác (guard từ chối, chưa đăng nhập, lỗi thường).
- Phạm vi listener (`run.failed` nguồn hub): run KHÔNG gắn task, `kind ∈ recovery.auto_resume_kinds` (mặc định `["adhoc"]`;
  manager tự mở session mới khi run lỗi — MGR, eval/review/lead không resume).
- Trần `recovery.auto_resume_max_attempts` (3) đếm lượt resume LIÊN TIẾP theo chuỗi `parent_run_id` (`resume_reason`
  `auto_resume:*` hoặc `daemon_restart`). Dùng CHUNG cho `recovery.resume_interrupted` → daemon restart lặp lại cũng dừng.
  Hết lượt: event `run.auto_resume{status: exhausted}` + audit.
- Backoff `auto_resume_backoff_s × 2^lượt` (30 s, trần `auto_resume_backoff_max_s` 900 s); rate limit chờ tới thời điểm reset
  đã biết (`dispatcher._run_reset`). Lịch hẹn ở `hub_state.auto_resume_pending` → `Hub.start` hẹn lại sau restart.
- Lúc tới hạn (`fire`) kiểm lại: tắt, ESTOP, agent tạm dừng/xoá, session bận (người đã `run continue`), run đã có
  `continued_in_run_id`, file của run bị sửa (`resume_conflicts`) → `skipped`. Idempotent: daemon luồng đơn, kiểm → `submit` →
  ghi `continued_in_run_id` không có `await` xen giữa; `fire` lần hai bỏ. Ngân sách/ESTOP/đăng nhập: `RunManager.submit`
  (HubError → `skipped`); đồng thời: run mới vào hàng đợi chung. Ngân sách run giữ `max_budget_usd` của run cũ.
- Run mới: `--resume <session cũ>` + lời nhắc `AUTO_RESUME_PROMPT` (tiếng Anh, nêu nguyên nhân), `resume_reason=auto_resume:<cause>`;
  run chưa kịp start → chạy lại prompt gốc. Event trên run CŨ: `run.auto_resume{scheduled|started|skipped|exhausted, cause,
  attempt, max_attempts, delay_s, new_run_id}` + `run.resumed`; audit `run.auto_resume` (actor=hub) mỗi bước.
- Cấu hình (`recovery.*`): `auto_resume_enabled`, `auto_resume_kinds`, `auto_resume_max_attempts`, `auto_resume_backoff_s`,
  `auto_resume_backoff_max_s`; hai cái đầu/ba có ở trang Cài đặt.

## Thống kê run treo (thẻ Sức khoẻ)
RPC đọc `report.hangs{since=14d, top}` → `auto_resume.hang_stats`: run `timed_out|interrupted|crashed` theo outcome, agent,
kind, **tool đang chạy dở** (tool.start cuối chưa có tool.complete cùng `tool_id`), khoảng thời lượng (started→ended), và số run
tự resume. Thẻ Sức khoẻ (`/partials/health`): dòng "Run treo / quá giờ (14 ngày)" (cảnh báo vàng) + bảng gập theo từng nhóm.
Lưu ý: event cũ hơn `retention.events_days` đã bị xoá → cột tool là "(không có tool dở)".

## Test
`tests/test_auto_resume.py` (phân loại danh sách cho phép; crash/timeout/mạng → resume cùng session; trần + exhausted; budget/
lỗi thường/người huỷ/task/manager/tắt → không; ESTOP lúc tới hạn → skipped; idempotent; lịch hẹn qua restart; trần chung với
recovery; `hang_stats`), `tests/test_web_pages_extra.py::test_health_card_shows_hang_stats`.

## Việc dở / câu hỏi
- Run của task: dispatcher vẫn requeue `interrupted` không giới hạn (không tính lỗi); có nên áp trần chung không?
- `timed_out` được coi là tạm thời (đa số do treo mạng/tool); nếu run quá giờ vì việc quá lớn sẽ tốn tối đa 3 lượt nữa.
