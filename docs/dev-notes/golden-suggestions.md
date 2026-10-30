# Đề xuất golden task từ task done — người duyệt một chạm (web + Telegram/Slack + CLI)

## Vì sao
Hub thật có 0 golden task nên eval gate / canary (M4/M5) không bao giờ chạy: mọi thay đổi L3 rơi vào `deferred_no_eval`.
Trước đây chỉ có `golden.suggested` (gate gợi ý run ứng viên vào digest) và người phải tự gõ `golden from-run … --judge …`.
Giờ hub tự dựng bản nháp golden từ task đã xong mà người chấp nhận nguyên trạng; người chỉ bấm Duyệt / Bỏ qua (Q-07: agent đề xuất,
người quyết).

## Hành vi
- `GoldenStore.auto_suggest()` (`evals/golden.py`), vòng nền M4-B gọi mỗi `eval.golden_suggest.every_min` (60) phút; CLI `golden scan`
  gọi ngay. Quét task `status=done`, `done_at` cũ hơn `min_age_h` (6 h — chừa thời gian cho người sửa/hoàn tác) và trong `lookback_days` (14).
- Bộ lọc (lý do bỏ trả về trong `skipped`):
  - `not_leaf`: task gốc đã tách việc hoặc không có agent nhận;
  - `weak_signal`: prompt (`title` + `body`) < `min_prompt_chars` hoặc `result_summary` < `min_result_chars` (20);
  - `secret`: prompt/kết quả có secret (`redact_str` đổi chuỗi, gồm cả giá trị secret hub biết);
  - `no_completed_run`: run làm việc cuối của task không `completed`;
  - `human_touched`: Signal `correction|edit_output|undo|reject|cancel` trên task hoặc run của nó, hay event `run.reverted` / `run.takeover`;
  - `duplicate`: hash prompt chuẩn hoá (chữ thường, gộp khoảng trắng) trùng golden task hoặc đề xuất có sẵn (mọi trạng thái), hoặc run đã dùng;
  - `agent_cap`: agent đã có `max_open_per_agent` (3) đề xuất đang mở; `agent_gone`: agent đã xoá. Mỗi lượt tối đa `max_per_scan` (10).
- Bản nháp: prompt = `title\n\nbody` của task; checker = `llm_judge` với rubric tiếng Anh cố định + `result_summary` làm kết quả tham chiếu.
  Lưu ở `golden_suggestions` (migration **v19**: `id gs_…`, `task_id`, `title`, `prompt`, `checker`, `prompt_hash`, `decided_*`,
  `golden_task_id`; dòng cũ được cấp `id`). Event `golden.suggestion_opened`. Đề xuất KHÔNG tạo golden task.
- Quyết định: `decide_suggestion(id, approve|reject, created_by)` — `_require_human` (actor agent/learning/review/curator/eval_gate/
  regression_monitor bị từ chối), đề xuất phải `open` (quyết lần 2 → `Conflict`). Duyệt → `add()` (golden thật, tag `auto_suggested`,
  `source_run_id`) + đặt `not_before=now` cho mọi proposal `deferred_no_eval` để `pipeline` thử lại ở tick kế tiếp (thay vì chờ tới 1 giờ).
  Từ chối → `dismissed` (không gợi ý lại). Mỗi quyết định: audit `golden.suggestion_decided` (actor `user:<kênh>`) + event cùng tên.
  Đề xuất cũ của gate (theo run, chưa có nháp) cũng duyệt được: nháp dựng từ `runs.prompt` + `runs.summary`, cùng bộ lọc secret.
- Eval gate / canary đọc `active_for(agent)` nên thấy golden mới ngay; không cần sửa gate.

## Bề mặt
- RPC: `golden.suggestions`, `golden.suggestion_show`, `golden.suggestion_approve`, `golden.suggestion_reject`, `golden.suggest_scan`.
  Approve/reject/scan nằm trong `auth.HUMAN_ONLY` (hậu duệ run bị từ chối, kể cả khi đã đọc được token) và `MUTATING_B` (audit theo kênh).
- CLI: `cfharness golden suggestions [agent] [--all]`, `golden approve <id>`, `golden reject <id>`, `golden scan`.
- Guard: `cfharness golden approve|reject` là `policy_change` forbidden với agent (`golden suggestions` chỉ đọc vẫn được);
  tool MCP `*golden_(approve|reject|suggestion_approve|suggestion_reject)` forbidden dưới mọi tên server. Không thêm tool MCP ghi.
- Web: mục "Đề xuất golden task" ở trang Học (`learnings.html`), nút Duyệt / Bỏ qua (action `golden_suggestion_approve|reject`, CSRF như
  mọi action, không hộp xác nhận — một chạm).
- Telegram/Slack: notifier nhận `golden.suggestion_opened` → `ChatBot.send_golden_suggestion`: tin nội dung (đề bài + kết quả do agent
  viết, trong `<pre>` sau `AGENT_FRAME`) và tin hệ thống có nút `gsa`/`gsr` qua `CallbackStore` (nonce một lần + HMAC + đúng chat, người
  bấm phải trong allowlist — y như approval). Bấm → `call()` → RPC HUMAN_ONLY, audit `<kênh>.command`. `golden.suggestion_decided`
  gỡ nút và sửa tại chỗ tin ở MỌI chat. Cấu hình có `golden.suggested` trong `notify.<kênh>.events` tự nhận cả event mới
  (giống `approval.escalated`).

## Test
`tests/test_golden_suggestions.py` (15 ca, cả 15 đỏ trên code cũ): bộ lọc (sửa/hoàn tác/secret/yếu/trùng/còn mới/run lỗi), trần + tắt,
duyệt tạo golden + đánh thức `deferred_no_eval` + audit, từ chối không gợi ý lại, 6 actor không phải người bị từ chối, HUMAN_ONLY +
guard CLI/MCP, RPC kênh web + caller agent bị chặn, parser CLI, tick nền theo giờ, Telegram một chạm (người lạ/chat khác/replay bị từ
chối, chat còn lại được gỡ nút).

## Việc dở / câu hỏi
- Rubric mặc định là LLM judge so với `result_summary` — rẻ nhưng lỏng. Task chạy trên worktree/repo (đa số task hub-dev) không seed
  lại file vào workspace eval, nên judge chỉ chấm chữ trả lời. Có thể cần nút "Duyệt + sửa cách chấm" trên web.
- Slack chưa có test e2e riêng (dùng chung lõi `ChatBot` + `CallbackStore` với Telegram); TUI chưa có màn đề xuất.
