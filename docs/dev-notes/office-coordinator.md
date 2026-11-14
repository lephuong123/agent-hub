# Văn phòng 3D — Điều phối viên là nhân vật chính, ngồi bàn ra lệnh, nói chuyện với agent, agent ít đi lại

Chủ hub (2026-11-03), nâng tiếp chế độ đi bộ (`office-walk.md`, PR #54). Nhánh `feat/office-coordinator`, PR xếp chồng trên `feat/office-walk`.
Client: `static/office.js`, `static/office/{walk,actors,chibi,chat,flows,talk}.js` (talk.js mới). Server: chỉ mở rộng danh sách trắng của
`POST /office/do/{name}` trong `web/office.py`. Không thêm endpoint, quyền, RPC hay thư viện mới.

## 1. Nhân vật chính = Điều phối viên
- Bật 🚶: người dùng điều khiển chibi **Điều phối viên** (agent `is_manager`, id = `state.manager_id`, hiện là `dieu-phoi`): nhãn tên + màu đỏ
  `#c8413a` + dáng điều phối viên (`Walker.setIdentity`). Chưa có điều phối viên → avatar "Bạn · chủ hub" như cũ (xuất hiện ở `spawnPoint`).
- **Không bao giờ 2 điều phối viên**: mỗi khung `office.js` đặt `a.puppet = a === coordinator()` khi đi bộ; `Actor.update` ẩn chibi của
  actor puppet (`group.visible = !puppet`) nhưng vẫn chạy máy trạng thái → bong bóng/vòng đỏ trạng thái run (đang chạy, chờ duyệt ✋, đang
  hỏi ❓…) chép sang avatar (`walk.setStatus(coord.bubble)`); thẻ đỏ "cần duyệt" bám theo avatar. Actor puppet không bấm được, không có nhãn,
  không tính "agent gần". Tắt đi bộ → `puppet = false`, actor AI hiện lại.
- Điểm xuất hiện (`doorSpawn`, từ 2026-11-04): ngay ngoài cửa phòng điều phối viên, nhìn vào giữa văn phòng (đứng cạnh bàn thì camera góc
  thứ ba kẹt sát tường bắc). Không được → `deskSpawn` (chỗ đứng `stand` cạnh ghế giám đốc; bị chặn → ô trống gần nhất còn trong phòng) → `spawnPoint`.

## 2. Ngồi bàn → ra lệnh
- Đứng cách ghế ≤ `SIT_R` = 1,3 m → gợi ý "[E] Ngồi vào bàn (ra lệnh)". E → `walk.sit(world.mgrSpot)` (tư thế gõ phím, khoá phím đi / bấm sàn,
  camera sau vai nhìn vào màn hình bàn) + `chat.open({ desk: true })`: màn hình bàn thành **bàn điều khiển** — đúng `chat.js` của màn hình
  lớn (cùng `/office/chat`, `POST /manager/send`, SSE `/sse/office/chat`, CSRF + khoá phiên, `<textarea>` thật cho IME) nhưng phủ trước camera
  bố cục rộng 1600×914 (`setOverlay(camera, wide)`), thay vì treo trên vách.
- **Esc** (đóng bàn điều khiển → `onOpenChange(false)` → `walk.stand()`), **E** khi không gõ chữ trong ô chat, bấm ra ngoài bảng, hoặc
  "✕ Đóng" → đứng dậy ở chỗ đứng cạnh ghế. E trong ô chat là chữ "e" (không đứng dậy khi đang gõ).

## 3. Gặp agent → nói chuyện (`office/talk.js`)
- Đứng gần agent (≤ 1,8 m, như PR #54) → "[E] Nói chuyện với <agent>" → bảng hội thoại phủ trước camera (phong cách office, 1500×900;
  màn dọc 760×1240). Agent đang nói chuyện (`actor.talking`) **đứng/ngồi yên** tại chỗ (không đổi mục tiêu, không đi; đang đứng giữa đường thì
  đứng lại) và quay mặt về người dùng nếu đang đứng; bong bóng 💬.
- **Chọn action (quyết định của team, `talkPlan`)**, chỉ 3 action run sẵn có của 2D qua `POST /office/do/<name>`:
  1. agent đang chạy (`working` / `waiting` / `waiting_human` / `asking`) và có `run_id` → **`steer`** vào run hiện tại;
  2. agent rảnh và hội thoại này (trong lần mở trang) đã bắt đầu một run → **`run_continue`** run đó (giữ ngữ cảnh, tạo run MỚI);
  3. còn lại → **`run_start`** run mới. Không tự "tiếp" một run cũ không do hội thoại này bắt đầu (vd. run của task, của lịch) — tránh
     chen ngữ cảnh lạ; muốn tiếp run cũ thì dùng trang run 2D.
- **Tiền**: dòng báo trên bảng luôn nói tin kế sẽ "chen vào run đang chạy" hay "bắt đầu run mới cho <agent> — có thể tốn tiền". **Mỗi lần
  bắt đầu run mới** (run_start / run_continue) và lần steer đầu **cho mỗi run** (`confirmKey` = `steer:<run_id>`): server trả `need: "confirm"` với thông điệp "Sẽ bắt đầu run mới cho <agent> (có thể tốn tiền; budget và
  giới hạn của agent vẫn áp dụng). Gửi?" → hộp xác nhận 3D (`flows.ask`), chỉ khi NGƯỜI bấm "Đồng ý" mới gửi `confirm=yes`. Budget/giới hạn
  do daemon áp như mọi run.
- **Câu trả lời**: `run_id` trả về (run mới, hoặc run được steer) → `EventSource('/sse/run/{id}')` sẵn có. talk.js CHỈ đọc khung `delta`
  (`k === 'text'`, chữ thô), `end` và `item.kind` (hàm thuần `replyFrame`; steer: luồng phát lại cả run → khung `item` kind `steer` (user.message)
  mở đoạn mới, đoạn cuối là trả lời sau tin của bạn; `refresh` — run.completed / duyệt / câu hỏi — KHÔNG xoá chữ). Luồng lỗi → nối lại
  lùi dần 1 s → 30 s (`retryDelay`, tối đa 6 lần) với `?since=<seq đã thấy>` (không phát lại, không nhân đôi chữ); hết lượt mới báo "Luồng
  trả lời đã đóng". Gửi tin mới vào run đang nghe → chốt đoạn trả lời trước thành một tin. Bảng hội thoại `renderOrder = TALK_ORDER` (990),
  dưới nền mờ / bảng modal (1000/1001) — bảng duyệt mở từ đây luôn nằm trên. Văn phòng dựng lại khi đang ngồi → bám `mgrSpot` của World mới; trường HTML của khung `item` **không bao giờ đọc**. Chữ agent chỉ `fillText` (sr-only: `textContent` qua Panel).
- Agent có approval / câu hỏi đang mở → bảng hội thoại có nút "🛡 Duyệt…" / "❓ Trả lời…" mở đúng bảng duyệt / trả lời sẵn có (`flows.*`).
- Lịch sử hội thoại chỉ giữ trong trang (Map theo agent, ≤ 100 tin); đóng bảng → luồng trả lời vẫn chạy nền, mở lại thấy tiếp.

### Server (`web/office.py`)
- `OFFICE_ACTIONS` thêm `steer`, `run_continue`, `run_start` → ĐÚNG `app.ACTIONS` cùng tên, cùng `post_guard` (Origin/Sec-Fetch-Site + CSRF +
  khoá phiên `k`, chỉ phiên người đăng nhập) và `plan_action`. 2D không hỏi xác nhận cho 3 action này; 3D LUÔN trả `need: "confirm"` trước khi
  có `confirm=yes` (fail closed: không có RPC ghi nào). `OFFICE_CONFIRM` nhận hàm `(form) → str` (nêu agent / run). Thành công trả thêm
  `run_id` (`RUN_ACTIONS`). `run_resume`, `feedback`… vẫn ngoài danh sách (404); `stop` có từ bảng quản lý agent (`office-agent-board.md`).

## 4. Agent ngồi ở team của mình, ít đi lại (`office/actors.js`, hằng số đầu file)
| Hằng | Giá trị | Ý nghĩa |
|---|---|---|
| `IDLE_DESK_MIN` + rnd·`IDLE_DESK_VAR` | 360 + 0…240 s | rảnh: ngồi bàn team mình 6–10 phút giữa hai lần nghỉ (lần đầu lệch pha ngẫu nhiên) |
| `IDLE_POSE_S` | 35 s | tại bàn đổi qua lại ngả lưng (`relax`) ↔ xem điện thoại (`sitPhone`, tư thế mới + điện thoại trên tay) |
| `BREAK_CAFE_P` | 0,5 | mỗi lần nghỉ: 50 % ra cafe điện tử chơi `BREAK_CAFE_S`…+`VAR` = 40–80 s |
| `BREAK_DRINK_S`…+`VAR` | 15–25 s | còn lại: lấy nước ở quầy cafe / pantry (50/50) |
| `TRIP_DELAY` | 6 s (trước 1,5) | làm việc: tool đọc/Bash phải chạy > 6 s mới đi server/thư viện |
| `TRIP_COOLDOWN` | 90 s (trước 0) | cách chuyến trước ≥ 90 s |
- Khu cafe (#52) giữ nguyên, đủ ghế cho mọi agent; chỉ đổi tần suất. Điều phối viên rảnh vẫn ngả lưng ở phòng riêng.
- Đo headless (19 agent, 2 team, 2 giờ mô phỏng): agent rảnh ngồi bàn 88,5 % (min) / 90,4 % (TB) thời gian; ~14 lần nghỉ / 2 giờ mỗi agent.
  Làm việc kiểu Read 8 s + Edit 12 s lặp lại: 18 chuyến / 30 phút (logic cũ ~90).

## Kiểm tra
- `tests/office_coordinator_check.mjs` (gọi từ `tests/test_office_coordinator.py`): deskSpawn trong phòng điều phối viên, không bị chặn, tới
  được 8/8 phòng (1/5/19/40/80 agent × 0/2/3 team); ngồi bàn khoá di chuyển, dựng lại World khi đang ngồi → ghế mới, đứng dậy ở ô trống; không có điều phối viên → avatar chủ hub;
  **đúng 1 điều phối viên hiện** suốt 2 giờ mô phỏng có bật/tắt đi bộ; idle ≥ 80 % ở bàn; nghỉ thưa; trip thưa; đang nói chuyện thì đứng yên;
  `talkPlan`, `replyFrame` (steer: chuỗi khung có refresh run.completed/duyệt/câu hỏi vẫn giữ câu trả lời cuối — đỏ trên 2f396de),
  `confirmKey`, `retryDelay`, `TALK_ORDER`. Test tĩnh: talk/chat/walk không dựng HTML, talk.js không đọc `.html`, chỉ 3 action trong danh sách trắng + `confirm=yes` chỉ sau
  `o.ask`; office.js ẩn actor puppet và loại khỏi `pick`.
- `tests/test_office_talk.py`: 401 khi chưa đăng nhập, 403 thiếu CSRF / khoá phiên / khác origin, hỏi xác nhận trước (không tạo run), run_start →
  `run_id` + audit kênh web, run_continue → run mới, steer → cùng run + `steer_count`, action ngoài danh sách 404. 3 test đỏ trên 054c70f.
- **Chưa kiểm hình** (run không có trình duyệt): góc camera khi ngồi bàn, cỡ chữ bàn điều khiển / bảng hội thoại, tư thế `sitPhone`, FPS.

## Rủi ro còn lại
- Lịch sử nói chuyện với agent chỉ trong trang (tải lại là mất; run vẫn xem được ở trang run 2D). Không đọc lại transcript cũ để khỏi phải
  thêm endpoint JSON mới.
- Steer: đoạn trả lời tách theo tin người (`item` kind `steer`) — tin steer của NGƯỜI KHÁC vào cùng run cũng mở đoạn mới.
- `flows.ask` bị `modals.clear()` (hết phiên) khi đang mở → promise không trả, nút Gửi kẹt "Đang gửi…" tới khi tải lại (trang đã hết phiên).
- Steer chỉ hỏi xác nhận một lần mỗi run (không tốn tiền thêm ngoài run đang chạy); run mới thì lần nào cũng hỏi.
