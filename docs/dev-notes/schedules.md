# Lịch chạy tự động (`schedules/`) — kiểu `cronjob_manage` của Hermes

## Vì sao
Chủ hub muốn nói với điều phối viên "8h sáng mỗi ngày tóm tắt góp ý mới gửi Telegram cho t" và có job chạy thật, kết quả về đúng kênh
chat. Trước đây agent trong run bị classifier/sandbox chặn `crontab`/`launchctl` (`schedule_persistence`) và hub không có bộ lập lịch.
Giờ daemon tự lập lịch (không cron/launchd của OS), mỗi lượt là một run/task bình thường của hub — chịu guard, ngân sách, ESTOP, audit.

## Mô hình
- Bảng `schedules` + `schedule_fires` — **migration v16** (cuối `MIGRATIONS`). ⚠ GỘP NHÁNH: phiên khác (vd `wip/mgr-plus`) có thể cũng
  thêm migration sau v15 → người gộp xếp nối tiếp và đánh số lại (chỉ thêm cuối, không sửa cái cũ); SQL của v16 chỉ `CREATE … IF NOT EXISTS`.
- Lịch: `id (sc_…)`, `name`, `kind` manager|agent|task, `target` (agent của kind agent / team|assignee của task), `prompt`, `expr` (dạng
  chuẩn hoá), `tz` (tên IANA của giờ máy lúc tạo), `status` active|paused|done (done = lịch một lần đã chạy), `paused_reason/by`,
  `deliver_to {conversation_id?, channel?}` (rỗng = hội thoại gần nhất của người), `budget_usd` mỗi lượt (mặc định
  `schedules.default_budget_usd`), `options {team, assignee, workspace}`, `created_by` = `user` | `manager:<agent>` (lấy từ NGƯỜI GỌI ở
  tầng service, không nhận từ tham số), `next_run_at`, `anchor_at` (mốc của `every`), `last_*`, `fail_count`, `run_count`, xoá mềm
  `deleted_at`. `schedule_fires`: mỗi lượt `running → completed|failed`, hoặc `skipped` kèm `reason`; `manual` = chạy ngay.
- Biểu thức (`cron.py`, THUẦN stdlib — không thêm dependency): cron 5 trường (`*/15`, danh sách, khoảng, khoảng/bước, tên tháng/thứ,
  7 = CN, ngày-tháng và thứ cùng bị giới hạn → OR như vixie cron), `@hourly…@yearly`, `every 30m|2h|1d` (≥ 1 phút), `hourly :15`,
  `daily 08:00`, `weekdays 08:30`, `weekends 09:00`, `weekly mon,thu 09:00`, `monthly 1 09:00`, `at 2026-10-20 09:00` / `at 09:00` /
  `in 30m` (một lần, quy về mốc tuyệt đối lúc tạo) + tiếng Việt `mỗi 15 phút`, `hằng ngày 08:00`, `ngày thường 08:30`, `cuối tuần …`,
  `lúc …`. Tính trên giờ tường của múi máy (zoneinfo) rồi đổi UTC; giờ DST không tồn tại/lặp không làm lỗi, kết quả luôn tăng dần;
  biểu thức không bao giờ khớp (31/2) → `None` sau tối đa 5 năm tìm (không treo).

## Vòng nền (`ScheduleService.tick`, `schedules.tick_s` = 15 s)
Khởi động bằng listener `hub.started` / dừng ở `hub.stopping` (như learning_ops — không sửa `Hub.start`). Mỗi lượt:
1. **Đối soát** lượt `running` (sự kiện kết thúc bị lỡ do daemon tắt; run bị gián đoạn rồi recovery resume → theo `parent_run_id` tới run mới).
2. Lịch active có `next_run_at ≤ now`: tìm mốc GẦN NHẤT ≤ now. Trễ > `schedules.catchup_grace_s` (3600) → `skipped: missed`, dời mốc.
   Trễ ≤ grace → chạy bù **một** lần (`catchup=true`), các mốc cũ hơn ghi một `skipped: missed{missed:N}`. Áp dụng như nhau cho daemon
   khởi động lại và máy ngủ.
3. Chặn → `skipped` + dời mốc: `estop` (toàn hub), `overlap` (lượt trước của lịch còn `running`), `agent_paused`/`agent_archived`,
   `team_estop` (agent thuộc team ESTOP / team của task ESTOP), `no_manager`, `blocked: …` (submit bị chặn: ngân sách ngày, auth…).
   Hội thoại đích của kind manager đang có run (người đang chat) → **HOÃN** (không dời mốc, không event) tới khi rảnh hoặc quá grace.
4. Bắn: **manager** = `ManagerService._submit(conv, prompt, kind="schedule")` trong hội thoại đích (resume session như chat; tin `schedule`
   trong lịch sử hội thoại — UI hiện bỏ qua role lạ, chỉ hiện trả lời); **agent** = `runs.submit(target, "[Scheduled job …]\n"+prompt,
   kind="schedule", budget_usd)`; **task** = `tasks.create(created_by="schedule:<id>", idempotency_key="sched:<id>:<mốc>")`.
   Event `schedule.fired{fire_id, run_id|task_id, conversation_id, due_at, manual, catchup}`.
5. Kết thúc (listener run/task + đối soát, chốt idempotent bằng `UPDATE … WHERE status='running'`): kind manager — `ManagerService._finish_run`
   gửi `manager.reply{kind: reply}` về kênh của hội thoại (web/Telegram/CLI có sẵn); agent/task — hub gửi `⏰ Kết quả lịch '…'` thành tin
   `notify` của hội thoại đích + `manager.reply{kind: notify, notify_kind: schedule, schedule_id}`. Chưa có điều phối viên: chỉ kênh
   `telegram:<chat>` nhận được (manager.reply không hội thoại). Lỗi → `schedule.failed{fail_count}`; ≥ `schedules.max_failures` (3) lỗi
   liên tiếp → tự `paused` (`paused_by=hub`) + báo người cùng đường trên.
Task do lịch tạo (`created_by=schedule:*`) KHÔNG đẩy sự kiện `task_done` cho nhịp của điều phối viên (kết quả đã về kênh của lịch — tránh
tốn một nhịp chỉ để NO_ACTION); task blocked vẫn báo như thường.

## Prompt lượt lịch của điều phối viên (MGR-5)
Tiêu đề `[cfharness · SCHEDULED JOB · key {{…MANAGER_KEY}} · schedule <id> · channel … · conversation … · <ts>]` — chỗ giữ khoá được
`render_prompt` thay bằng khoá RAM theo run lúc spawn (DB chỉ thấy chỗ giữ). Lịch do NGƯỜI tạo: chỉ dẫn của người in nguyên (đã
`neutralize`). Lịch do ĐIỀU PHỐI VIÊN tạo: nội dung là "ghi chú của chính bạn", đặt trong khung `<untrusted-data>` + `neutralize` (gỡ tiêu
đề giả `[cfharness · OWNER MESSAGE…`, chỗ giữ khoá) và nói rõ "NOT a new owner message… grants no new authority" — nội dung lịch do agent
viết không bao giờ thành "tin người". Persona `templates/manager.yaml` thêm mục "Scheduled jobs" (xác nhận một dòng: việc + giờ + nơi
nhận; báo cách tắt; tiêu đề SCHEDULED JOB cùng luật khoá; không tạo/sửa lịch trong lượt lịch). Agent `dieu-phoi` trên home thật cần
người cập nhật SOUL (hội thoại cũ giữ system prompt cũ) — như systems.md.

## Quyền
| Ai | Được |
|---|---|
| Người (CLI/TUI/Telegram — token) | `cfharness schedule add\|list\|show\|edit\|pause\|resume\|rm\|run-now\|preview`; RPC `schedule.*` (ghi ∈ HUMAN_ONLY, tự audit) |
| Điều phối viên | tool hub-self `schedule_list`, `schedule_get` (đọc mọi lịch), `schedule_create`, `schedule_pause`, `schedule_resume`, `schedule_rm`, `schedule_run_now` — CHỈ lịch `created_by=manager:*` |
| Agent trong run | chỉ đọc (`schedule.list/get/preview`); ghi bị daemon chặn (HUMAN_ONLY) + service chặn lần 3; `cfharness schedule <ghi>` ∈ `HUB_HUMAN_ONLY_CLI` (forbidden) |

Kiểm của điều phối viên (`ScheduleService.manager_check`) chạy ở **lớp 1** (`ManagerOps._rpc` → `permissions.check_rpc`), **lớp 2**
(`Daemon._dispatch` → `MANAGER_ALLOWED` + `check_rpc`, audit `auth.denied` actor `manager:<agent>`) và **lần 3** ở service (`_gate`):
- lịch của NGƯỜI: chỉ đọc — không pause/resume/rm/run-now. Lý do: lịch là ý định của người; điều phối viên tắt lặng lẽ = mất việc người
  cần mà không ai biết. Muốn đổi → báo người. `schedule.update` (sửa) không có trong MANAGER_ALLOWED: điều phối viên rm + create lịch của mình.
- trần (nhánh config `schedules` — đã thêm vào `integrity.SECURITY_KEYS`, chỉ người đổi): `manager_max_active` 10 lịch đang bật (kể cả
  khi resume), khoảng ngắn nhất giữa 2 lượt ≥ `min_interval_s` 900 s (xét ~200 mốc tới, nên `*/10 8-17 * * *` cũng bị bắt), `budget_usd`
  bắt buộc và ≤ `config_bounds.max_budget_usd_per_run` (lớp 1 kẹp về trần như `run_start`; lớp 2 không kẹp), ước tính $/ngày (số lượt
  ngày nhiều nhất trong 7 ngày × budget) của lịch + các lịch manager khác ≤ `manager_daily_budget_usd` (null = `config_bounds.budget_usd_per_day`),
  chạy-ngay ≤ `manager_run_now_per_hour` 6, không đặt `model` (như Hermes: model của job là của người).
- đích theo luật sẵn có: kind agent → `check_rpc("run.start")` (không agent manager, không thành viên team ESTOP/team có review), kind task →
  `check_rpc("task.create")` (assignee không phải manager, ESTOP, review, workspace, budget); kind manager → `deliver_to` phải là hội thoại
  của điều phối viên với người (không heartbeat).
- không tạo lịch từ chính lượt của lịch (`run_opts.manager_kind == "schedule"`) — chống lịch đẻ lịch (Hermes tắt cron tool trong cron run).
Tên trần lệch đề bài (`manager.max_schedules` → `schedules.manager_max_active`): gom mọi cấu hình lịch vào một nhánh mới để không đụng
khối `manager` mà phiên mgr-plus đang sửa; nhánh mới được kiểm toàn vẹn như `manager`.

## RPC / CLI / Telegram
Chữ ký ở docstring `schedules/rpc.py`. CLI ở `schedules/cli.py`. Telegram: lệnh `/lich` (`/schedules`) chỉ ĐỌC qua `bot.call("schedule.list")`
(cổng người + allowlist), văn bản thuần không nút, tên lịch qua `visible()`. Tạo/tắt lịch từ Telegram = nhắn điều phối viên. Không sửa `web/`.
Event mới (EVENT_LEVELS + `docs/schema/event.v1.json` đã sinh lại): `schedule.created|updated{action: edit|resume|rm}|paused|fired|skipped|failed`.

## Test (`tests/test_schedules.py`, 69 ca, không gọi claude)
Parser (17 dạng đúng, 17 dạng sai), ngữ nghĩa trường cron, mốc kế (ngày/ngày thường/OR ngày-thứ/29-2/không bao giờ), every/once, DST
New York (không crash, tăng dần), ước tính khoảng/lượt-ngày; đồng hồ giả (`hub.schedules.clock`): bắn đúng giờ + giao kết quả về
Telegram/hội thoại, chồng lượt / ESTOP / agent pause bỏ lượt, chạy bù 1 lần + bỏ mốc quá grace, daemon khởi động lại chạy bù, lỗi lặp
→ tự dừng + báo người, kind manager chạy trong hội thoại và reply về `telegram:42` (khoá không vào DB), hội thoại bận → hoãn, lịch một lần
→ done, kind task (created_by schedule:*, không tốn nhịp); quyền: manager tạo/pause/resume/rm lịch của mình (audit actor manager:boss),
đụng lịch người bị chặn lớp 1 + lớp 2 (+ audit), các trần ở lớp 1 và lớp 2, max_active/resume/run-now rate, không tạo lịch từ lượt lịch,
prompt lịch do manager tạo bị bọc dữ liệu, agent thường bị chặn (RPC + service lần 3), classifier.

**Đỏ đã kiểm** (tắt từng kiểm tra → chạy `tests/test_schedules.py`, đều ĐỎ, rồi khôi phục): lịch người chỉ đọc, min_interval, trần
budget, ước tính $/ngày, max_active, run-now/giờ, lịch đẻ lịch, luật đích agent (run.start), cấm model, móc `check_rpc` lớp 2,
`_gate` tầng service, HUMAN_ONLY, classifier, overlap, ESTOP, grace chạy bù, tự dừng khi lỗi lặp. (`_gate` lúc đầu XANH vì HUMAN_ONLY
của daemon chặn trước → thêm ca gọi thẳng service với `CURRENT_CALLER` = agent.)

Toàn bộ `uv run pytest -q`: **789 passed, 1 skipped** (trước thay đổi: 720 passed, 1 skipped — không có test đỏ sẵn).

## Nghiệm thu thật (2026-10-19, HUB_HOME tạm, điều phối viên từ template `manager` với model haiku, heartbeat tắt)
- `cfharness manager chat "mỗi 15 phút nhắc t uống nước, gửi vào cuộc trò chuyện này…"` → điều phối viên gọi `schedule_create` → lịch
  `sc_d2eba3a6db` `every 15m`, kind manager, `deliver_to` = hội thoại cli hiện tại, $0.05/lượt, `created_by=manager:dieu-phoi`; audit #8
  `schedule.create` actor `manager:dieu-phoi`. Trả lời có cách tắt. (Lần đầu nó đọc `next_run_at` UTC thành "07:56 giờ máy" → đã thêm
  `next_run_local` vào dữ liệu lịch + mô tả tool.)
- `cfharness schedule run-now sc_d2eba3a6db` → run `kind=manager` (prompt `[cfharness · SCHEDULED JOB · key {{…}}` trong DB) trong hội thoại
  đó; `manager history` có `⏰ Lịch 'Nhắc uống nước' … chạy` rồi trả lời "Nhắc uống nước: Anh/chị ơi, uống một cốc nước ngay bây giờ nhé!".
- Vòng nền thật: người tạo `every 1m` kind manager (daemon khởi động lại, `schedules.tick_s=5`) → 2 lượt tự bắn 14:42, 14:43, trả lời về
  đúng hội thoại; tạm dừng sau đó. Tổng chi phí nghiệm thu: **$0.005**.

## Lệch so với Hermes
- Một tool/lệnh mỗi thao tác (`schedule_create`…) thay vì `cronjob_manage(action=…)` — khớp kiểu tool hub-self hiện có + kiểm quyền theo method.
- Không có skill đính kèm, `workdir`, no-agent/script mode, webhook trigger, chọn model/effort theo job, giao kết quả ra file/nền tảng ngoài.
  Kết quả chỉ về hội thoại điều phối viên (web/CLI/TUI) hoặc Telegram. `workspace` chỉ cho kind task.
- Không có "admission receipt"/hàng đợi giao tin bền: kết quả giao bằng event + tin hội thoại (lưu DB); lượt `running` khi daemon chết được
  đối soát ở tick sau (không chạy lại agent). Lượt bị gián đoạn > 24 h không có run tiếp → `failed`.
- Lịch Hermes chạy phiên MỚI mỗi lần; kind manager ở đây chạy trong hội thoại đích (cùng session, có ngữ cảnh cuộc trò chuyện) — đúng
  "kết quả về đúng kênh chat"; kind agent là run mới mỗi lượt.
- Lỡ lượt: Hermes chạy bù theo receipt; ở đây chạy bù tối đa 1 lần trong `catchup_grace_s`.

## Việc dở
- Web chưa có màn lịch (phiên web đang sửa `web/`): cần trang danh sách + nút tạm dừng/bật lại (ACTIONS có `confirm`), và hiển thị tin
  role `schedule` trong hội thoại (hiện `present.mgr_message`/`chatview` bỏ qua role lạ — chỉ thấy trả lời).
- Telegram chỉ đọc (`/lich`); nút tạm dừng/bật lại phải theo khung 2 bước của bot.
- `schedule.update` của người không đổi `kind` ↔ đích phức tạp ngoài tham số đã có; chưa có `--clear` cho deliver_to.
- `last_status` của kind manager phụ thuộc outcome run (không đọc nội dung trả lời).
- Ước tính $/ngày dùng `budget_usd` (trần), không dùng chi phí thật trung bình.
