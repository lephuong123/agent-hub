# Rào chắn chi phí còn hở (đề xuất #4, 2026-11-10)

Bịt 5 chỗ tiền có thể rò mà không ai biết. Hàm dùng chung ở `src/cfharness/cost_guard.py`; không migration (mọi cột đã có).
Bất biến giữ nguyên: hết ngân sách chỉ chặn run MỚI (submit/claim) — không giết run đang chạy, không huỷ/từ chối approval đang
chờ (Q-04, có test). Mọi giới hạn là setting.

## 1. Trần ngày riêng của điều phối viên — `manager.budget_usd_per_day`
- Mặc định `null` (không trần riêng). Khi đặt: `RunManager.budget_scopes` thêm scope `manager` (owner = agent điều phối viên đang
  được chỉ định, đã dùng = `cost_today(agent)`), áp CÙNG `limits.budget_usd_per_day` của agent (nếu có) — scope nào chặt hơn thắng.
  Agent cũ (đã bỏ chỉ định) không chịu scope này.
- Như mọi scope: kẹp `--max-budget-usd` của run về phần còn lại, trừ phần giữ chỗ (H-19), hết → `Blocked` (chat/tick/lịch báo lỗi
  như hiện nay), event `budget.warning|exceeded{scope: manager}`.
- Chỉ người: `config.set` ∈ HUMAN_ONLY, nhánh `manager` ∈ `integrity.SECURITY_KEYS`; điều phối viên gọi `config.set` → bị chặn (test).
  Đặt: `cfharness config set manager.budget_usd_per_day 3`. Kiểm hợp lệ: > 0 hoặc null.

## 2. Trần requeue run `interrupted` của task — `tasks.max_interrupted_requeues` (5)
Run task `interrupted` (daemon dừng) không tính lỗi nên trước đây được requeue MÃI. Giờ `_on_work` đếm số run `interrupted` LIÊN TIẾP
theo chuỗi `parent_run_id` (`cost_guard.interrupted_streak`, gồm cả resume do recovery); vượt trần → task `blocked` + event
`task.requeue_exhausted{run_id, interrupted, limit}` (level summary). `0` = không giới hạn (hành vi cũ). Lead/review `interrupted` vẫn đi
`_on_failure` (đã có circuit breaker). Trần riêng, không gộp với `recovery.auto_resume_max_attempts` (run ad-hoc).

## 3. Chi phí run kết thúc KHÔNG có `result`
`crashed`, bị giết khi quá giờ / SIGTERM, `interrupted` lúc daemon dừng (`_finalize_error`): trước đây `cost_usd` trống → ngân sách
ngày và ngân sách chuỗi tự resume (`chain_budget_left`) tính thiếu. Giờ ghi `cost_usd` = ước lượng của adapter
(`ClaudeStreamAdapter.estimated_cost_usd()`, CÙNG công thức `usage.tick` estimated, từ `stream_event` đã thấy) và
`cost_basis = estimated_no_result`; event kết thúc mang cùng số. Không thấy token nào → để trống như cũ. Run có `result` không đổi (số
chuẩn vẫn là `total_cost_usd`). `sessions.cumulative_cost_usd` KHÔNG cộng phần ước lượng: nếu CLI tính phần dở dang vào luỹ kế của lần
`--resume` sau thì run sau có thể đếm trùng phần đó — lệch về phía AN TOÀN (chặn sớm hơn), chấp nhận.
Ước lượng cũng kích `budget.warning`.

## 4. `budget.warning` / `budget.exceeded` vào notify mặc định
- Thêm vào `notify.telegram.events` và `notify.slack.events` mặc định; tin: `notify/slack.format_message` (Telegram dùng lại).
  Tin vượt nói rõ "run mới bị chặn; việc đang chờ duyệt vẫn giữ".
- Ngưỡng `budget.warn_pct` (80) đã có. `budget.exceeded` giờ cũng chỉ phát MỘT lần/ngày/scope ở `RunManager._budget_for`
  (`cost_guard.flag_once`, cùng khoá `hub_state.budget_flags` với dispatcher) — trước đây mỗi lần submit bị chặn đều phát (sẽ spam kênh
  chat khi nhịp điều phối viên thử lại).
- Lưu ý cài đặt cũ: `config.yaml` đã đặt rõ `notify.<kênh>.events` thì danh sách đó thắng mặc định — tự thêm hai loại nếu muốn.

## 5. Ước tính $/ngày của lịch theo chi phí thật
`ScheduleService.fire_cost_estimate(schedule_id, budget)`: trung bình `schedule_fires.cost_usd` của ≤ `schedules.estimate_recent_fires`
(10) lượt xong gần nhất, cần ≥ `schedules.estimate_min_fires` (3) lượt; kẹp ≤ `budget_usd`; thiếu lịch sử (lịch mới) → `budget_usd`
như trước. Dùng trong trần `manager_daily_budget_usd` của điều phối viên (lịch đang xét + các lịch khác). `estimate_recent_fires=0` = luôn theo trần.

## Test
`tests/test_cost_guardrails.py` (12 ca): kiểm setting; manager bị chặn theo trần riêng, agent thường không, cảnh báo/vượt một lần,
điều phối viên không tự nới; approval đang chờ không bị đụng; trần requeue (≤ trần → ready, vượt → blocked + event); streak reset;
crashed có cost ước lượng + vào `cost_today` + `chain_budget_left`; crash không usage → trống; run có result giữ số chuẩn; notify mặc
định + định dạng; ước tính lịch (trần → thật → kẹp → tắt). `tests/m3_fake.py`: thêm `stream_usage`.

## Việc dở
- Trang Cài đặt web chưa có ô cho `manager.budget_usd_per_day` (nhánh `manager` là khoá an toàn, không trong `SAFE_CONFIG`) — dùng CLI.
- Resume sau approval vẫn đi `RunManager.submit` nên bị chặn khi hết ngân sách ngày (hành vi sẵn có cho mọi scope: approval đã quyết
  không bị huỷ, nhưng lượt tiếp tục báo `resume_failed`); có nên miễn trần cho lượt tiếp tục có vé không — chưa quyết.

## Miễn trần ngày cho lượt chạy tiếp sau khi người duyệt (chủ repo quyết 2026-11-14)
- `RunManager.on_approval_resolved`: nếu MỌI approval đã quyết của run là do NGƯỜI (`runs.human_decided`: có `resolved_by`, không phải
  `hub`, không phải `manager:*`) → `submit(budget_exempt=True)` → `_budget_for(exempt_day=True)` bỏ qua trần ngày hub/team/agent/điều
  phối viên (kể cả "đang giữ chỗ"). Cả "cho phép" lẫn "từ chối" đều được miễn (người đã quyết, agent cần chạy tiếp để kết thúc gọn).
- Vẫn áp: trần mỗi run (`max_budget_usd` kế thừa run gốc) và scope `admin` (chính sách root — user không tự miễn được).
- Điều phối viên/hub quyết → không miễn (lượt chạy tiếp bị chặn như cũ, event `error{code: resume_failed}`).
- Khi thực sự dùng miễn trừ (có scope ngày đã hết) → audit `budget.exempt_approved{from_run_id, approvals, by, max_budget_usd}`.
- Run mới bình thường vẫn bị chặn; miễn trừ chỉ cho đúng lượt chạy tiếp đó. Test: `tests/test_cost_guardrails.py` mục 6.
