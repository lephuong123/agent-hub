# Văn phòng 3D — khu "Cafe điện tử" cho agent rảnh

Chủ hub (2026-11-03): văn phòng `/office` cần một khu cafe điện tử riêng; agent nào **rảnh (`idle`)** thì ra đó ngồi máy.
Chỉ đổi client (`static/office/world.js`, `static/office/actors.js`), không đổi server/RPC/CSP, không asset ngoài, không thư viện mới.

## Vị trí & mặt bằng
- Một **dải riêng chèn giữa cụm bàn team và dải phía nam** (pantry / phòng nghỉ / hàng ghế chờ). `_plan` tính `zc` (= `zs` cũ),
  độ sâu cafe `cd`, rồi đẩy `zs` xuống `zc + cd` và `D = zs + 6` → khu cũ chỉ bị tịnh tiến về nam, không đè lên nhau.
  `openZ1` vẫn là mép nam cụm bàn (bảng kanban + đồng hồ trên tường tây giữ vị trí cũ). Camera toàn cảnh tự lùi vì `fitCamera` dùng `W`/`D`.
- Phòng: x từ `2` tới `W − 2` (chừa **lối đi 2 m hai bên ngoài vách tây/đông** để đi cụm bàn ↔ pantry/cửa ra không phải xuyên cafe).
  Vách thấp tím than viền neon RGB, **2 cửa ở vách bắc + 2 cửa ở vách nam** (đầu tây và đầu đông), biển "Cafe điện tử 🎮".
- Bên trong: lối vào + 2 máy arcade sát vách tây; giữa là các **dãy máy** (2 hàng quay lưng, bàn tối viền RGB, màn hình rộng, bàn phím
  sáng, thùng máy hông RGB, ghế gaming lưng cao); phía đông là **quầy nước nhỏ** (quầy + tủ lạnh kính, zone `cafeBar`, 2 chỗ đứng).
- Mọi đồ đạc đăng ký vào `grid` như phần còn lại (vách đệm 0.15, dãy bàn 0.08); ghế không chặn (giống cụm bàn).

## Số chỗ co giãn
- `need = max(6, số agent không phải điều phối viên + 2)` (cùng tập agent dùng để xếp cụm bàn; dư 2 máy).
- Một dãy tối đa `kMax = ⌊(W − 4 − 1.8 − 3.4) / 1.2⌋` máy mỗi hàng (W = 28 → 15, tức 30 chỗ/dãy).
  `rows = ⌈need / 2kMax⌉`, `k = ⌈need / 2·rows⌉` → tổng `2·k·rows ≥ need`; máy căn giữa phần giữa lối vào và quầy.
- Độ sâu `cd = 6 + (rows − 1) · 3.8` m. Ví dụ: 19 agent → 1 dãy × 11 máy/hàng = 22 chỗ; 40 → 2 dãy (44); 80 → 3 dãy (84).
- `layoutKey` đã gồm danh sách agent → thêm/bớt agent dựng lại văn phòng và tính lại số máy.

## Hành vi (`Actor.goal`, nhánh rảnh)
- Agent `idle` (không phải điều phối viên) → zone `cafe`, pose `typeFast`, bong bóng 🎮; màn hình máy đó sáng/nhấp nháy khi đã ngồi
  (`World.playCafe`), tắt khi nhả chỗ.
- Mỗi lượt chơi 25–60 s; hết lượt có **15%** (`CAFE_DRINK`) đi lấy nước: 50% quầy cafe (🥤) / 50% pantry (☕), 14–22 s, rồi quay lại cafe.
  Trung bình ~1 lần mỗi 4–5 phút. Bỏ hẳn việc idle ngồi lại bàn và idle ra sofa.
- Hết chỗ: mục tiêu có `alt` (cafe → sofa phòng nghỉ, quầy ↔ pantry) thử trước khi rơi về fallback bàn; cứ 4 s thử lại khu chính.
- Nhận việc (`working`/…): `wander` bị xoá, zone đổi → `_replan` **release** chỗ cafe (màn hình tắt) và đi về bàn như cũ.
- Giữ nguyên: điều phối viên (ngả lưng phòng riêng), `paused` (ngủ sofa), working/asking/waiting/error/queued, chuyến server/thư viện,
  review (phòng họp), mgrVisit, hàng ghế chờ rate-limit.
- `prefers-reduced-motion` và ESTOP: viền neon + màn hình cafe đứng màu tĩnh (không đổi màu/nhấp nháy); nhân vật vẫn theo luật cũ
  (dịch chuyển tức thời / đứng im).

## Kiểm tra
- `node --check` hai file; script node không trình duyệt (giả `document.createElement('canvas')`) dựng `World` với 1/5/19/40/80 agent:
  số chỗ ≥ số agent, giữ chỗ đủ cho tất cả, cafe không đè cụm bàn/dải nam, A* tới được mọi máy từ bàn, từ máy tới pantry/quầy/bàn,
  từ bàn tới pantry/hàng ghế chờ/cửa ra. Chưa kiểm hình bằng trình duyệt.

## Rủi ro còn lại
- Văn phòng dài thêm ~6 m (mỗi dãy thêm 3.8 m) → toàn cảnh lùi xa hơn, chữ nhỏ hơn trên màn nhỏ.
- ~12 mesh mỗi máy (22 máy ≈ 260 mesh) + 1 vật liệu màn hình riêng mỗi máy; chưa đo FPS trên máy yếu.
