# Luồng việc (workstream) — mỗi epic một luồng cho điều phối viên

Chủ hub (2026-11-12): "một sprint nhiều epic, cứ nhắn vào một chỗ, không quản lý được luồng làm việc riêng của từng việc — bị loạn".

## Vì sao (số liệu từ hub.db thật, 2026-11-12)
- 2 hội thoại gánh gần hết việc: `mc_e95355d6b8` (cli, 85 lượt / 8 ngày, 18 mã ticket, ~32 lần người đổi chủ đề) và DM Slack (49 lượt).
- Session xoay ~5 lượt một lần (`background_large`/`idle_large`/`cost`), chỉ mang 6 tin cuối → điều phối viên "quên" epic, nhầm việc
  ("xin lỗi, tôi lại hiểu nhầm"). 33 câu "đến đâu rồi", nhiều lần nhắc lại quy tắc cũ.
- Cùng epic rải ra nhiều hội thoại (Monitor 12, Dreem shot 27). Kết quả tick / báo cáo lịch đổ vào hội thoại đã giao việc bất kể chủ đề.
- Task không có khái niệm epic; nối task ↔ hội thoại chỉ ở `hub_state` (trần 200).

## Mô hình
- Bảng `workstreams` (migration v24): `id ws_…`, `key` (mã epic, tra không phân biệt hoa thường, duy nhất trong luồng đang mở), `title`,
  `sprint`, `status active|closed`, `state_md` (STATE.md ≤ 12 000 ký tự, `state_by`/`state_updated_at`).
- `manager_conversations.workstream_id` (NULL = **Sảnh**), `read_upto` (tin đã xem → `unread`). `tasks.workstream_id` (task con kế thừa
  từ `origin_task_id`), `tasks.manager_conversation_id` (nguồn chính của "hội thoại đã giao task"; bản đồ hub_state giữ cho run).
- Service: `manager/workstreams.py` (`hub.manager.ws`). `TaskService.resolve_workstream(ref)` (id hoặc mã).

## Hành vi
- **Session mới** (hội thoại mới, xoay session, run lỗi) của hội thoại trong luồng: prompt mở đầu bằng `context_block` — luồng, STATE.md
  (khung `untrusted-data`), task mở, task xong gần đây — rồi mới tới `carry_over` 6 tin. Hội thoại Sảnh: `lobby_block` = danh sách luồng
  đang mở + cách gán việc. Mỗi tin người có `· workstream <nhãn>` trong tiêu đề.
- **Tick theo luồng**: `_push` gắn `workstream_id` cho sự kiện (luồng của task, không thì của hội thoại gốc). `_tick` xử lý MỘT nhóm mỗi
  lượt (luồng của sự kiện cũ nhất) trong hội thoại nhịp RIÊNG của luồng (`channel=heartbeat, workstream_id`); nhóm khác chờ lượt sau
  (khoá chống chồng giữ nguyên). Sự kiện không thuộc luồng → hội thoại nhịp chung như cũ.
- **Định tuyến `notify_owner`/câu hỏi** (`ops._target_conv`): run chat → chính nó; có `task_id` → hội thoại đã giao task nếu cùng luồng
  với task, task thuộc luồng mà được giao từ Sảnh → hội thoại của luồng (`home_conversation`: hội thoại người gần nhất của luồng; chưa có
  → tạo hội thoại web cho luồng); nhịp của luồng → hội thoại của luồng; cuối cùng mới đoán.
- **Tool điều phối viên**: `workstream_list|get` (đọc; `workstream_get` cắt ở 13 000 ký tự để đủ STATE), `workstream_create|update|state_set`,
  `conversation_set_workstream` (chỉ gắn hội thoại Sảnh; chuyển giữa hai luồng là việc của người), `task_create(workstream=…)` (mặc định luồng
  của lượt; `none` = ngoài luồng), `hub_overview|task_list(workstream=…)`. Ghi → audit `manager.workstream` + `manager.action` (target
  `workstream:<id>`). Persona `templates/manager.yaml` mục "Workstreams" (chỉ agent tạo mới từ mẫu; agent cũ nhận hướng dẫn qua khối prompt).
- **RPC người** (HUMAN_ONLY): `workstream.create|update`, `manager.conversation_update {title, workstream, archived, read}`,
  `manager.conversation_read` (không audit). Đọc: `workstream.list|get`, `manager.conversations{workstream, lobby, archived}`.
  `manager.chat{workstream}` = hội thoại MỚI trong luồng. Event `workstream.updated`.

## Bề mặt
- **Web `/manager`** (bản 2, 2026-11-12 — chủ hub: "xấu, rối"): hàng TAB luồng ngay trên khung chat (Sảnh · các luồng: chấm trạng thái
  đỏ bị chặn/cần người, xanh đang chạy, xám xong; số tin chưa đọc) + "＋ Luồng" (chỉ MỘT ô tên; bỏ trống → hub đặt "Luồng <từ> <số>";
  tên bắt đầu bằng mã Jira "LH-55166 Monitor" → tách `key`) + "Đã đóng N". Thanh bên trái chỉ còn hội thoại của tab đang chọn.
  `?w=<id|mã>` mở hội thoại web gần nhất của luồng (chưa có → ô nhập mang `w`). Menu ⋯ đầu chat: đổi tên / chuyển luồng / lưu trữ.
  Panel phải: tiến độ (thanh `<progress>` — CSP cấm style inline) + task, "Ghi chú luồng" (STATE.md, nút Sửa), đổi tên / đóng luồng.
  Action: `ws_create|ws_edit (chỉ tên)|ws_state|ws_close|ws_reopen|conv_rename|conv_move|conv_archive`. Daemon cũ không có `workstream.*`
  → trang như trước.
- **Terminal** (`cfharness chat`): `/ws` (danh sách), `/ws <mã>` (sang hội thoại cli của luồng / mở mới), `/ws new <mã> <tên>`,
  `/ws state`, `/ws attach <mã>`, `/ws close`.

## Chưa làm
- Telegram forum topic ↔ luồng; Slack: lưu ánh xạ thread ↔ hội thoại vào DB (hiện `hub_state` 500 mục) và đặt tên thread theo luồng.
- Sảnh tự phân loại tin bằng model (hiện điều phối viên tự gán qua `task_create(workstream)` theo `lobby_block`).
- Digest sprint đầu ngày; văn phòng 3D (`office/chat.js`) chưa nhóm theo luồng; chưa có lệnh CLI `cfharness ws …` ngoài REPL.
- Hội thoại cũ (trước v24) đều ở Sảnh — người chuyển bằng menu ⋯ / `/ws attach`.
